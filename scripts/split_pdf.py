#!/usr/bin/env python
"""按页码、页段或章节把 PDF 拆开，供人 / LLM 分批处理。

为什么需要它：一本 300+ 页的书，``calibrate/source.md`` 有几十万字。
一次性读完再写成 EPUB，既塞不进上下文，也没法逐段复核。正确做法是按页数或
章节分批：校准一次看一段，撰写一章一个文件。

它跟流水线自己的切分**不是一回事**：

- 流水线的切分（``ingest.plan_chunks``）是机械的——按 ``max_pages_per_task``
  （默认 20 页）切成小分片并行解析，**自动发生，不需要你插手**；切出来的分片带页码
  偏移，流水线保证你看到的是原书页码。
- 这个脚本是给人看的顺手工：先 ``--list`` 看清章节边界和页数，据此决定
  分批的粒度；需要时把书落成小 PDF，单独处理某一章。

所以：**不要**把这里切出来的分片再喂给 ``pdf2epub init``（除非你有意把某一章
单独跑一遍）。拆分结果只用于规划和分批干活。

用法::

    # 1) 先看看有什么：页数、一级目录、按限定会切成几份
    python scripts/split_pdf.py book.pdf --list

    # 2) 按章节切，每份不超过 80 页（章节之间才切，不会切在章中间）
    python scripts/split_pdf.py book.pdf --by-chapters --max-pages 80 -o chunks/

    # 3) 纯按页数切
    python scripts/split_pdf.py book.pdf --by-pages 80 -o chunks/

    # 4) 自己指定页段（1 基，闭区间）
    python scripts/split_pdf.py book.pdf --ranges 1-40,41-120 -o chunks/

    # 只出计划，不写文件
    python scripts/split_pdf.py book.pdf --by-pages 80 --dry-run

产出 ``chunks/<stem>.p0001-0080.pdf`` 与 ``chunks/split-manifest.json``
（每份的起止页码、页数、对应的章标题），后者可以直接喂给 LLM 做分批计划。

需要 ``pypdf``（流水线本来就依赖它）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("需要 pypdf：pip install pypdf")

#: 流水线每片页数。与 pdf2epub.toml 的 max_pages_per_task 保持一致，
#: 只用于在 --list 里提示"解析会切成几片"。
PIPELINE_CHUNK_PAGES = 20
OUTLINE_TITLE_WIDTH = 48


@dataclass
class Part:
    """一份切分结果。``page_start`` / ``page_end`` 都是 1 基闭区间。"""

    index: int
    page_start: int
    page_end: int
    title: str = ""
    chapters: list[str] = field(default_factory=list)

    @property
    def pages(self) -> int:
        return self.page_end - self.page_start + 1

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "file": f"p{self.page_start:04d}-{self.page_end:04d}.pdf",
            "page_start": self.page_start,
            "page_end": self.page_end,
            "pages": self.pages,
            "title": self.title,
            "chapters": self.chapters,
        }


# ---------------------------------------------------------------------------
# 读 PDF
# ---------------------------------------------------------------------------


def open_reader(path: Path) -> PdfReader:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        # 空密码是 PDF 里最常见的"伪加密"
        if reader.decrypt("") == 0:
            sys.exit(f"PDF 已加密且需要密码：{path}（先用 qpdf --decrypt 去掉密码）")
        print("· PDF 使用空密码加密，已自动解密")
    if not len(reader.pages):
        sys.exit(f"PDF 没有任何页面：{path}")
    return reader


def read_outline(reader: PdfReader) -> list[tuple[int, str, int]]:
    """展平书签，返回 ``(层级, 标题, 页码 1 基)``。"""
    try:
        toc = reader.outline
    except Exception as exc:  # noqa: BLE001 - 书签坏了不该阻断拆分
        print(f"· 读取书签失败（{type(exc).__name__}: {exc}），按无目录处理")
        return []

    flat: list[tuple[int, str, int]] = []

    def walk(items: object, level: int) -> None:
        for item in items:  # type: ignore[union-attr]
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            raw = getattr(item, "title", None) or (
                item.get("title") if isinstance(item, dict) else None
            )
            # 空标题（有些书签只用来占位）丢掉，不然目录里多出空行
            title = str(raw).strip() if raw is not None else ""
            if not title:
                continue
            try:
                page_no = reader.get_destination_page_number(item) + 1
            except Exception:  # noqa: BLE001 - 目的页缺失就跳过这一条
                continue
            flat.append((level, title, page_no))

    walk(toc, 1)
    return flat


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------


def plan_by_pages(total: int, per_part: int) -> list[Part]:
    if per_part < 1:
        sys.exit("--by-pages 必须 ≥ 1")
    parts: list[Part] = []
    start = 1
    while start <= total:
        end = min(total, start + per_part - 1)
        parts.append(Part(index=len(parts) + 1, page_start=start, page_end=end))
        start = end + 1
    return parts


def plan_by_ranges(spec: str, total: int) -> list[Part]:
    """解析 ``1-40,41-120`` 这样的页段。"""
    parts: list[Part] = []
    covered: set[int] = set()
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        match = re.match(r"^(\d+)\s*-\s*(\d+)$", piece)
        if not match:
            sys.exit(f"页段写法不对：{piece}（应该形如 1-40 或 41-120）")
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start or end > total:
            sys.exit(f"页段超出范围：{piece}（本书共 {total} 页）")
        overlap = covered & set(range(start, end + 1))
        if overlap:
            sys.exit(f"页段重叠：{piece} 与前面的页段都包含第 {min(overlap)} 页")
        covered |= set(range(start, end + 1))
        parts.append(Part(index=len(parts) + 1, page_start=start, page_end=end))

    if not parts:
        sys.exit("--ranges 是空的")
    gaps = sorted(set(range(1, total + 1)) - covered)
    if gaps:
        print(f"· 提醒：有 {len(gaps)} 页没有被任何页段覆盖（如第 {gaps[0]} 页），这些页不会进产物")
    return parts


def top_level_chapters(
    outline: list[tuple[int, str, int]], level: int
) -> list[tuple[str, int]]:
    """取指定层级的书签作为章边界，按页码升序，去掉倒序的脏数据。"""
    chapters: list[tuple[str, int]] = []
    for depth, title, page_no in outline:
        if depth != level:
            continue
        if chapters and page_no <= chapters[-1][1]:
            continue
        chapters.append((title, page_no))
    return chapters


def plan_by_chapters(
    total: int,
    outline: list[tuple[int, str, int]],
    *,
    level: int,
    max_pages: int,
) -> list[Part]:
    """按章切；给了 ``max_pages`` 就把相邻章节合并成不超过该页数的份。"""
    chapters = top_level_chapters(outline, level)
    if not chapters:
        sys.exit(
            f"这本书没有第 {level} 层书签，没法按章切。"
            "改用 `--list` 看清结构，或者用 `--by-pages` / `--ranges`。"
        )
    if chapters[0][1] != 1:
        chapters.insert(0, ("（卷首：封面 / 目录等）", 1))

    # 章 -> (起始页, 结束页)
    spans: list[tuple[str, int, int]] = []
    for index, (title, start) in enumerate(chapters):
        stop = chapters[index + 1][1] - 1 if index + 1 < len(chapters) else total
        stop = max(start, min(stop, total))
        spans.append((title, start, stop))

    parts: list[Part] = []
    cursor: Part | None = None
    for title, start, stop in spans:
        size = stop - start + 1
        if cursor is None:
            cursor = Part(index=1, page_start=start, page_end=stop, title=title)
            cursor.chapters.append(title)
            continue

        fits = max_pages < 1 or cursor.pages + size <= max_pages
        if fits:
            cursor.page_end = stop
            cursor.chapters.append(title)
            continue

        parts.append(cursor)
        cursor = Part(
            index=len(parts) + 1, page_start=start, page_end=stop, title=title
        )
        cursor.chapters.append(title)
        if max_pages >= 1 and size > max_pages:
            print(
                f"· 提醒：`{title}` 有 {size} 页，超过 --max-pages={max_pages}，"
                "它单独成一份（章内部没法自动切）"
            )

    if cursor is not None:
        parts.append(cursor)

    for part in parts:
        if len(part.chapters) > 1:
            part.title = f"{part.chapters[0]} … {part.chapters[-1]}"
    return parts


# ---------------------------------------------------------------------------
# 展示与落盘
# ---------------------------------------------------------------------------


def print_outline(outline: list[tuple[int, str, int]], level: int) -> None:
    if not outline:
        print("· 这本书没有书签目录，只能按页数切")
        return
    print()
    print(f"书签目录（最多显示到第 {level + 1} 层）")
    print("-" * (OUTLINE_TITLE_WIDTH + 20))
    for depth, title, page_no in outline:
        if depth > level + 1:
            continue
        indent = "  " * (depth - 1)
        label = f"{indent}{title}"
        if len(label) > OUTLINE_TITLE_WIDTH:
            label = label[: OUTLINE_TITLE_WIDTH - 1] + "…"
        print(f"  L{depth}  p{page_no:>4}  {label}")


def print_plan(parts: list[Part], total: int) -> None:
    print()
    print(f"切分计划（共 {total} 页 -> {len(parts)} 份）")
    print("-" * (OUTLINE_TITLE_WIDTH + 24))
    for part in parts:
        label = part.title or "-"
        if len(label) > OUTLINE_TITLE_WIDTH:
            label = label[: OUTLINE_TITLE_WIDTH - 1] + "…"
        print(f"  {part.index:>3}. p{part.page_start:04d}-{part.page_end:04d}"
              f"  {part.pages:>4} 页  {label}")
    sizes = [part.pages for part in parts]
    if sizes:
        print(f"  最大 {max(sizes)} 页 / 最小 {min(sizes)} 页")


def write_parts(
    reader: PdfReader, parts: list[Part], out_dir: Path, source: Path, strategy: str
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_parts: list[dict[str, object]] = []
    for part in parts:
        target = out_dir / f"{source.stem}.p{part.page_start:04d}-{part.page_end:04d}.pdf"
        writer = PdfWriter()
        for page_no in range(part.page_start, part.page_end + 1):
            writer.add_page(reader.pages[page_no - 1])
        with target.open("wb") as handle:
            writer.write(handle)
        writer.close()
        entry = part.to_dict()
        entry["file"] = target.name
        entry["bytes"] = target.stat().st_size
        manifest_parts.append(entry)
        print(f"  {target.name}  {part.pages:>4} 页  {target.stat().st_size / 1e6:.1f} MB")

    manifest = {
        "schema_version": 1,
        "source": str(source),
        "page_count": len(reader.pages),
        "strategy": strategy,
        "parts": manifest_parts,
    }
    manifest_path = out_dir / "split-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按页码 / 页段 / 章节拆分 PDF，供人 / LLM 分批处理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python scripts/split_pdf.py book.pdf --list\n"
            "  python scripts/split_pdf.py book.pdf --by-chapters --max-pages 80 -o chunks/\n"
            "  python scripts/split_pdf.py book.pdf --ranges 1-40,41-120 -o chunks/\n"
        ),
    )
    parser.add_argument("pdf", help="输入 PDF")
    parser.add_argument("-o", "--out", default="chunks", help="输出目录（默认 chunks/）")
    parser.add_argument("--by-pages", type=int, metavar="N", help="每 N 页切一份")
    parser.add_argument("--ranges", metavar="A-B,C-D", help="显式页段，1 基闭区间")
    parser.add_argument("--by-chapters", action="store_true", help="按一级书签切")
    parser.add_argument(
        "--level", type=int, default=1, help="按章切时用第几层书签（默认 1，即一级章）"
    )
    parser.add_argument(
        "--max-pages", type=int, default=0, metavar="M",
        help="按章切时每份不超过 M 页（0 = 不限制，一章一份）",
    )
    parser.add_argument("--list", action="store_true", help="只打印目录与页数信息")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    args = parser.parse_args()

    source = Path(args.pdf)
    if not source.is_file():
        print(f"找不到 PDF：{source}", file=sys.stderr)
        return 1

    reader = open_reader(source)
    total = len(reader.pages)
    outline = read_outline(reader)
    chunks = -(-total // PIPELINE_CHUNK_PAGES)
    print(f"· {source.name}：{total} 页 / {source.stat().st_size / 1e6:.1f} MB"
          f" / 书签 {len(outline)} 条")
    if chunks > 1:
        print(f"· 流水线按每片 {PIPELINE_CHUNK_PAGES} 页解析，会切成 {chunks} 片"
              "并映射回原书页码，这一步不用你操心")

    if not (args.by_pages or args.ranges or args.by_chapters):
        print_outline(outline, args.level)
        print()
        print("下一步二选一：")
        print("  python scripts/split_pdf.py <pdf> --by-chapters --max-pages 80 -o chunks/")
        print("  python scripts/split_pdf.py <pdf> --by-pages 80 -o chunks/")
        return 0

    if args.ranges:
        parts = plan_by_ranges(args.ranges, total)
        strategy = "ranges"
    elif args.by_chapters:
        parts = plan_by_chapters(
            total, outline, level=args.level, max_pages=args.max_pages
        )
        strategy = "chapters" if not args.max_pages else f"chapters<= {args.max_pages}p"
    else:
        parts = plan_by_pages(total, args.by_pages)
        strategy = f"pages/{args.by_pages}"

    print_plan(parts, total)

    if args.list or args.dry_run:
        print()
        print("（--list / --dry-run：没有写任何文件）")
        return 0

    out_dir = Path(args.out)
    print()
    print(f"写出到 {out_dir}")
    print("-" * (OUTLINE_TITLE_WIDTH + 24))
    manifest = write_parts(reader, parts, out_dir, source, strategy)
    print()
    print(f"清单：{manifest}")
    print("拿这份清单规划分批：校准一次看一段，撰写一章一个文件。")
    print("注意：不要把这些分片再喂给 `pdf2epub init`——流水线自己会切分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

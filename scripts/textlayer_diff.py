#!/usr/bin/env python
"""用 PDF 自带的**文本层**当参照，逐页对差 OCR 产物——比逐页看图快，也比看图准。

适用前提：原文 PDF 有可提取的文本层（digital-born，或已经是别人的 OCR 结果）。
这类书根本不该只靠"对着截图看"来校准：文本层就是原书文字，直接拿来当 ground
truth，能一次性把"哪一页漏了内容 / 哪一页多了幻觉"全列出来，剩下的看图工作只
留给表格和插图。

做法：页级比较时**按字符比、忽略所有空白**（OCR 会重排、会在换行处拆词，文本层
还会在斜体切换处把词粘起来，比如 `DrumandFife`，按词比会全是假警报）。用
``difflib`` 找不匹配的片段，超过 ``--min-chars`` 才报，并区分方向：

- ``文本层有 / OCR 没有`` → 漏内容（含页眉页脚这类正常差异，看长度与内容判断）；
- ``OCR 有 / 文本层没有`` → 幻觉、复读、或 markdown 结构（表格/图片 div）。

用法::

    python scripts/textlayer_diff.py --run <run_id>
    python scripts/textlayer_diff.py --run <run_id> --min-chars 40 --out calibrate/layer-diff.md
"""

from __future__ import annotations

import argparse
import difflib
import html
import re
import sys
import unicodedata
from pathlib import Path

try:
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

PAGE_MARK = re.compile(r"<!--\s*=====\s*原书第\s*(\d+)\s*页（(\w+)）\s*=====\s*-->")
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
PUNCT_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00ad": "",
    "\u00a0": "", "\ufb01": "fi", "\ufb02": "fl", "\u2026": "...",
}


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """归一化并记录"归一化后第 i 个字符来自原文第几个字符"，便于回显上下文。

    归一化做的事（两边一致，才谈得上比对）：剥标签、还原实体、NFKC、统一
    引号/破折号/连字符、小写、**去掉所有空白**（OCR 会重排、会在换行处拆词，
    文本层还会在斜体切换处把词粘起来，按词比全是假警报）。
    """
    text = TAG.sub(" ", text)
    text = html.unescape(text)
    out: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        ch = PUNCT_MAP.get(ch, ch)
        for piece in unicodedata.normalize("NFKC", ch).lower():
            if piece.isspace():
                continue
            out.append(piece)
            pos.append(i)
    return "".join(out), pos


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def alnum_len(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def split_pages(paged_md: str) -> dict[int, str]:
    """按页码切开带页码的工作稿。"""
    pages: dict[int, str] = {}
    matches = list(PAGE_MARK.finditer(paged_md))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(paged_md)
        page = int(m.group(1))
        pages[page] = paged_md[start:end].strip("\n")
    return pages


def mostly_present(block: str, other: str, *, ratio: float = 0.8) -> bool:
    """这段的内容在对侧是否基本都在（命中字符数 ≥ ratio × 本段长度）？

    逐页 char-level 对齐会被 markdown 结构（图片 div、表格、行号）挤歪，产出大量
    "其实在，只是位置/写法对不上"的假片段；用**总覆盖率**（而不是最长连续命中，
    后者对夹着行号的代码清单太苛刻）把这类假片段滤掉。
    """
    if not block:
        return True
    if block in other:
        return True
    if ratio * len(block) > len(other):
        found = difflib.SequenceMatcher(None, block, other, autojunk=False).find_longest_match().size
        return found >= ratio * len(block)
    blocks = difflib.SequenceMatcher(None, block, other, autojunk=False).get_matching_blocks()
    matched = sum(b.size for b in blocks)
    return matched >= ratio * len(block)


def slice_raw(raw: str, pos_map: list[int], start: int, end: int) -> str:
    """把归一化坐标 [start,end) 还原成原文片段（带原本的空格、大小写、标点）。"""
    if not pos_map or start >= end:
        return ""
    start = max(0, min(start, len(pos_map) - 1))
    end = max(start + 1, min(end, len(pos_map)))
    return WS.sub(" ", raw[pos_map[start] : pos_map[end - 1] + 1]).strip()


def show(raw: str, pos_map: list[int], start: int, *, width: int = 400) -> str:
    """把不匹配片段还原成可读的一小段（原文大小写与标点都在）。"""
    if not pos_map:
        return ""
    start = max(0, min(start, len(pos_map) - 1))
    raw_start = pos_map[start]
    snippet = raw[max(0, raw_start - 80) : raw_start + width]
    return WS.sub(" ", snippet).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="文本层 vs OCR 逐页对差")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--min-chars", type=int, default=30, help="不匹配片段至少多少个字母数字才报（默认 30）")
    parser.add_argument("--max-per-page", type=int, default=6, help="每页最多列几条")
    parser.add_argument("--out", default=None, help="报告路径（缺省 calibrate/layer-diff.md）")
    args = parser.parse_args()

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    paged = run.calibrate_dir / "source_paged.md"
    if not paged.is_file():
        sys.exit(f"缺 {paged}，先跑 scripts/qc_scan.py --run {run.run_id}")
    pages = split_pages(paged.read_text(encoding="utf-8"))

    try:
        import pypdf
    except ModuleNotFoundError:
        sys.exit("需要 pypdf（pip install pypdf）")

    reader = pypdf.PdfReader(str(run.input_pdf))

    lines: list[str] = [
        "# 文本层 vs OCR 逐页对差",
        "",
        f"参照：`{run.input_pdf}` 的文本层；对照：`calibrate/source_paged.md`。",
        "只列**长度 ≥ {} 个字母数字**的不匹配片段（`<=` 方向=文本层有而 OCR 没有，"
        "即疑似漏内容；`=>` 方向=OCR 多出来的内容）。".format(args.min_chars),
        "",
        "页眉页脚、页码、页边注是正常的单向差异，看内容判断。",
        "",
    ]
    flagged: list[int] = []
    totals = {"missing": 0, "extra": 0}

    for page in sorted(pages):
        if page - 1 >= len(reader.pages):
            continue
        raw_layer = reader.pages[page - 1].extract_text() or ""
        raw_ocr = pages[page]
        layer, layer_pos = normalize_with_map(raw_layer)
        ocr, ocr_pos = normalize_with_map(raw_ocr)
        if not layer and not ocr:
            continue
        sm = difflib.SequenceMatcher(None, layer, ocr, autojunk=False)
        missing: list[tuple[str, int, int]] = []
        extra: list[tuple[str, int, int]] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("delete", "replace"):
                block = layer[i1:i2]
                # 对不齐不等于内容丢了：只在"对侧确实找不到这段"时才报
                if alnum_len(block) >= args.min_chars and not mostly_present(block, ocr):
                    missing.append((block, i1, i2))
            if tag in ("insert", "replace"):
                block = ocr[j1:j2]
                if alnum_len(block) >= args.min_chars and not mostly_present(block, layer):
                    extra.append((block, j1, j2))
        if not missing and not extra:
            continue
        flagged.append(page)
        totals["missing"] += len(missing)
        totals["extra"] += len(extra)
        lines.append(f"## 第 {page} 页")
        lines.append("")
        for block, i1, i2 in missing[: args.max_per_page]:
            exact = slice_raw(raw_layer, layer_pos, i1, i2)
            lines.append(f"- `<=` 文本层有、OCR 没有（{alnum_len(block)} 字）：")
            lines.append(f"  - 原文：{exact if exact else '（空）'}")
            lines.append(f"  - 紧邻上文：…{show(raw_layer, layer_pos, max(0, i1 - 200))}…")
        for block, j1, j2 in extra[: args.max_per_page]:
            exact = slice_raw(raw_ocr, ocr_pos, j1, j2)
            lines.append(f"- `=>` OCR 多出来（{alnum_len(block)} 字）：")
            lines.append(f"  - 产物：{exact if exact else '（空）'}")
            lines.append(f"  - 紧邻上文：…{show(raw_ocr, ocr_pos, max(0, j1 - 200))}…")
        if len(missing) > args.max_per_page or len(extra) > args.max_per_page:
            lines.append(f"- （本页还有更多片段，共 缺 {len(missing)} / 多 {len(extra)}）")
        lines.append("")

    out = Path(args.out) if args.out else run.calibrate_dir / "layer-diff.md"
    if not out.is_absolute():
        out = run.calibrate_dir / out
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"· 运行 {run.run_id}")
    print(f"· 有差异的页：{len(flagged)} / {len(pages)}")
    print(f"· 片段数：文本层有而 OCR 没有 {totals['missing']} 处，OCR 多出来 {totals['extra']} 处")
    print(f"· 报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

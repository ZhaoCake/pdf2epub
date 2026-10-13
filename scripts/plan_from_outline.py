#!/usr/bin/env python
"""按 PDF 书签（顶层目录）给 ``build/compose-plan.json`` 打草稿。

撰写阶段最费神的一步是"切章"：哪一行属于哪一章、章标题叫什么、前面还有几页前置页。
书签里这些信息都有（标题 + 起始页），所以先把草稿生成出来，人只需要复核两件事：

1. **章标题是否被截断/粘连**（解析产物里的标题有时少半句）；
2. **前置页怎么切**（书名页 / 致谢 / 简明目录 / 详细目录 / 图表清单），
   脚本只给一个从第 1 行到第一章的整块，需要人工拆成几个 ``part``。

脚本不改 ``source.md``，只写计划文件；写完用
``python scripts/compose_book.py --run <run> --audit`` 复核。

用法::

    python scripts/plan_from_outline.py --run <run_id>
    python scripts/plan_from_outline.py --run <run_id> --out build/compose-plan.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import pypdf
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

HEAD = re.compile(r"^(#{1,3})\s+(.*?)\s*$")
NONWORD = re.compile(r"[^a-z0-9]+")


def key(text: str) -> str:
    return NONWORD.sub("", text.lower())


def outline_titles(reader) -> list[tuple[str, int]]:
    """顶层书签：(标题, 起始页)。"""
    out: list[tuple[str, int]] = []

    def walk(items, depth=0):
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            if depth:
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                page = 0
            out.append((str(item.title), page))

    walk(reader.outline)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="由 PDF 书签生成 compose-plan 草稿")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--out", default=None, help="输出路径（缺省 <build>/compose-plan.json）")
    args = parser.parse_args()

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    source = run.calibrate_dir / "source.md"
    lines = source.read_text(encoding="utf-8").splitlines()
    headings = [(i + 1, m.group(2)) for i, line in enumerate(lines) if (m := HEAD.match(line))]

    reader = pypdf.PdfReader(str(run.input_pdf))
    entries = outline_titles(reader)

    parts: list[dict] = []
    cursor = 0
    misses: list[str] = []
    for title, page in entries:
        want = key(title)
        hit = None
        for line_no, text in headings:
            if line_no <= cursor:
                continue
            have = key(text)
            if have == want or (len(want) > 12 and (want in have or have in want)):
                hit = (line_no, text)
                break
            if len(want) > 8 and want[:8] == have[:8] and abs(len(want) - len(have)) < 12:
                hit = (line_no, text)
                break
        if hit is None:
            misses.append(f"{title}（书签第 {page} 页）")
            continue
        parts.append(
            {
                "file": f"ch{len(parts) + 1:03d}.xhtml",
                "start": hit[0],
                "end": 0,
                "title": title,
                "heading_in_source": hit[1],
            }
        )
        cursor = hit[0]

    for i, part in enumerate(parts):
        part["end"] = (parts[i + 1]["start"] - 1) if i + 1 < len(parts) else len(lines)
    if parts:
        parts.insert(
            0,
            {
                "file": "ch000.xhtml",
                "start": 1,
                "end": parts[0]["start"] - 1,
                "title": "Front Matter",
                "kind": "title",
            },
        )

    plan = {
        "title": run.source_name.rsplit(".", 1)[0],
        "author": "",
        "language": "en",
        "parts": parts,
    }
    out = Path(args.out) if args.out else run.build_dir / "compose-plan.json"
    if not out.is_absolute():
        out = run.build_dir / out
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"· 书签顶层条目 {len(entries)} 条，落到 {len(parts)} 个 part -> {out}")
    for part in parts:
        print(f"   {part['file']}  行 {part['start']:>6}-{part['end']:<6} {part['title'][:60]}")
    if misses:
        print(f"· 没在正文里找到标题的条目（{len(misses)} 条），需要人工定位：")
        for line in misses:
            print("   -", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

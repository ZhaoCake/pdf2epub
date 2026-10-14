#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""表格**逐行**与 PDF 文本层核对：抓"说明串行/整体错位"。

为什么需要它：``table_tool.py check`` 判的是**结构**（每行格数是否一致、有没有
跨页续表），判不出"结构完好、内容串位"。实测这本书里 3.3 节的开关表就整列往下
串了一行——行数少了、每条说明都挂到了上一行的键上，格数却完全一致，任何结构
检查都看不见。文本层里表格是"一行一条：键  说明"，拿它逐行对，一秒就露馅。

做法：对 ``calibrate/source.md`` 里每个 ``<tr>``，取首格为键、次格为说明；到页
面图对应的文本层里找以键开头的那一行，看说明的头几个词在不在这一行里。为了让
"表在哪一页"更稳，先按"键 + 说明首词"选页，再逐行核对。

用法::

    python scripts/table_row_check.py --run <run_id>
    python scripts/table_row_check.py --run <run_id> --words 3   # 说明要核前几个词
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

try:
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

TABLE = re.compile(r"<table.*?</table>", re.S)
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
WS = re.compile(r"\s+")
WORD = re.compile(r"[A-Za-z0-9_.:#+-]+")


def plain(text: str) -> str:
    text = TAG.sub(" ", text)
    text = html.unescape(text)
    return WS.sub(" ", text).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="表格逐行与文本层核对")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--words", type=int, default=4, help="每条说明核前几个词（默认 4）")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    src_path = run.calibrate_dir / "source.md"
    if not src_path.is_file():
        sys.exit(f"缺 {src_path}")
    src = src_path.read_text(encoding="utf-8").split("\n")

    try:
        import pypdf
    except ModuleNotFoundError:
        sys.exit("需要 pypdf（pip install pypdf）")
    reader = pypdf.PdfReader(str(run.input_pdf))

    layer_cache: dict[int, list[str]] = {}

    def layer_lines(page: int) -> list[str]:
        if page not in layer_cache:
            text = reader.pages[page - 1].extract_text() or ""
            layer_cache[page] = [WS.sub(" ", ln) for ln in text.splitlines() if ln.strip()]
        return layer_cache[page]

    def find_page(key: str, words: list[str]) -> int | None:
        needle = key.replace(" ", "")
        fallback = None
        for page in range(1, len(reader.pages) + 1):
            for cand in layer_lines(page):
                if cand.replace(" ", "").startswith(needle):
                    fallback = fallback or page
                    low = cand.lower()
                    if all(w.lower() in low for w in words[:2]):
                        return page
        return fallback

    checked = problems = 0
    for i, line in enumerate(src, 1):
        if "<table" not in line:
            continue
        page = None
        for row in ROW.findall(line):
            cells = [plain(c) for c in CELL.findall(row)]
            if len(cells) >= 2 and cells[0] and cells[1]:
                head = cells[0].split("\n")[0].strip()
                if head and not head.replace(" ", "").isdigit():
                    page = find_page(head, WORD.findall(cells[1])[:2])
                    if page:
                        break
        if page is None:
            print(f"[行 {i}] 定位不到所在页，跳过（首格为空/纯数字？）")
            continue
        lines = layer_lines(page)
        for row in ROW.findall(line):
            cells = [plain(c) for c in CELL.findall(row)]
            if len(cells) < 2 or not cells[0] or not cells[1]:
                continue
            key, desc = cells[0], cells[1]
            key_head = key.split("\n")[0].strip()
            desc_words = WORD.findall(desc)[: args.words]
            if not desc_words:
                continue
            target = next(
                (c for c in lines if c.replace(" ", "").startswith(key_head.replace(" ", ""))),
                None,
            )
            if target is None:
                print(f"[行 {i}] p{page} 文本层里找不到键：{key_head[:60]!r}")
                problems += 1
                continue
            checked += 1
            tail = WS.sub(" ", target[len(key_head):]).lower()
            if not all(w.lower() in tail for w in desc_words):
                print(f"[行 {i}] p{page} 键 {key_head[:40]!r} 的说明对不上：")
                print(f"        表里：{desc[:80]!r}")
                print(f"        文本层：{target[:120]!r}")
                problems += 1

    print(f"\n核对 {checked} 行，可疑 {problems} 行"
          f"（文本层掉下划线/连字符的行会假警报，逐条看图确认）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

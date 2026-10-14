#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词级对差：把 PDF **文本层**当基准，逐 token 比解析产物／校准稿。

和 ``textlayer_diff.py`` 的分工：那个按字符找 >=30 字母数字的**连续片段**，抓的是
整段漏读/幻觉；短词误读（``Bluetcl``→``Bluetooth``、``-Xl``→``-X1``、
``inlined``→``inclined``）不足 30 字符，它一条都看不见。这里的 token 级比对专抓
这类"短但关键"的误读，是数字版 PDF 校准的第二道网。

文本层不是万能的：LaTeX 的 ``\\_`` 抽出来常变成空格，``pre-\\ndict`` 这种换行连字符
会让文本层少掉原词——所以本脚本报的**两个方向都要看**：

- ``层有/产物无``：可疑漏词、误读、"OCR 自作主张纠正原书笔误"；
- ``产物有/层无``：可疑幻觉，或（更常见）文本层掉下划线/连字符造成的假警报。

用法::

    python scripts/layer_token_diff.py --run <run_id>            # 逐页：层有/产物无
    python scripts/layer_token_diff.py --run <run_id> --extra    # 逐页：产物有/层无
    python scripts/layer_token_diff.py --run <run_id> --source   # 全书级，比当前 source.md
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

try:
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

#: 只剥真正的 HTML 标签：不能用 ``<[^>]+>``，代码里的 ``<-``/``<=`` 会被整段吃掉
TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-$#]*")
PAGE_MARK = re.compile(r"<!--\s*=====\s*原书第\s*(\d+)\s*页（(\w+)）\s*=====\s*-->")
#: 页眉页脚、LaTeX 命令名——不是内容，报出来只是噪声
NOISE = {
    "nbsp", "text", "textsf", "texttt", "mathrm", "mathbb", "begin", "end", "array",
    "quad", "qquad", "boxed", "ldots", "cdots", "prime", "left", "right", "setminus",
    "backslash", "rightarrow", "leftarrow", "overline", "sup", "div", "span", "table",
    "img", "src", "style", "border",
}


def strip_tags(text: str) -> str:
    return TAG.sub(" ", text)


def squash(text: str) -> str:
    """去空白 + NFKC + 小写：用于判断"这个词其实以别的写法在文中出现过"。"""
    text = unicodedata.normalize("NFKC", html.unescape(strip_tags(text))).lower()
    return "".join(ch for ch in text if not ch.isspace())


def tokens(text: str) -> Counter:
    text = unicodedata.normalize("NFKC", html.unescape(strip_tags(text))).lower()
    out: Counter = Counter()
    for raw in TOKEN.findall(text):
        tok = raw.strip("._-$#")
        if len(tok) >= 3:
            out[tok] += 1
    return out


def drop_furniture(layer: str) -> str:
    """去掉文本层的页眉行与页脚行（首尾各一行非空行）。"""
    lines = layer.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(idx) < 3:
        return layer
    return "\n".join(lines[idx[0] + 1 : idx[-1]])


def split_pages(paged: str) -> dict[int, str]:
    pages: dict[int, str] = {}
    marks = list(PAGE_MARK.finditer(paged))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(paged)
        pages[int(m.group(1))] = paged[start:end]
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description="词级对差：文本层 vs 解析产物/校准稿")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--extra", action="store_true", help='列"产物有/层无"的 token')
    parser.add_argument("--source", action="store_true", help="与当前 calibrate/source.md 做全书级核对")
    parser.add_argument("--min-len", type=int, default=3, help="token 最短长度（默认 3）")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    try:
        import pypdf
    except ModuleNotFoundError:
        sys.exit("需要 pypdf（pip install pypdf）")
    reader = pypdf.PdfReader(str(run.input_pdf))
    layer_pages = [reader.pages[i].extract_text() or "" for i in range(len(reader.pages))]

    if args.source:
        # 全书级：拿文本层与**当前的** source.md 比（校准改完用它复核）
        cur = (run.calibrate_dir / "source.md").read_text(encoding="utf-8")
        layer_all: Counter = Counter()
        for text in layer_pages:
            layer_all.update(tokens(drop_furniture(text)))
        cur_tok, cur_sq = tokens(cur), squash(cur)
        lay_sq = squash("\n".join(layer_pages))
        missing = sorted(t for t in layer_all if t not in cur_tok and t not in cur_sq)
        extra = sorted(t for t in cur_tok if t not in layer_all and t not in lay_sq)
        print(f"· 运行 {run.run_id}（对照 {run.calibrate_dir / 'source.md'}）")
        for tok in missing:
            print("   层有/稿无：" + tok)
        print(f"   【层有/稿无】{len([t for t in missing if t not in NOISE])} 个"
              f"（含噪声 {len([t for t in missing if t in NOISE])}）")
        for tok in extra:
            print("   稿有/层无：" + tok)
        print(f"   【稿有/层无】{len([t for t in extra if t not in NOISE])} 个")
        return 0

    paged_path = run.calibrate_dir / "source_paged.md"
    if not paged_path.is_file():
        sys.exit(f"缺 {paged_path}，先跑 scripts/qc_scan.py --run {run.run_id}")
    pages = split_pages(paged_path.read_text(encoding="utf-8"))

    layer_all: Counter = Counter()
    prod_all: Counter = Counter()
    layer_pages_of: dict[str, list[int]] = defaultdict(list)
    prod_pages_of: dict[str, list[int]] = defaultdict(list)
    for page in sorted(pages):
        if page - 1 >= len(layer_pages):
            continue
        lt = tokens(drop_furniture(layer_pages[page - 1]))
        pt = tokens(pages[page])
        layer_all.update(lt)
        prod_all.update(pt)
        for tok in lt:
            layer_pages_of[tok].append(page)
        for tok in pt:
            prod_pages_of[tok].append(page)

    layer_sq = squash("\n".join(layer_pages))
    prod_sq = squash("\n".join(pages[p] for p in pages))

    if args.extra:
        candidates = sorted(
            t for t in prod_all
            if t not in layer_all and t not in layer_sq
            and t not in NOISE and len(t) >= args.min_len
        )
        print(f"· 运行 {run.run_id}｜产物有、文本层无：{len(candidates)} 个 token")
        for tok in candidates:
            print(f"   + {tok:<34} 页 {prod_pages_of[tok]}  ×{prod_all[tok]}")
        return 0

    candidates = sorted(
        t for t in layer_all
        if t not in prod_all and t not in prod_sq
        and t not in NOISE and len(t) >= args.min_len
    )
    print(f"· 运行 {run.run_id}｜文本层有、产物无：{len(candidates)} 个 token")
    for tok in candidates:
        print(f"   - {tok:<34} 页 {layer_pages_of[tok]}  ×{layer_all[tok]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

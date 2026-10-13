#!/usr/bin/env python
"""找出"被页界切断的句子"，产出合并计划（由 ``calibrate_fix.py`` 统一执行）。

解析是**按片、按页**做的：原书里跨过页脚的一句话，在产物里断成两段，EPUB 里就
成了两个段落。``qc_scan.py`` 的"跨页断句候选"只按标点初判，误报不少（图注、代码
清单、清单项都会命中）。这里多加两道关：

1. **文本层验证**：左页尾、右页首在 PDF 文本层的同一位置也对得上，才认为这是真的
   页界续写（解析把行搬错了位置的话，对不上）；
2. **锚点唯一**：左页尾这段文字可能整段在别页重复（本书同一条说明按路径重复 4 次，
   见第 244 页），锚点要往前加字直到全文唯一，否则宁可不合并。

左页以行尾连字符（``SystemVer-``）断开时，一并删掉连字符（``drop_hyphen``）。

只读产物 + 写计划文件，不改 source.md；真正的写入仍由 ``calibrate_fix.py`` 完成
（见 references/pitfalls.md 教训 8：单一入口）。

用法::

    python scripts/join_page_breaks.py --run <run_id>
    python scripts/join_page_breaks.py --run <run_id> --skip 21     # 页界夹了补写内容，手工处理
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibrate_fix import split_pages  # noqa: E402
from qc_scan import BLOCK_START, SENTENCE_END  # noqa: E402

SQUEEZE = re.compile(r"\s+")
#: 左页尾允许的收尾字符：字母数字、逗号、连字符——其余（句号、右括号、美元号等）都不像"被切断"
TAIL_OK = re.compile(r"[A-Za-z0-9,\-]$")
#: 代码清单特征：分号、花括号、赋值/通道运算符、行注释
CODEISH = re.compile(r"[;{}]|<=|//|::|\bend\w*\b|\bbegin\b")


def squeeze(text: str) -> str:
    return SQUEEZE.sub("", text)


def unique_anchor(source: str, body: str, *, min_len: int = 40, max_len: int = 600) -> str | None:
    """从页尾往前取一段"全文唯一"的锚点。"""
    body = body.rstrip()
    for take in range(min_len, min(len(body), max_len) + 1, 20):
        anchor = body[-take:].lstrip("\n")
        if anchor and source.count(anchor) == 1:
            return anchor
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="页界断句合并计划生成")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--out", default=None, help="计划文件（缺省 calibrate/join-plan.json）")
    parser.add_argument(
        "--skip",
        default="",
        help="跳过这些页界（逗号分隔的左页页码）；页界处夹了补写内容、要手工处理时用",
    )
    args = parser.parse_args()

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    paged_path = run.calibrate_dir / "source_paged.md"
    if not paged_path.is_file():
        sys.exit(f"缺 {paged_path}，先跑 scripts/qc_scan.py --run {run.run_id}")
    source = (run.calibrate_dir / "source.md").read_text(encoding="utf-8")
    pages = split_pages(paged_path.read_text(encoding="utf-8"))

    reader = pypdf.PdfReader(str(run.input_pdf))
    layer = {n: (reader.pages[n - 1].extract_text() or "") for n in pages}

    joins: list[dict[str, object]] = []
    skipped: list[str] = []
    manual = {int(x) for x in str(args.skip).replace(",", " ").split() if x.strip().isdigit()}
    for number in sorted(pages):
        if number + 1 not in pages:
            continue
        if number in manual:
            skipped.append(f"第 {number}/{number + 1} 页：按要求跳过（手工处理）")
            continue
        tail = pages[number].rstrip()
        head = pages[number + 1].lstrip()
        if not tail or not head:
            continue
        if tail[-1] in SENTENCE_END or tail.endswith(("$", "）", "】", ".", "，", "、")):
            continue
        if BLOCK_START.match(head) or head[0] in "#*-0123456789ABCD（(":
            continue
        if not TAIL_OK.search(tail) or not head[0].islower():
            skipped.append(f"第 {number}/{number + 1} 页：收尾字符/首字符不像断句（{tail[-12:]!r} | {head[:12]!r}）")
            continue
        # 代码清单也会跨页断开，但那是**按行**的，合并成一段反而把两行代码粘成一行
        if CODEISH.search(head[:60]) or CODEISH.search(tail[-60:]):
            skipped.append(f"第 {number}/{number + 1} 页：看着是代码清单跨页，不合并")
            continue
        # 文本层每页自己带页眉/页脚，所以不能要求"开头就对上"：左页尾按尾部窗口找，
        # 右页首按前部窗口找（页眉也就十几个字）。
        tail_key, head_key = squeeze(tail)[-40:], squeeze(head)[:40]
        if tail_key not in squeeze(layer.get(number, ""))[-500:]:
            skipped.append(f"第 {number}/{number + 1} 页：左页尾在文本层里对不上（{tail[-12:]!r}）")
            continue
        if head_key not in squeeze(layer.get(number + 1, ""))[:400]:
            skipped.append(f"第 {number}/{number + 1} 页：右页首在文本层里对不上（{head[:12]!r}）")
            continue
        anchor = unique_anchor(source, pages[number])
        if anchor is None:
            skipped.append(f"第 {number}/{number + 1} 页：页尾这段全文不唯一，锚不住（{tail[-20:]!r}）")
            continue
        hyphen = anchor.endswith("-")
        joins.append(
            {
                "left_page": number,
                "right_page": number + 1,
                "left_tail": anchor,
                "right_head": head[:60],
                "sep": "" if hyphen else " ",
                "drop_hyphen": hyphen,
                "why": "页界断句，文本层已核对",
            }
        )

    out = Path(args.out) if args.out else run.calibrate_dir / "join-plan.json"
    if not out.is_absolute():
        out = run.calibrate_dir / out
    out.write_text(json.dumps(joins, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"· 运行 {run.run_id}")
    print(f"· 合并计划 {len(joins)} 条 -> {out}")
    print(f"· 跳过 {len(skipped)} 条")
    for line in skipped[:50]:
        print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

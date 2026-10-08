#!/usr/bin/env python
"""表格工具：台账 / 结构体检 / 对图核对清单 / 裁图换图。

表格是扫描书最容易坏的部分，而且坏得很安静：数值多半是对的，坏的是**结构**
（整列丢失、表头错位、整表只剩空壳）和**助记符**（0↔O、1↔I、点号丢失）。
另外 EPUB 里宽表无法重排——阅读器要么把字缩到看不清，要么横向滚几十屏；
而"行多"只是纵向滚动，"字多"反而 HTML 更好（能搜索）。所以宽表和坏表的
正确形态是**图**：坐标用 PaddleOCR 原始 JSONL 里表格块的 `block_bbox`
（页面像素坐标），从渲染的页面图上精确裁下来。

四个子命令对应"表格专项"的四步：

1. ``list``   台账：每张表在哪页、几行几列、表题；按"手机可读"标准标出换图候选。
2. ``check``  结构体检：行内单元格数不一致（rowspan 丢失 → 整行左移）、
   疑似跨页续表、超长单元格（图被读成表）→ ``calibrate/table-check.md``。
3. ``pages``  把含表的页渲染成 2 倍图（``calibrate/table-pages/``）并出对图核对清单
   ``table-manifest.md``——逐张对图核对就照着它来（子代理分章核对效率最高）。
4. ``crop``   裁图：默认按阈值自动选，结构损坏的用 ``--extra 页:序号`` 追加；
   产出 ``calibrate/images/table-pNNNN-oK.png`` + 清单 ``table-images.json``
   （撰写阶段照它把 ``<table>`` 换成 ``<img>``，表题保留为正文）。

用法::

    python scripts/table_tool.py --run <run_id> list
    python scripts/table_tool.py --run <run_id> check
    python scripts/table_tool.py --run <run_id> pages
    python scripts/table_tool.py --run <run_id> crop
    python scripts/table_tool.py --run <run_id> crop --extra 325:1,497:1
    python scripts/table_tool.py --run <run_id> crop --only 50:1,149:1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from qc_scan import read_pages
except ModuleNotFoundError:
    sys.exit("请在 scripts/ 所在仓库里运行（与 qc_scan.py 同目录）")

try:
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

TABLE = re.compile(r"<table.*?</table>", re.S)
ROW = re.compile(r"<tr.*?</tr>", re.S)
CELL = re.compile(r"<td.*?</td>", re.S)
TAG = re.compile(r"<[^>]+>")

#: "手机上还能不能读"的容忍度：列数/行数/格子数超标 → 建议换成图。
#: 判据只看**宽度**：行多只是纵向滚动，字多 HTML 反而能重排能搜索。
MAX_COLS = 8
MIN_COLS_FOR_TALL = 6  # 又宽又长：缩到屏宽后每格只剩几个像素
MIN_ROWS_FOR_TALL = 15
MIN_CELLS = 150

#: 表题：表格 HTML 前不远处最后一个"表N-N ..."样式的小段
CAPTION = re.compile(r"(表\s*[\dA-Za-z.\-]+[^\n<]{0,60})")


def needs_image(cols: int, rows: int) -> bool:
    """该换成图吗？列 ≥9；或 列 ≥6 且行 ≥15；或格子数 ≥150。"""
    if cols >= MAX_COLS + 1:
        return True
    if cols >= MIN_COLS_FOR_TALL and rows >= MIN_ROWS_FOR_TALL:
        return True
    return cols * rows >= MIN_CELLS


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


def inventory(pages) -> list[dict]:
    out: list[dict] = []
    for page in pages:
        for order, match in enumerate(TABLE.finditer(page.text), 1):
            html = match.group(0)
            rows = ROW.findall(html)
            cols = max((len(CELL.findall(row)) for row in rows), default=0)
            plain = TAG.sub("", html)
            before = page.text[max(0, match.start() - 200) : match.start()]
            hits = CAPTION.findall(before)
            out.append(
                {
                    "page": page.number,
                    "order": order,
                    "rows": len(rows),
                    "cols": cols,
                    "chars": len(plain),
                    "html_len": len(html),
                    "caption": (hits or [""])[-1].strip(),
                    "too_wide": cols > MAX_COLS,
                    "too_tall": cols >= MIN_COLS_FOR_TALL and len(rows) >= MIN_ROWS_FOR_TALL,
                    "too_long": len(plain) > 1200,
                    "many_cells": cols * len(rows) >= MIN_CELLS,
                }
            )
    return out


def structural_issues(pages) -> tuple[list[str], list[str]]:
    """(结构可疑, 疑似续表)。单元格数不一致 = rowspan 丢失的典型症状。"""
    odd: list[str] = []
    continuation: list[str] = []
    for page in pages:
        for match in TABLE.finditer(page.text):
            body = match.group(0)
            trs = ROW.findall(body)
            counts = [len(CELL.findall(row)) for row in trs]
            cols = max(counts) if counts else 0
            before = page.text[max(0, match.start() - 160) : match.start()]
            caption = (CAPTION.findall(before) or [""])[-1].strip()
            label = f"第 {page.number} 页「{caption[:30]}」"
            ragged = sorted({c for c in counts if c != cols and c > 1})
            if ragged:
                odd.append(f"{label}：行内单元格数不一致 {ragged}（最大 {cols} 列）")
            if before.rstrip().endswith(("（续）", "(续)")) or "（续）" in before[-12:] or "(续)" in before[-12:]:
                continuation.append(f"{label}：可能是跨页续表（前面带“（续）”）")
            long_cells = [c for c in CELL.findall(body) if len(TAG.sub("", c)) > 400]
            if long_cells:
                odd.append(f"{label}：有 {len(long_cells)} 个单元格超 400 字（可能把图读成了表）")
    return odd, continuation


# ---------------------------------------------------------------------------
# PaddleOCR 原始 JSONL：版面块坐标（裁图就靠它）
# ---------------------------------------------------------------------------


def locate_chunk(run, page: int) -> tuple[str, int]:
    """页码 -> (chunk_id, 分片内 0 基页序号)。用 run.json 里 chunks 的 page_start。"""
    best_id, best_start = "", -1
    for chunk in run.counters.get("chunks") or []:
        start = int(chunk.get("page_start") or 0)
        chunk_id = str(chunk.get("chunk_id") or "")
        if start <= page and start > best_start and chunk_id:
            best_id, best_start = chunk_id, start
    if not best_id:
        return "", -1
    return best_id, page - best_start


def layout_result(run, page: int) -> dict | None:
    """这一页的 prunedResult（含 parsing_res_list 与页面像素尺寸）。"""
    chunk_id, index = locate_chunk(run, page)
    if not chunk_id or index < 0:
        return None
    jsonl = run.extracted_dir / chunk_id / "paddle_result.jsonl"
    if not jsonl.is_file():
        return None
    seen = 0
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        results = json.loads(line).get("result", {}).get("layoutParsingResults") or []
        if seen + len(results) > index:
            return results[index - seen].get("prunedResult") or {}
        seen += len(results)
    return None


def table_boxes(run, page: int) -> list[list[int]]:
    """这一页里表格块的坐标框（按阅读顺序），坐标是解析器眼里的页面像素。"""
    result = layout_result(run, page)
    if not result:
        return []
    return [
        block["block_bbox"]
        for block in (result.get("parsing_res_list") or [])
        if block.get("block_label") == "table" and block.get("block_bbox")
    ]


def parser_page_size(run, page: int) -> tuple[int, int]:
    result = layout_result(run, page)
    if not result:
        return 0, 0
    return int(result.get("width") or 0), int(result.get("height") or 0)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_list(run, pages, args) -> int:
    rows = inventory(pages)
    over = [r for r in rows if r["too_wide"] or r["too_tall"] or r["too_long"] or r["many_cells"]]
    manifest = run.calibrate_dir / "table-inventory.json"
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"表格 {len(rows)} 张；按『手机可读』标准超标 {len(over)} 张"
        f"（列>{MAX_COLS} / 列≥{MIN_COLS_FOR_TALL}且行≥{MIN_ROWS_FOR_TALL} / 格子≥{MIN_CELLS} / 正文>1200 字）"
    )
    print(f"{'页':>4} {'序':>2} {'行':>4} {'列':>4} {'字数':>6}  表题")
    for row in sorted(over, key=lambda r: (-r["cols"], -r["rows"])):
        print(
            f"{row['page']:>4} {row['order']:>2} {row['rows']:>4} {row['cols']:>4} "
            f"{row['chars']:>6}  {row['caption'][:36]}"
        )
    print(f"\n台账：{manifest}")
    return 0


def cmd_check(run, pages, args) -> int:
    odd, continuation = structural_issues(pages)
    lines = [
        "# 表格体检",
        "",
        f"由 `scripts/table_tool.py check` 生成，共 {len(inventory(pages))} 张表。",
        "下面只列需要人看一眼的；每张表都要对着页面图核对。",
        "",
        "## 结构可疑",
        "",
    ]
    lines += [f"- {line}" for line in odd] or ["- （无）"]
    lines += ["", "## 疑似跨页续表", ""]
    lines += [f"- {line}" for line in continuation] or ["- （无）"]
    target = run.calibrate_dir / "table-check.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"结构可疑 {len(odd)} 条；疑似续表 {len(continuation)} 条")
    for line in odd[:15]:
        print("  !", line)
    for line in continuation[:10]:
        print("  ~", line)
    print(f"\n报告：{target}")
    return 0


def cmd_pages(run, pages, args) -> int:
    try:
        import pypdfium2 as pdfium
    except ModuleNotFoundError:
        sys.exit("需要页面渲染：pip install -e \".[render]\"")
    rows = inventory(pages)
    page_dir = run.calibrate_dir / "table-pages"
    page_dir.mkdir(exist_ok=True)
    document = pdfium.PdfDocument(run.input_pdf)
    wanted = sorted({r["page"] for r in rows})
    for number in wanted:
        target = page_dir / f"page-{number:04d}.png"
        if target.is_file():
            continue
        document[number - 1].render(scale=2.0).to_pil().save(target)
    lines = [
        "# 待核对表格清单（对图用）",
        "",
        f"共 {len(rows)} 张表；页面图目录：`calibrate/table-pages/page-NNNN.png`"
        "（NNNN = 原书 PDF 页号，2 倍渲染）。",
        "",
        "逐张对照：HTML 里的行、列、格子和图上是否一致。带 ⚠ 的是按宽度标准超标的"
        "（多半要换图，先核对它有没有别的错）；换图清单见 `crop` 生成的 `table-images.json`。",
        "",
    ]
    for row in rows:
        flag = "⚠ " if row["too_wide"] or row["too_tall"] or row["too_long"] else ""
        lines.append(
            f"- {flag}**第 {row['page']} 页 第 {row['order']} 张**（{row['rows']} 行 × "
            f"{row['cols']} 列）{row['caption']}　→ `table-pages/page-{row['page']:04d}.png`"
        )
    target = run.calibrate_dir / "table-manifest.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"表格 {len(rows)} 张，涉及 {len(wanted)} 页；页面图已备在 {page_dir}")
    print(f"清单：{target}")
    return 0


def _parse_specs(text: str) -> list[tuple[int, int]]:
    specs: list[tuple[int, int]] = []
    for part in re.split(r"[,，\s]+", text.strip()):
        if not part:
            continue
        page, _, order = part.partition(":")
        specs.append((int(page), int(order or 1)))
    return specs


def cmd_crop(run, pages, args) -> int:
    try:
        import pypdfium2 as pdfium
    except ModuleNotFoundError:
        sys.exit("需要页面渲染：pip install -e \".[render]\"")
    rows = inventory(pages)
    by_key = {(r["page"], r["order"]): r for r in rows}

    if args.only:
        picked = []
        for key in _parse_specs(args.only):
            row = by_key.get(key)
            if row is None:
                print(f"! 第 {key[0]} 页第 {key[1]} 张表不存在，跳过")
                continue
            picked.append(row)
    else:
        picked = [
            r
            for r in rows
            if r["too_wide"] or r["too_tall"] or r["too_long"] or r["many_cells"]
        ]
    picked_seen = {(r["page"], r["order"]) for r in picked}
    extra = []
    for key in _parse_specs(args.extra or ""):
        if key in picked_seen:
            continue
        row = by_key.get(key)
        if row is None:
            print(f"! 第 {key[0]} 页第 {key[1]} 张表不存在，跳过")
            continue
        extra.append(row)

    print(f"按阈值自动选出 {len(picked)} 张；额外指定 {len(extra)} 张")
    todo = picked + extra
    if not todo:
        print("没有要裁的表。")
        return 0

    document = pdfium.PdfDocument(run.input_pdf)
    image_dir = run.calibrate_dir / "images"
    image_dir.mkdir(exist_ok=True)
    rendered: dict[int, object] = {}
    entries = []
    done = 0
    for row in todo:
        page, order = row["page"], row["order"]
        boxes = table_boxes(run, page)
        if order > len(boxes):
            print(f"! 第 {page} 页只有 {len(boxes)} 个表格框，跳过第 {order} 张")
            continue
        if page not in rendered:
            rendered.clear()  # 一次只留一页，避免几十张 2 倍图同时占内存
            rendered[page] = document[page - 1].render(scale=args.scale).to_pil()
        image = rendered[page]
        width, height = parser_page_size(run, page)
        left, top, right, bottom = boxes[order - 1]
        ratio_x = image.width / width if width else 1.0
        ratio_y = image.height / height if height else 1.0
        margin = 10
        crop = (
            max(0, int(left * ratio_x) - margin),
            max(0, int(top * ratio_y) - margin),
            min(image.width, int(right * ratio_x) + margin),
            min(image.height, int(bottom * ratio_y) + margin),
        )
        name = f"table-p{page:04d}-o{order}.png"
        image.crop(crop).save(image_dir / name)
        done += 1
        entries.append(
            {
                "page": page,
                "order": order,
                "image": name,
                "rows": row["rows"],
                "cols": row["cols"],
                "chars": row["chars"],
                "caption": row["caption"],
                "reason": "threshold" if (page, order) in picked_seen else "manual",
            }
        )
        print(f"   第 {page:>4} 页第 {order} 张 → {name}")

    target = run.calibrate_dir / "table-images.json"
    target.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n裁了 {done} 张图到 {image_dir}")
    print(f"换图清单：{target}（撰写阶段照它把 <table> 换成 <img>，表题保留为正文）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="表格工具：台账 / 体检 / 对图清单 / 裁图换图")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="台账：所有表的行列字数与表题，标出换图候选")
    sub.add_parser("check", help="结构体检：格数不一致 / 续表 / 巨型单元格")
    sub.add_parser("pages", help="渲染含表页 + 出对图核对清单")
    crop = sub.add_parser("crop", help="把读不动的表裁成图")
    crop.add_argument("--extra", default="", help="额外换图的 页:序号 列表，如 325:1,497:1")
    crop.add_argument("--only", default="", help="只裁这些 页:序号（不给则按阈值自动选）")
    crop.add_argument("--scale", type=float, default=2.0, help="渲染倍率（默认 2.0）")
    args = parser.parse_args()

    from pdf2epub.config import load_config

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    pages = read_pages(run)
    if not pages:
        print(f"· 没找到任何解析产物：{run.extracted_dir}（先跑 prepare）")
        return 1

    commands = {"list": cmd_list, "check": cmd_check, "pages": cmd_pages, "crop": cmd_crop}
    return commands[args.cmd](run, pages, args)


if __name__ == "__main__":
    raise SystemExit(main())

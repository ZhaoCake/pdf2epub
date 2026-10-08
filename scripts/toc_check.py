#!/usr/bin/env python
"""结构体检：把原书书签逐条对到解析产物上，出 ``calibrate/toc-check.md``。

为什么要它：字数、接缝、重复都有脚本核对，**但"章节标题有没有解析出来"没人管**——
实测有书 3 章标题缺失或被拆成两行（章首大字页解析不稳）。原书 PDF 带书签的话，
书签就是现成的基准：

- 书签标题在预期页（±1 页）里**找不到** → 标题可能被吃掉 / 认错；
- 在别的页出现 → 页码映射或标题本身有问题；
- 全文找不到 → 大概率整条标题丢了，要对着页面图补插。

只读，不改任何产物。没有书签的 PDF 会直接说明并退出。

用法::

    python scripts/toc_check.py --run <run_id>
"""

from __future__ import annotations

import argparse
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

try:
    from pypdf import PdfReader
except ModuleNotFoundError:
    sys.exit("需要 pypdf：pip install -e . 已带上")

TAG = re.compile(r"<[^>]+>")


def bookmarks(pdf: Path) -> list[tuple[int, int, str]]:
    """[(层级, 页码 1 基, 标题)]"""
    reader = PdfReader(str(pdf))
    out: list[tuple[int, int, str]] = []

    def walk(items, depth: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:  # noqa: BLE001
                page = -1
            out.append((depth, page, str(item.title).strip()))

    walk(reader.outline, 1)
    return out


def normalize(text: str) -> str:
    """去空白、去标签，便于"标题在不在正文里"这种包含判断。"""
    return re.sub(r"[\s\u3000]+", "", TAG.sub("", text))


def main() -> int:
    parser = argparse.ArgumentParser(description="书签 vs 解析产物逐条核对")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    args = parser.parse_args()

    from pdf2epub.config import load_config

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    pages = read_pages(run)
    if not pages:
        print(f"· 没找到任何解析产物：{run.extracted_dir}（先跑 prepare）")
        return 1

    marks = bookmarks(run.input_pdf)
    if not marks:
        print("· 这本 PDF 没有书签（outline 为空），没有核对基准")
        return 1
    flat = {page.number: normalize(page.text) for page in pages}

    missing: list[str] = []
    lines = ["# 结构体检：书签 vs 解析产物", "", f"由 `scripts/toc_check.py` 生成，书签 {len(marks)} 条。", ""]

    for depth, page, title in marks:
        # 书签常带"第N章"前缀，正文里未必有；两种形态都试
        keys = [normalize(title)]
        stripped = normalize(re.sub(r"^第\s*[\d一二三四五六七八九十]+\s*[章节讲篇]\s*", "", title))
        if stripped and stripped not in keys:
            keys.append(stripped)
        hit_pages: list[int] = []
        for key in keys:
            hit_pages = [n for n in (page - 1, page, page + 1) if n in flat and key in flat[n]]
            if hit_pages:
                break
        if not hit_pages:
            anywhere: list[int] = []
            for key in keys:
                anywhere = [n for n, text in flat.items() if key in text]
                if anywhere:
                    break
            note = f"其它页出现：{anywhere[:4]}" if anywhere else "全文都找不到"
            missing.append(f"{'  ' * (depth - 1)}{title}（书签第 {page} 页）→ {note}")
        else:
            lines.append(f"- [x] {'  ' * (depth - 1)}{title} @ 第 {page} 页（命中 {hit_pages}）")

    lines.insert(4, f"**找到 {len(marks) - len(missing)} / {len(marks)} 条**")
    if missing:
        lines.insert(5, "")
        lines.insert(6, "## 没在书签所在页找到的标题（要对着页面图确认）")
        lines.insert(7, "")
        lines += [f"- [ ] {line}" for line in missing]
    target = run.calibrate_dir / "toc-check.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"书签 {len(marks)} 条：命中 {len(marks) - len(missing)}，未命中 {len(missing)}")
    for line in missing:
        print("  !", line)
    print(f"\n报告：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

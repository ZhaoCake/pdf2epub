#!/usr/bin/env python
"""按需把原书某几页渲染成图，放进 ``calibrate/pages/``。

``qc_scan.py --render`` 只渲染它自己命中的页，接缝页由 calibrate 阶段渲染；
校准中途临时要看某一页（比如对一张表的版式、确认某处脚注的引号），不用重跑
整个体检，直接用这个脚本点名渲染。

用法::

    python scripts/render_pages.py --run <run_id> 47 79 98
    python scripts/render_pages.py --run <run_id> --dpi 200 98
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from pdf2epub import pageimage
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")


def parse_pages(raw: list[str]) -> list[int]:
    pages: list[int] = []
    for item in raw:
        for part in str(item).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, _, hi = part.partition("-")
                pages.extend(range(int(lo), int(hi) + 1))
            else:
                pages.append(int(part))
    return sorted({p for p in pages if p > 0})


def main() -> int:
    parser = argparse.ArgumentParser(description="按需渲染原书页面图")
    parser.add_argument("pages", nargs="+", help="原书页码（1 起），支持 47 79 或 47-52")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--dpi", type=int, default=None, help="渲染 DPI（缺省用配置值）")
    parser.add_argument("--force", action="store_true", help="已存在的图也重新渲染")
    args = parser.parse_args()

    if not pageimage.available():
        sys.exit("渲染需要 pypdfium2 + Pillow：pip install pypdfium2 Pillow")

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    dpi = args.dpi or config.calibrate.render_dpi
    dest = run.calibrate_dir / "pages"
    dest.mkdir(parents=True, exist_ok=True)

    wrote = 0
    for page in parse_pages(args.pages):
        target = dest / f"page-{page:04d}.png"
        if target.is_file() and not args.force:
            print(f"· 第 {page} 页已有图，跳过（--force 可覆盖）")
            continue
        rendered = pageimage.render_page(
            run.input_pdf, page - 1, dpi=dpi, max_width=config.calibrate.max_image_width
        )
        if rendered is None:
            print(f"! 第 {page} 页渲染失败")
            continue
        pageimage.save_png(rendered.image, target)
        print(f"· 第 {page} 页 -> {target}")
        wrote += 1
    print(f"· 渲染 {wrote} 张，目录：{dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

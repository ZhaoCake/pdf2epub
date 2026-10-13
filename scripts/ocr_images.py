"""把一批图片送 PaddleOCR-VL 识别，结果按图片落成 Markdown。

用法::

    python ocr_images.py <图片目录> <输出目录>

复用仓库的解析后端（``src/pdf2epub/paddle.py``），Token 读仓库根目录的 ``.env``。
每张图片一个 job：

- ``<输出目录>/jobs.json``      记录 job_id，重跑不必重复提交（服务端额度是有限的）
- ``<输出目录>/<图片名>/full.md`` 该图的识别结果（另有 ``content_list.json`` 与 ``images/``）

已经出结果（存在 ``full.md``）的图片直接跳过，所以中断之后重跑同一条命令即可。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
_NUMBER = re.compile(r"(\d+)")


def find_repo_root(start: Path) -> Path:
    """从脚本所在位置向上找 pdf2epub 仓库根目录。"""
    for candidate in (start, *start.parents):
        if (candidate / "src" / "pdf2epub" / "paddle.py").is_file():
            return candidate
    raise SystemExit("找不到 pdf2epub 仓库根目录（缺 src/pdf2epub/paddle.py）")


def natural_key(path: Path) -> tuple:
    """按文件名里的数字排序，避免 "10" 排在 "2" 前面。"""
    return tuple(
        int(part) if part.isdigit() else part
        for part in _NUMBER.split(path.name)
    )


def collect_images(images_dir: Path) -> list[Path]:
    files = [
        p
        for p in images_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    return sorted(files, key=natural_key)


def main() -> int:
    parser = argparse.ArgumentParser(description="批量图片 OCR（PaddleOCR-VL）")
    parser.add_argument("images", help="要识别的图片所在目录")
    parser.add_argument("out", help="结果输出目录")
    args = parser.parse_args()

    images_dir = Path(args.images).resolve()
    out_dir = Path(args.out).resolve()
    if not images_dir.is_dir():
        raise SystemExit(f"不是目录：{images_dir}")

    repo = find_repo_root(Path(__file__).resolve())
    sys.path.insert(0, str(repo / "src"))

    from pdf2epub.config import load_config
    from pdf2epub.paddle import PaddleClient, build_bundle

    # cwd 指向仓库根，好让 .env 里的 PADDLE_TOKEN 被加载
    config = load_config(cwd=repo)
    client = PaddleClient(config.paddle)

    images = collect_images(images_dir)
    if not images:
        raise SystemExit(f"目录里没有图片：{images_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    jobs_file = out_dir / "jobs.json"
    jobs: dict[str, str] = (
        json.loads(jobs_file.read_text(encoding="utf-8")) if jobs_file.is_file() else {}
    )

    # 先把没提交过的都提交掉：8 张图就是 8 个 job，一次提交完再统一等，省时间
    todo: list[Path] = []
    for image in images:
        if (out_dir / image.stem / "full.md").is_file():
            print(f"[跳过] {image.name}：已有结果")
            continue
        if not jobs.get(image.name):
            jobs[image.name] = client.submit_job(image, label=image.name)
            jobs_file.write_text(
                json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        todo.append(image)

    failed: list[str] = []
    for index, image in enumerate(todo, 1):
        print(f"[{index}/{len(todo)}] 等待 {image.name}（job {jobs[image.name]}）")
        try:
            outcome = client.wait_job(jobs[image.name], label=image.name)
            jsonl = client.download_jsonl(outcome.json_url)
            build_bundle(jsonl, out_dir / image.stem, chunk_id=image.stem)
        except Exception as exc:  # noqa: BLE001 - 单张失败不该拖累其余图片
            failed.append(image.name)
            print(f"    失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exc()

    print()
    print(f"完成 {len(todo) - len(failed)}/{len(todo)}，结果在 {out_dir}")
    if failed:
        print(f"失败：{', '.join(failed)}")
        print("（重跑本命令即可只重试失败的图片）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

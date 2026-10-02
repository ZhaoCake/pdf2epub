"""测试夹具：假的解析产物 + 空白 PDF，不需要联网就能跑通整条链路。"""

from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdf2epub.config import Config  # noqa: E402
from pdf2epub.runstate import Run, RunStore, Stage, StageStatus  # noqa: E402

PAGE_W, PAGE_H = 595.0, 842.0


# ---------------------------------------------------------------------------
# 造 PDF
# ---------------------------------------------------------------------------


def make_pdf(path: Path, pages: int = 4, title: str = "测试用书") -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=PAGE_W, height=PAGE_H)
    writer.add_metadata({"/Title": title, "/Author": "测试作者", "/Producer": "pdf2epub-tests"})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        writer.write(fh)
    writer.close()
    return path


def make_png(path: Path, size: tuple[int, int] = (240, 160), color: tuple[int, int, int] = (90, 140, 200)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# 造 MinerU 产物
# ---------------------------------------------------------------------------


def layout_json(pages: int, *, low: tuple[int, ...] = (0,), clean: bool = False) -> list[dict[str, Any]]:
    """旧版 ``*_model.json``：layout_dets + category_id + poly + score。"""
    out: list[dict[str, Any]] = []
    for page_idx in range(pages):
        if clean or page_idx not in low:
            dets = [
                {"category_id": 0, "poly": [72, 120, 520, 200], "score": 0.96},
                {"category_id": 0, "poly": [72, 220, 520, 300], "score": 0.94},
            ]
        else:
            dets = [
                {"category_id": 0, "poly": [72, 120, 520, 200], "score": 0.41},
                {"category_id": 0, "poly": [72, 220, 520, 300], "score": 0.93},
            ]
        out.append(
            {"page_info": {"page_no": page_idx, "width": PAGE_W, "height": PAGE_H}, "layout_dets": dets}
        )
    return out


def content_list_json(pages: int, *, low: tuple[int, ...] = (0,), clean: bool = False) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page_idx in range(pages):
        items.append(
            {
                "type": "text",
                "text": f"第 {page_idx + 1} 章　标题",
                "text_level": 1,
                "page_idx": page_idx,
                "bbox": [72, 70, 520, 110],
            }
        )
        first = (
            "巳经、卩、亍 之类的形近字，OCR 很容易认错。"
            if (not clean and page_idx in low)
            else f"这是第 {page_idx + 1} 页的第一段正文，识别质量良好。"
        )
        items.append({"type": "text", "text": first, "page_idx": page_idx, "bbox": [72, 120, 520, 200]})
        items.append(
            {
                "type": "text",
                "text": f"这是第 {page_idx + 1} 页的第二段正文。",
                "page_idx": page_idx,
                "bbox": [72, 220, 520, 300],
            }
        )
        items.append(
            {
                "type": "image",
                "img_path": "images/fig.png",
                "image_caption": [f"图 {page_idx + 1}　示意图"],
                "page_idx": page_idx,
                "bbox": [72, 320, 520, 560],
            }
        )
    return items


def markdown_text(pages: int, *, low: tuple[int, ...] = (0,), clean: bool = False) -> str:
    blocks = ["# 测试书", ""]
    for page_idx in range(pages):
        garbled = (
            "巳经、卩、亍 之类的形近字，OCR 很容易认错。"
            if (not clean and page_idx in low)
            else f"这是第 {page_idx + 1} 页的第一段正文，识别质量良好。"
        )
        blocks.extend(
            [
                f"## 第 {page_idx + 1} 章　标题",
                "",
                garbled,
                "",
                f"这是第 {page_idx + 1} 页的第二段正文。",
                "",
                f"![图 {page_idx + 1}　示意图](images/fig.png)",
                "",
            ]
        )
    return "\n".join(blocks)


def vlm_bundle(root: Path, *, pages: int = 3) -> Path:
    """复刻 MinerU vlm 模式的真实形状，用来锁住两个坑：

    - ``model.json`` 是 **list of list**（每页直接是 det 列表，没有 page_info 外壳）
    - ``content_list`` 的 bbox 是 **0-1000** 整数归一化，而 model.json 是 **0-1**
    - 版面层带分数的是 ``ocr_text``，粒度是**逐行**；正文条目是**整段**
    """
    root.mkdir(parents=True, exist_ok=True)

    model = []
    content = []
    for page_idx in range(pages):
        lines = [
            # 逐行 OCR：第一行没把握，其余正常
            {"type": "ocr_text", "bbox": [0.10, 0.20, 0.90, 0.24], "text": "第一行的识别结果", "score": 0.42},
            {"type": "ocr_text", "bbox": [0.10, 0.24, 0.90, 0.28], "text": "第二行的识别结果", "score": 1.0},
            {"type": "ocr_text", "bbox": [0.10, 0.28, 0.90, 0.32], "text": "第三行的识别结果", "score": 1.0},
            # 页内的公式：不带分数，只用来验证小框能落进行框
            # （放在第一行——那行分数最低——才能验证它继承的是所在行的分数）
            {"type": "inline_formula", "bbox": [0.30, 0.21, 0.40, 0.23], "content": None},
            {"type": "page_number", "bbox": [0.49, 0.94, 0.51, 0.96], "content": None},
        ]
        model.append(lines)

        # content_list：整段 + 每页一个公式，坐标是 0-1000
        content.append(
            {
                "type": "text",
                "text": f"第 {page_idx + 1} 页的整段正文，由三行 OCR 拼成。",
                "bbox": [100, 200, 900, 320],
                "page_idx": page_idx,
            }
        )
        content.append(
            {
                "type": "equation",
                "text": f"$$E_{page_idx + 1} = mc^2$$",
                "bbox": [300, 210, 400, 230],
                "page_idx": page_idx,
            }
        )

    (root / "abc123_model.json").write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    (root / "abc123_content_list.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    (root / "full.md").write_text("# 正文\n", encoding="utf-8")
    (root / ".extracted").write_text("ok", encoding="utf-8")
    return root


def write_bundle(root: Path, *, pages: int = 4, low: tuple[int, ...] = (0,), clean: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "book_model.json").write_text(
        json.dumps(layout_json(pages, low=low, clean=clean), ensure_ascii=False), encoding="utf-8"
    )
    (root / "book_content_list.json").write_text(
        json.dumps(content_list_json(pages, low=low, clean=clean), ensure_ascii=False), encoding="utf-8"
    )
    (root / "full.md").write_text(markdown_text(pages, low=low, clean=clean), encoding="utf-8")
    make_png(root / "images" / "fig.png")
    (root / ".extracted").write_text("ok", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# 造一个"已经跑完 prepare"的 Run
# ---------------------------------------------------------------------------


def make_run(
    tmp_path: Path,
    *,
    pages: int = 4,
    low: tuple[int, ...] = (0,),
    clean: bool = False,
    config: Config | None = None,
) -> tuple[Run, Config]:
    config = config or Config(workdir=str(tmp_path / "work"))
    store = RunStore(Path(config.workdir))
    pdf = make_pdf(tmp_path / "book.pdf", pages=pages, title="测试用书")
    run = store.create(pdf, run_id="test-run", config_snapshot=config.to_dict())

    chunk_id = "c001"
    run.counters["pdf"] = {
        "path": str(pdf),
        "page_count": pages,
        "byte_size": pdf.stat().st_size,
        "encrypted": False,
        "has_text_layer": False,
        "sampled_chars_per_page": 0.0,
        "title": "测试用书",
        "author": "测试作者",
        "producer": "pdf2epub-tests",
        "width": PAGE_W,
        "height": PAGE_H,
        "outline": [],
        "errors": [],
    }
    run.counters["chunks"] = [
        {
            "chunk_id": chunk_id,
            "path": str(pdf),
            "page_start": 0,
            "page_end": pages - 1,
            "page_count": pages,
            "is_ocr": True,
            "byte_size": pdf.stat().st_size,
        }
    ]

    write_bundle(run.extracted_dir / chunk_id, pages=pages, low=low, clean=clean)

    record = run.stage(Stage.PREPARE)
    record.status = StageStatus.DONE.value
    record.started_at = record.finished_at = 0.0
    run.save()
    return run, config


#: 测试自己的临时目录根。**刻意不交给 pytest 管**。
#:
#: pytest 默认只保留最近 3 次会话的 ``pytest-N`` 目录，每次跑测试都会把更早的那一份
#: 整棵删掉——一次就是几百个文件（实测每份约 300 个文件、连符号链接算 600 多个）。
#: 沙箱化的 IDE 会把这种批量删除当成破坏性操作拦下来，测试就没法一直自动跑下去。
#: 自己发号、自己留着，跑测试就不再产生任何删除；清理交给用户/系统。
TMP_ROOT = Path(tempfile.gettempdir()) / f"pdf2epub-tests-{os.getpid()}"
_TMP_SEQ = itertools.count(1)


@pytest.fixture()
def tmp_path() -> Path:
    """覆盖内置的 ``tmp_path``：每个测试一个独享目录，但永不自动删除。"""
    path = TMP_ROOT / f"t{next(_TMP_SEQ):04d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path / "work"


@pytest.fixture()
def cfg(workdir: Path) -> Config:
    return Config(workdir=str(workdir), log_json=False)

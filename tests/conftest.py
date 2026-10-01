"""测试夹具：假的 MinerU 产物 + 空白 PDF，不需要联网就能跑通整条链路。"""

from __future__ import annotations

import json
import sys
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


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path / "work"


@pytest.fixture()
def cfg(workdir: Path) -> Config:
    return Config(workdir=str(workdir), log_json=False)

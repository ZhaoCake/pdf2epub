"""PaddleOCR 后端：JSONL 结果 -> bundle 契约的归一。

流水线的其余部分（boundary / calibrate / compose）只吃 bundle，
所以适配层的关键承诺是：归一出来的产物和 MinerU 时代的形状一致——
``full.md`` + ``content_list.json``（每页一条，带 page_idx）+ ``images/``。
"""

from __future__ import annotations

import json
from pathlib import Path

from pdf2epub import boundary, paddle
from pdf2epub.ingest import Chunk
from pdf2epub.paddle import iter_pages

PAGE_TEMPLATE = (
    '{{"result": {{"layoutParsingResults": [{{"markdown": {{"text": {text!r}, '
    '"images": {images!r}}}}}]}}}}'
)


def _jsonl(pages: list[tuple[str, dict[str, str]]]) -> str:
    lines = []
    for text, images in pages:
        # json.dumps 保证引号转义正确；上面模板里的 !r 在这里手工拼等价物
        entry = {"result": {"layoutParsingResults": [{"markdown": {"text": text, "images": images}}]}}
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines)


def test_iter_pages_skips_blank_lines():
    pages = list(iter_pages("\n".join([_jsonl([("第1页", {})])]).replace("\n", "\n\n")))
    assert [p[0] for p in pages] == [0]


def test_build_bundle_writes_contract(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paddle, "_fetch", lambda url: b"png-bytes")
    jsonl = _jsonl(
        [
            ("第一页正文 ![图](https://img.example/a.png)", {"https://img.example/a.png": "https://img.example/a.png"}),
            ("第二页正文", {}),
        ]
    )

    bundle = paddle.build_bundle(jsonl, tmp_path / "c001", chunk_id="c001")

    assert bundle.markdown is not None
    text = bundle.markdown.read_text(encoding="utf-8")
    assert "第一页正文" in text and "第二页正文" in text
    # URL 引用被改写成本地相对路径
    assert "https://img.example/a.png" not in text
    assert "images/a.png" in text
    # 图片确实落盘（bundle 能定位到 images 目录）
    assert bundle.images_dir is not None
    assert (bundle.images_dir / "a.png").read_bytes() == b"png-bytes"
    # content_list 每页一条，交界处校验靠它对账
    entries = json.loads(bundle.content_list.read_text(encoding="utf-8"))
    assert [e["page_idx"] for e in entries] == [0, 1]
    assert "第二页正文" in entries[1]["text"]
    # 断点续跑的标记
    assert (tmp_path / "c001" / ".extracted").is_file()
    # 原始结果保留为 .jsonl，不会被 bundle 当成可解析 JSON
    assert (tmp_path / "c001" / "paddle_result.jsonl").is_file()
    assert all(p.suffix != ".jsonl" for p in bundle.other_json)


def test_boundary_audit_works_on_paddle_bundles(tmp_path: Path, monkeypatch):
    """归一产物要能直接喂给交界处校验：页数对账 + 接缝字符数。"""
    monkeypatch.setattr(paddle, "_fetch", lambda url: b"x")
    chunks = [
        Chunk("c001", Path("book.pdf"), 0, 19),
        Chunk("c002", Path("book.pdf"), 20, 39),
    ]
    bundles = [
        paddle.build_bundle(_jsonl([(f"第 {i} 页的正文内容。", {}) for i in range(1, 21)]), tmp_path / "c001"),
        paddle.build_bundle(_jsonl([(f"第 {i} 页的正文内容。", {}) for i in range(21, 41)]), tmp_path / "c002"),
    ]

    result = boundary.audit(page_count=40, chunks=chunks, bundles=bundles)

    assert result.ok, result.issues
    seam = result.seams[0]
    assert (seam.left_page, seam.right_page) == (20, 21)
    assert seam.left_chars > 0 and seam.right_chars > 0


def test_build_bundle_rejects_empty_result(tmp_path: Path):
    import pytest

    from pdf2epub.errors import ArtifactError

    with pytest.raises(ArtifactError):
        paddle.build_bundle("\n\n", tmp_path / "c001", chunk_id="c001")

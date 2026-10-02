"""分片与交界处校验：解析按 20 页一片，接缝必须是被检查过的结论。

这组测试锁住三件容易静默出错的事：

1. 切分计划本身首尾相接、不重不漏；
2. 分片文件名带**原书页码范围**——改切分方案后旧分片不会因 chunk_id 撞名被
   错误复用（那会静默解析出错误的页）；
3. 交界处校验能发现"某片少解析了页"和"接缝页没解析出正文"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_bundle

from pdf2epub import boundary
from pdf2epub.bundle import Bundle, load_bundle
from pdf2epub.config import PaddleConfig
from pdf2epub.ingest import Chunk, PdfProfile, chunk_filename, coverage_issues, plan_chunks

# ---------------------------------------------------------------------------
# 切分计划
# ---------------------------------------------------------------------------


def _profile(pages: int, *, size: int = 10_000_000) -> PdfProfile:
    return PdfProfile(path=Path("book.pdf"), page_count=pages, byte_size=size)


def test_pages_are_split_every_twenty():
    """386 页按 20 页一片 -> 20 片，最后一片是余数。"""
    chunks = plan_chunks(_profile(386), PaddleConfig())
    assert len(chunks) == 20
    assert [(c.page_count) for c in chunks[:3]] == [20, 20, 20]
    assert chunks[-1].page_count == 386 - 19 * 20 == 6
    assert chunks[0].page_start == 0 and chunks[-1].page_end == 385


def test_plan_is_contiguous_without_gap_or_overlap():
    chunks = plan_chunks(_profile(386), PaddleConfig())
    assert coverage_issues(chunks, 386) == []


def test_small_book_is_one_chunk():
    chunks = plan_chunks(_profile(18), PaddleConfig())
    assert len(chunks) == 1
    assert chunks[0].page_count == 18


def test_chunk_filename_carries_book_page_range():
    """文件名带原书页码：换切分方案后旧分片不会撞名复用。"""
    chunks = plan_chunks(_profile(386), PaddleConfig())
    assert chunk_filename("input", chunks[0]) == "input.c001.p0001-0020.pdf"
    assert chunk_filename("input", chunks[1]) == "input.c002.p0021-0040.pdf"


def test_coverage_check_reports_gap_and_overlap():
    gap = [Chunk("c001", Path("a.pdf"), 0, 9), Chunk("c002", Path("b.pdf"), 20, 29)]
    assert any("缺页" in issue for issue in coverage_issues(gap, 30))

    overlap = [Chunk("c001", Path("a.pdf"), 0, 9), Chunk("c002", Path("b.pdf"), 9, 19)]
    assert any("重叠" in issue for issue in coverage_issues(overlap, 20))

    short = [Chunk("c001", Path("a.pdf"), 0, 9)]
    assert any("结尾缺页" in issue for issue in coverage_issues(short, 20))


def test_batch_file_limit_beats_chunk_size():
    """单批最多 50 个文件：1200 页按 20 页一片会超出，宁可让每片大一点。"""
    config = PaddleConfig(max_pages_per_task=20, max_files_per_batch=50)
    chunks = plan_chunks(_profile(1200), config)
    assert len(chunks) == 50
    assert chunks[0].page_count == 24


# ---------------------------------------------------------------------------
# 交界处校验
# ---------------------------------------------------------------------------


def _chunks(*spans: tuple[int, int]) -> list[Chunk]:
    """spans 是 1 基闭区间，内部换算成 0 基。"""
    return [
        Chunk(f"c{index:03d}", Path("book.pdf"), start - 1, end - 1)
        for index, (start, end) in enumerate(spans, start=1)
    ]


def _bundles(tmp_path: Path, chunk_ids: list[str], *, pages: list[int]) -> list[Bundle]:
    out: list[Bundle] = []
    for chunk_id, count in zip(chunk_ids, pages):
        root = tmp_path / chunk_id
        write_bundle(root, pages=count)
        out.append(load_bundle(root))
    return out


def test_audit_passes_when_pages_line_up(tmp_path: Path):
    chunks = _chunks((1, 20), (21, 40))
    bundles = _bundles(tmp_path, ["c001", "c002"], pages=[20, 20])

    result = boundary.audit(page_count=40, chunks=chunks, bundles=bundles)

    assert result.ok, result.issues
    assert result.chunk_count == 2
    assert [(s.left_page, s.right_page) for s in result.seams] == [(20, 21)]
    text = result.to_markdown()
    assert "第 20 页" in text and "第 21 页" in text


def test_audit_flags_chunk_with_fewer_pages(tmp_path: Path):
    """某片只解析出 18 页：页数对不上必须报出来，不能当没发生。"""
    chunks = _chunks((1, 20), (21, 40))
    bundles = _bundles(tmp_path, ["c001", "c002"], pages=[18, 20])

    result = boundary.audit(page_count=40, chunks=chunks, bundles=bundles)

    assert not result.ok
    assert any("c001" in issue and "解析出 18 页" in issue for issue in result.issues)


def test_audit_flags_backend_reported_page_shortfall(tmp_path: Path):
    chunks = _chunks((1, 20), (21, 40))
    bundles = _bundles(tmp_path, ["c001", "c002"], pages=[20, 20])

    result = boundary.audit(
        page_count=40, chunks=chunks, bundles=bundles, reported={"c002": 19}
    )

    assert any("自报 19 页" in issue for issue in result.issues)


def test_audit_flags_missing_chunk(tmp_path: Path):
    chunks = _chunks((1, 20), (21, 40))
    bundles = _bundles(tmp_path, ["c001"], pages=[20])

    result = boundary.audit(page_count=40, chunks=chunks, bundles=bundles)

    assert any("c002 没有解析产物" in issue for issue in result.issues)


def test_audit_flags_empty_seam_page(tmp_path: Path):
    """接缝页一个字都没解析出来：缝上一定断了，必须让人去看图。"""
    chunks = _chunks((1, 20), (21, 40))
    bundles = _bundles(tmp_path, ["c001", "c002"], pages=[20, 20])
    # 把第 2 片第一页（=接缝右侧）的正文条目全删掉
    content_path = bundles[1].content_list
    items = [
        item
        for item in json.loads(content_path.read_text(encoding="utf-8"))
        if item.get("page_idx") != 0
    ]
    content_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    bundles[1] = Bundle(root=bundles[1].root, content_list=content_path)

    result = boundary.audit(page_count=40, chunks=chunks, bundles=bundles)

    seam = result.seams[0]
    assert seam.right_chars == 0
    assert 21 in seam.empty_pages
    assert any("第 21 页" in issue for issue in result.issues)


def test_pages_are_counted_from_content_list(tmp_path: Path):
    root = tmp_path / "c001"
    write_bundle(root, pages=7)
    assert boundary.parsed_pages(load_bundle(root)) == 7


def test_page_chars_are_counted_per_page(tmp_path: Path):
    root = tmp_path / "c001"
    write_bundle(root, pages=3)
    counts = boundary.page_chars(load_bundle(root))

    assert set(counts) == {0, 1, 2}
    assert all(count > 0 for count in counts.values())


@pytest.mark.parametrize("pages", [20, 21])
def test_audit_dict_is_serializable(tmp_path: Path, pages: int):
    chunks = _chunks((1, pages))
    bundles = _bundles(tmp_path, ["c001"], pages=[pages])

    payload = json.dumps(boundary.audit(page_count=pages, chunks=chunks, bundles=bundles).to_dict())

    assert "chunk_count" in payload

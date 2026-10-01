"""低分段落提取：三种 schema、分数归一、挑选用例。"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import vlm_bundle, write_bundle

from pdf2epub.bundle import load_bundle
from pdf2epub.segments import (
    _bbox_of,
    extract_segments,
    find_score,
    low_confidence,
    _normalize_score,
)


def test_extracts_from_content_list(tmp_path: Path):
    root = write_bundle(tmp_path / "b", pages=3)
    segments = extract_segments(load_bundle(root))

    assert segments, "应当抽出段落"
    assert all(s.text for s in segments)
    assert all(s.page_idx in (0, 1, 2) for s in segments)

    # 第一页的正文来自 content_list，分数靠 model.json 的版面框配对
    page0 = [s for s in segments if s.page_idx == 0]
    body = next(s for s in page0 if "形近字" in s.text)
    assert body.score is not None and body.score < 0.5
    assert body.score_source.startswith("layout") or body.score_source == "score"


def test_picks_low_confidence_in_order(tmp_path: Path):
    root = write_bundle(tmp_path / "b", pages=4)
    segments = extract_segments(load_bundle(root))
    low = low_confidence(segments, threshold=0.75, limit=10)

    assert len(low) == 1, [s.score for s in low]
    assert low[0].page_no == 1
    assert low[0].score < 0.5


def test_clean_bundle_has_no_low_segments(tmp_path: Path):
    root = write_bundle(tmp_path / "b", pages=3, clean=True)
    segments = extract_segments(load_bundle(root))
    assert low_confidence(segments, threshold=0.75, limit=10) == []


def test_limit_is_respected(tmp_path: Path):
    root = write_bundle(tmp_path / "b", pages=6, low=(0, 1, 2, 3, 4, 5))
    segments = extract_segments(load_bundle(root))
    assert len(low_confidence(segments, threshold=0.75, limit=2)) == 2


def test_generic_fallback_on_unknown_schema(tmp_path: Path):
    """不认识的 schema 也要能捞到东西，而不是崩溃或空手而归。"""
    root = tmp_path / "weird"
    root.mkdir()
    (root / "strange_output.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "page_index": 4,
                        "regions": [
                            {"label": "paragraph", "confidence": 0.31, "text": "这一段机器没把握"},
                            {"label": "paragraph", "confidence": 0.97, "text": "这一段没问题"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bundle = load_bundle(root)
    segments = extract_segments(bundle)

    assert segments, "兜底抽取应当能找到带分数带文本的节点"
    hit = next(s for s in segments if "没把握" in s.text)
    assert hit.score == 0.31
    assert hit.page_no == 5


class TestVlmBundle:
    """MinerU vlm 模式的真实形状。这组曾经全是 bug，值得单独锁住。"""

    def _segments(self, tmp_path, *, pages=3, sizes=True):
        root = vlm_bundle(tmp_path / "vlm", pages=pages)
        return extract_segments(
            load_bundle(root),
            page_sizes={i: (595.28, 841.89) for i in range(pages)} if sizes else None,
        )

    def test_pages_are_not_all_page_one(self, tmp_path):
        """model.json 是"列表套列表"，页码必须按顶层下标推，不能全落在 0。"""
        segments = self._segments(tmp_path, pages=4)
        assert {s.page_idx for s in segments} == {0, 1, 2, 3}

    def test_zero_to_thousand_coords_are_understood(self, tmp_path):
        """content_list 的 0-1000 坐标要和 model.json 的 0-1 坐标对齐。"""
        segments = self._segments(tmp_path)
        scored = [s for s in segments if s.score is not None]
        assert len(scored) == len(segments), [s.text for s in segments if s.score is None]

    def test_paragraph_score_is_the_worst_line(self, tmp_path):
        """整段由多行拼成时，取最低的那一行——整段里有一处没把握就值得看一眼。"""
        segments = self._segments(tmp_path)
        body = next(s for s in segments if s.kind == "text")
        assert body.score == 0.42, body.score
        assert body.lines == 3

    def test_small_formula_box_matches_enclosing_line(self, tmp_path):
        """公式是小框，落在某一行里，也该拿到那一行的分数。"""
        segments = self._segments(tmp_path)
        formula = next(s for s in segments if s.kind == "equation")
        assert formula.score == 0.42, formula.score

    def test_low_segments_spread_across_pages(self, tmp_path):
        """低分段必须散在各自的页上——这正是当初出错的症结。"""
        segments = self._segments(tmp_path, pages=5)
        low = low_confidence(segments, threshold=0.75, limit=100)
        assert {s.page_no for s in low} == {1, 2, 3, 4, 5}

    def test_without_page_sizes_coordinates_still_align(self, tmp_path):
        """两边都是 0-1000 时（夹具那种），没有页面尺寸也能对齐。"""
        segments = self._segments(tmp_path, sizes=False)
        assert all(s.score is not None for s in segments)


def test_wrong_scale_is_refused_not_guessed(tmp_path):
    """认不出坐标系时宁可没有分数，也不要拿错框硬配。"""
    root = tmp_path / "odd"
    root.mkdir()
    (root / "x_model.json").write_text(
        json.dumps([[[{"type": "ocr_text", "bbox": [3000, 4000, 5000, 4200], "score": 0.3, "text": "行"}]]]),
        encoding="utf-8",
    )
    (root / "x_content_list.json").write_text(
        json.dumps([{"type": "text", "text": "段落", "bbox": [1, 2, 3, 4], "page_idx": 0}]),
        encoding="utf-8",
    )
    segments = extract_segments(load_bundle(root))
    assert len(segments) == 1
    assert segments[0].score is None, "坐标系对不上就不该给分数"


def test_normalize_score_ranges():
    assert _normalize_score(0.42) == 0.42
    assert _normalize_score(42) == 0.42
    assert _normalize_score("0.8") == 0.8
    assert _normalize_score(120) is None
    assert _normalize_score(None) is None
    assert _normalize_score(True) is None


def test_find_score_digs_into_children():
    assert find_score({"block": {"score": 0.66}})[0] == 0.66
    assert find_score({"nothing": 1}) == (None, "")


def test_bbox_shapes():
    assert _bbox_of({"bbox": [1, 2, 3, 4]}) == (1.0, 2.0, 3.0, 4.0)
    assert _bbox_of({"poly": [1, 2, 3, 4, 5, 6, 7, 8]}) == (1.0, 2.0, 7.0, 8.0)
    assert _bbox_of({"bbox": "nope"}) is None
    assert _bbox_of({}) is None

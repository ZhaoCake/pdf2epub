"""低分段落提取：三种 schema、分数归一、挑选用例。"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import write_bundle

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

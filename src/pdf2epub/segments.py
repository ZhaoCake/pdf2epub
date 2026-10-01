"""从 MinerU 产物里抽出"带置信度的段落"。

这是脚本唯一需要"读懂"MinerU 的地方，所以刻意做得又笨又宽：

- 有 ``content_list`` 就用它——它是按阅读顺序排好的正文，最接近 LLM 要的东西；
- ``content_list`` 没带分数时，拿 ``model.json`` 的版面框按 IoU 去配对拿分数；
- 两个都没有，就在整个 JSON 里递归找"既有分数又有文本"的节点。

不追求把所有 schema 都吃透：抽不全就少报几条，比"为了正确而崩溃"划算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .bundle import Bundle, read_json
from .logutil import get_logger

log = get_logger("segments")

TEXT_KEYS = ("text", "content", "table_body", "html", "latex", "markdown")
CAPTION_KEYS = ("image_caption", "table_caption", "caption", "img_caption", "footnote")
IMG_KEYS = ("img_path", "image_path", "img", "image")
SCORE_KEYS = ("score", "confidence", "conf", "probability", "avg_score")
PAGE_KEYS = ("page_idx", "page_index", "page_no", "page_number", "page")
BBOX_KEYS = ("bbox", "box", "poly", "polygon")
PAGE_CONTAINER_KEYS = ("layout_dets", "blocks", "elements", "items", "content_list", "detections")


@dataclass
class Segment:
    """一段被 MinerU 打了分的内容。"""

    segment_id: str
    page_idx: int
    page_no: int
    kind: str
    text: str
    score: float | None
    score_source: str
    bbox: tuple[float, float, float, float] | None = None
    order: int = 0

    @property
    def critical(self) -> bool:
        return self.score is not None and self.score < 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "page": self.page_no,
            "page_idx": self.page_idx,
            "kind": self.kind,
            "score": self.score,
            "score_source": self.score_source,
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
        }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _get(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key in mapping:
            return mapping[key]
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, list):
        values = [v for v in (_as_float(v) for v in value) if v is not None]
        if values:
            return sum(values) / len(values)
    if isinstance(value, dict):
        for key in SCORE_KEYS:
            found = _as_float(_get(value, key))
            if found is not None:
                return found
    return None


def _normalize_score(value: Any) -> float | None:
    """把 0-1 或 0-100 的分数统一到 0-1。超出范围就当没有。"""
    score = _as_float(value)
    if score is None:
        return None
    if 0.0 <= score <= 1.0:
        return score
    if 1.0 < score <= 100.0:
        return score / 100.0
    return None


def find_score(node: Any, *, depth: int = 1) -> tuple[float | None, str]:
    """在节点（及其若干层子节点）里找分数。"""
    if depth < 0 or not isinstance(node, dict):
        return None, ""
    for key in SCORE_KEYS:
        value = _get(node, key)
        if value is None or isinstance(value, (dict, list)):
            continue
        score = _normalize_score(value)
        if score is not None:
            return score, key
    if depth:
        for key, value in node.items():
            if isinstance(value, dict):
                score, source = find_score(value, depth=depth - 1)
                if score is not None:
                    return score, f"{key}.{source}"
    return None, ""


def _bbox_of(node: Any) -> tuple[float, float, float, float] | None:
    raw = _get(node, *BBOX_KEYS)
    if isinstance(raw, dict):
        raw = _get(raw, "x0", "left", "x1", "right", "top", "bottom")
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    values = [v for v in (_as_float(v) for v in raw) if v is not None]
    if len(values) == 4:
        return (values[0], values[1], values[2], values[3])
    if len(values) == 8:
        xs, ys = values[0::2], values[1::2]
        return (min(xs), min(ys), max(xs), max(ys))
    return None


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _text_of(node: Any) -> str:
    parts: list[str] = []
    for key in TEXT_KEYS:
        value = _get(node, key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
            break
        if isinstance(value, list):
            joined = " ".join(str(v).strip() for v in value if isinstance(v, str) and v.strip())
            if joined:
                parts.append(joined)
                break
    for key in CAPTION_KEYS:
        value = _get(node, key)
        if isinstance(value, list):
            parts.extend(str(v).strip() for v in value if isinstance(v, str) and v.strip())
        elif isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if not parts:
        image = _get(node, *IMG_KEYS)
        if isinstance(image, str) and image.strip():
            parts.append(f"[图片] {image.strip()}")
    return "\n".join(parts).strip()


def _kind_of(node: Any) -> str:
    raw = _get(node, "type", "category", "label", "block_type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    if isinstance(raw, int):
        return f"category_{raw}"
    return "unknown"


def _page_of(node: Any, fallback: int) -> int:
    value = _get(node, *PAGE_KEYS)
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        # 有的版本用 1-based page_no，和 page_idx 混用时以键名为准
        lowered = {str(k).lower(): k for k in node} if isinstance(node, dict) else {}
        key = str(lowered.get("page_no") or lowered.get("page_number") or "").lower()
        return value - 1 if key else value
    return fallback


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


def extract_segments(bundle: Bundle) -> list[Segment]:
    """从产物里抽出所有可用的带分段落。

    优先用 ``content_list``：它是按阅读顺序排好的正文。但如果它身上一个分数都
    带不出来（旧版本常见），就退回去在其它 JSON 里递归捞。
    """
    content = _from_content_list(bundle) if bundle.content_list is not None else []
    if content and any(segment.score is not None for segment in content):
        return content

    for path in bundle.json_sources:
        if path == bundle.content_list:
            continue
        found = _from_generic(read_json(path), source=path.name)
        if found:
            return found

    if content:
        log.info("产物不带置信度，只有正文；交给 LLM 按需处理")
        return content

    log.info("产物里找不到任何带置信度的段落，交给 LLM 全量处理")
    return []


def _from_content_list(bundle: Bundle) -> list[Segment]:
    payload = read_json(bundle.content_list)
    if not isinstance(payload, list):
        return []

    items = [item for item in payload if isinstance(item, dict)]
    if not items:
        return []

    # 分数可能直接写在 content_list 里，也可能要靠 model.json 的版面框配对
    layout = _layout_index(bundle)
    segments: list[Segment] = []

    for order, item in enumerate(items):
        score, source = find_score(item, depth=1)
        bbox = _bbox_of(item)
        page_idx = _page_of(item, 0)
        if score is None and layout:
            score, source = _lookup_layout_score(layout, page_idx, bbox)
        text = _text_of(item)
        if not text:
            continue
        segments.append(
            Segment(
                segment_id=f"S{len(segments) + 1:05d}",
                page_idx=page_idx,
                page_no=page_idx + 1,
                kind=_kind_of(item),
                text=text,
                score=score,
                score_source=source or "none",
                bbox=bbox,
                order=order,
            )
        )
    return segments


def _layout_index(bundle: Bundle) -> dict[int, list[tuple[tuple[float, float, float, float], float, str]]]:
    """从 model.json / middle 里建 (页 -> [(框, 分数)]) 的索引。"""
    index: dict[int, list[tuple[tuple[float, float, float, float], float, str]]] = {}
    for path in (bundle.model, bundle.middle):
        payload = read_json(path)
        if payload is None:
            continue
        for page_idx, page_node in _iter_pages(payload):
            for child in _iter_children(page_node):
                bbox = _bbox_of(child)
                score, source = find_score(child, depth=1)
                if bbox is None or score is None:
                    continue
                index.setdefault(page_idx, []).append((bbox, score, source))
    return index


def _lookup_layout_score(
    index: dict[int, list[tuple[tuple[float, float, float, float], float, str]]],
    page_idx: int,
    bbox: tuple[float, float, float, float] | None,
) -> tuple[float | None, str]:
    if bbox is None:
        return None, ""
    entries = index.get(page_idx) or index.get(page_idx + 1)
    if not entries:
        return None, ""
    best = max(entries, key=lambda entry: _iou(entry[0], bbox))
    if _iou(best[0], bbox) < 0.2:
        return None, ""
    return best[1], f"layout:{best[2]}"


def _from_generic(payload: Any, *, source: str) -> list[Segment]:
    """兜底：递归找"既有分数又有文本"的节点。"""
    segments: list[Segment] = []

    def walk(node: Any, page_idx: int) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, page_idx)
            return
        if not isinstance(node, dict):
            return

        here = _page_of(node, page_idx)
        score, score_source = find_score(node, depth=0)
        text = _text_of(node) if score is not None else ""
        if score is not None and not text:
            # 分数在外层、文本在内层：把子孙文本拼起来
            text = " ".join(t for t in (_text_of(c) for c in _flatten_dicts(node)) if t).strip()

        if score is not None and text:
            segments.append(
                Segment(
                    segment_id=f"S{len(segments) + 1:05d}",
                    page_idx=here,
                    page_no=here + 1,
                    kind=_kind_of(node),
                    text=text,
                    score=score,
                    score_source=f"{source}:{score_source}",
                    bbox=_bbox_of(node),
                    order=len(segments),
                )
            )

        for key, value in node.items():
            if isinstance(value, (list, dict)):
                walk(value, here)

    walk(payload, 0)
    return segments


def _flatten_dicts(node: Any, depth: int = 3) -> Iterable[dict[str, Any]]:
    if depth < 0:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _flatten_dicts(value, depth - 1)
    elif isinstance(node, list):
        for child in node:
            yield from _flatten_dicts(child, depth - 1)


def _iter_pages(payload: Any) -> Iterable[tuple[int, Any]]:
    if isinstance(payload, list):
        for index, node in enumerate(payload):
            yield _page_of(node, index), node
        return
    if isinstance(payload, dict):
        pages = _get(payload, "pages", "page_info", "pdf_info")
        if isinstance(pages, list):
            for index, node in enumerate(pages):
                yield _page_of(node, index), node
            return
        yield 0, payload


def _iter_children(page_node: Any) -> Iterable[Any]:
    if not isinstance(page_node, dict):
        return
    for key in PAGE_CONTAINER_KEYS:
        value = _get(page_node, key)
        if isinstance(value, list):
            for child in value:
                yield child
            return
    yield page_node


# ---------------------------------------------------------------------------
# 挑选
# ---------------------------------------------------------------------------


def low_confidence(segments: list[Segment], *, threshold: float, limit: int) -> list[Segment]:
    """挑出需要校准的段落：分数越低越靠前。

    没有分数的段落不进清单——它们已经在 MinerU 的默认输出里了，
    校准的重点是"机器自己都没把握"的那部分。
    """
    scored = [s for s in segments if s.score is not None and s.score < threshold]
    scored.sort(key=lambda s: (s.score, s.page_idx, s.order))
    return scored[:limit]



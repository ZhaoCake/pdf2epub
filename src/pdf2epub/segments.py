"""从解析产物里抽出"带置信度的段落"。

这是脚本唯一需要"读懂"解析产物 schema 的地方，所以刻意做得又笨又宽：
（PaddleOCR-VL 不输出置信度，这套抽取主要服务带分数的历史产物 / 未来后端。）

- 有 ``content_list`` 就用它——它是按阅读顺序排好的正文，最接近 LLM 要的东西；
- ``content_list`` 没带分数时，拿 ``model.json`` 的版面框按 IoU 去配对拿分数；
- 两个都没有，就在整个 JSON 里递归找"既有分数又有文本"的节点。

不追求把所有 schema 都吃透：抽不全就少报几条，比"为了正确而崩溃"划算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

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
    """一段被解析后端打了分的内容。"""

    segment_id: str
    page_idx: int
    page_no: int
    kind: str
    text: str
    score: float | None
    score_source: str
    bbox: tuple[float, float, float, float] | None = None
    order: int = 0
    #: 分数是从多少个版面框（通常是 OCR 行）汇总来的；0 表示没配上
    lines: int = 0

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
            "lines": self.lines,
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


#: 0-1 归一化值不会超过这个数
_UNIT_MAX = 1.5
#: MinerU 常用 0-1000 的整数归一化坐标（content_list 就是这么给的）
_SCALE_MAX = 1000.0
#: 归一化之后允许略微出框（文字压到页边或溢出版心都是常事）
_SCALE_SLACK = 1.05

Box = tuple[float, float, float, float]
Converter = Callable[[Box], Box]


def _converter(
    boxes: Iterable[Box | None],
    size: tuple[float, float] | None,
) -> Converter | None:
    """给一组框挑一个"映射到 0-1"的换算方式。

    MinerU 的 bbox 有三套写法，**两套坐标必须按同一套规则处理**，
    否则 IoU 会静默归零——比报错难查得多：

    - 0-1 归一化：``model.json``（vlm 模式）给的就是这个
    - 0-1000 归一化：``content_list`` 给的是这个
    - 真实像素：需要页面尺寸才能换算

    实测同一份产物里，``content_list`` 的 ``[292,135,702,185]`` 除以 1000
    正好等于 ``model.json`` 的 ``[0.294,0.136,0.703,0.186]``。

    判定必须**整份文档一起做**，不能一页一页判：某页恰好只放了一小块内容时，
    按页判断会把这一页误判成 0-1 坐标，于是只有这一页配不上分数，
    比全错更难发现。

    返回 None 表示认不出来——那就别配了。配错分数比没有分数更糟。
    """
    values = [box for box in boxes if box is not None]
    if not values:
        return None

    top = max(max(box) for box in values)
    if top <= _UNIT_MAX:
        return lambda box: box

    if size and size[0] > 1 and size[1] > 1:
        width, height = size

        def by_size(box: Box) -> Box:
            return (box[0] / width, box[1] / height, box[2] / width, box[3] / height)

        if max(max(by_size(box)) for box in values) <= _SCALE_SLACK:
            return by_size

    if top <= _SCALE_MAX:
        return lambda box: (
            box[0] / _SCALE_MAX,
            box[1] / _SCALE_MAX,
            box[2] / _SCALE_MAX,
            box[3] / _SCALE_MAX,
        )

    return None


def _representative_size(sizes: dict[int, tuple[float, float]] | None) -> tuple[float, float] | None:
    """页面尺寸通常整本一致，取一个代表值就够。"""
    if not sizes:
        return None
    return next(iter(sizes.values()))


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


def extract_segments(
    bundle: Bundle,
    *,
    page_sizes: dict[int, tuple[float, float]] | None = None,
    page_offset: int = 0,
) -> list[Segment]:
    """从产物里抽出所有可用的带分段落。

    骨架一律用 ``content_list``：它是**最终正文**，页码、文本、阅读顺序都对。
    分数几乎总是躺在 ``model.json`` 的版面框里，所以做法是拿版面框按 IoU 把分数
    贴回正文条目。

    直接去 ``model.json`` 里捞"带分数的节点"是错的——那些是版面中间产物
    （``content`` 经常是 null，类型是 inline_formula/ocr_text 之类），
    LLM 拿着它们没法对着正文改。

    Args:
        page_sizes: 局部页码 -> (宽, 高)，用于把像素 bbox 归一到 0-1。
        page_offset: 该 chunk 在整本书里的起始页码（多 chunk 时用）。
    """
    if bundle.content_list is not None:
        segments = _from_content_list(bundle, page_sizes=page_sizes, page_offset=page_offset)
        if segments:
            without = sum(1 for s in segments if s.score is None)
            if without == len(segments):
                log.info("正文全部没有置信度可用，交给 LLM 全量处理")
            elif without:
                log.info("%d/%d 条正文没配上置信度，其余按分数筛选", without, len(segments))
            return segments

    for path in bundle.json_sources:
        found = _from_generic(read_json(path), source=path.name, page_offset=page_offset)
        if found:
            log.info("没有 content_list，退回从 %s 里直接捞带分数的节点", path.name)
            return found

    log.info("产物里找不到任何带置信度的段落，交给 LLM 全量处理")
    return []


def _from_content_list(
    bundle: Bundle,
    *,
    page_sizes: dict[int, tuple[float, float]] | None = None,
    page_offset: int = 0,
) -> list[Segment]:
    payload = read_json(bundle.content_list)
    if not isinstance(payload, list):
        return []

    items = [item for item in payload if isinstance(item, dict)]
    if not items:
        return []

    # 分数可能直接写在 content_list 里，也可能要靠 model.json 的版面框配对
    sizes = page_sizes or {}
    layout = _layout_index(bundle, page_sizes=sizes)
    convert = _converter((_bbox_of(item) for item in items), _representative_size(sizes))
    segments: list[Segment] = []

    for order, item in enumerate(items):
        score, source = find_score(item, depth=1)
        bbox = _bbox_of(item)
        local_page = _page_of(item, 0)
        lines = 0
        if score is None and layout and convert is not None and bbox is not None:
            score, source, lines = _lookup_layout_score(layout, local_page, convert(bbox))
        text = _text_of(item)
        if not text:
            continue
        page_idx = local_page + page_offset
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
                lines=lines,
            )
        )
    return segments


@dataclass
class LayoutPage:
    """一页的版面框（已归一到 0-1）。"""

    boxes: list[tuple[Box, float, str]] = field(default_factory=list)


def _layout_index(
    bundle: Bundle,
    *,
    page_sizes: dict[int, tuple[float, float]] | None = None,
) -> dict[int, LayoutPage]:
    """从 model.json / middle 里建 (局部页码 -> LayoutPage) 的索引。"""
    size = _representative_size(page_sizes)

    for path in (bundle.model, bundle.middle):
        if path is None:
            continue
        payload = read_json(path)
        if payload is None:
            continue

        pages: dict[int, list[Any]] = {}
        every_box: list[Box | None] = []
        for page_idx, page_node in _iter_pages(payload):
            children = list(_iter_children(page_node))
            pages[page_idx] = children
            every_box.extend(_bbox_of(child) for child in children)

        convert = _converter(every_box, size)
        if convert is None:
            if any(every_box):
                log.warning(
                    "版面框的坐标系认不出来（既不是 0-1、也不是 0-1000，又缺页面尺寸）；"
                    "置信度无法贴回正文，校准会退化成无分数"
                )
            continue

        index: dict[int, LayoutPage] = {}
        for page_idx, children in pages.items():
            page = LayoutPage()
            for child in children:
                bbox = _bbox_of(child)
                score, source = find_score(child, depth=1)
                if bbox is None or score is None:
                    continue
                page.boxes.append((convert(bbox), score, source))
            index[page_idx] = page
        if any(page.boxes for page in index.values()):
            return index
    return {}


#: 两个框至少要有一半叠在一起才算"同一处内容"
_OVERLAP_MIN = 0.5


def _area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection(a: Box, b: Box) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _lookup_layout_score(
    index: dict[int, LayoutPage],
    page_idx: int,
    bbox_unit: Box | None,
) -> tuple[float | None, str, int]:
    """把版面框的分数贴到正文条目上（两边都已归一到 0-1）。

    不能只比 IoU：版面层带分数的是 ``ocr_text`` ——**逐行**的结果，
    而 ``content_list`` 的条目是**整段**。逐行框和整段框的 IoU 很低，
    只比 IoU 会让"一整段正文 + 里面的公式"全都配不上分数，
    而那恰恰是最需要复核的部分。

    改用**重叠比例**：只要有一半叠在一起就算命中。逐行框会自然落进整段框里，
    公式这种小框也会落进它所在的那一行里。

    一段命中多行时取**最低**分——整段里只要有一处没把握，这一段就值得看一眼。
    """
    if bbox_unit is None:
        return None, "", 0
    page = index.get(page_idx) or index.get(page_idx + 1)
    if page is None or not page.boxes:
        return None, "", 0

    target_area = _area(bbox_unit)
    hits: list[tuple[float, str]] = []
    for box, score, source in page.boxes:
        overlap = _intersection(box, bbox_unit)
        if overlap <= 0:
            continue
        into_target = overlap / target_area if target_area > 0 else 0.0
        into_box = overlap / max(_area(box), 1e-9)
        if into_target >= _OVERLAP_MIN or into_box >= _OVERLAP_MIN:
            hits.append((score, source))

    if not hits:
        return None, "", 0
    worst_score, worst_source = min(hits, key=lambda hit: hit[0])
    return worst_score, f"layout:{worst_source}", len(hits)


def _from_generic(payload: Any, *, source: str, page_offset: int = 0) -> list[Segment]:
    """兜底：递归找"既有分数又有文本"的节点。

    只在没有 ``content_list`` 时才走这条路。页码靠 :func:`_iter_pages` 从
    顶层结构推出来——直接从根节点递归下去的话，``[[页1的 det...], [页2的...]]``
    这种"列表套列表"的形状会让所有节点的页码都停在 0。
    """
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
            page_idx = here + page_offset
            segments.append(
                Segment(
                    segment_id=f"S{len(segments) + 1:05d}",
                    page_idx=page_idx,
                    page_no=page_idx + 1,
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

    for page_idx, page_node in _iter_pages(payload):
        walk(page_node, page_idx)
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
    if isinstance(page_node, list):
        # 有的 schema（MinerU vlm 的 model.json）里，"页"直接就是 det 的列表，
        # 没有 page_info 外壳——这里不认列表的话，版面索引会整片为空。
        yield from page_node
        return
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

    没有分数的段落不进清单——它们已经在解析的默认输出里了，
    校准的重点是"机器自己都没把握"的那部分。
    """
    scored = [s for s in segments if s.score is not None and s.score < threshold]
    scored.sort(key=lambda s: (s.score, s.page_idx, s.order))
    return scored[:limit]



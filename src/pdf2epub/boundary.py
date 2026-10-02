"""分片交界处校验：接缝最容易丢内容，这里把它变成**检查过的结论**。

解析是按片做的（每片不超过 ``max_pages_per_task`` 页，默认 20 页）。片与片之间
的那两页——前一片的最后一页、后一片的第一页——是整本书里唯一"跨边界"的地方：
页码偏移算错、某片少解析一页、服务端丢一页，症状都可能只出现在接缝上，
而且静默无声。

所以这里做三件事，并把结论落成 ``parsing/boundary.md``：

1. **计划**：分片首尾相接、不重不漏（``ingest.coverage_issues``）。
2. **每片**：产物里的页数 == 计划页数；后端自报的提取页数也对得上。
3. **接缝**：接缝两页在产物里是否真的有正文；没有就要求对着页面图核对。

结论会被写进 ``run.counters["boundary"]``，校准工单据此把接缝页列为"必做核对"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import Bundle, read_json
from .ingest import Chunk, coverage_issues
from .logutil import get_logger

log = get_logger("boundary")

#: 正文类条目（用来数"这一页解析出了多少字"）
TEXT_TYPES = {"text", "equation", "title", "list", "table", "image_caption"}


# ---------------------------------------------------------------------------
# 从产物里数数
# ---------------------------------------------------------------------------


def parsed_pages(bundle: Bundle) -> int | None:
    """产物里到底有多少页。数不出来返回 None（不猜）。

    两条线索：``content_list`` 的 ``page_idx``（最可靠），以及 ``model.json``
    的顶层条目数（vlm 模式是"每页一个数组"）。
    """
    data = _load(bundle.content_list)
    if isinstance(data, list):
        indices = [
            item["page_idx"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("page_idx"), int)
        ]
        if indices:
            return max(indices) + 1

    model = _load(bundle.model)
    if isinstance(model, list) and model:
        return len(model)
    if isinstance(model, dict):
        for key in ("pages", "page_info", "layout_dets"):
            value = model.get(key)
            if isinstance(value, list) and value:
                return len(value)
    return None


def page_chars(bundle: Bundle) -> dict[int, int]:
    """每页解析出的正文字符数（键是**分片内部**的页下标，0 基）。

    只认 ``content_list``：它带 ``page_idx``，能明确落到页。数不出来就返回空表，
    调用方按"不知道"处理，而不是当成 0 页。
    """
    data = _load(bundle.content_list)
    if not isinstance(data, list):
        return {}
    counts: dict[int, int] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        page_idx = item.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        text = item.get("text") or item.get("content") or ""
        if not isinstance(text, str):
            continue
        counts[page_idx] = counts.get(page_idx, 0) + len(text.strip())
    return counts


def _load(path: Path | None) -> Any:
    """读 JSON，坏文件按"读不出来"处理——校验不该因为一个坏文件整体失败。"""
    if path is None:
        return None
    try:
        return read_json(path)
    except Exception as exc:  # noqa: BLE001 - 产物不可信，任何异常都算读不出来
        log.warning("交界处校验：读不了 %s（%s），跳过这条线索", path.name, exc)
        return None


# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------


@dataclass
class ChunkCoverage:
    """一个分片的页数对账。"""

    chunk_id: str
    page_start: int          # 1 基，原书页码
    page_end: int
    expected_pages: int
    parsed_pages: int | None = None    # 产物里数出来的
    reported_pages: int | None = None  # 后端自报的提取页数

    @property
    def problem(self) -> str:
        if self.parsed_pages is None:
            return ""
        if self.parsed_pages != self.expected_pages:
            return f"解析出 {self.parsed_pages} 页，计划 {self.expected_pages} 页"
        if self.reported_pages is not None and self.reported_pages != self.expected_pages:
            return f"后端自报 {self.reported_pages} 页，计划 {self.expected_pages} 页"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "expected_pages": self.expected_pages,
            "parsed_pages": self.parsed_pages,
            "reported_pages": self.reported_pages,
            "problem": self.problem,
        }


@dataclass
class Seam:
    """一道接缝：前一片的最后一页 + 后一片的第一页。"""

    index: int
    left_chunk: str
    left_page: int
    right_chunk: str
    right_page: int
    left_chars: int = -1   # -1 = 数不出来
    right_chars: int = -1

    @property
    def empty_pages(self) -> list[int]:
        pairs = ((self.left_page, self.left_chars), (self.right_page, self.right_chars))
        return [page for page, count in pairs if count == 0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "left_chunk": self.left_chunk,
            "left_page": self.left_page,
            "right_chunk": self.right_chunk,
            "right_page": self.right_page,
            "left_chars": self.left_chars,
            "right_chars": self.right_chars,
            "empty_pages": self.empty_pages,
        }


@dataclass
class BoundaryAudit:
    page_count: int
    chunks: list[ChunkCoverage] = field(default_factory=list)
    seams: list[Seam] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "ok": self.ok,
            "issues": list(self.issues),
            "chunks": [c.to_dict() for c in self.chunks],
            "seams": [s.to_dict() for s in self.seams],
        }

    def to_markdown(self, pdf_path: Path | None = None) -> str:
        out: list[str] = [
            "# 交界处校验",
            "",
            f"共 {self.page_count} 页，切成 {self.chunk_count} 片。",
            "下面每一道缝（前一片的最后一页 + 后一片的第一页）都要对着**原书页面**核对。",
            "",
        ]
        if self.issues:
            out.extend(["## 结论：有问题", ""])
            out.extend(f"- {issue}" for issue in self.issues)
            out.append("")
        else:
            out.extend(
                [
                    "## 结论：通过",
                    "",
                    "片数、每片页数、页码衔接都对得上；接缝两页也都解析出了正文。",
                    "注意这只说明没丢页，不代表这两页认得对——校准阶段仍要看图确认。",
                    "",
                ]
            )

        out.extend(["## 每片对账", "", "| 分片 | 原书页码 | 计划页数 | 解析页数 | 后端自报 | 问题 |", "| --- | --- | --- | --- | --- | --- |"])
        for chunk in self.chunks:
            out.append(
                f"| {chunk.chunk_id} | {chunk.page_start}-{chunk.page_end} | {chunk.expected_pages} "
                f"| {_fmt(chunk.parsed_pages)} | {_fmt(chunk.reported_pages)} | {chunk.problem or '—'} |"
            )
        out.append("")

        if self.seams:
            out.extend(["## 接缝清单（校准必看）", "", "| 缝 | 前一片末页 | 该页字数 | 后一片首页 | 该页字数 |", "| --- | --- | --- | --- | --- |"])
            for seam in self.seams:
                out.append(
                    f"| {seam.index} | 第 {seam.left_page} 页（{seam.left_chunk}） | {_fmt_chars(seam.left_chars)} "
                    f"| 第 {seam.right_page} 页（{seam.right_chunk}） | {_fmt_chars(seam.right_chars)} |"
                )
            out.append("")
            out.extend(
                [
                    "## 怎么核对",
                    "",
                    "1. 打开这两页的页面图（校准阶段的 `pages/page-XXXX.png`，页码就是原书页码）。",
                    "2. 在工作稿里找到这两页的正文：**接缝上下一句要能接上**，不能断在半句、"
                    "也不能重复一整段。",
                    "3. 结果写进 `calibrate/notes.md`：哪道缝对过了、哪道缝有问题、怎么处理的。",
                    "",
                ]
            )
        if pdf_path is not None:
            out.extend(["## 原书", "", f"`{pdf_path}`", ""])
        return "\n".join(out)


def _fmt(value: int | None) -> str:
    return "?" if value is None else str(value)


def _fmt_chars(value: int) -> str:
    return "数不出来" if value < 0 else str(value)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def audit(
    *,
    page_count: int,
    chunks: list[Chunk],
    bundles: list[Bundle],
    reported: dict[str, int] | None = None,
) -> BoundaryAudit:
    """对整本书做一次交界处校验。

    Args:
        page_count: 原书页数。
        chunks: prepare 规划出来的分片（带原书页码）。
        bundles: 各分片的解析产物，按 ``root.name == chunk_id`` 配对。
        reported: 后端自报的每片提取页数，键是 chunk_id。
    """
    reported = reported or {}
    by_chunk = {bundle.root.name: bundle for bundle in bundles}

    result = BoundaryAudit(page_count=page_count)
    result.issues.extend(coverage_issues(chunks, page_count))

    for chunk in chunks:
        coverage = ChunkCoverage(
            chunk_id=chunk.chunk_id,
            page_start=chunk.page_start + 1,
            page_end=chunk.page_end + 1,
            expected_pages=chunk.page_count,
            reported_pages=reported.get(chunk.chunk_id),
        )
        bundle = by_chunk.get(chunk.chunk_id)
        if bundle is None:
            result.issues.append(f"{chunk.chunk_id} 没有解析产物")
        else:
            coverage.parsed_pages = parsed_pages(bundle)
            if coverage.problem:
                result.issues.append(
                    f"{chunk.chunk_id}（第 {coverage.page_start}-{coverage.page_end} 页）：{coverage.problem}"
                )
        result.chunks.append(coverage)

    for index, (left, right) in enumerate(zip(chunks, chunks[1:]), start=1):
        seam = Seam(
            index=index,
            left_chunk=left.chunk_id,
            left_page=left.page_end + 1,
            right_chunk=right.chunk_id,
            right_page=right.page_start + 1,
        )
        left_counts = page_chars(by_chunk[left.chunk_id]) if left.chunk_id in by_chunk else {}
        right_counts = page_chars(by_chunk[right.chunk_id]) if right.chunk_id in by_chunk else {}
        if left_counts:
            seam.left_chars = left_counts.get(left.page_count - 1, 0)
        if right_counts:
            seam.right_chars = right_counts.get(0, 0)

        empty = seam.empty_pages
        if empty:
            result.issues.append(
                "接缝第 "
                + "、".join(str(page) for page in empty)
                + f" 页没解析出正文（0 字，缝 {index}），必须对着页面图核对"
            )
        result.seams.append(seam)

    if result.issues:
        log.warning("交界处校验：%d 个问题", len(result.issues))
    else:
        log.info("交界处校验通过：%d 片 / %d 道缝", result.chunk_count, len(result.seams))
    return result

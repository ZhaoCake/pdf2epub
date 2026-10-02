"""PaddleOCR（AI Studio 承载的 PaddleOCR-VL）解析后端。

流程（官方示例）::

    POST /api/v2/ocr/jobs            提交任务（multipart: file / model / optionalPayload）
    GET  /api/v2/ocr/jobs/<jobId>    轮询：state = pending | running | done | failed
    GET  <resultUrl.jsonUrl>         下载 JSONL 结果

一个文件一个 job，没有批量接口、没有 page_ranges——所以切分（``ingest``）在
上传之前就做完，每个分片独立成一个 job。

结果 JSONL 的每行是 ``{"result": {"layoutParsingResults": [...]}}``，每个元素是
一页，带 ``markdown.text`` 和 ``markdown.images``（markdown 里的相对路径 -> 图片
URL）。PaddleOCR-VL **不输出逐段置信度**，所以适配层的另一半职责是把结果归一成
现有流水线吃的 bundle 契约（``full.md`` + ``content_list.json`` + ``images/``）：

- ``full.md``：各页 markdown 按页序拼接；
- ``content_list.json``：每页一条 ``{"type": "text", "page_idx": i, "text": <该页
  markdown>}``。交界处校验（``boundary``）靠它数每页字符、对账页数；置信度没有
  就是没有，校准阶段的低分清单会自然为空；
- ``images/``：图片按 URL 去重后落盘，markdown 里的引用改写成相对路径。

本模块只负责"送上去、等下来、归一成 bundle"，不碰业务语义。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import requests

from .bundle import Bundle, load_bundle
from .config import PaddleConfig
from .errors import ArtifactError, ParseAuthError, ParseError, ParseJobFailed, ParseRateLimitError
from .logutil import get_logger

log = get_logger("paddle")

JOB_URL_PATH = "/api/v2/ocr/jobs"

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: 图片引用里的 URL/路径，转成本地相对路径前先转义正则
_SAFE_NAME = re.compile(r"[^\w.\-]+")


@dataclass
class JobOutcome:
    """一个解析 job 的最终状态。"""

    job_id: str = ""
    state: str = STATE_PENDING
    extracted_pages: int = 0
    total_pages: int = 0
    json_url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state == STATE_DONE and bool(self.json_url)


class PaddleClient:
    """带重试与退避的 PaddleOCR 客户端。"""

    def __init__(self, config: PaddleConfig) -> None:
        self.config = config
        token = config.resolve_token()
        if not token:
            raise ParseAuthError(
                f"未配置 PaddleOCR Token。请设置环境变量 {config.token_env}，"
                "或在配置文件的 [paddle] 节里填 token。"
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"bearer {token}",
                "Accept": "*/*",
            }
        )
        self.base_url = config.base_url.rstrip("/")

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        retryable: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self.config.http_timeout)
        attempts = self.config.max_retries if retryable else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._sleep_backoff(attempt, f"网络异常：{exc}")
                continue

            if response.status_code == 429:
                wait = self._retry_after(response) or self._backoff_seconds(attempt)
                if attempt >= attempts:
                    raise ParseRateLimitError(
                        f"PaddleOCR 限流（HTTP 429），重试 {attempt} 次后仍失败",
                        detail={"url": url},
                    )
                log.warning("PaddleOCR 限流，%.1fs 后重试（第 %d 次）", wait, attempt)
                time.sleep(wait)
                continue

            if response.status_code in (401, 403):
                raise ParseAuthError(
                    f"PaddleOCR 鉴权失败（HTTP {response.status_code}）",
                    detail={"url": url, "body": response.text[:500]},
                )

            if response.status_code >= 500:
                if attempt >= attempts:
                    raise ParseError(
                        f"PaddleOCR 服务端错误 HTTP {response.status_code}",
                        detail={"url": url, "body": response.text[:500]},
                    )
                self._sleep_backoff(attempt, f"HTTP {response.status_code}")
                continue

            return response

        raise ParseError(
            f"请求 PaddleOCR 失败：{url}",
            detail={"error": str(last_error) if last_error else "unknown"},
        )

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        wait = self._backoff_seconds(attempt)
        log.warning("%s，%.1fs 后重试（第 %d 次）", reason, wait, attempt)
        time.sleep(wait)

    def _backoff_seconds(self, attempt: int) -> float:
        base = self.config.retry_backoff * (2 ** (attempt - 1))
        return min(base, 60.0) * (0.7 + random.random() * 0.6)

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # 提交与轮询
    # ------------------------------------------------------------------
    def submit_job(self, path: Path, *, label: str = "") -> str:
        """上传一个分片并创建解析 job，返回 job_id。"""
        path = Path(path)
        payload = {
            "useDocOrientationClassify": self.config.use_doc_orientation_classify,
            "useDocUnwarping": self.config.use_doc_unwarping,
            "useChartRecognition": self.config.use_chart_recognition,
        }
        with path.open("rb") as fh:
            response = self._request(
                "POST",
                f"{self.base_url}{JOB_URL_PATH}",
                data={
                    "model": self.config.model,
                    "optionalPayload": json.dumps(payload, ensure_ascii=False),
                },
                files={"file": (path.name, fh)},
            )

        if response.status_code != 200:
            raise ParseError(
                f"提交解析任务失败（HTTP {response.status_code}）：{label or path.name}",
                detail={"body": response.text[:500]},
            )
        try:
            data = response.json().get("data") or {}
            job_id = str(data.get("jobId") or "")
        except ValueError as exc:
            raise ParseError(
                f"提交解析任务返回了非 JSON 响应：{label or path.name}",
                detail={"body": response.text[:500]},
            ) from exc
        if not job_id:
            raise ParseError(
                f"提交解析任务没有返回 jobId：{label or path.name}",
                detail={"body": response.text[:500]},
            )
        log.info("已提交解析任务 %s（%s，%.1f MB）", job_id, label or path.name, path.stat().st_size / 1e6)
        return job_id

    def query_job(self, job_id: str) -> JobOutcome:
        """查一次 job 状态。"""
        response = self._request("GET", f"{self.base_url}{JOB_URL_PATH}/{job_id}")
        if response.status_code != 200:
            raise ParseError(
                f"查询解析任务失败（HTTP {response.status_code}）：{job_id}",
                detail={"body": response.text[:500]},
            )
        try:
            data = response.json().get("data") or {}
        except ValueError as exc:
            raise ParseError(
                f"查询解析任务返回了非 JSON 响应：{job_id}",
                detail={"body": response.text[:500]},
            ) from exc

        progress = data.get("extractProgress") or {}
        result_url = data.get("resultUrl") or {}
        return JobOutcome(
            job_id=job_id,
            state=str(data.get("state") or STATE_PENDING),
            extracted_pages=int(progress.get("extractedPages") or 0),
            total_pages=int(progress.get("totalPages") or 0),
            json_url=str(result_url.get("jsonUrl") or ""),
            error=str(data.get("errorMsg") or ""),
        )

    def wait_job(self, job_id: str, *, label: str = "") -> JobOutcome:
        """轮询直到 done / failed。超时抛 :class:`ParseError`。"""
        label = label or job_id
        deadline = time.monotonic() + self.config.poll_timeout
        last_pages = -1
        while True:
            outcome = self.query_job(job_id)
            if outcome.state == STATE_DONE:
                log.info(
                    "解析完成 %s（%s）：%d 页", job_id, label, outcome.extracted_pages
                )
                return outcome
            if outcome.state == STATE_FAILED:
                raise ParseJobFailed(
                    f"解析任务失败：{label}（job {job_id}）",
                    detail={"error": outcome.error or "服务端未给出原因"},
                )
            if outcome.extracted_pages != last_pages:
                log.info(
                    "解析中 %s（%s）：%d / %s 页",
                    job_id,
                    label,
                    outcome.extracted_pages,
                    outcome.total_pages or "?",
                )
                last_pages = outcome.extracted_pages
            if time.monotonic() > deadline:
                raise ParseError(
                    f"解析任务超时（>{self.config.poll_timeout:.0f}s）：{label}（job {job_id}）",
                    detail={"state": outcome.state},
                )
            time.sleep(self.config.poll_interval)

    def download_jsonl(self, url: str) -> str:
        """下载结果 JSONL（结果 URL 不带鉴权头，用独立请求）。"""
        attempts = self.config.max_retries
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(url, timeout=self.config.http_timeout)
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._sleep_backoff(attempt, f"结果下载失败：{exc}")
        raise ParseError(
            f"解析结果下载失败：{url}",
            detail={"error": str(last_error) if last_error else "unknown"},
        )


# ---------------------------------------------------------------------------
# JSONL -> bundle 契约
# ---------------------------------------------------------------------------


def iter_pages(jsonl_text: str) -> Iterator[tuple[int, str, dict[str, str]]]:
    """从结果 JSONL 里按顺序抽出 ``(页下标, markdown, {引用路径: 图片URL})``。"""
    page_no = 0
    for line_no, line in enumerate(jsonl_text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = (json.loads(line) or {}).get("result") or {}
        except json.JSONDecodeError as exc:
            raise ArtifactError(
                f"解析结果第 {line_no} 行不是合法 JSON：{exc}",
                detail={"snippet": line[:200]},
            ) from exc
        for res in payload.get("layoutParsingResults") or []:
            markdown = res.get("markdown") or {}
            yield page_no, str(markdown.get("text") or ""), dict(markdown.get("images") or {})
            page_no += 1


def _safe_basename(key: str, fallback: str) -> str:
    name = PurePosixPath(key).name or fallback
    name = _SAFE_NAME.sub("_", name).strip("._") or fallback
    return name


def build_bundle(jsonl_text: str, dest_dir: Path, *, chunk_id: str = "") -> Bundle:
    """把一个分片的解析结果归一成 bundle 契约，落到 ``dest_dir``。

    - ``full.md``：各页 markdown 按页序拼接（引用改写成本地相对路径）；
    - ``content_list.json``：每页一条，交界处校验靠它对账；
    - ``images/``：按 URL 去重下载；
    - ``paddle_result.jsonl``：原始结果原样保存（后缀故意用 .jsonl，
      不让 ``bundle`` 的 JSON 模式把它当成产物再解析一遍）。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    (dest_dir / "paddle_result.jsonl").write_text(jsonl_text, encoding="utf-8")

    used: dict[str, str] = {}      # 图片 URL -> 本地相对路径
    md_parts: list[str] = []
    content_entries: list[dict[str, Any]] = []
    missing_images = 0

    for page_idx, text, images in iter_pages(jsonl_text):
        for key, url in images.items():
            local = used.get(url)
            if local is None:
                stem = _safe_basename(key, f"img_{len(used)}")
                suffix = PurePosixPath(key).suffix or ".jpg"
                candidate = f"images/{stem if stem.endswith(suffix) else stem + suffix}"
                index = 2
                while candidate in used.values():
                    candidate = f"images/{stem}_{index}{suffix}"
                    index += 1
                used[url] = candidate
                local = candidate
                target = dest_dir / local
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(_fetch(url))
                except Exception as exc:  # noqa: BLE001 - 缺图不该让整片作废
                    missing_images += 1
                    log.warning("分片 %s 第 %d 页图片下载失败（%s）：%s", chunk_id, page_idx, url, exc)
            # markdown 里可能是完整 URL，也可能是相对引用；哪个出现就替换哪个
            for needle in sorted({key, url}, key=len, reverse=True):
                if needle and needle in text:
                    text = text.replace(needle, local)

        text = text.strip()
        md_parts.append(text)
        content_entries.append({"type": "text", "page_idx": page_idx, "text": text})

    if not md_parts:
        raise ArtifactError(
            f"解析结果里没有任何页面（分片 {chunk_id}）",
            detail={"lines": len(jsonl_text.splitlines())},
        )
    if missing_images:
        log.warning("分片 %s 共有 %d 张图片没下载下来，后续校验会以缺图出现", chunk_id, missing_images)

    (dest_dir / "full.md").write_text("\n\n".join(md_parts) + "\n", encoding="utf-8")
    (dest_dir / "content_list.json").write_text(
        json.dumps(content_entries, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (dest_dir / ".extracted").write_text("ok", encoding="utf-8")
    log.info(
        "分片 %s 产物归一完成：%d 页 / %d 张图 -> %s",
        chunk_id,
        len(md_parts),
        len(used),
        dest_dir,
    )
    return load_bundle(dest_dir)


def _fetch(url: str) -> bytes:
    """下载一张图片，带少量重试。"""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(url, timeout=60.0)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= 3:
                break
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"图片下载失败：{url} -> {last_error}")

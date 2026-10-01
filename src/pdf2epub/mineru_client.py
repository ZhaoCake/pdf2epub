"""MinerU 官方 API（v4「精准解析」）客户端。

流程（见 https://mineru.net/apiManage/docs）::

    POST /api/v4/file-urls/batch        申请上传链接（自动提交解析）
    PUT  <file_url>                     上传文件（**不要**带 Content-Type）
    GET  /api/v4/extract-results/batch/<batch_id>   轮询
    GET  <full_zip_url>                 下载结果 zip

本模块只负责"把文件送上去、把 zip 拿下来"，不碰业务语义。
错误被归类成 ``MinerUAuthError`` / ``MinerURateLimitError`` / ``MinerUTaskFailed``，
让上层能对"可重试"与"不可重试"分别处理。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

from .config import MineruConfig
from .errors import MinerUAuthError, MinerURateLimitError, MinerUError, MinerUTaskFailed
from .logutil import get_logger

log = get_logger("mineru")

#: 服务端状态
STATE_WAITING_FILE = "waiting-file"
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_CONVERTING = "converting"
STATE_DONE = "done"
STATE_FAILED = "failed"

TERMINAL_STATES = {STATE_DONE, STATE_FAILED}

#: 不可重试的业务错误码（重试也没用）
FATAL_CODES = {
    "A0202",   # Token 错误
    "A0211",   # Token 过期
    "-60002",  # 文件格式识别失败
    "-60003",  # 文件读取失败
    "-60004",  # 空文件
    "-60005",  # 超大小
    "-60006",  # 超页数
    "-60013",  # 无权限访问该任务
    "-60018",  # 每日任务数达上限
    "-60019",  # html 额度不足
}

#: 可重试的瞬态错误码
TRANSIENT_CODES = {
    "-10001",  # 服务异常
    "-60001",  # 生成上传 URL 失败
    "-60007",  # 模型服务暂不可用
    "-60008",  # 文件读取超时
    "-60009",  # 提交队列已满
    "-60017",  # 重试次数达上限（可在新任务重试）
}


@dataclass
class BatchFile:
    """批量提交中的一个文件。"""

    path: Path
    data_id: str = ""
    is_ocr: bool | None = None
    page_ranges: str = ""


@dataclass
class FileResult:
    data_id: str = ""
    file_name: str = ""
    state: str = STATE_PENDING
    zip_url: str = ""
    err_msg: str = ""
    extracted_pages: int = 0
    total_pages: int = 0

    @property
    def done(self) -> bool:
        return self.state == STATE_DONE and bool(self.zip_url)


@dataclass
class ParseOptions:
    model_version: str = "vlm"
    language: str = "ch"
    is_ocr: bool = False
    enable_formula: bool = True
    enable_table: bool = True
    extra_formats: list[str] = field(default_factory=list)


class MinerUClient:
    """带重试与退避的 MinerU 客户端。"""

    def __init__(self, config: MineruConfig, *, language: str = "ch") -> None:
        self.config = config
        self.language = language
        token = config.resolve_token()
        if not token:
            raise MinerUAuthError(
                f"未配置 MinerU Token。请设置环境变量 {config.token_env}，"
                "或在配置文件的 [mineru] 节里填 token。"
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
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
                    raise MinerURateLimitError(
                        f"MinerU 限流（HTTP 429），重试 {attempt} 次后仍失败",
                        detail={"url": url},
                    )
                log.warning("MinerU 限流，%.1fs 后重试（第 %d 次）", wait, attempt)
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                if attempt >= attempts:
                    raise MinerUError(
                        f"MinerU 服务端错误 HTTP {response.status_code}",
                        detail={"url": url, "body": response.text[:500]},
                    )
                self._sleep_backoff(attempt, f"HTTP {response.status_code}")
                continue

            return response

        raise MinerUError(
            f"请求 MinerU 失败：{url}",
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

    def _check_envelope(self, response: requests.Response, action: str) -> dict[str, Any]:
        """校验 ``{code, msg, data}`` 信封，并归类错误。"""
        try:
            payload = response.json()
        except ValueError as exc:
            raise MinerUError(
                f"{action} 返回了非 JSON 响应（HTTP {response.status_code}）",
                detail={"body": response.text[:500]},
            ) from exc

        code = payload.get("code")
        if code == 0:
            return payload.get("data") or {}

        message = payload.get("msg") or "未知错误"
        code_str = str(code)
        detail = {"code": code, "msg": message, "trace_id": payload.get("trace_id")}

        if code_str in {"A0202", "A0211"}:
            raise MinerUAuthError(f"MinerU 鉴权失败（{code}）：{message}", detail=detail)
        if code_str in FATAL_CODES:
            raise MinerUTaskFailed(f"MinerU 拒绝请求（{code}）：{message}", detail=detail)
        if code_str in TRANSIENT_CODES:
            raise MinerUError(f"MinerU 暂时不可用（{code}）：{message}", detail=detail)
        raise MinerUError(f"{action} 失败（{code}）：{message}", detail=detail)

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------
    def _build_payload(self, files: Sequence[BatchFile], options: ParseOptions, *, url_mode: bool) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for item in files:
            entry: dict[str, Any] = {}
            if url_mode:
                entry["url"] = str(item.path)
            else:
                entry["name"] = item.path.name
                if item.data_id:
                    entry["data_id"] = item.data_id
            if item.is_ocr is not None:
                entry["is_ocr"] = item.is_ocr
            if item.page_ranges:
                entry["page_ranges"] = item.page_ranges
            entries.append(entry)

        payload: dict[str, Any] = {
            "files": entries,
            "model_version": options.model_version,
            "enable_formula": options.enable_formula,
            "enable_table": options.enable_table,
            "language": options.language,
        }
        if options.extra_formats:
            payload["extra_formats"] = list(options.extra_formats)
        return payload

    def submit_local_batch(
        self,
        files: Sequence[BatchFile],
        options: ParseOptions,
        *,
        on_upload_progress: Callable[[int, int, str], None] | None = None,
    ) -> str:
        """申请上传链接并逐个上传，返回 batch_id。上传后服务端自动提交解析。"""
        if not files:
            raise MinerUError("没有待上传的文件")
        limit = self.config.max_files_per_batch
        if len(files) > limit:
            raise MinerUError(f"单批最多 {limit} 个文件，当前 {len(files)} 个")

        url = f"{self.base_url}/api/v4/file-urls/batch"
        response = self._request(
            "POST",
            url,
            json=self._build_payload(files, options, url_mode=False),
            headers={"Content-Type": "application/json"},
        )
        data = self._check_envelope(response, "申请上传链接")
        batch_id = data.get("batch_id")
        upload_urls = data.get("file_urls") or []
        if not batch_id or len(upload_urls) != len(files):
            raise MinerUError(
                "申请上传链接返回不符合预期",
                detail={"batch_id": batch_id, "urls": len(upload_urls), "files": len(files)},
            )

        for index, (item, upload_url) in enumerate(zip(files, upload_urls), start=1):
            self._upload_one(upload_url, item.path)
            if on_upload_progress:
                on_upload_progress(index, len(files), item.path.name)

        log.info("已提交 %d 个文件，batch_id=%s", len(files), batch_id)
        return str(batch_id)

    def _upload_one(self, upload_url: str, path: Path) -> None:
        # 注意：官方要求 PUT 时不要设置 Content-Type
        with path.open("rb") as fh:
            response = self._request(
                "PUT",
                upload_url,
                data=fh,
                headers={"Content-Type": None},
                retryable=True,
            )
        if response.status_code not in (200, 201, 204):
            raise MinerUError(
                f"上传失败（HTTP {response.status_code}）：{path.name}",
                detail={"body": response.text[:500]},
            )

    def submit_url_task(self, file_url: str, options: ParseOptions, *, data_id: str = "") -> str:
        """URL 模式（文件已在公网可访问时）。返回 task_id。"""
        payload: dict[str, Any] = {
            "url": file_url,
            "model_version": options.model_version,
            "enable_formula": options.enable_formula,
            "enable_table": options.enable_table,
            "language": options.language,
            "is_ocr": options.is_ocr,
        }
        if data_id:
            payload["data_id"] = data_id
        if options.extra_formats:
            payload["extra_formats"] = list(options.extra_formats)

        response = self._request(
            "POST",
            f"{self.base_url}/api/v4/extract/task",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        data = self._check_envelope(response, "创建解析任务")
        return str(data.get("task_id", ""))

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def query_batch(self, batch_id: str) -> list[FileResult]:
        response = self._request(
            "GET",
            f"{self.base_url}/api/v4/extract-results/batch/{batch_id}",
            headers={"Content-Type": "application/json"},
        )
        data = self._check_envelope(response, "查询批量任务")
        results: list[FileResult] = []
        for item in data.get("extract_result") or []:
            progress = item.get("extract_progress") or {}
            results.append(
                FileResult(
                    data_id=str(item.get("data_id") or ""),
                    file_name=str(item.get("file_name") or ""),
                    state=str(item.get("state") or STATE_PENDING),
                    zip_url=str(item.get("full_zip_url") or ""),
                    err_msg=str(item.get("err_msg") or ""),
                    extracted_pages=int(progress.get("extracted_pages") or 0),
                    total_pages=int(progress.get("total_pages") or 0),
                )
            )
        return results

    def query_task(self, task_id: str) -> FileResult:
        response = self._request(
            "GET",
            f"{self.base_url}/api/v4/extract/task/{task_id}",
            headers={"Content-Type": "application/json"},
        )
        data = self._check_envelope(response, "查询任务")
        progress = data.get("extract_progress") or {}
        return FileResult(
            data_id=str(data.get("data_id") or ""),
            state=str(data.get("state") or STATE_PENDING),
            zip_url=str(data.get("full_zip_url") or ""),
            err_msg=str(data.get("err_msg") or ""),
            extracted_pages=int(progress.get("extracted_pages") or 0),
            total_pages=int(progress.get("total_pages") or 0),
        )

    def wait_batch(
        self,
        batch_id: str,
        *,
        expected: int = 0,
        on_progress: Callable[[list[FileResult]], None] | None = None,
    ) -> list[FileResult]:
        """阻塞轮询直到全部任务进入终态或超时。"""
        deadline = time.monotonic() + self.config.poll_timeout
        last_snapshot = ""
        results: list[FileResult] = []

        while True:
            results = self.query_batch(batch_id)
            snapshot = "|".join(f"{r.data_id}:{r.state}:{r.extracted_pages}" for r in results)
            if on_progress and snapshot != last_snapshot:
                on_progress(results)
                last_snapshot = snapshot

            if results and all(r.state in TERMINAL_STATES for r in results):
                if expected and len(results) < expected:
                    log.warning("返回结果数(%d)少于提交数(%d)，可能存在遗漏", len(results), expected)
                return results

            if time.monotonic() > deadline:
                raise MinerUError(
                    f"等待解析超时（{self.config.poll_timeout}s），batch_id={batch_id}",
                    detail={"states": [(r.file_name, r.state) for r in results]},
                )
            time.sleep(self.config.poll_interval)

    def wait_task(self, task_id: str) -> FileResult:
        deadline = time.monotonic() + self.config.poll_timeout
        while True:
            result = self.query_task(task_id)
            if result.state in TERMINAL_STATES:
                return result
            if time.monotonic() > deadline:
                raise MinerUError(f"等待解析超时，task_id={task_id}")
            time.sleep(self.config.poll_interval)

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def download_zip(self, zip_url: str, destination: Path) -> Path:
        """下载解析结果 zip；文件已存在且非空则直接复用（断点续跑友好）。"""
        destination = Path(destination)
        if destination.is_file() and destination.stat().st_size > 0:
            log.info("结果 zip 已存在，跳过下载：%s", destination.name)
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".part")

        response = self._request("GET", zip_url, stream=True, headers={"Content-Type": None})
        if response.status_code != 200:
            raise MinerUError(f"下载结果失败（HTTP {response.status_code}）", detail={"url": zip_url})

        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
        tmp.replace(destination)
        log.info("已下载结果 zip：%s (%.1f MB)", destination.name, destination.stat().st_size / 1e6)
        return destination


def classify_failures(results: Iterable[FileResult]) -> tuple[list[FileResult], list[FileResult]]:
    """按 done / failed 拆分结果。"""
    done = [r for r in results if r.done]
    failed = [r for r in results if not r.done]
    return done, failed

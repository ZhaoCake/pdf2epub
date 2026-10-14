"""百度翻译开放平台的文本翻译后端（``/ait/api/aiTextTranslate``）。

契约（2026-10 用真实账号探测确认，见运行目录 ``probe_aitrans*.py``）：

- ``POST {base_url}/ait/api/aiTextTranslate``，表单或 JSON 均可；
- 鉴权：**Header ``Authorization: Bearer <API_KEY>`` + 表单里带 ``appid``**——
  只带 Bearer 不带 appid 会报 ``54000 appid is empty``；也兼容通用翻译那套
  ``sign = MD5(appid + q + salt + 密钥)``（32 位小写，q 先不做 URL encode）；
- ``q`` 可用 ``\\n`` 分行，响应按行逐条返回 ``trans_result: [{src, dst}]``——
  行数与顺序在探测中保持 1:1，批量靠它；
- 单次 ``q`` 上限约 6000 字节（UTF-8），探测 5359 字节通过；
- 原文里的 XML 标签会**原样保留**且标签内不做翻译——代码、行内标记用
  ``<x>…</x>`` 包住就能躲开机翻，无需关心标签名；
- "选择模型/翻译指令"的参数名未在公开文档确认（文档要登录才能看），
  配置留了透传口，取值请以 https://fanyi-api.baidu.com/doc/21 为准。

本模块只负责"送上去、等下来、按行对回"，不碰业务语义。
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Any

import requests

from .config import BaiduTransConfig
from .errors import ParseAuthError, ParseError, ParseRateLimitError
from .logutil import get_logger

log = get_logger("baidutrans")

#: 超过 QPS 时的错误码；签名错误单独提示
QPS_ERROR = "54003"
SIGN_ERROR = "54001"
#: 账号层面的问题（没认证/没开通服务），重试无意义
AUTH_ERRORS = {"52003", "54000", "54002"}


class BaiduTransClient:
    """带重试与退避的百度翻译客户端，按"行"为单位批量翻译。"""

    def __init__(self, config: BaiduTransConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.base_url = config.base_url.rstrip("/")
        self.api_key = self._env(config.api_key_env)
        self.appid = self._env(config.appid_env)
        self.secret = self._env(config.secret_env)
        if not self.api_key and not (self.appid and self.secret):
            raise ParseAuthError(
                "未配置百度翻译凭据。请在 .env 里填 BAIDU_TRANS_API_KEY"
                "（API Key 方式），或 BAIDU_TRANS_APPID + BAIDU_TRANS_SECRET（sign 方式）。"
            )
        if not self.api_key and not self.appid:
            # sign 方式也必须带 appid（服务端按它记账）
            raise ParseAuthError("BAIDU_TRANS_APPID 为空：sign 方式鉴权同样需要 appid。")
        self._last_request = 0.0

    # ------------------------------------------------------------------
    # 底层
    # ------------------------------------------------------------------
    def _env(self, name: str) -> str:
        import os

        return os.environ.get(name, "").strip()

    def _throttle(self) -> None:
        wait = self.config.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _mark(self) -> None:
        self._last_request = time.monotonic()

    def _extra_params(self) -> dict[str, str]:
        """模型选择/翻译指令等透传参数，留空就不发。"""
        extra: dict[str, str] = {}
        for name in ("model", "prompt", "domain"):
            value = getattr(self.config, name, "").strip()
            if value:
                extra[name] = value
        return extra

    def _payload(self, q: str) -> dict[str, str]:
        from_lang = self.config.from_lang
        data: dict[str, str] = {"q": q, "from": from_lang, "to": self.config.to_lang}
        if self.api_key:
            # Bearer 方式：服务端仍要求 appid（报 54000 appid is empty）
            data["appid"] = self.appid
        else:
            salt = str(random.randint(1, 10**6))
            sign = hashlib.md5((self.appid + q + salt + self.secret).encode("utf-8")).hexdigest()
            data.update({"appid": self.appid, "salt": salt, "sign": sign})
        data.update(self._extra_params())
        return data

    def _post(self, q: str) -> dict[str, Any]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        attempts = self.config.max_retries
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = self.session.post(
                    f"{self.base_url}{self.config.path}",
                    data=self._payload(q),
                    headers=headers,
                    timeout=self.config.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._sleep(f"网络异常：{exc}", attempt)
                continue
            self._mark()

            if response.status_code in (401, 403):
                raise ParseAuthError(
                    f"百度翻译鉴权失败（HTTP {response.status_code}）：{response.text[:200]}"
                )
            if response.status_code == 429:
                self._sleep("百度翻译限流（HTTP 429）", attempt)
                continue
            if response.status_code >= 500:
                if attempt >= attempts:
                    raise ParseError(
                        f"百度翻译服务端错误 HTTP {response.status_code}",
                        detail={"body": response.text[:300]},
                    )
                self._sleep(f"HTTP {response.status_code}", attempt)
                continue

            try:
                data = response.json()
            except ValueError as exc:
                raise ParseError(
                    "百度翻译返回了非 JSON 响应",
                    detail={"body": response.text[:300]},
                ) from exc
            code = str(data.get("error_code") or "")
            if not code:
                return data
            if code == SIGN_ERROR:
                raise ParseAuthError(
                    "百度翻译签名错误（54001）：检查 BAIDU_TRANS_APPID/BAIDU_TRANS_SECRET，"
                    "sign 只对 appid+q+salt+密钥 做 MD5。"
                )
            if code == QPS_ERROR:
                self._sleep("百度翻译限流（54003）", attempt)
                continue
            if code in AUTH_ERRORS:
                raise ParseAuthError(
                    f"百度翻译拒绝访问（{code} {data.get('error_msg')}）："
                    "检查 appid/API Key，以及对应服务是否已在控制台开通。"
                )
            raise ParseError(
                f"百度翻译返回错误 {code}：{data.get('error_msg')}",
                detail={"data": data},
            )
        raise ParseRateLimitError(
            "百度翻译多次重试后仍失败",
            detail={"error": str(last_error) if last_error else "unknown"},
        )

    def _sleep(self, reason: str, attempt: int) -> None:
        wait = min(self.config.retry_backoff * (2 ** (attempt - 1)), 60.0) * (0.7 + random.random() * 0.6)
        log.warning("%s，%.1fs 后重试（第 %d 次）", reason, wait, attempt)
        time.sleep(wait)

    # ------------------------------------------------------------------
    # 对外：按行翻译
    # ------------------------------------------------------------------
    def translate_lines(self, lines: list[str]) -> list[str]:
        """把一批行送上去，返回等长的译文行。

        ``q`` 用 ``\\n`` 分行，但**服务端不保证按行返回**：实测会把一行再按
        句号/点线拆成多条（20 行请求回了 28 条）。好在每条都带 ``src`` 原文
        片段，且片段按顺序恰好拼回原行——所以按片段累计、拼满一行收一行。
        """
        if not lines:
            return []
        q = "\n".join(lines)
        size = len(q.encode("utf-8"))
        if size > 6000:
            raise ParseError(
                f"单次请求 {size} 字节，超过百度翻译 6000 字节上限：请减小批量。"
            )
        data = self._post(q)
        result = data.get("trans_result") or []
        if not result:
            raise ParseError("百度翻译没有返回任何译文", detail={"q_bytes": size})

        out: list[str] = []
        buf_src = buf_dst = ""
        li = 0
        for n, item in enumerate(result):
            src = str(item.get("src") or "")
            dst = str(item.get("dst") or "")
            if not src:
                continue
            buf_src += src
            buf_dst += dst
            while li < len(lines):
                if buf_src == lines[li]:
                    out.append(buf_dst)
                    buf_src = buf_dst = ""
                    li += 1
                    break
                if lines[li].startswith(buf_src) and len(buf_src) < len(lines[li]):
                    break  # 这一行还没拼满，继续吃下一条片段
                raise ParseError(
                    f"百度翻译第 {n} 条片段拼不回请求的第 {li + 1} 行，放弃整批",
                    detail={"fragment": src[:80], "line": lines[li][:80]},
                )
        if li != len(lines) or buf_src:
            raise ParseError(
                f"百度翻译片段拼完后还有 {len(lines) - li} 行没着落，放弃整批",
                detail={"left": buf_src[:80]},
            )
        return out

    def translate_batched(self, lines: list[str]) -> list[str]:
        """按字节上限自动分批，保持行序合并成一份译文。"""
        out: list[str] = []
        batch: list[str] = []
        budget = self.config.max_bytes

        def flush() -> None:
            nonlocal batch
            if not batch:
                return
            log.info("翻译 %d 行（%d 字节）", len(batch), len("\n".join(batch).encode("utf-8")))
            out.extend(self.translate_lines(batch))
            batch = []

        for line in lines:
            size = len(line.encode("utf-8"))
            if size + 1 > budget:
                # 单行超限：只能单独发（服务端 6000 硬限，仍超就交给调用方处理）
                flush()
                log.warning("单行 %d 字节，单独成批", size)
                out.extend(self.translate_lines([line]))
                continue
            if len(("\n".join([*batch, line])).encode("utf-8")) > budget:
                flush()
            batch.append(line)
        flush()
        return out

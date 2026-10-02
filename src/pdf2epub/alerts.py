"""告警出口。

流水线的对外契约只有两件事：**产出 EPUB** 或 **发出告警**。
所有需要人（或上游自动化）知道的事情都汇聚到这里，落盘为 ``alerts.json``，
并可按配置推送到 webhook / 执行命令。
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .logutil import get_logger
from .runstate import atomic_write_json

log = get_logger("alerts")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {
            Severity.INFO: 0,
            Severity.WARNING: 1,
            Severity.ERROR: 2,
            Severity.CRITICAL: 3,
        }[self]


class AlertCode(str, Enum):
    """稳定的告警码，上游可据此做路由。"""

    # 需要 Agent 干活（不是错误，是"轮到你了"）
    CALIBRATE_REQUIRED = "CALIBRATE_REQUIRED"
    COMPOSE_REQUIRED = "COMPOSE_REQUIRED"
    FORMAT_REQUIRED = "FORMAT_REQUIRED"
    AGENT_SKIPPED = "AGENT_SKIPPED"

    # 输入与解析
    SCANNED_PDF = "SCANNED_PDF"
    ENCRYPTED_PDF = "ENCRYPTED_PDF"
    SPLIT_REQUIRED = "SPLIT_REQUIRED"
    PARSE_FAILED = "PARSE_FAILED"
    PARSE_INCOMPLETE = "PARSE_INCOMPLETE"
    #: 分片交界处校验的结论（通过是 info，页数/接缝对不上是 warning）
    BOUNDARY_CHECK = "BOUNDARY_CHECK"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NO_PAGE_IMAGE = "NO_PAGE_IMAGE"

    # 校验
    FORMAT_FAILED = "FORMAT_FAILED"
    EPUBCHECK_UNAVAILABLE = "EPUBCHECK_UNAVAILABLE"
    EPUBCHECK_FAILED = "EPUBCHECK_FAILED"

    # 结果
    OUTPUT_READY = "OUTPUT_READY"


@dataclass
class Alert:
    code: str
    severity: str
    message: str
    stage: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    #: 同一条告警重复出现时只累计次数，不刷屏。
    #: "还没干活"这类状态每次 run 都会重新判断一次，逐条记下来会把告警列表变成噪声。
    occurrences: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "stage": self.stage,
            "message": self.message,
            "context": self.context,
            "created_at": self.created_at,
            "occurrences": self.occurrences,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.created_at)),
        }


class AlertSink:
    """收集告警 + 按需外发。线程安全由调用方保证（流水线各阶段串行写盘）。"""

    def __init__(
        self,
        path: Path,
        *,
        webhook: str = "",
        command: str = "",
        stage: str = "",
        min_external_severity: Severity = Severity.WARNING,
    ) -> None:
        self.path = Path(path)
        self.webhook = webhook
        self.command = command
        self.stage = stage
        self.min_external_severity = min_external_severity
        self._items: list[Alert] = []
        self._loaded = False
        #: 本次调用（而非历史上）新增或更新过的告警。
        #: 退出码要看的是"这一次跑出没出问题"，而不是"历史上出过没有"。
        self._session: dict[str, str] = {}

    # ---- 载入 / 持久化 --------------------------------------------------
    def load(self) -> "AlertSink":
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._items = [Alert(**{k: v for k, v in item.items() if k in Alert.__annotations__}) for item in raw]
            except Exception as exc:  # 损坏的告警文件不应阻断流水线
                log.warning("告警文件损坏，已忽略：%s (%s)", self.path, exc)
                self._items = []
        self._loaded = True
        return self

    def flush(self) -> None:
        atomic_write_json(self.path, [a.to_dict() for a in self._items])

    # ---- 写入 -----------------------------------------------------------
    def add(
        self,
        code: AlertCode | str,
        severity: Severity | str,
        message: str,
        *,
        stage: str | None = None,
        **context: Any,
    ) -> Alert:
        if not self._loaded:
            # 忘了先 load 的话，flush 会把已有告警冲掉——这里兜住这个坑
            self.load()

        code_value = code.value if isinstance(code, AlertCode) else str(code)
        sev = severity if isinstance(severity, Severity) else Severity(str(severity).lower())

        existing = next(
            (a for a in self._items if a.code == code_value and a.message == message),
            None,
        )
        if existing is not None:
            existing.occurrences += 1
            existing.created_at = time.time()
            existing.context = context or existing.context
            self._session[code_value] = existing.severity
            return existing

        alert = Alert(
            code=code_value,
            severity=sev.value,
            message=message,
            stage=stage if stage is not None else self.stage,
            context=context,
        )
        self._items.append(alert)
        self._session[code_value] = sev.value
        log.log(
            {"info": 20, "warning": 30, "error": 40, "critical": 50}[sev.value],
            "[%s] %s",
            code_value,
            message,
            extra={"extra_fields": {k: v for k, v in context.items() if isinstance(v, (str, int, float, bool))}},
        )
        if sev.rank >= self.min_external_severity.rank:
            self._emit_external(alert)
        return alert

    def info(self, code: AlertCode | str, message: str, **context: Any) -> Alert:
        return self.add(code, Severity.INFO, message, **context)

    def warn(self, code: AlertCode | str, message: str, **context: Any) -> Alert:
        return self.add(code, Severity.WARNING, message, **context)

    def error(self, code: AlertCode | str, message: str, **context: Any) -> Alert:
        return self.add(code, Severity.ERROR, message, **context)

    def critical(self, code: AlertCode | str, message: str, **context: Any) -> Alert:
        return self.add(code, Severity.CRITICAL, message, **context)

    # ---- 外发 -----------------------------------------------------------
    def _emit_external(self, alert: Alert) -> None:
        if self.webhook:
            try:
                import requests

                requests.post(self.webhook, json=alert.to_dict(), timeout=10)
            except Exception as exc:
                log.warning("告警 webhook 推送失败：%s", exc)
        if self.command:
            try:
                subprocess.run(  # noqa: S603 - 用户显式配置的命令
                    self.command,
                    shell=True,
                    input=json.dumps(alert.to_dict(), ensure_ascii=False),
                    text=True,
                    timeout=30,
                    check=False,
                )
            except Exception as exc:
                log.warning("告警命令执行失败：%s", exc)

    # ---- 查询 -----------------------------------------------------------
    @property
    def items(self) -> list[Alert]:
        return list(self._items)

    def by_severity(self, severity: Severity) -> list[Alert]:
        return [a for a in self._items if a.severity == severity.value]

    def has_at_least(self, severity: Severity) -> bool:
        return any(Severity(a.severity).rank >= severity.rank for a in self._items)

    def session_codes(self) -> set[str]:
        """本次调用涉及的告警码。"""
        return set(self._session)

    def has_session_at_least(self, severity: Severity) -> bool:
        """本次调用是否产生了不低于该级别的告警。退出码据此计算。"""
        return any(Severity(level).rank >= severity.rank for level in self._session.values())

    def codes(self) -> set[str]:
        return {a.code for a in self._items}

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self._items:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        return {
            "total": len(self._items),
            "by_severity": counts,
            "by_code": {code: sum(1 for a in self._items if a.code == code) for code in sorted(self.codes())},
        }

    def render(self, *, limit: int = 0) -> str:
        """人类可读的告警列表。"""
        items: Iterable[Alert] = self._items if not limit else self._items[-limit:]
        if not self._items:
            return "（无告警）"
        lines = []
        for alert in items:
            ctx = ", ".join(f"{k}={v}" for k, v in alert.context.items() if not isinstance(v, (dict, list)))
            times = f" ×{alert.occurrences}" if alert.occurrences > 1 else ""
            lines.append(
                f"[{alert.severity.upper():<8}] {alert.code:<22} {alert.message}{times}"
                + (f"  ({ctx})" if ctx else "")
            )
        return "\n".join(lines)

"""日志：控制台人类可读 + JSONL 结构化落盘，两者同源。

设计目标：
- 流水线无人值守，日志必须能事后追责：每条记录带 stage / run_id。
- 同时要给机器读：``logs/pipeline.jsonl`` 一行一条 JSON。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

_CONTEXT: dict[str, Any] = {}
_LOCK = threading.Lock()


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _CONTEXT.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in _CONTEXT.items():
            payload.setdefault(key, getattr(record, key, value))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", "") or "-"
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{stage}] {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def set_context(**kwargs: Any) -> None:
    """设置全局日志上下文（run_id / stage 等）。"""
    with _LOCK:
        _CONTEXT.update(kwargs)


def setup_logging(
    *,
    level: str = "INFO",
    json_file: Path | None = None,
    json_console: bool = False,
    stage: str = "",
) -> logging.Logger:
    """配置日志。幂等：重复调用只同步上下文，不重复挂 handler。"""
    global _configured
    root = logging.getLogger("pdf2epub")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    if stage:
        set_context(stage=stage)

    if not _configured:
        root.handlers.clear()

        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(_JsonFormatter() if json_console else _TextFormatter())
        console.addFilter(_ContextFilter())
        root.addHandler(console)

        if json_file is not None:
            json_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(json_file, encoding="utf-8")
            file_handler.setFormatter(_JsonFormatter())
            file_handler.addFilter(_ContextFilter())
            root.addHandler(file_handler)

        _configured = True
    else:
        # 仅同步 stage 到已有 handler 的 filter
        for handler in root.handlers:
            for filt in handler.filters:
                if isinstance(filt, _ContextFilter):
                    filt.filter(logging.LogRecord("", 0, "", 0, "", None, None))

    return root


def reset_logging() -> None:
    """测试用：拆除所有 handler。"""
    global _configured
    root = logging.getLogger("pdf2epub")
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    _configured = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"pdf2epub.{name}")



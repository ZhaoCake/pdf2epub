"""配置加载：内置默认值 < TOML 文件 < 环境变量。

环境变量覆盖规则：``PDF2EPUB__SECTION__KEY``，例如
``PDF2EPUB__MINERU__MODEL_VERSION=vlm``、``PDF2EPUB__WORKDIR=d:/tmp/run``。
值为字符串，会按目标字段的默认类型自动做 bool/int/float/list 转换。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from .errors import ConfigError

DEFAULT_CONFIG_NAME = "pdf2epub.toml"
ENV_PREFIX = "PDF2EPUB__"


# ---------------------------------------------------------------------------
# 子配置
# ---------------------------------------------------------------------------


@dataclass
class MineruConfig:
    base_url: str = "https://mineru.net"
    token_env: str = "MINERU_TOKEN"
    token: str = ""
    model_version: str = "vlm"
    is_ocr: str = "auto"
    enable_formula: bool = True
    enable_table: bool = True
    extra_formats: list[str] = field(default_factory=list)

    poll_interval: float = 5.0
    poll_timeout: float = 3600.0
    http_timeout: float = 60.0
    max_retries: int = 4
    retry_backoff: float = 2.0

    max_pages_per_task: int = 200
    max_bytes_per_task: int = 200_000_000
    max_files_per_batch: int = 50
    concurrency: int = 4

    def resolve_token(self) -> str:
        if self.token:
            return self.token
        return os.environ.get(self.token_env, "").strip()


@dataclass
class CalibrateConfig:
    """校准工单的参数：找哪些段落、给 LLM 看什么。"""

    #: 低于这个置信度的段落进入校准清单
    score_threshold: float = 0.75
    #: 单个运行最多派多少条（防止一本烂书把工单做成天书）
    max_segments: int = 200
    #: 每个低分段额外附上前后各几页的渲染图
    context_pages: int = 1
    #: 渲染 DPI 与最长边上限
    render_dpi: int = 140
    max_image_width: int = 1500
    #: 工单里每条最多附多少字的原文上下文
    context_chars: int = 600
    #: 低于这个分数就算"肯定要人看"，用于排序
    critical_score: float = 0.5


@dataclass
class ComposeConfig:
    """撰写工单的参数：告诉 LLM 写在哪、按什么规范写。"""

    #: 目录页与正文章节的默认文件名前缀
    chapter_prefix: str = "ch"
    #: 生成一个最小可用的样例章节，让 LLM 有具体参照
    scaffold_sample: bool = True
    #: 书名等元数据缺失时是否允许留空（交给 LLM 从正文推断）
    allow_empty_metadata: bool = True


@dataclass
class ValidateConfig:
    epubcheck_jar: str = ""
    java_cmd: str = "java"
    #: 没有 EPUBCheck 时是否直接失败。默认 False——自检已经能兜住大部分问题
    require_epubcheck: bool = False
    #: 达到该级别算"不合格"：FATAL / ERROR / WARNING
    fail_on_severity: str = "ERROR"


# ---------------------------------------------------------------------------
# 顶层配置
# ---------------------------------------------------------------------------


@dataclass
class Config:
    workdir: str = ".pdf2epub"
    language: str = "ch"
    log_level: str = "INFO"
    log_json: bool = True
    alert_webhook: str = ""
    alert_command: str = ""

    mineru: MineruConfig = field(default_factory=MineruConfig)
    calibrate: CalibrateConfig = field(default_factory=CalibrateConfig)
    compose: ComposeConfig = field(default_factory=ComposeConfig)
    validate: ValidateConfig = field(default_factory=ValidateConfig)

    #: 配置文件来源路径，主要用于诊断
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 加载逻辑
# ---------------------------------------------------------------------------


def _coerce(raw: str, default: Any) -> Any:
    """按默认值类型把环境变量字符串转成目标类型。"""
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on", "y"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"环境变量值 {raw!r} 无法解析为整数") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"环境变量值 {raw!r} 无法解析为浮点数") from exc
    if isinstance(default, list):
        items = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        if default and all(isinstance(x, int) for x in default):
            return [int(x) for x in items]
        return items
    return raw


def _merge_into(instance: Any, data: dict[str, Any], where: str) -> None:
    """把 dict 合并进 dataclass 实例；遇到未知键直接报错（避免静默打错配置）。"""
    known = {f.name for f in fields(instance)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(
                f"配置节 [{where}] 含未知字段 {key!r}；可用字段：{sorted(known)}"
            )
        current = getattr(instance, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"配置节 [{where}.{key}] 应为表（table）")
            _merge_into(current, value, f"{where}.{key}")
        else:
            setattr(instance, key, value)


def load_config(
    path: str | Path | None = None,
    *,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """加载配置。

    Args:
        path: 显式配置文件路径；为 None 时在 cwd 下查找 ``pdf2epub.toml``。
        cwd: 查找起点，默认当前工作目录。
        overrides: 最高优先级的点分键覆盖，如 ``{"build.title": "X"}``。
    """
    cwd = Path(cwd or Path.cwd())
    config = Config()

    resolved: Path | None = None
    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise ConfigError(f"配置文件不存在：{candidate}")
        resolved = candidate
    else:
        default = cwd / DEFAULT_CONFIG_NAME
        if default.is_file():
            resolved = default

    if resolved is not None:
        try:
            with resolved.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"配置文件 TOML 语法错误：{resolved} -> {exc}") from exc
        config.source_path = str(resolved)
        _merge_into(config, data, "root")

    _apply_env(config)
    for dotted, value in (overrides or {}).items():
        _set_dotted(config, dotted, value)

    _validate(config)
    return config


def _apply_env(config: Config) -> None:
    for name, raw in os.environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        dotted = name[len(ENV_PREFIX) :].lower().replace("__", ".")
        if not dotted:
            continue
        _set_dotted(config, dotted, raw, from_env=True)


def _set_dotted(config: Config, dotted: str, value: Any, *, from_env: bool = False) -> None:
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return
    target: Any = config
    for part in parts[:-1]:
        if not is_dataclass(target) or not hasattr(target, part):
            if from_env:
                return  # 未知的环境变量路径静默忽略
            raise ConfigError(f"未知的配置路径：{dotted}")
        target = getattr(target, part)
    leaf = parts[-1]
    if not is_dataclass(target) or not hasattr(target, leaf):
        if from_env:
            return
        raise ConfigError(f"未知的配置路径：{dotted}")
    current = getattr(target, leaf)
    if from_env:
        value = _coerce(str(value), current)
    elif isinstance(current, bool) and isinstance(value, str):
        value = _coerce(value, current)
    setattr(target, leaf, value)


def _validate(config: Config) -> None:
    if config.mineru.is_ocr not in {"auto", "true", "false", True, False}:
        raise ConfigError("mineru.is_ocr 只能是 auto / true / false")
    if isinstance(config.mineru.is_ocr, str):
        config.mineru.is_ocr = config.mineru.is_ocr.lower()
    if config.mineru.model_version not in {"pipeline", "vlm", "MinerU-HTML"}:
        raise ConfigError("mineru.model_version 只能是 pipeline / vlm / MinerU-HTML")
    if not 0.0 <= config.calibrate.critical_score <= config.calibrate.score_threshold <= 1.0:
        raise ConfigError(
            "calibrate 阈值不合法：需要 0 <= critical_score <= score_threshold <= 1"
        )
    if config.calibrate.max_segments <= 0:
        raise ConfigError("calibrate.max_segments 必须为正数")
    if config.calibrate.render_dpi <= 0:
        raise ConfigError("calibrate.render_dpi 必须为正数")
    if config.validate.fail_on_severity.upper() not in {"FATAL", "ERROR", "WARNING"}:
        raise ConfigError("validate.fail_on_severity 只能是 FATAL / ERROR / WARNING")
    if not config.language.strip():
        raise ConfigError("language 不能为空")



"""配置加载：内置默认值 < ``.env`` < TOML 文件 < 真实环境变量 < 显式覆盖。

环境变量覆盖规则：``PDF2EPUB__SECTION__KEY``，例如
``PDF2EPUB__PADDLE__MODEL=PaddleOCR-VL-1.6``、``PDF2EPUB__WORKDIR=d:/tmp/run``。
值为字符串，会按目标字段的默认类型自动做 bool/int/float/list 转换。

``.env``（当前工作目录下）专门用来放 Token 这类密钥，内容会被注入 ``os.environ``，
因此 :meth:`PaddleConfig.resolve_token` 也能直接读到。已有同名环境变量时不覆盖。
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
from .logutil import get_logger

log = get_logger("config")

DEFAULT_CONFIG_NAME = "pdf2epub.toml"
DEFAULT_ENV_NAME = ".env"
ENV_PREFIX = "PDF2EPUB__"


def load_env_file(path: Path | str | None = None, *, cwd: Path | None = None) -> Path | None:
    """把 ``.env`` 里的键值对注入 ``os.environ``，返回实际加载的文件。

    刻意不引入 python-dotenv：这个格式简单到不值得多一个依赖。

    规则：
    - **已有的真实环境变量优先**，``.env`` 只补空缺。这样临时
      ``$env:PADDLE_TOKEN=...`` 覆盖 ``.env`` 的行为符合直觉。
    - 支持 ``KEY=VALUE``、``export KEY=VALUE``、``#`` 注释、单双引号包裹。
    - 值**原样使用**，不做变量展开——secret 里出现 ``$`` 不该被吃掉。
    """
    base = Path(cwd) if cwd else Path.cwd()
    target = Path(path) if path else base / DEFAULT_ENV_NAME
    if not target.is_file():
        return None

    for number, raw in enumerate(target.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            log.debug("%s 第 %d 行不是 KEY=VALUE，已跳过", target.name, number)
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value

    log.debug("已加载环境文件：%s", target)
    return target


# ---------------------------------------------------------------------------
# 子配置
# ---------------------------------------------------------------------------


@dataclass
class PaddleConfig:
    """PaddleOCR（AI Studio 承载的 PaddleOCR-VL）解析后端。"""

    base_url: str = "https://paddleocr.aistudio-app.com"
    token_env: str = "PADDLE_TOKEN"
    token: str = ""
    model: str = "PaddleOCR-VL-1.6"

    #: optionalPayload 开关（官方示例给出的三项）
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_chart_recognition: bool = False

    poll_interval: float = 5.0
    poll_timeout: float = 3600.0
    http_timeout: float = 60.0
    max_retries: int = 4
    retry_backoff: float = 2.0

    #: 每片页数。API 本身没有页数限制的说法，但默认取 20：小片并行解析更快，
    #: 片与片的接缝（前一片末页 + 后一片首页）也才核得过来。见 boundary.py。
    max_pages_per_task: int = 20
    max_bytes_per_task: int = 200_000_000
    #: 一次 run 最多同时提交多少个解析 job（防止失控刷爆额度）
    max_files_per_batch: int = 50

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
    #: auto = 找得到就用；require = 必须跑，找不到就判失败；off = 完全不跑
    epubcheck_mode: str = "auto"
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

    paddle: PaddleConfig = field(default_factory=PaddleConfig)
    calibrate: CalibrateConfig = field(default_factory=CalibrateConfig)
    compose: ComposeConfig = field(default_factory=ComposeConfig)
    validate: ValidateConfig = field(default_factory=ValidateConfig)

    #: 配置文件来源路径，主要用于诊断
    source_path: str = ""
    #: 实际加载的 .env 路径，主要用于诊断
    env_path: str = ""

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

    优先级（后者覆盖前者）：

    内置默认值 < ``.env`` < ``pdf2epub.toml`` < 真实环境变量
    （``PDF2EPUB__SECTION__KEY``）< ``overrides``

    ``.env`` 只负责把 Token 之类的密钥填进环境，所以排在配置文件之前——
    它补的是"空缺"，不该悄悄盖掉用户显式写下的配置。

    Args:
        path: 显式配置文件路径；为 None 时在 cwd 下查找 ``pdf2epub.toml``。
        cwd: 查找起点，同时决定 ``.env`` 的位置，默认当前工作目录。
        overrides: 最高优先级的点分键覆盖，如 ``{"build.title": "X"}``。
    """
    cwd = Path(cwd or Path.cwd())
    config = Config()

    # .env 里的值会注入 os.environ，于是下面的 _apply_env / resolve_token 都能看见；
    # 真实环境变量已存在时不覆盖。
    env_file = load_env_file(cwd=cwd)

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

    config.env_path = str(env_file) if env_file else ""

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
    if not config.paddle.model.strip():
        raise ConfigError("paddle.model 不能为空")
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
    if config.validate.epubcheck_mode not in {"auto", "require", "off"}:
        raise ConfigError("validate.epubcheck_mode 只能是 auto / require / off")
    if not config.language.strip():
        raise ConfigError("language 不能为空")



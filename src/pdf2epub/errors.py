"""统一异常层级。

约定：
- ``Pdf2EpubError`` 及其子类都是"预期内"的失败，会被流水线捕获、记入告警，
  并以稳定的退出码结束，而不是抛栈。
- 退出码语义见 ``pdf2epub.cli`` 的 EXIT_* 常量。
"""

from __future__ import annotations

from typing import Any


class Pdf2EpubError(Exception):
    """所有可预期错误的基类。"""

    exit_code: int = 1
    #: 机器可读的错误码，便于上游自动化判断
    code: str = "E_UNKNOWN"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class ConfigError(Pdf2EpubError):
    code = "E_CONFIG"


class InputError(Pdf2EpubError):
    """输入 PDF 缺失、损坏、加密或不受支持。"""

    code = "E_INPUT"


class MinerUError(Pdf2EpubError):
    """MinerU 调用链上的任何失败（鉴权、限流、服务端、超时）。"""

    code = "E_MINERU"


class MinerUAuthError(MinerUError):
    code = "E_MINERU_AUTH"


class MinerURateLimitError(MinerUError):
    code = "E_MINERU_RATELIMIT"


class MinerUTaskFailed(MinerUError):
    code = "E_MINERU_TASK"


class ArtifactError(Pdf2EpubError):
    """MinerU 产物解压/读取失败。"""

    code = "E_ARTIFACT"


class AgentPause(Pdf2EpubError):
    """流水线暂停，等 Agent（LLM）干活。这是预期的"轮到你了"，不是失败。"""

    code = "E_AGENT_PAUSE"
    exit_code = 3


class CalibrateRequired(AgentPause):
    """需要 Agent 对照页面图校准低分段落。"""

    code = "E_CALIBRATE_REQUIRED"


class ComposeRequired(AgentPause):
    """需要 Agent 直接把内容写成 EPUB。"""

    code = "E_COMPOSE_REQUIRED"


class FormatRequired(AgentPause):
    """格式校验没过，需要 Agent 改文本。"""

    code = "E_FORMAT_REQUIRED"


class TaskError(Pdf2EpubError):
    """工单目录的内容不合法（比如 book.json 写错了）。"""

    code = "E_TASK"


class BuildError(Pdf2EpubError):
    code = "E_BUILD"


class ValidationError(Pdf2EpubError):
    """EPUBCheck 或自检判定不合格。"""

    code = "E_VALIDATION"


class DependencyMissing(Pdf2EpubError):
    """缺少可选依赖，且当前配置要求该能力。"""

    code = "E_DEPENDENCY"

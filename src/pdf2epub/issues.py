"""校验问题的统一模型。

EPUBCheck 的输出和自检的输出必须能被同一套逻辑消费，才能做"看问题 -> 定修复
-> 再校验"的闭环。所以两边都归一到 ``Issue``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SEVERITY_RANK = {"USAGE": -1, "INFO": 0, "WARNING": 1, "ERROR": 2, "FATAL": 3}
BLOCKING = {"ERROR", "FATAL"}


@dataclass
class Issue:
    id: str
    severity: str
    message: str
    path: str = ""
    line: int = 0
    column: int = 0
    source: str = "selflint"     # selflint | epubcheck
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity.upper(), 0)

    @property
    def blocking(self) -> bool:
        return self.severity.upper() in BLOCKING

    @property
    def normalized_path(self) -> str:
        """规整为**相对构建根**的路径。

        EPUBCheck 的路径形态很杂：``OEBPS/text/ch1.xhtml``、
        ``C:/x/book.epub/OEBPS/text/ch1.xhtml``、``book.epub/OEBPS/...``。
        统一从 ``OEBPS/`` 处截断即可稳定映射到构建树。
        """
        value = (self.path or "").replace("\\", "/").lstrip("/")
        marker = "OEBPS/"
        index = value.find(marker)
        if index >= 0:
            return value[index:]
        if value.startswith("META-INF/"):
            return value
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "source": self.source,
        }


@dataclass
class LintReport:
    issues: list[Issue] = field(default_factory=list)
    checked_files: int = 0
    source: str = "selflint"
    ran: bool = True
    error: str = ""

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def extend(self, issues: Iterable[Issue]) -> None:
        self.issues.extend(issues)

    @property
    def blocking_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.blocking]

    def count(self, severity: str) -> int:
        return sum(1 for i in self.issues if i.severity.upper() == severity.upper())

    @property
    def passed(self) -> bool:
        return not self.blocking_issues

    def by_id(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.issues:
            out[issue.id] = out.get(issue.id, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def to_dict(self, *, limit: int = 200) -> dict[str, Any]:
        return {
            "source": self.source,
            "ran": self.ran,
            "error": self.error,
            "checked_files": self.checked_files,
            "passed": self.passed,
            "counts": {
                "FATAL": self.count("FATAL"),
                "ERROR": self.count("ERROR"),
                "WARNING": self.count("WARNING"),
                "INFO": self.count("INFO"),
            },
            "by_id": self.by_id(),
            "issues": [i.to_dict() for i in self.issues[:limit]],
            "truncated": max(0, len(self.issues) - limit),
        }

    def render(self, *, limit: int = 60) -> str:
        if not self.ran:
            return f"[{self.source}] 未执行：{self.error}"
        if not self.issues:
            return f"[{self.source}] 通过（检查 {self.checked_files} 个文件）"
        lines = [
            f"[{self.source}] {len(self.issues)} 个问题"
            f"（FATAL {self.count('FATAL')} / ERROR {self.count('ERROR')} / "
            f"WARNING {self.count('WARNING')} / INFO {self.count('INFO')}）"
        ]
        for issue in self.issues[:limit]:
            location = f"{issue.path}:{issue.line}" if issue.line else issue.path
            lines.append(f"  {issue.severity:<7} {issue.id:<10} {location}  {issue.message}")
        if len(self.issues) > limit:
            lines.append(f"  ... 其余 {len(self.issues) - limit} 条见报告文件")
        return "\n".join(lines)


def merge_reports(*reports: LintReport) -> LintReport:
    """合并多个校验来源。

    ``ran`` 取"任一来源跑了"——因为自检总会跑，只要它跑过就有结论。
    未执行的来源记进 ``error``，避免"没校验"被误当成"校验通过"。
    """
    merged = LintReport(source="+".join(r.source for r in reports))
    merged.ran = False
    for report in reports:
        merged.checked_files += report.checked_files
        merged.extend(report.issues)
        if report.ran:
            merged.ran = True
        elif report.error:
            merged.error = (merged.error + " | " + report.error).strip(" |")
    return merged



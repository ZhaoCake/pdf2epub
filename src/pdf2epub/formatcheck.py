"""格式校验：跑自检 + EPUBCheck，把问题写成一份 LLM 能照着改的报告。

这里**只报告、不修复**。改文本是 Agent 的事：它看得懂上下文，脚本看不懂。
脚本的价值在于把问题定位得足够准——文件、行号、原文片段——让 Agent 一眼知道改哪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import epubcheck as epubcheck_mod
from . import selflint
from .config import ValidateConfig
from .errors import ValidationError
from .issues import Issue, LintReport, merge_reports
from .logutil import get_logger
from .runstate import Run, atomic_write_json

log = get_logger("formatcheck")

_EXCERPT_RADIUS = 3
_MAX_LISTED = 60
SEVERITY_ORDER = {"FATAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3, "USAGE": 4}


@dataclass
class CheckResult:
    passed: bool
    report: LintReport
    output: Path
    epubcheck: dict[str, Any] = field(default_factory=dict)
    #: 外部 EPUBCheck 到底跑没跑成。没跑成时必须让上游知道——
    #: "只跑了自检"和"跑了两道校验"是两回事，不能都叫"通过"。
    epubcheck_ran: bool = True
    epubcheck_reason: str = ""

    @property
    def blocking(self) -> list[Issue]:
        return self.report.blocking_issues

    def to_dict(self, *, limit: int = 100) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "output": str(self.output),
            "epubcheck_ran": self.epubcheck_ran,
            "epubcheck_reason": self.epubcheck_reason,
            "epubcheck": self.epubcheck,
            "report": self.report.to_dict(limit=limit),
        }


def run_check(run: Run, config: ValidateConfig, *, output: Path | None = None) -> CheckResult:
    """自检 + EPUBCheck，并把结果落盘到 ``check/``。"""
    output = Path(output) if output else run.output_epub()

    self_report = selflint.lint_epub(output)
    external = epubcheck_mod.run(output, config)
    if not external.ran:
        reason = external.error or "未找到 epubcheck"
        if config.epubcheck_mode == "require":
            raise ValidationError(
                f"未执行 EPUBCheck，但配置要求必须执行：{reason}",
                detail={
                    "hint": "把 epubcheck.jar 放进 tools/ 或设置 EPUBCHECK_JAR；"
                    "也可以把 validate.epubcheck_mode 改成 auto/off"
                },
            )
        log.warning("EPUBCheck 未执行：%s", reason)

    report = merge_reports(self_report, external) if external.ran else _merge_partial(self_report, external)
    passed = _passes(report, config)

    run.check_dir.mkdir(parents=True, exist_ok=True)
    run.check_report.write_text(
        render_report(run, report, passed=passed, tool=epubcheck_mod.tool_info(config)),
        encoding="utf-8",
    )
    atomic_write_json(
        run.check_json,
        {
            "schema_version": 1,
            "at": datetime.now(timezone.utc).isoformat(),
            "output": str(output),
            "passed": passed,
            "fail_on": config.fail_on_severity.upper(),
            "epubcheck": epubcheck_mod.tool_info(config),
            "epubcheck_ran": external.ran,
            "epubcheck_reason": "" if external.ran else (external.error or "未找到 epubcheck"),
            "report": report.to_dict(limit=200),
        },
    )

    log.info(
        "校验结论：%s（FATAL %d / ERROR %d / WARNING %d）",
        "通过" if passed else "不合格",
        report.count("FATAL"),
        report.count("ERROR"),
        report.count("WARNING"),
    )
    return CheckResult(
        passed=passed,
        report=report,
        output=output,
        epubcheck=epubcheck_mod.tool_info(config),
        epubcheck_ran=external.ran,
        epubcheck_reason="" if external.ran else (external.error or "未找到 epubcheck"),
    )


def _merge_partial(self_report: LintReport, external: LintReport) -> LintReport:
    """EPUBCheck 没跑成时，保留自检结论，同时把"没校验"这件事记下来。"""
    if external.error:
        self_report.error = external.error
    self_report.extend(external.issues)
    return self_report


def _passes(report: LintReport, config: ValidateConfig) -> bool:
    threshold = config.fail_on_severity.upper()
    blocking = [issue for issue in report.issues if issue.severity.upper() in {"ERROR", "FATAL"}]
    if threshold == "WARNING":
        blocking += [issue for issue in report.issues if issue.severity.upper() == "WARNING"]
    elif threshold == "FATAL":
        blocking = [issue for issue in blocking if issue.severity.upper() == "FATAL"]
    return not blocking


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _excerpt(run: Run, issue: Issue) -> list[str]:
    """从构建树里摘出问题附近几行——这是 Agent 定位问题的关键线索。"""
    relative = (issue.normalized_path or "").lstrip("./")
    if not relative or issue.line <= 0:
        return []
    target = run.build_dir / relative
    if not target.is_file():
        return []
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    start = max(1, issue.line - _EXCERPT_RADIUS)
    stop = min(len(lines), issue.line + _EXCERPT_RADIUS)
    return [
        f"{'>>' if number == issue.line else '  '} {number:>5} | {lines[number - 1]}"
        for number in range(start, stop + 1)
    ]


def _sort_key(issue: Issue) -> tuple[int, str, int]:
    return (SEVERITY_ORDER.get(issue.severity.upper(), 9), issue.normalized_path or "", issue.line)


def render_report(run: Run, report: LintReport, *, passed: bool, tool: dict[str, Any]) -> str:
    blocking = sorted(report.blocking_issues, key=_sort_key)
    warnings = sorted(
        (i for i in report.issues if i.severity.upper() == "WARNING"), key=_sort_key
    )

    out: list[str] = [
        "# 格式校验报告",
        "",
        ("**结论：通过。**" if passed else "**结论：不合格。**"),
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 产物 | `{report_output(run)}` |",
        f"| 可编辑根 | `{run.build_dir}` |",
        f"| 阻断性问题 | **{len(blocking)}**（FATAL {report.count('FATAL')} / ERROR {report.count('ERROR')}） |",
        f"| 警告 | {len(warnings)} |",
        f"| EPUBCheck | {tool.get('reason') or '已执行'} |",
        "",
    ]

    if passed:
        out.extend(
            [
                "## 收工",
                "",
                "格式没问题，EPUB 可以用了。",
                "",
            ]
        )
        if warnings:
            out.extend(_warning_section(warnings))
        return "\n".join(out)

    out.extend(
        [
            "## 怎么处理",
            "",
            f"1. 直接修改 `{run.build_dir}` 下对应的文件——报告里给到了文件和行号。",
            "2. 只改能被这些问题解释的地方，不要顺手重写整章。",
            "3. 改完再跑一次 `pdf2epub run`；通过就收工。",
            "4. 确实修不了的，用 `pdf2epub done --accept` 收尾，"
            "产物会保留，同时留下明确告警，绝不静默当成功。",
            "",
            "## 问题清单",
            "",
        ]
    )

    current_path = None
    listed = 0
    for issue in blocking:
        if listed >= _MAX_LISTED:
            out.append(f"（另有 {len(blocking) - listed} 条未列出，完整清单见 `{run.check_json}`）")
            out.append("")
            break
        path = issue.normalized_path or "(全书)"
        if path != current_path:
            current_path = path
            out.extend([f"### `{path}`", ""])
        location = f"{path}:{issue.line}" if issue.line else path
        out.append(f"**{issue.id}** ({issue.severity}) — `{location}`")
        out.append("")
        out.append(issue.message)
        out.append("")
        excerpt = _excerpt(run, issue)
        if excerpt:
            out.extend(["```text", *excerpt, "```", ""])
        listed += 1

    if warnings:
        out.extend(_warning_section(warnings))
    return "\n".join(out)


def report_output(run: Run) -> str:
    epub = run.output_epub()
    return str(epub) if epub.exists() else str(epub)


def _warning_section(warnings: list[Issue]) -> list[str]:
    lines = ["## 附：警告（不阻断，顺手能改就改）", ""]
    for issue in warnings[:30]:
        location = issue.normalized_path or "(全书)"
        if issue.line:
            location = f"{location}:{issue.line}"
        lines.append(f"- `{issue.id}` {location} — {issue.message}")
    if len(warnings) > 30:
        lines.append(f"- …其余 {len(warnings) - 30} 条见 report.json")
    lines.append("")
    return lines

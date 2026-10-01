"""EPUBCheck 封装。

EPUBCheck 是 EPUB 合规的事实标准，但它是 Java 工具，安装方式五花八门。
这里做三件事：

1. **自动发现**：配置 → 环境变量 → PATH → 常见安装位置 → 工作目录下的 jar。
2. **多版本兼容**：5.x 用 ``-j <file>``，4.x 用 ``-m json``，再不济解析文本输出。
3. **优雅降级**：找不到就明确报告"未执行"，由调用方决定是否阻断，
   绝不允许"校验没跑"被静默当成"校验通过"。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ValidateConfig
from .issues import Issue, LintReport
from .logutil import get_logger

log = get_logger("epubcheck")

_TEXT_RE = re.compile(
    r"^(FATAL|ERROR|WARNING|INFO|USAGE)\(([A-Za-z0-9_-]+)\):\s*(.*)$"
)
_LOCATION_RE = re.compile(r"^(.*?)\((\d+),(\d+)\)$")

_SEARCH_PATTERNS = (
    "epubcheck*.jar",
    "epubcheck/**/epubcheck*.jar",
    "tools/epubcheck*.jar",
    "tools/**/epubcheck*.jar",
    "vendor/**/epubcheck*.jar",
    "*.jar",
)
_SEARCH_ROOTS = (
    ".", "tools", "vendor", "bin", "lib",
    "~/epubcheck", "~/.local/share/epubcheck", "~/.epubcheck",
    "C:/Program Files/epubcheck", "C:/epubcheck", "/opt/epubcheck", "/usr/local/lib/epubcheck",
)


@dataclass
class EpubCheckTool:
    java: str = ""
    jar: str = ""
    available: bool = False
    reason: str = ""

    def describe(self) -> str:
        if not self.available:
            return f"不可用（{self.reason}）"
        return f"java={self.java} jar={self.jar}"


def discover(config: ValidateConfig, *, extra_roots: list[Path] | None = None) -> EpubCheckTool:
    """按优先级找到可用的 EPUBCheck。"""
    java = _find_java(config.java_cmd)

    # 1) 显式配置
    if config.epubcheck_jar:
        jar = Path(config.epubcheck_jar).expanduser()
        if jar.is_file():
            if not java:
                return EpubCheckTool(reason=f"找到 jar 但找不到 java：{jar}")
            return EpubCheckTool(java=java, jar=str(jar), available=True)
        log.warning("配置的 epubcheck_jar 不存在：%s", jar)

    # 2) 环境变量
    env_jar = os.environ.get("EPUBCHECK_JAR", "").strip()
    if env_jar:
        jar = Path(env_jar).expanduser()
        if jar.is_file():
            if not java:
                return EpubCheckTool(reason="设置了 EPUBCHECK_JAR 但找不到 java")
            return EpubCheckTool(java=java, jar=str(jar), available=True)

    # 3) PATH 里的 epubcheck 可执行文件（官方分发包自带的启动脚本）
    launcher = shutil.which("epubcheck") or shutil.which("epubcheck.bat")
    if launcher:
        return EpubCheckTool(java=launcher, jar="", available=True)

    # 4) 常见位置扫描
    for root in _iter_search_roots(extra_roots):
        for pattern in _SEARCH_PATTERNS:
            for candidate in sorted(root.glob(pattern)):
                if candidate.is_file() and candidate.suffix.lower() == ".jar":
                    if not java:
                        return EpubCheckTool(reason="找到 epubcheck jar 但找不到 java，请安装 JRE 并确保 java 在 PATH 上")
                    return EpubCheckTool(java=java, jar=str(candidate), available=True)

    if not java:
        return EpubCheckTool(reason="既没有 epubcheck 也没有 java")
    return EpubCheckTool(
        reason="未找到 epubcheck.jar。可从 https://github.com/w3c/epubcheck/releases 下载，"
        "或用 EPUBCHECK_JAR 环境变量指定路径",
        java=java,
    )


def _find_java(command: str) -> str:
    if command:
        found = shutil.which(command)
        if found:
            return found
        candidate = Path(command)
        if candidate.is_file():
            return str(candidate)
    return ""


def _iter_search_roots(extra_roots: list[Path] | None) -> list[Path]:
    roots: list[Path] = []
    for item in list(extra_roots or []) + [Path(p).expanduser() for p in _SEARCH_ROOTS]:
        if item.is_dir() and item not in roots:
            roots.append(item)
    return roots


def run(
    epub_path: Path,
    config: ValidateConfig,
    *,
    tool: EpubCheckTool | None = None,
    workdir: Path | None = None,
    timeout: float = 900.0,
) -> LintReport:
    """对 EPUB 执行 EPUBCheck。"""
    epub_path = Path(epub_path)
    report = LintReport(source="epubcheck")

    tool = tool or discover(config)
    if not tool.available:
        report.ran = False
        report.error = tool.reason or "EPUBCheck 不可用"
        log.warning("跳过 EPUBCheck：%s", report.error)
        return report

    workdir = Path(workdir or epub_path.parent)
    workdir.mkdir(parents=True, exist_ok=True)
    json_out = workdir / "epubcheck.json"
    if json_out.exists():
        json_out.unlink()

    base_cmd = _build_command(tool, json_out, epub_path)
    log.info("运行 EPUBCheck：%s", " ".join(base_cmd))
    completed = _execute(base_cmd, timeout=timeout)

    if completed is None:
        report.ran = False
        report.error = "EPUBCheck 执行超时"
        return report

    if json_out.is_file() and json_out.stat().st_size > 0:
        try:
            report.extend(_parse_json(json_out))
            _finalize(report, completed)
            return report
        except json.JSONDecodeError as exc:
            log.warning("EPUBCheck JSON 输出解析失败，回退到文本解析：%s", exc)

    # 回退 1：老版本用 -m json，输出在 stdout
    if not json_out.is_file():
        alt_cmd = _build_command(tool, None, epub_path, legacy=True)
        alt = _execute(alt_cmd, timeout=timeout)
        if alt is not None and alt.stdout.strip().startswith("{"):
            try:
                report.extend(_parse_json_text(alt.stdout))
                _finalize(report, alt)
                return report
            except json.JSONDecodeError:
                pass

    # 回退 2：解析文本输出
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    report.extend(_parse_text(text))
    _finalize(report, completed)
    return report


def _build_command(tool: EpubCheckTool, json_out: Path | None, epub_path: Path, *, legacy: bool = False) -> list[str]:
    if tool.jar:
        cmd = [tool.java, "-jar", tool.jar, "-q"]
    else:
        cmd = [tool.java]
    if json_out is not None and not legacy:
        cmd += ["-j", str(json_out)]
    elif legacy:
        cmd += ["-m", "json"]
    cmd.append(str(epub_path))
    return cmd


def _execute(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603 - 命令由本模块构造
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("EPUBCheck 超时（%.0fs）", timeout)
        return None
    except FileNotFoundError as exc:
        log.warning("EPUBCheck 无法启动：%s", exc)
        return None
    except OSError as exc:
        log.warning("EPUBCheck 执行异常：%s", exc)
        return None


def _finalize(report: LintReport, completed: subprocess.CompletedProcess[str]) -> None:
    report.checked_files = 1
    # EPUBCheck 退出码：0 无错误，1 有错误，2 用法错误/内部错误
    if completed.returncode not in (0, 1):
        tail = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()
        hint = tail[-1] if tail else ""
        if not report.issues:
            report.ran = False
            report.error = f"EPUBCheck 异常退出（code={completed.returncode}）：{hint[:300]}"
        else:
            report.error = f"EPUBCheck 退出码 {completed.returncode}：{hint[:200]}"


def _parse_json(path: Path) -> list[Issue]:
    return _parse_json_text(path.read_text(encoding="utf-8"))


def _parse_json_text(text: str) -> list[Issue]:
    payload = json.loads(text)
    messages = payload.get("messages") or []
    issues: list[Issue] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        issues.append(
            Issue(
                id=str(item.get("ID") or item.get("id") or "UNKNOWN"),
                severity=str(item.get("severity") or "ERROR").upper(),
                message=str(item.get("message") or "").strip(),
                path=str(item.get("path") or item.get("fileName") or ""),
                line=int(item.get("line") or 0),
                column=int(item.get("column") or 0),
                source="epubcheck",
                detail={k: v for k, v in item.items() if k not in {"ID", "severity", "message", "path", "line", "column"}},
            )
        )
    return issues


def _parse_text(text: str) -> list[Issue]:
    issues: list[Issue] = []
    for line in text.splitlines():
        line = line.strip()
        match = _TEXT_RE.match(line)
        if not match:
            continue
        severity, code, remainder = match.groups()
        path = ""
        line_no = 0
        column = 0
        message = remainder
        if ":" in remainder:
            head, _, tail = remainder.partition(":")
            location = _LOCATION_RE.match(head.strip())
            if location:
                path = location.group(1)
                line_no = int(location.group(2))
                column = int(location.group(3))
                message = tail.strip()
            else:
                path = head.strip()
                message = tail.strip()
        issues.append(
            Issue(
                id=code,
                severity=severity,
                message=message,
                path=path,
                line=line_no,
                column=column,
                source="epubcheck",
            )
        )
    return issues


def tool_info(config: ValidateConfig) -> dict[str, Any]:
    tool = discover(config)
    return {
        "available": tool.available,
        "java": tool.java,
        "jar": tool.jar,
        "reason": tool.reason,
        "describe": tool.describe(),
    }

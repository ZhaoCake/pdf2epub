"""命令行入口。

命令只有六个，对应"谁在什么时候需要它"：

``init``     人/Agent 起手：登记一个 PDF
``run``      主循环：跑流水线，需要 LLM 时停下并给出工单
``done``     LLM 干完活后表态：校准完了 / 撰写完了 / 就这样吧
``status``   看一眼现在停在哪、下一步该干嘛
``list``     有哪些运行
``doctor``   环境自检（装没装 Java、EPUBCheck、渲染依赖）

退出码固定，脚本里可以直接判断：

===  ====  ==========================================
0    成功  产物通过校验
1    失败  出错，看 ``alerts``
2    告警  跑完了但有问题（含"已接受不合格产物"）
3    待办  轮到 LLM 了，按工单干活后重跑 ``run``
===  ====  ==========================================
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__, epubcheck, pageimage
from .alerts import AlertCode, AlertSink, Severity
from .config import DEFAULT_CONFIG_NAME, DEFAULT_ENV_NAME, Config, load_config
from .errors import ConfigError, Pdf2EpubError
from .logutil import get_logger, reset_logging, set_context, setup_logging
from .pipeline import Pipeline, PipelineOptions
from .runstate import Run, RunStore, Stage, StageStatus, read_json

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_WARNINGS = 2
EXIT_AGENT = 3

log = get_logger("cli")

GLOBAL_DEFAULTS: dict[str, Any] = {
    "config": None,
    "workdir": None,
    "language": None,
    "json": False,
    "quiet": False,
    "debug": False,
}


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


class Printer:
    """统一收口 stdout，顺便让 --json 模式保持干净。"""

    def __init__(self, as_json: bool = False) -> None:
        self.as_json = as_json
        self.payload: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self.payload[key] = value

    def out(self, message: str = "") -> None:
        if not self.as_json:
            print(message)

    def section(self, title: str) -> None:
        if self.as_json:
            return
        print()
        print(title)
        print("-" * 48)

    def kv(self, key: str, value: Any) -> None:
        if not self.as_json:
            print(f"  {key:<16} {value}")

    def finish(self, exit_code: int) -> int:
        self.payload["exit_code"] = exit_code
        if self.as_json:
            print(json.dumps(self.payload, ensure_ascii=False, indent=2))
        return exit_code


def _fail(printer: Printer, exc: Pdf2EpubError) -> int:
    payload: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.detail is not None:
        payload["detail"] = exc.detail
    printer.set("error", payload)
    if not printer.as_json:
        print(f"错误[{exc.code}]：{exc.message}", file=sys.stderr)
        detail = exc.detail or {}
        if isinstance(detail, dict):
            for key, value in detail.items():
                if isinstance(value, (str, int, float, bool)):
                    print(f"  {key}：{value}", file=sys.stderr)
    return printer.finish(exc.exit_code or EXIT_FATAL)


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def _load(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    if getattr(args, "workdir", None):
        overrides["workdir"] = args.workdir
    if getattr(args, "language", None):
        overrides["language"] = args.language
    if getattr(args, "quiet", False):
        overrides["log_level"] = "ERROR"
    if getattr(args, "debug", False):
        overrides["log_level"] = "DEBUG"
    return load_config(getattr(args, "config", None), overrides=overrides or None)


def _store(config: Config) -> RunStore:
    return RunStore(Path(config.workdir))


def _alerts(config: Config, run: Run) -> AlertSink:
    return AlertSink(
        run.alerts_path,
        webhook=config.alert_webhook,
        command=config.alert_command,
    ).load()


def _resolve(store: RunStore, run_id: str | None) -> Run:
    return store.resolve(run_id)


def _print_run(printer: Printer, run: Run, alerts: AlertSink) -> None:
    printer.section("运行状态")
    printer.kv("run_id", run.run_id)
    printer.kv("状态", run.status)
    printer.kv("源文件", run.source_name)
    printer.kv("目录", run.root)
    output = run.output_epub()
    if output.exists():
        printer.kv("产物", output)

    printer.section("阶段")
    for stage in Stage.ordered():
        record = run.stage(stage)
        duration = f"{record.duration:.1f}s" if record.duration else "-"
        note = f"  {record.note}" if record.note else ""
        printer.out(f"  {stage.value:<10} {record.status:<10} {duration:>7}{note}")

    items = alerts.items
    if items:
        printer.section(f"告警（{len(items)} 条）")
        printer.out(alerts.render(limit=12))


def _print_next_steps(printer: Printer, run: Run, config: Config) -> None:
    """流水线停下来了，告诉调用者下一步干什么。

    只报**第一个**暂停的阶段：后面的阶段可能还留着上一轮的 blocked 状态，
    全打印出来会让调用者以为要同时做两件事。
    """
    pending = next(
        (stage for stage in Stage.ordered() if run.stage(stage).status == StageStatus.BLOCKED.value),
        None,
    )

    if pending is Stage.CALIBRATE:
        printer.section("轮到你了：校准")
        printer.out(f"  工单：    {run.calibrate_report}")
        printer.out(f"  要改的：  {run.calibrate_source}")
        printer.out(f"  页面图：  {run.pages_dir}")
        printer.out("")
        printer.out("  对着页面图把认错的地方改对，然后执行：")
        printer.out(f"      pdf2epub run --run {run.run_id}")
        printer.out("  确认不需要改的话：")
        printer.out(f"      pdf2epub done calibrate --note \"无需校准\" --run {run.run_id}")

    elif pending is Stage.COMPOSE:
        printer.section("轮到你了：撰写")
        printer.out(f"  说明书：  {run.build_dir / 'BRIEF.md'}")
        printer.out(f"  要写的：  {run.book_json}")
        printer.out(f"            {run.oebps_dir / 'text'}/*.xhtml")
        printer.out("")
        printer.out("  写完执行：")
        printer.out(f"      pdf2epub run --run {run.run_id}")

    elif pending is Stage.CHECK:
        printer.section("轮到你了：改格式")
        printer.out(f"  报告：    {run.check_report}")
        printer.out(f"  要改的：  {run.build_dir}")
        printer.out("")
        printer.out("  按报告改完执行：")
        printer.out(f"      pdf2epub run --run {run.run_id}")
        printer.out("  确认就这样也行的话：")
        printer.out(f"      pdf2epub done --accept --note \"人工确认\" --run {run.run_id}")

    if run.status == "succeeded" and run.output_epub().exists():
        printer.section("产物")
        printer.out(f"  {run.output_epub()}")


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    config = _load(args)
    printer = Printer(args.json)
    store = _store(config)
    pdf = Path(args.pdf)
    if not pdf.is_file():
        return _fail(printer, ConfigError(f"找不到 PDF：{pdf}"))

    run = store.create(pdf, run_id=args.run_id, config_snapshot=config.to_dict())
    printer.set("run", run.summary())
    printer.out(f"已创建运行：{run.run_id}")
    printer.out(f"  源文件：{run.source_pdf.name}")
    printer.out(f"  目录：  {run.root}")
    printer.out("")
    printer.out(f"下一步：pdf2epub run --run {run.run_id}")
    return printer.finish(EXIT_OK)


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    printer = Printer(args.json)
    store = _store(config)
    try:
        run = _resolve(store, args.run)
    except Pdf2EpubError as exc:
        return _fail(printer, exc)

    setup_logging(
        level=config.log_level,
        json_file=run.log_path,
        json_console=config.log_json and args.json,
    )
    set_context(run_id=run.run_id, stage="")

    alerts = _alerts(config, run)
    pipeline = Pipeline(config, run, alerts)

    force: tuple[str, ...] = ()
    if args.force:
        force = tuple(args.force)
    elif args.from_stage:
        names = [s.value for s in Stage.ordered()]
        force = tuple(names[names.index(args.from_stage) :])

    options = PipelineOptions(
        until=Stage(args.until) if args.until else None,
        force=force,
        accept=bool(args.accept),
    )
    if args.until is None and force and force == (Stage.CHECK.value,):
        options.until = Stage.CHECK

    result = pipeline.run_all(options)
    alerts.flush()

    printer.set("run", result.summary())
    printer.set("alerts", alerts.summary())
    _print_run(printer, result, alerts)
    _print_next_steps(printer, result, config)
    return printer.finish(result.exit_code)


def cmd_done(args: argparse.Namespace) -> int:
    """LLM 表态：这一步干完了。"""
    config = _load(args)
    printer = Printer(args.json)
    store = _store(config)
    try:
        run = _resolve(store, args.run)
    except Pdf2EpubError as exc:
        return _fail(printer, exc)

    setup_logging(level=config.log_level, json_file=run.log_path, json_console=False)
    set_context(run_id=run.run_id, stage="")

    if args.accept:
        run.counters["accepted_by_agent"] = {"note": args.note or "", "at": run.updated_at}
    if args.stage in (None, Stage.CALIBRATE.value):
        run.counters["calibrate_accepted"] = args.note or True
    if args.stage == Stage.COMPOSE.value:
        # 撰写没有"跳过"的说法：没有内容就没有书
        return _fail(
            printer,
            ConfigError("撰写阶段不能跳过；请把章节写到 build/OEBPS/text/ 下再重跑 run"),
        )
    run.save()

    alerts = _alerts(config, run)
    alerts.info(
        AlertCode.AGENT_SKIPPED,
        f"Agent 表态：{args.stage or 'calibrate'} 已处理",
        note=args.note or "",
    )
    alerts.flush()

    pipeline = Pipeline(config, run, alerts)
    result = pipeline.run_all(PipelineOptions(accept=bool(args.accept)))
    alerts.flush()

    printer.set("run", result.summary())
    printer.set("alerts", alerts.summary())
    _print_run(printer, result, alerts)
    _print_next_steps(printer, result, config)
    return printer.finish(result.exit_code)


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    printer = Printer(args.json)
    store = _store(config)
    try:
        run = _resolve(store, args.run)
    except Pdf2EpubError as exc:
        return _fail(printer, exc)

    alerts = _alerts(config, run)
    printer.set("run", run.summary())
    printer.set("alerts", alerts.summary())
    _print_run(printer, run, alerts)
    if run.status in {"blocked", "succeeded"}:
        _print_next_steps(printer, run, config)

    if args.tasks and run.check_report.is_file():
        printer.section("校验报告")
        printer.out(run.check_report.read_text(encoding="utf-8"))
    return printer.finish(run.exit_code if run.status != "succeeded" else EXIT_OK)


def cmd_list(args: argparse.Namespace) -> int:
    config = _load(args)
    printer = Printer(args.json)
    store = _store(config)
    runs = store.list_runs()
    printer.set("runs", [r.summary() for r in runs])
    if not runs:
        printer.out("（还没有任何运行）")
    else:
        printer.out(f"{'RUN_ID':<40} {'状态':<10} {'阶段':<12} {'源文件':<24} 产物")
        for run in runs:
            pending = run.first_incomplete()
            stage = pending.value if pending else "完成"
            output = "有" if run.output_epub().exists() else "-"
            printer.out(
                f"{run.run_id:<40} {run.status:<10} {stage:<12} {run.source_name[:24]:<24} {output}"
            )
    return printer.finish(EXIT_OK)


def cmd_alerts(args: argparse.Namespace) -> int:
    config = _load(args)
    printer = Printer(args.json)
    try:
        run = _resolve(_store(config), args.run)
    except Pdf2EpubError as exc:
        return _fail(printer, exc)

    alerts = _alerts(config, run)
    items = alerts.items
    if args.severity:
        items = [a for a in items if a.severity == args.severity]
    printer.set("alerts", alerts.summary())
    printer.set("items", [a.to_dict() for a in items])
    if not items:
        printer.out("（没有符合条件的告警）")
    else:
        for alert in items[-args.limit :]:
            printer.out(f"[{alert.severity.upper():<8}] {alert.code:<22} {alert.message}")
    return printer.finish(EXIT_OK)


def cmd_show(args: argparse.Namespace) -> int:
    """直接打印某个工单，省得调用者再去找文件路径。"""
    config = _load(args)
    printer = Printer(args.json)
    try:
        run = _resolve(_store(config), args.run)
    except Pdf2EpubError as exc:
        return _fail(printer, exc)

    targets = {
        "calibrate": run.calibrate_report,
        "compose": run.build_dir / "BRIEF.md",
        "check": run.check_report,
    }
    name = args.what
    target = targets[name]
    if not target.is_file():
        return _fail(printer, ConfigError(f"没有 {name} 工单（{target} 不存在）。先执行 pdf2epub run。"))

    printer.set("task", {"name": name, "path": str(target)})
    printer.out(target.read_text(encoding="utf-8"))
    return printer.finish(EXIT_OK)


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    printer = Printer(args.json)
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})

    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    record("python", sys.version_info >= (3, 10), version, "需要 Python 3.10+")

    token = config.paddle.resolve_token()
    record(
        "PaddleOCR Token",
        bool(token),
        "已配置" if token else f"未配置（环境变量 {config.paddle.token_env}）",
        "到 AI Studio（aistudio.baidu.com）的 PaddleOCR 服务创建 Token 后设置环境变量",
    )

    for module, package, hint in (
        ("requests", "requests", "pip install requests"),
        ("lxml", "lxml", "pip install lxml"),
        ("pypdf", "pypdf", "pip install pypdf"),
    ):
        try:
            __import__(module)
            record(module, True, "已安装")
        except ModuleNotFoundError:
            record(module, False, "未安装", hint)

    record(
        "页面渲染",
        pageimage.available(),
        "可用" if pageimage.available() else "不可用",
        "pip install pypdfium2 Pillow（缺了只能做纯文本校准）",
    )

    tool = epubcheck.discover(config.validate)
    record(
        "EPUBCheck",
        tool.available,
        tool.reason or "可用",
        "下载 https://github.com/w3c/epubcheck/releases 后设置 EPUBCHECK_JAR",
    )

    workdir = Path(config.workdir)
    record("工作目录", True, f"{workdir}（将自动创建）")
    record("配置文件", True, config.source_path or f"未找到 {DEFAULT_CONFIG_NAME}，使用内置默认值")
    record("环境文件", True, config.env_path or f"未找到 {DEFAULT_ENV_NAME}（可用它放 PADDLE_TOKEN）")

    printer.set("checks", checks)
    failed = [c for c in checks if not c["ok"]]
    printer.out("")
    printer.out("环境自检")
    printer.section("")
    for check in checks:
        mark = "OK  " if check["ok"] else "FAIL"
        printer.out(f"  [{mark}] {check['name']:<16} {check['detail']}")
        if not check["ok"] and check["hint"]:
            printer.out(f"           -> {check['hint']}")

    if failed:
        printer.out("")
        printer.out(f"结论：{len(failed)} 项需要注意（不影响跑通，但会影响效果）")
    else:
        printer.out("")
        printer.out("结论：环境齐备")
    return printer.finish(EXIT_OK)


def cmd_clean(args: argparse.Namespace) -> int:
    config = _load(args)
    printer = Printer(args.json)
    store = _store(config)
    if args.all:
        if not args.yes:
            return _fail(printer, ConfigError("删除全部运行需要 -y 确认"))
        count = store.purge()
        printer.set("removed", count)
        printer.out(f"已删除 {count} 个运行")
        return printer.finish(EXIT_OK)

    try:
        run = _resolve(store, args.run)
    except Pdf2EpubError as exc:
        return _fail(printer, exc)
    store.delete(run.run_id)
    printer.set("removed", run.run_id)
    printer.out(f"已删除运行：{run.run_id}")
    return printer.finish(EXIT_OK)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _global_options(*, suppress: bool) -> argparse.ArgumentParser:
    """全局选项同时挂到主 parser 和子命令上，两种位置都能写。"""
    default: Any = argparse.SUPPRESS if suppress else None
    store_true = {"default": argparse.SUPPRESS} if suppress else {}
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-c", "--config", default=default, help=f"配置文件（默认 {DEFAULT_CONFIG_NAME}）")
    parser.add_argument("--workdir", default=default, help="运行目录（覆盖配置）")
    parser.add_argument("--language", default=default, help="解析语言（如 ch/en/japan）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出", **store_true)
    parser.add_argument("--quiet", action="store_true", help="只输出错误", **store_true)
    parser.add_argument("--debug", action="store_true", help="出错时打印完整堆栈", **store_true)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2epub",
        description="把 PDF（含扫描版）转成 EPUB：脚本负责搬运与校验，LLM 负责判断与转写。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_global_options(suppress=False)],
        epilog=(
            "退出码：0 成功 / 1 失败 / 2 告警 / 3 轮到 LLM 了\n"
            "\n"
            "典型流程（Agent 驱动）：\n"
            "  pdf2epub init book.pdf\n"
            "  pdf2epub run                     # 跑到需要你干活时停下（exit 3）\n"
            "  pdf2epub show calibrate          # 读工单\n"
            "  # 对着页面图改 calibrate/source.md\n"
            "  pdf2epub run                     # 继续\n"
            "  pdf2epub show compose            # 读撰写说明\n"
            "  # 写 build/book.json 与 build/OEBPS/text/*.xhtml\n"
            "  pdf2epub run                     # 打包 + 校验\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"pdf2epub {__version__}")

    global_opts = _global_options(suppress=True)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[global_opts], **kwargs)

    p_init = add("init", help="登记一个 PDF，创建运行")
    p_init.add_argument("pdf", help="输入 PDF 路径")
    p_init.add_argument("--run-id", help="自定义运行 ID")
    p_init.set_defaults(func=cmd_init)

    p_run = add("run", help="执行流水线；需要 LLM 时停下并给出工单")
    p_run.add_argument("--run", help="运行 ID，缺省用最近一次")
    p_run.add_argument("--until", choices=[s.value for s in Stage.ordered()], help="执行到指定阶段后停止")
    p_run.add_argument(
        "--from", dest="from_stage", choices=[s.value for s in Stage.ordered()], help="从指定阶段起重跑"
    )
    p_run.add_argument(
        "--force",
        action="append",
        choices=[s.value for s in Stage.ordered()] + ["all"],
        help="强制重跑阶段（可多次）",
    )
    p_run.add_argument("--accept", action="store_true", help="校验不合格也接受（留下告警）")
    p_run.set_defaults(func=cmd_run)

    p_done = add("done", help="声明某个阶段已处理完，继续流水线")
    p_done.add_argument(
        "stage",
        nargs="?",
        choices=[Stage.CALIBRATE.value, Stage.COMPOSE.value],
        help="哪个阶段干完了，缺省 calibrate",
    )
    p_done.add_argument("--run", help="运行 ID，缺省用最近一次")
    p_done.add_argument("--note", help="说明，会写进记录")
    p_done.add_argument("--accept", action="store_true", help="同时接受不合格的校验结果")
    p_done.set_defaults(func=cmd_done)

    p_status = add("status", help="查看运行状态与下一步")
    p_status.add_argument("--run", help="运行 ID，缺省用最近一次")
    p_status.add_argument("--tasks", action="store_true", help="顺带打印校验报告")
    p_status.set_defaults(func=cmd_status)

    p_list = add("list", help="列出所有运行")
    p_list.set_defaults(func=cmd_list)

    p_show = add("show", help="打印某个阶段的工单")
    p_show.add_argument("what", choices=["calibrate", "compose", "check"], help="要看的工单")
    p_show.add_argument("--run", help="运行 ID，缺省用最近一次")
    p_show.set_defaults(func=cmd_show)

    p_alerts = add("alerts", help="查看告警")
    p_alerts.add_argument("--run", help="运行 ID，缺省用最近一次")
    p_alerts.add_argument("--severity", choices=[s.value for s in Severity], help="只看某个级别")
    p_alerts.add_argument("--limit", type=int, default=50)
    p_alerts.set_defaults(func=cmd_alerts)

    p_doctor = add("doctor", help="环境自检")
    p_doctor.set_defaults(func=cmd_doctor)

    p_clean = add("clean", help="删除运行记录")
    p_clean.add_argument("--run", help="运行 ID，缺省用最近一次")
    p_clean.add_argument("--all", action="store_true", help="删除全部运行")
    p_clean.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p_clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(argv)

    for key, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    if not hasattr(args, "func"):
        sub = next(
            (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
            None,
        )
        if sub and args.command in sub.choices:
            sub.choices[args.command].print_help()
        else:
            parser.print_help()
        return EXIT_OK

    reset_logging()
    try:
        return int(args.func(args))
    except Pdf2EpubError as exc:
        return _fail(Printer(args.json), exc)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_FATAL
    except Exception as exc:  # 兜底：不让用户看到裸栈
        if args.debug:
            raise
        printer = Printer(args.json)
        printer.set("error", {"code": "E_UNEXPECTED", "message": f"{type(exc).__name__}: {exc}"})
        if printer.as_json:
            return printer.finish(EXIT_FATAL)
        print(f"未预期的错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("（加 --debug 可查看完整堆栈）", file=sys.stderr)
        return EXIT_FATAL

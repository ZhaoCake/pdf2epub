"""流水线：四个阶段，边界就是"谁来干活"。

::

    prepare    脚本   PDF -> MinerU -> 解压产物（原样保存）
    calibrate  LLM    对着页面截图校准低分段落（编辑 calibrate/source.md）
    compose    LLM    把内容直接写成 EPUB（write build/book.json + OEBPS/text/*.xhtml）
    check      脚本   打包 + 格式校验 + 出报告

脚本只做搬运、截图、打包、报告；**判断和转写全部交给 LLM**。
所以脚本里没有"把 Markdown 翻译成 XHTML"这类容易做错的逻辑——
那种事让 LLM 做，做得更好，而且不需要为边界情况写代码。

需要 LLM 介入时阶段会 ``BLOCKED``，进程以退出码 3 结束，并在运行目录里留下
一份工单。LLM 干完活再执行一次 ``pdf2epub run`` 就会接着往下走。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import epubcheck as epubcheck_mod
from . import epubpack, formatcheck, pageimage, tasks
from .alerts import AlertCode, AlertSink, Severity
from .bundle import Bundle, load_bundle
from .config import Config
from .errors import (
    AgentPause,
    BuildError,
    CalibrateRequired,
    ComposeRequired,
    FormatRequired,
    InputError,
    MinerUError,
    Pdf2EpubError,
    TaskError,
)
from .ingest import inspect_pdf, materialize_chunks, plan_chunks
from .logutil import get_logger, set_context
from .mineru_client import BatchFile, MinerUClient, ParseOptions, classify_failures
from .runstate import Run, Stage, StageStatus, fingerprint

log = get_logger("pipeline")

#: 判定"LLM 是否动过手"时忽略的文件
_IGNORED_CHANGES = {"BRIEF.md"}


@dataclass
class PipelineOptions:
    until: Stage | None = None
    force: tuple[str, ...] = ()
    #: 校验不合格也接受（留下告警），用于人工判断后强行收尾
    accept: bool = False


class Pipeline:
    def __init__(self, config: Config, run: Run, alerts: AlertSink) -> None:
        self.config = config
        self.run = run
        self.alerts = alerts
        self.accept = False
        self.alerts.stage = ""

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def run_all(self, options: PipelineOptions | None = None) -> Run:
        options = options or PipelineOptions()
        self.accept = self.accept or options.accept
        self.run.status = "running"
        self.run.save()

        try:
            for stage in Stage.ordered():
                if options.until is not None and not self._reached(stage, options.until):
                    break
                handler = self._handler(stage)
                self._execute(stage, handler, force=self._is_forced(stage, options))
                if options.until is not None and stage == options.until:
                    break
        except AgentPause:
            self.run.status = "blocked"
            self.run.exit_code = 3
            self._finalize(success=False)
            return self.run
        except Pdf2EpubError as exc:
            log.error("流水线失败：%s", exc.message)
            self.run.status = "failed"
            self.run.exit_code = exc.exit_code or 1
            self._finalize(success=False)
            return self.run

        if any(self.run.stage(s).status == StageStatus.BLOCKED.value for s in Stage.ordered()):
            self.run.status = "blocked"
            self.run.exit_code = 3
        else:
            self.run.status = "succeeded"
            # 只看"这一次跑"的告警：历史告警不该让一次干净的收尾变成 exit 2
            self.run.exit_code = 2 if self.alerts.has_session_at_least(Severity.WARNING) else 0
        self._finalize(success=self.run.exit_code in (0, 2))
        return self.run

    @staticmethod
    def _reached(stage: Stage, until: Stage) -> bool:
        order = Stage.ordered()
        return order.index(stage) <= order.index(until)

    def _handler(self, stage: Stage) -> Callable[[], list[str]]:
        return {
            Stage.PREPARE: self.stage_prepare,
            Stage.CALIBRATE: self.stage_calibrate,
            Stage.COMPOSE: self.stage_compose,
            Stage.CHECK: self.stage_check,
        }[stage]

    def _is_forced(self, stage: Stage, options: PipelineOptions) -> bool:
        return bool(options.force) and ("all" in options.force or stage.value in options.force)

    def _execute(self, stage: Stage, handler: Callable[[], list[str]], *, force: bool = False) -> None:
        record = self.run.stage(stage)
        if record.status == StageStatus.DONE.value and not force:
            return

        record.status = StageStatus.RUNNING.value
        record.attempts += 1
        record.started_at = time.time()
        record.finished_at = None
        record.error = None
        record.note = ""
        self.run.save()
        set_context(stage=stage.value)

        log.info("=== 阶段 %s 开始（第 %d 次）===", stage.value, record.attempts)
        try:
            outputs = handler() or []
            record.outputs = [str(p) for p in outputs]
            record.status = StageStatus.DONE.value
            log.info("=== 阶段 %s 完成（%.1fs）===", stage.value, time.time() - record.started_at)
        except AgentPause as exc:
            record.status = StageStatus.BLOCKED.value
            record.note = exc.message
            record.error = exc.to_dict()
            log.info("=== 阶段 %s 暂停等 Agent：%s ===", stage.value, exc.message)
            raise
        except Pdf2EpubError as exc:
            record.status = StageStatus.FAILED.value
            record.error = exc.to_dict()
            raise
        except Exception as exc:  # 未预期异常也要落盘，方便事后定位
            record.status = StageStatus.FAILED.value
            record.error = {"code": "E_UNEXPECTED", "message": f"{type(exc).__name__}: {exc}"}
            log.exception("阶段 %s 出现未预期异常", stage.value)
            raise
        finally:
            record.finished_at = time.time()
            self.alerts.stage = stage.value
            self.run.save()
            self.alerts.flush()

    def _finalize(self, *, success: bool) -> None:
        self.run.finished_at = time.time()
        self.run.counters["alerts"] = self.alerts.summary()
        self.run.save()
        self.alerts.flush()
        if success:
            tail = "（有告警，请查阅）" if self.run.exit_code == 2 else ""
            self.alerts.info(
                AlertCode.OUTPUT_READY,
                f"转换完成{tail}：{self.run.output_epub().name}",
                output=str(self.run.output_epub()),
            )
            self.alerts.flush()

    # ------------------------------------------------------------------
    # 阶段 1：prepare（脚本干活）
    # ------------------------------------------------------------------

    def stage_prepare(self) -> list[str]:
        pdf = self.run.input_pdf
        if not pdf.is_file():
            raise InputError(f"找不到输入 PDF：{pdf}")

        profile = inspect_pdf(pdf)
        if profile.encrypted:
            self.alerts.error(AlertCode.ENCRYPTED_PDF, "输入 PDF 已加密，无法解析", path=str(pdf))
            raise InputError("输入 PDF 已加密")

        if profile.is_scanned:
            self.alerts.info(
                AlertCode.SCANNED_PDF,
                f"检测到扫描版 PDF（采样页均 {profile.sampled_chars_per_page:.0f} 字符），将走 OCR",
                pages=profile.page_count,
            )

        chunks = plan_chunks(profile, self.config.mineru, language=self.config.language)
        if len(chunks) > 1:
            self.alerts.info(
                AlertCode.SPLIT_REQUIRED,
                f"PDF 超出 MinerU 单任务上限，切分为 {len(chunks)} 份",
                pages=profile.page_count,
                parts=len(chunks),
            )
        materialized = materialize_chunks(chunks, self.run.chunks_dir, pdf)

        self.run.counters["pdf"] = profile.to_dict()
        self.run.counters["chunks"] = [c.to_dict() for c in materialized]

        client = MinerUClient(self.config.mineru, language=self.config.language)
        files = [
            BatchFile(
                path=chunk.path,
                data_id=chunk.chunk_id,
                is_ocr=chunk.is_ocr,
                page_ranges=chunk.page_ranges,
            )
            for chunk in materialized
        ]
        options = ParseOptions(
            model_version=self.config.mineru.model_version,
            language=self.config.language,
            is_ocr=any(chunk.is_ocr for chunk in materialized),
            enable_formula=self.config.mineru.enable_formula,
            enable_table=self.config.mineru.enable_table,
        )

        batch_id = client.submit_local_batch(files, options)
        results = client.wait_batch(batch_id, expected=len(files))
        done, failed = classify_failures(results)
        if failed:
            self.alerts.error(
                AlertCode.MINERU_FAILED,
                f"{len(failed)} 个文件解析失败",
                batch=batch_id,
                failures=[{"file": r.file_name, "state": r.state, "error": r.err_msg} for r in failed[:5]],
            )
            raise MinerUError(
                f"MinerU 解析失败（{len(failed)}/{len(results)} 个文件）",
                detail={"failures": [r.file_name for r in failed]},
            )

        extracted: list[str] = []
        for result in done:
            chunk_id = result.data_id or Path(result.file_name).stem
            destination = self.run.extracted_dir / chunk_id
            if not (destination / ".extracted").is_file():
                zip_path = client.download_zip(
                    result.zip_url, self.run.zips_dir / f"{chunk_id}.zip"
                )
                load_bundle_after_extract(zip_path, destination)
            else:
                load_bundle(destination)
            extracted.append(str(destination))

        if not extracted:
            raise MinerUError("MinerU 没有返回任何可用的解析结果")

        self.run.counters["mineru"] = {
            "batch_id": batch_id,
            "submitted": len(materialized),
            "extracted": extracted,
        }
        return extracted

    # ------------------------------------------------------------------
    # 阶段 2：calibrate（LLM 干活）
    # ------------------------------------------------------------------

    def stage_calibrate(self) -> list[str]:
        bundles = self._bundles()
        config = self.config.calibrate

        task = tasks.scaffold_calibration(
            self.run,
            bundles,
            config,
            pdf_path=self.run.input_pdf,
            alerts=self.alerts,
        )
        low_count = int(task.counts.get("low_segments", 0))
        self.run.counters["calibrate"] = task.counts

        # LLM 的完成信号：source.md 被改过，或者显式声明"不用改"
        changed = self._source_changed()
        accepted = bool(self.run.counters.get("calibrate_accepted"))

        # 没有什么低分段落时不要拿工单去烦 LLM——脚本能判断的部分就别打扰它
        if not low_count and not changed and not accepted:
            log.info("没有低置信度段落，自动跳过校准")
            self.alerts.info(
                AlertCode.CALIBRATE_REQUIRED,
                "MinerU 没有给出低置信度段落，已跳过校准",
                report=str(task.report),
            )
            self.run.counters["calibrate_accepted"] = True
            return [str(self.run.calibrate_source)]

        if not changed and not accepted:
            hint = (
                "改完 calibrate/source.md 后重跑 `pdf2epub run`"
                if pageimage.available()
                else "缺少 pypdfium2/Pillow，只能纯文本校准；详见 report.md"
            )
            self.alerts.warn(
                AlertCode.CALIBRATE_REQUIRED,
                f"有 {low_count} 段低置信度内容需要对照页面图校准",
                report=str(task.report),
                source_md=str(self.run.calibrate_source),
                pages=str(self.run.pages_dir),
                next_step=hint,
            )
            raise CalibrateRequired(f"等待校准：{low_count} 段低置信度内容", detail=task.to_dict())

        log.info("校准阶段完成：%s", "工作稿被修改" if changed else "声明无需校准")
        return [str(self.run.calibrate_source)]

    def _source_changed(self) -> bool:
        """source.md 相对 scaffold 时的内容是否变了。"""
        source = self.run.calibrate_source
        if not source.is_file():
            return False
        digest = fingerprint(source.read_bytes().decode("utf-8", errors="replace"))
        recorded = self.run.counters.get("calibrate_source_hash")
        if recorded is None:
            # 第一次进这个阶段：记下原始指纹，本次不算"改过"
            self.run.counters["calibrate_source_hash"] = digest
            self.run.save()
            return False
        return digest != recorded

    # ------------------------------------------------------------------
    # 阶段 3：compose（LLM 干活）
    # ------------------------------------------------------------------

    def stage_compose(self) -> list[str]:
        bundles = self._bundles()
        task = tasks.scaffold_compose(self.run, bundles, self.config.compose)

        if not self._compose_ready():
            self.alerts.warn(
                AlertCode.COMPOSE_REQUIRED,
                "请把内容写成 EPUB：填写 build/book.json 并在 build/OEBPS/text/ 下写出章节",
                brief=str(task.report),
                build_dir=str(self.run.build_dir),
                next_step=f"阅读 {task.report}，写完后重跑 `pdf2epub run`",
            )
            raise ComposeRequired("等待撰写：build/book.json 或章节文件还没写好", detail=task.to_dict())

        chapters = self._written_chapters()
        log.info("撰写阶段完成：%d 个章节", len(chapters))
        self.run.counters["compose"] = {
            "chapters": [str(p.relative_to(self.run.oebps_dir)) for p in chapters],
            "images": task.counts.get("images", 0),
        }
        return [str(self.run.book_json), *[str(p) for p in chapters]]

    def _compose_ready(self) -> bool:
        """撰写是否算完成：有书目，且至少写出了一个真正的章节。

        判定标准刻意只有"是不是样例"这一条——不按字数卡。
        薄薄一章也是内容，脚本没资格替 LLM 判断它够不够长。
        """
        if not self.run.book_json.is_file():
            return False
        return bool(self._written_chapters())

    def _written_chapters(self) -> list[Path]:
        """LLM 真正写出来的章节（排除脚本放的样例）。"""
        text_dir = self.run.oebps_dir / "text"
        if not text_dir.is_dir():
            return []
        chapters: list[Path] = []
        for path in sorted(text_dir.rglob("*.xhtml")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if tasks.SAMPLE_MARKER in content:
                continue
            chapters.append(path)
        return chapters

    # ------------------------------------------------------------------
    # 阶段 4：check（脚本干活）
    # ------------------------------------------------------------------

    def stage_check(self, *, accept: bool | None = None) -> list[str]:
        accept = self.accept if accept is None else accept
        build_dir = self.run.build_dir
        if not build_dir.is_dir():
            raise BuildError(f"构建目录不存在：{build_dir}")

        mode = self.config.validate.epubcheck_mode
        packed = epubpack.build_package(build_dir, identifier_seed=self.run.source_hash)
        for warning in packed.warnings:
            self.alerts.warn(AlertCode.FORMAT_FAILED, warning)
            log.warning("打包提示：%s", warning)

        output = epubpack.pack(build_dir, self.run.output_epub())
        result = formatcheck.run_check(self.run, self.config.validate, output=output)

        if not result.epubcheck_ran and mode != "off":
            # off 是用户显式选择，不必每轮再提醒他一次；
            # 其余情况必须说清"这道校验没跑"，不能和"跑过了"混为一谈。
            emit = self.alerts.error if mode == "require" else self.alerts.warn
            emit(
                AlertCode.EPUBCHECK_UNAVAILABLE,
                f"EPUBCheck 没有跑成，本次只做了自检：{result.epubcheck_reason}",
                hint="下载 https://github.com/w3c/epubcheck/releases，"
                "把 epubcheck.jar 放进 tools/ 或设置 EPUBCHECK_JAR",
            )

        self.run.counters["check"] = {
            "passed": result.passed,
            "spine": packed.spine,
            "entries": packed.entries,
            "book": packed.book.to_dict(),
            "report": result.report.to_dict(limit=100),
            "epubcheck": result.epubcheck,
        }

        if result.passed:
            # 不再单独发 OUTPUT_READY——_finalize 会在收尾时统一报一次，避免重复
            warnings = result.report.count("WARNING")
            log.info("EPUB 已通过格式校验（%d 个警告）：%s", warnings, output)
            if warnings:
                self.alerts.warn(
                    AlertCode.FORMAT_FAILED,
                    f"格式校验通过，但有 {warnings} 个警告",
                    report=str(run.check_report),
                )
            return [str(output)]

        blocking = len(result.blocking)
        message = f"格式校验不合格：{blocking} 个阻断性问题"
        if accept:
            self.alerts.warn(
                AlertCode.FORMAT_FAILED,
                f"{message}（已按 --accept 接受，产物保留但请人工确认）",
                report=str(self.run.check_report),
                output=str(output),
            )
            return [str(output)]

        self.alerts.warn(
            AlertCode.FORMAT_REQUIRED,
            message,
            report=str(self.run.check_report),
            build_dir=str(build_dir),
            samples=[i.to_dict() for i in result.blocking[:5]],
            next_step=f"按 {self.run.check_report} 改文件后重跑 `pdf2epub run`",
        )
        raise FormatRequired(
            message,
            detail={
                "blocking": blocking,
                "report": str(self.run.check_report),
                "build_dir": str(build_dir),
                "output": str(output),
            },
        )

    # ------------------------------------------------------------------
    # 公共
    # ------------------------------------------------------------------

    def _bundles(self) -> list[Bundle]:
        """按 chunk 顺序加载解析产物。"""
        chunks = self.run.counters.get("chunks") or []
        bundles: list[Bundle] = []
        for chunk in chunks:
            chunk_id = chunk.get("chunk_id") if isinstance(chunk, dict) else None
            if not chunk_id:
                continue
            root = self.run.extracted_dir / chunk_id
            if root.is_dir():
                bundles.append(load_bundle(root))
        if not bundles:
            # 兜底：直接扫解压目录（chunks 信息缺失时也能跑）
            for child in sorted(self.run.extracted_dir.glob("*")):
                if child.is_dir():
                    bundles.append(load_bundle(child))
        if not bundles:
            raise TaskError(
                f"找不到任何解析产物：{self.run.extracted_dir}。请先跑通 prepare 阶段。"
            )
        return bundles


def load_bundle_after_extract(zip_path: Path, dest_dir: Path) -> Bundle:
    """解压并定位产物。"""
    from .bundle import extract_zip

    extract_zip(zip_path, dest_dir)
    return load_bundle(dest_dir)

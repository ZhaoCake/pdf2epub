"""端到端：脚本 ↔ LLM 的交接点。

四步走的完整来回：
    run -> 停在 calibrate（exit 3）-> 改 source.md -> run
        -> 停在 compose（exit 3）-> 写 book.json + 章节 -> run
        -> check 通过（exit 0）
以及三条岔路：格式不过、明确放弃、干净的书不用校准。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_run

from pdf2epub import selflint
from pdf2epub.alerts import AlertCode, AlertSink
from pdf2epub.config import Config
from pdf2epub.pipeline import Pipeline, PipelineOptions
from pdf2epub.runstate import Stage, StageStatus

CHAPTER = """\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh" lang="zh">
  <head>
    <title>{title}</title>
    <link rel="stylesheet" type="text/css" href="../style/base.css"/>
  </head>
  <body>
    <h1>{title}</h1>
    <p>{body}</p>
  </body>
</html>
"""


def _pipeline(run, config: Config) -> Pipeline:
    return Pipeline(config, run, AlertSink(run.alerts_path).load())


def _tune(config: Config) -> Config:
    """测试里不依赖外部 EPUBCheck，自检已经足够覆盖。"""
    config.validate.require_epubcheck = False
    config.validate.epubcheck_jar = str(Path(config.workdir) / "no-such-epubcheck.jar")
    return config


def _write_book(run, *, chapters: int = 2, broken: bool = False) -> None:
    text_dir = run.oebps_dir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    (run.oebps_dir / "style").mkdir(parents=True, exist_ok=True)
    (run.oebps_dir / "style" / "base.css").write_text("body{margin:1em}", encoding="utf-8")

    hrefs = []
    for index in range(1, chapters + 1):
        href = f"text/ch{index:03d}.xhtml"
        hrefs.append(href)
        body = f"这是第 {index} 章的正文。" * 40
        if broken and index == 1:
            content = (
                '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>坏章节</title></head>'
                '<body><p>引用了不存在的图片<img src="../images/nope.png" alt="x"/></p></body></html>'
            )
        else:
            content = CHAPTER.format(title=f"第 {index} 章", body=body)
        (text_dir / Path(href).name).write_text(content, encoding="utf-8")

    run.book_json.write_text(
        json.dumps(
            {
                "title": "测试书",
                "author": "测试作者",
                "language": "zh",
                "identifier": "urn:uuid:test",
                "spine": hrefs,
                "toc": [{"title": f"第 {i} 章", "href": href, "level": 1} for i, href in enumerate(hrefs, 1)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 主干
# ---------------------------------------------------------------------------


def test_full_round_trip(tmp_path: Path):
    run, config = make_run(tmp_path, pages=3)
    _tune(config)

    # 1) 第一次跑：停在校准
    result = _pipeline(run, config).run_all()
    assert result.exit_code == 3, result.summary()
    assert result.stage(Stage.CALIBRATE).status == StageStatus.BLOCKED.value
    assert run.calibrate_report.is_file()

    # 2) LLM 改工作稿
    text = run.calibrate_source.read_text(encoding="utf-8")
    run.calibrate_source.write_text(text.replace("巳经", "已经"), encoding="utf-8")

    # 3) 再跑：停在撰写
    result = _pipeline(run, config).run_all()
    assert result.exit_code == 3, result.summary()
    assert run.stage(Stage.CALIBRATE).status == StageStatus.DONE.value
    assert result.stage(Stage.COMPOSE).status == StageStatus.BLOCKED.value
    assert (run.build_dir / "BRIEF.md").is_file()

    # 4) LLM 写书
    _write_book(run)

    # 5) 再跑：打包 + 校验通过
    result = _pipeline(run, config).run_all()
    assert result.exit_code in (0, 2), result.summary()
    assert run.stage(Stage.CHECK).status == StageStatus.DONE.value

    epub = run.output_epub()
    assert epub.is_file() and epub.stat().st_size > 0
    assert selflint.lint_epub(epub).passed
    assert run.counters["check"]["passed"] is True

    # 没有 EPUBCheck 时必须留下告警，不能静默当成"完全校验过了"
    codes = {a.code for a in AlertSink(run.alerts_path).load().items}
    assert AlertCode.EPUBCHECK_UNAVAILABLE.value in codes
    assert AlertCode.OUTPUT_READY.value in codes


def test_clean_book_skips_calibration(tmp_path: Path):
    run, config = make_run(tmp_path, pages=3, clean=True)
    _tune(config)

    result = _pipeline(run, config).run_all()
    assert result.exit_code == 3
    assert run.stage(Stage.CALIBRATE).status == StageStatus.DONE.value
    assert result.stage(Stage.COMPOSE).status == StageStatus.BLOCKED.value

    codes = {a.code for a in AlertSink(run.alerts_path).load().items}
    assert AlertCode.CALIBRATE_REQUIRED.value in codes, "跳过校准也要留痕"


def test_done_calibrate_skips_without_editing(tmp_path: Path):
    run, config = make_run(tmp_path, pages=2)
    _tune(config)
    assert _pipeline(run, config).run_all().exit_code == 3

    # 模拟 `pdf2epub done calibrate`
    run.counters["calibrate_accepted"] = "人工确认无需校准"
    run.save()

    result = _pipeline(run, config).run_all()
    assert result.stage(Stage.CALIBRATE).status == StageStatus.DONE.value
    assert result.stage(Stage.COMPOSE).status == StageStatus.BLOCKED.value


# ---------------------------------------------------------------------------
# 格式校验
# ---------------------------------------------------------------------------


def test_format_failure_blocks_with_actionable_report(tmp_path: Path):
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    _pipeline(run, config).run_all()

    _write_book(run, broken=True)
    result = _pipeline(run, config).run_all()

    assert result.exit_code == 3, result.summary()
    assert run.stage(Stage.CHECK).status == StageStatus.BLOCKED.value

    report = run.check_report.read_text(encoding="utf-8")
    assert "不合格" in report
    assert "ch001.xhtml" in report, "报告必须指到具体文件"
    assert "nope.png" in report, "报告必须说清是哪个引用坏了"
    assert str(run.build_dir) in report

    # 报告得告诉 LLM 怎么收尾
    assert "pdf2epub run" in report


def test_fixing_reported_problem_passes(tmp_path: Path):
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    _pipeline(run, config).run_all()
    _write_book(run, broken=True)
    assert _pipeline(run, config).run_all().exit_code == 3

    target = run.oebps_dir / "text" / "ch001.xhtml"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            '<img src="../images/nope.png" alt="x"/>', "（原图缺失，已改写为说明）"
        ),
        encoding="utf-8",
    )

    result = _pipeline(run, config).run_all()
    assert result.exit_code in (0, 2), result.summary()
    assert run.counters["check"]["passed"] is True
    assert selflint.lint_epub(run.output_epub()).passed


def test_accept_flag_delivers_with_loud_alert(tmp_path: Path):
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    _pipeline(run, config).run_all()
    _write_book(run, broken=True)

    result = _pipeline(run, config).run_all(PipelineOptions(accept=True))

    # 明确接受 -> 交付"带告警的产物"，退出码 2 而不是 0，绝不静默当成功
    assert result.exit_code == 2, result.summary()
    assert run.output_epub().is_file()
    codes = {a.code for a in AlertSink(run.alerts_path).load().items}
    assert AlertCode.FORMAT_FAILED.value in codes


def test_require_epubcheck_blocks_when_missing(tmp_path: Path):
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    config.validate.require_epubcheck = True
    _pipeline(run, config).run_all()
    _write_book(run)

    result = _pipeline(run, config).run_all()
    assert result.exit_code == 1
    assert run.stage(Stage.CHECK).status == StageStatus.FAILED.value


# ---------------------------------------------------------------------------
# 工程性
# ---------------------------------------------------------------------------


def test_resume_does_not_repeat_finished_stages(tmp_path: Path):
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    _pipeline(run, config).run_all()
    attempts = run.stage(Stage.PREPARE).attempts

    _write_book(run)
    _pipeline(run, config).run_all()
    assert run.stage(Stage.PREPARE).attempts == attempts, "已完成的阶段不该重跑"


def test_missing_artifacts_fails_clearly(tmp_path: Path):
    import shutil

    run, config = make_run(tmp_path, pages=2)
    _tune(config)
    shutil.rmtree(run.extracted_dir)

    result = _pipeline(run, config).run_all()
    assert result.exit_code == 1
    assert run.stage(Stage.CALIBRATE).status == StageStatus.FAILED.value
    error = run.stage(Stage.CALIBRATE).error or {}
    assert error.get("code") == "E_TASK"


def test_short_chapter_counts_as_written(tmp_path: Path):
    """薄薄一章也是内容，脚本不该按字数替 LLM 判断"写够了没"。"""
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    _pipeline(run, config).run_all()

    text_dir = run.oebps_dir / "text"
    (text_dir / "ch001.xhtml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>很短的一章</title></head>'
        "<body><h1>很短的一章</h1><p>就这一句。</p></body></html>",
        encoding="utf-8",
    )
    run.book_json.write_text(json.dumps({"title": "薄书"}), encoding="utf-8")

    result = _pipeline(run, config).run_all()
    assert run.stage(Stage.COMPOSE).status == StageStatus.DONE.value
    assert result.exit_code in (0, 2), result.summary()


def test_sample_chapter_alone_is_not_enough(tmp_path: Path):
    """只留着脚本放的样例，不算 LLM 写完了。"""
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    _pipeline(run, config).run_all()

    assert (run.oebps_dir / "text" / "ch001.xhtml").is_file(), "样例应当已被放好"
    result = _pipeline(run, config).run_all()
    assert result.exit_code == 3
    assert run.stage(Stage.COMPOSE).status == StageStatus.BLOCKED.value


def test_repeated_waiting_alerts_are_collapsed(tmp_path: Path):
    """反复 run 不该把同一条"等你干活"的告警刷成一屏。"""
    run, config = make_run(tmp_path, pages=2)
    _tune(config)
    for _ in range(3):
        assert _pipeline(run, config).run_all().exit_code == 3

    alerts = AlertSink(run.alerts_path).load()
    calibrate = [a for a in alerts.items if a.code == AlertCode.CALIBRATE_REQUIRED.value]
    assert len(calibrate) == 1, "同一条告警应当合并"
    assert calibrate[0].occurrences >= 3


def test_exit_code_reflects_this_run_not_history(tmp_path: Path):
    """历史告警不该让一次干净的收尾永远报 exit 2。"""
    run, config = make_run(tmp_path, pages=2)
    _tune(config)
    assert _pipeline(run, config).run_all().exit_code == 3  # 留下 CALIBRATE_REQUIRED

    run.counters["calibrate_accepted"] = True
    run.save()
    _pipeline(run, config).run_all()
    _write_book(run)
    assert _pipeline(run, config).run_all().exit_code in (0, 2)

    # 再跑一次：什么新问题都没出，应当是干净的 0
    result = _pipeline(run, config).run_all()
    assert result.exit_code == 0, result.summary()
    assert AlertSink(run.alerts_path).load().items, "历史告警仍然保留在案"


def test_packaging_reuses_agent_edits_after_rebuild(tmp_path: Path):
    """重跑 check 不会把 LLM 写好的章节弄丢。"""
    run, config = make_run(tmp_path, pages=2, clean=True)
    _tune(config)
    _pipeline(run, config).run_all()
    _write_book(run)
    _pipeline(run, config).run_all()

    before = run.output_epub().read_bytes()
    result = _pipeline(run, config).run_all(PipelineOptions(force=("check",)))
    assert result.exit_code in (0, 2)
    assert run.output_epub().read_bytes() == before

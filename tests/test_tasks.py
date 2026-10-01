"""工单：脚本给 LLM 准备的东西够不够用、说得清不清楚。"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_run

from pdf2epub import tasks
from pdf2epub.alerts import AlertSink
from pdf2epub.bundle import load_bundle
from pdf2epub.config import CalibrateConfig, ComposeConfig


def _bundles(run):
    return [load_bundle(child) for child in sorted(run.extracted_dir.glob("*")) if child.is_dir()]


def test_calibration_scaffold_is_self_contained(tmp_path: Path):
    run, config = make_run(tmp_path, pages=3)
    sink = AlertSink(run.alerts_path)
    task = tasks.scaffold_calibration(
        run, _bundles(run), CalibrateConfig(), pdf_path=run.input_pdf, alerts=sink
    )

    assert run.calibrate_source.is_file(), "必须给出一份可编辑的工作稿"
    assert run.segments_path.is_file()
    assert task.report.is_file()

    report = run.calibrate_report.read_text(encoding="utf-8")
    # 报告必须让 LLM 知道：改哪个文件、看图在哪、第几页、原文是什么
    assert str(run.calibrate_source) in report
    assert str(run.pages_dir) in report
    assert "巳经" in report, "低分段的原文应当在报告里"
    assert "score" in report

    segments = json.loads(run.segments_path.read_text(encoding="utf-8"))
    assert segments["total_segments"] > 0
    assert segments["segments"], "应当有低分段"
    assert segments["segments"][0]["page"] == 1


def test_calibration_renders_pages_with_context(tmp_path: Path):
    run, _ = make_run(tmp_path, pages=6, low=(2,))
    sink = AlertSink(run.alerts_path)
    tasks.scaffold_calibration(
        run,
        _bundles(run),
        CalibrateConfig(context_pages=1),
        pdf_path=run.input_pdf,
        alerts=sink,
    )

    rendered = sorted(p.name for p in run.pages_dir.glob("*.png"))
    # 低分段在第 3 页（1-based），上下文各一页 -> 第 2/3/4 页
    assert rendered == ["page-0002.png", "page-0003.png", "page-0004.png"], rendered


def test_calibration_does_not_clobber_agent_edits(tmp_path: Path):
    run, _ = make_run(tmp_path, pages=2)
    sink = AlertSink(run.alerts_path)
    tasks.scaffold_calibration(run, _bundles(run), CalibrateConfig(), pdf_path=run.input_pdf, alerts=sink)

    run.calibrate_source.write_text("# 我改过了", encoding="utf-8")
    tasks.scaffold_calibration(run, _bundles(run), CalibrateConfig(), pdf_path=run.input_pdf, alerts=sink)

    assert run.calibrate_source.read_text(encoding="utf-8") == "# 我改过了"


def test_calibration_report_when_nothing_is_low(tmp_path: Path):
    run, _ = make_run(tmp_path, pages=3, clean=True)
    sink = AlertSink(run.alerts_path)
    task = tasks.scaffold_calibration(
        run, _bundles(run), CalibrateConfig(), pdf_path=run.input_pdf, alerts=sink
    )
    assert task.counts["low_segments"] == 0
    report = run.calibrate_report.read_text(encoding="utf-8")
    assert "好消息" in report
    assert "done calibrate" in report, "要告诉 LLM 可以直接跳过"


def test_compose_scaffold_gives_working_material(tmp_path: Path):
    run, _ = make_run(tmp_path, pages=2)
    task = tasks.scaffold_compose(run, _bundles(run), ComposeConfig())

    assert run.book_json.is_file()
    template = json.loads(run.book_json.read_text(encoding="utf-8"))
    assert "spine" in template and "toc" in template

    sample = run.oebps_dir / "text" / "ch001.xhtml"
    assert sample.is_file(), "应当放一个样例章节"
    assert (run.oebps_dir / "style" / "base.css").is_file()
    assert list((run.oebps_dir / "images").glob("*.png")), "图片应当被搬好"

    brief = task.report.read_text(encoding="utf-8")
    assert "book.json" in brief
    assert str(run.build_dir) in brief
    assert "content.opf" in brief, "要明说 OPF 不用 LLM 管"
    assert "../images/" in brief, "要讲清图片怎么引用"


def test_compose_scaffold_is_idempotent(tmp_path: Path):
    run, _ = make_run(tmp_path, pages=2)
    tasks.scaffold_compose(run, _bundles(run), ComposeConfig())

    mine = json.dumps({"title": "我填的"}, ensure_ascii=False)
    run.book_json.write_text(mine, encoding="utf-8")
    chapter = run.oebps_dir / "text" / "ch002.xhtml"
    chapter.write_text("<html/>", encoding="utf-8")

    tasks.scaffold_compose(run, _bundles(run), ComposeConfig())
    assert run.book_json.read_text(encoding="utf-8") == mine, "不能覆盖 LLM 填好的书目"
    assert chapter.is_file(), "已经有章节了就不该再放样例"


def test_compose_sample_omits_image_when_no_images(tmp_path: Path):
    run, _ = make_run(tmp_path, pages=2)
    for image in (run.oebps_dir / "images").glob("*"):
        image.unlink()
    for child in run.extracted_dir.rglob("images"):
        for image in child.glob("*"):
            image.unlink()

    tasks.scaffold_compose(run, _bundles(run), ComposeConfig())
    sample = (run.oebps_dir / "text" / "ch001.xhtml").read_text(encoding="utf-8")
    assert "<img" not in sample, "没有图片时不该在样例里诱导 LLM 引用不存在的图"

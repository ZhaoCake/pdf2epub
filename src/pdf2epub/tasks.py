"""给 LLM 派工单：把"该干什么"写成文件，摆在它面前。

两个工单，边界就是"谁来干活"：

- **校准工单**（``calibrate/``）：脚本挑出 MinerU 自己都没把握的段落，
  渲染相关页的截图，让 LLM 对着图把文本改对。LLM 直接编辑 ``calibrate/source.md``。
- **撰写工单**（``build/``）：脚本把图片搬好、书目模板和样例章节摆好，
  LLM 直接写 ``build/book.json`` 与 ``build/OEBPS/text/*.xhtml``。

这里刻意不做任何"内容生成"——脚本只负责搬运、截图、写说明书。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import pageimage
from .alerts import AlertCode, AlertSink
from .bundle import Bundle, copy_images, read_markdown
from .config import ComposeConfig, CalibrateConfig
from .logutil import get_logger
from .runstate import Run, atomic_write_json
from .segments import Segment, low_confidence

log = get_logger("tasks")

BASE_CSS = """\
/* pdf2epub 的默认样式：够用就好，内容才是重点 */
html { font-size: 100%; }
body { line-height: 1.6; margin: 0 0.6em; }
h1, h2, h3 { line-height: 1.3; margin: 1.2em 0 0.6em; }
p { margin: 0 0 0.8em; text-indent: 2em; }
figure { margin: 1em 0; text-align: center; }
img { max-width: 100%; height: auto; }
figcaption, caption { font-size: 0.9em; color: #444; text-align: center; }
table { border-collapse: collapse; margin: 1em auto; }
th, td { border: 1px solid #999; padding: 0.3em 0.6em; }
"""

#: 样例章节里的标记句。流水线靠它区分"脚本放的样例"和"LLM 写出来的真章节"，
#: 所以这句话不能出现在任何真实内容里。
SAMPLE_MARKER = "这是样例章节，用来说明文件应该长什么样"

SAMPLE_CHAPTER = f"""\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh" lang="zh">
  <head>
    <title>示例章节标题</title>
    <link rel="stylesheet" type="text/css" href="../style/base.css"/>
  </head>
  <body>
    <h1>示例章节标题</h1>
    <p>{SAMPLE_MARKER}。真正的内容请按 calibrate/source.md 写。</p>
    <figure>
      <img src="../images/example.png" alt="示例插图"/>
      <figcaption>图 1　示例插图</figcaption>
    </figure>
  </body>
</html>
"""

BOOK_JSON_TEMPLATE: dict[str, Any] = {
    "title": "",
    "author": "",
    "language": "zh",
    "publisher": "",
    "description": "",
    "identifier": "",
    "cover": "",
    "spine": [],
    "toc": [],
}


@dataclass
class Task:
    """一个派给 LLM 的工单。"""

    stage: str
    directory: Path
    report: Path
    files: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "directory": str(self.directory),
            "report": str(self.report),
            "files": self.files,
            "counts": self.counts,
        }


# ---------------------------------------------------------------------------
# 校准工单
# ---------------------------------------------------------------------------


def scaffold_calibration(
    run: Run,
    bundles: list[Bundle],
    config: CalibrateConfig,
    *,
    pdf_path: Path,
    alerts: AlertSink,
) -> Task:
    """准备校准工单：挑低分段、渲染相关页、写说明书。"""
    run.calibrate_dir.mkdir(parents=True, exist_ok=True)

    # 1) 合并各 chunk 的 Markdown 成一份可编辑的工作稿（存在就不覆盖，保住 LLM 的改动）
    source = run.calibrate_source
    if not source.is_file():
        chunks = [read_markdown(bundle.markdown) for bundle in bundles]
        merged = "\n\n".join(text for text in chunks if text.strip())
        if not merged.strip():
            merged = "<!-- MinerU 没有返回 Markdown。可参考 images/ 与 pages/ 自行转写。 -->\n"
        source.write_text(merged, encoding="utf-8")
        log.info("已生成校准工作稿：%s（%d 字）", source, len(merged))

    # 2) 抽低分段
    segments: list[Segment] = []
    for bundle in bundles:
        from .segments import extract_segments

        segments.extend(extract_segments(bundle))
    low = low_confidence(segments, threshold=config.score_threshold, limit=config.max_segments)

    atomic_write_json(
        run.segments_path,
        {
            "schema_version": 1,
            "total_segments": len(segments),
            "threshold": config.score_threshold,
            "source_md": str(source),
            "segments": [segment.to_dict() for segment in low],
        },
    )

    # 3) 渲染相关页（含上下文页），并搬运 MinerU 抽出的图
    wanted: set[int] = set()
    for segment in low:
        for offset in range(-config.context_pages, config.context_pages + 1):
            page_idx = segment.page_idx + offset
            if page_idx >= 0:
                wanted.add(page_idx)

    pages = _render_pages(pdf_path, sorted(wanted), run.pages_dir, config)
    images = copy_images(bundles, run.calibrate_dir / "images")

    report = _calibration_report(run, low, pages, images, merged_source_len=len(read_markdown(source)))
    run.calibrate_report.write_text(report, encoding="utf-8")

    if low and not pages:
        alerts.warn(
            AlertCode.NO_PAGE_IMAGE,
            "没有生成任何页面截图，多模态校准会退化成纯文本校准",
            hint="安装 pypdfium2 与 Pillow：pip install pypdfium2 Pillow",
        )

    return Task(
        stage="calibrate",
        directory=run.calibrate_dir,
        report=run.calibrate_report,
        files=[str(source), str(run.segments_path)],
        counts={
            "total_segments": len(segments),
            "low_segments": len(low),
            "pages_rendered": len(pages),
            "images": len(images),
        },
    )


def _render_pages(
    pdf_path: Path, page_indices: list[int], dest: Path, config: CalibrateConfig
) -> list[Path]:
    if not page_indices:
        return []
    if not pageimage.available():
        log.warning("缺少 pypdfium2/Pillow，跳过页面渲染")
        return []

    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for page_idx in page_indices:
        target = dest / f"page-{page_idx + 1:04d}.png"
        if target.is_file():
            written.append(target)
            continue
        rendered = pageimage.render_page(
            pdf_path, page_idx, dpi=config.render_dpi, max_width=config.max_image_width
        )
        if rendered is None:
            continue
        pageimage.save_png(rendered.image, target)
        written.append(target)

    log.info("已渲染 %d 页截图到 %s", len(written), dest)
    return written


def _excerpt(text: str, needle: str, limit: int) -> str:
    """在整篇 Markdown 里找到这段的位置，截出前后一段作为上下文。"""
    condensed = needle.strip().splitlines()[0][:60] if needle.strip() else ""
    position = text.find(condensed) if condensed else -1
    if position < 0:
        return ""
    half = max(80, limit // 2)
    start = max(0, position - half)
    end = min(len(text), position + half)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _calibration_report(
    run: Run,
    low: list[Segment],
    pages: list[Path],
    images: dict[str, str],
    *,
    merged_source_len: int,
) -> str:
    source_text = read_markdown(run.calibrate_source)
    out: list[str] = [
        "# 校准工单",
        "",
        "MinerU 把这本书解析出来了，但有些地方**它自己也没把握**。",
        "请对着页面截图，把认错的地方改对。",
        "",
        "## 你要改的东西",
        "",
        f"- 唯一的可编辑文件：`{run.calibrate_source}`",
        f"- 页面截图：`{run.pages_dir}`（`page-0007.png` = 第 7 页）",
        f"- MinerU 抽出的图片：`{run.calibrate_dir / 'images'}`",
        f"- 原始 PDF：`{run.input_pdf}`",
        f"- 机器可读的同一份清单：`{run.segments_path}`",
        f"- 改完执行：`pdf2epub run`（或 `pdf2epub done calibrate`）",
        "",
        "## 规矩（重要）",
        "",
        "1. **只改识别错的地方**：形近字、串行、公式符号、表格串列、段落粘连。",
        "2. **不要重写文案**，不要调整风格，不要补充原文里没有的内容。",
        "3. 看图确认机器其实没认错的，**保持原样**即可，不必强行改动。",
        "4. 实在无法辨认的（图糊、缺页），保持原样，并写进 "
        f"`{run.calibrate_dir / 'notes.md'}` 说明情况。",
        "5. 不确定的地方宁可不改——改错比不改更糟。",
        "",
        "## 概况",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 工作稿字数 | {merged_source_len} |",
        f"| 带分数的段落 | {len(low)} 条低于阈值（全部低分段） |",
        f"| 已渲染页面 | {len(pages)} 张 |",
        f"| 已搬运图片 | {len(images)} 张 |",
        "",
    ]

    if not low:
        out.extend(
            [
                "## 好消息",
                "",
                "MinerU 没有给出任何低置信度段落。你可以：",
                "",
                "1. 快速浏览一遍 `source.md`，有问题就顺手改；",
                "2. 没问题就直接执行 `pdf2epub done calibrate` 进入撰写阶段。",
                "",
            ]
        )
        return "\n".join(out)

    out.extend(["## 低分段落清单（分数升序）", ""])
    for index, segment in enumerate(low, 1):
        excerpt = _excerpt(source_text, segment.text, 1200)
        out.extend(
            [
                f"### {index}. 第 {segment.page_no} 页 · score {segment.score:.3f} · {segment.kind}",
                "",
                f"- 编号：`{segment.segment_id}`",
                f"- 截图：`{run.pages_dir / f'page-{segment.page_no:04d}.png'}`",
                "",
                "MinerU 认出来的原文：",
                "",
                "```text",
                segment.text[:1200],
                "```",
                "",
            ]
        )
        if excerpt:
            out.extend(["工作稿里附近的上下文：", "", "```text", excerpt, "```", ""])
        if segment.bbox:
            out.extend([f"- 版面位置（PDF 坐标）：`{tuple(round(v, 1) for v in segment.bbox)}`", ""])

    out.extend(
        [
            "## 改完之后",
            "",
            "```",
            "pdf2epub run",
            "```",
            "",
            "脚本会检查 `source.md` 是否被改过；改过就进入撰写阶段。",
            "如果看完觉得确实不需要改，用 `pdf2epub done calibrate` 明确跳过。",
            "",
        ]
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 撰写工单
# ---------------------------------------------------------------------------


def scaffold_compose(run: Run, bundles: list[Bundle], config: ComposeConfig) -> Task:
    """准备撰写工单：建目录、搬图片、放模板与样例。"""
    oebps = run.oebps_dir
    text_dir = oebps / "text"
    for path in (text_dir, oebps / "images", oebps / "style"):
        path.mkdir(parents=True, exist_ok=True)

    style = oebps / "style" / "base.css"
    if not style.is_file():
        style.write_text(BASE_CSS, encoding="utf-8")

    images = copy_images(bundles, oebps / "images")

    if not run.book_json.is_file():
        atomic_write_json(run.book_json, BOOK_JSON_TEMPLATE)

    created_sample = False
    if config.scaffold_sample and not any(text_dir.glob("*.xhtml")):
        # 图片目录空的话，样例里的图会指向不存在的文件，反而误导；去掉 figure
        sample = SAMPLE_CHAPTER
        if not images:
            sample = sample.replace(
                """    <figure>
      <img src="../images/example.png" alt="示例插图"/>
      <figcaption>图 1　示例插图</figcaption>
    </figure>
""",
                "",
            )
        (text_dir / f"{config.chapter_prefix}001.xhtml").write_text(sample, encoding="utf-8")
        created_sample = True

    brief = _compose_report(run, images, sample=created_sample)
    (run.build_dir / "BRIEF.md").write_text(brief, encoding="utf-8")

    return Task(
        stage="compose",
        directory=run.build_dir,
        report=run.build_dir / "BRIEF.md",
        files=[str(run.book_json), str(text_dir)],
        counts={"images": len(images), "sample_created": created_sample},
    )


def _compose_report(run: Run, images: dict[str, str], *, sample: bool) -> str:
    return "\n".join(
        [
            "# 撰写工单",
            "",
            "把校准后的内容**直接写成 EPUB**。中间不需要任何转换器——",
            "你写出来的 `OEBPS/` 目录树就是最终电子书的正文。",
            "",
            "## 你要产出两样东西",
            "",
            "1. `book.json` —— 书目信息和阅读顺序（模板已放好，填空即可）",
            "2. `OEBPS/text/*.xhtml` —— 章节正文，一章一个文件",
            "",
            "## 目录现状",
            "",
            "```",
            str(run.build_dir),
            "├── book.json                 ← 你要填的",
            "├── BRIEF.md                  ← 本文件",
            "└── OEBPS/",
            "    ├── text/                 ← 你要写的章节",
            f"    ├── images/               ← 已搬好 {len(images)} 张图，直接引用",
            "    └── style/base.css        ← 样式已备好，引用即可",
            "```",
            "",
            "## book.json 的字段",
            "",
            "```json",
            json.dumps(
                {
                    "title": "书名",
                    "author": "作者",
                    "language": "zh",
                    "publisher": "",
                    "description": "",
                    "identifier": "",
                    "cover": "images/cover.png",
                    "spine": ["text/ch001.xhtml", "text/ch002.xhtml"],
                    "toc": [{"title": "第一章", "href": "text/ch001.xhtml", "level": 1}],
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "- `spine` 是阅读顺序，必须覆盖所有章节文件；留空则由脚本按文件名排序。",
            "- `toc` 是目录；留空则由脚本按 `spine` 生成一层目录。",
            "- `cover` 留空则不生成封面页。",
            "- 不确定的元数据宁可留空，脚本不会因为空值报错。",
            "",
            "## 章节 XHTML 只有三条硬要求",
            "",
            "1. 良构 XML，根元素带命名空间："
            '`<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh" lang="zh">`',
            "2. 有 `<head><title>…</title></head>` 和 `<body>`。",
            "3. 图片用相对 `text/` 的路径引用：`../images/xxx.png`，"
            "并且每个 `<img>` 都要有 `alt`。",
            "",
            "内容来源是 `" + str(run.calibrate_source) + "`（校准后的工作稿）"
            "以及 MinerU 的解析产物，**不要自己编内容**。",
            "",
        ]
        + (
            [
                "## 样例",
                "",
                f"`OEBPS/text/` 下已经放了一个样例章节，照着写就行；"
                "它只是形状示范，请直接覆盖或删掉它。",
                "",
            ]
            if sample
            else []
        )
        + [
            "## 不用你操心的事",
            "",
            "- `content.opf` / `nav.xhtml` / `META-INF/container.xml`：打包时脚本生成",
            "- zip 结构、mimetype、media-type、spine 合法性：脚本负责",
            "- 目录层级与样式：已备好",
            "",
            "## 做完之后",
            "",
            "```",
            "pdf2epub run",
            "```",
            "",
            "脚本会打包、跑格式校验，结果写在 `check/report.md`。",
            "有问题它会指到具体文件和行号，改完再跑一次即可。",
            "",
        ]
    )

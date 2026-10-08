#!/usr/bin/env python
"""OCR 体检：把解析产物摊成"每页一段"，跑一组全局检测器，出一份体检报告。

为什么要它：PaddleOCR-VL 不输出逐段置信度，校准阶段的低分清单**恒为空**，
"该看哪里"这件事脚本不给任何线索。实测（见 docs/agent-guide.md）真正有效的是
**先按模式全局搜一遍，再对可疑处看图确认**——而不是从第 1 页硬啃到第 351 页。

它做六件事：

1. **页级索引**：产出 ``<run>/calibrate/source_paged.md``，在正文里插入
   ``<!-- 第 N 页 -->`` 标记（原书页码），供人按页定位；``source.md`` 本身
   保持干净（它是流水线的输入，不要塞标记进去）。
2. **结构异常**：极短页、超长页、相邻页高度雷同（扫描透印/复读）。
3. **重复内容**：整段逐字复读（幻觉的典型特征）；相邻页去标签后大段逐字相同
   （"跨页扫再切开"的扫描把上一页的尾巴又印了一遍）。
4. **跨页断句**：左页末字符不是终止标点、右页首字符是正文 —— 跨页被切断的候选。
5. **页码混入正文**："跨页扫再切开"时页边页码方框会被整页模型顺着读进句子
   （如 ``另58一个``）；中文之间夹 1~3 位数字且不是量词/编号语境的列出来对图确认。
6. **模式命中**：定界符失配、裸 LaTeX 记号、幻觉关键词、坏字形、常见混淆。

结论写成 ``<run>/calibrate/qc-report.md``；每条命中都给页码，便于直接对照
``pages/page-NNNN.png`` 看图（没渲染的页用 ``--render`` 补渲染）。

用法::

    python scripts/qc_scan.py --run <run_id>            # --run 省略则用最近一次
    python scripts/qc_scan.py --run <run_id> --render    # 顺带渲染所有命中页

只读 + 渲染，不改任何产物。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

try:
    from pdf2epub import pageimage
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore, read_json
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")


# ---------------------------------------------------------------------------
# 页级切分
# ---------------------------------------------------------------------------


@dataclass
class Page:
    number: int      # 原书页码（1 基）
    chunk: str
    text: str


def read_pages(run) -> list[Page]:
    """按 chunk 顺序读出每页文本，页码映射回原书。"""
    pages: list[Page] = []
    for chunk in run.counters.get("chunks") or []:
        chunk_id = str(chunk.get("chunk_id") or "")
        if not chunk_id:
            continue
        start = int(chunk.get("page_start") or 0)
        listing = run.extracted_dir / chunk_id / "content_list.json"
        if not listing.is_file():
            continue
        entries = json.loads(listing.read_text(encoding="utf-8"))
        for index, entry in enumerate(entries):
            pages.append(
                Page(number=start + index + 1, chunk=chunk_id, text=str(entry.get("text") or ""))
            )
    return pages


#: 句子终止标点：左页以这些结尾就不像"被切断"。
SENTENCE_END = "。！？…；;!?：:" 
#: 右页以这些开头说明是新的一块内容，不是断句续写。
BLOCK_START = re.compile(r"^\s*(#|【|例|图|表|注|解|分析|证明|题型|习题|第\s*\d+\s*[章节讲]|<!--)")

#: 幻觉高发词：与考研数学讲义无关的跨领域常识、凭空出现的人名日期。
HALLUCINATION_HINTS = [
    r"E\s*=\s*mc", r"能量守恒", r"C₆H₁₂O₆", r"氢键", r"光合作用", r"牛顿第一定律",
    r"20\d\d\s*年\s*\d+\s*月\s*\d+\s*日", r"习近平", r"马克思", r"元素周期表",
]

#: 裸 LaTeX：不该出现在 $…$ 之外的记号。两类都收：
#: 反斜杠命令（``\lim``、``\frac``）**和**裸露的下标上标（``x_{2}``）——
#: 后者没有反斜杠，只按命令名搜会整片漏掉（"x=x_{2}，左侧单调递减"就是这么漏的）。
BARE_LATEX = re.compile(r"(?<!\\)\\[a-zA-Z]{2,}|(?:[0-9A-Za-z\)\]])[_^]\{")

#: 常见混淆：整串重复 &、坏字形方块、中文里孤立的拉丁小写 o（多半是句号被认错）。
LATIN_NOISE = re.compile(r"[\u4e00-\u9fff]\s+o\s+[\u4e00-\u9fff]")
DOUBLE_AMP = re.compile(r"&amp;&amp;")
BROKEN_GLYPH = re.compile(r"[\u25a1\ufffd]")

#: 页边页码方框被读进正文的样子：中文/中文标点之间夹 1~3 位数字（无空格）。
STAMP = re.compile(r"(?<=[\u4e00-\u9fff。，、；：？！）】”])(\d{1,3})(?=[\u4e00-\u9fff（【“])")
#: 数字前面是这些字，说明是正常语义里的数量/编号，不算页码
NOT_PAGENUM_BEFORE = set(
    "第共约近超达至了于和与及等该这此其每余前后左右年月日时分秒版次章节点页个条"
    "件项类种代期层位名分数值比中上下不的之与图表格有為为是在要需能可占到从由多小大"
)
#: 数字后面接这些单位/量词，同样不算页码
NOT_PAGENUM_AFTER = (
    "美元", "元", "瓦", "天", "倍", "个", "种", "世纪", "年", "月", "日", "字节", "位", "行", "列",
    "块", "级", "条", "页", "秒", "小时", "分钟", "纳秒", "毫秒", "微秒", "纳米", "平方", "次", "项",
    "GHz", "MHz", "Hz", "KB", "MB", "GB", "TB", "nm", "bit", "ms", "ns", "cm", "mm", "%", "％", "℃",
)
#: 页码量级上限（按书调整；只是"值得看一眼"的初筛）
MAX_PAGE_NO = 999

#: 去标签 + 压空白：相邻页大段重复要按"眼睛看到的文本"比，表格样板不算
TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", text)).strip()


def _is_stamp(text: str, match: re.Match) -> bool:
    value = match.group(1)
    if text[match.start() - 1] in NOT_PAGENUM_BEFORE:
        return False
    if text[match.end() : match.end() + 2].startswith(NOT_PAGENUM_AFTER):
        return False
    if len(value) == 3 and value[0] == "0":
        return False
    return int(value) <= MAX_PAGE_NO


def detectors(pages: list[Page]) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = defaultdict(list)

    # ---- 1. 结构异常 ----
    lengths = {page.number: len(page.text.strip()) for page in pages}
    if lengths:
        median = sorted(lengths.values())[len(lengths) // 2]
        for number, size in sorted(lengths.items()):
            if size == 0:
                findings["空白页"].append(f"第 {number} 页：0 字（透印/装订占位？对图确认）")
            elif size < max(60, median * 0.15):
                findings["极短页"].append(f"第 {number} 页：{size} 字（中位数 {median}），对图确认没漏内容")

    # ---- 2. 相邻页雷同（复读/透印鬼影） ----
    order = sorted(pages, key=lambda p: p.number)
    for left, right in zip(order, order[1:]):
        a, b = left.text.strip(), right.text.strip()
        if len(a) < 80 or len(b) < 80:
            continue
        if a[:80] == b[:80]:
            findings["相邻页雷同"].append(f"第 {left.number}/{right.number} 页开头 80 字相同")

    # ---- 3. 相邻页大段重复（扫描"跨页扫再切开"把上一页尾巴又印一遍） ----
    # 去标签再比：表格里 <td style=...> 这类样板会互相命中，制造满屏假重复；
    # 每对页只报最大的重复块，且要求重复里有实词（纯数字/符号的雷同多为页眉页脚）。
    for left, right in zip(order, order[1:]):
        if right.number != left.number + 1:
            continue
        a, b = _plain(left.text)[-2000:], _plain(right.text)[:4000]
        if len(a) < 120 or len(b) < 120:
            continue
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        block = max(matcher.get_matching_blocks(), key=lambda blk: blk.size)
        if block.size < 120:
            continue
        frag = a[block.a : block.a + block.size]
        if len(re.findall(r"[\u4e00-\u9fff]", frag)) + len(frag) // 2 < 40:
            continue
        findings["相邻页大段重复"].append(
            f"第 {left.number} → {right.number} 页：{block.size} 字相同「{frag[:60]}…」"
        )

    # ---- 4. 段落复读（**整段**逐字重复才算数，幻觉的典型特征） ----
    # 只比前缀会误报：同一页里几句都以同一个长公式开头，前缀一样、后面并不一样。
    seen: Counter[str] = Counter()
    where: dict[str, list[int]] = defaultdict(list)
    for page in pages:
        for para in re.split(r"\n\s*\n", page.text):
            para = para.strip()
            if len(para) < 40 or para.startswith(("<div", "<!--", "|")):
                continue
            seen[para] += 1
            where[para].append(page.number)
    for key, count in seen.most_common():
        if count >= 2:
            pages_hit = "、".join(str(n) for n in where[key])
            findings["段落复读"].append(f"出现 {count} 次（第 {pages_hit} 页）：{key[:60]}…")

    # ---- 5. 页码混入正文（页边页码方框被整页模型顺着读进句子） ----
    for page in pages:
        for match in STAMP.finditer(page.text):
            if not _is_stamp(page.text, match):
                continue
            findings["页码混入正文"].append(
                f"第 {page.number} 页 [{match.group(1)}]：…{_context(page.text, match.start(), 26)}…"
            )

    # ---- 6. 跨页断句 ----
    for left, right in zip(order, order[1:]):
        if right.number != left.number + 1:
            continue
        tail = re.sub(r"[\s\u3000]+$", "", left.text)
        head = right.text.lstrip()
        if not tail or not head:
            continue
        if tail[-1] in SENTENCE_END or tail.endswith(("$", "）", "】", ".", "，", "、")):
            continue
        if BLOCK_START.match(head):
            continue
        if head[0] in "#*-0123456789ABCD（(":
            continue
        findings["跨页断句候选"].append(
            f"第 {left.number} → {right.number} 页：左尾「…{tail[-18:]}」 / 右首「{head[:18]}…」"
        )

    # ---- 7. 定界符失配（行内公式 $ 个数为奇数） ----
    for page in pages:
        for index, line in enumerate(page.text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("<!--"):
                continue
            if stripped.count("$") % 2:
                findings["定界符失配"].append(f"第 {page.number} 页第 {index} 行（$ 计数为奇数）：{stripped[:70]}")

    # ---- 8. 裸 LaTeX / 坏字形 / 混淆 ----
    # 裸 LaTeX = 定界符没跟上，公式体漏进了正文。这才是这份产物里最该找的东西：
    # 形如 `x=x_{2}，左侧单调递减`、`设 ，则 \lim_{x\to…}`（连定界符和公式一起丢）。
    for page in pages:
        body = strip_math(page.text)
        for match in BARE_LATEX.finditer(body):
            findings["裸 LaTeX"].append(f"第 {page.number} 页：…{_context(body, match.start())}…")
        for match in DOUBLE_AMP.finditer(page.text):
            findings["重复转义 &"].append(f"第 {page.number} 页：…{_context(page.text, match.start())}…")
        for match in BROKEN_GLYPH.finditer(page.text):
            findings["坏字形"].append(f"第 {page.number} 页：…{_context(page.text, match.start())}…")
        for match in LATIN_NOISE.finditer(page.text):
            findings["中文里的孤立 o"].append(f"第 {page.number} 页：…{_context(page.text, match.start())}…")

    # ---- 9. 幻觉关键词 ----
    for pattern in HALLUCINATION_HINTS:
        regex = re.compile(pattern)
        for page in pages:
            for match in regex.finditer(page.text):
                findings["幻觉关键词"].append(
                    f"第 {page.number} 页：命中 /{pattern}/ —— …{_context(page.text, match.start())}…"
                )

    return findings


def _context(text: str, position: int, width: int = 28) -> str:
    start = max(0, position - width)
    end = min(len(text), position + width)
    return text[start:end].replace("\n", " ")


#: 整段公式 / 行内公式。**先吃 ``$$…$$`` 再吃 ``$…$``**：反过来会把 ``$$`` 当成
#: 一对空的行内公式，剩下的公式体就全被当成正文，制造满屏假的"裸 LaTeX"。
DISPLAY_MATH = re.compile(r"\$\$[\s\S]*?\$\$")
INLINE_MATH = re.compile(r"\$[^$\n]*\$")


def strip_math(text: str) -> str:
    return INLINE_MATH.sub(" ", DISPLAY_MATH.sub(" ", text))


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def write_paged_source(run, pages: list[Page]) -> Path:
    target = run.calibrate_dir / "source_paged.md"
    parts: list[str] = [
        "<!-- 本文件由 scripts/qc_scan.py 生成：正文按原书页码切开，只用于定位。",
        "     真正要改的是 calibrate/source.md（本文件是它的带页码版本，改这里不作数）。 -->",
        "",
    ]
    for page in pages:
        parts.append(f"<!-- ===== 原书第 {page.number} 页（{page.chunk}）===== -->")
        parts.append("")
        parts.append(page.text.strip())
        parts.append("")
    target.write_text("\n".join(parts), encoding="utf-8")
    return target


def write_report(run, pages: list[Page], findings: dict[str, list[str]], paged: Path) -> Path:
    target = run.calibrate_dir / "qc-report.md"
    total = sum(len(p.text.strip()) for p in pages)
    by_number = {page.number: page for page in pages}
    lines: list[str] = [
        "# OCR 体检报告",
        "",
        f"由 `scripts/qc_scan.py` 生成。共 {len(pages)} 页 / {total} 字。",
        "",
        f"- 带页码的正文：`{paged}`（只用于定位；要改的是 `calibrate/source.md`）",
        f"- 页面图：`{run.pages_dir}`（`page-0007.png` = 原书第 7 页）",
        "",
        "命中项都只是**值得看一眼**，不是结论——每条都要对着页面图确认。",
        "",
        "## 汇总",
        "",
        "| 检测项 | 命中数 |",
        "| --- | --- |",
    ]
    for name in findings:
        lines.append(f"| {name} | {len(findings[name])} |")
    if not findings:
        lines.append("| （无） | 0 |")

    seam_lines = seam_section(run, by_number)
    if seam_lines:
        lines.extend(seam_lines)

    for name, items in findings.items():
        lines.extend(["", f"## {name}（{len(items)} 条）", ""])
        shown = items[:250]
        for item in shown:
            lines.append(f"- {item}")
        if len(items) > len(shown):
            lines.append(f"- …另有 {len(items) - len(shown)} 条，见 stdout 或自行按模式搜")
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def seam_section(run, by_number: dict[int, Page]) -> list[str]:
    """接缝上下文的正文对照。

    校准阶段的必做项是"每道缝的两页都看图"。看图之前先把**接缝两侧的原文**摆出来，
    大部分缝一眼就能看出接没接上（左页以句末标点收尾、右页从新段起），只有真接不上
    或者接得可疑的才需要打开 ``page-NNNN.png`` 逐字核。
    """
    seams = (run.counters.get("boundary") or {}).get("seams") or []
    if not seams:
        return []
    lines = [
        "",
        "## 接缝上下文（必做项）",
        "",
        "逐缝看：左页末尾与右页开头能否接上，**不能断在半句、也不能重复一整段**。",
        "「是否可疑」只是按标点做的初判，拿不准就打开 `pages/` 里的图确认。",
        "",
    ]
    for seam in seams:
        left_no = seam.get("left_page")
        right_no = seam.get("right_page")
        if not isinstance(left_no, int) or not isinstance(right_no, int):
            continue
        left = by_number.get(left_no)
        right = by_number.get(right_no)
        tail = re.sub(r"\s+", " ", (left.text if left else "").strip())[-160:]
        head = re.sub(r"\s+", " ", (right.text if right else "").strip())[:160]
        verdict = "看着接得上" if tail and tail[-1] in SENTENCE_END else "**可疑，看图**"
        if head and BLOCK_START.match(head):
            verdict = "右页从新段起，看着正常"
        lines.extend(
            [
                f"### 缝 {seam.get('index')}：第 {left_no} 页 → 第 {right_no} 页（{verdict}）",
                "",
                f"- 左页尾部：`…{tail}`",
                f"- 右页开头：`{head}…`",
                f"- 图：`{run.pages_dir / f'page-{left_no:04d}.png'}`、"
                f"`{run.pages_dir / f'page-{right_no:04d}.png'}`",
                "",
            ]
        )
    return lines


def render_hit_pages(run, findings: dict[str, list[str]], config) -> int:
    numbers: set[int] = set()
    for items in findings.values():
        for item in items:
            for match in re.finditer(r"第 (\d+) 页", item):
                numbers.add(int(match.group(1)))
    written = 0
    for number in sorted(numbers):
        target = run.pages_dir / f"page-{number:04d}.png"
        if target.is_file():
            continue
        rendered = pageimage.render_page(
            run.input_pdf,
            number - 1,
            dpi=config.calibrate.render_dpi,
            max_width=config.calibrate.max_image_width,
        )
        if rendered is None:
            continue
        pageimage.save_png(rendered.image, target)
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR 体检：页级索引 + 全局模式检测")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--render", action="store_true", help="顺带渲染所有命中页的页面图")
    parser.add_argument("--quiet", action="store_true", help="只在 stdout 打印汇总")
    args = parser.parse_args()

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    pages = read_pages(run)
    if not pages:
        print(f"· 没找到任何解析产物：{run.extracted_dir}（先跑 prepare）")
        return 1

    findings = detectors(pages)
    paged = write_paged_source(run, pages)
    report = write_report(run, pages, findings, paged)

    print(f"· {run.run_id}")
    print(f"· {len(pages)} 页 / {sum(len(p.text.strip()) for p in pages)} 字")
    print(f"· 页级正文：{paged}")
    print(f"· 体检报告：{report}")
    print()
    print("检测项命中：")
    for name, items in findings.items():
        print(f"  {name:<14} {len(items)}")
    if not findings:
        print("  （没有命中，正文看起来干净）")

    if args.render:
        written = render_hit_pages(run, findings, config)
        print(f"\n· 补渲染 {written} 张命中页到 {run.pages_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

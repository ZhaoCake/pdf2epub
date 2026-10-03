#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 ``build/compose-plan.json`` 把 ``calibrate/source.md`` 写成 EPUB 章节 XHTML。

和 ``scripts/compose_mathml.py`` 的分工：那个脚本自己猜章节边界（按 MinerU 的 ``## `` 启发式，
用编号里有没有点来区分章与节）；本书是 ``# 第N章`` 结构，而且前置页（书名页 / 内容简介 /
版权信息 / 前言 / 印刷目录）要各自单独成页——这些猜不出来，所以放在 plan 里由撰写者定。

机械部分照做：转义、``$$…$$`` / ``$…$`` → MathML、表格 / 插图 / 代码块的包装、
表格单元格里的字面 ``\\n`` → ``<br/>``、``border=1`` 这类不成对的属性补引号。

用法::

    python scripts/compose_book.py --run .pdf2epub/runs/<id>            # 全部重写
    python scripts/compose_book.py --run .pdf2epub/runs/<id> --audit    # 只出审计报告
    python scripts/compose_book.py --run .pdf2epub/runs/<id> --only ch005
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compose_mathml import (  # noqa: E402
    _escape_bare_amp,
    _protect_tags,
    _restore_tags,
    bad_structure,
    escape,
    sanitize,
)

try:
    import latex2mathml.converter as l2m
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("需要 latex2mathml：pip install latex2mathml")

#: MIPS 汇编里的寄存器 ``$`` 与 ``$…$`` 数学约定冲突。这两类在本书里出现，先抠出来。
REGISTER = re.compile(r"\$31|\$k[01]")
REG = "\x01"

#: 源里的公式被 HTML 转义过（``2&#x27;b00``）。latex2mathml 会把 ``&`` 当数学字符拆成
#: 一个裸 ``&``，产出的 XML 直接解析不了，所以先还原成原始字符。
ENTITIES = {
    "&#x27;": "'", "&#39;": "'", "&apos;": "'", "&quot;": '"', "&#x22;": '"',
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": "~", "&times;": r"\times",
    "&minus;": "-", "&plusmn;": r"\pm",
}
ENTITY_RE = re.compile(r"&#x[0-9A-Fa-f]+;|&#\d+;")

#: MathML 认得的元素；不在这张表里的 ``<xxx>`` 是 latex2mathml 漏转义的文本
MATHML_TAGS = (
    "math", "mrow", "mi", "mn", "mo", "mtext", "ms", "mspace", "msup", "msub",
    "msubsup", "mfrac", "msqrt", "mroot", "munder", "mover", "munderover", "mtable",
    "mtr", "mtd", "mstyle", "mpadded", "mphantom", "menclose", "mglyph", "semantics",
    "annotation", "annotation-xml",
)
#: 开/闭标签分开判：写成 ``</?(?!…)`` 时 ``/?`` 会回溯成空匹配，把合法的 ``</mi>`` 也抓进来
_NAMES = "(?:" + "|".join(MATHML_TAGS) + ")"
STRAY_OPEN = re.compile(r"<(?!/)(?!" + _NAMES + r"[\s/>])[^<>]{0,120}>")
STRAY_CLOSE = re.compile(r"</(?!" + _NAMES + r"\s*>)[^<>]{0,120}>")

#: 顶层页面模板（与示例脚本一致，只多一个 epub:type 便于阅读器识别）
PAGE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">
  <head>
    <title>{title}</title>
    <link rel="stylesheet" type="text/css" href="../style/base.css"/>
  </head>
  <body>
{body}
  </body>
</html>
"""

HEAD = re.compile(r"^(#{1,4})\s+(.*?)\s*$")
IMG_DIV = re.compile(r"<div[^>]*>(\s*<img[^>]*/>\s*)</div>", re.S)
CAPTION_DIV = re.compile(r"<div[^>]*>(.*?)</div>", re.S)
TABLE = re.compile(r"<table.*?</table>", re.S)
CELL = re.compile(r"(<td[^>]*>)(.*?)(</td>)", re.S)
#: 表格里 ``<`` 被转义过，但属性值 ``border=1`` 没引号，XHTML 过不了
UNQUOTED_ATTR = re.compile(r"\b(border|rowspan|colspan|width|height)=(\d+|\d+%|\d+\.?\d*%\d*)")
#: 代码块：RTL / 汇编 / C
CODE_START = re.compile(
    r"^\s*(?:assign\b|always\b|case[z]?\s*\(|endcase\b|end\b|begin\b|if\s*\(|else\b|for\s*\("
    r"|module\b|function\b|input\b|output\b|\d+'[bodhBODH]|always@)"
)
CODE_LINE = re.compile(
    r"^\s*(?:\d+'[bodhBODH]|[A-Za-z_][\w.]*\s*=|assign\b|end\b|endcase\b|case[z]?\b"
    r"|//|/\*|\*/|if\s*\(|else\b|for\s*\(|\}|\{|\)\s*;|[A-Z]{2,6}\s+[Rr]\d)"
)
ASM_LINE = re.compile(r"^\s*(?:[A-Z][A-Z0-9.]*\s+[Rr]\d|LDM|STM|[a-z]{2,6}\s+[A-Z]?\d+\b)")
FIG_CAPTION = re.compile(r"^图\s*[\d.]+")
TAB_CAPTION = re.compile(r"^表\s*[\d.]+")
SUBCAPTION = re.compile(r"^[（(][a-zA-Z][）)]")
TOC_LINE = re.compile(r"^(.*?)[\s.．]*?(\d{1,3})\s*$")


# ---------------------------------------------------------------------------
# 行内渲染
# ---------------------------------------------------------------------------


def unescape_entities(latex: str) -> str:
    for raw, plain in ENTITIES.items():
        latex = latex.replace(raw, plain)
    return ENTITY_RE.sub(lambda m: chr(int(m.group(0)[2:-1].lstrip("xX"), 16)), latex)


def repair_mathml(xml: str) -> str:
    """把 latex2mathml 漏掉的转义补上：裸 ``&`` 与 ``<register_list>`` 这类假标签。"""
    xml = _escape_bare_amp(xml)
    for pattern in (STRAY_OPEN, STRAY_CLOSE):
        hit = pattern.search(xml)
        if hit:
            print(f"    · 修掉漏转义的尖括号：{hit.group(0)[:40]}")
            xml = pattern.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), xml)
    return xml


def mathml(latex: str, *, display: str) -> str:
    """转 MathML；结构不合法或转不动就返回 LaTeX 兜底（调用方决定怎么显示）。"""
    source = sanitize(unescape_entities(latex).strip())
    try:
        out = repair_mathml(l2m.convert(source, display=display))
    except Exception as exc:  # pragma: no cover - 转不动就退回原文
        print(f"    ! MathML 转换失败（保留 LaTeX）：{type(exc).__name__}: {source[:50]}")
        return f'<code class="math-raw">{escape(source)}</code>'
    if bad_structure(out):
        try:
            retry = repair_mathml(l2m.convert("{" + source + "}", display=display))
            if not bad_structure(retry):
                print(f"    · 加分组括号后转好：{source[:50]}")
                return retry
        except Exception:
            pass
        print(f"    ! MathML 结构不合法（保留 LaTeX）：{source[:50]}")
        return f'<code class="math-raw">{escape(source)}</code>'
    return out


def render_display(latex: str) -> str:
    """独立公式：是汇编清单就还原成代码；MathML 转不动就退回可读文本。"""
    latex = unescape_entities(latex.strip())
    if is_asm_math(latex):
        return f'  <pre class="code">{escape(latex_plain(latex))}</pre>'
    body = mathml(latex, display="block")
    if 'class="math-raw"' in body:
        return f'  <p class="formula-text">{escape(latex_plain(latex))}</p>'
    return f'  <div class="math-block">\n    {body}\n  </div>'


def inline(text: str) -> str:
    """行内：``$…$`` → MathML，寄存器 ``$`` 保留为文本，其余转义。"""
    text = REGISTER.sub(lambda m: m.group(0).replace("$", REG), text)
    text, stash = _protect_tags(text)
    parts = text.split("$")
    if len(parts) % 2 == 0:  # 有落单的 $，整段按文本处理，别猜
        out = escape(text)
    else:
        out = "".join(
            mathml(part, display="inline") if index % 2 else escape(part)
            for index, part in enumerate(parts)
        )
    return _restore_tags(out, stash).replace(REG, "$")


def inline_keep_html(text: str) -> str:
    """表格单元格：里面可能已经有 ``<br/>`` 之类，逐片处理，不整体转义。"""
    out: list[str] = []
    for index, part in enumerate(re.split(r"(<[^>]+>)", text)):
        out.append(part if index % 2 else inline(part))
    return "".join(out)


def render_table(html: str) -> str:
    html = UNQUOTED_ATTR.sub(lambda m: f'{m.group(1)}="{m.group(2)}"', html)
    html = html.replace("\\n", "<br/>")

    def cell(match: re.Match) -> str:
        return match.group(1) + inline_keep_html(match.group(2)) + match.group(3)

    return CELL.sub(cell, html)


def fix_img(html: str) -> str:
    """图片属性修成 EPUB 认的：路径加 ``../``，``width="79%"`` 挪进 ``style``。

    XHTML 里 ``width`` 只接受整数，百分比会让 EPUBCheck 报 RSC-005。
    """
    html = html.replace('src="images/', 'src="../images/')
    return re.sub(r'\swidth="(\d+(?:\.\d+)?)%"', r' style="width:\1%"', html)


def render_figure(img_html: str, caption: str = "") -> str:
    img_html = fix_img(img_html)
    img_html = re.sub(r'alt="[^"]*"', f'alt="{escape(caption or "插图")}"', img_html)
    lines = ["  <figure>", f"    {img_html.strip()}"]
    if caption:
        lines.append(f"    <figcaption>{inline(caption)}</figcaption>")
    lines.append("  </figure>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 块分类
# ---------------------------------------------------------------------------


def is_code(block: str) -> bool:
    """整块都像代码才算代码，避免把正文里一句 ``if(a == 0)`` 误判。"""
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) == 1 and len(lines[0]) > 120 and not CODE_START.match(lines[0]):
        return False
    hits = sum(1 for line in lines if CODE_LINE.match(line) or ASM_LINE.match(line))
    if len(lines) == 1:
        return bool(CODE_START.match(lines[0])) and hits == 1
    return hits >= max(2, int(len(lines) * 0.6))


#: 汇编/RTL 清单的特征。不能只看"字母被拆成 ``\\mathrm{L D R}``"——解析器给很多
#: 正常公式（``(H i,L o)=…``、``H i = R s R t;``）也是这么写的。
ASM_HINT = re.compile(r"//|\bassign\b|\bendcase\b|\bcasez?\b|1'b[01x]|0x[0-9a-fA-F]")


def is_asm_math(latex: str) -> bool:
    """``$$…$$`` 里其实是汇编/RTL 清单，硬转 MathML 会出一串散开的字母。"""
    if ASM_HINT.search(latex):
        return True
    # ``lui r1, #5918 ; r1 = #59180000`` 这种：带 ``;`` 又有 ``\#``
    return ";" in latex and bool(re.search(r"\\#|\\\\", latex))


def latex_plain(latex: str) -> str:
    """把 parser 塞进公式的汇编清单还原成可读文本。"""
    text = re.sub(r"\\begin\s*\{(?:array|aligned|alignedat|matrix|tabular)\}\s*\{[^{}]*\}", "", latex)
    text = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", "", text)
    text = text.replace("\\\\", "\n").replace("\\ ", " ")  # LaTeX 换行 / 空格
    text = re.sub(r"\\(?:mathrm|text|textsf|mathbf|mathtt|mathit)\s*\{([^{}]*)\}", r"\1", text)
    text = text.replace(r"^{\prime}", "'").replace(r"\prime", "'")
    text = text.replace(r"\rightarrow", "→").replace(r"\leftarrow", "←")
    text = re.sub(r"\\(?:quad|qquad|thinspace|,|;|:|!)", " ", text)
    for char in "_&%#{}":
        text = text.replace("\\" + char, char)
    text = re.sub(r"\\[a-zA-Z]+", " ", text)
    text = text.replace("~", " ").replace("&", "").replace("{", "").replace("}", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def toc_title(title: str) -> str:
    """目录里的标题：去掉脚注上标（``$ ^{[1]} $``）这类标记，导航里不需要它们。"""
    text = re.sub(r"\$\s*[^$]*\$\s*", "", title)  # 行内公式/脚注整体去掉
    return re.sub(r"\s{2,}", " ", text).strip() or title


def heading_kind(title: str) -> int | None:
    """标题 → XHTML 级别。返回 None 表示这行其实是代码（例如 ``## BEQ LABEL1``）。"""
    if re.match(r"^[A-Z][A-Z0-9]{1,6}(\s|$)", title) and not re.search(r"[\u4e00-\u9fff]", title):
        return None  # 全大写助记符开头、又没中文：是汇编
    numbered = re.match(r"^(\d+(?:\.\d+)*)[\s.、]", title)
    if numbered:
        dots = numbered.group(1).count(".")
        if dots == 1:
            return 2
        if dots >= 2:
            return 3
        return 4  # ``1. 直接映射`` 这种层内序号
    return 4


# ---------------------------------------------------------------------------
# 正文渲染
# ---------------------------------------------------------------------------


def render_blocks(blocks: list[str], *, prefix: str, audit: list[str]) -> tuple[list[str], list[dict]]:
    """返回 (body 行, 目录条目)。prefix 用于生成锚点 id。"""
    out: list[str] = []
    toc: list[dict] = []
    counter = 0
    index = 0
    while index < len(blocks):
        block = blocks[index]
        index += 1
        counter += 1
        anchor = f"{prefix}-{counter}"

        # 独立公式
        if block.startswith("$$") and block.endswith("$$"):
            latex = block[2:-2].strip()
            if is_asm_math(latex):
                audit.append(f"[公式实为汇编清单] {latex_plain(latex)[:64]}  ⟵ 原文 {latex[:64]}")
            out.append(render_display(latex))
            continue

        # 表格
        if block.startswith("<table"):
            out.append(render_table(block))
            continue

        # 图片 div（可能紧跟图注）
        img = IMG_DIV.match(block)
        if img and block.startswith("<div"):
            caption = ""
            if index < len(blocks) and FIG_CAPTION.match(blocks[index]) and len(blocks[index]) < 120:
                caption = blocks[index].strip()
                index += 1
            out.append(render_figure(img.group(1), caption))
            continue

        # 居中的图注 / 表注 / 子图标注
        div = CAPTION_DIV.match(block)
        if div and block.startswith("<div"):
            text = div.group(1).strip()
            if FIG_CAPTION.match(text) or TAB_CAPTION.match(text) or SUBCAPTION.match(text):
                out.append(f'  <p class="caption">{inline(text)}</p>')
            elif text:
                out.append(f'  <p class="caption">{inline(text)}</p>')
            continue

        # 标题
        head = HEAD.match(block) if "\n" not in block else None
        if head:
            title = head.group(2)
            level = heading_kind(title)
            if level is None:
                out.append(f'  <pre class="code">{escape(title)}</pre>')
                continue
            out.append(f'  <h{level} id="{anchor}">{inline(title)}</h{level}>')
            toc.append({"title": toc_title(title), "anchor": anchor, "level": level})
            continue

        # 代码块：不转数学（代码里的 $ 不一定是公式），只转义
        if is_code(block):
            joined = "\n".join(escape(line) for line in block.splitlines())
            out.append(f'  <pre class="code">{joined}</pre>')
            continue

        # 列表（项目符号）
        bullet = [line for line in block.splitlines() if re.match(r"^[•·\-]\s+", line)]
        if bullet and len(bullet) == len([l for l in block.splitlines() if l.strip()]):
            out.append("  <ul>")
            for line in bullet:
                out.append(f'    <li>{inline(re.sub(r"^[•·-]\s+", "", line))}</li>')
            out.append("  </ul>")
            continue

        # 普通段落（特例：版权页一行一条的出版信息、参考文献条目）
        lines = [inline(line) for line in block.splitlines()]
        body = "<br/>".join(lines)
        if re.match(r"^\[\d+\]\s", block):
            out.append(f'  <p class="reference">{body}</p>')
        elif len(block) < 60 and re.match(r"^[\u4e00-\u9fff]{2,8}[：:]", block):
            out.append(f'  <p class="copyright">{body}</p>')
        else:
            out.append(f'  <p>{body}</p>')

    return out, toc


def split_blocks(text: str) -> list[str]:
    return [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="按 plan 把 source.md 写成章节 XHTML")
    parser.add_argument("--run", required=True, help="运行目录，例如 .pdf2epub/runs/<id>")
    parser.add_argument("--plan", help="默认 <build>/compose-plan.json")
    parser.add_argument("--audit", action="store_true", help="只打印审计报告，不写文件")
    parser.add_argument("--only", help="只写这一个章节文件（文件名或 stem）")
    args = parser.parse_args()
    try:  # 控制台可能是 GBK，公式里带 ® 之类的字符会直接崩
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    run = Path(args.run)
    build = run / "build"
    plan_path = Path(args.plan) if args.plan else build / "compose-plan.json"
    source = run / "calibrate" / "source.md"
    if not plan_path.is_file():
        print(f"找不到撰写计划：{plan_path}", file=sys.stderr)
        return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    lines = source.read_text(encoding="utf-8").splitlines()

    text_dir = build / "OEBPS" / "text"
    text_dir.mkdir(parents=True, exist_ok=True)

    audit: list[str] = []
    spine: list[str] = []
    toc: list[dict] = []
    report: list[str] = []

    for part in plan["parts"]:
        name = part["file"]
        if args.only and args.only not in (name, Path(name).stem):
            continue
        body_lines = lines[part["start"] - 1 : part["end"]]
        title = part["title"]
        kind = part.get("kind", "chapter")
        prefix = Path(name).stem
        blocks = split_blocks("\n".join(body_lines))

        # 页面自己的标题从正文里摘掉，避免重复（原书章首标题有的带"第N章"、有的不带，
        # 第 11 章还被拆成了两行，都在这里吃掉）
        number = part.get("number")
        while blocks:
            first = HEAD.match(blocks[0])
            if not first:
                break
            text = first.group(2).strip()
            if text == title or (number and text == f"第{number}章 {title}") or re.fullmatch(r"第\s*\d+\s*章", text):
                blocks = blocks[1:]
                continue
            break

        if kind == "title":
            out = ['  <div class="titlepage">']
            for block in blocks:
                img = IMG_DIV.match(block)
                if img:
                    out.append("    " + fix_img(img.group(1)).strip())
                elif re.match(r"^#", block):
                    continue
                else:
                    for line in block.splitlines():
                        if line.strip():
                            out.append(f'    <p class="title-line">{inline(line.strip())}</p>')
            out.append("  </div>")
            toc_entries: list[dict] = []
        elif kind == "toc":
            out = [f"  <h1>{escape(title)}</h1>", '  <div class="toc-print">']
            for block in blocks:
                if HEAD.match(block):
                    continue
                for line in block.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    match = TOC_LINE.match(line)
                    if match and match.group(1).strip():
                        title_text = inline(match.group(1).strip())
                        cls = "toc-item toc-chapter" if re.match(r"^第\s*\d+\s*章", line) else "toc-item"
                        out.append(
                            f'    <p class="{cls}">{title_text} '
                            f'<span class="toc-page">{match.group(2)}</span></p>'
                        )
                    else:
                        out.append(f'    <p class="toc-item">{inline(line)}</p>')
            out.append("  </div>")
            toc_entries = []
        else:
            heading = f"第{part['number']}章 {title}" if part.get("number") else title
            out = [f'  <h1 id="{prefix}-h1">{escape(heading)}</h1>']
            body, toc_entries = render_blocks(blocks, prefix=prefix, audit=audit)
            out.extend(body)
            toc_entries = [{"title": toc_title(heading), "anchor": f"{prefix}-h1", "level": 1}] + toc_entries

        level = part.get("level", 1)
        for entry in toc_entries:
            entry["level"] = level + entry["level"] - 1 if entry["level"] > 1 else level
            entry["href"] = f"text/{name}#{entry.pop('anchor')}"
            if entry["title"] and entry["href"]:
                toc.append(entry)

        report.append(f"  {name:<22} {title:<34} {sum(len(x) for x in out):>7} 字符  "
                      f"{len(toc_entries)} 个目录项")
        if not args.audit:
            (text_dir / name).write_text(
                PAGE.format(title=escape(heading if kind == "chapter" else title),
                            body="\n".join(out), lang=plan.get("language", "zh")),
                encoding="utf-8",
            )
        spine.append(f"text/{name}")

    print(f"{'审计' if args.audit else '已写出'} {len(report)} 个页面：")
    print("\n".join(report))
    if audit:
        print("\n== 需要人工看的块 ==")
        for line in audit:
            print("  ·", line)

    if args.audit or args.only:
        return 0

    book = {
        "title": plan["title"],
        "author": plan["author"],
        "language": plan.get("language", "zh"),
        "publisher": plan.get("publisher", ""),
        "description": plan.get("description", ""),
        "identifier": "",
        "cover": plan.get("cover", ""),
        "spine": spine,
        "toc": toc,
    }
    (build / "book.json").write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nbook.json 已写好：{len(spine)} 页 / {len(toc)} 个目录项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

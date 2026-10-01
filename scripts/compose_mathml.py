#!/usr/bin/env python
"""把 ``calibrate/source.md`` 写成 EPUB 的章节 XHTML（公式转 MathML）。

这是**示例脚本，不是流水线的一部分**。``pdf2epub run`` 在 compose 阶段停下来
等 LLM 动手，这个脚本只是把其中机械的部分（转义、包装、MathML 转换）固化下来，
免得每次手敲几万字。章节怎么切、标题叫什么、元数据填什么，仍然由调用者决定，
也应该由调用者复核。

用法::

    python scripts/compose_mathml.py --run .pdf2epub/runs/<run_id> \
        --title "书名" --author "作者"

需要 ``latex2mathml``：``pip install latex2mathml``。

为什么单独写这么个脚本、而不是塞进 ``src/pdf2epub/``：流水线的立场是"撰写是 LLM
的活"。把它变成流水线的一步，就等于承认"Markdown 转 XHTML"可以交给脚本，那正是
这个项目想避开的那类麻烦。放在 ``scripts/`` 里，它就是个顺手的工具，用不用、
改不改都由你。

里面有三处是踩过坑才加上的，删掉会重新踩：

1. ``sanitize()``：``\\binom{a}{b}^\\top`` 会让 latex2mathml 产出**非法** MathML
   （``<msup>`` 里塞四个子元素）。XML 良构但不是合法 MathML，只有跑 EPUBCheck
   才会暴露。
2. ``bad_structure()``：转换后自查 MathML 结构，不合法就退回 LaTeX 原文。
   宁可显示得丑，也不要产出"看起来对、校验不过"的产物。
3. ``_escape_bare_amp()``：裸 ``&`` 会让整份 XHTML 变成非良构。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import uuid

try:
    from lxml import etree
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("需要 lxml：pip install lxml")

try:
    import latex2mathml.converter as l2m
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("需要 latex2mathml：pip install latex2mathml")


MATHML_NS = "http://www.w3.org/1998/Math/MathML"
HTML_TAGS = re.compile(r"</?(?:sup|sub|em|strong|br)\s*/?>")
BARE_AMP = re.compile(r"&(?!#?\w+;)")
#: 只允许固定个数子元素的 MathML 结构
FIXED_ARITY = {"msup", "msub", "mfrac", "mroot", "munder", "mover", "munderover"}

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


# ---------------------------------------------------------------------------
# 转义与行内标记
# ---------------------------------------------------------------------------


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _protect_tags(text: str) -> tuple[str, list[str]]:
    """把 <sup> 这类允许的行内标签先抠出来，避免被当成文本转义。"""
    stash: list[str] = []

    def take(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"\x00{len(stash) - 1}\x00"

    return HTML_TAGS.sub(take, text), stash


def _restore_tags(text: str, stash: list[str]) -> str:
    for index, tag in enumerate(stash):
        text = text.replace(f"\x00{index}\x00", tag)
    return text


# ---------------------------------------------------------------------------
# LaTeX -> MathML
# ---------------------------------------------------------------------------


def _skip_group(text: str, start: int) -> int:
    """从 text[start]（必须是 `{`）跳到配对的 `}` 之后。"""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def sanitize(latex: str) -> str:
    r"""给 ``\binom{a}{b}`` 这类"多记号基"套一层花括号。

    不套的话，它后面的 ``^\top`` 会让 latex2mathml 把上标挂到一个残缺的基上，
    产出 ``<msup>`` 里塞四个子元素的非法 MathML。

    参数里常有嵌套花括号（``\binom{\mathbf{u}}{\mathbf{J}_{m}\mathbf{u}}``），
    所以得按括号配对扫，正则匹配不了。
    """
    out: list[str] = []
    index = 0
    while True:
        found = latex.find("\\binom", index)
        if found < 0:
            out.append(latex[index:])
            return "".join(out)

        cursor = found + len("\\binom")
        groups = 0
        while groups < 2:
            while cursor < len(latex) and latex[cursor].isspace():
                cursor += 1
            if cursor >= len(latex) or latex[cursor] != "{":
                break
            cursor = _skip_group(latex, cursor)
            groups += 1

        tail = cursor
        while tail < len(latex) and latex[tail].isspace():
            tail += 1
        needs_group = groups == 2 and tail < len(latex) and latex[tail] in "^_"

        out.append(latex[index:found])
        body = latex[found:cursor]
        out.append(f"{{{body}}}" if needs_group else body)
        index = cursor


def bad_structure(xml: str) -> str:
    """检查 MathML 结构合法性，返回问题描述（没问题返回空串）。"""
    try:
        root = etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        return f"XML 不合法：{exc}"
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        local = etree.QName(element).localname
        if local in FIXED_ARITY:
            kids = [c for c in element if isinstance(c.tag, str)]
            if len(kids) != 2:
                return f"<{local}> 有 {len(kids)} 个子元素，应该是 2 个"
    return ""


def _escape_bare_amp(xml: str) -> str:
    if BARE_AMP.search(xml):
        print("    ! MathML 里有裸 &，已转义")
        return BARE_AMP.sub("&amp;", xml)
    return xml


def mathml(latex: str, *, display: str) -> str:
    source = sanitize(latex.strip())
    try:
        out = l2m.convert(source, display=display)
    except Exception as exc:  # 转不动就退回原文，至少内容不丢
        print(f"    ! MathML 转换失败（保留 LaTeX）：{type(exc).__name__}: {source[:60]}")
        return f'<code class="math-raw">{escape(source)}</code>'

    problem = bad_structure(out)
    if problem:
        try:
            retry = l2m.convert(f"{{{source}}}", display=display)
            if not bad_structure(retry):
                print(f"    · 加了分组括号后转好：{source[:56]}")
                return _escape_bare_amp(retry)
        except Exception:
            pass
        print(f"    ! MathML 结构不合法（{problem}），保留 LaTeX：{source[:56]}")
        return f'<code class="math-raw">{escape(source)}</code>'

    return _escape_bare_amp(out)


# ---------------------------------------------------------------------------
# Markdown -> XHTML
# ---------------------------------------------------------------------------


def render_inline(text: str) -> str:
    """行内：$...$ -> MathML，其余转义（允许的 HTML 标签除外）。"""
    text, stash = _protect_tags(text)
    parts = text.split("$")
    if len(parts) % 2 == 0:  # \( 的个数是奇数，说明有没配对的
        return escape(_restore_tags(text, stash))
    out: list[str] = []
    for index, part in enumerate(parts):
        out.append(mathml(part, display="inline") if index % 2 else escape(part))
    return _restore_tags("".join(out), stash)


def render_display(latex: str) -> str:
    """独立公式：抽出 \\tag{n}，编号单独右对齐。"""
    number = ""
    match = re.search(r"\\tag\s*\{\s*(\w+)\s*\}", latex)
    if match:
        number = match.group(1)
        latex = latex[: match.start()] + latex[match.end() :]
    body = mathml(latex, display="block")
    tag = f'\n    <span class="eqno">（{number}）</span>' if number else ""
    return f'  <div class="math-block">\n    {body}{tag}\n  </div>'


def render_toc_line(line: str) -> str:
    match = re.match(r"^(.*?)\s+(\d+)$", line)
    if not match:
        return f'  <p class="toc-item">{render_inline(line)}</p>'
    return (
        f'  <p class="toc-item"><span>{render_inline(match.group(1))}</span>'
        f'<span class="toc-page">{match.group(2)}</span></p>'
    )


def heading_level(title: str) -> int:
    """`2.1` -> h2，`5.2.1` -> h3；一级章标题由文件承载，用 h1。"""
    match = re.match(r"^(\d+(?:\.\d+)*)\b", title.strip())
    return len(match.group(1).split(".")) if match else 1


def render_body(markdown: str, *, is_toc: bool) -> str:
    blocks = re.split(r"\n\s*\n", markdown)
    out: list[str] = []
    list_open: str | None = None

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append(f"  </{list_open}>")
            list_open = None

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        if block.startswith("$$") and block.endswith("$$"):
            close_list()
            out.append(render_display(block[2:-2]))
            continue

        if block.startswith("## "):
            close_list()
            title = block[3:].strip()
            level = heading_level(title)
            out.append(f"  <h{level}>{render_inline(title)}</h{level}>")
            continue

        if is_toc:
            close_list()
            for line in block.splitlines():
                if line.strip():
                    out.append(render_toc_line(line.strip()))
            continue

        bullet = re.match(r"^[•\-\*]\s+(.*)$", block)
        numbered = re.match(r"^(\d+)[.、)]\s+(.*)$", block)
        if bullet or numbered:
            wanted = "ul" if bullet else "ol"
            if list_open != wanted:
                close_list()
                out.append(f"  <{wanted}>")
                list_open = wanted
            item = bullet.group(1) if bullet else numbered.group(2)
            out.append(f"    <li>{render_inline(item)}</li>")
            continue

        close_list()
        lines = [render_inline(line) for line in block.splitlines()]
        out.append(f'  <p>{"<br/>".join(lines)}</p>')

    close_list()
    return "\n".join(out)


#: 一级章节：`## 摘要` / `## 目录` / `## 参考文献` / `## 3 标题`（数字后**不带点**）。
#: MinerU 把一级章和二级小节都输出成 `##`，靠"编号里有没有点"区分。
TOP_LEVEL = re.compile(r"^##\s+(.+?)\s*$")
NUMBERED_SECTION = re.compile(r"^\d+(\.\d+)*\s+\S")


def split_chapters(lines: list[str]) -> list[tuple[int, str]]:
    chapters: list[tuple[int, str]] = []
    for number, line in enumerate(lines):
        match = TOP_LEVEL.match(line)
        if not match:
            continue
        title = match.group(1)
        if NUMBERED_SECTION.match(title) and "." in title.split()[0]:
            continue  # 二级小节，不是章
        chapters.append((number, title))
    return chapters


def main() -> int:
    parser = argparse.ArgumentParser(description="把 source.md 写成 EPUB 章节（公式转 MathML）")
    parser.add_argument("--run", required=True, help="运行目录，例如 .pdf2epub/runs/<id>")
    parser.add_argument("--source", help="默认 <run>/calibrate/source.md")
    parser.add_argument("--build", help="默认 <run>/build")
    parser.add_argument("--title", default="", help="书名（默认取 source.md 第一行）")
    parser.add_argument("--author", default="")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()

    run = pathlib.Path(args.run)
    if not run.is_dir():
        print(f"找不到运行目录：{run}", file=sys.stderr)
        return 1
    source = pathlib.Path(args.source) if args.source else run / "calibrate" / "source.md"
    build = pathlib.Path(args.build) if args.build else run / "build"
    if not source.is_file():
        print(f"找不到工作稿：{source}", file=sys.stderr)
        return 1

    lines = source.read_text(encoding="utf-8").splitlines()
    chapters = split_chapters(lines)
    if not chapters:
        print("没找到任何一级章节，检查 `## ` 标题", file=sys.stderr)
        return 1

    title = args.title or (lines[0].lstrip("# ").strip() if lines else "Untitled")
    text_dir = build / "OEBPS" / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    for stale in text_dir.glob("*.xhtml"):
        stale.unlink()

    spine: list[str] = []
    toc: list[dict] = []
    print(f"切出 {len(chapters)} 个一级章节：")
    for index, (start, chapter_title) in enumerate(chapters):
        stop = chapters[index + 1][0] if index + 1 < len(chapters) else len(lines)
        body = render_body(
            "\n".join(lines[start + 1 : stop]).strip(),
            is_toc=chapter_title.strip() == "目录",
        )
        name = f"ch{index + 1:03d}.xhtml"
        (text_dir / name).write_text(
            PAGE.format(title=escape(chapter_title), body=body, lang=args.language),
            encoding="utf-8",
        )
        spine.append(f"text/{name}")
        toc.append({"title": chapter_title, "href": f"text/{name}", "level": 1})
        print(f"  {name}  {chapter_title:<30} {len(body):>7} 字符")

    identifier = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'pdf2epub/{title}')}"
    book = {
        "title": title,
        "author": args.author,
        "language": args.language,
        "publisher": "",
        "description": "",
        "identifier": identifier,
        "cover": "",
        "spine": spine,
        "toc": toc,
    }
    (build / "book.json").write_text(
        json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\nbook.json 已写好（{len(spine)} 章）")
    print("接着复核章节切分与标题，然后跑：pdf2epub run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

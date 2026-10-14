#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 ``build/compose-plan.json`` 撰写**代码密集型**技术书（终端会话、Verilog、脚本）。

与 ``compose_book.py`` 的分工：那个脚本判定代码块靠 ``is_code()``——只认 RTL 关键
字，且要求整块 60% 以上命中。技术手册里大量"终端会话 + 输出"的清单（``# bsc -u
-sim DUT.bsv`` 后面跟一串 ``... created: xxx``）一条都不命中，于是整段代码被渲染成
``<p>``：比例字体、2em 首行缩进，缩进层次全丢。实测这本书 60 多处清单全中招。

这里改用**行级分类 + 成段合并**，再交给 ``compose_book`` 的渲染函数干活：

1. 每行先判类型：code / bullet / heading / html / math / prose；
2. 相邻的 code 行（中间允许一个空行）并成一个 ``<pre class="code">``；
3. 相邻的 bullet 行并成一个 ``<ul>``；
4. 印刷目录与索引单独一种 ``kind``：条目右对齐页码，缩进条目用 ``toc-sub``；
5. 行内 ``$…$`` 先保护 ``$标识符``（Verilog 的 ``$display`` / ``$D_IN`` /
   ``$test$plusargs``），否则它们会被当成公式之间的一段普通文字转成 MathML。

用法::

    python scripts/compose_codebook.py --run <run_id>             # 写章节 + book.json
    python scripts/compose_codebook.py --run <run_id> --audit     # 只出审计
    python scripts/compose_codebook.py --run <run_id> --code-map  # 看代码块判定，逐段复核
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
try:
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

import compose_book  # noqa: E402
from compose_book import (  # noqa: E402
    HEAD,
    IMG_DIV,
    MD_IMG,
    PAGE,
    escape,
    fix_img,
    heading_kind,
    render_blocks,
    render_display,
    render_figure,
    toc_title,
)

# --------------------------------------------------------------------------
# 行级分类
# --------------------------------------------------------------------------

#: 一眼就是代码/终端会话的行
STRONG = [
    re.compile(r"^#\s+\S"),                       # shell 注释/提示符
    re.compile(r"^%\s*\S"),                       # Bluesim 交互会话
    re.compile(r"^\$\s+\S"),                      # shell 提示符
    re.compile(r"^(?:bsc|c\+\+|cc|gc?make|make|setenv|export|exec|package\s+require|namespace\s+import|bluetcl|bsim|sim)\b"),
    re.compile(r"^\(\*"),                         # BSV 属性
    re.compile(r"^`[a-zA-Z]"),                    # Verilog 预处理指令
    re.compile(r"^//"),                           # Verilog 注释
    re.compile(r"^(?:assign|wire|reg|input|output|module|endmodule|always|initial|begin|end|else|endcase|casez?|parameter|localparam|function|endfunction|task|endtask|interface|endinterface|typedef|import|rule|endrule|method|endmethod|actionvalue|endactionvalue|instance|endinstance|typeclass|endtypeclass|deriving|return|let|noinline|match|endmatch)\b"),
    re.compile(r"^(?:if|else|for|while|switch|case)\s*[\(\{]"),
    re.compile(r"^\{"),                           # {a b c} 结构
    re.compile(r"^\}"),                           # 闭合
    re.compile(r"^[A-Za-z_][\w$\[\]\.]*\s*(?:<=|=|->|<-)\s*\S"),  # 赋值 / 端口连接
]

#: 强代码行的补充：行尾分号、纯标识符、工具输出行
MEDIUM = [
    re.compile(r";\s*$"),
    re.compile(r"^[A-Za-z_][\w$.{}\[\]<>:/+-]*$"),
    re.compile(r"^[A-Za-z][\w$]*\s*\([^)]*\)\s*[;.]?$"),
    re.compile(r"^[\w.]+\.(?:bsv|ba|bo|v|vcd|so|o|h|c|cxx|cpp|exe|tcl|mk|dot)\b"),
    re.compile(r"^(?:File|Simulation|Bluesim|SystemC|Verilog|Foreign|User|Elaborated|code generation)\b.*", re.I),
    re.compile(r"^[A-Za-z_][\w$]*\s+\S+\s+[A-Za-z_][\w$]*\s*$"),
]

#: 只要出现就算代码/工具输出
CONTAINS = [
    re.compile(r"^[\w./-]+\.(?:bsv|ba|bo|v|vcd|so|o|h|c|cxx|cpp|tcl)\b"),
    re.compile(r"\bexec:\s"),
    re.compile(r"\b[A-Z][A-Za-z]+ (?:object|file|module|library) created:"),
    re.compile(r"#\("),                                   # mkFIFO #(参数)
    re.compile(r"<-"),                                    # BSV 绑定
    re.compile(r"::\s*\*"),                               # import FIFO::*
    re.compile(r"^\s*\.[A-Za-z_]\w*\s*\("),                # Verilog 端口连接 .CLK(...)
]

#: 印刷目录/索引里"条目标题 + 右对齐页码"：页码可能是 "12, 50" 或 "1-1"（章-页）
TOC_TAIL = re.compile(r"^(.*?)[\s.．]*(\d+(?:[-–]\d+)?(?:\s*,\s*\d+(?:[-–]\d+)?)*)\s*$")

#: 项目符号（连字符后必须有空格，免得把 "-D" 这种 flag 也当条目）
BULLET = re.compile(r"^(?:[•·]\s*|-\s+|☐\s*|□\s*)")

PROSE_END = re.compile(r"[.:;)\]]$")
WORD = re.compile(r"[A-Za-z]{2,}")
PROSE_HINT = re.compile(
    r"\b(?:the|this|that|these|those|is|are|was|were|be|been|will|would|can|could|should|"
    r"may|might|must|you|your|we|our|it|its|if|when|then|than|which|where|while|for|with|"
    r"from|into|about|there|have|has|had|do|does|did|not|but|and|or|so|as|at|by|in|of|on|to)\b",
    re.I,
)

#: Verilog 系统任务/系统变量与公式约定冲突，先保护起来
SYSVAR = re.compile(r"\$\w+")


_BASE_INLINE = compose_book.inline


def inline(text: str) -> str:
    guarded = SYSVAR.sub(lambda m: m.group(0).replace("$", compose_book.REG), text)
    return _BASE_INLINE(guarded)


compose_book.inline = inline                # 表格单元格、图注、目录一并跟着走


def prose_score(line: str) -> int:
    words = WORD.findall(line)
    if len(words) < 8:
        return 0
    hits = len(PROSE_HINT.findall(line))
    if hits >= 4 and PROSE_END.search(line):
        return 2
    if hits >= 6:
        return 1
    return 0


COMMAND_HEADING = re.compile(
    r"^(?:bsc|c\+\+|cc|make|setenv|export|exec|package\s+require|namespace\s+import|"
    r"sim|bsim|bluetcl|#\s|\.\.\.|--?\w+\b)"
)


def is_command_heading(title: str) -> bool:
    if COMMAND_HEADING.match(title):
        return True
    return bool(re.match(r"^[a-z][\w-]*\s+.*\s-\w", title)) and not PROSE_HINT.search(title)


def classify_line(line: str) -> str:
    s = line.strip()
    if not s:
        return "blank"
    if s.startswith("<table") or s.startswith("<div") or s.startswith("</div"):
        return "html"
    if MD_IMG.match(s):
        return "html"
    if s.startswith("$$") or s.endswith("$$"):
        return "math"
    if BULLET.match(s):
        return "bullet"
    if HEAD.match(s):
        title = HEAD.match(s).group(2)          # type: ignore[union-attr]
        return "code" if is_command_heading(title) else "heading"
    if prose_score(s) >= 2:
        return "prose"
    for pattern in STRONG:
        if pattern.match(s):
            return "code"
    if prose_score(s) >= 1:
        return "prose"
    for pattern in CONTAINS:
        if pattern.search(s):
            return "code"
    for pattern in MEDIUM:
        # search 而非 match：`;` 结尾这类**后缀**规则锚在行尾
        if pattern.search(s):
            if pattern.pattern.startswith("^[A-Za-z_][\\w$.{}\\[\\]<>:/+-]*$"):
                # 单 token 要够长、带技术特征，否则 "and"、"Bluesim" 也会算代码
                if len(s) < 8 or not any(ch in s for ch in "_./$("):
                    continue
            return "code"
    return "prose"


def is_command_line(s: str) -> bool:
    return bool(
        re.match(r"^(?:[#$%]\s+)?(?:bsc|c\+\+|cc|make|setenv|export|exec|package|namespace|sim|bsim|bluetcl)\b", s)
    )


def is_definitely_code(s: str) -> bool:
    return (
        is_command_line(s)
        or any(p.search(s) for p in CONTAINS)
        or any(p.match(s) for p in STRONG)
    )


# --------------------------------------------------------------------------
# 成块
# --------------------------------------------------------------------------


def _extend_same(lines_kinds: list[str], start: int, end: int, kind: str, allow_blank: bool):
    """从 start 起收集同类行；allow_blank 时允许夹一个空行。"""
    j = start
    k = start
    while k < end:
        if lines_kinds[k] == kind:
            j = k + 1
            k += 1
            continue
        if allow_blank and lines_kinds[k] == "blank":
            nxt = k + 1
            while nxt < end and lines_kinds[nxt] == "blank":
                nxt += 1
            if nxt < end and lines_kinds[nxt] == kind:
                k = nxt
                continue
        break
    return j


def group_blocks(lines: list[str], kinds: list[str], start: int, end: int) -> list[tuple[int, int, str]]:
    """把 [start, end) 切成块，返回 (起行, 止行, 类型)。行号 0 基、左闭右开。"""
    blocks: list[tuple[int, int, str]] = []
    i = start
    while i < end:
        kind = kinds[i]
        if kind == "blank":
            i += 1
            continue
        if kind == "code":
            j = _extend_same(kinds, i, end, "code", allow_blank=True)
            # 只有一行、又没有明确技术特征的（"and" 这种），退回普通段落
            if j - i == 1 and not is_definitely_code(lines[i].strip()):
                blocks.append((i, j, "prose"))
            else:
                blocks.append((i, j, "code"))
            i = j
            continue
        if kind == "bullet":
            j = _extend_same(kinds, i, end, "bullet", allow_blank=True)
            blocks.append((i, j, "bullets"))
            i = j
            continue
        if kind in ("heading", "math", "html"):
            blocks.append((i, i + 1, kind))
            i += 1
            continue
        j = i
        while j < end and kinds[j] == kind:
            j += 1
        blocks.append((i, j, kind))
        i = j
    return blocks


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def render_code(lines: list[str]) -> str:
    out = [escape(line).replace("\\_", "_") for line in lines if line.strip()]
    return '  <pre class="code">' + "\n".join(out) + "</pre>"


def render_part(lines: list[str], kinds: list[str], start: int, end: int, *, prefix: str, audit: list[str]):
    out: list[str] = []
    toc: list[dict] = []
    counter = 0
    grouped = group_blocks(lines, kinds, start, end)
    index = 0
    while index < len(grouped):
        b_start, b_end, kind = grouped[index]
        index += 1
        counter += 1
        anchor = f"{prefix}-{counter}"
        raw_lines = lines[b_start:b_end]
        text = "\n".join(raw_lines).strip()

        if kind == "code":
            out.append(render_code(raw_lines))
            continue

        if kind == "html":
            img = IMG_DIV.match(text)
            if img:
                caption = ""
                if index < len(grouped):
                    nxt_start, nxt_end, _ = grouped[index]
                    nxt = "\n".join(lines[nxt_start:nxt_end]).strip()
                    if re.match(r"^<div[^>]*>(?:Figure|Fig|Table|图|表)", nxt) and len(nxt) < 200:
                        caption = re.sub(r"^<div[^>]*>|</div>$", "", nxt).strip()
                        index += 1
                out.append(render_figure(img.group(1), caption))
                continue
            rest, sub_toc = render_blocks([text], prefix=prefix, audit=audit)
            # 解析器给每个单元格塞了 text-align: center，长说明居中对齐很难读
            rest = [line.replace("text-align: center; ", "") for line in rest]
            out.extend(rest)
            toc.extend(sub_toc)
            continue

        if kind == "math":
            out.append(render_display(text.strip("$").strip()))
            continue

        if kind == "bullets":
            out.append("  <ul>")
            for line in raw_lines:
                if line.strip():
                    out.append(f'    <li>{inline(BULLET.sub("", line.strip()))}</li>')
            out.append("  </ul>")
            continue

        if kind == "heading":
            head = HEAD.match(text)
            if head is None:
                out.append(render_code(raw_lines))
                continue
            title = head.group(2)
            level = heading_kind(title)
            if level is None:
                out.append(render_code(raw_lines))
                continue
            out.append(f'  <h{level} id="{anchor}">{inline(title)}</h{level}>')
            toc.append({"title": toc_title(title), "anchor": anchor, "level": level})
            continue

        body_lines = [ln for ln in raw_lines if ln.strip()]
        joined = "<br/>".join(inline(ln) for ln in body_lines)
        out.append(f"  <p>{joined}</p>")
    return out, toc


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="代码密集型技术书的撰写脚本")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--plan", help="撰写计划（缺省 <build>/compose-plan.json）")
    parser.add_argument("--source", help="要撰写的校准稿（缺省该 run 的 calibrate/source.md）")
    parser.add_argument("--audit", action="store_true", help="只出审计，不写文件")
    parser.add_argument("--code-map", action="store_true", help="打印代码块判定结果")
    parser.add_argument("--only", help="只写这一个章节")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    build = run.build_dir
    plan_path = Path(args.plan) if args.plan else build / "compose-plan.json"
    if not plan_path.is_file():
        print(f"找不到撰写计划：{plan_path}", file=sys.stderr)
        return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    src_path = Path(args.source) if args.source else run.calibrate_dir / "source.md"
    if not src_path.is_file():
        print(f"找不到校准稿：{src_path}", file=sys.stderr)
        return 1
    src_lines = src_path.read_text(encoding="utf-8").splitlines()
    kinds = [classify_line(line) for line in src_lines]

    if args.code_map:
        for b_start, b_end, kind in group_blocks(src_lines, kinds, 0, len(src_lines)):
            if kind == "code":
                print(f"{b_start + 1:6d}-{b_end:6d}  {b_end - b_start:3d} 行  {src_lines[b_start].strip()[:80]}")
                continue
            if kind == "bullets":
                print(f"{b_start + 1:6d}-{b_end:6d}  {b_end - b_start:3d} 行  [列表] {src_lines[b_start].strip()[:70]}")
        return 0

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
        start, end = part["start"] - 1, part["end"]
        title = part["title"]
        kind = part.get("kind", "chapter")
        prefix = Path(name).stem

        # 页面自己的标题从正文里摘掉
        number = part.get("number")
        labels = {title, *(part.get("strip") or [])}
        if number:
            labels.add(f"第{number}章 {title}")
        stripped_first = False
        while start < end:
            head = HEAD.match(src_lines[start].strip())
            if head:
                text = head.group(2).strip()
                if kind == "chapter" and not stripped_first:
                    # 计划里的 title 是权威——校准稿被翻译过后，正文标题对不上原文
                    stripped_first = True
                    start += 1
                    continue
                if text in labels or re.fullmatch(r"第\s*\d+\s*章", text):
                    start += 1
                    continue
            if not src_lines[start].strip():
                start += 1
                continue
            break

        heading = f"第{part['number']}章 {title}" if part.get("number") else title
        if kind == "title":
            out = ['  <div class="titlepage">']
            for line in src_lines[start:end]:
                s = line.strip()
                if not s or HEAD.match(s):
                    continue
                img = IMG_DIV.match(s)
                if img:
                    out.append("    " + fix_img(img.group(1)).strip())
                    continue
                out.append(f'    <p class="title-line">{inline(s)}</p>')
            out.append("  </div>")
            toc_entries: list[dict] = []
        elif kind in ("toc", "index"):
            out = [f'  <h1 id="{prefix}-h1">{inline(title)}</h1>', '  <div class="toc-print">']
            for line in src_lines[start:end]:
                if not line.strip():
                    continue
                sub = " toc-sub" if line[:1] in (" ", "\t") else ""
                s = re.sub(r"^#{1,4}\s+", "", line.strip())
                match = TOC_TAIL.match(s)
                if match and match.group(1).strip():
                    out.append(
                        f'    <p class="toc-item{sub}">{inline(match.group(1).strip())} '
                        f'<span class="toc-page">{match.group(2)}</span></p>'
                    )
                else:
                    out.append(f'    <p class="toc-item{sub}">{inline(s)}</p>')
            out.append("  </div>")
            toc_entries = [{"title": toc_title(title), "anchor": f"{prefix}-h1", "level": 1}]
        else:
            out = [f'  <h1 id="{prefix}-h1">{inline(heading)}</h1>']
            body, toc_entries = render_part(src_lines, kinds, start, end, prefix=prefix, audit=audit)
            out.extend(body)
            toc_entries = [{"title": toc_title(heading), "anchor": f"{prefix}-h1", "level": 1}] + toc_entries

        level = part.get("level", 1)
        for entry in toc_entries:
            entry["level"] = level + entry["level"] - 1 if entry["level"] > 1 else level
            entry["href"] = f"text/{name}#{entry.pop('anchor')}"
            if entry["title"] and entry["href"]:
                toc.append(entry)

        report.append(f"  {name:<22} {title:<46} {sum(len(x) for x in out):>7} 字符  {len(toc_entries)} 目录项")
        if not args.audit:
            (text_dir / name).write_text(
                PAGE.format(title=inline(heading if kind == "chapter" else title),
                            body="\n".join(out), lang=plan.get("language", "zh")),
                encoding="utf-8",
            )
        spine.append(f"text/{name}")

    print(f"{'审计' if args.audit else '已写出'} {len(report)} 个页面：")
    print("\n".join(report))

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

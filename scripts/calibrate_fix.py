#!/usr/bin/env python
"""校准阶段的**单一修复入口**：所有对 ``calibrate/source.md`` 的机械修改都由它执行。

为什么必须只有一个：（见 references/pitfalls.md 教训 8）多个脚本各自从原始产物重写
source.md 时，后执行的那个会把先执行的改动静默抹掉。所以把"补脚注、改错字、删复读
段落"都收敛到这个脚本，由一份 JSON 规格驱动，**一次读、一次写**，并把每条改动追加
到 ``calibrate/notes.md`` 留痕。

规格文件（默认 ``calibrate/fix-plan.json``）::

    {
      "page_appends": {            // 页尾补内容（脚注最常用）：插在该页最后一行之后
        "19": "<sup>1</sup> FPGAs and ASICs are certain kinds of silicon ..."
      },
      "replacements": [            // 定点替换：find 必须全文唯一，否则报错停下
        {"find": "…", "with": "…", "why": "…"}
      ],
      "regex_replacements": [      // 成批同形替换：必须写明 expect，命中数不符就报错
        {"pattern": "…", "with": "…", "expect": 29, "why": "…"}
      ],
      "drop_paragraphs": [         // 删复读段落：同文出现多次时删掉第 occurrence 次
        {"text": "…", "occurrence": 2, "why": "…"}
      ]
    }

每个动作都要求**定位唯一**：定位不到、或定位到多处，脚本直接报错退出、不做任何
写入——宁可停下来核对，也不要改错位置。

用法::

    python scripts/calibrate_fix.py --run <run_id> --plan calibrate/fix-plan.json
    python scripts/calibrate_fix.py --run <run_id> --plan ... --dry-run   # 只看会怎么改
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

PAGE_MARK = re.compile(r"<!--\s*=====\s*原书第\s*(\d+)\s*页（(\w+)）\s*=====\s*-->")


class FixError(SystemExit):
    pass


def split_pages(paged_md: str) -> dict[int, str]:
    pages: dict[int, str] = {}
    matches = list(PAGE_MARK.finditer(paged_md))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(paged_md)
        pages[int(m.group(1))] = paged_md[start:end]
    return pages


def page_tail(paged_md: str, page: int, text: str) -> str:
    """该页正文的结尾片段（保留原始换行），足够唯一才返回。

    页尾常常是 ``endrule`` 这种到处都有的行，而代码行之间是单换行、段落之间是
    空行——所以锚点必须**按原文的换行原样取**，不能自己拼接分隔符。从最后一行
    往前加行，直到全文唯一为止；加到头也不唯一就报错停下，宁可不改。
    """
    body = split_pages(paged_md).get(page)
    if body is None:
        raise FixError(f"带页码工作稿里没有第 {page} 页")
    lines = body.rstrip("\n").split("\n")
    last = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
    if last < 0:
        raise FixError(f"第 {page} 页在带页码工作稿里是空的，无法定锚点；改用 replacements 手工锚")
    for take in range(1, last + 2):
        anchor = "\n".join(lines[last - take + 1 : last + 1]).strip()
        if anchor and text.count(anchor) == 1:
            return anchor
    raise FixError(
        f"第 {page} 页尾锚点加了 {last + 1} 行仍不唯一：{lines[last][:60]!r}；"
        "改用 replacements 手工指定更长的锚"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="校准阶段的单一修复入口")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--plan", default=None, help="规格文件（缺省 calibrate/fix-plan.json）")
    parser.add_argument("--dry-run", action="store_true", help="只检查定位、不写盘")
    args = parser.parse_args()

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    plan_path = Path(args.plan) if args.plan else run.calibrate_dir / "fix-plan.json"
    if not plan_path.is_absolute():
        plan_path = (Path.cwd() / plan_path).resolve()
    if not plan_path.is_file():
        raise FixError(f"没有规格文件：{plan_path}")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    # 检测脚本（如 join_page_breaks.py）产出的是"片段计划"，这里按引用加载，
    # 保证仍然只有本脚本一个写入口。
    if plan.get("joins_file"):
        joins_path = Path(str(plan["joins_file"]))
        if not joins_path.is_absolute():
            joins_path = (plan_path.parent / joins_path).resolve()
        plan["page_joins"] = json.loads(joins_path.read_text(encoding="utf-8"))
    source_path = run.calibrate_dir / "source.md"
    paged_path = run.calibrate_dir / "source_paged.md"
    text = source_path.read_text(encoding="utf-8")
    paged = paged_path.read_text(encoding="utf-8") if paged_path.is_file() else ""
    log: list[str] = []

    # --- 1) 页尾补内容 -----------------------------------------------------
    appends = plan.get("page_appends") or {}
    applied_appends = 0
    for page_key in sorted(appends, key=lambda k: int(k), reverse=True):
        page = int(page_key)
        payload = str(appends[page_key]).strip()
        if not payload:
            continue
        if payload and payload.splitlines()[0].strip() and payload.splitlines()[0].strip() in text:
            log.append(f"- 第 {page} 页尾：已在文中，跳过")
            continue
        if not paged:
            raise FixError("缺少 source_paged.md，先跑 scripts/qc_scan.py")
        anchor = page_tail(paged, page, text)
        hits = text.count(anchor)
        if hits != 1:
            raise FixError(
                f"第 {page} 页锚点定位不唯一（命中 {hits} 次）：{anchor[:80]!r}；"
                "请改用 replacements 手工指定更长的锚"
            )
        index = text.index(anchor) + len(anchor)
        text = text[:index] + "\n\n" + payload + "\n" + text[index:]
        log.append(f"- 第 {page} 页尾补入 {len(payload)} 字：{payload[:60]}…")
        applied_appends += 1

    # --- 2) 页界断句合并 ---------------------------------------------------
    # 解析按页做，原书里跨过页脚的句子在产物里变成了两段。合并要**精确定位页界**：
    # 用左页尾行的原文（末尾若干字符）当锚，确认后面紧跟右页首行，才把中间的
    # 空行换成一个空格（左页以连字符结尾则连字符一起吃掉）。
    joins = plan.get("page_joins") or []
    applied_joins = 0
    for item in joins:
        left_tail = str(item.get("left_tail") or "").strip()
        right_head = str(item.get("right_head") or "").strip()
        sep = str(item.get("sep") or " ")
        left_page = item.get("left_page", "?")
        right_page = item.get("right_page", "?")
        if not left_tail or not right_head:
            continue
        hits = text.count(left_tail)
        if hits == 0:
            log.append(f"- 合并第 {left_page}/{right_page} 页：锚点已不存在，跳过")
            continue
        if hits != 1:
            raise FixError(
                f"合并第 {left_page}/{right_page} 页：左页尾锚点定位不唯一（{hits} 次）：{left_tail[-60:]!r}"
            )
        index = text.index(left_tail) + len(left_tail)
        gap = re.match(r"[\s\u3000]*", text[index:]).end()
        following = text[index + gap :]
        if not following.startswith(right_head[:40]):
            raise FixError(
                f"合并第 {left_page}/{right_page} 页：左页尾之后不是右页首行（看到 {following[:40]!r}）"
            )
        keep = index
        if item.get("drop_hyphen") and left_tail.endswith("-"):
            keep = index - 1  # 行尾连字符是断词符，接上时删掉
        text = text[:keep] + sep + text[index + gap :]
        log.append(
            f"- 合并第 {left_page}/{right_page} 页断句：…{left_tail[-30:]!r} + {right_head[:30]!r}…"
        )
        applied_joins += 1

    # --- 3) 定点替换 -------------------------------------------------------
    for item in plan.get("replacements") or []:
        find = str(item.get("find") or "")
        repl = str(item.get("with") or "")
        why = str(item.get("why") or "")
        if not find:
            continue
        hits = text.count(find)
        if hits == 0:
            if repl and repl in text:
                log.append(f"- 替换：已是目标状态，跳过（{why}）")
                continue
            raise FixError(f"替换定位不到：{find[:80]!r}")
        expect = int(item.get("count") or 1)
        if hits != expect:
            raise FixError(f"替换定位命中 {hits} 次，期望 {expect} 次：{find[:80]!r}")
        text = text.replace(find, repl, expect)
        log.append(f"- 替换（{why or '定点'}）：{find[:60]!r} → {repl[:60]!r}")

    # --- 4) 同形成批替换 ---------------------------------------------------
    for item in plan.get("regex_replacements") or []:
        pattern = str(item.get("pattern") or "")
        repl = str(item.get("with") or "")
        why = str(item.get("why") or "")
        expect = item.get("expect")
        if not pattern:
            continue
        hits = len(re.findall(pattern, text))
        if hits == 0:
            log.append(f"- 成批替换：已无命中，跳过（{why}）")
            continue
        if expect is not None and hits != int(expect):
            raise FixError(f"成批替换命中 {hits} 次，期望 {expect} 次：{pattern[:60]!r}")
        text = re.sub(pattern, repl, text)
        log.append(f"- 成批替换 {hits} 处（{why}）：{pattern[:50]!r} → {repl[:50]!r}")

    # --- 5) 删复读段落 -----------------------------------------------------
    for item in plan.get("drop_paragraphs") or []:
        para = str(item.get("text") or "")
        occ = int(item.get("occurrence") or 2)
        why = str(item.get("why") or "")
        if not para:
            continue
        positions = [m.start() for m in re.finditer(re.escape(para), text)]
        if len(positions) < occ:
            if len(positions) == 0:
                log.append(f"- 删复读：已删过或找不到，跳过（{why}）")
                continue
            raise FixError(f"删复读：只找到 {len(positions)} 处，第 {occ} 处不存在：{para[:60]!r}")
        start = positions[occ - 1]
        end = start + len(para)
        # 连同前后空行一起吃掉，避免留下多余空段
        while start > 0 and text[start - 1] in "\n\r":
            start -= 1
        while end < len(text) and text[end] in "\n\r":
            end += 1
        text = text[:start] + "\n\n" + text[end:]
        log.append(f"- 删复读第 {occ} 处（{why}）：{para[:60]!r}")

    if args.dry_run:
        print("\n".join(log))
        print(f"· dry-run：未写盘（共 {len(log)} 条动作）")
        return 0

    source_path.write_text(text, encoding="utf-8")
    notes = run.calibrate_dir / "notes.md"
    header = f"\n## 机械修复（scripts/calibrate_fix.py，plan={plan_path.name}）\n\n"
    with notes.open("a", encoding="utf-8") as fh:
        if notes.stat().st_size == 0:
            fh.write("# 校准记录\n")
        fh.write(header)
        fh.write("\n".join(log) + "\n")
    print(f"· 已写入 {source_path}（{len(log)} 条动作），留痕见 {notes}")
    print(f"· 页尾补内容 {applied_appends} 页，页界断句合并 {applied_joins} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

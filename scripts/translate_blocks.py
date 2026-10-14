#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""翻译校准稿里的**正文行**：代码清单、表格、公式、索引原样保留。

为什么按"行"翻：百度翻译的 ``q`` 用 ``\\n`` 分行、响应逐行返回且顺序保持
（探测确认），一行一段正好一一对应。代码/表格这些不该翻的内容根本不进
请求，比"译完整本书再修"安全得多——机翻最擅长把 ``bsc -sim`` 改成
"bsc -模拟"，这里从源头杜绝。

缓存：每行的译文落在 ``<out>.cache.json``（键含语言方向与 model/prompt），
重跑只补新增行，不重复计费。

用法::

    python scripts/translate_blocks.py --run <run_id>                 # source.md -> source.zh.md
    python scripts/translate_blocks.py --run <run_id> --plan          # 连 compose-plan.json 标题一起译
    python scripts/translate_blocks.py --input a.md --out a.zh.md
    python scripts/translate_blocks.py --run <run_id> --dry-run       # 只统计，不调接口
    python scripts/translate_blocks.py --run <run_id> --limit 50      # 只译前 50 行（试额度用）

凭据与语言方向在 pdf2epub.toml 的 ``[baidutrans]``（或 .env 的三个 BAIDU_TRANS_*）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
try:
    from pdf2epub.baidutrans import BaiduTransClient
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")

from compose_codebook import classify_line, group_blocks  # noqa: E402

#: 行内代码与行内公式：送翻前包成 XML 标签（服务端原样保留、标签内不翻）
SPAN = re.compile(r"`([^`]+)`|\$([^$]+)\$")
TAG_NAME = "pdf2epubspan"

#: 一行里 ASCII 字母少于这个数就当"没有可翻内容"（页码、纯数字、已是中文）
MIN_LETTERS = 4

#: 这些类型的块要翻译；code/html/math 不进请求
TRANSLATABLE = ("prose", "heading", "bullet", "bullets")

#: 这些类型的 part 整段跳过：印刷目录/索引是"条目 + 页码"，按条目标题单独译
SKIP_KINDS = ("toc", "index")

#: 印刷目录/索引的"条目标题 + 页码"：页码可能是 "12, 50" 或 "1-1"（章-页）
TOC_TAIL = re.compile(
    r"^(?P<title>.*?)[\s.．]*(?P<page>\d+(?:[-–]\d+)?(?:\s*,\s*\d+(?:[-–]\d+)?)*)\s*$"
)


def needs_translation(line: str) -> bool:
    return len(re.findall(r"[A-Za-z]", line)) >= MIN_LETTERS


def protect(line: str) -> str:
    """行内 `code` 与 $math$ 包成 <pdf2epubspan>…</pdf2epubspan>。"""
    def repl(match: re.Match) -> str:
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        return f"<{TAG_NAME}>{inner}</{TAG_NAME}>"

    return SPAN.sub(repl, line)


def restore(line: str) -> str:
    def repl(match: re.Match) -> str:
        inner = match.group(1) or ""
        return f"`{inner}`"

    return re.sub(rf"<{TAG_NAME}>(.*?)</{TAG_NAME}>", repl, line)


def strip_marker(line: str) -> tuple[str, str]:
    """把 '## 标题' / '- 条目' 的记号摘下来，只翻正文。"""
    match = re.match(r"^(\s*(?:#{1,6}|[-•·☐□])\s+)(.*)$", line)
    if match and needs_translation(match.group(2)):
        return match.group(1), match.group(2)
    return "", line


def main() -> int:
    parser = argparse.ArgumentParser(description="翻译校准稿正文（代码/表格不动）")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--input", help="要翻译的 markdown（缺省用该 run 的 calibrate/source.md）")
    parser.add_argument("--out", help="输出路径（缺省 <input> 同目录、加 .zh 后缀）")
    parser.add_argument("--plan", action="store_true", help="同时翻译 compose-plan.json 里的标题")
    parser.add_argument("--dry-run", action="store_true", help="只统计要翻译多少行，不调接口")
    parser.add_argument("--limit", type=int, default=0, help="只译前 N 行（试额度/试效果用）")
    parser.add_argument("--force", action="store_true", help="忽略缓存全部重译")
    parser.add_argument("--from", dest="from_lang", help="覆盖源语言")
    parser.add_argument("--to", dest="to_lang", help="覆盖目标语言")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    config = load_config()
    trans = config.baidutrans
    if args.from_lang:
        trans.from_lang = args.from_lang
    if args.to_lang:
        trans.to_lang = args.to_lang

    if args.input:
        src_path = Path(args.input)
    else:
        run = RunStore(Path(config.workdir)).resolve(args.run)
        src_path = run.calibrate_dir / "source.md"
    if not src_path.is_file():
        sys.exit(f"找不到输入：{src_path}")
    out_path = Path(args.out) if args.out else src_path.with_name(src_path.stem + ".zh.md")
    cache_path = out_path.with_suffix(out_path.suffix + ".cache.json")
    cache: dict[str, str] = {}
    if cache_path.is_file() and not args.force:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def cache_key(text: str) -> str:
        material = f"{trans.from_lang}>{trans.to_lang}|{trans.model}|{trans.prompt}|{text}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    lines = src_path.read_text(encoding="utf-8").splitlines()
    kinds = [classify_line(line) for line in lines]

    # ---------------------------------------------------------------
    # 撰写计划：印刷目录/索引这两类 part 的行不整行送翻，改成只译条目标题
    # ---------------------------------------------------------------
    toc_ranges: list[tuple[int, int]] = []
    plan = None
    if not args.input:
        run = RunStore(Path(config.workdir)).resolve(args.run)
        plan_path = run.build_dir / "compose-plan.json"
        if plan_path.is_file():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            for part in plan["parts"]:
                if part.get("kind") in SKIP_KINDS:
                    toc_ranges.append((part["start"] - 1, part["end"]))

    def in_toc(index: int) -> bool:
        return any(lo <= index < hi for lo, hi in toc_ranges)

    # ---------------------------------------------------------------
    # 收集要翻译的行（块类型 + 行内是否有字母），顺带把行内 span 包起来
    # ---------------------------------------------------------------
    jobs: list[tuple[int, str, str]] = []   # (行号, 记号, 待译文本)
    for b_start, b_end, kind in group_blocks(lines, kinds, 0, len(lines)):
        if kind not in TRANSLATABLE:
            continue
        for i in range(b_start, b_end):
            if in_toc(i):
                continue
            marker, text = strip_marker(lines[i])
            if not needs_translation(text):
                continue
            jobs.append((i, marker, protect(text)))

    # 印刷目录/索引：只译"条目标题"，页码与点线保持原样；无页码的整行算标题
    toc_jobs: list[tuple[int, str, str]] = []   # (行号, 待译标题, 保留的尾巴)
    for lo, hi in toc_ranges:
        for i in range(lo, min(hi, len(lines))):
            stripped = lines[i].strip()
            if not stripped:
                continue
            title = re.sub(r"^#{1,4}\s+", "", stripped)
            match = TOC_TAIL.match(title)
            if match and match.group("title").strip():
                head = match.group("title").strip()
                pos = lines[i].find(head)
                if pos < 0:
                    continue
                tail = lines[i][pos + len(head):]
                if not needs_translation(head):
                    continue
                toc_jobs.append((i, protect(head), tail))
            elif needs_translation(title) and len(title) <= 120:
                toc_jobs.append((i, protect(title), ""))

    if args.limit:
        jobs = jobs[: args.limit]

    total_bytes = sum(len(text.encode("utf-8")) for _, _, text in jobs)
    total_bytes += sum(len(text.encode("utf-8")) for _, text, _ in toc_jobs)
    print(f"· 输入 {src_path}")
    print(f"· 待译 正文 {len(jobs)} 行 + 目录/索引条目 {len(toc_jobs)} 行，"
          f"共 {total_bytes} 字节"
          f"（缓存命中 {sum(1 for _, _, t in jobs if cache_key(t) in cache)}"
          f"+{sum(1 for _, t, _ in toc_jobs if cache_key(t) in cache)}）")
    if args.dry_run:
        return 0
    if not jobs and not toc_jobs:
        print("· 没有需要翻译的行")
        return 0

    client = BaiduTransClient(trans)

    # ---------------------------------------------------------------
    # 缓存命中的直接用，没命中的分批送接口
    # ---------------------------------------------------------------
    done: dict[int, str] = {}
    pending = [(i, marker, text) for i, marker, text in jobs if cache_key(text) not in cache]
    toc_pending = [(i, text, tail) for i, text, tail in toc_jobs if cache_key(text) not in cache]
    print(f"· 实际请求 正文 {len(pending)} 行 + 条目 {len(toc_pending)} 行，"
          f"按 ≤{trans.max_bytes} 字节分批")

    batch: list[tuple[int, str, str]] = []
    skipped: list[str] = []

    def save_cache() -> None:
        # 逐批落盘：跑一半被限流/出错也不丢进度
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")

    def translate_one(text: str) -> str | None:
        """单行重试：批里有毒行时用来隔离。返回 None 表示这行翻不了。"""
        try:
            return client.translate_batched([text])[0]
        except Exception as exc:  # noqa: BLE001 - 单行失败记录后跳过，不拖垮整本书
            print(f"! 单行翻译失败（{exc}）：{text[:80]!r}", file=sys.stderr)
            skipped.append(text)
            return None

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        q_lines = [text for _, _, text in batch]
        try:
            translated = client.translate_batched(q_lines)
        except Exception as exc:  # noqa: BLE001 - 整批失败：降级成逐行，隔离坏行
            print(f"! 批次失败（{exc}），降级逐行翻译 {len(batch)} 行", file=sys.stderr)
            for index, marker, text in batch:
                dst = translate_one(text)
                if dst is not None:
                    cache[cache_key(text)] = dst
                    done[index] = marker + restore(dst)
            batch = []
            save_cache()
            return
        if len(translated) != len(batch):
            sys.exit(f"返回行数 {len(translated)} != 请求 {len(batch)}，中止")
        for (index, marker, text), dst in zip(batch, translated):
            cache[cache_key(text)] = dst
            done[index] = marker + restore(dst)
        batch = []
        save_cache()

    budget = trans.max_bytes
    for index, marker, text in pending:
        if sum(len(t.encode("utf-8")) for _, _, t in batch) + len(text.encode("utf-8")) + 1 > budget:
            flush()
        batch.append((index, marker, text))
    flush()

    if toc_pending:
        # 条目标题一行一条送（片段回填按行拼），页码与点线不在请求里
        try:
            translated = client.translate_batched([text for _, text, _ in toc_pending])
            for (index, text, _), dst in zip(toc_pending, translated):
                cache[cache_key(text)] = restore(dst)
        except Exception as exc:  # noqa: BLE001 - 同样降级逐行
            print(f"! 目录批次失败（{exc}），降级逐行", file=sys.stderr)
            for index, text, _ in toc_pending:
                dst = translate_one(text)
                if dst is not None:
                    cache[cache_key(text)] = restore(dst)
        save_cache()

    if skipped:
        skip_path = out_path.with_suffix(out_path.suffix + ".skipped.txt")
        skip_path.write_text("\n".join(skipped) + "\n", encoding="utf-8")
        print(f"! {len(skipped)} 行翻不了，原文保留，清单见 {skip_path.name}", file=sys.stderr)

    # ---------------------------------------------------------------
    # 回填：缓存里有的一律用缓存（包括本次新译的）；没缓存的保留原文
    # ---------------------------------------------------------------
    result = list(lines)
    for index, marker, text in jobs:
        if index in done:
            result[index] = done[index]
        elif cache_key(text) in cache:
            result[index] = marker + restore(cache[cache_key(text)])
    for index, text, tail in toc_jobs:
        if cache_key(text) not in cache:
            continue
        result[index] = restore(cache[cache_key(text)]) + tail

    out_path.write_text("\n".join(result) + "\n", encoding="utf-8")
    print(f"· 已写出 {out_path}（缓存 {len(cache)} 行 -> {cache_path.name}）")

    # ---------------------------------------------------------------
    # compose-plan.json 的标题也要翻，否则撰写时对不上标题
    # ---------------------------------------------------------------
    if args.plan:
        if args.input:
            print("· --input 模式不处理撰写计划，跳过")
            return 0
        run = RunStore(Path(config.workdir)).resolve(args.run)
        plan_path = run.build_dir / "compose-plan.json"
        if not plan_path.is_file():
            print(f"· 没有撰写计划（{plan_path}），跳过")
            return 0
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        titles = [part["title"] for part in plan["parts"] if needs_translation(part["title"])]
        if plan.get("title") and needs_translation(plan["title"]):
            titles.append(plan["title"])
        if titles:
            translated = client.translate_batched([protect(t) for t in titles])
            mapping = dict(zip(titles, (restore(t) for t in translated)))
            for part in plan["parts"]:
                if part["title"] in mapping:
                    # 原标题进 strip：翻译后的校准稿里还是原文，撰写时要摘掉
                    strip = [s for s in (part.get("strip") or []) if s]
                    if part["title"] not in strip:
                        strip.append(part["title"])
                    part["strip"] = strip
                    part["title"] = mapping[part["title"]]
            if plan.get("title") in mapping:
                plan["title"] = mapping[plan["title"]]
        plan["language"] = trans.to_lang
        target = plan_path.with_name("compose-plan.zh.json")
        target.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"· 撰写计划标题已译：{target}（撰写时 --plan 指到它）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

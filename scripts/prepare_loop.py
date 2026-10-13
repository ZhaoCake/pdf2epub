#!/usr/bin/env python
"""低并发额度下的 prepare 驱动器：提交一片、取回一片，直到全部分片到手。

为什么需要它：``submit_jobs.py`` 在一次被拒后就 ``break``，而 AI Studio 这个令牌
的**提交频次/在飞任务**额度很小——实测一次只放行 1~2 片，被拒后要等十几秒到几十秒
（HTTP 400，``code 10010 当前提交任务数过多，请稍后重试``）。用 ``--batch 6`` 提交
时反复撞限额，一条批里前面成功的没取回、后面的一片没提交，只能人肉来回跑
submit/fetch。这个脚本把两件事合成一个循环：

1. 有未提交的分片 → 试提交一片（被拒就退避，不放弃其余分片）；
2. 有已提交未取回的分片 → 查状态，``done`` 就下载并归一成 bundle；
3. 循环直到所有分片都有 ``extracted/<chunk>/.extracted``，或到达 ``--max-hours``。

已取回的分片不会重复提交，可以中断后反复跑。

用法::

    python scripts/prepare_loop.py --run <run_id>
    python scripts/prepare_loop.py --run <run_id> --pause 10 --backoff 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from pdf2epub import paddle
    from pdf2epub.config import load_config
    from pdf2epub.runstate import RunStore
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")


def chunk_pdf(run, chunk_id: str) -> Path | None:
    matches = sorted(run.chunks_dir.glob(f"*.{chunk_id}.p*.pdf"))
    return matches[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(description="低额度下的 prepare 驱动循环")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--pause", type=float, default=10.0, help="两次提交之间的停顿秒数")
    parser.add_argument("--backoff", type=float, default=30.0, help="被拒后的退避秒数（逐次翻倍）")
    parser.add_argument("--max-backoff", type=float, default=90.0, help="退避上限；额度是按分钟放行的，等太久不如勤试")
    parser.add_argument("--attempts", type=int, default=200, help="单片连续被拒多少次算放弃")
    parser.add_argument("--max-hours", type=float, default=6.0, help="整个循环的时间上限")
    args = parser.parse_args()

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    client = paddle.PaddleClient(config.paddle)

    todo = [str(c.get("chunk_id")) for c in (run.counters.get("chunks") or []) if c.get("chunk_id")]

    def extracted(chunk_id: str) -> bool:
        return (run.extracted_dir / chunk_id / ".extracted").is_file()

    def load_jobs() -> dict[str, str]:
        if not run.jobs_path.is_file():
            return {}
        return (json.loads(run.jobs_path.read_text(encoding="utf-8")) or {}).get("jobs") or {}

    def save_jobs(jobs: dict[str, str]) -> None:
        run.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        run.jobs_path.write_text(
            json.dumps({"jobs": jobs}, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    deadline = time.monotonic() + args.max_hours * 3600
    backoff = args.backoff
    strikes = 0
    last_chunk = ""

    print(f"· 运行 {run.run_id}，共 {len(todo)} 片，输出 {run.extracted_dir}", flush=True)

    while True:
        jobs = load_jobs()
        missing = [c for c in todo if not extracted(c)]
        if not missing:
            print(f"· 全部 {len(todo)} 片已取回", flush=True)
            return 0
        if time.monotonic() > deadline:
            print(f"· 到达时间上限，未完成：{' '.join(missing)}", flush=True)
            return 1

        # 1) 取回已完成的任务
        fetched = False
        for chunk_id in list(missing):
            job_id = jobs.get(chunk_id)
            if not job_id:
                continue
            outcome = client.query_job(job_id)
            stamp = time.strftime("%H:%M:%S")
            if outcome.state == paddle.STATE_DONE:
                jsonl = client.download_jsonl(outcome.json_url)
                paddle.build_bundle(jsonl, run.extracted_dir / chunk_id, chunk_id=chunk_id)
                print(f"[{stamp}] {chunk_id} 已取回（{outcome.extracted_pages} 页）", flush=True)
                fetched = True
            elif outcome.state == paddle.STATE_FAILED:
                print(f"[{stamp}] {chunk_id} 服务端解析失败：{outcome.error}", flush=True)
                jobs.pop(chunk_id, None)
                save_jobs(jobs)
            else:
                print(
                    f"[{stamp}] {chunk_id} {outcome.state} "
                    f"{outcome.extracted_pages}/{outcome.total_pages or '?'} 页",
                    flush=True,
                )

        # 2) 提交一片新的（在飞任务的服务端额度很小，一次只推一片）
        unsubmitted = [c for c in todo if not extracted(c) and c not in jobs]
        if unsubmitted:
            chunk_id = unsubmitted[0]
            pdf = chunk_pdf(run, chunk_id)
            if pdf is None:
                print(f"  ! {chunk_id} 找不到分片 PDF", flush=True)
                return 1
            try:
                job_id = client.submit_job(pdf, label=chunk_id)
            except Exception as exc:  # noqa: BLE001 - 限流/400/网络异常统一退避
                detail = getattr(exc, "detail", None)
                body = (detail or {}).get("body") if isinstance(detail, dict) else None
                strikes = strikes + 1 if chunk_id == last_chunk else 1
                last_chunk = chunk_id
                if strikes >= args.attempts:
                    print(f"  ! {chunk_id} 连续被拒 {strikes} 次，放弃：{exc}", flush=True)
                    print(f"    仍未提交：{' '.join(unsubmitted)}", flush=True)
                    return 1
                wait = min(backoff * (2 ** (strikes - 1)), args.max_backoff)
                print(
                    f"  ! {chunk_id} 第 {strikes} 次被拒：{exc}"
                    f"{('｜' + str(body)[:160]) if body else ''}；{wait:.0f}s 后重试",
                    flush=True,
                )
                time.sleep(wait)
                continue
            strikes = 0
            jobs[chunk_id] = job_id
            save_jobs(jobs)
            print(f"[{time.strftime('%H:%M:%S')}] {chunk_id} -> {job_id}（已写入 jobs.json）", flush=True)
            time.sleep(args.pause)
        elif not fetched:
            time.sleep(config.paddle.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())

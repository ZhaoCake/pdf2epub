#!/usr/bin/env python
"""接续一次被中断的 prepare：按 ``parsing/jobs.json`` 里已有的 job 取回结果。

为什么需要它：PaddleOCR 的解析在服务端跑，动辄几分钟到几十分钟。``pdf2epub run``
一旦在等待期间被打断（终端关掉、Agent 调用超时），本地 extracted/ 还是空的，
再跑一次 ``prepare`` 就会**重新提交一遍**——白烧一次额度，之前的 job 结果也留在
服务端没人取。job_id 已经写在 ``parsing/jobs.json`` 里了，直接去取就行。

它只做"查状态 -> 下载 JSONL -> 归一成 bundle"，与 prepare 里那段逻辑完全一致；
已取回的分片（``extracted/<chunk>/.extracted`` 存在）直接跳过，可以反复跑。
取完之后再执行 ``pdf2epub run --until prepare``，prepare 会看到所有分片都在，
跳过提交，只做剩下的交界处校验。

用法::

    python scripts/fetch_jobs.py --run <run_id>          # --run 省略则用最近一次
    python scripts/fetch_jobs.py --run <run_id> --interval 10 --timeout 3600

少数情况下某个 job 会在服务端挂住（状态一直 ``running``、extractedPages 不动），
其余分片都回来了就它没有。这时用 ``--resubmit`` 单独重投那几片（会重新提交、
重新计费，所以只在确实卡住时用）::

    python scripts/fetch_jobs.py --run <run_id> --resubmit c008

需要 ``PADDLE_TOKEN``（会读当前目录的 .env，与流水线同源）。
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
    from pdf2epub.runstate import RunStore, atomic_write_json, read_json
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里运行（pip install -e .）")


def load_run(workdir: Path, run_id: str | None):
    store = RunStore(workdir)
    return store.resolve(run_id)


def find_chunk_pdf(run, chunk_id: str) -> Path | None:
    """按分片 PDF 文件名里的 chunk_id 找文件（``<stem>.<chunk_id>.pNNNN-NNNN.pdf``）。"""
    matches = sorted(run.chunks_dir.glob(f"*.{chunk_id}.p*.pdf"))
    return matches[0] if matches else None


def resubmit(run, client, chunk_id: str) -> str:
    """重新提交一个分片，返回新的 job_id；同时把 jobs.json 更新掉。"""
    pdf = find_chunk_pdf(run, chunk_id)
    if pdf is None:
        raise SystemExit(f"找不到分片 PDF：{run.chunks_dir}/*.{chunk_id}.p*.pdf")
    job_id = client.submit_job(pdf, label=f"{chunk_id}（重投）")
    payload = read_json(run.jobs_path, {}) or {}
    payload.setdefault("jobs", {})[chunk_id] = job_id
    payload.setdefault("resubmitted", {})[chunk_id] = job_id
    atomic_write_json(run.jobs_path, payload)
    return job_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="接续被中断的 prepare：取回 jobs.json 里已有的解析结果",
    )
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--workdir", default=None, help="工作目录（缺省读配置，默认 .pdf2epub）")
    parser.add_argument("--interval", type=float, default=None, help="轮询间隔秒数（缺省用配置值）")
    parser.add_argument("--timeout", type=float, default=None, help="等待单个 job 的上限秒数")
    parser.add_argument(
        "--resubmit",
        metavar="c008,c009",
        help="卡住的分片：重新提交这几个分片（会产生新的 job 与费用）",
    )
    args = parser.parse_args()

    config = load_config(overrides={"workdir": args.workdir} if args.workdir else None)
    interval = args.interval if args.interval is not None else config.paddle.poll_interval
    timeout = args.timeout if args.timeout is not None else config.paddle.poll_timeout

    run = load_run(Path(config.workdir), args.run)
    jobs = (read_json(run.jobs_path, {}) or {}).get("jobs") or {}
    if not jobs:
        print(f"· {run.jobs_path} 里没有 job 清单，没什么可取的（先跑一次 prepare 提交）")
        return 1

    client = paddle.PaddleClient(config.paddle)
    print(f"· 运行 {run.run_id}")
    print(f"· job 清单 {len(jobs)} 条，输出到 {run.extracted_dir}")

    if args.resubmit:
        targets = [c.strip() for c in args.resubmit.split(",") if c.strip()]
        unknown = [c for c in targets if c not in jobs]
        if unknown:
            print(f"· 清单里没有这些分片，检查拼写：{' '.join(unknown)}")
            return 1
        for chunk_id in targets:
            new_job = resubmit(run, client, chunk_id)
            jobs[chunk_id] = new_job
            print(f"· 重投 {chunk_id} -> job {new_job}", flush=True)

    pending = {
        chunk_id: job_id
        for chunk_id, job_id in sorted(jobs.items())
        if not (run.extracted_dir / chunk_id / ".extracted").is_file()
    }
    if not pending:
        print("· 全部分片都已取回，无需操作")
        return 0
    print(f"· 待取回 {len(pending)} 片：{' '.join(pending)}")
    print()

    deadline = time.monotonic() + timeout
    done: list[str] = []
    failed: list[str] = []
    while pending:
        for chunk_id in list(pending):
            job_id = pending[chunk_id]
            outcome = client.query_job(job_id)
            if outcome.state == paddle.STATE_DONE:
                jsonl = client.download_jsonl(outcome.json_url)
                paddle.build_bundle(
                    jsonl,
                    run.extracted_dir / chunk_id,
                    chunk_id=chunk_id,
                )
                done.append(chunk_id)
                del pending[chunk_id]
                print(f"[{time.strftime('%H:%M:%S')}] {chunk_id} 已取回（{outcome.extracted_pages} 页）", flush=True)
            elif outcome.state == paddle.STATE_FAILED:
                failed.append(chunk_id)
                del pending[chunk_id]
                print(f"[{time.strftime('%H:%M:%S')}] {chunk_id} 服务端解析失败：{outcome.error}", flush=True)
            else:
                print(
                    f"[{time.strftime('%H:%M:%S')}] {chunk_id} {outcome.state}"
                    f" {outcome.extracted_pages}/{outcome.total_pages or '?'} 页",
                    flush=True,
                )
        if pending:
            if time.monotonic() > deadline:
                print(f"· 超时（>{timeout:.0f}s），仍在等：{' '.join(pending)}")
                return 1
            time.sleep(interval)

    print()
    print(f"· 取回 {len(done)} 片，失败 {len(failed)} 片")
    if failed:
        print(f"  失败的分片：{' '.join(failed)}（需要重新提交，可直接重跑 prepare）")
        return 1
    print("· 下一步：pdf2epub run --until prepare（跳过提交，只做交界处校验）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

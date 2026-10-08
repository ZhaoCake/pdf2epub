#!/usr/bin/env python
"""分批提交解析任务，并把 job_id **逐个**写回 ``parsing/jobs.json``。

为什么需要它：``pdf2epub run --until prepare`` 是一次性把全部分片提交完、**成功之后**
才落盘 jobs.json。实测 612 页的书（31 片）第 8 片被服务端以 HTTP 400 拒绝（前 7 片连着
几秒内提交完，期间已经出现过一次 429 限流），此时：

- 前 7 个 job 已经在服务端跑起来了，但 job_id 只留在 ``logs/pipeline.jsonl`` 里；
- jobs.json 根本没生成，``scripts/fetch_jobs.py`` 无从下手；
- 再跑一次 prepare 会**重新提交**这 7 片，白烧一次额度。

所以大书不要用 prepare 一次性提交，而是：``submit_jobs.py`` 分批提交（每批之间停顿、
被拒就退避重试）→ ``fetch_jobs.py`` 取回结果 → ``pdf2epub run --until prepare``
（这时 31 片的 ``.extracted`` 都在，会跳过提交，只做交界处校验）。

用法::

    python scripts/submit_jobs.py --run <run_id>                  # 提交一批（默认 4 片）
    python scripts/submit_jobs.py --run <run_id> --batch 6        # 一批 6 片
    python scripts/submit_jobs.py --run <run_id> --only c008,c009 # 点名提交
    python scripts/submit_jobs.py --run <run_id> --all            # 把所有未提交的分片排空

已提交过的分片（jobs.json 里有、且没有 ``.extracted``）**不会重复提交**；
已经取回的分片（``.extracted`` 存在）直接跳过。可以反复跑。
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
    from pdf2epub.runstate import RunStore, read_json
except ModuleNotFoundError:
    sys.exit("需要在已安装本项目的环境里跑（pip install -e .）")


def pending_chunks(run) -> list[str]:
    """按 chunk 顺序列出"还没取回"的分片 id。"""
    out: list[str] = []
    for chunk in run.counters.get("chunks") or []:
        chunk_id = str(chunk.get("chunk_id") or "")
        if chunk_id and not (run.extracted_dir / chunk_id / ".extracted").is_file():
            out.append(chunk_id)
    return out


def chunk_pdf(run, chunk_id: str) -> Path | None:
    matches = sorted(run.chunks_dir.glob(f"*.{chunk_id}.p*.pdf"))
    return matches[0] if matches else None


def submit_with_retry(client, pdf: Path, chunk_id: str, *, attempts: int, pause: float) -> str | None:
    """提交一片；被限流/被拒就退避重试。返回 job_id，全部失败返回 None。"""
    for attempt in range(1, attempts + 1):
        try:
            return client.submit_job(pdf, label=chunk_id)
        except Exception as exc:  # noqa: BLE001 - 限流、400、网络异常都按"再试一次"处理
            detail = getattr(exc, "detail", None)
            body = (detail or {}).get("body") if isinstance(detail, dict) else None
            wait = pause * (2 ** attempt)
            print(f"  ! {chunk_id} 第 {attempt} 次提交失败：{exc}"
                  f"{('｜服务端返回：' + str(body)[:200]) if body else ''}")
            if attempt >= attempts:
                return None
            print(f"    {wait:.0f}s 后再试")
            time.sleep(wait)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="分批提交解析任务并逐个落盘 job_id")
    parser.add_argument("--run", help="运行 ID，缺省用最近一次")
    parser.add_argument("--batch", type=int, default=4, help="本次最多提交几片（默认 4）")
    parser.add_argument("--pause", type=float, default=3.0, help="片与片之间的停顿秒数")
    parser.add_argument("--attempts", type=int, default=4, help="单片被拒时的重试次数")
    parser.add_argument("--only", help="只提交这几片（逗号分隔）")
    parser.add_argument("--all", action="store_true", help="把所有未提交的分片排空（仍按 --batch 分批停顿）")
    args = parser.parse_args()

    config = load_config()
    run = RunStore(Path(config.workdir)).resolve(args.run)
    todo = pending_chunks(run)
    jobs = (read_json(run.jobs_path, {}) or {}).get("jobs") or {}
    if args.only:
        wanted = [c.strip() for c in args.only.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in todo]
        if unknown:
            print(f"· 这些分片不用提交（已取回或不存在）：{' '.join(unknown)}")
        todo = [c for c in wanted if c in todo]
    already = [c for c in todo if c in jobs]
    fresh = [c for c in todo if c not in jobs]

    print(f"· 运行 {run.run_id}")
    print(f"· 未取回 {len(todo)} 片：已提交待取回 {len(already)} 片，未提交 {len(fresh)} 片")

    client = paddle.PaddleClient(config.paddle)
    limit = len(fresh) if args.all else args.batch
    submitted: list[str] = []

    for chunk_id in fresh[:limit]:
        pdf = chunk_pdf(run, chunk_id)
        if pdf is None:
            print(f"  ! {chunk_id} 找不到分片 PDF，跳过")
            continue
        job_id = submit_with_retry(
            client, pdf, chunk_id, attempts=args.attempts, pause=args.pause
        )
        if job_id is None:
            print(f"  · {chunk_id} 放弃提交（已提交的 {len(submitted)} 片仍会保留在 jobs.json 里）")
            break
        jobs[chunk_id] = job_id
        run.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        run.jobs_path.write_text(
            json.dumps({"jobs": jobs}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        submitted.append(chunk_id)
        print(f"  · {chunk_id} -> {job_id}（已写入 jobs.json）", flush=True)
        time.sleep(args.pause)

    remaining = [c for c in pending_chunks(run) if c not in jobs]
    print()
    print(f"· 本次提交 {len(submitted)} 片；仍未提交 {len(remaining)} 片"
          f"{('：' + ' '.join(remaining[:8]) + (' …' if len(remaining) > 8 else '')) if remaining else ''}")
    print("· 下一步：python scripts/fetch_jobs.py --run " + run.run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

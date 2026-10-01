"""运行状态：可断点续跑、可缓存、可审计。

目录布局（每个目录都对应一次"谁来干活"的约定）::

    <workdir>/
      runs/
        <run_id>/
          run.json               # 状态机（唯一真相源）
          input.pdf              # 输入副本（内容寻址，避免源文件被改动）
          logs/pipeline.jsonl
          alerts.json
          chunks/                # 超限 PDF 的切分产物
          mineru/
            batch.json           # 提交记录
            zips/                # 官方 API 返回的原始 zip
            extracted/           # 解压后的解析产物（原样保留，不加工）
          calibrate/             # ① 脚本派单、LLM 校准低分段落
            report.md            #   工单：哪些段落分数低、页码、原文
            segments.json        #   机器可读的同一份清单
            source.md            #   ★ LLM 直接编辑这个文件
            pages/*.png          #   相关页的渲染图（含上下文页）
            images/              #   MinerU 抽出的图，供对照
          build/                 # ② LLM 直接在这里写 EPUB 内容
            book.json            #   ★ 书目 + 阅读顺序（LLM 写）
            OEBPS/text/*.xhtml   #   ★ 章节正文（LLM 写）
            OEBPS/images/        #   脚本搬运的图片
            OEBPS/content.opf    #   打包时由脚本生成
          check/                 # ③ 脚本校验格式并出报告
            report.md
            report.json
          output/<name>.epub

每个 stage 记录 ``status / attempts / started_at / finished_at / input_hash / outputs``，
``input_hash`` 用于判断缓存是否仍然有效。

带 ★ 的文件是 Agent（LLM）的唯一工作面——除此之外它不需要理解任何私有格式。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .errors import InputError
from .logutil import get_logger

log = get_logger("state")

STATE_FILE = "run.json"
STATE_VERSION = 1


class Stage(str, Enum):
    """四个阶段，边界就是"谁来干活"。"""

    PREPARE = "prepare"        # 脚本：解析 PDF -> MinerU -> 解压产物
    CALIBRATE = "calibrate"    # LLM：对着页面图校准低分段落
    COMPOSE = "compose"        # LLM：把内容直接写成 EPUB 的 XHTML
    CHECK = "check"            # 脚本：打包 + 格式校验 + 出报告

    @classmethod
    def ordered(cls) -> list["Stage"]:
        return [cls.PREPARE, cls.CALIBRATE, cls.COMPOSE, cls.CHECK]


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"    # 等待外部（Agent 复核）输入


@dataclass
class StageRecord:
    name: str
    status: str = StageStatus.PENDING.value
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    input_hash: str = ""
    outputs: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    note: str = ""

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)

    def reset(self) -> None:
        """退回未执行状态。

        重跑前面的阶段时，后面阶段的结果就不作数了；留着旧的 blocked/done
        会让人误以为还有别的活在等着。``attempts`` 保留，那是历史。
        """
        self.status = StageStatus.PENDING.value
        self.started_at = None
        self.finished_at = None
        self.input_hash = ""
        self.outputs = []
        self.error = None
        self.note = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "attempts": self.attempts,
            "input_hash": self.input_hash,
            "outputs": list(self.outputs),
            "note": self.note,
        }
        if self.started_at is not None:
            data["started_at"] = self.started_at
        if self.finished_at is not None:
            data["finished_at"] = self.finished_at
            data["duration_s"] = self.duration
        if self.error:
            data["error"] = self.error
        return data


def _slugify(text: str, fallback: str = "run") -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text, flags=re.UNICODE).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return (text or fallback)[:60]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写：先写临时文件再替换，避免中断留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def remove_tree(path: Path) -> None:
    """删除整棵目录树，容忍中途失败。

    刻意不用 ``shutil.rmtree``：某些沙箱环境（含 IDE 运行时）会把它重定向成
    "移到回收站"，在非 ASCII 路径、网络盘或权限受限目录上会直接抛错。
    自动流水线不该因为"删不掉旧产物"而整体失败，所以这里自行遍历删除，
    遇到单个文件删不掉就跳过，最后尽力删掉空目录。
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_file() or path.is_symlink():
        try:
            path.unlink()
        except OSError as exc:
            log.debug("删除文件失败（忽略）：%s -> %s", path, exc)
        return

    entries = sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for child in entries:
        try:
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        except OSError as exc:
            log.debug("删除失败（忽略）：%s -> %s", child, exc)
    try:
        path.rmdir()
    except OSError as exc:
        log.debug("删除目录失败（忽略）：%s -> %s", path, exc)


@dataclass
class Run:
    """一次转换任务的全部状态与路径。"""

    run_id: str
    root: Path
    source_pdf: Path
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "created"          # created | running | blocked | succeeded | failed
    exit_code: int = 0
    source_hash: str = ""
    source_name: str = ""
    stages: dict[str, StageRecord] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)

    # ---- 路径 -----------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.root / STATE_FILE

    @property
    def log_path(self) -> Path:
        return self.root / "logs" / "pipeline.jsonl"

    @property
    def alerts_path(self) -> Path:
        return self.root / "alerts.json"

    @property
    def input_pdf(self) -> Path:
        return self.root / "input.pdf"

    @property
    def chunks_dir(self) -> Path:
        return self.root / "chunks"

    @property
    def zips_dir(self) -> Path:
        return self.root / "mineru" / "zips"

    @property
    def extracted_dir(self) -> Path:
        return self.root / "mineru" / "extracted"

    @property
    def batch_path(self) -> Path:
        return self.root / "mineru" / "batch.json"

    # ---- 校准工作区（LLM 编辑 source.md）--------------------------------
    @property
    def calibrate_dir(self) -> Path:
        return self.root / "calibrate"

    @property
    def calibrate_report(self) -> Path:
        return self.calibrate_dir / "report.md"

    @property
    def calibrate_source(self) -> Path:
        return self.calibrate_dir / "source.md"

    @property
    def segments_path(self) -> Path:
        return self.calibrate_dir / "segments.json"

    @property
    def pages_dir(self) -> Path:
        return self.calibrate_dir / "pages"

    # ---- 撰写工作区（LLM 写 book.json 与 OEBPS/text/）--------------------
    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def oebps_dir(self) -> Path:
        return self.build_dir / "OEBPS"

    @property
    def book_json(self) -> Path:
        return self.build_dir / "book.json"

    # ---- 校验工作区 ------------------------------------------------------
    @property
    def check_dir(self) -> Path:
        return self.root / "check"

    @property
    def check_report(self) -> Path:
        return self.check_dir / "report.md"

    @property
    def check_json(self) -> Path:
        return self.check_dir / "report.json"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def output_epub(self, stem: str | None = None) -> Path:
        return self.output_dir / f"{stem or self.source_pdf.stem}.epub"

    # ---- 状态机 ---------------------------------------------------------
    def stage(self, stage: Stage) -> StageRecord:
        rec = self.stages.get(stage.value)
        if rec is None:
            rec = StageRecord(name=stage.value)
            self.stages[stage.value] = rec
        return rec

    def touch(self) -> None:
        self.updated_at = time.time()

    def save(self) -> None:
        self.touch()
        atomic_write_json(self.state_path, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "run_id": self.run_id,
            "root": str(self.root),
            "source_pdf": str(self.source_pdf),
            "source_name": self.source_name,
            "source_hash": self.source_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "counters": self.counters,
            "config": self.config_snapshot,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Run":
        stages: dict[str, StageRecord] = {}
        for name, raw in (data.get("stages") or {}).items():
            stages[name] = StageRecord(
                name=name,
                status=raw.get("status", StageStatus.PENDING.value),
                attempts=int(raw.get("attempts", 0)),
                started_at=raw.get("started_at"),
                finished_at=raw.get("finished_at"),
                input_hash=raw.get("input_hash", ""),
                outputs=list(raw.get("outputs") or []),
                error=raw.get("error"),
                note=raw.get("note", ""),
            )
        return cls(
            run_id=data["run_id"],
            root=Path(data["root"]),
            source_pdf=Path(data["source_pdf"]),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            finished_at=data.get("finished_at"),
            status=data.get("status", "created"),
            exit_code=int(data.get("exit_code", 0)),
            source_hash=data.get("source_hash", ""),
            source_name=data.get("source_name", ""),
            stages=stages,
            config_snapshot=data.get("config") or {},
            counters=data.get("counters") or {},
        )

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "source": self.source_name,
            "root": str(self.root),
            "output": str(self.output_epub()) if self.output_epub().exists() else None,
            "stages": {
                name: {
                    "status": rec.status,
                    "attempts": rec.attempts,
                    "duration_s": rec.duration,
                    "note": rec.note,
                }
                for name, rec in self.stages.items()
            },
        }

    def first_incomplete(self) -> Stage | None:
        for stage in Stage.ordered():
            rec = self.stage(stage)
            if rec.status not in {StageStatus.DONE.value, StageStatus.SKIPPED.value}:
                return stage
        return None


class RunStore:
    """运行目录的创建、查找与清理。"""

    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir).resolve()
        self.runs_dir = self.workdir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ---- 创建 -----------------------------------------------------------
    def create(
        self,
        source: Path,
        *,
        run_id: str | None = None,
        title_hint: str = "",
        config_snapshot: dict[str, Any] | None = None,
    ) -> Run:
        source = Path(source)
        if not source.is_file():
            raise InputError(f"输入文件不存在：{source}")
        if source.suffix.lower() != ".pdf":
            raise InputError(f"当前仅支持 PDF 输入，收到：{source.suffix}")
        if source.stat().st_size == 0:
            raise InputError(f"输入文件为空：{source}")

        run_id = run_id or self._make_run_id(source, title_hint)
        root = self.runs_dir / run_id
        if root.exists():
            raise InputError(f"运行目录已存在：{root}（换个 --run-id 或先清理）")

        for sub in ("logs", "chunks", "mineru/zips", "mineru/extracted", "review/packets", "build", "output"):
            (root / sub).mkdir(parents=True, exist_ok=True)

        run = Run(
            run_id=run_id,
            root=root,
            source_pdf=source,
            source_name=source.name,
            source_hash=sha256_file(source),
            config_snapshot=config_snapshot or {},
        )
        shutil.copy2(source, run.input_pdf)
        run.save()
        log.info("创建运行 %s -> %s", run_id, root, extra={"extra_fields": {"run_id": run_id}})
        return run

    @staticmethod
    def _make_run_id(source: Path, title_hint: str) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = _slugify(title_hint or source.stem)
        return f"{stamp}-{base}-{uuid.uuid4().hex[:6]}"

    # ---- 查找 -----------------------------------------------------------
    def load(self, run_id: str) -> Run:
        root = self.runs_dir / run_id
        state = root / STATE_FILE
        if not state.is_file():
            raise InputError(f"找不到运行：{run_id}（缺少 {state}）")
        data = read_json(state, {})
        return Run.from_dict(data)

    def latest(self) -> Run:
        candidates = self.list_runs()
        if not candidates:
            raise InputError("当前没有任何运行记录，请先执行 `pdf2epub init <file.pdf>`")
        return candidates[0]

    def resolve(self, run_id: str | None) -> Run:
        return self.load(run_id) if run_id else self.latest()

    def list_runs(self) -> list[Run]:
        runs: list[Run] = []
        for child in sorted(self.runs_dir.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            state = child / STATE_FILE
            if state.is_file():
                try:
                    runs.append(Run.from_dict(read_json(state, {})))
                except Exception:  # 损坏的运行目录不应拖垮列表
                    log.warning("跳过损坏的运行目录：%s", child)
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs

    # ---- 清理 -----------------------------------------------------------
    def delete(self, run_id: str) -> None:
        root = self.runs_dir / run_id
        if not root.is_dir():
            raise InputError(f"找不到运行：{run_id}")
        remove_tree(root)

    def purge(self) -> int:
        count = 0
        for child in self.runs_dir.iterdir():
            if child.is_dir():
                remove_tree(child)
                count += 1
        return count


def fingerprint(*parts: Any) -> str:
    """把任意可 JSON 化的输入折成一个稳定的 hash，用于缓存判定。"""
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return sha256_text(payload)[:16]



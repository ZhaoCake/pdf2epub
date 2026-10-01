"""输入处理：探测 PDF 特性、决定是否需要 OCR、按官方限制切分。

为什么需要这一步：
- 官方 API 限制单文件 ≤200MB / ≤200 页，超限必须切分，否则整本书直接失败。
- 扫描版必须开 OCR（``is_ocr=true``），否则解析结果为空。自动探测比让用户
  自己判断可靠得多。
- 切分后仍需把 chunk 内页号映射回原书页号，供复核时定位。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import MineruConfig
from .errors import InputError
from .logutil import get_logger

log = get_logger("ingest")

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import PdfReadError

    HAS_PYPDF = True
except ModuleNotFoundError:  # pragma: no cover
    HAS_PYPDF = False
    PdfReadError = Exception  # type: ignore[assignment,misc]


#: 采样页的平均字符数低于此值，判定为扫描版（无文本层）
TEXT_LAYER_MIN_CHARS = 40
SAMPLE_PAGES = 12


@dataclass
class Chunk:
    """一个待提交给 MinerU 的子文件。"""

    chunk_id: str
    path: Path
    page_start: int   # 0 基，含
    page_end: int     # 0 基，含
    is_ocr: bool = False
    byte_size: int = 0

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1

    @property
    def page_ranges(self) -> str:
        """MinerU 的页码范围参数（1 基，闭区间）。"""
        return f"{self.page_start + 1}-{self.page_end + 1}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "path": str(self.path),
            "page_start": self.page_start,
            "page_end": self.page_end,
            "page_count": self.page_count,
            "is_ocr": self.is_ocr,
            "byte_size": self.byte_size,
        }


@dataclass
class PdfProfile:
    path: Path
    page_count: int = 0
    byte_size: int = 0
    encrypted: bool = False
    has_text_layer: bool = False
    sampled_chars_per_page: float = 0.0
    title: str = ""
    author: str = ""
    producer: str = ""
    outline: list[tuple[int, str, int]] = field(default_factory=list)  # (level, title, page_no 1基)
    width: float = 0.0
    height: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def is_scanned(self) -> bool:
        return not self.has_text_layer

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["path"] = str(self.path)
        data["is_scanned"] = self.is_scanned
        return data


def inspect_pdf(path: Path, *, sample_pages: int = SAMPLE_PAGES) -> PdfProfile:
    """读取 PDF 元信息、探测加密与文本层、抽取书签目录。"""
    path = Path(path)
    profile = PdfProfile(path=path, byte_size=path.stat().st_size)

    if not HAS_PYPDF:
        profile.errors.append("未安装 pypdf，无法探测页数与文本层")
        log.warning("未安装 pypdf，跳过 PDF 探测（切分与 OCR 自动判定将不可用）")
        return profile

    try:
        reader = PdfReader(str(path))
    except PdfReadError as exc:
        raise InputError(f"PDF 无法解析，文件可能损坏：{path} -> {exc}") from exc

    if reader.is_encrypted:
        profile.encrypted = True
        try:
            # 空密码是 PDF 里最常见的"伪加密"
            if reader.decrypt("") == 0:
                raise InputError(
                    f"PDF 已加密且需要密码，无法自动处理：{path}",
                    detail={"hint": "请先用 qpdf --decrypt 或类似工具去除密码"},
                )
            profile.encrypted = False
            profile.errors.append("PDF 使用空密码加密，已自动解密")
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f"PDF 加密处理失败：{path} -> {exc}") from exc

    try:
        profile.page_count = len(reader.pages)
    except Exception as exc:
        raise InputError(f"无法读取 PDF 页数：{path} -> {exc}") from exc

    if profile.page_count == 0:
        raise InputError(f"PDF 没有任何页面：{path}")

    _read_metadata(reader, profile)
    _read_outline(reader, profile)
    _read_page_geometry(reader, profile)
    _detect_text_layer(reader, profile, sample_pages=sample_pages)

    log.info(
        "PDF 探测：%d 页 / %.1f MB / %s / 文本层=%s / 书签 %d 条",
        profile.page_count,
        profile.byte_size / 1e6,
        "加密" if profile.encrypted else "未加密",
        "有" if profile.has_text_layer else "无",
        len(profile.outline),
    )
    return profile


def _read_metadata(reader: "PdfReader", profile: PdfProfile) -> None:
    try:
        meta = reader.metadata or {}
    except Exception:
        return
    profile.title = _clean_meta(meta.get("/Title"))
    profile.author = _clean_meta(meta.get("/Author"))
    profile.producer = _clean_meta(meta.get("/Producer"))


def _clean_meta(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # PDF 元数据里常见的 UTF-16 BOM 残留
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    return text


def _read_outline(reader: "PdfReader", profile: PdfProfile) -> None:
    try:
        toc = reader.outline
    except Exception:
        return
    flat: list[tuple[int, str, int]] = []

    def walk(items: Any, level: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            title = getattr(item, "title", None) or (item.get("title") if isinstance(item, dict) else None)
            if not title:
                continue
            try:
                page_no = reader.get_destination_page_number(item) + 1
            except Exception:
                continue
            flat.append((level, str(title).strip(), page_no))

    walk(toc, 1)
    profile.outline = flat


def _read_page_geometry(reader: "PdfReader", profile: PdfProfile) -> None:
    try:
        box = reader.pages[0].mediabox
        profile.width = float(box.width)
        profile.height = float(box.height)
    except Exception:
        profile.width = profile.height = 0.0


def _detect_text_layer(reader: "PdfReader", profile: PdfProfile, *, sample_pages: int) -> None:
    """采样若干页，统计平均字符数，据此判断是否扫描版。"""
    total = profile.page_count
    if total == 0:
        return

    if total <= sample_pages:
        indices = list(range(total))
    else:
        step = max(1, total // sample_pages)
        indices = list(range(0, total, step))[:sample_pages]
        # 补上中间与末尾，避免只看到封面/目录
        indices = sorted(set(indices + [total // 2, total - 1]))

    chars = 0
    ok_pages = 0
    for idx in indices:
        try:
            text = reader.pages[idx].extract_text() or ""
        except Exception:
            continue
        ok_pages += 1
        chars += len(text.strip())

    if ok_pages == 0:
        profile.has_text_layer = False
        profile.errors.append("无法从任何采样页提取文本，按扫描版处理")
        return

    profile.sampled_chars_per_page = chars / ok_pages
    profile.has_text_layer = profile.sampled_chars_per_page >= TEXT_LAYER_MIN_CHARS


def plan_chunks(profile: PdfProfile, config: MineruConfig, *, language: str = "ch") -> list[Chunk]:
    """按官方限制规划切分方案。未超限时返回单块。"""
    max_pages = max(1, config.max_pages_per_task)
    max_bytes = max(1, config.max_bytes_per_task)
    total = profile.page_count

    by_pages = math.ceil(total / max_pages)
    by_bytes = math.ceil(profile.byte_size / max_bytes) if profile.byte_size else 1
    parts = max(1, by_pages, by_bytes)

    per_part = math.ceil(total / parts)
    wants_ocr = _resolve_ocr(profile, config)
    chunks: list[Chunk] = []
    for index in range(parts):
        start = index * per_part
        if start >= total:
            break
        end = min(total - 1, start + per_part - 1)
        chunks.append(
            Chunk(
                chunk_id=f"c{index + 1:03d}",
                path=profile.path,  # 单块时直接复用原文件，不复制
                page_start=start,
                page_end=end,
                is_ocr=wants_ocr,
                byte_size=profile.byte_size,
            )
        )

    if parts > 1:
        log.info(
            "PDF 超出单任务限制（%d 页 / %.1f MB），切分为 %d 份",
            total,
            profile.byte_size / 1e6,
            len(chunks),
        )
    return chunks


def _resolve_ocr(profile: PdfProfile, config: MineruConfig) -> bool:
    """把 ``is_ocr = auto`` 解析成实际布尔值。"""
    setting = config.is_ocr
    if isinstance(setting, bool):
        return setting
    if setting == "true":
        return True
    if setting == "false":
        return False
    # auto：扫描版必开；有文本层则只在语言需要时开（这里保守地不开，加快速度）
    return profile.is_scanned


def materialize_chunks(
    chunks: list[Chunk],
    dest_dir: Path,
    source: Path,
    *,
    max_bytes: int = 200_000_000,
) -> list[Chunk]:
    """把切分计划落成实际文件。单块时直接返回原文件。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if len(chunks) == 1:
        chunks[0].path = Path(source)
        chunks[0].byte_size = Path(source).stat().st_size
        return chunks

    if not HAS_PYPDF:
        raise InputError(
            "PDF 超出官方 API 限制需要切分，但未安装 pypdf。请先 `pip install pypdf`。"
        )

    reader = PdfReader(str(source))
    stem = Path(source).stem
    for chunk in chunks:
        out_path = dest_dir / f"{stem}.{chunk.chunk_id}.pdf"
        if out_path.is_file() and out_path.stat().st_size > 0:
            chunk.path = out_path
            chunk.byte_size = out_path.stat().st_size
            continue
        writer = PdfWriter()
        for page_idx in range(chunk.page_start, chunk.page_end + 1):
            writer.add_page(reader.pages[page_idx])
        with out_path.open("wb") as fh:
            writer.write(fh)
        writer.close()
        chunk.path = out_path
        chunk.byte_size = out_path.stat().st_size
        log.info(
            "写出分片 %s（原书第 %d-%d 页，%.1f MB）",
            out_path.name,
            chunk.page_start + 1,
            chunk.page_end + 1,
            chunk.byte_size / 1e6,
        )

    oversized = [c for c in chunks if c.byte_size > max_bytes]
    if oversized:
        log.warning(
            "有 %d 个分片仍超出字节上限（%.1f MB > %.1f MB），MinerU 可能拒绝",
            len(oversized),
            max(c.byte_size for c in oversized) / 1e6,
            max_bytes / 1e6,
        )
    return chunks

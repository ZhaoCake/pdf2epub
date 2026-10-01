"""MinerU 产物包（官方 API 返回的 zip）的解压与定位。

**本模块刻意不做任何加工**：解压出来的东西原样保留，脚本只负责"哪些文件是
markdown / 哪些是分段 JSON / 图片在哪"。怎么理解这些内容由 LLM 负责。

MinerU 的输出文件名在版本之间变过好几轮：

- 1.x/2.x：``auto/<name>_model.json``、``auto/<name>_content_list.json``、
  ``auto/<name>.md``、``auto/images/``
- 3.x 云 API：``full.md``、``*_model.json``、``*_content_list.json``、``images/``
- 4.0：``markdown.md``、``middle_json.json``、``model_output.json``、``images/``

所以这里不假设任何一种布局，而是**递归按模式搜索**。上游改文件名时，
流水线不会整体失效。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import ArtifactError
from .logutil import get_logger
from .runstate import remove_tree

log = get_logger("bundle")

MD_PATTERNS = ("full.md", "markdown.md", "*.md")
CONTENT_PATTERNS = ("*content_list*.json", "content_list.json")
MODEL_PATTERNS = ("*_model.json", "model_output.json", "model.json")
MIDDLE_PATTERNS = ("*middle*.json", "middle_json.json", "layout.json")
IMAGE_DIR_NAMES = {"images", "image", "img"}


@dataclass
class Bundle:
    """一次解析（一个 chunk）的产物集合。"""

    root: Path
    markdown: Path | None = None
    content_list: Path | None = None
    model: Path | None = None
    middle: Path | None = None
    images_dir: Path | None = None
    all_files: list[Path] = field(default_factory=list)
    #: 没被认出来的 JSON。MinerU 换个文件名时，靠它兜底而不是直接失败。
    other_json: list[Path] = field(default_factory=list)

    @property
    def json_sources(self) -> list[Path]:
        """所有可能带置信度信息的 JSON，按可信度排序。"""
        known = [p for p in (self.content_list, self.model, self.middle) if p is not None]
        return known + list(self.other_json)

    def describe(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "markdown": _name(self.markdown),
            "content_list": _name(self.content_list),
            "model": _name(self.model),
            "middle": _name(self.middle),
            "images_dir": str(self.images_dir) if self.images_dir else None,
            "other_json": [p.name for p in self.other_json],
            "file_count": len(self.all_files),
        }


def _name(path: Path | None) -> str | None:
    return path.name if path else None


# ---------------------------------------------------------------------------
# 解压
# ---------------------------------------------------------------------------


def extract_zip(zip_path: Path, dest_dir: Path, *, force: bool = False) -> Path:
    """解压结果 zip。已解压过则复用（幂等，支持断点续跑）。"""
    zip_path = Path(zip_path)
    dest_dir = Path(dest_dir)

    marker = dest_dir / ".extracted"
    if marker.is_file() and not force:
        log.debug("已解压，跳过：%s", dest_dir.name)
        return dest_dir

    if dest_dir.exists() and force:
        remove_tree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, dest_dir)
    except zipfile.BadZipFile as exc:
        remove_tree(dest_dir)
        raise ArtifactError(f"解析结果不是合法 zip：{zip_path} -> {exc}") from exc

    marker.write_text("ok", encoding="utf-8")
    return dest_dir


def _safe_extract(archive: zipfile.ZipFile, dest_dir: Path) -> None:
    """防目录穿越。上游返回的 zip 也应视为不可信输入。"""
    base = dest_dir.resolve()
    for member in archive.infolist():
        target = (dest_dir / member.filename).resolve()
        if not str(target).startswith(str(base)):
            raise ArtifactError(f"解析结果 zip 含非法路径：{member.filename}")
    archive.extractall(dest_dir)


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------


def _first_match(root: Path, patterns: Iterable[str], files: list[Path]) -> Path | None:
    """按模式顺序查找；同名时优先层级浅的（避免抓到嵌套副本）。"""
    for pattern in patterns:
        hits = sorted(
            (p for p in files if p.match(pattern)),
            key=lambda p: (len(p.relative_to(root).parts), str(p)),
        )
        if hits:
            return hits[0]
    return None


def load_bundle(root: Path) -> Bundle:
    """在一个解压目录里定位各类产物。"""
    root = Path(root)
    if not root.is_dir():
        raise ArtifactError(f"解析产物目录不存在：{root}")

    files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
    bundle = Bundle(root=root, all_files=files)
    bundle.markdown = _first_match(root, MD_PATTERNS, files)
    bundle.content_list = _first_match(root, CONTENT_PATTERNS, files)
    bundle.model = _first_match(root, MODEL_PATTERNS, files)
    bundle.middle = _first_match(root, MIDDLE_PATTERNS, files)

    image_dirs = [p for p in root.rglob("*") if p.is_dir() and p.name.lower() in IMAGE_DIR_NAMES]
    if image_dirs:
        bundle.images_dir = sorted(image_dirs, key=lambda p: (len(p.relative_to(root).parts), str(p)))[0]

    claimed = {bundle.markdown, bundle.content_list, bundle.model, bundle.middle}
    bundle.other_json = sorted(
        p
        for p in files
        if p.suffix.lower() == ".json" and p not in claimed
    )

    # 只要还有任何可用的东西就继续跑。真的什么都没有才失败——
    # 宁可让 LLM 从页面图里硬啃，也不要因为"文件名不认识"就整单失败。
    if not bundle.markdown and not bundle.other_json and bundle.content_list is None and bundle.model is None:
        if bundle.images_dir is None:
            raise ArtifactError(
                f"解析产物里既没有 Markdown、JSON，也没有图片：{root}",
                detail={"files": [p.name for p in files[:20]]},
            )

    log.info("产物包定位完成：%s", bundle.describe())
    return bundle


def read_json(path: Path | None) -> Any:
    if path is None:
        return None
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"JSON 解析失败：{path} -> {exc}") from exc
    except OSError as exc:
        raise ArtifactError(f"JSON 读取失败：{path} -> {exc}") from exc


def read_markdown(path: Path | None) -> str:
    """读 Markdown，容忍上游不是 UTF-8。"""
    if path is None:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return Path(path).read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return Path(path).read_text(encoding="utf-8", errors="replace")


def copy_images(bundles: list[Bundle], dest_dir: Path) -> dict[str, str]:
    """把多个 chunk 的图片平铺到一个目录，同内容去重。

    刻意**不缩放、不转码**——图片不是本次要"校准"的东西，原样搬运最省事。

    Returns:
        原始相对路径 -> 目标文件名 的映射。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    seen: dict[str, str] = {}

    for bundle in bundles:
        if not bundle.images_dir:
            continue
        for image in sorted(bundle.images_dir.rglob("*")):
            if not image.is_file():
                continue
            relative = image.relative_to(bundle.root).as_posix()
            if image.name in seen:
                mapping[relative] = seen[image.name]
                continue
            target = dest_dir / image.name
            index = 2
            while target.exists() and target.stat().st_size != image.stat().st_size:
                target = dest_dir / f"{image.stem}_{index}{image.suffix}"
                index += 1
            if not target.exists():
                shutil.copy2(image, target)
            seen[image.name] = target.name
            mapping[relative] = target.name

    return mapping

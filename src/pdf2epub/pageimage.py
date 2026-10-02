"""把 PDF 页渲染成图片，交给多模态 LLM 对照。

这是整个流程里 LLM 的"眼睛"：光看解析文本没法判断它认错了什么，
把对应页（以及前后各一页）渲染出来，LLM 才能回到原始版面上做判断。

渲染用 pypdfium2（纯 wheel，无外部依赖），缩放裁剪用 Pillow。
两者都是可选依赖，缺失时退化为"纯文本校准"并告警。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DependencyMissing
from .logutil import get_logger

log = get_logger("pageimage")

try:
    import pypdfium2 as pdfium

    HAS_PDFIUM = True
except ModuleNotFoundError:  # pragma: no cover
    pdfium = None  # type: ignore[assignment]
    HAS_PDFIUM = False

try:
    from PIL import Image

    HAS_PIL = True
except ModuleNotFoundError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    HAS_PIL = False


def available() -> bool:
    return bool(HAS_PDFIUM and HAS_PIL)


@dataclass
class RenderedPage:
    """渲染结果及其坐标换算所需的元信息。"""

    page_idx: int
    image: Any                      # PIL.Image
    scale: float                    # 像素 / PDF point
    pdf_width: float
    pdf_height: float

    def to_pixel_bbox(self, bbox: tuple[float, float, float, float], padding: float = 0.0) -> tuple[int, int, int, int]:
        left = max(0, int((bbox[0] - padding) * self.scale))
        top = max(0, int((bbox[1] - padding) * self.scale))
        right = min(self.image.width, int((bbox[2] + padding) * self.scale) + 1)
        bottom = min(self.image.height, int((bbox[3] + padding) * self.scale) + 1)
        return left, top, right, bottom


def render_page(
    pdf_path: Path,
    page_idx: int,
    *,
    dpi: int = 140,
    max_width: int = 1500,
    require: bool = False,
) -> RenderedPage | None:
    """渲染 PDF 的指定页。缺依赖或渲染失败时返回 None（除非 require=True）。"""
    if not available():
        if require:
            raise DependencyMissing("页面渲染需要 pypdfium2 与 Pillow：pip install pypdfium2 Pillow")
        return None

    pdf_path = Path(pdf_path)
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        if require:
            raise DependencyMissing(f"无法打开 PDF 用于渲染：{pdf_path} -> {exc}") from exc
        log.warning("渲染失败，跳过页面图：%s", exc)
        return None

    try:
        if page_idx < 0 or page_idx >= len(document):
            if require:
                raise DependencyMissing(f"页号越界：{page_idx}（共 {len(document)} 页）")
            return None

        page = document[page_idx]
        try:
            pdf_width, pdf_height = page.get_size()
        except Exception:
            pdf_width, pdf_height = 0.0, 0.0

        scale = dpi / 72.0
        image = page.render(scale=scale).to_pil()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        if max_width and image.width > max_width:
            ratio = max_width / image.width
            image = image.resize((max_width, max(1, int(image.height * ratio))), Image.LANCZOS)
            scale *= ratio

        return RenderedPage(
            page_idx=page_idx,
            image=image,
            scale=scale,
            pdf_width=pdf_width,
            pdf_height=pdf_height,
        )
    except Exception as exc:
        if require:
            raise DependencyMissing(f"渲染第 {page_idx} 页失败：{exc}") from exc
        log.warning("渲染第 %d 页失败：%s", page_idx, exc)
        return None
    finally:
        try:
            document.close()
        except Exception:
            pass


def crop(
    page: RenderedPage,
    bbox: tuple[float, float, float, float],
    *,
    padding: float = 12.0,
    max_width: int = 1100,
) -> Any:
    """按 PDF 坐标裁剪出问题区域。"""
    if not HAS_PIL:
        raise DependencyMissing("裁剪需要 Pillow：pip install Pillow")
    left, top, right, bottom = page.to_pixel_bbox(bbox, padding)
    if right - left < 4 or bottom - top < 4:
        left, top = max(0, left - 8), max(0, top - 8)
        right = min(page.image.width, right + 8)
        bottom = min(page.image.height, bottom + 8)
    cropped = page.image.crop((left, top, max(left + 1, right), max(top + 1, bottom)))
    if max_width and cropped.width > max_width:
        ratio = max_width / cropped.width
        cropped = cropped.resize((max_width, max(1, int(cropped.height * ratio))), Image.LANCZOS)
    return cropped


def save_png(image: Any, path: Path, *, optimize: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=optimize)
    return path

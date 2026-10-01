"""打包：把 ``build/`` 目录变成合规的 EPUB 文件。

**这里只做机械活**：扫描目录、生成 OPF/nav/container、按 EPUB 要求压 zip。
内容（章节 XHTML 与书目）是 LLM 直接写的，本模块不对它做任何改写。

不生成 ``toc.ncx``：那是 EPUB2 的遗留物，EPUB3 只用 ``nav.xhtml``。
少写一份、少一处不一致。
"""

from __future__ import annotations

import datetime
import json
import re
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from .errors import BuildError
from .logutil import get_logger
from .runstate import remove_tree

log = get_logger("epubpack")

MIMETYPE = "application/epub+zip"
#: 文档里出现它就说明用了 MathML，manifest 必须声明 properties="mathml"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
CONTAINER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

MEDIA_TYPES = {
    ".xhtml": "application/xhtml+xml",
    ".html": "application/xhtml+xml",
    ".htm": "application/xhtml+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".jp2": "image/jp2",
    ".css": "text/css",
    ".js": "text/javascript",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

COVER_PAGE = """\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">
  <head>
    <title>{title}</title>
    <link rel="stylesheet" type="text/css" href="../style/base.css"/>
  </head>
  <body>
    <div style="text-align:center; margin:0; padding:0;">
      <img src="../{image}" alt="{alt}" style="max-width:100%; max-height:100%;"/>
    </div>
  </body>
</html>
"""


@dataclass
class BookSpec:
    """``book.json`` 解析后的书目信息。"""

    title: str = ""
    author: str = ""
    language: str = "zh"
    publisher: str = ""
    description: str = ""
    identifier: str = ""
    cover: str = ""
    spine: list[str] = field(default_factory=list)
    toc: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "publisher": self.publisher,
            "description": self.description,
            "identifier": self.identifier,
            "cover": self.cover,
            "spine": list(self.spine),
            "toc": list(self.toc),
            "warnings": list(self.warnings),
        }


def load_book(build_dir: Path, *, fallback_identifier: str = "") -> BookSpec:
    """读 ``book.json``。缺字段就用默认值，不因为"不完整"报错。"""
    path = Path(build_dir) / "book.json"
    if not path.is_file():
        raise BuildError(
            f"找不到 {path}。撰写阶段需要先写好书目文件（见 BRIEF.md）。",
            detail={"expected": str(path)},
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BuildError(f"book.json 不是合法 JSON：{exc}") from exc

    if not isinstance(raw, dict):
        raise BuildError("book.json 的顶层必须是对象")

    def text(key: str) -> str:
        value = raw.get(key)
        return str(value).strip() if isinstance(value, (str, int, float)) else ""

    book = BookSpec(
        title=text("title"),
        author=text("author"),
        language=text("language") or "zh",
        publisher=text("publisher"),
        description=text("description"),
        identifier=text("identifier"),
        cover=text("cover").lstrip("./"),
        raw=raw,
    )

    spine = raw.get("spine")
    if isinstance(spine, list):
        book.spine = [str(item).replace("\\", "/").lstrip("./") for item in spine if str(item).strip()]

    toc = raw.get("toc")
    if isinstance(toc, list):
        book.toc = [entry for entry in toc if isinstance(entry, dict)]

    book.identifier = _sane_identifier(book, fallback_identifier)
    return book


def _sane_identifier(book: BookSpec, fallback_identifier: str) -> str:
    """保证标识符合法。

    ``urn:uuid:`` 前缀会被 EPUBCheck 当作"这必须是个 UUID"来检查（OPF-085）。
    LLM 随手写 ``urn:uuid:demo`` 很常见，与其让校验来回报错，不如脚本直接纠正——
    标识符格式是包装层的事，不是内容的事。
    """
    value = book.identifier.strip()
    if value.startswith("urn:uuid:"):
        candidate = value[len("urn:uuid:") :]
        try:
            uuid.UUID(candidate)
            return value
        except ValueError:
            book.warnings.append(
                f"identifier {value!r} 不是合法 UUID，已替换为按内容生成的稳定标识符"
            )
    elif value and ":" in value:
        return value  # 其它 URN/URI 形式原样保留
    elif value:
        return value

    seed = fallback_identifier or f"{book.title}|{book.author}|{book.language}"
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


# ---------------------------------------------------------------------------
# 扫描目录
# ---------------------------------------------------------------------------


@dataclass
class ManifestItem:
    item_id: str
    href: str
    media_type: str
    properties: str = ""


def _walk(oebps: Path) -> list[ManifestItem]:
    items: list[ManifestItem] = []
    used: set[str] = set()

    def unique_id(base: str) -> str:
        candidate = base
        index = 2
        while candidate in used:
            candidate = f"{base}-{index}"
            index += 1
        used.add(candidate)
        return candidate

    for path in sorted(oebps.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(oebps).as_posix()
        if relative.startswith("content.opf") or relative == "nav.xhtml":
            continue
        media = MEDIA_TYPES.get(path.suffix.lower())
        if media is None:
            log.warning("跳过无法识别类型的文件：%s", relative)
            continue
        base = path.stem if path.parent == oebps else f"{path.parent.relative_to(oebps).as_posix().replace('/', '-')}-{path.stem}"
        items.append(
            ManifestItem(
                item_id=unique_id(base),
                href=relative,
                media_type=media,
                properties=_detect_properties(path, media),
            )
        )
    return items


def _detect_properties(path: Path, media_type: str) -> str:
    """认出需要写进 manifest 的 properties。

    EPUB 3 规定：文档里用了 MathML 就得声明 ``mathml``，否则 EPUBCheck 报 OPF-014。
    这是纯机械的探测，不该指望作者记得手写。
    """
    if media_type != "application/xhtml+xml":
        return ""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:  # pragma: no cover
        return ""
    if MATHML_NS in head or re.search(r"<math[\s>]", head):
        return "mathml"
    return ""


def _spine_order(book: BookSpec, items: list[ManifestItem]) -> list[ManifestItem]:
    by_href = {item.href: item for item in items}
    ordered: list[ManifestItem] = []
    seen: set[str] = set()

    for href in book.spine:
        item = by_href.get(href)
        if item is None:
            book.warnings.append(f"spine 里的 {href} 在 OEBPS 下不存在，已忽略")
            continue
        if item.media_type != "application/xhtml+xml":
            book.warnings.append(f"spine 里的 {href} 不是 XHTML，已忽略")
            continue
        ordered.append(item)
        seen.add(href)

    missing = sorted(
        item.href
        for item in items
        if item.media_type == "application/xhtml+xml"
        and item.href not in seen
        and "nav" not in item.properties
    )
    if missing:
        book.warnings.append(f"有 {len(missing)} 个 XHTML 不在 spine 中，已按文件名补在后面")
        ordered.extend(by_href[href] for href in missing)

    return ordered


# ---------------------------------------------------------------------------
# 生成包内文件
# ---------------------------------------------------------------------------


def _cover_page(book: BookSpec) -> str:
    return COVER_PAGE.format(
        lang=xml_escape(book.language or "zh"),
        title=xml_escape(book.title or "Cover"),
        image=xml_escape(book.cover),
        alt=xml_escape(book.title or "封面"),
    )


def _nest_toc(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把扁平的 (level, title, href) 列表转成嵌套树。

    不信任 level 的连续性：跳跃、回退、乱序都不该让导航文档变成非法 XML。
    """
    root: list[dict[str, Any]] = []
    stack: list[tuple[int, list[dict[str, Any]]]] = [(0, root)]
    for entry in entries:
        level = max(1, int(entry.get("level") or 1))
        while len(stack) > 1 and stack[-1][0] >= level:
            stack.pop()
        node = {
            "title": str(entry.get("title") or "").strip(),
            "href": str(entry.get("href") or "").strip(),
            "children": [],
        }
        stack[-1][1].append(node)
        stack.append((level, node["children"]))
    return root


def _render_toc(nodes: list[dict[str, Any]], indent: int = 4) -> list[str]:
    pad = " " * indent
    lines: list[str] = [f"{pad}<ol>"]
    for node in nodes:
        title = xml_escape(node["title"] or node["href"] or "未命名")
        href = xml_escape(node["href"])
        lines.append(f"{pad}  <li>")
        lines.append(f'{pad}    <a href="{href}">{title}</a>')
        if node["children"]:
            lines.extend(_render_toc(node["children"], indent + 4))
        lines.append(f"{pad}  </li>")
    lines.append(f"{pad}</ol>")
    return lines


def _landmarks(spine: list[ManifestItem], cover_href: str = "") -> list[tuple[str, str, str]]:
    """书签导航。

    只放**确实在 spine 里**的目标——landmark 指向非 spine 项会被 EPUBCheck
    判为 RSC-011（"引用了不是 spine 项的资源"）。导航文档自身就不在 spine 里，
    所以"目录"这一条不能指向 nav.xhtml。
    """
    entries: list[tuple[str, str, str]] = []
    if cover_href:
        entries.append(("cover", cover_href, "封面"))
    body = next((item.href for item in spine if item.href != cover_href), "")
    if body:
        entries.append(("bodymatter", body, "正文"))
    return entries


def _nav_xhtml(book: BookSpec, entries: list[dict[str, Any]], spine: list[ManifestItem], cover_href: str) -> str:
    lang = xml_escape(book.language or "zh")
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!DOCTYPE html>",
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{lang}" lang="{lang}">',
        "  <head>",
        "    <title>目录</title>",
        '    <link rel="stylesheet" type="text/css" href="style/base.css"/>',
        "  </head>",
        "  <body>",
        '    <nav epub:type="toc" id="toc">',
        "      <h1>目录</h1>",
    ]
    lines.extend(_render_toc(_nest_toc(entries), indent=6))
    lines.append("    </nav>")

    landmarks = _landmarks(spine, cover_href)
    if landmarks:
        lines.extend(
            [
                '    <nav epub:type="landmarks" id="landmarks" hidden="hidden">',
                "      <ol>",
            ]
        )
        for kind, href, label in landmarks:
            lines.append(
                f'        <li><a epub:type="{kind}" href="{xml_escape(href)}">'
                f"{xml_escape(label)}</a></li>"
            )
        lines.extend(["      </ol>", "    </nav>"])

    lines.extend(["  </body>", "</html>", ""])
    return "\n".join(lines)


def _tic_toc(book: BookSpec, spine: list[ManifestItem]) -> list[dict[str, Any]]:
    if book.toc:
        return [
            {
                "title": str(entry.get("title") or "").strip() or Path(str(entry.get("href") or "")).stem,
                "href": str(entry.get("href") or "").replace("\\", "/").lstrip("./"),
                "level": max(1, int(entry.get("level") or 1)),
            }
            for entry in book.toc
            if str(entry.get("href") or "").strip()
        ]
    return [{"title": Path(item.href).stem, "href": item.href, "level": 1} for item in spine]


def _opf(book: BookSpec, items: list[ManifestItem], spine: list[ManifestItem], modified: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id" '
        'xml:lang="%s">' % xml_escape(book.language or "zh"),
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
        f'    <dc:identifier id="pub-id">{xml_escape(book.identifier)}</dc:identifier>',
        f"    <dc:title>{xml_escape(book.title or 'Untitled')}</dc:title>",
        f"    <dc:language>{xml_escape(book.language or 'zh')}</dc:language>",
    ]
    if book.author:
        lines.append(f'    <dc:creator id="creator">{xml_escape(book.author)}</dc:creator>')
        lines.append('    <meta refines="#creator" property="role" scheme="marc:relators">aut</meta>')
    if book.publisher:
        lines.append(f"    <dc:publisher>{xml_escape(book.publisher)}</dc:publisher>")
    if book.description:
        lines.append(f"    <dc:description>{xml_escape(book.description)}</dc:description>")
    lines.append(f'    <meta property="dcterms:modified">{modified}</meta>')
    lines.extend(["  </metadata>", "  <manifest>"])

    for item in items:
        properties = f' properties="{item.properties}"' if item.properties else ""
        lines.append(
            f'    <item id="{xml_escape(item.item_id)}" href="{xml_escape(item.href)}" '
            f'media-type="{item.media_type}"{properties}/>'
        )

    lines.extend(["  </manifest>", "  <spine>"])
    for item in spine:
        lines.append(f'    <itemref idref="{xml_escape(item.item_id)}"/>')
    lines.extend(["  </spine>", "</package>", ""])
    return "\n".join(lines)


@dataclass
class PackResult:
    build_dir: Path
    book: BookSpec
    entries: int
    spine: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_dir": str(self.build_dir),
            "entries": self.entries,
            "spine": list(self.spine),
            "book": self.book.to_dict(),
            "warnings": list(self.warnings),
        }


def _content_modified(oebps: Path, generated: set[str]) -> str:
    """由**内容文件**的最后修改时间推出 ``dcterms:modified``。

    不能用 ``now()``：那样同样的内容两次打包会产出不同的字节，产物既没法比对
    也没法复现。用内容的 mtime 既贴合这个字段的语义（"这份出版物最后一次改动
    是什么时候"），又让打包变成幂等的。

    ``generated`` 里的文件是打包器自己生成的（OPF / nav / 自动封面），
    每次打包都会重写，必须排除，否则 mtime 永远等于"刚刚"。
    """
    newest = 0.0
    for path in oebps.rglob("*"):
        if not path.is_file():
            continue
        if path.relative_to(oebps).as_posix() in generated:
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:  # pragma: no cover - 并发删除等
            continue
    stamp = newest or time.time()
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_package(build_dir: Path, *, identifier_seed: str = "") -> PackResult:
    """扫描 ``build/``，生成 OPF/nav/container，压成 EPUB。"""
    build_dir = Path(build_dir)
    oebps = build_dir / "OEBPS"
    if not oebps.is_dir():
        raise BuildError(f"构建目录不完整，缺少 {oebps}")

    book = load_book(build_dir, fallback_identifier=identifier_seed)

    #: 由本函数生成、不算"内容"的文件，算 dcterms:modified 时要跳过
    generated: set[str] = set()

    # 封面页：只声明了封面图、而且没有人手写封面页时才生成
    if book.cover:
        cover_image = oebps / book.cover
        if not cover_image.is_file():
            book.warnings.append(f"封面图不存在：{book.cover}")
            book.cover = ""
        else:
            cover_href = "text/cover.xhtml"
            if not (oebps / cover_href).is_file():
                (oebps / cover_href).write_text(_cover_page(book), encoding="utf-8")
                generated.add(cover_href)

    items = _walk(oebps)
    if not items:
        raise BuildError("OEBPS 下没有任何可打包的文件")

    if book.cover:
        for item in items:
            if item.href == book.cover:
                item.properties = (item.properties + " cover-image").strip()

    spine = _spine_order(book, items)

    # 封面页排在最前
    if book.cover:
        cover_item = next((i for i in items if i.href == "text/cover.xhtml"), None)
        if cover_item is not None:
            spine = [cover_item] + [i for i in spine if i.href != cover_item.href]

    cover_href = "text/cover.xhtml" if book.cover and (oebps / "text" / "cover.xhtml").is_file() else ""
    nav_href = "nav.xhtml"
    nav_item = ManifestItem(item_id="nav", href=nav_href, media_type="application/xhtml+xml", properties="nav")
    (oebps / nav_href).write_text(
        _nav_xhtml(book, _tic_toc(book, spine), spine, cover_href), encoding="utf-8"
    )
    generated.update({nav_href, "content.opf"})

    modified = _content_modified(oebps, generated)
    (oebps / "content.opf").write_text(_opf(book, items + [nav_item], spine, modified), encoding="utf-8")

    meta_inf = build_dir / "META-INF"
    meta_inf.mkdir(parents=True, exist_ok=True)
    (meta_inf / "container.xml").write_text(CONTAINER_XML, encoding="utf-8")
    (build_dir / "mimetype").write_text(MIMETYPE, encoding="utf-8")

    return PackResult(build_dir=build_dir, book=book, entries=len(items) + 1, spine=[i.href for i in spine])


PACKAGE_ROOT = "OEBPS"
#: 只打进 EPUB 的目录。``build/`` 下还放着 BRIEF.md 之类的工作文件，
#: 它们是给 LLM 看的，不该出现在读者手里。
PACKAGED_PREFIXES = ("META-INF/", f"{PACKAGE_ROOT}/")


def _packable(build_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in build_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(build_dir).as_posix()
        if relative == "mimetype" or relative.startswith(PACKAGED_PREFIXES):
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(build_dir).as_posix())


def pack(build_dir: Path, output: Path) -> Path:
    """把构建树压成 EPUB。

    ``mimetype`` 必须是 zip 的第一项、且**不压缩**——这不是风格问题，
    是 EPUB 规范要求，读是读不出来的，写错了很多阅读器直接拒收。
    """
    build_dir = Path(build_dir)
    output = Path(output)
    mimetype = build_dir / "mimetype"
    if not mimetype.is_file():
        raise BuildError(f"缺少 {mimetype}；请先执行 build_package()")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        remove_tree(output)

    entries = _packable(build_dir)
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            MIMETYPE.encode("ascii"),
            compress_type=zipfile.ZIP_STORED,
        )
        for path in entries:
            relative = path.relative_to(build_dir).as_posix()
            if relative == "mimetype":
                continue
            archive.write(path, relative, compress_type=zipfile.ZIP_DEFLATED)

    log.info("EPUB 已打包：%s（%d 个条目，%.2f MB）", output.name, len(entries), output.stat().st_size / 1e6)
    return output

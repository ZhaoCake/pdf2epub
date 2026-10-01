"""EPUB 结构自检（纯 Python，不依赖 Java）。

为什么要有这个：EPUBCheck 是权威，但它需要 Java 且下载起来麻烦。在"每轮修复后
都要重新校验"的闭环里，一个能立刻跑的轻量检查能省掉大量等待。而且当 EPUBCheck
压根不存在时，自检是流水线**唯一**的合规保障——不能因为外部工具缺失就裸奔。

检查项按 EPUBCheck 的判定逻辑来，覆盖最常见的失败原因。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from lxml import etree

from .issues import Issue, LintReport
from .logutil import get_logger

log = get_logger("selflint")

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

REMOTE_SCHEMES = ("http://", "https://", "ftp://", "//")

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False, huge_tree=False)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def lint_epub(epub_path: Path, *, check_xhtml: bool = True) -> LintReport:
    """对一个已打包的 EPUB 做结构自检。"""
    epub_path = Path(epub_path)
    report = LintReport(source="selflint")

    if not epub_path.is_file():
        report.ran = False
        report.error = f"EPUB 不存在：{epub_path}"
        return report

    try:
        with zipfile.ZipFile(epub_path) as archive:
            _check_mimetype(archive, report)
            container = _check_container(archive, report)
            opf_path = _resolve_opf(archive, container, report)
            if opf_path is None:
                return report
            opf_root = _parse_xml(archive, opf_path, report, required=True)
            if opf_root is None:
                return report
            opf_dir = posixpath.dirname(opf_path)
            _check_opf(opf_root, opf_path, report)
            manifest, spine_ids = _collect_manifest(opf_root, opf_path, report)
            _check_files_exist(archive, manifest, opf_dir, report)
            _check_opf_ids(opf_root, report)
            _check_nav(manifest, report)
            _check_spine(spine_ids, manifest, report)
            _check_remote_resources(archive, manifest, opf_dir, report)

            if check_xhtml:
                _check_content_documents(archive, manifest, opf_dir, report)
    except zipfile.BadZipFile as exc:
        report.add(Issue("PKG-001", "FATAL", f"EPUB 不是合法的 zip：{exc}", source="selflint"))
    return report


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------


def _check_mimetype(archive: zipfile.ZipFile, report: LintReport) -> None:
    infos = archive.infolist()
    if not infos:
        report.add(Issue("PKG-006", "FATAL", "zip 为空", source="selflint"))
        return
    first = infos[0]
    if first.filename != "mimetype":
        report.add(
            Issue(
                "PKG-006",
                "FATAL",
                f"zip 的第一个条目必须是 mimetype，实际是 {first.filename!r}",
                path=first.filename,
                source="selflint",
            )
        )
    elif first.compress_type != zipfile.ZIP_STORED:
        report.add(
            Issue("PKG-006", "ERROR", "mimetype 条目必须是不压缩（STORED）存储", path="mimetype", source="selflint")
        )
    content = archive.read("mimetype").decode("ascii", errors="replace").strip() if "mimetype" in archive.namelist() else ""
    if content != "application/epub+zip":
        report.add(
            Issue(
                "PKG-006",
                "FATAL",
                f"mimetype 内容必须是 'application/epub+zip'，实际是 {content!r}",
                path="mimetype",
                source="selflint",
            )
        )


def _check_container(archive: zipfile.ZipFile, report: LintReport) -> str | None:
    if "META-INF/container.xml" not in archive.namelist():
        report.add(Issue("PKG-002", "FATAL", "缺少 META-INF/container.xml", path="META-INF/container.xml", source="selflint"))
        return None
    root = _parse_xml(archive, "META-INF/container.xml", report)
    if root is None:
        return None
    rootfiles = root.findall(f"{{{CONTAINER_NS}}}rootfiles/{{{CONTAINER_NS}}}rootfile")
    if not rootfiles:
        # 有些工具用默认命名空间之外的前缀写法，这里退一步用本地名找
        rootfiles = [el for el in root.iter() if etree.QName(el).localname == "rootfile"]
    if not rootfiles:
        report.add(Issue("PKG-003", "FATAL", "container.xml 里没有 rootfile", path="META-INF/container.xml", source="selflint"))
        return None
    full_path = rootfiles[0].get("full-path", "")
    media_type = rootfiles[0].get("media-type", "")
    if media_type != "application/oebps-package+xml":
        report.add(
            Issue(
                "PKG-003",
                "ERROR",
                f"rootfile 的 media-type 应为 application/oebps-package+xml，实际 {media_type!r}",
                path="META-INF/container.xml",
                source="selflint",
            )
        )
    if full_path not in archive.namelist():
        report.add(
            Issue("PKG-003", "FATAL", f"rootfile 指向的 {full_path!r} 在包内不存在", path="META-INF/container.xml", source="selflint")
        )
        return None
    return full_path


def _resolve_opf(archive: zipfile.ZipFile, container_path: str | None, report: LintReport) -> str | None:
    if container_path:
        return container_path
    candidates = [name for name in archive.namelist() if name.endswith(".opf")]
    if len(candidates) == 1:
        return candidates[0]
    report.add(Issue("PKG-003", "FATAL", "无法定位 OPF 文件", source="selflint"))
    return None


def _parse_xml(archive: zipfile.ZipFile, path: str, report: LintReport, *, required: bool = False) -> etree._Element | None:
    try:
        data = archive.read(path)
    except KeyError:
        if required:
            report.add(Issue("RSC-007", "ERROR", f"包内缺少文件：{path}", path=path, source="selflint"))
        return None
    try:
        return etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError as exc:
        report.add(
            Issue(
                "RSC-005",
                "FATAL",
                f"XML 解析失败：{exc.msg if hasattr(exc, 'msg') else exc}",
                path=path,
                line=getattr(exc, "lineno", 0) or 0,
                source="selflint",
            )
        )
        return None


def _check_opf(opf: etree._Element, opf_path: str, report: LintReport) -> None:
    if etree.QName(opf).namespace != OPF_NS:
        report.add(
            Issue("OPF-001", "FATAL", f"package 元素命名空间应为 {OPF_NS}", path=opf_path, source="selflint")
        )
    version = opf.get("version", "")
    if not version.startswith("3"):
        report.add(
            Issue("OPF-001", "WARNING", f"package[@version]={version!r}，本流水线按 EPUB 3 生成", path=opf_path, source="selflint")
        )

    unique_id = opf.get("unique-identifier", "")
    identifiers = [el for el in opf.iter(f"{{{DC_NS}}}identifier")]
    if not identifiers:
        report.add(Issue("OPF-001", "ERROR", "缺少 dc:identifier", path=opf_path, source="selflint"))
    else:
        if unique_id and not any(el.get("id") == unique_id for el in identifiers):
            report.add(
                Issue(
                    "OPF-001",
                    "ERROR",
                    f"unique-identifier={unique_id!r} 没有对应的 dc:identifier@id",
                    path=opf_path,
                    source="selflint",
                )
            )
        for element in identifiers:
            if not (element.text or "").strip():
                report.add(Issue("OPF-001", "ERROR", "dc:identifier 不能为空", path=opf_path, source="selflint"))

    for tag in ("title", "language"):
        elements = list(opf.iter(f"{{{DC_NS}}}{tag}"))
        if not elements:
            report.add(Issue("OPF-001", "ERROR", f"缺少 dc:{tag}", path=opf_path, source="selflint"))
        elif not any((el.text or "").strip() for el in elements):
            report.add(Issue("OPF-001", "ERROR", f"dc:{tag} 不能为空", path=opf_path, source="selflint"))

    modified = [
        el
        for el in opf.iter(f"{{{OPF_NS}}}meta")
        if (el.get("property") or "") == "dcterms:modified"
    ]
    if not modified:
        report.add(
            Issue(
                "OPF-001",
                "ERROR",
                "EPUB3 要求存在 <meta property=\"dcterms:modified\">",
                path=opf_path,
                source="selflint",
            )
        )
    else:
        for element in modified:
            value = (element.text or "").strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", value):
                report.add(
                    Issue(
                        "OPF-001",
                        "ERROR",
                        f"dcterms:modified 必须是 CCYY-MM-DDThh:mm:ssZ 格式，实际 {value!r}",
                        path=opf_path,
                        source="selflint",
                    )
                )

    # EPUB2 遗留写法：<meta name="..." content="..."/> 在 EPUB3 只允许 name="cover"
    for element in opf.iter(f"{{{OPF_NS}}}meta"):
        name = element.get("name")
        if name and name != "cover":
            report.add(
                Issue(
                    "OPF-001",
                    "WARNING",
                    f"EPUB3 中 <meta name={name!r}> 属于已废弃写法，建议改用 property",
                    path=opf_path,
                    source="selflint",
                )
            )


def _collect_manifest(
    opf: etree._Element, opf_path: str, report: LintReport
) -> tuple[dict[str, dict[str, str]], list[str]]:
    manifest: dict[str, dict[str, str]] = {}
    for item in opf.iter(f"{{{OPF_NS}}}item"):
        item_id = item.get("id", "")
        href = item.get("href", "")
        if not item_id or not href:
            report.add(Issue("OPF-003", "ERROR", "manifest item 必须同时有 id 和 href", path=opf_path, source="selflint"))
            continue
        if item_id in manifest:
            report.add(Issue("OPF-003", "ERROR", f"manifest 中 id 重复：{item_id}", path=opf_path, source="selflint"))
        manifest[item_id] = {
            "href": href,
            "media-type": item.get("media-type", ""),
            "properties": item.get("properties", ""),
        }

    seen_href: dict[str, str] = {}
    for item_id, item in manifest.items():
        key = item["href"]
        if key in seen_href:
            report.add(
                Issue("OPF-003", "ERROR", f"manifest 中同一资源被声明两次：{key}", path=opf_path, source="selflint")
            )
        seen_href[key] = item_id

    spine_ids: list[str] = []
    for itemref in opf.iter(f"{{{OPF_NS}}}itemref"):
        idref = itemref.get("idref", "")
        if idref:
            spine_ids.append(idref)
    return manifest, spine_ids


def _check_opf_ids(opf: etree._Element, report: LintReport) -> None:
    """同一个 XML 文档内 id 必须唯一。"""
    seen: dict[str, int] = {}
    for element in opf.iter():
        element_id = element.get("id")
        if element_id:
            seen[element_id] = seen.get(element_id, 0) + 1
    for element_id, count in seen.items():
        if count > 1:
            report.add(Issue("OPF-003", "ERROR", f"id 重复：{element_id}（出现 {count} 次）", source="selflint"))


def _check_files_exist(
    archive: zipfile.ZipFile, manifest: dict[str, dict[str, str]], opf_dir: str, report: LintReport
) -> None:
    names = set(archive.namelist())
    for item_id, item in manifest.items():
        href = item["href"]
        if href.startswith(REMOTE_SCHEMES):
            continue
        target = _resolve_href(opf_dir, href)
        if target not in names:
            report.add(
                Issue(
                    "RSC-007",
                    "ERROR",
                    f"manifest 项 {item_id} 引用的资源不存在：{href}",
                    path=target,
                    source="selflint",
                    detail={"item_id": item_id, "href": href},
                )
            )


def _check_nav(manifest: dict[str, dict[str, str]], report: LintReport) -> None:
    nav_items = [item for item in manifest.values() if "nav" in (item["properties"] or "").split()]
    if not nav_items:
        report.add(Issue("OPF-014", "ERROR", "缺少导航文档（manifest 中没有 properties=\"nav\" 的项）", source="selflint"))
    elif len(nav_items) > 1:
        report.add(Issue("OPF-014", "ERROR", f"存在 {len(nav_items)} 个导航文档，只允许一个", source="selflint"))


def _check_spine(spine_ids: Iterable[str], manifest: dict[str, dict[str, str]], report: LintReport) -> None:
    ids = list(spine_ids)
    if not ids:
        report.add(Issue("OPF-012", "ERROR", "spine 为空", source="selflint"))
    for idref in ids:
        if idref not in manifest:
            report.add(Issue("OPF-012", "ERROR", f"spine 的 idref={idref!r} 在 manifest 中不存在", source="selflint"))
    for item_id, item in manifest.items():
        media = item["media-type"]
        if media == "application/xhtml+xml" and item_id not in set(ids) and "nav" not in (item["properties"] or ""):
            report.add(
                Issue("OPF-012", "WARNING", f"XHTML 文档 {item['href']} 未在 spine 中，可能无法阅读", source="selflint")
            )


def _check_remote_resources(
    archive: zipfile.ZipFile, manifest: dict[str, dict[str, str]], opf_dir: str, report: LintReport
) -> None:
    for item in manifest.values():
        if item["href"].startswith(REMOTE_SCHEMES):
            report.add(
                Issue("RSC-006", "ERROR", f"manifest 引用了远程资源：{item['href']}", source="selflint")
            )


def _check_content_documents(
    archive: zipfile.ZipFile, manifest: dict[str, dict[str, str]], opf_dir: str, report: LintReport
) -> None:
    """逐个检查 XHTML 文档的良构性与基本约束。"""
    names = set(archive.namelist())
    for item in manifest.values():
        if item["media-type"] != "application/xhtml+xml":
            continue
        target = _resolve_href(opf_dir, item["href"])
        if target not in names:
            continue
        report.checked_files += 1
        root = _parse_xml(archive, target, report)
        if root is None:
            continue
        _check_xhtml_document(archive, root, target, opf_dir, names, report)


def _check_xhtml_document(
    archive: zipfile.ZipFile,
    root: etree._Element,
    path: str,
    opf_dir: str,
    names: set[str],
    report: LintReport,
) -> None:
    qname = etree.QName(root)
    if qname.localname != "html":
        report.add(Issue("RSC-005", "ERROR", f"XHTML 根元素应为 html，实际 {qname.localname}", path=path, source="selflint"))
        return
    if qname.namespace != XHTML_NS:
        report.add(
            Issue(
                "RSC-005",
                "ERROR",
                f"XHTML 根元素必须在命名空间 {XHTML_NS} 内",
                path=path,
                source="selflint",
            )
        )

    heads = [el for el in root.iter() if etree.QName(el).localname == "head"]
    bodies = [el for el in root.iter() if etree.QName(el).localname == "body"]
    if not heads:
        report.add(Issue("RSC-005", "ERROR", "缺少 <head>", path=path, source="selflint"))
    else:
        titles = [el for el in heads[0] if etree.QName(el).localname == "title"]
        if not titles or not (titles[0].text or "").strip():
            report.add(Issue("RSC-005", "ERROR", "<head> 中缺少非空的 <title>", path=path, source="selflint"))
    if not bodies:
        report.add(Issue("RSC-005", "ERROR", "缺少 <body>", path=path, source="selflint"))

    # img 必须有 alt
    for element in root.iter():
        local = etree.QName(element).localname
        if local == "img" and element.get("alt") is None:
            report.add(
                Issue("ACC-001", "ERROR", f"<img> 缺少 alt 属性：src={element.get('src','')!r}", path=path, source="selflint")
            )

    # 文档内 id 唯一
    seen: dict[str, int] = {}
    for element in root.iter():
        element_id = element.get("id")
        if element_id:
            seen[element_id] = seen.get(element_id, 0) + 1
    for element_id, count in seen.items():
        if count > 1:
            report.add(Issue("RSC-005", "ERROR", f"文档内 id 重复：{element_id}", path=path, source="selflint"))

    # 本地引用必须存在；远程引用不允许
    for element in root.iter():
        for attr in ("src", "href"):
            value = element.get(attr)
            if not value:
                continue
            line = int(getattr(element, "sourceline", 0) or 0)
            if value.startswith(REMOTE_SCHEMES):
                report.add(
                    Issue(
                        "RSC-006",
                        "ERROR",
                        f"引用了远程资源：{value}",
                        path=path,
                        line=line,
                        source="selflint",
                    )
                )
                continue
            if attr == "href" and value.startswith(("mailto:", "tel:")):
                continue
            clean = value.split("#", 1)[0]
            if not clean:
                continue
            target = _resolve_href(posixpath.dirname(path), clean)
            if target not in names:
                report.add(
                    Issue(
                        "RSC-007",
                        "ERROR",
                        f"引用的资源不存在：{value}",
                        path=path,
                        line=line,
                        source="selflint",
                    )
                )

    # epub:type 属性必须有对应的 epub 前缀声明（我们只在 html 上声明）
    if any("epub:" in name for name in _attribute_names(root)):
        if not root.get(f"{{{EPUB_NS}}}type") and "epub" not in (root.nsmap or {}) and not any(
            el.get("epub:type") for el in root.iter()
        ):
            report.add(
                Issue("RSC-005", "WARNING", "使用了 epub: 前缀但可能未声明命名空间", path=path, source="selflint")
            )


def _attribute_names(root: etree._Element) -> list[str]:
    names: list[str] = []
    for element in root.iter():
        names.extend(element.attrib.keys())
    return names


def _resolve_href(base_dir: str, href: str) -> str:
    """把相对 href 解析为 zip 内的绝对路径。"""
    raw = unquote(urlparse(href).path.replace("\\", "/"))
    if raw.startswith("/"):
        return raw.lstrip("/")
    joined = posixpath.normpath(posixpath.join(base_dir, raw))
    return "" if joined == "." else joined



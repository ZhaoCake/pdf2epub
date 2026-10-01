"""打包：book.json -> OPF/nav/container -> 合法的 EPUB。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from lxml import etree

from pdf2epub import epubpack, selflint
from pdf2epub.errors import BuildError

OPF_NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}

CHAPTER = """\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh" lang="zh">
  <head><title>{title}</title><link rel="stylesheet" type="text/css" href="../style/base.css"/></head>
  <body><h1>{title}</h1><p>{body}</p></body>
</html>
"""


def _tree(tmp_path: Path, *, chapters: int = 2, book: dict | None = None) -> Path:
    build = tmp_path / "build"
    text = build / "OEBPS" / "text"
    (build / "OEBPS" / "style").mkdir(parents=True)
    text.mkdir(parents=True)
    (build / "OEBPS" / "style" / "base.css").write_text("body{margin:1em}", encoding="utf-8")
    for index in range(1, chapters + 1):
        (text / f"ch{index:03d}.xhtml").write_text(
            CHAPTER.format(title=f"第 {index} 章", body=f"正文 {index}。"), encoding="utf-8"
        )
    payload = {
        "title": "测试书",
        "author": "作者",
        "language": "zh",
        "identifier": "urn:uuid:fixed-identifier",
    }
    payload.update(book or {})
    (build / "book.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return build


def test_build_package_generates_opf_nav_container(tmp_path: Path):
    build = _tree(tmp_path)
    result = epubpack.build_package(build)

    assert result.entries >= 3
    assert (build / "META-INF" / "container.xml").is_file()
    assert (build / "mimetype").read_text(encoding="utf-8") == epubpack.MIMETYPE

    opf = etree.fromstring((build / "OEBPS" / "content.opf").read_bytes())
    assert opf.get("version") == "3.0"
    assert opf.get("unique-identifier") == "pub-id"
    titles = [e.text for e in opf.findall(".//dc:title", OPF_NS)]
    assert titles == ["测试书"]
    metas = [m.get("property") for m in opf.findall(".//opf:meta", OPF_NS)]
    assert "dcterms:modified" in metas

    nav_item = next(
        i for i in opf.findall(".//opf:item", OPF_NS) if "nav" in (i.get("properties") or "")
    )
    assert nav_item.get("href") == "nav.xhtml"

    spine = [i.get("idref") for i in opf.findall(".//opf:spine/opf:itemref", OPF_NS)]
    assert len(spine) == 2


def test_pack_passes_selflint(tmp_path: Path):
    build = _tree(tmp_path)
    epubpack.build_package(build)
    output = epubpack.pack(build, tmp_path / "out.epub")

    report = selflint.lint_epub(output)
    assert report.passed, report.render()


def test_mimetype_is_first_and_stored(tmp_path: Path):
    build = _tree(tmp_path)
    epubpack.build_package(build)
    output = epubpack.pack(build, tmp_path / "out.epub")

    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"


def test_spine_defaults_to_sorted_chapters(tmp_path: Path):
    build = _tree(tmp_path, chapters=3)
    result = epubpack.build_package(build)
    assert result.spine == ["text/ch001.xhtml", "text/ch002.xhtml", "text/ch003.xhtml"]


def test_spine_from_book_json_and_missing_warns(tmp_path: Path):
    build = _tree(tmp_path, chapters=2, book={"spine": ["text/ch002.xhtml", "text/nope.xhtml"]})
    result = epubpack.build_package(build)

    assert result.spine[0] == "text/ch002.xhtml"
    assert "text/ch001.xhtml" in result.spine, "没列进 spine 的章节应当被补上"
    assert any("不存在" in w for w in result.book.warnings)


def test_toc_nesting_tolerates_bad_levels(tmp_path: Path):
    build = _tree(
        tmp_path,
        book={
            "toc": [
                {"title": "一", "href": "text/ch001.xhtml", "level": 1},
                {"title": "二", "href": "text/ch002.xhtml", "level": 3},
                {"title": "三", "href": "text/ch001.xhtml", "level": 1},
            ]
        },
    )
    epubpack.build_package(build)
    nav = etree.fromstring((build / "OEBPS" / "nav.xhtml").read_bytes())

    ns = {"x": "http://www.w3.org/1999/xhtml"}
    links = [a.get("href") for a in nav.findall(".//x:a", ns)]
    assert links[:3] == ["text/ch001.xhtml", "text/ch002.xhtml", "text/ch001.xhtml"]
    # 层级跳跃也必须生成良构 XML
    assert nav.find(".//x:nav", ns) is not None


def test_landmarks_only_point_into_spine(tmp_path: Path):
    """书签导航指向非 spine 项会被 EPUBCheck 判 RSC-011。"""
    build = _tree(tmp_path, chapters=2, book={"cover": "images/cover.png"})
    (build / "OEBPS" / "images").mkdir(parents=True, exist_ok=True)
    (build / "OEBPS" / "images" / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n0")
    result = epubpack.build_package(build)

    nav = etree.fromstring((build / "OEBPS" / "nav.xhtml").read_bytes())
    ns = {"x": "http://www.w3.org/1999/xhtml", "epub": "http://www.idpf.org/2007/ops"}
    landmarks = nav.findall(".//x:nav[@epub:type='landmarks']//x:a", ns)
    assert landmarks, "应当有书签导航"

    spine = set(result.spine)
    for anchor in landmarks:
        href = anchor.get("href")
        assert href in spine, f"landmark 指向了非 spine 项：{href}"
        assert href != "nav.xhtml", "导航文档自身不在 spine 里，不能作为 landmark"


def test_invalid_uuid_identifier_is_replaced(tmp_path: Path):
    """LLM 写 `urn:uuid:demo` 很常见，脚本应当自己纠正（EPUBCheck 会报 OPF-085）。"""
    build = _tree(tmp_path, book={"identifier": "urn:uuid:demo"})
    result = epubpack.build_package(build)

    assert result.book.identifier != "urn:uuid:demo"
    assert result.book.identifier.startswith("urn:uuid:")
    import uuid as uuid_mod

    uuid_mod.UUID(result.book.identifier[len("urn:uuid:") :])  # 不抛异常即合法
    assert any("UUID" in w for w in result.book.warnings)


def test_other_identifier_schemes_are_kept(tmp_path: Path):
    build = _tree(tmp_path, book={"identifier": "https://example.com/books/1"})
    assert epubpack.load_book(build).identifier == "https://example.com/books/1"


def test_cover_page_generated_when_declared(tmp_path: Path):
    build = _tree(tmp_path, book={"cover": "images/cover.png"})
    (build / "OEBPS" / "images").mkdir(parents=True, exist_ok=True)
    (build / "OEBPS" / "images" / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n0")

    result = epubpack.build_package(build)

    assert (build / "OEBPS" / "text" / "cover.xhtml").is_file()
    assert result.spine[0] == "text/cover.xhtml", "封面页应当排在阅读顺序最前"

    opf = etree.fromstring((build / "OEBPS" / "content.opf").read_bytes())
    cover_item = next(
        i for i in opf.findall(".//opf:item", OPF_NS) if "cover-image" in (i.get("properties") or "")
    )
    assert cover_item.get("href") == "images/cover.png"


def test_missing_cover_warns_instead_of_crashing(tmp_path: Path):
    build = _tree(tmp_path, book={"cover": "images/nope.png"})
    result = epubpack.build_package(build)
    assert any("封面图不存在" in w for w in result.book.warnings)


def test_identifier_defaults_to_stable_uuid(tmp_path: Path):
    build = _tree(tmp_path, book={"identifier": ""})
    a = epubpack.load_book(build, fallback_identifier="seed-1")
    b = epubpack.load_book(build, fallback_identifier="seed-1")
    assert a.identifier.startswith("urn:uuid:")
    assert a.identifier == b.identifier


def test_missing_book_json_is_a_clear_error(tmp_path: Path):
    build = tmp_path / "build"
    (build / "OEBPS").mkdir(parents=True)
    try:
        epubpack.load_book(build)
    except BuildError as exc:
        assert "book.json" in exc.message
    else:  # pragma: no cover
        raise AssertionError("缺 book.json 应当明确报错")


def test_broken_book_json_is_a_clear_error(tmp_path: Path):
    build = _tree(tmp_path)
    (build / "book.json").write_text("{ not json", encoding="utf-8")
    try:
        epubpack.load_book(build)
    except BuildError as exc:
        assert "JSON" in exc.message
    else:  # pragma: no cover
        raise AssertionError("坏 JSON 应当明确报错")


def test_pack_requires_mimetype(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    try:
        epubpack.pack(empty, tmp_path / "x.epub")
    except BuildError as exc:
        assert "mimetype" in exc.message
    else:  # pragma: no cover
        raise AssertionError("缺 mimetype 应当报错")


def test_working_files_are_not_packaged(tmp_path: Path):
    """build/ 下给 LLM 看的说明书不能被塞进读者的电子书里。"""
    build = _tree(tmp_path)
    (build / "BRIEF.md").write_text("# 给 LLM 看的说明书", encoding="utf-8")
    (build / "notes.txt").write_text("随手记的", encoding="utf-8")
    epubpack.build_package(build)
    output = epubpack.pack(build, tmp_path / "out.epub")

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert "BRIEF.md" not in names
    assert "notes.txt" not in names
    assert all(n.startswith(("META-INF/", "OEBPS/")) for n in names if n != "mimetype")


def test_unknown_file_types_are_skipped_not_fatal(tmp_path: Path):
    build = _tree(tmp_path)
    (build / "OEBPS" / "note.txt").write_text("随手放的备注", encoding="utf-8")
    result = epubpack.build_package(build)
    opf = etree.fromstring((build / "OEBPS" / "content.opf").read_bytes())
    hrefs = {i.get("href") for i in opf.findall(".//opf:item", OPF_NS)}
    assert "note.txt" not in hrefs
    assert result.entries >= 3

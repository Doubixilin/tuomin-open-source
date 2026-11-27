"""Conservative, dependency-free XLSX/PPTX text patching.

Units are individual paragraphs, cells, comments or property values. Only XML
parts actually changed are serialized; minidom retains namespace declarations
(including prefixes referenced by mc:Ignorable). Uninspected executable/data
containers are rejected rather than silently copied into a redacted document.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from xml.dom import Node, minidom
from urllib.parse import unquote

from tuomin_gateway.placeholders import PLACEHOLDER_RE, find_altered_placeholders

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_MAX_ENTRIES = 4096
_MAX_PART = 32 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emf", ".wmf", ".svg", ".webp"}


_ERROR_MESSAGES = {
    "embedded_object": "文件包含无法处理的嵌入对象（OLE/ActiveX 或非 Excel 的嵌入文件）",
    "readonly_text_replacement": "需要替换的文字位于只读结构（公式、超链接目标、未知结构或属性），无法安全改写",
    "macro_present": "文件含宏，不支持原格式脱敏",
    "encrypted_package": "文件已加密，请先去除打开密码",
    "digital_signature": "文件带数字签名，改写会使签名失效",
    "unsupported_data_container": "文件含查询表、数据模型、切片器或其他不支持的数据容器",
    "external_link": "文件引用了不支持的外部资源（非工作簿链接、非超链接）",
    "unsupported_office_namespace": "不支持严格 OOXML 命名空间的文件",
    "not_a_package": "不是有效的 Office 文件",
    "unsupported_binary_part": "文件含未知的二进制部件",
    "unsupported_comments": "文件使用了不支持的批注格式",
    "unsupported_removed_part_reference": "无法安全移除被引用的部件",
    "replacement_crosses_control": "替换范围跨越了换行或制表控制符",
    "invalid_replacement": "替换指令无效",
    "invalid_replacement_range": "替换范围与文件内容不一致，请重新处理",
    "invalid_refill_values": "回填值包含不允许的字符",
}


class OfficeUnsupportedError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        text = _ERROR_MESSAGES.get(code)
        message = f"{code}: {text}" if text else code
        if detail:
            message += f"（{detail}）"
        super().__init__(message)


# Containers whose content would be silently inconsistent after text replacement
# (query tables / connections pull data from outside on refresh, slicers and
# timelines index pivots). Everything else that is XML is parsed and its text
# participates in detection like any unknown structure. External workbook links
# are handled like pivot caches: their cached cell values are rewritten.
_REJECTED_CONTAINER_SEGMENTS = (
    "querytable", "connections", "/model/", "slicer", "timeline",
    "webextension", "customdata", "ctrlprop",
)
# Relationship types of an external workbook link (xl/externalLinks/*.rels).
# The target is a path on the author's machine; rewriting it cannot break the
# recipient's file (the link never resolves there) and refill restores it.
_EXTERNAL_LINK_RELATIONSHIPS = {"externalLinkPath", "xlPathMissing", "xlAlternateStartup", "xlLibrary", "xlStartup"}
# Attributes in unknown (vendor) namespaces that carry a person's name, e.g.
# WPS comment chains in customXml. Nothing references them by value.
_PERSON_ATTRIBUTES = {"userName", "author", "creator", "displayName", "lastModifiedBy", "modifiedBy"}
# Pivot parts: the cache keeps a copy of cell values and the table definition
# carries names/captions, so both are rewritten together with the sheets.
_PIVOT_TEXT_ATTRIBUTES = (
    "name", "n", "caption", "dataCaption", "grandTotalCaption", "rowHeaderCaption",
    "colHeaderCaption", "subtotalCaption", "missingCaption", "errorCaption",
    "stringValue1", "stringValue2",
)
_PIVOT_TEXT_ELEMENTS = {
    "pivotTableDefinition", "pivotField", "dataField", "cacheField", "item", "filter",
    "pageField", "pivotHierarchy", "cacheHierarchy", "calculatedItem", "member", "group",
}
TC = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"


# Namespace and schema identifiers in attributes are never document prose.
_SCHEMA_URI_PREFIXES = (
    "http://schemas.openxmlformats.org/", "http://schemas.microsoft.com/", "urn:schemas-microsoft-com:",
    "http://www.w3.org/", "http://purl.org/", "http://schemas.xmlsoap.org/", "http://purl.oclc.org/",
)
_EMBEDDED_WORKBOOK_SUFFIX = ".xlsx"


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_NON_TEXT_ATTRIBUTES = {"typeface", "panose", "charset", "pitchFamily", "lang", "altLang", "script", "val", "codeName"}


def _attribute_looks_like_prose(value: str) -> bool:
    """Only attribute values that could carry document text or identifiers.

    DrawingML geometry (``gd@fmla="*/ w 1 2"``, ``pos@x``), colour scheme keys,
    typeface names and similar ASCII tokens are not prose: feeding tens of
    thousands of them to the detectors made real decks slow and produced
    model false positives that blocked exports."""
    # Purely numeric attribute values are coordinates, sizes and ids (EMU
    # offsets are 7+ digits), never phone numbers or account identifiers.
    return bool(_CJK_RE.search(value)) or "@" in value or PLACEHOLDER_RE.search(value) is not None


def _inner_name(part: str) -> str:
    """Part name inside its own package (strips the ``outer!`` prefix of nested packages)."""
    return part.split("!", 1)[1] if "!" in part else part


def _close_all(package: zipfile.ZipFile, nested: dict) -> None:
    for inner in nested.values():
        inner.close()
    package.close()


@dataclass(frozen=True)
class OfficeText:
    part: str
    index: int
    text: str
    readonly: bool = False
    # Structural role of a read-only unit: formula, sheet_name, defined_name,
    # hyperlink, chart_cache, metadata, attribute:<name>; "text" otherwise.
    kind: str = "text"
    # XLSX cell values only: header text of the column(s) the value sits in,
    # used as a rule clue (a bare account number under「收款账号」).
    context: tuple[str, ...] = ()


@dataclass
class _Ref:
    node: Node
    attribute: str | None = None
    numeric: bool = False

    def get(self) -> str:
        if self.attribute is not None:
            return self.node.getAttribute(self.attribute)
        if self.node.localName in {"br", "tab"} and self.node.namespaceURI == A:
            return "\n" if self.node.localName == "br" else "\t"
        raw = "".join(n.data for n in self.node.childNodes if n.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE))
        return _numeric_identifier(raw) or raw if self.numeric else raw

    def set(self, value: str) -> None:
        if self.attribute is not None:
            self.node.setAttribute(self.attribute, value)
            return
        if self.node.localName in {"br", "tab"} and self.node.namespaceURI == A:
            if value != self.get():
                raise OfficeUnsupportedError("replacement_crosses_control")
            return
        for child in list(self.node.childNodes):
            if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
                self.node.removeChild(child)
        self.node.appendChild(self.node.ownerDocument.createTextNode(value))
        if self.node.localName == "t":
            self.node.setAttributeNS("http://www.w3.org/XML/1998/namespace", "xml:space", "preserve")


@dataclass
class _Unit:
    part: str
    index: int
    refs: list[_Ref]
    readonly: bool = False
    numeric_cell: Node | None = None
    kind: str = "text"

    @property
    def text(self) -> str:
        return "".join(ref.get() for ref in self.refs)


def _elements(node: Node):
    stack = [(child, 1) for child in reversed(node.childNodes) if child.nodeType == Node.ELEMENT_NODE]
    while stack:
        child, depth = stack.pop()
        if depth > 128:
            raise OfficeUnsupportedError("xml_depth_exceeded")
        yield child
        stack.extend((descendant, depth + 1) for descendant in reversed(child.childNodes) if descendant.nodeType == Node.ELEMENT_NODE)


def _children(node: Node, ns: str, name: str) -> list[Node]:
    return [n for n in node.childNodes if n.nodeType == Node.ELEMENT_NODE and n.namespaceURI == ns and n.localName == name]


def _numeric_identifier(text: str) -> str | None:
    # Scientific notation is common when Excel stores a phone as a number.
    if len(text) > 80 or not re.fullmatch(r"\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?", text):
        return None
    try:
        number = Decimal(text)
        if not number.is_finite() or number.adjusted() < 6 or number.adjusted() > 31 or number != number.to_integral_value():
            return None
        return format(number, "f").split(".", 1)[0]
    except (InvalidOperation, ValueError, OverflowError):
        return None


_XML_DECLARATION_ENCODING = re.compile(r'^(\s*<\?xml[^>]*?)\s+encoding\s*=\s*(["\'])[^"\']*\2', re.I)


def _utf8(raw: bytes) -> bytes:
    """Re-encode UTF-16 parts (WPS writes customXml items with a BOM) as UTF-8.

    The NUL-byte guard below would otherwise reject every ASCII character of a
    UTF-16 document; changed parts are always written back as UTF-8."""
    if raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        return raw
    try:
        text = raw.decode("utf-16")
    except UnicodeDecodeError as exc:
        raise OfficeUnsupportedError("invalid_xml") from exc
    return _XML_DECLARATION_ENCODING.sub(r'\1 encoding="UTF-8"', text, count=1).encode("utf-8")


def _parse(raw: bytes) -> minidom.Document:
    raw = _utf8(raw)
    if b"\x00" in raw or re.search(br"<!\s*(DOCTYPE|ENTITY)\b", raw, re.I):
        raise OfficeUnsupportedError("unsafe_xml")
    try:
        document = minidom.parseString(raw)
    except Exception as exc:
        raise OfficeUnsupportedError("invalid_xml") from exc
    if document.doctype is not None:
        raise OfficeUnsupportedError("unsafe_xml")
    return document


def _format(file_format: str) -> str:
    if not isinstance(file_format, str) or file_format not in {"xlsx", "pptx"}:
        raise OfficeUnsupportedError("unsupported_format")
    return file_format


def _open(data: bytes) -> zipfile.ZipFile:
    if not isinstance(data, bytes):
        raise OfficeUnsupportedError("not_a_package")
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise OfficeUnsupportedError("encrypted_or_legacy_package")
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
        entries = package.infolist()
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise OfficeUnsupportedError("not_a_package") from exc
    try:
        if len(entries) > _MAX_ENTRIES or sum(n.file_size for n in entries) > _MAX_TOTAL:
            raise OfficeUnsupportedError("package_limit_exceeded")
        seen: set[str] = set()
        for info in entries:
            name = info.filename
            if not name or name.startswith("/") or "\\" in name or ":" in name or "\x00" in name or any(p in {".", "..", ""} for p in name.rstrip("/").split("/")):
                raise OfficeUnsupportedError("unsafe_package_path")
            if name.casefold() in seen:
                raise OfficeUnsupportedError("duplicate_package_part")
            seen.add(name.casefold())
            if info.flag_bits & 1:
                raise OfficeUnsupportedError("encrypted_package")
            if info.file_size > _MAX_PART:
                raise OfficeUnsupportedError("package_limit_exceeded")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise OfficeUnsupportedError("unsupported_zip_compression")
        return package
    except Exception:
        package.close()
        raise


def _read(package: zipfile.ZipFile, name: str) -> bytes:
    try:
        return package.read(name)
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, EOFError, zlib.error) as exc:
        raise OfficeUnsupportedError("invalid_package_part") from exc


def _relationship_target(part: str, target: str) -> str:
    source = "" if part == "_rels/.rels" else part.rsplit("/_rels/", 1)[0] + "/"
    target_path = unquote(target.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(source, target_path)).lstrip("/")


def _load(data: bytes, file_format: str, *, _depth: int = 0):
    kind = _format(file_format)
    package = _open(data)
    nested: dict[str, zipfile.ZipFile] = {}
    try:
        names = package.namelist()
        main = "xl/workbook.xml" if kind == "xlsx" else "ppt/presentation.xml"
        if main not in names or "[Content_Types].xml" not in names:
            raise OfficeUnsupportedError("not_a_package")
        roots: dict[str, minidom.Document] = {}
        warnings: list[dict[str, str]] = []
        thumbnail_parts = {n for n in names if n.lower().startswith("docprops/thumbnail.")}
        printer_parts = {n for n in names if n.lower().startswith(("ppt/printersettings/", "xl/printersettings/"))}
        nested_drop: set[str] = set()
        external_data_refs: list[tuple[str, str]] = []
        dangling_external_data: list[str] = []
        for name in names:
            low = name.lower()
            if name.endswith("/"):
                continue
            if "encryptioninfo" in low or "encryptedpackage" in low:
                raise OfficeUnsupportedError("encrypted_package")
            if low.startswith("_xmlsignatures/"):
                raise OfficeUnsupportedError("digital_signature")
            if "vba" in low or "macro" in low:
                raise OfficeUnsupportedError("macro_present")
            if "/embeddings/" in low or "/activex/" in low:
                if _depth == 0 and "/embeddings/" in low and low.endswith(_EMBEDDED_WORKBOOK_SUFFIX):
                    # Chart data workbooks (PowerPoint always embeds one per chart):
                    # process them as a nested package so their cells are redacted
                    # and refilled together with the slide text.
                    inner_package, inner_roots, _inner_warnings, inner_drop, _ = _load(_read(package, name), "xlsx", _depth=1)
                    nested[name] = inner_package
                    roots.update({f"{name}!{inner_part}": root for inner_part, root in inner_roots.items()})
                    nested_drop.update(f"{name}!{inner_part}" for inner_part in inner_drop)
                    continue
                raise OfficeUnsupportedError("embedded_object")
            if name in printer_parts:
                continue
            if any(segment in low for segment in _REJECTED_CONTAINER_SEGMENTS):
                raise OfficeUnsupportedError("unsupported_data_container", name)
            if low.endswith((".xml", ".rels", ".vml")):
                root = _parse(_read(package, name))
                roots[name] = root
                if (low.startswith("ppt/comments/") or low == "ppt/commentauthors.xml" or low.startswith("ppt/authors/")) and root.documentElement.namespaceURI != P:
                    raise OfficeUnsupportedError("unsupported_comments")
                for node in _elements(root):
                    local = (node.localName or "").lower()
                    if local in {"oleobject", "oleobj", "control", "ddeitems"}:
                        raise OfficeUnsupportedError("embedded_object")
                    if local == "externaldata" and node.namespaceURI == C:
                        external_data_refs.append((name, node.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id")))
                    if local in {"querytable", "connection", "webpr"}:
                        raise OfficeUnsupportedError("unsupported_data_container", f"{name}: {node.localName}")
                    if node.namespaceURI == CT and "macroenabled" in node.getAttribute("ContentType").lower():
                        raise OfficeUnsupportedError("macro_present")
                    if node.namespaceURI == R and node.localName == "Relationship":
                        rel_type = node.getAttribute("Type").rsplit("/", 1)[-1]
                        target = node.getAttribute("Target")
                        if node.getAttribute("TargetMode").lower() == "external":
                            if rel_type != "hyperlink" and rel_type not in _EXTERNAL_LINK_RELATIONSHIPS:
                                raise OfficeUnsupportedError("external_link", f"{name}: {rel_type}")
                        elif rel_type == "thumbnail":
                            thumbnail_parts.add(_relationship_target(name, target))
            elif name in thumbnail_parts:
                continue
            elif "/media/" in low and posixpath.splitext(low)[1] in _IMAGE_SUFFIXES:
                continue
            else:
                raise OfficeUnsupportedError("unsupported_binary_part")
        expected_ns = S if kind == "xlsx" else P
        expected_name = "workbook" if kind == "xlsx" else "presentation"
        if roots[main].documentElement.namespaceURI != expected_ns or roots[main].documentElement.localName != expected_name:
            raise OfficeUnsupportedError("unsupported_office_namespace")
        if any("/media/" in n.lower() for n in names):
            warnings.append({"code": "images_not_inspected", "message": "图片和图片中的文字未做 OCR，需人工检查；图片中的敏感信息不会被自动脱敏"})
        if thumbnail_parts:
            warnings.append({"code": "thumbnail_removed", "message": "输出文件会移除文档预览缩略图，回填不会恢复缩略图"})
        if printer_parts:
            warnings.append({"code": "printer_settings_removed", "message": "输出文件会移除二进制打印机配置并重置打印机选择；页面尺寸和方向保留，回填不会恢复打印机配置"})
        if any(_automatic_metadata(node) for part, root in roots.items() if _inner_name(part) == "docProps/core.xml" for node in _elements(root)):
            warnings.append({"code": "automatic_timestamps_removed", "message": "输出文件会移除自动创建、修改和最后打印时间元数据，回填不会恢复这些时间"})
        # A chart's external data must point at an embedded workbook handled above.
        for chart_part, rel_id in external_data_refs:
            folder, filename = posixpath.split(chart_part)
            rels_root = roots.get(f"{folder}/_rels/{filename}.rels")
            target = None
            if rels_root is not None and rel_id:
                for node in _elements(rels_root):
                    if node.namespaceURI == R and node.localName == "Relationship" and node.getAttribute("Id") == rel_id:
                        target = _relationship_target(f"{folder}/_rels/{filename}.rels", node.getAttribute("Target"))
            if target not in nested:
                if target is not None and "/embeddings/" in target.lower():
                    raise OfficeUnsupportedError("embedded_object", f"{chart_part}: {target}")
                # Dangling reference (no such relationship, or it points at a style
                # part): left behind when a chart is pasted from another workbook.
                # The series formulas and caches live in this package and are
                # processed like any chart, so nothing is hidden behind it.
                dangling_external_data.append(chart_part)
        if dangling_external_data:
            shown = "、".join(posixpath.basename(part) for part in dangling_external_data[:3])
            warnings.append({"code": "chart_external_data_ignored", "message": f"图表 {shown} 带有指向不存在嵌入对象的外部数据引用（常见于从其他工作簿粘贴的图表），已按本文件内的图表数据处理"})
        if nested:
            warnings.append({"code": "embedded_workbook_processed", "message": "图表内嵌的 Excel 工作簿已一并检测和改写；在 PowerPoint 里「编辑数据」看到的也是脱敏后的内容"})
        if any("pivot" in part.lower() for part in roots):
            warnings.append({"code": "pivot_cache_rewritten", "message": "透视表缓存与透视表定义里的文本已一并脱敏；在 Excel 中刷新透视表会按脱敏后的数据重算"})
        if any("externallink" in part.lower() for part in roots):
            warnings.append({"code": "external_link_cache_rewritten", "message": "外部工作簿链接的缓存值与链接路径已一并脱敏；收件人若在能访问源工作簿的环境里「更新链接」，会重新拉取源数据，请提醒对方不要更新"})
        return package, roots, warnings, thumbnail_parts | printer_parts | nested_drop, nested
    except Exception:
        _close_all(package, nested)
        raise


def _automatic_metadata(node: Node) -> bool:
    return (
        node.namespaceURI == "http://purl.org/dc/terms/" and node.localName in {"created", "modified"}
        or node.namespaceURI == "http://schemas.openxmlformats.org/package/2006/metadata/core-properties" and node.localName == "lastPrinted"
    )


def _units(part: str, document: minidom.Document) -> list[_Unit]:
    units: list[_Unit] = []
    consumed: set[int] = set()
    consumed_attributes: set[tuple[int, str]] = set()

    def add(nodes: list[Node], readonly: bool = False, numeric_cell: Node | None = None, numeric: bool = False, kind: str = "text"):
        if not nodes:
            return
        refs = [_Ref(node, numeric=numeric) for node in nodes]
        consumed.update(id(node) for node in nodes)
        if any(ref.get() for ref in refs):
            units.append(_Unit(part, len(units), refs, readonly, numeric_cell, kind))

    def attr(node: Node, name: str, readonly: bool = False, kind: str | None = None):
        consumed_attributes.add((id(node), name))
        if node.getAttribute(name):
            units.append(_Unit(part, len(units), [_Ref(node, name)], readonly, None, kind or (f"attribute:{name}" if readonly else "text")))

    elements = list(_elements(document))
    for node in elements:
        ns, local = node.namespaceURI, node.localName
        if ns == A and local == "p":
            # a:p is a complete semantic paragraph, including table/SmartArt text.
            refs = [n for n in _elements(node) if n.namespaceURI == A and n.localName in {"t", "br", "tab"}]
            add(refs)
        elif ns == S and local in {"si", "is", "text"}:
            # Phonetic annotations are separate units, never appended to cell text.
            refs = [n for n in _elements(node) if n.namespaceURI == S and n.localName == "t" and n.parentNode.localName != "rPh"]
            add(refs)
        elif ns == S and local == "rPh":
            add([n for n in _elements(node) if n.namespaceURI == S and n.localName == "t"])
        elif ns == S and local in {"c", "cell"}:
            # c: worksheet cell; cell: cached copy of an external workbook cell.
            values = _children(node, S, "v")
            cell_type = node.getAttribute("t")
            formula = bool(_children(node, S, "f"))
            if cell_type == "str":
                add(values, readonly=formula, kind="formula" if formula else "text")
            elif cell_type in {"", "n"} and values and _numeric_identifier(_Ref(values[0]).get()):
                add(values, readonly=formula, numeric_cell=node, numeric=True, kind="formula" if formula else "text")
            elif cell_type not in {"s", "b", "e", "d", "inlineStr", "", "n"}:
                raise OfficeUnsupportedError("unsupported_cell_type")
            else:
                consumed.update(id(value) for value in values)
        elif ns == S and local in {"author", "oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"}:
            add([node])
        elif ns == S and local == "f":
            add([node], readonly=True, kind="formula")
        elif ns == S and local == "definedName":
            add([node], readonly=True, kind="defined_name")
        elif ns == S and local == "sheet":
            attr(node, "name", readonly=True, kind="sheet_name")
        elif ns == S and local == "sheetName":
            attr(node, "val", readonly=True, kind="sheet_name")  # external workbook sheet name, referenced by formulas
        elif local in {"filter", "customFilter"} and node.hasAttribute("val"):
            attr(node, "val")  # AutoFilter criteria mirror cell values (sheets and WPS customXml)
        elif ns == C and local == "v":
            ancestor = node.parentNode
            while ancestor is not None and ancestor.nodeType == Node.ELEMENT_NODE and ancestor.localName not in {"strCache", "strLit", "numCache", "numLit"}:
                ancestor = ancestor.parentNode
            if ancestor is not None and ancestor.localName in {"strCache", "strLit"}:
                add([node])
            elif ancestor is not None and ancestor.localName in {"numCache", "numLit"}:
                if _numeric_identifier(_Ref(node).get()):
                    add([node], readonly=True, numeric=True, kind="chart_cache")
                else:
                    consumed.add(id(node))
            elif node.parentNode.namespaceURI == C and node.parentNode.localName == "tx":
                add([node])
            else:
                add([node], readonly=True, kind="chart_cache")
        elif ns == C and local == "f":
            add([node], readonly=True, kind="formula")
        elif ns == P and local == "text":
            add([node])
        elif ns == P and local == "cmAuthor":
            attr(node, "name")
            attr(node, "initials")
        elif ns == S and local == "s" and "pivot" in part.lower() and node.parentNode is not None and node.parentNode.localName in {"sharedItems", "r", "groupItems"}:
            attr(node, "v")  # cached string item (definition) or inline string record
        elif ns == S and local in _PIVOT_TEXT_ELEMENTS and "pivot" in part.lower():
            for attribute in _PIVOT_TEXT_ATTRIBUTES:
                attr(node, attribute)
        elif ns == TC and local == "text":
            # Excel threaded comment body (the legacy comments part mirrors it as text).
            add([node])
        elif ns == TC and local == "person":
            attr(node, "displayName")
            attr(node, "userId")
        elif ns == R and local == "Relationship":
            rel_type = node.getAttribute("Type").rsplit("/", 1)[-1]
            if rel_type == "hyperlink":
                attr(node, "Target", readonly=True, kind="hyperlink")
            elif rel_type in _EXTERNAL_LINK_RELATIONSHIPS:
                attr(node, "Target")
        elif _inner_name(part) == "docProps/core.xml" and _automatic_metadata(node):
            consumed.add(id(node))  # Always removed from output, never rewritten.
        elif _inner_name(part).startswith("docProps/") and not any(n.nodeType == Node.ELEMENT_NODE for n in node.childNodes):
            # Typed custom properties must retain their value type. Inserting a
            # placeholder into vt:i8, vt:bool or a timestamp corrupts the file.
            string_property = (
                ns == "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes" and local in {"lpstr", "lpwstr", "bstr"}
                or ns == "http://purl.org/dc/elements/1.1/"
                or ns == "http://schemas.openxmlformats.org/package/2006/metadata/core-properties" and local in {"keywords", "lastModifiedBy", "category", "contentStatus", "version"}
                or ns == "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" and local in {"Application", "AppVersion", "Company", "Manager", "Template", "HyperlinkBase", "PresentationFormat"}
            )
            add([node], readonly=not string_property, kind="text" if string_property else "metadata")
        # Nonvisual shape descriptions are user text; object names are references.
        if local in {"cNvPr", "docPr"}:
            # Shape names are labels (WPS often copies the text into them); nothing
            # references them by value, so they are safe to rewrite and refill.
            for attribute in ("descr", "title", "name"):
                attr(node, attribute)
        if ns == S and local == "hyperlink":
            for attribute in ("display", "tooltip"):
                attr(node, attribute)
            attr(node, "location", readonly=True, kind="hyperlink")

    # Any unrecognized textual leaf stays visible to detection, but cannot be
    # modified because its semantic role is unknown. This also covers VML text.
    for node in elements:
        if id(node) not in consumed:
            text = _Ref(node).get()
            if text.strip():
                # Replacing the text of an unknown element cannot break the XML
                # structure, so it is patched like any paragraph; only unknown
                # attributes stay read-only (they may be references).
                add([node], numeric=_numeric_identifier(text) is not None)
        # Unknown string attributes (e.g. prompts, tooltips, custom number
        # formats) are read-only too. Known structural IDs/coordinates and
        # schema declarations are not document prose.
        for i in range(node.attributes.length):
            attribute = node.attributes.item(i)
            if (id(node), attribute.name) in consumed_attributes or attribute.name.startswith("xmlns") or attribute.namespaceURI == "http://www.w3.org/XML/1998/namespace":
                continue
            structural = {"id", "Id", "r", "ref", "s", "t", "idx", "authorId", "lastIdx", "min", "max", "count", "uniqueCount", "pid", "fmtid", "sb", "eb", "rgb"}
            if attribute.namespaceURI == "http://schemas.openxmlformats.org/markup-compatibility/2006":
                continue
            if attribute.localName in structural and not _numeric_identifier(attribute.value):
                continue
            if node.namespaceURI == R and attribute.localName in {"Type", "TargetMode"}:
                continue
            if node.namespaceURI == CT:
                continue
            value = attribute.value
            if attribute.localName in _NON_TEXT_ATTRIBUTES:
                continue  # font names and similar presentation settings (often CJK, never document prose)
            if not value or value.startswith(_SCHEMA_URI_PREFIXES) or not _attribute_looks_like_prose(value):
                continue
            attr(node, attribute.name, readonly=attribute.localName not in _PERSON_ATTRIBUTES)
    # First pass groups rich text; final order is stable for redaction/refill and
    # deliberately does not imply adjacency across independent units.
    return units


def _collect(roots: dict[str, minidom.Document]) -> list[_Unit]:
    # Presentation order is independent of filenames (slide10 may precede
    # slide2 in the archive); show the visible slides first in their real order.
    ordered: list[str] = []
    presentation = roots.get("ppt/presentation.xml")
    rels_part = "ppt/_rels/presentation.xml.rels"
    rels = roots.get(rels_part)
    if presentation is not None and rels is not None:
        targets = {node.getAttribute("Id"): _relationship_target(rels_part, node.getAttribute("Target")) for node in _elements(rels) if node.namespaceURI == R and node.localName == "Relationship" and node.getAttribute("Type").endswith("/slide")}
        for node in _elements(presentation):
            if node.namespaceURI == P and node.localName == "sldId":
                target = targets.get(node.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id"))
                if target in roots and target not in ordered:
                    ordered.append(target)
    ordered.extend(part for part in roots if part not in ordered)
    return [unit for part in ordered for unit in _units(part, roots[part])]


def probe_office(data: bytes, file_format: str) -> list[dict[str, str]]:
    package, roots, warnings, _, nested = _load(data, file_format)
    try:
        units = _collect(roots)
        if any(unit.readonly for unit in units):
            warnings.append({"code": "readonly_text_present", "message": "公式、工作表名、超链接目标及未知结构文本会参与检测；若命中需脱敏内容，将阻止导出"})
        if any(unit.numeric_cell is not None for unit in units):
            warnings.append({"code": "numeric_identifiers_present", "message": "以数值保存的长整数参与检测；命中后改为文本单元格，回填也保留文本类型以避免精度损失"})
        return warnings
    finally:
        _close_all(package, nested)


def extract_office_texts(data: bytes, file_format: str) -> list[OfficeText]:
    package, roots, _, _, nested = _load(data, file_format)
    try:
        units = _collect(roots)
        headers = _xlsx_column_headers(roots, units) if file_format == "xlsx" else {}
        return [OfficeText(unit.part, unit.index, unit.text, unit.readonly, unit.kind, headers.get(id(unit), ())) for unit in units]
    finally:
        _close_all(package, nested)


_CELL_REFERENCE = re.compile(r"([A-Z]{1,3})(\d+)")


def _xlsx_column_headers(roots: dict[str, minidom.Document], units: list[_Unit]) -> dict[int, tuple[str, ...]]:
    """id(unit) → header text of every column the unit's value appears in.

    A column's header is its topmost non-empty cell text without digits; only
    cells below it get the header. Shared strings can sit in several columns,
    so they collect every header they appear under.
    """
    owner = {id(ref.node): unit for unit in units for ref in unit.refs}
    shared: list[_Unit | None] = []
    strings = roots.get("xl/sharedStrings.xml")
    if strings is not None:
        for item in _children(strings.documentElement, S, "si"):
            shared.append(next((owner[id(node)] for node in _elements(item)
                                if id(node) in owner and node.parentNode.localName != "rPh"), None))

    result: dict[int, list[str]] = {}
    for part, document in roots.items():
        if not (part.startswith("xl/worksheets/") and part.endswith(".xml")):
            continue
        columns: dict[str, list[tuple[int, str, _Unit | None]]] = {}
        for cell in _elements(document):
            if cell.namespaceURI != S or cell.localName != "c":
                continue
            match = _CELL_REFERENCE.fullmatch(cell.getAttribute("r"))
            if match is None:
                continue
            unit: _Unit | None = None
            cell_type = cell.getAttribute("t")
            if cell_type == "s":
                values = _children(cell, S, "v")
                try:
                    unit = shared[int(_Ref(values[0]).get())] if values else None
                except (ValueError, IndexError):
                    unit = None
            else:
                unit = next((owner[id(node)] for node in _elements(cell) if id(node) in owner), None)
            text = unit.text.strip() if unit is not None else ""
            columns.setdefault(match.group(1), []).append((int(match.group(2)), text, unit))
        for cells in columns.values():
            cells.sort(key=lambda item: item[0])
            header_row, header = next(((row, text) for row, text, _ in cells if text), (0, ""))
            if not header or any(ch.isdigit() for ch in header):
                continue
            for row, _, unit in cells:
                if row > header_row and unit is not None and header not in result.setdefault(id(unit), []):
                    result[id(unit)].append(header)
    return {key: tuple(value) for key, value in result.items()}


def _validate_replacements(replacements: list[dict]) -> dict[tuple[str, int], list[dict]]:
    if not isinstance(replacements, list):
        raise OfficeUnsupportedError("invalid_replacement")
    grouped: dict[tuple[str, int], list[dict]] = {}
    for item in replacements:
        if not isinstance(item, dict) or not isinstance(item.get("part"), str) or not all(type(item.get(key)) is int for key in ("paragraph", "start", "end")) or not isinstance(item.get("placeholder"), str):
            raise OfficeUnsupportedError("invalid_replacement")
        if item["paragraph"] < 0 or item["start"] < 0 or item["start"] >= item["end"] or not PLACEHOLDER_RE.fullmatch(item["placeholder"]):
            raise OfficeUnsupportedError("invalid_replacement")
        grouped.setdefault((item["part"], item["paragraph"]), []).append(item)
    return grouped


def _describe(unit: _Unit) -> str:
    ref = unit.refs[0]
    node = ref.node
    where = f"{unit.part} <{node.localName}>"
    return where + (f"@{ref.attribute}" if ref.attribute else "")


def _patch(unit: _Unit, spans: list[dict], *, refill: bool = False) -> list[dict]:
    if unit.readonly:
        raise OfficeUnsupportedError("readonly_text_replacement", _describe(unit))
    original_texts = [ref.get() for ref in unit.refs]
    starts: list[int] = []
    offset = 0
    for text in original_texts:
        starts.append(offset)
        offset += len(text)
    ordered = sorted(spans, key=lambda s: (s["start"], s["end"]))
    if any(span["end"] > offset or span["start"] < 0 for span in ordered) or any(left["end"] > right["start"] for left, right in zip(ordered, ordered[1:])):
        raise OfficeUnsupportedError("invalid_replacement_range")
    if unit.numeric_cell is not None and unit.numeric_cell.localName == "cell":
        # Cached external cell: CT_ExternalCell only allows <v>, so keep the
        # node, write the normalised identifier as text and mark it t="str".
        value_node = unit.refs[0].node
        normalized = unit.text
        _Ref(value_node).set(normalized)
        unit.numeric_cell.setAttribute("t", "str")
        unit.refs = [_Ref(value_node)]
        original_texts = [normalized]
        starts = [0]
    elif unit.numeric_cell is not None:
        # A value cannot be a placeholder in an OOXML numeric cell. Keep style
        # and cell coordinates, and replace only v with inlineStr/is/t.
        cell = unit.numeric_cell
        doc = cell.ownerDocument
        prefix = (cell.prefix + ":") if cell.prefix else ""
        inline = doc.createElementNS(S, prefix + "is")
        text_node = doc.createElementNS(S, prefix + "t")
        text_node.appendChild(doc.createTextNode(unit.text))
        inline.appendChild(text_node)
        value_node = unit.refs[0].node
        cell.replaceChild(inline, value_node)
        cell.setAttribute("t", "inlineStr")
        unit.refs = [_Ref(text_node)]
        original_texts = [unit.text]
        starts = [0]
    ledger = []
    for span in reversed(ordered):
        covered = [i for i, (start, text) in enumerate(zip(starts, original_texts)) if start < span["end"] and start + len(text) > span["start"]]
        if not covered:
            raise OfficeUnsupportedError("invalid_replacement_range")
        first, last = covered[0], covered[-1]
        prefix = unit.refs[first].get()[:span["start"] - starts[first]]
        suffix = unit.refs[last].get()[span["end"] - starts[last]:]
        value = span["value"] if refill else span["placeholder"]
        unit.refs[first].set(prefix + value + (suffix if first == last else ""))
        if first != last:
            for index in covered[1:-1]:
                unit.refs[index].set("")
            unit.refs[last].set(suffix)
        ledger.append({"part": unit.part, "paragraph": unit.index, "start": span["start"], "end": span["end"], "node_start": first, "node_end": last, "crossed_nodes": first != last, "format_mixed": first != last, "placeholder": span.get("placeholder", "")})
    return ledger


def _strip_output_metadata(roots: dict[str, minidom.Document], drop: set[str]) -> set[str]:
    changed: set[str] = set()
    for part, root in roots.items():
        for node in list(_elements(root)):
            if _inner_name(part) == "docProps/core.xml" and _automatic_metadata(node):
                node.parentNode.removeChild(node)
                changed.add(part)
            elif node.namespaceURI == R and node.localName == "Relationship" and node.getAttribute("TargetMode").lower() != "external" and _relationship_target(part, node.getAttribute("Target")) in drop:
                removed_id = node.getAttribute("Id")
                if "/_rels/" in part:
                    folder, filename = part.rsplit("/_rels/", 1)
                    source = folder + "/" + filename.removesuffix(".rels")
                    source_root = roots.get(source)
                    if source_root is not None:
                        for element in list(_elements(source_root)):
                            ref_namespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                            if not removed_id or element.getAttributeNS(ref_namespace, "id") != removed_id:
                                continue
                            if element.namespaceURI == P and element.localName == "prnPr":
                                element.parentNode.removeChild(element)
                            elif element.namespaceURI == S and element.localName == "pageSetup":
                                element.removeAttributeNS(ref_namespace, "id")
                            else:
                                raise OfficeUnsupportedError("unsupported_removed_part_reference")
                            changed.add(source)
                node.parentNode.removeChild(node)
                changed.add(part)
            elif node.namespaceURI == CT and node.localName == "Override" and (part.split("!", 1)[0] + "!" if "!" in part else "") + node.getAttribute("PartName").lstrip("/") in drop:
                node.parentNode.removeChild(node)
                changed.add(part)
    return changed


def _rebuild(package: zipfile.ZipFile, roots: dict[str, minidom.Document], changed: set[str], drop: set[str], nested: dict | None = None):
    output = io.BytesIO()
    hashes: dict[str, str] = {}
    nested = nested or {}
    with zipfile.ZipFile(output, "w") as target:
        for info in package.infolist():
            if info.filename in drop:
                continue
            original = _read(package, info.filename)
            if info.filename in nested:
                prefix = info.filename + "!"
                inner_changed = {p[len(prefix):] for p in changed if p.startswith(prefix)}
                inner_drop = {p[len(prefix):] for p in drop if p.startswith(prefix)}
                if inner_changed or inner_drop:
                    inner_roots = {p[len(prefix):]: root for p, root in roots.items() if p.startswith(prefix)}
                    payload, _ = _rebuild(nested[info.filename], inner_roots, inner_changed, inner_drop)
                    target.writestr(info, payload, compress_type=info.compress_type)
                    continue
                payload = original
            else:
                payload = roots[info.filename].toxml(encoding="utf-8") if info.filename in changed else original
            target.writestr(info, payload, compress_type=info.compress_type)
            if info.filename not in changed:
                hashes[info.filename] = hashlib.sha256(original).hexdigest()
    return output.getvalue(), hashes


def redact_office(data: bytes, replacements: list[dict], file_format: str) -> dict:
    grouped = _validate_replacements(replacements)
    warnings = probe_office(data, file_format)
    package, roots, _, drop, nested = _load(data, file_format)
    try:
        units = {(unit.part, unit.index): unit for unit in _collect(roots)}
        if any(key not in units for key in grouped):
            raise OfficeUnsupportedError("invalid_replacement_range")
        ledger: list[dict] = []
        changed: set[str] = set()
        for key, spans in grouped.items():
            unit = units[key]
            ledger.extend(_patch(unit, spans))
            changed.add(unit.part)
            if unit.numeric_cell is not None:
                warnings.append({"code": "numeric_cell_converted_to_text", "message": "命中的数值单元格已改为文本；回填后继续保留文本类型", "part": unit.part})
        if any(item["crossed_nodes"] for item in ledger):
            warnings.append({"code": "run_boundary_crossed", "message": "跨文本片段替换保留原格式节点，占位符和回填文本采用起始片段格式"})
        changed.update(_strip_output_metadata(roots, drop))
        rebuilt, hashes = _rebuild(package, roots, changed, drop, nested)
        return {"data": rebuilt, "ledger": ledger, "warnings": warnings, "untouched_hashes": hashes}
    finally:
        _close_all(package, nested)


def refill_office(data: bytes, values: dict[str, str], file_format: str) -> dict:
    if not isinstance(values, dict) or any(not isinstance(key, str) or not PLACEHOLDER_RE.fullmatch(key) or not isinstance(value, str) or any(not (char in "\t\n\r" or " " <= char <= "\ud7ff" or "\ue000" <= char <= "\ufffd" or "\U00010000" <= char <= "\U0010ffff") for char in value) for key, value in values.items()):
        raise OfficeUnsupportedError("invalid_refill_values")
    warnings = probe_office(data, file_format)
    package, roots, _, drop, nested = _load(data, file_format)
    try:
        units = _collect(roots)
        error_types: set[str] = set()
        diff: dict[str, list[dict]] = {"unknown": [], "altered": [], "readonly": []}
        for unit in units:
            placeholders = set(PLACEHOLDER_RE.findall(unit.text))
            unknown = placeholders - values.keys()
            altered = find_altered_placeholders(unit.text)
            for found, category, code in ((unknown, "unknown", "unknown_placeholder"), (altered, "altered", "altered_placeholder"), (placeholders if unit.readonly else set(), "readonly", "readonly_placeholder")):
                if found:
                    error_types.add(code)
                    # Do not return raw source fragments or unknown values.
                    diff[category].append({"part": unit.part, "paragraph": unit.index, "count": len(found)})
        if error_types:
            return {"status": "blocked", "error_types": sorted(error_types), "diff": {key: value for key, value in diff.items() if value}}
        changed: set[str] = set()
        restored_count = 0
        for unit in units:
            spans = [{"start": match.start(), "end": match.end(), "value": values[match.group()]} for match in PLACEHOLDER_RE.finditer(unit.text)]
            if spans:
                _patch(unit, spans, refill=True)
                restored_count += len(spans)
                changed.add(unit.part)
        changed.update(_strip_output_metadata(roots, drop))
        rebuilt, hashes = _rebuild(package, roots, changed, drop, nested)
        return {"status": "ok", "data": rebuilt, "restored_count": restored_count, "warnings": warnings, "untouched_hashes": hashes}
    finally:
        _close_all(package, nested)

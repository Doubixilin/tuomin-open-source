"""Pure-standard-library DOCX OOXML text patching and safe refill."""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from tuomin_gateway.placeholders import PLACEHOLDER_RE, find_altered_placeholders

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(name: str) -> str:
    return f"{{{_W}}}{name}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class TextNodeRef:
    part: str
    paragraph: int
    node: int
    start: int
    text: str


@dataclass(frozen=True)
class ParagraphText:
    part: str
    index: int
    text: str
    nodes: tuple[TextNodeRef, ...]


class DocxUnsupportedError(ValueError):
    code: str

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
        package.infolist()
        return package
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise DocxUnsupportedError("not_a_package", "Not a valid DOCX package") from exc


def _patchable_parts(names: list[str]) -> list[str]:
    pattern = re.compile(r"word/(?:header|footer)\d*\.xml$")
    return [name for name in names if name == "word/document.xml" or pattern.fullmatch(name)]


def _parse(raw: bytes, part: str) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML part: {part}") from exc


def _has_nonempty_text(root: ET.Element) -> bool:
    return any((node.text or "").strip() for node in root.iter(_qn("t")))


def _own_text_nodes(paragraph: ET.Element) -> list[ET.Element]:
    """A paragraph's own ``w:t`` nodes, excluding nested textbox content.

    VML/DrawingML text boxes nest full ``w:p`` structures inside the host
    paragraph (``w:p > w:r > w:pict/w:drawing > … > w:txbxContent > w:p``).
    A plain ``iter(w:t)`` would count the nested paragraph's text in BOTH the
    host and the nested paragraph — double-covering it in detection and
    corrupting coordinates when both get patched (observed on a real contract
    cover page: patching the host shrank the nested paragraph's nodes, then
    the nested replacement failed its range check). Nested textbox paragraphs
    are enumerated as their own paragraphs and patched there.
    """
    nodes: list[ET.Element] = []

    def walk(element: ET.Element) -> None:
        for child in list(element):
            if child.tag == _qn("txbxContent"):
                continue
            if child.tag == _qn("t"):
                nodes.append(child)
                continue
            walk(child)

    walk(paragraph)
    return nodes


def _notes_have_text(root: ET.Element) -> bool:
    for note in list(root):
        if _local(note.tag) not in {"footnote", "endnote"}:
            continue
        if note.get(_qn("type"), "") in {"separator", "continuationSeparator"}:
            continue
        if any((node.text or "").strip() for node in note.iter(_qn("t"))):
            return True
    return False


def _contains_smartart(root: ET.Element) -> bool:
    for frame in root.iter():
        if _local(frame.tag) != "graphicFrame":
            continue
        if any("diagram" in node.tag.lower() or "dgm" in node.tag.lower() for node in frame.iter()):
            return True
    return False


def probe_docx(data: bytes, allow_ole_strip: bool = False) -> list[dict[str, str]]:
    with _open(data) as package:
        names = package.namelist()
        name_set = set(names)
        if "word/document.xml" not in name_set:
            raise DocxUnsupportedError("not_a_package", "DOCX package lacks word/document.xml")
        if "EncryptionInfo" in name_set or any(name.endswith("/EncryptionInfo") for name in names):
            raise DocxUnsupportedError("encrypted_package")
        if any(name.lower().startswith("_xmlsignatures/") for name in names):
            raise DocxUnsupportedError("digital_signature")

        roots: list[tuple[str, ET.Element]] = []
        for part in _patchable_parts(names):
            roots.append((part, _parse(package.read(part), part)))
        document = next(root for part, root in roots if part == "word/document.xml")
        if any(document.find(f".//{_qn(kind)}") is not None for kind in ("ins", "del")):
            raise DocxUnsupportedError("tracked_changes")

        if "word/comments.xml" in name_set:
            comments = _parse(package.read("word/comments.xml"), "word/comments.xml")
            if _has_nonempty_text(comments):
                raise DocxUnsupportedError("comments_present")
        for part in ("word/footnotes.xml", "word/endnotes.xml"):
            if part in name_set and _notes_have_text(_parse(package.read(part), part)):
                raise DocxUnsupportedError("notes_present")

        all_roots = list(roots)
        for part in ("word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"):
            if part in name_set:
                all_roots.append((part, _parse(package.read(part), part)))

        warnings: list[dict[str, str]] = []
        # Text boxes: their paragraphs are ordinary nested w:p and ARE covered
        # by extraction/patching (engine test proves it), so text-bearing boxes
        # are patched with a warning instead of being blocked. Empty shape-only
        # boxes carry no text and need no warning.
        has_textbox = any(
            root.find(f".//{_qn('txbxContent')}") is not None for _, root in all_roots
        )
        if has_textbox:
            textbox_has_text = any(
                _has_nonempty_text(box)
                for _, root in all_roots
                for box in root.iter(_qn("txbxContent"))
            )
            if textbox_has_text:
                warnings.append({"code": "textbox_patched", "message": "文本框文本已纳入脱敏与回填范围"})
        if any(_contains_smartart(root) for _, root in roots):
            raise DocxUnsupportedError("smartart_present")
        # OLE 嵌入对象（WPS 公式、嵌入表格等）：二进制黑盒无法脱敏，默认
        # fail-closed 拒绝；allow_ole_strip 时降级为"整体移除对象"——删除是
        # 彻底的（.bin、关系、内容类型、w:object 子树全部清除），无残留泄漏面。
        ole_object_count = sum(
            1 for _, root in all_roots for node in root.iter() if _local(node.tag) == "object"
        )
        ole_ref_count = sum(
            1 for _, root in all_roots for node in root.iter() if _local(node.tag).lower() == "oleobject"
        )
        has_stray_ole = ole_ref_count > 0 and ole_object_count == 0
        if ole_object_count or has_stray_ole:
            # 游离 oleObject（不在 w:object 内）是未识别的形态，剥离也不能放行
            if has_stray_ole or not allow_ole_strip:
                raise DocxUnsupportedError("ole_object_present")
            warnings.append({
                "code": "ole_stripped",
                "message": f"已移除 {ole_object_count} 个 OLE 嵌入对象（公式/嵌入对象），正文脱敏与回填不受影响",
            })

        if any(any(_local(node.tag) in {"fldSimple", "instrText"} for node in root.iter()) for _, root in roots):
            warnings.append({"code": "field_codes", "message": "Field codes are preserved but not patched"})
        if any(name.startswith("word/media/") and not name.endswith("/") for name in names):
            warnings.append({"code": "images_present", "message": "Embedded images are preserved but not inspected"})
        return warnings


def _strip_ole_from_roots(roots: dict[str, tuple[ET.Element, bytes]]) -> int:
    """移除所有 ``w:object`` 子树（OLE 对象及其预览形状），返回移除数量。"""
    removed = 0
    for root, _ in roots.values():
        parents = {child: parent for parent in root.iter() for child in list(parent)}
        for node in [n for n in root.iter() if _local(n.tag) == "object"]:
            parent = parents.get(node)
            if parent is not None:
                parent.remove(node)
                removed += 1
    return removed


def _clean_rels_for_ole_strip(raw: bytes) -> bytes | None:
    """删除指向 embeddings 的关系条目；无改动返回 None。"""
    ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    root = ET.fromstring(raw)
    removed = False
    for rel in list(root):
        if "embeddings/" in rel.get("Target", ""):
            root.remove(rel)
            removed = True
    if not removed:
        return None
    ET.register_namespace("", ns)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _clean_content_types_for_ole_strip(raw: bytes) -> bytes | None:
    """删除 embeddings 部件的内容类型声明；无改动返回 None。"""
    ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    root = ET.fromstring(raw)
    removed = False
    for element in list(root):
        if element.get("PartName", "").startswith("/word/embeddings/"):
            root.remove(element)
            removed = True
    if not removed:
        return None
    ET.register_namespace("", ns)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _paragraphs_from_root(part: str, root: ET.Element) -> list[ParagraphText]:
    result: list[ParagraphText] = []
    for paragraph_index, paragraph in enumerate(root.iter(_qn("p"))):
        refs: list[TextNodeRef] = []
        pieces: list[str] = []
        offset = 0
        for node_index, node in enumerate(_own_text_nodes(paragraph)):
            text = node.text or ""
            refs.append(TextNodeRef(part, paragraph_index, node_index, offset, text))
            pieces.append(text)
            offset += len(text)
        result.append(ParagraphText(part, paragraph_index, "".join(pieces), tuple(refs)))
    return result


def extract_paragraph_texts(data: bytes, allow_ole_strip: bool = False) -> list[ParagraphText]:
    probe_docx(data, allow_ole_strip=allow_ole_strip)
    result: list[ParagraphText] = []
    with _open(data) as package:
        for part in _patchable_parts(package.namelist()):
            result.extend(_paragraphs_from_root(part, _parse(package.read(part), part)))
    return result


def _register_namespaces(raw: bytes) -> None:
    try:
        for _, pair in ET.iterparse(io.BytesIO(raw), events=("start-ns",)):
            prefix, uri = pair
            if prefix != "xml" and not re.fullmatch(r"ns\d+", prefix or ""):
                ET.register_namespace(prefix or "", uri)
    except (ET.ParseError, ValueError):
        pass
    ET.register_namespace("w", _W)


def _serialize(root: ET.Element, original: bytes) -> bytes:
    _register_namespaces(original)
    declaration = original.lstrip().startswith(b"<?xml")
    return ET.tostring(root, encoding="utf-8", xml_declaration=declaration)


def _rebuild(
    package: zipfile.ZipFile, changed: dict[str, bytes], drop: frozenset[str] = frozenset()
) -> tuple[bytes, dict[str, str]]:
    output = io.BytesIO()
    hashes: dict[str, str] = {}
    with zipfile.ZipFile(output, "w") as target:
        for info in package.infolist():
            if info.filename in drop:
                continue
            original = package.read(info.filename)
            payload = changed.get(info.filename, original)
            target.writestr(info, payload, compress_type=info.compress_type)
            if info.filename not in changed:
                hashes[info.filename] = hashlib.sha256(original).hexdigest()
    return output.getvalue(), hashes


def _run_properties(paragraph: ET.Element, text_node: ET.Element) -> bytes:
    parents = {child: parent for parent in paragraph.iter() for child in list(parent)}
    current = parents.get(text_node)
    while current is not None and current.tag != _qn("r"):
        current = parents.get(current)
    if current is None:
        return b""
    properties = current.find(_qn("rPr"))
    return ET.tostring(properties, encoding="utf-8") if properties is not None else b""


def redact_docx(data: bytes, replacements: list[dict], allow_ole_strip: bool = False) -> dict:
    base_warnings = probe_docx(data, allow_ole_strip=allow_ole_strip)
    ledger: list[dict] = []
    warnings: list[dict[str, str]] = list(base_warnings)
    grouped: dict[tuple[str, int], list[dict]] = {}
    for replacement in replacements:
        try:
            part = str(replacement["part"])
            paragraph = int(replacement["paragraph"])
            start = int(replacement["start"])
            end = int(replacement["end"])
            placeholder = str(replacement["placeholder"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid DOCX replacement") from exc
        if start >= end or not PLACEHOLDER_RE.fullmatch(placeholder):
            raise ValueError("Replacement span or placeholder is invalid")
        grouped.setdefault((part, paragraph), []).append({"start": start, "end": end, "placeholder": placeholder})

    with _open(data) as package:
        valid_parts = set(_patchable_parts(package.namelist()))
        if any(part not in valid_parts for part, _ in grouped):
            raise ValueError("Replacement references a non-patchable DOCX part")
        changed: dict[str, bytes] = {}
        roots: dict[str, tuple[ET.Element, bytes]] = {}
        for part, _ in grouped:
            if part not in roots:
                raw = package.read(part)
                roots[part] = (_parse(raw, part), raw)

        for (part, paragraph_index), spans in grouped.items():
            root, _ = roots[part]
            paragraphs = list(root.iter(_qn("p")))
            if paragraph_index < 0 or paragraph_index >= len(paragraphs):
                raise ValueError("Replacement paragraph is out of range")
            paragraph = paragraphs[paragraph_index]
            nodes = _own_text_nodes(paragraph)
            original_texts = [node.text or "" for node in nodes]
            starts: list[int] = []
            offset = 0
            for text in original_texts:
                starts.append(offset)
                offset += len(text)
            ordered = sorted(spans, key=lambda item: (item["start"], item["end"]))
            if any(item["start"] < 0 or item["end"] > offset for item in ordered):
                raise ValueError("Replacement span is out of paragraph range")
            if any(left["end"] > right["start"] for left, right in zip(ordered, ordered[1:])):
                raise ValueError("Replacement spans overlap")

            for span in reversed(ordered):
                covered = [i for i, (start, text) in enumerate(zip(starts, original_texts)) if start < span["end"] and start + len(text) > span["start"]]
                if not covered:
                    raise ValueError("Replacement span covers no text node")
                first, last = covered[0], covered[-1]
                local_start = span["start"] - starts[first]
                local_end = span["end"] - starts[last]
                prefix = (nodes[first].text or "")[:local_start]
                if first == last:
                    suffix = (nodes[first].text or "")[local_end:]
                    nodes[first].text = prefix + span["placeholder"] + suffix
                else:
                    suffix = (nodes[last].text or "")[local_end:]
                    nodes[first].text = prefix + span["placeholder"]
                    for index in covered[1:-1]:
                        nodes[index].text = ""
                    nodes[last].text = suffix
                formats = [_run_properties(paragraph, nodes[index]) for index in covered]
                format_mixed = len(set(formats)) > 1
                record = {
                    "part": part,
                    "paragraph": paragraph_index,
                    "node_start": first,
                    "node_end": last,
                    "start": span["start"],
                    "end": span["end"],
                    "placeholder": span["placeholder"],
                    "crossed_nodes": first != last,
                    "format_mixed": format_mixed,
                }
                ledger.append(record)
                if format_mixed:
                    warnings.append({"code": "format_boundary_crossed", "message": "Replacement crossed differently formatted runs", "part": part})

        drop: frozenset[str] = frozenset()
        if allow_ole_strip:
            # OLE 也可能藏在页眉页脚等没有文本替换的部件里，剥离需覆盖全部
            # 可补丁部件；包级清理（.bin / 关系 / 内容类型）同步进行。
            for part in valid_parts:
                if part not in roots:
                    raw = package.read(part)
                    roots[part] = (_parse(raw, part), raw)
            if _strip_ole_from_roots(roots):
                names = package.namelist()
                drop = frozenset(
                    n for n in names if n.startswith("word/embeddings/") and not n.endswith("/")
                )
                for rels_part in [n for n in names if n.startswith("word/_rels/") and n.endswith(".rels")]:
                    cleaned = _clean_rels_for_ole_strip(package.read(rels_part))
                    if cleaned is not None:
                        changed[rels_part] = cleaned
                cleaned_ct = _clean_content_types_for_ole_strip(package.read("[Content_Types].xml"))
                if cleaned_ct is not None:
                    changed["[Content_Types].xml"] = cleaned_ct

        for part, (root, raw) in roots.items():
            changed[part] = _serialize(root, raw)
        rebuilt, hashes = _rebuild(package, changed, drop)
    return {"data": rebuilt, "ledger": ledger, "warnings": warnings, "untouched_hashes": hashes}


def refill_docx(data: bytes, values: dict[str, str]) -> dict:
    probe_docx(data)
    mapping = {str(key): str(value) for key, value in values.items()}
    ordered_keys = sorted(mapping, key=len, reverse=True)
    replacement_re = re.compile("|".join(re.escape(key) for key in ordered_keys)) if ordered_keys else None
    error_types: set[str] = set()
    diff: dict[str, list[dict]] = {"unknown": [], "altered": [], "split": []}

    with _open(data) as package:
        roots: dict[str, tuple[ET.Element, bytes]] = {}
        for part in _patchable_parts(package.namelist()):
            raw = package.read(part)
            roots[part] = (_parse(raw, part), raw)
        for part, (root, _) in roots.items():
            for paragraph_index, paragraph in enumerate(root.iter(_qn("p"))):
                nodes = _own_text_nodes(paragraph)
                paragraph_text = "".join(node.text or "" for node in nodes)
                unknown = sorted(set(PLACEHOLDER_RE.findall(paragraph_text)) - set(mapping))
                if unknown:
                    error_types.add("unknown_placeholder")
                    diff["unknown"].append({"part": part, "paragraph": paragraph_index, "values": unknown})
                altered = find_altered_placeholders(paragraph_text)
                if altered:
                    error_types.add("altered_placeholder")
                    diff["altered"].append({"part": part, "paragraph": paragraph_index, "values": altered})
                split = [key for key in ordered_keys if key in paragraph_text and not any(key in (node.text or "") for node in nodes)]
                if split:
                    error_types.add("placeholder_split_across_runs")
                    diff["split"].append({"part": part, "paragraph": paragraph_index, "values": split})
        if error_types:
            return {"status": "blocked", "error_types": sorted(error_types), "diff": {key: value for key, value in diff.items() if value}}

        changed: dict[str, bytes] = {}
        restored_count = 0
        for part, (root, raw) in roots.items():
            part_changed = False
            for node in root.iter(_qn("t")):
                text = node.text or ""
                if replacement_re is None:
                    continue
                replaced, count = replacement_re.subn(lambda match: mapping[match.group(0)], text)
                if count:
                    node.text = replaced
                    restored_count += count
                    part_changed = True
            if part_changed:
                changed[part] = _serialize(root, raw)
        rebuilt, hashes = _rebuild(package, changed)
    return {"status": "ok", "data": rebuilt, "restored_count": restored_count, "untouched_hashes": hashes}

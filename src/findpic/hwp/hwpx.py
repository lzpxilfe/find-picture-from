"""HWPX(.hwpx) 읽기.

HWPX 는 ZIP 안에 XML 이 든 형식이라 바이너리 HWP 보다 다루기 쉽지만, 나름의
함정이 있다. 여기서 지키는 것들(모두 실제 문서 다발로 확인한 규칙):

  * binaryItemIDRef 는 **문자열 열쇠**다. 숫자만 뽑아 쓰면 안 된다.
    'image12' 로 시작하는 문서, 차례가 뒤섞인 문서, 'BINHDR' 처럼 숫자가 아예
    없는 문서가 실제로 있다. 반드시 content.hpf 의 item/@id 로 찾아야 한다.
  * borderFill 도 id 속성으로 찾는다. 순서대로 1,2,3... 이 아닌 문서가 있다.
  * <hp:tr> 은 그 행에서 **시작하는** 셀만 담는다. 행 번호는 cellAddr 를 봐야 한다.
  * 셀 안 그림은 <hp:run> 밑에 바로 있기도 하고 <hp:container> 밑에 묶여 있기도 하다.
  * 이름공간 접두어는 문서마다 다를 수 있으니 지역 이름(local name)으로 맞춘다.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from ..caption import build_slots, extract_fields
from ..model import BinItem, Cell, HwpDocument, Table
from .section import parse_picture_hint
from .reader import EncryptedHwpError, HwpError

MANIFEST = "Contents/content.hpf"
HEADER = "Contents/header.xml"
ODF_MANIFEST = "META-INF/manifest.xml"

# <hp:t> 안에 섞여 나오는 표시 요소를 글자로 바꾸는 표
_TEXT_MARKS = {
    "tab": "\t",
    "lineBreak": "\n",
    "columnBreak": "\n",
    "nbSpace": " ",
    "fwSpace": " ",
    "hypen": "­",      # 스펙의 철자가 'hypen' 이다 ('hyphen' 아님)
    "hyphen": "­",
    "softHyphen": "­",
    "titleMark": "",
    "markpenBegin": "",
    "markpenEnd": "",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_local(node, name: str):
    for child in node.iter():
        if _local(child.tag) == name:
            yield child


def _find_local(node, name: str):
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class _Zip:
    """ZIP 안 이름을 대소문자 구분 없이 찾아 준다."""

    def __init__(self, path: Path):
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise HwpError(f"HWPX(zip) 파일을 열 수 없습니다: {exc}") from exc
        self._lower = {n.lower(): n for n in self.zf.namelist()}

    def close(self):
        try:
            self.zf.close()
        except Exception:
            pass

    def read(self, name: str) -> bytes:
        real = self._lower.get(name.lower())
        if real is None:
            raise HwpError(f"HWPX 안에 {name} 이 없습니다")
        return self.zf.read(real)

    def has(self, name: str) -> bool:
        return name.lower() in self._lower

    def names(self) -> List[str]:
        return self.zf.namelist()


def _check_encrypted(z: _Zip) -> None:
    if not z.has(ODF_MANIFEST):
        return
    try:
        blob = z.read(ODF_MANIFEST)
    except HwpError:
        return
    if b"encryption-data" in blob:
        raise EncryptedHwpError("암호가 걸린 HWPX 파일입니다")


def _read_manifest(z: _Zip) -> Dict[str, dict]:
    """content.hpf 의 item/@id -> {'href':..., 'embedded':bool}"""
    out: Dict[str, dict] = {}
    try:
        root = ET.fromstring(z.read(MANIFEST))
    except (HwpError, ET.ParseError) as exc:
        raise HwpError(f"HWPX 목록(content.hpf)을 읽지 못했습니다: {exc}") from exc
    for item in _iter_local(root, "item"):
        item_id = item.get("id")
        if not item_id:
            continue
        out[item_id] = {
            "href": item.get("href", ""),
            "media": item.get("media-type", ""),
            # 스펙 원문의 철자가 'isEmbeded' 다 (d 하나)
            "embedded": item.get("isEmbeded", "1") != "0",
        }
    return out


def _read_border_fills(z: _Zip) -> Dict[str, str]:
    """borderFill id(문자열) -> 이미지 binaryItemIDRef(문자열)"""
    out: Dict[str, str] = {}
    if not z.has(HEADER):
        return out
    try:
        root = ET.fromstring(z.read(HEADER))
    except ET.ParseError:
        return out
    for bf in _iter_local(root, "borderFill"):
        bf_id = bf.get("id")
        if not bf_id:
            continue
        for brush in _iter_local(bf, "imgBrush"):
            img = next(_iter_local(brush, "img"), None)
            if img is not None and img.get("binaryItemIDRef"):
                out[bf_id] = img.get("binaryItemIDRef")
                break
    return out


def _run_text(node) -> str:
    """<hp:t> 하나에서 글자를 모은다. 자식 요소 사이의 tail 을 놓치면 글이 잘린다."""
    parts = [node.text or ""]
    for child in node:
        parts.append(_TEXT_MARKS.get(_local(child.tag), ""))
        parts.append(child.text or "")
        parts.append(child.tail or "")
    return "".join(parts)


def _own_cells(tbl):
    """표가 직접 가진 셀만. 셀 안에 든 표의 셀은 그 표가 따로 맡는다.

    node.iter() 로 훑으면 중첩 표의 셀까지 바깥 표 목록에 딸려 들어온다.
    그러면 같은 (행, 열)이 여러 번 나오고, 캡션이 뒤바뀌거나 한 사진이
    두 번 잡힌다. 실제 문서 다발에서 334군데에 이 일이 일어났다.
    """
    out = []

    def walk(node):
        for child in node:
            name = _local(child.tag)
            if name == "tbl":
                continue                # 중첩 표는 그 표의 차례에 처리된다
            if name == "tc":
                out.append(child)
                continue                # 셀 속으로는 내려가지 않는다
            walk(child)

    walk(tbl)
    return out


def _cell_text(tc) -> str:
    """셀의 글. 셀 안에 또 표가 있으면 그 안쪽은 세지 않는다."""
    lines: List[str] = []

    def walk_paragraph(node, buf: List[str]) -> None:
        for child in node:
            name = _local(child.tag)
            if name == "tbl":
                continue                       # 중첩 표는 그 표의 셀이 따로 맡는다
            if name == "t":
                buf.append(_run_text(child))
            elif name in ("tab", "lineBreak", "columnBreak"):
                buf.append(_TEXT_MARKS.get(name, ""))
            elif name in ("linesegarray", "pic", "shapeComment"):
                continue
            else:
                walk_paragraph(child, buf)

    sub = _find_local(tc, "subList")
    if sub is None:
        return ""
    for para in sub:
        if _local(para.tag) != "p":
            continue
        buf: List[str] = []
        walk_paragraph(para, buf)
        line = "".join(buf).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _cell_pictures(tc) -> List[tuple]:
    """셀 안 그림들. (binaryItemIDRef, 그림 설명) 목록. 중첩 표 안쪽은 제외."""
    out: List[tuple] = []

    def walk(node) -> None:
        for child in node:
            name = _local(child.tag)
            if name == "tbl":
                continue
            if name == "pic":
                img = next((e for e in child.iter() if _local(e.tag) == "img"), None)
                ref = img.get("binaryItemIDRef") if img is not None else None
                comment = next((e for e in child.iter() if _local(e.tag) == "shapeComment"), None)
                note = "".join(comment.itertext()).strip() if comment is not None else ""
                if ref:
                    out.append((ref, note))
                continue
            walk(child)

    sub = _find_local(tc, "subList")
    if sub is not None:
        walk(sub)
    return out


def extract_hwpx(path, *, min_edge: Optional[int] = None,
                 min_pixels: Optional[int] = None,
                 include_floating: bool = False) -> HwpDocument:
    from .extract import MIN_EDGE, MIN_PIXELS, is_photo_like

    min_edge = MIN_EDGE if min_edge is None else min_edge
    min_pixels = MIN_PIXELS if min_pixels is None else min_pixels

    path = Path(path)
    doc = HwpDocument(path=path, format="hwpx")
    z = _Zip(path)
    try:
        _check_encrypted(z)
        manifest = _read_manifest(z)
        bf_image = _read_border_fills(z)

        # 문자열 참조를 문서 안에서만 쓰는 정수 번호로 바꾼다 (HWP 쪽과 모양을 맞추기 위해)
        ref_to_id: Dict[str, int] = {}
        bf_to_id: Dict[str, int] = {}

        def bin_id_for(ref: str) -> int:
            if ref not in ref_to_id:
                ref_to_id[ref] = len(ref_to_id) + 1
            return ref_to_id[ref]

        def bf_id_for(bf_ref: str) -> int:
            if bf_ref not in bf_to_id:
                bf_to_id[bf_ref] = len(bf_to_id) + 1
            return bf_to_id[bf_ref]

        bf_int_to_bin: Dict[int, int] = {}
        sections = sorted(
            (n for n in z.names() if n.lower().startswith("contents/section")
             and n.lower().endswith(".xml")),
            key=lambda n: _int("".join(ch for ch in Path(n).stem if ch.isdigit()), 0),
        )
        order = 0
        for si, name in enumerate(sections):
            try:
                root = ET.fromstring(z.read(name))
            except (HwpError, ET.ParseError) as exc:
                doc.warnings.append(f"{name} 을 읽지 못했습니다: {exc}")
                continue
            for tbl in _iter_local(root, "tbl"):
                order += 1
                table = Table(section=si, order=order,
                              row_count=_int(tbl.get("rowCnt")),
                              col_count=_int(tbl.get("colCnt")))
                for tc in _own_cells(tbl):
                    addr = _find_local(tc, "cellAddr")
                    span = _find_local(tc, "cellSpan")
                    bf_ref = tc.get("borderFillIDRef") or ""
                    cell = Cell(
                        row=_int(addr.get("rowAddr")) if addr is not None else 0,
                        col=_int(addr.get("colAddr")) if addr is not None else 0,
                        col_span=max(1, _int(span.get("colSpan"), 1) if span is not None else 1),
                        row_span=max(1, _int(span.get("rowSpan"), 1) if span is not None else 1),
                        border_fill_id=bf_id_for(bf_ref) if bf_ref else 0,
                        text=_cell_text(tc),
                    )
                    if bf_ref and bf_ref in bf_image:
                        bf_int_to_bin[cell.border_fill_id] = bin_id_for(bf_image[bf_ref])
                    for ref, note in _cell_pictures(tc):
                        bid = bin_id_for(ref)
                        cell.inline_bin_ids.append(bid)
                        hint = parse_picture_hint(note)
                        if hint is not None:
                            doc.hints.setdefault(bid, hint)
                    table.cells.append(cell)
                if table.cells:
                    doc.tables.append(table)

        # 실제 바이트 읽어오기
        for ref, bin_id in ref_to_id.items():
            entry = manifest.get(ref)
            if entry is None:
                doc.warnings.append(f"'{ref}' 그림이 문서 목록에 없습니다")
                continue
            href = entry["href"]
            if not entry["embedded"] or not z.has(href):
                doc.bin_items[bin_id] = BinItem(
                    bin_id=bin_id, ext=Path(href).suffix.lstrip(".").lower(),
                    data=b"", stream=href, kind="link", link_path=href,
                )
                continue
            try:
                data = z.read(href)
            except HwpError as exc:
                doc.warnings.append(f"'{ref}' 그림을 꺼내지 못했습니다: {exc}")
                continue
            doc.bin_items[bin_id] = BinItem(
                bin_id=bin_id, ext=Path(href).suffix.lstrip(".").lower(),
                data=data, stream=href,
            )
    finally:
        z.close()

    dropped = 0
    for bin_id, item in list(doc.bin_items.items()):
        if not is_photo_like(item, min_edge=min_edge, min_pixels=min_pixels):
            del doc.bin_items[bin_id]
            dropped += 1
    if dropped:
        doc.warnings.append(f"아이콘·머리표처럼 사진으로 보기 어려운 그림 {dropped}종은 건너뛰었습니다")

    def image_of(cell):
        bid = bf_int_to_bin.get(cell.border_fill_id)
        return bid if bid in doc.bin_items else None

    for table in doc.tables:
        for cell in table.cells:
            cell.inline_bin_ids = [b for b in cell.inline_bin_ids if b in doc.bin_items]

    doc.slots = build_slots(doc, image_of)
    doc.fields = extract_fields(doc.tables)
    return doc

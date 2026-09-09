"""한글 파일 하나 -> HwpDocument (사진 + 이름 + 문서 정보)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..caption import build_slots, extract_fields
from ..model import BinItem, HwpDocument
from ..imaging.loader import real_size
from . import docinfo as DI
from .reader import EncryptedHwpError, Hwp5File, HwpError, _inflate
from .section import parse_section

# 이미지 파일의 첫머리 서명. 압축 해제가 제대로 됐는지 확인하는 데 쓴다.
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",            # JPEG
    b"\x89PNG\r\n\x1a\n",       # PNG
    b"GIF8",                    # GIF
    b"BM",                      # BMP
    b"II*\x00", b"MM\x00*",     # TIFF
    b"RIFF",                    # WEBP
    b"\x01\x00\x00\x00",        # EMF
    b"\xd7\xcd\xc6\x9a",        # WMF(placeable)
)


# 사진이 아니라 아이콘·머리표·체크박스인 그림을 걸러내는 최소 크기.
# 보고서에 싣는 사진은 아무리 작아도 이보다 크다.
MIN_EDGE = 200
MIN_PIXELS = 80_000
# 사진이 아니라 도형인 형식들. 원본 사진 폴더에서 찾을 것이 없다.
VECTOR_EXTENSIONS = {"wmf", "emf", "ole", "svg", "dxf"}


def _looks_like_image(data: bytes) -> bool:
    return any(data.startswith(sig) for sig in _IMAGE_MAGIC)


def is_photo_like(item: BinItem, *, min_edge: int = MIN_EDGE,
                  min_pixels: int = MIN_PIXELS) -> bool:
    """사진이라고 볼 만한 크기인가."""
    if item.kind == "link":
        return True                     # 파일이 없어 판단 불가. 일단 살린다.
    if (item.ext or "").lower() in VECTOR_EXTENSIONS:
        return False
    size = real_size(item.data)
    if size is None:
        return False
    w, h = size
    return min(w, h) >= min_edge and w * h >= min_pixels


def _decode_bin(raw: bytes, entry: DI.BinDataEntry, file_compressed: bool) -> bytes:
    """BIN_DATA 의 압축 방침에 따라 풀어 준다. 방침이 틀려도 결과로 판정한다."""
    if entry.compress == DI.COMPRESS_ALWAYS:
        order = (True, False)
    elif entry.compress == DI.COMPRESS_NEVER:
        order = (False, True)
    else:
        order = (file_compressed, not file_compressed)
    best = raw
    for want_inflate in order:
        if not want_inflate:
            if _looks_like_image(raw):
                return raw
            best = raw
            continue
        try:
            out = _inflate(raw)
        except HwpError:
            continue
        if _looks_like_image(out):
            return out
        best = out or best
    return best


def extract_hwp5(path, *, min_edge: int = MIN_EDGE, min_pixels: int = MIN_PIXELS,
                 include_floating: bool = False) -> HwpDocument:
    path = Path(path)
    doc = HwpDocument(path=path, format="hwp")
    with Hwp5File(path) as f:
        if f.header.distribution:
            doc.warnings.append(
                "배포용(읽기 전용) 문서입니다. 본문이 따로 암호화되어 있어 표와 사진을 읽을 수 없습니다. "
                "한글에서 열어 일반 문서로 다시 저장한 뒤 실행해 주세요."
            )
        info = DI.parse_doc_info(f.stream("DocInfo"))
        doc.warnings.extend(info.warnings)
        max_bin = len(info.bin_data)
        floating = []

        for i, name in enumerate(f.section_names()):
            try:
                buf = f.stream(name)
            except HwpError as exc:
                doc.warnings.append(f"{name} 을 읽지 못했습니다: {exc}")
                continue
            result = parse_section(buf, section_index=i, max_bin=max_bin)
            doc.tables.extend(result.tables)
            doc.hints.update(result.hints)
            doc.warnings.extend(result.warnings)
            if include_floating:
                floating.extend((i, para, bin_id, result.body_text)
                                for para, bin_id in result.floating)

        used = set()
        for _si, _para, bin_id, _texts in floating:
            used.add(bin_id)
        for table in doc.tables:
            for cell in table.cells:
                bid = info.image_bin_id_for_border_fill(cell.border_fill_id)
                if bid:
                    used.add(bid)
                used.update(cell.inline_bin_ids)

        for bin_id in sorted(used):
            entry = info.bin_entry(bin_id)
            if entry is None:
                doc.warnings.append(f"{bin_id}번 그림 정보가 문서에 없습니다")
                continue
            if entry.is_link:
                doc.bin_items[bin_id] = BinItem(
                    bin_id=bin_id, ext="", data=b"", stream="",
                    kind="link", link_path=entry.abs_path or entry.rel_path,
                )
                continue
            stream_name = f.find_bindata(entry.bin_id or bin_id)
            if stream_name is None:
                doc.warnings.append(f"{bin_id}번 그림 파일을 문서 안에서 찾지 못했습니다")
                continue
            raw = f.raw(stream_name)
            data = _decode_bin(raw, entry, f.header.compressed)
            doc.bin_items[bin_id] = BinItem(
                bin_id=bin_id,
                ext=(entry.ext or stream_name.rsplit(".", 1)[-1]).lower(),
                data=data,
                stream=stream_name,
            )

    # 아이콘·머리표 같은 작은 그림은 사진으로 치지 않는다
    dropped = 0
    for bin_id, item in list(doc.bin_items.items()):
        if not is_photo_like(item, min_edge=min_edge, min_pixels=min_pixels):
            del doc.bin_items[bin_id]
            dropped += 1
    if dropped:
        doc.warnings.append(f"아이콘·머리표처럼 사진으로 보기 어려운 그림 {dropped}종은 건너뛰었습니다")

    def image_of(cell):
        bid = info.image_bin_id_for_border_fill(cell.border_fill_id)
        return bid if bid in doc.bin_items else None

    for table in doc.tables:
        for cell in table.cells:
            cell.inline_bin_ids = [b for b in cell.inline_bin_ids if b in doc.bin_items]

    doc.slots = build_slots(doc, image_of)
    if include_floating:
        doc.slots.extend(_floating_slots(floating, doc))
        for order, slot in enumerate(doc.slots, start=1):
            slot.doc_order = order
    doc.fields = extract_fields(doc.tables)
    return doc


def _floating_slots(floating, doc: HwpDocument):
    """표 밖에 떠 있는 그림의 이름은 바로 다음(없으면 바로 앞) 문단에서 가져온다."""
    from ..caption import MAX_CAPTION_LEN, normalize_text
    from ..model import PhotoSlot

    out = []
    for section, para, bin_id, texts in floating:
        if bin_id not in doc.bin_items:
            continue
        after = [t for i, t in texts if i > para]
        before = [t for i, t in texts if i <= para]
        caption, source = "", ""
        for candidates, label in ((after[:1], "다음 문단"), (before[-1:], "앞 문단")):
            for text in candidates:
                clean = normalize_text(text)
                if clean and len(clean) <= MAX_CAPTION_LEN:
                    caption, source = clean, label
                    break
            if caption:
                break
        out.append(PhotoSlot(bin_id=bin_id, caption=caption, caption_source=source,
                             placement="floating", section=section))
    return out


# 파일 첫머리 서명. 확장자가 거짓말하는 파일이 실제로 있어 이걸로 판별한다.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


def sniff_format(path) -> str:
    """확장자가 아니라 내용으로 형식을 가린다.

    .hwpx 인데 속은 HWP 5.x 인 파일, .hwp 인데 속은 HWPX 인 파일이 실제로 돌아다닌다.
    확장자만 믿으면 '읽을 수 없는 파일' 로 잘못 처리하게 된다.
    """
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        head = b""
    if head.startswith(_OLE_MAGIC):
        return "hwp"
    if head.startswith(_ZIP_MAGIC):
        return "hwpx"
    return "hwpx" if path.suffix.lower() == ".hwpx" else "hwp"


def extract(path, **kwargs) -> HwpDocument:
    """내용을 보고 알맞은 방식으로 연다."""
    path = Path(path)
    if sniff_format(path) == "hwpx":
        from .hwpx import extract_hwpx

        return extract_hwpx(path, **kwargs)
    return extract_hwp5(path, **kwargs)


__all__ = ["extract", "extract_hwp5", "EncryptedHwpError", "HwpError"]

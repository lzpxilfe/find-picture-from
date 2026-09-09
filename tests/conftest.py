"""테스트용 도우미.

진짜 한글 파일을 저장소에 넣지 않고도 파서를 검증할 수 있도록,
레코드 스트림을 바이트로 직접 만들어 쓴다.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from findpic.hwp import tags as T  # noqa: E402


def record(tag: int, level: int, payload: bytes) -> bytes:
    """레코드 하나를 바이트로. 4096바이트 이상이면 확장 길이 형식을 쓴다."""
    size = len(payload)
    if size < 0xFFF:
        head = (size << 20) | (level << 10) | tag
        return struct.pack("<I", head) + payload
    head = (0xFFF << 20) | (level << 10) | tag
    return struct.pack("<I", head) + struct.pack("<I", size) + payload


def wchars(text: str) -> bytes:
    """UINT16 길이 + UTF-16LE."""
    return struct.pack("<H", len(text)) + text.encode("utf-16le")


def para_text(text: str) -> bytes:
    """PARA_TEXT payload. 문단 끝 표시를 붙인다."""
    return text.encode("utf-16le") + struct.pack("<H", 13)


def bin_data_record(bin_id: int, ext: str, *, compress: int = 2) -> bytes:
    prop = 1 | (compress << 4)          # EMBEDDING
    return struct.pack("<HH", prop, bin_id) + wchars(ext)


def border_fill_record(*, image_bin_id: int = 0, fill_mode: int = 5,
                       solid: bool = False, gradient_colors: int = 0) -> bytes:
    """테두리/배경 레코드.

    실제 문서에서 확인한 채우기 블록 순서를 그대로 따른다.
        fillType(u32) -> [단색 12바이트] -> [그러데이션] -> [이미지 6바이트]
        -> 추가정보 크기(u32) -> 종류마다 투명도 1바이트
    """
    body = struct.pack("<H", 0)                 # 속성
    body += b"\x00\x00\x00\x00\x00\x00" * 5     # 테두리 4방향 + 대각선

    fill_type = 0
    if solid:
        fill_type |= 0x01
    if image_bin_id:
        fill_type |= 0x02
    if gradient_colors:
        fill_type |= 0x04
    body += struct.pack("<I", fill_type)
    if not fill_type:
        return body + struct.pack("<I", 0)

    if solid:
        body += struct.pack("<IIi", 0x00FFFFFF, 0, -1)      # 배경색, 무늬색, 무늬 종류
    if gradient_colors:
        n = gradient_colors
        body += struct.pack("<BIIIII", 1, 0, 50, 50, 50, n)
        if n > 2:
            body += b"\x00" * (4 * n)                       # 색이 바뀌는 위치
        body += b"\x00" * (4 * n)                           # 색상
    if image_bin_id:
        body += struct.pack("<BbbB", fill_mode, 0, 0, 0)
        body += struct.pack("<H", image_bin_id)
    body += struct.pack("<I", 0)                            # 추가정보 크기
    body += b"\x00" * bin(fill_type).count("1")             # 종류별 투명도
    return body


def cell_record(*, col: int, row: int, col_span: int = 1, row_span: int = 1,
                border_fill_id: int = 1) -> bytes:
    body = struct.pack("<II", 1, 0x20)                     # 문단 수, 속성
    body += struct.pack("<HHHH", col, row, col_span, row_span)
    body += struct.pack("<II", 1000, 1000)                 # 셀 크기
    body += struct.pack("<HHHH", 0, 0, 0, 0)               # 여백
    body += struct.pack("<H", border_fill_id)
    body += b"\x00" * 12
    return body


def table_record(rows: int, cols: int) -> bytes:
    return struct.pack("<IHH", 0, rows, cols) + b"\x00" * 40


def ctrl_header(ctrl_id: str, extra: bytes = b"") -> bytes:
    return ctrl_id.encode("latin-1")[::-1] + (extra or b"\x00" * 20)


def build_section(cells) -> bytes:
    """cells: [(col,row,colspan,rowspan,border_fill_id,text), ...] 로 표 하나짜리 섹션."""
    out = record(T.PARA_HEADER, 0, b"\x00" * 24)
    out += record(T.CTRL_HEADER, 1, ctrl_header("tbl "))
    rows = max((c[1] + c[3]) for c in cells) if cells else 0
    cols = max((c[0] + c[2]) for c in cells) if cells else 0
    out += record(T.TABLE, 2, table_record(rows, cols))
    for col, row, cs, rs, bf, text in cells:
        out += record(T.LIST_HEADER, 2,
                      cell_record(col=col, row=row, col_span=cs, row_span=rs, border_fill_id=bf))
        out += record(T.PARA_HEADER, 2, b"\x00" * 24)
        if text:
            out += record(T.PARA_TEXT, 3, para_text(text))
    return out


def build_doc_info(bin_items, border_fills) -> bytes:
    """bin_items: [(storage_id, ext), ...]

    border_fills 의 각 항목은 image_bin_id 정수이거나,
    border_fill_record 에 그대로 넘길 dict 다.
    """
    out = record(T.DOCUMENT_PROPERTIES, 0, b"\x00" * 26)
    for storage_id, ext in bin_items:
        out += record(T.BIN_DATA, 1, bin_data_record(storage_id, ext))
    for entry in border_fills:
        kwargs = entry if isinstance(entry, dict) else {"image_bin_id": entry}
        out += record(T.BORDER_FILL, 1, border_fill_record(**kwargs))
    return out


def make_jpeg(width: int, height: int, *, seed: int = 0, exif: bytes = None) -> bytes:
    """테스트용 그림 한 장. seed 를 바꾸면 확실히 다른 그림이 나온다."""
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, size=(8, 8, 3), dtype=np.uint8)
    im = Image.fromarray(base).resize((width, height), Image.BICUBIC)
    buf = io.BytesIO()
    if exif:
        im.save(buf, "JPEG", quality=92, exif=exif)
    else:
        im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


@pytest.fixture
def tmp_photos(tmp_path):
    """원본 사진 폴더 흉내."""
    folder = tmp_path / "originals"
    folder.mkdir()
    return folder

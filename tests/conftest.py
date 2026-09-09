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


def border_fill_record(*, image_bin_id: int = 0, fill_mode: int = 5) -> bytes:
    """테두리/배경 레코드. image_bin_id 가 0 이면 채우기 없음."""
    body = struct.pack("<H", 0)                 # 속성
    body += b"\x00\x00\x00\x00\x00\x00" * 5     # 테두리 4방향 + 대각선
    if image_bin_id:
        body += struct.pack("<I", 0x02)         # 이미지 채우기
        body += struct.pack("<BbbB", fill_mode, 0, 0, 0)
        body += struct.pack("<H", image_bin_id)
        body += b"\x00" * 5
    else:
        body += struct.pack("<I", 0)
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
    """bin_items: [(storage_id, ext), ...] / border_fills: [image_bin_id 또는 0, ...]"""
    out = record(T.DOCUMENT_PROPERTIES, 0, b"\x00" * 26)
    for storage_id, ext in bin_items:
        out += record(T.BIN_DATA, 1, bin_data_record(storage_id, ext))
    for image_bin_id in border_fills:
        out += record(T.BORDER_FILL, 1, border_fill_record(image_bin_id=image_bin_id))
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

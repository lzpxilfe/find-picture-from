"""DocInfo 스트림에서 필요한 것만 뽑아낸다.

우리가 쓰는 건 두 가지뿐이다.
  * HWPTAG_BIN_DATA  : 문서에 박힌 이진 자료(사진) 목록
  * HWPTAG_BORDER_FILL : 셀 배경으로 쓰인 '이미지 채우기'가 어느 사진을 가리키는지
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import tags as T
from .records import Reader, iter_records

# BIN_DATA 속성의 자료 종류 (하위 4비트)
BIN_LINK = 0
BIN_EMBEDDING = 1
BIN_STORAGE = 2

# 압축 방침 (비트 4-5)
COMPRESS_FOLLOW_STORAGE = 0
COMPRESS_ALWAYS = 1
COMPRESS_NEVER = 2

# 테두리/배경의 채우기 종류 비트
FILL_SOLID = 0x01
FILL_IMAGE = 0x02
FILL_GRADATION = 0x04

# 테두리 4방향 + 대각선 = 5개, 각 6바이트. 앞의 속성 UINT16 을 더해 32바이트.
_BORDER_HEADER_LEN = 2 + 6 * 5


@dataclass
class BinDataEntry:
    index: int                  # DocInfo 에 나온 순서 (1부터). 문서가 참조하는 번호다.
    kind: int = BIN_EMBEDDING
    bin_id: int = 0             # 스트림 이름에 쓰이는 번호
    ext: str = ""
    abs_path: str = ""
    rel_path: str = ""
    compress: int = COMPRESS_FOLLOW_STORAGE
    raw_property: int = 0

    @property
    def is_link(self) -> bool:
        return self.kind == BIN_LINK


@dataclass
class BorderFillEntry:
    index: int                  # 1부터. 셀이 참조하는 번호다.
    fill_type: int = 0
    image_bin_id: Optional[int] = None   # 이미지 채우기가 가리키는 BinData 번호
    image_fill_mode: int = 0
    parse_note: str = ""


@dataclass
class DocInfo:
    bin_data: List[BinDataEntry] = field(default_factory=list)
    border_fills: List[BorderFillEntry] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def border_fill(self, index: int) -> Optional[BorderFillEntry]:
        if 1 <= index <= len(self.border_fills):
            return self.border_fills[index - 1]
        return None

    def image_bin_id_for_border_fill(self, index: int) -> Optional[int]:
        bf = self.border_fill(index)
        return bf.image_bin_id if bf else None

    def bin_entry(self, index: int) -> Optional[BinDataEntry]:
        if 1 <= index <= len(self.bin_data):
            return self.bin_data[index - 1]
        return None


def _parse_bin_data(payload: bytes, index: int) -> BinDataEntry:
    r = Reader(payload)
    prop = r.u16()
    entry = BinDataEntry(index=index, raw_property=prop)
    entry.kind = prop & 0x0F
    entry.compress = (prop >> 4) & 0x03
    if entry.kind == BIN_LINK:
        entry.abs_path = r.wchar_str()
        entry.rel_path = r.wchar_str()
    elif entry.kind == BIN_EMBEDDING:
        entry.bin_id = r.u16()
        entry.ext = r.wchar_str()
    else:  # STORAGE
        entry.bin_id = r.u16()
    return entry


def _skip_solid_fill(r: Reader) -> None:
    """단색 채우기는 12바이트다: 배경색, 무늬색, 무늬 종류.

    투명도(alpha)는 여기가 아니라 채우기 블록 맨 끝에 종류별로 1바이트씩 붙는다.
    여기서 함께 건너뛰면 뒤따르는 이미지 채우기의 위치가 5바이트씩 밀린다.
    """
    r.skip(4 + 4 + 4)


def _skip_gradation_fill(r: Reader) -> None:
    r.u8()          # 그러데이션 유형 (스펙 문서는 INT16 이라 하나 실제는 1바이트)
    r.u32()         # 시작 각
    r.u32()         # 중심 x
    r.u32()         # 중심 y
    r.u32()         # 번짐 정도
    n = r.u32()     # 색 개수
    if n > 2:
        r.skip(4 * n)   # 색이 바뀌는 위치
    r.skip(4 * n)       # 색상


def _read_image_fill(r: Reader) -> tuple:
    mode = r.u8()   # 채우기 유형(바둑판/가운데/크기에 맞추어 ...)
    r.i8()          # 밝기
    r.i8()          # 명암
    r.u8()          # 효과
    bin_id = r.u16()
    return mode, bin_id


def _parse_border_fill(payload: bytes, index: int, max_bin: int) -> BorderFillEntry:
    """채우기 정보에서 이미지가 가리키는 BinData 번호를 뽑는다.

    단색/그러데이션이 함께 켜져 있으면 앞부분 길이가 달라지는데, 구현체마다
    읽는 순서가 다르게 알려져 있다. 그래서 후보 순서를 몇 가지 시도해 보고
    'BinData 번호가 실제 존재하는 범위 안에 들어오는' 해석을 채택한다.
    """
    entry = BorderFillEntry(index=index)
    if len(payload) < _BORDER_HEADER_LEN + 4:
        entry.parse_note = "너무 짧음"
        return entry

    r = Reader(payload, _BORDER_HEADER_LEN)
    fill_type = r.u32()
    entry.fill_type = fill_type
    if not (fill_type & FILL_IMAGE):
        return entry

    body_start = r.pos
    # 실제 문서로 확인한 순서: 단색 -> 그러데이션 -> 이미지.
    # (fillType 비트로는 1 -> 4 -> 2 순서다. 비트 번호 순서가 아니다.)
    orders = (
        (FILL_SOLID, FILL_GRADATION, FILL_IMAGE),
        (FILL_SOLID, FILL_IMAGE, FILL_GRADATION),   # 혹시 다르게 쓴 문서를 위한 대비
    )
    for order in orders:
        rr = Reader(payload, body_start)
        ok = True
        mode = bin_id = 0
        for part in order:
            if not (fill_type & part):
                continue
            if part == FILL_SOLID:
                _skip_solid_fill(rr)
            elif part == FILL_GRADATION:
                _skip_gradation_fill(rr)
            else:
                if rr.remaining < 6:
                    ok = False
                    break
                mode, bin_id = _read_image_fill(rr)
        if ok and 1 <= bin_id <= max(max_bin, 1):
            entry.image_bin_id = bin_id
            entry.image_fill_mode = mode
            return entry

    # 어떤 순서로도 말이 안 되면, 꼬리에서 그럴듯한 번호를 찾아본다.
    tail = payload[body_start:]
    for off in range(0, max(0, len(tail) - 5)):
        cand = int.from_bytes(tail[off + 4:off + 6], "little")
        if 1 <= cand <= max_bin and tail[off] <= 7:
            entry.image_bin_id = cand
            entry.image_fill_mode = tail[off]
            entry.parse_note = f"꼬리 탐색으로 추정 (offset={off})"
            return entry
    entry.parse_note = "이미지 채우기지만 BinData 번호를 찾지 못함"
    return entry


def parse_doc_info(buf: bytes) -> DocInfo:
    info = DocInfo()
    pending_border_fills = []
    for rec in iter_records(buf):
        if rec.tag == T.BIN_DATA:
            info.bin_data.append(_parse_bin_data(rec.payload, len(info.bin_data) + 1))
        elif rec.tag == T.BORDER_FILL:
            pending_border_fills.append(rec.payload)

    max_bin = len(info.bin_data)
    for i, payload in enumerate(pending_border_fills, start=1):
        entry = _parse_border_fill(payload, i, max_bin)
        if entry.parse_note:
            info.warnings.append(f"테두리/배경 {i}번: {entry.parse_note}")
        info.border_fills.append(entry)
    return info

"""BodyText 섹션에서 표·셀·그림을 뽑아낸다.

레코드는 평평한 목록이지만 level 값으로 트리를 이룬다. 표 하나는
    CTRL_HEADER('tbl ')            level L
      TABLE                        level L+1
      LIST_HEADER (셀 1)           level L+1
      PARA_HEADER (셀 1의 문단)    level L+1
        PARA_TEXT                  level L+2
      LIST_HEADER (셀 2)           level L+1
      ...
처럼 이어진다. 셀 안에 또 표가 있으면 그 표의 CTRL_HEADER 는 level L+2 로 나온다.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import List, Optional

from ..model import Cell, PictureHint, Table
from . import tags as T
from .records import Reader, iter_records

# 본문 글자 코드 중 제어 문자 분류
CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}
INLINE_CONTROLS = {4, 5, 6, 7, 8, 9, 19, 20}
EXTENDED_CONTROLS = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
# 인라인/확장 컨트롤은 코드 + 자료 12바이트 + 코드, 즉 8글자(16바이트)를 차지한다
WIDE_CONTROL_BYTES = 16

# 표 셀의 LIST_HEADER 안에서 각 값이 있는 위치
_CELL_COL = 8
_CELL_ROW = 10
_CELL_COL_SPAN = 12
_CELL_ROW_SPAN = 14
_CELL_BORDER_FILL = 32
_CELL_MIN_LEN = 34

# 그림 개체 속성에서 BinData 번호가 있는 위치.
#   테두리색 4 + 두께 4 + 속성 4 + 그림 사각형 4점 32 + 자르기 16
#   + 안쪽 여백 8 + 밝기 1 + 명암 1 + 효과 1 = 71
# 실제 문서 다발(태그 85 레코드 2,042개)로 확인했다. 71 은 2,041개에서 유효 범위 안에
# 들었고 다른 위치는 모두 5% 미만이었다. 그래서 다른 위치는 시도하지 않는다.
# 억지로 다른 위치를 넣으면 엉뚱한 사진을 조용히 집어 오게 된다.
_PICTURE_BIN_ID_OFFSET = 71

# 한글이 개체 설명문에 적어 두는 메모. 예:
#   그림입니다.
#   원본 그림의 이름: DSC_1234.JPG
#   원본 그림의 크기: 가로 6000pixel, 세로 4000pixel
#   사진 찍은 날짜: 2025년 09월 04일 오전 11:16
_HINT_MARKER = "원본 그림의 이름"
_RE_NAME = re.compile(r"원본 그림의 이름\s*[:：]\s*(.+)")
_RE_SIZE = re.compile(r"가로\s*(\d+)\s*pixel\s*,\s*세로\s*(\d+)\s*pixel")
_RE_TAKEN = re.compile(r"사진 찍은 날짜\s*[:：]\s*(.+)")


def parse_picture_hint(text: str) -> Optional[PictureHint]:
    """개체 설명문에서 원본 파일 이름과 화소 크기를 건져낸다."""
    if not text or _HINT_MARKER not in text:
        return None
    hint = PictureHint(raw=text[:400])
    m = _RE_NAME.search(text)
    if m:
        hint.original_name = m.group(1).strip().strip('"\'')
    m = _RE_SIZE.search(text)
    if m:
        hint.width, hint.height = int(m.group(1)), int(m.group(2))
    m = _RE_TAKEN.search(text)
    if m:
        hint.taken_note = m.group(1).strip()
    return hint if (hint.original_name or hint.width) else None


def find_shape_description(payload: bytes) -> str:
    """gso 컨트롤 헤더 안에 박힌 설명문을 찾아낸다.

    버전마다 앞부분 길이가 달라 오프셋으로 잡으면 자주 빗나간다. 대신
    표시 문구를 직접 찾아 그 주변을 읽는다.
    """
    needle = _HINT_MARKER.encode("utf-16le")
    at = payload.find(needle)
    if at < 0:
        return ""
    # 문구 앞쪽에도 '그림입니다.' 같은 머리말이 붙으므로 조금 앞에서부터 읽는다.
    # 앞뒤로는 NUL 이 잔뜩 붙어 있으니 문구를 기준으로 양쪽을 잘라 낸다.
    start = max(0, at - 40)
    if (start % 2) != (at % 2):
        start += 1
    text = payload[start:at + 600].decode("utf-16le", "replace")
    idx = text.find(_HINT_MARKER)
    if idx < 0:
        return ""
    head = text[:idx].split("\x00")[-1]
    tail = text[idx:].split("\x00", 1)[0]
    return head + tail


def decode_para_text(payload: bytes) -> str:
    """PARA_TEXT 레코드를 사람이 읽는 문자열로. 제어 문자는 건너뛴다."""
    out = []
    i = 0
    n = len(payload) - 1
    while i < n:
        (code,) = struct.unpack_from("<H", payload, i)
        if code in EXTENDED_CONTROLS or code in INLINE_CONTROLS:
            if code == 9:      # 탭
                out.append("\t")
            i += WIDE_CONTROL_BYTES
        elif code in CHAR_CONTROLS:
            if code in (10, 13):
                out.append("\n")
            i += 2
        else:
            out.append(chr(code))
            i += 2
    return "".join(out)


def _parse_cell(payload: bytes) -> Optional[Cell]:
    if len(payload) < _CELL_MIN_LEN:
        return None
    u16 = lambda off: struct.unpack_from("<H", payload, off)[0]  # noqa: E731
    return Cell(
        row=u16(_CELL_ROW),
        col=u16(_CELL_COL),
        col_span=max(1, u16(_CELL_COL_SPAN)),
        row_span=max(1, u16(_CELL_ROW_SPAN)),
        border_fill_id=u16(_CELL_BORDER_FILL),
    )


def _parse_table_head(payload: bytes) -> tuple:
    r = Reader(payload)
    r.u32()                 # 속성
    row_count = r.u16()
    col_count = r.u16()
    return row_count, col_count


def parse_picture_bin_id(payload: bytes, max_bin: int) -> Optional[int]:
    """SHAPE_COMPONENT_PICTURE 레코드에서 BinData 번호를 뽑는다.

    이 번호는 BinData 스트림 이름의 번호가 아니라 DocInfo 안 BIN_DATA 레코드의
    순번(1부터)이다. 스트림 이름은 그 레코드가 들고 있는 값으로 따로 만든다.
    """
    off = _PICTURE_BIN_ID_OFFSET
    if off + 2 > len(payload):
        return None
    (value,) = struct.unpack_from("<H", payload, off)
    return value if 1 <= value <= max(max_bin, 1) else None


@dataclass
class _OpenTable:
    ctrl_level: int
    table: Table
    cell: Optional[Cell] = None


@dataclass
class SectionResult:
    tables: List[Table] = field(default_factory=list)
    # 표 밖에 떠 있는 그림들 (문단 순서, BinData 번호)
    floating: List[tuple] = field(default_factory=list)
    # 표 밖 문단의 글 (문단 순서, 글). 떠 있는 그림의 이름을 찾을 때 쓴다.
    body_text: List[tuple] = field(default_factory=list)
    hints: dict = field(default_factory=dict)       # bin_id -> PictureHint
    warnings: List[str] = field(default_factory=list)


def parse_section(buf: bytes, *, section_index: int, max_bin: int) -> SectionResult:
    result = SectionResult()
    stack: List[_OpenTable] = []
    table_order = 0
    # 그림 개체(gso) 안으로 들어간 상태인지 — 들어간 시점의 level 을 기억한다
    gso_level: Optional[int] = None
    gso_hint: Optional[PictureHint] = None
    para_index = 0

    def current_cell() -> Optional[Cell]:
        return stack[-1].cell if stack else None

    for rec in iter_records(buf):
        # 열려 있던 표보다 얕은 곳으로 나왔으면 그 표는 끝난 것이다
        while stack and rec.level <= stack[-1].ctrl_level:
            stack.pop()
        if gso_level is not None and rec.level <= gso_level:
            gso_level = None
            gso_hint = None

        if rec.tag == T.CTRL_HEADER:
            ctrl_id = Reader(rec.payload).signature()
            if ctrl_id == "tbl ":
                table_order += 1
                table = Table(section=section_index, order=table_order, depth=len(stack))
                stack.append(_OpenTable(ctrl_level=rec.level, table=table))
                result.tables.append(table)
            elif ctrl_id == "gso ":
                gso_level = rec.level
                gso_hint = parse_picture_hint(find_shape_description(rec.payload))

        elif rec.tag == T.TABLE and stack:
            rows, cols = _parse_table_head(rec.payload)
            stack[-1].table.row_count = rows
            stack[-1].table.col_count = cols

        elif rec.tag == T.LIST_HEADER:
            # 표 셀의 LIST_HEADER 는 그 표의 CTRL_HEADER 바로 아래 층에 온다.
            # 글상자/각주 등 다른 LIST_HEADER 와 이것으로 구분한다.
            if stack and rec.level == stack[-1].ctrl_level + 1 and gso_level is None:
                cell = _parse_cell(rec.payload)
                if cell is not None:
                    stack[-1].table.cells.append(cell)
                    stack[-1].cell = cell

        elif rec.tag == T.PARA_HEADER:
            para_index += 1

        elif rec.tag == T.PARA_TEXT:
            cell = current_cell()
            if gso_level is not None:
                pass                        # 그리기 개체 안의 글은 셀 글이 아니다
            elif cell is not None:
                # 문단 끝 표시가 줄바꿈으로 남으므로 문단 단위로 다듬어 붙인다.
                # 여러 문단이 있으면 줄바꿈으로 이어 두고, 나중에 캡션 판단에서
                # '줄이 여럿이면 캡션이 아니다' 로 쓴다.
                text = decode_para_text(rec.payload).strip()
                if text:
                    cell.text = (cell.text + "\n" + text) if cell.text else text
            else:
                text = decode_para_text(rec.payload).strip()
                if text:
                    result.body_text.append((para_index, text))

        elif rec.tag == T.SHAPE_COMPONENT_PICTURE:
            bin_id = parse_picture_bin_id(rec.payload, max_bin)
            if bin_id is None:
                result.warnings.append("그림 개체에서 BinData 번호를 읽지 못했습니다")
                continue
            if gso_hint is not None:
                result.hints.setdefault(bin_id, gso_hint)
            cell = current_cell()
            if cell is not None:
                cell.inline_bin_ids.append(bin_id)
            else:
                result.floating.append((para_index, bin_id))

    return result

"""표에서 사진에 붙일 이름(캡션)을 찾아낸다.

핵심 착안: 캡션 셀은 사진 셀과 **열 병합 범위가 정확히 같다**. 표 안의 다른
내용 셀들은 병합 폭이 다르기 때문에, 이 조건만으로 대부분의 오답이 걸러진다.

    ┌───────── 사진 (r11, c1, colspan 5) ─────────┐
    ├───────── 항공사진 (r12, c1, colspan 5) ─────┤   ← 폭이 같다. 캡션이다.

    ┌───────── 사진 (r3, c1, colspan 5) ──────────┐
    ├── 현상보존 (r4,c1,span3) ──┼── ... ─────────┤   ← 폭이 다르다. 캡션이 아니다.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from .model import Cell, HwpDocument, PhotoSlot, Table

# 캡션이라기엔 너무 긴 글자 수
MAX_CAPTION_LEN = 40
# 이 정도보다 길면 '값'이지 이름이 아니다
MAX_FIELD_VALUE_LEN = 200

_WS = re.compile(r"[\s 　]+")
_LEADING_NUM = re.compile(r"^\s*[<\[(【〈]?\s*(?:사진|그림|도면)?\s*[0-9①-⑳]+\s*[.)\]>》】]?\s*")


def normalize_text(text: str) -> str:
    """공백·전각문자·한글 자모 표현을 정돈한다."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _WS.sub(" ", text)
    return text.strip()


def clean_caption(text: str, *, strip_numbering: bool = False) -> str:
    text = normalize_text(text)
    if strip_numbering:
        text = _LEADING_NUM.sub("", text).strip()
    return text


def _is_photo_cell(cell: Cell, image_of) -> bool:
    return image_of(cell) is not None


def _plausible_caption(cell: Optional[Cell], image_of) -> bool:
    if cell is None:
        return False
    if _is_photo_cell(cell, image_of):
        return False
    text = normalize_text(cell.text)
    if not text or "\n" in cell.text.strip():
        return False
    return len(text) <= MAX_CAPTION_LEN


def _row_header(table: Table, cell: Cell) -> str:
    """같은 행을 세로로 덮고 있는 맨 왼쪽 열 셀의 글."""
    best = ""
    for c in table.cells:
        if c.col != 0:
            continue
        if c.row <= cell.row < c.row_end:
            text = normalize_text(c.text)
            if text and len(text) <= MAX_CAPTION_LEN:
                best = text
    return best


def _aligned(table: Table, row: int, col: int, col_span: int) -> Optional[Cell]:
    return table.cell_starting_at(row, col, col_span)


def resolve_table_captions(table: Table, image_of) -> List[Tuple[Cell, int, str, str, str]]:
    """표 하나에서 (셀, BinData번호, 캡션, 구역, 출처) 목록을 만든다.

    image_of(cell) 은 그 셀의 배경 이미지 BinData 번호(없으면 None)를 준다.
    """
    photo_cells = []
    for cell in table.cells:
        bin_id = image_of(cell)
        if bin_id is not None:
            photo_cells.append((cell, bin_id, "background"))
        for inline_id in cell.inline_bin_ids:
            photo_cells.append((cell, inline_id, "inline"))
    if not photo_cells:
        return []

    # 같은 행의 사진들은 캡션 위치(아래/위)가 같을 것이다. 행 단위로 투표한다.
    rows: Dict[int, List[Cell]] = {}
    for cell, _bin, _kind in photo_cells:
        rows.setdefault(cell.row, []).append(cell)

    direction_by_row: Dict[int, str] = {}
    for row, cells in rows.items():
        below = sum(
            1 for c in cells
            if _plausible_caption(_aligned(table, c.row_end, c.col, c.col_span), image_of)
        )
        above = sum(
            1 for c in cells
            if _plausible_caption(_aligned(table, c.row - 1, c.col, c.col_span), image_of)
        )
        if below >= above and below > 0:
            direction_by_row[row] = "below"
        elif above > 0:
            direction_by_row[row] = "above"
        else:
            direction_by_row[row] = "none"

    out = []
    for cell, bin_id, kind in photo_cells:
        caption, source = "", ""
        own = normalize_text(cell.text)
        if kind == "inline" and own and len(own) <= MAX_CAPTION_LEN:
            caption, source = own, "같은 칸"
        if not caption:
            order = ["below", "above"] if direction_by_row.get(cell.row) != "above" else ["above", "below"]
            for direction in order:
                if direction == "below":
                    cand = _aligned(table, cell.row_end, cell.col, cell.col_span)
                    label = "아래 칸"
                else:
                    cand = _aligned(table, cell.row - 1, cell.col, cell.col_span)
                    label = "위 칸"
                if _plausible_caption(cand, image_of):
                    caption, source = normalize_text(cand.text), label
                    break
        if not caption and own and len(own) <= MAX_CAPTION_LEN:
            caption, source = own, "사진 칸 자체"
        out.append((cell, bin_id, caption, _row_header(table, cell), source))
    return out


def extract_fields(tables: List[Table]) -> Dict[str, str]:
    """'도면 명칭' 옆칸에 '대전_026' 이 있는 식의 머리글/값 쌍을 모은다."""
    fields: Dict[str, str] = {}
    for table in tables:
        for cell in table.cells:
            key = normalize_text(cell.text)
            if not key or len(key) > MAX_CAPTION_LEN or "\n" in cell.text.strip():
                continue
            # 오른쪽으로 가면서 첫 번째로 '내용이 있는' 칸을 값으로 삼는다.
            # 사이에 '-' 나 빈 칸이 끼어 있는 서식이 흔하다.
            value = ""
            col = cell.col_end
            for _ in range(4):
                neighbour = next(
                    (o for o in table.cells if o.row == cell.row and o.col == col), None
                )
                if neighbour is None:
                    break
                text = normalize_text(neighbour.text)
                col = neighbour.col_end
                if text and text != "-" and len(text) <= MAX_FIELD_VALUE_LEN:
                    value = text
                    break
            if not value:
                continue
            fields.setdefault(key, value)
            fields.setdefault(key.replace(" ", ""), value)
    return fields


def build_slots(doc: HwpDocument, image_of) -> List[PhotoSlot]:
    """문서 전체를 훑어 PhotoSlot 목록을 만든다."""
    slots: List[PhotoSlot] = []
    for table in doc.tables:
        for cell, bin_id, caption, group, source in resolve_table_captions(table, image_of):
            slots.append(PhotoSlot(
                bin_id=bin_id,
                caption=caption,
                group=group,
                caption_source=source,
                placement="background" if image_of(cell) == bin_id else "inline",
                section=table.section,
                table_order=table.order,
                row=cell.row,
                col=cell.col,
            ))
    for order, slot in enumerate(slots, start=1):
        slot.doc_order = order
    return slots

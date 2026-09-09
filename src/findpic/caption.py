"""표에서 사진에 붙일 이름(캡션)을 찾아낸다.

세 가지 착안으로 푼다.

**1. 폭이 같은 칸이 이름이다.**
캡션 칸은 사진 칸과 열 병합 범위가 정확히 같다. 표 안의 다른 내용 칸은 폭이 달라
이 조건 하나로 대부분의 오답이 걸러진다.

    ┌───────── 사진 (r11, c1, 5칸) ─────────┐
    ├───────── 항공사진 (r12, c1, 5칸) ─────┤   폭이 같다. 이름이다.

    ┌───────── 사진 (r3, c1, 5칸) ──────────┐
    ├── 현상보존 (3칸) ──┼── 시굴조사 ──────┤   폭이 다르다. 이름이 아니다.

**2. 방향은 '밴드' 단위로 정한다.**
한 표 안에서도 어떤 묶음은 이름이 아래에, 어떤 묶음은 위에 온다. 실제 조사카드가
그렇다. 그래서 표 전체가 아니라 행 머리글이 덮는 행 묶음(밴드)마다 따로 정한다.
방향이 명백한 사진(한쪽에만 폭이 맞는 칸이 있는 사진)만 투표한다.

**3. 이름 하나에 사진 하나.**
점수가 높은 짝부터 확정하고, 쓴 칸은 후보에서 뺀다. 그래야 위아래 양쪽에 폭이
맞는 칸이 있는 사진이 옆 사진의 이름을 가로채지 않는다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .model import Cell, HwpDocument, PhotoSlot, Table

# 이보다 길면 이름이 아니라 본문이다
MAX_CAPTION_LEN = 40
# 머리글 옆에서 읽어 오는 값의 길이 한도
MAX_FIELD_VALUE_LEN = 200

# --- 점수표 -----------------------------------------------------------------
SCORE_OWN_TEXT = 170        # 사진 칸에 이름을 같이 써 둔 경우
SCORE_EXACT = 100           # 열 병합 범위가 정확히 같은 칸
SCORE_OVERLAP = 45          # 걸치기만 하는 칸 (겹침 비율을 곱한다)
SCORE_ROW_HEADER = 45       # 행 머리글 (여러 사진이 나눠 쓸 수 있다)
BONUS_BELOW = 12            # 아래쪽을 조금 더 친다 (한국 보고서의 기본 서식)
BONUS_ABOVE = 8
BONUS_SAME_BAND = 25
PENALTY_OTHER_BAND = -35
BONUS_NO_PHOTO_ROW = 10     # 후보가 있는 행에 사진이 없으면 캡션 행일 가능성이 크다
BONUS_SINGLE_LINE = 8
BONUS_SHORT = 6
PENALTY_COLON = -14         # '소재지:' 처럼 항목 이름인 경우
PENALTY_SINGLE_CHAR = -20
VOTE_BONUS = 15             # 밴드 투표 결과. 아래쪽 편향(12)을 이겨야 한다
MIN_ACCEPT = 55

# 투표가 방향 편향을 뒤집을 수 있어야 규칙이 성립한다
assert VOTE_BONUS > max(BONUS_BELOW, BONUS_ABOVE)

_WS = re.compile(r"[\s　]+")
# 번호 뒤에 구분자나 공백이 반드시 있어야 벗긴다.
# 이게 없으면 '55세 이상 근로자' 가 '세 이상 근로자' 로 잘린다.
_LEADING_NUM = re.compile(
    r"^\s*[<\[(【〈]?\s*(?:사진|그림|도면)\s*[0-9①-⑳]+\s*[.)\]>》】]?[\s:·]+"   # 사진 3. / 그림 2
    r"|^\s*[<\[(【〈]\s*[0-9①-⑳]+\s*[)\]>》】]\s*"                          # (3) / [1]
    r"|^\s*[0-9]+\s*[.)\]>》】]\s*"                                        # 3.  ← 구분자 필수
    r"|^\s*[①-⑳]+\s*[.)\]>》】]?\s*"                                      # ①  ← 원문자는 그 자체가 표지
)
# 이름이 될 수 없는 글: 자리표시자, 숫자·기호뿐인 글
_PLACEHOLDER = re.compile(r"^[\s\-–—·ㆍ.,:;/\\()\[\]{}<>~=_]*$")
_NUMERIC_ONLY = re.compile(r"^[\s0-9①-⑳.,\-–—/()\[\]=~·]*$")


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


def is_caption_like(text: str) -> bool:
    """이름이 될 만한 글인가."""
    text = normalize_text(text)
    if not text or len(text) > MAX_CAPTION_LEN:
        return False
    if _PLACEHOLDER.match(text) or _NUMERIC_ONLY.match(text):
        return False
    return True


# --- 밴드 ------------------------------------------------------------------

@dataclass
class Band:
    """행 머리글 하나가 덮는 행 묶음."""

    start: int
    end: int                 # 포함하지 않는 끝
    header: str = ""

    def covers(self, row: int) -> bool:
        return self.start <= row < self.end


def find_bands(table: Table, is_photo) -> List[Band]:
    """맨 왼쪽 열의 머리글 칸으로 표를 가로 묶음으로 나눈다.

    머리글이 없는 표는 표 전체가 하나의 밴드다.
    """
    bands = [
        Band(start=c.row, end=c.row_end, header=normalize_text(c.text))
        for c in table.cells
        if c.col == 0 and not is_photo(c) and is_caption_like(c.text)
    ]
    if not bands:
        rows = [c.row for c in table.cells] or [0]
        ends = [c.row_end for c in table.cells] or [1]
        return [Band(start=min(rows), end=max(ends))]
    return sorted(bands, key=lambda b: b.start)


def band_for(bands: List[Band], row: int) -> Optional[Band]:
    for band in bands:
        if band.covers(row):
            return band
    return None


# --- 후보 ------------------------------------------------------------------

@dataclass
class Candidate:
    cell: Cell
    text: str
    source: str              # '아래 칸' 등, 사람에게 보여 줄 말
    direction: str           # below | above | own | header
    score: float
    shareable: bool = False  # 여러 사진이 나눠 쓸 수 있는가 (행 머리글 등)
    exact: bool = False      # 열 병합 범위가 정확히 같은가


def _overlap(a: Cell, b: Cell) -> float:
    """두 칸의 열 범위가 얼마나 겹치는가 (0-1)."""
    lo = max(a.col, b.col)
    hi = min(a.col_end, b.col_end)
    if hi <= lo:
        return 0.0
    return (hi - lo) / max(1, a.col_end - a.col)


def _row_has_photo(table: Table, row: int, is_photo) -> bool:
    return any(c.row == row and is_photo(c) for c in table.cells)


def _shape_bonus(text: str) -> float:
    bonus = 0.0
    if "\n" not in text:
        bonus += BONUS_SINGLE_LINE
    if len(text) <= 20:
        bonus += BONUS_SHORT
    if text.rstrip().endswith((":", "：")):
        bonus += PENALTY_COLON
    if len(text) == 1:
        bonus += PENALTY_SINGLE_CHAR
    return bonus


def collect_candidates(table: Table, photo: Cell, bands: List[Band], is_photo) -> List[Candidate]:
    """사진 칸 하나에 대한 이름 후보들."""
    out: List[Candidate] = []
    photo_band = band_for(bands, photo.row)

    own = normalize_text(photo.text)
    if is_caption_like(own):
        out.append(Candidate(cell=photo, text=own, source="사진 칸 자체", direction="own",
                             score=SCORE_OWN_TEXT + _shape_bonus(own), exact=True))

    for row, direction, source, bonus in (
        (photo.row_end, "below", "아래 칸", BONUS_BELOW),
        (photo.row - 1, "above", "위 칸", BONUS_ABOVE),
    ):
        if row < 0:
            continue
        for cell in table.cells:
            if cell.row != row or is_photo(cell):
                continue
            text = normalize_text(cell.text)
            if not is_caption_like(text):
                continue
            ratio = _overlap(photo, cell)
            if ratio <= 0:
                continue
            exact = (cell.col == photo.col and cell.col_span == photo.col_span)
            base = SCORE_EXACT if exact else SCORE_OVERLAP * ratio
            score = base + bonus + _shape_bonus(text)
            cand_band = band_for(bands, cell.row)
            score += BONUS_SAME_BAND if cand_band is photo_band else PENALTY_OTHER_BAND
            if not _row_has_photo(table, row, is_photo):
                score += BONUS_NO_PHOTO_ROW
            out.append(Candidate(cell=cell, text=text, source=source, direction=direction,
                                 score=score, exact=exact))

    if photo_band is not None and photo_band.header:
        header_cell = next(
            (c for c in table.cells
             if c.col == 0 and c.row == photo_band.start and not is_photo(c)),
            None,
        )
        if header_cell is not None:
            out.append(Candidate(cell=header_cell, text=photo_band.header, source="행 머리글",
                                 direction="header",
                                 score=SCORE_ROW_HEADER + _shape_bonus(photo_band.header),
                                 shareable=True))
    return out


def vote_directions(photo_cells: List[Cell], candidates: Dict[int, List[Candidate]],
                    bands: List[Band]) -> Dict[int, str]:
    """밴드마다 이름이 위에 있는지 아래에 있는지 정한다.

    한쪽에만 '폭이 정확히 같은 칸' 이 있는 사진, 즉 방향이 뻔한 사진만 투표한다.
    양쪽 다 있는 사진은 기권한다. 그래야 애매한 표가 결과를 흐리지 않는다.
    """
    tally: Dict[int, Dict[str, int]] = {}
    for i, cell in enumerate(photo_cells):
        band = band_for(bands, cell.row)
        key = id(band) if band else 0
        dirs = {c.direction for c in candidates.get(i, []) if c.exact and c.direction in ("below", "above")}
        if len(dirs) != 1:
            continue                    # 양쪽 다 있거나 아예 없으면 기권
        counts = tally.setdefault(key, {"below": 0, "above": 0})
        counts[next(iter(dirs))] += 1

    out: Dict[int, str] = {}
    for key, counts in tally.items():
        if counts["below"] != counts["above"]:
            out[key] = "below" if counts["below"] > counts["above"] else "above"
    return out


def resolve_table_captions(table: Table, image_of) -> List[Tuple[Cell, int, str, str, str]]:
    """표 하나에서 (셀, BinData번호, 이름, 구역, 출처) 목록을 만든다."""
    def is_photo(cell: Cell) -> bool:
        return image_of(cell) is not None or bool(cell.inline_bin_ids)

    photos: List[Tuple[Cell, int]] = []
    for cell in table.cells:
        bin_id = image_of(cell)
        if bin_id is not None:
            photos.append((cell, bin_id))
        for inline_id in cell.inline_bin_ids:
            photos.append((cell, inline_id))
    if not photos:
        return []

    bands = find_bands(table, is_photo)
    photo_cells = [c for c, _ in photos]
    candidates = {i: collect_candidates(table, c, bands, is_photo)
                  for i, c in enumerate(photo_cells)}

    votes = vote_directions(photo_cells, candidates, bands)
    for i, cell in enumerate(photo_cells):
        band = band_for(bands, cell.row)
        winner = votes.get(id(band) if band else 0)
        if not winner:
            continue
        for cand in candidates[i]:
            if cand.direction == winner:
                cand.score += VOTE_BONUS

    # 점수 높은 짝부터 확정한다. 쓴 칸은 다른 사진이 쓰지 못한다.
    pairs = [(cand.score, i, cand)
             for i, cands in candidates.items() for cand in cands
             if cand.score >= MIN_ACCEPT]
    pairs.sort(key=lambda p: (-p[0], p[1]))

    chosen: Dict[int, Candidate] = {}
    taken = set()
    for _score, i, cand in pairs:
        if i in chosen:
            continue
        key = (cand.cell.row, cand.cell.col)
        if key in taken and not cand.shareable:
            continue
        chosen[i] = cand
        if not cand.shareable:
            taken.add(key)

    out = []
    for i, (cell, bin_id) in enumerate(photos):
        cand = chosen.get(i)
        band = band_for(bands, cell.row)
        group = band.header if band else ""
        caption = cand.text if cand else ""
        source = cand.source if cand else ""
        if cand is not None and cand.direction == "header":
            group = ""              # 머리글을 이름으로 썼으면 구역으로 또 쓰지 않는다
        out.append((cell, bin_id, caption, group, source))
    return out


def extract_fields(tables: List[Table], *, skip_cells=()) -> Dict[str, str]:
    """'도면 명칭' 옆칸에 '대전_026' 이 있는 식의 머리글/값 쌍을 모은다.

    오른쪽으로 가면서 값을 찾되 **빈 칸을 만나면 멈춘다**. 빈 칸을 건너뛰면
    옆 항목의 머리글(예: '유적명')을 값으로 집어 오게 된다.
    '-' 같은 자리표시자만 건너뛴다.
    """
    fields: Dict[str, str] = {}
    skip = set(skip_cells)
    for table in tables:
        by_start = {}
        for cell in table.cells:
            by_start.setdefault((cell.row, cell.col), cell)
        for cell in table.cells:
            key = normalize_text(cell.text)
            if not key or len(key) > MAX_CAPTION_LEN or "\n" in cell.text.strip():
                continue
            if (table.order, cell.row, cell.col) in skip:
                continue
            value = ""
            col = cell.col_end
            for _ in range(4):
                neighbour = by_start.get((cell.row, col))
                if neighbour is None:
                    break
                text = normalize_text(neighbour.text)
                col = neighbour.col_end
                if not text:
                    break                       # 빈 칸에서 멈춘다
                if _PLACEHOLDER.match(text):
                    continue                    # '-' 는 건너뛴다
                if len(text) <= MAX_FIELD_VALUE_LEN:
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

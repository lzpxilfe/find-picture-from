"""도구 전체가 주고받는 자료 구조."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class BinItem:
    """문서에 박혀 있는 이진 데이터(주로 사진) 한 건."""

    bin_id: int                 # 문서 안에서의 1-based 번호
    ext: str                    # 'jpg', 'png', ...
    data: bytes                 # 압축을 푼 실제 바이트
    stream: str                 # 원래 있던 스트림/파일 이름 (예: 'BinData/BIN0001.jpg')
    kind: str = "embed"         # embed | link | storage
    link_path: str = ""         # kind == 'link' 일 때 문서가 가리키던 경로

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Cell:
    """표의 셀 하나."""

    row: int
    col: int
    row_span: int
    col_span: int
    border_fill_id: int
    text: str = ""
    # 이 셀에 그림이 어떻게 들어 있는가
    bg_bin_id: Optional[int] = None          # 셀 배경 이미지(테두리/배경의 이미지 채우기)
    inline_bin_ids: list = field(default_factory=list)  # 셀 안에 얹은 그림 개체들

    @property
    def row_end(self) -> int:
        return self.row + max(1, self.row_span)

    @property
    def col_end(self) -> int:
        return self.col + max(1, self.col_span)

    def covers(self, row: int, col: int) -> bool:
        return self.row <= row < self.row_end and self.col <= col < self.col_end


@dataclass
class Table:
    """표 하나. 셀은 문서에 나온 순서 그대로 담는다."""

    cells: list = field(default_factory=list)
    row_count: int = 0
    col_count: int = 0
    section: int = 0
    order: int = 0              # 섹션 안에서 몇 번째 표인지
    depth: int = 0              # 중첩 표의 깊이 (0 = 최상위)

    def cell_at(self, row: int, col: int) -> Optional[Cell]:
        for c in self.cells:
            if c.covers(row, col):
                return c
        return None

    def cell_starting_at(self, row: int, col: int, col_span: Optional[int] = None) -> Optional[Cell]:
        """(row, col) 에서 시작하는 셀. col_span 을 주면 병합 폭까지 일치해야 한다."""
        for c in self.cells:
            if c.row == row and c.col == col:
                if col_span is None or c.col_span == col_span:
                    return c
        return None


@dataclass
class PictureHint:
    """한글이 그림과 함께 남겨 둔 메모.

    한글은 그림을 넣을 때 개체 설명문에 '원본 그림의 이름: DSC_1234.JPG' 처럼
    원래 파일 이름과 화소 크기를 적어 둔다. 셀 배경으로 넣은 그림에는 없지만,
    셀 안에 얹은 그림에는 대개 남아 있어 원본 찾기에 큰 도움이 된다.
    """

    original_name: str = ""
    width: int = 0
    height: int = 0
    taken_note: str = ""
    raw: str = ""


@dataclass
class PhotoSlot:
    """'문서 안의 사진 한 장 + 그 사진에 붙일 이름' 한 쌍."""

    bin_id: int
    caption: str = ""            # 표에서 찾아낸 이름 (예: '항공사진')
    group: str = ""              # 행 머리글 등 상위 구분 (예: '사진자료')
    caption_source: str = ""     # 이름을 어디서 가져왔는지 ('아래 셀' 등) — 리포트용
    placement: str = "background"  # background | inline | floating
    section: int = 0
    table_order: int = 0
    row: int = -1
    col: int = -1
    doc_order: int = 0           # 문서 전체에서 몇 번째 사진인지 (읽기 순서)


@dataclass
class HwpDocument:
    """한글 파일 하나에서 뽑아낸 것 전부."""

    path: Path
    bin_items: dict = field(default_factory=dict)   # bin_id -> BinItem
    tables: list = field(default_factory=list)
    slots: list = field(default_factory=list)       # PhotoSlot
    fields: dict = field(default_factory=dict)      # '도면 명칭' -> '대전_026' 같은 머리글/값 쌍
    hints: dict = field(default_factory=dict)       # bin_id -> PictureHint
    format: str = "hwp"                             # hwp | hwpx
    warnings: list = field(default_factory=list)

    @property
    def stem(self) -> str:
        return self.path.stem

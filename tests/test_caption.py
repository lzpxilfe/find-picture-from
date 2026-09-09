"""표에서 사진 이름을 찾아내는 규칙."""

from conftest import build_section

from findpic.caption import (clean_caption, extract_fields, normalize_text,
                             resolve_table_captions)
from findpic.hwp.section import parse_section


def make_table(cells, image_map):
    section = build_section(cells)
    result = parse_section(section, section_index=0, max_bin=9)
    table = result.tables[0]
    return table, (lambda cell: image_map.get(cell.border_fill_id))


def test_바로_아래_칸에서_이름을_가져온다():
    table, image_of = make_table([
        (1, 0, 5, 1, 7, ""),            # 사진
        (1, 1, 5, 1, 1, "항공사진"),      # 아래 칸
    ], {7: 1})
    out = resolve_table_captions(table, image_of)
    assert [(b, cap, src) for _c, b, cap, _g, src in out] == [(1, "항공사진", "아래 칸")]


def test_아래_칸의_병합_폭이_다르면_이름으로_치지_않는다():
    """사진 아래에 '현상보존' 같은 내용 칸이 오는 서식이 실제로 있다."""
    table, image_of = make_table([
        (1, 0, 5, 1, 1, "기존"),          # 위 칸 (폭이 같다)
        (1, 1, 5, 1, 7, ""),             # 사진
        (1, 2, 3, 1, 1, "현상보존"),      # 아래 칸이지만 폭이 다르다
    ], {7: 1})
    out = resolve_table_captions(table, image_of)
    assert out[0][2] == "기존"
    assert out[0][4] == "위 칸"


def test_한_표_안에서_아래와_위가_섞여도_행별로_판단한다():
    table, image_of = make_table([
        (1, 0, 2, 1, 1, "기존"),
        (1, 1, 2, 1, 7, ""),            # 이름이 위에 있는 사진
        (1, 2, 3, 1, 1, "다른 내용"),
        (1, 3, 2, 1, 8, ""),            # 이름이 아래에 있는 사진
        (1, 4, 2, 1, 1, "항공사진"),
    ], {7: 1, 8: 2})
    out = {b: (cap, src) for _c, b, cap, _g, src in resolve_table_captions(table, image_of)}
    assert out[1] == ("기존", "위 칸")
    assert out[2] == ("항공사진", "아래 칸")


def test_행_머리글을_구역으로_붙인다():
    table, image_of = make_table([
        (0, 0, 1, 3, 1, "사진자료"),
        (1, 0, 2, 1, 7, ""),
        (1, 1, 2, 1, 1, "전경"),
    ], {7: 1})
    out = resolve_table_captions(table, image_of)
    assert out[0][3] == "사진자료"


def test_사진_칸끼리는_서로_이름이_되지_않는다():
    table, image_of = make_table([
        (1, 0, 2, 1, 7, ""),
        (1, 1, 2, 1, 8, ""),
    ], {7: 1, 8: 2})
    out = resolve_table_captions(table, image_of)
    assert all(cap == "" for _c, _b, cap, _g, _s in out)


def test_긴_글은_이름이_아니다():
    long_text = "가" * 80
    table, image_of = make_table([
        (1, 0, 2, 1, 7, ""),
        (1, 1, 2, 1, 1, long_text),
    ], {7: 1})
    assert resolve_table_captions(table, image_of)[0][2] == ""


def test_머리글_옆칸에서_문서_정보를_읽는다():
    section = build_section([
        (0, 0, 1, 1, 1, "도면 명칭"),
        (1, 0, 1, 1, 1, "대전_026"),
        (0, 1, 1, 1, 1, "소재지"),
        (1, 1, 1, 1, 1, "-"),
        (2, 1, 1, 1, 1, "대전광역시 동구"),
    ])
    tables = parse_section(section, section_index=0, max_bin=1).tables
    fields = extract_fields(tables)
    assert fields["도면 명칭"] == "대전_026"
    assert fields["도면명칭"] == "대전_026"
    # 사이에 '-' 가 끼어 있어도 그 다음 칸을 값으로 본다
    assert fields["소재지"] == "대전광역시 동구"


def test_글자_정돈():
    assert normalize_text("  항공　사진 \n") == "항공 사진"
    assert clean_caption("① 전경", strip_numbering=True) == "전경"
    assert clean_caption("사진 3. 근경", strip_numbering=True) == "근경"

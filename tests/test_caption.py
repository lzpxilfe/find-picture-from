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


def test_캡션이_위에_오는_표에서도_제대로_찾는다():
    """캡션/사진/캡션/사진 서식. 아래를 먼저 보는 기본 편향을 이겨야 한다."""
    table, image_of = make_table([
        (1, 0, 2, 1, 1, "기존"),
        (1, 1, 2, 1, 7, ""),
        (1, 2, 2, 1, 1, "변경"),
        (1, 3, 2, 1, 8, ""),
    ], {7: 1, 8: 2})
    out = {b: (cap, src) for _c, b, cap, _g, src in resolve_table_captions(table, image_of)}
    assert out[1] == ("기존", "위 칸")
    assert out[2] == ("변경", "위 칸")


def test_이름_하나에_사진_하나():
    """위아래 양쪽에 폭이 맞는 칸이 있어도 옆 사진 이름을 가로채지 않는다."""
    table, image_of = make_table([
        (1, 0, 2, 1, 7, ""),
        (1, 1, 2, 1, 1, "항공사진"),
        (1, 2, 2, 1, 8, ""),          # 위(항공사진)·아래(전경) 둘 다 폭이 맞는다
        (1, 3, 2, 1, 1, "전경"),
    ], {7: 1, 8: 2})
    out = {b: cap for _c, b, cap, _g, _s in resolve_table_captions(table, image_of)}
    assert out == {1: "항공사진", 2: "전경"}


def test_숫자나_기호뿐인_칸은_이름이_아니다():
    for junk in ("6", "= 1 = 2 =", "- - -", "(3)"):
        table, image_of = make_table([
            (1, 0, 2, 1, 7, ""),
            (1, 1, 2, 1, 1, junk),
        ], {7: 1})
        assert resolve_table_captions(table, image_of)[0][2] == "", junk


def test_행_머리글은_여러_사진이_나눠_쓴다():
    table, image_of = make_table([
        (0, 0, 1, 2, 1, "사진자료"),
        (1, 0, 2, 1, 7, ""),
        (1, 1, 2, 1, 8, ""),
    ], {7: 1, 8: 2})
    out = resolve_table_captions(table, image_of)
    assert all(cap == "사진자료" for _c, _b, cap, _g, _s in out)


def test_빈_칸을_건너뛰어_옆_머리글을_집지_않는다():
    """'연번' 옆이 비어 있는데 그 다음 '유적명' 을 값으로 집으면 안 된다."""
    section = build_section([
        (0, 0, 1, 1, 1, "도면 명칭"),
        (1, 0, 1, 1, 1, "대전_026"),
        (2, 0, 1, 1, 1, "연번"),
        (3, 0, 1, 1, 1, ""),
        (4, 0, 1, 1, 1, "유적명"),
        (5, 0, 1, 1, 1, "대전 효평동 유물산포지2"),
    ])
    fields = extract_fields(parse_section(section, section_index=0, max_bin=1).tables)
    assert fields.get("연번") is None
    assert fields["도면 명칭"] == "대전_026"
    assert fields["유적명"] == "대전 효평동 유물산포지2"


def test_번호_벗기기가_멀쩡한_글을_자르지_않는다():
    assert clean_caption("55세 이상 기간제 근로자", strip_numbering=True) == "55세 이상 기간제 근로자"
    assert clean_caption("2구역 전경", strip_numbering=True) == "2구역 전경"
    assert clean_caption("[1] 전경", strip_numbering=True) == "전경"


def test_HWPX_중첩_표의_셀이_바깥_표에_섞이지_않는다(tmp_path):
    """셀 안에 표가 있으면 안쪽 셀이 바깥 표 목록에도 들어가, 같은 (행,열)이
    여러 번 나오고 캡션이 뒤바뀐다. 실제 문서 다발에서 334군데에 있었다."""
    import zipfile

    from findpic.hwp.hwpx import extract_hwpx

    NS = ('xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
          'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
          'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"')

    def tc(row, col, text, inner=""):
        return (f'<hp:tc><hp:subList><hp:p><hp:run><hp:t>{text}</hp:t>{inner}'
                f'</hp:run></hp:p></hp:subList>'
                f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
                f'<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')

    inner_table = ('<hp:tbl rowCnt="1" colCnt="1"><hp:tr>'
                   + tc(0, 0, "안쪽") + '</hp:tr></hp:tbl>')
    section = (f'<hp:sec {NS}><hp:p><hp:run>'
               '<hp:tbl rowCnt="1" colCnt="2"><hp:tr>'
               + tc(0, 0, "바깥1", inner_table) + tc(0, 1, "바깥2")
               + '</hp:tr></hp:tbl></hp:run></hp:p></hp:sec>')
    manifest = (f'<opf:package xmlns:opf="http://www.idpf.org/2007/opf/"><opf:manifest>'
                '<opf:item id="section0" href="Contents/section0.xml"/>'
                '</opf:manifest></opf:package>')

    path = tmp_path / "중첩.hwpx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("META-INF/manifest.xml", "<manifest/>")
        z.writestr("Contents/content.hpf", manifest)
        z.writestr("Contents/header.xml", f"<hh:head {NS}/>")
        z.writestr("Contents/section0.xml", section)

    doc = extract_hwpx(path)
    assert len(doc.tables) == 2                       # 바깥 표, 안쪽 표
    outer = next(t for t in doc.tables if len(t.cells) == 2)
    inner = next(t for t in doc.tables if len(t.cells) == 1)
    assert sorted(c.text for c in outer.cells) == ["바깥1", "바깥2"]
    assert inner.cells[0].text == "안쪽"
    # 같은 (행, 열)이 두 번 나오면 안 된다
    keys = [(c.row, c.col) for c in outer.cells]
    assert len(keys) == len(set(keys))

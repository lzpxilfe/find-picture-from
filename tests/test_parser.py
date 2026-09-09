"""바이너리 파서: 레코드 · DocInfo · 섹션."""

import struct

from conftest import (border_fill_record, build_doc_info, build_section, cell_record,
                      ctrl_header, para_text, record)

from findpic.hwp import tags as T
from findpic.hwp.docinfo import parse_doc_info
from findpic.hwp.records import Reader, read_records
from findpic.hwp.section import (decode_para_text, find_shape_description,
                                 parse_picture_bin_id, parse_picture_hint, parse_section)


def test_레코드_머리를_읽는다():
    buf = record(T.PARA_TEXT, 3, b"abcd") + record(T.TABLE, 1, b"xy")
    recs = read_records(buf)
    assert [(r.tag, r.level, r.payload) for r in recs] == [
        (T.PARA_TEXT, 3, b"abcd"),
        (T.TABLE, 1, b"xy"),
    ]


def test_4095바이트_넘는_레코드는_확장_길이를_쓴다():
    payload = b"z" * 5000
    recs = read_records(record(T.PARA_TEXT, 0, payload))
    assert len(recs) == 1 and recs[0].payload == payload


def test_잘린_스트림에서도_죽지_않는다():
    buf = record(T.PARA_TEXT, 0, b"abcdefgh")[:-3]
    recs = read_records(buf)
    assert len(recs) == 1 and len(recs[0].payload) == 5


def test_리더는_범위를_넘어도_0을_준다():
    r = Reader(b"\x01\x00")
    assert r.u16() == 1
    assert r.u32() == 0 and r.u16() == 0


def test_컨트롤_아이디는_뒤집어_읽는다():
    assert Reader(ctrl_header("tbl ")).signature() == "tbl "


def test_문단_글에서_제어문자를_걸러낸다():
    payload = struct.pack("<H", 2) + b"\x00" * 12 + struct.pack("<H", 2)   # 확장 컨트롤
    payload += "항공사진".encode("utf-16le") + struct.pack("<H", 13)
    assert decode_para_text(payload) == "항공사진\n"


def test_탭은_글자로_남는다():
    payload = struct.pack("<H", 9) + b"\x00" * 12 + struct.pack("<H", 9)
    payload += "가".encode("utf-16le")
    assert decode_para_text(payload) == "\t가"


def test_BIN_DATA_와_이미지_채우기를_읽는다():
    buf = build_doc_info([(1, "jpg"), (7, "JPG")], [0, 0, 1, 2])
    info = parse_doc_info(buf)
    assert [(b.index, b.bin_id, b.ext) for b in info.bin_data] == [(1, 1, "jpg"), (2, 7, "JPG")]
    assert info.image_bin_id_for_border_fill(3) == 1
    assert info.image_bin_id_for_border_fill(4) == 2
    assert info.image_bin_id_for_border_fill(1) is None


def test_이미지_채우기가_아닌_배경은_None():
    info = parse_doc_info(build_doc_info([(1, "jpg")], [0]))
    assert info.border_fills[0].image_bin_id is None


def test_표와_셀과_배경사진을_찾아낸다():
    section = build_section([
        (0, 0, 1, 1, 1, "사진자료"),
        (1, 0, 2, 1, 3, ""),          # 배경 이미지 셀
        (1, 1, 2, 1, 1, "항공사진"),
    ])
    result = parse_section(section, section_index=0, max_bin=2)
    assert len(result.tables) == 1
    table = result.tables[0]
    assert len(table.cells) == 3
    assert table.cell_starting_at(0, 1, 2).border_fill_id == 3
    assert table.cell_starting_at(1, 1, 2).text == "항공사진"


def test_그림_개체의_BinData_번호는_오프셋_71():
    payload = bytearray(91)
    struct.pack_into("<H", payload, 71, 5)
    assert parse_picture_bin_id(bytes(payload), 6) == 5


def test_범위를_벗어난_그림_번호는_받지_않는다():
    payload = bytearray(91)
    struct.pack_into("<H", payload, 71, 900)
    assert parse_picture_bin_id(bytes(payload), 6) is None


def test_개체_설명문에서_원본_이름을_뽑는다():
    text = ("그림입니다.\r\n원본 그림의 이름: DSC_1234.JPG\r\n"
            "원본 그림의 크기: 가로 6000pixel, 세로 4000pixel\r\n"
            "사진 찍은 날짜: 2025년 09월 04일 오전 11:16")
    hint = parse_picture_hint(text)
    assert hint.original_name == "DSC_1234.JPG"
    assert (hint.width, hint.height) == (6000, 4000)
    assert "2025년" in hint.taken_note


def test_설명문은_NUL_에_둘러싸여_있어도_찾는다():
    payload = b"\x00" * 62 + "원본 그림의 이름: A.JPG".encode("utf-16le") + b"\x00" * 20
    assert "A.JPG" in find_shape_description(payload)


def test_설명문이_없으면_빈_문자열():
    assert find_shape_description(b"\x00" * 200) == ""
    assert parse_picture_hint("그냥 글") is None


def test_확장자가_거짓말해도_내용으로_형식을_가린다(tmp_path):
    """.hwpx 인데 속은 HWP 5.x 인 파일, 그 반대인 파일이 실제로 돌아다닌다."""
    from findpic.hwp.extract import sniff_format

    ole = tmp_path / "속았지.hwpx"
    ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    assert sniff_format(ole) == "hwp"

    zipped = tmp_path / "이것도.hwp"
    zipped.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert sniff_format(zipped) == "hwpx"

    # 알 수 없는 내용이면 확장자를 따른다
    unknown = tmp_path / "몰라.hwpx"
    unknown.write_bytes(b"?" * 32)
    assert sniff_format(unknown) == "hwpx"
    assert sniff_format(tmp_path / "없는파일.hwp") == "hwp"


def test_문단이_여럿이면_줄바꿈으로_잇는다():
    from conftest import para_text, record
    from findpic.hwp import tags as T

    section = record(T.PARA_HEADER, 0, b"\x00" * 24)
    section += record(T.CTRL_HEADER, 1, ctrl_header("tbl "))
    section += record(T.TABLE, 2, b"\x00" * 44)
    section += record(T.LIST_HEADER, 2, cell_record(col=0, row=0))
    section += record(T.PARA_HEADER, 2, b"\x00" * 24)
    section += record(T.PARA_TEXT, 3, para_text("첫째 줄"))
    section += record(T.PARA_HEADER, 2, b"\x00" * 24)
    section += record(T.PARA_TEXT, 3, para_text("둘째 줄"))
    table = parse_section(section, section_index=0, max_bin=1).tables[0]
    assert table.cells[0].text == "첫째 줄\n둘째 줄"


def test_셀_안의_표는_따로_센다():
    from conftest import para_text, record
    from findpic.hwp import tags as T

    section = record(T.PARA_HEADER, 0, b"\x00" * 24)
    section += record(T.CTRL_HEADER, 1, ctrl_header("tbl "))          # 바깥 표
    section += record(T.TABLE, 2, b"\x00" * 44)
    section += record(T.LIST_HEADER, 2, cell_record(col=0, row=0))
    section += record(T.PARA_HEADER, 2, b"\x00" * 24)
    section += record(T.PARA_TEXT, 3, para_text("바깥"))
    section += record(T.CTRL_HEADER, 3, ctrl_header("tbl "))          # 안쪽 표
    section += record(T.TABLE, 4, b"\x00" * 44)
    section += record(T.LIST_HEADER, 4, cell_record(col=0, row=0))
    section += record(T.PARA_HEADER, 4, b"\x00" * 24)
    section += record(T.PARA_TEXT, 5, para_text("안쪽"))

    result = parse_section(section, section_index=0, max_bin=1)
    assert len(result.tables) == 2
    assert result.tables[0].cells[0].text == "바깥"       # 안쪽 글이 섞이지 않는다
    assert result.tables[1].cells[0].text == "안쪽"
    assert result.tables[1].depth == 1


def test_단색과_이미지가_함께_켜진_배경도_읽는다():
    """단색 채우기는 12바이트다. 17바이트로 잘못 세면 그림 번호가 5바이트 밀린다."""
    from conftest import build_doc_info

    info = parse_doc_info(build_doc_info(
        [(1, "jpg"), (2, "jpg")],
        [{"image_bin_id": 2, "solid": True}],
    ))
    assert info.border_fills[0].fill_type == 0x03
    assert info.image_bin_id_for_border_fill(1) == 2
    assert not info.warnings                       # 추정으로 넘어가지 않아야 한다


def test_그러데이션과_이미지가_함께_켜진_배경도_읽는다():
    from conftest import build_doc_info

    info = parse_doc_info(build_doc_info(
        [(1, "jpg")] * 3,
        [{"image_bin_id": 3, "gradient_colors": 2}],
    ))
    assert info.border_fills[0].fill_type == 0x06
    assert info.image_bin_id_for_border_fill(1) == 3
    assert not info.warnings


def test_색_셋_이상인_그러데이션도_읽는다():
    from conftest import build_doc_info

    info = parse_doc_info(build_doc_info(
        [(1, "jpg")] * 4,
        [{"image_bin_id": 4, "solid": True, "gradient_colors": 4}],
    ))
    assert info.image_bin_id_for_border_fill(1) == 4
    assert not info.warnings


def test_BMP_밖_글자가_반쪽으로_깨지지_않는다():
    """확장 한자 같은 글자는 UTF-16 에서 두 칸을 쓴다.

    한 칸씩 chr() 로 만들면 짝이 풀려, 파일 이름으로 쓰는 순간
    UnicodeEncodeError 로 작업 전체가 멈춘다.
    """
    from findpic.naming import sanitize

    payload = "𠮷石窟 전경".encode("utf-16le") + struct.pack("<H", 13)
    text = decode_para_text(payload)
    assert text.strip() == "𠮷石窟 전경"
    text.encode("utf-8")                       # 여기서 터지면 안 된다
    assert sanitize(text) == "𠮷石窟 전경"


def test_반쪽_문자가_섞여_들어와도_파일_이름을_만든다():
    from findpic.naming import number_captions, sanitize

    broken = "\ud842" + "전경"
    assert sanitize(broken) == "전경"
    assert number_captions([broken, broken]) == ["전경(1)", "전경(2)"]


def test_글상자_안의_표도_셀을_읽는다():
    """글상자(gso) 안에 표가 든 문서가 흔하다. 표가 통째로 사라지면 안 된다."""
    from conftest import para_text, record
    from findpic.hwp import tags as T

    section = record(T.PARA_HEADER, 0, b"\x00" * 24)
    section += record(T.CTRL_HEADER, 1, ctrl_header("gso "))       # 글상자
    section += record(T.SHAPE_COMPONENT, 2, b"\x00" * 40)
    section += record(T.LIST_HEADER, 2, b"\x00" * 30)              # 글상자 자신의 목록
    section += record(T.PARA_HEADER, 2, b"\x00" * 24)
    section += record(T.PARA_TEXT, 3, para_text("글상자 안내문"))
    section += record(T.CTRL_HEADER, 3, ctrl_header("tbl "))       # 그 안의 표
    section += record(T.TABLE, 4, b"\x00" * 44)
    section += record(T.LIST_HEADER, 4, cell_record(col=0, row=0))
    section += record(T.PARA_HEADER, 4, b"\x00" * 24)
    section += record(T.PARA_TEXT, 5, para_text("전경"))

    result = parse_section(section, section_index=0, max_bin=1)
    assert len(result.tables) == 1
    assert len(result.tables[0].cells) == 1
    assert result.tables[0].cells[0].text == "전경"
    # 글상자 자신의 글은 셀 글도 본문도 아니다
    assert result.body_text == []


def test_겹친_글상자에서_바깥_상태가_먼저_풀리지_않는다():
    from conftest import para_text, record
    from findpic.hwp import tags as T

    section = record(T.PARA_HEADER, 0, b"\x00" * 24)
    section += record(T.CTRL_HEADER, 1, ctrl_header("gso "))       # 바깥 글상자
    section += record(T.SHAPE_COMPONENT, 2, b"\x00" * 40)
    section += record(T.CTRL_HEADER, 4, ctrl_header("gso "))       # 안쪽 글상자
    section += record(T.SHAPE_COMPONENT, 5, b"\x00" * 40)
    section += record(T.PARA_HEADER, 3, b"\x00" * 24)              # 아직 바깥 글상자 안이다
    section += record(T.PARA_TEXT, 4, para_text("도장 문구"))

    result = parse_section(section, section_index=0, max_bin=1)
    assert result.body_text == []          # 본문으로 새면 안 된다


def test_압축을_푼_결과를_버리지_않는다():
    """서명 목록에 없는 형식이어도 압축본을 그대로 돌려주면 안 된다."""
    import zlib

    from findpic.hwp.docinfo import COMPRESS_ALWAYS, BinDataEntry
    from findpic.hwp.extract import _decode_bin

    payload = b"ftypheic" + b"\x00" * 5000        # 아는 서명이 아닌 형식
    packed = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = packed.compress(payload) + packed.flush()
    entry = BinDataEntry(index=1, compress=COMPRESS_ALWAYS)
    assert _decode_bin(raw, entry, True) == payload


def test_압축_안_한_JPEG_은_그대로_돌려준다():
    from findpic.hwp.docinfo import COMPRESS_NEVER, BinDataEntry
    from findpic.hwp.extract import _decode_bin

    raw = b"\xff\xd8\xff\xe0" + b"\x00" * 200
    entry = BinDataEntry(index=1, compress=COMPRESS_NEVER)
    assert _decode_bin(raw, entry, True) == raw

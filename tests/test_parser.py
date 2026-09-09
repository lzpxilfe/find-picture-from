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

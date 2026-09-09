"""파일 이름 정하기."""

import pytest

from findpic.naming import apply_template, number_captions, sanitize, unique_path


def test_같은_이름은_번호를_붙인다():
    assert number_captions(["항공사진", "전경", "근경", "근경", "유물사진"]) == [
        "항공사진", "전경", "근경(1)", "근경(2)", "유물사진",
    ]


def test_한_번만_나오는_이름에는_번호를_안_붙인다():
    assert number_captions(["전경"]) == ["전경"]


def test_셋_이상도_차례로():
    assert number_captions(["근경"] * 3) == ["근경(1)", "근경(2)", "근경(3)"]


def test_이름이_비면_기본값을_쓴다():
    assert number_captions(["", "", "전경"]) == ["사진(1)", "사진(2)", "전경"]


@pytest.mark.parametrize("raw, expected", [
    ("확인 유물", "확인 유물"),
    ('사진/근경', "사진근경"),
    ("전경: 남쪽", "전경 남쪽"),
    ("근경.", "근경"),
    ("  전경  ", "전경"),
    ("a<b>c", "abc"),
])
def test_운영체제가_싫어하는_글자를_뺀다(raw, expected):
    assert sanitize(raw) == expected


def test_윈도우_예약어는_피한다():
    assert sanitize("CON") == "_CON"
    assert sanitize("aux.jpg") == "_aux.jpg"


def test_아주_긴_이름은_자른다():
    out = sanitize("가" * 200)
    assert len(out.encode("utf-8")) <= 150


def test_서식_채우기():
    assert apply_template("{도면명칭}_{이름}", {"도면명칭": "대전_026", "이름": "전경"}) == "대전_026_전경"
    # 없는 변수는 비워 두고 구분자가 겹치지 않게 정리한다
    assert apply_template("{없음}_{이름}", {"이름": "전경"}) == "전경"


def test_겹치는_파일은_덮어쓰지_않는다(tmp_path):
    (tmp_path / "전경.jpg").write_bytes(b"x")
    assert unique_path(tmp_path, "전경", ".jpg").name == "전경-2.jpg"
    (tmp_path / "전경-2.jpg").write_bytes(b"x")
    assert unique_path(tmp_path, "전경", ".jpg").name == "전경-3.jpg"

"""검토 뒤 고친 것을 다시 넣기."""

import csv
import json

from conftest import make_jpeg

from findpic.cli import main
from findpic.organize import STATE_NAME


def setup_state(tmp_path):
    """정리를 한 번 끝낸 상태를 흉내 낸다."""
    folder = tmp_path / "정리" / "대전_026"
    folder.mkdir(parents=True)
    wrong = folder / "근경.JPG"
    wrong.write_bytes(make_jpeg(400, 300, seed=1))

    originals = tmp_path / "originals"
    originals.mkdir()
    right = originals / "DSC_1005.JPG"
    right.write_bytes(make_jpeg(1600, 1200, seed=2))

    state = tmp_path / "정리" / "_findpic" / STATE_NAME
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "version": 1,
        "documents": [{
            "문서": "대전_026.hwp",
            "문서경로": str(tmp_path / "대전_026.hwp"),
            "폴더": str(folder),
            "사진": [{"이름": "근경", "번호": 5, "넣은파일": str(wrong),
                     "종류": "원본", "판정": "확인필요"}],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return folder, wrong, right, state


def write_corrections(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["한글파일", "사진이름", "가져올파일"])
        writer.writerows(rows)


def test_고른_원본으로_바꿔_넣는다(tmp_path):
    folder, wrong, right, state = setup_state(tmp_path)
    csv_path = tmp_path / "수정목록.csv"
    write_corrections(csv_path, [["대전_026.hwp", "근경", str(right)]])

    assert main(["apply", "--csv", str(csv_path), "--state", str(state)]) == 0
    assert (folder / "근경.JPG").read_bytes() == right.read_bytes()
    assert right.exists()                       # 원본은 그대로 남는다


def test_계획만_보기는_바꾸지_않는다(tmp_path):
    folder, wrong, right, state = setup_state(tmp_path)
    before = wrong.read_bytes()
    csv_path = tmp_path / "수정목록.csv"
    write_corrections(csv_path, [["대전_026.hwp", "근경", str(right)]])

    assert main(["apply", "--csv", str(csv_path), "--state", str(state), "--dry-run"]) == 0
    assert wrong.read_bytes() == before


def test_상태_파일을_옆에서_스스로_찾는다(tmp_path):
    folder, wrong, right, state = setup_state(tmp_path)
    csv_path = state.parent / "수정목록.csv"      # _findpic 폴더 안에 두면
    write_corrections(csv_path, [["대전_026.hwp", "근경", str(right)]])
    assert main(["apply", "--csv", str(csv_path)]) == 0
    assert (folder / "근경.JPG").read_bytes() == right.read_bytes()


def test_바뀐_내용이_상태에_남는다(tmp_path):
    folder, wrong, right, state = setup_state(tmp_path)
    csv_path = tmp_path / "수정목록.csv"
    write_corrections(csv_path, [["대전_026.hwp", "근경", str(right)]])
    main(["apply", "--csv", str(csv_path), "--state", str(state)])

    saved = json.loads(state.read_text(encoding="utf-8"))
    photo = saved["documents"][0]["사진"][0]
    assert photo["판정"] == "사람이 고름"
    assert photo["종류"] == "원본"


def test_없는_원본을_지정하면_알려_준다(tmp_path, capsys):
    _folder, _wrong, _right, state = setup_state(tmp_path)
    csv_path = tmp_path / "수정목록.csv"
    write_corrections(csv_path, [["대전_026.hwp", "근경", "/없는/경로.jpg"]])
    main(["apply", "--csv", str(csv_path), "--state", str(state)])
    out = capsys.readouterr().out
    assert "원본이 없습니다" in out and "1건 실패" in out


def test_모르는_줄은_건너뛴다(tmp_path, capsys):
    _folder, _wrong, right, state = setup_state(tmp_path)
    csv_path = tmp_path / "수정목록.csv"
    write_corrections(csv_path, [["모르는파일.hwp", "전경", str(right)]])
    main(["apply", "--csv", str(csv_path), "--state", str(state)])
    assert "어디에 넣었는지 알 수 없어" in capsys.readouterr().out


def test_수정목록이_없으면_1을_돌려준다(tmp_path, capsys):
    assert main(["apply", "--csv", str(tmp_path / "없다.csv")]) == 1
    assert "수정목록.csv" in capsys.readouterr().out


def test_상태_파일이_없으면_알려_준다(tmp_path, capsys):
    csv_path = tmp_path / "수정목록.csv"
    write_corrections(csv_path, [])
    assert main(["apply", "--csv", str(csv_path)]) == 1
    assert STATE_NAME in capsys.readouterr().out

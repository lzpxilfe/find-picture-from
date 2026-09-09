"""명령줄 다루기."""

from pathlib import Path

import pytest

from findpic.cli import build_parser, clean_path, collect_hwp, main, _dur


@pytest.mark.parametrize("raw, expected", [
    ('"D:\\보고서\\대전_026.hwp"', "D:\\보고서\\대전_026.hwp"),
    ("'/home/사용자/보고서'", "/home/사용자/보고서"),
    ("  D:\\보고서  ", "D:\\보고서"),
])
def test_끌어다_놓은_경로의_따옴표를_뗀다(raw, expected):
    assert clean_path(raw) == expected


def test_한글_파일만_골라낸다(tmp_path):
    (tmp_path / "가.hwp").write_bytes(b"")
    (tmp_path / "나.hwpx").write_bytes(b"")
    (tmp_path / "다.docx").write_bytes(b"")
    (tmp_path / "~$임시.hwp").write_bytes(b"")          # 한글이 만드는 임시 파일
    sub = tmp_path / "하위"
    sub.mkdir()
    (sub / "라.hwp").write_bytes(b"")

    names = sorted(p.name for p in collect_hwp([tmp_path]))
    assert names == ["가.hwp", "나.hwpx", "라.hwp"]

    shallow = sorted(p.name for p in collect_hwp([tmp_path], recursive=False))
    assert shallow == ["가.hwp", "나.hwpx"]


def test_파일을_직접_줘도_된다(tmp_path):
    target = tmp_path / "가.hwp"
    target.write_bytes(b"")
    assert collect_hwp([str(target)]) == [target]


def test_없는_경로는_조용히_넘어간다():
    assert collect_hwp(["/존재하지/않는/경로"]) == []


def test_기본_동작은_run():
    args = build_parser().parse_args(["--hwp", "a", "--source", "b", "--dest", "c"])
    assert args.command is None            # cli.main 이 run 으로 해석한다
    assert args.hwp == ["a"] and args.dest == "c"


def test_하위_명령도_인자를_받는다():
    args = build_parser().parse_args(["scan", "--hwp", "a", "--hwp", "b"])
    assert args.command == "scan" and args.hwp == ["a", "b"]


def test_저용량_선택지는_정해진_값만():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--lowres", "이상한값"])


def test_한글_파일이_없으면_1을_돌려준다(tmp_path, capsys):
    code = main(["scan", "--hwp", str(tmp_path)])
    assert code == 1
    assert "찾지 못했습니다" in capsys.readouterr().out


def test_원본_폴더를_안_주면_알려_준다(tmp_path, capsys):
    (tmp_path / "가.hwp").write_bytes(b"not really a hwp")
    code = main(["run", "--hwp", str(tmp_path), "--dest", str(tmp_path / "out")])
    assert code == 1
    assert "--source" in capsys.readouterr().out


def test_읽을_수_없는_파일은_건너뛰고_계속한다(tmp_path, capsys):
    (tmp_path / "가짜.hwp").write_bytes(b"this is not a hwp file")
    code = main(["scan", "--hwp", str(tmp_path)])
    out = capsys.readouterr().out
    assert "건너뜀" in out
    assert code == 0


def test_걸린_시간_표기():
    assert _dur(5) == "5초"
    assert _dur(125) == "2분 5초"
    assert _dur(3725) == "1시간 2분"

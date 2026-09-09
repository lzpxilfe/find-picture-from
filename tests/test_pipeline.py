"""파이프라인 자체 (창과 명령줄이 함께 쓰는 부분)."""

import pytest

from findpic.pipeline import JobConfig, clean_path, collect_hwp, default_workers, run_job


def test_경로의_따옴표를_뗀다():
    assert clean_path('"D:\\보고서"') == "D:\\보고서"


def test_일꾼_수는_코어_수를_넘지_않는다():
    import os
    assert 2 <= default_workers() <= max(2, min(8, os.cpu_count() or 4))


def test_한글_파일이_없으면_알려_준다(tmp_path):
    messages = []
    result = run_job(JobConfig(hwp=[str(tmp_path)], source=["x"], dest=str(tmp_path)),
                     on_message=lambda t, k="info": messages.append((k, t)))
    assert not result.ok
    assert "찾지 못했습니다" in result.error
    assert ("error", result.error) in messages


def test_원본_폴더가_없으면_알려_준다(tmp_path):
    (tmp_path / "a.hwp").write_bytes(b"nope")
    result = run_job(JobConfig(hwp=[str(tmp_path)], source=[str(tmp_path / "없음")],
                               dest=str(tmp_path)))
    assert not result.ok and "찾을 수 없습니다" in result.error


def test_읽을_수_없는_파일은_건너뛰고_이어간다(tmp_path):
    (tmp_path / "가짜.hwp").write_bytes(b"not a hwp")
    src = tmp_path / "photos"
    src.mkdir()
    result = run_job(JobConfig(hwp=[str(tmp_path)], source=[str(src)],
                               dest=str(tmp_path / "out"),
                               cache=str(tmp_path / "idx.sqlite3")))
    assert "가짜.hwp" in result.skipped_documents
    assert not result.ok                       # 넣을 사진이 없어서 끝난다


def test_중단하면_cancelled_로_끝난다(tmp_path):
    (tmp_path / "a.hwp").write_bytes(b"nope")
    src = tmp_path / "photos"
    src.mkdir()
    result = run_job(JobConfig(hwp=[str(tmp_path)], source=[str(src)],
                               dest=str(tmp_path / "out"),
                               cache=str(tmp_path / "idx.sqlite3")),
                     should_stop=lambda: True)
    assert result.cancelled and not result.ok


def test_진행_알림이_단계_이름과_함께_온다(tmp_path):
    from conftest import make_jpeg

    (tmp_path / "a.hwp").write_bytes(b"nope")
    src = tmp_path / "photos"
    src.mkdir()
    (src / "p.JPG").write_bytes(make_jpeg(400, 300, seed=1))
    stages = set()
    run_job(JobConfig(hwp=[str(tmp_path)], source=[str(src)], dest=str(tmp_path / "out"),
                      cache=str(tmp_path / "idx.sqlite3")),
            on_progress=lambda stage, done, total, note="": stages.add(stage))
    assert "읽는 중" in stages

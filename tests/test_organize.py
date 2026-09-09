"""폴더 찾기 · 파일 넣기 · 리포트."""

import io
from pathlib import Path

from conftest import make_jpeg

from findpic.index import PhotoIndex
from findpic.match import CERTAIN, NOT_FOUND, Matcher, build_query
from findpic.model import BinItem, HwpDocument, PhotoSlot
from findpic.organize import (LOWRES_SKIP, LOWRES_SUBDIR, LOWRES_SUFFIX, csv_rows,
                              place, plan_names, resolve_folder, write_csv)
from findpic.report import DocReport
from findpic import report as report_mod
from tests.test_match import shrink


def make_doc(tmp_path, name="대전_026 효평동 유물산포지2.hwp", captions=("항공사진", "전경"),
             photos=None):
    doc = HwpDocument(path=tmp_path / name)
    doc.fields = {"도면 명칭": "대전_026", "도면명칭": "대전_026", "유적명": "대전 효평동 유물산포지2"}
    for i, caption in enumerate(captions, start=1):
        doc.slots.append(PhotoSlot(bin_id=i, caption=caption, group="사진자료",
                                   caption_source="아래 칸", doc_order=i))
        data = (photos or {}).get(caption) or make_jpeg(400, 300, seed=i)
        doc.bin_items[i] = BinItem(bin_id=i, ext="jpg", data=data,
                                   stream=f"BinData/BIN{i:04X}.jpg")
    return doc


def test_파일_이름과_같은_폴더를_찾는다(tmp_path):
    dest = tmp_path / "정리"
    (dest / "대전_026 효평동 유물산포지2").mkdir(parents=True)
    doc = make_doc(tmp_path)
    choice = resolve_folder(doc, dest)
    assert choice.path.name == "대전_026 효평동 유물산포지2"
    assert not choice.created


def test_도면_명칭으로도_폴더를_찾는다(tmp_path):
    dest = tmp_path / "정리"
    (dest / "대전_026").mkdir(parents=True)
    doc = make_doc(tmp_path, name="이름이 전혀 다른 보고서.hwp")
    choice = resolve_folder(doc, dest)
    assert choice.path.name == "대전_026"


def test_폴더_이름이_파일_이름에_들어_있어도_찾는다(tmp_path):
    dest = tmp_path / "정리"
    (dest / "효평동 유물산포지2").mkdir(parents=True)
    doc = make_doc(tmp_path, name="2025 대전 효평동 유물산포지2 정밀지표조사.hwp")
    doc.fields = {}
    choice = resolve_folder(doc, dest)
    assert choice.path.name == "효평동 유물산포지2"


def test_맞는_폴더가_없으면_새로_만든다(tmp_path):
    dest = tmp_path / "정리"
    dest.mkdir()
    choice = resolve_folder(make_doc(tmp_path), dest)
    assert choice.created and choice.path.parent == dest


def test_새로_만들지_말라고_하면_건너뛴다(tmp_path):
    dest = tmp_path / "정리"
    dest.mkdir()
    choice = resolve_folder(make_doc(tmp_path), dest, create_missing=False)
    assert choice.path is None


def test_같은_이름은_번호를_붙여_넣는다(tmp_path):
    doc = make_doc(tmp_path, captions=("항공사진", "근경", "근경", "유물사진"))
    assert plan_names(doc) == ["항공사진", "근경(1)", "근경(2)", "유물사진"]


def test_원본을_찾으면_원본을_복사한다(tmp_path):
    originals = tmp_path / "originals"
    originals.mkdir()
    big = make_jpeg(1600, 1200, seed=5)
    (originals / "DSC_0005.JPG").write_bytes(big)
    for i in range(6):
        (originals / f"other{i}.JPG").write_bytes(make_jpeg(1600, 1200, seed=100 + i))
    index = PhotoIndex(tmp_path / "index.sqlite3")
    index.refresh([originals], workers=2)
    matcher = Matcher(index.load_all())

    doc = make_doc(tmp_path, captions=("항공사진",), photos={"항공사진": shrink(big)})
    matches = [matcher.match(build_query(doc.bin_items[1].data))]
    folder = tmp_path / "정리" / "대전_026"
    placed = place(doc, matches, folder)
    assert placed[0].verdict == CERTAIN
    assert placed[0].target.name == "항공사진.JPG"
    assert placed[0].target.read_bytes() == big          # 원본 그대로
    assert (originals / "DSC_0005.JPG").exists()          # 원본은 그대로 남는다


def test_못_찾으면_저용량이라도_넣는다(tmp_path):
    from findpic.match import SlotMatch
    doc = make_doc(tmp_path, captions=("전경",))
    folder = tmp_path / "정리" / "대전_026"
    placed = place(doc, [SlotMatch(verdict=NOT_FOUND)], folder)
    assert placed[0].origin == "저용량"
    assert placed[0].target.name == "전경.jpg"
    assert placed[0].target.read_bytes() == doc.bin_items[1].data


def test_저용량_표시_방식을_고를_수_있다(tmp_path):
    from findpic.match import SlotMatch
    doc = make_doc(tmp_path, captions=("전경",))
    base = tmp_path / "정리"
    assert place(doc, [SlotMatch(verdict=NOT_FOUND)], base / "a",
                 lowres=LOWRES_SUFFIX)[0].target.name == "전경(저용량).jpg"
    assert place(doc, [SlotMatch(verdict=NOT_FOUND)], base / "b",
                 lowres=LOWRES_SUBDIR)[0].target.parent.name == "_저용량"
    skipped = place(doc, [SlotMatch(verdict=NOT_FOUND)], base / "c", lowres=LOWRES_SKIP)[0]
    assert skipped.target is None and skipped.skipped


def test_이미_있는_파일을_덮어쓰지_않는다(tmp_path):
    from findpic.match import SlotMatch
    doc = make_doc(tmp_path, captions=("전경",))
    folder = tmp_path / "정리" / "대전_026"
    folder.mkdir(parents=True)
    (folder / "전경.jpg").write_bytes("먼저 있던 파일".encode("utf-8"))
    placed = place(doc, [SlotMatch(verdict=NOT_FOUND)], folder)
    assert placed[0].target.name == "전경-2.jpg"
    assert (folder / "전경.jpg").read_bytes() == "먼저 있던 파일".encode("utf-8")


def test_계획만_보기는_파일을_만들지_않는다(tmp_path):
    from findpic.match import SlotMatch
    doc = make_doc(tmp_path, captions=("전경",))
    folder = tmp_path / "정리" / "대전_026"
    placed = place(doc, [SlotMatch(verdict=NOT_FOUND)], folder, dry_run=True)
    assert placed[0].target is not None
    assert not folder.exists()


def test_결과_목록과_리포트를_만든다(tmp_path):
    from findpic.match import SlotMatch
    doc = make_doc(tmp_path, captions=("항공사진", "전경"))
    folder = tmp_path / "정리" / "대전_026"
    matches = [SlotMatch(verdict=NOT_FOUND), SlotMatch(verdict=NOT_FOUND)]
    placed = place(doc, matches, folder)

    csv_path = tmp_path / "결과목록.csv"
    write_csv(csv_path, csv_rows(doc, placed))
    text = csv_path.read_text(encoding="utf-8-sig")
    assert "항공사진" in text and "저용량" in text

    html_path = tmp_path / "리포트.html"
    report_mod.write(html_path, [DocReport(doc=doc, folder=folder, folder_how="테스트",
                                           matches=matches, placed=placed)])
    html = html_path.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "항공사진" in html and "못찾음" in html
    assert "data:image/jpeg;base64," in html          # 썸네일이 파일 안에 들어 있다


def test_연번만_다른_폴더에_넣지_않는다(tmp_path):
    """가장 위험한 오답이다. '대전_001' 의 사진이 '대전_002' 폴더로 가면 안 된다."""
    dest = tmp_path / "정리"
    for i in (2, 4, 6):
        (dest / f"대전_{i:03d} 조사카드").mkdir(parents=True)
    doc = make_doc(tmp_path, name="대전_001 조사카드.hwp")
    doc.fields = {}
    choice = resolve_folder(doc, dest)
    assert choice.created                                # 새로 만들어야 한다
    assert choice.path.name == "대전_001 조사카드"


def test_숫자가_같으면_비슷한_이름_폴더를_받는다(tmp_path):
    dest = tmp_path / "정리"
    (dest / "대전_026 조사카드").mkdir(parents=True)
    doc = make_doc(tmp_path, name="대전_026 조사카드 (수정).hwp")
    doc.fields = {}
    choice = resolve_folder(doc, dest)
    assert not choice.created and choice.path.name == "대전_026 조사카드"


def test_두루뭉술한_폴더가_모든_문서를_빨아들이지_않는다(tmp_path):
    dest = tmp_path / "정리"
    (dest / "조사카드").mkdir(parents=True)
    for i in (1, 2, 3):
        doc = make_doc(tmp_path, name=f"대전_{i:03d} 조사카드.hwp")
        doc.fields = {}
        choice = resolve_folder(doc, dest)
        assert choice.created, f"대전_{i:03d} 이 '조사카드' 폴더로 빨려 들어갔다"


def test_연번이_많아도_서로_다른_폴더로_간다(tmp_path):
    """실제로 이 상황에서 60건이 30개 폴더로 몰리는 사고가 있었다."""
    dest = tmp_path / "정리"
    dest.mkdir()
    existing = []
    for i in range(2, 21, 2):
        folder = dest / f"대전_{i:03d} 조사카드"
        folder.mkdir()
        existing.append(folder)

    seen = {}
    for i in range(1, 21):
        doc = make_doc(tmp_path, name=f"대전_{i:03d} 조사카드.hwp")
        doc.fields = {}
        choice = resolve_folder(doc, dest, existing=list(existing))
        assert choice.path.name not in seen, \
            f"{doc.path.name} 이 {seen.get(choice.path.name)} 과 같은 폴더로 갔다"
        seen[choice.path.name] = doc.path.name
        if choice.created:
            existing.append(choice.path)
    assert len(seen) == 20


def test_덮어쓰기는_확장자가_달라진_옛_파일도_치운다(tmp_path):
    """다시 돌려 고쳐 넣을 때 '항공사진.jpg'(옛 사본)와 '항공사진.JPG'(새 원본)가
    둘 다 남으면 어느 쪽이 원본인지 알 수 없다."""
    from findpic.match import SlotMatch

    folder = tmp_path / "정리" / "대전_026"
    folder.mkdir(parents=True)
    (folder / "전경.jpg").write_bytes(b"old small copy")

    originals = tmp_path / "originals"
    originals.mkdir()
    big = originals / "DSC_0001.JPG"
    big.write_bytes(make_jpeg(1600, 1200, seed=3))

    doc = make_doc(tmp_path, captions=("전경",))
    from findpic.index import PhotoIndex
    index = PhotoIndex(tmp_path / "idx.sqlite3")
    index.refresh([originals], workers=2)
    rec = index.load_all()[0]
    from findpic.match import Candidate
    match = SlotMatch(verdict=CERTAIN, best=Candidate(record=rec, score=0.99))

    placed = place(doc, [match], folder, overwrite=True)
    assert placed[0].target.name == "전경.JPG"
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["전경.JPG"]                    # 옛 .jpg 는 사라져야 한다
    assert big.exists()                             # 원본은 그대로


def test_덮어쓰기를_켜도_원본_사진은_지우지_않는다(tmp_path):
    """원본 폴더와 결과 폴더가 같은 곳일 때도 원본을 지우면 안 된다."""
    from findpic.index import PhotoIndex
    from findpic.match import Candidate, SlotMatch

    folder = tmp_path / "한곳"
    folder.mkdir()
    original = folder / "전경.JPG"
    original.write_bytes(make_jpeg(1600, 1200, seed=4))

    index = PhotoIndex(tmp_path / "idx.sqlite3")
    index.refresh([folder], workers=2)
    rec = index.load_all()[0]

    doc = make_doc(tmp_path, captions=("전경",))
    match = SlotMatch(verdict=CERTAIN, best=Candidate(record=rec, score=0.99))
    place(doc, [match], folder, overwrite=True)
    assert original.exists() and original.stat().st_size > 0

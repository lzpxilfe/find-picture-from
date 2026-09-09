"""축소 사진과 원본 잇기."""

import io

from conftest import make_jpeg
from PIL import Image

from findpic.index import PhotoIndex
from findpic.match import CERTAIN, NOT_FOUND, REVIEW, Matcher, assign, build_query


def shrink(data: bytes, factor: int = 4, quality: int = 70,
           keep_exif: bool = True) -> bytes:
    """원본을 보고서에 넣을 때처럼 줄이고 다시 압축한다.

    한글은 사진을 줄여 넣어도 EXIF 를 그대로 남긴다. 그 성질이 원본 찾기의
    핵심이므로 기본값으로 EXIF 를 유지한다.
    """
    im = Image.open(io.BytesIO(data))
    exif = im.info.get("exif")
    out = io.BytesIO()
    small = im.resize((im.width // factor, im.height // factor), Image.LANCZOS)
    if exif and keep_exif:
        small.save(out, "JPEG", quality=quality, exif=exif)
    else:
        small.save(out, "JPEG", quality=quality)
    return out.getvalue()


def build_index(tmp_path, photos):
    folder = tmp_path / "originals"
    folder.mkdir(exist_ok=True)
    for name, data in photos.items():
        (folder / name).write_bytes(data)
    index = PhotoIndex(tmp_path / "index.sqlite3")
    index.refresh([folder], workers=2)
    return Matcher(index.load_all()), folder


def test_줄인_사진에서_원본을_찾는다(tmp_path):
    photos = {f"DSC_{i:04d}.JPG": make_jpeg(1200, 800, seed=i) for i in range(1, 26)}
    matcher, _ = build_index(tmp_path, photos)
    result = matcher.match(build_query(shrink(photos["DSC_0007.JPG"])))
    assert result.verdict == CERTAIN
    assert result.path.endswith("DSC_0007.JPG")


def test_전혀_없는_사진은_못찾음(tmp_path):
    photos = {f"DSC_{i:04d}.JPG": make_jpeg(1200, 800, seed=i) for i in range(1, 26)}
    matcher, _ = build_index(tmp_path, photos)
    result = matcher.match(build_query(make_jpeg(1200, 800, seed=777)))
    assert result.verdict == NOT_FOUND
    assert result.path is None


def test_EXIF_가_같으면_바로_확정한다(tmp_path):
    blob = _exif("D5500", "2025:09:04 11:16:16", "59")
    target = make_jpeg(1200, 800, seed=11, exif=blob)
    photos = {"DSC_0001.JPG": target}
    photos.update({f"DSC_{i:04d}.JPG": make_jpeg(1200, 800, seed=i) for i in range(2, 21)})
    matcher, _ = build_index(tmp_path, photos)
    result = matcher.match(build_query(shrink(target)))
    assert result.verdict == CERTAIN
    assert "EXIF" in result.best.reason


def _exif(model, taken, subsec):
    from PIL import Image as _I
    exif = _I.Exif()
    exif[0x0110] = model
    exif[0x8769] = {0x9003: taken, 0x9291: subsec}
    return exif.tobytes()


def test_EXIF_는_같은데_그림이_다르면_확인필요(tmp_path):
    blob = _exif("D5500", "2025:09:04 11:16:16", "59")
    query_photo = make_jpeg(1200, 800, seed=31, exif=blob)
    decoy = make_jpeg(1200, 800, seed=32, exif=blob)      # EXIF 만 같고 그림은 다르다
    photos = {"WRONG.JPG": decoy}
    photos.update({f"D{i}.JPG": make_jpeg(1200, 800, seed=100 + i) for i in range(12)})
    matcher, _ = build_index(tmp_path, photos)
    result = matcher.match(build_query(shrink(query_photo)))
    assert result.verdict == REVIEW


def test_한글에_적힌_원본_이름으로도_찾는다(tmp_path):
    from findpic.model import PictureHint
    target = make_jpeg(1200, 800, seed=51)
    photos = {"현장사진_012.JPG": target}
    photos.update({f"D{i}.JPG": make_jpeg(1200, 800, seed=200 + i) for i in range(12)})
    matcher, _ = build_index(tmp_path, photos)
    hint = PictureHint(original_name="현장사진_012.JPG", width=1200, height=800)
    result = matcher.match(build_query(shrink(target), hint))
    assert result.verdict == CERTAIN
    assert "파일 이름 일치" in result.best.reason


def test_임시_이름은_단서로_쓰지_않는다(tmp_path):
    from findpic.model import PictureHint
    photos = {"CLP00001d1c371e.bmp": make_jpeg(1200, 800, seed=61)}
    photos.update({f"D{i}.JPG": make_jpeg(1200, 800, seed=300 + i) for i in range(12)})
    matcher, _ = build_index(tmp_path, photos)
    query = build_query(make_jpeg(1200, 800, seed=999),
                        PictureHint(original_name="CLP00001d1c371e.bmp"))
    assert query.useful_hint_name == ""
    assert matcher.match(query).verdict == NOT_FOUND


def test_같은_원본이_두_자리를_차지하지_않는다(tmp_path):
    photos = {f"DSC_{i:04d}.JPG": make_jpeg(1200, 800, seed=i) for i in range(1, 21)}
    matcher, _ = build_index(tmp_path, photos)
    a = matcher.match(build_query(shrink(photos["DSC_0003.JPG"])))
    b = matcher.match(build_query(shrink(photos["DSC_0003.JPG"])))
    assign([a, b])
    assert not (a.path and b.path and a.path == b.path)


def test_색인은_두_번째부터_다시_읽지_않는다(tmp_path):
    folder = tmp_path / "originals"
    folder.mkdir()
    for i in range(6):
        (folder / f"D{i}.JPG").write_bytes(make_jpeg(600, 400, seed=i))
    index = PhotoIndex(tmp_path / "index.sqlite3")
    first = index.refresh([folder], workers=2)
    second = index.refresh([folder], workers=2)
    assert first["새로 읽음"] == 6
    assert second["새로 읽음"] == 0 and second["재사용"] == 6


def test_사라진_파일은_색인에서_빠진다(tmp_path):
    folder = tmp_path / "originals"
    folder.mkdir()
    for i in range(4):
        (folder / f"D{i}.JPG").write_bytes(make_jpeg(600, 400, seed=i))
    index = PhotoIndex(tmp_path / "index.sqlite3")
    index.refresh([folder], workers=2)
    (folder / "D0.JPG").unlink()
    index.refresh([folder], workers=2)
    assert index.count() == 3


def test_EXIF_가_없어도_그림만으로_찾는다(tmp_path):
    """지도·도면처럼 EXIF 가 없는 그림도 이어 붙일 수 있어야 한다."""
    photos = {f"DSC_{i:04d}.JPG": make_jpeg(1200, 800, seed=i) for i in range(1, 41)}
    matcher, _ = build_index(tmp_path, photos)
    result = matcher.match(build_query(shrink(photos["DSC_0021.JPG"], keep_exif=False)))
    assert result.verdict == CERTAIN
    assert result.path.endswith("DSC_0021.JPG")
    assert result.best.score - result.runners_up[0].score > 0.3


def test_돌려_넣은_사진도_찾는다(tmp_path):
    photos = {f"DSC_{i:04d}.JPG": make_jpeg(1200, 800, seed=i) for i in range(1, 21)}
    matcher, _ = build_index(tmp_path, photos)
    turned = Image.open(io.BytesIO(photos["DSC_0005.JPG"])).transpose(Image.ROTATE_90)
    buf = io.BytesIO()
    turned.resize((turned.width // 4, turned.height // 4), Image.LANCZOS).save(buf, "JPEG", quality=75)
    result = matcher.match(build_query(buf.getvalue()))
    assert result.path.endswith("DSC_0005.JPG")
    assert result.best.orientation != "그대로"


def test_흑백으로_바꾼_사본을_원본으로_치지_않는다(tmp_path):
    """밝기 배치가 완전히 같아 다른 지표로는 걸러지지 않는 함정이다."""
    from PIL import Image as _Image

    color = make_jpeg(1200, 800, seed=41)
    gray = io.BytesIO()
    _Image.open(io.BytesIO(color)).convert("L").convert("RGB").save(gray, "JPEG", quality=94)
    photos = {"GRAY_COPY.JPG": gray.getvalue()}
    photos.update({f"D{i}.JPG": make_jpeg(1200, 800, seed=400 + i) for i in range(10)})
    matcher, _ = build_index(tmp_path, photos)

    result = matcher.match(build_query(shrink(color, keep_exif=False)))
    assert result.verdict != CERTAIN
    if result.best:
        assert result.best.chroma_gap > 10


def test_진짜_원본이_있으면_흑백_사본을_이긴다(tmp_path):
    from PIL import Image as _Image

    color = make_jpeg(1200, 800, seed=42)
    gray = io.BytesIO()
    _Image.open(io.BytesIO(color)).convert("L").convert("RGB").save(gray, "JPEG", quality=94)
    photos = {"GRAY_COPY.JPG": gray.getvalue(), "TRUE_ORIGINAL.JPG": color}
    photos.update({f"D{i}.JPG": make_jpeg(1200, 800, seed=500 + i) for i in range(10)})
    matcher, _ = build_index(tmp_path, photos)

    result = matcher.match(build_query(shrink(color, keep_exif=False)))
    assert result.verdict == CERTAIN
    assert result.path.endswith("TRUE_ORIGINAL.JPG")


def test_흑백_원본끼리는_감점되지_않는다(tmp_path):
    """스캔한 도면처럼 원래 흑백인 자료는 그대로 맞아야 한다."""
    from PIL import Image as _Image

    gray_src = io.BytesIO()
    _Image.open(io.BytesIO(make_jpeg(1200, 800, seed=43))).convert("L").convert("RGB").save(
        gray_src, "JPEG", quality=95)
    photos = {"SCAN_001.JPG": gray_src.getvalue()}
    photos.update({f"D{i}.JPG": make_jpeg(1200, 800, seed=600 + i) for i in range(10)})
    matcher, _ = build_index(tmp_path, photos)

    result = matcher.match(build_query(shrink(gray_src.getvalue(), keep_exif=False)))
    assert result.verdict == CERTAIN
    assert result.path.endswith("SCAN_001.JPG")

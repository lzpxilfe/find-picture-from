"""EXIF 읽기와 겉모습 지문."""

import io
import struct

import numpy as np
from conftest import make_jpeg
from PIL import Image

from findpic.imaging import fingerprint as FP
from findpic.imaging.exif import fingerprint_bytes, read_exif_from_bytes
from findpic.imaging.loader import open_image


def exif_blob(**values) -> bytes:
    """Pillow 로 EXIF 덩어리를 만든다."""
    exif = Image.Exif()
    if "Model" in values:
        exif[0x0110] = values["Model"]
    if "Make" in values:
        exif[0x010F] = values["Make"]
    ifd = {}
    if "DateTimeOriginal" in values:
        ifd[0x9003] = values["DateTimeOriginal"]
    if "SubSecTimeOriginal" in values:
        ifd[0x9291] = values["SubSecTimeOriginal"]
    if ifd:
        exif[0x8769] = ifd
    return exif.tobytes()


def test_JPEG_에서_EXIF_를_읽는다():
    data = make_jpeg(200, 150, seed=1,
                     exif=exif_blob(Make="NIKON", Model="D5500",
                                    DateTimeOriginal="2025:09:04 11:16:16",
                                    SubSecTimeOriginal="59"))
    exif = read_exif_from_bytes(data)
    assert exif.get("Model") == "D5500"
    assert exif.get("DateTimeOriginal") == "2025:09:04 11:16:16"


def test_지문의_열쇠는_강한_것부터():
    data = make_jpeg(200, 150, seed=1,
                     exif=exif_blob(Model="D5500",
                                    DateTimeOriginal="2025:09:04 11:16:16",
                                    SubSecTimeOriginal="59"))
    fp = fingerprint_bytes(data)
    names = [n for n, _ in fp.keys]
    assert names[0] == "촬영시각+1/100초+기종"
    assert "촬영시각+기종" in names
    assert fp.has_signal


def test_EXIF_가_없으면_조용히_빈_지문():
    fp = fingerprint_bytes(make_jpeg(120, 90, seed=2))
    assert not fp.has_signal and fp.keys == []


def test_망가진_바이트에도_죽지_않는다():
    assert read_exif_from_bytes(b"\xff\xd8\xff\xe1\x00\x08Exif") == {}
    assert read_exif_from_bytes(b"") == {}


def test_같은_사진은_지문이_같고_다른_사진은_멀다():
    a = open_image(make_jpeg(400, 300, seed=1))
    b = open_image(make_jpeg(400, 300, seed=1))
    c = open_image(make_jpeg(400, 300, seed=99))
    fa, fb, fc = FP.compute(a), FP.compute(b), FP.compute(c)
    assert FP.hamming(fa.dhash, fb.dhash) == 0
    assert FP.hamming(fa.dhash, fc.dhash) > 40
    assert FP.ncc(fa.gray_array(), fb.gray_array()) > 0.99
    assert FP.ncc(fa.gray_array(), fc.gray_array()) < 0.6


def test_크기를_줄이고_다시_저장해도_지문이_살아남는다():
    original = Image.open(io.BytesIO(make_jpeg(1600, 1200, seed=7)))
    small = io.BytesIO()
    original.resize((400, 300), Image.LANCZOS).save(small, "JPEG", quality=70)
    fa = FP.compute(open_image(make_jpeg(1600, 1200, seed=7)))
    fb = FP.compute(open_image(small.getvalue()))
    assert FP.ncc(fa.gray_array(), fb.gray_array()) > 0.95
    assert FP.hamming(fa.dhash, fb.dhash) < 40


def test_색_정보가_사라지지_않는다():
    """JPEG 을 흑백으로 디코딩해 버리면 색으로 구별되는 사진을 놓친다."""
    red = Image.new("RGB", (300, 200), (220, 30, 30))
    blue = Image.new("RGB", (300, 200), (30, 30, 220))
    buf_r, buf_b = io.BytesIO(), io.BytesIO()
    red.save(buf_r, "JPEG"); blue.save(buf_b, "JPEG")
    fr = FP.compute(open_image(buf_r.getvalue()))
    fb = FP.compute(open_image(buf_b.getvalue()))
    assert FP.tile_distance(fr.tile_array(), fb.tile_array()) > 50
    channels = fr.tile_array().reshape(-1, 3).mean(axis=0)
    assert channels[0] > channels[2] + 50


def test_돌린_사진의_지문도_만들어_둔다():
    im = open_image(make_jpeg(400, 300, seed=3))
    names = [n for n, _ in FP.variants(im)]
    assert "그대로" in names and "90도 회전" in names and "좌우 반전" in names


def test_종횡비_차이():
    assert FP.aspect_gap(1.5, 1.5) == 0
    assert FP.aspect_gap(1.5, 1.0) > 0.3
    assert FP.aspect_gap(0, 1.5) == 1.0


def test_열_수_없는_바이트는_None():
    assert open_image(b"not an image") is None


def _jpeg_with_thumbnail(size=(1200, 800), thumb=(160, 106), seed=5, shift=0):
    """EXIF 축소판(IFD1)이 든 JPEG 을 손으로 만든다.

    Pillow 는 축소판을 써 주지 않으므로 TIFF 구조를 직접 조립한다.
    """
    import struct

    body = Image.open(io.BytesIO(make_jpeg(*size, seed=seed))).convert("RGB")
    small = body.resize(thumb, Image.LANCZOS)
    if shift:
        small = Image.eval(small, lambda v: min(255, v + shift))
    tbuf = io.BytesIO()
    small.save(tbuf, "JPEG", quality=80)
    tdata = tbuf.getvalue()

    # TIFF 헤더 + IFD0(PixelXDimension 없이 ImageWidth/Length) + IFD1(축소판)
    def ifd(entries, next_offset, data_offset):
        out = struct.pack("<H", len(entries))
        extra = b""
        for tag, typ, count, value in entries:
            out += struct.pack("<HHI", tag, typ, count)
            out += struct.pack("<I", value)
        out += struct.pack("<I", next_offset)
        return out + extra

    header = b"II" + struct.pack("<HI", 42, 8)
    ifd0_entries = [(0x0100, 4, 1, size[0]), (0x0101, 4, 1, size[1])]
    ifd0_len = 2 + len(ifd0_entries) * 12 + 4
    ifd1_off = 8 + ifd0_len
    ifd1_entries_count = 3
    ifd1_len = 2 + ifd1_entries_count * 12 + 4
    thumb_off = ifd1_off + ifd1_len
    ifd1_entries = [
        (0x0103, 3, 1, 6),                 # Compression = JPEG
        (0x0201, 4, 1, thumb_off),
        (0x0202, 4, 1, len(tdata)),
    ]
    tiff = header + ifd(ifd0_entries, ifd1_off, 0) + ifd(ifd1_entries, 0, 0) + tdata

    out = io.BytesIO()
    # Pillow 는 exif 인자가 "Exif\x00\x00" 로 시작하지 않으면 통째로 버린다
    body.save(out, "JPEG", quality=90, exif=b"Exif\x00\x00" + tiff)
    return out.getvalue()


def test_EXIF_축소판을_꺼낸다(tmp_path):
    from findpic.imaging.exif import read_thumbnail

    path = tmp_path / "photo.JPG"
    path.write_bytes(_jpeg_with_thumbnail())
    thumb = read_thumbnail(path)
    assert thumb and thumb[:2] == b"\xff\xd8"
    with Image.open(io.BytesIO(thumb)) as im:
        assert im.size == (160, 106)


def test_축소판이_없으면_None(tmp_path):
    path = tmp_path / "plain.JPG"
    path.write_bytes(make_jpeg(400, 300, seed=1))
    from findpic.imaging.exif import read_thumbnail
    assert read_thumbnail(path) is None


def test_축소판으로_만든_지문이_본체와_거의_같다(tmp_path):
    """사진기가 넣어 둔 축소판만 읽어도 같은 사진임을 알아볼 수 있어야 한다."""
    from findpic.imaging.exif import read_thumbnail

    path = tmp_path / "photo.JPG"
    path.write_bytes(_jpeg_with_thumbnail(size=(1600, 1067), thumb=(160, 107), seed=9))
    full = FP.compute(open_image(path))
    small = FP.compute(open_image(read_thumbnail(path)))
    assert FP.ncc(full.gray_array(), small.gray_array()) > 0.97
    assert FP.hamming(full.dhash, small.dhash) < 40
    assert FP.tile_distance(full.tile_array(), small.tile_array()) < 12


def test_축소판을_써도_크기는_진짜_값을_남긴다(tmp_path):
    """축소 디코딩한 크기를 그대로 저장하면 6000x4000 사진이 750x500 이 된다."""
    from findpic.index import analyse

    path = tmp_path / "photo.JPG"
    path.write_bytes(_jpeg_with_thumbnail(size=(1600, 1067), thumb=(160, 107), seed=10))
    for fast in (False, True):
        rec = analyse(path, fast=fast)
        assert (rec.width, rec.height) == (1600, 1067), fast

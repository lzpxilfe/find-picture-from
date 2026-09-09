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

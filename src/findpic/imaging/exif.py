"""EXIF 를 직접 읽는다.

Pillow 를 쓰지 않고 TIFF IFD 를 직접 걸어가는 이유:
  * NEF/CR2/ARW/DNG 같은 RAW 원본도 그대로 읽을 수 있다 (전부 TIFF 파생 형식이다)
  * 파일 앞부분 몇십 KB 만 읽으면 되므로 사진 수만 장을 훑을 때 훨씬 빠르다
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# --- TIFF 자료형별 바이트 수 ------------------------------------------------
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

# --- 우리가 쓰는 태그 --------------------------------------------------------
IFD0_TAGS = {
    0x0100: "ImageWidth",
    0x0101: "ImageLength",
    0x010F: "Make",
    0x0110: "Model",
    0x0112: "Orientation",
    0x0131: "Software",
    0x0132: "DateTime",
    0x8769: "_ExifIFD",
    0x8825: "_GPSIFD",
}
EXIF_TAGS = {
    0x829A: "ExposureTime",
    0x829D: "FNumber",
    0x8827: "ISOSpeedRatings",
    0x9003: "DateTimeOriginal",
    0x9004: "DateTimeDigitized",
    0x9201: "ShutterSpeedValue",
    0x9202: "ApertureValue",
    0x920A: "FocalLength",
    0x9290: "SubSecTime",
    0x9291: "SubSecTimeOriginal",
    0x9292: "SubSecTimeDigitized",
    0xA002: "PixelXDimension",
    0xA003: "PixelYDimension",
    0xA420: "ImageUniqueID",
    0xA431: "BodySerialNumber",
    0xA433: "LensMake",
    0xA434: "LensModel",
    0xA435: "LensSerialNumber",
}
GPS_TAGS = {
    0x0001: "GPSLatitudeRef",
    0x0002: "GPSLatitude",
    0x0003: "GPSLongitudeRef",
    0x0004: "GPSLongitude",
    0x0005: "GPSAltitudeRef",
    0x0006: "GPSAltitude",
    0x001D: "GPSDateStamp",
    0x0007: "GPSTimeStamp",
}

# 사진 원본으로 볼 수 있는 확장자
RAW_EXTENSIONS = {".nef", ".cr2", ".cr3", ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw", ".raf", ".3fr"}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".tif", ".tiff", ".bmp", ".gif",
    ".webp", ".heic", ".heif",
} | RAW_EXTENSIONS

_DATE_RE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


class _Window:
    """파일에서 필요한 부분만 읽어 오는 얇은 창.

    EXIF 는 앞쪽에 몰려 있지만 RAW 는 가끔 멀리 떨어진 곳을 가리킨다.
    한 번 읽은 덩어리는 재사용한다.
    """

    CHUNK = 256 * 1024

    def __init__(self, fh, base: int = 0, limit: Optional[int] = None):
        self.fh = fh
        self.base = base
        self.limit = limit
        self._cache: Dict[int, bytes] = {}

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset < 0:
            return b""
        if self.limit is not None and offset + length > self.limit:
            length = max(0, self.limit - offset)
            if length == 0:
                return b""
        start_block = (offset) // self.CHUNK
        end_block = (offset + length - 1) // self.CHUNK
        out = bytearray()
        for block in range(start_block, end_block + 1):
            if block not in self._cache:
                self.fh.seek(self.base + block * self.CHUNK)
                self._cache[block] = self.fh.read(self.CHUNK)
                if len(self._cache) > 8:          # 메모리 상한
                    self._cache.pop(next(iter(self._cache)))
            chunk = self._cache[block]
            lo = max(offset, block * self.CHUNK) - block * self.CHUNK
            hi = min(offset + length, (block + 1) * self.CHUNK) - block * self.CHUNK
            out += chunk[lo:hi]
        return bytes(out)


def _rational(raw: bytes, endian: str, signed: bool) -> Optional[float]:
    fmt = endian + ("ii" if signed else "II")
    if len(raw) < 8:
        return None
    num, den = struct.unpack(fmt, raw[:8])
    if den == 0:
        return 0.0 if num == 0 else None
    return num / den


def _read_ifd(win: _Window, tiff_base: int, ifd_offset: int, endian: str,
              wanted: Dict[int, str], out: Dict[str, Any], depth: int = 0) -> None:
    if depth > 3 or ifd_offset <= 0:
        return
    head = win.read(tiff_base + ifd_offset, 2)
    if len(head) < 2:
        return
    (count,) = struct.unpack(endian + "H", head)
    if count == 0 or count > 4096:      # 깨진 파일 방어
        return
    entries = win.read(tiff_base + ifd_offset + 2, count * 12)
    for i in range(min(count, len(entries) // 12)):
        tag, typ, num = struct.unpack_from(endian + "HHI", entries, i * 12)
        if tag not in wanted:
            continue
        size = _TYPE_SIZE.get(typ, 0) * num
        if size == 0:
            continue
        if size <= 4:
            raw = entries[i * 12 + 8: i * 12 + 8 + size]
        else:
            (ptr,) = struct.unpack_from(endian + "I", entries, i * 12 + 8)
            raw = win.read(tiff_base + ptr, min(size, 4096))
        name = wanted[tag]
        out[name] = _decode_value(raw, typ, num, endian)

    if depth == 0:
        exif_ptr = out.pop("_ExifIFD", None)
        gps_ptr = out.pop("_GPSIFD", None)
        if isinstance(exif_ptr, (int, float)):
            _read_ifd(win, tiff_base, int(exif_ptr), endian, EXIF_TAGS, out, depth + 1)
        if isinstance(gps_ptr, (int, float)):
            gps: Dict[str, Any] = {}
            _read_ifd(win, tiff_base, int(gps_ptr), endian, GPS_TAGS, gps, depth + 1)
            if gps:
                out["GPS"] = gps


def _decode_value(raw: bytes, typ: int, num: int, endian: str) -> Any:
    if typ == 2:        # ASCII
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    if typ in (1, 6, 7):
        return raw[0] if num == 1 and raw else raw
    if typ in (3, 8):
        fmt = endian + ("H" if typ == 3 else "h")
        vals = [struct.unpack_from(fmt, raw, k * 2)[0] for k in range(min(num, len(raw) // 2))]
        return vals[0] if len(vals) == 1 else vals
    if typ in (4, 9):
        fmt = endian + ("I" if typ == 4 else "i")
        vals = [struct.unpack_from(fmt, raw, k * 4)[0] for k in range(min(num, len(raw) // 4))]
        return vals[0] if len(vals) == 1 else vals
    if typ in (5, 10):
        signed = typ == 10
        vals = [_rational(raw[k * 8:(k + 1) * 8], endian, signed) for k in range(min(num, len(raw) // 8))]
        return vals[0] if len(vals) == 1 else vals
    if typ == 11:
        return struct.unpack_from(endian + "f", raw)[0] if len(raw) >= 4 else None
    if typ == 12:
        return struct.unpack_from(endian + "d", raw)[0] if len(raw) >= 8 else None
    return raw


def _parse_tiff(win: _Window) -> Dict[str, Any]:
    head = win.read(0, 8)
    if len(head) < 8:
        return {}
    if head[:2] == b"II":
        endian = "<"
    elif head[:2] == b"MM":
        endian = ">"
    else:
        return {}
    (magic,) = struct.unpack_from(endian + "H", head, 2)
    if magic not in (42, 0x4F52, 0x5352, 85):    # 42 = TIFF, 나머지는 ORF/RW2 변종
        return {}
    (ifd0,) = struct.unpack_from(endian + "I", head, 4)
    out: Dict[str, Any] = {}
    _read_ifd(win, 0, ifd0, endian, IFD0_TAGS, out)
    return out


def _find_jpeg_exif(fh) -> Optional[Tuple[int, int]]:
    """JPEG 안의 APP1(Exif) 조각 위치. (TIFF 시작 오프셋, 길이)"""
    fh.seek(0)
    if fh.read(2) != b"\xff\xd8":
        return None
    pos = 2
    for _ in range(64):          # 조각을 무한정 따라가지 않는다
        fh.seek(pos)
        marker = fh.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return None
        code = marker[1]
        if code in (0xD8, 0xD9) or code == 0xDA:
            return None
        length_raw = fh.read(2)
        if len(length_raw) < 2:
            return None
        (length,) = struct.unpack(">H", length_raw)
        if code == 0xE1:
            sig = fh.read(6)
            if sig == b"Exif\x00\x00":
                return pos + 4 + 6, length - 8
        pos += 2 + length
    return None


def read_exif(path) -> Dict[str, Any]:
    """파일에서 EXIF 를 읽어 {태그이름: 값} 으로. 없으면 빈 딕셔너리."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
            if magic[:2] == b"\xff\xd8":
                found = _find_jpeg_exif(fh)
                if not found:
                    return {}
                base, length = found
                return _parse_tiff(_Window(fh, base=base, limit=max(length, 0) or None))
            if magic[:2] in (b"II", b"MM"):
                return _parse_tiff(_Window(fh))
            if suffix in (".heic", ".heif", ".png", ".webp", ".cr3"):
                return _read_via_pillow(path)
    except (OSError, struct.error, ValueError):
        return {}
    return {}


def read_exif_from_bytes(data: bytes) -> Dict[str, Any]:
    """메모리 위의 이미지(한글 파일에서 꺼낸 사진)에서 EXIF 읽기."""
    import io

    fh = io.BytesIO(data)
    try:
        if data[:2] == b"\xff\xd8":
            found = _find_jpeg_exif(fh)
            if not found:
                return {}
            base, length = found
            return _parse_tiff(_Window(fh, base=base, limit=max(length, 0) or None))
        if data[:2] in (b"II", b"MM"):
            return _parse_tiff(_Window(fh))
    except (struct.error, ValueError):
        return {}
    return {}


def _read_via_pillow(path: Path) -> Dict[str, Any]:
    """직접 걸어가기 어려운 형식은 Pillow 에 맡긴다 (있을 때만)."""
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return {}
    try:
        with Image.open(path) as im:
            raw = im.getexif()
            if not raw:
                return {}
            out: Dict[str, Any] = {}
            for k, v in raw.items():
                name = ExifTags.TAGS.get(k)
                if name in IFD0_TAGS.values() or name in EXIF_TAGS.values():
                    out[name] = v
            try:
                for k, v in raw.get_ifd(0x8769).items():
                    name = ExifTags.TAGS.get(k)
                    if name:
                        out[name] = v
            except Exception:
                pass
            return out
    except Exception:
        return {}


# --- 지문 ------------------------------------------------------------------

@dataclass
class ExifFingerprint:
    """두 사진이 같은 원본인지 판단하는 데 쓰는 값들."""

    taken_at: str = ""          # 'YYYY:MM:DD HH:MM:SS'
    subsec: str = ""            # 1/100 초 단위 꼬리
    make: str = ""
    model: str = ""
    exposure: str = ""
    fnumber: str = ""
    iso: str = ""
    focal: str = ""
    lens: str = ""
    serial: str = ""
    unique_id: str = ""
    gps: str = ""
    width: int = 0
    height: int = 0
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    # 강한 열쇠부터 약한 열쇠까지. 앞의 것이 맞으면 사실상 같은 사진이다.
    @property
    def keys(self):
        out = []
        if self.taken_at and self.model:
            if self.subsec:
                out.append(("촬영시각+1/100초+기종", f"{self.taken_at}|{self.subsec}|{self.model}"))
            out.append(("촬영시각+기종", f"{self.taken_at}|{self.model}"))
        if self.unique_id:
            out.insert(0, ("사진 고유 ID", self.unique_id))
        if self.taken_at:
            out.append(("촬영시각", self.taken_at))
        return out

    @property
    def settings(self) -> str:
        return f"{self.exposure}|{self.fnumber}|{self.iso}|{self.focal}"

    @property
    def has_signal(self) -> bool:
        return bool(self.taken_at or self.unique_id)

    def describe(self) -> str:
        bits = []
        if self.model:
            bits.append(self.model)
        if self.taken_at:
            t = self.taken_at
            if self.subsec:
                t += "." + self.subsec
            bits.append(t)
        return " · ".join(bits)


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip("\x00").strip()
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).strip()


def _gps_string(gps: Dict[str, Any]) -> str:
    def dms(value) -> Optional[float]:
        if isinstance(value, (list, tuple)) and len(value) == 3:
            try:
                d, m, s = (float(x) for x in value)
                return d + m / 60 + s / 3600
            except (TypeError, ValueError):
                return None
        return None

    lat = dms(gps.get("GPSLatitude"))
    lon = dms(gps.get("GPSLongitude"))
    if lat is None or lon is None:
        return ""
    if _s(gps.get("GPSLatitudeRef")).upper().startswith("S"):
        lat = -lat
    if _s(gps.get("GPSLongitudeRef")).upper().startswith("W"):
        lon = -lon
    return f"{lat:.6f},{lon:.6f}"


def fingerprint(exif: Dict[str, Any]) -> ExifFingerprint:
    if not exif:
        return ExifFingerprint()
    taken = _s(exif.get("DateTimeOriginal")) or _s(exif.get("DateTimeDigitized")) or _s(exif.get("DateTime"))
    m = _DATE_RE.match(taken)
    if m:
        taken = "{}:{}:{} {}:{}:{}".format(*m.groups())
    else:
        taken = ""
    subsec = _s(exif.get("SubSecTimeOriginal")) or _s(exif.get("SubSecTime"))
    subsec = subsec.strip().rstrip("\x00")
    fp = ExifFingerprint(
        taken_at=taken,
        subsec=subsec,
        make=_s(exif.get("Make")),
        model=_s(exif.get("Model")),
        exposure=_s(exif.get("ExposureTime")),
        fnumber=_s(exif.get("FNumber")),
        iso=_s(exif.get("ISOSpeedRatings")),
        focal=_s(exif.get("FocalLength")),
        lens=_s(exif.get("LensModel")),
        serial=_s(exif.get("BodySerialNumber")),
        unique_id=_s(exif.get("ImageUniqueID")),
        gps=_gps_string(exif.get("GPS") or {}),
        raw=exif,
    )
    for key, attr in (("PixelXDimension", "width"), ("PixelYDimension", "height"),
                      ("ImageWidth", "width"), ("ImageLength", "height")):
        val = exif.get(key)
        if isinstance(val, (int, float)) and not getattr(fp, attr):
            setattr(fp, attr, int(val))
    return fp


def fingerprint_file(path) -> ExifFingerprint:
    return fingerprint(read_exif(path))


def fingerprint_bytes(data: bytes) -> ExifFingerprint:
    return fingerprint(read_exif_from_bytes(data))

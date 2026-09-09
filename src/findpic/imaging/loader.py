"""이미지를 되도록 빨리, 되도록 많은 형식에서 연다."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageOps

# 사진 수만 장을 훑을 때 부분 손상 파일에서 멈추지 않도록
Image.LOAD_TRUNCATED_IMAGES = True
# 한글 보고서용 사진은 아무리 커도 2억 화소를 넘지 않는다
Image.MAX_IMAGE_PIXELS = 300_000_000

_HEIF_READY: Optional[bool] = None


def _ensure_heif() -> bool:
    global _HEIF_READY
    if _HEIF_READY is None:
        try:
            import pillow_heif  # type: ignore

            pillow_heif.register_heif_opener()
            _HEIF_READY = True
        except Exception:
            _HEIF_READY = False
    return _HEIF_READY


def open_image(source, *, target: int = 256, mode: Optional[str] = None) -> Optional[Image.Image]:
    """파일 경로나 바이트에서 이미지를 연다.

    target 은 '이 정도 크기면 충분하다'는 힌트다. JPEG 은 이 힌트를 이용해
    디코딩 자체를 축소해서 하기 때문에 수십 배 빨라진다.

    mode 는 기본값 None 이다. 여기에 'L' 을 주면 디코딩 단계에서 흑백으로
    바꿔 버려 색 정보가 사라지므로, 색을 봐야 하는 곳에서는 절대 주지 않는다.
    """
    try:
        if isinstance(source, (bytes, bytearray)):
            fh = io.BytesIO(source)
        else:
            path = Path(source)
            if path.suffix.lower() in (".heic", ".heif"):
                _ensure_heif()
            fh = open(path, "rb")
        try:
            im = Image.open(fh)
            try:
                im.draft(mode, (target, target))
            except Exception:
                pass
            im.load()
        finally:
            if not isinstance(source, (bytes, bytearray)):
                fh.close()
    except Exception:
        return None
    try:
        im = ImageOps.exif_transpose(im) or im
    except Exception:
        pass
    return im


def real_size(source) -> Optional[Tuple[int, int]]:
    """픽셀을 읽지 않고 가로·세로만."""
    try:
        if isinstance(source, (bytes, bytearray)):
            with Image.open(io.BytesIO(source)) as im:
                return im.size
        with Image.open(source) as im:
            return im.size
    except Exception:
        return None

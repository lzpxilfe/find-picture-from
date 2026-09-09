"""사진의 '겉모습 지문'.

크기를 줄이고 다시 압축한 사진과 원본을 이어 붙이려면, 화소를 그대로 비교할
수는 없다. 대신 아주 작게 줄인 뒤의 밝기·색 배치를 비교한다. 이 배치는
축소·재압축·밝기 보정에도 거의 변하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
from PIL import Image

GRAY_SIZE = 32          # 구조 비교용 흑백 축소판
TILE_SIZE = 8           # 색 배치 비교용 격자
DHASH_SIZE = 16         # 16x16 -> 256비트
PHASH_SIZE = 32         # 32x32 DCT 에서 8x8 만 사용

_DCT_MATRIX: Optional[np.ndarray] = None


def _dct_matrix(n: int) -> np.ndarray:
    global _DCT_MATRIX
    if _DCT_MATRIX is None or _DCT_MATRIX.shape[0] != n:
        k = np.arange(n).reshape(-1, 1)
        i = np.arange(n).reshape(1, -1)
        m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
        m[0] *= np.sqrt(1 / n)
        m[1:] *= np.sqrt(2 / n)
        _DCT_MATRIX = m
    return _DCT_MATRIX


@dataclass
class VisualFingerprint:
    width: int = 0
    height: int = 0
    gray: bytes = b""       # GRAY_SIZE^2 개의 uint8
    tiles: bytes = b""      # TILE_SIZE^2 * 3 개의 uint8 (RGB)
    dhash: bytes = b""      # 32바이트
    phash: bytes = b""      # 8바이트

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def ok(self) -> bool:
        return bool(self.gray) and bool(self.tiles)

    def gray_array(self) -> np.ndarray:
        return np.frombuffer(self.gray, dtype=np.uint8).reshape(GRAY_SIZE, GRAY_SIZE).astype(np.float32)

    def tile_array(self) -> np.ndarray:
        return np.frombuffer(self.tiles, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE, 3).astype(np.float32)


def _bits_to_bytes(bits: np.ndarray) -> bytes:
    return np.packbits(bits.astype(np.uint8).ravel()).tobytes()


def compute(image: Image.Image, *, true_size=None) -> VisualFingerprint:
    """지문을 만든다.

    true_size 를 주면 그 값을 크기로 기록한다. 빠르게 읽으려고 축소 디코딩한
    그림을 넘길 때, 지문은 그 그림으로 만들되 크기는 원래 값을 남기기 위한 것이다.
    (종횡비는 축소해도 유지되므로 비교 자체에는 영향이 없다.)
    """
    if image is None:
        return VisualFingerprint()
    w, h = true_size or image.size
    try:
        rgb = image.convert("RGB")
        gray_im = rgb.convert("L")
        gray = np.asarray(gray_im.resize((GRAY_SIZE, GRAY_SIZE), Image.BILINEAR), dtype=np.uint8)
        tiles = np.asarray(rgb.resize((TILE_SIZE, TILE_SIZE), Image.BILINEAR), dtype=np.uint8)
        d_src = np.asarray(gray_im.resize((DHASH_SIZE + 1, DHASH_SIZE), Image.BILINEAR), dtype=np.int16)
        dbits = d_src[:, 1:] > d_src[:, :-1]
        p_src = np.asarray(gray_im.resize((PHASH_SIZE, PHASH_SIZE), Image.BILINEAR), dtype=np.float32)
        m = _dct_matrix(PHASH_SIZE)
        coeffs = m @ p_src @ m.T
        block = coeffs[:8, :8].copy()
        block[0, 0] = 0.0
        pbits = block > np.median(block)
    except Exception:
        return VisualFingerprint(width=w, height=h)
    return VisualFingerprint(
        width=w, height=h,
        gray=gray.tobytes(), tiles=tiles.tobytes(),
        dhash=_bits_to_bytes(dbits), phash=_bits_to_bytes(pbits),
    )


def variants(image: Image.Image, *, true_size=None) -> List[tuple]:
    """돌리거나 뒤집은 판본들의 지문. (설명, 지문) 목록.

    보고서에 넣으면서 사진을 돌려 넣은 경우까지 잡기 위해 **질의 쪽에만**
    적용한다. 원본 수만 장에 대해 전부 만들면 낭비다.
    """
    turned = (true_size[1], true_size[0]) if true_size else None
    out = [("그대로", compute(image, true_size=true_size))]
    try:
        out.append(("90도 회전", compute(image.transpose(Image.ROTATE_90), true_size=turned)))
        out.append(("180도 회전", compute(image.transpose(Image.ROTATE_180), true_size=true_size)))
        out.append(("270도 회전", compute(image.transpose(Image.ROTATE_270), true_size=turned)))
        out.append(("좌우 반전", compute(image.transpose(Image.FLIP_LEFT_RIGHT), true_size=true_size)))
    except Exception:
        pass
    return [(name, fp) for name, fp in out if fp.ok]


# --- 거리 계산 --------------------------------------------------------------

_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def hamming(a: bytes, b: bytes) -> int:
    if not a or not b or len(a) != len(b):
        return 10 ** 6
    xor = np.frombuffer(a, dtype=np.uint8) ^ np.frombuffer(b, dtype=np.uint8)
    return int(_POPCOUNT[xor].sum())


def ncc(a: np.ndarray, b: np.ndarray) -> float:
    """평균을 뺀 정규화 상관. 1.0 이면 똑같은 구조, 0 이면 무관."""
    x = a.ravel() - a.mean()
    y = b.ravel() - b.mean()
    nx = np.linalg.norm(x)
    ny = np.linalg.norm(y)
    if nx < 1e-6 or ny < 1e-6:
        return 0.0
    return float(np.dot(x, y) / (nx * ny))


def tile_distance(a: np.ndarray, b: np.ndarray) -> float:
    """격자 평균색의 평균 절대 오차 (0-255). 작을수록 비슷하다."""
    return float(np.abs(a - b).mean())


def chroma(tiles: np.ndarray) -> float:
    """색이 얼마나 들어 있는가 (0 이면 완전한 흑백).

    격자마다 RGB 최댓값과 최솟값의 차를 평균낸다. 원본을 흑백으로 바꿔 둔
    파일은 밝기 배치가 원본과 완전히 같아서 다른 지표로는 걸러지지 않는다.
    '색이 있느냐 없느냐' 는 그 경우를 가르는 거의 유일한 단서다.
    """
    return float((tiles.max(axis=2) - tiles.min(axis=2)).mean())


def aspect_gap(a: float, b: float) -> float:
    """종횡비 차이. 0 이면 같고, 0.05 면 5% 어긋난 것."""
    if a <= 0 or b <= 0:
        return 1.0
    return abs(np.log(a / b))

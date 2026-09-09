"""HWP 5.x 레코드 스트림 훑기.

레코드 하나의 머리 4바이트는 비트로 쪼개져 있다.
    bit  0-9  : 태그 번호
    bit 10-19 : 계층(level)
    bit 20-31 : 자료 크기. 0xFFF 이면 뒤이어 오는 4바이트가 진짜 크기다.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, List


@dataclass
class Record:
    tag: int
    level: int
    payload: bytes
    offset: int          # 스트림 안에서 payload 가 시작하는 위치 (디버깅용)

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의
        from . import tags as T
        return f"<Record {T.NAMES.get(self.tag, self.tag)} lvl={self.level} len={len(self.payload)}>"


def iter_records(buf: bytes, *, strict: bool = False) -> Iterator[Record]:
    """바이트열에서 레코드를 순서대로 꺼낸다.

    깨진 문서에서도 최대한 건져야 하므로, 길이가 이상하면 조용히 멈춘다.
    strict=True 면 예외를 낸다.
    """
    pos = 0
    n = len(buf)
    while pos + 4 <= n:
        (head,) = struct.unpack_from("<I", buf, pos)
        tag = head & 0x3FF
        level = (head >> 10) & 0x3FF
        size = (head >> 20) & 0xFFF
        pos += 4
        if size == 0xFFF:
            if pos + 4 > n:
                if strict:
                    raise ValueError("확장 길이 필드가 잘렸습니다")
                return
            (size,) = struct.unpack_from("<I", buf, pos)
            pos += 4
        if pos + size > n:
            if strict:
                raise ValueError(f"레코드 길이가 스트림을 벗어납니다 (tag={tag}, size={size})")
            # 남은 만큼이라도 넘겨준다
            yield Record(tag, level, buf[pos:n], pos)
            return
        yield Record(tag, level, buf[pos:pos + size], pos)
        pos += size


def read_records(buf: bytes, *, strict: bool = False) -> List[Record]:
    return list(iter_records(buf, strict=strict))


class Reader:
    """레코드 payload 를 앞에서부터 조금씩 떼어 읽는 도우미.

    스펙에 없는 뒷부분이 붙어 있거나(버전 차이) 잘려 있어도 죽지 않도록,
    범위를 벗어나면 0/빈 값을 돌려준다.
    """

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    @property
    def remaining(self) -> int:
        return max(0, len(self.buf) - self.pos)

    def skip(self, n: int) -> "Reader":
        self.pos += n
        return self

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            self.pos = len(self.buf)
            return b""
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        b = self._take(1)
        return b[0] if b else 0

    def i8(self) -> int:
        b = self._take(1)
        return struct.unpack("<b", b)[0] if b else 0

    def u16(self) -> int:
        b = self._take(2)
        return struct.unpack("<H", b)[0] if b else 0

    def i16(self) -> int:
        b = self._take(2)
        return struct.unpack("<h", b)[0] if b else 0

    def u32(self) -> int:
        b = self._take(4)
        return struct.unpack("<I", b)[0] if b else 0

    def i32(self) -> int:
        b = self._take(4)
        return struct.unpack("<i", b)[0] if b else 0

    def bytes(self, n: int) -> bytes:
        return self._take(n)

    def wchar_str(self) -> str:
        """UINT16 길이 + UTF-16LE 문자열."""
        ln = self.u16()
        if ln == 0:
            return ""
        raw = self._take(ln * 2)
        return raw.decode("utf-16le", "replace")

    def signature(self) -> str:
        """4바이트 컨트롤 ID. 파일에는 뒤집혀 들어 있어 되돌린다."""
        raw = self._take(4)
        if len(raw) < 4:
            return ""
        return raw[::-1].decode("latin-1")

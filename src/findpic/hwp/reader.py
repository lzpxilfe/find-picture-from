"""HWP 5.x 컨테이너(OLE 복합 문서) 열기와 스트림 압축 해제."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import olefile

HWP5_SIGNATURE = b"HWP Document File"


class HwpError(Exception):
    """한글 파일을 읽을 수 없을 때."""


class EncryptedHwpError(HwpError):
    pass


@dataclass
class FileHeader:
    version: tuple            # (5, 1, 0, 1)
    compressed: bool
    password: bool
    distribution: bool
    raw_properties: int

    @property
    def version_str(self) -> str:
        return ".".join(str(x) for x in self.version)


def _inflate(data: bytes) -> bytes:
    """한글이 쓰는 raw deflate. 실패하면 zlib 헤더가 붙은 경우도 시도한다."""
    for wbits in (-15, 15, 47):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    # 끝이 잘린 스트림이라도 앞부분은 건진다
    for wbits in (-15, 15):
        try:
            obj = zlib.decompressobj(wbits)
            out = obj.decompress(data)
            if out:
                return out
        except zlib.error:
            continue
    raise HwpError("압축을 풀 수 없는 스트림입니다")


class Hwp5File:
    """.hwp 파일 하나. with 문으로 쓰거나 close() 를 부른다."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.ole = olefile.OleFileIO(str(self.path))
        except Exception as exc:  # olefile 이 내는 예외 종류가 다양하다
            raise HwpError(f"OLE 복합 문서로 열 수 없습니다: {exc}") from exc
        self._streams = {"/".join(p): p for p in self.ole.listdir(streams=True)}
        self.header = self._read_header()
        if self.header.password:
            raise EncryptedHwpError("암호가 걸린 한글 파일입니다")

    # -- 컨테이너 -------------------------------------------------------
    def close(self) -> None:
        try:
            self.ole.close()
        except Exception:
            pass

    def __enter__(self) -> "Hwp5File":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def has(self, name: str) -> bool:
        return name in self._streams

    def raw(self, name: str) -> bytes:
        if name not in self._streams:
            raise HwpError(f"스트림이 없습니다: {name}")
        with self.ole.openstream(self._streams[name]) as fh:
            return fh.read()

    def stream(self, name: str) -> bytes:
        """문서 압축 설정을 따라 필요하면 압축을 풀어 돌려준다."""
        data = self.raw(name)
        if self.header.compressed:
            return _inflate(data)
        return data

    def stream_names(self) -> List[str]:
        return list(self._streams)

    # -- 파일 머리 ------------------------------------------------------
    def _read_header(self) -> FileHeader:
        if "FileHeader" not in self._streams:
            raise HwpError("FileHeader 스트림이 없습니다. 한글 5.x 파일이 아닌 것 같습니다")
        fh = self.raw("FileHeader")
        if not fh.startswith(HWP5_SIGNATURE):
            raise HwpError("한글 5.x 서명이 맞지 않습니다")
        (ver,) = struct.unpack_from("<I", fh, 32)
        version = ((ver >> 24) & 0xFF, (ver >> 16) & 0xFF, (ver >> 8) & 0xFF, ver & 0xFF)
        (prop,) = struct.unpack_from("<I", fh, 36)
        return FileHeader(
            version=version,
            compressed=bool(prop & 0x01),
            password=bool(prop & 0x02),
            distribution=bool(prop & 0x04),
            raw_properties=prop,
        )

    # -- 본문 -----------------------------------------------------------
    def section_names(self) -> List[str]:
        """본문 섹션 스트림 이름을 순서대로.

        배포용 문서는 BodyText 대신 ViewText 에 (따로 암호화된) 내용이 들어간다.
        """
        prefix = "BodyText/"
        names = [n for n in self._streams if n.startswith(prefix)]
        if not names and any(n.startswith("ViewText/") for n in self._streams):
            prefix = "ViewText/"
            names = [n for n in self._streams if n.startswith(prefix)]

        def order(name: str) -> int:
            tail = name[len(prefix):]
            digits = "".join(ch for ch in tail if ch.isdigit())
            return int(digits) if digits else 0

        return sorted(names, key=order)

    # -- 첨부된 이진 자료 ------------------------------------------------
    def bindata_streams(self) -> Dict[str, str]:
        """'BIN0001.JPG' 처럼 확장자를 뺀 이름(대문자) -> 실제 스트림 이름."""
        out = {}
        for name in self._streams:
            if not name.startswith("BinData/"):
                continue
            tail = name.split("/", 1)[1]
            stem = tail.rsplit(".", 1)[0].upper()
            out[stem] = name
        return out

    def find_bindata(self, bin_id: int) -> Optional[str]:
        """BinData 번호로 스트림 이름 찾기.

        스펙상 이름은 'BIN' + 4자리 대문자 16진수지만, 만든 프로그램에 따라
        10진수로 적힌 것도 있어 둘 다 본다.
        """
        table = self.bindata_streams()
        for stem in (f"BIN{bin_id:04X}", f"BIN{bin_id:04d}", f"BIN{bin_id:X}", f"BIN{bin_id:d}"):
            if stem in table:
                return table[stem]
        return None

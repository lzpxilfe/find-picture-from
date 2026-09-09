"""원본 사진 폴더를 훑어 색인을 만든다.

사진이 수만 장이면 매번 전부 다시 읽을 수 없다. 그래서 파일별로
'크기 + 수정시각' 이 그대로면 지난번 계산 결과를 재사용한다.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np

from .imaging import exif as EX
from .imaging import fingerprint as FP
from .imaging.loader import open_image, real_size

SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS photos (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    width     INTEGER,
    height    INTEGER,
    taken_at  TEXT,
    subsec    TEXT,
    model     TEXT,
    make      TEXT,
    settings  TEXT,
    gps       TEXT,
    unique_id TEXT,
    head_sha1 TEXT,
    thumbed   INTEGER,
    gray      BLOB,
    tiles     BLOB,
    dhash     BLOB,
    phash     BLOB,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS photos_taken ON photos(taken_at);
CREATE INDEX IF NOT EXISTS photos_uid ON photos(unique_id);
"""


@dataclass
class PhotoRecord:
    path: str
    size: int = 0
    mtime: float = 0.0
    width: int = 0
    height: int = 0
    taken_at: str = ""
    subsec: str = ""
    model: str = ""
    make: str = ""
    settings: str = ""
    gps: str = ""
    unique_id: str = ""
    head_sha1: str = ""
    thumbed: int = 0            # EXIF 축소판으로 지문을 만들었는가
    gray: bytes = b""
    tiles: bytes = b""
    dhash: bytes = b""
    phash: bytes = b""
    error: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def has_visual(self) -> bool:
        return bool(self.gray) and bool(self.tiles)

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1_000_000

    def exif_keys(self) -> List[tuple]:
        out = []
        if self.unique_id:
            out.append(("사진 고유 ID", self.unique_id))
        if self.taken_at and self.model:
            if self.subsec:
                out.append(("촬영시각+1/100초+기종", f"{self.taken_at}|{self.subsec}|{self.model}"))
            out.append(("촬영시각+기종", f"{self.taken_at}|{self.model}"))
        if self.taken_at:
            out.append(("촬영시각", self.taken_at))
        return out

    def gray_array(self) -> np.ndarray:
        return np.frombuffer(self.gray, dtype=np.uint8).reshape(FP.GRAY_SIZE, FP.GRAY_SIZE).astype(np.float32)

    def tile_array(self) -> np.ndarray:
        return np.frombuffer(self.tiles, dtype=np.uint8).reshape(FP.TILE_SIZE, FP.TILE_SIZE, 3).astype(np.float32)


def iter_photo_files(roots: Sequence, *, follow_links: bool = False) -> Iterable[Path]:
    """사진 파일만 골라 재귀적으로 훑는다. 숨김/시스템 폴더는 건너뛴다."""
    skip_dirs = {".git", "__pycache__", "$RECYCLE.BIN", "System Volume Information",
                 ".Trash", ".Trashes", "node_modules"}
    for root in roots:
        root = Path(root)
        if root.is_file():
            if root.suffix.lower() in EX.IMAGE_EXTENSIONS:
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_links):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                if Path(fn).suffix.lower() in EX.IMAGE_EXTENSIONS:
                    yield Path(dirpath) / fn


def _head_sha1(path: Path, size: int) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return ""
    h = hashlib.sha1()
    h.update(str(size).encode())
    h.update(head)
    return h.hexdigest()


# EXIF 축소판의 가로세로 비율이 본체와 이만큼 넘게 다르면 믿지 않는다
_THUMB_ASPECT_TOLERANCE = 0.03


def analyse(path: Path, *, fast: bool = True) -> PhotoRecord:
    """사진 한 장에서 EXIF 와 시각 지문을 뽑는다.

    fast 면 사진기가 넣어 둔 EXIF 축소판을 먼저 본다. 파일 앞부분만 읽으면
    되므로 큰 사진 수만 장을 훑을 때 훨씬 빠르다. 축소판이 없거나 본체와
    가로세로 비율이 어긋나면(잘라 편집한 사진 등) 파일 전체를 읽는다.
    """
    try:
        st = path.stat()
    except OSError as exc:
        return PhotoRecord(path=str(path), error=f"열 수 없음: {exc}")
    rec = PhotoRecord(path=str(path), size=st.st_size, mtime=st.st_mtime)
    rec.head_sha1 = _head_sha1(path, st.st_size)

    fp = EX.fingerprint_file(path)
    rec.taken_at, rec.subsec = fp.taken_at, fp.subsec
    rec.model, rec.make = fp.model, fp.make
    rec.settings, rec.gps, rec.unique_id = fp.settings, fp.gps, fp.unique_id

    image = None
    if fast and fp.width and fp.height:
        thumb = EX.read_thumbnail(path)
        if thumb:
            candidate = open_image(thumb, target=FP.GRAY_SIZE * 2)
            if candidate is not None and candidate.height:
                gap = FP.aspect_gap(candidate.width / candidate.height, fp.width / fp.height)
                if gap <= _THUMB_ASPECT_TOLERANCE:
                    image, rec.thumbed = candidate, 1
                else:
                    try:
                        candidate.close()
                    except Exception:
                        pass
    if image is None:
        image = open_image(path, target=FP.GRAY_SIZE * 2)
    if image is None:
        rec.error = "이미지를 열지 못했습니다"
        if fp.width and fp.height:
            rec.width, rec.height = fp.width, fp.height
        return rec
    visual = FP.compute(image)
    try:
        image.close()
    except Exception:
        pass
    # 크기는 반드시 '진짜' 값을 쓴다.
    # 지문을 만들 때 쓴 그림은 빠르게 읽으려고 축소 디코딩한 것이라,
    # 그 크기를 그대로 쓰면 6000x4000 사진이 750x500 으로 기록된다.
    size = real_size(path)
    if size:
        rec.width, rec.height = size
    elif fp.width and fp.height:
        rec.width, rec.height = fp.width, fp.height
    else:
        rec.width, rec.height = visual.width, visual.height
    rec.gray, rec.tiles = visual.gray, visual.tiles
    rec.dhash, rec.phash = visual.dhash, visual.phash
    return rec


class PhotoIndex:
    """SQLite 에 저장되는 원본 사진 색인."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as con:
            con.executescript(_SCHEMA)
            row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                con.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            elif row[0] != str(SCHEMA_VERSION):
                con.executescript("DROP TABLE photos;" + _SCHEMA)
                con.execute("REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    def _connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(str(self.db_path), timeout=60)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            self._local.con = con
        return con

    # -- 만들기 ---------------------------------------------------------
    def refresh(self, roots: Sequence, *,
                workers: int = 8,
                fast: bool = True,
                progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, int]:
        files = list(iter_photo_files(roots))
        total = len(files)
        con = self._connect()
        known = {
            row[0]: (row[1], row[2])
            for row in con.execute("SELECT path, size, mtime FROM photos")
        }
        todo = []
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            cached = known.get(str(path))
            if cached and cached[0] == st.st_size and abs(cached[1] - st.st_mtime) < 1e-6:
                continue
            todo.append(path)

        stats = {"전체": total, "새로 읽음": 0, "재사용": total - len(todo),
                 "실패": 0, "축소판 사용": 0}

        # 지워진 파일은 먼저 색인에서 뺀다. 새로 읽을 게 없어도 이건 해야 한다.
        current = {str(p) for p in files}
        gone = [p for p in known if p not in current]
        if gone:
            con.executemany("DELETE FROM photos WHERE path=?", ((p,) for p in gone))
            con.commit()

        if not todo:
            if progress:
                progress(total, total, "")
            return stats

        done = stats["재사용"]
        batch: List[PhotoRecord] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for rec in pool.map(lambda p: analyse(p, fast=fast), todo):
                batch.append(rec)
                done += 1
                stats["새로 읽음"] += 1
                if rec.thumbed:
                    stats["축소판 사용"] += 1
                if rec.error:
                    stats["실패"] += 1
                if progress and done % 25 == 0:
                    progress(done, total, rec.name)
                if len(batch) >= 200:
                    self._store(batch)
                    batch.clear()
        if batch:
            self._store(batch)
        if progress:
            progress(total, total, "")
        return stats

    def _store(self, records: Sequence[PhotoRecord]) -> None:
        con = self._connect()
        con.executemany(
            """REPLACE INTO photos
               (path,size,mtime,width,height,taken_at,subsec,model,make,settings,gps,
                unique_id,head_sha1,thumbed,gray,tiles,dhash,phash,error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(r.path, r.size, r.mtime, r.width, r.height, r.taken_at, r.subsec, r.model,
              r.make, r.settings, r.gps, r.unique_id, r.head_sha1, r.thumbed, r.gray,
              r.tiles, r.dhash, r.phash, r.error) for r in records],
        )
        con.commit()

    # -- 읽기 -----------------------------------------------------------
    def count(self) -> int:
        return self._connect().execute("SELECT COUNT(*) FROM photos").fetchone()[0]

    def load_all(self, *, roots: Optional[Sequence] = None) -> List[PhotoRecord]:
        con = self._connect()
        rows = con.execute(
            """SELECT path,size,mtime,width,height,taken_at,subsec,model,make,settings,gps,
                      unique_id,head_sha1,thumbed,gray,tiles,dhash,phash,error FROM photos"""
        )
        prefixes = [str(Path(r).resolve()) for r in roots] if roots else None
        out = []
        for row in rows:
            rec = PhotoRecord(*row)
            if prefixes:
                rp = str(Path(rec.path).resolve())
                if not any(rp == p or rp.startswith(p + os.sep) for p in prefixes):
                    continue
            out.append(rec)
        return out

"""축소된 사진 <-> 원본 사진 잇기.

두 갈래로 판단한다.

1. EXIF 지문 — 사진기가 찍을 때 넣어 둔 촬영 시각(1/100초까지), 기종, 노출값.
   한글에 넣느라 크기를 줄여도 이 값들은 대개 그대로 살아남는다. 이게 맞으면
   사실상 같은 사진이다.
2. 겉모습 지문 — EXIF 가 지워진 사진(지도·도면 등)을 위해, 아주 작게 줄인
   밝기·색 배치를 비교한다.

둘이 어긋나면 사람에게 넘긴다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .imaging import fingerprint as FP
from .imaging.exif import ExifFingerprint
from .imaging.loader import open_image
from .index import PhotoRecord

# --- 판정 기준 ---------------------------------------------------------------
# 겉모습 점수(0-1)가 이보다 높고 2등과의 차이가 충분하면 자동으로 확정한다
SCORE_CERTAIN = 0.86
SCORE_REVIEW = 0.62
MARGIN_CERTAIN = 0.05
# 종횡비가 이보다 어긋나면 '자른 사진'으로 보고 감점한다
ASPECT_TOLERANCE = 0.04
# 1차로 추려낼 후보 수
SHORTLIST = 400
# 1·2등이 이만큼 붙어 있으면 원본 화소를 더 크게 읽어 다시 비교한다
REFINE_MARGIN = 0.04
REFINE_SIZE = 256
# 국소 차이를 볼 때 화면을 몇 칸으로 자를지, 그리고 감점 환산 계수
REFINE_LOCAL_GRID = 32
REFINE_LOCAL_SCALE = 120.0

# 붙여넣기로 들어간 그림은 'CLP00001d1c371e.bmp' 같은 임시 이름을 남긴다. 단서가 못 된다.
_MEANINGLESS_NAME = re.compile(r"^(CLP[0-9a-f]{8,}|image\d*|clip(board)?_?\d*|무제|untitled)$", re.I)

CERTAIN = "확실"
REVIEW = "확인필요"
NOT_FOUND = "못찾음"


@dataclass
class Candidate:
    record: PhotoRecord
    score: float = 0.0
    exif_key: str = ""          # EXIF 로 맞은 열쇠 이름 (없으면 빈 문자열)
    ncc: float = 0.0
    dhash_distance: int = 10 ** 6
    phash_distance: int = 10 ** 6
    tile_distance: float = 255.0
    aspect_gap: float = 1.0
    orientation: str = "그대로"
    name_match: bool = False
    refined: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        bits = []
        if self.exif_key:
            bits.append(f"EXIF {self.exif_key} 일치")
        if self.name_match:
            bits.append("파일 이름 일치")
        bits.append(f"겉모습 {self.score * 100:.1f}점")
        if self.orientation != "그대로":
            bits.append(self.orientation)
        if self.refined:
            bits.append("원본 화소 재확인")
        return " · ".join(bits)


@dataclass
class SlotMatch:
    verdict: str = NOT_FOUND
    best: Optional[Candidate] = None
    runners_up: List[Candidate] = field(default_factory=list)
    message: str = ""

    @property
    def path(self) -> Optional[str]:
        return self.best.record.path if self.best else None


@dataclass
class Query:
    """한글 파일에서 꺼낸 사진 한 장을 비교 가능한 형태로 만든 것."""

    exif: ExifFingerprint
    variants: List[Tuple[str, FP.VisualFingerprint]] = field(default_factory=list)
    data: bytes = b""
    hint_name: str = ""          # 한글이 적어 둔 원래 파일 이름
    hint_width: int = 0
    hint_height: int = 0

    @property
    def primary(self) -> Optional[FP.VisualFingerprint]:
        return self.variants[0][1] if self.variants else None

    @property
    def useful_hint_name(self) -> str:
        if not self.hint_name:
            return ""
        stem = Path(self.hint_name).stem
        return "" if _MEANINGLESS_NAME.match(stem) else self.hint_name


def build_query(data: bytes, hint=None) -> Query:
    from .imaging.exif import fingerprint_bytes

    image = open_image(data, target=FP.GRAY_SIZE * 2)
    variants = FP.variants(image) if image is not None else []
    if image is not None:
        try:
            image.close()
        except Exception:
            pass
    query = Query(exif=fingerprint_bytes(data), variants=variants, data=data)
    if hint is not None:
        query.hint_name = getattr(hint, "original_name", "") or ""
        query.hint_width = getattr(hint, "width", 0) or 0
        query.hint_height = getattr(hint, "height", 0) or 0
    return query


def _norm_name(name: str) -> str:
    return unicodedata.normalize("NFC", Path(name).name).casefold()


class Matcher:
    """색인을 한 번 메모리에 올려 두고 여러 사진을 이어 붙인다."""

    def __init__(self, records: Sequence[PhotoRecord]):
        self.records = [r for r in records if not r.error or r.taken_at]
        self.visual = [r for r in self.records if r.has_visual]
        self._dhash = self._stack([r.dhash for r in self.visual], 32)
        self._exif_map: Dict[str, List[PhotoRecord]] = {}
        self._name_map: Dict[str, List[PhotoRecord]] = {}
        for rec in self.records:
            for _name, key in rec.exif_keys():
                self._exif_map.setdefault(key, []).append(rec)
            self._name_map.setdefault(_norm_name(rec.path), []).append(rec)

    def name_candidates(self, query: Query) -> List[PhotoRecord]:
        """한글이 적어 둔 원래 파일 이름과 같은 이름의 원본들."""
        name = query.useful_hint_name
        if not name:
            return []
        hits = list(self._name_map.get(_norm_name(name), []))
        if hits:
            return hits
        # 확장자만 바뀐 경우 (JPG 원본을 PNG 로 넣었다든지)
        stem = _norm_name(Path(name).stem)
        return [r for key, group in self._name_map.items()
                if _norm_name(Path(key).stem) == stem for r in group]

    @staticmethod
    def _stack(blobs: Sequence[bytes], width: int) -> np.ndarray:
        if not blobs:
            return np.zeros((0, width), dtype=np.uint8)
        out = np.zeros((len(blobs), width), dtype=np.uint8)
        for i, b in enumerate(blobs):
            if len(b) == width:
                out[i] = np.frombuffer(b, dtype=np.uint8)
        return out

    # -- 1단계: EXIF ------------------------------------------------------
    def exif_candidates(self, query: Query) -> List[Tuple[str, PhotoRecord]]:
        seen = set()
        out = []
        for name, key in query.exif.keys:
            for rec in self._exif_map.get(key, []):
                if rec.path in seen:
                    continue
                seen.add(rec.path)
                out.append((name, rec))
            if out:
                break        # 가장 강한 열쇠에서 걸리면 더 약한 건 볼 필요가 없다
        return out

    # -- 2단계: 겉모습 ----------------------------------------------------
    def _shortlist(self, query: Query, limit: int) -> List[Tuple[int, str, int]]:
        """dhash 로 후보를 빠르게 추린다. (색인 위치, 방향, dhash 거리)"""
        if not len(self._dhash):
            return []
        best_dist = np.full(len(self._dhash), 10 ** 6, dtype=np.int32)
        best_kind = np.zeros(len(self._dhash), dtype=np.int8)
        lut = FP._POPCOUNT
        for vi, (name, fp) in enumerate(query.variants):
            if len(fp.dhash) != 32:
                continue
            q = np.frombuffer(fp.dhash, dtype=np.uint8)
            dist = lut[self._dhash ^ q].sum(axis=1).astype(np.int32)
            better = dist < best_dist
            best_dist[better] = dist[better]
            best_kind[better] = vi
        order = np.argsort(best_dist, kind="stable")[:limit]
        names = [n for n, _ in query.variants]
        return [(int(i), names[best_kind[i]], int(best_dist[i])) for i in order]

    def _score(self, query: Query, rec: PhotoRecord, orientation: str) -> Candidate:
        fp = dict(query.variants).get(orientation) or query.primary
        cand = Candidate(record=rec, orientation=orientation)
        if fp is None or not rec.has_visual:
            return cand
        cand.dhash_distance = FP.hamming(fp.dhash, rec.dhash)
        cand.phash_distance = FP.hamming(fp.phash, rec.phash)
        cand.ncc = FP.ncc(fp.gray_array(), rec.gray_array())
        cand.tile_distance = FP.tile_distance(fp.tile_array(), rec.tile_array())
        cand.aspect_gap = FP.aspect_gap(fp.aspect, rec.aspect)

        ncc_part = max(0.0, cand.ncc)
        dhash_part = max(0.0, 1.0 - cand.dhash_distance / 256.0)
        phash_part = max(0.0, 1.0 - cand.phash_distance / 64.0)
        tile_part = max(0.0, 1.0 - cand.tile_distance / 80.0)
        score = 0.40 * ncc_part + 0.20 * dhash_part + 0.10 * phash_part + 0.30 * tile_part

        if query.hint_width and query.hint_height and rec.width and rec.height:
            if (rec.width, rec.height) == (query.hint_width, query.hint_height):
                score = min(1.0, score + 0.06)
                cand.notes.append("한글에 적힌 원래 화소 크기와 같음")

        if cand.aspect_gap > ASPECT_TOLERANCE:
            # 잘라 넣은 사진일 수 있으니 버리지는 않고 깎기만 한다
            penalty = min(0.25, (cand.aspect_gap - ASPECT_TOLERANCE) * 1.2)
            score -= penalty
            cand.notes.append(f"가로세로 비율이 {cand.aspect_gap:.2f} 만큼 다름")
        cand.score = max(0.0, min(1.0, score))
        return cand

    # -- 3단계: 접전이면 원본 화소로 재확인 --------------------------------
    @staticmethod
    def _fine_array(source):
        image = open_image(source, target=REFINE_SIZE)
        if image is None:
            return None
        try:
            from PIL import Image as _Image
            return np.asarray(
                image.convert("RGB").resize((REFINE_SIZE, REFINE_SIZE), _Image.BILINEAR),
                dtype=np.float32,
            )
        except Exception:
            return None
        finally:
            try:
                image.close()
            except Exception:
                pass

    @staticmethod
    def _local_peak(a: np.ndarray, b: np.ndarray) -> float:
        """국소 차이의 최댓값.

        거의 똑같아 보이는 두 도면을 가르는 건 전체 평균이 아니라 '한 군데가
        확 다른가' 이다. 격자로 잘라 칸별 평균 오차를 구한 뒤 가장 큰 칸을 본다.
        """
        grid = REFINE_LOCAL_GRID
        side = a.shape[0] // grid
        if side < 1:
            return 0.0
        diff = np.abs(a - b).mean(axis=2)
        tiles = diff[:side * grid, :side * grid].reshape(grid, side, grid, side).mean(axis=(1, 3))
        return float(tiles.max())

    def _refine(self, query: Query, candidates: List[Candidate]) -> None:
        base = self._fine_array(query.data)
        if base is None:
            return
        base_gray = base.mean(axis=2)
        for cand in candidates:
            arr = self._fine_array(cand.record.path)
            if arr is None:
                continue
            fine = FP.ncc(base_gray, arr.mean(axis=2))
            peak = self._local_peak(base, arr)
            penalty = min(0.40, peak / REFINE_LOCAL_SCALE)
            adjusted = max(0.0, fine - penalty)
            cand.refined = True
            cand.notes.append(f"원본 화소 비교 {fine:+.4f}, 가장 다른 부분 {peak:.1f}")
            cand.score = 0.30 * cand.score + 0.70 * adjusted

    # -- 종합 ------------------------------------------------------------
    def match(self, query: Query, *, exclude: Optional[set] = None) -> SlotMatch:
        exclude = exclude or set()
        pool: Dict[str, Candidate] = {}

        for key_name, rec in self.exif_candidates(query):
            if rec.path in exclude:
                continue
            cand = self._score(query, rec, "그대로")
            cand.exif_key = key_name
            pool[rec.path] = cand

        name_hits = []
        for rec in self.name_candidates(query):
            if rec.path in exclude:
                continue
            cand = pool.get(rec.path) or self._score(query, rec, "그대로")
            cand.notes.append("한글에 적힌 원래 파일 이름과 같음")
            cand.name_match = True
            pool[rec.path] = cand
            name_hits.append(cand)

        for idx, orientation, _dist in self._shortlist(query, SHORTLIST):
            rec = self.visual[idx]
            if rec.path in exclude or rec.path in pool:
                continue
            pool[rec.path] = self._score(query, rec, orientation)

        if not pool:
            return SlotMatch(verdict=NOT_FOUND, message="원본 후보를 하나도 찾지 못했습니다")

        # EXIF 로 맞은 것은 겉모습이 비슷하기만 하면 무조건 앞세운다
        def sort_key(c: Candidate):
            return (1 if c.exif_key else 0, 1 if c.name_match else 0, c.score)

        ranked = sorted(pool.values(), key=sort_key, reverse=True)

        exif_hits = [c for c in ranked if c.exif_key]
        if exif_hits:
            return self._decide_with_exif(query, exif_hits, ranked)

        if len(name_hits) == 1 and name_hits[0].score >= 0.45:
            best = name_hits[0]
            others = [c for c in ranked if c is not best][:3]
            return SlotMatch(verdict=CERTAIN, best=best, runners_up=others,
                             message="한글 파일에 적힌 원래 파일 이름과 같은 사진을 찾았습니다")

        top = ranked[:5]
        if len(top) >= 2 and (top[0].score - top[1].score) < REFINE_MARGIN and top[0].score >= SCORE_REVIEW:
            self._refine(query, top[:3])
            ranked = sorted(ranked, key=sort_key, reverse=True)
            top = ranked[:5]

        best = ranked[0]
        margin = best.score - (ranked[1].score if len(ranked) > 1 else 0.0)
        if best.score >= SCORE_CERTAIN and margin >= MARGIN_CERTAIN:
            verdict, msg = CERTAIN, ""
        elif best.score >= SCORE_CERTAIN:
            verdict = REVIEW
            msg = f"비슷한 사진이 여럿입니다 (1등 {best.score:.3f}, 2등 {ranked[1].score:.3f})"
        elif best.score >= SCORE_REVIEW:
            verdict, msg = REVIEW, "닮긴 했지만 확신할 수 없습니다"
        else:
            verdict, msg = NOT_FOUND, "충분히 닮은 사진이 없습니다"
        return SlotMatch(verdict=verdict, best=best if verdict != NOT_FOUND else None,
                         runners_up=[c for c in ranked[1:4]], message=msg)

    def _decide_with_exif(self, query: Query, exif_hits: List[Candidate],
                          ranked: List[Candidate]) -> SlotMatch:
        strong = exif_hits[0].exif_key in ("사진 고유 ID", "촬영시각+1/100초+기종", "촬영시각+기종")
        others = [c for c in ranked if c is not exif_hits[0]][:3]
        if len(exif_hits) > 1:
            # 연사로 같은 초에 찍힌 사진들. 겉모습으로 가른다.
            exif_hits = sorted(exif_hits, key=lambda c: c.score, reverse=True)
            if exif_hits[0].score - exif_hits[1].score < REFINE_MARGIN:
                self._refine(query, exif_hits[:3])
                exif_hits = sorted(exif_hits, key=lambda c: c.score, reverse=True)
            best = exif_hits[0]
            if best.score - exif_hits[1].score < 0.02:
                return SlotMatch(verdict=REVIEW, best=best, runners_up=exif_hits[1:4],
                                 message="EXIF 가 같은 사진이 여러 장입니다 (연사로 보입니다)")
            return SlotMatch(verdict=CERTAIN if strong else REVIEW, best=best,
                             runners_up=exif_hits[1:4],
                             message="같은 촬영 시각의 사진 중 겉모습이 가장 가까운 것")

        best = exif_hits[0]
        if best.record.has_visual and query.primary is not None and best.score < 0.45:
            return SlotMatch(verdict=REVIEW, best=best, runners_up=others,
                             message="EXIF 는 같은데 사진이 달라 보입니다. 눈으로 확인해 주세요")
        return SlotMatch(verdict=CERTAIN if strong else REVIEW, best=best, runners_up=others,
                         message="" if strong else "촬영 시각만으로 맞춘 것이라 확인이 필요합니다")


def assign(matches: Sequence[SlotMatch]) -> None:
    """한 문서 안에서 같은 원본이 두 자리를 차지하지 않게 정리한다."""
    taken: Dict[str, int] = {}
    for i, m in enumerate(matches):
        if not m.best:
            continue
        path = m.best.record.path
        prev = taken.get(path)
        if prev is None:
            taken[path] = i
            continue
        # 점수가 낮은 쪽을 다음 후보로 밀어낸다
        loser = i if matches[i].best.score <= matches[prev].best.score else prev
        winner = prev if loser == i else i
        taken[path] = winner
        m2 = matches[loser]
        m2.message = (m2.message + " / " if m2.message else "") + "같은 원본이 다른 사진과 겹쳐 다음 후보로 넘겼습니다"
        if m2.runners_up:
            m2.best = m2.runners_up.pop(0)
            m2.verdict = REVIEW
        else:
            m2.best = None
            m2.verdict = NOT_FOUND

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
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .imaging import fingerprint as FP
from .imaging.exif import ExifFingerprint
from .imaging.loader import open_image, real_size
from .index import PhotoRecord

# --- 판정 기준 ---------------------------------------------------------------
# 겉모습 점수(0-1)가 이보다 높고 2등과의 차이가 충분하면 자동으로 확정한다
SCORE_CERTAIN = 0.86
SCORE_REVIEW = 0.62
MARGIN_CERTAIN = 0.05
# 종횡비가 이보다 어긋나면 '자른 사진'으로 보고 감점한다
ASPECT_TOLERANCE = 0.04
# 색이 있고 없고가 이만큼 어긋나면 원본이 아니라 흑백 사본이다.
# 감점 폭은 '확실' 구간에서 '확인 필요' 구간으로 내려가도록 잡았다.
CHROMA_TOLERANCE = 4.0
CHROMA_SCALE = 95.0
CHROMA_MAX_PENALTY = 0.32
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

# 같은 사진의 여러 판본 중 이만큼 크면 '더 큰 원본' 으로 보고 갈아탄다.
# 같은 크기끼리는 갈아타지 않는다 — 거의 똑같아 보이는 다른 사진일 수 있다.
LARGER_RATIO = 1.3
# 고른 파일이 한글에 든 사진보다 이만큼도 크지 않으면 원본이 아닐 수 있다고 알린다
ORIGINAL_RATIO = 1.2

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
    chroma_gap: float = 0.0
    orientation: str = "그대로"
    name_match: bool = False
    identical_file: bool = False    # 한글에 든 사진과 바이트까지 같은가
    size_ratio: float = 0.0         # 한글에 든 사진의 몇 배 크기인가
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
        if self.size_ratio:
            bits.append(f"한글 속 사진의 {self.size_ratio:.1f}배 크기")
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
    pixels: int = 0              # 한글에 든 사진의 화소 수
    head_sha1: str = ""          # 같은 파일인지 가리는 데 쓴다

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
    variants = FP.variants(image, true_size=real_size(data)) if image is not None else []
    if image is not None:
        try:
            image.close()
        except Exception:
            pass
    query = Query(exif=fingerprint_bytes(data), variants=variants, data=data)
    size = real_size(data)
    if size:
        query.pixels = size[0] * size[1]
    query.head_sha1 = _head_sha1_bytes(data)
    if hint is not None:
        query.hint_name = getattr(hint, "original_name", "") or ""
        query.hint_width = getattr(hint, "width", 0) or 0
        query.hint_height = getattr(hint, "height", 0) or 0
    return query


def _norm_name(name: str) -> str:
    return unicodedata.normalize("NFC", Path(name).name).casefold()


def _head_sha1_bytes(data: bytes) -> str:
    """색인이 파일에 쓰는 것과 같은 방식으로 앞부분 지문을 만든다."""
    h = hashlib.sha1()
    h.update(str(len(data)).encode())
    h.update(data[:65536])
    return h.hexdigest()


def same_photo(a: PhotoRecord, b: PhotoRecord) -> bool:
    """같은 사진의 다른 판본인가 (크기만 다른 사본인가).

    보고서를 만들 때 줄여 둔 사본이 원본 폴더에 함께 남아 있는 일이 흔하다.
    그 사본은 한글에 든 사진과 화소까지 똑같아서 점수가 가장 높게 나온다.
    하지만 제본에 필요한 것은 큰 원본이므로, 같은 사진임을 알아보고
    그중 큰 쪽을 골라야 한다.
    """
    if a.path == b.path:
        return True
    if a.unique_id and a.unique_id == b.unique_id:
        return True
    if a.taken_at and a.taken_at == b.taken_at and a.model == b.model:
        if a.subsec and b.subsec and a.subsec != b.subsec:
            return False            # 연사로 같은 초에 찍힌 다른 사진
        return True
    if not (a.has_visual and b.has_visual):
        return False
    if FP.aspect_gap(a.aspect, b.aspect) > 0.02:
        return False
    if FP.hamming(a.dhash, b.dhash) > 16:
        return False
    if FP.tile_distance(a.tile_array(), b.tile_array()) > 10:
        return False
    return FP.ncc(a.gray_array(), b.gray_array()) > 0.97


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

        cand.chroma_gap = abs(FP.chroma(fp.tile_array()) - FP.chroma(rec.tile_array()))

        ncc_part = max(0.0, cand.ncc)
        dhash_part = max(0.0, 1.0 - cand.dhash_distance / 256.0)
        phash_part = max(0.0, 1.0 - cand.phash_distance / 64.0)
        tile_part = max(0.0, 1.0 - cand.tile_distance / 80.0)
        score = 0.40 * ncc_part + 0.20 * dhash_part + 0.10 * phash_part + 0.30 * tile_part

        if cand.chroma_gap > CHROMA_TOLERANCE:
            # 흑백으로 바꿔 둔 사본이거나, 아예 다른 사진이다
            score -= min(CHROMA_MAX_PENALTY, cand.chroma_gap / CHROMA_SCALE)
            cand.notes.append(f"색이 들어 있는 정도가 {cand.chroma_gap:.0f}만큼 다름")

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

    # -- 마무리: 같은 사진이면 큰 쪽을 고른다 ------------------------------
    @staticmethod
    def _prefer_larger(query: Query, decision: SlotMatch) -> SlotMatch:
        """줄여 둔 사본 대신 큰 원본을 고른다.

        보고서용으로 줄인 사본이 원본 폴더에 함께 남아 있으면, 그 사본이
        한글에 든 사진과 화소까지 같아서 점수가 가장 높다. 하지만 제본에는
        큰 원본이 필요하다. 그래서 '같은 사진' 들을 묶고 그중 큰 것을 고른다.
        크기가 비슷하면 갈아타지 않는다 — 거의 똑같아 보이는 다른 사진일 수 있다.
        """
        if decision.best is None:
            return decision
        pool = [decision.best] + list(decision.runners_up)
        chosen = decision.best
        group = [c for c in pool if same_photo(chosen.record, c.record)]
        if len(group) > 1:
            def area(c):
                return c.record.width * c.record.height

            biggest = max(group, key=lambda c: (area(c), c.record.size))
            if biggest is not chosen and area(chosen) and \
                    area(biggest) >= area(chosen) * LARGER_RATIO:
                biggest.notes.append(
                    f"같은 사진이 여러 판본 있어 가장 큰 것을 골랐습니다 "
                    f"({chosen.record.width}×{chosen.record.height} → "
                    f"{biggest.record.width}×{biggest.record.height})")
                decision.runners_up = [c for c in pool if c is not biggest][:3]
                decision.best = biggest
                chosen = biggest
                if decision.verdict == REVIEW and not decision.message.startswith("EXIF 는"):
                    decision.verdict = CERTAIN
                    decision.message = ""

        # 고른 것이 한글에 든 사진과 견줘 얼마나 큰지 남긴다
        if query.pixels and chosen.record.width and chosen.record.height:
            chosen.size_ratio = (chosen.record.width * chosen.record.height) / query.pixels
        if query.head_sha1 and chosen.record.head_sha1 == query.head_sha1:
            chosen.identical_file = True
        return decision

    @staticmethod
    def _warn_if_not_original(query: Query, decision: SlotMatch,
                              allow_same_size: bool) -> SlotMatch:
        """고른 것이 원본이 아니라 또 다른 저용량 사본으로 보이면 알린다."""
        if decision.best is None or allow_same_size or not query.pixels:
            return decision
        cand = decision.best
        if not (cand.record.width and cand.record.height):
            return decision
        if cand.size_ratio >= ORIGINAL_RATIO:
            return decision
        if cand.identical_file:
            note = ("한글에 든 사진과 완전히 같은 파일입니다. "
                    "원본이 아니라 보고서용으로 줄인 사본으로 보입니다.")
        else:
            note = (f"한글에 든 사진과 크기가 비슷합니다 "
                    f"({cand.record.width}×{cand.record.height}). "
                    "원본이 아니라 줄인 사본일 수 있습니다.")
        decision.verdict = REVIEW
        decision.message = (decision.message + " / " if decision.message else "") + note
        return decision

    # -- 종합 ------------------------------------------------------------
    def match(self, query: Query, *, exclude: Optional[set] = None,
              allow_same_size: bool = False) -> SlotMatch:
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
            return self._finish(query, self._decide_with_exif(query, exif_hits, ranked),
                                allow_same_size)

        if len(name_hits) == 1 and name_hits[0].score >= 0.45:
            best = name_hits[0]
            others = [c for c in ranked if c is not best][:3]
            return self._finish(query, SlotMatch(
                verdict=CERTAIN, best=best, runners_up=others,
                message="한글 파일에 적힌 원래 파일 이름과 같은 사진을 찾았습니다"), allow_same_size)

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
        return self._finish(query, SlotMatch(
            verdict=verdict, best=best if verdict != NOT_FOUND else None,
            runners_up=[c for c in ranked[1:4]], message=msg), allow_same_size)

    def _finish(self, query: Query, decision: SlotMatch, allow_same_size: bool) -> SlotMatch:
        decision = self._prefer_larger(query, decision)
        return self._warn_if_not_original(query, decision, allow_same_size)

    @staticmethod
    def _collapse_same_photo(candidates: List[Candidate]) -> List[Candidate]:
        """같은 사진의 여러 판본을 하나로 묶고, 그중 가장 큰 것만 남긴다.

        이걸 하지 않으면 '원본 + 보고서용 사본' 을 연사로 찍은 다른 사진으로 오인해
        멀쩡한 짝을 '확인 필요' 로 내려보낸다.
        """
        groups: List[List[Candidate]] = []
        for cand in candidates:
            for group in groups:
                if same_photo(group[0].record, cand.record):
                    group.append(cand)
                    break
            else:
                groups.append([cand])
        out = []
        for group in groups:
            biggest = max(group, key=lambda c: (c.record.width * c.record.height, c.record.size))
            if len(group) > 1 and biggest is not group[0]:
                biggest.notes.append(
                    f"같은 사진이 여러 판본 있어 가장 큰 것을 골랐습니다 "
                    f"({biggest.record.width}×{biggest.record.height})")
            out.append(biggest)
        return out

    def _decide_with_exif(self, query: Query, exif_hits: List[Candidate],
                          ranked: List[Candidate]) -> SlotMatch:
        exif_hits = self._collapse_same_photo(exif_hits)
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

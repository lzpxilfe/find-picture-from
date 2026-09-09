"""찾아낸 원본을 알맞은 폴더에 알맞은 이름으로 넣는다.

원본은 **읽기만** 한다. 복사만 하고 옮기거나 지우지 않는다.
"""

from __future__ import annotations

import csv
import difflib
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .match import CERTAIN, NOT_FOUND, REVIEW, SlotMatch
from .model import HwpDocument, PhotoSlot
from .naming import apply_template, number_captions, sanitize, unique_path

# 원본을 못 찾았을 때 문서 안의 저용량 사진을 어떻게 넣을지
LOWRES_PLAIN = "plain"        # 같은 이름으로 그냥 넣는다
LOWRES_SUFFIX = "suffix"      # 이름 뒤에 (저용량) 을 붙인다
LOWRES_SUBDIR = "subdir"      # _저용량 하위 폴더에 넣는다
LOWRES_SKIP = "skip"          # 넣지 않는다

LOWRES_MARK = "저용량"


_DIGITS = re.compile(r"\d+")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    return "".join(ch for ch in text.lower() if not ch.isspace())


def digit_signature(text: str):
    """이름에 든 숫자들. 보고서 이름에서 숫자는 곧 신원이다.

    '대전_001 조사카드' 와 '대전_002 조사카드' 는 글자로만 보면 94% 닮았지만
    완전히 다른 유적이다. 숫자가 다르면 아무리 닮아도 같은 것으로 보면 안 된다.
    """
    return tuple(int(m) for m in _DIGITS.findall(unicodedata.normalize("NFC", text or "")))


def _digits_agree(folder_name: str, doc_name: str) -> bool:
    """폴더 이름의 숫자가 모두 문서 이름에도 있어야 한다.

    '대전_002 조사카드' 와 '대전_001 조사카드' 는 글자로만 보면 94% 닮았지만
    다른 건이다. 반대로 '효평동 유물산포지2' 폴더는 '2025 대전 효평동
    유물산포지2 정밀지표조사.hwp' 의 폴더가 맞다 — 폴더의 숫자(2)가 문서에도 있다.

    문서에 숫자가 있는데 폴더에 하나도 없으면, 그 폴더는 여러 건을 한꺼번에
    빨아들이는 두루뭉술한 이름일 가능성이 크므로 받지 않는다.
    """
    folder_digits = set(digit_signature(folder_name))
    doc_digits = set(digit_signature(doc_name))
    if not folder_digits and not doc_digits:
        return True
    if not doc_digits or not folder_digits:
        return False
    return folder_digits <= doc_digits


@dataclass
class FolderChoice:
    path: Optional[Path] = None
    how: str = ""          # 어떻게 정했는지 (사람에게 보여줄 말)
    created: bool = False
    ambiguous: List[str] = field(default_factory=list)


def resolve_folder(doc: HwpDocument, dest_root: Path, *,
                   existing: Optional[Sequence[Path]] = None,
                   create_missing: bool = True,
                   fuzzy_cutoff: float = 0.90) -> FolderChoice:
    """한글 파일 하나에 대응하는 '이미 만들어 둔 폴더'를 찾는다.

    이름이 정확히 같은 폴더 -> 공백/대소문자만 다른 폴더 -> 도면 명칭 같은
    문서 안 값과 같은 폴더 -> 비슷한 이름 순으로 본다.
    """
    dest_root = Path(dest_root)
    if existing is None:
        existing = [p for p in dest_root.iterdir() if p.is_dir()] if dest_root.is_dir() else []
    by_norm: Dict[str, List[Path]] = {}
    for p in existing:
        by_norm.setdefault(_norm(p.name), []).append(p)

    stem = doc.path.stem
    keys = [(stem, "파일 이름과 같은 폴더")]
    for field_name in ("도면 명칭", "도면명칭", "유적명", "유적 명칭", "연번"):
        value = doc.fields.get(field_name)
        if value:
            keys.append((value, f"문서의 '{field_name}' 과 같은 폴더"))

    for value, how in keys:
        hits = [p for p in by_norm.get(_norm(value), []) if _digits_agree(p.name, value)]
        if hits:
            return FolderChoice(path=hits[0], how=how,
                                ambiguous=[str(p) for p in hits[1:]])

    # 폴더 이름이 파일 이름 안에 통째로 들어 있는 경우 (가장 긴 것을 고른다).
    # 숫자가 어긋나면 다른 건이므로 제외한다.
    contained = [p for p in existing
                 if _norm(p.name) and _norm(p.name) in _norm(stem)
                 and _digits_agree(p.name, stem)]
    if contained:
        contained.sort(key=lambda p: len(p.name), reverse=True)
        return FolderChoice(path=contained[0], how="폴더 이름이 파일 이름에 들어 있음",
                            ambiguous=[str(p) for p in contained[1:3]])

    # 마지막으로 이름이 비슷한 폴더. 숫자가 같은 것만 후보로 둔다.
    for value, _how in keys:
        names = [p.name for p in existing if _digits_agree(p.name, value)]
        close = difflib.get_close_matches(value, names, n=3, cutoff=fuzzy_cutoff)
        if close:
            match = next(p for p in existing if p.name == close[0])
            return FolderChoice(path=match, how=f"'{value}' 와 이름이 비슷한 폴더",
                                ambiguous=close[1:])

    if create_missing:
        target = dest_root / sanitize(stem)
        return FolderChoice(path=target, how="맞는 폴더가 없어 새로 만듦", created=True)
    return FolderChoice(path=None, how="맞는 폴더를 찾지 못함")


@dataclass
class PlacedFile:
    slot: PhotoSlot
    name: str                    # 확장자를 뺀 최종 이름
    target: Optional[Path] = None
    source: str = ""             # 어디서 온 파일인지 (원본 경로 또는 '한글 파일 안')
    origin: str = "원본"          # 원본 | 저용량
    verdict: str = NOT_FOUND
    score: float = 0.0
    size_ratio: float = 0.0      # 한글 속 사진의 몇 배 크기인가
    reason: str = ""
    message: str = ""
    skipped: str = ""            # 비어 있지 않으면 넣지 않은 이유


def plan_names(doc: HwpDocument, *, template: str = "{이름}") -> List[str]:
    """문서 안 사진들의 최종 이름. 같은 이름은 (1) (2) 로 가른다."""
    base = number_captions([s.caption for s in doc.slots])
    if template.strip() in ("{이름}", ""):
        return base
    out = []
    for slot, name in zip(doc.slots, base):
        variables = dict(doc.fields)
        variables.update({
            "이름": name,
            "구역": slot.group,
            "파일명": doc.path.stem,
            "번호": f"{slot.doc_order:02d}",
        })
        variables.setdefault("도면명칭", doc.fields.get("도면 명칭", ""))
        out.append(apply_template(template, variables) or name)
    return out


def place(doc: HwpDocument,
          matches: Sequence[SlotMatch],
          folder: Path,
          *,
          template: str = "{이름}",
          lowres: str = LOWRES_PLAIN,
          accept_review: bool = True,
          overwrite: bool = False,
          dry_run: bool = False) -> List[PlacedFile]:
    """사진들을 폴더에 넣는다. dry_run 이면 계획만 세운다."""
    names = plan_names(doc, template=template)
    placed: List[PlacedFile] = []
    if not dry_run:
        folder.mkdir(parents=True, exist_ok=True)

    for slot, match, name in zip(doc.slots, matches, names):
        item = doc.bin_items.get(slot.bin_id)
        rec = PlacedFile(slot=slot, name=name,
                         verdict=match.verdict,
                         score=match.best.score if match.best else 0.0,
                         size_ratio=match.best.size_ratio if match.best else 0.0,
                         reason=match.best.reason if match.best else "",
                         message=match.message)

        use_original = match.best is not None and (
            match.verdict == CERTAIN or (match.verdict == REVIEW and accept_review)
        )
        if use_original:
            src = Path(match.best.record.path)
            rec.source = str(src)
            rec.origin = "원본"
            suffix = src.suffix or ".jpg"
            target_dir = folder
        else:
            if item is None or not item.data:
                rec.skipped = "문서 안에도 사진 자료가 없습니다"
                placed.append(rec)
                continue
            if lowres == LOWRES_SKIP:
                rec.skipped = "원본을 찾지 못했습니다"
                placed.append(rec)
                continue
            rec.source = f"{doc.path.name} 안의 {item.stream}"
            rec.origin = LOWRES_MARK
            suffix = "." + (item.ext or "jpg").lower().lstrip(".")
            if suffix == ".jpeg":
                suffix = ".jpg"
            target_dir = folder / f"_{LOWRES_MARK}" if lowres == LOWRES_SUBDIR else folder
            if lowres == LOWRES_SUFFIX:
                name = f"{name}({LOWRES_MARK})"

        stem = sanitize(name)
        if dry_run:
            rec.target = target_dir / f"{stem}{suffix}"
            placed.append(rec)
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{stem}{suffix}"
        if target.exists() and not overwrite:
            target = unique_path(target_dir, stem, suffix)
        rec.target = target
        try:
            if rec.origin == "원본":
                shutil.copy2(rec.source, target)
            else:
                target.write_bytes(item.data)
        except OSError as exc:
            rec.skipped = f"복사 실패: {exc}"
            rec.target = None
        placed.append(rec)
    return placed


STATE_NAME = "상태.json"


def write_state(path: Path, entries: Sequence[dict]) -> None:
    """어느 문서의 어느 사진을 어디에 넣었는지 남긴다.

    나중에 사람이 리포트에서 고친 것을 다시 적용할 때(findpic apply) 쓴다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "documents": list(entries)},
                               ensure_ascii=False, indent=1), encoding="utf-8")


def read_state(path: Path) -> List[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("documents", []) if isinstance(data, dict) else []


def state_entry(doc: HwpDocument, folder: Path, placed: Sequence[PlacedFile]) -> dict:
    return {
        "문서": doc.path.name,
        "문서경로": str(doc.path),
        "폴더": str(folder),
        "사진": [
            {
                "이름": rec.name,
                "번호": rec.slot.bin_id,
                "넣은파일": str(rec.target) if rec.target else "",
                "종류": rec.origin,
                "판정": rec.verdict,
            }
            for rec in placed
        ],
    }


def write_csv(path: Path, rows: Sequence[tuple]) -> None:
    """엑셀에서 바로 열리도록 BOM 을 붙여 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "한글파일", "사진이름", "구역", "이름출처", "판정", "점수", "크기배수",
            "넣은파일", "가져온곳", "종류", "메모",
        ])
        writer.writerows(rows)


def csv_rows(doc: HwpDocument, placed: Sequence[PlacedFile]) -> List[tuple]:
    out = []
    for rec in placed:
        out.append((
            doc.path.name,
            rec.name,
            rec.slot.group,
            rec.slot.caption_source,
            rec.verdict if not rec.skipped else "넣지않음",
            f"{rec.score:.3f}" if rec.score else "",
            f"{rec.size_ratio:.1f}배" if rec.size_ratio else "",
            str(rec.target) if rec.target else "",
            rec.source,
            rec.origin,
            rec.skipped or rec.message or rec.reason,
        ))
    return out

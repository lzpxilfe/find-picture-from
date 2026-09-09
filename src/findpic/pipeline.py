"""찾아서 정리하는 전체 흐름.

명령줄과 창(GUI)이 같은 코드를 쓰도록, 화면에 무엇을 어떻게 보여줄지는
바깥에서 받은 함수에 맡기고 여기서는 일만 한다.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .hwp.extract import extract
from .hwp.reader import EncryptedHwpError, HwpError
from .index import PhotoIndex
from .match import CERTAIN, NOT_FOUND, REVIEW, Matcher, SlotMatch, assign, build_query
from .model import HwpDocument
from .organize import (LOWRES_PLAIN, STATE_NAME, csv_rows, place, resolve_folder,
                       state_entry, write_csv, write_state)
from . import report as report_mod
from .report import DocReport

HWP_SUFFIXES = (".hwp", ".hwpx")


class Cancelled(Exception):
    """사용자가 중간에 멈췄다."""


def default_workers() -> int:
    """동시에 읽을 사진 수.

    실측상 코어 수보다 많이 띄우면 오히려 느려진다(디스크가 아니라 디코딩이 병목).
    """
    return max(2, min(8, os.cpu_count() or 4))


def clean_path(text: str) -> str:
    """탐색기에서 끌어다 놓으면 따옴표가 같이 붙는다. 그걸 떼어 낸다."""
    text = (text or "").strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1]
            break
    return text.strip().rstrip("\\/") or text.strip()


def collect_hwp(paths: Sequence, recursive: bool = True) -> List[Path]:
    out: List[Path] = []
    for raw in paths:
        p = Path(clean_path(str(raw)))
        if p.is_file():
            if p.suffix.lower() in HWP_SUFFIXES:
                out.append(p)
        elif p.is_dir():
            walker = p.rglob("*") if recursive else p.glob("*")
            for child in walker:
                if child.is_file() and child.suffix.lower() in HWP_SUFFIXES \
                        and not child.name.startswith("~"):
                    out.append(child)
    return sorted(set(out))


@dataclass
class JobConfig:
    hwp: List[str] = field(default_factory=list)
    source: List[str] = field(default_factory=list)
    dest: str = ""
    cache: Optional[str] = None
    name_template: str = "{이름}"
    lowres: str = LOWRES_PLAIN
    flat: bool = False
    strict: bool = False
    allow_duplicate: bool = False
    overwrite: bool = False
    dry_run: bool = False
    create_missing: bool = True
    include_floating: bool = False
    recursive: bool = True
    workers: int = 0
    fast_index: bool = True
    allow_same_size: bool = False   # 한글 속 사진과 크기가 같은 것도 원본으로 인정할지
    make_report: bool = True
    report_path: Optional[str] = None
    thumbs: str = "auto"

    def cache_path(self) -> Path:
        if self.cache:
            return Path(clean_path(str(self.cache)))
        if self.dest:
            return Path(clean_path(self.dest)) / "_findpic" / "index.sqlite3"
        return Path.home() / ".findpic" / "index.sqlite3"


@dataclass
class JobResult:
    ok: bool = False
    error: str = ""
    documents: int = 0
    photos: int = 0
    candidates: int = 0
    counts: dict = field(default_factory=dict)
    lowres_used: int = 0
    skipped_documents: List[str] = field(default_factory=list)
    csv_path: Optional[Path] = None
    report_path: Optional[Path] = None
    dest_root: Optional[Path] = None
    elapsed: float = 0.0
    cancelled: bool = False


def run_job(config: JobConfig,
            *,
            on_message: Optional[Callable[[str, str], None]] = None,
            on_progress: Optional[Callable[[str, int, int, str], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None) -> JobResult:
    """한글 파일을 읽고, 원본을 찾아, 정리해 넣는다.

    on_message(글, 종류)   종류: head | info | warn | error | done
    on_progress(단계, 한 것, 전체, 지금 파일)
    should_stop()          True 를 돌려주면 멈춘다
    """
    say = on_message or (lambda text, kind="info": None)
    tick = on_progress or (lambda stage, done, total, note="": None)
    stop = should_stop or (lambda: False)

    def check():
        if stop():
            raise Cancelled()

    result = JobResult(counts={CERTAIN: 0, REVIEW: 0, NOT_FOUND: 0})
    started = time.time()

    files = collect_hwp(config.hwp, recursive=config.recursive)
    if not files:
        result.error = "한글 파일(.hwp/.hwpx)을 찾지 못했습니다."
        say(result.error, "error")
        return result
    if not config.source:
        result.error = "원본 사진이 있는 폴더를 지정해 주세요."
        say(result.error, "error")
        return result
    if not config.dest:
        result.error = "정리해 넣을 폴더를 지정해 주세요."
        say(result.error, "error")
        return result

    dest_root = Path(clean_path(config.dest))
    result.dest_root = dest_root
    sources = [clean_path(str(s)) for s in config.source if str(s).strip()]
    missing = [s for s in sources if not Path(s).exists()]
    if missing:
        result.error = "원본 사진 폴더를 찾을 수 없습니다: " + ", ".join(missing)
        say(result.error, "error")
        return result

    workers = config.workers or default_workers()

    try:
        # 1단계 — 한글 파일 읽기
        say(f"한글 파일 {len(files)}개를 읽습니다.", "head")
        docs: List[HwpDocument] = []
        for i, path in enumerate(files, start=1):
            check()
            tick("읽는 중", i, len(files), path.name)
            try:
                docs.append(extract(path, include_floating=config.include_floating))
            except EncryptedHwpError:
                say(f"건너뜀 (암호가 걸린 파일): {path.name}", "warn")
                result.skipped_documents.append(path.name)
            except HwpError as exc:
                say(f"건너뜀 ({exc}): {path.name}", "warn")
                result.skipped_documents.append(path.name)
            except Exception as exc:          # 한 파일 때문에 전체가 멈추면 안 된다
                say(f"건너뜀 (읽기 실패: {exc}): {path.name}", "warn")
                result.skipped_documents.append(path.name)
        result.documents = len(docs)
        slots_total = sum(len(d.slots) for d in docs)
        result.photos = slots_total
        say(f"표에서 사진 {slots_total}장과 이름을 찾았습니다.", "info")
        if not slots_total:
            result.error = "넣을 사진이 없습니다."
            say(result.error, "error")
            return result

        # 2단계 — 원본 사진 색인
        check()
        say("원본 사진 폴더를 훑습니다.", "head")
        index = PhotoIndex(config.cache_path())
        stats = index.refresh(
            sources, workers=workers, fast=config.fast_index,
            progress=lambda done, total, note: (tick("색인 만드는 중", done, total, note), check())[0],
        )
        result.candidates = stats["전체"]
        say(f"원본 후보 {stats['전체']:,}장 "
            f"(새로 읽음 {stats['새로 읽음']:,} · 지난 결과 재사용 {stats['재사용']:,})", "info")
        if stats["전체"] == 0:
            result.error = "원본 사진을 한 장도 찾지 못했습니다. 폴더 경로를 확인해 주세요."
            say(result.error, "error")
            return result

        records = index.load_all(roots=sources)
        matcher = Matcher(records)

        # 3단계 — 대조하고 넣기
        check()
        say("사진을 하나씩 대조합니다.", "head")
        existing = [p for p in dest_root.iterdir() if p.is_dir()] if dest_root.is_dir() else []
        reports: List[DocReport] = []
        rows: List[tuple] = []
        states: List[dict] = []
        done = 0

        for doc in docs:
            matches = []
            for slot in doc.slots:
                check()
                item = doc.bin_items.get(slot.bin_id)
                if item is None or not item.data:
                    matches.append(SlotMatch(verdict=NOT_FOUND,
                                             message="문서에서 사진 자료를 꺼내지 못했습니다"))
                else:
                    matches.append(matcher.match(
                        build_query(item.data, doc.hints.get(slot.bin_id)),
                        allow_same_size=config.allow_same_size))
                done += 1
                tick("대조 중", done, slots_total, slot.caption or doc.path.name)
            if not config.allow_duplicate:
                assign(matches)

            if config.flat:
                choice_path, choice_how, created = dest_root, "한 폴더에 모으기", False
            else:
                choice = resolve_folder(doc, dest_root, existing=existing,
                                        create_missing=config.create_missing)
                choice_path, choice_how, created = choice.path, choice.how, choice.created
            if choice_path is None:
                say(f"{doc.path.name}: 넣을 폴더를 찾지 못해 건너뜁니다.", "warn")
                result.skipped_documents.append(doc.path.name)
                continue
            if created and not config.dry_run:
                existing.append(choice_path)

            placed = place(doc, matches, choice_path,
                           template=config.name_template,
                           lowres=config.lowres,
                           accept_review=not config.strict,
                           overwrite=config.overwrite,
                           dry_run=config.dry_run)
            for rec in placed:
                result.counts[rec.verdict] = result.counts.get(rec.verdict, 0) + 1
                if rec.origin != "원본" and not rec.skipped:
                    result.lowres_used += 1
            rows.extend(csv_rows(doc, placed))
            states.append(state_entry(doc, choice_path, placed))
            reports.append(DocReport(doc=doc, folder=choice_path, folder_how=choice_how,
                                     matches=matches, placed=placed))

        # 4단계 — 결과 남기기
        if not config.dry_run:
            out_dir = dest_root / "_findpic"
            out_dir.mkdir(parents=True, exist_ok=True)
            result.csv_path = out_dir / "결과목록.csv"
            write_csv(result.csv_path, rows)
            write_state(out_dir / STATE_NAME, states)
            if config.make_report:
                path = (Path(clean_path(str(config.report_path))) if config.report_path
                        else out_dir / "검토리포트.html")
                say("검토 리포트를 만드는 중입니다. 사진이 많으면 조금 걸립니다.", "info")
                report_mod.write(path, reports, thumbs=config.thumbs)
                result.report_path = path

        result.ok = True
    except Cancelled:
        result.cancelled = True
        result.error = "중단했습니다. 지금까지 만든 색인은 남아 있어 다시 실행하면 이어서 합니다."
        say(result.error, "warn")
    except Exception as exc:                  # 예상 못 한 오류도 사용자에게 알린다
        result.error = f"뜻밖의 오류로 멈췄습니다: {exc}"
        say(result.error, "error")

    result.elapsed = time.time() - started
    return result

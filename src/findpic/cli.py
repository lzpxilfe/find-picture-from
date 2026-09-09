"""명령줄 도구."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .hwp.extract import extract
from .hwp.reader import EncryptedHwpError, HwpError
from .index import PhotoIndex
from .match import CERTAIN, NOT_FOUND, REVIEW, Matcher, assign, build_query
from .model import HwpDocument
from .organize import (LOWRES_PLAIN, LOWRES_SKIP, LOWRES_SUBDIR, LOWRES_SUFFIX,
                       STATE_NAME, csv_rows, place, read_state, resolve_folder,
                       state_entry, write_csv, write_state)
from .report import DocReport
from . import report as report_mod

HWP_SUFFIXES = (".hwp", ".hwpx")


# --- 콘솔 ------------------------------------------------------------------

def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def default_workers() -> int:
    """동시에 읽을 사진 수.

    실측상 코어 수보다 많이 띄우면 오히려 느려진다(디스크가 아니라 디코딩이 병목).
    """
    return max(2, min(8, os.cpu_count() or 4))


def say(*args) -> None:
    print(*args, flush=True)


class Progress:
    """한 줄을 덮어쓰며 진행 상황을 보여 준다."""

    def __init__(self, label: str, enabled: bool = True):
        self.label = label
        self.enabled = enabled and sys.stdout.isatty()
        self.started = time.time()
        self._last = 0.0

    def __call__(self, done: int, total: int, note: str = "") -> None:
        if not self.enabled:
            return
        now = time.time()
        if done < total and now - self._last < 0.15:
            return
        self._last = now
        pct = (done / total * 100) if total else 100.0
        elapsed = now - self.started
        eta = (elapsed / done * (total - done)) if done else 0
        bar_len = 22
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "·" * (bar_len - filled)
        tail = f" 남은 시간 약 {_dur(eta)}" if done < total and eta > 2 else ""
        line = f"\r  {self.label} [{bar}] {done:,}/{total:,} ({pct:4.1f}%){tail}   "
        sys.stdout.write(line[:150])
        sys.stdout.flush()

    def done(self, message: str = "") -> None:
        if self.enabled:
            sys.stdout.write("\r" + " " * 150 + "\r")
            sys.stdout.flush()
        if message:
            say(message)


def _dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분 {seconds % 60}초"
    return f"{seconds // 3600}시간 {(seconds % 3600) // 60}분"


# --- 경로 다루기 -------------------------------------------------------------

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


# --- 대화형 ------------------------------------------------------------------

def ask(prompt: str, *, must_exist: bool = True, allow_empty: bool = False) -> Optional[Path]:
    while True:
        raw = input(prompt).strip()
        if not raw and allow_empty:
            return None
        path = Path(clean_path(raw))
        if not raw:
            say("  경로를 입력해 주세요. (창에 파일이나 폴더를 끌어다 놓아도 됩니다)")
            continue
        if must_exist and not path.exists():
            say(f"  '{path}' 를 찾을 수 없습니다. 다시 입력해 주세요.")
            continue
        return path


def interactive(args: argparse.Namespace) -> argparse.Namespace:
    say("")
    say("  ┌───────────────────────────────────────────────┐")
    say("  │  한글 보고서 사진 → 원본 사진 찾아 정리하기   │")
    say("  └───────────────────────────────────────────────┘")
    say("")
    say("  경로는 탐색기에서 파일/폴더를 이 창에 끌어다 놓으면 자동으로 입력됩니다.")
    say("")
    hwp = ask("  1) 한글 파일 또는 한글 파일들이 든 폴더 : ")
    say("")
    sources: List[Path] = []
    first = ask("  2) 원본 사진이 들어 있는 폴더           : ")
    sources.append(first)
    while True:
        more = ask("     원본 폴더를 더 추가하려면 입력 (없으면 Enter) : ", allow_empty=True)
        if more is None:
            break
        sources.append(more)
    say("")
    dest = ask("  3) 정리해 넣을 폴더 (이미 만들어 둔 폴더들의 상위 폴더) : ")
    say("")
    args.hwp = [hwp]
    args.source = sources
    args.dest = dest
    return args


# --- 실행 --------------------------------------------------------------------

def load_documents(files: Sequence[Path], progress: Progress,
                   *, include_floating: bool = False) -> List[HwpDocument]:
    docs: List[HwpDocument] = []
    for i, path in enumerate(files, start=1):
        progress(i, len(files), path.name)
        try:
            docs.append(extract(path, include_floating=include_floating))
        except EncryptedHwpError:
            say(f"  · 건너뜀 (암호가 걸린 파일): {path.name}")
        except HwpError as exc:
            say(f"  · 건너뜀 ({exc}): {path.name}")
        except Exception as exc:                    # 한 파일 때문에 전체가 멈추면 안 된다
            say(f"  · 건너뜀 (읽기 실패: {exc}): {path.name}")
    progress.done()
    return docs


def cmd_scan(args: argparse.Namespace) -> int:
    files = collect_hwp(args.hwp, recursive=not args.no_recursive)
    if not files:
        say("한글 파일(.hwp/.hwpx)을 찾지 못했습니다.")
        return 1
    say(f"한글 파일 {len(files)}개를 읽습니다.\n")
    docs = load_documents(files, Progress("읽는 중", not args.quiet),
                          include_floating=args.include_floating)
    total = 0
    for doc in docs:
        say(f"■ {doc.path.name}")
        label = doc.fields.get("도면 명칭") or doc.fields.get("유적명")
        if label:
            say(f"   문서 정보: {label}")
        if not doc.slots:
            say("   표 안에서 사진을 찾지 못했습니다.")
        for slot in doc.slots:
            item = doc.bin_items.get(slot.bin_id)
            size = f"{item.size / 1024:,.0f}KB" if item else "?"
            place_txt = "셀 배경" if slot.placement == "background" else "셀 안 그림"
            say(f"   {slot.doc_order:2d}. {slot.caption or '(이름 없음)':<12s}"
                f" | {slot.group or '-':<10s} | {place_txt} | {size} | 이름 출처: {slot.caption_source or '-'}")
            total += 1
        for w in doc.warnings:
            say(f"   ! {w}")
        say("")
    say(f"모두 {len(docs)}개 문서, 사진 {total}장.")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    if not args.source:
        say("--source 로 원본 사진 폴더를 지정해 주세요.")
        return 1
    index = PhotoIndex(_cache_path(args))
    say(f"원본 사진 폴더를 훑습니다: {', '.join(str(s) for s in args.source)}")
    progress = Progress("색인 만드는 중", not args.quiet)
    stats = index.refresh(args.source, workers=args.workers,
                          fast=not args.no_fast_index, progress=progress)
    progress.done()
    say(f"  색인 완료: 전체 {stats['전체']:,}장 "
        f"(새로 읽음 {stats['새로 읽음']:,} / 재사용 {stats['재사용']:,} / 실패 {stats['실패']:,})")
    say(f"  색인 파일: {index.db_path}")
    return 0


def _cache_path(args: argparse.Namespace) -> Path:
    if getattr(args, "cache", None):
        return Path(clean_path(str(args.cache)))
    if getattr(args, "dest", None):
        return Path(args.dest) / ".findpic" / "index.sqlite3"
    return Path.home() / ".findpic" / "index.sqlite3"


def cmd_run(args: argparse.Namespace) -> int:
    files = collect_hwp(args.hwp, recursive=not args.no_recursive)
    if not files:
        say("한글 파일(.hwp/.hwpx)을 찾지 못했습니다.")
        return 1
    if not args.source:
        say("--source 로 원본 사진이 있는 폴더를 지정해 주세요.")
        return 1
    dest_root = Path(clean_path(str(args.dest))) if args.dest else None
    if dest_root is None:
        say("--dest 로 정리해 넣을 폴더를 지정해 주세요.")
        return 1

    started = time.time()
    say("")
    say(f"● 한글 파일 {len(files)}개를 읽습니다.")
    docs = load_documents(files, Progress("읽는 중", not args.quiet),
                          include_floating=args.include_floating)
    slots_total = sum(len(d.slots) for d in docs)
    say(f"  표에서 사진 {slots_total}장과 이름을 찾았습니다.")
    if not slots_total:
        say("  넣을 사진이 없습니다.")
        return 1

    say("")
    say(f"● 원본 사진 폴더를 훑습니다.")
    index = PhotoIndex(_cache_path(args))
    progress = Progress("색인 만드는 중", not args.quiet)
    stats = index.refresh(args.source, workers=args.workers,
                          fast=not args.no_fast_index, progress=progress)
    progress.done()
    say(f"  원본 후보 {stats['전체']:,}장 "
        f"(새로 읽음 {stats['새로 읽음']:,} · 지난 결과 재사용 {stats['재사용']:,})")
    if stats["전체"] == 0:
        say("  원본 사진을 한 장도 찾지 못했습니다. 폴더 경로를 확인해 주세요.")
        return 1

    records = index.load_all(roots=args.source)
    matcher = Matcher(records)

    say("")
    say("● 사진을 하나씩 대조합니다.")
    existing = [p for p in dest_root.iterdir() if p.is_dir()] if dest_root.is_dir() else []
    reports: List[DocReport] = []
    rows: List[tuple] = []
    states: List[dict] = []
    counts = {CERTAIN: 0, REVIEW: 0, NOT_FOUND: 0}
    lowres_used = 0
    progress = Progress("대조 중", not args.quiet)
    done = 0

    for doc in docs:
        matches = []
        for slot in doc.slots:
            item = doc.bin_items.get(slot.bin_id)
            if item is None or not item.data:
                from .match import SlotMatch
                matches.append(SlotMatch(verdict=NOT_FOUND, message="문서에서 사진 자료를 꺼내지 못했습니다"))
            else:
                matches.append(matcher.match(build_query(item.data, doc.hints.get(slot.bin_id))))
            done += 1
            progress(done, slots_total, slot.caption)
        if not args.allow_duplicate:
            assign(matches)

        if args.flat:
            choice_path, choice_how = dest_root, "한 폴더에 모으기"
            created = False
        else:
            choice = resolve_folder(doc, dest_root, existing=existing,
                                    create_missing=not args.no_create_missing)
            choice_path, choice_how, created = choice.path, choice.how, choice.created
        if choice_path is None:
            say(f"  · {doc.path.name}: 넣을 폴더를 찾지 못해 건너뜁니다.")
            continue
        if created and not args.dry_run:
            existing.append(choice_path)

        placed = place(doc, matches, choice_path,
                       template=args.name,
                       lowres=args.lowres,
                       accept_review=not args.strict,
                       overwrite=args.overwrite,
                       dry_run=args.dry_run)
        for rec in placed:
            counts[rec.verdict] = counts.get(rec.verdict, 0) + 1
            if rec.origin != "원본" and not rec.skipped:
                lowres_used += 1
        rows.extend(csv_rows(doc, placed))
        states.append(state_entry(doc, choice_path, placed))
        reports.append(DocReport(doc=doc, folder=choice_path, folder_how=choice_how,
                                 matches=matches, placed=placed))
    progress.done()

    say("")
    say("● 결과")
    say(f"  원본 확실       {counts.get(CERTAIN, 0):>5,}장")
    say(f"  확인 필요       {counts.get(REVIEW, 0):>5,}장")
    say(f"  못 찾음         {counts.get(NOT_FOUND, 0):>5,}장")
    if lowres_used:
        say(f"  저용량으로 대체 {lowres_used:>5,}장  (원본을 못 찾아 한글 파일 안의 사진을 넣었습니다)")

    out_dir = dest_root / "_findpic" if not args.dry_run else dest_root
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / "결과목록.csv"
        write_csv(csv_path, rows)
        write_state(out_dir / STATE_NAME, states)
        say(f"\n  결과 목록: {csv_path}")
        if not args.no_report:
            report_path = Path(clean_path(str(args.report))) if args.report else out_dir / "검토리포트.html"
            report_mod.write(report_path, reports, thumbs=args.thumbs)
            say(f"  검토 리포트: {report_path}")
            say("    (브라우저로 열면 한글 파일 속 사진과 찾은 원본을 나란히 볼 수 있습니다)")
    else:
        say("\n  --dry-run 이라 실제로 파일을 넣지는 않았습니다.")

    say(f"\n  걸린 시간 {_dur(time.time() - started)}")
    if counts.get(REVIEW) or counts.get(NOT_FOUND):
        say("\n  '확인 필요' 와 '못 찾음' 은 검토 리포트에서 눈으로 확인해 주세요.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """사람이 리포트에서 고친 것을 그대로 다시 넣는다."""
    import csv as _csv

    csv_path = Path(clean_path(str(args.csv))) if args.csv else None
    if csv_path is None or not csv_path.exists():
        say("--csv 로 검토 리포트에서 내려받은 수정목록.csv 를 지정해 주세요.")
        return 1

    state_path = Path(clean_path(str(args.state))) if args.state else None
    if state_path is None:
        for candidate in (csv_path.parent / STATE_NAME,
                          csv_path.parent / "_findpic" / STATE_NAME):
            if candidate.exists():
                state_path = candidate
                break
    if state_path is None or not state_path.exists():
        say(f"어디에 넣었는지 적힌 {STATE_NAME} 을 찾지 못했습니다. --state 로 알려 주세요.")
        say("  (정리한 결과 폴더 안 _findpic 폴더에 있습니다)")
        return 1

    documents = read_state(state_path)
    if not documents:
        say(f"{state_path} 를 읽지 못했습니다.")
        return 1

    lookup = {}
    for entry in documents:
        for photo in entry.get("사진", []):
            lookup[(entry.get("문서", ""), photo.get("이름", ""))] = (entry, photo)

    changed = missed = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in _csv.DictReader(fh):
            key = ((row.get("한글파일") or "").strip(), (row.get("사진이름") or "").strip())
            found = lookup.get(key)
            if found is None:
                say(f"  · 어디에 넣었는지 알 수 없어 건너뜁니다: {key[0]} / {key[1]}")
                missed += 1
                continue
            entry, photo = found
            source = (row.get("가져올파일") or "").strip()
            target = Path(photo.get("넣은파일") or "")
            try:
                if source:
                    src = Path(clean_path(source))
                    if not src.exists():
                        say(f"  · 원본이 없습니다: {src}")
                        missed += 1
                        continue
                    new_target = target.with_suffix(src.suffix or target.suffix)
                    if args.dry_run:
                        say(f"  (계획) {new_target.name} <- {src}")
                    else:
                        if target.exists() and target != new_target:
                            target.unlink()
                        import shutil as _shutil
                        _shutil.copy2(src, new_target)
                        photo["넣은파일"] = str(new_target)
                        photo["종류"] = "원본"
                        photo["판정"] = "사람이 고름"
                else:
                    # '맞는 원본이 없음' 을 고른 경우: 문서 안 저용량 사진으로 되돌린다
                    doc = extract(entry.get("문서경로", ""))
                    item = doc.bin_items.get(photo.get("번호"))
                    if item is None or not item.data:
                        say(f"  · 문서에서 사진을 꺼내지 못했습니다: {key[1]}")
                        missed += 1
                        continue
                    new_target = target.with_suffix("." + (item.ext or "jpg"))
                    if args.dry_run:
                        say(f"  (계획) {new_target.name} <- {doc.path.name} 안의 저용량 사진")
                    else:
                        if target.exists() and target != new_target:
                            target.unlink()
                        new_target.parent.mkdir(parents=True, exist_ok=True)
                        new_target.write_bytes(item.data)
                        photo["넣은파일"] = str(new_target)
                        photo["종류"] = "저용량"
                        photo["판정"] = "사람이 고름"
                changed += 1
            except OSError as exc:
                say(f"  · 넣지 못했습니다 ({exc}): {key[1]}")
                missed += 1

    if not args.dry_run and changed:
        write_state(state_path, documents)
    say("")
    say(f"● {changed}건을 고쳐 넣었습니다." + (f" ({missed}건 실패)" if missed else ""))
    if args.dry_run:
        say("  --dry-run 이라 실제로 넣지는 않았습니다.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="findpic",
        description="한글(HWP/HWPX) 보고서 표에 들어간 축소 사진을 원본 사진 폴더에서 찾아 "
                    "표의 이름대로 바꿔 정리합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예

  # 대화형 (아무 것도 안 쓰고 그냥 실행)
  findpic

  # 한 번에 처리
  findpic --hwp "D:\\보고서" --source "E:\\사진원본" --dest "D:\\정리"

  # 무엇이 들어 있는지만 보기
  findpic scan --hwp "D:\\보고서\\대전_026.hwp"

  # 실제로 넣지 않고 계획만 보기
  findpic --hwp "D:\\보고서" --source "E:\\사진원본" --dest "D:\\정리" --dry-run
""")
    p.add_argument("--version", action="version", version=f"findpic {__version__}")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("--hwp", action="append", default=[],
                        help="한글 파일 또는 한글 파일들이 든 폴더 (여러 번 쓸 수 있음)")
        sp.add_argument("--no-recursive", action="store_true",
                        help="폴더를 줄 때 하위 폴더까지 뒤지지 않음")
        sp.add_argument("--quiet", "-q", action="store_true", help="진행 표시 끄기")
        sp.add_argument("--workers", type=int, default=default_workers(),
                        help=f"동시에 읽을 사진 수 (기본 {default_workers()}, 이 컴퓨터의 코어 수 기준)")
        sp.add_argument("--no-fast-index", action="store_true",
                        help="사진기가 넣어 둔 EXIF 축소판을 쓰지 않고 사진 파일 전체를 읽음 "
                             "(느리지만, 사진을 편집해 축소판이 옛것으로 남은 경우 안전함)")
        sp.add_argument("--cache", help="색인 파일 위치 (기본: 결과 폴더 안 .findpic)")
        sp.add_argument("--include-floating", action="store_true",
                        help="표 밖에 따로 놓인 사진도 대상에 넣기 (표지·로고까지 딸려올 수 있음)")

    def source_dest(sp):
        sp.add_argument("--source", action="append", default=[],
                        help="원본 사진이 있는 폴더 (여러 번 쓸 수 있음)")
        sp.add_argument("--dest", help="정리해 넣을 폴더")

    sp_scan = sub.add_parser("scan", help="한글 파일에 어떤 사진과 이름이 있는지만 보기")
    common(sp_scan)

    sp_index = sub.add_parser("index", help="원본 사진 색인만 미리 만들어 두기")
    common(sp_index)
    source_dest(sp_index)

    sp_apply = sub.add_parser("apply", help="검토 리포트에서 고친 것을 다시 넣기")
    sp_apply.add_argument("--csv", help="검토 리포트에서 내려받은 수정목록.csv")
    sp_apply.add_argument("--state", help=f"결과 폴더 안 _findpic/{STATE_NAME} 위치")
    sp_apply.add_argument("--dry-run", action="store_true", help="실제로 넣지 않고 계획만 보기")
    sp_apply.add_argument("--quiet", "-q", action="store_true")

    sp_run = sub.add_parser("run", help="찾아서 정리하기 (기본 동작)")
    common(sp_run)
    source_dest(sp_run)
    _run_options(sp_run)

    common(p)
    source_dest(p)
    _run_options(p)
    return p


def _run_options(sp) -> None:
    sp.add_argument("--name", default="{이름}",
                    help="파일 이름 서식. 쓸 수 있는 것: {이름} {구역} {번호} {파일명} {도면명칭} "
                         "그리고 문서 표의 머리글 이름. 기본값 '{이름}'")
    sp.add_argument("--flat", action="store_true",
                    help="문서별 폴더로 나누지 않고 결과 폴더에 바로 넣기")
    sp.add_argument("--no-create-missing", action="store_true",
                    help="맞는 폴더가 없으면 새로 만들지 말고 건너뛰기")
    sp.add_argument("--lowres", choices=[LOWRES_PLAIN, LOWRES_SUFFIX, LOWRES_SUBDIR, LOWRES_SKIP],
                    default=LOWRES_PLAIN,
                    help="원본을 못 찾았을 때 한글 파일 안의 저용량 사진을 어떻게 넣을지. "
                         "plain=같은 이름으로(기본) / suffix=이름 뒤에 (저용량) 표시 / "
                         "subdir=_저용량 폴더에 / skip=넣지 않음")
    sp.add_argument("--strict", action="store_true",
                    help="'확인 필요' 는 원본으로 치지 않기")
    sp.add_argument("--allow-duplicate", action="store_true",
                    help="한 원본이 여러 사진에 겹쳐 쓰이는 것을 허용")
    sp.add_argument("--overwrite", action="store_true", help="같은 이름 파일 덮어쓰기")
    sp.add_argument("--dry-run", action="store_true", help="실제로 넣지 않고 계획만 보기")
    sp.add_argument("--report", help="검토 리포트 HTML 저장 위치")
    sp.add_argument("--no-report", action="store_true", help="검토 리포트 만들지 않기")
    sp.add_argument("--thumbs", choices=["auto", "all", "attention"], default="auto",
                    help="리포트에 썸네일을 얼마나 넣을지 (기본 auto)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "run" and not args.hwp and sys.stdin.isatty():
        try:
            args = interactive(args)
        except (EOFError, KeyboardInterrupt):
            say("\n취소했습니다.")
            return 130

    try:
        if command == "scan":
            return cmd_scan(args)
        if command == "index":
            return cmd_index(args)
        if command == "apply":
            return cmd_apply(args)
        return cmd_run(args)
    except KeyboardInterrupt:
        say("\n\n중단했습니다. 지금까지 만든 색인은 남아 있으니 다시 실행하면 이어서 합니다.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

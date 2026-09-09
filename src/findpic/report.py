"""사람이 눈으로 확인하는 HTML 리포트.

파일 하나만 열면 되도록 썸네일까지 안에 넣는다. 인터넷 연결도, 별도 파일도
필요 없다. 웹에 올리는 게 아니라 결과 폴더에 저장되는 로컬 파일이다.
"""

from __future__ import annotations

import base64
import html
import io
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .imaging.loader import open_image
from .match import CERTAIN, NOT_FOUND, REVIEW, SlotMatch
from .model import HwpDocument
from .organize import PlacedFile

THUMB_SMALL = 150
THUMB_LARGE = 300


def thumbnail(source, size: int) -> str:
    """base64 로 박아 넣을 작은 JPEG. 못 만들면 빈 문자열."""
    image = open_image(source, target=size * 2)
    if image is None:
        return ""
    try:
        image = image.convert("RGB")
        image.thumbnail((size, size))
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=72, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""
    finally:
        try:
            image.close()
        except Exception:
            pass


@dataclass
class DocReport:
    doc: HwpDocument
    folder: Optional[Path]
    folder_how: str
    matches: Sequence[SlotMatch]
    placed: Sequence[PlacedFile]


_CSS = """
:root{--bg:#f7f7f8;--card:#fff;--ink:#1c1c1f;--dim:#6b6b73;--line:#e3e3e8;
      --ok:#0a7d3f;--warn:#a86400;--bad:#b02020;--okbg:#e8f6ee;--warnbg:#fdf3e0;--badbg:#fdeaea;}
@media (prefers-color-scheme:dark){
  :root{--bg:#17171a;--card:#212126;--ink:#ececed;--dim:#a0a0a8;--line:#33333a;
        --ok:#5fd08a;--warn:#f0b95a;--bad:#f08a8a;--okbg:#1b3527;--warnbg:#3a2f16;--badbg:#3a1f1f;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.6 -apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕","Apple SD Gothic Neo",
     "Noto Sans KR",sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:24px;margin:0 0 6px}
.sub{color:var(--dim);margin-bottom:22px}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:110px}
.stat b{display:block;font-size:22px;line-height:1.2}
.stat span{color:var(--dim);font-size:13px}
.filters{display:flex;gap:8px;margin:0 0 18px;flex-wrap:wrap}
.filters button{font:inherit;font-size:13px;padding:7px 14px;border-radius:999px;cursor:pointer;
                border:1px solid var(--line);background:var(--card);color:var(--ink)}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.doc{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:18px;overflow:hidden}
.doc>header{padding:14px 18px;border-bottom:1px solid var(--line)}
.doc h2{font-size:16px;margin:0 0 4px}
.doc .path{color:var(--dim);font-size:13px;word-break:break-all}
.row{display:grid;grid-template-columns:minmax(150px,1fr) 26px minmax(150px,1fr) minmax(220px,1.1fr);
     gap:14px;align-items:start;padding:16px 18px;border-top:1px solid var(--line)}
.row:first-of-type{border-top:none}
.cellhead{font-size:12px;color:var(--dim);margin-bottom:6px}
.name{font-weight:600;margin-bottom:4px;word-break:break-all}
img.shot{width:100%;height:auto;border-radius:8px;border:1px solid var(--line);background:var(--bg);display:block}
.arrow{align-self:center;text-align:center;color:var(--dim);font-size:20px}
.badge{display:inline-block;font-size:12px;padding:2px 9px;border-radius:999px;font-weight:600}
.b-확실{background:var(--okbg);color:var(--ok)}
.b-확인필요{background:var(--warnbg);color:var(--warn)}
.b-못찾음{background:var(--badbg);color:var(--bad)}
.meta{font-size:13px;color:var(--dim);margin-top:6px;word-break:break-all}
.meta code{font-size:12px}
.alts{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line)}
.alts .altrow{display:flex;gap:8px;align-items:center;margin-top:6px;font-size:12px;color:var(--dim)}
.alts img{width:56px;height:42px;object-fit:cover;border-radius:5px;border:1px solid var(--line)}
.note{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:8px 11px;font-size:13px;margin-top:8px}
.empty{padding:26px;text-align:center;color:var(--dim)}
"""

_JS = """
(function(){
  var buttons = document.querySelectorAll('.filters button');
  function apply(kind){
    document.querySelectorAll('.row').forEach(function(row){
      row.style.display = (kind === '전체' || row.dataset.verdict === kind) ? '' : 'none';
    });
    document.querySelectorAll('.doc').forEach(function(doc){
      var any = Array.prototype.some.call(doc.querySelectorAll('.row'), function(r){
        return r.style.display !== 'none';
      });
      doc.style.display = any ? '' : 'none';
    });
    buttons.forEach(function(b){ b.setAttribute('aria-pressed', b.dataset.kind === kind); });
  }
  buttons.forEach(function(b){ b.addEventListener('click', function(){ apply(b.dataset.kind); }); });
  apply('전체');
})();
"""


def _esc(text) -> str:
    return html.escape(str(text or ""))


def build(reports: Sequence[DocReport], *, title: str = "사진 원본 찾기 결과",
          thumbs: str = "auto") -> str:
    """thumbs: 'all' 전부 / 'attention' 확인 필요한 것만 / 'auto' 문서 수를 보고 결정."""
    if thumbs == "auto":
        thumbs = "all" if len(reports) <= 60 else "attention"

    counts = {CERTAIN: 0, REVIEW: 0, NOT_FOUND: 0}
    lowres = 0
    for rep in reports:
        for rec in rep.placed:
            counts[rec.verdict] = counts.get(rec.verdict, 0) + 1
            if rec.origin != "원본" and not rec.skipped:
                lowres += 1
    total = sum(counts.values())

    parts = [
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body><div class='wrap'>",
        f"<h1>{_esc(title)}</h1>",
        f"<div class='sub'>한글 파일 {len(reports)}개 · 사진 {total}장</div>",
        "<div class='summary'>",
        f"<div class='stat'><b>{counts.get(CERTAIN,0)}</b><span>원본 확실</span></div>",
        f"<div class='stat'><b>{counts.get(REVIEW,0)}</b><span>확인 필요</span></div>",
        f"<div class='stat'><b>{counts.get(NOT_FOUND,0)}</b><span>못 찾음</span></div>",
        f"<div class='stat'><b>{lowres}</b><span>저용량으로 대체</span></div>",
        "</div>",
        "<div class='filters'>",
    ]
    for kind in ("전체", REVIEW, NOT_FOUND, CERTAIN):
        parts.append(f"<button data-kind='{_esc(kind)}' aria-pressed='false'>{_esc(kind)}</button>")
    parts.append("</div>")

    for rep in reports:
        parts.append("<section class='doc'><header>")
        parts.append(f"<h2>{_esc(rep.doc.path.name)}</h2>")
        where = f"{rep.folder} · {rep.folder_how}" if rep.folder else rep.folder_how
        parts.append(f"<div class='path'>넣은 곳: {_esc(where)}</div>")
        parts.append("</header>")

        for rec, match in zip(rep.placed, rep.matches):
            big = thumbs == "all" or rec.verdict != CERTAIN
            size = THUMB_LARGE if big else THUMB_SMALL
            item = rep.doc.bin_items.get(rec.slot.bin_id)
            left = thumbnail(item.data, size) if item and item.data else ""
            right = ""
            if match.best:
                right = thumbnail(match.best.record.path, size)

            parts.append(f"<div class='row' data-verdict='{_esc(rec.verdict)}'>")
            parts.append("<div><div class='cellhead'>한글 파일 안</div>")
            parts.append(f"<div class='name'>{_esc(rec.slot.caption or '(이름 없음)')}</div>")
            if left:
                parts.append(f"<img class='shot' src='{left}' alt=''>")
            src_note = f"{rec.slot.group} · {rec.slot.caption_source}".strip(" ·")
            parts.append(f"<div class='meta'>{_esc(src_note)}</div></div>")
            parts.append("<div class='arrow'>&rarr;</div>")

            parts.append("<div><div class='cellhead'>찾은 원본</div>")
            if match.best:
                rec_src = Path(match.best.record.path)
                parts.append(f"<div class='name'>{_esc(rec_src.name)}</div>")
                if right:
                    parts.append(f"<img class='shot' src='{right}' alt=''>")
                r = match.best.record
                parts.append(
                    f"<div class='meta'>{r.width}×{r.height} · {r.size/1_048_576:.1f}MB"
                    + (f" · {_esc(r.model)}" if r.model else "")
                    + (f" · {_esc(r.taken_at)}" if r.taken_at else "")
                    + "</div>"
                )
            else:
                parts.append("<div class='meta'>원본을 찾지 못했습니다</div>")
            parts.append("</div>")

            parts.append("<div><div class='cellhead'>판정</div>")
            parts.append(f"<span class='badge b-{_esc(rec.verdict)}'>{_esc(rec.verdict)}</span>")
            parts.append(f"<div class='meta'>{_esc(rec.reason)}</div>")
            if rec.target:
                origin = "원본" if rec.origin == "원본" else "한글 파일 안의 저용량 사진"
                parts.append(
                    f"<div class='meta'>넣은 파일: <code>{_esc(rec.target.name)}</code><br>({_esc(origin)})</div>"
                )
            if rec.message:
                parts.append(f"<div class='note'>{_esc(rec.message)}</div>")
            if rec.skipped:
                parts.append(f"<div class='note'>{_esc(rec.skipped)}</div>")
            if match.runners_up and rec.verdict != CERTAIN:
                parts.append("<div class='alts'><div class='cellhead'>다음 후보</div>")
                for alt in match.runners_up[:3]:
                    thumb = thumbnail(alt.record.path, 90)
                    img = f"<img src='{thumb}' alt=''>" if thumb else ""
                    parts.append(
                        f"<div class='altrow'>{img}<span>{_esc(Path(alt.record.path).name)}"
                        f" · {alt.score * 100:.1f}점</span></div>"
                    )
                parts.append("</div>")
            parts.append("</div></div>")

        if not rep.placed:
            parts.append("<div class='empty'>이 문서에서는 표 안의 사진을 찾지 못했습니다.</div>")
        parts.append("</section>")

    parts.append(f"</div><script>{_JS}</script></body></html>")
    return "".join(parts)


def write(path: Path, reports: Sequence[DocReport], **kwargs) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(reports, **kwargs), encoding="utf-8")
    return path

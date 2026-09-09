"""사진에 붙일 최종 파일 이름 정하기."""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Sequence

# 윈도우에서 파일 이름에 쓸 수 없는 글자
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# 윈도우 예약 이름
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_STEM_BYTES = 150        # 경로 길이 여유를 두고 넉넉히 자른다


def _drop_lone_surrogates(text: str) -> str:
    """짝이 풀린 반쪽 문자를 뺀다.

    제대로 읽었다면 나올 일이 없지만, 하나라도 남으면 파일 이름으로 쓰는 순간
    UnicodeEncodeError 로 작업 전체가 멈춘다. 마지막 안전망이다.
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


def sanitize(name: str, *, fallback: str = "이름없음") -> str:
    """어느 운영체제에서도 안전한 파일 이름 조각으로 다듬는다."""
    name = _drop_lone_surrogates(name or "")
    name = unicodedata.normalize("NFC", name)
    name = name.replace("\n", " ").replace("\t", " ")
    name = _FORBIDDEN.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")        # 윈도우는 끝의 점·공백을 못 쓴다
    if not name:
        return fallback
    if name.upper() in _RESERVED or name.upper().split(".")[0] in _RESERVED:
        name = "_" + name
    encoded = name.encode("utf-8")
    if len(encoded) > MAX_STEM_BYTES:
        name = encoded[:MAX_STEM_BYTES].decode("utf-8", "ignore").rstrip()
    return name or fallback


def number_captions(captions: Sequence[str], *, fallback: str = "사진") -> List[str]:
    """같은 이름이 여러 번 나오면 (1) (2) 를 붙인다.

        ['항공사진', '전경', '근경', '근경', '유물사진']
        -> ['항공사진', '전경', '근경(1)', '근경(2)', '유물사진']

    한 번만 나오는 이름에는 번호를 붙이지 않는다.
    """
    cleaned = [sanitize(c, fallback=fallback) for c in captions]
    counts: Dict[str, int] = {}
    for c in cleaned:
        counts[c] = counts.get(c, 0) + 1
    seen: Dict[str, int] = {}
    out: List[str] = []
    for c in cleaned:
        if counts[c] == 1:
            out.append(c)
        else:
            seen[c] = seen.get(c, 0) + 1
            out.append(f"{c}({seen[c]})")
    return out


def apply_template(template: str, variables: Dict[str, str]) -> str:
    """'{도면명칭}_{이름}' 같은 서식을 채운다. 없는 변수는 빈 칸으로 둔다."""
    def repl(m):
        key = m.group(1).strip()
        return sanitize(str(variables.get(key, "")), fallback="")

    out = re.sub(r"\{([^{}]+)\}", repl, template)
    out = re.sub(r"[ _\-]{2,}", lambda m: m.group(0)[0], out)
    return sanitize(out.strip(" _-"))


def unique_path(directory, stem: str, suffix: str):
    """이미 있는 파일을 덮어쓰지 않도록 뒤에 -2, -3 을 붙인다."""
    from pathlib import Path

    directory = Path(directory)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    for i in range(2, 1000):
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"이름을 정할 수 없습니다: {directory}/{stem}{suffix}")

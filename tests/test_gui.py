"""창(GUI) 이 무거운 것을 미리 불러오지 않는지, 설정을 잘 다루는지."""

import ast
import importlib.util
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "findpic"

# tkinter 는 파이썬에 기본으로 들어 있지만, 일부 리눅스는 따로 설치해야 한다.
# 창을 실제로 불러오는 검사만 건너뛰고 나머지는 그대로 돌린다.
needs_tk = pytest.mark.skipif(
    importlib.util.find_spec("tkinter") is None,
    reason="이 파이썬에는 tkinter 가 없습니다 (리눅스라면 python3-tk 를 설치하세요)",
)


def _module_imports(path: Path):
    """모듈 맨 위에서 import 하는 이름들 (함수 안의 import 는 뺀다)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_창은_무거운_것을_미리_부르지_않는다():
    """Pillow·numpy 를 창 뜨기 전에 부르면 더블클릭 후 몇 초씩 멈춰 보인다."""
    top = _module_imports(SRC / "gui.py")
    assert "PIL" not in top and "numpy" not in top and "olefile" not in top
    assert "tkinter" in top


def test_창은_명령줄_모듈에_기대지_않는다():
    """창과 명령줄이 같은 파이프라인을 쓰되 서로를 부르지는 않아야 한다."""
    assert "cli" not in _module_imports(SRC / "gui.py")
    assert "cli" not in _module_imports(SRC / "pipeline.py")


def test_창_모듈은_화면_없이도_불러진다():
    """exe 로 묶을 때 import 만으로 창이 뜨면 안 된다."""
    import importlib.util

    spec = importlib.util.find_spec("findpic.gui")
    assert spec is not None


@needs_tk
def test_저용량_선택지가_파이프라인_값과_맞는다():
    from findpic.gui import LOWRES_CHOICES
    from findpic.organize import (LOWRES_PLAIN, LOWRES_SKIP, LOWRES_SUBDIR,
                                  LOWRES_SUFFIX)

    values = {value for _label, value in LOWRES_CHOICES}
    assert values == {LOWRES_PLAIN, LOWRES_SUFFIX, LOWRES_SUBDIR, LOWRES_SKIP}


def test_판정_이름이_창과_맞는다():
    """창은 결과를 문자열 열쇠로 읽는다. 이름이 바뀌면 0장으로 보인다."""
    from findpic.match import CERTAIN, NOT_FOUND, REVIEW

    assert (CERTAIN, REVIEW, NOT_FOUND) == ("확실", "확인필요", "못찾음")


def test_묶음용_시작점이_창을_부른다():
    entry = Path(__file__).resolve().parents[1] / "packaging" / "entry_gui.py"
    source = entry.read_text(encoding="utf-8")
    assert "findpic.gui" in source
    assert "freeze_support" in source          # 윈도우에서 자기 자신을 다시 띄우지 않게

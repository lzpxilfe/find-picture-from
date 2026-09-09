"""exe 로 묶을 때의 시작점.

PyInstaller 는 모듈이 아니라 스크립트 파일을 받으므로 얇은 껍데기를 둔다.
"""

import multiprocessing
import sys


def main() -> int:
    # 윈도우에서 exe 가 자기 자신을 다시 띄우는 것을 막는다
    multiprocessing.freeze_support()
    from findpic.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())

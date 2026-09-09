"""exe 로 묶을 때의 시작점.

PyInstaller 는 모듈이 아니라 스크립트 파일을 받으므로 얇은 껍데기를 둔다.
검은 명령창 없이 도니까, 뜨기도 전에 잘못되면 사용자는 아무것도 못 본다.
그래서 시작 단계의 오류는 반드시 창으로 알려 준다.
"""

import multiprocessing
import sys
import traceback


def _show_error(message: str) -> None:
    """창이 뜨기 전에 잘못됐을 때 그 이유를 보여 준다."""
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext

        root = tk.Tk()
        root.title("사진찾기 — 시작하지 못했습니다")
        root.geometry("760x420")
        tk.Label(root, text="프로그램을 시작하지 못했습니다. 아래 내용을 알려 주세요.",
                 anchor="w").pack(fill="x", padx=12, pady=(12, 4))
        box = scrolledtext.ScrolledText(root, wrap="word")
        box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        box.insert("1.0", message)
        tk.Button(root, text="내용 복사",
                  command=lambda: (root.clipboard_clear(), root.clipboard_append(message),
                                   messagebox.showinfo("사진찾기", "복사했습니다."))
                  ).pack(pady=(0, 12))
        root.mainloop()
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    # 윈도우에서 exe 가 자기 자신을 다시 띄우는 것을 막는다
    multiprocessing.freeze_support()
    try:
        from findpic.gui import main as gui_main
    except Exception:
        _show_error("필요한 부분을 불러오지 못했습니다.\n\n" + traceback.format_exc())
        return 1
    try:
        return gui_main()
    except Exception:
        _show_error("실행 중에 멈췄습니다.\n\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())

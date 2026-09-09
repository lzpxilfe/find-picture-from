"""창으로 쓰는 findpic.

명령 프롬프트를 모르는 사람도 더블클릭해서 쓸 수 있게 만든 화면이다.
파이썬에 기본으로 들어 있는 tkinter 만 쓰므로 따로 설치할 것이 없다.

무거운 것들(Pillow, numpy, 파서)은 '시작' 을 누른 뒤에야 불러온다.
그래야 창이 곧바로 뜬다.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "한글 보고서 사진 원본 찾기"
SETTINGS_PATH = Path.home() / ".findpic" / "창설정.json"

LOWRES_CHOICES = [
    ("같은 이름으로 그대로 넣기", "plain"),
    ("이름 뒤에 (저용량) 표시하기", "suffix"),
    ("_저용량 폴더에 따로 넣기", "subdir"),
    ("넣지 않기", "skip"),
]


def open_in_explorer(path) -> None:
    """탐색기(또는 파인더)로 열기."""
    path = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                                   # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.result = None

        root.title(APP_TITLE)
        root.minsize(760, 620)
        self._build()
        self._load_settings()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._drain)

    # -- 화면 만들기 -----------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Label(head, text=APP_TITLE, font=("", 15, "bold")).pack(anchor="w")
        ttk.Label(
            head,
            text="한글 파일의 표에 들어간 사진을 찾아, 원본 사진 폴더에서 같은 사진을 찾아,\n"
                 "표에 적힌 이름으로 바꿔 폴더에 넣습니다. 원본 사진은 건드리지 않고 복사만 합니다.",
            foreground="#555",
        ).pack(anchor="w", pady=(4, 0))

        # 입력 세 가지
        box = ttk.LabelFrame(outer, text=" 무엇을, 어디서, 어디로 ")
        box.grid(row=1, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="1. 한글 파일 또는 폴더").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        self.hwp_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.hwp_var).grid(row=0, column=1, sticky="ew", padx=6, pady=(10, 2))
        btns = ttk.Frame(box)
        btns.grid(row=0, column=2, padx=(0, 10), pady=(10, 2))
        ttk.Button(btns, text="폴더 선택", width=9, command=self._pick_hwp_dir).pack(side="left", padx=2)
        ttk.Button(btns, text="파일 선택", width=9, command=self._pick_hwp_file).pack(side="left", padx=2)

        ttk.Label(box, text="2. 원본 사진 폴더").grid(row=1, column=0, sticky="nw", padx=10, pady=2)
        src_frame = ttk.Frame(box)
        src_frame.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        src_frame.columnconfigure(0, weight=1)
        self.source_list = tk.Listbox(src_frame, height=3, activestyle="none")
        self.source_list.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(src_frame, orient="vertical", command=self.source_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.source_list.configure(yscrollcommand=sb.set)
        src_btns = ttk.Frame(box)
        src_btns.grid(row=1, column=2, padx=(0, 10), pady=2, sticky="n")
        ttk.Button(src_btns, text="추가", width=9, command=self._add_source).pack(pady=1)
        ttk.Button(src_btns, text="빼기", width=9, command=self._remove_source).pack(pady=1)

        ttk.Label(box, text="3. 정리해 넣을 폴더").grid(row=2, column=0, sticky="w", padx=10, pady=(2, 12))
        self.dest_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.dest_var).grid(row=2, column=1, sticky="ew", padx=6, pady=(2, 12))
        ttk.Button(box, text="폴더 선택", width=9, command=self._pick_dest).grid(
            row=2, column=2, padx=(0, 10), pady=(2, 12), sticky="w")

        # 설정
        opts = ttk.LabelFrame(outer, text=" 어떻게 넣을까요 ")
        opts.grid(row=2, column=0, sticky="ew", **pad)
        opts.columnconfigure(1, weight=1)

        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="실제로 넣지 않고 무엇이 어디로 갈지만 확인 (미리보기)",
                        variable=self.dry_var).grid(row=0, column=0, columnspan=3, sticky="w",
                                                    padx=10, pady=(10, 2))
        self.strict_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="'확인 필요' 로 판정된 것은 원본으로 치지 않기",
                        variable=self.strict_var).grid(row=1, column=0, columnspan=3, sticky="w",
                                                       padx=10, pady=2)
        self.floating_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="표 밖에 따로 놓인 사진도 대상에 넣기 (표지·로고까지 딸려올 수 있음)",
                        variable=self.floating_var).grid(row=2, column=0, columnspan=3, sticky="w",
                                                         padx=10, pady=2)
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="이미 있는 파일 덮어쓰기 (다시 돌려 고쳐 넣을 때 켜세요)",
                        variable=self.overwrite_var).grid(row=3, column=0, columnspan=3, sticky="w",
                                                          padx=10, pady=2)

        ttk.Label(opts, text="원본을 못 찾으면").grid(row=4, column=0, sticky="w", padx=10, pady=(6, 12))
        self.lowres_var = tk.StringVar(value=LOWRES_CHOICES[0][0])
        combo = ttk.Combobox(opts, textvariable=self.lowres_var, state="readonly",
                             values=[label for label, _ in LOWRES_CHOICES], width=32)
        combo.grid(row=4, column=1, sticky="w", padx=6, pady=(6, 12))

        # 진행
        run = ttk.Frame(outer)
        run.grid(row=3, column=0, sticky="nsew", **pad)
        run.columnconfigure(0, weight=1)
        run.rowconfigure(2, weight=1)

        bar_row = ttk.Frame(run)
        bar_row.grid(row=0, column=0, sticky="ew")
        bar_row.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(bar_row, text="시작", width=14, command=self._start)
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(bar_row, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=1, sticky="ew", padx=10)
        self.stop_btn = ttk.Button(bar_row, text="중단", width=8, command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=2, sticky="e")

        self.status_var = tk.StringVar(value="준비됨")
        ttk.Label(run, textvariable=self.status_var, foreground="#555").grid(
            row=1, column=0, sticky="w", pady=(6, 4))

        log_frame = ttk.Frame(run)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, wrap="word", state="disabled",
                           background="#fbfbfc", relief="solid", borderwidth=1)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_sb.set)
        self.log.tag_configure("head", font=("", 10, "bold"))
        self.log.tag_configure("warn", foreground="#a86400")
        self.log.tag_configure("error", foreground="#b02020")
        self.log.tag_configure("done", foreground="#0a7d3f", font=("", 10, "bold"))

        tail = ttk.Frame(outer)
        tail.grid(row=4, column=0, sticky="ew", **pad)
        self.report_btn = ttk.Button(tail, text="검토 리포트 열기", command=self._open_report,
                                     state="disabled")
        self.report_btn.pack(side="left")
        self.folder_btn = ttk.Button(tail, text="결과 폴더 열기", command=self._open_dest,
                                     state="disabled")
        self.folder_btn.pack(side="left", padx=8)

    # -- 입력 도우미 -----------------------------------------------------
    def _pick_hwp_dir(self) -> None:
        path = filedialog.askdirectory(title="한글 파일들이 든 폴더를 고르세요")
        if path:
            self.hwp_var.set(path)

    def _pick_hwp_file(self) -> None:
        path = filedialog.askopenfilename(
            title="한글 파일을 고르세요",
            filetypes=[("한글 문서", "*.hwp *.hwpx"), ("모든 파일", "*.*")])
        if path:
            self.hwp_var.set(path)

    def _add_source(self) -> None:
        path = filedialog.askdirectory(title="원본 사진이 든 폴더를 고르세요")
        if path and path not in self.source_list.get(0, "end"):
            self.source_list.insert("end", path)

    def _remove_source(self) -> None:
        for i in reversed(self.source_list.curselection()):
            self.source_list.delete(i)

    def _pick_dest(self) -> None:
        path = filedialog.askdirectory(title="정리해 넣을 폴더를 고르세요")
        if path:
            self.dest_var.set(path)

    # -- 설정 기억 -------------------------------------------------------
    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.hwp_var.set(data.get("hwp", ""))
        self.dest_var.set(data.get("dest", ""))
        for path in data.get("source", []):
            self.source_list.insert("end", path)
        self.lowres_var.set(data.get("lowres", LOWRES_CHOICES[0][0]))

    def _save_settings(self) -> None:
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps({
                "hwp": self.hwp_var.get(),
                "dest": self.dest_var.get(),
                "source": list(self.source_list.get(0, "end")),
                "lowres": self.lowres_var.get(),
            }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # -- 실행 ------------------------------------------------------------
    def _say(self, text: str, kind: str = "info") -> None:
        self.log.configure(state="normal")
        prefix = "\n● " if kind == "head" else "   "
        self.log.insert("end", f"{prefix}{text}\n", kind)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        hwp = self.hwp_var.get().strip()
        sources = [s for s in self.source_list.get(0, "end") if s.strip()]
        dest = self.dest_var.get().strip()
        if not hwp or not sources or not dest:
            messagebox.showwarning(APP_TITLE, "세 칸을 모두 채워 주세요.\n\n"
                                              "1. 한글 파일 또는 폴더\n"
                                              "2. 원본 사진 폴더 (추가 단추)\n"
                                              "3. 정리해 넣을 폴더")
            return
        self._save_settings()

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress.configure(value=0)
        self.result = None
        self.report_btn.configure(state="disabled")
        self.folder_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("준비하는 중입니다...")
        self.stop_flag.clear()

        lowres = dict(LOWRES_CHOICES).get(self.lowres_var.get(), "plain")
        params = {
            "hwp": [hwp], "source": sources, "dest": dest,
            "lowres": lowres, "dry_run": self.dry_var.get(),
            "strict": self.strict_var.get(), "include_floating": self.floating_var.get(),
            "overwrite": self.overwrite_var.get(),
        }
        self.worker = threading.Thread(target=self._work, args=(params,), daemon=True)
        self.worker.start()

    def _work(self, params: dict) -> None:
        try:
            from .pipeline import JobConfig, run_job

            config = JobConfig(**params)
            result = run_job(
                config,
                on_message=lambda text, kind="info": self.events.put(("msg", text, kind)),
                on_progress=lambda stage, done, total, note="":
                    self.events.put(("prog", stage, done, total, note)),
                should_stop=self.stop_flag.is_set,
            )
            self.events.put(("result", result))
        except Exception:
            self.events.put(("msg", traceback.format_exc(), "error"))
            self.events.put(("result", None))

    def _stop(self) -> None:
        self.stop_flag.set()
        self.status_var.set("멈추는 중입니다. 하던 사진 하나를 마칠 때까지 기다려 주세요...")
        self.stop_btn.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "msg":
                    self._say(event[1], event[2])
                elif event[0] == "prog":
                    _kind, stage, done, total, note = event
                    pct = (done / total * 100) if total else 0
                    self.progress.configure(value=pct)
                    tail = f" · {note}" if note else ""
                    self.status_var.set(f"{stage}  {done:,} / {total:,}{tail}")
                elif event[0] == "result":
                    self._finish(event[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _finish(self, result) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.result = result
        if result is None:
            self.status_var.set("오류로 멈췄습니다.")
            return
        if result.cancelled:
            self.status_var.set("중단했습니다. 다시 실행하면 이어서 합니다.")
            self.progress.configure(value=0)
            return
        if not result.ok:
            self.status_var.set(result.error or "끝내지 못했습니다.")
            messagebox.showerror(APP_TITLE, result.error or "끝내지 못했습니다.")
            return

        self.progress.configure(value=100)
        certain = result.counts.get("확실", 0)
        review = result.counts.get("확인필요", 0)
        missing = result.counts.get("못찾음", 0)
        self._say(f"끝났습니다. 원본 확실 {certain:,}장 · 확인 필요 {review:,}장 · "
                  f"못 찾음 {missing:,}장", "done")
        if result.lowres_used:
            self._say(f"원본을 못 찾은 {result.lowres_used:,}장은 한글 파일 안의 "
                      "저용량 사진을 넣었습니다.", "warn")
        if result.skipped_documents:
            self._say(f"건너뛴 한글 파일 {len(result.skipped_documents)}개: "
                      + ", ".join(result.skipped_documents[:5])
                      + (" ..." if len(result.skipped_documents) > 5 else ""), "warn")
        self.status_var.set(f"끝났습니다 ({int(result.elapsed)}초)")
        if result.report_path:
            self.report_btn.configure(state="normal")
        if result.dest_root:
            self.folder_btn.configure(state="normal")
        if review or missing:
            self._say("'확인 필요' 와 '못 찾음' 은 검토 리포트에서 눈으로 확인해 주세요.", "warn")

    def _open_report(self) -> None:
        if self.result and self.result.report_path:
            open_in_explorer(self.result.report_path)

    def _open_dest(self) -> None:
        if self.result and self.result.dest_root:
            open_in_explorer(self.result.dest_root)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(APP_TITLE, "아직 일하는 중입니다. 정말 닫을까요?"):
                return
            self.stop_flag.set()
        self._save_settings()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.3)
    except tk.TclError:
        pass
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

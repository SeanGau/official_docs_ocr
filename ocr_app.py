#!/usr/bin/env python3
"""公文 PDF -> Markdown 地端版桌面程式（Tk）。

開啟時先離線檢查模型檔（model_manifest.json 固定版本與大小）：完整就完全離線執行；
缺檔才顯示需要下載的大小，按下載後在子程序以 huggingface_hub 下載並顯示進度。
OCR 與 ocr_doc_local.py 共用同一套程式與輸出格式（<檔名>.local.md）。

    uv run --extra local ocr_app.py [PDF 或資料夾…]
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import model_store

APP_TITLE = "公文 OCR（地端版）"
DATA_DIR = model_store.app_data_dir()
LOG_PATH = DATA_DIR / "app.log"
GB = 1024**3


class _Stopped(Exception):
    pass


def _redirect_stdio() -> None:
    """windowed exe 沒有 stdout/stderr；模型載入的 print、tqdm、logging 改寫到 app.log。"""
    if sys.stdout is not None and sys.stderr is not None and not getattr(sys, "frozen", False):
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(LOG_PATH, "a", encoding="utf-8", buffering=1)


def _download_worker(store: str, events) -> None:
    """子程序：下載模型。GUI 程序維持離線模式，取消時直接結束這個程序。"""
    _redirect_stdio()
    os.environ.pop("HF_HUB_OFFLINE", None)
    last = 0.0

    def on_bytes(n: int) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last >= 0.25:
            last = now
            events.put(("bytes", n))

    try:
        model_store.download(Path(store), on_bytes)
        events.put(("done", None))
    except Exception as e:
        traceback.print_exc()
        events.put(("error", f"{type(e).__name__}: {e}"))


def _fmt_gb(n: int) -> str:
    return f"{n / GB:.2f} GB"


class App(tk.Tk):
    def __init__(self, initial: list[str]) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(720, 560)
        self.events: queue.Queue = queue.Queue()
        self.store: Path | None = None
        self.runtime = None
        self.stop_event = threading.Event()
        self.ocr_thread: threading.Thread | None = None
        self.download_proc = None
        self.download_events = None
        self.download_total = 0
        self.download_started = 0.0
        self.pdfs: list[Path] = []

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._add_paths([Path(p) for p in initial])
        self.after(0, self.check_model)
        threading.Thread(target=self._detect_device, daemon=True).start()
        self.after(100, self._drain_events)

    # ---------- 版面 ----------
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)

        res = ttk.LabelFrame(self, text="離線資源檢查")
        res.grid(row=0, column=0, sticky="ew", **pad)
        res.columnconfigure(0, weight=1)
        self.model_label = ttk.Label(res, text="檢查中…", wraplength=660, justify="left")
        self.model_label.grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        self.device_label = ttk.Label(res, text="運算裝置：偵測中…")
        self.device_label.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        self.download_bar = ttk.Progressbar(res, maximum=1000)
        self.download_bar.grid(row=2, column=0, sticky="ew", **pad)
        self.download_btn = ttk.Button(res, text="下載模型", command=self.start_download)
        self.download_btn.grid(row=2, column=1, **pad)
        self.cancel_download_btn = ttk.Button(res, text="取消下載", command=self.cancel_download)
        self.cancel_download_btn.grid(row=2, column=2, **pad)
        self.download_bar.grid_remove()
        self.download_btn.grid_remove()
        self.cancel_download_btn.grid_remove()

        files = ttk.LabelFrame(self, text="PDF 檔案")
        files.grid(row=1, column=0, sticky="nsew", **pad)
        files.columnconfigure(0, weight=1)
        files.rowconfigure(0, weight=1)
        self.file_list = tk.Listbox(files, selectmode="extended", height=6)
        self.file_list.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=4)
        scroll = ttk.Scrollbar(files, command=self.file_list.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=4)
        self.file_list.configure(yscrollcommand=scroll.set)
        buttons = ttk.Frame(files)
        buttons.grid(row=0, column=2, sticky="n", **pad)
        for text, command in (
            ("加入檔案…", self.pick_files),
            ("加入資料夾…", self.pick_folder),
            ("移除選取", self.remove_selected),
            ("全部清除", self.clear_files),
        ):
            ttk.Button(buttons, text=text, command=command).pack(fill="x", pady=2)

        opts = ttk.Frame(self)
        opts.grid(row=2, column=0, sticky="ew", **pad)
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="輸出資料夾").grid(row=0, column=0, sticky="w")
        self.outdir = tk.StringVar()
        ttk.Entry(opts, textvariable=self.outdir).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(opts, text="選擇…", command=self.pick_outdir).grid(row=0, column=2)
        self.with_meta = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="抽取 metadata（發文字號、發文日期、主旨等）", variable=self.with_meta,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        run = ttk.Frame(self)
        run.grid(row=3, column=0, sticky="ew", **pad)
        run.columnconfigure(3, weight=1)
        self.start_btn = ttk.Button(run, text="開始轉換", command=self.start_ocr, state="disabled")
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(run, text="停止", command=self.stop_ocr, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=6)
        ttk.Button(run, text="開啟輸出資料夾", command=self.open_outdir).grid(row=0, column=2)
        self.ocr_status = ttk.Label(run, text="")
        self.ocr_status.grid(row=0, column=3, sticky="w", padx=8)
        self.ocr_bar = ttk.Progressbar(run, maximum=1000)
        self.ocr_bar.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        logf = ttk.LabelFrame(self, text="紀錄")
        logf.grid(row=4, column=0, sticky="nsew", **pad)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log_text = tk.Text(logf, height=8, state="disabled", wrap="word", font="TkDefaultFont")
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=4)
        log_scroll = ttk.Scrollbar(logf, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns", pady=4)
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- 離線資源 ----------
    def check_model(self) -> None:
        self.store = model_store.find_ready_store()
        if self.store:
            self.model_label.configure(
                text=f"✔ 離線資源完整，不需要網路。\n模型位置：{self.store}", foreground="#1a7f37",
            )
            self.download_btn.grid_remove()
            self.log(f"離線檢查通過：{self.store}")
        else:
            missing = model_store.find_missing(model_store.download_store())
            self.download_total = sum(m.size for m in missing)
            self.model_label.configure(
                text=(
                    f"✘ 缺少模型檔 {len(missing)} 個，需要連網下載 {_fmt_gb(self.download_total)}。\n"
                    f"下載一次後即可完全離線使用，存放位置：{model_store.download_store()}"
                ),
                foreground="#cf222e",
            )
            self.download_btn.grid()
            self.log(f"離線檢查：缺少 {len(missing)} 個模型檔（{_fmt_gb(self.download_total)}）")
        self._update_start_state()

    def start_download(self) -> None:
        store = model_store.download_store()
        try:
            store.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"無法建立下載資料夾 {store}：{e}")
            return
        ctx = multiprocessing.get_context("spawn")
        self.download_events = ctx.Queue()
        self.download_proc = ctx.Process(
            target=_download_worker, args=(str(store), self.download_events), daemon=True,
        )
        self.download_proc.start()
        self.download_started = time.monotonic()
        self.download_bar.configure(value=0)
        self.download_bar.grid()
        self.download_btn.grid_remove()
        self.cancel_download_btn.grid()
        self.model_label.configure(
            text=f"下載中… 0.00 / {_fmt_gb(self.download_total)}（本次需下載的量；連線中）", foreground="",
        )
        self.log(f"開始下載模型到 {store}")
        self.after(200, self._poll_download)

    def _poll_download(self) -> None:
        if self.download_proc is None:
            return
        finished = error = None
        try:
            while True:
                kind, value = self.download_events.get_nowait()
                if kind == "bytes":
                    self._show_download_progress(value)
                elif kind == "done":
                    finished = True
                else:
                    error = value
        except queue.Empty:
            pass
        if not finished and not error and self.download_proc.is_alive():
            self.after(200, self._poll_download)
            return
        if not finished and not error:
            error = f"下載程序意外結束（exit code {self.download_proc.exitcode}），詳見 {LOG_PATH}"
        self._end_download()
        if error:
            self.log(f"下載失敗：{error}")
            messagebox.showerror(
                APP_TITLE, f"下載失敗，請確認網路後再試一次（已完成的檔案不會重新下載）。\n\n{error}",
            )
        else:
            self.log("下載完成")
        self.check_model()

    def _show_download_progress(self, done: int) -> None:
        total = max(self.download_total, 1)
        elapsed = time.monotonic() - self.download_started
        speed = done / elapsed if elapsed > 0 else 0
        # 位元組計數可能因檔案重建方式略超過預估；顯示時一律夾在總量以內。
        done = min(done, total)
        self.download_bar.configure(value=done / total * 1000)
        eta = f"，估計約剩 {(total - done) / speed / 60:.0f} 分鐘" if speed > 0 else ""
        self.model_label.configure(
            text=(
                f"下載中… {_fmt_gb(done)} / {_fmt_gb(total)}（本次需下載的量；"
                f"{speed / 1024**2:.1f} MB/s{eta}）"
            ),
        )

    def cancel_download(self) -> None:
        if self.download_proc and self.download_proc.is_alive():
            self.download_proc.terminate()
            self.download_proc.join(5)
        self._end_download()
        self.log("已取消下載；已完成的檔案會保留，下次只下載缺少的部分")
        self.check_model()

    def _end_download(self) -> None:
        self.download_proc = None
        self.download_bar.grid_remove()
        self.cancel_download_btn.grid_remove()

    def _detect_device(self) -> None:
        try:
            import torch

            if torch.cuda.is_available():
                text = f"運算裝置：GPU {torch.cuda.get_device_name(0)}（CUDA）"
            else:
                text = "運算裝置：未偵測到可用的 NVIDIA GPU，將使用 CPU（每頁可能要十分鐘以上）"
        except Exception as e:
            traceback.print_exc()
            text = f"運算裝置：偵測失敗（{e}）"
        self.events.put(("device", text))

    # ---------- 檔案 ----------
    def _add_paths(self, paths: list[Path]) -> None:
        for path in paths:
            found = (
                sorted(p for p in path.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
                if path.is_dir()
                else [path] if path.suffix.lower() == ".pdf" and path.is_file() else []
            )
            for pdf in found:
                pdf = pdf.resolve()
                if pdf not in self.pdfs:
                    self.pdfs.append(pdf)
                    self.file_list.insert("end", str(pdf))
        if self.pdfs and not self.outdir.get():
            self.outdir.set(str(self.pdfs[0].parent))
        self._update_start_state()

    def pick_files(self) -> None:
        names = filedialog.askopenfilenames(title="選擇 PDF", filetypes=[("PDF", "*.pdf")])
        self._add_paths([Path(n) for n in names])

    def pick_folder(self) -> None:
        if name := filedialog.askdirectory(title="選擇含 PDF 的資料夾"):
            self._add_paths([Path(name)])

    def remove_selected(self) -> None:
        for index in reversed(self.file_list.curselection()):
            self.file_list.delete(index)
            del self.pdfs[index]
        self._update_start_state()

    def clear_files(self) -> None:
        self.file_list.delete(0, "end")
        self.pdfs.clear()
        self._update_start_state()

    def pick_outdir(self) -> None:
        if name := filedialog.askdirectory(title="選擇輸出資料夾"):
            self.outdir.set(name)

    def open_outdir(self) -> None:
        outdir = self.outdir.get()
        if not outdir or not Path(outdir).is_dir():
            messagebox.showinfo(APP_TITLE, "輸出資料夾還不存在")
        elif sys.platform == "win32":
            os.startfile(outdir)
        else:
            subprocess.Popen(["xdg-open", outdir])

    # ---------- OCR ----------
    def _busy(self) -> bool:
        return bool(self.ocr_thread and self.ocr_thread.is_alive())

    def _update_start_state(self) -> None:
        ready = self.store is not None and self.pdfs and not self._busy()
        self.start_btn.configure(state="normal" if ready else "disabled")

    def start_ocr(self) -> None:
        outdir = Path(self.outdir.get().strip())
        if not self.outdir.get().strip():
            messagebox.showerror(APP_TITLE, "請選擇輸出資料夾")
            return
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"無法建立輸出資料夾：{e}")
            return
        self.stop_event.clear()
        self.ocr_bar.configure(value=0)
        self.ocr_thread = threading.Thread(
            target=self._ocr_worker,
            args=(self.store, list(self.pdfs), outdir, self.with_meta.get()),
            daemon=True,
        )
        self.ocr_thread.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def stop_ocr(self) -> None:
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.ocr_status.configure(text="目前這頁完成後停止…")

    def _ocr_worker(self, store: Path, pdfs: list[Path], outdir: Path, with_meta: bool) -> None:
        post = self.events.put
        ok = failed = 0
        try:
            if self.runtime is None:
                post(("status", "載入模型中（約 30–60 秒）…"))
                # huggingface_hub 在 import 時讀這些設定，必須在 import ocr_doc_local 之前設定。
                os.environ["HF_HUB_CACHE"] = str(store)
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["HF_MODULES_CACHE"] = str(DATA_DIR / "modules")
                import ocr_doc_local

                started = time.monotonic()
                self.runtime = ocr_doc_local.NemotronRuntime(
                    model_store.main_model_id(), local_files_only=True,
                )
                post(("log", f"模型載入完成（{time.monotonic() - started:.0f} 秒，裝置 {self.runtime.device}）"))
            import ocr_doc_local as ocr

            for index, pdf in enumerate(pdfs):
                def on_page(page: int, total: int, index=index, pdf=pdf) -> None:
                    # 先回報「前 page-1 頁已完成」再檢查停止：停止要求期間跑完的那頁也算進進度。
                    post(("page", (index, len(pdfs), page, total, pdf.name)))
                    if self.stop_event.is_set():
                        raise _Stopped

                started = time.monotonic()
                try:
                    parts = ocr.ocr_pdf(
                        self.runtime, pdf, ocr.RENDER_DPI, ocr.MAX_WIDTH, ocr.MAX_HEIGHT,
                        ocr.PAGE_MAX_TOKENS, on_page,
                    )
                    meta = ocr.extract_meta(parts[0]) if with_meta and parts else {}
                    out = ocr.write_doc(pdf, outdir, parts, meta)
                    ok += 1
                    post(("log", f"✔ {pdf.name} → {out.name}（{len(parts)} 頁，{time.monotonic() - started:.1f} 秒）"))
                except _Stopped:
                    raise
                except Exception as e:
                    failed += 1
                    traceback.print_exc()
                    post(("log", f"✘ {pdf.name} 失敗：{e}"))
            post(("finished", f"完成 {ok} 份" + (f"，失敗 {failed} 份" if failed else "")))
        except _Stopped:
            post(("stopped", f"已停止（完成 {ok} 份" + (f"，失敗 {failed} 份" if failed else "") + "）"))
        except Exception as e:
            traceback.print_exc()
            post(("fatal", f"{type(e).__name__}: {e}"))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "device":
                    self.device_label.configure(text=value)
                elif kind == "status":
                    self.ocr_status.configure(text=value)
                    self.log(value)
                elif kind == "log":
                    self.log(value)
                elif kind == "page":
                    index, count, page, total, name = value
                    self.ocr_bar.configure(value=(index + (page - 1) / total) / count * 1000)
                    self.ocr_status.configure(text=f"第 {index + 1}/{count} 份 {name}：第 {page}/{total} 頁")
                elif kind in ("finished", "stopped", "fatal"):
                    self.stop_btn.configure(state="disabled")
                    self.ocr_thread = None
                    self._update_start_state()
                    if kind != "fatal":
                        # 停止時進度條停在已完成的位置，只有全部跑完才填滿。
                        if kind == "finished":
                            self.ocr_bar.configure(value=1000)
                        self.ocr_status.configure(text=value)
                        self.log(value)
                    else:
                        self.ocr_status.configure(text="發生錯誤")
                        self.log(f"錯誤：{value}")
                        messagebox.showerror(APP_TITLE, f"{value}\n\n詳細紀錄：{LOG_PATH}")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_close(self) -> None:
        if self._busy():
            messagebox.showinfo(APP_TITLE, "轉換進行中；請先按「停止」，等目前這頁完成後再關閉。")
            return
        if self.download_proc and self.download_proc.is_alive():
            self.download_proc.terminate()
        self.destroy()


def main() -> None:
    multiprocessing.freeze_support()
    _redirect_stdio()
    App(sys.argv[1:]).mainloop()


if __name__ == "__main__":
    main()

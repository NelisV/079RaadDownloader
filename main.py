"""079 Raad Downloader - GUI and CLI entry point.

Run without arguments for the CustomTkinter GUI, or with ``--url``/``--output`` for a
headless command-line download (see ``python main.py --help``).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from typing import Optional

from hls import DEFAULT_WORKERS, HLSDownloader, Progress, format_time, parse_time
from scraper import ScrapeError, get_m3u8_url, is_playlist_url

APP_TITLE = "079 Raad Downloader"
DEFAULT_URL_HINT = "https://zoetermeer.bestuurlijkeinformatie.nl/Agenda/Index/..."

# Progress bar budget: the scrape is a fixed-ish 10-20 s, the download scales with length.
SCRAPE_SHARE = 0.15


def _validate_times(start: str, duration: str) -> tuple[float, Optional[float]]:
    """Return (start_secs, duration_secs or None for 'until the end'); raises ValueError."""
    try:
        start_secs = parse_time(start) if start.strip() else 0.0
    except ValueError:
        raise ValueError(f"Start time {start!r} is not valid. Use HH:MM:SS.")
    try:
        dur_secs = parse_time(duration) if duration.strip() else 0.0
    except ValueError:
        raise ValueError(f"Duration {duration!r} is not valid. Use HH:MM:SS, or 00:00:00 for the whole recording.")
    return start_secs, (dur_secs or None)


# =============================================================================== GUI


def run_gui() -> None:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox

    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title(APP_TITLE)
            self.geometry("640x500")
            self.minsize(560, 460)

            self.output_path = ""
            self.downloader: Optional[HLSDownloader] = None
            self.cancel_event = threading.Event()
            self.worker: Optional[threading.Thread] = None
            self.busy = False

            self.url_var = ctk.StringVar()
            self.start_time_var = ctk.StringVar(value="00:00:00")
            self.duration_var = ctk.StringVar(value="00:00:00")
            self.status_var = ctk.StringVar(value="Ready")
            self.detail_var = ctk.StringVar(value="")

            ctk.CTkLabel(self, text="Gemeenteraad Stream Downloader", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(18, 8))

            form = ctk.CTkFrame(self)
            form.pack(pady=8, padx=20, fill="both", expand=True)
            form.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(form, text="iBabs agenda URL (or .m3u8):").grid(row=0, column=0, padx=10, pady=(15, 5), sticky="w")
            self.url_entry = ctk.CTkEntry(form, textvariable=self.url_var, placeholder_text=DEFAULT_URL_HINT)
            self.url_entry.grid(row=0, column=1, padx=10, pady=(15, 5), sticky="we")

            ctk.CTkLabel(form, text="Start time (HH:MM:SS):").grid(row=1, column=0, padx=10, pady=5, sticky="w")
            self.start_entry = ctk.CTkEntry(form, textvariable=self.start_time_var, width=150)
            self.start_entry.grid(row=1, column=1, padx=10, pady=5, sticky="w")

            ctk.CTkLabel(form, text="Duration (HH:MM:SS):").grid(row=2, column=0, padx=10, pady=5, sticky="w")
            dur_row = ctk.CTkFrame(form, fg_color="transparent")
            dur_row.grid(row=2, column=1, padx=10, pady=5, sticky="we")
            self.duration_entry = ctk.CTkEntry(dur_row, textvariable=self.duration_var, width=150)
            self.duration_entry.pack(side="left")
            ctk.CTkLabel(dur_row, text="00:00:00 = until the end of the recording", text_color="gray").pack(side="left", padx=10)

            self.save_btn = ctk.CTkButton(form, text="Select save location", command=self.select_save_location)
            self.save_btn.grid(row=3, column=0, padx=10, pady=15, sticky="w")
            self.save_lbl = ctk.CTkLabel(form, text="No file selected", text_color="gray", anchor="w")
            self.save_lbl.grid(row=3, column=1, padx=10, pady=15, sticky="we")

            progress_frame = ctk.CTkFrame(self, fg_color="transparent")
            progress_frame.pack(pady=5, padx=20, fill="x")
            self.status_label = ctk.CTkLabel(progress_frame, textvariable=self.status_var, anchor="w", wraplength=580, justify="left")
            self.status_label.pack(side="top", fill="x")
            self.detail_label = ctk.CTkLabel(progress_frame, textvariable=self.detail_var, anchor="w", text_color="gray", font=ctk.CTkFont(size=12))
            self.detail_label.pack(side="top", fill="x")
            self.progress_bar = ctk.CTkProgressBar(progress_frame)
            self.progress_bar.pack(side="top", fill="x", pady=(5, 0))
            self.progress_bar.set(0)

            btn_frame = ctk.CTkFrame(self, fg_color="transparent")
            btn_frame.pack(pady=(10, 16), padx=20, fill="x")
            self.download_btn = ctk.CTkButton(btn_frame, text="Start download", command=self.start_process)
            self.download_btn.pack(side="left", padx=5, expand=True, fill="x")
            self.cancel_btn = ctk.CTkButton(btn_frame, text="Cancel", command=self.cancel_process, state="disabled", fg_color="#b3261e", hover_color="#8c1d18")
            self.cancel_btn.pack(side="left", padx=5, expand=True, fill="x")

            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.url_entry.focus()

        # ---- helpers that must run on the Tk thread -----------------------------

        def ui(self, fn, *args):
            """Schedule ``fn(*args)`` on the Tk main thread (Tk is not thread-safe)."""
            self.after(0, lambda: fn(*args))

        def set_status(self, msg: str, detail: str = ""):
            self.status_var.set(msg)
            self.detail_var.set(detail)

        def set_inputs_enabled(self, enabled: bool):
            state = "normal" if enabled else "disabled"
            for w in (self.url_entry, self.start_entry, self.duration_entry, self.save_btn, self.download_btn):
                w.configure(state=state)
            self.cancel_btn.configure(state="disabled" if enabled else "normal")

        # ---- actions ------------------------------------------------------------

        def select_save_location(self):
            filename = filedialog.asksaveasfilename(
                title="Save video as",
                defaultextension=".mp4",
                filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
                initialfile="raadsvergadering.mp4",
            )
            if filename:
                self.output_path = filename
                shown = filename if len(filename) < 60 else "...\\" + os.path.basename(filename)
                self.save_lbl.configure(text=shown, text_color=("black", "white"))

        def start_process(self):
            if self.busy:
                return
            url = self.url_var.get().strip()
            if not url.lower().startswith("http"):
                messagebox.showerror("Missing URL", "Paste the iBabs agenda page URL (or a .m3u8 stream URL).")
                return
            if not self.output_path:
                messagebox.showerror("No save location", "Choose where to save the video first.")
                return
            try:
                _validate_times(self.start_time_var.get(), self.duration_var.get())
            except ValueError as e:
                messagebox.showerror("Invalid time", str(e))
                return

            self.busy = True
            self.cancel_event.clear()
            self.downloader = None
            self.set_inputs_enabled(False)
            self.progress_bar.set(0)
            self.set_status("Starting...")
            self.worker = threading.Thread(target=self.run_pipeline, args=(url,), daemon=True)
            self.worker.start()

        def run_pipeline(self, url: str):
            """Worker thread: scrape, then download. Only touches the UI via ``self.ui``."""
            try:
                if is_playlist_url(url):
                    m3u8_url = url
                else:
                    self.ui(self.progress_bar.set, 0.03)
                    m3u8_url = get_m3u8_url(
                        url,
                        headless=True,
                        log=lambda msg: self.ui(self.set_status, msg, "Step 1/2: finding the stream (10-20 s)"),
                        cancel=self.cancel_event,
                    )
                if self.cancel_event.is_set():
                    self.ui(self.finish, "cancelled", "Cancelled before the download started.")
                    return
                self.ui(self.progress_bar.set, SCRAPE_SHARE)

                dl = HLSDownloader(
                    m3u8_url=m3u8_url,
                    output_path=self.output_path,
                    start_time=self.start_time_var.get(),
                    duration=self.duration_var.get(),
                )
                self.downloader = dl
                dl.status_callback = lambda msg: self.ui(self.set_status, msg, "Step 2/2: downloading")
                dl.progress_callback = self.on_progress
                dl.on_complete_callback = lambda path: self.ui(self.finish, "ok", path)
                dl.on_error_callback = lambda msg: self.ui(self.finish, "error", msg)
                dl.on_cancel_callback = lambda msg: self.ui(self.finish, "cancelled", msg)
                if self.cancel_event.is_set():
                    dl.cancel()
                dl.run()
            except ScrapeError as e:
                self.ui(self.finish, "cancelled" if "Cancelled" in str(e) else "error", str(e))
            except Exception as e:  # noqa: BLE001
                self.ui(self.finish, "error", f"{type(e).__name__}: {e}")

        def on_progress(self, p: Progress):
            def apply():
                self.progress_bar.set(SCRAPE_SHARE + p.fraction * (1 - SCRAPE_SHARE))
                eta = f"  |  ETA {format_time(p.eta_secs)}" if p.eta_secs is not None else ""
                self.set_status(
                    f"Downloading... {format_time(p.done_secs)} of {format_time(p.total_secs)} ({p.fraction * 100:.0f}%)",
                    f"{p.done_bytes / 1e6:.0f} MB at {p.speed_bps / 1e6:.1f} MB/s{eta}",
                )
            self.ui(apply)

        def cancel_process(self):
            if not self.busy:
                return
            self.cancel_event.set()
            if self.downloader:
                self.downloader.cancel()
            self.cancel_btn.configure(state="disabled")
            self.set_status("Cancelling... finishing the file so it stays playable.")

        def finish(self, outcome: str, msg: str = ""):
            if not self.busy:
                return  # a late callback after we already reported; ignore it
            self.busy = False
            self.set_inputs_enabled(True)
            if outcome == "ok":
                self.progress_bar.set(1.0)
                self.set_status("Download complete", msg)
                if messagebox.askyesno("Done", f"Video saved to:\n{msg}\n\nOpen the folder?"):
                    _open_folder(msg)
            elif outcome == "cancelled":
                self.progress_bar.set(0)
                self.set_status("Cancelled", msg)
            else:
                self.progress_bar.set(0)
                self.set_status("Failed", msg.splitlines()[0] if msg else "")
                messagebox.showerror("Download failed", msg)

        def on_close(self):
            if self.busy:
                if not messagebox.askyesno("Download in progress", "A download is running. Stop it and quit?"):
                    return
                self.cancel_process()
                if self.worker and self.worker.is_alive():
                    self.worker.join(timeout=15)  # lets FFmpeg finalise the partial file
            self.destroy()

    App().mainloop()


def _open_folder(path: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", folder])
    except OSError:
        pass


# =============================================================================== CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="079RaadDownloader",
        description="Download (a part of) a Zoetermeer council meeting video from an iBabs agenda page.",
        epilog="Run without arguments to open the graphical interface.",
    )
    parser.add_argument("--url", required=True, help="iBabs agenda page URL, or a .m3u8 playlist URL")
    parser.add_argument("--output", required=True, help="output file, e.g. vergadering.mp4")
    parser.add_argument("--start", default="00:00:00", help="start time HH:MM:SS (default: beginning)")
    parser.add_argument("--duration", default="00:00:00", help="length HH:MM:SS; 00:00:00 = until the end (default)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"parallel connections (default {DEFAULT_WORKERS})")
    parser.add_argument("--no-faststart", action="store_true", help="skip the final moov relocation pass (slightly faster, worse for streaming playback)")
    parser.add_argument("--visible", action="store_true", help="show the browser window while finding the stream (debugging)")
    parser.add_argument("--print-m3u8", action="store_true", help="only print the intercepted playlist URL and exit")
    return parser


def run_cli(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_times(args.start, args.duration)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        m3u8_url = get_m3u8_url(args.url, headless=not args.visible, log=lambda m: print(m, flush=True))
    except ScrapeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.print_m3u8:
        print(m3u8_url)
        return 0

    dl = HLSDownloader(
        m3u8_url=m3u8_url,
        output_path=args.output,
        start_time=args.start,
        duration=args.duration,
        workers=args.workers,
        faststart=not args.no_faststart,
    )
    result: dict[str, str] = {}
    is_tty = sys.stdout.isatty()

    def on_progress(p: Progress):
        line = p.summary()
        print(("\r" + line.ljust(100)) if is_tty else line, end="" if is_tty else "\n", flush=True)

    def newline():
        if is_tty:
            print()

    dl.status_callback = lambda m: (newline(), print(m, flush=True))
    dl.progress_callback = on_progress
    dl.on_complete_callback = lambda path: result.setdefault("ok", path)
    dl.on_error_callback = lambda msg: result.setdefault("error", msg)
    dl.on_cancel_callback = lambda msg: result.setdefault("cancelled", msg)

    try:
        dl.run()
    except KeyboardInterrupt:
        dl.cancel()
        result.setdefault("cancelled", "Interrupted")
    newline()
    if "ok" in result:
        print(f"Saved: {result['ok']}")
        return 0
    if "cancelled" in result:
        print(result["cancelled"])
        return 130
    print(f"error: {result.get('error', 'unknown error')}", file=sys.stderr)
    return 1


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(run_cli(sys.argv[1:]))
    run_gui()


if __name__ == "__main__":
    main()

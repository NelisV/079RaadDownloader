import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import argparse
import sys
import os

from scraper import get_m3u8_url
from downloader import FFmpegDownloader, time_to_seconds

ctk.set_appearance_mode("System")  # Modes: "System" (standard), "Dark", "Light"
ctk.set_default_color_theme("blue")  # Themes: "blue" (standard), "green", "dark-blue"

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("079 Raad Downloader")
        self.geometry("600x450")
        self.resizable(False, False)

        # State
        self.output_path = ""
        self.downloader = None
        self.download_thread = None

        # --- Variables ---
        self.url_var = ctk.StringVar(value="https://zoetermeer.bestuurlijkeinformatie.nl/...")
        self.start_time_var = ctk.StringVar(value="00:00:00")
        self.duration_var = ctk.StringVar(value="00:10:00")
        self.status_var = ctk.StringVar(value="Ready")

        # --- UI Elements ---
        
        # Title
        self.title_label = ctk.CTkLabel(self, text="Gemeenteraad Stream Downloader", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_label.pack(pady=(20, 10))

        # Main Frame
        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(pady=10, padx=20, fill="both", expand=True)

        # URL Input
        self.url_label = ctk.CTkLabel(self.main_frame, text="iBabs Agenda URL:")
        self.url_label.grid(row=0, column=0, padx=10, pady=(15, 5), sticky="w")
        
        self.url_entry = ctk.CTkEntry(self.main_frame, textvariable=self.url_var, width=400)
        self.url_entry.grid(row=0, column=1, padx=10, pady=(15, 5), sticky="we")
        
        # Start Time
        self.start_label = ctk.CTkLabel(self.main_frame, text="Start Time (HH:MM:SS):")
        self.start_label.grid(row=1, column=0, padx=10, pady=5, sticky="w")
        
        self.start_entry = ctk.CTkEntry(self.main_frame, textvariable=self.start_time_var, width=150)
        self.start_entry.grid(row=1, column=1, padx=10, pady=5, sticky="w")

        # Duration
        self.duration_label = ctk.CTkLabel(self.main_frame, text="Duration (HH:MM:SS):")
        self.duration_label.grid(row=2, column=0, padx=10, pady=5, sticky="w")
        
        self.duration_entry = ctk.CTkEntry(self.main_frame, textvariable=self.duration_var, width=150)
        self.duration_entry.grid(row=2, column=1, padx=10, pady=5, sticky="w")
        
        # Save Location
        self.save_btn = ctk.CTkButton(self.main_frame, text="Select Save Location", command=self.select_save_location)
        self.save_btn.grid(row=3, column=0, padx=10, pady=15, sticky="w")
        
        self.save_lbl = ctk.CTkLabel(self.main_frame, text="No file selected...", text_color="gray")
        self.save_lbl.grid(row=3, column=1, padx=10, pady=15, sticky="w")

        # Progress Frame
        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.progress_frame.pack(pady=5, padx=20, fill="x")

        self.status_label = ctk.CTkLabel(self.progress_frame, textvariable=self.status_var)
        self.status_label.pack(side="top", anchor="w")

        self.progress_bar = ctk.CTkProgressBar(self.progress_frame)
        self.progress_bar.pack(side="top", fill="x", pady=(5, 0))
        self.progress_bar.set(0)

        # Action Buttons
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(pady=10, padx=20, fill="x")

        self.download_btn = ctk.CTkButton(self.btn_frame, text="Start Download", command=self.start_process_thread)
        self.download_btn.pack(side="left", padx=5, expand=True, fill="x")

        self.cancel_btn = ctk.CTkButton(self.btn_frame, text="Cancel", command=self.cancel_process, state="disabled", fg_color="red", hover_color="darkred")
        self.cancel_btn.pack(side="left", padx=5, expand=True, fill="x")

    def select_save_location(self):
        filename = filedialog.asksaveasfilename(
            title="Save Video As",
            defaultextension=".mp4",
            filetypes=[("MP4 files", "*.mp4"), ("All files", "*.*")]
        )
        if filename:
            self.output_path = filename
            # Display shortened path if too long
            display_path = filename if len(filename) < 40 else "...\\" + os.path.basename(filename)
            self.save_lbl.configure(text=display_path, text_color="white")

    def log_status(self, msg: str):
        self.status_var.set(msg)
        self.update_idletasks()

    def set_progress(self, val: float):
        self.progress_bar.set(val)

    def start_process_thread(self):
        url = self.url_var.get().strip()
        if not url.startswith("http"):
            messagebox.showerror("Error", "Please enter a valid URL.")
            return
            
        if not self.output_path:
            messagebox.showerror("Error", "Please select a save location first.")
            return

        # Disable inputs
        self.url_entry.configure(state="disabled")
        self.start_entry.configure(state="disabled")
        self.duration_entry.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        
        self.set_progress(0)
        self.log_status("Starting...")

        self.download_thread = threading.Thread(target=self.run_process, daemon=True)
        self.download_thread.start()

    def run_process(self):
        try:
            url = self.url_var.get().strip()
            
            # 1. Scrape for M3U8
            self.log_status("Launching headless browser to intercept stream tokens... (This takes a few seconds)")
            self.set_progress(0.1)
            
            # Using Playwright requires some time to initialize and click
            m3u8_url = get_m3u8_url(url, headless=True)
            self.log_status("Stream URL intercepted! Starting FFmpeg download...")
            self.set_progress(0.2)

            # 2. Download with FFmpeg
            start_t = self.start_time_var.get()
            dur_t = self.duration_var.get()

            self.downloader = FFmpegDownloader(
                m3u8_url=m3u8_url,
                output_path=self.output_path,
                start_time=start_t,
                duration=dur_t
            )

            # Setup callbacks
            def on_progress(pct, current_time_str):
                # Scale between 0.2 and 1.0 (since 0 to 0.2 was scraping)
                scaled_pct = 0.2 + (pct * 0.8)
                self.after(0, lambda: self.set_progress(scaled_pct))
                self.after(0, lambda: self.log_status(f"Downloading... (Video time: {current_time_str})"))

            def on_complete():
                self.after(0, lambda: self.finish_process(True))

            def on_error(err_msg):
                self.after(0, lambda: self.finish_process(False, err_msg))

            self.downloader.progress_callback = on_progress
            self.downloader.on_complete_callback = on_complete
            self.downloader.on_error_callback = on_error

            self.downloader.start_download()

        except Exception as e:
            self.after(0, lambda: self.finish_process(False, str(e)))

    def cancel_process(self):
        self.log_status("Cancelling...")
        if self.downloader:
            self.downloader.cancel_download()
        self.finish_process(False, "User cancelled.")

    def finish_process(self, success: bool, msg: str = ""):
        # Re-enable UI
        self.url_entry.configure(state="normal")
        self.start_entry.configure(state="normal")
        self.duration_entry.configure(state="normal")
        self.save_btn.configure(state="normal")
        self.download_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

        if success:
            self.set_progress(1.0)
            self.log_status("Download Complete!")
            messagebox.showinfo("Success", f"Video has been saved successfully to:\n{self.output_path}")
        else:
            self.set_progress(0)
            self.log_status(f"Status: Failed - {msg}")
            if "User cancelled" not in msg:
                messagebox.showerror("Error", f"An error occurred:\n{msg}")

def run_cli():
    parser = argparse.ArgumentParser(description="079 Raad Downloader CLI")
    parser.add_argument("--url", help="iBabs Agenda URL")
    parser.add_argument("--output", help="Output file path (e.g. video.mp4)")
    parser.add_argument("--start", default="00:00:00", help="Start time (HH:MM:SS)")
    parser.add_argument("--duration", default="00:00:10", help="Duration (HH:MM:SS)")
    
    args = parser.parse_args()
    
    if not args.url or not args.output:
        # If no args, just return so we can launch GUI
        return False
        
    print(f"Starting CLI download...")
    print(f"URL: {args.url}")
    print(f"Output: {args.output}")
    print(f"Start: {args.start}, Duration: {args.duration}")
    
    try:
        # 1. Scrape
        print("Launching headless browser to intercept stream tokens...")
        m3u8_url = get_m3u8_url(args.url, headless=True)
        print(f"Stream URL intercepted: {m3u8_url}")
        
        # 2. Download
        downloader = FFmpegDownloader(
            m3u8_url=m3u8_url,
            output_path=args.output,
            start_time=args.start,
            duration=args.duration
        )
        
        done_event = threading.Event()
        error_occurred = None
        
        def on_progress(pct, current_time_str):
            print(f"Progress: {pct*100:.1f}% ({current_time_str})", end="\r")

        def on_complete():
            print("\nDownload Complete!")
            done_event.set()

        def on_error(err_msg):
            nonlocal error_occurred
            error_occurred = err_msg
            print(f"\nError: {err_msg}")
            done_event.set()

        downloader.progress_callback = on_progress
        downloader.on_complete_callback = on_complete
        downloader.on_error_callback = on_error

        downloader.start_download()
        done_event.wait()
        
        if error_occurred:
            sys.exit(1)
        return True
        
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Check if we should run in CLI mode
    if len(sys.argv) > 1:
        if run_cli():
            sys.exit(0)
            
    # Otherwise run GUI
    app = App()
    app.mainloop()

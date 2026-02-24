import subprocess
import imageio_ffmpeg
import re
import threading

def time_to_seconds(time_str: str) -> int:
    """Converts HH:MM:SS format to total seconds."""
    try:
        h, m, s = map(int, time_str.strip().split(':'))
        return h * 3600 + m * 60 + s
    except Exception:
        return 0

class FFmpegDownloader:
    def __init__(self, m3u8_url: str, output_path: str, start_time: str, duration: str):
        self.m3u8_url = m3u8_url
        self.output_path = output_path
        self.start_time = start_time
        self.duration = duration
        
        # Calculate total seconds to download for progress tracking
        self.total_duration_secs = time_to_seconds(duration)
        if self.total_duration_secs == 0:
            self.total_duration_secs = 1 # Avoid division by zero
            
        self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        self.process = None
        self.is_running = False
        self.progress_callback = None
        self.on_complete_callback = None
        self.on_error_callback = None

    def start_download(self):
        """Starts the FFmpeg download in a separate thread."""
        thread = threading.Thread(target=self._run_ffmpeg, daemon=True)
        thread.start()

    def _run_ffmpeg(self):
        self.is_running = True
        
        # Construct FFmpeg command
        cmd = [
            self.ffmpeg_exe,
            '-y',  # Overwrite output
        ]
        
        # Add start time if provided
        if self.start_time and self.start_time.strip() != "00:00:00":
            cmd.extend(['-ss', self.start_time.strip()])
            
        cmd.extend([
            '-i', self.m3u8_url,
        ])
        
        # Add duration if provided
        if self.duration and self.duration.strip() != "00:00:00":
            cmd.extend(['-t', self.duration.strip()])
            
        cmd.extend([
            '-c', 'copy',
            self.output_path
        ])

        try:
            # We redirect stderr to stdout and read it to parse progress
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )

            # Regex to match FFmpeg time output: time=00:01:23.45
            time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2})\.\d{2}")

            for line in self.process.stdout:
                if not self.is_running:
                    self.process.terminate()
                    break
                    
                match = time_regex.search(line)
                if match and self.progress_callback:
                    current_time_str = match.group(1)
                    current_secs = time_to_seconds(current_time_str)
                    
                    # Calculate percentage based on expected duration
                    progress_pct = min(1.0, current_secs / self.total_duration_secs)
                    self.progress_callback(progress_pct, current_time_str)

            self.process.wait()

            if self.is_running:
                if self.process.returncode == 0:
                    if self.on_complete_callback:
                        self.on_complete_callback()
                else:
                    if self.on_error_callback:
                        self.on_error_callback(f"FFmpeg exited with code {self.process.returncode}")
        except Exception as e:
            if self.on_error_callback:
                self.on_error_callback(str(e))
        finally:
            self.is_running = False

    def cancel_download(self):
        """Cancels the running download."""
        self.is_running = False
        if self.process:
            self.process.terminate()

if __name__ == "__main__":
    pass

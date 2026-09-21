# 079 Raad Downloader

Download (a fragment of) a Zoetermeer gemeenteraad meeting video from its iBabs agenda page.

1. Paste the agenda URL (`https://zoetermeer.bestuurlijkeinformatie.nl/Agenda/Index/...`).
2. Optionally set a start time and duration (`00:00:00` duration = the whole recording).
3. Pick a save location and press **Start download**.

The tool finds the stream with a headless browser (10-20 s), then downloads the segments
with 16 parallel connections and stitches them into an MP4. A three-hour meeting takes
about half a minute on a fast connection.

## Command line

```
079RaadDownloader --url <agenda-or-m3u8-url> --output clip.mp4 [--start HH:MM:SS] [--duration HH:MM:SS]
```

## Development

```
python -m venv venv
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m playwright install chromium
venv\Scripts\python -m pytest
venv\Scripts\python main.py
```

Build the Windows executable with `venv\Scripts\pyinstaller main.spec`.

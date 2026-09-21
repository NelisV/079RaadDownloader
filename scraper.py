"""Find the tokenized HLS playlist URL behind an iBabs agenda page.

The CompanyWebcast player embedded on ``*.bestuurlijkeinformatie.nl`` requests a signed
``sdk-ssl.m3u8`` (CloudFront Policy/Signature in the query string) the moment its
"Start now" button is pressed. We load the page in headless Chromium, press that button
and listen passively for the request. The signature is short-lived, so callers should
download immediately.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from typing import Callable, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from hls import USER_AGENT

# How long to wait for the player to request its playlist after the page has loaded.
PLAYLIST_TIMEOUT_SECS = 30
PAGE_LOAD_TIMEOUT_MS = 45_000

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ScrapeError(Exception):
    pass


def is_playlist_url(url: str) -> bool:
    return ".m3u8" in url.split("#")[0].lower()


def _browsers_path_for_frozen_build():
    """When running from the PyInstaller bundle, keep browsers in the normal per-user
    location instead of next to the (read-only, temporary) bundle."""
    if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(local_appdata, "ms-playwright")


def install_chromium(log: Callable[[str], None] = print) -> None:
    """Run Playwright's own installer through the bundled Node driver.

    This works both from source and from the frozen exe (where ``python -m playwright``
    is not available because ``sys.executable`` is the app itself).
    """
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    log("Chromium for Playwright is not installed yet; downloading it (one-time, ~150 MB)...")
    cmd = [*compute_driver_executable(), "install", "chromium"]
    result = subprocess.run(
        cmd,
        env=get_driver_env(),
        capture_output=True,
        text=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise ScrapeError(
            "Could not install the Playwright Chromium browser.\n"
            + (result.stderr or result.stdout).strip()[-800:]
        )


def get_m3u8_url(
    page_url: str,
    headless: bool = True,
    log: Callable[[str], None] = lambda _msg: None,
    timeout_secs: float = PLAYLIST_TIMEOUT_SECS,
    cancel: Optional[threading.Event] = None,
) -> str:
    """Return the signed playlist URL for an iBabs agenda page (or pass an m3u8 URL through)."""
    page_url = page_url.strip()
    if is_playlist_url(page_url):
        return page_url
    if not re.match(r"^https?://", page_url, re.I):
        raise ScrapeError("Enter a full URL starting with http:// or https://")

    _browsers_path_for_frozen_build()

    found = threading.Event()
    result: dict[str, str] = {}

    debug = bool(os.environ.get("RAAD_DEBUG"))

    def on_request(request):
        url = request.url
        if debug:
            print(f"[req] {url[:160]}", file=sys.stderr, flush=True)
        if ".m3u8" in url and "url" not in result:
            result["url"] = url
            found.set()

    with sync_playwright() as p:
        if not os.path.exists(p.chromium.executable_path):
            install_chromium(log)

        log("Launching headless browser...")
        browser = p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        try:
            # Tall viewport: the player only starts loading its stream once it is on screen.
            context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 2000})
            page = context.new_page()
            page.on("request", on_request)

            log("Loading agenda page...")
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            except PlaywrightError as e:
                raise ScrapeError(f"Could not load the page: {e.message.splitlines()[0]}") from e

            # The player does not request its playlist until its "Start now" button is
            # pressed, so keep the player in view and click that button as soon as it
            # exists (retrying while the iframe is still loading).
            log("Waiting for the video player to request its stream...")
            deadline_steps = int(timeout_secs / 0.25)
            for step in range(deadline_steps):
                if found.is_set():
                    break
                if cancel is not None and cancel.is_set():
                    raise ScrapeError("Cancelled")
                if step % 4 == 0:
                    _scroll_player_into_view(page, debug)
                if step >= 8 and step % 12 == 8:  # 2 s after load, then every 3 s
                    _click_start(page, debug)
                page.wait_for_timeout(250)

            if not found.is_set():
                has_player = _try(lambda: page.locator(".cwc, iframe[src*='companywebcast']").count() > 0) or False
                hint = (
                    "The player was found but never requested a stream. The recording may not be published yet."
                    if has_player
                    else "No CompanyWebcast player was found on this page. Is this the agenda page of a meeting with a recording?"
                )
                raise ScrapeError(f"Could not find the video stream URL. {hint}")
        finally:
            browser.close()

    return result["url"]


def _scroll_player_into_view(page, debug: bool = False) -> None:
    """JS scroll: unlike ``scroll_into_view_if_needed`` it does not wait for the element
    to be 'stable', which can time out while the page is still loading."""
    try:
        hit = page.evaluate(
            """() => {
                const el = document.querySelector('.cwc, iframe[src*="companywebcast"]');
                if (!el) return false;
                el.scrollIntoView({block: 'center'});
                return true;
            }"""
        )
        if debug:
            print(f"[scroll] player element found: {hit}", file=sys.stderr, flush=True)
    except PlaywrightError as e:
        if debug:
            print(f"[scroll] failed: {e.message.splitlines()[0]}", file=sys.stderr, flush=True)


def _click_start(page, debug: bool = False) -> bool:
    """Click the player's start button. Only child frames are searched: the iBabs page
    itself has a search button whose accessible name also matches 'start'."""
    for frame in page.frames[1:]:
        for locator in (
            frame.get_by_role("button", name=re.compile(r"^\s*(start now|nu starten|start|afspelen|play)\s*$", re.I)),
            frame.get_by_text(re.compile(r"^\s*(start now|nu starten)\s*$", re.I)),
        ):
            try:
                if locator.count() > 0:
                    locator.first.click(timeout=2000)
                    if debug:
                        print(f"[click] start button in {frame.url[:80]}", file=sys.stderr, flush=True)
                    return True
            except PlaywrightError:
                continue
    return False


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - best-effort nudges only
        return None


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://zoetermeer.bestuurlijkeinformatie.nl/Agenda/Index/9c36980b-f51f-4cc5-bc9c-88861c33d485"
    print("M3U8:", get_m3u8_url(url, log=print))

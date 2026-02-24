import os
import sys
import subprocess
import time
from playwright.sync_api import sync_playwright

def ensure_browser_installed():
    """Ensures that the Playwright chromium browser is installed on the user's system."""
    # If running as a bundled executable, we should point to the system-wide browsers
    # instead of looking inside the internal bundle directory.
    if getattr(sys, 'frozen', False):
        local_appdata = os.environ.get('LOCALAPPDATA')
        if local_appdata:
            ms_playwright_path = os.path.join(local_appdata, 'ms-playwright')
            if os.path.exists(ms_playwright_path):
                os.environ['PLAYWRIGHT_BROWSERS_PATH'] = ms_playwright_path
                return # Browsers found in system location
    
    try:
        # In source mode (or if not found in system location), try to install/verify
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], 
                       check=True, 
                       creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
    except Exception as e:
        print(f"Note: Could not verify/install browser via subprocess: {e}")

def get_m3u8_url(page_url: str, headless: bool = True) -> str:
    """
    Navigates to the given page URL using Playwright, finds the CompanyWebcast
    player iframe, clicks the 'Start now' button, and intercepts the m3u8 stream URL.
    """
    ensure_browser_installed()
    
    m3u8_url = None
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = context.new_page()

        def handle_request(route, request):
            nonlocal m3u8_url
            if "m3u8" in request.url:
                if m3u8_url is None:
                    m3u8_url = request.url
            route.continue_()

        page.route("**/*", handle_request)
        page.goto(page_url, wait_until="networkidle")
        
        try:
            player_element = page.locator(".cwc")
            if player_element.count() > 0:
                player_element.first.scroll_into_view_if_needed()
        except Exception:
            pass

        page.wait_for_timeout(2000)
        
        try:
            frame_locator = page.frame_locator("iframe").first
            btn = frame_locator.get_by_text("Start now")
            if btn.count() > 0:
                btn.first.click()
        except Exception:
            pass
            
        # Wait a bit for the playlist request to fire
        for _ in range(10):
            if m3u8_url is not None:
                break
            page.wait_for_timeout(1000)
            
        browser.close()
    
    if m3u8_url is None:
        raise Exception("Could not retrieve the m3u8 stream URL. The page might be protected or the player failed to load.")
        
    return m3u8_url

if __name__ == "__main__":
    # Test
    url = "https://zoetermeer.bestuurlijkeinformatie.nl/Agenda/Index/9c36980b-f51f-4cc5-bc9c-88861c33d485"
    print("M3U8:", get_m3u8_url(url))

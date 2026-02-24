from playwright.sync_api import sync_playwright
import time

def get_m3u8_url(page_url):
    m3u8_url = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = context.new_page()

        def handle_request(route, request):
            nonlocal m3u8_url
            if "m3u8" in request.url:
                if m3u8_url is None:
                    m3u8_url = request.url
                    print(f"DEBUG: Found m3u8: {m3u8_url}")
            route.continue_()

        page.route("**/*", handle_request)
        print(f"DEBUG: Navigating to {page_url}...")
        page.goto(page_url, wait_until="networkidle")
        
        print("Scrolling to player...")
        try:
            player_element = page.locator(".cwc")
            if player_element.count() > 0:
                player_element.first.scroll_into_view_if_needed()
                print("Scrolled to .cwc element.")
        except Exception as e:
            print("Error scrolling:", e)

        page.wait_for_timeout(2000)
        
        print("Trying to click 'Start now' button in iframe...")
        try:
            # Try to grab any iframe and check for Start now
            frame_locator = page.frame_locator("iframe").first
            btn = frame_locator.get_by_text("Start now")
            if btn.count() > 0:
                btn.first.click()
                print("Clicked 'Start now' button!")
            else:
                print("Start now button not found in the first iframe.")
        except Exception as e:
            print("Could not click Start now:", e)
            
        # Give it a few seconds to load the playlist after the click
        page.wait_for_timeout(5000)
        
        if m3u8_url is None:
            page.screenshot(path="screenshot2.png")
            print("Screenshot saved to screenshot2.png")
            
        browser.close()
    
    return m3u8_url

if __name__ == "__main__":
    url = "https://zoetermeer.bestuurlijkeinformatie.nl/Agenda/Index/9c36980b-f51f-4cc5-bc9c-88861c33d485"
    result = get_m3u8_url(url)
    print("FINAL M3U8 URL:", result)

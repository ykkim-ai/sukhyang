"""Streamlit Community Cloud 앱이 잠들지 않도록 실제 브라우저로 접속한다.

잠든 상태면 'get this app back up' 버튼을 눌러 깨운다.
"""
import re
import sys

from playwright.sync_api import sync_playwright

URL = "https://sukhyang.streamlit.app/"
WAKE_TEXT = re.compile(r"get this app back up", re.I)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(10_000)

        button = page.get_by_role("button", name=WAKE_TEXT)
        if button.count() > 0:
            print("앱이 잠들어 있어 깨우는 중...")
            button.first.click()
            page.wait_for_timeout(90_000)
            print("깨우기 버튼을 눌렀습니다.")
        else:
            # 깨어 있는 앱도 접속 상태를 잠시 유지해 활동으로 인식되게 한다
            page.wait_for_timeout(30_000)
            print("앱이 깨어 있습니다.")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

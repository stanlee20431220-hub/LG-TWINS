"""Fast local Chromium launch smoke test for GitHub Actions setup."""

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<!doctype html><title>playwright-smoke</title><p>ok</p>")
            if page.title() != "playwright-smoke":
                raise RuntimeError("Chromium smoke page did not render")
        finally:
            browser.close()


if __name__ == "__main__":
    main()

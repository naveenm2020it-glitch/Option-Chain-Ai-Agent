import os

from playwright.sync_api import sync_playwright


def test_dashboard_navigation_and_primary_views():
    base_url = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8765")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(base_url, wait_until="networkidle", timeout=30000)
        assert response is not None and response.status == 200
        page.locator("#view-overview").wait_for(state="visible")
        for view in ("data", "agents", "levels", "history", "settings", "overview"):
            page.locator(f'.nav-item[data-view="{view}"]').click()
            page.locator(f"#view-{view}.active").wait_for(state="visible", timeout=5000)
        page.locator('[data-view-link="levels"]').click()
        page.locator("#view-levels.active").wait_for(state="visible", timeout=5000)
        assert not errors, "Browser JavaScript errors: " + "; ".join(errors)
        browser.close()

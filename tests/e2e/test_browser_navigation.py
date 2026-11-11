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
        page.get_by_role("button", name="Open detailed market levels ↗").click()
        page.locator("#view-levels.active").wait_for(state="visible", timeout=5000)

        # Exercise the real archived NSE EOD dataset; do not inject synthetic market values.
        page.locator('.nav-item[data-view="overview"]').click()
        page.locator("#view-overview.active").wait_for(state="visible", timeout=5000)
        with page.expect_response(
            lambda response: response.url.endswith("/api/analyze")
            and response.request.method == "POST",
            timeout=120000,
        ) as analysis_response:
            page.locator("#run-analysis").click()
        response = analysis_response.value
        assert response.status == 200, response.text()
        payload = response.json()
        assert payload.get("status") in ("complete", "partial")
        result = payload.get("result") or {}
        assert result.get("agent_count") == 13
        assert "eod_price_action" in result
        assert result["eod_price_action"].get("intraday_status") == "LIMITED — EOD DATA ONLY"
        page.locator("#eod-price-action-overview-content").get_by_text("Status:").wait_for(state="visible", timeout=5000)
        page.get_by_role("button", name="Open detailed market levels ↗").click()
        page.locator("#view-levels.active").wait_for(state="visible", timeout=5000)
        page.locator("#eod-price-action-content").get_by_text("Status:").wait_for(state="visible", timeout=5000)
        assert not errors, "Browser JavaScript errors: " + "; ".join(errors)
        browser.close()

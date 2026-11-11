# Full application validation and release gates

This validation suite is designed for the NIFTY AI EOD-only application. It does not introduce intraday feeds, sample market values, or trading orders.

## Automated test layers

| Layer | Automated coverage | Release gate |
|---|---|---|
| Unit | Formula, parser, schema, integrity, expiry, EOD price-action and institutional helper tests | All pytest tests pass |
| Integration | Real archived NSE report parsing, manifest import, analytics integration, SQLite-backed API fixture | All integration tests pass |
| API | Health, dashboard state, reports, analyses, prices, config, authentication, invalid analysis and upload validation | Expected HTTP status and response contracts |
| E2E | Chromium loads the dashboard and switches all six navigation views and a cross-view link | No page errors; all target views become active |
| Regression | Entire existing and new pytest suite on every push/PR | No test regressions |
| Smoke | Live Render health, root HTML and static JavaScript | All public routes respond successfully |
| Load/performance | 200 concurrent health API requests on CI app, 12 workers; mean/p95/max recorded | No request failures; p95 below 2 seconds |
| Static/security | Bandit high-severity/high-confidence scan and pip-audit dependency CVE scan | No qualifying findings |

## Important scope and limitations

- The CI load test is a repeatable baseline against an isolated CI instance. It is not a production capacity certification or a stress test of the free Render instance.
- Live Render smoke checks exercise public routes only. Authenticated report upload and analysis require valid user-provided report files and are covered by API tests with isolated test storage.
- Market-analysis correctness is tested against genuine archived report fixtures where present. Missing reports/fields must remain visibly partial or insufficient; tests must not fabricate market data.
- Render's free filesystem is ephemeral. Raw NSE source reports in GitHub are durable, but SQLite analysis history and user uploads on Render may be lost after restart/redeploy. Durable user history requires persistent external storage.
- Intraday-only features remain marked LIMITED — EOD DATA ONLY.
- A green CI pipeline does not prove trading accuracy or guarantee future market outcomes.

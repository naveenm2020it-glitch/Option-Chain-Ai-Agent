#!/usr/bin/env python3
"""Repeatable CI concurrency probe; not a production stress test."""
import concurrent.futures
import json
import os
import statistics
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LOAD_TEST_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
REQUESTS = int(os.environ.get("LOAD_TEST_REQUESTS", "200"))
WORKERS = int(os.environ.get("LOAD_TEST_WORKERS", "12"))


def request_once(_):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            elapsed = time.perf_counter() - started
            if response.status != 200 or payload.get("ok") is not True:
                return elapsed, "invalid health response"
            return elapsed, None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return time.perf_counter() - started, str(exc)


with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
    results = list(pool.map(request_once, range(REQUESTS)))
latencies = sorted(item[0] for item in results)
errors = [item[1] for item in results if item[1]]
p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
print(json.dumps({
    "requests": REQUESTS,
    "workers": WORKERS,
    "errors": len(errors),
    "mean_ms": round(statistics.mean(latencies) * 1000, 2),
    "p95_ms": round(p95 * 1000, 2),
    "max_ms": round(max(latencies) * 1000, 2),
}, indent=2))
if errors:
    raise SystemExit("Load test encountered errors: " + "; ".join(errors[:5]))
if p95 > 2.0:
    raise SystemExit(f"p95 latency exceeded 2000 ms: {p95 * 1000:.0f} ms")

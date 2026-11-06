"""Local Windows connector for NIFTY LAB.

One-shot by default. Use --watch only after a successful local NSE test.
No retries, proxying, user-agent rotation, or access-control bypass.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from indian_option_mcp_test import test_provider

IST = ZoneInfo("Asia/Kolkata")


def fetch_and_push(render_url: str, token: str) -> bool:
    result = test_provider()
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] NSE status={result.get('status')} HTTP={result.get('http_status')} "
          f"elapsed={result.get('elapsed_ms')}ms")
    if result.get("status") != "CONNECTED" or not result.get("rows"):
        print("No verified option-chain snapshot received.")
        print(result.get("message") or "Unknown provider response.")
        return False

    payload = {
        "status": "CONNECTED",
        "http_status": result.get("http_status"),
        "exchange_timestamp": result.get("exchange_timestamp"),
        "received_at": result.get("received_at"),
        "spot": result.get("spot"),
        "expiry_dates": result.get("expiry_dates") or [],
        "rows": result.get("rows") or [],
        "elapsed_ms": result.get("elapsed_ms"),
    }
    try:
        response = requests.post(
            render_url.rstrip("/") + "/api/local-market",
            json=payload,
            headers={"X-NIFTYLAB-TOKEN": token},
            timeout=20,
        )
        if response.status_code != 200:
            print(f"Render rejected the snapshot: HTTP {response.status_code}")
            print(response.text[:500])
            return False
        print("Snapshot delivered to Render:", response.json())
        return True
    except requests.RequestException as exc:
        print("Could not deliver snapshot to Render:", exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Push genuine NSE option-chain snapshots to NIFTY LAB.")
    parser.add_argument("--watch", action="store_true", help="Fetch once every 60 seconds; stops on any failed fetch.")
    args = parser.parse_args()

    render_url = os.getenv("NIFTYLAB_RENDER_URL", "").strip()
    token = os.getenv("NIFTYLAB_INGEST_TOKEN", "").strip()
    if not render_url or not token:
        print("Set NIFTYLAB_RENDER_URL and NIFTYLAB_INGEST_TOKEN before running.")
        return 2

    if not args.watch:
        return 0 if fetch_and_push(render_url, token) else 1

    while True:
        if not fetch_and_push(render_url, token):
            print("Stopping watch mode after a failed fetch. No retry will be attempted.")
            return 1
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())

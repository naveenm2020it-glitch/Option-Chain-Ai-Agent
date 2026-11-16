from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.nse_download_check import HEADERS, TIMEOUT, fetch_report, report_specs
from scripts.nse_ml_features import _index_features, _normalised_name, _participant_features

SOURCES = {
    "index_ohlc": "nifty_index_close_all",
    "participant_oi": "fo_participant_oi",
    "participant_volume": "fo_participant_volume",
}


def _csv_frame(payload: bytes, report_name: str) -> pd.DataFrame:
    decoded = payload.decode("utf-8-sig")
    lines = decoded.splitlines()
    header = 0
    if report_name in {"fo_participant_oi", "fo_participant_volume"}:
        for i, line in enumerate(lines[:15]):
            normalized = _normalised_name(line)
            if "clienttype" in normalized and "futureindexlong" in normalized:
                header = i
                break
    elif report_name == "nifty_index_close_all":
        for i, line in enumerate(lines[:15]):
            normalized = _normalised_name(line)
            if "indexname" in normalized and ("closing" in normalized or "close" in normalized):
                header = i
                break
    frame = pd.read_csv(io.StringIO("\n".join(lines[header:])), dtype=str, low_memory=False)
    if frame.empty:
        raise ValueError(f"{report_name}: CSV has no data rows")
    return frame


def _parse_source(name: str, payload: bytes) -> dict[str, float]:
    frame = _csv_frame(payload, name)
    if name == "nifty_index_close_all":
        features = _index_features(frame)
        if not features.get("index_data_available"):
            raise ValueError("NIFTY 50 index OHLC row/columns not present")
        return features
    prefix = "participant_oi" if name == "fo_participant_oi" else "participant_volume"
    features = _participant_features(frame, prefix)
    if not features.get(f"{prefix}_data_available"):
        raise ValueError(f"{name}: required participant categories/columns are incomplete")
    return features


def run(checkpoint_path: Path, output_dir: Path, delay_seconds: float = 0.35) -> dict:
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"Verified checkpoint missing: {checkpoint_path}")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    features = checkpoint.get("features")
    if not isinstance(features, dict) or len(features) != 494:
        raise RuntimeError("Repair blocked: expected exactly 494 verified feature rows")

    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=5, connect=5, read=5, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    session.mount("https://", HTTPAdapter(max_retries=retry))

    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "date_count": len(features),
        "source_results": {key: {"downloaded": 0, "already_present": 0, "unavailable": 0, "invalid": 0, "missing_dates": []}
                           for key in SOURCES},
        "errors": [],
        "b2_requests": 0,
        "training_allowed": False,
    }

    for day in sorted(features):
        row = features[day]
        row_features = row.setdefault("features", {})
        specs = {item[0]: item for item in report_specs(date.fromisoformat(day))}
        for source_key, report_name in SOURCES.items():
            availability_key = {
                "index_ohlc": "index_data_available",
                "participant_oi": "participant_oi_data_available",
                "participant_volume": "participant_volume_data_available",
            }[source_key]
            state = report["source_results"][source_key]
            if float(row_features.get(availability_key, 0.0)) >= 1.0:
                state["already_present"] += 1
                continue
            try:
                spec = specs[report_name]
                payload, meta = fetch_report(session, *spec)
                parsed = _parse_source(report_name, payload)
                if float(parsed.get(availability_key, 0.0)) < 1.0:
                    raise ValueError("source parsed but availability flag is not set")
                row_features.update(parsed)
                state["downloaded"] += 1
                # Keep a compact provenance record; raw NSE reports are stored as
                # workflow artifacts, never sent to B2 by this repair job.
                raw_name = f"{report_name}_{day}.csv"
                (output_dir / raw_name).write_bytes(payload)
                (output_dir / f"{raw_name}.sha256").write_text(
                    hashlib.sha256(payload).hexdigest(), encoding="utf-8"
                )
                row.setdefault("context_sources", {})[source_key] = {
                    "report_date": day, "file": raw_name, "sha256": meta["sha256"],
                    "url": meta["url"], "bytes": meta["bytes"],
                }
            except Exception as exc:
                state["unavailable"] += 1
                state["missing_dates"].append(day)
                report["errors"].append({
                    "date": day, "source": source_key, "error": str(exc)[:500]
                })
            if delay_seconds:
                time.sleep(delay_seconds)

    total = len(features)
    coverage = {}
    for source_key, result in report["source_results"].items():
        coverage[source_key] = (
            result["already_present"] + result["downloaded"]
        ) / total if total else 0.0
    report["coverage"] = coverage
    report["training_allowed"] = all(value >= 0.95 for value in coverage.values())
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["checkpoint_sha256"] = hashlib.sha256(
        json.dumps(checkpoint, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()

    checkpoint_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    (output_dir / "context_repair_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "coverage": coverage,
        "training_allowed": report["training_allowed"],
        "unavailable_by_source": {k: v["unavailable"] for k, v in report["source_results"].items()},
        "b2_requests": 0,
    }, indent=2), flush=True)
    if not report["training_allowed"]:
        raise SystemExit("Context repair incomplete: one or more NSE context sources remain below 95%; training is blocked.")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair missing historical NSE context data without B2 access.")
    parser.add_argument("--checkpoint", default="data/historical_audit/checkpoint.json")
    parser.add_argument("--output-dir", default="data/context_repair")
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    args = parser.parse_args()
    run(Path(args.checkpoint), Path(args.output_dir), args.delay_seconds)

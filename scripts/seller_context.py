from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from scripts.nse_ml_features import extract_nifty_features
from seller_levels import build_seller_levels


def _outer_archive(reports_dir: Path) -> bytes:
    manifest_path = reports_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("core_session_status") != "aligned" or not manifest.get("core_session_date"):
        raise ValueError("Current NSE reports are not aligned to one trading session")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, item in manifest.get("results", {}).items():
            if not isinstance(item, dict) or not item.get("file"):
                continue
            filename = Path(str(item["file"]))
            if filename.name != str(item["file"]):
                raise ValueError(f"Unsafe report filename in manifest: {item['file']}")
            path = reports_dir / filename.name
            if not path.is_file():
                raise FileNotFoundError(f"Manifest report file missing: {filename.name}")
            payload = path.read_bytes()
            expected = item.get("sha256")
            if expected and hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError(f"Report SHA-256 mismatch: {filename.name}")
            archive.writestr(filename.name, payload)
    return buffer.getvalue()


def snapshot_from_contracts(session_date: str, spot: float, contracts: list[dict[str, Any]]) -> dict:
    valid = []
    seen = set()
    session = date.fromisoformat(session_date)
    for item in contracts:
        if not isinstance(item, dict):
            continue
        if not item.get("expiry") or str(item.get("option_type", "")).upper() not in {"CE", "PE"}:
            continue
        try:
            expiry = date.fromisoformat(str(item["expiry"]))
            strike = float(item["strike"])
        except (TypeError, ValueError):
            raise ValueError("Option contract has an invalid expiry or strike.")
        if expiry < session or not math.isfinite(strike) or strike <= 0:
            raise ValueError("Option contract expiry/strike is outside valid bounds.")
        key = (expiry.isoformat(), strike, str(item["option_type"]).upper())
        if key in seen:
            raise ValueError(f"Duplicate NIFTY option contract: {key}")
        seen.add(key)
        numeric = {}
        for field in ("oi", "change_oi", "volume", "close"):
            try:
                value = float(item[field])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"Option contract is missing valid {field}.")
            if not math.isfinite(value):
                raise ValueError(f"Option contract has non-finite {field}.")
            numeric[field] = value
        if numeric["oi"] < 0 or numeric["volume"] < 0 or numeric["close"] <= 0:
            raise ValueError("Option contract has negative OI/volume or non-positive close.")
        valid.append((item, expiry, strike, numeric))
    if not valid:
        return {"status": "blocked", "session_date": session_date,
                "support": [], "resistance": [], "seller_exit_zones": [],
                "reason": "No validated NIFTY option contracts were available."}
    front_expiry = min(expiry for _, expiry, _, _ in valid)
    front = [entry for entry in valid if entry[1] == front_expiry]
    grouped: dict[float, dict] = defaultdict(dict)
    for item, expiry, strike, numeric in front:
        side = str(item["option_type"]).upper().lower()
        oi = numeric["oi"]
        change = numeric["change_oi"]
        volume = numeric["volume"]
        premium_raw = item.get("premium_return")
        previous_raw = item.get("previous_close")
        try:
            premium = float(premium_raw) if premium_raw is not None else None
        except (TypeError, ValueError):
            premium = None
        try:
            previous_close = float(previous_raw) if previous_raw is not None else None
        except (TypeError, ValueError):
            previous_close = None
        close = numeric["close"]
        premium_pct = (
            premium * 100
            if premium is not None and math.isfinite(premium)
            and previous_close is not None and math.isfinite(previous_close)
            and previous_close > 0 and close > 0
            else None
        )
        row = grouped[strike].setdefault(side, [])
        row.append((oi, change, volume, premium_pct))
    chain = []
    for strike, sides in sorted(grouped.items()):
        row = {"strike": strike}
        for side in ("ce", "pe"):
            values = sides.get(side, [])
            if not values:
                row.update({f"{side}_oi": None, f"{side}_change_oi": None,
                            f"{side}_volume": None, f"{side}_price_change_pct": None})
                continue
            weights = [v[2] if v[2] > 0 else v[0] for v in values]
            premium_values = [(v[3], w) for v, w in zip(values, weights) if v[3] is not None]
            weight_total = sum(w for _, w in premium_values)
            premium_avg = (sum(p * w for p, w in premium_values) / weight_total
                           if weight_total else None)
            row.update({
                f"{side}_oi": sum(v[0] for v in values),
                f"{side}_change_oi": sum(v[1] for v in values),
                f"{side}_volume": sum(v[2] for v in values),
                f"{side}_price_change_pct": premium_avg,
            })
        chain.append(row)
    return build_seller_levels({
        "data_quality": {"aligned": True, "session_date": session_date},
        "spot_price": float(spot),
        "option_chain": chain,
    })


def _weekday_gap(start: str, end: str) -> int:
    left, right = date.fromisoformat(start), date.fromisoformat(end)
    count = 0
    cursor = left.fromordinal(left.toordinal() + 1)
    while cursor < right:
        if cursor.weekday() < 5:
            count += 1
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    return count


def _window_summary(history_rows: list[tuple[str, dict]], window: int, as_of_date: str) -> dict:
    selected = history_rows[-window:]
    support: dict[int, list[float]] = defaultdict(list)
    resistance: dict[int, list[float]] = defaultdict(list)
    used = 0
    for day, levels in selected:
        if levels.get("status") != "analysis_complete":
            continue
        used += 1
        for item in levels.get("support", []):
            support[int(item["level"])].append(float(item["strength_score"]))
        for item in levels.get("resistance", []):
            resistance[int(item["level"])].append(float(item["strength_score"]))

    def rank(source: dict[int, list[float]]) -> list[dict]:
        values = []
        for level, scores in source.items():
            values.append({
                "level": level,
                "sessions_present": len(scores),
                "persistence_pct": round(100 * len(scores) / used, 1) if used else 0,
                "average_strength": round(sum(scores) / len(scores), 1),
            })
        return sorted(values, key=lambda item: (
            -item["sessions_present"], -item["average_strength"], item["level"]
        ))[:5]

    gap_weekdays = sum(
        _weekday_gap(left[0], right[0])
        for left, right in zip(selected, selected[1:])
    )
    if selected:
        gap_weekdays += _weekday_gap(selected[-1][0], as_of_date)
    complete = len(selected) == window and used == len(selected) and gap_weekdays == 0
    return {
        "requested_sessions": window,
        "sessions_available": len(selected),
        "sessions_analyzed": used,
        "latest_prior_session": selected[-1][0] if selected else None,
        "possible_missing_weekdays": gap_weekdays,
        "coverage_status": "complete" if complete else "incomplete_or_exchange_holiday_gap",
        "support_persistence": rank(support),
        "resistance_persistence": rank(resistance),
    }


def build_context(reports_dir: Path, checkpoint_path: Path, recent_history_path: Path | None = None) -> dict:
    manifest = json.loads((reports_dir / "manifest.json").read_text(encoding="utf-8"))
    session_date = str(manifest.get("core_session_date") or "")
    if manifest.get("core_session_status") != "aligned" or not session_date:
        raise ValueError("Current NSE manifest failed same-session alignment")
    extracted = extract_nifty_features(_outer_archive(reports_dir), date.fromisoformat(session_date))
    current = snapshot_from_contracts(
        session_date,
        float(extracted["features"]["spot_close"]),
        extracted.get("contracts", []),
    )
    if current.get("status") != "analysis_complete":
        raise ValueError(current.get("reason", "Current seller levels could not be built"))

    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    feature_rows = checkpoint.get("features")
    if not isinstance(feature_rows, dict) or len(feature_rows) != 494:
        raise ValueError("Historical context requires the verified 494-session checkpoint")
    history_by_date = {}
    for day in sorted(feature_rows):
        if day >= session_date:
            continue
        row = feature_rows[day]
        values = row.get("features", {}) if isinstance(row, dict) else {}
        contracts = row.get("contracts", []) if isinstance(row, dict) else []
        if not contracts or float(values.get("spot_close") or 0) <= 0:
            continue
        levels = snapshot_from_contracts(day, float(values["spot_close"]), contracts)
        history_by_date[day] = levels

    if recent_history_path and recent_history_path.is_file():
        recent_payload = json.loads(recent_history_path.read_text(encoding="utf-8"))
        recent_records = recent_payload.get("records", []) if isinstance(recent_payload, dict) else recent_payload
        for record in recent_records if isinstance(recent_records, list) else []:
            if not isinstance(record, dict):
                continue
            day = str(record.get("session_date") or "")
            levels = record.get("current")
            if day and day < session_date and isinstance(levels, dict):
                history_by_date[day] = levels

    history = sorted(history_by_date.items())

    return {
        "schema_version": 1,
        "status": "ready",
        "session_date": session_date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "spot_reference": current.get("spot_reference"),
        "current": current,
        "lookback": {
            "last_5_sessions": _window_summary(history, 5, session_date),
            "last_22_sessions": _window_summary(history, 22, session_date),
        },
        "provenance": {
            "current_source": "NSE EOD UDiFF F&O bhavcopy; same-session validated",
            "historical_source": "494-session integrity-verified feature checkpoint",
            "historical_sessions_available": len(history),
            "b2_reads": 0,
            "b2_writes": 0,
            "expiry_policy": "front unexpired NIFTY index-option expiry per session",
        },
        "interpretation": (
            "Support/resistance and persistence are seller-positioning proxies, not proof "
            "of participant identity or guaranteed defended levels."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build NIFTY seller levels with 5/22-session historical context.")
    parser.add_argument("--reports-dir", type=Path, default=Path("data/nse_reports"))
    parser.add_argument("--checkpoint", type=Path, default=Path("data/historical_audit/checkpoint.json"))
    parser.add_argument("--output", type=Path, default=Path("data/seller_context/seller_context.json"))
    parser.add_argument("--recent-history", type=Path, default=None)
    args = parser.parse_args()
    payload = build_context(args.reports_dir, args.checkpoint, args.recent_history)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "session_date": payload["session_date"],
        "current_support_levels": len(payload["current"]["support"]),
        "current_resistance_levels": len(payload["current"]["resistance"]),
        "last_5_sessions_analyzed": payload["lookback"]["last_5_sessions"]["sessions_analyzed"],
        "last_22_sessions_analyzed": payload["lookback"]["last_22_sessions"]["sessions_analyzed"],
        "b2_reads": 0,
        "b2_writes": 0,
    }, indent=2))


if __name__ == "__main__":
    main()

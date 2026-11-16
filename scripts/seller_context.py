from __future__ import annotations

import argparse
import hashlib
import io
import json
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
    valid = [
        item for item in contracts
        if isinstance(item, dict)
        and item.get("expiry")
        and item.get("option_type") in {"CE", "PE"}
        and float(item.get("strike") or 0) > 0
    ]
    if not valid:
        return {"status": "blocked", "session_date": session_date,
                "support": [], "resistance": [], "seller_exit_zones": [],
                "reason": "No validated NIFTY option contracts were available."}
    front_expiry = min(str(item["expiry"]) for item in valid)
    front = [item for item in valid if str(item["expiry"]) == front_expiry]
    grouped: dict[float, dict] = defaultdict(dict)
    for item in front:
        strike = float(item["strike"])
        side = str(item["option_type"]).upper().lower()
        oi = max(0.0, float(item.get("oi") or 0))
        change = float(item.get("change_oi") or 0)
        volume = max(0.0, float(item.get("volume") or 0))
        premium = item.get("premium_return")
        premium_pct = float(premium) * 100 if premium is not None else None
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


def _window_summary(history_rows: list[tuple[str, dict]], window: int) -> dict:
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

    return {
        "requested_sessions": window,
        "sessions_available": len(selected),
        "sessions_analyzed": used,
        "support_persistence": rank(support),
        "resistance_persistence": rank(resistance),
    }


def build_context(reports_dir: Path, checkpoint_path: Path) -> dict:
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
    history = []
    for day in sorted(feature_rows):
        if day >= session_date:
            continue
        row = feature_rows[day]
        values = row.get("features", {}) if isinstance(row, dict) else {}
        contracts = row.get("contracts", []) if isinstance(row, dict) else []
        if not contracts or float(values.get("spot_close") or 0) <= 0:
            continue
        levels = snapshot_from_contracts(day, float(values["spot_close"]), contracts)
        history.append((day, levels))

    return {
        "schema_version": 1,
        "status": "ready",
        "session_date": session_date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "spot_reference": current.get("spot_reference"),
        "current": current,
        "lookback": {
            "last_5_sessions": _window_summary(history, 5),
            "last_22_sessions": _window_summary(history, 22),
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
    args = parser.parse_args()
    payload = build_context(args.reports_dir, args.checkpoint)
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

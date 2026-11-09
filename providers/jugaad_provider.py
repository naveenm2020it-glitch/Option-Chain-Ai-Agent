"""Unofficial jugaad-data adapter for NIFTY index option-chain snapshots.

This adapter is intentionally disabled unless the operator explicitly enables
it AND confirms that their intended automated use is permitted. It does not
bypass NSE access controls, retry blocks, or alter browser fingerprints.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class JugaadProviderError(RuntimeError):
    pass


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def jugaad_configuration() -> dict[str, bool]:
    return {
        "enabled": _enabled("JUGAAD_PROVIDER_ENABLED"),
        "automated_use_permitted": _enabled("JUGAAD_AUTOMATED_USE_PERMITTED"),
    }


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y, %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IST).isoformat(timespec="seconds")
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        return None


def _leg(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "lastPrice": _number(raw.get("lastPrice")),
        "change": _number(raw.get("change")),
        "pChange": _number(raw.get("pChange")),
        "totalTradedVolume": _number(raw.get("totalTradedVolume")),
        "openInterest": _number(raw.get("openInterest")),
        "changeinOpenInterest": _number(raw.get("changeinOpenInterest")),
        "impliedVolatility": _number(raw.get("impliedVolatility")),
        "bidprice": _number(raw.get("bidprice")),
        "askPrice": _number(raw.get("askPrice")),
        "bidQty": _number(raw.get("bidQty")),
        "askQty": _number(raw.get("askQty")),
    }


def normalize_option_chain(payload: dict[str, Any], *, requested_expiry: str | None = None) -> dict[str, Any]:
    """Convert NSELive.index_option_chain JSON into the app's normalized schema."""
    records = payload.get("records") if isinstance(payload.get("records"), dict) else payload
    data = records.get("data") if isinstance(records, dict) else None
    if not isinstance(data, list):
        raise JugaadProviderError("jugaad-data returned no option-chain records.")

    expiry_dates = records.get("expiryDates") or payload.get("expiryDates") or []
    selected_expiry = requested_expiry or records.get("expiryDate")
    if not selected_expiry and expiry_dates:
        selected_expiry = expiry_dates[0]

    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row_expiry = item.get("expiryDate") or selected_expiry
        if selected_expiry and row_expiry != selected_expiry:
            continue
        strike = _number(item.get("strikePrice"))
        ce = _leg(item.get("CE"))
        pe = _leg(item.get("PE"))
        # Downstream quality validation requires both legs to have real LTPs.
        if strike is None or strike <= 0 or not ce or not pe:
            continue
        if not ce.get("lastPrice") or not pe.get("lastPrice"):
            continue
        rows.append({
            "strike": strike,
            "expiry": str(row_expiry or ""),
            "ce": ce,
            "pe": pe,
        })

    rows.sort(key=lambda row: row["strike"])
    timestamp = _timestamp(records.get("timestamp") or payload.get("timestamp"))
    spot = _number(records.get("underlyingValue") or payload.get("underlyingValue"))
    if not selected_expiry and rows:
        selected_expiry = rows[0]["expiry"]
    if selected_expiry and selected_expiry not in expiry_dates:
        expiry_dates = [selected_expiry, *[x for x in expiry_dates if x != selected_expiry]]

    return {
        "instrument": "NIFTY",
        "source": "jugaad-data (unofficial NSE website wrapper)",
        "source_timestamp": timestamp,
        "received_at": datetime.now(IST).isoformat(timespec="seconds"),
        "spot": spot,
        "expiry_dates": expiry_dates if isinstance(expiry_dates, list) else [],
        "selected_expiry": selected_expiry,
        "rows": rows,
        "provider_notice": "Unofficial third-party wrapper; availability and permitted automated use are not guaranteed.",
    }


class JugaadOptionChainProvider:
    name = "jugaad-data"
    capabilities = {
        "instrument": "NIFTY",
        "option_chain": True,
        "spot": True,
        "documented_api": False,
        "automated_use_permitted": False,
        "requires_credentials": False,
        "requires_payment": False,
    }

    def configuration_status(self) -> tuple[bool, str | None]:
        cfg = jugaad_configuration()
        if not cfg["enabled"]:
            return False, "jugaad-data provider is installed but disabled."
        if not cfg["automated_use_permitted"]:
            return False, (
                "jugaad-data is unofficial and uses NSE website endpoints. "
                "Automated use is disabled until its permitted use is independently confirmed."
            )
        return True, None

    def fetch_snapshot(self, instrument: str = "NIFTY", expiry: str | None = None) -> dict[str, Any]:
        if instrument.upper() != "NIFTY":
            raise JugaadProviderError("This adapter is restricted to NIFTY.")
        allowed, reason = self.configuration_status()
        if not allowed:
            raise JugaadProviderError(reason or "jugaad-data provider is disabled.")
        try:
            from jugaad_data.nse import NSELive
        except ImportError as exc:
            raise JugaadProviderError("jugaad-data is not installed in this deployment.") from exc
        try:
            client = NSELive()
            contract_info = client.option_chain_contract_info("NIFTY")
            expiry_dates = contract_info.get("expiryDates", []) if isinstance(contract_info, dict) else []
            selected = expiry or (expiry_dates[0] if expiry_dates else None)
            payload = client.index_option_chain("NIFTY", expiry=selected)
            snapshot = normalize_option_chain(payload, requested_expiry=selected)
            if not snapshot["expiry_dates"] and expiry_dates:
                snapshot["expiry_dates"] = expiry_dates
            return snapshot
        except JugaadProviderError:
            raise
        except Exception as exc:
            raise JugaadProviderError(f"jugaad-data fetch failed: {type(exc).__name__}: {exc}") from exc

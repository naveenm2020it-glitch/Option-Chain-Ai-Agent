"""Fail-closed validation for normalized market snapshots."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping


@dataclass(frozen=True)
class QualityResult:
    status: str
    is_fresh: bool
    age_seconds: float | None
    checks: tuple[str, ...]
    warnings: tuple[str, ...]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        and value > 0
    )


def _valid_leg(leg: Any) -> bool:
    if not isinstance(leg, Mapping):
        return False
    ltp = leg.get("lastPrice", leg.get("ltp"))
    if not _positive_number(ltp):
        return False
    for key in ("openInterest", "changeinOpenInterest", "totalTradedVolume"):
        value = leg.get(key)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(value)
        ):
            return False
    return True


def validate_snapshot(
    snapshot: Mapping[str, Any],
    *,
    provider_permitted: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = 15.0,
    future_tolerance_seconds: float = 5.0,
) -> QualityResult:
    """Validate a normalized snapshot; never infer LIVE from HTTP 200.

    provider_permitted must come from trusted application configuration,
    not from an untrusted provider response.
    """
    checks: list[str] = []
    warnings: list[str] = []
    if not provider_permitted:
        return QualityResult(
            status="UNAVAILABLE",
            is_fresh=False,
            age_seconds=None,
            checks=(),
            warnings=(),
            error="No provider has been approved for this automated use.",
        )

    if str(snapshot.get("instrument", "")).upper() != "NIFTY":
        return QualityResult("UNAVAILABLE", False, None, (), (), "Instrument must be NIFTY.")
    checks.append("instrument")

    source_time = _parse_aware(snapshot.get("source_timestamp"))
    received_time = _parse_aware(snapshot.get("received_at"))
    if source_time is None:
        return QualityResult(
            "UNAVAILABLE", False, None, tuple(checks), tuple(warnings),
            "Missing or timezone-naive source timestamp.",
        )
    if received_time is None:
        return QualityResult(
            "UNAVAILABLE", False, None, tuple(checks), tuple(warnings),
            "Missing or timezone-naive received_at timestamp.",
        )
    checks.extend(("source_timestamp", "received_at"))

    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (reference - source_time).total_seconds()
    if age < -future_tolerance_seconds:
        return QualityResult(
            "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
            "Source timestamp is unexpectedly in the future.",
        )
    if age < 0:
        warnings.append("Source timestamp is slightly ahead of local clock tolerance.")
        age = 0.0

    if not _positive_number(snapshot.get("spot")):
        return QualityResult(
            "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
            "Missing or invalid NIFTY spot value.",
        )
    checks.append("spot")

    rows = snapshot.get("rows")
    if not isinstance(rows, list) or not rows:
        return QualityResult(
            "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
            "Option-chain rows are empty or malformed.",
        )
    checks.append("rows_present")

    selected_expiry = snapshot.get("selected_expiry")
    expiries = snapshot.get("expiry_dates")
    if selected_expiry is not None and (
        not isinstance(expiries, list) or selected_expiry not in expiries
    ):
        return QualityResult(
            "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
            "Selected expiry is not present in expiry_dates.",
        )

    seen: set[tuple[str, float]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return QualityResult(
                "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
                "An option-chain row is not an object.",
            )
        strike = row.get("strike")
        expiry = row.get("expiry")
        if not _positive_number(strike) or not isinstance(expiry, str) or not expiry:
            return QualityResult(
                "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
                "A row has an invalid strike or expiry.",
            )
        identity = (expiry, float(strike))
        if identity in seen:
            return QualityResult(
                "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
                "Duplicate strike/expiry rows detected.",
            )
        seen.add(identity)
        if selected_expiry is not None and expiry != selected_expiry:
            return QualityResult(
                "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
                "Snapshot mixes contracts from different expiries.",
            )
        if not _valid_leg(row.get("ce")) or not _valid_leg(row.get("pe")):
            return QualityResult(
                "UNAVAILABLE", False, age, tuple(checks), tuple(warnings),
                "A row is missing a valid CE or PE LTP quote.",
            )
    checks.extend(("unique_strikes", "single_expiry", "ce_pe_quotes"))

    if age > max_age_seconds:
        return QualityResult(
            "STALE", False, age, tuple(checks), tuple(warnings),
            f"Source snapshot is older than {max_age_seconds:g} seconds.",
        )
    checks.append("freshness")
    return QualityResult("LIVE", True, age, tuple(checks), tuple(warnings), None)

"""Provider-neutral contract for normalized market-data sources."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    instrument: str
    option_chain: bool
    spot: bool
    option_greeks: bool = False
    option_candles: bool = False
    spot_candles: bool = False
    documented_api: bool = False
    automated_use_permitted: bool = False
    requires_credentials: bool = False
    requires_payment: bool = False


class MarketDataProvider(Protocol):
    """A provider returns the internal normalized snapshot contract."""

    name: str
    capabilities: ProviderCapabilities

    def fetch_snapshot(self, instrument: str, expiry: str | None = None) -> dict[str, Any]:
        """Return a normalized snapshot with source and received timestamps."""
        ...


def provider_is_eligible(
    capabilities: ProviderCapabilities,
    *,
    allow_credentials: bool = False,
    allow_paid: bool = False,
) -> tuple[bool, str | None]:
    """Fail closed unless automated use is permitted and user constraints fit."""
    if not capabilities.automated_use_permitted:
        return False, "Provider terms do not confirm permission for automated use."
    if not capabilities.option_chain:
        return False, "Provider does not supply an option chain."
    if not capabilities.spot:
        return False, "Provider does not supply the underlying spot value."
    if capabilities.requires_credentials and not allow_credentials:
        return False, "Provider requires credentials, which are not enabled."
    if capabilities.requires_payment and not allow_paid:
        return False, "Provider requires payment, which is not enabled."
    return True, None

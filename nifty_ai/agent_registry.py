"""Canonical metadata for every specialist and the final consensus agent.

The registry is the single source of truth for backend counts and dashboard labels.
"""
AGENT_REGISTRY = (
    {"key": "price_action", "name": "Price Action", "role": "specialist", "scope": "EOD"},
    {"key": "option_chain", "name": "Option Chain", "role": "specialist", "scope": "EOD"},
    {"key": "volume_profile", "name": "Volume Profile", "role": "specialist", "scope": "EOD_LIMITED"},
    {"key": "trend", "name": "Trend", "role": "specialist", "scope": "EOD"},
    {"key": "volatility", "name": "Volatility", "role": "specialist", "scope": "EOD"},
    {"key": "pattern_recognition", "name": "Pattern Recognition", "role": "specialist", "scope": "EOD"},
    {"key": "historical_validation", "name": "Historical Validation", "role": "specialist", "scope": "EOD"},
    {"key": "buyer_seller_absorption", "name": "Buyer & Seller Absorption", "role": "specialist", "scope": "EOD_LIMITED"},
    {"key": "institutional_positioning", "name": "Institutional Positioning & Migration", "role": "specialist", "scope": "EOD"},
    {"key": "prediction_accuracy", "name": "Prediction Accuracy & Calibration", "role": "specialist", "scope": "NOT_CALIBRATED"},
    {"key": "next_day_regime", "name": "Next-Day Regime", "role": "specialist", "scope": "EOD"},
    {"key": "zone_confluence", "name": "Zone Confluence & Quality", "role": "specialist", "scope": "EOD"},
    {"key": "final_consensus", "name": "Final Consensus", "role": "consensus", "scope": "EOD"},
)

SPECIALIST_COUNT = sum(1 for item in AGENT_REGISTRY if item["role"] == "specialist")
AGENT_COUNT = len(AGENT_REGISTRY)


def public_agent_registry():
    """Return JSON-safe registry metadata for the UI."""
    return [dict(item) for item in AGENT_REGISTRY]

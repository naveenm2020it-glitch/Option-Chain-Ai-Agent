"""Conservative multi-session FII/DII cash-flow trend classification."""
from collections import defaultdict
from datetime import date


MIN_SESSIONS_FOR_TREND = 3


def summarize_cash_flow_trend(rows, minimum_sessions=MIN_SESSIONS_FOR_TREND):
    """Classify dated cash net flows; never infer a trend from one session.

    Inputs are already-normalized rows with category, net and trading_date.
    Missing dates and unrecognized participant categories are excluded.
    """
    by_category = defaultdict(dict)
    for row in rows or []:
        category = str(row.get("category") or "").strip().upper()
        raw_date = row.get("trading_date")
        try:
            day = date.fromisoformat(str(raw_date)[:10]).isoformat()
        except (TypeError, ValueError):
            continue
        if "FII" in category or "FPI" in category:
            key = "FII"
        elif "DII" in category:
            key = "DII"
        else:
            continue
        try:
            net = float(row.get("net"))
        except (TypeError, ValueError):
            continue
        if net != net:
            continue
        by_category[key][day] = net

    result = {}
    for key in ("FII", "DII"):
        sessions = by_category.get(key, {})
        days = sorted(sessions)
        values = [sessions[day] for day in days]
        if len(days) < minimum_sessions:
            result[key] = {
                "status": "INSUFFICIENT HISTORY",
                "sessions": len(days),
                "required_sessions": minimum_sessions,
                "net_total": sum(values) if values else None,
                "positive_sessions": sum(value > 0 for value in values),
                "negative_sessions": sum(value < 0 for value in values),
                "classification": None,
                "dates": days,
            }
            continue
        total = sum(values)
        positive = sum(value > 0 for value in values)
        negative = sum(value < 0 for value in values)
        classification = (
            "NET_BUYING_TREND" if total > 0 and positive > negative else
            "NET_SELLING_TREND" if total < 0 and negative > positive else
            "MIXED"
        )
        result[key] = {
            "status": "PARTIAL — EOD CASH FLOW TREND",
            "sessions": len(days),
            "required_sessions": minimum_sessions,
            "net_total": total,
            "positive_sessions": positive,
            "negative_sessions": negative,
            "classification": classification,
            "dates": days,
        }
    return result

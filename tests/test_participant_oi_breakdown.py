import pandas as pd

from nifty_ai import analytics


def test_participant_oi_exposes_each_participant_and_keeps_raw_long_short_fields(monkeypatch):
    frame = pd.DataFrame({
        "Client Type": ["FII", "DII", "PRO", "CLIENT", "TOTAL"],
        "Future Index Long": [100, 80, 60, 40, 280],
        "Future Index Short": [70, 90, 55, 45, 260],
        "Option Index Call Long": [50, 40, 30, 20, 140],
        "Option Index Call Short": [30, 45, 35, 25, 135],
        "Option Index Put Long": [20, 25, 15, 10, 70],
        "Option Index Put Short": [35, 30, 20, 15, 100],
    })
    monkeypatch.setattr(analytics, "load_tables", lambda _: [("oi", frame)])
    result = analytics.extract_participant_summary([{
        "report_type": "participant_oi", "file_path": "unused.csv", "row_count": 5
    }])
    detail = result["sources"]["participant_oi"]
    assert len(detail["participant_positions"]) == 4
    fii = next(row for row in detail["participant_positions"] if row["participant"] == "FII")
    assert fii["future_index_net"] == 30
    assert fii["option_directional_proxy_contracts"] == 35
    assert fii["combined_directional_proxy_contracts"] == 65
    assert "may include hedges" in fii["interpretation"]

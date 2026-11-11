from nifty_ai.eod_intelligence import _wilson, walk_forward_zone_validation, build_defence_map


def test_wilson_interval_is_bounded_and_contains_rate():
    low, high = _wilson(8, 10)
    assert 0 <= low <= 0.8 <= high <= 1


def test_walk_forward_validation_does_not_claim_calibration_without_prediction_log():
    rows = []
    for i in range(45):
        base = 22000 + (i % 5) * 5
        rows.append({"date": f"2025-01-{i+1:02d}", "open": base, "high": base + 30,
                     "low": base - 30, "close": base + (5 if i % 2 else -5)})
    result = walk_forward_zone_validation(rows)
    assert result["forecast_probability_calibration"] == "NOT_CALIBRATED_NO_PROSPECTIVE_PROBABILITY_LOG"
    assert result["sessions_available"] == 45


def test_defence_map_keeps_buyer_and_seller_sides_separate():
    rows = [{"open": 100, "high": 110, "low": 90, "close": 105}]
    options = {"available": True, "top_put_oi_strike": 90, "top_call_oi_strike": 110,
               "top_put_oi_share_pct": 40, "top_call_oi_share_pct": 45,
               "put_writing_strikes": [90], "call_writing_strikes": [110]}
    levels = {"support": [], "resistance": []}
    result = build_defence_map(rows, options, levels)
    assert result["buyer_defence"][0]["center"] == 90
    assert result["seller_defence"][0]["center"] == 110
    assert result["score_type"] == "EVIDENCE_SCORE_NOT_PROBABILITY"


def test_option_positioning_uses_nearest_expiry_and_nifty_only(monkeypatch):
    import pandas as pd
    from nifty_ai import eod_intelligence

    frame = pd.DataFrame([
        {"TckrSymb": "NIFTY", "OptnTp": "CE", "StrkPric": 25000, "OpnIntrst": 1000,
         "ChngInOpnIntrst": 200, "XpryDt": "2026-11-12", "TtlTradgVol": 500,
         "ClsPric": 100, "PrvsClsgPric": 110},
        {"TckrSymb": "NIFTY", "OptnTp": "PE", "StrkPric": 24900, "OpnIntrst": 1200,
         "ChngInOpnIntrst": 250, "XpryDt": "2026-11-12", "TtlTradgVol": 600,
         "ClsPric": 95, "PrvsClsgPric": 105},
        {"TckrSymb": "NIFTY", "OptnTp": "CE", "StrkPric": 25000, "OpnIntrst": 9000,
         "ChngInOpnIntrst": 500, "XpryDt": "2026-11-19", "TtlTradgVol": 900,
         "ClsPric": 80, "PrvsClsgPric": 100},
        {"TckrSymb": "BANKNIFTY", "OptnTp": "CE", "StrkPric": 56000, "OpnIntrst": 99999,
         "ChngInOpnIntrst": 9999, "XpryDt": "2026-11-12", "TtlTradgVol": 999,
         "ClsPric": 10, "PrvsClsgPric": 20},
    ])
    monkeypatch.setattr(eod_intelligence, "load_tables", lambda _: [("options.csv", frame)])
    result = eod_intelligence._load_option_rows(
        [{"report_type": "fo_bhavcopy", "file_path": "unused", "trading_date": "2026-11-11"}],
        "2026-11-11",
    )
    assert result["expiry"] == "2026-11-12"
    assert len(result["strikes"]) == 2
    assert result["call_writing_strikes"] == [25000.0]
    assert result["put_writing_strikes"] == [24900.0]


def test_oi_migration_compares_same_expiry_across_uploaded_sessions(monkeypatch):
    from nifty_ai import eod_intelligence

    previous = {"available": True, "expiry": "2026-11-19", "top_call_oi_strike": 25000,
                "top_put_oi_strike": 24900,
                "strikes": [{"strike": 25000, "ce_oi": 1000, "pe_oi": 100},
                            {"strike": 24900, "ce_oi": 100, "pe_oi": 900}]}
    current = {"available": True, "expiry": "2026-11-19", "top_call_oi_strike": 25100,
               "top_put_oi_strike": 25000,
               "strikes": [{"strike": 25100, "ce_oi": 1800, "pe_oi": 50},
                           {"strike": 25000, "ce_oi": 1200, "pe_oi": 1100}]}

    def fake_load(items, day):
        return current if day == "2026-11-11" else previous

    monkeypatch.setattr(eod_intelligence, "_load_option_rows", fake_load)
    reports = [
        {"report_type": "fo_bhavcopy", "trading_date": "2026-11-10"},
        {"report_type": "fo_bhavcopy", "trading_date": "2026-11-11"},
    ]
    result = eod_intelligence._compare_oi_migration(reports, "2026-11-11")
    assert result["available"] is True
    assert result["call_wall_shift_points"] == 100
    assert result["put_wall_shift_points"] == 100


def test_option_pressure_labels_cover_four_eod_combinations_and_missing_fields():
    from nifty_ai.eod_intelligence import _classify_option_pressure

    assert _classify_option_pressure(100, 5) == "LONG_BUILDUP_PROXY"
    assert _classify_option_pressure(100, -5) == "SHORT_BUILDUP_PROXY"
    assert _classify_option_pressure(-100, 5) == "SHORT_COVERING_PROXY"
    assert _classify_option_pressure(-100, -5) == "LONG_UNWINDING_PROXY"
    assert _classify_option_pressure(100, None) == "INSUFFICIENT_FIELDS"
    assert _classify_option_pressure(100, 5, has_oi_change=False) == "INSUFFICIENT_FIELDS"


def test_option_pressure_summary_marks_missing_required_fields_partial():
    from nifty_ai.eod_intelligence import _summarize_option_pressure

    result = _summarize_option_pressure(
        [{"ce_pressure_label": "INSUFFICIENT_FIELDS", "pe_pressure_label": "INSUFFICIENT_FIELDS"}],
        {"oi", "volume"},
    )
    assert result["status"] == "PARTIAL"
    assert result["classification_counts"]["ce"]["INSUFFICIENT_FIELDS"] == 1
    assert "change_in_open_interest" in result["required_fields"]

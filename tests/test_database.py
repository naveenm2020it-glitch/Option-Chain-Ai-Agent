from storage.database import ResearchDatabase


def test_wait_analysis_and_raw_snapshot_are_persisted(tmp_path):
    db = ResearchDatabase(tmp_path / "history.sqlite3")
    db.initialize()
    snapshot = {
        "instrument": "NIFTY",
        "status": "UNAVAILABLE",
        "source": None,
        "source_timestamp": None,
        "received_at": "2026-11-08T18:00:00+05:30",
        "spot": None,
        "rows": [],
    }
    analysis = {
        "direction": "WAIT",
        "decision": "NO_TRADE",
        "missing_inputs": ["verified market feed", "premium candles"],
        "engine_version": "rules-v1",
    }
    snapshot_id, analysis_id = db.save_analysis(snapshot, analysis)
    assert snapshot_id > 0
    assert analysis_id > 0
    saved = db.list_analyses()
    assert len(saved) == 1
    assert saved[0]["direction"] == "WAIT"
    assert saved[0]["decision"] == "NO_TRADE"


def test_signal_events_are_append_only_and_ordered(tmp_path):
    db = ResearchDatabase(tmp_path / "history.sqlite3")
    db.initialize()
    snapshot_id, analysis_id = db.save_analysis(
        {"instrument": "NIFTY", "status": "LIVE", "rows": []},
        {"direction": "BULLISH", "decision": "TRADE_CANDIDATE"},
    )
    signal_id = db.create_signal(
        analysis_id,
        {
            "contract_key": "NIFTY-2026-11-12-24000-CE",
            "side": "CE",
            "entry_trigger": 100,
            "target_price": 120,
            "stop_price": 90,
            "horizon_seconds": 900,
        },
    )
    db.append_signal_event(signal_id, "CANDIDATE", "2026-11-09T09:30:00+05:30")
    db.append_signal_event(signal_id, "ARMED", "2026-11-09T09:31:00+05:30")
    events = db.list_signal_events(signal_id)
    assert [event["event_type"] for event in events] == ["CANDIDATE", "ARMED"]


def test_analysis_pagination_bounds_are_checked(tmp_path):
    db = ResearchDatabase(tmp_path / "history.sqlite3")
    db.initialize()
    try:
        db.list_analyses(limit=0)
    except ValueError:
        pass
    else:
        raise AssertionError("Expected invalid page size to be rejected.")

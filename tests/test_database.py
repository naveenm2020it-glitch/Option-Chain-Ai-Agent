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


def test_signal_journal_and_performance_summary(tmp_path):
    db = ResearchDatabase(tmp_path / "journal.sqlite3")
    db.initialize()
    _, analysis_id = db.save_analysis(
        {"instrument": "NIFTY", "status": "LIVE", "rows": []},
        {"direction": "BULLISH", "decision": "SIGNAL_CANDIDATE"},
    )
    signal_id = db.create_signal(analysis_id, {
        "contract_key": "NIFTY|2026-11-12|24000|CE",
        "side": "CE", "entry_trigger": 100, "actual_entry": 101,
        "target_price": 121, "stop_price": 91, "horizon_seconds": 900,
        "state": "ACTIVE", "created_at": "2026-11-09T09:30:00+05:30",
    })
    db.update_signal(signal_id, state="CLOSED", outcome="TARGET",
                      triggered_at="2026-11-09T09:30:00+05:30",
                      closed_at="2026-11-09T09:31:00+05:30",
                      gross_points=20, net_points=20)
    db.append_signal_event(signal_id, "TARGET", "2026-11-09T09:31:00+05:30",
                           observed_price=121)
    records = db.list_signals()
    assert len(records) == 1
    assert records[0]["outcome"] == "TARGET"
    summary = db.performance_summary()
    assert summary["targets"] == 1
    assert summary["accuracy"] == 100.0
    assert summary["sample_is_statistically_validated"] is False


def test_analysis_source_deduplication(tmp_path):
    db = ResearchDatabase(tmp_path / "dedupe.sqlite3")
    db.initialize()
    snapshot = {
        "instrument": "NIFTY", "status": "LIVE", "selected_expiry": "2026-11-12",
        "source_timestamp": "2026-11-09T09:30:00+05:30", "rows": [],
    }
    db.save_analysis(snapshot, {"direction": "WAIT", "decision": "WARMING_UP"})
    assert db.analysis_source_seen("2026-11-09T09:30:00+05:30", "2026-11-12") is True

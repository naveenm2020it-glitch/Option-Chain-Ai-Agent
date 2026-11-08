"""Small SQLite event store for NIFTY option-chain research history."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,
    instrument TEXT NOT NULL,
    expiry TEXT,
    spot REAL,
    source_timestamp TEXT,
    received_at TEXT,
    status TEXT NOT NULL,
    quality_json TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS option_chain_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id) ON DELETE CASCADE,
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,
    ce_json TEXT NOT NULL,
    pe_json TEXT NOT NULL,
    UNIQUE(snapshot_id, expiry, strike)
);

CREATE TABLE IF NOT EXISTS candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,
    instrument_type TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    interval TEXT NOT NULL,
    candle_timestamp TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    source_timestamp TEXT,
    quality_status TEXT NOT NULL,
    UNIQUE(provider, instrument_type, contract_key, interval, candle_timestamp)
);

CREATE TABLE IF NOT EXISTS greeks_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id) ON DELETE CASCADE,
    contract_key TEXT NOT NULL,
    model TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    rho REAL,
    calculated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER REFERENCES market_snapshots(id),
    created_at TEXT NOT NULL,
    direction TEXT NOT NULL,
    evidence_score REAL,
    confidence_calibrated REAL,
    evidence_json TEXT NOT NULL,
    counter_evidence_json TEXT NOT NULL,
    missing_inputs_json TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    decision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    contract_key TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_trigger REAL,
    actual_entry REAL,
    target_price REAL,
    stop_price REAL,
    horizon_seconds INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    triggered_at TEXT,
    closed_at TEXT,
    outcome TEXT,
    gross_points REAL,
    net_points REAL,
    unresolved_reason TEXT
);

CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    observed_price REAL,
    candle_id INTEGER REFERENCES candles(id),
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_received_at ON market_snapshots(received_at);
CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
CREATE INDEX IF NOT EXISTS idx_signal_events_signal_id ON signal_events(signal_id);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ResearchDatabase:
    """Connection-per-operation SQLite repository; safe for local single-user use."""

    def __init__(self, path: str | Path = "data/nifty_research.sqlite3") -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def save_analysis(
        self,
        snapshot: Mapping[str, Any],
        analysis: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Persist every analysis, including WAIT and NO_TRADE decisions."""
        rows = snapshot.get("rows")
        if not isinstance(rows, list):
            rows = []
        created_at = str(analysis.get("created_at") or _now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO market_snapshots
                (provider, instrument, expiry, spot, source_timestamp, received_at,
                 status, quality_json, raw_payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.get("source"),
                    str(snapshot.get("instrument") or "NIFTY"),
                    snapshot.get("selected_expiry"),
                    snapshot.get("spot"),
                    snapshot.get("source_timestamp"),
                    snapshot.get("received_at"),
                    str(snapshot.get("status") or "UNAVAILABLE"),
                    _json(snapshot.get("quality") or {}),
                    _json(snapshot),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                if row.get("strike") is None or not row.get("expiry"):
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO option_chain_rows
                    (snapshot_id, strike, expiry, ce_json, pe_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        float(row["strike"]),
                        str(row["expiry"]),
                        _json(row.get("ce") or {}),
                        _json(row.get("pe") or {}),
                    ),
                )
            cursor = connection.execute(
                """
                INSERT INTO analyses
                (snapshot_id, created_at, direction, evidence_score,
                 confidence_calibrated, evidence_json, counter_evidence_json,
                 missing_inputs_json, engine_version, decision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    created_at,
                    str(analysis.get("direction") or "WAIT"),
                    analysis.get("evidence_score"),
                    analysis.get("confidence_calibrated"),
                    _json(analysis.get("evidence") or []),
                    _json(analysis.get("counter_evidence") or []),
                    _json(analysis.get("missing_inputs") or []),
                    str(analysis.get("engine_version") or "rules-v1"),
                    str(analysis.get("decision") or "WAIT"),
                ),
            )
            analysis_id = int(cursor.lastrowid)
        return snapshot_id, analysis_id

    def create_signal(self, analysis_id: int, signal: Mapping[str, Any]) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signals
                (analysis_id, contract_key, side, entry_trigger, actual_entry,
                 target_price, stop_price, horizon_seconds, state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    str(signal["contract_key"]),
                    str(signal["side"]),
                    signal.get("entry_trigger"),
                    signal.get("actual_entry"),
                    signal.get("target_price"),
                    signal.get("stop_price"),
                    int(signal.get("horizon_seconds", 900)),
                    str(signal.get("state") or "CANDIDATE"),
                    str(signal.get("created_at") or _now()),
                ),
            )
            return int(cursor.lastrowid)

    def append_signal_event(
        self,
        signal_id: int,
        event_type: str,
        event_timestamp: str,
        *,
        observed_price: float | None = None,
        candle_id: int | None = None,
        details: Any = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signal_events
                (signal_id, event_type, event_timestamp, observed_price, candle_id, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (signal_id, event_type, event_timestamp, observed_price, candle_id, _json(details or {})),
            )
            return int(cursor.lastrowid)

    def list_analyses(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("limit must be 1..500 and offset must be non-negative.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM analyses ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_signal_events(self, signal_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signal_events WHERE signal_id = ? ORDER BY event_timestamp, id",
                (signal_id,),
            ).fetchall()
            return [dict(row) for row in rows]


    def analysis_source_seen(self, source_timestamp: str, expiry: str | None) -> bool:
        """Avoid persisting duplicate analyses for the same source snapshot."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM market_snapshots
                   WHERE source_timestamp = ? AND COALESCE(expiry, '') = COALESCE(?, '')
                   LIMIT 1""",
                (source_timestamp, expiry),
            ).fetchone()
            return row is not None

    def list_signals(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500.")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT s.*, a.direction, a.evidence_score, a.created_at AS analysis_created_at
                   FROM signals s JOIN analyses a ON a.id = s.analysis_id
                   ORDER BY s.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_active_signals(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signals WHERE state IN ('CANDIDATE', 'TRIGGERED', 'ACTIVE') ORDER BY id",
            ).fetchall()
            return [dict(row) for row in rows]

    def update_signal(
        self, signal_id: int, *, state: str, outcome: str | None = None,
        triggered_at: str | None = None, closed_at: str | None = None,
        actual_entry: float | None = None, target_price: float | None = None,
        stop_price: float | None = None, gross_points: float | None = None,
        net_points: float | None = None, unresolved_reason: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE signals SET state = ?, outcome = COALESCE(?, outcome),
                   triggered_at = COALESCE(?, triggered_at),
                   closed_at = COALESCE(?, closed_at),
                   actual_entry = COALESCE(?, actual_entry),
                   target_price = COALESCE(?, target_price),
                   stop_price = COALESCE(?, stop_price),
                   gross_points = COALESCE(?, gross_points),
                   net_points = COALESCE(?, net_points),
                   unresolved_reason = COALESCE(?, unresolved_reason)
                   WHERE id = ?""",
                (state, outcome, triggered_at, closed_at, actual_entry,
                 target_price, stop_price, gross_points, net_points, unresolved_reason, signal_id),
            )

    def performance_summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT outcome, COUNT(*) AS count,
                   SUM(COALESCE(net_points, gross_points, 0)) AS points
                   FROM signals WHERE outcome IS NOT NULL GROUP BY outcome"""
            ).fetchall()
            totals = {str(row["outcome"]): {"count": int(row["count"]), "points": float(row["points"] or 0)} for row in rows}
            closed = sum(item["count"] for item in totals.values())
            targets = totals.get("TARGET", {}).get("count", 0)
            stops = totals.get("STOP_LOSS", {}).get("count", 0)
            resolved = targets + stops
            return {
                "total_signals": self._count_signals(connection),
                "closed_signals": closed,
                "targets": targets,
                "stop_losses": stops,
                "unresolved": totals.get("UNRESOLVED", {}).get("count", 0),
                "time_exits": totals.get("TIME_EXIT", {}).get("count", 0),
                "accuracy": (targets / resolved * 100.0) if resolved else None,
                "net_points": sum(item["points"] for item in totals.values()),
                "outcomes": totals,
                "sample_is_statistically_validated": False,
            }

    @staticmethod
    def _count_signals(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT COUNT(*) AS count FROM signals").fetchone()
        return int(row["count"])

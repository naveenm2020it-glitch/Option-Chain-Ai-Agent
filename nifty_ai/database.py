import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import DB_PATH


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS report_files (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          fingerprint TEXT NOT NULL UNIQUE,
          original_name TEXT NOT NULL,
          report_type TEXT NOT NULL,
          trading_date TEXT,
          file_path TEXT NOT NULL,
          byte_size INTEGER NOT NULL DEFAULT 0,
          row_count INTEGER NOT NULL DEFAULT 0,
          columns_json TEXT NOT NULL DEFAULT '[]',
          warnings_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'imported',
          imported_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reports_date ON report_files(trading_date);
        CREATE INDEX IF NOT EXISTS idx_reports_type ON report_files(report_type);
        CREATE TABLE IF NOT EXISTS analysis_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          trading_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          status TEXT NOT NULL,
          result_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_date ON analysis_runs(trading_date);
        CREATE TABLE IF NOT EXISTS session_prices (
          trading_date TEXT PRIMARY KEY,
          open REAL, high REAL, low REAL, close REAL, vix REAL,
          source_report TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prediction_outcomes (
          trading_date TEXT PRIMARY KEY,
          forecast_direction TEXT,
          actual_direction TEXT,
          forecast_support REAL,
          forecast_resistance REAL,
          actual_low REAL,
          actual_high REAL,
          evaluated_at TEXT
        );
        """)


def add_report(item):
    with connect() as con:
        con.execute("""INSERT OR IGNORE INTO report_files
          (fingerprint, original_name, report_type, trading_date, file_path, byte_size,
           row_count, columns_json, warnings_json, status, imported_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
          (item["fingerprint"], item["original_name"], item["report_type"], item.get("trading_date"),
           item["file_path"], item["byte_size"], item.get("row_count", 0),
           json.dumps(item.get("columns", [])), json.dumps(item.get("warnings", [])),
           item.get("status", "imported"), now_iso()))
        row = con.execute("SELECT * FROM report_files WHERE fingerprint=?", (item["fingerprint"],)).fetchone()
        return dict(row) if row else None


def list_reports(trading_date=None):
    with connect() as con:
        if trading_date:
            rows = con.execute("SELECT * FROM report_files WHERE trading_date=? ORDER BY imported_at DESC", (trading_date,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM report_files ORDER BY imported_at DESC LIMIT 300").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["columns"] = json.loads(item.pop("columns_json") or "[]")
        item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        out.append(item)
    return out


def update_report_status(fingerprint, status, warnings=None):
    with connect() as con:
        if warnings is None:
            con.execute("UPDATE report_files SET status=? WHERE fingerprint=?", (status, fingerprint))
        else:
            con.execute("UPDATE report_files SET status=?, warnings_json=? WHERE fingerprint=?",
                        (status, json.dumps(warnings), fingerprint))


def save_session_price(date, values, source_report=""):
    with connect() as con:
        con.execute("""INSERT INTO session_prices(trading_date,open,high,low,close,vix,source_report,updated_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(trading_date) DO UPDATE SET
        open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
        vix=COALESCE(excluded.vix,session_prices.vix), source_report=excluded.source_report,
        updated_at=excluded.updated_at""",
        (date, values.get("open"), values.get("high"), values.get("low"), values.get("close"),
         values.get("vix"), source_report, now_iso()))


def get_session_prices(limit=800):
    with connect() as con:
        rows = con.execute("SELECT * FROM session_prices ORDER BY trading_date DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def save_analysis(date, result, status="complete"):
    with connect() as con:
        cur = con.execute("INSERT INTO analysis_runs(trading_date,created_at,status,result_json) VALUES(?,?,?,?)",
                          (date, now_iso(), status, json.dumps(result, default=str)))
        return cur.lastrowid


def list_analyses(limit=30):
    with connect() as con:
        rows = con.execute("SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "result": json.loads(r["result_json"])} for r in rows]


def save_outcome(item):
    with connect() as con:
        con.execute("""INSERT INTO prediction_outcomes(trading_date,forecast_direction,actual_direction,
          forecast_support,forecast_resistance,actual_low,actual_high,evaluated_at)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(trading_date) DO UPDATE SET
          actual_direction=excluded.actual_direction,actual_low=excluded.actual_low,
          actual_high=excluded.actual_high,evaluated_at=excluded.evaluated_at""",
          (item["trading_date"], item.get("forecast_direction"), item.get("actual_direction"),
           item.get("forecast_support"), item.get("forecast_resistance"), item.get("actual_low"),
           item.get("actual_high"), now_iso()))

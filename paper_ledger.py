"""PostgreSQL persistence for paper quotes and simulated trades.

Requires DATABASE_URL. Append-only tables; no broker order endpoints exist.
"""
from __future__ import annotations
import json
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4, UUID
from paper_trading import OptionQuote, FeeConfig, simulate_round_trip

def _max_quote_age():
    import os
    value=int(os.getenv("PAPER_MAX_QUOTE_AGE_SECONDS","15"))
    if value < 1 or value > 60:
        raise RuntimeError("PAPER_MAX_QUOTE_AGE_SECONDS must be between 1 and 60")
    return value

def _connect():
    import os
    url=os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required for durable paper-trade storage")
    import psycopg
    if url.startswith("postgres://"):
        url="postgresql://"+url[len("postgres://"):]
    return psycopg.connect(url, connect_timeout=5)

def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_quotes (
      quote_id UUID PRIMARY KEY, symbol TEXT NOT NULL, expiry DATE NOT NULL,
      strike NUMERIC NOT NULL, option_type TEXT NOT NULL, bid NUMERIC NOT NULL,
      ask NUMERIC NOT NULL, quote_time TIMESTAMPTZ NOT NULL, lot_size INTEGER NOT NULL,
      source TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE(symbol, expiry, strike, option_type, quote_time, source)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS paper_quotes_contract_time
      ON paper_quotes(symbol, expiry, strike, option_type, quote_time DESC)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
      trade_id UUID PRIMARY KEY, entry_quote_id UUID NOT NULL REFERENCES paper_quotes(quote_id),
      exit_quote_id UUID NOT NULL REFERENCES paper_quotes(quote_id),
      lots INTEGER NOT NULL CHECK(lots > 0), result JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

def _database_fingerprint(conn):
    """Opaque identity for the database serving this request; never expose credentials or host."""
    row = conn.execute(
        "SELECT current_database(), current_schema(), COALESCE(inet_server_addr()::text, 'local')"
    ).fetchone()
    identity = "|".join(str(value) for value in row)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _quote(row):
    return OptionQuote(row["symbol"],row["expiry"].isoformat(),Decimal(str(row["strike"])),
      row["option_type"],Decimal(str(row["bid"])),Decimal(str(row["ask"])),
      row["quote_time"],row["lot_size"],row["source"])

def save_quote(payload: dict, *, now: datetime):
    q=OptionQuote(payload["symbol"],payload["expiry"],Decimal(str(payload["strike"])),
      payload["option_type"],Decimal(str(payload["bid"])),Decimal(str(payload["ask"])),
      datetime.fromisoformat(payload["timestamp"].replace("Z","+00:00")),
      int(payload["lot_size"]),payload["source"])
    q.validate(now, _max_quote_age())
    quote_id=uuid4()
    with _connect() as conn:
      _schema(conn)
      row=conn.execute("""INSERT INTO paper_quotes
        (quote_id,symbol,expiry,strike,option_type,bid,ask,quote_time,lot_size,source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(symbol,expiry,strike,option_type,quote_time,source)
        DO UPDATE SET bid=EXCLUDED.bid, ask=EXCLUDED.ask,
          lot_size=EXCLUDED.lot_size RETURNING quote_id""",
        (quote_id,q.symbol,q.expiry,q.strike,q.option_type,q.bid,q.ask,q.timestamp,
         q.lot_size,q.source)).fetchone()
      if not row:
        raise RuntimeError("Database did not return a persisted quote ID")
      persisted=conn.execute(
        "SELECT quote_id FROM paper_quotes WHERE quote_id=%s", (row[0],)
      ).fetchone()
      if not persisted:
        raise RuntimeError("Quote insert returned an ID but the row is not readable in the same database")
      database_fingerprint=_database_fingerprint(conn)
    return {"quote_id":str(persisted[0]),"accepted":True,"source":q.source,
            "timestamp":q.timestamp.isoformat(),"contract":q.symbol+" "+q.expiry+" "+str(q.strike)+q.option_type,
            "database_fingerprint":database_fingerprint}


def get_quote(quote_id: str):
    """Read back a quote by the exact ID returned from save_quote."""
    try:
      parsed_id=UUID(str(quote_id))
    except (ValueError, TypeError, AttributeError):
      raise ValueError("quote_id must be a valid UUID")
    with _connect() as conn:
      _schema(conn)
      row=conn.execute("""SELECT quote_id,symbol,expiry,strike,option_type,bid,ask,
        quote_time,lot_size,source FROM paper_quotes WHERE quote_id=%s""",
        (parsed_id,)).fetchone()
      fingerprint=_database_fingerprint(conn)
    if not row:
      return None
    names=("quote_id","symbol","expiry","strike","option_type","bid","ask",
           "quote_time","lot_size","source")
    item=dict(zip(names,row))
    return {"quote_id":str(item["quote_id"]),"symbol":item["symbol"],
            "expiry":item["expiry"].isoformat(),"strike":str(item["strike"]),
            "option_type":item["option_type"],"bid":str(item["bid"]),
            "ask":str(item["ask"]),"timestamp":item["quote_time"].isoformat(),
            "lot_size":item["lot_size"],"source":item["source"],
            "database_fingerprint":fingerprint}


def paper_health():
    with _connect() as conn:
      _schema(conn)
      fingerprint=_database_fingerprint(conn)
      conn.execute("SELECT 1").fetchone()
    return {"database_connected":True,"storage":"postgresql",
            "database_fingerprint":fingerprint}

def save_trade(payload: dict, *, now: datetime):
    with _connect() as conn:
      _schema(conn)
      entry=conn.execute("SELECT symbol,expiry,strike,option_type,bid,ask,quote_time,lot_size,source FROM paper_quotes WHERE quote_id=%s",(payload["entry_quote_id"],)).fetchone()
      exit=conn.execute("SELECT symbol,expiry,strike,option_type,bid,ask,quote_time,lot_size,source FROM paper_quotes WHERE quote_id=%s",(payload["exit_quote_id"],)).fetchone()
      if not entry or not exit:
        missing=[]
        if not entry: missing.append("entry_quote_id")
        if not exit: missing.append("exit_quote_id")
        raise ValueError("quote not found in this database: "+", ".join(missing)+
                         ". Save both quotes through this same API deployment and use the returned IDs.")
      names=("symbol","expiry","strike","option_type","bid","ask","quote_time","lot_size","source")
      e=_quote(dict(zip(names,entry))); x=_quote(dict(zip(names,exit)))
      required=("brokerage_per_order","stt_sell_rate","exchange_turnover_rate",
                "sebi_turnover_rate","stamp_buy_rate","gst_rate")
      if not all(k in payload["fees"] for k in required):
        raise ValueError("all six fee fields are required; use a dated broker tariff")
      fee=FeeConfig(**{k:Decimal(str(payload["fees"][k])) for k in required})
      result=simulate_round_trip(e,x,int(payload["lots"]),fee,now=now,
        max_quote_age_seconds=_max_quote_age(),
        slippage_bps=Decimal(str(payload.get("slippage_bps",5))),
        entry_as_of=e.timestamp)
      trade_id=uuid4()
      conn.execute("INSERT INTO paper_trades(trade_id,entry_quote_id,exit_quote_id,lots,result) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (trade_id,payload["entry_quote_id"],payload["exit_quote_id"],int(payload["lots"]),
         json.dumps(result,separators=(",",":"))))
      database_fingerprint=_database_fingerprint(conn)
    return {"trade_id":str(trade_id),"database_fingerprint":database_fingerprint,**result}

def paper_summary(limit: int=100):
    with _connect() as conn:
      _schema(conn)
      rows=conn.execute("SELECT trade_id,created_at,result FROM paper_trades ORDER BY created_at DESC LIMIT %s",(limit,)).fetchall()
    records=[{"trade_id":str(r[0]),"created_at":r[1].isoformat(),**(r[2] if isinstance(r[2],dict) else json.loads(r[2]))} for r in rows]
    pnl=sum((Decimal(x["net_pnl"]) for x in records),Decimal("0"))
    return {"available":True,"storage":"postgresql","count":len(records),
            "net_pnl":str(pnl.quantize(Decimal("0.01"))),"records":records}

"""PostgreSQL persistence for paper quotes and simulated trades.

Requires DATABASE_URL. Append-only tables; no broker order endpoints exist.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from paper_trading import OptionQuote, FeeConfig, simulate_round_trip

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

def _quote(row):
    return OptionQuote(row["symbol"],row["expiry"].isoformat(),Decimal(str(row["strike"])),
      row["option_type"],Decimal(str(row["bid"])),Decimal(str(row["ask"])),
      row["quote_time"],row["lot_size"],row["source"])

def save_quote(payload: dict, *, now: datetime):
    q=OptionQuote(payload["symbol"],payload["expiry"],Decimal(str(payload["strike"])),
      payload["option_type"],Decimal(str(payload["bid"])),Decimal(str(payload["ask"])),
      datetime.fromisoformat(payload["timestamp"].replace("Z","+00:00")),
      int(payload["lot_size"]),payload["source"])
    q.validate(now, int(payload.get("max_age_seconds",15)))
    quote_id=uuid4()
    with _connect() as conn:
      _schema(conn)
      row=conn.execute("""INSERT INTO paper_quotes
        (quote_id,symbol,expiry,strike,option_type,bid,ask,quote_time,lot_size,source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(symbol,expiry,strike,option_type,quote_time,source)
        DO UPDATE SET source=EXCLUDED.source RETURNING quote_id""",
        (quote_id,q.symbol,q.expiry,q.strike,q.option_type,q.bid,q.ask,q.timestamp,
         q.lot_size,q.source)).fetchone()
    return {"quote_id":str(row[0]),"accepted":True,"source":q.source,
            "timestamp":q.timestamp.isoformat(),"contract":q.symbol+" "+q.expiry+" "+str(q.strike)+q.option_type}

def save_trade(payload: dict, *, now: datetime):
    with _connect() as conn:
      _schema(conn)
      entry=conn.execute("SELECT symbol,expiry,strike,option_type,bid,ask,quote_time,lot_size,source FROM paper_quotes WHERE quote_id=%s",(payload["entry_quote_id"],)).fetchone()
      exit=conn.execute("SELECT symbol,expiry,strike,option_type,bid,ask,quote_time,lot_size,source FROM paper_quotes WHERE quote_id=%s",(payload["exit_quote_id"],)).fetchone()
      if not entry or not exit:
        raise ValueError("entry_quote_id and exit_quote_id must exist")
      names=("symbol","expiry","strike","option_type","bid","ask","quote_time","lot_size","source")
      e=_quote(dict(zip(names,entry))); x=_quote(dict(zip(names,exit)))
      required=("brokerage_per_order","stt_sell_rate","exchange_turnover_rate",
                "sebi_turnover_rate","stamp_buy_rate","gst_rate")
      if not all(k in payload["fees"] for k in required):
        raise ValueError("all six fee fields are required; use a dated broker tariff")
      fee=FeeConfig(**{k:Decimal(str(payload["fees"][k])) for k in required})
      result=simulate_round_trip(e,x,int(payload["lots"]),fee,now=now,
        max_quote_age_seconds=int(payload.get("max_quote_age_seconds",15)),
        slippage_bps=Decimal(str(payload.get("slippage_bps",5))))
      trade_id=uuid4()
      conn.execute("INSERT INTO paper_trades(trade_id,entry_quote_id,exit_quote_id,lots,result) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (trade_id,payload["entry_quote_id"],payload["exit_quote_id"],int(payload["lots"]),
         json.dumps(result,separators=(",",":"))))
    return {"trade_id":str(trade_id),**result}

def paper_summary(limit: int=100):
    with _connect() as conn:
      _schema(conn)
      rows=conn.execute("SELECT trade_id,created_at,result FROM paper_trades ORDER BY created_at DESC LIMIT %s",(limit,)).fetchall()
    records=[{"trade_id":str(r[0]),"created_at":r[1].isoformat(),**(r[2] if isinstance(r[2],dict) else json.loads(r[2]))} for r in rows]
    pnl=sum((Decimal(x["net_pnl"]) for x in records),Decimal("0"))
    return {"available":True,"storage":"postgresql","count":len(records),
            "net_pnl":str(pnl.quantize(Decimal("0.01"))),"records":records}

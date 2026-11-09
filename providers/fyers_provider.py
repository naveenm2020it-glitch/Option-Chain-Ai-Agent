"""Read-only FYERS NIFTY option-chain + Data Socket provider.

REST supplies the chain's OI/previous OI/Greeks/IV and a 60-second snapshot.
The Data Socket streams live NIFTY index and individual option price/volume/
bid/ask updates. Standard plan is limited to 100 symbols, so this adapter
subscribes to 24 strikes either side of ATM (49 CE + 49 PE) plus NIFTY index.
No trading/order endpoints are imported or called.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

INDEX_SYMBOL = "NSE:NIFTY50-INDEX"


class FyersProviderError(RuntimeError):
    pass


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def fyers_configuration() -> dict[str, Any]:
    return {
        "enabled": _truthy("FYERS_PROVIDER_ENABLED"),
        "app_id": os.getenv("FYERS_APP_ID", "").strip(),
        "access_token": os.getenv("FYERS_ACCESS_TOKEN", "").strip(),
        "rest_refresh_seconds": max(30, min(300, int(os.getenv("FYERS_REST_REFRESH_SECONDS", "60")))),
        "strike_count": max(1, min(24, int(os.getenv("FYERS_STANDARD_STRIKE_COUNT", "24")))),
    }


def _iso_epoch(value: Any) -> str | None:
    try:
        stamp = float(value)
        if stamp <= 0:
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


class FyersOptionChainProvider:
    name = "FYERS Data API + Data Socket"
    capabilities = {
        "instrument": "NIFTY",
        "option_chain": True,
        "spot": True,
        "option_greeks": True,
        "documented_api": True,
        "automated_use_permitted": True,
        "requires_credentials": True,
        "requires_payment": False,
        "socket_symbol_limit": 100,
    }

    def __init__(self) -> None:
        self.cfg = fyers_configuration()
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = None
        self._last_rest = 0.0
        self._expiry: str | None = None
        self._socket = None
        self._socket_thread: threading.Thread | None = None
        self._symbols: list[str] = []
        self._symbol_to_leg: dict[str, tuple[float, str]] = {}
        self._last_index_time: str | None = None
        self._last_index_spot: float | None = None
        self._last_socket_error: str | None = None

    def configuration_status(self) -> tuple[bool, str | None]:
        self.cfg = fyers_configuration()
        if not self.cfg["enabled"]:
            return False, "FYERS provider is disabled. Set FYERS_PROVIDER_ENABLED=true after configuring credentials."
        if not self.cfg["app_id"] or not self.cfg["access_token"]:
            return False, "Set FYERS_APP_ID and FYERS_ACCESS_TOKEN in the server environment. Never put tokens in browser code."
        return True, None

    def status(self) -> dict[str, Any]:
        allowed, reason = self.configuration_status()
        with self._lock:
            return {
                "provider": self.name,
                "enabled": self.cfg["enabled"],
                "credentials_configured": bool(self.cfg["app_id"] and self.cfg["access_token"]),
                "ready": allowed and self._snapshot is not None and self._last_index_time is not None,
                "status": "STREAMING" if self._last_index_time else ("CONFIGURED" if allowed else "DISABLED"),
                "socket_connected_or_started": self._socket_thread is not None and self._socket_thread.is_alive(),
                "subscribed_symbols": len(self._symbols),
                "symbol_limit": 100,
                "strike_count_each_side": self.cfg["strike_count"],
                "rest_refresh_seconds": self.cfg["rest_refresh_seconds"],
                "oi_greeks_iv_source": "REST option-chain snapshot; refresh interval shown above",
                "live_stream_fields": ["ltp", "open/high/low", "change", "change_percent", "volume", "bid/ask price", "bid/ask size"],
                "not_streamed_by_data_socket": ["open_interest", "change_in_open_interest", "implied_volatility", "option_greeks"],
                "order_execution": False,
                "message": reason or self._last_socket_error or "Configured; awaiting first exchange timestamped WebSocket tick.",
            }

    def _rest_client(self):
        try:
            from fyers_apiv3 import fyersModel
        except ImportError as exc:
            raise FyersProviderError("fyers-apiv3 SDK is not installed in this deployment.") from exc
        return fyersModel.FyersModel(
            client_id=self.cfg["app_id"],
            token=self.cfg["access_token"],
            is_async=False,
            log_path="",
        )

    def _fetch_chain(self, expiry: str | None) -> dict[str, Any]:
        self.cfg = fyers_configuration()
        client = self._rest_client()
        payload = {
            "symbol": INDEX_SYMBOL,
            "strikecount": self.cfg["strike_count"],
            "timestamp": "",
            "greeks": "1",
        }
        # Resolve a UI date to the expiry epoch returned by FYERS.
        if expiry and self._snapshot:
            match = next((x for x in self._snapshot.get("expiry_meta", []) if x.get("date") == expiry), None)
            if match:
                payload["timestamp"] = str(match.get("expiry") or "")
        response = client.optionchain(data=payload)
        if not isinstance(response, dict) or response.get("s") != "ok" or not isinstance(response.get("data"), dict):
            message = response.get("message") if isinstance(response, dict) else "Invalid response"
            raise FyersProviderError(f"FYERS option-chain request failed: {message or response}")
        data = response["data"]
        expiry_meta = data.get("expiryData") or []
        if not isinstance(expiry_meta, list) or not expiry_meta:
            raise FyersProviderError("FYERS returned no available NIFTY expiries.")
        selected_meta = next((x for x in expiry_meta if x.get("date") == expiry), None) if expiry else None
        selected_meta = selected_meta or expiry_meta[0]
        selected_expiry = str(selected_meta.get("date") or "")
        rows_by_strike: dict[float, dict[str, Any]] = {}
        symbols: list[str] = [INDEX_SYMBOL]
        symbol_to_leg: dict[str, tuple[float, str]] = {}
        spot = None
        for item in data.get("optionsChain") or []:
            if not isinstance(item, dict):
                continue
            option_type = str(item.get("option_type") or "").upper()
            if option_type == "" and (item.get("symbol") == INDEX_SYMBOL or _num(item.get("strike_price")) == -1):
                spot = _num(item.get("ltp")) or _num(item.get("fp"))
                continue
            strike = _num(item.get("strike_price"))
            symbol = item.get("symbol")
            if strike is None or not symbol or option_type not in {"CE", "PE"}:
                continue
            greeks = item.get("greeks") if isinstance(item.get("greeks"), dict) else {}
            leg = {
                "lastPrice": _num(item.get("ltp")),
                "change": _num(item.get("ltpch")),
                "pChange": _num(item.get("ltpchp")),
                "totalTradedVolume": _num(item.get("volume")),
                "openInterest": _num(item.get("oi")),
                "changeinOpenInterest": _num(item.get("oich")),
                "impliedVolatility": _num(greeks.get("iv")),
                "bidprice": _num(item.get("bid")),
                "askPrice": _num(item.get("ask")),
                "bidQty": None,
                "askQty": None,
                "delta": _num(greeks.get("delta")),
                "gamma": _num(greeks.get("gamma")),
                "theta": _num(greeks.get("theta")),
                "vega": _num(greeks.get("vega")),
                "prev_oi": _num(item.get("prev_oi")),
                "symbol": str(symbol),
            }
            row = rows_by_strike.setdefault(strike, {"strike": strike, "expiry": selected_expiry, "ce": None, "pe": None})
            row["ce" if option_type == "CE" else "pe"] = leg
            symbols.append(str(symbol))
            symbol_to_leg[str(symbol)] = (strike, option_type)
        rows = [
            r for _, r in sorted(rows_by_strike.items())
            if r.get("ce") and r.get("pe")
            and _num(r["ce"].get("lastPrice")) is not None and _num(r["ce"].get("lastPrice")) > 0
            and _num(r["pe"].get("lastPrice")) is not None and _num(r["pe"].get("lastPrice")) > 0
        ]
        if not rows:
            raise FyersProviderError("FYERS returned no paired CE/PE contracts for the selected expiry.")
        # 49 strikes * 2 legs + index = 99 symbols on the Standard plan.
        symbols = list(dict.fromkeys(symbols))
        if len(symbols) > 100:
            raise FyersProviderError(f"FYERS Standard symbol limit exceeded: attempted {len(symbols)}.")
        return {
            "instrument": "NIFTY",
            "source": "FYERS Option Chain REST + Data Socket",
            "source_timestamp": None,
            "received_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "spot": spot,
            "expiry_dates": [str(x.get("date")) for x in expiry_meta if x.get("date")],
            "selected_expiry": selected_expiry,
            "expiry_meta": expiry_meta,
            "rows": rows,
            "call_oi": data.get("callOi"),
            "put_oi": data.get("putOi"),
            "india_vix": data.get("indiavixData"),
            "oi_snapshot_received_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "provider_notice": "OI, change-OI, IV and Greeks are from periodic REST snapshots; option LTP/volume/bid/ask and NIFTY spot update from WebSocket. No orders are placed.",
            "_symbols": symbols,
            "_symbol_to_leg": symbol_to_leg,
        }

    def _on_message(self, message: Any) -> None:
        updates = message if isinstance(message, list) else [message]
        with self._lock:
            if not self._snapshot:
                return
            for update in updates:
                if not isinstance(update, dict):
                    continue
                symbol = update.get("symbol")
                if symbol == INDEX_SYMBOL:
                    spot = _num(update.get("ltp"))
                    if spot:
                        self._last_index_spot = spot
                        self._snapshot["spot"] = spot
                        tick_stamp = _iso_epoch(update.get("exch_feed_time")) or _iso_epoch(update.get("last_traded_time"))
                        if tick_stamp and (self._last_index_time is None or tick_stamp > self._last_index_time):
                            self._last_index_time = tick_stamp
                            self._snapshot["source_timestamp"] = tick_stamp
                    continue
                tick_stamp = _iso_epoch(update.get("exch_feed_time")) or _iso_epoch(update.get("last_traded_time"))
                if tick_stamp and (self._last_index_time is None or tick_stamp > self._last_index_time):
                    self._last_index_time = tick_stamp
                    self._snapshot["source_timestamp"] = tick_stamp
                identity = self._symbol_to_leg.get(str(symbol))
                if not identity:
                    continue
                strike, side = identity
                row = next((r for r in self._snapshot.get("rows", []) if float(r["strike"]) == strike), None)
                if not row:
                    continue
                leg = row["ce" if side == "CE" else "pe"]
                mapping = {
                    "ltp": "lastPrice", "ch": "change", "chp": "pChange",
                    "vol_traded_today": "totalTradedVolume", "bid_price": "bidprice",
                    "ask_price": "askPrice", "bid_size": "bidQty", "ask_size": "askQty",
                }
                for source, target in mapping.items():
                    value = _num(update.get(source))
                    if value is not None:
                        leg[target] = value
                # Keep the newest exchange timestamp from the index as the
                # snapshot freshness anchor; individual option ticks can be sparse.
            self._snapshot["received_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def _on_error(self, error: Any) -> None:
        with self._lock:
            self._last_socket_error = str(error)[:500]

    def _on_close(self, message: Any) -> None:
        with self._lock:
            self._last_socket_error = f"FYERS Data Socket closed: {message}"[:500]

    def _connect_socket(self, symbols: list[str]) -> None:
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
            socket_token = f'{self.cfg["app_id"]}:{self.cfg["access_token"]}'
            socket = data_ws.FyersDataSocket(
                access_token=socket_token,
                log_path="",
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=lambda: self._on_connect(symbols),
                on_close=self._on_close,
                on_error=self._on_error,
                on_message=self._on_message,
                reconnect_retry=10,
            )
            with self._lock:
                self._socket = socket
            socket.connect()
        except Exception as exc:
            self._on_error(f"{type(exc).__name__}: {exc}")

    def _on_connect(self, symbols: list[str]) -> None:
        with self._lock:
            socket = self._socket
        if socket:
            socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
            socket.keep_running()

    def _ensure_socket(self, symbols: list[str]) -> None:
        with self._lock:
            if self._symbols == symbols and self._socket_thread and self._socket_thread.is_alive():
                return
            # One data socket instance is maintained. If expiry changes, close
            # the previous instance before starting the new symbol subscription.
            old = self._socket
            self._symbols = list(symbols)
            if old:
                try:
                    old.close_connection()
                except Exception:
                    pass
            self._socket = None
            self._socket_thread = threading.Thread(
                target=self._connect_socket, args=(list(symbols),),
                name="fyers-data-socket", daemon=True,
            )
            self._socket_thread.start()

    def fetch_snapshot(self, instrument: str = "NIFTY", expiry: str | None = None) -> dict[str, Any]:
        if instrument.upper() != "NIFTY":
            raise FyersProviderError("This provider is restricted to NIFTY.")
        allowed, reason = self.configuration_status()
        if not allowed:
            raise FyersProviderError(reason or "FYERS provider is not configured.")
        now_mono = time.monotonic()
        with self._lock:
            needs_rest = (
                self._snapshot is None or
                (expiry is not None and self._expiry != expiry) or
                now_mono - self._last_rest >= self.cfg["rest_refresh_seconds"]
            )
        if needs_rest:
            fresh = self._fetch_chain(expiry)
            symbols = fresh.pop("_symbols")
            mapping = fresh.pop("_symbol_to_leg")
            with self._lock:
                self._snapshot = fresh
                self._symbol_to_leg = mapping
                self._expiry = fresh["selected_expiry"]
                self._last_rest = time.monotonic()
                self._last_index_time = None
                self._last_index_spot = fresh.get("spot")
            self._ensure_socket(symbols)
        with self._lock:
            if not self._snapshot:
                raise FyersProviderError("FYERS has not returned an option-chain snapshot.")
            snapshot = dict(self._snapshot)
            snapshot["rows"] = [
                {**row, "ce": dict(row["ce"]), "pe": dict(row["pe"])}
                for row in self._snapshot["rows"]
            ]
            if not self._last_index_time:
                snapshot["source_timestamp"] = None
                snapshot["provider_notice"] += " Waiting for the first timestamped NIFTY index WebSocket update."
            else:
                snapshot["source_timestamp"] = self._last_index_time
                snapshot["spot"] = self._last_index_spot
            snapshot["received_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            return snapshot

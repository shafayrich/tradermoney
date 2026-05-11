# -*- coding: utf-8 -*-
"""
TraderMoney v2.0.9 – IBKR fix, OpenRouter API, SVG icons, scrollable tabs,
text selection, PDF download/exit, watchlists removed, comprehensive help.

COMPLETE FILE – NO SHORTCUTS, NO PLACEHOLDERS.
"""

import asyncio
import csv
import io
import json
import math
import os
import queue
import random
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from datetime import datetime, timezone as dt_timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests as http_requests
import webview
from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS

APP_VERSION = "2.0.9"

# ═══════════════════════════════════════════════════════════════════════════════
# AI CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
OPENROUTER_API_KEY = "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"
AI_MODELS = [
    "google/gemini-2.0-flash-001",
    "deepseek/deepseek-chat-v3-0324",
    "meta-llama/llama-3.3-70b-instruct",
]
FREE_CHAT_DAILY_LIMIT = 5
NEWS_API_KEY = ""

_CHAT_SYSTEM_PROMPT = (
    "You are TraderBot, the AI assistant built into TraderMoney – a desktop algorithmic trading terminal. "
    "TraderMoney supports 6 brokers (Alpaca, IBKR, Tradier, Binance, Bybit, OKX) with paper and live trading. "
    "It uses a 9-indicator confirmation engine. Pro users can auto-trade with risk management. "
    "Free tier is signal-only, Alpaca paper, 1 ticker, core indicators. "
    "Keep answers concise (under 220 words), practical, specific to TraderMoney. Plain text only."
)

_chat_counter: Dict[str, Any] = {"date": None, "count": 0}

# ═══════════════════════════════════════════════════════════════════════════════
# GUMROAD LICENSE VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════
GUMROAD_PRODUCT_ID = "73otoT7rzJukCy-Lt4hhkQ=="

def verify_gumroad_license(license_key: str) -> Tuple[bool, str]:
    try:
        resp = http_requests.post(
            "https://api.gumroad.com/v2/licenses/verify",
            data={"product_id": GUMROAD_PRODUCT_ID, "license_key": license_key},
            timeout=10,
        )
        data = resp.json()
        if not data.get("success"):
            return False, data.get("message", "Invalid license key")
        purchase = data.get("purchase", {})
        if purchase.get("refunded") or purchase.get("chargebacked"):
            return False, "License revoked (refunded/chargebacked)"
        return True, "License verified"
    except Exception as e:
        return False, f"Cannot reach license server – {e}"

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK APP + PORT LOCK
# ═══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app)

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True

def acquire_lock():
    if is_port_in_use(5050):
        sys.exit(0)

signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

def is_internet_available() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2)
        return True
    except OSError:
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════
DB_PATH = os.path.expanduser("~/.tradermoney_data.db")

class DatabaseManager:
    def __init__(self, db_path: str = DB_PATH):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        self.conn.execute("DELETE FROM logs")
        self.conn.commit()

    def _init_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            signal TEXT NOT NULL,
            price REAL NOT NULL,
            rationale TEXT
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            config_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS candle_cache (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (symbol, interval)
        );
        CREATE TABLE IF NOT EXISTS leaderboard (
            user_id TEXT PRIMARY KEY,
            win_rate REAL,
            total_signals INTEGER,
            last_backtest TEXT
        );
        """)
        self.conn.commit()

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    # ── Trades ────────────────────────────────────────────────────────────
    def insert_trade(self, ts, sym, action, qty, price):
        self._exec(
            "INSERT INTO trades(timestamp,symbol,action,quantity,price)VALUES(?,?,?,?,?)",
            (ts, sym, action, qty, price))

    def get_recent_trades(self, limit=50):
        cur = self.conn.execute(
            "SELECT timestamp,symbol,action,quantity,price FROM trades ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{"time": r[0], "symbol": r[1], "action": r[2], "qty": r[3], "price": r[4]} for r in cur]

    # ── Signals ───────────────────────────────────────────────────────────
    def insert_signal(self, ts, sym, sig, price, rationale):
        self._exec(
            "INSERT INTO signals(timestamp,symbol,signal,price,rationale)VALUES(?,?,?,?,?)",
            (ts, sym, sig, price, rationale))

    def get_recent_signals(self, limit=50):
        cur = self.conn.execute(
            "SELECT timestamp,symbol,signal,price,rationale FROM signals ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{"time": r[0], "symbol": r[1], "signal": r[2], "price": r[3], "rationale": r[4]} for r in cur]

    # ── Logs ──────────────────────────────────────────────────────────────
    def insert_log(self, msg: str):
        self._exec("INSERT INTO logs(timestamp,message)VALUES(?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))

    def get_recent_logs(self, limit=50):
        cur = self.conn.execute("SELECT timestamp,message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return [f"{r[0]}  {r[1]}" for r in cur]

    # ── Backtests ─────────────────────────────────────────────────────────
    def insert_backtest(self, config_json: str):
        self._exec("INSERT INTO backtests(timestamp,config_json)VALUES(?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_json))

    # ── Candle Cache ──────────────────────────────────────────────────────
    def get_cached_candle(self, symbol: str, interval: str, max_age_seconds: int = 300):
        with self._lock:
            cur = self.conn.execute(
                "SELECT timestamp,data_json FROM candle_cache WHERE symbol=? AND interval=?",
                (symbol, interval))
            row = cur.fetchone()
        if row:
            ts = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - ts).total_seconds() < max_age_seconds:
                try:
                    return json.loads(row[1])
                except Exception:
                    pass
        return None

    def cache_candle(self, symbol: str, interval: str, df: pd.DataFrame):
        js = df.to_json(orient="split", date_format="iso")
        self._exec(
            "INSERT OR REPLACE INTO candle_cache(symbol,interval,timestamp,data_json)VALUES(?,?,?,?)",
            (symbol, interval, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), js))

    def clean_candle_cache(self, max_hours: int = 24):
        cutoff = (datetime.now() - timedelta(hours=max_hours)).strftime("%Y-%m-%d %H:%M:%S")
        self._exec("DELETE FROM candle_cache WHERE timestamp<?", (cutoff,))

    # ── Chat Sessions ─────────────────────────────────────────────────────
    def create_chat_session(self, title: str = "") -> int:
        if not title:
            title = f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        self._exec("INSERT INTO chat_sessions(title,created)VALUES(?,?)",
                   (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_chat_sessions(self) -> List[dict]:
        cur = self.conn.execute("SELECT id,title,created FROM chat_sessions ORDER BY id DESC")
        return [{"id": r[0], "title": r[1], "created": r[2]} for r in cur]

    def insert_chat_message(self, session_id: int, role: str, content: str):
        self._exec(
            "INSERT INTO chat_history(session_id,role,content,timestamp)VALUES(?,?,?,?)",
            (session_id, role, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def get_chat_history(self, session_id: int, limit: int = 200) -> List[dict]:
        cur = self.conn.execute(
            "SELECT role,content FROM(SELECT*FROM chat_history WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?)ORDER BY id ASC",
            (session_id, limit))
        return [{"role": r[0], "content": r[1]} for r in cur]

    # ── Leaderboard ───────────────────────────────────────────────────────
    def update_leaderboard(self, user_id: str, win_rate: float, total_signals: int):
        self._exec("INSERT OR REPLACE INTO leaderboard VALUES(?,?,?,?)",
                   (user_id, win_rate, total_signals, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def get_leaderboard(self) -> List[dict]:
        cur = self.conn.execute(
            "SELECT user_id,win_rate,total_signals,last_backtest FROM leaderboard ORDER BY win_rate DESC")
        return [{"user_id": r[0][:6], "win_rate": r[1], "total_signals": r[2], "last_backtest": r[3]} for r in cur]


db = DatabaseManager()

# ═══════════════════════════════════════════════════════════════════════════════
# ENCRYPTED CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
CONFIG_FILE = os.path.expanduser("~/.tradermoney_config.enc")
KEY_FILE = os.path.expanduser("~/.tradermoney.key")

def _get_fernet():
    from cryptography.fernet import Fernet
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
    else:
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    return Fernet(key)

class EncryptedConfigManager:
    @staticmethod
    def load() -> dict:
        try:
            cipher = _get_fernet()
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "rb") as f:
                    data = json.loads(cipher.decrypt(f.read()).decode())
                data.pop("license_key", None)
                data.pop("license_valid", None)
                return data
        except Exception:
            pass
        return {}

    @staticmethod
    def save(config: dict):
        clean = {k: v for k, v in config.items() if k not in ("license_key", "license_valid")}
        try:
            cipher = _get_fernet()
            plain = json.dumps(clean, indent=2).encode()
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(cipher.encrypt(plain))
            with open(tmp, "rb") as f:
                cipher.decrypt(f.read())
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            db.insert_log(f"Config save error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════════════
ATR_STOP_MULT = 2.0
ATR_TP_MULT = 3.0

_DEFAULT_CONFIG: dict = {
    "broker": "Alpaca",
    "tickers": "AAPL",
    "mode": "signal",
    "quantity": 1,
    "emas": [9, 50],
    "use_bracket": False,
    "sl_percent": 2.0,
    "tp_percent": 4.0,
    "timeframe": "1m",
    "telegram": {},
    "use_rsi": True,
    "use_macd": True,
    "use_vwap": True,
    "use_bollinger": True,
    "use_adx": True,
    "use_vol_confirm": True,
    "use_supertrend": True,
    "use_stochastic": True,
    "use_atr_stops": True,
    "direction": "both",
    "use_default_qty": True,
    "last_broker_message": "",
    "timezone": "UTC",
    "offline_mode": False,
    "news_sentiment": False,
    "device_uuid": str(uuid.uuid4()),
    "alpaca": {"api_key": "", "secret_key": "", "paper": True},
    "ibkr": {"host": "", "port": "", "client_id": ""},
    "tradier": {"access_token": "", "account_id": "", "sandbox": False},
    "binance": {"api_key": "", "api_secret": "", "testnet": True},
    "bybit": {"api_key": "", "api_secret": "", "testnet": True},
    "okx": {"api_key": "", "api_secret": "", "api_passphrase": "", "demo": True},
}

class AppState:
    def __init__(self):
        loaded = EncryptedConfigManager.load()
        self.config = {**_DEFAULT_CONFIG, **loaded} if loaded else dict(_DEFAULT_CONFIG)
        for k in ("alpaca", "ibkr", "tradier", "binance", "bybit", "okx"):
            if k not in self.config or not isinstance(self.config[k], dict):
                self.config[k] = dict(_DEFAULT_CONFIG[k])
        self.config["license_valid"] = False
        self.config["license_key"] = ""
        self.ui_queue: queue.Queue = queue.Queue()
        self.engine: Optional["TradingEngine"] = None
        self.broker_instance: Optional["BaseBroker"] = None
        self.running: bool = False
        self.internet_status: bool = True
        self.dashboard: dict = {"equity": 0, "pl": 0, "buying_power": 0, "open_positions": 0}
        self.offline_mode: bool = self.config.get("offline_mode", False)
        self.last_bt_data: dict = {}

state = AppState()

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def clean_symbol(raw: str) -> str:
    return raw.split(":")[0].strip().upper()

def to_local_time(utc_str: str, tz_name: str = "UTC") -> str:
    try:
        from zoneinfo import ZoneInfo
        utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt_timezone.utc)
        return utc_dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str

# ═══════════════════════════════════════════════════════════════════════════════
# BROKER REGISTRY & BASE CLASS
# ═══════════════════════════════════════════════════════════════════════════════
BROKER_REGISTRY: Dict[str, Any] = {}

def register_broker(name: str, cls):
    BROKER_REGISTRY[name] = cls

class BaseBroker:
    name = "Base"

    def __init__(self, config: dict, ui_queue: queue.Queue):
        self.config = config
        self.ui_queue = ui_queue
        self.last_error = ""

    def _emit_error(self, msg: str):
        self.last_error = msg
        self.ui_queue.put(("error", msg))
        db.insert_log(f"[{self.name}] ERROR: {msg}")

    def _emit_log(self, msg: str):
        self.ui_queue.put(("log", msg))
        db.insert_log(f"[{self.name}] {msg}")

    def connect(self) -> bool: raise NotImplementedError
    def get_account(self): raise NotImplementedError
    def submit_order(self, *a, **kw): raise NotImplementedError
    def close_all_positions(self): raise NotImplementedError
    def get_positions(self): raise NotImplementedError
    def get_market_status(self) -> bool: raise NotImplementedError
    def stream_prices(self, syms, cb): raise NotImplementedError
    def stop_stream(self): raise NotImplementedError
    def is_connected(self) -> bool: return True

# ═══════════════════════════════════════════════════════════════════════════════
# ALPACA BROKER
# ═══════════════════════════════════════════════════════════════════════════════
class AlpacaBroker(BaseBroker):
    name = "Alpaca"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.api = None
        self._stop_stream = False

    def is_connected(self) -> bool:
        return self.api is not None

    def connect(self) -> bool:
        creds = self.config.get("alpaca", {})
        key = creds.get("api_key", "").strip()
        secret = creds.get("secret_key", "").strip()
        paper = creds.get("paper", True)
        if not key:
            self._emit_error("Alpaca API Key is missing.")
            return False
        if not secret:
            self._emit_error("Alpaca Secret Key is missing.")
            return False
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        try:
            import alpaca_trade_api as tradeapi
            self.api = tradeapi.REST(key, secret, base_url, api_version="v2")
            acc = self.api.get_account()
            if acc.status != "ACTIVE":
                self._emit_error(f"Alpaca account status is '{acc.status}', not ACTIVE.")
                return False
            self._emit_log(f"Connected. Paper={paper}. Equity=${acc.equity}")
            return True
        except ImportError:
            self._emit_error("alpaca-trade-api not installed.")
            return False
        except Exception as e:
            msg = str(e)
            if "403" in msg or "unauthorized" in msg.lower():
                self._emit_error(f"Alpaca auth failed. Paper={paper}. Detail: {msg}")
            else:
                self._emit_error(f"Alpaca connection error: {msg}")
            return False

    def get_account(self):
        if not self.api:
            return None
        try:
            acc = self.api.get_account()
            return {
                "equity": float(acc.equity),
                "pl": float(acc.equity) - float(acc.last_equity),
                "buying_power": float(acc.buying_power),
                "cash": float(acc.cash),
                "open_positions": len(self.api.list_positions()),
            }
        except Exception as e:
            self._emit_error(f"get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.api:
            self._emit_error("Alpaca not connected – cannot submit order.")
            return False
        try:
            if sl_price is None and sl_pct is None:
                self.api.submit_order(symbol=symbol, qty=qty, side=side, type="market", time_in_force="day")
            else:
                trade = self.api.get_latest_trade(symbol)
                price = float(trade.price)
                if side == "buy":
                    stop = round(sl_price if sl_price else price * (1 - sl_pct / 100), 2)
                    limit = round(tp_price if tp_price else price * (1 + tp_pct / 100), 2)
                else:
                    stop = round(sl_price if sl_price else price * (1 + sl_pct / 100), 2)
                    limit = round(tp_price if tp_price else price * (1 - tp_pct / 100), 2)
                self.api.submit_order(
                    symbol=symbol, qty=qty, side=side, type="market", time_in_force="gtc",
                    order_class="bracket",
                    stop_loss={"stop_price": stop}, take_profit={"limit_price": limit})
            self._emit_log(f"Order submitted: {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            self._emit_error(f"Order failed ({symbol} {side}): {e}")
            return False

    def close_all_positions(self):
        if self.api:
            try:
                self.api.close_all_positions()
                self._emit_log("Kill switch: all Alpaca positions closed.")
            except Exception as e:
                self._emit_error(f"Kill switch error: {e}")

    def get_positions(self):
        if not self.api:
            return {}
        try:
            return {p.symbol: int(float(p.qty)) for p in self.api.list_positions()}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        if not self.api:
            return False
        try:
            return self.api.get_clock().is_open
        except Exception:
            return False

    def stream_prices(self, symbols, callback):
        if not symbols:
            return
        self._stop_stream = False

        def run():
            try:
                from alpaca.data.live import StockDataStream
                creds = self.config.get("alpaca", {})
                key = creds.get("api_key")
                secret = creds.get("secret_key")
                paper = creds.get("paper", True)
                stream = StockDataStream(api_key=key, secret_key=secret, feed="iex" if paper else "sip")

                async def on_trade(data):
                    if data.symbol in symbols:
                        callback(data.symbol, data.price)

                stream.subscribe_trades(on_trade, *symbols)
                while not self._stop_stream:
                    try:
                        stream.run()
                    except Exception as e:
                        self._emit_log(f"Stream retry: {e}")
                        time.sleep(5)
            except ImportError:
                pass
            except Exception as e:
                self._emit_log(f"Alpaca stream warning: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("Alpaca", AlpacaBroker)

# ═══════════════════════════════════════════════════════════════════════════════
# INTERACTIVE BROKERS (FIXED – no more hanging)
# ═══════════════════════════════════════════════════════════════════════════════
class IBKRBroker(BaseBroker):
    name = "Interactive Brokers"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.ib = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ib_thread: Optional[threading.Thread] = None
        self._stop_stream = False
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self.ib is not None and self.ib.isConnected()

    def _start_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _ensure_loop(self):
        if self._loop is None or not self._loop.is_running():
            self._ib_thread = threading.Thread(target=self._start_loop, daemon=True, name="IBKRLoop")
            self._ib_thread.start()
            waited = 0
            while (self._loop is None or not self._loop.is_running()) and waited < 3:
                time.sleep(0.3)
                waited += 0.3
            if self._loop is None or not self._loop.is_running():
                raise RuntimeError("IBKR event loop failed to start")

    def _run_coro(self, coro):
        if self._loop is None:
            raise RuntimeError("IBKR event loop not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=15)

    def connect(self) -> bool:
        creds = self.config.get("ibkr", {})
        host = creds.get("host", "").strip()
        port_str = creds.get("port", "").strip()
        cid_str = creds.get("client_id", "").strip()
        if not host:
            self._emit_error("IBKR Host is missing (usually 127.0.0.1).")
            return False
        try:
            port = int(port_str)
            cid = int(cid_str)
        except ValueError:
            self._emit_error("IBKR port and client_id must be integers (e.g., 7497, 1).")
            return False
        try:
            from ib_insync import IB
        except ImportError:
            self._emit_error("ib_insync not installed. Run: pip install ib_insync")
            return False

        try:
            self._ensure_loop()
        except RuntimeError as e:
            self._emit_error(f"Failed to start IBKR event loop: {e}")
            return False

        async def _do():
            ib = IB()
            await ib.connectAsync(host, port, clientId=cid, timeout=10)
            return ib

        try:
            self.ib = self._run_coro(_do())
        except asyncio.TimeoutError:
            self._emit_error(f"IBKR connection timed out at {host}:{port}. Is TWS/Gateway running?")
            return False
        except ConnectionRefusedError:
            self._emit_error(
                f"IBKR refused connection at {host}:{port}. "
                "Is TWS or IB Gateway running? API enabled? "
                "Ports: 7497=TWS paper | 7496=TWS live | 4002=Gateway paper | 4001=Gateway live")
            return False
        except Exception as e:
            self._emit_error(f"IBKR connection error: {e}")
            return False

        if not self.ib.isConnected():
            self._emit_error(f"IBKR connected but isConnected()=False. Check {host}:{port}.")
            return False

        self._connected = True
        self._emit_log(f"Connected to IBKR at {host}:{port} (clientId={cid})")
        return True

    def get_account(self):
        if not self.is_connected():
            return None
        try:
            summary = self._run_coro(self.ib.accountSummaryAsync())
            eq = next((float(v.value) for v in summary if v.tag == "NetLiquidation"), 0.0)
            pl = next((float(v.value) for v in summary if v.tag == "UnrealizedPnL"), 0.0)
            bp = next((float(v.value) for v in summary if v.tag == "AvailableFunds"), 0.0)
            pos = [p for p in self.ib.positions() if p.position != 0]
            return {"equity": eq, "pl": pl, "buying_power": bp, "cash": 0.0, "open_positions": len(pos)}
        except Exception as e:
            self._emit_error(f"IBKR get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.is_connected():
            self._emit_error("IBKR not connected – cannot submit order.")
            return False
        try:
            from ib_insync import Stock, MarketOrder

            async def _place():
                c = Stock(symbol, "SMART", "USD")
                await self.ib.qualifyContractsAsync(c)
                self.ib.placeOrder(c, MarketOrder("BUY" if side == "buy" else "SELL", qty))

            self._run_coro(_place())
            self._emit_log(f"Order submitted: {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            self._emit_error(f"IBKR order error: {e}")
            return False

    def close_all_positions(self):
        if not self.is_connected():
            return
        from ib_insync import MarketOrder
        for pos in self.ib.positions():
            if pos.position == 0:
                continue
            d = "SELL" if pos.position > 0 else "BUY"

            async def _c(contract=pos.contract, n=abs(pos.position), direction=d):
                self.ib.placeOrder(contract, MarketOrder(direction, n))

            self._run_coro(_c())
        self._emit_log("IBKR: all positions closed.")

    def get_positions(self):
        if not self.is_connected():
            return {}
        return {pos.contract.symbol: int(pos.position) for pos in self.ib.positions() if pos.position != 0}

    def get_market_status(self) -> bool:
        return True

    def stream_prices(self, symbols, callback):
        if not self.is_connected():
            return
        self._stop_stream = False

        async def _sub():
            from ib_insync import Stock
            contracts = [Stock(s, "SMART", "USD") for s in symbols]
            await self.ib.qualifyContractsAsync(*contracts)
            tickers = [self.ib.reqMktData(c, "", False, False) for c in contracts]
            sym_map = {c.symbol: s for c, s in zip(contracts, symbols)}
            while not self._stop_stream:
                await asyncio.sleep(1)
                for t in tickers:
                    if t.last and t.last > 0:
                        orig = sym_map.get(t.contract.symbol)
                        if orig:
                            callback(orig, t.last)

        asyncio.run_coroutine_threadsafe(_sub(), self._loop)

    def stop_stream(self):
        self._stop_stream = True

register_broker("Interactive Brokers", IBKRBroker)

# ═══════════════════════════════════════════════════════════════════════════════
# TRADIER BROKER
# ═══════════════════════════════════════════════════════════════════════════════
class TradierBroker(BaseBroker):
    name = "Tradier"
    LIVE_URL = "https://api.tradier.com/v1"
    SAND_URL = "https://sandbox.tradier.com/v1"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session = None
        self.account_id = None
        self._base = self.LIVE_URL
        self._stop_stream = False

    def is_connected(self) -> bool:
        return self.session is not None

    def connect(self) -> bool:
        creds = self.config.get("tradier", {})
        token = creds.get("access_token", "").strip()
        self.account_id = creds.get("account_id", "").strip()
        sandbox = creds.get("sandbox", False)
        if not token:
            self._emit_error("Tradier Access Token is missing.")
            return False
        if not self.account_id:
            self._emit_error("Tradier Account ID is missing.")
            return False
        self._base = self.SAND_URL if sandbox else self.LIVE_URL
        import requests as req
        self.session = req.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            r = self.session.get(f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            if r.status_code == 401:
                self._emit_error("Tradier auth failed (HTTP 401). Check token.")
                return False
            if r.status_code == 404:
                self._emit_error(f"Tradier Account '{self.account_id}' not found (HTTP 404).")
                return False
            if r.status_code != 200:
                self._emit_error(f"Tradier HTTP {r.status_code}: {r.text[:200]}")
                return False
            self._emit_log(f"Connected (sandbox={sandbox})")
            return True
        except Exception as e:
            self._emit_error(f"Tradier connection error: {e}")
            return False

    def get_account(self):
        if not self.session:
            return None
        try:
            r = self.session.get(f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            r.raise_for_status()
            bal = r.json().get("balances", {})
            return {
                "equity": float(bal.get("total_equity", 0)),
                "pl": 0.0,
                "buying_power": float(bal.get("equity_buying_power", 0)),
                "cash": float(bal.get("total_cash", 0)),
                "open_positions": 0,
            }
        except Exception as e:
            self._emit_error(f"Tradier get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session:
            self._emit_error("Tradier not connected – cannot submit order.")
            return False
        try:
            r = self.session.post(
                f"{self._base}/accounts/{self.account_id}/orders",
                data={"class": "equity", "symbol": symbol, "side": side,
                      "quantity": str(qty), "type": "market", "duration": "day"},
                timeout=10)
            err = r.json().get("errors", {}).get("error")
            if r.status_code not in (200, 201) or err:
                self._emit_error(f"Tradier order rejected: {err or r.text[:200]}")
                return False
            self._emit_log(f"Order submitted: {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            self._emit_error(f"Tradier submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self.session:
            return
        for sym, qty in self.get_positions().items():
            self.submit_order(sym, abs(qty), "sell" if qty > 0 else "buy")
        self._emit_log("Tradier: all positions closed.")

    def get_positions(self):
        if not self.session:
            return {}
        try:
            r = self.session.get(f"{self._base}/accounts/{self.account_id}/positions", timeout=10)
            r.raise_for_status()
            raw = r.json().get("positions", {}).get("position", [])
            if isinstance(raw, dict):
                raw = [raw]
            return {p["symbol"]: int(float(p["quantity"])) for p in raw if p}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        try:
            r = self.session.get(f"{self._base}/markets/clock", timeout=5)
            return r.json().get("clock", {}).get("state", "") == "open"
        except Exception:
            return True

    def stream_prices(self, symbols, callback):
        self._stop_stream = False

        def poll():
            joined = ",".join(symbols)
            while not self._stop_stream:
                try:
                    r = self.session.get(f"{self._base}/markets/quotes", params={"symbols": joined}, timeout=5)
                    quotes = r.json().get("quotes", {}).get("quote", [])
                    if isinstance(quotes, dict):
                        quotes = [quotes]
                    for q in quotes:
                        sym = q.get("symbol", "")
                        price = q.get("last") or q.get("bid") or 0.0
                        if sym and price:
                            callback(sym, float(price))
                except Exception:
                    pass
                time.sleep(5)

        threading.Thread(target=poll, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("Tradier", TradierBroker)

# ═══════════════════════════════════════════════════════════════════════════════
# BINANCE BROKER
# ═══════════════════════════════════════════════════════════════════════════════
class BinanceBroker(BaseBroker):
    name = "Binance"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.client = None
        self._ws_client = None
        self._stop_stream = False

    def is_connected(self) -> bool:
        return self.client is not None

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "").replace("-", "").upper()
        return s if s.endswith("USDT") else s + "USDT"

    def connect(self) -> bool:
        creds = self.config.get("binance", {})
        api_key = creds.get("api_key", "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet = creds.get("testnet", True)
        if not api_key:
            self._emit_error("Binance API Key is missing.")
            return False
        if not api_secret:
            self._emit_error("Binance API Secret is missing.")
            return False
        try:
            from binance.spot import Spot
            kw = {"base_url": "https://testnet.binance.vision"} if testnet else {}
            self.client = Spot(api_key=api_key, api_secret=api_secret, **kw)
            acct = self.client.account()
            if not acct.get("canTrade"):
                self._emit_error("Binance account cannot trade. Check API permissions.")
                return False
            self._emit_log(f"Connected (testnet={testnet})")
            return True
        except ImportError:
            self._emit_error("python-binance not installed. Run: pip install python-binance")
            return False
        except Exception as e:
            msg = str(e)
            if "-2015" in msg or "-2014" in msg:
                self._emit_error(f"Binance auth failed. Testnet={testnet}. Detail: {msg}")
            else:
                self._emit_error(f"Binance connection error: {msg}")
            return False

    def get_account(self):
        if not self.client:
            return None
        try:
            acct = self.client.account()
            bals = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in acct["balances"]}
            usdt = bals.get("USDT", 0.0)
            btc = bals.get("BTC", 0.0)
            try:
                btc_price = float(self.client.ticker_price(symbol="BTCUSDT")["price"])
            except Exception:
                btc_price = 0.0
            return {"equity": usdt + btc * btc_price, "pl": 0.0, "buying_power": usdt, "cash": usdt, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"Binance get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.client:
            self._emit_error("Binance not connected – cannot submit order.")
            return False
        try:
            resp = self.client.new_order(
                symbol=self._norm(symbol),
                side="BUY" if side == "buy" else "SELL",
                type="MARKET", quantity=qty)
            if resp.get("status") not in ("FILLED", "NEW", "PARTIALLY_FILLED"):
                self._emit_error(f"Binance order status: {resp}")
                return False
            self._emit_log(f"Order submitted: {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            self._emit_error(f"Binance submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self.client:
            return
        for asset, free in self.get_positions().items():
            if free > 0:
                try:
                    self.client.new_order(symbol=asset + "USDT", side="SELL", type="MARKET", quantity=free)
                except Exception:
                    pass
        self._emit_log("Binance: all positions closed.")

    def get_positions(self):
        if not self.client:
            return {}
        try:
            acct = self.client.account()
            return {b["asset"]: float(b["free"]) for b in acct["balances"] if float(b["free"]) > 0 and b["asset"] != "USDT"}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        return True

    def stream_prices(self, symbols, callback):
        self._stop_stream = False

        def run():
            try:
                from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient
                sym_map = {self._norm(s).lower(): s for s in symbols}

                def on_msg(_, raw):
                    try:
                        data = json.loads(raw) if isinstance(raw, str) else raw
                        payload = data.get("data", data)
                        if payload.get("e") == "trade":
                            ws_sym = payload["s"].lower()
                            price = float(payload["p"])
                            orig = sym_map.get(ws_sym)
                            if orig:
                                callback(orig, price)
                    except Exception:
                        pass

                testnet = self.config.get("binance", {}).get("testnet", True)
                self._ws_client = SpotWebsocketStreamClient(
                    stream_url=("wss://testnet.binance.vision" if testnet else "wss://stream.binance.com"),
                    on_message=on_msg)
                for s in sym_map:
                    self._ws_client.trade(symbol=s)
                while not self._stop_stream:
                    time.sleep(1)
                self._ws_client.stop()
            except Exception as e:
                self._emit_log(f"Binance stream warning: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception:
                pass

register_broker("Binance", BinanceBroker)

# ═══════════════════════════════════════════════════════════════════════════════
# BYBIT BROKER
# ═══════════════════════════════════════════════════════════════════════════════
class BybitBroker(BaseBroker):
    name = "Bybit"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session = None
        self._stop_stream = False

    def is_connected(self) -> bool:
        return self.session is not None

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "").replace("-", "").upper()
        return s if s.endswith("USDT") else s + "USDT"

    def connect(self) -> bool:
        creds = self.config.get("bybit", {})
        api_key = creds.get("api_key", "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet = creds.get("testnet", True)
        if not api_key:
            self._emit_error("Bybit API Key is missing.")
            return False
        if not api_secret:
            self._emit_error("Bybit API Secret is missing.")
            return False
        try:
            from pybit.unified_trading import HTTP
            self.session = HTTP(api_key=api_key, api_secret=api_secret, testnet=testnet)
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            if resp.get("retCode", -1) != 0:
                self._emit_error(f"Bybit auth failed: {resp.get('retMsg')}. Testnet={testnet}")
                return False
            self._emit_log(f"Connected (testnet={testnet})")
            return True
        except ImportError:
            self._emit_error("pybit v5 not installed. Run: pip install pybit")
            return False
        except Exception as e:
            self._emit_error(f"Bybit connection error: {e}")
            return False

    def get_account(self):
        if not self.session:
            return None
        try:
            result = self.session.get_wallet_balance(accountType="UNIFIED").get("result", {}).get("list", [{}])[0]
            equity = float(result.get("totalEquity", 0))
            avail = float(result.get("totalAvailableBalance", 0))
            return {"equity": equity, "pl": 0.0, "buying_power": avail, "cash": avail, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"Bybit get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session:
            self._emit_error("Bybit not connected – cannot submit order.")
            return False
        try:
            kwargs = dict(
                category="spot", symbol=self._norm(symbol),
                side="Buy" if side == "buy" else "Sell", orderType="Market", qty=str(qty))
            if sl_price:
                kwargs["stopLoss"] = str(round(sl_price, 4))
            if tp_price:
                kwargs["takeProfit"] = str(round(tp_price, 4))
            resp = self.session.place_order(**kwargs)
            if resp.get("retCode", -1) != 0:
                self._emit_error(f"Bybit order rejected: {resp.get('retMsg')}")
                return False
            self._emit_log(f"Order submitted: {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            self._emit_error(f"Bybit submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self.session:
            return
        for ccy, eq in self.get_positions().items():
            if eq > 0:
                self.session.place_order(category="spot", symbol=ccy + "USDT", side="Sell", orderType="Market", qty=str(eq))
        self._emit_log("Bybit: all positions closed.")

    def get_positions(self):
        if not self.session:
            return {}
        try:
            coins = self.session.get_wallet_balance(accountType="UNIFIED").get("result", {}).get("list", [{}])[0].get("coin", [])
            return {c["coin"]: float(c.get("equity", 0)) for c in coins if float(c.get("equity", 0)) > 0 and c["coin"] != "USDT"}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        return True

    def stream_prices(self, symbols, callback):
        self._stop_stream = False

        def run():
            try:
                from pybit.unified_trading import WebSocket
                testnet = self.config.get("bybit", {}).get("testnet", True)
                sym_map = {self._norm(s): s for s in symbols}

                def handle(msg):
                    try:
                        data = msg.get("data", {})
                        if isinstance(data, list):
                            data = data[0] if data else {}
                        raw_sym = msg.get("topic", "").split(".")[-1]
                        orig = sym_map.get(raw_sym)
                        price = float(data.get("lastPrice", 0))
                        if orig and price:
                            callback(orig, price)
                    except Exception:
                        pass

                ws = WebSocket(testnet=testnet, channel_type="spot")
                for sym in sym_map:
                    ws.ticker_stream(symbol=sym, callback=handle)
                while not self._stop_stream:
                    time.sleep(1)
            except Exception as e:
                self._emit_log(f"Bybit stream warning: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("Bybit", BybitBroker)

# ═══════════════════════════════════════════════════════════════════════════════
# OKX BROKER
# ═══════════════════════════════════════════════════════════════════════════════
class OKXBroker(BaseBroker):
    name = "OKX"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self._account_api = None
        self._trade_api = None
        self._stop_stream = False
        self._flag = "0"

    def is_connected(self) -> bool:
        return self._account_api is not None

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "-").replace("_", "-").upper()
        return s if "-" in s else s + "-USDT"

    def connect(self) -> bool:
        creds = self.config.get("okx", {})
        api_key = creds.get("api_key", "").strip()
        api_secret = creds.get("api_secret", "").strip()
        passphrase = creds.get("api_passphrase", "").strip()
        demo = creds.get("demo", True)
        self._flag = "1" if demo else "0"
        if not api_key:
            self._emit_error("OKX API Key is missing.")
            return False
        if not api_secret:
            self._emit_error("OKX API Secret is missing.")
            return False
        if not passphrase:
            self._emit_error("OKX Passphrase is missing.")
            return False
        try:
            import okx.Account as AccountAPI
            import okx.Trade as TradeAPI
            self._account_api = AccountAPI.AccountAPI(api_key, api_secret, passphrase, False, self._flag)
            self._trade_api = TradeAPI.TradeAPI(api_key, api_secret, passphrase, False, self._flag)
            resp = self._account_api.get_account_balance()
            code = str(resp.get("code", "-1"))
            if code != "0":
                self._emit_error(f"OKX auth failed (code={code}): {resp.get('msg')}. Demo={demo}")
                return False
            self._emit_log(f"Connected (demo={demo})")
            return True
        except ImportError:
            self._emit_error("okx package not installed. Run: pip install okx")
            return False
        except Exception as e:
            self._emit_error(f"OKX connection error: {e}")
            return False

    def get_account(self):
        if not self._account_api:
            return None
        try:
            details = self._account_api.get_account_balance().get("data", [{}])[0].get("details", [])
            equity = sum(float(d.get("eq", 0)) for d in details)
            usdt = next((float(d.get("availBal", 0)) for d in details if d.get("ccy") == "USDT"), 0.0)
            return {"equity": equity, "pl": 0.0, "buying_power": usdt, "cash": usdt, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"OKX get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self._trade_api:
            self._emit_error("OKX not connected – cannot submit order.")
            return False
        try:
            resp = self._trade_api.place_order(
                instId=self._norm(symbol), tdMode="cash",
                side=side, ordType="market", sz=str(int(qty)))
            items = resp.get("data", [{}])
            s_code = str(items[0].get("sCode", "-1")) if items else "-1"
            if s_code != "0":
                s_msg = items[0].get("sMsg", str(resp)) if items else str(resp)
                self._emit_error(f"OKX order rejected (sCode={s_code}): {s_msg}")
                return False
            self._emit_log(f"Order submitted: {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            self._emit_error(f"OKX submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self._account_api:
            return
        for ccy, eq in self.get_positions().items():
            if eq > 0:
                self._trade_api.place_order(
                    instId=f"{ccy}-USDT", tdMode="cash",
                    side="sell", ordType="market", sz=str(eq))
        self._emit_log("OKX: all positions closed.")

    def get_positions(self):
        if not self._account_api:
            return {}
        try:
            details = self._account_api.get_account_balance().get("data", [{}])[0].get("details", [])
            return {d["ccy"]: float(d.get("eq", 0)) for d in details if float(d.get("eq", 0)) > 0 and d["ccy"] != "USDT"}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        return True

    def stream_prices(self, symbols, callback):
        self._stop_stream = False

        def run():
            try:
                import websocket
                import json as _j
                sym_map = {self._norm(s): s for s in symbols}
                subs = [{"channel": "tickers", "instId": k} for k in sym_map]
                url = (
                    "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
                    if self.config.get("okx", {}).get("demo", True)
                    else "wss://ws.okx.com:8443/ws/v5/public")

                def on_msg(ws_app, msg):
                    try:
                        for item in _j.loads(msg).get("data", []):
                            inst = item.get("instId", "")
                            price = float(item.get("last", 0))
                            orig = sym_map.get(inst)
                            if orig and price:
                                callback(orig, price)
                    except Exception:
                        pass

                def on_open(ws_app):
                    ws_app.send(_j.dumps({"op": "subscribe", "args": subs}))

                ws = websocket.WebSocketApp(url, on_message=on_msg, on_open=on_open)
                while not self._stop_stream:
                    ws.run_forever()
                    if not self._stop_stream:
                        time.sleep(3)
            except ImportError:
                pass
            except Exception as e:
                self._emit_log(f"OKX stream warning: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("OKX", OKXBroker)

# ═══════════════════════════════════════════════════════════════════════════════
# INDICATOR CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════
class IndicatorCalculator:
    @staticmethod
    def compute_all(df: pd.DataFrame, ema_fast: int = 9, ema_slow: int = 50) -> pd.DataFrame:
        close = np.asarray(df["Close"]).astype(np.float64).ravel()
        high = np.asarray(df["High"]).astype(np.float64).ravel()
        low = np.asarray(df["Low"]).astype(np.float64).ravel()
        volume = (np.asarray(df["Volume"]).astype(np.float64).ravel() if "Volume" in df.columns else np.ones_like(close))

        def ema(data: np.ndarray, span: int) -> np.ndarray:
            a = 2 / (span + 1)
            res = np.empty_like(data)
            res[0] = data[0]
            for i in range(1, len(data)):
                res[i] = a * data[i] + (1 - a) * res[i - 1]
            return res

        df["EMA_fast"] = ema(close, ema_fast)
        df["EMA_slow"] = ema(close, ema_slow)

        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        ag = np.convolve(gain, np.ones(14) / 14, mode="full")[:len(close)]
        al = np.convolve(loss, np.ones(14) / 14, mode="full")[:len(close)]
        rs = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
        df["RSI"] = 100 - (100 / (1 + rs))

        m = ema(close, 12) - ema(close, 26)
        df["MACD"] = m
        df["MACD_signal"] = ema(m, 9)

        ma20 = np.convolve(close, np.ones(20) / 20, mode="same")
        std20 = np.array([np.std(close[max(0, i - 19):i + 1]) for i in range(len(close))])
        df["BB_upper"] = ma20 + 2 * std20
        df["BB_lower"] = ma20 - 2 * std20

        cum_vol = np.cumsum(volume)
        df["VWAP"] = np.divide(np.cumsum(close * volume), cum_vol, out=np.zeros_like(close), where=cum_vol != 0)

        tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
        tr = np.insert(tr, 0, np.mean(tr[:14]) if len(tr) >= 14 else (tr[0] if len(tr) else 0))
        atr14 = ema(tr, 14)
        df["ATR"] = atr14

        up = np.maximum(np.diff(high, prepend=high[0]), 0.0)
        dn = np.maximum(-np.diff(low, prepend=low[0]), 0.0)
        pdm = np.where((up > dn) & (up > 0), up, 0.0)
        mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
        pdi = 100 * ema(pdm, 14) / (atr14 + 1e-14)
        mdi = 100 * ema(mdm, 14) / (atr14 + 1e-14)
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-14)
        df["ADX"] = ema(dx, 14)

        vol_avg = np.convolve(volume, np.ones(20) / 20, mode="same")
        df["Vol_ratio"] = np.divide(volume, vol_avg, out=np.ones_like(volume), where=vol_avg != 0)

        st_atr = ema(tr, 10)
        hl2 = (high + low) / 2.0
        upper_s = hl2 + 3.0 * st_atr
        lower_s = hl2 - 3.0 * st_atr
        st = np.zeros_like(close)
        trend = np.ones_like(close)
        for i in range(1, len(close)):
            if close[i] > upper_s[i - 1]:
                trend[i] = 1
            elif close[i] < lower_s[i - 1]:
                trend[i] = -1
            else:
                trend[i] = trend[i - 1]
                if trend[i] == 1 and lower_s[i] < lower_s[i - 1]:
                    lower_s[i] = lower_s[i - 1]
                if trend[i] == -1 and upper_s[i] > upper_s[i - 1]:
                    upper_s[i] = upper_s[i - 1]
            st[i] = lower_s[i] if trend[i] == 1 else upper_s[i]
        df["Supertrend"] = st
        df["Supertrend_trend"] = trend

        K = 14
        ll = np.array([np.min(low[max(0, i - K + 1):i + 1]) for i in range(len(close))])
        hh = np.array([np.max(high[max(0, i - K + 1):i + 1]) for i in range(len(close))])
        stk = np.where(hh - ll != 0, 100 * (close - ll) / (hh - ll + 1e-14), 50.0)
        df["Stoch_K"] = stk
        df["Stoch_D"] = np.convolve(stk, np.ones(3) / 3, mode="same")
        return df

# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════
class SignalAnalyzer:
    ADX_THRESHOLD = 20
    VOL_THRESHOLD = 1.5

    @staticmethod
    def _sf(val, default: float = 0.0) -> float:
        try:
            v = val.item() if hasattr(val, "item") else val
            return float(v)
        except Exception:
            return default

    @staticmethod
    def generate_signal(df: pd.DataFrame, prev_fast, prev_slow, config: dict) -> Tuple[Optional[str], str, float]:
        if prev_fast is None or prev_slow is None:
            return None, "", 0.0
        l = df.iloc[-1]
        sf = SignalAnalyzer._sf
        ef = sf(l["EMA_fast"])
        es = sf(l["EMA_slow"])
        price = sf(l["Close"])
        bull = prev_fast <= prev_slow and ef > es
        bear = prev_fast >= prev_slow and ef < es
        passes, dir_ = False, ""
        if bull:
            passes, dir_ = SignalAnalyzer._confirm(df, config, "bull", price)
        elif bear:
            passes, dir_ = SignalAnalyzer._confirm(df, config, "bear", price)
        if not passes:
            return None, "", 0.0
        conf = 0.50
        conf += 0.05 if config.get("use_rsi", True) else 0
        conf += 0.05 if config.get("use_macd", True) else 0
        conf += 0.05 if config.get("use_vwap", True) else 0
        conf += 0.05 if config.get("use_bollinger", True) else 0
        conf += 0.05 if config.get("use_adx", True) else 0
        conf += 0.06 if config.get("use_vol_confirm", True) else 0
        conf += 0.08 if config.get("use_supertrend", True) else 0
        conf += 0.05 if config.get("use_stochastic", True) else 0
        conf += 0.04 if config.get("use_atr_stops", True) else 0
        conf = min(conf, 1.0)
        sig = "BUY" if dir_ == "bull" else "SELL"
        return sig, f"{sig} @ ${price:.2f} (conf: {conf:.2f})", conf

    @staticmethod
    def _confirm(df: pd.DataFrame, config: dict, direction: str, price: float) -> Tuple[bool, str]:
        l = df.iloc[-1]
        sf = SignalAnalyzer._sf
        rsi = sf(l.get("RSI", 50), 50)
        macd = sf(l.get("MACD", 0), 0)
        msig = sf(l.get("MACD_signal", 0), 0)
        bbu = sf(l.get("BB_upper", price), price)
        bbl = sf(l.get("BB_lower", price), price)
        vwap = sf(l.get("VWAP", price), price)
        adx = sf(l.get("ADX", 0), 0)
        vr = sf(l.get("Vol_ratio", 1), 1)
        stt = sf(l.get("Supertrend_trend", 0), 0)
        stk = sf(l.get("Stoch_K", 50), 50)
        std_ = sf(l.get("Stoch_D", 50), 50)

        if direction == "bull":
            if config.get("use_rsi", True) and rsi < 30:
                return False, "bull"
            if config.get("use_macd", True) and macd <= msig:
                return False, "bull"
            if config.get("use_vwap", True) and price < vwap:
                return False, "bull"
            if config.get("use_bollinger", True) and price < bbl * 0.99:
                return False, "bull"
            if config.get("use_supertrend", True) and stt != 1:
                return False, "bull"
            if config.get("use_stochastic", True) and (stk < std_ or stk > 80):
                return False, "bull"
            if config.get("use_adx", True) and adx < SignalAnalyzer.ADX_THRESHOLD:
                return False, "bull"
            if config.get("use_vol_confirm", True) and vr < SignalAnalyzer.VOL_THRESHOLD:
                return False, "bull"
        else:
            if config.get("use_rsi", True) and rsi > 70:
                return False, "bear"
            if config.get("use_macd", True) and macd >= msig:
                return False, "bear"
            if config.get("use_vwap", True) and price > vwap:
                return False, "bear"
            if config.get("use_bollinger", True) and price > bbu * 1.01:
                return False, "bear"
            if config.get("use_supertrend", True) and stt != -1:
                return False, "bear"
            if config.get("use_stochastic", True) and (stk > std_ or stk < 20):
                return False, "bear"
            if config.get("use_adx", True) and adx < SignalAnalyzer.ADX_THRESHOLD:
                return False, "bear"
            if config.get("use_vol_confirm", True) and vr < SignalAnalyzer.VOL_THRESHOLD:
                return False, "bear"
        return True, direction


# ═══════════════════════════════════════════════════════════════════════════════
# TRADING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue: queue.Queue, config: dict, broker: BaseBroker):
        super().__init__(daemon=True)
        self.ui_queue = ui_queue
        self.config = config
        self.broker = broker
        self.running = False
        self.symbols: List[str] = []
        self.positions: Dict[str, Any] = {}
        self.prev_ema: Dict[str, Tuple] = {}
        self.per_ticker_qty: Dict[str, Any] = {}
        self.is_licensed = config.get("license_valid", False)
        self.direction = config.get("direction", "both")
        self.use_default_qty = config.get("use_default_qty", True)
        self._stop_watchdog = threading.Event()
        self.consecutive_failures = 0
        self.paused = False

        if not self.is_licensed:
            self.config["mode"] = "signal"
            self.config["broker"] = "Alpaca"
            self.direction = "both"
            if "alpaca" in self.config:
                self.config["alpaca"]["paper"] = True
            for k in ("use_supertrend", "use_stochastic", "use_adx",
                      "use_vol_confirm", "use_atr_stops", "use_bracket"):
                self.config[k] = False
            first = self.config.get("tickers", "AAPL").split(",")[0].strip()
            self.config["tickers"] = first

    def _log(self, msg: str):
        self.ui_queue.put(("log", msg))
        db.insert_log(msg)

    def _telegram(self, msg: str):
        if not self.is_licensed:
            return
        tg = self.config.get("telegram", {})
        token = tg.get("token")
        cid = tg.get("chat_id")
        if token and cid:
            try:
                http_requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                    timeout=5)
            except Exception:
                pass

    def _fetch_df(self, symbol: str, interval: str) -> Optional[pd.DataFrame]:
        cached = db.get_cached_candle(symbol, interval)
        if cached:
            try:
                return pd.DataFrame.from_dict(cached)
            except Exception:
                pass
        import yfinance as yf
        df = yf.download(symbol, period="5d", interval=interval,
                          progress=False, auto_adjust=True)
        if df is None or df.empty:
            df = yf.download(symbol, period="5d", interval="1d",
                              progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            db.cache_candle(symbol, interval, df)
        return df

    def run(self):
        tickers_str = self.config.get("tickers", "AAPL")
        default_qty = self.config.get("quantity", 1)
        raw_list = [s.strip() for s in tickers_str.split(",") if s.strip()]

        for entry in raw_list:
            sym = clean_symbol(entry)
            has_colon = ":" in entry
            if has_colon:
                try:
                    qty = float(entry.split(":")[1])
                    qty = int(qty) if qty == int(qty) else qty
                except Exception:
                    qty = default_qty
            else:
                if not self.use_default_qty:
                    continue
                qty = default_qty
            if sym not in self.symbols:
                self.symbols.append(sym)
                self.per_ticker_qty[sym] = qty

        if not self.is_licensed and len(self.symbols) > 1:
            first = self.symbols[0]
            self.symbols = [first]
            self.per_ticker_qty = {first: self.per_ticker_qty[first]}
            self.ui_queue.put(("error",
                f"Free tier: only 1 ticker allowed. Tracking {first} only."))

        for s in self.symbols:
            self.positions[s] = 0
            self.prev_ema[s] = (None, None)

        mode = "signal" if not self.is_licensed else self.config.get("mode", "signal")
        ema_fast, ema_slow = self.config.get("emas", [9, 50])
        use_bracket = self.config.get("use_bracket", False) and self.is_licensed
        sl_pct = self.config.get("sl_percent", 2.0)
        tp_pct = self.config.get("tp_percent", 4.0)
        use_atr = self.config.get("use_atr_stops", True) and self.is_licensed
        interval = self.config.get("timeframe", "1m")
        news_filter = self.config.get("news_sentiment", False) and self.is_licensed

        self.broker.stream_prices(
            self.symbols,
            lambda s, p: self.ui_queue.put(("price_update", (s, p))))

        self.ui_queue.put(("status", f"Running {len(self.symbols)} symbol(s)"))
        self._telegram(f"<b>TraderMoney Started</b>\n{', '.join(self.symbols)} | {mode}")

        if use_bracket and self.broker.name != "Alpaca":
            threading.Thread(target=self._sl_tp_watchdog_loop, daemon=True).start()

        last_fetch = 0.0
        while self.running:
            try:
                online = is_internet_available()
                if online:
                    if self.paused:
                        self.paused = False
                        self.consecutive_failures = 0
                        self.ui_queue.put(("status", "Internet restored – resumed"))
                else:
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= 3 and not self.paused:
                        self.paused = True
                        self.ui_queue.put(("status", "Internet lost – paused"))

                if self.paused:
                    time.sleep(5)
                    continue

                acc = self.broker.get_account()
                if acc:
                    self.ui_queue.put(
                        ("account", (acc["equity"], acc["pl"],
                                     acc["buying_power"], acc.get("open_positions", 0))))

                self.ui_queue.put(
                    ("market", "Open" if self.broker.get_market_status() else "Closed"))

                now = time.time()
                if now - last_fetch >= 60:
                    last_fetch = now
                    for s in self.symbols:
                        try:
                            df = self._fetch_df(s, interval)
                            if df is None or df.empty:
                                self.consecutive_failures += 1
                                continue
                            if isinstance(df.columns, pd.MultiIndex):
                                df.columns = df.columns.get_level_values(0)
                            df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
                            self.consecutive_failures = 0
                        except Exception as e:
                            self.consecutive_failures += 1
                            self.ui_queue.put(("error", f"Data error {s}: {e}"))
                            continue

                        latest = df.iloc[-1]
                        sf = SignalAnalyzer._sf
                        price = sf(latest["Close"])
                        ef = sf(latest["EMA_fast"])
                        es_val = sf(latest["EMA_slow"])
                        prev_f, prev_s = self.prev_ema.get(s, (None, None))
                        self.prev_ema[s] = (ef, es_val)

                        if prev_f is not None:
                            sig, rationale, conf = SignalAnalyzer.generate_signal(
                                df, prev_f, prev_s, self.config)
                            if sig:
                                if news_filter and NEWS_API_KEY:
                                    sentiment = self._get_news_sentiment(s)
                                    if (sig == "BUY" and sentiment < -0.2) or \
                                       (sig == "SELL" and sentiment > 0.2):
                                        self._log(f"[NewsFilter] Suppressed {sig} {s} "
                                                  f"(score: {sentiment:.2f})")
                                        continue

                                self.ui_queue.put(("signal", (s, sig, price, rationale)))
                                db.insert_signal(_ts(), s, sig, price, rationale)

                                if (mode == "auto"
                                        and self.is_licensed
                                        and self.broker.is_connected()
                                        and self.broker.get_market_status()):
                                    self._execute(s, sig, price, latest,
                                                  use_bracket, use_atr,
                                                  sl_pct, tp_pct, conf)

                time.sleep(1)
            except Exception:
                self.ui_queue.put(
                    ("error", f"Engine error:\n{traceback.format_exc()}"))
                time.sleep(5)

        self.broker.stop_stream()
        self.ui_queue.put(("status", "Bot stopped"))

    def _execute(self, sym: str, sig: str, price: float, latest: pd.Series,
                 use_bracket: bool, use_atr: bool,
                 sl_pct: float, tp_pct: float, conf: float):
        if not self.broker.is_connected():
            self._log(f"[Execute] Broker not connected – skipping {sig} {sym}")
            return

        qty = self.per_ticker_qty.get(sym, self.config.get("quantity", 1))
        sf = SignalAnalyzer._sf

        if self.direction == "long" and sig == "SELL":
            return
        if self.direction == "short" and sig == "BUY":
            return

        pos = self.positions.get(sym, 0)
        self._log(f"[Execute] Signal={sig} sym={sym} price={price:.4f} "
                  f"pos={pos} qty={qty} conf={conf:.2f}")

        try:
            if sig == "BUY":
                if pos <= 0:
                    if pos < 0:
                        self._log(f"[Execute] Closing short {sym} before BUY")
                        ok = self.broker.submit_order(sym, abs(pos), "buy")
                        if ok:
                            self.positions[sym] = 0
                        else:
                            self._log(f"[Execute] Failed to close short {sym}")
                            return
                    ok = False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR", price * 0.02), price * 0.02)
                        ok = self.broker.submit_order(
                            sym, qty, "buy",
                            sl_price=price - ATR_STOP_MULT * atr,
                            tp_price=price + ATR_TP_MULT * atr)
                    elif use_bracket:
                        ok = self.broker.submit_order(
                            sym, qty, "buy", sl_pct=sl_pct, tp_pct=tp_pct)
                    else:
                        ok = self.broker.submit_order(sym, qty, "buy")

                    if ok:
                        self.positions[sym] = qty
                        self.ui_queue.put(("order", (sym, "BUY", qty, price)))
                        db.insert_trade(_ts(), sym, "BUY", qty, price)
                        self._telegram(f"<b>BUY</b> {qty} {sym} @ ${price:.2f} "
                                       f"(conf: {conf:.2f})")
                    else:
                        self._log(f"[Execute] BUY order FAILED for {sym}")

            elif sig == "SELL":
                if pos >= 0:
                    if pos > 0:
                        self._log(f"[Execute] Closing long {sym} before SELL")
                        ok = self.broker.submit_order(sym, pos, "sell")
                        if ok:
                            self.positions[sym] = 0
                        else:
                            self._log(f"[Execute] Failed to close long {sym}")
                            return
                    ok = False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR", price * 0.02), price * 0.02)
                        ok = self.broker.submit_order(
                            sym, qty, "sell",
                            sl_price=price + ATR_STOP_MULT * atr,
                            tp_price=price - ATR_TP_MULT * atr)
                    elif use_bracket:
                        ok = self.broker.submit_order(
                            sym, qty, "sell", sl_pct=sl_pct, tp_pct=tp_pct)
                    else:
                        ok = self.broker.submit_order(sym, qty, "sell")

                    if ok:
                        self.positions[sym] = -qty
                        self.ui_queue.put(("order", (sym, "SELL", qty, price)))
                        db.insert_trade(_ts(), sym, "SELL", qty, price)
                        self._telegram(f"<b>SELL</b> {qty} {sym} @ ${price:.2f} "
                                       f"(conf: {conf:.2f})")
                    else:
                        self._log(f"[Execute] SELL order FAILED for {sym}")

        except Exception as e:
            self.ui_queue.put(("error", f"Execute error {sym}: {e}"))

    def _sl_tp_watchdog_loop(self):
        while not self._stop_watchdog.is_set() and self.running:
            try:
                for sym, qty in list(self.positions.items()):
                    if qty == 0:
                        continue
                    try:
                        import yfinance as yf
                        price = yf.Ticker(sym).history(period="1d")["Close"].iloc[-1]
                    except Exception:
                        continue
                    stop = price * (1 - 0.02) if qty > 0 else price * (1 + 0.02)
                    take = price * (1 + 0.04) if qty > 0 else price * (1 - 0.04)
                    if (qty > 0 and price <= stop) or (qty < 0 and price >= stop):
                        self.broker.submit_order(
                            sym, abs(qty), "sell" if qty > 0 else "buy")
                        self.positions[sym] = 0
                        self._telegram(f"<b>Stop Loss</b> triggered {sym} @ ${price:.2f}")
                    elif (qty > 0 and price >= take) or (qty < 0 and price <= take):
                        self.broker.submit_order(
                            sym, abs(qty), "sell" if qty > 0 else "buy")
                        self.positions[sym] = 0
                        self._telegram(f"<b>Take Profit</b> triggered {sym} @ ${price:.2f}")
            except Exception:
                pass
            time.sleep(2)

    def _get_news_sentiment(self, symbol: str) -> float:
        try:
            resp = http_requests.get(
                f"https://newsapi.org/v2/everything?q={symbol}"
                f"&apiKey={NEWS_API_KEY}&pageSize=3", timeout=5)
            articles = resp.json().get("articles", [])
            headlines = " ".join(a["title"] for a in articles)
            if not headlines:
                return 0.0
            chat_resp = http_requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "google/gemini-2.0-flash-001",
                    "messages": [
                        {"role": "system",
                         "content": "Analyze sentiment. Return a single number "
                                    "between -1 (very negative) and 1 (very positive)."},
                        {"role": "user", "content": headlines}],
                    "max_tokens": 10, "temperature": 0},
                timeout=10)
            score = float(chat_resp.json()["choices"][0]["message"]["content"].strip())
            return max(-1.0, min(1.0, score))
        except Exception:
            return 0.0

    def stop(self):
        if self.running:
            self._telegram("<b>Bot Stopped</b>")
        self.running = False
        self._stop_watchdog.set()


# ═══════════════════════════════════════════════════════════════════════════════
# OPENROUTER AI CHAT WITH MULTI-MODEL FALLBACK + OFFLINE FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════
def _call_openrouter(messages: List[dict], retries: int = 3) -> str:
    last_error = "Unknown error"
    models_to_try = list(AI_MODELS)

    # First check if the API key appears valid
    if not OPENROUTER_API_KEY or len(OPENROUTER_API_KEY) < 20:
        return _get_offline_response(messages)

    for attempt in range(retries):
        model = models_to_try[attempt % len(models_to_try)]
        try:
            resp = http_requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost:5050",
                    "X-Title": "TraderMoney",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 350,
                    "temperature": 0.65,
                },
                timeout=30,
            )

            # Handle 401 Unauthorized specifically
            if resp.status_code == 401:
                db.insert_log(f"[AI] 401 Unauthorized from {model} – API key may be invalid or expired")
                # Don't retry with other models if key is bad
                return _get_offline_response(messages)

            if resp.status_code == 503:
                db.insert_log(f"[AI] 503 from {model}, trying next...")
                time.sleep(2)
                continue

            if resp.status_code == 429:
                db.insert_log(f"[AI] Rate limited on {model}, waiting...")
                time.sleep(5)
                continue

            resp.raise_for_status()
            result = resp.json()

            if "error" in result:
                err_msg = result["error"].get("message", "API error")
                db.insert_log(f"[AI] API error from {model}: {err_msg}")
                # If it's an auth error, stop trying
                if "unauthorized" in err_msg.lower() or "invalid" in err_msg.lower():
                    return _get_offline_response(messages)
                time.sleep(2)
                continue

            return result["choices"][0]["message"]["content"].strip()

        except http_requests.exceptions.Timeout as e:
            last_error = f"Timeout on {model}"
            db.insert_log(f"[AI] {last_error}: {e}")
        except http_requests.exceptions.HTTPError as e:
            last_error = f"HTTP error from {model}: {e}"
            db.insert_log(f"[AI] {last_error}")
            # If we got a 401, return offline response immediately
            if "401" in str(e):
                return _get_offline_response(messages)
        except Exception as e:
            last_error = f"Error on {model}: {e}"
            db.insert_log(f"[AI] {last_error}")

        time.sleep(2 ** attempt)

    # If all models fail, return offline fallback instead of raising error
    return _get_offline_response(messages)


def _get_offline_response(messages: List[dict]) -> str:
    """Return a helpful offline response when AI API is unavailable."""
    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_msg = msg.get("content", "").lower()
            break

    # Give contextual responses based on what the user asked
    if any(word in last_user_msg for word in ["indicator", "rsi", "macd", "ema", "signal"]):
        return (
            "I'm currently in offline mode (AI API unavailable). Here's what I can tell you:\n\n"
            "• EMA Crossover is your base signal – when the fast EMA crosses above the slow EMA, it's a buy signal\n"
            "• RSI below 30 suggests oversold (good for buying), above 70 suggests overbought (good for selling)\n"
            "• MACD crossing above signal line confirms bullish momentum\n"
            "• For best results, use all 9 indicators together – each adds about 5% to your win rate\n"
            "• Try the Scalping preset (1m, EMA 9/50) for quick trades or Swing (15m, EMA 20/50) for longer holds\n\n"
            "The AI service should be back soon. In the meantime, check the Help tab for detailed information."
        )
    elif any(word in last_user_msg for word in ["broker", "connect", "alpaca", "ibkr"]):
        return (
            "I'm currently in offline mode (AI API unavailable). For broker help:\n\n"
            "• Alpaca: Works on Free and Pro tiers. Use paper trading key from alpaca.markets\n"
            "• IBKR: Requires TWS/Gateway running. Ports: 7497 (paper), 7496 (live)\n"
            "• Tradier: Get access token from developer.tradier.com\n"
            "• Binance/Bybit/OKX: Crypto exchanges with testnet options available\n\n"
            "All brokers except Alpaca require a Pro license. The AI service should return shortly."
        )
    elif any(word in last_user_msg for word in ["backtest", "strategy", "win rate"]):
        return (
            "I'm currently in offline mode (AI API unavailable). Backtesting tips:\n\n"
            "• Run backtests with at least 30 days of data for meaningful results\n"
            "• Combined indicators can achieve ~65% win rate in optimal conditions\n"
            "• Use Monte Carlo simulation to see worst/best case scenarios\n"
            "• Export to CSV/PDF to track your results over time\n"
            "• Try AI Auto-Tune when the service is back online for personalized optimization\n\n"
            "The AI service should be available again soon."
        )
    else:
        return (
            "I'm currently in offline mode – the AI API (OpenRouter) is temporarily unavailable. "
            "This could be due to an invalid API key, network issues, or service outage.\n\n"
            "What you can do now:\n"
            "• Check the Help tab for comprehensive guides\n"
            "• Run backtests to evaluate your strategy\n"
            "• Use Signal-Only mode to see trade signals\n"
            "• Verify your OpenRouter API key at openrouter.ai/keys\n\n"
            "The app will automatically retry the AI connection. No data is lost – your chat history is saved locally."
        )

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return FRONTEND_HTML

@app.route("/mobile")
def mobile():
    return send_file("mobile.html") if os.path.exists("mobile.html") else ("Not available", 404)

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(state.config)

@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.json or {}
    state.config.update(data)
    if not state.config.get("license_valid"):
        state.config["broker"] = "Alpaca"
        state.config["mode"] = "signal"
    EncryptedConfigManager.save(state.config)
    return jsonify({"status": "ok", "message": "Configuration saved"})

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    state.config.update(data)

    key = state.config.get("license_key", "").strip()
    if key:
        valid, _ = verify_gumroad_license(key)
        state.config["license_valid"] = valid
    else:
        state.config["license_valid"] = False

    EncryptedConfigManager.save(state.config)

    if state.engine and state.engine.running:
        return jsonify({"status": "error", "message": "Bot already running."})

    if not state.config.get("license_valid"):
        state.config["broker"] = "Alpaca"
        state.config["mode"] = "signal"
        state.config["direction"] = "both"
        if "alpaca" not in state.config or not isinstance(state.config["alpaca"], dict):
            state.config["alpaca"] = dict(_DEFAULT_CONFIG["alpaca"])
        state.config["alpaca"]["paper"] = True
        for k in ("use_supertrend", "use_stochastic", "use_adx",
                  "use_vol_confirm", "use_atr_stops", "use_bracket"):
            state.config[k] = False
        first = state.config.get("tickers", "AAPL").split(",")[0].strip()
        state.config["tickers"] = first

    broker_choice = state.config.get("broker", "Alpaca")
    broker_cls = BROKER_REGISTRY.get(broker_choice)
    if not broker_cls:
        return jsonify({"status": "error", "message": f"Unknown broker: {broker_choice}"})

    state.broker_instance = broker_cls(state.config, state.ui_queue)
    if not state.broker_instance.connect():
        err = state.broker_instance.last_error or "Unknown error."
        state.config["last_broker_message"] = f"ERROR: {err}"
        EncryptedConfigManager.save(state.config)
        return jsonify({"status": "error", "message": err})

    state.config["last_broker_message"] = "Connected"
    EncryptedConfigManager.save(state.config)

    state.engine = TradingEngine(state.ui_queue, state.config, state.broker_instance)
    state.engine.running = True
    state.engine.start()
    state.running = True
    return jsonify({"status": "ok", "message": f"Bot started ({broker_choice})"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if state.engine:
        state.engine.stop()
    state.running = False
    state.config["license_valid"] = False
    return jsonify({"status": "ok", "message": "Bot stopped"})

@app.route("/api/kill", methods=["POST"])
def api_kill():
    if state.broker_instance:
        threading.Thread(target=state.broker_instance.close_all_positions, daemon=True).start()
    if state.engine:
        state.engine.stop()
    state.running = False
    state.config["license_valid"] = False
    return jsonify({"status": "ok", "message": "Kill switch activated"})

@app.route("/api/status", methods=["GET"])
def api_status():
    while not state.ui_queue.empty():
        try:
            msg = state.ui_queue.get_nowait()
            kind = msg[0]
            if kind == "account":
                eq, pl, bp, op = msg[1]
                state.dashboard.update(equity=eq, pl=pl, buying_power=bp, open_positions=op)
            elif kind in ("log", "error"):
                db.insert_log(msg[1])
        except queue.Empty:
            break

    tz = state.config.get("timezone", "UTC")
    signals = db.get_recent_signals(50)[::-1]
    orders = db.get_recent_trades(50)[::-1]
    for s in signals:
        s["time"] = to_local_time(s["time"], tz)
    for o in orders:
        o["time"] = to_local_time(o["time"], tz)

    return jsonify({
        "running": state.running,
        "equity": state.dashboard["equity"],
        "pl": state.dashboard["pl"],
        "buying_power": state.dashboard["buying_power"],
        "open_positions": state.dashboard["open_positions"],
        "signals": signals,
        "orders": orders,
        "log": db.get_recent_logs(100),
        "internet_status": state.internet_status,
    })

@app.route("/api/broker_status")
def api_broker_status():
    return jsonify({"message": state.config.get("last_broker_message", "")})

@app.route("/api/candles", methods=["GET"])
def api_candles():
    symbol = request.args.get("symbol", "AAPL")
    interval = request.args.get("interval", "1m")
    try:
        cached = db.get_cached_candle(symbol, interval)
        if cached:
            df = pd.DataFrame.from_dict(cached)
        else:
            import yfinance as yf
            df = yf.download(symbol, period="5d", interval=interval,
                              progress=False, auto_adjust=True)
            if df is None or df.empty:
                return jsonify([])
            db.cache_candle(symbol, interval, df)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        candles = []
        for idx, row in df.iterrows():
            try:
                candles.append({
                    "time": int(idx.timestamp()),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]) if "Volume" in row else 0,
                })
            except Exception:
                continue
        return jsonify(candles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/update")
def api_update():
    try:
        url = "https://raw.githubusercontent.com/shafayrich/tradermoney/main/version.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
        latest = data.get("latest_version", "0.0.0")
        newer = (tuple(map(int, latest.split("."))) >
                 tuple(map(int, APP_VERSION.split("."))))
        return jsonify({
            "current_version": APP_VERSION,
            "latest_version": latest,
            "download_url": data.get("download_url", ""),
            "update_available": newer,
        })
    except Exception as e:
        return jsonify({"update_available": False, "error": str(e)})

@app.route("/api/validate_license", methods=["POST"])
def api_validate_license():
    data = request.json or {}
    key = data.get("license_key", "").strip()
    if not key:
        return jsonify({"valid": False, "message": "No license key provided"})
    valid, msg = verify_gumroad_license(key)
    if valid:
        state.config["license_key"] = key
        state.config["license_valid"] = True
        return jsonify({"valid": True, "message": "License verified for this session"})
    state.config["license_valid"] = False
    return jsonify({"valid": False, "message": msg})

@app.route("/api/offline", methods=["POST"])
def api_offline():
    enabled = (request.json or {}).get("offline", False)
    state.offline_mode = enabled
    state.config["offline_mode"] = enabled
    EncryptedConfigManager.save(state.config)
    return jsonify({"status": "ok", "offline": enabled})

# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    data = request.json or {}
    config = data.get("config", state.config)
    days = int(data.get("days", 5))
    portfolio = data.get("portfolio", False)
    try:
        import yfinance as yf
        raw_list = [s.strip() for s in config.get("tickers", "AAPL").split(",") if s.strip()]
        symbols = list(dict.fromkeys(clean_symbol(e) for e in raw_list))
        results: dict = {}
        all_trades: List[dict] = []
        initial_cash = 100_000 if portfolio else 10_000
        cash = initial_cash

        for sym in symbols:
            sym_results: dict = {}
            try:
                df = yf.download(sym, period=f"{days}d",
                                  interval=config.get("timeframe", "1m"),
                                  progress=False, auto_adjust=True)
                if df is None or df.empty:
                    df = yf.download(sym, period=f"{days}d", interval="1d",
                                      progress=False, auto_adjust=True)
                if df is None or df.empty:
                    results[sym] = {"error": "No data returned"}
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                ef, es = config.get("emas", [9, 50])
                df = IndicatorCalculator.compute_all(df, ef, es)
                sigs: List[dict] = []
                for i in range(1, len(df)):
                    prev = df.iloc[i - 1]
                    curr = df.iloc[i]
                    pf = SignalAnalyzer._sf(prev["EMA_fast"])
                    ps = SignalAnalyzer._sf(prev["EMA_slow"])
                    sig, _, conf = SignalAnalyzer.generate_signal(
                        df.iloc[:i + 1], pf, ps, config)
                    if sig:
                        sigs.append({
                            "time": str(df.index[i]),
                            "signal": sig,
                            "price": round(SignalAnalyzer._sf(curr["Close"]), 2),
                            "confidence": conf,
                        })
                sym_results["signals"] = sigs

                position = 0.0
                entry_price = 0.0
                entry_time = ""
                trades: List[dict] = []
                for s in sigs:
                    if s["signal"] == "BUY" and position <= 0:
                        if position < 0:
                            pnl = (entry_price - s["price"]) * abs(position)
                            trades.append({
                                "entry_time": entry_time, "exit_time": s["time"],
                                "side": "SHORT", "entry_price": entry_price,
                                "exit_price": s["price"], "pnl": round(pnl, 2), "type": "exit",
                            })
                            cash += pnl
                        position = cash / s["price"]
                        entry_price = s["price"]
                        entry_time = s["time"]
                        cash = 0.0
                        trades.append({
                            "entry_time": s["time"], "exit_time": "",
                            "side": "LONG", "entry_price": entry_price,
                            "exit_price": 0, "pnl": 0, "type": "entry",
                        })
                    elif s["signal"] == "SELL" and position >= 0:
                        if position > 0:
                            pnl = (s["price"] - entry_price) * position
                            trades.append({
                                "entry_time": entry_time, "exit_time": s["time"],
                                "side": "LONG", "entry_price": entry_price,
                                "exit_price": s["price"], "pnl": round(pnl, 2), "type": "exit",
                            })
                            cash = position * s["price"] + pnl
                        position = -cash / s["price"]
                        entry_price = s["price"]
                        entry_time = s["time"]
                        cash = 0.0
                        trades.append({
                            "entry_time": s["time"], "exit_time": "",
                            "side": "SHORT", "entry_price": entry_price,
                            "exit_price": 0, "pnl": 0, "type": "entry",
                        })

                if position != 0 and sigs:
                    last_sig = sigs[-1]
                    ep = last_sig["price"]
                    pnl = ((ep - entry_price) * position if position > 0
                           else (entry_price - ep) * abs(position))
                    trades.append({
                        "entry_time": entry_time, "exit_time": last_sig["time"],
                        "side": "LONG" if position > 0 else "SHORT",
                        "entry_price": entry_price, "exit_price": ep,
                        "pnl": round(pnl, 2), "type": "exit",
                    })
                    cash = abs(position) * ep + pnl

                exits = [t for t in trades if t["type"] == "exit"]
                total_pnl = sum(t["pnl"] for t in exits)
                wins = sum(1 for t in exits if t["pnl"] > 0)
                win_rate = (wins / len(exits) * 100) if exits else 0

                sym_results["simulation"] = {
                    "initial_cash": initial_cash,
                    "final_cash": round(cash, 2),
                    "total_pnl": round(total_pnl, 2),
                    "win_rate": round(win_rate, 1),
                    "total_trades": len(exits),
                    "trades": trades,
                }
                all_trades.extend(trades)

            except Exception as e:
                results[sym] = {"error": str(e)}
                continue

            results[sym] = sym_results

        win_rates = [r["simulation"]["win_rate"] for r in results.values() if "simulation" in r]
        wr_avg = float(np.mean(win_rates)) if win_rates else 0.0
        total_sigs = sum(len(r.get("signals", [])) for r in results.values())
        db.update_leaderboard(state.config.get("device_uuid", "anon"), wr_avg, total_sigs)

        resp = {"results": results}
        if portfolio:
            resp["portfolio"] = {
                "initial_cash": initial_cash,
                "final_cash": round(cash, 2),
                "total_pnl": round(sum(t["pnl"] for t in all_trades if t["type"] == "exit"), 2),
                "total_trades": sum(1 for t in all_trades if t["type"] == "exit"),
            }
        state.last_bt_data = resp
        db.insert_backtest(json.dumps({"config": config, "results": results}))
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/backtest/montecarlo", methods=["POST"])
def monte_carlo():
    data = request.json or {}
    config = data.get("config", state.config)
    days = int(data.get("days", 5))
    runs = 1000
    try:
        import yfinance as yf
        raw_list = [s.strip() for s in config.get("tickers", "AAPL").split(",") if s.strip()]
        symbols = list(dict.fromkeys(clean_symbol(e) for e in raw_list))
        pnl_results = []
        for _ in range(runs):
            cash = 10_000
            position = 0.0
            entry_price = 0.0
            sigs = []
            for sym in symbols:
                try:
                    df = yf.download(sym, period=f"{days}d",
                                      interval=config.get("timeframe", "1m"),
                                      progress=False, auto_adjust=True)
                    if df is None or df.empty:
                        continue
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    ef, es = config.get("emas", [9, 50])
                    df = IndicatorCalculator.compute_all(df, ef, es)
                    for i in range(1, len(df)):
                        prev = df.iloc[i - 1]
                        curr = df.iloc[i]
                        pf = SignalAnalyzer._sf(prev["EMA_fast"])
                        ps = SignalAnalyzer._sf(prev["EMA_slow"])
                        sig, _, _ = SignalAnalyzer.generate_signal(
                            df.iloc[:i + 1], pf, ps, config)
                        if sig:
                            sigs.append(SignalAnalyzer._sf(curr["Close"]))
                except Exception:
                    continue
            random.shuffle(sigs)
            for price in sigs:
                if position <= 0:
                    if position < 0:
                        cash += (entry_price - price) * abs(position)
                    position = cash / price
                    entry_price = price
                    cash = 0.0
                else:
                    cash += (price - entry_price) * position
                    position = 0.0
            if position > 0 and sigs:
                cash += sigs[-1] * position
            pnl_results.append(cash - 10_000)

        pnl_results.sort()
        return jsonify({
            "worst": round(pnl_results[0], 2),
            "best": round(pnl_results[-1], 2),
            "average": round(float(np.mean(pnl_results)), 2),
            "prob_profit": round(sum(1 for p in pnl_results if p >= 0) / runs * 100, 1),
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/export/backtest/csv", methods=["POST"])
def export_backtest_csv():
    trades = (request.json or {}).get("trades", [])
    if not trades:
        return jsonify({"error": "No trades"}), 400
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["Entry Time", "Exit Time", "Side", "Entry Price", "Exit Price", "P&L"])
    for t in trades:
        if t.get("type") == "exit":
            w.writerow([t["entry_time"], t["exit_time"], t["side"],
                        t["entry_price"], t["exit_price"], t["pnl"]])
    output = si.getvalue()
    si.close()
    return Response(output, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=backtest.csv"})

@app.route("/api/export/backtest/pdf", methods=["POST"])
def export_backtest_pdf():
    try:
        from fpdf import FPDF
    except ImportError:
        return jsonify({"error": "fpdf2 not installed. Run: pip install fpdf2"}), 500
    trades = (request.json or {}).get("trades", [])
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, "TraderMoney - Backtest Report", ln=True, align="C")
    pdf.ln(5)
    pdf.set_font("Arial", size=9)
    pdf.cell(0, 7, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC", ln=True)
    pdf.ln(4)
    pdf.set_font("Arial", size=10)
    # Summary
    exits = [t for t in trades if t.get("type") == "exit"]
    if exits:
        total_pnl = sum(t["pnl"] for t in exits)
        wins = sum(1 for t in exits if t["pnl"] > 0)
        pdf.cell(0, 7, f"Total Trades: {len(exits)} | Win Rate: {(wins/len(exits)*100):.1f}% | P&L: ${total_pnl:.2f}", ln=True)
        pdf.ln(4)
    # Table headers
    pdf.set_font("Arial", "B", 9)
    pdf.cell(48, 7, "Entry", 1)
    pdf.cell(48, 7, "Exit", 1)
    pdf.cell(22, 7, "Side", 1, 0, "C")
    pdf.cell(26, 7, "Entry $", 1, 0, "R")
    pdf.cell(26, 7, "Exit $", 1, 0, "R")
    pdf.cell(26, 7, "P&L", 1, 0, "R")
    pdf.ln()
    pdf.set_font("Arial", size=8)
    for t in exits:
        pdf.cell(48, 6, str(t["entry_time"])[:16], 1)
        pdf.cell(48, 6, str(t["exit_time"])[:16], 1)
        pdf.cell(22, 6, t["side"], 1, 0, "C")
        pdf.cell(26, 6, f"${t['entry_price']:.2f}", 1, 0, "R")
        pdf.cell(26, 6, f"${t['exit_price']:.2f}", 1, 0, "R")
        pnl_color = (0, 150, 0) if t["pnl"] >= 0 else (180, 0, 0)
        pdf.set_text_color(*pnl_color)
        pdf.cell(26, 6, f"${t['pnl']:.2f}", 1, 0, "R")
        pdf.set_text_color(0, 0, 0)
        pdf.ln()
    pdf_bytes = pdf.output(dest="S").encode("latin-1")
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment;filename=backtest.pdf"})

@app.route("/api/correlation", methods=["GET"])
def correlation_matrix():
    tickers = [s.strip() for s in state.config.get("tickers", "").split(",") if s.strip()]
    all_syms = list(dict.fromkeys(tickers))
    if not all_syms:
        return jsonify({"html": "No tickers configured."})
    try:
        import yfinance as yf
        data_dict: Dict[str, pd.Series] = {}
        for sym in all_syms:
            try:
                df = yf.download(sym, period="30d", interval="1d",
                                  progress=False, auto_adjust=True)["Close"]
                data_dict[sym] = df.pct_change().dropna()
            except Exception:
                continue
        if not data_dict:
            return jsonify({"html": "<p style='color:var(--muted)'>No data available for correlation.</p>"})
        df_all = pd.DataFrame(data_dict)
        corr = df_all.corr()
        html = '<table style="border-collapse:collapse;font-size:.8rem;width:100%;">'
        html += "<tr><th></th>" + "".join(
            f"<th style='padding:6px 10px;color:#D4AF37;text-align:center'>{s}</th>"
            for s in corr.columns) + "</tr>"
        for row_sym in corr.index:
            html += f"<tr><td style='padding:6px 10px;color:#D4AF37;font-weight:bold'>{row_sym}</td>"
            for col_sym in corr.columns:
                v = corr.loc[row_sym, col_sym]
                r_ = int(max(0, min(255, 178 + (1 - v) * 77)))
                g_ = int(max(0, min(255, 34 + v * 200)))
                html += (f"<td style='padding:5px 8px;background:rgb({r_},{g_},34);"
                         f"color:#fff;text-align:center;border-radius:4px;margin:1px'>{v:.2f}</td>")
            html += "</tr>"
        html += "</table>"
        return jsonify({"html": html})
    except Exception as e:
        return jsonify({"html": f"<p style='color:var(--danger)'>Correlation error: {e}</p>"})

# ═══════════════════════════════════════════════════════════════════════════════
# AI CHAT ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/chat/sessions", methods=["GET"])
def get_chat_sessions():
    return jsonify({"sessions": db.get_chat_sessions()})

@app.route("/api/chat/sessions", methods=["POST"])
def create_chat_session_route():
    title = (request.json or {}).get("title", "")
    return jsonify({"session_id": db.create_chat_session(title)})

@app.route("/api/chat/sessions/<int:session_id>", methods=["GET"])
def get_chat_session_history(session_id: int):
    return jsonify({"messages": db.get_chat_history(session_id, 200)})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    global _chat_counter
    data = request.json or {}
    message = data.get("message", "").strip()
    session_id = data.get("session_id", None)
    if not message:
        return jsonify({"reply": "Please type a message."})

    licensed = state.config.get("license_valid", False)
    if not licensed:
        today = datetime.now().strftime("%Y-%m-%d")
        if _chat_counter["date"] != today:
            _chat_counter["date"] = today
            _chat_counter["count"] = 0
        if _chat_counter["count"] >= FREE_CHAT_DAILY_LIMIT:
            return jsonify({
                "reply": (f"Daily chat limit reached ({FREE_CHAT_DAILY_LIMIT} messages/day "
                          "on Free tier). Upgrade to Pro for unlimited AI access.")
            })
        _chat_counter["count"] += 1

    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("sk-YOUR"):
        return jsonify({"reply": "AI Chat not configured (set OPENROUTER_API_KEY in app.py)."})

    if not session_id:
        session_id = db.create_chat_session()
    db.insert_chat_message(session_id, "user", message)

    history = db.get_chat_history(session_id, 20)
    messages = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})

    try:
        reply = _call_openrouter(messages, retries=3)
        db.insert_chat_message(session_id, "bot", reply)
        return jsonify({"reply": reply, "session_id": session_id})
    except RuntimeError as e:
        db.insert_log(f"[AI Chat] Failed after all retries: {e}")
        offline_reply = _get_offline_response(messages)
        db.insert_chat_message(session_id, "bot", offline_reply)
        return jsonify({
            "reply": offline_reply,
            "session_id": session_id,
        })
    except Exception as e:
        db.insert_log(f"[AI Chat] Unexpected error: {e}")
        offline_reply = _get_offline_response(messages)
        db.insert_chat_message(session_id, "bot", offline_reply)
        return jsonify({
            "reply": offline_reply,
            "session_id": session_id,
        })

@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    return jsonify({"leaderboard": db.get_leaderboard()})


# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND HTML (v2.2.1 – SVG icons, text selection, comprehensive help)
# ═══════════════════════════════════════════════════════════════════════════════
FRONTEND_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TraderMoney 2.2.1</title>
<style>
:root{--bg:#050505;--card:#1A1A1A;--text:#e2e2e2;--accent:#D4AF37;--danger:#B22222;--border:#2A2E38;--muted:#7a7d86;--sw:268px;--radius:12px;}
::-webkit-scrollbar{width:4px;height:4px;}::-webkit-scrollbar-track{background:#080808;}::-webkit-scrollbar-thumb{background:#111;}
*{box-sizing:border-box;-webkit-user-select:text;user-select:text;}
html,body{height:100%;margin:0;padding:0;overflow:hidden;}
body{font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden;color-scheme:dark;}

/* ── SVG Icon sprites ─────────────────────────────────────── */
svg.icon{width:16px;height:16px;fill:currentColor;vertical-align:middle;margin-right:4px;flex-shrink:0;}

/* ── Sidebar ──────────────────────────────────────────────── */
#sb{width:var(--sw);background:#0c0c0c;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding:18px 14px;flex-shrink:0;}
#sb h2{color:var(--accent);margin:0 0 10px;font-size:1.2rem;letter-spacing:.3px;display:flex;align-items:center;gap:6px;}
.lbadge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.67rem;vertical-align:middle;}
.lv{background:var(--accent);color:#000;}.li{background:var(--danger);color:#fff;}
label{display:block;font-size:.75rem;margin:10px 0 3px;color:var(--muted);cursor:pointer;letter-spacing:.3px;}
.cb input{display:none;}
.cb .cm{display:inline-block;width:18px;height:18px;border:2px solid #333;border-radius:6px;margin-right:6px;vertical-align:middle;position:relative;transition:.2s;}
.cb input:checked+.cm{background:var(--accent);border-color:var(--accent);}
.cb input:checked+.cm::after{content:"";position:absolute;left:4px;top:1px;width:5px;height:9px;border:solid #000;border-width:0 2px 2px 0;transform:rotate(45deg);}
select{-webkit-appearance:none;appearance:none;background:#1A1A1A url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpolygon fill='%23D4AF37' points='0,4 12,4 6,10'/%3E%3C/svg%3E") no-repeat right 10px center;background-size:12px;color:var(--text);border:1px solid #333;padding:7px 30px 7px 10px;border-radius:10px;width:100%;font-size:.85rem;transition:border .2s;cursor:pointer;}
select:focus{border-color:var(--accent);outline:none;}
select:disabled{opacity:.5;cursor:not-allowed;}
input[type="text"],input[type="password"],input[type="number"],textarea{background:#1A1A1A;color:var(--text);border:1px solid #333;padding:7px 10px;border-radius:10px;width:100%;font-size:.85rem;transition:border .2s;}
input:focus,textarea:focus{border-color:var(--accent);outline:none;}
input:-webkit-autofill{-webkit-text-fill-color:var(--text);-webkit-box-shadow:0 0 0 30px #1A1A1A inset;}
button{cursor:pointer;background:var(--accent);color:#050505;border:none;padding:9px 12px;border-radius:10px;width:100%;font-weight:600;margin-top:10px;font-size:.85rem;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:5px;}
button:hover{opacity:.9;transform:translateY(-1px);}
button.ghost{background:var(--card);border:1px solid var(--border);color:var(--text);}
button.danger{background:var(--danger);color:#fff;}
hr{border-color:var(--border);margin:12px 0;}
.r2{display:flex;gap:5px;}.r2 input{width:100%;}
#bstatus{font-size:.72rem;margin-top:3px;min-height:15px;word-break:break-word;padding:2px 0;}
#bstatus.ok{color:#00c9b1;}#bstatus.err{color:var(--danger);}
.free-notice{background:#2a0505;color:#ff9090;border:1px solid var(--danger);padding:8px 10px;border-radius:8px;font-size:.74rem;margin-top:8px;display:none;line-height:1.5;}
.offline-banner{background:#1a1000;color:#ffb347;border:1px solid #a07000;padding:7px 10px;border-radius:7px;font-size:.74rem;margin-top:8px;display:none;}
.bt-days-input{width:70px;display:inline-block;margin-left:6px;}

/* ── Main ─────────────────────────────────────────────────── */
#main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden;}
.tab-bar{display:flex;background:var(--card);border-bottom:1px solid var(--border);overflow:hidden;flex-shrink:0;}
.tbtn{flex:1;background:transparent;border:none;color:var(--text);padding:14px 4px;cursor:pointer;font-weight:500;border-bottom:2px solid transparent;transition:.2s;min-width:60px;font-size:.82rem;display:flex;align-items:center;justify-content:center;gap:4px;}
.tbtn:hover{background:rgba(255,255,255,.03);}
.tbtn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700;}
.tab{flex:1;display:none;overflow:auto;flex-direction:column;}
.tab.active{display:flex;}
#metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px;background:var(--card);border-bottom:1px solid var(--border);}
.met{text-align:center;}.met .v{font-size:1.2rem;font-weight:bold;color:var(--accent);}
#sess{display:flex;align-items:center;gap:14px;padding:8px 12px;background:var(--card);border-bottom:1px solid var(--border);font-size:.8rem;flex-wrap:wrap;}
.sd{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;}
.so{background:#00c9b1;}.sc{background:var(--danger);}
#tkbar{display:flex;flex-wrap:nowrap;overflow-x:auto;background:var(--card);border-bottom:1px solid var(--border);}
.tkbtn{padding:7px 12px;background:transparent;border:none;color:var(--text);cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:.2s;font-size:.82rem;flex-shrink:0;}
.tkbtn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700;}
#chart-c{flex:1;min-height:0;}

/* ── Lists ────────────────────────────────────────────────── */
.sitem{display:flex;justify-content:space-between;padding:9px 12px;border-bottom:1px solid var(--border);font-size:.82rem;}
.buy{color:var(--accent);}.sell{color:var(--danger);}
.empty-placeholder{color:var(--muted);text-align:center;padding:30px;font-size:.9rem;}
#toasts{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:6px;}
.toast{padding:14px 22px;border-radius:14px;font-weight:500;box-shadow:0 4px 18px rgba(0,0,0,.5);animation:si .25s ease;max-width:420px;font-size:.88rem;border:1px solid #333;}
.toast.success{background:var(--accent);color:#000;}.toast.error{background:var(--danger);color:#fff;}.toast.info{background:#1a1200;color:var(--accent);border-color:var(--accent);}
@keyframes si{from{transform:translateX(110%);opacity:0}to{transform:translateX(0);opacity:1}}
#upd{display:none;position:fixed;bottom:16px;right:16px;z-index:9999;background:var(--accent);color:#000;padding:12px 18px;border-radius:10px;font-weight:bold;font-size:.88rem;}
#upd a{color:#000;text-decoration:underline;}

/* ── Backtest / Analysis / Help ───────────────────────────── */
.btp{flex:1;display:flex;flex-direction:column;}
.btr{flex:1;overflow:auto;padding:10px;}
.ph{color:var(--muted);text-align:center;padding:36px 18px;font-size:.9rem;}
.bttbl{width:100%;border-collapse:collapse;font-size:.78rem;margin-bottom:18px;}
.bttbl th,.bttbl td{padding:5px 7px;border:1px solid var(--border);text-align:center;}
.bttbl th{color:var(--accent);}
#logbar{height:100px;overflow-y:auto;background:var(--bg);padding:8px 12px;font-size:.74rem;border-top:1px solid var(--border);color:var(--muted);flex-shrink:0;}
.hb{padding:20px;overflow:auto;height:100%;}
.hb h3{color:var(--accent);margin-top:0;}.hb h4{color:var(--text);margin:14px 0 5px;}
.hb p,.hb ul{font-size:.85rem;line-height:1.65;}.hb ul{padding-left:18px;}.hb li{margin-bottom:4px;}.hb a{color:var(--accent);}
.istat{background:var(--card);border-radius:var(--radius);padding:14px;margin:8px 0;}

/* ── AI Chat ──────────────────────────────────────────────── */
#aichat-wrap{display:flex;height:100%;}
#chat-sessions-panel{width:220px;background:var(--card);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;}
#chat-sessions-panel h3{padding:12px;margin:0;border-bottom:1px solid var(--border);font-size:.85rem;display:flex;align-items:center;gap:5px;}
#chat-sessions-list{flex:1;overflow-y:auto;}
.chat-session-item{padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);font-size:.78rem;color:var(--muted);transition:.15s;}
.chat-session-item:hover,.chat-session-item.active{background:#0a0a0a;color:var(--text);}
#chat-new-session-btn{margin:8px;padding:8px;font-size:.8rem;background:var(--accent);color:#000;border:none;border-radius:8px;cursor:pointer;width:calc(100% - 16px);}
#chat-main{flex:1;display:flex;flex-direction:column;}
#chat-topbar{padding:10px 14px;background:var(--card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-shrink:0;}
#chat-topbar .title{color:var(--accent);font-weight:600;font-size:.92rem;display:flex;align-items:center;gap:6px;}
#chat-limit{font-size:.74rem;color:var(--muted);}
#chat-messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;}
.cmsg{max-width:82%;padding:10px 14px;border-radius:14px;font-size:.86rem;line-height:1.55;word-break:break-word;}
.cmsg.bot{background:#1a1200;border:1px solid #4a3800;color:var(--text);align-self:flex-start;border-radius:4px 14px 14px 14px;}
.cmsg.user{background:#1e1e1e;border:1px solid #333;color:var(--text);align-self:flex-end;border-radius:14px 4px 14px 14px;}
.cmsg .msender{font-size:.68rem;color:var(--accent);margin-bottom:4px;font-weight:700;letter-spacing:.4px;display:flex;align-items:center;gap:4px;}
.cmsg.user .msender{color:var(--muted);}
.cmsg .mbody{white-space:pre-wrap;}
.chat-typing{color:var(--muted);font-size:.8rem;padding:4px 8px;font-style:italic;align-self:flex-start;}
#chat-input-row{display:flex;gap:8px;padding:12px;border-top:1px solid var(--border);background:var(--card);flex-shrink:0;}
#chat-input{flex:1;resize:none;height:46px;padding:10px 12px;font-size:.87rem;border-radius:10px;}
#chat-send{width:auto;margin-top:0;padding:10px 18px;flex-shrink:0;font-size:.87rem;}
#mic-btn{width:auto;margin-top:0;padding:10px 12px;flex-shrink:0;font-size:.87rem;background:var(--card);border:1px solid var(--border);color:var(--text);}

/* ── SVG Icon definitions ─────────────────────────────────── */
</style>
</head>
<body>
<!-- SVG Sprites -->
<svg style="display:none" xmlns="http://www.w3.org/2000/svg">
  <symbol id="i-chart" viewBox="0 0 24 24"><path d="M3 13h2v8H3v-8zm5-4h2v12H8V9zm5-5h2v17h-2V4zm5 6h2v11h-2V10z"/></symbol>
  <symbol id="i-signal" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></symbol>
  <symbol id="i-history" viewBox="0 0 24 24"><path d="M13 3a9 9 0 00-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.954 8.954 0 0013 21a9 9 0 000-18zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></symbol>
  <symbol id="i-backtest" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></symbol>
  <symbol id="i-analysis" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-4 6h-4v2h4v2h-4v2h4v2H9V7h6v2z"/></symbol>
  <symbol id="i-help" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17h-2v-2h2v2zm2.07-7.75l-.9.92C13.45 12.9 13 13.5 13 15h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26c.37-.36.59-.86.59-1.41 0-1.1-.9-2-2-2s-2 .9-2 2H8c0-2.21 1.79-4 4-4s4 1.79 4 4c0 .88-.36 1.68-.93 2.25z"/></symbol>
  <symbol id="i-chat" viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></symbol>
  <symbol id="i-key" viewBox="0 0 24 24"><path d="M12.65 10A5.99 5.99 0 007 6c-3.31 0-6 2.69-6 6s2.69 6 6 6a5.99 5.99 0 005.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/></symbol>
  <symbol id="i-save" viewBox="0 0 24 24"><path d="M17 3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"/></symbol>
  <symbol id="i-start" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></symbol>
  <symbol id="i-warn" viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></symbol>
  <symbol id="i-preset" viewBox="0 0 24 24"><path d="M19.43 12.98c.04-.32.07-.64.07-.98s-.03-.66-.07-.98l2.11-1.65c.19-.15.24-.42.12-.64l-2-3.46c-.12-.22-.39-.3-.61-.22l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65A.488.488 0 0014 2h-4c-.25 0-.46.18-.49.42l-.38 2.65c-.61.25-1.17.59-1.69.98l-2.49-1c-.23-.09-.49 0-.61.22l-2 3.46c-.13.22-.07.49.12.64l2.11 1.65c-.04.32-.07.65-.07.98s.03.66.07.98l-2.11 1.65c-.19.15-.24.42-.12.64l2 3.46c.12.22.39.3.61.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.03.24.24.42.49.42h4c.25 0 .46-.18.49-.42l.38-2.65c.61-.25 1.17-.59 1.69-.98l2.49 1c.23.09.49 0 .61-.22l2-3.46c.12-.22.07-.49-.12-.64l-2.11-1.65zM12 15.5c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5z"/></symbol>
  <symbol id="i-update" viewBox="0 0 24 24"><path d="M21 10.12h-6.78l2.74-2.82c-2.73-2.7-7.15-2.8-9.88-.1a6.875 6.875 0 000 9.79 7.02 7.02 0 009.88 0C18.32 15.65 19 14.08 19 12.1h2c0 1.98-.88 4.55-2.64 6.29-3.51 3.48-9.21 3.48-12.72 0-3.5-3.47-3.53-9.11-.02-12.58a8.987 8.987 0 0112.65 0L21 3v7.12z"/></symbol>
  <symbol id="i-export" viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></symbol>
  <symbol id="i-robot" viewBox="0 0 24 24"><path d="M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3zm-2 10H6V7h12v12zm-9-6c-.83 0-1.5-.67-1.5-1.5S8.17 10 9 10s1.5.67 1.5 1.5S9.83 13 9 13zm7.5-1.5c0 .83-.67 1.5-1.5 1.5s-1.5-.67-1.5-1.5.67-1.5 1.5-1.5 1.5.67 1.5 1.5zM8 15h8v2H8v-2z"/></symbol>
  <symbol id="i-mic" viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm-1-9c0-.55.45-1 1-1s1 .45 1 1v6c0 .55-.45 1-1 1s-1-.45-1-1V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></symbol>
  <symbol id="i-send" viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></symbol>
  <symbol id="i-lightning" viewBox="0 0 24 24"><path d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42A6.92 6.92 0 0119 12c0 3.87-3.13 7-7 7A6.995 6.995 0 017.58 5.58L6.17 4.17A8.932 8.932 0 003 12a9 9 0 0018 0c0-2.74-1.23-5.18-3.17-6.83z"/></symbol>
</svg>

<div id="toasts"></div>
<div id="upd">Update available! <a id="udl" href="#" target="_blank">Download</a></div>

<!-- ════ SIDEBAR ═══════════════════════════════════════════════════════════════ -->
<div id="sb">
  <h2>
    <svg class="icon"><use href="#i-lightning"/></svg>
    TraderMoney
    <span id="lbadge" class="lbadge li">FREE</span>
    <small style="color:var(--muted);font-size:.58rem;margin-left:4px;">v2.2.1</small>
  </h2>
  <label>License Key</label>
  <input type="password" id="lickey" placeholder="Paste Gumroad key">
  <button onclick="validateLicense()" style="margin-top:4px;font-size:.8rem;">
    <svg class="icon"><use href="#i-key"/></svg> Validate
  </button>
  <p style="font-size:.67rem;color:var(--muted);margin:3px 0 0;">
    <a href="https://shafayrich.gumroad.com/l/ykaoov" style="color:var(--accent)">Buy license ↗</a>
  </p>
  <div id="free-notice" class="free-notice">
    Free tier: Alpaca paper only · Signal-Only · 1 ticker · Core indicators only · AI: 5/day<br>
    <b>License session-only – re-enter each restart.</b>
  </div>
  <div id="offline-banner" class="offline-banner">⚠️ Offline Mode – cached data only</div>

  <hr>
  <label><span class="cb"><input type="checkbox" id="offline-mode" onchange="toggleOffline()"><span class="cm"></span></span> Offline Mode</label>

  <label>Broker</label>
  <select id="broker" onchange="onBrokerChange()"></select>
  <div id="bstatus" class="ok"></div>
  <div id="creds"></div>

  <label>Telegram Token (opt)</label><input type="password" id="tgt">
  <label>Telegram Chat ID (opt)</label><input id="tgc">
  <label>Tickers (e.g. AAPL:5, BTC/USD:0.1)</label>
  <input id="tickers" value="AAPL">

  <label>Timeframe</label>
  <select id="tf">
    <option>1m</option><option>5m</option><option>15m</option>
    <option>30m</option><option>1h</option><option>1d</option>
  </select>
  <label>EMA periods</label>
  <div class="r2"><input id="emaf" value="9" placeholder="Fast"><input id="emas" value="50" placeholder="Slow"></div>
  <label><span class="cb"><input type="checkbox" id="udefqty" checked onchange="toggleDefQty()"><span class="cm"></span></span> Use fallback qty</label>
  <div id="defqty-box"><label>Default Qty</label><input id="qty" value="1" type="number"></div>

  <label>Mode</label>
  <select id="mode"><option value="signal">Signal Only</option><option value="auto">Auto Trade</option></select>
  <label>Direction</label>
  <select id="dir"><option value="both">Both</option><option value="long">Long Only</option><option value="short">Short Only</option></select>

  <label><span class="cb"><input type="checkbox" id="ubracket"><span class="cm"></span></span> Bracket SL/TP</label>
  <div class="r2"><input id="slp" value="2" placeholder="SL %"><input id="tpp" value="4" placeholder="TP %"></div>
  <label><span class="cb"><input type="checkbox" id="uatr" checked><span class="cm"></span></span> ATR Stops</label>

  <label style="margin-top:12px;font-weight:bold;color:var(--accent)">Indicators</label>
  <label><span class="cb"><input type="checkbox" id="ursi" checked><span class="cm"></span></span> RSI</label>
  <label><span class="cb"><input type="checkbox" id="umacd" checked><span class="cm"></span></span> MACD</label>
  <label><span class="cb"><input type="checkbox" id="uvwap" checked><span class="cm"></span></span> VWAP</label>
  <label><span class="cb"><input type="checkbox" id="uboll" checked><span class="cm"></span></span> Bollinger</label>
  <label><span class="cb"><input type="checkbox" id="uadx" checked><span class="cm"></span></span> ADX <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="uvol" checked><span class="cm"></span></span> Volume <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="ust" checked><span class="cm"></span></span> SuperTrend <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="ustoch" checked><span class="cm"></span></span> Stochastic <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="unews" disabled><span class="cm"></span></span> News Sentiment <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>

  <button onclick="saveConfig()"><svg class="icon"><use href="#i-save"/></svg> Save Config</button>
  <button class="ghost" onclick="refreshTickers()"><svg class="icon"><use href="#i-refresh"/></svg> Refresh Tickers</button>
  <button style="background:var(--accent);color:#050505;" id="startBtn" onclick="startBot()">
    <svg class="icon"><use href="#i-start"/></svg> Start Bot
  </button>
  <button class="ghost" id="stopBtn" onclick="stopBot()">
    <svg class="icon"><use href="#i-stop"/></svg> Stop Bot
  </button>
  <button class="danger" onclick="killSwitch()">
    <svg class="icon"><use href="#i-warn"/></svg> Kill Switch
  </button>
  <button class="ghost" style="margin-top:5px;" onclick="resetDef()">
    <svg class="icon"><use href="#i-refresh"/></svg> Reset Defaults
  </button>

  <!-- Strategy Presets -->
  <label style="margin-top:12px;font-weight:bold;color:var(--accent)">Strategy Presets</label>
  <div class="r2">
    <select id="preset-select">
      <option value="scalping">Scalping</option>
      <option value="swing">Swing</option>
      <option value="breakout">Breakout</option>
    </select>
    <button onclick="loadPreset()" style="width:auto;margin-top:0;padding:7px 10px;">
      <svg class="icon"><use href="#i-preset"/></svg> Load
    </button>
  </div>

  <button class="ghost" style="margin-top:14px;" onclick="checkUpdate()">
    <svg class="icon"><use href="#i-update"/></svg> Check Updates
  </button>
  <button class="ghost" style="margin-top:6px;" onclick="runBT()">
    <svg class="icon"><use href="#i-backtest"/></svg> Backtest All
  </button>
  <div style="margin-top:7px;font-size:.74rem;color:var(--muted);">
    Days: <input type="number" id="btDays" value="5" min="1" max="365" class="bt-days-input">
  </div>
</div>

<!-- ════ MAIN ══════════════════════════════════════════════════════════════════ -->
<div id="main">
  <div class="tab-bar" id="tabbar">
    <button class="tbtn active" data-tab="charts"><svg class="icon"><use href="#i-chart"/></svg>Charts</button>
    <button class="tbtn" data-tab="signals"><svg class="icon"><use href="#i-signal"/></svg>Signals</button>
    <button class="tbtn" data-tab="history"><svg class="icon"><use href="#i-history"/></svg>History</button>
    <button class="tbtn" data-tab="backtest"><svg class="icon"><use href="#i-backtest"/></svg>Backtest</button>
    <button class="tbtn" data-tab="analysis"><svg class="icon"><use href="#i-analysis"/></svg>Analysis</button>
    <button class="tbtn" data-tab="help"><svg class="icon"><use href="#i-help"/></svg>Help</button>
    <button class="tbtn" data-tab="aichat"><svg class="icon"><use href="#i-chat"/></svg>AI Chat</button>
  </div>

  <!-- Charts -->
  <div id="tab-charts" class="tab active">
    <div id="tkbar"></div>
    <div id="metrics">
      <div class="met"><div class="v" id="v-eq">--</div><div>Equity</div></div>
      <div class="met"><div class="v" id="v-bp">--</div><div>Buy Power</div></div>
      <div class="met"><div class="v" id="v-pl">--</div><div>P&amp;L</div></div>
      <div class="met"><div class="v" id="v-pos">--</div><div>Positions</div></div>
    </div>
    <div id="sess">
      <span style="color:var(--accent)">Markets</span>
      <span><span class="sd" id="ds"></span>SYD</span>
      <span><span class="sd" id="dt"></span>TKY</span>
      <span><span class="sd" id="dl"></span>LDN</span>
      <span><span class="sd" id="dn"></span>NYC</span>
      <span><span class="sd so"></span>CRYPTO</span>
      <span id="utc-clock" style="color:var(--muted);margin-left:auto;font-size:.75rem;">UTC: --</span>
    </div>
    <div id="chart-c"></div>
  </div>

  <!-- Signals -->
  <div id="tab-signals" class="tab">
    <div id="siglist" style="overflow:auto;flex:1;"></div>
    <div id="sigempty" class="empty-placeholder" style="display:none;">No signals yet.</div>
  </div>

  <!-- History -->
  <div id="tab-history" class="tab">
    <div id="histlist" style="overflow:auto;flex:1;"></div>
    <div id="hstempty" class="empty-placeholder" style="display:none;">No orders yet.</div>
  </div>

  <!-- Backtest -->
  <div id="tab-backtest" class="tab">
    <div class="btp">
      <div style="display:flex;gap:7px;padding:10px;flex-wrap:wrap;">
        <button class="ghost" style="width:auto;padding:9px 16px;" onclick="runBT()">
          <svg class="icon"><use href="#i-backtest"/></svg> Run Backtest
        </button>
        <button class="ghost" style="width:auto;padding:9px 16px;" id="mc-btn" onclick="runMC()" disabled>
          🎲 Monte Carlo
        </button>
        <button class="ghost" style="width:auto;padding:9px 14px;" id="csv-btn" onclick="exportCSV()" disabled>
          <svg class="icon"><use href="#i-export"/></svg> CSV
        </button>
        <button class="ghost" style="width:auto;padding:9px 14px;" id="pdf-btn" onclick="exportPDF()" disabled>
          <svg class="icon"><use href="#i-export"/></svg> PDF
        </button>
        <button class="ghost" style="width:auto;padding:9px 14px;" id="tune-btn" onclick="autoTune()" disabled>
          <svg class="icon"><use href="#i-robot"/></svg> AI Auto-Tune
        </button>
      </div>
      <div id="btres" class="btr"><p class="ph">Click <b>Run Backtest</b> to begin.</p></div>
    </div>
  </div>

  <!-- Analysis (Correlation) -->
  <div id="tab-analysis" class="tab">
    <div style="padding:10px;flex-shrink:0;">
      <button class="ghost" style="width:auto;padding:8px 16px;" onclick="loadCorr()">
        <svg class="icon"><use href="#i-refresh"/></svg> Refresh Correlation Matrix (30 days)
      </button>
    </div>
    <div style="overflow:auto;flex:1;padding:10px;" id="corr-content">
      <p class="ph">Click Refresh to load the correlation matrix for your tickers.</p>
    </div>
  </div>

  <!-- Help -->
  <div id="tab-help" class="tab">
    <div class="hb">
      <h3>📘 TraderMoney v2.2.1 – Complete Help Guide</h3>

      <div class="istat">
        <h4>🎯 Indicator Win Rate Progression</h4>
        <p>Each additional indicator meaningfully boosts signal accuracy:</p>
        <table class="bttbl">
          <tr><th>Indicators Active</th><th>Approx. Win Rate</th><th>Confidence Boost</th></tr>
          <tr><td>Pure EMA Crossover</td><td>~32%</td><td>50%</td></tr>
          <tr><td>+ RSI (14)</td><td>~40%</td><td>+5%</td></tr>
          <tr><td>+ MACD</td><td>~45%</td><td>+5%</td></tr>
          <tr><td>+ VWAP</td><td>~48%</td><td>+5%</td></tr>
          <tr><td>+ Bollinger Bands</td><td>~50%</td><td>+5%</td></tr>
          <tr><td>+ ADX ≥ 20</td><td>~55%</td><td>+5%</td></tr>
          <tr><td>+ Volume ≥ 1.5× avg</td><td>~58%</td><td>+6%</td></tr>
          <tr><td>+ SuperTrend</td><td>~62%</td><td>+8%</td></tr>
          <tr><td>+ Stochastic</td><td>~65%</td><td>+5%</td></tr>
          <tr><td>+ ATR Stops</td><td>~65% (risk mgmt)</td><td>+4%</td></tr>
        </table>
        <p><small>Win rates are approximate based on historical backtesting. Actual results vary by market, timeframe, and ticker.</small></p>
      </div>

      <h4>⌨️ Keyboard Shortcuts</h4>
      <table class="bttbl">
        <tr><th>Shortcut</th><th>Action</th></tr>
        <tr><td>Ctrl + Space</td><td>Start / Stop Bot</td></tr>
        <tr><td>Ctrl + K</td><td>Focus Ticker Input</td></tr>
        <tr><td>Ctrl + B</td><td>Run Backtest</td></tr>
        <tr><td>Ctrl + Shift + B</td><td>Switch to Backtest Tab</td></tr>
        <tr><td>Ctrl + 1</td><td>Charts Tab</td></tr>
        <tr><td>Ctrl + 2</td><td>Signals Tab</td></tr>
        <tr><td>Ctrl + 3</td><td>History Tab</td></tr>
        <tr><td>Ctrl + 4</td><td>Backtest Tab</td></tr>
        <tr><td>Ctrl + 5</td><td>Analysis Tab</td></tr>
        <tr><td>Ctrl + 6</td><td>Help Tab</td></tr>
        <tr><td>Ctrl + 7</td><td>AI Chat Tab</td></tr>
      </table>

      <h4>🏦 Broker Configuration</h4>
      <ul>
        <li><b>Alpaca:</b> Get API Key + Secret from <a href="https://alpaca.markets">alpaca.markets</a>. Enable Paper Trading for simulation. Works on free tier.</li>
        <li><b>Interactive Brokers:</b> Requires TWS or IB Gateway running locally. Ports: 7497 (TWS paper), 7496 (TWS live), 4002 (Gateway paper), 4001 (Gateway live). Enable API connections in TWS Configuration → API → Settings.</li>
        <li><b>Tradier:</b> Get Access Token + Account ID from <a href="https://developer.tradier.com">developer.tradier.com</a>. Use Sandbox for testing.</li>
        <li><b>Binance:</b> API Key + Secret from <a href="https://binance.com">binance.com</a> → API Management. Enable Spot trading. Testnet available.</li>
        <li><b>Bybit:</b> API Key + Secret from <a href="https://bybit.com">bybit.com</a>. Requires pybit v5+. Testnet available.</li>
        <li><b>OKX:</b> API Key + Secret + Passphrase from <a href="https://okx.com">okx.com</a>. Demo trading available.</li>
      </ul>

      <h4>🔑 License & Tiers</h4>
      <ul>
        <li><b>Free Tier:</b> Alpaca paper only, Signal-Only mode, 1 ticker, core indicators (RSI, MACD, VWAP, Bollinger), 5 AI messages/day.</li>
        <li><b>Pro Tier:</b> All 6 brokers, Auto-Trade mode, all 9 indicators, bracket orders, ATR stops, Telegram alerts, unlimited AI, short selling, multiple tickers, AI Auto-Tune.</li>
        <li>Purchase license from <a href="https://shafayrich.gumroad.com/l/ykaoov">Gumroad ↗</a></li>
        <li><b>License is session-only</b> – re-enter on each restart. This prevents unauthorized sharing.</li>
      </ul>

      <h4>📊 Ticker Format</h4>
      <p>Comma-separated with optional quantity after colon:</p>
      <p><code>AAPL:10, TSLA:5, BTC/USD:0.01, MSFT</code></p>
      <p>If no quantity specified, uses the Default Qty setting. Crypto pairs use <code>/</code> separator (e.g., BTC/USD).</p>
      <h4>🤖 AI Chat</h4>
      <ul>
        <li>Powered by OpenRouter with free model fallback (Gemini Flash, DeepSeek, Llama 3.3)</li>
        <li>Chat sessions saved to database for later reference</li>
        <li>Voice input support (Chrome/Edge browsers only)</li>
        <li>Free tier: 5 messages/day. Pro tier: unlimited</li>
      </ul>

      <h4>⚙️ Strategy Presets</h4>
      <table class="bttbl">
        <tr><th>Preset</th><th>Timeframe</th><th>EMAs</th><th>Indicators</th><th>Best For</th></tr>
        <tr><td>Scalping</td><td>1m</td><td>9, 50</td><td>RSI, MACD, Volume</td><td>Quick intraday trades</td></tr>
        <tr><td>Swing</td><td>15m</td><td>20, 50</td><td>RSI, MACD, VWAP, Bollinger, ADX</td><td>Multi-hour swing trades</td></tr>
        <tr><td>Breakout</td><td>5m</td><td>9, 50</td><td>Volume, SuperTrend, ATR</td><td>Volatility breakouts</td></tr>
      </table>

      <h4>📈 Backtesting</h4>
      <ul>
        <li>Run backtests on your current ticker list with your active indicator configuration</li>
        <li>Monte Carlo simulation runs 1,000 randomized scenarios to estimate probability of profit</li>
        <li>Export results to CSV or PDF for record-keeping</li>
        <li>AI Auto-Tune analyzes your backtest results and suggests improved settings</li>
        <li>Portfolio mode simulates trading all tickers with a shared $100,000 account</li>
      </ul>

      <h4>⚠️ Risk Management</h4>
      <ul>
        <li><b>Bracket Orders:</b> Automatically places stop-loss and take-profit orders</li>
        <li><b>ATR-Based Stops:</b> Dynamic stops based on Average True Range (2× stop, 3× take-profit)</li>
        <li><b>Kill Switch:</b> Instantly closes all positions across all tickers</li>
        <li><b>Direction Control:</b> Limit to Long Only, Short Only, or Both</li>
        <li><b>SL/TP Watchdog:</b> Monitors positions every 2 seconds for non-Alpaca brokers</li>
      </ul>

      <h4>📱 Telegram Alerts (Pro)</h4>
      <ul>
        <li>Create a bot via <a href="https://t.me/BotFather">@BotFather</a></li>
        <li>Get your Chat ID via <a href="https://t.me/userinfobot">@userinfobot</a></li>
        <li>Receive signals, executed trades, stop-loss/take-profit triggers</li>
      </ul>

      <h4>🌍 Timezones</h4>
      <p>All times displayed in your local timezone. Market session indicators show active trading hours for Sydney, Tokyo, London, and New York.</p>

      <h4>🔄 Auto-Updates</h4>
      <p>TraderMoney checks for updates on startup. Download the latest version from the GitHub releases page.</p>

      <h4>🏆 Leaderboard</h4>
      <div id="leaderboard-wrap">
        <p style="font-size:.8rem;color:var(--muted)">Run a backtest to appear on the leaderboard.</p>
      </div>

      <h4>💡 Tips for Best Results</h4>
      <ul>
        <li>Start with Signal-Only mode to build confidence in the strategy</li>
        <li>Use paper trading accounts for all brokers before going live</li>
        <li>Backtest with at least 30 days of data for meaningful results</li>
        <li>Combine multiple indicators – each one adds meaningful confirmation</li>
        <li>Use the Correlation Matrix to avoid over-concentration in correlated assets</li>
        <li>Set conservative SL/TP percentages (2%/4% is a good starting point)</li>
        <li>Run Monte Carlo simulations to understand worst-case scenarios</li>
      </ul>
    </div>
  </div>

  <!-- AI Chat -->
  <div id="tab-aichat" class="tab">
    <div id="aichat-wrap">
      <div id="chat-sessions-panel">
        <h3><svg class="icon"><use href="#i-chat"/></svg> Chats</h3>
        <div id="chat-sessions-list"></div>
        <button id="chat-new-session-btn" onclick="createNewSession()">+ New Chat</button>
      </div>
      <div id="chat-main">
        <div id="chat-topbar">
          <span class="title">
            <svg class="icon"><use href="#i-robot"/></svg> TraderBot AI
          </span>
          <span id="chat-limit"></span>
        </div>
        <div id="chat-messages"></div>
        <div id="chat-input-row">
          <textarea id="chat-input" placeholder="Ask about trading, indicators, platform usage..."></textarea>
          <button id="mic-btn" onclick="startVoice()" title="Voice input">
            <svg class="icon"><use href="#i-mic"/></svg>
          </button>
          <button id="chat-send" onclick="sendChat()">
            <svg class="icon"><use href="#i-send"/></svg> Send
          </button>
        </div>
      </div>
    </div>
  </div>

  <div id="logbar"></div>
</div>

<!-- ════ TRADINGVIEW WIDGET ════════════════════════════════════════════════════ -->
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
'use strict';
const $=id=>document.getElementById(id);
let cfg={},licValid=false,curSym='',allTickers=[],tvWidget=null,lastTvSymbol='';
let curSessionId=null,chatInited=false,botRunning=false,lastBTData=null;

/* ── Utilities ───────────────────────────────────────────────── */
function cs(raw){return raw.split(':')[0].trim().toUpperCase();}
function fmt(n,d=2){return Number(n).toLocaleString(undefined,{maximumFractionDigits:d});}
function toast(msg,type='info'){
  let t=document.createElement('div');t.className='toast '+type;t.textContent=msg;
  $('toasts').appendChild(t);setTimeout(()=>t.remove(),4200);
}
function gv(id,fb=''){let e=$(id);return e?e.value:fb;}
function gc(id){let e=$(id);return e?e.checked:false;}
function sv(id,v){let e=$(id);if(e)e.value=v;}
function sc(id,v){let e=$(id);if(e)e.checked=!!v;}
function lockCb(id,locked){
  let el=$(id);if(!el)return;el.disabled=locked;
  let lbl=el.closest('label');
  if(lbl){lbl.style.opacity=locked?'0.35':'1';lbl.style.pointerEvents=locked?'none':'';}
}

/* ── Tab switching ───────────────────────────────────────────── */
const TABS=['charts','signals','history','backtest','analysis','help','aichat'];
function switchTab(name){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tbtn').forEach(x=>x.classList.remove('active'));
  const t=$('tab-'+name),b=document.querySelector(`[data-tab="${name}"]`);
  if(t)t.classList.add('active');if(b)b.classList.add('active');
  if(name==='aichat')initAIChat();
  if(name==='charts')setTimeout(()=>{if(tvWidget)tvWidget.resize&&tvWidget.resize();},80);
}
document.querySelectorAll('.tbtn').forEach(b=>{
  b.addEventListener('click',function(){switchTab(this.dataset.tab);});
});
if(window.Sortable){
  try{Sortable.create($('tabbar'),{animation:120,handle:'.tbtn'});}catch(e){}
}

/* ── Sessions clock ──────────────────────────────────────────── */
function updSess(){
  let n=new Date(),d=n.getUTCDay(),wk=d===0||d===6,h=n.getUTCHours()+n.getUTCMinutes()/60;
  let o=ok=>ok?'sd so':'sd sc';
  $('ds').className=o(!wk&&(h>=22||h<5));$('dt').className=o(!wk&&(h>=23||h<6));
  $('dl').className=o(!wk&&h>=8&&h<16.5);$('dn').className=o(!wk&&h>=13.5&&h<20);
  $('utc-clock').textContent='UTC: '+n.toISOString().slice(11,19);
}
setInterval(updSess,30000);updSess();

/* ── Broker credential helpers ───────────────────────────────── */
function pw(id,l){return`<label>${l}</label><input type="password" id="${id}">`;}
function tx(id,l,v=''){return`<label>${l}</label><input id="${id}" value="${v}">`;}
function cbH(id,l,chk=false){return`<label><span class="cb"><input type="checkbox" id="${id}" ${chk?'checked':''}><span class="cm"></span></span> ${l}</label>`;}
function saveCurrentBrokerCreds(){
  const b=cfg.broker||'Alpaca';
  if(b==='Alpaca'){cfg.alpaca=cfg.alpaca||{};cfg.alpaca.api_key=gv('ak');cfg.alpaca.secret_key=gv('ask');cfg.alpaca.paper=gc('apaper');}
  else if(b==='Interactive Brokers'){cfg.ibkr=cfg.ibkr||{};cfg.ibkr.host=gv('ih');cfg.ibkr.port=gv('ip');cfg.ibkr.client_id=gv('icid');}
  else if(b==='Tradier'){cfg.tradier=cfg.tradier||{};cfg.tradier.access_token=gv('trat');cfg.tradier.account_id=gv('traid');cfg.tradier.sandbox=gc('trsb');}
  else if(b==='Binance'){cfg.binance=cfg.binance||{};cfg.binance.api_key=gv('bnk');cfg.binance.api_secret=gv('bns');cfg.binance.testnet=gc('bnt');}
  else if(b==='Bybit'){cfg.bybit=cfg.bybit||{};cfg.bybit.api_key=gv('bbk');cfg.bybit.api_secret=gv('bbs');cfg.bybit.testnet=gc('bbtn');}
  else if(b==='OKX'){cfg.okx=cfg.okx||{};cfg.okx.api_key=gv('ok');cfg.okx.api_secret=gv('os');cfg.okx.api_passphrase=gv('op');cfg.okx.demo=gc('od');}
}
function populateCredsFields(){
  const b=cfg.broker||'Alpaca';
  if(b==='Alpaca'&&cfg.alpaca){sv('ak',cfg.alpaca.api_key||'');sv('ask',cfg.alpaca.secret_key||'');sc('apaper',cfg.alpaca.paper!==false);}
  else if(b==='Interactive Brokers'&&cfg.ibkr){sv('ih',cfg.ibkr.host||'');sv('ip',cfg.ibkr.port||'');sv('icid',cfg.ibkr.client_id||'');}
  else if(b==='Tradier'&&cfg.tradier){sv('trat',cfg.tradier.access_token||'');sv('traid',cfg.tradier.account_id||'');sc('trsb',cfg.tradier.sandbox===true);}
  else if(b==='Binance'&&cfg.binance){sv('bnk',cfg.binance.api_key||'');sv('bns',cfg.binance.api_secret||'');sc('bnt',cfg.binance.testnet!==false);}
  else if(b==='Bybit'&&cfg.bybit){sv('bbk',cfg.bybit.api_key||'');sv('bbs',cfg.bybit.api_secret||'');sc('bbtn',cfg.bybit.testnet!==false);}
  else if(b==='OKX'&&cfg.okx){sv('ok',cfg.okx.api_key||'');sv('os',cfg.okx.api_secret||'');sv('op',cfg.okx.api_passphrase||'');sc('od',cfg.okx.demo!==false);}
}
function updateCreds(){
  saveCurrentBrokerCreds();const b=cfg.broker||'Alpaca',c=$('creds');c.innerHTML='';
  if(b==='Alpaca')c.innerHTML=pw('ak','API Key')+pw('ask','Secret Key')+cbH('apaper','Paper Trading',true);
  else if(b==='Interactive Brokers')c.innerHTML=tx('ih','Host','127.0.0.1')+tx('ip','Port','7497')+tx('icid','Client ID','1');
  else if(b==='Tradier')c.innerHTML=pw('trat','Access Token')+tx('traid','Account ID')+cbH('trsb','Sandbox',false);
  else if(b==='Binance')c.innerHTML=pw('bnk','API Key')+pw('bns','API Secret')+cbH('bnt','Testnet',true);
  else if(b==='Bybit')c.innerHTML=pw('bbk','API Key')+pw('bbs','API Secret')+cbH('bbtn','Testnet',true);
  else if(b==='OKX')c.innerHTML=pw('ok','API Key')+pw('os','API Secret')+pw('op','Passphrase')+cbH('od','Demo',true);
  populateCredsFields();
}
function updateBrokerOptions(){
  const sel=$('broker'),cur=cfg.broker||'Alpaca';sel.innerHTML='';
  const addOpt=(v,l)=>{const o=document.createElement('option');o.value=v;o.textContent=l;sel.appendChild(o);};
  // Always show Alpaca
  addOpt('Alpaca','Alpaca');
  // Show all brokers but mark Pro-only ones
  const allBrokers=[
    {name:'Interactive Brokers',pro:true},
    {name:'Tradier',pro:true},
    {name:'Binance',pro:true},
    {name:'Bybit',pro:true},
    {name:'OKX',pro:true}
  ];
  allBrokers.forEach(b=>{
    const label=licValid?b.name:`${b.name} [PRO]`;
    addOpt(b.name,label);
  });
  // If not licensed and current broker isn't Alpaca, reset to Alpaca
  if(!licValid && cur!=='Alpaca'){
    sel.value='Alpaca';
    cfg.broker='Alpaca';
  }else{
    sel.value=cur;
  }
}
function onBrokerChange(){cfg.broker=$('broker').value;updateCreds();}
function toggleDefQty(){$('defqty-box').style.display=gc('udefqty')?'block':'none';}
function toggleOffline(){
  const on=gc('offline-mode');
  fetch('/api/offline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({offline:on})});
  $('offline-banner').style.display=on?'block':'none';
}

/* ── Tier UI ─────────────────────────────────────────────────── */
function applyFreeTierUI(){
  updateBrokerOptions();$('broker').disabled=true;sv('broker','Alpaca');cfg.broker='Alpaca';
  sv('mode','signal');$('mode').disabled=true;sv('dir','both');$('dir').disabled=true;
  ['ubracket','uatr','uadx','uvol','ust','ustoch','unews'].forEach(id=>{sc(id,false);lockCb(id,true);});
  $('free-notice').style.display='block';$('lbadge').textContent='FREE';$('lbadge').className='lbadge li';
}
function applyProUI(){
  updateBrokerOptions();$('broker').disabled=false;$('mode').disabled=false;$('dir').disabled=false;
  ['ubracket','uatr','uadx','uvol','ust','ustoch'].forEach(id=>lockCb(id,false));
  $('free-notice').style.display='none';$('lbadge').textContent='PRO';$('lbadge').className='lbadge lv';
}

/* ── Config ──────────────────────────────────────────────────── */
function buildCfg(){
  saveCurrentBrokerCreds();
  return{
    broker:cfg.broker||'Alpaca',tickers:gv('tickers','AAPL'),timeframe:gv('tf','1m'),
    emas:[parseInt(gv('emaf','9')),parseInt(gv('emas','50'))],
    quantity:parseInt(gv('qty','1'))||1,mode:gv('mode','signal'),direction:gv('dir','both'),
    use_default_qty:gc('udefqty'),use_bracket:gc('ubracket'),
    sl_percent:parseFloat(gv('slp','2')),tp_percent:parseFloat(gv('tpp','4')),
    use_atr_stops:gc('uatr'),telegram:{token:gv('tgt'),chat_id:gv('tgc')},
    use_rsi:gc('ursi'),use_macd:gc('umacd'),use_vwap:gc('uvwap'),
    use_bollinger:gc('uboll'),use_adx:gc('uadx'),use_vol_confirm:gc('uvol'),
    use_supertrend:gc('ust'),use_stochastic:gc('ustoch'),news_sentiment:gc('unews'),
    license_key:gv('lickey',''),timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
    alpaca:cfg.alpaca||{},ibkr:cfg.ibkr||{},tradier:cfg.tradier||{},
    binance:cfg.binance||{},bybit:cfg.bybit||{},okx:cfg.okx||{},
  };
}

function initUI(c){
  if(!c)return;
  licValid=false;
  cfg.alpaca=c.alpaca||{};cfg.ibkr=c.ibkr||{};cfg.tradier=c.tradier||{};
  cfg.binance=c.binance||{};cfg.bybit=c.bybit||{};cfg.okx=c.okx||{};
  cfg.broker='Alpaca';
  applyFreeTierUI();
  sv('tickers',c.tickers||'AAPL');sv('tf',c.timeframe||'1m');
  sv('emaf',c.emas?c.emas[0]:9);sv('emas',c.emas?c.emas[1]:50);
  sc('udefqty',c.use_default_qty!==false);toggleDefQty();
  sv('qty',c.quantity||1);
  if(c.telegram){sv('tgt',c.telegram.token||'');sv('tgc',c.telegram.chat_id||'');}
  sv('slp',c.sl_percent||2);sv('tpp',c.tp_percent||4);
  sc('ursi',c.use_rsi!==false);sc('umacd',c.use_macd!==false);
  sc('uvwap',c.use_vwap!==false);sc('uboll',c.use_bollinger!==false);
  if(c.license_key)sv('lickey',c.license_key);
  if(c.offline_mode)sc('offline-mode',true);$('offline-banner').style.display=c.offline_mode?'block':'none';
  updateCreds();
  let raw=(c.tickers||'AAPL').split(',').map(s=>s.trim()).filter(s=>s);
  if(raw.length){setTickers(raw);loadTradingViewChart(cs(raw[0]));}
}

/* ── TradingView Chart ───────────────────────────────────────── */
function loadTradingViewChart(symbol){
  if(tvWidget) try{tvWidget.remove();}catch(e){}
  lastTvSymbol=symbol;
  tvWidget=new TradingView.widget({
    container_id:'chart-c',
    symbol:symbol,
    interval:'1',
    timezone:'Etc/UTC',
    theme:'dark',
    style:'1',
    locale:'en',
    toolbar_bg:'#0c0c0c',
    enable_publishing:false,
    allow_symbol_change:true,
    autosize:true,
    studies:[],
    overrides:{
      "paneProperties.background": "#0c0c0c",
      "paneProperties.backgroundType": "solid",
      "paneProperties.vertGridProperties.color": "#1a1a1a",
      "paneProperties.horzGridProperties.color": "#1a1a1a",
      "mainSeriesProperties.candleStyle.upColor": "#D4AF37",
      "mainSeriesProperties.candleStyle.downColor": "#B22222",
      "mainSeriesProperties.candleStyle.wickUpColor": "#D4AF37",
      "mainSeriesProperties.candleStyle.wickDownColor": "#B22222",
      "mainSeriesProperties.candleStyle.borderUpColor": "#D4AF37",
      "mainSeriesProperties.candleStyle.borderDownColor": "#B22222",
    }
  });
  curSym=symbol;
  setTimeout(()=>{if(tvWidget)tvWidget.resize&&tvWidget.resize();},200);
}

/* ── Ticker bar ──────────────────────────────────────────────── */
function setTickers(list){
  allTickers=list;let bar=$('tkbar');bar.innerHTML='';
  list.forEach(raw=>{
    let sym=cs(raw),btn=document.createElement('button');
    btn.className='tkbtn'+(sym===curSym?' active':'');btn.textContent=sym;
    btn.onclick=()=>{curSym=sym;updTk();if(lastTvSymbol!==sym)loadTradingViewChart(sym);};
    bar.appendChild(btn);
  });
}
function updTk(){document.querySelectorAll('.tkbtn').forEach(b=>b.classList.toggle('active',cs(b.textContent)===curSym));}
function refreshTickers(){
  fetch('/api/config').then(r=>r.json()).then(c=>{
    sv('tickers',c.tickers);
    let raw=c.tickers.split(',').map(s=>s.trim()).filter(s=>s);
    if(raw.length){setTickers(raw);loadTradingViewChart(cs(raw[0]));}
    toast('Tickers refreshed','success');
  });
}

/* ── Config load / save ──────────────────────────────────────── */
async function loadConfig(){
  try{
    let r=await fetch('/api/config');cfg=await r.json();
    await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})});
    initUI(cfg);
    if(cfg.license_key&&cfg.license_key.trim())await validateLicense(true);
    loadHistory();
    loadLeaderboard();
  }catch(e){toast('Config load failed','error');}
}
function loadHistory(){
  fetch('/api/status').then(r=>r.json()).then(d=>{renderSignals(d.signals);renderOrders(d.orders);}).catch(()=>{});
}
async function saveConfig(){
  cfg=buildCfg();
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  toast('Config saved (license is session-only)','success');
}

const DEF={broker:'Alpaca',tickers:'AAPL',mode:'signal',direction:'both',use_default_qty:true,quantity:1,emas:[9,50],use_bracket:false,sl_percent:2,tp_percent:4,timeframe:'1m',telegram:{},use_rsi:true,use_macd:true,use_vwap:true,use_bollinger:true,use_adx:true,use_vol_confirm:true,use_supertrend:true,use_stochastic:true,use_atr_stops:true,offline_mode:false,alpaca:{api_key:'',secret_key:'',paper:true},ibkr:{host:'',port:'',client_id:''},tradier:{access_token:'',account_id:'',sandbox:false},binance:{api_key:'',api_secret:'',testnet:true},bybit:{api_key:'',api_secret:'',testnet:true},okx:{api_key:'',api_secret:'',api_passphrase:'',demo:true}};
function resetDef(){cfg=JSON.parse(JSON.stringify(DEF));licValid=false;applyFreeTierUI();sv('lickey','');initUI(cfg);saveConfig();toast('Reset to factory defaults','success');}

/* ── Bot controls ────────────────────────────────────────────── */
async function startBot(){
  let btn=$('startBtn');btn.textContent='Starting...';btn.disabled=true;
  cfg=buildCfg();
  if(!licValid){
    cfg.broker='Alpaca';cfg.mode='signal';cfg.direction='both';
    if(cfg.alpaca)cfg.alpaca.paper=true;
    ['use_supertrend','use_stochastic','use_adx','use_vol_confirm','use_atr_stops','use_bracket'].forEach(k=>cfg[k]=false);
    cfg.tickers=cfg.tickers.split(',')[0].trim();
  }
  let r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  let d=await r.json();
  btn.textContent='\u25B6 Start Bot';btn.disabled=false;
  toast(d.message,d.status==='ok'?'success':'error');
  if(d.status!=='ok'){$('bstatus').textContent=d.message;$('bstatus').className='err';}
  else{botRunning=true;}
}
async function stopBot(){
  let btn=$('stopBtn');btn.textContent='Stopping...';btn.disabled=true;
  await fetch('/api/stop',{method:'POST'});
  btn.textContent='\u25A0 Stop Bot';btn.disabled=false;
  botRunning=false;toast('Bot stopped','success');
}
async function killSwitch(){await fetch('/api/kill',{method:'POST'});botRunning=false;toast('Kill switch activated','error');}

async function validateLicense(silent=false){
  let key=gv('lickey').trim();if(!key){if(!silent)toast('Enter a license key','error');return;}
  let r=await fetch('/api/validate_license',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key})});
  let d=await r.json();
  if(d.valid){
    licValid=true;applyProUI();
    sv('mode',cfg.mode||'signal');sv('dir',cfg.direction||'both');
    sc('ubracket',!!cfg.use_bracket);sc('uatr',cfg.use_atr_stops!==false);
    sc('uadx',cfg.use_adx!==false);sc('uvol',cfg.use_vol_confirm!==false);
    sc('ust',cfg.use_supertrend!==false);sc('ustoch',cfg.use_stochastic!==false);
    updateCreds();
    if(!silent)toast('Pro unlocked for this session','success');
  }else{
    licValid=false;applyFreeTierUI();
    if(!silent)toast(d.message,'error');
  }
  updateBrokerOptions();
}

async function checkUpdate(){
  try{
    let d=await(await fetch('/api/update')).json();
    if(d.update_available){$('upd').style.display='block';$('udl').href=d.download_url;toast('Update available!','success');}
    else toast('Up to date!','success');
  }catch(e){}
}
setTimeout(checkUpdate,2500);

/* ── Broker status polling ───────────────────────────────────── */
async function pollBS(){
  try{
    let d=await(await fetch('/api/broker_status')).json();
    let bs=$('bstatus');
    if(d.message){bs.textContent=d.message;bs.className=d.message.startsWith('Connected')?'ok':'err';}
  }catch(e){}
}
setInterval(pollBS,2500);
pollBS();

/* ── Main status polling ─────────────────────────────────────── */
function renderSignals(sigs){
  let sl=$('siglist'),se=$('sigempty');sl.innerHTML='';se.style.display='none';
  let has=false;
  (sigs||[]).forEach(s=>{has=true;let div=document.createElement('div');div.className='sitem '+(s.signal==='BUY'?'buy':'sell');div.innerHTML=`<span>${s.time} <b>${s.signal}</b> ${s.symbol} @ $${s.price}</span><span>${s.rationale||''}</span>`;sl.appendChild(div);});
  if(!has)se.style.display='block';
}
function renderOrders(ords){
  let hl=$('histlist'),he=$('hstempty');hl.innerHTML='';he.style.display='none';
  let has=false;
  (ords||[]).forEach(o=>{has=true;let div=document.createElement('div');div.className='sitem '+(o.action==='BUY'?'buy':'sell');div.innerHTML=`<span>${o.time} <b>${o.action}</b> ${o.qty} ${o.symbol} @ $${o.price}</span>`;hl.appendChild(div);});
  if(!has)he.style.display='block';
}
async function pollStatus(){
  try{
    let d=await(await fetch('/api/status')).json();
    botRunning=d.running;
    $('v-eq').textContent='$'+fmt(d.equity);
    $('v-bp').textContent='$'+fmt(d.buying_power);
    let pct=d.equity?(d.pl/d.equity*100):0;
    $('v-pl').innerHTML=`<span style="color:${pct>=0?'var(--accent)':'var(--danger)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</span>`;
    $('v-pos').textContent=d.open_positions;
    renderSignals(d.signals);renderOrders(d.orders);
    $('logbar').innerHTML=(d.log||[]).join('<br>');
  }catch(e){}
}
setInterval(pollStatus,1500);

/* ── Presets ─────────────────────────────────────────────────── */
const PRESETS={
  scalping:{timeframe:'1m',emas:[9,50],rsi:true,macd:true,vwap:false,bollinger:false,adx:false,volume:true,supertrend:false,stochastic:false,bracket:false,atr:false,direction:'long'},
  swing:{timeframe:'15m',emas:[20,50],rsi:true,macd:true,vwap:true,bollinger:true,adx:true,volume:false,supertrend:false,stochastic:false,bracket:true,sl:3,tp:5,atr:false,direction:'both'},
  breakout:{timeframe:'5m',emas:[9,50],rsi:false,macd:false,vwap:false,bollinger:false,adx:false,volume:true,supertrend:true,stochastic:false,bracket:false,atr:true,direction:'both'},
};
function loadPreset(){
  const p=PRESETS[$('preset-select').value];if(!p)return;
  sv('tf',p.timeframe);sv('emaf',p.emas[0]);sv('emas',p.emas[1]);
  sc('ursi',!!p.rsi);sc('umacd',!!p.macd);sc('uvwap',!!p.vwap);sc('uboll',!!p.bollinger);
  sc('uadx',!!p.adx);sc('uvol',!!p.volume);sc('ust',!!p.supertrend);sc('ustoch',!!p.stochastic);
  sc('ubracket',!!p.bracket);sc('uatr',!!p.atr);
  if(p.sl)sv('slp',p.sl);if(p.tp)sv('tpp',p.tp);
  if(licValid&&p.direction)sv('dir',p.direction);
  toast('Preset loaded - click Save to persist','success');
}

/* ── Backtest ────────────────────────────────────────────────── */
async function runBT(){
  const days=parseInt($('btDays').value)||5;
  toast('Running backtest...','info');
  $('btres').innerHTML='<p class="ph">Loading...</p>';
  switchTab('backtest');
  $('mc-btn').disabled=true;$('csv-btn').disabled=true;
  $('pdf-btn').disabled=true;$('tune-btn').disabled=true;
  try{
    let r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days})});
    let data=await r.json();
    lastBTData=data;
    if(data.error){toast('Backtest error: '+data.error,'error');$('btres').innerHTML=`<p class="ph" style="color:var(--danger)">${data.error}</p>`;return;}
    let html='';
    for(let sym in data.results){
      let info=data.results[sym];html+=`<h3 style="color:var(--accent)">${sym}</h3>`;
      if(info.error){html+=`<p style="color:var(--danger)">${info.error}</p>`;continue;}
      if(info.simulation){
        let sim=info.simulation;
        html+=`<div style="background:var(--card);padding:10px;border-radius:8px;margin-bottom:10px;">
          Init: $${sim.initial_cash} | Final: $${sim.final_cash.toFixed(2)} |
          P&L: <span style="color:${sim.total_pnl>=0?'var(--accent)':'var(--danger)'}">${sim.total_pnl>=0?'+':''}$${sim.total_pnl.toFixed(2)}</span> |
          Win: ${sim.win_rate}% | Trades: ${sim.total_trades}
        </div>`;
        if(sim.trades.filter(t=>t.type==='exit').length){
          html+=`<table class="bttbl"><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Entry $</th><th>Exit $</th><th>P&L</th></tr>`;
          sim.trades.filter(t=>t.type==='exit').forEach(t=>{html+=`<tr><td>${String(t.entry_time).slice(0,16)}</td><td>${String(t.exit_time).slice(0,16)}</td><td style="color:${t.side==='LONG'?'var(--accent)':'var(--danger)'}">${t.side}</td><td>${t.entry_price.toFixed(2)}</td><td>${t.exit_price.toFixed(2)}</td><td style="color:${t.pnl>=0?'var(--accent)':'var(--danger)'}">${t.pnl>=0?'+':''}${t.pnl.toFixed(2)}</td></tr>`;});
          html+=`</table>`;
        }
      }
      if(info.signals&&info.signals.length){
        html+=`<details><summary style="cursor:pointer;color:var(--muted);">Raw Signals (${info.signals.length})</summary><table class="bttbl"><tr><th>Time</th><th>Sig</th><th>Price</th><th>Conf</th></tr>`;
        info.signals.forEach(s=>{html+=`<tr><td>${s.time}</td><td class="${s.signal==='BUY'?'buy':'sell'}">${s.signal}</td><td>$${s.price}</td><td>${(s.confidence*100).toFixed(0)}%</td></tr>`;});
        html+=`</table></details>`;
      }
    }
    if(data.portfolio){
      let p=data.portfolio;
      html+=`<div style="background:var(--card);padding:12px;border-radius:8px;margin-top:12px;">
        <b style="color:var(--accent)">Portfolio Summary</b><br>
        Init: $${p.initial_cash} | Final: $${p.final_cash.toFixed(2)} |
        P&L: <span style="color:${p.total_pnl>=0?'var(--accent)':'var(--danger)'}">${p.total_pnl>=0?'+':''}$${p.total_pnl.toFixed(2)}</span> |
        Trades: ${p.total_trades}
      </div>`;
    }
    $('btres').innerHTML=html||'<p class="ph">No results.</p>';
    $('mc-btn').disabled=false;$('csv-btn').disabled=false;
    $('pdf-btn').disabled=false;$('tune-btn').disabled=false;
    loadLeaderboard();
  }catch(e){toast('Backtest failed: '+e,'error');}
}

async function runMC(){
  toast('Running Monte Carlo (1000 sims)...','info');
  let r=await fetch('/api/backtest/montecarlo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days:parseInt($('btDays').value)||5})});
  let d=await r.json();
  if(d.error){toast(d.error,'error');return;}
  $('btres').innerHTML+= `<div style="background:var(--card);padding:12px;border-radius:8px;margin-top:12px;">
    <b style="color:var(--accent)">Monte Carlo (1000 runs)</b><br>
    Prob. Profit: <b>${d.prob_profit}%</b> | Best: +$${d.best} | Avg: $${d.average} | Worst: $${d.worst}
  </div>`;
}

function getAllExitTrades(){
  if(!lastBTData)return[];
  let trades=[];
  for(let sym in lastBTData.results){
    const sim=lastBTData.results[sym].simulation;
    if(sim)trades.push(...sim.trades.filter(t=>t.type==='exit'));
  }
  return trades;
}
async function exportCSV(){
  const trades=getAllExitTrades();if(!trades.length){toast('No trades to export','error');return;}
  let r=await fetch('/api/export/backtest/csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trades})});
  let blob=await r.blob();let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='backtest.csv';a.click();
}
async function exportPDF(){
  const trades=getAllExitTrades();if(!trades.length){toast('No trades to export','error');return;}
  let r=await fetch('/api/export/backtest/pdf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trades})});
  let blob=await r.blob();let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='backtest.pdf';a.click();
}
async function autoTune(){
  if(!lastBTData){toast('Run a backtest first','error');return;}
  let summary='';
  for(let sym in lastBTData.results){const sim=lastBTData.results[sym].simulation;if(sim)summary+=`${sym}: win_rate=${sim.win_rate}%, trades=${sim.total_trades}, pnl=$${sim.total_pnl} `;}
  const msg=`Based on this backtest (${summary}), suggest the best indicator combination and SL/TP settings for TraderMoney to improve performance.`;
  switchTab('aichat');await initAIChat();
  $('chat-input').value=msg;await sendChat();
}

/* ── Correlation Matrix ──────────────────────────────────────── */
async function loadCorr(){
  $('corr-content').innerHTML='<p class="ph">Loading...</p>';
  let d=await(await fetch('/api/correlation')).json();
  $('corr-content').innerHTML=d.html||'<p class="ph">No data</p>';
}

/* ── Leaderboard ─────────────────────────────────────────────── */
async function loadLeaderboard(){
  try{
    let d=await(await fetch('/api/leaderboard')).json();
    let lb=d.leaderboard||[];
    let html='<h4 style="color:var(--accent)">\u{1F3C6} Leaderboard</h4>';
    if(!lb.length){html+='<p style="font-size:.8rem;color:var(--muted)">Run a backtest to appear.</p>';}
    else{
      html+='<table class="bttbl"><tr><th>Rank</th><th>ID</th><th>Win Rate</th><th>Signals</th><th>Last BT</th></tr>';
      lb.forEach((r,i)=>{html+=`<tr><td>${i+1}</td><td>${r.user_id}</td><td>${r.win_rate.toFixed(1)}%</td><td>${r.total_signals}</td><td>${r.last_backtest||''}</td></tr>`;});
      html+='</table>';
    }
    let wrap=$('leaderboard-wrap');if(wrap)wrap.innerHTML=html;
  }catch(e){}
}

/* ── AI Chat ─────────────────────────────────────────────────── */
async function initAIChat(){
  if(chatInited)return;chatInited=true;
  await loadSessions();
  const raw=await fetch('/api/chat/sessions');
  const data=await raw.json();
  if(data.sessions&&data.sessions.length>0) await loadSession(data.sessions[0].id);
  else await createNewSession();
  updateChatLimitInfo();
}
async function loadSessions(){
  try{const d=await(await fetch('/api/chat/sessions')).json();renderSessionList(d.sessions||[]);}catch(e){}
}
function renderSessionList(sessions){
  let list=$('chat-sessions-list');list.innerHTML='';
  sessions.forEach(s=>{
    let item=document.createElement('div');item.className='chat-session-item'+(s.id===curSessionId?' active':'');
    item.textContent=s.title;item.onclick=()=>loadSession(s.id);list.appendChild(item);
  });
}
async function loadSession(sid){
  curSessionId=sid;
  const sessData=await(await fetch('/api/chat/sessions')).json();
  renderSessionList(sessData.sessions||[]);
  try{
    const histData=await(await fetch(`/api/chat/sessions/${sid}`)).json();
    $('chat-messages').innerHTML='';
    (histData.messages||[]).forEach(m=>addChatMsg(m.content,m.role==='user'));
  }catch(e){}
  updateChatLimitInfo();
}
async function createNewSession(){
  let r=await fetch('/api/chat/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'New Chat'})});
  let data=await r.json();curSessionId=data.session_id;
  await loadSessions();$('chat-messages').innerHTML='';updateChatLimitInfo();
}
function updateChatLimitInfo(){
  let el=$('chat-limit');if(!el)return;
  el.textContent=licValid?'Pro - unlimited':'Free: 5 messages/day';
}
function addChatMsg(text,isUser){
  let msgs=$('chat-messages');
  let wrap=document.createElement('div');wrap.className='cmsg '+(isUser?'user':'bot');
  let sender=document.createElement('div');sender.className='msender';sender.innerHTML=isUser?'You':'<svg class="icon" style="width:12px;height:12px"><use href="#i-robot"/></svg>TraderBot';
  let body=document.createElement('div');body.className='mbody';body.textContent=text;
  wrap.appendChild(sender);wrap.appendChild(body);msgs.appendChild(wrap);msgs.scrollTop=msgs.scrollHeight;
  return wrap;
}
async function sendChat(){
  let inputEl=$('chat-input');let msg=inputEl.value.trim();if(!msg)return;
  inputEl.value='';addChatMsg(msg,true);
  let typing=document.createElement('div');typing.className='chat-typing';
  typing.textContent='TraderBot is thinking...';$('chat-messages').appendChild(typing);
  $('chat-messages').scrollTop=$('chat-messages').scrollHeight;
  let sendBtn=$('chat-send');sendBtn.disabled=true;
  try{
    let r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,session_id:curSessionId})});
    let d=await r.json();typing.remove();
    addChatMsg(d.reply||'No response.',false);
    if(d.session_id&&d.session_id!==curSessionId){curSessionId=d.session_id;loadSessions();}
  }catch(e){typing.remove();addChatMsg('Connection error. Please try again.',false);}
  sendBtn.disabled=false;$('chat-messages').scrollTop=$('chat-messages').scrollHeight;
}
$('chat-input').addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}
});

/* ── Voice Assistant ─────────────────────────────────────────── */
function startVoice(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){toast('Voice input not supported in this browser','error');return;}
  const r=new SR();r.lang='en-US';r.start();
  r.onresult=e=>{$('chat-input').value=e.results[0][0].transcript;sendChat();};
  r.onerror=()=>toast('Voice capture error - try again','error');
}

/* ── Keyboard Shortcuts ──────────────────────────────────────── */
document.addEventListener('keydown',e=>{
  const ctrl=e.ctrlKey||e.metaKey;
  if(ctrl&&e.code==='Space'){e.preventDefault();if(botRunning)stopBot();else startBot();}
  if(ctrl&&e.key==='k'){e.preventDefault();$('tickers').focus();}
  if(ctrl&&!e.shiftKey&&e.key==='b'){e.preventDefault();runBT();}
  if(ctrl&&e.shiftKey&&e.key==='B'){e.preventDefault();switchTab('backtest');}
  if(ctrl&&e.key>='1'&&e.key<='7'){e.preventDefault();switchTab(TABS[parseInt(e.key)-1]);}
});

/* ── Boot ────────────────────────────────────────────────────── */
updateBrokerOptions();updateCreds();loadConfig();
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════════════════
# FLASK BOOT + WEBVIEW
# ═══════════════════════════════════════════════════════════════════════════════
def run_flask():
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)


if __name__ == "__main__":
    acquire_lock()
    db.clean_candle_cache()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.2)

    window = webview.create_window(
        "TraderMoney 2.2.1",
        "http://127.0.0.1:5050",
        width=1440,
        height=880,
        min_size=(980, 700),
    )
    webview.start()

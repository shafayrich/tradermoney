# -*- coding: utf-8 -*-
"""
TraderMoney v2.2.0 – Major Feature Release
Changes from v2.1.3:
  1. AI Chatbot: markdown rendering, session rename/delete.
  2. Backtesting: enriched with symbol, shares, entry/exit reasons, indicator snapshots.
  3. Custom Thesis Builder: user-configurable indicator parameters.
  4. PDF/CSV: downloads to ~/Downloads instead of opening in-app.
  5. Broker parity improvements for all 6 brokers.
  6. Modern UI redesign (same gold/black palette).
  7. Expanded Help section, fixed keyboard shortcuts.
  8. API key security via environment variables.
  9. Windows build fixes, Apple Silicon native build.

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

APP_VERSION = "2.2.0"

# ═══════════════════════════════════════════════════════════════════════════════
# AI CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MODELS = [
    "google/gemini-2.5-flash",
    "deepseek/deepseek-chat-v3-0324",
    "meta-llama/llama-4-maverick",
]
FREE_CHAT_DAILY_LIMIT = 5
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")

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

    def insert_trade(self, ts, sym, action, qty, price):
        self._exec(
            "INSERT INTO trades(timestamp,symbol,action,quantity,price)VALUES(?,?,?,?,?)",
            (ts, sym, action, qty, price))

    def get_recent_trades(self, limit=50):
        cur = self.conn.execute(
            "SELECT timestamp,symbol,action,quantity,price FROM trades ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{"time": r[0], "symbol": r[1], "action": r[2], "qty": r[3], "price": r[4]} for r in cur]

    def insert_signal(self, ts, sym, sig, price, rationale):
        self._exec(
            "INSERT INTO signals(timestamp,symbol,signal,price,rationale)VALUES(?,?,?,?,?)",
            (ts, sym, sig, price, rationale))

    def get_recent_signals(self, limit=50):
        cur = self.conn.execute(
            "SELECT timestamp,symbol,signal,price,rationale FROM signals ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{"time": r[0], "symbol": r[1], "signal": r[2], "price": r[3], "rationale": r[4]} for r in cur]

    def insert_log(self, msg: str):
        self._exec("INSERT INTO logs(timestamp,message)VALUES(?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))

    def get_recent_logs(self, limit=50):
        cur = self.conn.execute("SELECT timestamp,message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return [f"{r[0]}  {r[1]}" for r in cur]

    def insert_backtest(self, config_json: str):
        self._exec("INSERT INTO backtests(timestamp,config_json)VALUES(?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_json))

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

    def update_leaderboard(self, user_id: str, win_rate: float, total_signals: int):
        self._exec("INSERT OR REPLACE INTO leaderboard VALUES(?,?,?,?)",
                   (user_id, win_rate, total_signals, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def get_leaderboard(self) -> List[dict]:
        cur = self.conn.execute(
            "SELECT user_id,win_rate,total_signals,last_backtest FROM leaderboard ORDER BY win_rate DESC")
        return [{"user_id": r[0][:6], "win_rate": r[1], "total_signals": r[2], "last_backtest": r[3]} for r in cur]

    def rename_chat_session(self, session_id: int, title: str):
        self._exec("UPDATE chat_sessions SET title=? WHERE id=?", (title, session_id))

    def delete_chat_session(self, session_id: int):
        with self._lock:
            self.conn.execute("DELETE FROM chat_history WHERE session_id=?", (session_id,))
            self.conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
            self.conn.commit()


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

def get_indicator_params(config: dict) -> dict:
    """Get indicator parameters from config, with defaults."""
    defaults = _DEFAULT_CONFIG.get("indicator_params", {})
    params = config.get("indicator_params", {})
    merged = {**defaults, **params}
    return merged

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
    "news_sentiment": False,
    "device_uuid": str(uuid.uuid4()),
    "alpaca": {"api_key": "", "secret_key": "", "paper": True},
    "ibkr": {"host": "", "port": "", "client_id": ""},
    "tradier": {"access_token": "", "account_id": "", "sandbox": False},
    "binance": {"api_key": "", "api_secret": "", "testnet": True},
    "bybit": {"api_key": "", "api_secret": "", "testnet": True},
    "okx": {"api_key": "", "api_secret": "", "api_passphrase": "", "demo": True},
    # Customizable indicator parameters for thesis builder
    "indicator_params": {
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "bb_period": 20,
        "bb_std": 2.0,
        "adx_threshold": 20,
        "adx_period": 14,
        "vol_threshold": 1.5,
        "vol_period": 20,
        "supertrend_period": 10,
        "supertrend_multiplier": 3.0,
        "stoch_k_period": 14,
        "stoch_d_period": 3,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
        "atr_tp_mult": 3.0,
    },
    "custom_theses": [],
}

class AppState:
    def __init__(self):
        loaded = EncryptedConfigManager.load()
        self.config = {**_DEFAULT_CONFIG, **loaded} if loaded else dict(_DEFAULT_CONFIG)
        for k in ("alpaca", "ibkr", "tradier", "binance", "bybit", "okx"):
            if k not in self.config or not isinstance(self.config[k], dict):
                self.config[k] = dict(_DEFAULT_CONFIG[k])
        if "indicator_params" not in self.config or not isinstance(self.config["indicator_params"], dict):
            self.config["indicator_params"] = dict(_DEFAULT_CONFIG["indicator_params"])
        else:
            # Merge with defaults to fill any missing keys
            merged = {**_DEFAULT_CONFIG["indicator_params"], **self.config["indicator_params"]}
            self.config["indicator_params"] = merged
        if "custom_theses" not in self.config:
            self.config["custom_theses"] = []
        self.config["license_valid"] = False
        self.config["license_key"] = ""
        self.ui_queue: queue.Queue = queue.Queue()
        self.engine: Optional["TradingEngine"] = None
        self.broker_instance: Optional["BaseBroker"] = None
        self.running: bool = False
        self.internet_status: bool = True
        self.dashboard: dict = {"equity": 0, "pl": 0, "buying_power": 0, "open_positions": 0}
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
            pos_count = len(self.get_positions())
            return {
                "equity": float(bal.get("total_equity", 0)),
                "pl": 0.0,
                "buying_power": float(bal.get("equity_buying_power", 0)),
                "cash": float(bal.get("total_cash", 0)),
                "open_positions": pos_count,
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
            positions = [a for a, v in bals.items() if a != "USDT" and v > 0]
            total_equity = usdt
            for a in positions:
                try:
                    price = float(self.client.ticker_price(symbol=a + "USDT")["price"])
                    total_equity += bals[a] * price
                except Exception:
                    pass
            return {"equity": total_equity, "pl": 0.0, "buying_power": usdt, "cash": usdt, "open_positions": len(positions)}
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
            coins = result.get("coin", [])
            pos_count = sum(1 for c in coins if c["coin"] != "USDT" and float(c.get("walletBalance", 0)) > 0)
            return {"equity": equity, "pl": 0.0, "buying_power": avail, "cash": avail, "open_positions": pos_count}
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
            pos_count = sum(1 for d in details if d.get("ccy") != "USDT" and float(d.get("eq", 0)) > 0)
            return {"equity": equity, "pl": 0.0, "buying_power": usdt, "cash": usdt, "open_positions": pos_count}
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
    def compute_all(df: pd.DataFrame, ema_fast: int = 9, ema_slow: int = 50,
                    indicator_params: Optional[dict] = None) -> pd.DataFrame:
        p = indicator_params or {}
        rsi_period = int(p.get("rsi_period", 14))
        macd_fast_p = int(p.get("macd_fast", 12))
        macd_slow_p = int(p.get("macd_slow", 26))
        macd_signal_p = int(p.get("macd_signal", 9))
        bb_period = int(p.get("bb_period", 20))
        bb_std = float(p.get("bb_std", 2.0))
        adx_period = int(p.get("adx_period", 14))
        vol_period = int(p.get("vol_period", 20))
        st_period = int(p.get("supertrend_period", 10))
        st_mult = float(p.get("supertrend_multiplier", 3.0))
        stoch_k = int(p.get("stoch_k_period", 14))
        stoch_d = int(p.get("stoch_d_period", 3))
        atr_period = int(p.get("atr_period", 14))

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

        # RSI with custom period
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        ag = np.convolve(gain, np.ones(rsi_period) / rsi_period, mode="full")[:len(close)]
        al = np.convolve(loss, np.ones(rsi_period) / rsi_period, mode="full")[:len(close)]
        rs = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
        df["RSI"] = 100 - (100 / (1 + rs))

        # MACD with custom periods
        m = ema(close, macd_fast_p) - ema(close, macd_slow_p)
        df["MACD"] = m
        df["MACD_signal"] = ema(m, macd_signal_p)

        # Bollinger Bands with custom period and std
        ma_bb = np.convolve(close, np.ones(bb_period) / bb_period, mode="same")
        std_bb = np.array([np.std(close[max(0, i - bb_period + 1):i + 1]) for i in range(len(close))])
        df["BB_upper"] = ma_bb + bb_std * std_bb
        df["BB_lower"] = ma_bb - bb_std * std_bb

        cum_vol = np.cumsum(volume)
        df["VWAP"] = np.divide(np.cumsum(close * volume), cum_vol, out=np.zeros_like(close), where=cum_vol != 0)

        # ATR with custom period
        tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
        tr = np.insert(tr, 0, np.mean(tr[:atr_period]) if len(tr) >= atr_period else (tr[0] if len(tr) else 0))
        atr_val = ema(tr, atr_period)
        df["ATR"] = atr_val

        # ADX with custom period
        up = np.maximum(np.diff(high, prepend=high[0]), 0.0)
        dn = np.maximum(-np.diff(low, prepend=low[0]), 0.0)
        pdm = np.where((up > dn) & (up > 0), up, 0.0)
        mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
        pdi = 100 * ema(pdm, adx_period) / (atr_val + 1e-14)
        mdi = 100 * ema(mdm, adx_period) / (atr_val + 1e-14)
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-14)
        df["ADX"] = ema(dx, adx_period)

        # Volume ratio with custom period
        vol_avg = np.convolve(volume, np.ones(vol_period) / vol_period, mode="same")
        df["Vol_ratio"] = np.divide(volume, vol_avg, out=np.ones_like(volume), where=vol_avg != 0)

        # SuperTrend with custom period and multiplier
        st_atr = ema(tr, st_period)
        hl2 = (high + low) / 2.0
        upper_s = hl2 + st_mult * st_atr
        lower_s = hl2 - st_mult * st_atr
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

        # Stochastic with custom periods
        ll = np.array([np.min(low[max(0, i - stoch_k + 1):i + 1]) for i in range(len(close))])
        hh = np.array([np.max(high[max(0, i - stoch_k + 1):i + 1]) for i in range(len(close))])
        stk_val = np.where(hh - ll != 0, 100 * (close - ll) / (hh - ll + 1e-14), 50.0)
        df["Stoch_K"] = stk_val
        df["Stoch_D"] = np.convolve(stk_val, np.ones(stoch_d) / stoch_d, mode="same")
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
    def generate_signal(df: pd.DataFrame, prev_fast, prev_slow, config: dict,
                        indicator_params: Optional[dict] = None) -> Tuple[Optional[str], str, float]:
        p = indicator_params or {}
        adx_threshold = int(p.get("adx_threshold", SignalAnalyzer.ADX_THRESHOLD))
        vol_threshold = float(p.get("vol_threshold", SignalAnalyzer.VOL_THRESHOLD))
        rsi_oversold = int(p.get("rsi_oversold", 30))
        rsi_overbought = int(p.get("rsi_overbought", 70))
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
            passes, dir_ = SignalAnalyzer._confirm(df, config, "bull", price, p)
        elif bear:
            passes, dir_ = SignalAnalyzer._confirm(df, config, "bear", price, p)
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
    def _confirm(df: pd.DataFrame, config: dict, direction: str, price: float,
                 indicator_params: Optional[dict] = None) -> Tuple[bool, str]:
        p = indicator_params or {}
        adx_threshold = int(p.get("adx_threshold", SignalAnalyzer.ADX_THRESHOLD))
        vol_threshold = float(p.get("vol_threshold", SignalAnalyzer.VOL_THRESHOLD))
        rsi_oversold = int(p.get("rsi_oversold", 30))
        rsi_overbought = int(p.get("rsi_overbought", 70))
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
            if config.get("use_rsi", True) and rsi < rsi_oversold:
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
            if config.get("use_adx", True) and adx < adx_threshold:
                return False, "bull"
            if config.get("use_vol_confirm", True) and vr < vol_threshold:
                return False, "bull"
        else:
            if config.get("use_rsi", True) and rsi > rsi_overbought:
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
            if config.get("use_adx", True) and adx < adx_threshold:
                return False, "bear"
            if config.get("use_vol_confirm", True) and vr < vol_threshold:
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
                            ind_params = self.config.get("indicator_params", {})
                            sig, rationale, conf = SignalAnalyzer.generate_signal(
                                df, prev_f, prev_s, self.config, indicator_params=ind_params)
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

            if resp.status_code == 401:
                db.insert_log(f"[AI] 401 Unauthorized from {model} – API key may be invalid or expired")
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
            if "401" in str(e):
                return _get_offline_response(messages)
        except Exception as e:
            last_error = f"Error on {model}: {e}"
            db.insert_log(f"[AI] {last_error}")

        time.sleep(2 ** attempt)

    return _get_offline_response(messages)


def _get_offline_response(messages: List[dict]) -> str:
    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_msg = msg.get("content", "").lower()
            break

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
        import platform as _plat
        sys_name = _plat.system()
        is_arm = _plat.machine() in ("arm64", "aarch64")
        if sys_name == "Windows":
            dl = data.get("download_url_windows", "")
        elif sys_name == "Darwin" and is_arm:
            dl = data.get("download_url_silicon", "")
        elif sys_name == "Darwin":
            dl = data.get("download_url_intel", "")
        else:
            dl = data.get("download_url_silicon", "")
        return jsonify({
            "current_version": APP_VERSION,
            "latest_version": latest,
            "download_url": dl,
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

# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ROUTES  [FIX 1: corrected P&L accounting]
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
        portfolio_equity = float(initial_cash)   # used only in portfolio mode

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
                ind_params = config.get("indicator_params", {})
                df = IndicatorCalculator.compute_all(df, ef, es, indicator_params=ind_params)
                sigs: List[dict] = []
                for i in range(1, len(df)):
                    prev = df.iloc[i - 1]
                    curr = df.iloc[i]
                    pf = SignalAnalyzer._sf(prev["EMA_fast"])
                    ps = SignalAnalyzer._sf(prev["EMA_slow"])
                    sig, _, conf = SignalAnalyzer.generate_signal(
                        df.iloc[:i + 1], pf, ps, config)
                    if sig:
                        row = df.iloc[i]
                        rsi_v = SignalAnalyzer._sf(row.get("RSI", 50))
                        macd_v = SignalAnalyzer._sf(row.get("MACD", 0))
                        bb_pct = SignalAnalyzer._sf(row.get("Close", row.get("close", price)))
                        reasons = []
                        if config.get("use_rsi", True):
                            reasons.append(f"RSI={rsi_v:.1f}")
                        if config.get("use_macd", True):
                            reasons.append(f"MACD={'above' if macd_v > SignalAnalyzer._sf(row.get('MACD_signal',0)) else 'below'} signal")
                        if config.get("use_adx", True):
                            reasons.append(f"ADX={SignalAnalyzer._sf(row.get('ADX',0)):.1f}")
                        sigs.append({
                            "time": str(df.index[i]),
                            "signal": sig,
                            "symbol": sym,
                            "price": round(SignalAnalyzer._sf(curr["Close"]), 2),
                            "shares": 0,
                            "confidence": conf,
                            "reason": "; ".join(reasons) if reasons else "EMA crossover",
                            "indicators": {
                                "rsi": round(rsi_v, 1),
                                "macd": round(macd_v, 2),
                                "adx": round(SignalAnalyzer._sf(row.get("ADX", 0)), 1),
                                "vol_ratio": round(SignalAnalyzer._sf(row.get("Vol_ratio", 1)), 2),
                                "bb_upper": round(SignalAnalyzer._sf(row.get("BB_upper", 0)), 2),
                                "bb_lower": round(SignalAnalyzer._sf(row.get("BB_lower", 0)), 2),
                            }
                        })
                sym_results["signals"] = sigs

                # ── FIXED simulation ──────────────────────────────────────────
                # equity tracks current total value; cash is liquid (0 when invested).
                # position > 0 = long shares, position < 0 = short shares (abs = count).
                # Long entry:  cash → shares; cash = 0
                # Long exit:   shares → cash = shares * exit_price
                # Short entry: cash → short; cash = 0 (collateral locked)
                # Short exit:  cash = abs(pos) * entry_price + pnl
                #              (= principal ± profit from short)
                equity: float = float(initial_cash)
                cash: float = float(initial_cash)
                position: float = 0.0
                entry_price: float = 0.0
                entry_time: str = ""
                entry_reason: str = ""
                entry_indicators: dict = {}
                entry_shares: float = 0.0
                trades: List[dict] = []

                for s in sigs:
                    price = float(s["price"])

                    if s["signal"] == "BUY" and position <= 0:
                        # Close any open short first
                        if position < 0:
                            pnl = (entry_price - price) * abs(position)
                            cash = abs(position) * entry_price + pnl
                            equity = cash
                            trades.append({
                                "entry_time": entry_time, "exit_time": s["time"],
                                "side": "SHORT", "symbol": sym,
                                "entry_price": entry_price, "exit_price": price,
                                "shares": abs(position), "pnl": round(pnl, 2), "type": "exit",
                                "reason_open": entry_reason, "reason_close": "BUY signal closed short",
                                "indicators_at_entry": entry_indicators,
                            })
                        # Open long
                        entry_shares = cash / price
                        position = entry_shares
                        entry_price = price
                        entry_time = s["time"]
                        entry_reason = s.get("reason", "EMA crossover bullish")
                        entry_indicators = s.get("indicators", {})
                        cash = 0.0
                        trades.append({
                            "entry_time": s["time"], "exit_time": "",
                            "side": "LONG", "symbol": sym,
                            "entry_price": entry_price, "exit_price": 0,
                            "shares": round(entry_shares, 4), "pnl": 0, "type": "entry",
                            "reason_open": entry_reason, "reason_close": "",
                            "indicators_at_entry": entry_indicators,
                        })

                    elif s["signal"] == "SELL" and position >= 0:
                        # Close any open long first
                        if position > 0:
                            pnl = (price - entry_price) * position
                            cash = position * price
                            equity = cash
                            trades.append({
                                "entry_time": entry_time, "exit_time": s["time"],
                                "side": "LONG", "symbol": sym,
                                "entry_price": entry_price, "exit_price": price,
                                "shares": round(position, 4), "pnl": round(pnl, 2), "type": "exit",
                                "reason_open": entry_reason, "reason_close": s.get("reason", "EMA crossover bearish"),
                                "indicators_at_entry": entry_indicators,
                            })
                        # Open short
                        entry_shares = cash / price
                        position = -(entry_shares)   # negative = short shares
                        entry_price = price
                        entry_time = s["time"]
                        entry_reason = s.get("reason", "EMA crossover bearish")
                        entry_indicators = s.get("indicators", {})
                        cash = 0.0
                        trades.append({
                            "entry_time": s["time"], "exit_time": "",
                            "side": "SHORT", "symbol": sym,
                            "entry_price": entry_price, "exit_price": 0,
                            "shares": round(entry_shares, 4), "pnl": 0, "type": "entry",
                            "reason_open": entry_reason, "reason_close": "",
                            "indicators_at_entry": entry_indicators,
                        })

                # Mark-to-market any open position at the last signal price
                if position != 0 and sigs:
                    last_price = float(sigs[-1]["price"])
                    if position > 0:
                        pnl = (last_price - entry_price) * position
                        cash = position * last_price
                        side_label = "LONG"
                    else:
                        pnl = (entry_price - last_price) * abs(position)
                        cash = abs(position) * entry_price + pnl
                        side_label = "SHORT"
                    equity = cash
                    trades.append({
                        "entry_time": entry_time, "exit_time": sigs[-1]["time"],
                        "side": side_label, "symbol": sym,
                        "entry_price": entry_price, "exit_price": last_price,
                        "shares": round(abs(position), 4), "pnl": round(pnl, 2), "type": "exit",
                        "reason_open": entry_reason,
                        "reason_close": "Mark-to-market (end of data)",
                        "indicators_at_entry": entry_indicators,
                    })

                final_cash = equity
                exits = [t for t in trades if t["type"] == "exit"]
                total_pnl = sum(t["pnl"] for t in exits)
                wins = sum(1 for t in exits if t["pnl"] > 0)
                losses = sum(1 for t in exits if t["pnl"] < 0)
                win_rate = (wins / len(exits) * 100) if exits else 0

                # Advanced metrics
                pnl_list = [t["pnl"] for t in exits]
                avg_trade = float(np.mean(pnl_list)) if pnl_list else 0.0
                best_trade = max(pnl_list) if pnl_list else 0.0
                worst_trade = min(pnl_list) if pnl_list else 0.0
                gross_profit = sum(p for p in pnl_list if p > 0)
                gross_loss = abs(sum(p for p in pnl_list if p < 0))
                profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)
                avg_win = (gross_profit / wins) if wins > 0 else 0.0
                avg_loss = (gross_loss / losses) if losses > 0 else 0.0
                expectancy = avg_trade

                # Equity curve
                eq_curve = [{"time": "Start", "equity": float(initial_cash)}]
                running_eq = float(initial_cash)
                for t in exits:
                    running_eq += t["pnl"]
                    eq_curve.append({"time": t["exit_time"], "equity": round(running_eq, 2)})

                # Max drawdown
                peak = float(initial_cash)
                max_dd = 0.0
                max_dd_pct = 0.0
                for pt in eq_curve:
                    if pt["equity"] > peak:
                        peak = pt["equity"]
                    dd = peak - pt["equity"]
                    dd_pct = (dd / peak * 100) if peak > 0 else 0
                    if dd > max_dd:
                        max_dd = dd
                    if dd_pct > max_dd_pct:
                        max_dd_pct = dd_pct

                # Sharpe ratio (annualized, using trade returns)
                if len(pnl_list) >= 2:
                    returns = [p / initial_cash for p in pnl_list]
                    avg_ret = float(np.mean(returns))
                    std_ret = float(np.std(returns, ddof=1))
                    sharpe = (avg_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
                else:
                    sharpe = 0.0

                # Return on investment
                roi = ((final_cash - initial_cash) / initial_cash * 100) if initial_cash > 0 else 0.0

                sym_results["simulation"] = {
                    "initial_cash": initial_cash,
                    "final_cash": round(final_cash, 2),
                    "total_pnl": round(total_pnl, 2),
                    "win_rate": round(win_rate, 1),
                    "total_trades": len(exits),
                    "wins": wins,
                    "losses": losses,
                    "avg_trade": round(avg_trade, 2),
                    "best_trade": round(best_trade, 2),
                    "worst_trade": round(worst_trade, 2),
                    "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.99,
                    "sharpe_ratio": round(sharpe, 2),
                    "max_drawdown": round(max_dd, 2),
                    "max_drawdown_pct": round(max_dd_pct, 1),
                    "roi": round(roi, 2),
                    "avg_win": round(avg_win, 2),
                    "avg_loss": round(avg_loss, 2),
                    "expectancy": round(expectancy, 2),
                    "equity_curve": eq_curve,
                    "trades": trades,
                }
                all_trades.extend(trades)
                # carry equity forward for portfolio mode
                portfolio_equity = final_cash

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
            exits_all = [t for t in all_trades if t["type"] == "exit"]
            resp["portfolio"] = {
                "initial_cash": initial_cash,
                "final_cash": round(portfolio_equity, 2),
                "total_pnl": round(sum(t["pnl"] for t in exits_all), 2),
                "total_trades": len(exits_all),
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
            equity = 10_000.0
            cash = 10_000.0
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
                            df.iloc[:i + 1], pf, ps, config,
                            indicator_params=config.get("indicator_params", {}))
                        if sig:
                            sigs.append(SignalAnalyzer._sf(curr["Close"]))
                except Exception:
                    continue
            random.shuffle(sigs)
            for price in sigs:
                if position <= 0:
                    if position < 0:
                        pnl = (entry_price - price) * abs(position)
                        cash = abs(position) * entry_price + pnl
                        equity = cash
                    position = cash / price
                    entry_price = price
                    cash = 0.0
                else:
                    pnl = (price - entry_price) * position
                    cash = position * price
                    equity = cash
                    position = 0.0
            # close any open long
            if position > 0 and sigs:
                equity = position * sigs[-1]
            elif position < 0 and sigs:
                pnl = (entry_price - sigs[-1]) * abs(position)
                equity = abs(position) * entry_price + pnl
            pnl_results.append(equity - 10_000)

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
    # [FIX 2] fpdf2 v2.x returns bytes from output(), not a string.
    # The old code called .encode("latin-1") on bytes which raised AttributeError.
    try:
        from fpdf import FPDF
    except ImportError:
        return jsonify({"error": "fpdf2 not installed. Run: pip install fpdf2"}), 500
    trades = (request.json or {}).get("trades", [])
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "TraderMoney – Backtest Report", ln=True, align="C")
    pdf.ln(3)
    pdf.set_font("Arial", size=9)
    pdf.cell(0, 7, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC", ln=True)
    pdf.ln(4)
    exits = [t for t in trades if t.get("type") == "exit"]
    if exits:
        total_pnl = sum(t["pnl"] for t in exits)
        wins = sum(1 for t in exits if t["pnl"] > 0)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 7,
                 f"Total Trades: {len(exits)}  |  "
                 f"Win Rate: {(wins / len(exits) * 100):.1f}%  |  "
                 f"P&L: ${total_pnl:.2f}",
                 ln=True)
        pdf.ln(4)
    # Table header
    pdf.set_font("Arial", "B", 9)
    col_widths = [46, 46, 22, 26, 26, 26]
    headers = ["Entry", "Exit", "Side", "Entry $", "Exit $", "P&L"]
    aligns = ["L", "L", "C", "R", "R", "R"]
    for w, h, a in zip(col_widths, headers, aligns):
        pdf.cell(w, 7, h, 1, 0, a)
    pdf.ln()
    pdf.set_font("Arial", size=8)
    for t in exits:
        pdf.cell(46, 6, str(t["entry_time"])[:16], 1)
        pdf.cell(46, 6, str(t["exit_time"])[:16], 1)
        pdf.cell(22, 6, t["side"], 1, 0, "C")
        pdf.cell(26, 6, f"${t['entry_price']:.2f}", 1, 0, "R")
        pdf.cell(26, 6, f"${t['exit_price']:.2f}", 1, 0, "R")
        pdf.set_text_color(*(0, 150, 0) if t["pnl"] >= 0 else (180, 0, 0))
        pdf.cell(26, 6, f"${t['pnl']:.2f}", 1, 0, "R")
        pdf.set_text_color(0, 0, 0)
        pdf.ln()

    # FIX: fpdf2 v2.x returns bytes directly; v1.x returned a latin-1 string.
    raw = pdf.output()
    if isinstance(raw, (bytes, bytearray)):
        pdf_bytes = bytes(raw)
    else:
        pdf_bytes = raw.encode("latin-1")

    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment;filename=backtest.pdf"})

@app.route("/api/export/backtest/csv/file", methods=["POST"])
def export_backtest_csv_file():
    trades = (request.json or {}).get("trades", [])
    if not trades:
        return jsonify({"error": "No trades"}), 400
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["Entry Time", "Exit Time", "Symbol", "Side", "Shares", "Entry Price", "Exit Price", "P&L", "Reason Open", "Reason Close"])
    for t in trades:
        if t.get("type") == "exit":
            w.writerow([t.get("entry_time",""), t.get("exit_time",""), t.get("symbol",""),
                        t.get("side",""), t.get("shares",""), t.get("entry_price",""),
                        t.get("exit_price",""), t.get("pnl",""),
                        t.get("reason_open",""), t.get("reason_close","")])
    output = si.getvalue()
    si.close()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    downloads = os.path.expanduser("~/Downloads")
    os.makedirs(downloads, exist_ok=True)
    fpath = os.path.join(downloads, f"tradermoney_backtest_{ts}.csv")
    with open(fpath, "w") as f:
        f.write(output)
    return jsonify({"path": fpath})

@app.route("/api/export/backtest/pdf/file", methods=["POST"])
def export_backtest_pdf_file():
    try:
        from fpdf import FPDF
    except ImportError:
        return jsonify({"error": "fpdf2 not installed"}), 500
    trades = (request.json or {}).get("trades", [])
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "TraderMoney - Backtest Report", ln=True, align="C")
    pdf.ln(3)
    pdf.set_font("Arial", size=9)
    pdf.cell(0, 7, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC", ln=True)
    pdf.ln(4)
    exits = [t for t in trades if t.get("type") == "exit"]
    if exits:
        total_pnl = sum(t["pnl"] for t in exits)
        wins = sum(1 for t in exits if t["pnl"] > 0)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 7, f"Total Trades: {len(exits)} | Win Rate: {(wins/len(exits)*100):.1f}% | P&L: ${total_pnl:.2f}", ln=True)
        pdf.ln(4)
        pdf.set_font("Arial", "B", 9)
        col_widths = [36, 36, 18, 16, 16, 22, 22, 22]
        headers = ["Entry", "Exit", "Sym", "Side", "Shrs", "Entry $", "Exit $", "P&L"]
        aligns = ["L","L","C","C","R","R","R","R"]
        for w, h, a in zip(col_widths, headers, aligns):
            pdf.cell(w, 7, h, 1, 0, a)
        pdf.ln()
        pdf.set_font("Arial", size=7)
        for t in exits:
            pdf.cell(36, 5, str(t.get("entry_time",""))[:14], 1)
            pdf.cell(36, 5, str(t.get("exit_time",""))[:14], 1)
            pdf.cell(18, 5, str(t.get("symbol","")), 1, 0, "C")
            pdf.cell(16, 5, t.get("side",""), 1, 0, "C")
            pdf.cell(16, 5, str(t.get("shares","")), 1, 0, "R")
            pdf.cell(22, 5, f"${t.get('entry_price',0):.2f}", 1, 0, "R")
            pdf.cell(22, 5, f"${t.get('exit_price',0):.2f}", 1, 0, "R")
            pdf.set_text_color(*(0,150,0) if t.get("pnl",0) >= 0 else (180,0,0))
            pdf.cell(22, 5, f"${t.get('pnl',0):.2f}", 1, 0, "R")
            pdf.set_text_color(0,0,0)
            pdf.ln()
    raw = pdf.output()
    pdf_bytes = bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw.encode("latin-1")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    downloads = os.path.expanduser("~/Downloads")
    os.makedirs(downloads, exist_ok=True)
    fpath = os.path.join(downloads, f"tradermoney_backtest_{ts}.pdf")
    with open(fpath, "wb") as f:
        f.write(pdf_bytes)
    return jsonify({"path": fpath})

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

@app.route("/api/chat/sessions/<int:session_id>", methods=["PUT"])
def rename_chat_session_route(session_id: int):
    title = (request.json or {}).get("title", "")
    if not title:
        return jsonify({"error": "Title required"}), 400
    db.rename_chat_session(session_id, title)
    return jsonify({"ok": True})

@app.route("/api/chat/sessions/<int:session_id>", methods=["DELETE"])
def delete_chat_session_route(session_id: int):
    db.delete_chat_session(session_id)
    return jsonify({"ok": True})

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
    except Exception as e:
        db.insert_log(f"[AI Chat] Unexpected error: {e}")
        offline_reply = _get_offline_response(messages)
        db.insert_chat_message(session_id, "bot", offline_reply)
        return jsonify({"reply": offline_reply, "session_id": session_id})

@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    return jsonify({"leaderboard": db.get_leaderboard()})


@app.route("/api/thesis/save", methods=["POST"])
def save_thesis():
    data = request.json or {}
    name = data.get("name", "").strip()
    params = data.get("params", {})
    if not name or not params:
        return jsonify({"error": "Name and params required"}), 400
    theses = state.config.get("custom_theses", [])
    for t in theses:
        if t["name"] == name:
            t["params"] = params
            break
    else:
        theses.append({"name": name, "params": params})
    state.config["custom_theses"] = theses
    EncryptedConfigManager.save(state.config)
    return jsonify({"ok": True})

@app.route("/api/thesis/delete", methods=["POST"])
def delete_thesis():
    data = request.json or {}
    name = data.get("name", "").strip()
    theses = [t for t in state.config.get("custom_theses", []) if t["name"] != name]
    state.config["custom_theses"] = theses
    EncryptedConfigManager.save(state.config)
    return jsonify({"ok": True})

@app.route("/api/thesis/list", methods=["GET"])
def list_theses():
    return jsonify({"theses": state.config.get("custom_theses", [])})

@app.route("/api/thesis/apply", methods=["POST"])
def apply_thesis():
    data = request.json or {}
    name = data.get("name", "").strip()
    params = data.get("params", {})
    if params:
        state.config["indicator_params"] = {**state.config.get("indicator_params", {}), **params}
        EncryptedConfigManager.save(state.config)
        return jsonify({"ok": True})
    for t in state.config.get("custom_theses", []):
        if t["name"] == name:
            state.config["indicator_params"] = {**state.config.get("indicator_params", {}), **t["params"]}
            EncryptedConfigManager.save(state.config)
            return jsonify({"ok": True, "params": t["params"]})
    return jsonify({"error": "Thesis not found"}), 404

# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND HTML
# ═══════════════════════════════════════════════════════════════════════════════
FRONTEND_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TraderMoney 2.2.0</title>
<style>
:root{--bg:#050505;--card:#1A1A1A;--text:#e2e2e2;--accent:#D4AF37;--danger:#B22222;--border:#2A2E38;--muted:#7a7d86;--sw:270px;--radius:12px;--shadow:0 4px 20px rgba(0,0,0,.4);--glow:0 0 12px rgba(212,175,55,.15);}
::-webkit-scrollbar{width:4px;height:4px;}::-webkit-scrollbar-track{background:#080808;}::-webkit-scrollbar-thumb{background:linear-gradient(180deg,#333,#555);border-radius:2px;}
*{box-sizing:border-box;-webkit-user-select:text;user-select:text;}
html,body{height:100%;margin:0;padding:0;overflow:hidden;}
body{font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden;color-scheme:dark;}
svg.icon{width:16px;height:16px;fill:currentColor;vertical-align:middle;margin-right:4px;flex-shrink:0;}
#sb{width:var(--sw);background:linear-gradient(180deg,#0c0c0c,#080808);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding:18px 14px;flex-shrink:0;}
#sb h2{color:var(--accent);margin:0 0 10px;font-size:1.2rem;letter-spacing:.3px;display:flex;align-items:center;gap:6px;text-shadow:0 0 10px rgba(212,175,55,.2);}
.lbadge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.67rem;vertical-align:middle;letter-spacing:.5px;text-transform:uppercase;}
.lv{background:linear-gradient(135deg,#D4AF37,#b8962e);color:#000;box-shadow:0 0 8px rgba(212,175,55,.3);}.li{background:linear-gradient(135deg,#B22222,#8b1a1a);color:#fff;}
label{display:block;font-size:.75rem;margin:10px 0 3px;color:var(--muted);cursor:pointer;letter-spacing:.3px;transition:color .2s;}
label:hover{color:var(--text);}
.cb input{display:none;}
.cb .cm{display:inline-block;width:18px;height:18px;border:2px solid #333;border-radius:6px;margin-right:6px;vertical-align:middle;position:relative;transition:all .2s;}
.cb:hover .cm{border-color:#555;}
.cb input:checked+.cm{background:var(--accent);border-color:var(--accent);box-shadow:0 0 6px rgba(212,175,55,.3);}
.cb input:checked+.cm::after{content:"";position:absolute;left:4px;top:1px;width:5px;height:9px;border:solid #000;border-width:0 2px 2px 0;transform:rotate(45deg);}
select{-webkit-appearance:none;appearance:none;background:#1A1A1A url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpolygon fill='%23D4AF37' points='0,4 12,4 6,10'/%3E%3C/svg%3E") no-repeat right 10px center;background-size:12px;color:var(--text);border:1px solid #333;padding:7px 30px 7px 10px;border-radius:10px;width:100%;font-size:.85rem;transition:border .2s,box-shadow .2s;cursor:pointer;}
select:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 2px rgba(212,175,55,.1);}
select:disabled{opacity:.5;cursor:not-allowed;}
input[type="text"],input[type="password"],input[type="number"],textarea{background:#1A1A1A;color:var(--text);border:1px solid #333;padding:7px 10px;border-radius:10px;width:100%;font-size:.85rem;transition:border .2s,box-shadow .2s;}
input:focus,textarea:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 2px rgba(212,175,55,.1);}
input:-webkit-autofill{-webkit-text-fill-color:var(--text);-webkit-box-shadow:0 0 0 30px #1A1A1A inset;}
button{cursor:pointer;background:linear-gradient(135deg,var(--accent),#b8962e);color:#050505;border:none;padding:9px 12px;border-radius:10px;width:100%;font-weight:600;margin-top:10px;font-size:.85rem;transition:all .25s;display:flex;align-items:center;justify-content:center;gap:5px;box-shadow:0 2px 8px rgba(0,0,0,.3);}
button:hover{opacity:.95;transform:translateY(-1px);box-shadow:0 4px 14px rgba(212,175,55,.25);}
button:active{transform:translateY(0);}
button.ghost{background:var(--card);border:1px solid var(--border);color:var(--text);box-shadow:none;}
button.ghost:hover{background:#222;border-color:#555;box-shadow:0 2px 8px rgba(0,0,0,.2);}
button.danger{background:linear-gradient(135deg,var(--danger),#8b1a1a);color:#fff;}
hr{border:0;height:1px;background:linear-gradient(90deg,transparent,var(--border),transparent);margin:12px 0;}
.r2{display:flex;gap:5px;}.r2 input{width:100%;}
#bstatus{font-size:.72rem;margin-top:3px;min-height:15px;word-break:break-word;padding:2px 0;}
#bstatus.ok{color:#00c9b1;}#bstatus.err{color:var(--danger);}
.free-notice{background:linear-gradient(135deg,#2a0505,#1a0303);color:#ff9090;border:1px solid var(--danger);padding:8px 10px;border-radius:8px;font-size:.74rem;margin-top:8px;display:none;line-height:1.5;}
.bt-days-input{width:70px;display:inline-block;margin-left:6px;}
#main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden;}
.tab-bar{display:flex;background:var(--card);border-bottom:1px solid var(--border);overflow:hidden;flex-shrink:0;gap:2px;padding:0 4px;}
.tbtn{flex:1;background:transparent;border:none;color:var(--text);padding:12px 4px;cursor:pointer;font-weight:500;transition:all .2s;min-width:60px;font-size:.82rem;display:flex;align-items:center;justify-content:center;gap:4px;margin:6px 0;border-radius:8px;}
.tbtn:hover{background:rgba(255,255,255,.05);}
.tbtn.active{background:rgba(212,175,55,.12);color:var(--accent);font-weight:700;box-shadow:inset 0 0 12px rgba(212,175,55,.05);}
.tab{flex:1;display:none;overflow:auto;flex-direction:column;}
.tab.active{display:flex;}
#metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px;background:linear-gradient(180deg,var(--card),#151515);border-bottom:1px solid var(--border);}
.met{text-align:center;padding:4px;border-radius:8px;transition:background .2s;}.met:hover{background:rgba(255,255,255,.02);}.met .v{font-size:1.2rem;font-weight:bold;color:var(--accent);letter-spacing:.3px;}
#sess{display:flex;align-items:center;gap:14px;padding:8px 12px;background:linear-gradient(180deg,var(--card),#151515);border-bottom:1px solid var(--border);font-size:.8rem;flex-wrap:wrap;}
.sd{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;box-shadow:0 0 4px currentColor;}
.so{background:#00c9b1;color:#00c9b1;}.sc{background:var(--danger);color:var(--danger);}
#tkbar{display:flex;flex-wrap:nowrap;overflow-x:auto;background:var(--card);border-bottom:1px solid var(--border);}
.tkbtn{padding:7px 12px;background:transparent;border:none;color:var(--text);cursor:pointer;white-space:nowrap;transition:all .2s;font-size:.82rem;flex-shrink:0;border-radius:6px;margin:3px 2px;}
.tkbtn:hover{background:rgba(255,255,255,.05);}
.tkbtn.active{background:rgba(212,175,55,.12);color:var(--accent);font-weight:700;}
#chart-c{flex:1;min-height:0;background:linear-gradient(180deg,#0c0c0c,#080808);}
.sitem{display:flex;justify-content:space-between;padding:9px 12px;border-bottom:1px solid var(--border);font-size:.82rem;transition:background .15s;}
.sitem:hover{background:rgba(255,255,255,.02);}
.buy{color:var(--accent);}.sell{color:var(--danger);}
.empty-placeholder{color:var(--muted);text-align:center;padding:30px;font-size:.9rem;}
#toasts{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:6px;}
.toast{padding:14px 22px;border-radius:14px;font-weight:500;box-shadow:0 8px 32px rgba(0,0,0,.6);animation:si .3s ease;max-width:420px;font-size:.88rem;backdrop-filter:blur(10px);}
.toast.success{background:linear-gradient(135deg,rgba(212,175,55,.95),rgba(184,150,46,.95));color:#000;border:1px solid rgba(212,175,55,.5);}
.toast.error{background:linear-gradient(135deg,rgba(178,34,34,.95),rgba(139,26,26,.95));color:#fff;border:1px solid rgba(178,34,34,.5);}
.toast.info{background:linear-gradient(135deg,rgba(26,18,0,.9),rgba(20,14,0,.9));color:var(--accent);border:1px solid rgba(212,175,55,.3);}
@keyframes si{from{transform:translateX(120%);opacity:0}to{transform:translateX(0);opacity:1}}
#upd{display:none;position:fixed;bottom:16px;right:16px;z-index:9999;background:linear-gradient(135deg,var(--accent),#b8962e);color:#000;padding:12px 18px;border-radius:10px;font-weight:bold;font-size:.88rem;box-shadow:0 4px 16px rgba(212,175,55,.3);}
#upd a{color:#000;text-decoration:underline;}
.btp{flex:1;display:flex;flex-direction:column;}
.btr{flex:1;overflow:auto;padding:10px;}
.ph{color:var(--muted);text-align:center;padding:36px 18px;font-size:.9rem;}
.bttbl{width:100%;border-collapse:collapse;font-size:.76rem;margin-bottom:18px;}
.bttbl th,.bttbl td{padding:5px 7px;border:1px solid var(--border);text-align:center;}
.bttbl th{color:var(--accent);background:rgba(212,175,55,.04);font-weight:600;text-transform:uppercase;letter-spacing:.5px;font-size:.7rem;}
.bttbl tr:hover td{background:rgba(255,255,255,.02);}
#logbar{height:100px;overflow-y:auto;background:linear-gradient(180deg,#0a0a0a,#050505);padding:8px 12px;font-size:.74rem;border-top:1px solid var(--border);color:var(--muted);flex-shrink:0;}
#logbar::-webkit-scrollbar-thumb{background:linear-gradient(180deg,#222,#333);}
.hb{padding:20px;overflow:auto;height:100%;}
.hb h3{color:var(--accent);margin-top:0;font-size:1.1rem;letter-spacing:.3px;}
.hb h4{color:var(--text);margin:16px 0 6px;font-size:.92rem;border-left:3px solid var(--accent);padding-left:8px;}
.hb p,.hb ul{font-size:.85rem;line-height:1.65;}.hb ul{padding-left:18px;}.hb li{margin-bottom:4px;}.hb a{color:var(--accent);text-decoration:none;}.hb a:hover{text-decoration:underline;}
.istat{background:linear-gradient(135deg,var(--card),#151515);border-radius:var(--radius);padding:14px;margin:8px 0;border:1px solid var(--border);box-shadow:var(--shadow);}
#aichat-wrap{display:flex;height:100%;}
#chat-sessions-panel{width:220px;background:linear-gradient(180deg,var(--card),#121212);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;}
#chat-sessions-panel h3{padding:12px;margin:0;border-bottom:1px solid var(--border);font-size:.85rem;display:flex;align-items:center;gap:5px;color:var(--accent);}
#chat-sessions-list{flex:1;overflow-y:auto;}
.chat-session-item{padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);font-size:.78rem;color:var(--muted);transition:.15s;display:flex;align-items:center;gap:4px;}
.chat-session-item:hover,.chat-session-item.active{background:rgba(212,175,55,.06);color:var(--text);}
.cmsg .mbody code{background:#2a2a2a;padding:1px 5px;border-radius:4px;font-size:.8rem;color:#D4AF37;font-family:monospace;}
#chat-new-session-btn{margin:8px;padding:8px;font-size:.8rem;background:linear-gradient(135deg,var(--accent),#b8962e);color:#000;border:none;border-radius:8px;cursor:pointer;width:calc(100% - 16px);font-weight:600;transition:all .2s;}
#chat-new-session-btn:hover{box-shadow:0 2px 10px rgba(212,175,55,.3);}
#chat-main{flex:1;display:flex;flex-direction:column;background:linear-gradient(180deg,#0a0a0a,#050505);}
#chat-topbar{padding:10px 14px;background:linear-gradient(180deg,var(--card),#151515);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-shrink:0;}
#chat-topbar .title{color:var(--accent);font-weight:600;font-size:.92rem;display:flex;align-items:center;gap:6px;}
#chat-limit{font-size:.74rem;color:var(--muted);}
#chat-messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;}
.cmsg{max-width:82%;padding:10px 14px;border-radius:14px;font-size:.86rem;line-height:1.55;word-break:break-word;}
.cmsg.bot{background:linear-gradient(135deg,#1a1200,#141000);border:1px solid rgba(74,56,0,.6);color:var(--text);align-self:flex-start;border-radius:4px 14px 14px 14px;box-shadow:0 2px 8px rgba(0,0,0,.3);}
.cmsg.user{background:linear-gradient(135deg,#1e1e1e,#181818);border:1px solid #333;color:var(--text);align-self:flex-end;border-radius:14px 4px 14px 14px;}
.cmsg .msender{font-size:.68rem;color:var(--accent);margin-bottom:4px;font-weight:700;letter-spacing:.4px;display:flex;align-items:center;gap:4px;}
.cmsg.user .msender{color:var(--muted);}
.cmsg .mbody{white-space:pre-wrap;}
.chat-typing{color:var(--muted);font-size:.8rem;padding:4px 8px;font-style:italic;align-self:flex-start;}
#chat-input-row{display:flex;gap:8px;padding:12px;border-top:1px solid var(--border);background:linear-gradient(180deg,var(--card),#151515);flex-shrink:0;}
#chat-input{flex:1;resize:none;height:46px;padding:10px 12px;font-size:.87rem;border-radius:10px;background:#222;border-color:#444;}
#chat-input:focus{border-color:var(--accent);}
#chat-send{width:auto;margin-top:0;padding:10px 18px;flex-shrink:0;font-size:.87rem;}
</style>
</head>
<body>
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
  <symbol id="i-send" viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></symbol>
  <symbol id="i-lightning" viewBox="0 0 24 24"><path d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42A6.92 6.92 0 0119 12c0 3.87-3.13 7-7 7A6.995 6.995 0 017.58 5.58L6.17 4.17A8.932 8.932 0 003 12a9 9 0 0018 0c0-2.74-1.23-5.18-3.17-6.83z"/></symbol>
</svg>

<div id="toasts"></div>
<div id="upd">Update available! <a id="udl" href="#" target="_blank">Download</a></div>

<!-- ════ SIDEBAR ════════════════════════════════════════════════ -->
<div id="sb">
  <h2>
    <svg class="icon"><use href="#i-lightning"/></svg>
    TraderMoney
    <span id="lbadge" class="lbadge li">FREE</span>
    <small style="color:var(--muted);font-size:.58rem;margin-left:4px;">v2.2.0</small>
  </h2>
  <label>License Key</label>
  <input type="password" id="lickey" placeholder="Paste Gumroad key">
  <button onclick="validateLicense()" style="margin-top:4px;font-size:.8rem;">
    <svg class="icon"><use href="#i-key"/></svg> Validate
  </button>
  <p style="font-size:.67rem;color:var(--muted);margin:3px 0 0;">
    <a href="https://shafayrich.gumroad.com/l/ykaoov" style="color:var(--accent)">Buy license</a>
  </p>
  <div id="free-notice" class="free-notice">
    Free tier: Alpaca paper only · Signal-Only · 1 ticker · Core indicators · AI: 5/day<br>
    <b>License session-only – re-enter each restart.</b>
  </div>

  <hr>
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

  <details id="thesis-builder" style="margin-top:10px;">
    <summary style="cursor:pointer;color:var(--accent);font-size:.82rem;font-weight:bold;">🧪 Thesis Builder</summary>
    <div style="margin-top:6px;">
      <label style="font-size:.7rem;">Thesis Name</label>
      <input id="thesis-name" placeholder="e.g., Momentum RSI" style="font-size:.78rem;">
      <label style="font-size:.7rem;margin-top:6px;">RSI Period</label>
      <input id="tp-rsi-period" type="number" value="14" min="2" max="50">
      <label style="font-size:.7rem;">RSI Oversold</label>
      <input id="tp-rsi-os" type="number" value="30" min="1" max="50">
      <label style="font-size:.7rem;">RSI Overbought</label>
      <input id="tp-rsi-ob" type="number" value="70" min="50" max="100">
      <label style="font-size:.7rem;">MACD Fast/Slow/Signal</label>
      <div class="r2"><input id="tp-macd-fast" type="number" value="12"><input id="tp-macd-slow" type="number" value="26"><input id="tp-macd-sig" type="number" value="9"></div>
      <label style="font-size:.7rem;">BB Period / Std</label>
      <div class="r2"><input id="tp-bb-per" type="number" value="20"><input id="tp-bb-std" type="number" value="2.0" step="0.1"></div>
      <label style="font-size:.7rem;">ADX Period / Threshold</label>
      <div class="r2"><input id="tp-adx-per" type="number" value="14"><input id="tp-adx-thr" type="number" value="20"></div>
      <label style="font-size:.7rem;">Volume Period / Threshold</label>
      <div class="r2"><input id="tp-vol-per" type="number" value="20"><input id="tp-vol-thr" type="number" value="1.5" step="0.1"></div>
      <label style="font-size:.7rem;">SuperTrend Period / Mult</label>
      <div class="r2"><input id="tp-st-per" type="number" value="10"><input id="tp-st-mult" type="number" value="3.0" step="0.1"></div>
      <label style="font-size:.7rem;">Stoch K / D</label>
      <div class="r2"><input id="tp-stoch-k" type="number" value="14"><input id="tp-stoch-d" type="number" value="3"></div>
      <label style="font-size:.7rem;">ATR Period</label>
      <input id="tp-atr-per" type="number" value="14">
      <div style="display:flex;gap:5px;margin-top:6px;">
        <button onclick="saveThesis()" style="padding:6px;font-size:.75rem;">💾 Save</button>
        <button onclick="applyThesis()" style="padding:6px;font-size:.75rem;">▶ Apply</button>
      </div>
      <div id="saved-theses" style="margin-top:6px;"></div>
      <button class="ghost" onclick="loadSavedTheses()" style="font-size:.72rem;padding:5px;">🔄 Refresh List</button>
    </div>
  </details>

  <button onclick="saveConfig()"><svg class="icon"><use href="#i-save"/></svg> Save Config</button>
  <button class="ghost" onclick="refreshTickers()"><svg class="icon"><use href="#i-refresh"/></svg> Refresh Tickers</button>
  <button id="startBtn" onclick="startBot()"><svg class="icon"><use href="#i-start"/></svg> Start Bot</button>
  <button class="ghost" id="stopBtn" onclick="stopBot()"><svg class="icon"><use href="#i-stop"/></svg> Stop Bot</button>
  <button class="danger" onclick="killSwitch()"><svg class="icon"><use href="#i-warn"/></svg> Kill Switch</button>
  <button class="ghost" style="margin-top:5px;" onclick="resetDef()"><svg class="icon"><use href="#i-refresh"/></svg> Reset Defaults</button>

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

  <button class="ghost" style="margin-top:14px;" onclick="checkUpdate()"><svg class="icon"><use href="#i-update"/></svg> Check Updates</button>
  <button class="ghost" style="margin-top:6px;" onclick="runBT()"><svg class="icon"><use href="#i-backtest"/></svg> Backtest All</button>
  <div style="margin-top:7px;font-size:.74rem;color:var(--muted);">
    Days: <input type="number" id="btDays" value="5" min="1" max="365" class="bt-days-input">
  </div>
</div>

<!-- ════ MAIN ════════════════════════════════════════════════════ -->
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

  <!-- Charts tab -->
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

  <!-- Signals tab -->
  <div id="tab-signals" class="tab">
    <div id="siglist" style="overflow:auto;flex:1;"></div>
    <div id="sigempty" class="empty-placeholder" style="display:none;">No signals yet.</div>
  </div>

  <!-- History tab -->
  <div id="tab-history" class="tab">
    <div id="histlist" style="overflow:auto;flex:1;"></div>
    <div id="hstempty" class="empty-placeholder" style="display:none;">No orders yet.</div>
  </div>

  <!-- Backtest tab -->
  <div id="tab-backtest" class="tab">
    <div class="btp">
      <div style="display:flex;gap:7px;padding:10px;flex-wrap:wrap;">
        <button class="ghost" style="width:auto;padding:9px 16px;" onclick="runBT()"><svg class="icon"><use href="#i-backtest"/></svg> Run Backtest</button>
        <button class="ghost" style="width:auto;padding:9px 16px;" id="mc-btn" onclick="runMC()" disabled>Monte Carlo</button>
        <button class="ghost" style="width:auto;padding:9px 14px;" id="csv-btn" onclick="exportCSV()" disabled><svg class="icon"><use href="#i-export"/></svg> CSV</button>
        <button class="ghost" style="width:auto;padding:9px 14px;" id="pdf-btn" onclick="exportPDF()" disabled><svg class="icon"><use href="#i-export"/></svg> PDF</button>
        <button class="ghost" style="width:auto;padding:9px 14px;" id="tune-btn" onclick="autoTune()" disabled><svg class="icon"><use href="#i-robot"/></svg> AI Auto-Tune</button>
      </div>
      <div id="btres" class="btr"><p class="ph">Click <b>Run Backtest</b> to begin.</p></div>
    </div>
  </div>

  <!-- Analysis tab -->
  <div id="tab-analysis" class="tab">
    <div style="padding:10px;flex-shrink:0;">
      <button class="ghost" style="width:auto;padding:8px 16px;" onclick="loadCorr()"><svg class="icon"><use href="#i-refresh"/></svg> Refresh Correlation Matrix (30 days)</button>
    </div>
    <div style="overflow:auto;flex:1;padding:10px;" id="corr-content">
      <p class="ph">Click Refresh to load the correlation matrix for your tickers.</p>
    </div>
  </div>

  <!-- Help tab -->
  <div id="tab-help" class="tab">
    <div class="hb">
      <h3>TraderMoney v2.2.0 – Complete Help Guide</h3>
      <p style="font-size:.82rem;color:var(--muted);margin-top:-4px;">Your desktop algorithmic trading terminal. All features documented below.</p>

      <details>
        <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:.92rem;">Getting Started Guide</summary>
        <div style="padding:8px 0;">
          <h4>First Run</h4>
          <ol style="font-size:.82rem;line-height:1.7;">
            <li>Enter optional license key from Gumroad (otherwise works in Free mode)</li>
            <li>Select your preferred <b>Broker</b> (Alpaca works out-of-box on free tier)</li>
            <li>Enter broker API credentials in the sidebar fields</li>
            <li>Set your <b>Tickers</b> (e.g. AAPL, TSLA, BTC/USD)</li>
            <li>Choose a <b>Timeframe</b> for analysis (1m/5m/15m/30m/1h/1d)</li>
            <li>Enable the indicators you want to use (more = higher confidence)</li>
            <li>Click <b>Save Config</b> to persist settings</li>
            <li>Click <b>Start Bot</b> to begin analyzing markets</li>
            <li>View signals in the Signals tab, charts in Charts tab</li>
          </ol>
          <h4>Auto Trading</h4>
          <p style="font-size:.82rem;">Set Mode to "Auto Trade" (Pro only) to automatically execute trades when signals fire. Configure position sizing via quantity field or per-ticker quantity in ticker format (e.g. AAPL:10).</p>
        </div>
      </details>

      <hr>

      <div class="istat">
        <h4>Indicator Win Rate Progression</h4>
        <table class="bttbl">
          <tr><th>Indicators Active</th><th>Approx. Win Rate</th><th>Confidence</th></tr>
          <tr><td>Pure EMA Crossover</td><td>~32%</td><td>50%</td></tr>
          <tr><td>+ RSI (14)</td><td>~40%</td><td>+5%</td></tr>
          <tr><td>+ MACD</td><td>~45%</td><td>+5%</td></tr>
          <tr><td>+ VWAP</td><td>~48%</td><td>+5%</td></tr>
          <tr><td>+ Bollinger Bands</td><td>~50%</td><td>+5%</td></tr>
          <tr><td>+ ADX &ge;20</td><td>~55%</td><td>+5%</td></tr>
          <tr><td>+ Volume &ge;1.5x avg</td><td>~58%</td><td>+6%</td></tr>
          <tr><td>+ SuperTrend</td><td>~62%</td><td>+8%</td></tr>
          <tr><td>+ Stochastic</td><td>~65%</td><td>+5%</td></tr>
          <tr><td>+ ATR Stops</td><td>~65% (risk mgmt)</td><td>+4%</td></tr>
        </table>
        <p style="font-size:.75rem;color:var(--muted);margin-top:6px;">Customize indicator parameters in the Thesis Builder section of the sidebar.</p>
      </div>

      <details>
        <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:.92rem;">Indicator Reference</summary>
        <div style="padding:8px 0;">
          <h4>EMA (Exponential Moving Average) Crossover</h4>
          <p><b>What it does:</b> Tracks two EMAs (fast and slow). A buy signal triggers when the fast EMA crosses above the slow EMA. A sell signal triggers when the fast EMA crosses below the slow EMA.</p>
          <p><b>Best for:</b> Trend identification. Works well in trending markets.</p>
          <p><b>Parameters:</b> Fast period (default 9), Slow period (default 50).</p>

          <h4>RSI (Relative Strength Index)</h4>
          <p><b>What it does:</b> Measures price momentum on a scale of 0-100. Values below oversold threshold suggest a security is oversold (buy opportunity). Values above overbought threshold suggest overbought (sell opportunity).</p>
          <p><b>Best for:</b> Overbought/oversold detection, divergence spotting.</p>
          <p><b>Customizable:</b> Period (default 14), Oversold threshold (default 30), Overbought threshold (default 70).</p>

          <h4>MACD (Moving Average Convergence Divergence)</h4>
          <p><b>What it does:</b> Shows the relationship between two EMAs. A buy signal occurs when the MACD line crosses above the signal line. A sell signal occurs when it crosses below.</p>
          <p><b>Best for:</b> Trend direction and momentum confirmation.</p>
          <p><b>Customizable:</b> Fast period (default 12), Slow period (default 26), Signal period (default 9).</p>

          <h4>VWAP (Volume-Weighted Average Price)</h4>
          <p><b>What it does:</b> Represents the average price weighted by volume. Price above VWAP = bullish, below = bearish.</p>
          <p><b>Best for:</b> Intraday trend confirmation, institutional order flow tracking.</p>

          <h4>Bollinger Bands</h4>
          <p><b>What it does:</b> Plots a moving average with upper/lower bands at N standard deviations. Price touching lower band = oversold, upper band = overbought.</p>
          <p><b>Best for:</b> Volatility measurement, mean reversion strategies.</p>
          <p><b>Customizable:</b> Period (default 20), Standard deviations (default 2.0).</p>

          <h4>ADX (Average Directional Index)</h4>
          <p><b>What it does:</b> Measures trend strength regardless of direction. Values above threshold = strong trend. Values below = ranging market.</p>
          <p><b>Best for:</b> Filtering out false signals during range-bound markets.</p>
          <p><b>Customizable:</b> Period (default 14), Threshold (default 20).</p>

          <h4>Volume Confirmation</h4>
          <p><b>What it does:</b> Compares current volume to the average volume. Spikes above threshold confirm price move strength.</p>
          <p><b>Best for:</b> Validating breakouts and reversals.</p>
          <p><b>Customizable:</b> Period (default 20), Threshold multiplier (default 1.5x).</p>

          <h4>SuperTrend</h4>
          <p><b>What it does:</b> Plots a trend-following indicator above/below price. A bullish trend flips when price closes below the SuperTrend line.</p>
          <p><b>Best for:</b> Trend following with dynamic support/resistance.</p>
          <p><b>Customizable:</b> ATR Period (default 10), Multiplier (default 3.0).</p>

          <h4>Stochastic Oscillator</h4>
          <p><b>What it does:</b> Compares closing price to the price range over N periods. K line crossing above D line = bullish momentum.</p>
          <p><b>Best for:</b> Identifying momentum shifts and overbought/oversold conditions.</p>
          <p><b>Customizable:</b> K period (default 14), D period (default 3).</p>

          <h4>ATR (Average True Range) Stops</h4>
          <p><b>What it does:</b> Uses ATR to set dynamic stop-loss and take-profit levels. Stop = entry price - (ATR * stop_mult). TP = entry price + (ATR * tp_mult).</p>
          <p><b>Best for:</b> Adaptive risk management that accounts for volatility.</p>
          <p><b>Customizable:</b> ATR Period (default 14), Stop multiplier (default 2.0), TP multiplier (default 3.0).</p>
        </div>
      </details>

      <hr>

      <details>
        <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:.92rem;">Broker Setup Guides</summary>
        <div style="padding:8px 0;">
          <h4>Alpaca (Free Tier Compatible)</h4>
          <ol style="font-size:.82rem;">
            <li>Sign up at <a href="https://alpaca.markets">alpaca.markets</a></li>
            <li>Go to Dashboard &gt; API Keys &gt; Generate Key</li>
            <li>Copy API Key ID and Secret Key into TraderMoney</li>
            <li>Toggle Paper Trading for risk-free testing (recommended)</li>
            <li>Paper account starts with $100,000 virtual USD</li>
          </ol>

          <h4>Interactive Brokers (IBKR)</h4>
          <ol style="font-size:.82rem;">
            <li>Install TWS or IB Gateway from <a href="https://interactivebrokers.com">interactivebrokers.com</a></li>
            <li>Launch TWS/Gateway and enable API connections: Edit &gt; Global Configuration &gt; API &gt; Settings &gt; Enable "Enable ActiveX and Socket Clients"</li>
            <li>Set port: 7497 (TWS paper), 7496 (TWS live), 4002 (Gateway paper), 4001 (Gateway live)</li>
            <li>In TraderMoney: Host = 127.0.0.1, Port = your chosen port, Client ID = any number (e.g. 1)</li>
            <li>Ensure TWS/Gateway is running before connecting</li>
          </ol>

          <h4>Tradier</h4>
          <ol style="font-size:.82rem;">
            <li>Sign up at <a href="https://developer.tradier.com">developer.tradier.com</a></li>
            <li>Navigate to "Access Tokens" and generate a new token</li>
            <li>Find your Account ID in Account Settings</li>
            <li>Enter Access Token and Account ID in TraderMoney</li>
          </ol>

          <h4>Binance</h4>
          <ol style="font-size:.82rem;">
            <li>Sign up at <a href="https://binance.com">binance.com</a></li>
            <li>Go to API Management and create a new API key</li>
            <li>Enable spot trading permissions on the key</li>
            <li>Use Testnet keys from <a href="https://testnet.binance.vision">testnet.binance.vision</a> for paper trading</li>
            <li>Check "Testnet" in TraderMoney if using testnet keys</li>
            <li>Error -2015 typically means key/testnet mode mismatch</li>
          </ol>

          <h4>Bybit</h4>
          <ol style="font-size:.82rem;">
            <li>Sign up at <a href="https://bybit.com">bybit.com</a></li>
            <li>Go to API Management &gt; Create New API</li>
            <li>Enable "Spot" and "Wallet" permissions</li>
            <li>Use testnet at <a href="https://testnet.bybit.com">testnet.bybit.com</a> for paper trading</li>
            <li>Requires pybit v5+ (included in bundled version)</li>
          </ol>

          <h4>OKX</h4>
          <ol style="font-size:.82rem;">
            <li>Sign up at <a href="https://okx.com">okx.com</a></li>
            <li>Go to Trading &gt; API and create a new API key</li>
            <li>Enter API Key, Secret Key, and Passphrase</li>
            <li>Enable Demo trading mode for paper trading</li>
          </ol>
        </div>
      </details>

      <hr>

      <details>
        <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:.92rem;">Backtesting &amp; Metrics Guide</summary>
        <div style="padding:8px 0;">
          <h4>How Backtesting Works</h4>
          <p style="font-size:.82rem;">The backtester downloads historical data via Yahoo Finance, applies your selected indicator configuration, generates signals, and simulates trades using your chosen settings. Results show detailed per-ticker performance.</p>
          <h4>Key Metrics Explained</h4>
          <ul style="font-size:.82rem;">
            <li><b>Win Rate:</b> Percentage of profitable trades out of total closed trades.</li>
            <li><b>Profit Factor:</b> Gross profit / Gross loss. Above 1.5 = good, above 2.0 = excellent.</li>
            <li><b>Sharpe Ratio:</b> Risk-adjusted return. Above 1.0 = good, above 2.0 = excellent.</li>
            <li><b>Max Drawdown:</b> Largest peak-to-trough decline. Lower is better for risk management.</li>
            <li><b>ROI:</b> Total return on investment as percentage.</li>
            <li><b>Expectancy:</b> Average expected P&L per trade. Should be positive.</li>
            <li><b>Monte Carlo:</b> 1000 randomized simulations to estimate probability of profit.</li>
          </ul>
          <h4>Tips</h4>
          <ul style="font-size:.82rem;">
            <li>Use at least 30 days of data for meaningful results</li>
            <li>Test across different market conditions (bull/bear/sideways)</li>
            <li>Use AI Auto-Tune to optimize indicator combinations</li>
            <li>Export results to CSV/PDF for further analysis</li>
          </ul>
        </div>
      </details>

      <hr>

      <details>
        <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:.92rem;">Thesis Builder Guide</summary>
        <div style="padding:8px 0;">
          <p style="font-size:.82rem;">The Thesis Builder lets you create custom trading strategies by customizing indicator parameters.</p>
          <ol style="font-size:.82rem;">
            <li>Open the <b>Thesis Builder</b> section in the sidebar (under Indicators)</li>
            <li>Adjust any parameter (RSI period, MACD speeds, BB width, etc.)</li>
            <li>Give your thesis a name and click <b>Save</b> to store it</li>
            <li>Click <b>Apply</b> to activate the thesis parameters</li>
            <li>Saved theses appear in a list below the builder - click to load or delete</li>
            <li>Run a backtest to compare thesis performance</li>
          </ol>
          <p style="font-size:.82rem;"><b>Example:</b> Create a thesis named "Fast Momentum" with RSI period=7, MACD fast=6/slow=13/signal=5, BB period=10. This creates a faster-reacting strategy for shorter timeframes.</p>
        </div>
      </details>

      <hr>

      <details>
        <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:.92rem;">FAQ &amp; Troubleshooting</summary>
        <div style="padding:8px 0;">
          <h4>Q: Why are no signals appearing?</h4>
          <p>A: Check that: (1) your broker is connected, (2) tickers are valid, (3) the bot is started, (4) market is open (or use crypto which is 24/7).</p>
          <h4>Q: Why is the backtest showing no trades?</h4>
          <p>A: Try a longer date range (30+ days). The strategy may not generate signals in short windows. Check that your indicators are enabled.</p>
          <h4>Q: License validation fails?</h4>
          <p>A: Ensure you have internet connectivity. Copy-paste the full license key exactly. License is session-only (re-enter each restart).</p>
          <h4>Q: PDF/CSV export not working?</h4>
          <p>A: Files are saved to your ~/Downloads folder. Check there for tradermoney_backtest_*.csv/pdf files.</p>
          <h4>Q: AI Chat not responding?</h4>
          <p>A: An OpenRouter API key must be configured. Free tier allows 5 messages/day. Pro has unlimited access.</p>
          <h4>Q: Can I run this headless (no GUI window)?</h4>
          <p>A: Yes! The Flask server runs on http://localhost:5050. You can access the full UI from any browser on your network.</p>
          <h4>Q: Windows build crashes on launch?</h4>
          <p>A: Ensure you have the latest Visual C++ Redistributable installed. Run the .exe from command prompt to see error messages.</p>
        </div>
      </details>

      <hr>

      <h4>Keyboard Shortcuts</h4>
      <table class="bttbl">
        <tr><th>Shortcut</th><th>Action</th></tr>
        <tr><td>Ctrl + Space</td><td>Start / Stop Bot</td></tr>
        <tr><td>Ctrl + K</td><td>Focus Ticker Input</td></tr>
        <tr><td>Ctrl + B</td><td>Run Backtest</td></tr>
        <tr><td>Ctrl + Shift + B</td><td>Switch to Backtest Tab</td></tr>
        <tr><td>Ctrl + 1–7</td><td>Switch Tabs</td></tr>
        <tr><td>Ctrl + S</td><td>Save Config</td></tr>
        <tr><td>Escape</td><td>Close any open modal</td></tr>
      </table>

      <hr>

      <h4>Broker Configuration</h4>
      <ul>
        <li><b>Alpaca:</b> Get API Key + Secret from alpaca.markets. Enable Paper Trading. Works on free tier.</li>
        <li><b>IBKR:</b> TWS or IB Gateway must be running. Ports: 7497 (TWS paper), 7496 (live), 4002 (GW paper), 4001 (GW live). Enable API in TWS: Edit &gt; Global Configuration &gt; API &gt; Settings.</li>
        <li><b>Tradier:</b> Access Token + Account ID from developer.tradier.com.</li>
        <li><b>Binance:</b> API Key + Secret from binance.com. Testnet keys only work with Testnet=True. Error -2015 = key/testnet mismatch.</li>
        <li><b>Bybit:</b> API Key + Secret from bybit.com. Requires pybit v5+.</li>
        <li><b>OKX:</b> API Key + Secret + Passphrase from okx.com. Demo available.</li>
      </ul>

      <hr>

      <h4>License &amp; Tiers</h4>
      <ul>
        <li><b>Free:</b> Alpaca paper, Signal-Only, 1 ticker, core indicators (RSI/MACD/VWAP/Bollinger), 5 AI messages/day.</li>
        <li><b>Pro:</b> All 6 brokers, Auto Trade, all 9 indicators, brackets, ATR stops, Telegram, unlimited AI, direction control, multiple tickers, AI Auto-Tune, Custom Thesis Builder.</li>
        <li>Purchase at <a href="https://shafayrich.gumroad.com/l/ykaoov">shafayrich.gumroad.com</a></li>
        <li>License is session-only – re-enter each restart.</li>
      </ul>

      <hr>

      <h4>Ticker Format</h4>
      <p>Comma-separated, optional qty after colon: <code>AAPL:10, TSLA:5, BTC/USD:0.01</code></p>

      <hr>

      <h4>Backtesting Notes</h4>
      <ul>
        <li>Correctly tracks equity through LONG &rarr; SHORT transitions. No more double-counted P&L.</li>
        <li>Short positions: principal correctly recovered on close (entry_price x shares + profit/loss).</li>
        <li>Open positions at end of data are marked to market using the last available signal price.</li>
        <li>Monte Carlo uses the same corrected simulation logic.</li>
        <li>PDF/CSV exports are saved to ~/Downloads folder.</li>
        <li>Each trade now shows entry/exit reason and indicator snapshot.</li>
      </ul>

      <hr>

      <h4>Strategy Presets</h4>
      <table class="bttbl">
        <tr><th>Preset</th><th>Timeframe</th><th>EMAs</th><th>Best For</th></tr>
        <tr><td>Scalping</td><td>1m</td><td>9/50</td><td>Quick intraday trades</td></tr>
        <tr><td>Swing</td><td>15m</td><td>20/50</td><td>Multi-hour swing trades</td></tr>
        <tr><td>Breakout</td><td>5m</td><td>9/50</td><td>Volatility breakouts</td></tr>
      </table>

      <hr>

      <h4>Risk Management</h4>
      <ul>
        <li><b>Bracket Orders:</b> Auto place SL and TP with every order.</li>
        <li><b>ATR Stops:</b> Dynamic stops: 2x ATR stop, 3x ATR take-profit (customizable in Thesis Builder).</li>
        <li><b>Kill Switch:</b> Instantly close all positions. No confirmation.</li>
        <li><b>SL/TP Watchdog:</b> Monitors non-Alpaca positions every 2 seconds.</li>
      </ul>

      <hr>

      <h4>Telegram Alerts (Pro)</h4>
      <ul>
        <li>Create bot via @BotFather. Get Chat ID via @userinfobot.</li>
        <li>Receive: BUY/SELL signals, executed trades, stop/take-profit triggers.</li>
      </ul>

      <hr>

      <h4>AI Chat</h4>
      <ul>
        <li>Powered by OpenRouter. Models: Gemini 2.5 Flash, DeepSeek, Llama 4 Maverick (auto-fallback).</li>
        <li>Sessions can be renamed and deleted via hover actions on session list.</li>
        <li>Bot messages support bold, italic, code formatting.</li>
        <li>Free: 5 messages/day. Pro: unlimited.</li>
        <li>Offline fallback returns useful built-in answers when API is unavailable.</li>
      </ul>

      <hr>

      <h4>Glossary</h4>
      <table class="bttbl">
        <tr><th>Term</th><th>Definition</th></tr>
        <tr><td>Equity</td><td>Total account value (cash + positions).</td></tr>
        <tr><td>Buying Power</td><td>Available funds for new trades.</td></tr>
        <tr><td>P&L</td><td>Profit and Loss (realized + unrealized).</td></tr>
        <tr><td>Drawdown</td><td>Peak-to-trough decline in account value.</td></tr>
        <tr><td>Sharpe Ratio</td><td>Return per unit of risk (higher = better).</td></tr>
        <tr><td>Signal-Only</td><td>Mode where bot shows signals but does not trade.</td></tr>
        <tr><td>Auto Trade</td><td>Mode where bot automatically executes orders.</td></tr>
        <tr><td>Bracket Order</td><td>Order with attached stop-loss and take-profit.</td></tr>
        <tr><td>Monte Carlo</td><td>Randomized simulation to estimate outcome probabilities.</td></tr>
      </table>

      <hr>

      <h4>Leaderboard</h4>
      <div id="leaderboard-wrap"><p style="font-size:.8rem;color:var(--muted)">Run a backtest to appear.</p></div>
    </div>
  </div>

  <!-- AI Chat tab -->
  <div id="tab-aichat" class="tab">
    <div id="aichat-wrap">
      <div id="chat-sessions-panel">
        <h3><svg class="icon"><use href="#i-chat"/></svg> Chats</h3>
        <div id="chat-sessions-list"></div>
        <button id="chat-new-session-btn" onclick="createNewSession()">+ New Chat</button>
      </div>
      <div id="chat-main">
        <div id="chat-topbar">
          <span class="title"><svg class="icon"><use href="#i-robot"/></svg> TraderBot AI</span>
          <span id="chat-limit"></span>
        </div>
        <div id="chat-messages"></div>
        <div id="chat-input-row">
          <textarea id="chat-input" placeholder="Ask about trading, indicators, platform usage..."></textarea>
          <button id="chat-send" onclick="sendChat()"><svg class="icon"><use href="#i-send"/></svg> Send</button>
        </div>
      </div>
    </div>
  </div>

  <div id="logbar"></div>
</div>

<!-- TradingView embedded chart -->
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
'use strict';
const $=id=>document.getElementById(id);
let cfg={},licValid=false,curSym='',allTickers=[],tvWidget=null,lastTvSymbol='';
let curSessionId=null,chatInited=false,botRunning=false,lastBTData=null;

function cs(raw){return raw.split(':')[0].trim().toUpperCase();}
function fmt(n,d=2){return Number(n).toLocaleString(undefined,{maximumFractionDigits:d});}
function toast(msg,type='info'){
  const t=document.createElement('div');t.className='toast '+type;t.textContent=msg;
  $('toasts').appendChild(t);setTimeout(()=>t.remove(),4200);
}
function gv(id,fb=''){const e=$(id);return e?e.value:fb;}
function gc(id){const e=$(id);return e?e.checked:false;}
function sv(id,v){const e=$(id);if(e)e.value=v;}
function sc(id,v){const e=$(id);if(e)e.checked=!!v;}
function lockCb(id,locked){
  const el=$(id);if(!el)return;el.disabled=locked;
  const lbl=el.closest('label');
  if(lbl){lbl.style.opacity=locked?'0.35':'1';lbl.style.pointerEvents=locked?'none':'';}
}

/* ── Tab switching ── */
const TABS=['charts','signals','history','backtest','analysis','help','aichat'];
function switchTab(name){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tbtn').forEach(x=>x.classList.remove('active'));
  const t=$('tab-'+name),b=document.querySelector(`[data-tab="${name}"]`);
  if(t)t.classList.add('active');if(b)b.classList.add('active');
  if(name==='aichat')initAIChat();
  if(name==='charts')setTimeout(()=>{if(tvWidget&&tvWidget.resize)tvWidget.resize();},80);
}
document.querySelectorAll('.tbtn').forEach(b=>{b.addEventListener('click',function(){switchTab(this.dataset.tab);});});

/* ── Session clock ── */
function updSess(){
  const n=new Date(),d=n.getUTCDay(),wk=d===0||d===6,h=n.getUTCHours()+n.getUTCMinutes()/60;
  const o=ok=>ok?'sd so':'sd sc';
  $('ds').className=o(!wk&&(h>=22||h<5));$('dt').className=o(!wk&&(h>=23||h<6));
  $('dl').className=o(!wk&&h>=8&&h<16.5);$('dn').className=o(!wk&&h>=13.5&&h<20);
  $('utc-clock').textContent='UTC: '+n.toISOString().slice(11,19);
}
setInterval(updSess,30000);updSess();

/* ── Broker credential helpers ── */
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
  addOpt('Alpaca','Alpaca');
  [{name:'Interactive Brokers'},{name:'Tradier'},{name:'Binance'},{name:'Bybit'},{name:'OKX'}].forEach(b=>{
    addOpt(b.name,licValid?b.name:`${b.name} [PRO]`);
  });
  if(!licValid&&cur!=='Alpaca'){sel.value='Alpaca';cfg.broker='Alpaca';}
  else{sel.value=cur;}
}
function onBrokerChange(){cfg.broker=$('broker').value;updateCreds();}
function toggleDefQty(){$('defqty-box').style.display=gc('udefqty')?'block':'none';}

/* ── Tier UI ── */
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

/* ── Config ── */
function buildCfg(){
  saveCurrentBrokerCreds();
  const ip=collectIndicatorParams();
  return{broker:cfg.broker||'Alpaca',tickers:gv('tickers','AAPL'),timeframe:gv('tf','1m'),
    emas:[parseInt(gv('emaf','9')),parseInt(gv('emas','50'))],
    quantity:parseInt(gv('qty','1'))||1,mode:gv('mode','signal'),direction:gv('dir','both'),
    use_default_qty:gc('udefqty'),use_bracket:gc('ubracket'),
    sl_percent:parseFloat(gv('slp','2')),tp_percent:parseFloat(gv('tpp','4')),
    use_atr_stops:gc('uatr'),telegram:{token:gv('tgt'),chat_id:gv('tgc')},
    use_rsi:gc('ursi'),use_macd:gc('umacd'),use_vwap:gc('uvwap'),use_bollinger:gc('uboll'),
    use_adx:gc('uadx'),use_vol_confirm:gc('uvol'),use_supertrend:gc('ust'),
    use_stochastic:gc('ustoch'),news_sentiment:gc('unews'),
    license_key:gv('lickey',''),timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
    alpaca:cfg.alpaca||{},ibkr:cfg.ibkr||{},tradier:cfg.tradier||{},
    binance:cfg.binance||{},bybit:cfg.bybit||{},okx:cfg.okx||{},
    indicator_params:ip};
}
function collectIndicatorParams(){
  return{
    rsi_period:parseInt(gv('tp-rsi-period','14'))||14,
    rsi_oversold:parseInt(gv('tp-rsi-os','30'))||30,
    rsi_overbought:parseInt(gv('tp-rsi-ob','70'))||70,
    macd_fast:parseInt(gv('tp-macd-fast','12'))||12,
    macd_slow:parseInt(gv('tp-macd-slow','26'))||26,
    macd_signal:parseInt(gv('tp-macd-sig','9'))||9,
    bb_period:parseInt(gv('tp-bb-per','20'))||20,
    bb_std:parseFloat(gv('tp-bb-std','2'))||2,
    adx_period:parseInt(gv('tp-adx-per','14'))||14,
    adx_threshold:parseInt(gv('tp-adx-thr','20'))||20,
    vol_period:parseInt(gv('tp-vol-per','20'))||20,
    vol_threshold:parseFloat(gv('tp-vol-thr','1.5'))||1.5,
    supertrend_period:parseInt(gv('tp-st-per','10'))||10,
    supertrend_multiplier:parseFloat(gv('tp-st-mult','3'))||3,
    stoch_k_period:parseInt(gv('tp-stoch-k','14'))||14,
    stoch_d_period:parseInt(gv('tp-stoch-d','3'))||3,
    atr_period:parseInt(gv('tp-atr-per','14'))||14,
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
  updateCreds();
  const raw=(c.tickers||'AAPL').split(',').map(s=>s.trim()).filter(s=>s);
  if(raw.length){setTickers(raw);loadTradingViewChart(cs(raw[0]));}
}

/* ── TradingView Chart ── */
function loadTradingViewChart(symbol){
  if(tvWidget)try{tvWidget.remove();}catch(e){}
  lastTvSymbol=symbol;
  tvWidget=new TradingView.widget({
    container_id:'chart-c',symbol:symbol,interval:'1',timezone:'Etc/UTC',
    theme:'dark',style:'1',locale:'en',toolbar_bg:'#0c0c0c',
    enable_publishing:false,allow_symbol_change:true,autosize:true,studies:[],
    overrides:{
      "paneProperties.background":"#0c0c0c","paneProperties.backgroundType":"solid",
      "paneProperties.vertGridProperties.color":"#1a1a1a","paneProperties.horzGridProperties.color":"#1a1a1a",
      "mainSeriesProperties.candleStyle.upColor":"#D4AF37","mainSeriesProperties.candleStyle.downColor":"#B22222",
      "mainSeriesProperties.candleStyle.wickUpColor":"#D4AF37","mainSeriesProperties.candleStyle.wickDownColor":"#B22222",
      "mainSeriesProperties.candleStyle.borderUpColor":"#D4AF37","mainSeriesProperties.candleStyle.borderDownColor":"#B22222",
    }
  });
  curSym=symbol;
  setTimeout(()=>{if(tvWidget&&tvWidget.resize)tvWidget.resize();},200);
}

/* ── Ticker bar ── */
function setTickers(list){
  allTickers=list;const bar=$('tkbar');bar.innerHTML='';
  list.forEach(raw=>{
    const sym=cs(raw),btn=document.createElement('button');
    btn.className='tkbtn'+(sym===curSym?' active':'');btn.textContent=sym;
    btn.onclick=()=>{curSym=sym;updTk();if(lastTvSymbol!==sym)loadTradingViewChart(sym);};
    bar.appendChild(btn);
  });
}
function updTk(){document.querySelectorAll('.tkbtn').forEach(b=>b.classList.toggle('active',cs(b.textContent)===curSym));}
function refreshTickers(){
  fetch('/api/config').then(r=>r.json()).then(c=>{
    sv('tickers',c.tickers);
    const raw=c.tickers.split(',').map(s=>s.trim()).filter(s=>s);
    if(raw.length){setTickers(raw);loadTradingViewChart(cs(raw[0]));}
    toast('Tickers refreshed','success');
  });
}

/* ── Config load/save ── */
async function loadConfig(){
  try{
    const r=await fetch('/api/config');cfg=await r.json();
    await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})});
    initUI(cfg);
    if(cfg.license_key&&cfg.license_key.trim())await validateLicense(true);
    loadHistory();loadLeaderboard();
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

const DEF={broker:'Alpaca',tickers:'AAPL',mode:'signal',direction:'both',use_default_qty:true,quantity:1,emas:[9,50],use_bracket:false,sl_percent:2,tp_percent:4,timeframe:'1m',telegram:{},use_rsi:true,use_macd:true,use_vwap:true,use_bollinger:true,use_adx:true,use_vol_confirm:true,use_supertrend:true,use_stochastic:true,use_atr_stops:true,alpaca:{api_key:'',secret_key:'',paper:true},ibkr:{host:'',port:'',client_id:''},tradier:{access_token:'',account_id:'',sandbox:false},binance:{api_key:'',api_secret:'',testnet:true},bybit:{api_key:'',api_secret:'',testnet:true},okx:{api_key:'',api_secret:'',api_passphrase:'',demo:true}};
function resetDef(){cfg=JSON.parse(JSON.stringify(DEF));licValid=false;applyFreeTierUI();sv('lickey','');initUI(cfg);saveConfig();toast('Reset to factory defaults','success');}

/* ── Thesis Builder ── */
async function saveThesis(){
  const name=$('thesis-name').value.trim();if(!name){toast('Enter a thesis name','error');return;}
  const params=collectIndicatorParams();
  const r=await fetch('/api/thesis/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,params})});
  const d=await r.json();if(d.ok){toast('Thesis saved: '+name,'success');loadSavedTheses();}else toast(d.error||'Save failed','error');
}
async function applyThesis(){
  const sel=document.querySelector('#saved-theses select');
  const name=sel?sel.value:null;const manual=$('thesis-name').value.trim();
  let params=collectIndicatorParams();
  if(name&&!manual){
    const r=await fetch('/api/thesis/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    const d=await r.json();if(d.ok&&d.params)params=d.params;else{toast(d.error||'Apply failed','error');return;}
  }
  sv('tp-rsi-period',params.rsi_period||14);sv('tp-rsi-os',params.rsi_oversold||30);sv('tp-rsi-ob',params.rsi_overbought||70);
  sv('tp-macd-fast',params.macd_fast||12);sv('tp-macd-slow',params.macd_slow||26);sv('tp-macd-sig',params.macd_signal||9);
  sv('tp-bb-per',params.bb_period||20);sv('tp-bb-std',params.bb_std||2);
  sv('tp-adx-per',params.adx_period||14);sv('tp-adx-thr',params.adx_threshold||20);
  sv('tp-vol-per',params.vol_period||20);sv('tp-vol-thr',params.vol_threshold||1.5);
  sv('tp-st-per',params.supertrend_period||10);sv('tp-st-mult',params.supertrend_multiplier||3);
  sv('tp-stoch-k',params.stoch_k_period||14);sv('tp-stoch-d',params.stoch_d_period||3);
  sv('tp-atr-per',params.atr_period||14);
  toast('Thesis applied! Save config to persist.','success');
}
async function loadSavedTheses(){
  try{
    const d=await(await fetch('/api/thesis/list')).json();
    const list=d.theses||[];let html='<label style="font-size:.7rem;margin-top:6px;">Saved Theses</label>';
    if(list.length){
      html+='<select id="thesis-select" style="font-size:.76rem;">';
      list.forEach(t=>{html+=`<option value="${t.name}">${t.name}</option>`;});
      html+='</select><div style="display:flex;gap:5px;margin-top:4px;"><button onclick="applyThesis()" style="padding:4px;font-size:.7rem;">▶ Apply Selected</button><button onclick="deleteThesis()" style="padding:4px;font-size:.7rem;background:var(--danger);color:#fff;">🗑</button></div>';
    }else html+='<p style="font-size:.7rem;color:var(--muted)">No saved theses</p>';
    $('saved-theses').innerHTML=html;
  }catch(e){}
}
async function deleteThesis(){
  const sel=document.querySelector('#thesis-select');
  if(!sel||!sel.value)return;
  if(!confirm('Delete thesis "'+sel.value+'"?'))return;
  await fetch('/api/thesis/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel.value})});
  toast('Thesis deleted','success');loadSavedTheses();
}

/* ── Bot controls ── */
async function startBot(){
  const btn=$('startBtn');btn.textContent='Starting...';btn.disabled=true;
  cfg=buildCfg();
  if(!licValid){cfg.broker='Alpaca';cfg.mode='signal';cfg.direction='both';if(cfg.alpaca)cfg.alpaca.paper=true;['use_supertrend','use_stochastic','use_adx','use_vol_confirm','use_atr_stops','use_bracket'].forEach(k=>cfg[k]=false);cfg.tickers=cfg.tickers.split(',')[0].trim();}
  const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  const d=await r.json();
  btn.textContent='\u25B6 Start Bot';btn.disabled=false;
  toast(d.message,d.status==='ok'?'success':'error');
  if(d.status!=='ok'){$('bstatus').textContent=d.message;$('bstatus').className='err';}
  else{botRunning=true;}
}
async function stopBot(){
  const btn=$('stopBtn');btn.textContent='Stopping...';btn.disabled=true;
  await fetch('/api/stop',{method:'POST'});
  btn.textContent='\u25A0 Stop Bot';btn.disabled=false;
  botRunning=false;toast('Bot stopped','success');
}
async function killSwitch(){await fetch('/api/kill',{method:'POST'});botRunning=false;toast('Kill switch activated','error');}

async function validateLicense(silent=false){
  const key=gv('lickey').trim();if(!key){if(!silent)toast('Enter a license key','error');return;}
  const r=await fetch('/api/validate_license',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key})});
  const d=await r.json();
  if(d.valid){
    licValid=true;applyProUI();
    sv('mode',cfg.mode||'signal');sv('dir',cfg.direction||'both');
    sc('ubracket',!!cfg.use_bracket);sc('uatr',cfg.use_atr_stops!==false);
    sc('uadx',cfg.use_adx!==false);sc('uvol',cfg.use_vol_confirm!==false);
    sc('ust',cfg.use_supertrend!==false);sc('ustoch',cfg.use_stochastic!==false);
    updateCreds();if(!silent)toast('Pro unlocked for this session','success');
  }else{licValid=false;applyFreeTierUI();if(!silent)toast(d.message,'error');}
  updateBrokerOptions();
}

async function checkUpdate(){
  try{const d=await(await fetch('/api/update')).json();if(d.update_available){$('upd').style.display='block';$('udl').href=d.download_url;toast('Update available!','success');}else toast('Up to date!','success');}catch(e){}
}
setTimeout(checkUpdate,2500);

/* ── Broker status polling ── */
async function pollBS(){
  try{const d=await(await fetch('/api/broker_status')).json();const bs=$('bstatus');if(d.message){bs.textContent=d.message;bs.className=d.message.startsWith('Connected')?'ok':'err';}}catch(e){}
}
setInterval(pollBS,2500);pollBS();

/* ── Main status polling ── */
function renderSignals(sigs){
  const sl=$('siglist'),se=$('sigempty');sl.innerHTML='';se.style.display='none';let has=false;
  (sigs||[]).forEach(s=>{has=true;const div=document.createElement('div');div.className='sitem '+(s.signal==='BUY'?'buy':'sell');div.innerHTML=`<span>${s.time} <b>${s.signal}</b> ${s.symbol} @ $${s.price}</span><span>${s.rationale||''}</span>`;sl.appendChild(div);});
  if(!has)se.style.display='block';
}
function renderOrders(ords){
  const hl=$('histlist'),he=$('hstempty');hl.innerHTML='';he.style.display='none';let has=false;
  (ords||[]).forEach(o=>{has=true;const div=document.createElement('div');div.className='sitem '+(o.action==='BUY'?'buy':'sell');div.innerHTML=`<span>${o.time} <b>${o.action}</b> ${o.qty} ${o.symbol} @ $${o.price}</span>`;hl.appendChild(div);});
  if(!has)he.style.display='block';
}
async function pollStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    botRunning=d.running;
    $('v-eq').textContent='$'+fmt(d.equity);$('v-bp').textContent='$'+fmt(d.buying_power);
    const pct=d.equity?(d.pl/d.equity*100):0;
    $('v-pl').innerHTML=`<span style="color:${pct>=0?'var(--accent)':'var(--danger)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</span>`;
    $('v-pos').textContent=d.open_positions;
    renderSignals(d.signals);renderOrders(d.orders);
    $('logbar').innerHTML=(d.log||[]).join('<br>');
  }catch(e){}
}
setInterval(pollStatus,1500);

/* ── Presets ── */
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
  toast('Preset loaded – click Save to persist','success');
}

/* ── Backtest ── */
async function runBT(){
  const days=parseInt($('btDays').value)||5;
  toast('Running backtest...','info');
  $('btres').innerHTML='<p class="ph">Loading...</p>';
  switchTab('backtest');
  $('mc-btn').disabled=true;$('csv-btn').disabled=true;$('pdf-btn').disabled=true;$('tune-btn').disabled=true;
  try{
    const r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days})});
    const data=await r.json();lastBTData=data;
    if(data.error){toast('Backtest error: '+data.error,'error');$('btres').innerHTML=`<p class="ph" style="color:var(--danger)">${data.error}</p>`;return;}
    let html='';
    for(const sym in data.results){
      const info=data.results[sym];html+=`<h3 style="color:var(--accent)">${sym}</h3>`;
      if(info.error){html+=`<p style="color:var(--danger)">${info.error}</p>`;continue;}
      if(info.simulation){
        const sim=info.simulation;
        html+=`<div style="background:var(--card);padding:10px;border-radius:8px;margin-bottom:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:6px;font-size:.78rem;">
          <div><b>Initial</b><br>$${sim.initial_cash.toLocaleString()}</div>
          <div><b>Final</b><br>$${sim.final_cash.toFixed(2)}</div>
          <div><b>P&L</b><br><span style="color:${sim.total_pnl>=0?'var(--accent)':'var(--danger)'}">${sim.total_pnl>=0?'+':''}$${sim.total_pnl.toFixed(2)}</span></div>
          <div><b>ROI</b><br>${sim.roi}%</div>
          <div><b>Win Rate</b><br>${sim.win_rate}%</div>
          <div><b>Trades</b><br>${sim.total_trades}</div>
          <div><b>Profit Factor</b><br>${sim.profit_factor}</div>
          <div><b>Sharpe</b><br>${sim.sharpe_ratio}</div>
          <div><b>Max DD</b><br>${sim.max_drawdown_pct}%</div>
        </div>`;
        const exits=sim.trades.filter(t=>t.type==='exit');
        if(exits.length){
          html+=`<table class="bttbl"><tr><th>Entry</th><th>Exit</th><th>Sym</th><th>Side</th><th>Shares</th><th>Entry $</th><th>Exit $</th><th>P&L</th><th>Why Open</th><th>Why Close</th></tr>`;
          exits.forEach(t=>{html+=`<tr><td>${String(t.entry_time).slice(0,12)}</td><td>${String(t.exit_time).slice(0,12)}</td><td>${t.symbol||''}</td><td style="color:${t.side==='LONG'?'var(--accent)':'var(--danger)'}">${t.side}</td><td>${t.shares?t.shares.toFixed(2):''}</td><td>$${t.entry_price.toFixed(2)}</td><td>$${t.exit_price.toFixed(2)}</td><td style="color:${t.pnl>=0?'var(--accent)':'var(--danger)'}">${t.pnl>=0?'+':''}$${t.pnl.toFixed(2)}</td><td style="font-size:.7rem;max-width:120px;overflow:hidden;text-overflow:ellipsis;">${t.reason_open||''}</td><td style="font-size:.7rem;max-width:120px;overflow:hidden;text-overflow:ellipsis;">${t.reason_close||''}</td></tr>`;});
          html+=`</table>`;
        }
      }
      if(info.signals&&info.signals.length){
        html+=`<details><summary style="cursor:pointer;color:var(--muted);">Raw Signals (${info.signals.length})</summary><table class="bttbl"><tr><th>Time</th><th>Sig</th><th>Price</th><th>Conf</th><th>Reason</th><th>RSI</th><th>MACD</th><th>ADX</th></tr>`;
        info.signals.forEach(s=>{const ind=s.indicators||{};html+=`<tr><td>${s.time}</td><td class="${s.signal==='BUY'?'buy':'sell'}">${s.signal}</td><td>$${s.price}</td><td>${(s.confidence*100).toFixed(0)}%</td><td style="font-size:.7rem">${s.reason||''}</td><td>${ind.rsi||''}</td><td>${ind.macd||''}</td><td>${ind.adx||''}</td></tr>`;});
        html+=`</table></details>`;
      }
    }
    if(data.portfolio){
      const p=data.portfolio;
      html+=`<div style="background:var(--card);padding:12px;border-radius:8px;margin-top:12px;"><b style="color:var(--accent)">Portfolio</b><br>Init: $${p.initial_cash.toLocaleString()} | Final: $${p.final_cash.toFixed(2)} | P&L: <span style="color:${p.total_pnl>=0?'var(--accent)':'var(--danger)'}">${p.total_pnl>=0?'+':''}$${p.total_pnl.toFixed(2)}</span> | Trades: ${p.total_trades}</div>`;
    }
    $('btres').innerHTML=html||'<p class="ph">No results.</p>';
    $('mc-btn').disabled=false;$('csv-btn').disabled=false;$('pdf-btn').disabled=false;$('tune-btn').disabled=false;
    loadLeaderboard();
  }catch(e){toast('Backtest failed: '+e,'error');}
}

async function runMC(){
  toast('Running Monte Carlo (1000 sims)...','info');
  const r=await fetch('/api/backtest/montecarlo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days:parseInt($('btDays').value)||5})});
  const d=await r.json();
  if(d.error){toast(d.error,'error');return;}
  $('btres').innerHTML+=`<div style="background:var(--card);padding:12px;border-radius:8px;margin-top:12px;"><b style="color:var(--accent)">Monte Carlo (1000 runs)</b><br>Prob. Profit: <b>${d.prob_profit}%</b> | Best: +$${d.best} | Avg: $${d.average} | Worst: $${d.worst}</div>`;
}

function getAllExitTrades(){
  if(!lastBTData)return[];
  const trades=[];
  for(const sym in lastBTData.results){const sim=lastBTData.results[sym].simulation;if(sim)trades.push(...sim.trades.filter(t=>t.type==='exit'));}
  return trades;
}
async function exportCSV(){
  const trades=getAllExitTrades();if(!trades.length){toast('No trades to export','error');return;}
  const r=await fetch('/api/export/backtest/csv/file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trades})});
  const d=await r.json();
  if(d.path){toast('CSV saved to '+d.path,'success');}
  else if(d.error){toast(d.error,'error');}
}
async function exportPDF(){
  const trades=getAllExitTrades();if(!trades.length){toast('No trades to export','error');return;}
  const r=await fetch('/api/export/backtest/pdf/file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trades})});
  const d=await r.json();
  if(d.path){toast('PDF saved to '+d.path,'success');}
  else if(d.error){toast(d.error,'error');}
}
async function autoTune(){
  if(!lastBTData){toast('Run a backtest first','error');return;}
  let summary='';
  for(const sym in lastBTData.results){const sim=lastBTData.results[sym].simulation;if(sim)summary+=`${sym}: win_rate=${sim.win_rate}%, trades=${sim.total_trades}, pnl=$${sim.total_pnl} `;}
  const msg=`Based on this backtest (${summary}), suggest the best indicator combination and SL/TP settings for TraderMoney to improve performance.`;
  switchTab('aichat');await initAIChat();
  $('chat-input').value=msg;await sendChat();
}

/* ── Correlation Matrix ── */
async function loadCorr(){
  $('corr-content').innerHTML='<p class="ph">Loading...</p>';
  const d=await(await fetch('/api/correlation')).json();
  $('corr-content').innerHTML=d.html||'<p class="ph">No data</p>';
}

/* ── Leaderboard ── */
async function loadLeaderboard(){
  try{
    const d=await(await fetch('/api/leaderboard')).json();
    const lb=d.leaderboard||[];
    let html='<h4 style="color:var(--accent)">Leaderboard</h4>';
    if(!lb.length){html+='<p style="font-size:.8rem;color:var(--muted)">Run a backtest to appear.</p>';}
    else{
      html+='<table class="bttbl"><tr><th>Rank</th><th>ID</th><th>Win Rate</th><th>Signals</th><th>Last BT</th></tr>';
      lb.forEach((r,i)=>{html+=`<tr><td>${i+1}</td><td>${r.user_id}</td><td>${r.win_rate.toFixed(1)}%</td><td>${r.total_signals}</td><td>${r.last_backtest||''}</td></tr>`;});
      html+='</table>';
    }
    const wrap=$('leaderboard-wrap');if(wrap)wrap.innerHTML=html;
  }catch(e){}
}

/* ── Markdown renderer ── */
function renderMarkdown(text){
  let s=text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
    .replace(/\*(.+?)\*/g,'<i>$1</i>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\n/g,'<br>');
  return s;
}

/* ── AI Chat ── */
async function initAIChat(){
  if(chatInited)return;chatInited=true;
  await loadSessions();
  const data=await(await fetch('/api/chat/sessions')).json();
  if(data.sessions&&data.sessions.length>0)await loadSession(data.sessions[0].id);
  else await createNewSession();
  updateChatLimitInfo();
}
async function loadSessions(){
  try{const d=await(await fetch('/api/chat/sessions')).json();renderSessionList(d.sessions||[]);}catch(e){}
}
function renderSessionList(sessions){
  const list=$('chat-sessions-list');list.innerHTML='';
  sessions.forEach(s=>{
    const item=document.createElement('div');item.className='chat-session-item'+(s.id===curSessionId?' active':'');
    const titleSpan=document.createElement('span');titleSpan.textContent=s.title;titleSpan.style.flex='1';
    item.appendChild(titleSpan);
    const actions=document.createElement('span');actions.className='chat-actions';actions.style.display='none';actions.style.gap='4px';
    const renBtn=document.createElement('button');renBtn.innerHTML='✏️';renBtn.style.background='none';renBtn.style.border='none';renBtn.style.cursor='pointer';renBtn.style.padding='2px 4px';renBtn.style.fontSize='.7rem';renBtn.title='Rename';
    renBtn.onclick=async(e)=>{e.stopPropagation();const t=prompt('Session name:',s.title);if(t&&t.trim()){await fetch(`/api/chat/sessions/${s.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:t.trim()})});await loadSessions();}};
    const delBtn=document.createElement('button');delBtn.innerHTML='🗑️';delBtn.style.background='none';delBtn.style.border='none';delBtn.style.cursor='pointer';delBtn.style.padding='2px 4px';delBtn.style.fontSize='.7rem';delBtn.title='Delete';
    delBtn.onclick=async(e)=>{e.stopPropagation();if(!confirm('Delete this chat?'))return;await fetch(`/api/chat/sessions/${s.id}`,{method:'DELETE'});if(s.id===curSessionId){curSessionId=null;$('chat-messages').innerHTML='';}await loadSessions();};
    actions.appendChild(renBtn);actions.appendChild(delBtn);item.appendChild(actions);
    item.onmouseenter=()=>actions.style.display='inline-flex';item.onmouseleave=()=>actions.style.display='none';
    item.onclick=()=>loadSession(s.id);list.appendChild(item);
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
  const r=await fetch('/api/chat/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'New Chat'})});
  const data=await r.json();curSessionId=data.session_id;
  await loadSessions();$('chat-messages').innerHTML='';updateChatLimitInfo();
}
function updateChatLimitInfo(){
  const el=$('chat-limit');if(!el)return;
  el.textContent=licValid?'Pro – unlimited':'Free: 5 messages/day';
}
function addChatMsg(text,isUser){
  const msgs=$('chat-messages');
  const wrap=document.createElement('div');wrap.className='cmsg '+(isUser?'user':'bot');
  const sender=document.createElement('div');sender.className='msender';
  sender.innerHTML=isUser?'You':'<svg class="icon" style="width:12px;height:12px"><use href="#i-robot"/></svg>TraderBot';
  const body=document.createElement('div');body.className='mbody';
  if(isUser)body.textContent=text;else body.innerHTML=renderMarkdown(text);
  wrap.appendChild(sender);wrap.appendChild(body);msgs.appendChild(wrap);msgs.scrollTop=msgs.scrollHeight;
  return wrap;
}
async function sendChat(){
  const inputEl=$('chat-input');const msg=inputEl.value.trim();if(!msg)return;
  inputEl.value='';addChatMsg(msg,true);
  const typing=document.createElement('div');typing.className='chat-typing';
  typing.textContent='TraderBot is thinking...';$('chat-messages').appendChild(typing);
  $('chat-messages').scrollTop=$('chat-messages').scrollHeight;
  const sendBtn=$('chat-send');sendBtn.disabled=true;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,session_id:curSessionId})});
    const d=await r.json();typing.remove();
    addChatMsg(d.reply||'No response.',false);
    if(d.session_id&&d.session_id!==curSessionId){curSessionId=d.session_id;loadSessions();}
  }catch(e){typing.remove();addChatMsg('Connection error. Please try again.',false);}
  sendBtn.disabled=false;$('chat-messages').scrollTop=$('chat-messages').scrollHeight;
}
$('chat-input').addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}});

/* ── Keyboard Shortcuts ── */
document.addEventListener('keydown',e=>{
  const ctrl=e.ctrlKey||e.metaKey;
  const tag=e.target.tagName;
  const isInput=tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT';
  if(e.key==='Escape'){document.querySelectorAll('.toast').forEach(t=>t.remove());return;}
  if(isInput&&e.key==='Escape'){e.target.blur();return;}
  if(ctrl&&e.code==='Space'){e.preventDefault();if(botRunning)stopBot();else startBot();}
  if(isInput&&ctrl&&e.code==='Space'){e.preventDefault();if(botRunning)stopBot();else startBot();}
  if(ctrl&&e.key==='k'&&!isInput){e.preventDefault();$('tickers').focus();}
  if(ctrl&&!e.shiftKey&&e.key==='b'&&!isInput){e.preventDefault();runBT();}
  if(ctrl&&e.shiftKey&&e.key==='B'){e.preventDefault();switchTab('backtest');}
  if(ctrl&&e.key==='s'&&!isInput){e.preventDefault();saveConfig();}
  if(ctrl&&e.key>='1'&&e.key<='7'&&!isInput){e.preventDefault();switchTab(TABS[parseInt(e.key)-1]);}
});

/* ── Boot ── */
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
        "TraderMoney 2.2.0",
        "http://127.0.0.1:5050",
        width=1440,
        height=880,
        min_size=(980, 700),
    )
    webview.start()

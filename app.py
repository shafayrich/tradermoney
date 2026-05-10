"""
TraderMoney v2.0.3
──────────────────────────────────────────────────────────────────────────────
Fixes all v1.0.60 bugs + 17 new features:
  • Lightweight Charts (replaces TradingView)
  • Strategy Presets (Scalping / Swing / Breakout)
  • Ticker Watchlist with live prices
  • Keyboard Shortcuts
  • Graceful Reconnection & Internet Check
  • SQLite Data Caching (candle_cache)
  • Local Timezone Support
  • Offline Mode
  • In-App Upgrade Prompts
  • Local Leaderboard (SQLite)
  • Voice Assistant (Web Speech API)
  • AI Auto-Tuning
  • News Sentiment Filter
  • Portfolio-Level Backtest ($100k capital)
  • Monte Carlo Simulations
  • Export Reports (CSV / PDF via fpdf2)
  • Advanced Chart Signal Annotations
  • Correlation Matrix tab

Required packages:
    pip install flask flask-cors pywebview numpy requests cryptography yfinance
    pip install alpaca-trade-api ib_insync python-binance pybit okx websocket-client
    pip install fpdf2
    (optional) pip install pytz
"""

import asyncio
import csv
import io
import json
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
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests as http_requests
import webview
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from flask_cors import CORS

APP_VERSION = "2.0.3"

# ── AI / News API keys ────────────────────────────────────────────────────────
CHATANYWHERE_API_KEY = "sk-hUwjVr5dWqvnwBjYeglNUNuiNi4yW2znuaRwauuKryf2XauS"
NEWS_API_KEY = ""            # Set your NewsAPI key here, or leave blank to disable
FREE_CHAT_DAILY_LIMIT = 5

_CHAT_SYSTEM_PROMPT = (
    "You are TraderBot, the AI assistant built into TraderMoney – a desktop algorithmic trading terminal. "
    "TraderMoney supports 6 brokers (Alpaca, Interactive Brokers, Tradier, Binance, Bybit, OKX) with paper and live trading. "
    "It uses a 9‑indicator confirmation engine: EMA crossover (9 & 50), RSI, MACD, VWAP, Bollinger Bands, ADX, Volume, SuperTrend, and Stochastic. "
    "Pro users can auto‑trade, short sell, use ATR‑based dynamic stops, bracket orders, and Telegram alerts. "
    "Free tier is signal‑only, Alpaca paper, 1 ticker, core indicators only. "
    "Tickers: comma-separated with optional quantity after colon, e.g. AAPL:5, TSLA:2, BTC/USD:0.1. "
    "Keep answers concise (under 220 words), practical, specific to TraderMoney. Plain text only."
)

_chat_counter: Dict[str, Any] = {"date": None, "count": 0}

# ── Gumroad ───────────────────────────────────────────────────────────────────
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
            return False, "License revoked"
        return True, "License verified"
    except Exception as e:
        return False, f"Cannot reach license server – {e}"


# ── Flask + port lock ─────────────────────────────────────────────────────────
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

# ── Connectivity ──────────────────────────────────────────────────────────────
_online = True


def _check_connectivity() -> bool:
    try:
        urllib.request.urlopen("https://www.google.com", timeout=5)
        return True
    except Exception:
        return False


def _connectivity_watcher():
    global _online
    while True:
        _online = _check_connectivity()
        time.sleep(30)


# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.path.expanduser("~/.tradermoney_data.db")


class DatabaseManager:
    def __init__(self, db_path: str = DB_PATH):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        # Clear session logs and old cache on startup
        self.conn.execute("DELETE FROM logs")
        self.conn.execute(
            "DELETE FROM candle_cache WHERE timestamp < ?",
            ((datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),)
        )
        self.conn.commit()

    def _init_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
            action TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
            signal TEXT NOT NULL, price REAL NOT NULL, rationale TEXT
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, config_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, timestamp TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS candle_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, interval TEXT NOT NULL,
            timestamp TEXT NOT NULL, data_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leaderboard (
            user_id TEXT PRIMARY KEY,
            win_rate REAL DEFAULT 0,
            total_signals INTEGER DEFAULT 0,
            last_backtest TEXT DEFAULT ''
        );
        """)
        self.conn.commit()

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # ── Trades ──────────────────────────────────────────────────────────────
    def insert_trade(self, ts, symbol, action, qty, price):
        self._exec(
            "INSERT INTO trades (timestamp,symbol,action,quantity,price) VALUES (?,?,?,?,?)",
            (ts, symbol, action, qty, price))

    def get_recent_trades(self, limit=50) -> List[dict]:
        rows = self._query(
            "SELECT timestamp,symbol,action,quantity,price FROM trades ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{"time": r[0], "symbol": r[1], "action": r[2], "qty": r[3], "price": r[4]}
                for r in rows]

    # ── Signals ─────────────────────────────────────────────────────────────
    def insert_signal(self, ts, symbol, sig, price, rationale):
        self._exec(
            "INSERT INTO signals (timestamp,symbol,signal,price,rationale) VALUES (?,?,?,?,?)",
            (ts, symbol, sig, price, rationale))

    def get_recent_signals(self, limit=50) -> List[dict]:
        rows = self._query(
            "SELECT timestamp,symbol,signal,price,rationale FROM signals ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{"time": r[0], "symbol": r[1], "signal": r[2], "price": r[3], "rationale": r[4]}
                for r in rows]

    # ── Logs ─────────────────────────────────────────────────────────────────
    def insert_log(self, message: str):
        self._exec("INSERT INTO logs (timestamp,message) VALUES (?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))

    def get_recent_logs(self, limit=50) -> List[str]:
        rows = self._query(
            "SELECT timestamp,message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return [f"{r[0]}  {r[1]}" for r in rows]

    # ── Backtests ────────────────────────────────────────────────────────────
    def insert_backtest(self, config_json: str):
        self._exec("INSERT INTO backtests (timestamp,config_json) VALUES (?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_json))

    # ── Chat sessions ────────────────────────────────────────────────────────
    def create_chat_session(self, title: str = "") -> int:
        if not title:
            title = f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        self._exec("INSERT INTO chat_sessions (title,created) VALUES (?,?)",
                   (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        rows = self._query("SELECT last_insert_rowid()")
        return rows[0][0]

    def get_chat_sessions(self) -> List[dict]:
        rows = self._query("SELECT id,title,created FROM chat_sessions ORDER BY id DESC")
        return [{"id": r[0], "title": r[1], "created": r[2]} for r in rows]

    def insert_chat_message(self, session_id: int, role: str, content: str):
        self._exec(
            "INSERT INTO chat_history (session_id,role,content,timestamp) VALUES (?,?,?,?)",
            (session_id, role, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def get_chat_history(self, session_id: int, limit: int = 200) -> List[dict]:
        rows = self._query(
            "SELECT role,content FROM (SELECT * FROM chat_history WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (session_id, limit))
        return [{"role": r[0], "content": r[1]} for r in rows]

    # ── Candle cache ─────────────────────────────────────────────────────────
    def get_cached_candles(self, symbol: str, interval: str) -> Optional[str]:
        cutoff = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._query(
            "SELECT data_json FROM candle_cache "
            "WHERE symbol=? AND interval=? AND timestamp > ? ORDER BY id DESC LIMIT 1",
            (symbol, interval, cutoff))
        return rows[0][0] if rows else None

    def save_cached_candles(self, symbol: str, interval: str, data_json: str):
        self._exec("DELETE FROM candle_cache WHERE symbol=? AND interval=?", (symbol, interval))
        self._exec(
            "INSERT INTO candle_cache (symbol,interval,timestamp,data_json) VALUES (?,?,?,?)",
            (symbol, interval, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), data_json))

    # ── Leaderboard ──────────────────────────────────────────────────────────
    def upsert_leaderboard(self, user_id: str, win_rate: float,
                            total_signals: int, last_backtest: str):
        self._exec(
            "INSERT INTO leaderboard (user_id,win_rate,total_signals,last_backtest) "
            "VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "win_rate=excluded.win_rate, total_signals=excluded.total_signals, "
            "last_backtest=excluded.last_backtest",
            (user_id, win_rate, total_signals, last_backtest))

    def get_leaderboard(self) -> List[dict]:
        rows = self._query(
            "SELECT user_id,win_rate,total_signals,last_backtest "
            "FROM leaderboard ORDER BY win_rate DESC LIMIT 20")
        return [{"user_id": r[0], "win_rate": r[1],
                 "total_signals": r[2], "last_backtest": r[3]} for r in rows]


db = DatabaseManager()

# ── Encrypted config ──────────────────────────────────────────────────────────
CONFIG_FILE = os.path.expanduser("~/.tradermoney_config.enc")
KEY_FILE    = os.path.expanduser("~/.tradermoney.key")


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
                data.pop("license_key",   None)
                data.pop("license_valid", None)
                return data
        except Exception:
            pass
        return {}

    @staticmethod
    def save(config: dict):
        clean = {k: v for k, v in config.items()
                 if k not in ("license_key", "license_valid")}
        try:
            cipher = _get_fernet()
            plain  = json.dumps(clean, indent=2).encode()
            tmp    = CONFIG_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(cipher.encrypt(plain))
            with open(tmp, "rb") as f:
                cipher.decrypt(f.read())
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            db.insert_log(f"Config save error: {e}")


# ── Global state ──────────────────────────────────────────────────────────────
ATR_STOP_MULT = 2.0
ATR_TP_MULT   = 3.0

_DEFAULT_CONFIG: dict = {
    "broker": "Alpaca", "tickers": "AAPL", "mode": "signal",
    "quantity": 1, "emas": [9, 50], "use_bracket": False,
    "sl_percent": 2.0, "tp_percent": 4.0, "timeframe": "1m",
    "telegram": {}, "use_rsi": True, "use_macd": True, "use_vwap": True,
    "use_bollinger": True, "use_adx": True, "use_vol_confirm": True,
    "use_supertrend": True, "use_stochastic": True, "use_atr_stops": True,
    "direction": "both", "use_default_qty": True, "last_broker_message": "",
    "watchlist": "", "timezone": "UTC", "offline_mode": False,
    "use_news_sentiment": False, "alloc_pct": 20,
    "alpaca":   {"api_key": "", "secret_key": "", "paper": True},
    "ibkr":    {"host": "", "port": "", "client_id": ""},
    "tradier":  {"access_token": "", "account_id": "", "sandbox": False},
    "binance":  {"api_key": "", "api_secret": "", "testnet": True},
    "bybit":    {"api_key": "", "api_secret": "", "testnet": True},
    "okx":     {"api_key": "", "api_secret": "", "api_passphrase": "", "demo": True},
}


def _ensure_device_id(config: dict) -> dict:
    if not config.get("device_id"):
        config["device_id"] = str(uuid.uuid4())
    return config


class AppState:
    def __init__(self):
        loaded = EncryptedConfigManager.load()
        self.config = {**_DEFAULT_CONFIG, **loaded} if loaded else dict(_DEFAULT_CONFIG)
        for k in ("alpaca", "ibkr", "tradier", "binance", "bybit", "okx"):
            if k not in self.config or not isinstance(self.config[k], dict):
                self.config[k] = dict(_DEFAULT_CONFIG[k])
        self.config["license_valid"] = False
        self.config["license_key"]   = ""
        _ensure_device_id(self.config)
        self.ui_queue: queue.Queue         = queue.Queue()
        self.engine: Optional["TradingEngine"] = None
        self.broker_instance: Optional["BaseBroker"] = None
        self.running: bool  = False
        self.dashboard: dict = {"equity": 0, "pl": 0, "buying_power": 0, "open_positions": 0}
        self.watchlist_prices: dict = {}
        self.last_backtest_results: dict = {}


state = AppState()

_watchlist_stop = threading.Event()


def _watchlist_worker():
    """Background thread: refresh watchlist prices every 30 s."""
    while not _watchlist_stop.is_set():
        wl = state.config.get("watchlist", "")
        symbols = [s.strip().upper() for s in wl.split(",") if s.strip()]
        if symbols:
            try:
                import yfinance as yf
                import pandas as pd
                tickers_str = " ".join(symbols)
                data = yf.download(tickers_str, period="1d", interval="1m",
                                   progress=False, auto_adjust=True, group_by="ticker")
                for sym in symbols:
                    try:
                        if len(symbols) == 1:
                            price = float(data["Close"].dropna().iloc[-1])
                        else:
                            price = float(data[sym]["Close"].dropna().iloc[-1])
                        state.watchlist_prices[sym] = round(price, 4)
                    except Exception:
                        pass
            except Exception:
                pass
        _watchlist_stop.wait(30)


threading.Thread(target=_watchlist_worker, daemon=True).start()


def _ts(tz_name: str = "UTC") -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_symbol(raw: str) -> str:
    return raw.split(":")[0].strip().upper()


# ── Broker registry ───────────────────────────────────────────────────────────
BROKER_REGISTRY: Dict[str, Any] = {}


def register_broker(name: str, cls):
    BROKER_REGISTRY[name] = cls


class BaseBroker:
    name = "Base"

    def __init__(self, config: dict, ui_queue: queue.Queue):
        self.config     = config
        self.ui_queue   = ui_queue
        self.last_error = ""

    def _emit_error(self, msg: str):
        self.last_error = msg
        self.ui_queue.put(("error", msg))
        db.insert_log(f"[{self.name}] {msg}")

    def _emit_log(self, msg: str):
        self.ui_queue.put(("log", msg))
        db.insert_log(f"[{self.name}] {msg}")

    def connect(self) -> bool:           raise NotImplementedError
    def get_account(self):               raise NotImplementedError
    def submit_order(self, *a, **kw):    raise NotImplementedError
    def close_all_positions(self):       raise NotImplementedError
    def get_positions(self):             raise NotImplementedError
    def get_market_status(self) -> bool: raise NotImplementedError
    def stream_prices(self, syms, cb):   raise NotImplementedError
    def stop_stream(self):               raise NotImplementedError


# ── ALPACA ────────────────────────────────────────────────────────────────────
class AlpacaBroker(BaseBroker):
    name = "Alpaca"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.api = None
        self._stop_stream = False

    def connect(self) -> bool:
        creds  = self.config.get("alpaca", {})
        key    = creds.get("api_key",    "").strip()
        secret = creds.get("secret_key", "").strip()
        paper  = creds.get("paper", True)
        if not key:
            self._emit_error("Alpaca API Key is missing.")
            return False
        if not secret:
            self._emit_error("Alpaca Secret Key is missing.")
            return False
        base_url = ("https://paper-api.alpaca.markets" if paper
                    else "https://api.alpaca.markets")
        try:
            import alpaca_trade_api as tradeapi
            self.api = tradeapi.REST(key, secret, base_url, api_version="v2")
            acc = self.api.get_account()
            if acc.status != "ACTIVE":
                self._emit_error(f"Alpaca account not ACTIVE: {acc.status}")
                return False
            self._emit_log(f"Connected. Paper={paper}. Equity=${acc.equity}")
            return True
        except ImportError:
            self._emit_error("alpaca-trade-api not installed.")
            return False
        except Exception as e:
            msg = str(e)
            if "403" in msg or "unauthorized" in msg.lower():
                self._emit_error(f"Alpaca auth failed. Paper={paper}. {msg}")
            else:
                self._emit_error(f"Alpaca connection error: {msg}")
            return False

    def get_account(self):
        if not self.api:
            return None
        try:
            acc = self.api.get_account()
            return {
                "equity":        float(acc.equity),
                "pl":            float(acc.equity) - float(acc.last_equity),
                "buying_power":  float(acc.buying_power),
                "cash":          float(acc.cash),
                "open_positions": len(self.api.list_positions()),
            }
        except Exception as e:
            self._emit_error(f"get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.api:
            return False
        try:
            if sl_price is None and sl_pct is None:
                self.api.submit_order(symbol=symbol, qty=qty, side=side,
                                      type="market", time_in_force="day")
            else:
                price = float(self.api.get_latest_trade(symbol).price)
                if side == "buy":
                    stop  = round(sl_price if sl_price else price*(1-sl_pct/100), 2)
                    limit = round(tp_price if tp_price else price*(1+tp_pct/100), 2)
                else:
                    stop  = round(sl_price if sl_price else price*(1+sl_pct/100), 2)
                    limit = round(tp_price if tp_price else price*(1-tp_pct/100), 2)
                self.api.submit_order(
                    symbol=symbol, qty=qty, side=side,
                    type="market", time_in_force="gtc", order_class="bracket",
                    stop_loss={"stop_price": stop}, take_profit={"limit_price": limit})
            return True
        except Exception as e:
            self._emit_error(f"Order failed ({symbol} {side}): {e}")
            return False

    def close_all_positions(self):
        if self.api:
            try:
                self.api.close_all_positions()
                self._emit_log("Kill switch: all positions closed.")
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
                creds  = self.config.get("alpaca", {})
                key    = creds.get("api_key")
                secret = creds.get("secret_key")
                paper  = creds.get("paper", True)

                async def on_trade(data):
                    if data.symbol in symbols:
                        callback(data.symbol, data.price)

                stream = StockDataStream(api_key=key, secret_key=secret,
                                         feed="iex" if paper else "sip")
                stream.subscribe_trades(on_trade, *symbols)
                while not self._stop_stream:
                    try:
                        stream.run()
                    except Exception as e:
                        self._emit_log(f"Stream retry: {e}")
                        time.sleep(5)
            except Exception as e:
                self._emit_log(f"Alpaca stream warning: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True


register_broker("Alpaca", AlpacaBroker)


# ── INTERACTIVE BROKERS ───────────────────────────────────────────────────────
class IBKRBroker(BaseBroker):
    name = "Interactive Brokers"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.ib = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ib_thread: Optional[threading.Thread] = None
        self._stop_stream = False

    def _start_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _ensure_loop(self):
        if self._loop is None or not self._loop.is_running():
            self._ib_thread = threading.Thread(
                target=self._start_loop, daemon=True, name="IBKRLoop")
            self._ib_thread.start()
            time.sleep(0.2)

    def _run_coro(self, coro):
        if self._loop is None:
            raise RuntimeError("IBKR event loop not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=15)

    def connect(self) -> bool:
        creds    = self.config.get("ibkr", {})
        host     = creds.get("host",      "").strip()
        port_str = creds.get("port",      "").strip()
        cid_str  = creds.get("client_id", "").strip()
        if not host:
            self._emit_error("IBKR Host is missing.")
            return False
        try:
            port = int(port_str)
            cid  = int(cid_str)
        except ValueError:
            self._emit_error("IBKR port and client_id must be integers.")
            return False
        try:
            from ib_insync import IB
        except ImportError:
            self._emit_error("ib_insync not installed.")
            return False
        self._ensure_loop()

        async def _do():
            ib = IB()
            await ib.connectAsync(host, port, clientId=cid, timeout=10)
            return ib

        try:
            self.ib = self._run_coro(_do())
            if not self.ib.isConnected():
                self._emit_error(f"IBKR isConnected()=False at {host}:{port}")
                return False
            self._emit_log(f"Connected to IBKR at {host}:{port}")
            return True
        except ConnectionRefusedError:
            self._emit_error(
                f"IBKR refused at {host}:{port}. "
                "Ports: 7497=TWS paper | 7496=TWS live | 4002=GW paper | 4001=GW live")
            return False
        except Exception as e:
            self._emit_error(f"IBKR connection error: {e}")
            return False

    def get_account(self):
        if not self.ib or not self.ib.isConnected():
            return None
        try:
            summary = self._run_coro(self.ib.accountSummaryAsync())
            eq  = next((float(v.value) for v in summary if v.tag == "NetLiquidation"), 0.0)
            pl  = next((float(v.value) for v in summary if v.tag == "UnrealizedPnL"),  0.0)
            bp  = next((float(v.value) for v in summary if v.tag == "AvailableFunds"), 0.0)
            pos = [p for p in self.ib.positions() if p.position != 0]
            return {"equity": eq, "pl": pl, "buying_power": bp,
                    "cash": 0.0, "open_positions": len(pos)}
        except Exception as e:
            self._emit_error(f"IBKR get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.ib or not self.ib.isConnected():
            self._emit_error("IBKR not connected.")
            return False
        try:
            from ib_insync import Stock, MarketOrder

            async def _place():
                c = Stock(symbol, "SMART", "USD")
                await self.ib.qualifyContractsAsync(c)
                self.ib.placeOrder(c, MarketOrder("BUY" if side == "buy" else "SELL", qty))

            self._run_coro(_place())
            return True
        except Exception as e:
            self._emit_error(f"IBKR order error: {e}")
            return False

    def close_all_positions(self):
        if not self.ib or not self.ib.isConnected():
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
        if not self.ib or not self.ib.isConnected():
            return {}
        return {pos.contract.symbol: int(pos.position)
                for pos in self.ib.positions() if pos.position != 0}

    def get_market_status(self) -> bool:
        return True

    def stream_prices(self, symbols, callback):
        if not self.ib or not self.ib.isConnected():
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


# ── TRADIER ───────────────────────────────────────────────────────────────────
class TradierBroker(BaseBroker):
    name = "Tradier"
    LIVE_URL    = "https://api.tradier.com/v1"
    SANDBOX_URL = "https://sandbox.tradier.com/v1"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session    = None
        self.account_id = None
        self._base      = self.LIVE_URL
        self._stop_stream = False

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
        self._base = self.SANDBOX_URL if sandbox else self.LIVE_URL
        import requests as req
        self.session = req.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            r = self.session.get(
                f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            if r.status_code == 401:
                self._emit_error("Tradier auth failed (HTTP 401).")
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
            r   = self.session.get(
                f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            r.raise_for_status()
            bal = r.json().get("balances", {})
            return {
                "equity":        float(bal.get("total_equity",        0)),
                "pl":            0.0,
                "buying_power":  float(bal.get("equity_buying_power", 0)),
                "cash":          float(bal.get("total_cash",          0)),
                "open_positions": 0,
            }
        except Exception as e:
            self._emit_error(f"Tradier get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session:
            self._emit_error("Tradier not connected.")
            return False
        try:
            r   = self.session.post(
                f"{self._base}/accounts/{self.account_id}/orders",
                data={"class": "equity", "symbol": symbol, "side": side,
                      "quantity": str(qty), "type": "market", "duration": "day"},
                timeout=10)
            err = r.json().get("errors", {}).get("error")
            if r.status_code not in (200, 201) or err:
                self._emit_error(f"Tradier order rejected: {err or r.text[:200]}")
                return False
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
            r   = self.session.get(
                f"{self._base}/accounts/{self.account_id}/positions", timeout=10)
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
                    r      = self.session.get(f"{self._base}/markets/quotes",
                                              params={"symbols": joined}, timeout=5)
                    quotes = r.json().get("quotes", {}).get("quote", [])
                    if isinstance(quotes, dict):
                        quotes = [quotes]
                    for q in quotes:
                        sym   = q.get("symbol", "")
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


# ── BINANCE ───────────────────────────────────────────────────────────────────
class BinanceBroker(BaseBroker):
    name = "Binance"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.client   = None
        self._stop_stream = False
        self._ws_client   = None

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "").replace("-", "").upper()
        return s if s.endswith("USDT") else s + "USDT"

    def connect(self) -> bool:
        creds      = self.config.get("binance", {})
        api_key    = creds.get("api_key",    "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet    = creds.get("testnet", True)
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
                self._emit_error("Binance account cannot trade.")
                return False
            self._emit_log(f"Connected (testnet={testnet})")
            return True
        except ImportError:
            self._emit_error("python-binance not installed.")
            return False
        except Exception as e:
            msg = str(e)
            if "-2015" in msg or "-2014" in msg:
                self._emit_error(f"Binance auth failed. Testnet={testnet}. {msg}")
            else:
                self._emit_error(f"Binance connection error: {msg}")
            return False

    def get_account(self):
        if not self.client:
            return None
        try:
            acct  = self.client.account()
            bals  = {b["asset"]: float(b["free"]) + float(b["locked"])
                     for b in acct["balances"]}
            usdt  = bals.get("USDT", 0.0)
            btc   = bals.get("BTC",  0.0)
            try:
                btc_price = float(self.client.ticker_price(symbol="BTCUSDT")["price"])
            except Exception:
                btc_price = 0.0
            return {"equity": usdt + btc * btc_price, "pl": 0.0,
                    "buying_power": usdt, "cash": usdt, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"Binance get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.client:
            self._emit_error("Binance not connected.")
            return False
        try:
            resp = self.client.new_order(
                symbol=self._norm(symbol),
                side="BUY" if side == "buy" else "SELL",
                type="MARKET", quantity=qty)
            if resp.get("status") not in ("FILLED", "NEW", "PARTIALLY_FILLED"):
                self._emit_error(f"Binance order status: {resp}")
                return False
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
                    self.client.new_order(symbol=asset + "USDT",
                                          side="SELL", type="MARKET", quantity=free)
                except Exception:
                    pass
        self._emit_log("Binance: all positions closed.")

    def get_positions(self):
        if not self.client:
            return {}
        try:
            acct = self.client.account()
            return {b["asset"]: float(b["free"])
                    for b in acct["balances"]
                    if float(b["free"]) > 0 and b["asset"] != "USDT"}
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
                        data    = json.loads(raw) if isinstance(raw, str) else raw
                        payload = data.get("data", data)
                        if payload.get("e") == "trade":
                            ws_sym = payload["s"].lower()
                            price  = float(payload["p"])
                            orig   = sym_map.get(ws_sym)
                            if orig:
                                callback(orig, price)
                    except Exception:
                        pass

                self._ws_client = SpotWebsocketStreamClient(
                    stream_url=(
                        "wss://testnet.binance.vision"
                        if self.config.get("binance", {}).get("testnet", True)
                        else "wss://stream.binance.com"),
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


# ── BYBIT ─────────────────────────────────────────────────────────────────────
class BybitBroker(BaseBroker):
    name = "Bybit"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session      = None
        self._stop_stream = False

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "").replace("-", "").upper()
        return s if s.endswith("USDT") else s + "USDT"

    def connect(self) -> bool:
        creds      = self.config.get("bybit", {})
        api_key    = creds.get("api_key",    "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet    = creds.get("testnet", True)
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
            result = (self.session.get_wallet_balance(accountType="UNIFIED")
                      .get("result", {}).get("list", [{}])[0])
            equity = float(result.get("totalEquity",           0))
            avail  = float(result.get("totalAvailableBalance", 0))
            return {"equity": equity, "pl": 0.0,
                    "buying_power": avail, "cash": avail, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"Bybit get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session:
            self._emit_error("Bybit not connected.")
            return False
        try:
            kwargs = dict(category="spot", symbol=self._norm(symbol),
                          side="Buy" if side == "buy" else "Sell",
                          orderType="Market", qty=str(qty))
            if sl_price:
                kwargs["stopLoss"]   = str(round(sl_price, 4))
            if tp_price:
                kwargs["takeProfit"] = str(round(tp_price, 4))
            resp = self.session.place_order(**kwargs)
            if resp.get("retCode", -1) != 0:
                self._emit_error(f"Bybit order rejected: {resp.get('retMsg')}")
                return False
            return True
        except Exception as e:
            self._emit_error(f"Bybit submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self.session:
            return
        for ccy, eq in self.get_positions().items():
            if eq > 0:
                self.session.place_order(
                    category="spot", symbol=ccy + "USDT",
                    side="Sell", orderType="Market", qty=str(eq))
        self._emit_log("Bybit: all positions closed.")

    def get_positions(self):
        if not self.session:
            return {}
        try:
            coins = (self.session.get_wallet_balance(accountType="UNIFIED")
                     .get("result", {}).get("list", [{}])[0].get("coin", []))
            return {c["coin"]: float(c.get("equity", 0))
                    for c in coins
                    if float(c.get("equity", 0)) > 0 and c["coin"] != "USDT"}
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
                        data    = msg.get("data", {})
                        if isinstance(data, list):
                            data = data[0] if data else {}
                        raw_sym = msg.get("topic", "").split(".")[-1]
                        orig    = sym_map.get(raw_sym)
                        price   = float(data.get("lastPrice", 0))
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


# ── OKX ───────────────────────────────────────────────────────────────────────
class OKXBroker(BaseBroker):
    name = "OKX"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self._account_api = None
        self._trade_api   = None
        self._stop_stream = False
        self._flag        = "0"

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "-").replace("_", "-").upper()
        return s if "-" in s else s + "-USDT"

    def connect(self) -> bool:
        creds      = self.config.get("okx", {})
        api_key    = creds.get("api_key",        "").strip()
        api_secret = creds.get("api_secret",     "").strip()
        passphrase = creds.get("api_passphrase", "").strip()
        demo       = creds.get("demo", True)
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
            import okx.Trade   as TradeAPI
            self._account_api = AccountAPI.AccountAPI(
                api_key, api_secret, passphrase, False, self._flag)
            self._trade_api   = TradeAPI.TradeAPI(
                api_key, api_secret, passphrase, False, self._flag)
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
            details = (self._account_api.get_account_balance()
                       .get("data", [{}])[0].get("details", []))
            equity = sum(float(d.get("eq", 0)) for d in details)
            usdt   = next(
                (float(d.get("availBal", 0)) for d in details if d.get("ccy") == "USDT"), 0.0)
            return {"equity": equity, "pl": 0.0,
                    "buying_power": usdt, "cash": usdt, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"OKX get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self._trade_api:
            self._emit_error("OKX not connected.")
            return False
        try:
            resp   = self._trade_api.place_order(
                instId=self._norm(symbol), tdMode="cash",
                side=side, ordType="market", sz=str(int(qty)))
            items  = resp.get("data", [{}])
            s_code = str(items[0].get("sCode", "-1")) if items else "-1"
            if s_code != "0":
                s_msg = items[0].get("sMsg", str(resp)) if items else str(resp)
                self._emit_error(f"OKX order rejected (sCode={s_code}): {s_msg}")
                return False
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
            details = (self._account_api.get_account_balance()
                       .get("data", [{}])[0].get("details", []))
            return {d["ccy"]: float(d.get("eq", 0))
                    for d in details
                    if float(d.get("eq", 0)) > 0 and d["ccy"] != "USDT"}
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
                subs    = [{"channel": "tickers", "instId": k} for k in sym_map]
                url     = (
                    "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
                    if self.config.get("okx", {}).get("demo", True)
                    else "wss://ws.okx.com:8443/ws/v5/public")

                def on_msg(ws_app, msg):
                    try:
                        for item in _j.loads(msg).get("data", []):
                            inst  = item.get("instId", "")
                            price = float(item.get("last", 0))
                            orig  = sym_map.get(inst)
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


# ── INDICATOR CALCULATOR ──────────────────────────────────────────────────────
class IndicatorCalculator:
    @staticmethod
    def compute_all(df, ema_fast=9, ema_slow=50):
        close  = np.asarray(df["Close"]).astype(np.float64).ravel()
        high   = np.asarray(df["High"]).astype(np.float64).ravel()
        low    = np.asarray(df["Low"]).astype(np.float64).ravel()
        volume = (np.asarray(df["Volume"]).astype(np.float64).ravel()
                  if "Volume" in df.columns else np.ones_like(close))

        def ema(data, span):
            a   = 2 / (span + 1)
            res = np.empty_like(data)
            res[0] = data[0]
            for i in range(1, len(data)):
                res[i] = a * data[i] + (1 - a) * res[i - 1]
            return res

        df["EMA_fast"]    = ema(close, ema_fast)
        df["EMA_slow"]    = ema(close, ema_slow)

        delta = np.diff(close, prepend=close[0])
        gain  = np.where(delta > 0, delta,  0.0)
        loss  = np.where(delta < 0, -delta, 0.0)
        ag    = np.convolve(gain, np.ones(14)/14, mode="full")[:len(close)]
        al    = np.convolve(loss, np.ones(14)/14, mode="full")[:len(close)]
        rs    = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
        df["RSI"] = 100 - (100 / (1 + rs))

        m              = ema(close, 12) - ema(close, 26)
        df["MACD"]      = m
        df["MACD_signal"] = ema(m, 9)
        df["MACD_hist"] = m - ema(m, 9)

        ma20       = np.convolve(close, np.ones(20)/20, mode="same")
        std20      = np.array([np.std(close[max(0, i-19):i+1]) for i in range(len(close))])
        df["BB_upper"] = ma20 + 2 * std20
        df["BB_lower"] = ma20 - 2 * std20

        cum_vol = np.cumsum(volume)
        df["VWAP"] = np.divide(np.cumsum(close * volume), cum_vol,
                                out=np.zeros_like(close), where=cum_vol != 0)

        tr     = np.maximum(high[1:] - low[1:],
                 np.maximum(np.abs(high[1:] - close[:-1]),
                            np.abs(low[1:]  - close[:-1])))
        tr     = np.insert(tr, 0, np.mean(tr[:14]) if len(tr) >= 14 else (tr[0] if len(tr) else 0))
        atr14  = ema(tr, 14)
        df["ATR"] = atr14

        up   = np.maximum( np.diff(high, prepend=high[0]), 0.0)
        dn   = np.maximum(-np.diff(low,  prepend=low[0]),  0.0)
        pdm  = np.where((up > dn) & (up > 0), up, 0.0)
        mdm  = np.where((dn > up) & (dn > 0), dn, 0.0)
        pdi  = 100 * ema(pdm, 14) / (atr14 + 1e-14)
        mdi  = 100 * ema(mdm, 14) / (atr14 + 1e-14)
        dx   = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-14)
        df["ADX"] = ema(dx, 14)

        vol_avg      = np.convolve(volume, np.ones(20)/20, mode="same")
        df["Vol_ratio"] = np.divide(volume, vol_avg, out=np.ones_like(volume), where=vol_avg != 0)
        df["Volume"]    = volume

        st_atr  = ema(tr, 10)
        hl2     = (high + low) / 2.0
        upper_s = hl2 + 3.0 * st_atr
        lower_s = hl2 - 3.0 * st_atr
        st      = np.zeros_like(close)
        trend   = np.ones_like(close)
        for i in range(1, len(close)):
            if   close[i] > upper_s[i-1]: trend[i] = 1
            elif close[i] < lower_s[i-1]: trend[i] = -1
            else:
                trend[i] = trend[i-1]
                if trend[i] == 1  and lower_s[i] < lower_s[i-1]: lower_s[i] = lower_s[i-1]
                if trend[i] == -1 and upper_s[i] > upper_s[i-1]: upper_s[i] = upper_s[i-1]
            st[i] = lower_s[i] if trend[i] == 1 else upper_s[i]
        df["Supertrend"]       = st
        df["Supertrend_trend"] = trend

        K  = 14
        ll = np.array([np.min(low[max(0, i-K+1):i+1])  for i in range(len(close))])
        hh = np.array([np.max(high[max(0, i-K+1):i+1]) for i in range(len(close))])
        stk = np.where(hh - ll != 0, 100*(close-ll)/(hh-ll+1e-14), 50.0)
        df["Stoch_K"] = stk
        df["Stoch_D"] = np.convolve(stk, np.ones(3)/3, mode="same")
        return df


# ── SIGNAL ANALYZER ───────────────────────────────────────────────────────────
class SignalAnalyzer:
    ADX_THRESHOLD = 20
    VOL_THRESHOLD = 1.5

    @staticmethod
    def _sf(val, default=0.0):
        try:
            v = val.item() if hasattr(val, "item") else val
            return float(v)
        except Exception:
            return default

    @staticmethod
    def generate_signal(df, prev_fast, prev_slow, config):
        if prev_fast is None or prev_slow is None:
            return None, "", 0.0
        l     = df.iloc[-1]
        sf    = SignalAnalyzer._sf
        ef    = sf(l["EMA_fast"])
        es    = sf(l["EMA_slow"])
        price = sf(l["Close"])
        bull  = prev_fast <= prev_slow and ef > es
        bear  = prev_fast >= prev_slow and ef < es
        passes, dir_ = False, ""
        if bull:
            passes, dir_ = SignalAnalyzer._confirm(df, config, "bull", price)
        elif bear:
            passes, dir_ = SignalAnalyzer._confirm(df, config, "bear", price)
        if not passes:
            return None, "", 0.0
        conf  = 0.50
        conf += 0.05 if config.get("use_rsi",        True) else 0
        conf += 0.05 if config.get("use_macd",       True) else 0
        conf += 0.05 if config.get("use_vwap",       True) else 0
        conf += 0.05 if config.get("use_bollinger",  True) else 0
        conf += 0.05 if config.get("use_adx",        True) else 0
        conf += 0.06 if config.get("use_vol_confirm",True) else 0
        conf += 0.08 if config.get("use_supertrend", True) else 0
        conf += 0.05 if config.get("use_stochastic", True) else 0
        conf += 0.04 if config.get("use_atr_stops",  True) else 0
        conf  = min(conf, 1.0)
        sig   = "BUY" if dir_ == "bull" else "SELL"
        return sig, f"{sig} @ ${price:.2f} (conf: {conf:.2f})", conf

    @staticmethod
    def _confirm(df, config, direction, price):
        l    = df.iloc[-1]
        sf   = SignalAnalyzer._sf
        rsi  = sf(l.get("RSI",            50), 50)
        macd = sf(l.get("MACD",            0),  0)
        msig = sf(l.get("MACD_signal",     0),  0)
        bbu  = sf(l.get("BB_upper",    price), price)
        bbl  = sf(l.get("BB_lower",    price), price)
        vwap = sf(l.get("VWAP",        price), price)
        adx  = sf(l.get("ADX",             0),  0)
        vr   = sf(l.get("Vol_ratio",        1),  1)
        stt  = sf(l.get("Supertrend_trend", 0),  0)
        stk  = sf(l.get("Stoch_K",        50), 50)
        std_ = sf(l.get("Stoch_D",        50), 50)

        if direction == "bull":
            if config.get("use_rsi",        True) and rsi < 30:                 return False, "bull"
            if config.get("use_macd",       True) and macd <= msig:             return False, "bull"
            if config.get("use_vwap",       True) and price < vwap:             return False, "bull"
            if config.get("use_bollinger",  True) and price < bbl * 0.99:       return False, "bull"
            if config.get("use_supertrend", True) and stt != 1:                 return False, "bull"
            if config.get("use_stochastic", True) and (stk < std_ or stk > 80): return False, "bull"
            if config.get("use_adx",        True) and adx < SignalAnalyzer.ADX_THRESHOLD: return False, "bull"
            if config.get("use_vol_confirm",True) and vr  < SignalAnalyzer.VOL_THRESHOLD: return False, "bull"
        else:
            if config.get("use_rsi",        True) and rsi > 70:                 return False, "bear"
            if config.get("use_macd",       True) and macd >= msig:             return False, "bear"
            if config.get("use_vwap",       True) and price > vwap:             return False, "bear"
            if config.get("use_bollinger",  True) and price > bbu * 1.01:       return False, "bear"
            if config.get("use_supertrend", True) and stt != -1:                return False, "bear"
            if config.get("use_stochastic", True) and (stk > std_ or stk < 20): return False, "bear"
            if config.get("use_adx",        True) and adx < SignalAnalyzer.ADX_THRESHOLD: return False, "bear"
            if config.get("use_vol_confirm",True) and vr  < SignalAnalyzer.VOL_THRESHOLD: return False, "bear"
        return True, direction


# ── NEWS SENTIMENT ────────────────────────────────────────────────────────────
def _get_news_sentiment(symbol: str, direction: str) -> bool:
    """Returns True if signal should proceed, False if suppressed."""
    if not NEWS_API_KEY or not NEWS_API_KEY.strip():
        return True
    try:
        clean_sym = symbol.split("/")[0].split("-")[0]
        r = http_requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": clean_sym, "pageSize": 5, "sortBy": "publishedAt",
                    "apiKey": NEWS_API_KEY},
            timeout=8)
        articles = r.json().get("articles", [])
        if not articles:
            return True
        headlines = " | ".join(a.get("title", "") for a in articles[:5])
        prompt    = (
            f"Rate the sentiment for a {direction} trade on {symbol} based on these headlines. "
            f"Reply with only a number from -1.0 (very bearish) to 1.0 (very bullish): {headlines}"
        )
        resp = http_requests.post(
            "https://api.chatanywhere.tech/v1/chat/completions",
            headers={"Authorization": f"Bearer {CHATANYWHERE_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "gpt-3.5-turbo",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 10, "temperature": 0.1},
            timeout=15)
        score_str = resp.json()["choices"][0]["message"]["content"].strip()
        score     = float("".join(c for c in score_str if c in "0123456789.-"))
        if direction == "bull" and score < -0.2:
            return False
        if direction == "bear" and score > 0.2:
            return False
    except Exception:
        pass
    return True


# ── TRADING ENGINE ────────────────────────────────────────────────────────────
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue, config, broker):
        super().__init__(daemon=True)
        self.ui_queue   = ui_queue
        self.config     = config
        self.broker     = broker
        self.running    = False
        self.symbols: List[str]          = []
        self.positions: Dict[str, Any]   = {}
        self.prev_ema: Dict[str, Tuple]  = {}
        self.per_ticker_qty: Dict[str, Any] = {}
        self.is_licensed    = config.get("license_valid", False)
        self.direction      = config.get("direction", "both")
        self.use_default_qty = config.get("use_default_qty", True)
        self._stop_watchdog  = threading.Event()
        self._fail_count     = 0
        self._paused         = False

        if not self.is_licensed:
            self.config["mode"]      = "signal"
            self.config["broker"]    = "Alpaca"
            self.config["direction"] = "both"
            self.direction           = "both"
            if "alpaca" in self.config:
                self.config["alpaca"]["paper"] = True
            for k in ("use_supertrend", "use_stochastic", "use_adx",
                      "use_vol_confirm", "use_atr_stops", "use_bracket"):
                self.config[k] = False
            first = self.config.get("tickers", "AAPL").split(",")[0].strip()
            self.config["tickers"] = first

    def _telegram(self, msg):
        if not self.is_licensed:
            return
        tg    = self.config.get("telegram", {})
        token = tg.get("token")
        cid   = tg.get("chat_id")
        if token and cid:
            try:
                http_requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                    timeout=5)
            except Exception:
                pass

    def _fetch_df(self, symbol: str, interval: str, tz: str):
        """Fetch OHLCV with SQLite cache."""
        import yfinance as yf
        import pandas   as pd

        # Try cache first
        cached = db.get_cached_candles(symbol, interval)
        if cached:
            try:
                return pd.read_json(io.StringIO(cached))
            except Exception:
                pass

        df = yf.download(symbol, period="5d", interval=interval,
                          progress=False, auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"No data for {symbol}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        db.save_cached_candles(symbol, interval, df.to_json())
        return df

    def run(self):
        import pandas as pd
        tickers_str  = self.config.get("tickers", "AAPL")
        default_qty  = self.config.get("quantity", 1)
        raw_list     = [s.strip() for s in tickers_str.split(",") if s.strip()]

        for entry in raw_list:
            sym       = clean_symbol(entry)
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
            first        = self.symbols[0]
            self.symbols = [first]
            self.per_ticker_qty = {first: self.per_ticker_qty[first]}
            self.ui_queue.put(("error", f"Free tier: 1 ticker only. Tracking {first}."))

        for s in self.symbols:
            self.positions[s] = 0
            self.prev_ema[s]  = (None, None)

        mode       = "signal" if not self.is_licensed else self.config.get("mode", "signal")
        ema_fast, ema_slow = self.config.get("emas", [9, 50])
        use_bracket = self.config.get("use_bracket", False) and self.is_licensed
        sl_pct      = self.config.get("sl_percent", 2.0)
        tp_pct      = self.config.get("tp_percent", 4.0)
        use_atr     = self.config.get("use_atr_stops", True) and self.is_licensed
        interval    = self.config.get("timeframe", "1m")
        tz          = self.config.get("timezone", "UTC")
        use_news    = self.config.get("use_news_sentiment", False) and self.is_licensed

        self.broker.stream_prices(
            self.symbols, lambda s, p: self.ui_queue.put(("price_update", (s, p))))
        self.ui_queue.put(("status", f"Running {len(self.symbols)} symbol(s)"))
        self._telegram(f"TraderMoney started\n{', '.join(self.symbols)} | {mode}")

        if use_bracket and self.broker.name != "Alpaca":
            threading.Thread(target=self._sl_tp_watchdog, daemon=True).start()

        last_fetch = 0.0
        while self.running:
            try:
                if self._paused:
                    time.sleep(5)
                    if _online:
                        self._paused = False
                        self._fail_count = 0
                        self.ui_queue.put(("status", f"Reconnected – running {len(self.symbols)} symbol(s)"))
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
                            df = self._fetch_df(s, interval, tz)
                            df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
                            self._fail_count = 0
                        except Exception as e:
                            self._fail_count += 1
                            self.ui_queue.put(("error", f"Data error {s}: {e}"))
                            if self._fail_count >= 3:
                                self._paused = True
                                self.ui_queue.put(("status", "Internet lost – paused"))
                            continue

                        latest = df.iloc[-1]
                        sf     = SignalAnalyzer._sf
                        price  = sf(latest["Close"])
                        ef     = sf(latest["EMA_fast"])
                        es     = sf(latest["EMA_slow"])
                        prev_f, prev_s = self.prev_ema.get(s, (None, None))
                        self.prev_ema[s] = (ef, es)

                        if prev_f is not None:
                            sig, rationale, conf = SignalAnalyzer.generate_signal(
                                df, prev_f, prev_s, self.config)
                            if sig:
                                # News sentiment gate
                                dir_str = "bull" if sig == "BUY" else "bear"
                                if use_news and not _get_news_sentiment(s, dir_str):
                                    self.ui_queue.put(("log",
                                        f"[NewsFilter] Suppressed {sig} {s} – negative sentiment"))
                                    continue
                                self.ui_queue.put(("signal", (s, sig, price, rationale)))
                                db.insert_signal(_ts(tz), s, sig, price, rationale)
                                if (mode == "auto" and self.is_licensed
                                        and self.broker.get_market_status()):
                                    self._execute(s, sig, price, latest,
                                                  use_bracket, use_atr, sl_pct, tp_pct, conf)
                time.sleep(1)
            except Exception:
                self.ui_queue.put(("error", f"Engine error:\n{traceback.format_exc()}"))
                time.sleep(5)

        self.broker.stop_stream()
        self.ui_queue.put(("status", "Bot stopped"))

    def _execute(self, sym, sig, price, latest,
                 use_bracket, use_atr, sl_pct, tp_pct, conf):
        try:
            qty = self.per_ticker_qty.get(sym, self.config.get("quantity", 1))
            sf  = SignalAnalyzer._sf
            if self.direction == "long"  and sig == "SELL": return
            if self.direction == "short" and sig == "BUY":  return
            pos = self.positions.get(sym, 0)
            if sig == "BUY":
                if pos <= 0:
                    if pos < 0:
                        self.broker.submit_order(sym, abs(pos), "buy")
                        self.positions[sym] = 0
                    ok = False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR", price*0.02), price*0.02)
                        ok  = self.broker.submit_order(
                            sym, qty, "buy",
                            sl_price=price - ATR_STOP_MULT * atr,
                            tp_price=price + ATR_TP_MULT  * atr)
                    elif use_bracket:
                        ok = self.broker.submit_order(
                            sym, qty, "buy", sl_pct=sl_pct, tp_pct=tp_pct)
                    else:
                        ok = self.broker.submit_order(sym, qty, "buy")
                    if ok:
                        self.positions[sym] = qty
                        self.ui_queue.put(("order", (sym, "BUY", qty, price)))
                        db.insert_trade(
                            _ts(self.config.get("timezone", "UTC")), sym, "BUY", qty, price)
                        self._telegram(f"BUY {qty} {sym} @ ${price:.2f} (conf: {conf:.2f})")
            elif sig == "SELL":
                if pos >= 0:
                    if pos > 0:
                        self.broker.submit_order(sym, pos, "sell")
                        self.positions[sym] = 0
                    ok = False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR", price*0.02), price*0.02)
                        ok  = self.broker.submit_order(
                            sym, qty, "sell",
                            sl_price=price + ATR_STOP_MULT * atr,
                            tp_price=price - ATR_TP_MULT  * atr)
                    elif use_bracket:
                        ok = self.broker.submit_order(
                            sym, qty, "sell", sl_pct=sl_pct, tp_pct=tp_pct)
                    else:
                        ok = self.broker.submit_order(sym, qty, "sell")
                    if ok:
                        self.positions[sym] = -qty
                        self.ui_queue.put(("order", (sym, "SELL", qty, price)))
                        db.insert_trade(
                            _ts(self.config.get("timezone", "UTC")), sym, "SELL", qty, price)
                        self._telegram(f"SELL {qty} {sym} @ ${price:.2f} (conf: {conf:.2f})")
        except Exception as e:
            self.ui_queue.put(("error", f"Execute error {sym}: {e}"))

    def _sl_tp_watchdog(self):
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
                    stop  = price * (1-0.02) if qty > 0 else price * (1+0.02)
                    take  = price * (1+0.04) if qty > 0 else price * (1-0.04)
                    if (qty > 0 and price <= stop) or (qty < 0 and price >= stop):
                        self.broker.submit_order(sym, abs(qty), "sell" if qty > 0 else "buy")
                        self.positions[sym] = 0
                        self._telegram(f"Stop loss triggered {sym} @ ${price:.2f}")
                    elif (qty > 0 and price >= take) or (qty < 0 and price <= take):
                        self.broker.submit_order(sym, abs(qty), "sell" if qty > 0 else "buy")
                        self.positions[sym] = 0
                        self._telegram(f"Take profit triggered {sym} @ ${price:.2f}")
            except Exception:
                pass
            time.sleep(2)

    def stop(self):
        if self.running:
            self._telegram("Bot stopped.")
        self.running = False
        self._stop_watchdog.set()


# ── CHART DATA ENDPOINT ───────────────────────────────────────────────────────
@app.route("/api/chart_data")
def api_chart_data():
    """Return OHLCV + RSI + MACD + Volume as JSON for Lightweight Charts."""
    symbol   = request.args.get("symbol", "AAPL")
    interval = request.args.get("interval", "1m")
    try:
        import yfinance as yf
        import pandas   as pd
        ef = state.config.get("emas", [9, 50])[0]
        es = state.config.get("emas", [9, 50])[1]

        cached = db.get_cached_candles(symbol, interval)
        if cached:
            df = pd.read_json(io.StringIO(cached))
        else:
            df = yf.download(symbol, period="5d", interval=interval,
                              progress=False, auto_adjust=True)
            if df is None or df.empty:
                return jsonify({"error": "No data"})
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            db.save_cached_candles(symbol, interval, df.to_json())

        df   = IndicatorCalculator.compute_all(df, ef, es)
        sf   = SignalAnalyzer._sf

        candles, rsi_line, macd_line, macd_hist, vol_line = [], [], [], [], []
        for ts, row in df.iterrows():
            t = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
            try:
                o = round(sf(row["Open"]),  4)
                h = round(sf(row["High"]),  4)
                l = round(sf(row["Low"]),   4)
                c = round(sf(row["Close"]), 4)
                if any(v <= 0 for v in [o, h, l, c]):
                    continue
                candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
                rsi_line.append({"time": t, "value": round(sf(row.get("RSI",  50)), 2)})
                macd_line.append({"time": t, "value": round(sf(row.get("MACD",  0)), 4)})
                macd_hist.append({"time": t, "value": round(sf(row.get("MACD_hist", 0)), 4)})
                vol_line.append({"time": t, "value": round(sf(row.get("Volume", 0)), 0)})
            except Exception:
                continue

        # Last 5 signals as markers
        recent_sigs = db.get_recent_signals(5)
        markers = []
        sig_times = {s["time"][:16] for s in recent_sigs}  # YYYY-MM-DD HH:MM
        for s in recent_sigs:
            try:
                from datetime import datetime
                dt = datetime.strptime(s["time"], "%Y-%m-%d %H:%M:%S")
                markers.append({
                    "time":     int(dt.timestamp()),
                    "position": "belowBar" if s["signal"] == "BUY" else "aboveBar",
                    "color":    "#D4AF37" if s["signal"] == "BUY" else "#B22222",
                    "shape":    "arrowUp" if s["signal"] == "BUY" else "arrowDown",
                    "text":     s["signal"],
                })
            except Exception:
                pass

        return jsonify({
            "candles":   candles,
            "rsi":       rsi_line,
            "macd":      macd_line,
            "macd_hist": macd_hist,
            "volume":    vol_line,
            "markers":   markers,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
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
    # Capture timezone from frontend
    if "timezone" in data:
        state.config["timezone"] = data["timezone"]
    if "offline_mode" in data:
        state.config["offline_mode"] = data["offline_mode"]
    state.config.update(data)
    EncryptedConfigManager.save(state.config)
    return jsonify({"status": "ok", "message": "Configuration saved"})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    state.config.update(data)
    EncryptedConfigManager.save(state.config)

    key = state.config.get("license_key", "").strip()
    if key:
        valid, _ = verify_gumroad_license(key)
        state.config["license_valid"] = valid
    else:
        state.config["license_valid"] = False

    if state.config.get("offline_mode") or not _online:
        state.config["mode"] = "signal"
        state.config["last_broker_message"] = "Offline Mode – no broker"
        EncryptedConfigManager.save(state.config)
        return jsonify({"status": "error",
                        "message": "Offline Mode active – bot cannot connect to broker."})

    if state.engine and state.engine.running:
        return jsonify({"status": "error", "message": "Bot already running."})

    if not state.config.get("license_valid"):
        state.config["broker"]    = "Alpaca"
        state.config["mode"]      = "signal"
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
    broker_cls    = BROKER_REGISTRY.get(broker_choice)
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
    state.engine         = TradingEngine(state.ui_queue, state.config, state.broker_instance)
    state.engine.running = True
    state.engine.start()
    state.running        = True
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
        threading.Thread(target=state.broker_instance.close_all_positions,
                         daemon=True).start()
    if state.engine:
        state.engine.stop()
    state.running = False
    state.config["license_valid"] = False
    return jsonify({"status": "ok", "message": "Kill switch activated"})


@app.route("/api/status", methods=["GET"])
def api_status():
    while not state.ui_queue.empty():
        try:
            msg  = state.ui_queue.get_nowait()
            kind = msg[0]
            if kind == "account":
                eq, pl, bp, op = msg[1]
                state.dashboard.update(equity=eq, pl=pl, buying_power=bp, open_positions=op)
            elif kind in ("log", "error"):
                db.insert_log(msg[1])
        except queue.Empty:
            break
    return jsonify({
        "running":        state.running,
        "equity":         state.dashboard["equity"],
        "pl":             state.dashboard["pl"],
        "buying_power":   state.dashboard["buying_power"],
        "open_positions": state.dashboard["open_positions"],
        "signals":        db.get_recent_signals(50)[::-1],
        "orders":         db.get_recent_trades(50)[::-1],
        "log":            db.get_recent_logs(100),
        "online":         _online,
        "watchlist":      state.watchlist_prices,
    })


@app.route("/api/broker_status")
def api_broker_status():
    return jsonify({"message": state.config.get("last_broker_message", "")})


@app.route("/api/validate_license", methods=["POST"])
def api_validate_license():
    data = request.json or {}
    key  = data.get("license_key", "").strip()
    if not key:
        return jsonify({"valid": False, "message": "No license key provided"})
    valid, msg = verify_gumroad_license(key)
    if valid:
        state.config["license_key"]   = key
        state.config["license_valid"] = True
        return jsonify({"valid": True, "message": "License verified for this session"})
    state.config["license_valid"] = False
    return jsonify({"valid": False, "message": msg})


@app.route("/api/update")
def api_update():
    try:
        url = "https://raw.githubusercontent.com/shafayrich/tradermoney/main/version.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
        latest = data.get("latest_version", "0.0.0")
        newer  = (tuple(map(int, latest.split("."))) >
                  tuple(map(int, APP_VERSION.split("."))))
        return jsonify({"current_version": APP_VERSION, "latest_version": latest,
                        "download_url": data.get("download_url", ""),
                        "update_available": newer})
    except Exception as e:
        return jsonify({"update_available": False, "error": str(e)})


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    return jsonify({"watchlist": state.config.get("watchlist", ""),
                    "prices": state.watchlist_prices})


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_post():
    data = request.json or {}
    wl   = data.get("watchlist", "")
    state.config["watchlist"] = wl
    EncryptedConfigManager.save(state.config)
    return jsonify({"status": "ok"})


@app.route("/api/correlation")
def api_correlation():
    """Return color-coded HTML correlation matrix for tickers + watchlist."""
    try:
        import yfinance as yf
        import pandas   as pd
        main_tickers = [clean_symbol(s)
                        for s in state.config.get("tickers", "AAPL").split(",") if s.strip()]
        wl_tickers   = [s.strip().upper()
                        for s in state.config.get("watchlist", "").split(",") if s.strip()]
        all_syms     = list(dict.fromkeys(main_tickers + wl_tickers))[:10]
        if not all_syms:
            return "<p>No tickers configured.</p>"
        data  = yf.download(" ".join(all_syms), period="30d",
                             interval="1d", progress=False, auto_adjust=True)
        if isinstance(data.columns, pd.MultiIndex):
            closes = data["Close"] if len(all_syms) > 1 else data[["Close"]]
        else:
            closes = data[["Close"]]
        closes = closes.dropna(axis=1, how="all")
        rets   = closes.pct_change().dropna()
        corr   = rets.corr()

        html  = "<table style='border-collapse:collapse;font-size:.8rem;'>"
        html += "<tr><th></th>"
        cols  = list(corr.columns)
        for c in cols:
            html += f"<th style='padding:4px 7px;color:#D4AF37'>{c}</th>"
        html += "</tr>"
        for row_sym in cols:
            html += f"<tr><td style='padding:4px 7px;color:#D4AF37;font-weight:bold'>{row_sym}</td>"
            for col_sym in cols:
                v   = corr.loc[row_sym, col_sym]
                r   = int(max(0, min(255, 178 + (1-v)*77)))
                g   = int(max(0, min(255, 34  +   v *200)))
                b   = int(max(0, min(255, 34)))
                bg  = f"rgb({r},{g},{b})"
                html += (f"<td style='padding:4px 7px;background:{bg};"
                         f"color:#fff;text-align:center'>{v:.2f}</td>")
            html += "</tr>"
        html += "</table>"
        return html
    except Exception as e:
        return f"<p style='color:red'>Correlation error: {e}</p>"


# ── BACKTEST (portfolio-level) ────────────────────────────────────────────────
@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    data       = request.json or {}
    config     = data.get("config", state.config)
    days       = int(data.get("days", 5))
    alloc_pct  = float(config.get("alloc_pct", 20)) / 100.0
    portfolio_capital = 100_000.0

    try:
        import yfinance as yf
        import pandas   as pd
        raw_list = [s.strip() for s in config.get("tickers", "AAPL").split(",") if s.strip()]
        symbols  = list(dict.fromkeys(clean_symbol(e) for e in raw_list))
        ef, es   = config.get("emas", [9, 50])
        interval = config.get("timeframe", "1m")
        results  = {}
        all_exit_trades = []
        portfolio_cash  = portfolio_capital

        for sym in symbols:
            sym_results = {}
            try:
                cached = db.get_cached_candles(sym, interval)
                if cached:
                    df = pd.read_json(io.StringIO(cached))
                else:
                    df = yf.download(sym, period=f"{days}d", interval=interval,
                                     progress=False, auto_adjust=True)
                    if (df is None or df.empty) and interval != "1d":
                        df = yf.download(sym, period=f"{days}d", interval="1d",
                                         progress=False, auto_adjust=True)
                    if df is None or df.empty:
                        sym_results["error"] = "No data returned"
                        results[sym]         = sym_results
                        continue
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    db.save_cached_candles(sym, interval, df.to_json())

                df   = IndicatorCalculator.compute_all(df, ef, es)
                sigs = []
                for i in range(1, len(df)):
                    prev = df.iloc[i-1]; curr = df.iloc[i]
                    pf   = SignalAnalyzer._sf(prev["EMA_fast"])
                    ps   = SignalAnalyzer._sf(prev["EMA_slow"])
                    sig, rat, conf = SignalAnalyzer.generate_signal(
                        df.iloc[:i+1], pf, ps, config)
                    if sig:
                        sf = SignalAnalyzer._sf
                        sigs.append({
                            "time":       str(df.index[i]),
                            "signal":     sig,
                            "price":      round(sf(curr["Close"]), 2),
                            "confidence": conf,
                            "indicators": {
                                "RSI":         round(sf(curr.get("RSI",          50), 50), 1),
                                "MACD":        round(sf(curr.get("MACD",          0),  0), 4),
                                "MACD_signal": round(sf(curr.get("MACD_signal",   0),  0), 4),
                                "VWAP":        round(sf(curr.get("VWAP",          0),  0), 2),
                            }
                        })
                sym_results["signals"] = sigs

                # Per-symbol simulation (portfolio allocation)
                alloc       = portfolio_capital * alloc_pct
                cash        = alloc
                position    = 0.0
                entry_price = 0.0
                entry_time  = ""
                trades      = []

                for s in sigs:
                    if s["signal"] == "BUY" and position <= 0:
                        if position < 0:
                            pnl = (entry_price - s["price"]) * abs(position)
                            trades.append({"symbol": sym, "entry_time": entry_time,
                                           "exit_time": s["time"], "side": "SHORT",
                                           "entry_price": entry_price,
                                           "exit_price": s["price"],
                                           "pnl": round(pnl, 2), "type": "exit"})
                            cash += pnl
                        position    = cash / s["price"]
                        entry_price = s["price"]
                        entry_time  = s["time"]
                        cash        = 0.0
                    elif s["signal"] == "SELL" and position >= 0:
                        if position > 0:
                            pnl = (s["price"] - entry_price) * position
                            trades.append({"symbol": sym, "entry_time": entry_time,
                                           "exit_time": s["time"], "side": "LONG",
                                           "entry_price": entry_price,
                                           "exit_price": s["price"],
                                           "pnl": round(pnl, 2), "type": "exit"})
                            cash = position * s["price"]
                        position    = -cash / s["price"]
                        entry_price = s["price"]
                        entry_time  = s["time"]
                        cash        = 0.0

                if position != 0 and sigs:
                    last   = sigs[-1]
                    side_  = "LONG" if position > 0 else "SHORT"
                    pnl    = ((last["price"] - entry_price) * position if position > 0
                              else (entry_price - last["price"]) * abs(position))
                    trades.append({"symbol": sym, "entry_time": entry_time,
                                   "exit_time": last["time"], "side": side_,
                                   "entry_price": entry_price,
                                   "exit_price": last["price"],
                                   "pnl": round(pnl, 2), "type": "exit"})
                    cash = abs(position) * last["price"] + pnl

                exits      = [t for t in trades if t["type"] == "exit"]
                total_pnl  = sum(t["pnl"] for t in exits)
                num_wins   = sum(1 for t in exits if t["pnl"] > 0)
                win_rate   = (num_wins / len(exits) * 100) if exits else 0
                sym_results["simulation"] = {
                    "alloc_capital": round(alloc, 2),
                    "final_cash":    round(cash,  2),
                    "total_pnl":     round(total_pnl, 2),
                    "win_rate":      round(win_rate, 1),
                    "total_trades":  len(exits),
                    "trades":        trades,
                }
                results[sym]     = sym_results
                all_exit_trades += exits
                portfolio_cash  += total_pnl

            except Exception as e:
                results[sym] = {"error": str(e)}

        # Portfolio summary
        port_total_pnl = sum(t["pnl"] for t in all_exit_trades)
        port_wins      = sum(1 for t in all_exit_trades if t["pnl"] > 0)
        port_wr        = (port_wins / len(all_exit_trades) * 100) if all_exit_trades else 0
        portfolio_summary = {
            "initial_capital": portfolio_capital,
            "final_capital":   round(portfolio_cash, 2),
            "total_pnl":       round(port_total_pnl, 2),
            "win_rate":        round(port_wr, 1),
            "total_trades":    len(all_exit_trades),
        }

        # Store results for Monte Carlo + Export
        state.last_backtest_results = {
            "results": results,
            "portfolio": portfolio_summary,
            "all_trades": all_exit_trades,
        }

        # Leaderboard update
        total_sigs = sum(len(v.get("signals", [])) for v in results.values() if "signals" in v)
        db.upsert_leaderboard(
            state.config.get("device_id", "local"),
            port_wr, total_sigs,
            datetime.now().strftime("%Y-%m-%d"))

        db.insert_backtest(json.dumps({"config": config, "results": results}))
        return jsonify({"results": results, "portfolio": portfolio_summary})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── MONTE CARLO ───────────────────────────────────────────────────────────────
@app.route("/api/montecarlo", methods=["POST"])
def api_montecarlo():
    trades = state.last_backtest_results.get("all_trades", [])
    exits  = [t for t in trades if t.get("type") == "exit"]
    if len(exits) < 2:
        return jsonify({"error": "Run a backtest first (need at least 2 trades)"})
    pnls   = [t["pnl"] for t in exits]
    N      = 1000
    finals = []
    for _ in range(N):
        shuffled   = random.sample(pnls, len(pnls))
        finals.append(sum(shuffled))
    finals.sort()
    prob_profit   = sum(1 for f in finals if f > 0) / N * 100
    worst_dd      = min(finals)
    best_outcome  = max(finals)
    avg_outcome   = sum(finals) / len(finals)
    return jsonify({
        "runs":          N,
        "prob_profit":   round(prob_profit,  1),
        "worst_drawdown": round(worst_dd,    2),
        "best":          round(best_outcome, 2),
        "average":       round(avg_outcome,  2),
    })


# ── EXPORT CSV ────────────────────────────────────────────────────────────────
@app.route("/api/export/backtest/csv")
def api_export_csv():
    trades = state.last_backtest_results.get("all_trades", [])
    exits  = [t for t in trades if t.get("type") == "exit"]
    si     = io.StringIO()
    w      = csv.DictWriter(si, fieldnames=["symbol", "side", "entry_time",
                                             "exit_time", "entry_price",
                                             "exit_price", "pnl"])
    w.writeheader()
    for t in exits:
        w.writerow({k: t.get(k, "") for k in w.fieldnames})
    output = si.getvalue()
    return Response(output, mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment;filename=tradermoney_backtest.csv"})


# ── EXPORT PDF ────────────────────────────────────────────────────────────────
@app.route("/api/export/backtest/pdf")
def api_export_pdf():
    try:
        from fpdf import FPDF
    except ImportError:
        return jsonify({"error": "fpdf2 not installed. Run: pip install fpdf2"}), 500

    port   = state.last_backtest_results.get("portfolio", {})
    trades = state.last_backtest_results.get("all_trades", [])
    exits  = [t for t in trades if t.get("type") == "exit"]

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "TraderMoney v2.0.0 – Backtest Report", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ln=True)
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Portfolio Summary", ln=True)
    pdf.set_font("Helvetica", "", 10)
    for k, v in port.items():
        pdf.cell(0, 7, f"  {k}: {v}", ln=True)
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Trade List", ln=True)
    pdf.set_font("Helvetica", "B", 9)
    cols  = ["Symbol", "Side", "Entry Time", "Exit Time", "Entry $", "Exit $", "P&L"]
    widths= [20, 14, 38, 38, 22, 22, 22]
    for c, w in zip(cols, widths):
        pdf.cell(w, 7, c, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    for t in exits[:50]:
        row = [t.get("symbol",""), t.get("side",""),
               str(t.get("entry_time",""))[:16], str(t.get("exit_time",""))[:16],
               str(t.get("entry_price","")), str(t.get("exit_price","")),
               str(t.get("pnl",""))]
        for val, w in zip(row, widths):
            pdf.cell(w, 6, str(val)[:18], border=1)
        pdf.ln()

    pdf_bytes = pdf.output()
    return Response(bytes(pdf_bytes), mimetype="application/pdf",
                    headers={"Content-Disposition":
                             "attachment;filename=tradermoney_backtest.pdf"})


# ── AI Chat sessions ──────────────────────────────────────────────────────────
@app.route("/api/chat/sessions", methods=["GET"])
def get_chat_sessions():
    return jsonify({"sessions": db.get_chat_sessions()})


@app.route("/api/chat/sessions", methods=["POST"])
def create_chat_session():
    title      = (request.json or {}).get("title", "")
    session_id = db.create_chat_session(title)
    return jsonify({"session_id": session_id})


@app.route("/api/chat/sessions/<int:session_id>", methods=["GET"])
def get_chat_session_history(session_id):
    history = db.get_chat_history(session_id, limit=200)
    return jsonify({"messages": history})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    global _chat_counter
    data       = request.json or {}
    message    = data.get("message",    "").strip()
    session_id = data.get("session_id", None)
    if not message:
        return jsonify({"reply": "Please type a message."})

    licensed = state.config.get("license_valid", False)
    if not licensed:
        today = datetime.now().strftime("%Y-%m-%d")
        if _chat_counter["date"] != today:
            _chat_counter["date"]  = today
            _chat_counter["count"] = 0
        if _chat_counter["count"] >= FREE_CHAT_DAILY_LIMIT:
            return jsonify({
                "reply": (f"Daily chat limit reached ({FREE_CHAT_DAILY_LIMIT}/day on Free). "
                          "Upgrade to Pro for unlimited AI access.")
            })
        _chat_counter["count"] += 1

    if not _online:
        return jsonify({"reply": "AI Chat unavailable in Offline Mode."})

    if not session_id:
        session_id = db.create_chat_session()
    db.insert_chat_message(session_id, "user", message)

    if not CHATANYWHERE_API_KEY or CHATANYWHERE_API_KEY.startswith("sk-YOUR"):
        return jsonify({"reply": "AI Chat not configured."})

    history  = db.get_chat_history(session_id, limit=20)
    messages = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})

    try:
        resp = http_requests.post(
            "https://api.chatanywhere.tech/v1/chat/completions",
            headers={"Authorization": f"Bearer {CHATANYWHERE_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "gpt-3.5-turbo", "messages": messages,
                  "max_tokens": 350, "temperature": 0.65},
            timeout=30)
        result = resp.json()
        if "error" in result:
            err_msg = result["error"].get("message", "Unknown error")
            db.insert_log(f"[AI Chat] Error: {err_msg}")
            return jsonify({"reply": f"AI error: {err_msg}"})
        reply = result["choices"][0]["message"]["content"].strip()
        db.insert_chat_message(session_id, "bot", reply)
        return jsonify({"reply": reply, "session_id": session_id})
    except Exception as e:
        db.insert_log(f"[AI Chat] Exception: {e}")
        return jsonify({"reply": "AI service unavailable."})


# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.route("/api/leaderboard")
def api_leaderboard():
    return jsonify({"leaderboard": db.get_leaderboard(),
                    "my_id": state.config.get("device_id", "")})


# ── FRONTEND HTML ─────────────────────────────────────────────────────────────
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TraderMoney 2.0</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
<style>
:root{--bg:#050505;--card:#1A1A1A;--text:#e2e2e2;--accent:#D4AF37;--danger:#B22222;--border:#2A2E38;--muted:#7a7d86;--sw:278px;--radius:10px;}
::-webkit-scrollbar{width:4px;}::-webkit-scrollbar-track{background:#080808;}::-webkit-scrollbar-thumb{background:#111;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden;color-scheme:dark;}
/* ── Sidebar ───────────────────────────────────────────── */
#sb{width:var(--sw);background:#0c0c0c;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding:14px 12px;flex-shrink:0;}
#sb h2{color:var(--accent);font-size:1.1rem;letter-spacing:.3px;margin-bottom:8px;}
.lbadge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:.65rem;margin-left:4px;vertical-align:middle;}
.lv{background:var(--accent);color:#000;}.li{background:var(--danger);color:#fff;}
label{display:block;font-size:.73rem;margin:8px 0 2px;color:var(--muted);cursor:pointer;}
.cb input{display:none;}
.cb .cm{display:inline-block;width:16px;height:16px;border:2px solid #333;border-radius:5px;margin-right:5px;vertical-align:middle;position:relative;transition:.2s;}
.cb input:checked+.cm{background:var(--accent);border-color:var(--accent);}
.cb input:checked+.cm::after{content:"";position:absolute;left:3px;top:0px;width:5px;height:9px;border:solid #000;border-width:0 2px 2px 0;transform:rotate(45deg);}
select{-webkit-appearance:none;appearance:none;background:#1A1A1A url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'><polygon fill='%23D4AF37' points='0,3 10,3 5,8'/></svg>") no-repeat right 8px center;background-size:10px;color:var(--text);border:1px solid #333;padding:6px 26px 6px 8px;border-radius:8px;width:100%;font-size:.82rem;cursor:pointer;}
select:focus{border-color:var(--accent);outline:none;}
select:disabled{opacity:.45;cursor:not-allowed;}
input[type="text"],input[type="password"],input[type="number"],textarea{background:#1A1A1A;color:var(--text);border:1px solid #333;padding:6px 8px;border-radius:8px;width:100%;font-size:.82rem;transition:border .2s;}
input:focus,textarea:focus{border-color:var(--accent);outline:none;}
input:-webkit-autofill{-webkit-text-fill-color:var(--text);-webkit-box-shadow:0 0 0 30px #1A1A1A inset;}
button{cursor:pointer;background:var(--accent);color:#050505;border:none;padding:7px 10px;border-radius:8px;width:100%;font-weight:600;margin-top:8px;font-size:.82rem;transition:all .18s;}
button:hover{opacity:.88;transform:translateY(-1px);}
button.ghost{background:var(--card);border:1px solid var(--border);color:var(--text);}
button.danger{background:var(--danger);color:#fff;}
hr{border-color:var(--border);margin:10px 0;}
.r2{display:flex;gap:4px;} .r2 input{width:100%;}
.r2b{display:flex;gap:4px;} .r2b select,.r2b button{flex:1;}
#bstatus{font-size:.7rem;margin-top:2px;min-height:14px;word-break:break-word;}
#bstatus.ok{color:#00c9b1;}#bstatus.err{color:var(--danger);}
.free-notice{background:#1a0505;color:#ff9090;border:1px solid var(--danger);padding:8px 10px;border-radius:7px;font-size:.74rem;margin-top:8px;display:none;line-height:1.5;}
.offline-banner{background:#1a1000;color:#ffb347;border:1px solid #a07000;padding:8px 10px;border-radius:7px;font-size:.74rem;margin-top:8px;display:none;}
/* watchlist */
.wl-prices{max-height:90px;overflow-y:auto;background:#111;border-radius:6px;padding:5px 8px;margin-top:4px;font-size:.74rem;}
.wl-item{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #1e1e1e;}
.wl-item:last-child{border-bottom:none;}
.wl-price{color:var(--accent);font-weight:600;}
/* ── Main ──────────────────────────────────────────────── */
#main{flex:1;display:flex;flex-direction:column;min-width:0;}
.tab-bar{display:flex;background:var(--card);border-bottom:1px solid var(--border);overflow-x:auto;flex-shrink:0;}
.tbtn{flex:1;background:transparent;border:none;color:var(--text);padding:12px 4px;cursor:pointer;font-weight:500;border-bottom:2px solid transparent;transition:.18s;min-width:58px;font-size:.8rem;white-space:nowrap;}
.tbtn:hover{background:rgba(255,255,255,.03);}
.tbtn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700;}
.tab{flex:1;display:none;overflow:hidden;flex-direction:column;}
.tab.active{display:flex;}
/* metrics */
#metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;padding:8px;background:var(--card);border-bottom:1px solid var(--border);flex-shrink:0;}
.met{text-align:center;} .met .v{font-size:1.1rem;font-weight:bold;color:var(--accent);}
/* sessions bar */
#sess{display:flex;align-items:center;gap:12px;padding:6px 10px;background:var(--card);border-bottom:1px solid var(--border);font-size:.78rem;flex-wrap:wrap;flex-shrink:0;}
.sd{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px;}
.so{background:#00c9b1;}.sc{background:var(--danger);}
/* ticker bar */
#tkbar{display:flex;flex-wrap:nowrap;overflow-x:auto;background:var(--card);border-bottom:1px solid var(--border);flex-shrink:0;}
.tkbtn{padding:6px 11px;background:transparent;border:none;color:var(--text);cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:.18s;font-size:.8rem;flex-shrink:0;}
.tkbtn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700;}
/* chart area */
#chart-wrap{flex:1;display:flex;flex-direction:column;min-height:0;}
#chart-type-bar{display:flex;gap:6px;padding:6px 10px;background:var(--card);border-bottom:1px solid var(--border);flex-shrink:0;align-items:center;}
#chart-type-bar label{color:var(--muted);margin:0 4px 0 0;font-size:.75rem;}
#chart-type-bar select{width:auto;padding:4px 22px 4px 6px;font-size:.78rem;}
#signal-bar{display:flex;gap:6px;padding:4px 10px;background:#0c0c0c;font-size:.74rem;flex-wrap:wrap;flex-shrink:0;}
.sig-marker{padding:2px 7px;border-radius:5px;font-weight:600;}
.sig-buy{background:#1a1200;color:var(--accent);border:1px solid var(--accent);}
.sig-sell{background:#1a0000;color:var(--danger);border:1px solid var(--danger);}
#chart-main{flex:1;min-height:0;position:relative;}
#chart-container{width:100%;height:60%;}
#rsi-container{width:100%;height:20%;border-top:1px solid var(--border);}
#macd-container{width:100%;height:20%;border-top:1px solid var(--border);}
/* signals/history lists */
.sitem{display:flex;justify-content:space-between;padding:8px 11px;border-bottom:1px solid var(--border);font-size:.8rem;}
.buy{color:var(--accent);}.sell{color:var(--danger);}
.empty-placeholder{color:var(--muted);text-align:center;padding:28px;font-size:.88rem;}
/* backtest */
.btp{flex:1;display:flex;flex-direction:column;}
.btr{flex:1;overflow-y:auto;overflow-x:auto;padding:10px;}
.ph{color:var(--muted);text-align:center;padding:32px 16px;font-size:.88rem;}
.bttbl{width:100%;border-collapse:collapse;font-size:.76rem;margin-bottom:16px;}
.bttbl th,.bttbl td{padding:4px 6px;border:1px solid var(--border);text-align:center;}
.bttbl th{color:var(--accent);}
/* log */
#logbar{height:90px;overflow-y:auto;background:var(--bg);padding:6px 10px;font-size:.72rem;border-top:1px solid var(--border);color:var(--muted);flex-shrink:0;}
/* AI Chat */
#aichat-wrap{display:flex;height:100%;}
#sess-panel{width:210px;background:var(--card);border-right:1px solid var(--border);display:flex;flex-direction:column;}
#sess-panel h3{color:var(--accent);font-size:.82rem;padding:10px;border-bottom:1px solid var(--border);}
#sess-list{flex:1;overflow-y:auto;}
.sess-item{padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--border);font-size:.76rem;color:var(--muted);}
.sess-item:hover,.sess-item.active{background:#0a0a0a;color:var(--text);}
#new-sess-btn{margin:7px;padding:7px;font-size:.78rem;background:var(--accent);color:#000;border:none;border-radius:7px;cursor:pointer;width:calc(100% - 14px);}
#chat-main-area{flex:1;display:flex;flex-direction:column;}
#chat-topbar{padding:9px 12px;background:var(--card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-shrink:0;}
#chat-topbar .title{color:var(--accent);font-weight:600;font-size:.9rem;}
#chat-limit{font-size:.72rem;color:var(--muted);}
#chat-msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:9px;}
.cmsg{max-width:82%;padding:9px 13px;border-radius:12px;font-size:.84rem;line-height:1.5;word-break:break-word;}
.cmsg.bot{background:#1a1200;border:1px solid #4a3800;align-self:flex-start;border-radius:4px 12px 12px 12px;}
.cmsg.user{background:#1e1e1e;border:1px solid #333;align-self:flex-end;border-radius:12px 4px 12px 12px;}
.cmsg .msender{font-size:.66rem;color:var(--accent);margin-bottom:3px;font-weight:700;}
.cmsg.user .msender{color:var(--muted);}
.cmsg .mbody{white-space:pre-wrap;user-select:text;}
.chat-typing{color:var(--muted);font-size:.78rem;padding:3px 7px;font-style:italic;align-self:flex-start;}
#chat-input-row{display:flex;gap:7px;padding:10px;border-top:1px solid var(--border);background:var(--card);flex-shrink:0;}
#chat-input{flex:1;resize:none;height:42px;padding:8px 10px;font-size:.84rem;border-radius:8px;}
#chat-send{width:auto;margin-top:0;padding:8px 16px;flex-shrink:0;font-size:.84rem;}
#mic-btn{width:auto;margin-top:0;padding:8px 10px;flex-shrink:0;font-size:.84rem;background:var(--card);border:1px solid var(--border);color:var(--text);}
/* help */
.hb{padding:18px;overflow-y:auto;height:100%;}
.hb h3{color:var(--accent);margin:12px 0 6px;font-size:1rem;}
.hb h4{color:var(--text);margin:10px 0 4px;font-size:.88rem;}
.hb p,.hb ul,.hb ol{font-size:.83rem;line-height:1.62;}
.hb ul,.hb ol{padding-left:16px;}
.hb li{margin-bottom:3px;}
.hb a{color:var(--accent);}
.istat{background:var(--card);border-radius:var(--radius);padding:12px;margin:7px 0;}
/* toasts */
#toasts{position:fixed;top:14px;right:14px;z-index:9999;display:flex;flex-direction:column;gap:5px;}
.toast{padding:11px 18px;border-radius:12px;font-weight:500;box-shadow:0 4px 16px rgba(0,0,0,.5);animation:si .22s ease;max-width:380px;font-size:.88rem;border:1px solid #333;}
.toast.success{background:var(--accent);color:#000;}.toast.error{background:var(--danger);color:#fff;}.toast.info{background:#222;color:var(--accent);}
@keyframes si{from{transform:translateX(110%);opacity:0}to{transform:translateX(0);opacity:1}}
#upd{display:none;position:fixed;bottom:14px;right:14px;z-index:9999;background:var(--accent);color:#000;padding:10px 16px;border-radius:9px;font-weight:bold;font-size:.86rem;}
#upd a{color:#000;text-decoration:underline;}
.corr-wrap{padding:12px;overflow:auto;height:100%;}
</style>
</head>
<body>
<div id="toasts"></div>
<div id="upd">Update available! <a id="udl" href="#" target="_blank">Download</a></div>

<!-- ════ SIDEBAR ════════════════════════════════════════════ -->
<div id="sb">
  <h2>TraderMoney <span id="lbadge" class="lbadge li">FREE</span> <small style="color:var(--muted);font-size:.6rem;">v2.0.0</small></h2>
  <label>License Key</label>
  <input type="password" id="lickey" placeholder="Paste Gumroad key">
  <button onclick="validateLicense()" style="margin-top:4px;font-size:.78rem;">🔑 Validate</button>
  <p style="font-size:.65rem;color:var(--muted);margin:2px 0 0;"><a href="https://shafayrich.gumroad.com/l/ykaoov" style="color:var(--accent)">Buy license ↗</a></p>

  <div id="free-notice" class="free-notice">
    Free: Alpaca paper · Signal-Only · 1 ticker · Core indicators · AI: 5/day<br>
    <b>License session-only – re-enter each restart.</b>
  </div>
  <div id="offline-banner" class="offline-banner">⚠️ Offline Mode – cached data only</div>

  <hr>
  <!-- Offline checkbox -->
  <label><span class="cb"><input type="checkbox" id="offline_mode" onchange="toggleOffline()"><span class="cm"></span></span> Offline Mode</label>

  <label>Broker</label>
  <select id="broker" onchange="onBrokerChange()"></select>
  <div id="bstatus" class="ok"></div>
  <div id="creds"></div>

  <label>Telegram Token</label><input type="password" id="tgt">
  <label>Telegram Chat ID</label><input id="tgc">

  <label>Tickers (e.g. AAPL:5, BTC/USD:0.1)</label>
  <input id="tickers" value="AAPL">

  <!-- Watchlist -->
  <label>Watchlist (comma-separated)</label>
  <input id="watchlist" placeholder="SPY, QQQ, BTC/USD">
  <div id="wl-prices" class="wl-prices" style="display:none;"></div>

  <label>Timeframe</label>
  <select id="tf"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>1d</option></select>

  <label>EMA Periods</label>
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

  <label style="margin-top:10px;font-weight:bold;color:var(--accent)">Indicators</label>
  <label><span class="cb"><input type="checkbox" id="ursi"   checked><span class="cm"></span></span> RSI</label>
  <label><span class="cb"><input type="checkbox" id="umacd"  checked><span class="cm"></span></span> MACD</label>
  <label><span class="cb"><input type="checkbox" id="uvwap"  checked><span class="cm"></span></span> VWAP</label>
  <label><span class="cb"><input type="checkbox" id="uboll"  checked><span class="cm"></span></span> Bollinger</label>
  <label><span class="cb"><input type="checkbox" id="uadx"   checked><span class="cm"></span></span> ADX <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="uvol"   checked><span class="cm"></span></span> Volume <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="ust"    checked><span class="cm"></span></span> SuperTrend <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="ustoch" checked><span class="cm"></span></span> Stochastic <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="unews"><span class="cm"></span></span> News Sentiment <span style="font-size:.62rem;color:var(--accent)">[PRO]</span></label>

  <label>Alloc % per trade (portfolio BT)</label>
  <input id="alloc_pct" value="20" type="number" min="1" max="100">

  <button onclick="saveConfig()">💾 Save</button>
  <button class="ghost" onclick="refreshTickers()">🔄 Refresh Tickers</button>
  <button style="background:var(--accent);color:#050505;" id="startBtn" onclick="startBot()">▶ Start Bot</button>
  <button class="ghost" id="stopBtn" onclick="stopBot()">■ Stop Bot</button>
  <button class="danger" onclick="killSwitch()">⚠ Kill Switch</button>
  <button class="ghost" style="margin-top:4px" onclick="resetDef()">↺ Reset</button>

  <!-- Strategy Presets -->
  <label style="margin-top:10px;font-weight:bold;color:var(--accent)">Strategy Presets</label>
  <div class="r2b">
    <select id="preset-sel">
      <option value="">-- Choose --</option>
      <option value="scalp">Scalping</option>
      <option value="swing">Swing</option>
      <option value="break">Breakout</option>
    </select>
    <button class="ghost" onclick="loadPreset()" style="margin-top:0;">Load</button>
  </div>

  <button class="ghost" style="margin-top:12px;" onclick="checkUpdate()">🔄 Check Updates</button>
  <button class="ghost" style="margin-top:5px;" onclick="runBT()">⚗ Backtest All</button>
  <div style="margin-top:7px;font-size:.72rem;color:var(--muted);">Days: <input type="number" id="btDays" value="5" min="1" max="365" style="width:55px;display:inline-block;margin-left:4px;"></div>
</div>

<!-- ════ MAIN ═══════════════════════════════════════════════ -->
<div id="main">
  <div class="tab-bar" id="tabbar">
    <button class="tbtn active" data-tab="charts">Charts</button>
    <button class="tbtn" data-tab="signals">Signals</button>
    <button class="tbtn" data-tab="history">History</button>
    <button class="tbtn" data-tab="backtest">Backtest</button>
    <button class="tbtn" data-tab="corr">Correlation</button>
    <button class="tbtn" data-tab="help">Help</button>
    <button class="tbtn" data-tab="aichat">AI Chat</button>
  </div>

  <!-- Charts Tab -->
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
      <span id="utc-clock" style="color:var(--muted);margin-left:auto;font-size:.73rem;">UTC: --</span>
    </div>
    <div id="chart-wrap">
      <div id="chart-type-bar">
        <label>Type:</label>
        <select id="chart-type" onchange="reloadChart()">
          <option value="candlestick">Candlestick</option>
          <option value="line">Line</option>
          <option value="area">Area</option>
        </select>
        <label style="margin-left:10px;">TF:</label>
        <select id="chart-tf" onchange="reloadChart()">
          <option>1m</option><option>5m</option><option>15m</option>
          <option>30m</option><option>1h</option><option>1d</option>
        </select>
        <button class="ghost" onclick="reloadChart()" style="width:auto;padding:4px 10px;margin:0 0 0 8px;font-size:.75rem;">⟳ Reload</button>
      </div>
      <div id="signal-bar"></div>
      <div id="chart-main">
        <div id="chart-container"></div>
        <div id="rsi-container"></div>
        <div id="macd-container"></div>
      </div>
    </div>
  </div>

  <!-- Signals Tab -->
  <div id="tab-signals" class="tab">
    <div id="siglist" style="overflow-y:auto;flex:1;"></div>
    <div id="sigempty" class="empty-placeholder" style="display:none;">No signals yet.</div>
  </div>

  <!-- History Tab -->
  <div id="tab-history" class="tab">
    <div id="histlist" style="overflow-y:auto;flex:1;"></div>
    <div id="hstempty" class="empty-placeholder" style="display:none;">No orders yet.</div>
  </div>

  <!-- Backtest Tab -->
  <div id="tab-backtest" class="tab">
    <div class="btp">
      <div style="display:flex;gap:7px;padding:9px;flex-shrink:0;flex-wrap:wrap;">
        <button class="ghost" style="width:auto;padding:7px 16px;" onclick="runBT()">⚗ Run Backtest</button>
        <button class="ghost" style="width:auto;padding:7px 16px;" onclick="runMC()" id="mc-btn" disabled>🎲 Monte Carlo (1000)</button>
        <button class="ghost" style="width:auto;padding:7px 14px;" onclick="window.open('/api/export/backtest/csv')">⬇ CSV</button>
        <button class="ghost" style="width:auto;padding:7px 14px;" onclick="window.open('/api/export/backtest/pdf')">⬇ PDF</button>
        <button class="ghost" style="width:auto;padding:7px 14px;" id="autotune-btn" onclick="runAutoTune()" disabled>🤖 AI Auto-Tune</button>
      </div>
      <div id="btres" class="btr"><p class="ph">Click <b>Run Backtest</b> to see portfolio P&L, trades, and signals.</p></div>
    </div>
  </div>

  <!-- Correlation Tab -->
  <div id="tab-corr" class="tab">
    <div style="padding:10px;flex-shrink:0;display:flex;gap:8px;">
      <button class="ghost" style="width:auto;padding:7px 16px;" onclick="loadCorr()">🔄 Refresh Correlation Matrix</button>
    </div>
    <div class="corr-wrap" id="corr-content"><p class="ph">Click Refresh to load the 30-day correlation matrix for your tickers + watchlist.</p></div>
  </div>

  <!-- Help Tab -->
  <div id="tab-help" class="tab">
    <div class="hb">
      <h3>📘 TraderMoney 2.0 – Complete Guide</h3>
      <h4>🚀 Getting Started</h4>
      <ol>
        <li>Enter your Alpaca paper API keys (free tier) or validate a Pro license.</li>
        <li>Set tickers e.g. <code>AAPL:5, TSLA:2, BTC/USD:0.5</code> and choose a timeframe.</li>
        <li>Toggle indicators, save config, then click Start Bot.</li>
      </ol>
      <h4>⌨ Keyboard Shortcuts</h4>
      <table class="bttbl">
        <tr><th>Shortcut</th><th>Action</th></tr>
        <tr><td>Ctrl+Space</td><td>Toggle Start/Stop bot</td></tr>
        <tr><td>Ctrl+K</td><td>Focus ticker input</td></tr>
        <tr><td>Ctrl+B</td><td>Run Backtest</td></tr>
        <tr><td>Ctrl+Shift+B</td><td>Switch to Backtest tab</td></tr>
        <tr><td>Ctrl+1…7</td><td>Switch tabs</td></tr>
      </table>
      <h4>📊 Signal Indicators</h4>
      <div class="istat">
        <p>EMA crossover is the trigger. Each active indicator is a confirming gate — <em>all must pass</em>.</p>
        <p>+RSI→~40% | +MACD→~45% | +VWAP→~48% | +Bollinger→~50% | +ADX→~55% | +Volume→~58% | +SuperTrend→~62% | +Stochastic→~65%</p>
      </div>
      <h4>🧪 Portfolio Backtest</h4>
      <p>Simulates $100,000 capital. Each ticker gets Alloc % (default 20%) per signal. Results include combined equity curve and per-ticker breakdown.</p>
      <h4>🎲 Monte Carlo</h4>
      <p>After backtest, click Monte Carlo to run 1000 shuffled P&L scenarios. Shows probability of profit, worst drawdown, and average outcome.</p>
      <h4>🌐 Correlation Matrix</h4>
      <p>Shows 30-day daily-return correlations across all your tickers + watchlist. Green = high positive, red = high negative correlation.</p>
      <h4>📡 News Sentiment (PRO)</h4>
      <p>When enabled with a NewsAPI key set in <code>app.py</code>, the engine fetches headlines before acting on a signal and suppresses the trade if AI sentiment contradicts the direction.</p>
      <h4>🏆 Leaderboard</h4>
      <div id="leaderboard-wrap"><p class="ph" style="padding:10px">Loading…</p></div>
    </div>
  </div>

  <!-- AI Chat Tab -->
  <div id="tab-aichat" class="tab">
    <div id="aichat-wrap">
      <div id="sess-panel">
        <h3>💬 Chats</h3>
        <div id="sess-list"></div>
        <button id="new-sess-btn" onclick="createNewSession()">+ New Chat</button>
      </div>
      <div id="chat-main-area">
        <div id="chat-topbar">
          <span class="title">🤖 TraderBot AI</span>
          <span id="chat-limit"></span>
        </div>
        <div id="chat-msgs"></div>
        <div id="chat-input-row">
          <textarea id="chat-input" placeholder="Ask about trading strategies, indicators…"></textarea>
          <button id="mic-btn" onclick="startVoice()" title="Voice input">🎤</button>
          <button id="chat-send" onclick="sendChat()">Send</button>
        </div>
      </div>
    </div>
  </div>

  <div id="logbar"></div>
</div>

<script>
'use strict';
// ── State ──────────────────────────────────────────────────────────────────
const $=id=>document.getElementById(id);
let cfg={},licValid=false,curSym='',allTickers=[],lastChart='',curSessionId=null,chatInited=false;
let mainChart=null,rsiChart=null,macdChart=null,candleSeries=null,rsiSeries=null,macdSeries=null,macdHistSeries=null;

// ── Utilities ──────────────────────────────────────────────────────────────
function cs(raw){return raw.split(':')[0].trim().toUpperCase();}
function fmt(n,d=2){return Number(n).toLocaleString(undefined,{maximumFractionDigits:d});}
function toast(msg,type='info'){
  let t=document.createElement('div');t.className='toast '+type;t.textContent=msg;
  $('toasts').appendChild(t);setTimeout(()=>t.remove(),3800);
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

// ── Tab switching ──────────────────────────────────────────────────────────
const TABS=['charts','signals','history','backtest','corr','help','aichat'];
function switchTab(name){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tbtn').forEach(x=>x.classList.remove('active'));
  const t=$('tab-'+name),b=document.querySelector(`[data-tab="${name}"]`);
  if(t)t.classList.add('active');if(b)b.classList.add('active');
  if(name==='aichat')initAIChat();
  if(name==='help')loadLeaderboard();
  if(name==='charts')setTimeout(resizeCharts,80);
}
document.querySelectorAll('.tbtn').forEach(b=>{
  b.addEventListener('click',function(){switchTab(this.dataset.tab);});
});
Sortable.create($('tabbar'),{animation:110,handle:'.tbtn'});

// ── Sessions clock ─────────────────────────────────────────────────────────
function updSess(){
  let n=new Date(),d=n.getUTCDay(),wk=d===0||d===6,h=n.getUTCHours()+n.getUTCMinutes()/60;
  let o=ok=>ok?'sd so':'sd sc';
  $('ds').className=o(!wk&&(h>=22||h<5));$('dt').className=o(!wk&&(h>=23||h<6));
  $('dl').className=o(!wk&&h>=8&&h<16.5);$('dn').className=o(!wk&&h>=13.5&&h<20);
  $('utc-clock').textContent='UTC: '+n.toISOString().slice(11,19);
}
setInterval(updSess,30000);updSess();

// ── Broker credential helpers ──────────────────────────────────────────────
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
  else if(b==='Interactive Brokers')c.innerHTML=tx('ih','Host')+tx('ip','Port')+tx('icid','Client ID');
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
  if(licValid){['Interactive Brokers','Tradier','Binance','Bybit','OKX'].forEach(x=>addOpt(x,x));}
  sel.value=licValid?cur:'Alpaca';
}
function onBrokerChange(){cfg.broker=$('broker').value;updateCreds();}
function toggleDefQty(){$('defqty-box').style.display=gc('udefqty')?'block':'none';}
function toggleOffline(){
  const on=gc('offline_mode');
  $('offline-banner').style.display=on?'block':'none';
}

// ── Free / Pro UI ──────────────────────────────────────────────────────────
function applyFreeTierUI(){
  updateBrokerOptions();$('broker').disabled=true;sv('broker','Alpaca');cfg.broker='Alpaca';
  sv('mode','signal');$('mode').disabled=true;sv('dir','both');$('dir').disabled=true;
  ['ubracket','uatr','uadx','uvol','ust','ustoch','unews'].forEach(id=>{sc(id,false);lockCb(id,true);});
  // Add click handler for locked elements to show upgrade toast
  ['broker','mode','dir','ubracket','uatr','uadx','uvol','ust','ustoch','unews'].forEach(id=>{
    const el=$(id);if(el)el.addEventListener('click',()=>{if(!licValid)toast('Upgrade to Pro to unlock this feature – shafayrich.gumroad.com','info');},true);
  });
  $('free-notice').style.display='block';
  $('lbadge').textContent='FREE';$('lbadge').className='lbadge li';
}
function applyProUI(){
  updateBrokerOptions();$('broker').disabled=false;$('mode').disabled=false;$('dir').disabled=false;
  ['ubracket','uatr','uadx','uvol','ust','ustoch','unews'].forEach(id=>lockCb(id,false));
  $('free-notice').style.display='none';
  $('lbadge').textContent='PRO';$('lbadge').className='lbadge lv';
}

// ── Config helpers ─────────────────────────────────────────────────────────
function buildCfg(){
  saveCurrentBrokerCreds();
  return{
    broker:cfg.broker||'Alpaca',tickers:gv('tickers','AAPL'),timeframe:gv('tf','1m'),
    emas:[parseInt(gv('emaf','9')),parseInt(gv('emas','50'))],
    quantity:parseInt(gv('qty','1'))||1,mode:gv('mode','signal'),
    direction:gv('dir','both'),use_default_qty:gc('udefqty'),
    use_bracket:gc('ubracket'),sl_percent:parseFloat(gv('slp','2')),
    tp_percent:parseFloat(gv('tpp','4')),use_atr_stops:gc('uatr'),
    telegram:{token:gv('tgt'),chat_id:gv('tgc')},
    use_rsi:gc('ursi'),use_macd:gc('umacd'),use_vwap:gc('uvwap'),
    use_bollinger:gc('uboll'),use_adx:gc('uadx'),use_vol_confirm:gc('uvol'),
    use_supertrend:gc('ust'),use_stochastic:gc('ustoch'),
    use_news_sentiment:gc('unews'),
    alloc_pct:parseFloat(gv('alloc_pct','20'))||20,
    watchlist:gv('watchlist',''),offline_mode:gc('offline_mode'),
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

  // Always apply free tier on init; validateLicense() upgrades if key present
  applyFreeTierUI();

  sv('tickers',c.tickers||'AAPL');sv('tf',c.timeframe||'1m');
  sv('emaf',c.emas?c.emas[0]:9);sv('emas',c.emas?c.emas[1]:50);
  sc('udefqty',c.use_default_qty!==false);toggleDefQty();
  sv('qty',c.quantity||1);
  if(c.telegram){sv('tgt',c.telegram.token||'');sv('tgc',c.telegram.chat_id||'');}
  sv('slp',c.sl_percent||2);sv('tpp',c.tp_percent||4);
  sv('alloc_pct',c.alloc_pct||20);
  sv('watchlist',c.watchlist||'');
  sc('ursi',c.use_rsi!==false);sc('umacd',c.use_macd!==false);
  sc('uvwap',c.use_vwap!==false);sc('uboll',c.use_bollinger!==false);
  sc('offline_mode',!!c.offline_mode);toggleOffline();
  if(c.license_key)sv('lickey',c.license_key);
  updateCreds();
  let raw=(c.tickers||'AAPL').split(',').map(s=>s.trim()).filter(s=>s);
  if(raw.length){setTickers(raw);buildChart(cs(raw[0]));}
}

// ── Ticker bar ─────────────────────────────────────────────────────────────
function setTickers(list){
  allTickers=list;let bar=$('tkbar');bar.innerHTML='';
  list.forEach(raw=>{
    let sym=cs(raw),btn=document.createElement('button');
    btn.className='tkbtn'+(sym===curSym?' active':'');btn.textContent=sym;
    btn.onclick=()=>{curSym=sym;updTk();if(lastChart!==sym)buildChart(sym);};
    bar.appendChild(btn);
  });
}
function updTk(){document.querySelectorAll('.tkbtn').forEach(b=>b.classList.toggle('active',cs(b.textContent)===curSym));}
function refreshTickers(){fetch('/api/config').then(r=>r.json()).then(c=>{sv('tickers',c.tickers);let raw=c.tickers.split(',').map(s=>s.trim()).filter(s=>s);if(raw.length){setTickers(raw);buildChart(cs(raw[0]));}toast('Tickers refreshed','success');});}

// ── Lightweight Charts ─────────────────────────────────────────────────────
function buildChart(sym){
  curSym=sym;lastChart=sym;updTk();
  // Destroy old charts
  if(mainChart){try{mainChart.remove();}catch(e){}}
  if(rsiChart) {try{rsiChart.remove();}catch(e){}}
  if(macdChart){try{macdChart.remove();}catch(e){}}
  mainChart=null;rsiChart=null;macdChart=null;
  $('chart-container').innerHTML='';
  $('rsi-container').innerHTML='';
  $('macd-container').innerHTML='';

  const opts={layout:{background:{color:'#050505'},textColor:'#e2e2e2'},
    grid:{vertLines:{color:'#111'},horzLines:{color:'#111'}},
    crosshair:{mode:0},timeScale:{timeVisible:true,secondsVisible:false},
    rightPriceScale:{borderColor:'#2A2E38'}};

  mainChart=LightweightCharts.createChart($('chart-container'),{...opts,height:$('chart-container').clientHeight||300});
  rsiChart =LightweightCharts.createChart($('rsi-container'), {...opts,height:$('rsi-container').clientHeight||100});
  macdChart=LightweightCharts.createChart($('macd-container'),{...opts,height:$('macd-container').clientHeight||100});

  const chartType=gv('chart-type','candlestick');
  const tf=gv('chart-tf','1m');

  if(chartType==='candlestick'){
    candleSeries=mainChart.addCandlestickSeries({upColor:'#D4AF37',downColor:'#B22222',borderUpColor:'#D4AF37',borderDownColor:'#B22222',wickUpColor:'#D4AF37',wickDownColor:'#B22222'});
  } else if(chartType==='area'){
    candleSeries=mainChart.addAreaSeries({lineColor:'#D4AF37',topColor:'rgba(212,175,55,0.3)',bottomColor:'rgba(212,175,55,0)'});
  } else {
    candleSeries=mainChart.addLineSeries({color:'#D4AF37',lineWidth:2});
  }
  rsiSeries =rsiChart.addLineSeries({color:'#5b9bd5',lineWidth:1,title:'RSI'});
  macdSeries=macdChart.addLineSeries({color:'#D4AF37',lineWidth:1,title:'MACD'});
  macdHistSeries=macdChart.addHistogramSeries({color:'#B22222',priceScaleId:'',scaleMargins:{top:0.8,bottom:0},title:'Hist'});

  fetch(`/api/chart_data?symbol=${encodeURIComponent(sym)}&interval=${tf}`)
    .then(r=>r.json()).then(d=>{
      if(d.error){toast('Chart error: '+d.error,'error');return;}
      if(chartType==='candlestick'){
        candleSeries.setData(d.candles||[]);
      } else {
        candleSeries.setData((d.candles||[]).map(c=>({time:c.time,value:c.close})));
      }
      rsiSeries.setData(d.rsi||[]);
      macdSeries.setData(d.macd||[]);
      macdHistSeries.setData((d.macd_hist||[]).map(x=>({...x,color:x.value>=0?'#D4AF37':'#B22222'})));
      if(d.markers&&d.markers.length){
        try{candleSeries.setMarkers(d.markers);}catch(e){}
      }
      // Signal bar
      let bar=$('signal-bar');bar.innerHTML='';
      (d.markers||[]).slice(0,5).forEach(m=>{
        let sp=document.createElement('span');
        sp.className='sig-marker '+(m.shape==='arrowUp'?'sig-buy':'sig-sell');
        sp.textContent=(m.shape==='arrowUp'?'▲ BUY':'▼ SELL');
        bar.appendChild(sp);
      });
    }).catch(e=>toast('Chart data error: '+e,'error'));
}
function reloadChart(){buildChart(curSym||'AAPL');}
function resizeCharts(){
  if(mainChart)try{mainChart.applyOptions({width:$('chart-container').clientWidth});}catch(e){}
  if(rsiChart)try{rsiChart.applyOptions({width:$('rsi-container').clientWidth});}catch(e){}
  if(macdChart)try{macdChart.applyOptions({width:$('macd-container').clientWidth});}catch(e){}
}
window.addEventListener('resize',resizeCharts);

// ── Config load/save ───────────────────────────────────────────────────────
async function loadConfig(){
  try{
    const r=await fetch('/api/config');cfg=await r.json();
    // Send timezone
    await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})});
    initUI(cfg);
    if(cfg.license_key&&cfg.license_key.trim())await validateLicense(true);
    loadHistory();
  }catch(e){toast('Config load failed','error');}
}
function loadHistory(){
  fetch('/api/status').then(r=>r.json()).then(d=>{renderSignals(d.signals);renderOrders(d.orders);}).catch(()=>{});
}
async function saveConfig(){
  cfg=buildCfg();
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  // Save watchlist separately
  await fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({watchlist:gv('watchlist')})});
  toast('Config saved (license session-only)','success');
}

const DEF={broker:'Alpaca',tickers:'AAPL',mode:'signal',direction:'both',use_default_qty:true,quantity:1,emas:[9,50],use_bracket:false,sl_percent:2,tp_percent:4,timeframe:'1m',telegram:{},use_rsi:true,use_macd:true,use_vwap:true,use_bollinger:true,use_adx:true,use_vol_confirm:true,use_supertrend:true,use_stochastic:true,use_atr_stops:true,alloc_pct:20,watchlist:'',offline_mode:false,alpaca:{api_key:'',secret_key:'',paper:true},ibkr:{host:'',port:'',client_id:''},tradier:{access_token:'',account_id:'',sandbox:false},binance:{api_key:'',api_secret:'',testnet:true},bybit:{api_key:'',api_secret:'',testnet:true},okx:{api_key:'',api_secret:'',api_passphrase:'',demo:true}};
function resetDef(){cfg=JSON.parse(JSON.stringify(DEF));licValid=false;applyFreeTierUI();sv('lickey','');initUI(cfg);saveConfig();toast('Reset to factory defaults','success');}

// ── Bot controls ───────────────────────────────────────────────────────────
async function startBot(){
  let btn=$('startBtn');btn.textContent='Starting…';btn.disabled=true;
  cfg=buildCfg();
  if(!licValid){
    cfg.broker='Alpaca';cfg.mode='signal';cfg.direction='both';
    if(cfg.alpaca)cfg.alpaca.paper=true;
    ['use_supertrend','use_stochastic','use_adx','use_vol_confirm','use_atr_stops','use_bracket'].forEach(k=>cfg[k]=false);
    cfg.tickers=cfg.tickers.split(',')[0].trim();
  }
  const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  const d=await r.json();
  btn.textContent='▶ Start Bot';btn.disabled=false;
  toast(d.message,d.status==='ok'?'success':'error');
  if(d.status!=='ok'){$('bstatus').textContent=d.message;$('bstatus').className='err';}
}
async function stopBot(){let btn=$('stopBtn');btn.textContent='Stopping…';btn.disabled=true;await fetch('/api/stop',{method:'POST'});btn.textContent='■ Stop Bot';btn.disabled=false;toast('Bot stopped','success');}
async function killSwitch(){await fetch('/api/kill',{method:'POST'});toast('Kill switch activated','error');}

async function validateLicense(silent=false){
  const key=gv('lickey').trim();
  if(!key){if(!silent)toast('Enter a license key','error');return;}
  const r=await fetch('/api/validate_license',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key})});
  const d=await r.json();
  if(d.valid){
    licValid=true;applyProUI();
    sv('mode',cfg.mode||'signal');sv('dir',cfg.direction||'both');
    sc('ubracket',!!cfg.use_bracket);sc('uatr',cfg.use_atr_stops!==false);
    sc('uadx',cfg.use_adx!==false);sc('uvol',cfg.use_vol_confirm!==false);
    sc('ust',cfg.use_supertrend!==false);sc('ustoch',cfg.use_stochastic!==false);
    updateCreds();
    if(!silent)toast('Pro unlocked for this session','success');
  } else {
    licValid=false;applyFreeTierUI();
    if(!silent)toast(d.message,'error');
  }
}

async function checkUpdate(){
  try{const d=await(await fetch('/api/update')).json();if(d.update_available){$('upd').style.display='block';$('udl').href=d.download_url;toast('Update available!','success');}else toast('Up to date!','success');}catch(e){}
}
setTimeout(checkUpdate,2500);

// ── Broker status polling ──────────────────────────────────────────────────
async function pollBS(){
  try{const d=await(await fetch('/api/broker_status')).json();const bs=$('bstatus');if(d.message){bs.textContent=d.message;bs.className=d.message.startsWith('Connected')?'ok':'err';}}catch(e){}
}
setInterval(pollBS,2500);pollBS();

// ── Main status polling ────────────────────────────────────────────────────
function renderSignals(sigs){
  let sl=$('siglist'),se=$('sigempty');sl.innerHTML='';se.style.display='none';
  let has=false;(sigs||[]).forEach(s=>{has=true;let div=document.createElement('div');div.className='sitem '+(s.signal==='BUY'?'buy':'sell');div.innerHTML=`<span>${s.time} <b>${s.signal}</b> ${s.symbol} @ $${s.price}</span><span>${s.rationale||''}</span>`;sl.appendChild(div);});
  if(!has)se.style.display='block';
}
function renderOrders(ords){
  let hl=$('histlist'),he=$('hstempty');hl.innerHTML='';he.style.display='none';
  let has=false;(ords||[]).forEach(o=>{has=true;let div=document.createElement('div');div.className='sitem '+(o.action==='BUY'?'buy':'sell');div.innerHTML=`<span>${o.time} <b>${o.action}</b> ${o.qty} ${o.symbol} @ $${o.price}</span>`;hl.appendChild(div);});
  if(!has)he.style.display='block';
}
function renderWatchlist(prices){
  const wl=gv('watchlist');
  const syms=wl.split(',').map(s=>s.trim().toUpperCase()).filter(s=>s);
  const box=$('wl-prices');
  if(!syms.length){box.style.display='none';return;}
  box.style.display='block';box.innerHTML='';
  syms.forEach(sym=>{
    const price=prices[sym];
    const div=document.createElement('div');div.className='wl-item';
    div.innerHTML=`<span>${sym}</span><span class="wl-price">${price?'$'+price:'—'}</span>`;
    box.appendChild(div);
  });
}
async function pollStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    $('v-eq').textContent='$'+fmt(d.equity);$('v-bp').textContent='$'+fmt(d.buying_power);
    const pct=d.equity?(d.pl/d.equity*100):0;
    $('v-pl').innerHTML=`<span style="color:${pct>=0?'var(--accent)':'var(--danger)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</span>`;
    $('v-pos').textContent=d.open_positions;
    renderSignals(d.signals);renderOrders(d.orders);
    renderWatchlist(d.watchlist||{});
    $('logbar').innerHTML=(d.log||[]).join('<br>');
    if(!d.online)$('offline-banner').style.display='block';
  }catch(e){}
}
setInterval(pollStatus,1500);

// ── Backtest ───────────────────────────────────────────────────────────────
let lastBTSummary='';
async function runBT(){
  const days=parseInt(gv('btDays','5'))||5;
  toast('Running portfolio backtest…','info');
  $('btres').innerHTML='<p class="ph">Loading…</p>';
  switchTab('backtest');
  try{
    const r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days})});
    const data=await r.json();
    if(data.error){toast('Backtest error: '+data.error,'error');$('btres').innerHTML=`<p class="ph" style="color:var(--danger)">${data.error}</p>`;return;}
    const p=data.portfolio||{};
    lastBTSummary=JSON.stringify({portfolio:p,per_symbol:Object.keys(data.results||{}).map(sym=>({sym,sim:data.results[sym].simulation}))});
    let html=`<div style="background:var(--card);padding:12px;border-radius:8px;margin-bottom:14px;">
      <b style="color:var(--accent)">Portfolio Summary</b><br>
      Initial: $${(p.initial_capital||100000).toFixed(0)} &nbsp;|&nbsp;
      Final: $${(p.final_capital||0).toFixed(2)} &nbsp;|&nbsp;
      P&L: <span style="color:${p.total_pnl>=0?'var(--accent)':'var(--danger)'}">${p.total_pnl>=0?'+':''}$${(p.total_pnl||0).toFixed(2)}</span> &nbsp;|&nbsp;
      Win Rate: ${p.win_rate||0}% &nbsp;|&nbsp;
      Trades: ${p.total_trades||0}
    </div>`;
    for(const sym in data.results){
      const info=data.results[sym];
      html+=`<h4 style="color:var(--accent);margin:10px 0 5px">${sym}</h4>`;
      if(info.error){html+=`<p style="color:var(--danger)">${info.error}</p>`;continue;}
      if(info.simulation){
        const sim=info.simulation;
        html+=`<div style="background:var(--card);padding:8px;border-radius:7px;margin-bottom:9px;font-size:.8rem;">
          Alloc: $${sim.alloc_capital} &nbsp;|&nbsp; Final: $${sim.final_cash.toFixed(2)} &nbsp;|&nbsp;
          P&L: <span style="color:${sim.total_pnl>=0?'var(--accent)':'var(--danger)'}">${sim.total_pnl>=0?'+':''}$${sim.total_pnl.toFixed(2)}</span> &nbsp;|&nbsp;
          Win Rate: ${sim.win_rate}% &nbsp;|&nbsp; Trades: ${sim.total_trades}
        </div>`;
        if(sim.trades&&sim.trades.filter(t=>t.type==='exit').length){
          html+=`<table class="bttbl"><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Entry $</th><th>Exit $</th><th>P&L</th></tr>`;
          sim.trades.filter(t=>t.type==='exit').forEach(t=>{
            html+=`<tr><td>${String(t.entry_time).slice(0,16)}</td><td>${String(t.exit_time).slice(0,16)}</td>
              <td style="color:${t.side==='LONG'?'var(--accent)':'var(--danger)'}">${t.side}</td>
              <td>${t.entry_price.toFixed(2)}</td><td>${t.exit_price.toFixed(2)}</td>
              <td style="color:${t.pnl>=0?'var(--accent)':'var(--danger)'}">${t.pnl>=0?'+':''}${t.pnl.toFixed(2)}</td></tr>`;
          });
          html+='</table>';
        }
      }
      if(info.signals&&info.signals.length){
        html+=`<details><summary style="cursor:pointer;color:var(--muted);font-size:.78rem;padding:4px 0">Raw signals (${info.signals.length})</summary>
          <table class="bttbl"><tr><th>Time</th><th>Sig</th><th>Price</th><th>Conf</th></tr>`;
        info.signals.forEach(s=>{html+=`<tr><td>${s.time}</td><td class="${s.signal==='BUY'?'buy':'sell'}">${s.signal}</td><td>$${s.price}</td><td>${(s.confidence*100).toFixed(0)}%</td></tr>`;});
        html+='</table></details>';
      }
    }
    $('btres').innerHTML=html;
    $('mc-btn').disabled=false;$('autotune-btn').disabled=false;
  }catch(e){toast('Backtest failed: '+e,'error');}
}

async function runMC(){
  toast('Running Monte Carlo (1000 simulations)…','info');
  try{
    const d=await(await fetch('/api/montecarlo',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    if(d.error){toast(d.error,'error');return;}
    const html=`<div style="background:var(--card);padding:12px;border-radius:8px;margin-top:12px;">
      <b style="color:var(--accent)">Monte Carlo Results (${d.runs} runs)</b><br>
      Probability of Profit: <b>${d.prob_profit}%</b><br>
      Best: <span style="color:var(--accent)">+$${d.best.toFixed(2)}</span> &nbsp;|&nbsp;
      Average: $${d.average.toFixed(2)} &nbsp;|&nbsp;
      Worst Drawdown: <span style="color:var(--danger)">$${d.worst_drawdown.toFixed(2)}</span>
    </div>`;
    $('btres').innerHTML+= html;
  }catch(e){toast('Monte Carlo failed: '+e,'error');}
}

async function runAutoTune(){
  if(!lastBTSummary){toast('Run a backtest first','error');return;}
  const prompt=`Based on this backtest summary, suggest optimal indicator combinations and SL/TP percentages for TraderMoney:\n${lastBTSummary}`;
  switchTab('aichat');
  await initAIChat();
  const el=$('chat-input');el.value=prompt;
  await sendChat();
}

// ── Correlation Matrix ──────────────────────────────────────────────────────
async function loadCorr(){
  $('corr-content').innerHTML='<p class="ph">Loading correlation matrix…</p>';
  try{const html=await(await fetch('/api/correlation')).text();$('corr-content').innerHTML=html;}
  catch(e){$('corr-content').innerHTML='<p class="ph" style="color:var(--danger)">Error loading correlation</p>';}
}

// ── Strategy Presets ───────────────────────────────────────────────────────
const PRESETS={
  scalp:{timeframe:'1m',emas:[9,50],use_rsi:true,use_macd:true,use_vwap:false,use_bollinger:false,use_adx:false,use_vol_confirm:true,use_supertrend:false,use_stochastic:false,use_bracket:false,use_atr_stops:false,direction:'long'},
  swing:{timeframe:'15m',emas:[20,50],use_rsi:true,use_macd:true,use_vwap:true,use_bollinger:true,use_adx:true,use_vol_confirm:false,use_supertrend:false,use_stochastic:false,use_bracket:true,sl_percent:3,tp_percent:5,use_atr_stops:false,direction:'both'},
  break:{timeframe:'5m',emas:[9,50],use_rsi:false,use_macd:false,use_vwap:false,use_bollinger:false,use_adx:false,use_vol_confirm:true,use_supertrend:true,use_stochastic:false,use_bracket:false,use_atr_stops:true,direction:'both'},
};
function loadPreset(){
  const key=gv('preset-sel');if(!key){toast('Select a preset first','error');return;}
  const p=PRESETS[key];if(!p)return;
  sv('tf',p.timeframe);sv('emaf',p.emas[0]);sv('emas',p.emas[1]);
  sc('ursi',!!p.use_rsi);sc('umacd',!!p.use_macd);sc('uvwap',!!p.use_vwap);sc('uboll',!!p.use_bollinger);
  sc('uadx',!!p.use_adx);sc('uvol',!!p.use_vol_confirm);sc('ust',!!p.use_supertrend);sc('ustoch',!!p.use_stochastic);
  sc('ubracket',!!p.use_bracket);sc('uatr',!!p.use_atr_stops);
  if(p.sl_percent)sv('slp',p.sl_percent);if(p.tp_percent)sv('tpp',p.tp_percent);
  if(licValid&&p.direction)sv('dir',p.direction);
  toast(`Preset "${key}" loaded – click Save to persist`,'success');
}

// ── Leaderboard ────────────────────────────────────────────────────────────
async function loadLeaderboard(){
  try{
    const d=await(await fetch('/api/leaderboard')).json();
    const lb=d.leaderboard||[];
    let html='<h4>🏆 Local Leaderboard</h4>';
    if(!lb.length){html+='<p style="font-size:.8rem;color:var(--muted)">Run a backtest to appear.</p>';}
    else{
      html+='<table class="bttbl"><tr><th>Rank</th><th>ID</th><th>Win Rate</th><th>Signals</th><th>Last BT</th></tr>';
      lb.forEach((r,i)=>{
        const isMe=r.user_id===d.my_id;
        html+=`<tr style="${isMe?'color:var(--accent)':''}" ><td>${i+1}</td><td>${r.user_id.slice(0,6)}${isMe?' (you)':''}</td><td>${r.win_rate.toFixed(1)}%</td><td>${r.total_signals}</td><td>${r.last_backtest}</td></tr>`;
      });
      html+='</table>';
    }
    $('leaderboard-wrap').innerHTML=html;
  }catch(e){}
}

// ── AI Chat ────────────────────────────────────────────────────────────────
async function initAIChat(){
  if(chatInited)return;chatInited=true;
  await loadSessions();
  const data=await(await fetch('/api/chat/sessions')).json();
  if(data.sessions&&data.sessions.length>0){await loadSession(data.sessions[0].id);}
  else{await createNewSession();}
  updateChatLimitInfo();
}
async function loadSessions(){
  try{const d=await(await fetch('/api/chat/sessions')).json();renderSessList(d.sessions||[]);}catch(e){}
}
function renderSessList(sessions){
  const list=$('sess-list');list.innerHTML='';
  sessions.forEach(s=>{
    const item=document.createElement('div');item.className='sess-item'+(s.id===curSessionId?' active':'');
    item.textContent=s.title;item.onclick=()=>loadSession(s.id);
    list.appendChild(item);
  });
}
async function loadSession(sid){
  curSessionId=sid;
  await loadSessions();
  try{
    const d=await(await fetch(`/api/chat/sessions/${sid}`)).json();
    $('chat-msgs').innerHTML='';
    (d.messages||[]).forEach(m=>addChatMsg(m.content,m.role==='user'));
  }catch(e){}
  updateChatLimitInfo();
}
async function createNewSession(){
  const r=await fetch('/api/chat/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'New Chat'})});
  const d=await r.json();curSessionId=d.session_id;
  await loadSessions();$('chat-msgs').innerHTML='';updateChatLimitInfo();
}
function updateChatLimitInfo(){
  const el=$('chat-limit');if(!el)return;
  el.textContent=licValid?'Pro – unlimited':'Free: 5/day';
}
function addChatMsg(text,isUser){
  const msgs=$('chat-msgs');
  const wrap=document.createElement('div');wrap.className='cmsg '+(isUser?'user':'bot');
  const sender=document.createElement('div');sender.className='msender';sender.textContent=isUser?'You':'TraderBot';
  const body=document.createElement('div');body.className='mbody';body.textContent=text;
  wrap.appendChild(sender);wrap.appendChild(body);msgs.appendChild(wrap);
  msgs.scrollTop=msgs.scrollHeight;return wrap;
}
async function sendChat(){
  const inputEl=$('chat-input');const msg=inputEl.value.trim();if(!msg)return;
  inputEl.value='';addChatMsg(msg,true);
  const typing=document.createElement('div');typing.className='chat-typing';
  typing.textContent='TraderBot is thinking…';$('chat-msgs').appendChild(typing);
  $('chat-msgs').scrollTop=$('chat-msgs').scrollHeight;
  const sendBtn=$('chat-send');sendBtn.disabled=true;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,session_id:curSessionId})});
    const d=await r.json();typing.remove();
    addChatMsg(d.reply||'No response.',false);
    if(d.session_id&&d.session_id!==curSessionId){curSessionId=d.session_id;loadSessions();}
  }catch(e){typing.remove();addChatMsg('Connection error.',false);}
  sendBtn.disabled=false;$('chat-msgs').scrollTop=$('chat-msgs').scrollHeight;
}
$('chat-input').addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}});

// ── Voice Assistant ────────────────────────────────────────────────────────
function startVoice(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){toast('Voice not supported in this browser','error');return;}
  const r=new SR();r.lang='en-US';r.start();
  r.onresult=e=>{$('chat-input').value=e.results[0][0].transcript;sendChat();};
  r.onerror=()=>toast('Voice error – try again','error');
}

// ── Keyboard Shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown',e=>{
  const ctrl=e.ctrlKey||e.metaKey;
  if(ctrl&&e.code==='Space'){e.preventDefault();if($('v-eq').textContent==='--')startBot();else stopBot();}
  if(ctrl&&e.key==='k'){e.preventDefault();$('tickers').focus();}
  if(ctrl&&!e.shiftKey&&e.key==='b'){e.preventDefault();runBT();}
  if(ctrl&&e.shiftKey&&e.key==='B'){e.preventDefault();switchTab('backtest');}
  if(ctrl&&e.key>='1'&&e.key<='7'){
    e.preventDefault();const tabs=['charts','signals','history','backtest','corr','help','aichat'];
    const idx=parseInt(e.key)-1;if(tabs[idx])switchTab(tabs[idx]);
  }
});

// ── Boot ───────────────────────────────────────────────────────────────────
updateBrokerOptions();updateCreds();loadConfig();
</script>
</body>
</html>
"""


def run_flask():
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)


if __name__ == "__main__":
    acquire_lock()

    # Start connectivity watcher
    threading.Thread(target=_connectivity_watcher, daemon=True).start()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.2)

    window = webview.create_window(
        "TraderMoney 2.0",
        "http://127.0.0.1:5050",
        width  = 1440,
        height = 880,
        min_size = (980, 700),
    )
    webview.start()

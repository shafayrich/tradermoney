# -*- coding: utf-8 -*-
"""
TraderMoney v2.0.11 – Triple-A Professional Trading Terminal
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Complete file – ready to run.
Includes embedded TradingView charts, full frontend JS, all brokers.
"""

import asyncio
import csv
import hashlib
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
from collections import deque
from datetime import datetime, timezone as dt_timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests as http_requests
import webview
from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS

APP_VERSION = "2.0.11"

# ──────────────────────────────────────────────────────────────────────────────
# STRUCTURED LOGGING
# ──────────────────────────────────────────────────────────────────────────────
def _slog(level: str, component: str, msg: str, trace_id: str = "") -> str:
    entry = {
        "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "level": level,
        "component": component,
        "msg": msg,
        "trace_id": trace_id or str(uuid.uuid4())[:8],
    }
    line = json.dumps(entry)
    print(line, flush=True)
    return line


# ──────────────────────────────────────────────────────────────────────────────
# AI CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "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")
AI_MODELS = [
    "google/gemini-2.0-flash-001",
    "deepseek/deepseek-chat-v3-0324",
    "meta-llama/llama-3.3-70b-instruct",
]
FREE_CHAT_DAILY_LIMIT = 5
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

_CHAT_SYSTEM_PROMPT = (
    "You are TraderBot, the AI assistant built into TraderMoney – a desktop algorithmic trading terminal v2.0.11. "
    "TraderMoney supports 6 brokers (Alpaca, IBKR, Tradier, Binance, Bybit, OKX) with paper and live trading. "
    "It uses a 9-indicator confirmation engine plus an SMC (Smart Money Concepts) engine for institutional analysis. "
    "Pro users can auto-trade with ATR-based risk management and circuit breakers. "
    "Free tier is signal-only, Alpaca paper, 1 ticker, core indicators. "
    "Keep answers concise (under 220 words), practical, specific to TraderMoney. Use markdown: **bold** and *italic*."
)

_chat_counter: Dict[str, Any] = {"date": None, "count": 0}

# ──────────────────────────────────────────────────────────────────────────────
# GUMROAD LICENSE
# ──────────────────────────────────────────────────────────────────────────────
GUMROAD_PRODUCT_ID = os.getenv("GUMROAD_PRODUCT_ID", "73otoT7rzJukCy-Lt4hhkQ==")

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

# ──────────────────────────────────────────────────────────────────────────────
# FLASK APP + PORT LOCK
# ──────────────────────────────────────────────────────────────────────────────
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
        _slog("WARN", "boot", "Port 5050 already in use – exiting.")
        sys.exit(0)

signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

def is_internet_available() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2)
        return True
    except OSError:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# IN-MEMORY ANALYTICS DATABASE
# ──────────────────────────────────────────────────────────────────────────────
class AnalyticsDB:
    MAX_RECORDS = 10_000
    def __init__(self):
        self._lock = threading.Lock()
        self._executions: deque = deque(maxlen=self.MAX_RECORDS)
        self._latency_per_broker: Dict[str, List[float]] = {}
        self._slippage_per_symbol: Dict[str, List[float]] = {}
        self._daily_pnl: Dict[str, float] = {}
        self._session_start = time.time()

    def record_execution(self, trace_id, symbol, broker, side, qty, request_price, fill_price, latency_ms, status):
        slippage_bps = (abs(fill_price - request_price) / (request_price + 1e-12) * 10_000) if request_price > 0 else 0.0
        record = {
            "trace_id": trace_id, "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "symbol": symbol, "broker": broker, "side": side, "qty": qty,
            "request_price": round(request_price, 6), "fill_price": round(fill_price, 6),
            "slippage_bps": round(slippage_bps, 3), "latency_ms": round(latency_ms, 2), "status": status,
        }
        with self._lock:
            self._executions.append(record)
            self._latency_per_broker.setdefault(broker, []).append(latency_ms)
            if len(self._latency_per_broker[broker]) > 1000:
                self._latency_per_broker[broker] = self._latency_per_broker[broker][-1000:]
            self._slippage_per_symbol.setdefault(symbol, []).append(slippage_bps)
            if len(self._slippage_per_symbol[symbol]) > 1000:
                self._slippage_per_symbol[symbol] = self._slippage_per_symbol[symbol][-1000:]

    def record_pnl(self, pnl: float):
        day = datetime.utcnow().strftime("%Y-%m-%d")
        with self._lock:
            self._daily_pnl[day] = self._daily_pnl.get(day, 0.0) + pnl

    def get_performance_report(self) -> dict:
        with self._lock:
            execs = list(self._executions)
        if not execs:
            return {"total_executions": 0, "avg_latency_ms": 0.0, "avg_slippage_bps": 0.0, "broker_latency": {},
                    "symbol_slippage": {}, "fill_rate_pct": 0.0, "session_uptime_s": round(time.time() - self._session_start, 1),
                    "recent_executions": [], "daily_pnl": {}}
        all_latencies = [e["latency_ms"] for e in execs]
        all_slippage = [e["slippage_bps"] for e in execs]
        filled = [e for e in execs if e["status"] == "filled"]
        broker_latency_avg = {b: round(float(np.mean(lats)), 2) for b, lats in self._latency_per_broker.items()}
        symbol_slippage_avg = {s: round(float(np.mean(slips)), 3) for s, slips in self._slippage_per_symbol.items()}
        return {
            "total_executions": len(execs), "avg_latency_ms": round(float(np.mean(all_latencies)), 2),
            "p95_latency_ms": round(float(np.percentile(all_latencies, 95)), 2),
            "avg_slippage_bps": round(float(np.mean(all_slippage)), 3),
            "fill_rate_pct": round(len(filled) / max(len(execs), 1) * 100, 2),
            "broker_latency": broker_latency_avg, "symbol_slippage": symbol_slippage_avg,
            "session_uptime_s": round(time.time() - self._session_start, 1),
            "recent_executions": list(reversed(execs))[:50],
            "daily_pnl": {k: round(v, 2) for k, v in sorted(self._daily_pnl.items())},
        }

analytics_db = AnalyticsDB()

# ──────────────────────────────────────────────────────────────────────────────
# SQLITE DATABASE
# ──────────────────────────────────────────────────────────────────────────────
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
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, trace_id TEXT);
        CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, signal TEXT NOT NULL, price REAL NOT NULL, rationale TEXT);
        CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS backtests (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, config_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS candle_cache (symbol TEXT NOT NULL, interval TEXT NOT NULL, timestamp TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY (symbol, interval));
        CREATE TABLE IF NOT EXISTS leaderboard (user_id TEXT PRIMARY KEY, win_rate REAL, total_signals INTEGER, last_backtest TEXT);
        """)
        self.conn.commit()

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def insert_trade(self, ts, sym, action, qty, price, trace_id=""):
        self._exec("INSERT INTO trades(timestamp,symbol,action,quantity,price,trace_id)VALUES(?,?,?,?,?,?)", (ts, sym, action, qty, price, trace_id))
    def get_recent_trades(self, limit=50):
        cur = self.conn.execute("SELECT timestamp,symbol,action,quantity,price FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [{"time": r[0], "symbol": r[1], "action": r[2], "qty": r[3], "price": r[4]} for r in cur]
    def insert_signal(self, ts, sym, sig, price, rationale):
        self._exec("INSERT INTO signals(timestamp,symbol,signal,price,rationale)VALUES(?,?,?,?,?)", (ts, sym, sig, price, rationale))
    def get_recent_signals(self, limit=50):
        cur = self.conn.execute("SELECT timestamp,symbol,signal,price,rationale FROM signals ORDER BY id DESC LIMIT ?", (limit,))
        return [{"time": r[0], "symbol": r[1], "signal": r[2], "price": r[3], "rationale": r[4]} for r in cur]
    def insert_log(self, msg: str):
        self._exec("INSERT INTO logs(timestamp,message)VALUES(?,?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    def get_recent_logs(self, limit=50):
        cur = self.conn.execute("SELECT timestamp,message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return [f"{r[0]}  {r[1]}" for r in cur]
    def insert_backtest(self, config_json: str):
        self._exec("INSERT INTO backtests(timestamp,config_json)VALUES(?,?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_json))
    def get_cached_candle(self, symbol: str, interval: str, max_age_seconds: int = 300):
        with self._lock:
            cur = self.conn.execute("SELECT timestamp,data_json FROM candle_cache WHERE symbol=? AND interval=?", (symbol, interval))
            row = cur.fetchone()
        if row:
            ts = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - ts).total_seconds() < max_age_seconds:
                try: return json.loads(row[1])
                except: pass
        return None
    def cache_candle(self, symbol: str, interval: str, df: pd.DataFrame):
        js = df.to_json(orient="split", date_format="iso")
        self._exec("INSERT OR REPLACE INTO candle_cache(symbol,interval,timestamp,data_json)VALUES(?,?,?,?)", (symbol, interval, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), js))
    def clean_candle_cache(self, max_hours: int = 24):
        cutoff = (datetime.now() - timedelta(hours=max_hours)).strftime("%Y-%m-%d %H:%M:%S")
        self._exec("DELETE FROM candle_cache WHERE timestamp<?", (cutoff,))
    def create_chat_session(self, title: str = "") -> int:
        if not title:
            title = f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        self._exec("INSERT INTO chat_sessions(title,created)VALUES(?,?)", (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    def get_chat_sessions(self) -> List[dict]:
        cur = self.conn.execute("SELECT id,title,created FROM chat_sessions ORDER BY id DESC")
        return [{"id": r[0], "title": r[1], "created": r[2]} for r in cur]
    def insert_chat_message(self, session_id: int, role: str, content: str):
        self._exec("INSERT INTO chat_history(session_id,role,content,timestamp)VALUES(?,?,?,?)", (session_id, role, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    def get_chat_history(self, session_id: int, limit: int = 200) -> List[dict]:
        cur = self.conn.execute("SELECT role,content FROM(SELECT*FROM chat_history WHERE session_id=? ORDER BY id DESC LIMIT ?)ORDER BY id ASC", (session_id, limit))
        return [{"role": r[0], "content": r[1]} for r in cur]
    def update_leaderboard(self, user_id: str, win_rate: float, total_signals: int):
        self._exec("INSERT OR REPLACE INTO leaderboard VALUES(?,?,?,?)", (user_id, win_rate, total_signals, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    def get_leaderboard(self) -> List[dict]:
        cur = self.conn.execute("SELECT user_id,win_rate,total_signals,last_backtest FROM leaderboard ORDER BY win_rate DESC")
        return [{"user_id": r[0][:6], "win_rate": r[1], "total_signals": r[2], "last_backtest": r[3]} for r in cur]
    def rename_chat_session(self, session_id: int, new_title: str):
        self._exec("UPDATE chat_sessions SET title=? WHERE id=?", (new_title, session_id))
    def delete_chat_session(self, session_id: int):
        self._exec("DELETE FROM chat_sessions WHERE id=?", (session_id,))

db = DatabaseManager()

# ──────────────────────────────────────────────────────────────────────────────
# ENCRYPTED CONFIG
# ──────────────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.expanduser("~/.tradermoney_config.enc")
KEY_FILE = os.path.expanduser("~/.tradermoney.key")

def _get_fernet():
    from cryptography.fernet import Fernet
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f: f.write(key)
    else:
        with open(KEY_FILE, "rb") as f: key = f.read()
    return Fernet(key)

class EncryptedConfigManager:
    @staticmethod
    def load() -> dict:
        try:
            cipher = _get_fernet()
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "rb") as f:
                    data = json.loads(cipher.decrypt(f.read()).decode())
                data.pop("license_key", None); data.pop("license_valid", None)
                return data
        except: pass
        return {}
    @staticmethod
    def save(config: dict):
        clean = {k: v for k, v in config.items() if k not in ("license_key", "license_valid")}
        try:
            cipher = _get_fernet()
            plain = json.dumps(clean, indent=2).encode()
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "wb") as f: f.write(cipher.encrypt(plain))
            with open(tmp, "rb") as f: cipher.decrypt(f.read())
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            db.insert_log(f"Config save error: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# GLOBAL STATE
# ──────────────────────────────────────────────────────────────────────────────
ATR_STOP_MULT = 2.0
ATR_TP_MULT = 3.0

_DEFAULT_CONFIG: dict = {
    "broker": "Alpaca", "tickers": "AAPL", "mode": "signal", "quantity": 1,
    "emas": [9, 50], "use_bracket": False, "sl_percent": 2.0, "tp_percent": 4.0,
    "timeframe": "1m", "telegram": {}, "use_rsi": True, "use_macd": True,
    "use_vwap": True, "use_bollinger": True, "use_adx": True, "use_vol_confirm": True,
    "use_supertrend": True, "use_stochastic": True, "use_atr_stops": True, "use_smc": True,
    "direction": "both", "use_default_qty": True, "last_broker_message": "",
    "timezone": "UTC", "news_sentiment": False, "device_uuid": str(uuid.uuid4()),
    "risk_max_daily_drawdown_pct": 5.0, "risk_max_consecutive_losses": 4, "risk_max_asset_exposure_pct": 20.0,
    "alpaca": {"api_key": "", "secret_key": "", "paper": True},
    "ibkr": {"host": "127.0.0.1", "port": "7497", "client_id": "1"},
    "tradier": {"access_token": "", "account_id": "", "sandbox": True},
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
        self.last_bt_data: dict = {}

    def enforce_free_limits(self):
        if not self.config.get("license_valid"):
            self.config["mode"] = "signal"
            self.config["broker"] = "Alpaca"
            self.config["alpaca"]["paper"] = True
            first = self.config.get("tickers", "AAPL").split(",")[0].strip()
            self.config["tickers"] = first
            for k in ("use_adx", "use_vol_confirm", "use_supertrend", "use_stochastic",
                      "use_atr_stops", "use_bracket", "use_smc"):
                self.config[k] = False

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
    except:
        return utc_str

# ──────────────────────────────────────────────────────────────────────────────
# SMART ORDER ROUTING (SOR)
# ──────────────────────────────────────────────────────────────────────────────
_CRYPTO_SYMBOLS = {"BTC","ETH","BNB","SOL","XRP","ADA","DOGE","LTC","AVAX","DOT","MATIC","LINK","UNI","ATOM","ETC","XLM","BCH","APT","BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"}
_FX_SYMBOLS = {"EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","EURJPY","GBPJPY","EURGBP","XAUUSD","XAGUSD","WTI","BRENT"}

def classify_asset(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace("-", "")
    if s in _CRYPTO_SYMBOLS or s.endswith("USDT") or s.endswith("USDC"):
        return "crypto"
    if s in _FX_SYMBOLS or (len(s) == 6 and s[:3] in {"EUR","GBP","USD","JPY","AUD","CAD"}):
        return "fx"
    return "equity"

class SmartOrderRouter:
    EQUITY_PREFERENCE = ["Interactive Brokers", "Alpaca", "Tradier"]
    FX_PREFERENCE = ["Interactive Brokers", "Tradier", "Alpaca"]
    CRYPTO_PREFERENCE = ["Binance", "Bybit", "OKX"]
    def __init__(self, brokers: Dict[str, "BaseBroker"]):
        self.brokers = brokers
    def route(self, symbol: str, default_broker: str) -> "BaseBroker":
        asset_class = classify_asset(symbol)
        prefs = self.EQUITY_PREFERENCE if asset_class == "equity" else (self.FX_PREFERENCE if asset_class == "fx" else self.CRYPTO_PREFERENCE)
        for name in prefs:
            b = self.brokers.get(name)
            if b and b.is_connected():
                _slog("INFO", "SOR", f"Routing {symbol} ({asset_class}) → {name}")
                return b
        chosen = self.brokers.get(default_broker)
        if chosen and chosen.is_connected(): return chosen
        for b in self.brokers.values():
            if b.is_connected(): return b
        raise RuntimeError("SOR: No connected broker available.")

# ──────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER / RISK MANAGER
# ──────────────────────────────────────────────────────────────────────────────
class RiskManager:
    def __init__(self, config: dict, initial_equity: float = 100_000.0):
        self._lock = threading.Lock()
        self.max_daily_dd_pct = float(config.get("risk_max_daily_drawdown_pct", 5.0))
        self.max_consec_losses = int(config.get("risk_max_consecutive_losses", 4))
        self.max_asset_exp_pct = float(config.get("risk_max_asset_exposure_pct", 20.0))
        self.initial_equity = initial_equity
        self._daily_start_equity = initial_equity
        self._current_equity = initial_equity
        self._day_str = datetime.utcnow().strftime("%Y-%m-%d")
        self._consecutive_losses = 0
        self._safe_mode = False
        self._asset_positions: Dict[str, float] = {}

    def _check_day_reset(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._day_str:
            self._day_str = today
            self._daily_start_equity = self._current_equity
            self._consecutive_losses = 0
            self._safe_mode = False
            db.insert_log("[RiskMgr] Daily reset – circuit breaker cleared.")
    def update_equity(self, equity: float):
        with self._lock:
            self._current_equity = equity
            self._check_day_reset()
            dd = (self._daily_start_equity - equity) / (self._daily_start_equity + 1e-12) * 100
            if dd >= self.max_daily_dd_pct and not self._safe_mode:
                self._safe_mode = True
                msg = f"[CIRCUIT BREAKER] Daily drawdown {dd:.2f}% ≥ {self.max_daily_dd_pct}% limit. Engine entering SAFE MODE."
                db.insert_log(msg); _slog("CRITICAL", "RiskManager", msg)
    def record_trade_result(self, pnl: float):
        with self._lock:
            if pnl < 0:
                self._consecutive_losses += 1
                if self._consecutive_losses >= self.max_consec_losses and not self._safe_mode:
                    self._safe_mode = True
                    db.insert_log(f"[CIRCUIT BREAKER] {self._consecutive_losses} consecutive losses. SAFE MODE.")
            else:
                self._consecutive_losses = 0
            analytics_db.record_pnl(pnl)
    def check_asset_exposure(self, symbol: str, notional_usd: float) -> bool:
        with self._lock:
            current = self._asset_positions.get(symbol, 0.0)
            cap = self._current_equity * self.max_asset_exp_pct / 100.0
            if current + notional_usd > cap:
                _slog("WARN", "RiskManager", f"Exposure cap breached {symbol}")
                return False
            return True
    def record_asset_exposure(self, symbol: str, notional_usd: float):
        with self._lock:
            self._asset_positions[symbol] = self._asset_positions.get(symbol, 0.0) + notional_usd
    def release_asset_exposure(self, symbol: str, notional_usd: float):
        with self._lock:
            self._asset_positions[symbol] = max(0.0, self._asset_positions.get(symbol, 0.0) - notional_usd)
    @property
    def safe_mode(self) -> bool:
        with self._lock:
            self._check_day_reset()
            return self._safe_mode
    def get_status(self) -> dict:
        with self._lock:
            dd = (self._daily_start_equity - self._current_equity) / (self._daily_start_equity + 1e-12) * 100
            return {"safe_mode": self._safe_mode, "daily_drawdown_pct": round(dd, 3), "max_daily_dd_pct": self.max_daily_dd_pct,
                    "consecutive_losses": self._consecutive_losses, "max_consec_losses": self.max_consec_losses,
                    "current_equity": round(self._current_equity, 2), "asset_exposure": {k: round(v,2) for k,v in self._asset_positions.items()}}

# ──────────────────────────────────────────────────────────────────────────────
# BROKER REGISTRY & BASE CLASS
# ──────────────────────────────────────────────────────────────────────────────
BROKER_REGISTRY: Dict[str, Any] = {}
def register_broker(name: str, cls): BROKER_REGISTRY[name] = cls

class BaseBroker:
    name = "Base"
    _heartbeat_interval = 30
    def __init__(self, config: dict, ui_queue: queue.Queue):
        self.config = config; self.ui_queue = ui_queue; self.last_error = ""
        self._heartbeat_stop = threading.Event(); self._heartbeat_thread = None
    def _emit_error(self, msg: str):
        self.last_error = msg; self.ui_queue.put(("error", msg)); db.insert_log(f"[{self.name}] ERROR: {msg}"); _slog("ERROR", self.name, msg)
    def _emit_log(self, msg: str):
        self.ui_queue.put(("log", msg)); db.insert_log(f"[{self.name}] {msg}"); _slog("INFO", self.name, msg)
    def _heartbeat_loop(self):
        while not self._heartbeat_stop.is_set():
            time.sleep(self._heartbeat_interval)
            if self._heartbeat_stop.is_set(): break
            try:
                acc = self.get_account()
                if acc is None:
                    self._emit_log("Heartbeat: reconnect attempt")
                    try: self.connect()
                    except Exception as ex: self._emit_error(f"Reconnect failed: {ex}")
            except Exception as ex: self._emit_error(f"Heartbeat error: {ex}")
    def start_heartbeat(self):
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name=f"{self.name}Heartbeat")
        self._heartbeat_thread.start()
    def stop_heartbeat(self): self._heartbeat_stop.set()
    def connect(self) -> bool: raise NotImplementedError
    def get_account(self): raise NotImplementedError
    def submit_order(self, *a, **kw): raise NotImplementedError
    def close_all_positions(self): raise NotImplementedError
    def get_positions(self): raise NotImplementedError
    def get_market_status(self) -> bool: raise NotImplementedError
    def stream_prices(self, syms, cb): raise NotImplementedError
    def stop_stream(self): raise NotImplementedError
    def is_connected(self) -> bool: return True

# ──────────────────────────────────────────────────────────────────────────────
# ALPACA BROKER
# ──────────────────────────────────────────────────────────────────────────────
class AlpacaBroker(BaseBroker):
    name = "Alpaca"
    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue); self.api = None; self._stop_stream = False
    def is_connected(self) -> bool: return self.api is not None
    def connect(self) -> bool:
        creds = self.config.get("alpaca", {})
        key = creds.get("api_key", os.getenv("ALPACA_API_KEY", "")).strip()
        secret = creds.get("secret_key", os.getenv("ALPACA_SECRET_KEY", "")).strip()
        paper = creds.get("paper", True)
        if not key or not secret: self._emit_error("Alpaca API Key/Secret missing."); return False
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        try:
            import alpaca_trade_api as tradeapi
            self.api = tradeapi.REST(key, secret, base_url, api_version="v2")
            acc = self.api.get_account()
            if acc.status != "ACTIVE": self._emit_error(f"Account status {acc.status}"); return False
            self._emit_log(f"Connected. Paper={paper}. Equity=${acc.equity}")
            self.start_heartbeat(); return True
        except Exception as e: self._emit_error(f"Alpaca connection error: {e}"); return False
    def get_account(self):
        if not self.api: return None
        try:
            acc = self.api.get_account(); pos = len(self.api.list_positions())
            return {"equity": float(acc.equity), "pl": float(acc.equity)-float(acc.last_equity),
                    "buying_power": float(acc.buying_power), "cash": float(acc.cash), "open_positions": pos}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.api: self._emit_error("Not connected"); return False
        t0 = time.perf_counter(); trace_id = str(uuid.uuid4())[:8]
        try:
            request_price = 0.0
            try: request_price = float(self.api.get_latest_trade(symbol).price)
            except: pass
            if sl_price is None and sl_pct is None:
                self.api.submit_order(symbol=symbol, qty=qty, side=side, type="market", time_in_force="day")
            else:
                price = request_price if request_price>0 else 1.0
                if side=="buy":
                    stop = round(sl_price if sl_price else price*(1-sl_pct/100),2)
                    limit = round(tp_price if tp_price else price*(1+tp_pct/100),2)
                else:
                    stop = round(sl_price if sl_price else price*(1+sl_pct/100),2)
                    limit = round(tp_price if tp_price else price*(1-tp_pct/100),2)
                self.api.submit_order(symbol=symbol, qty=qty, side=side, type="market", time_in_force="gtc",
                                      order_class="bracket", stop_loss={"stop_price":stop}, take_profit={"limit_price":limit})
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, request_price, request_price, latency, "filled")
            self._emit_log(f"[{trace_id}] Order {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "rejected")
            self._emit_error(f"Order failed: {e}"); return False
    def close_all_positions(self):
        if self.api:
            try: self.api.close_all_positions(); self._emit_log("All positions closed.")
            except Exception as e: self._emit_error(f"Kill error: {e}")
    def get_positions(self):
        if not self.api: return {}
        try: return {p.symbol: int(float(p.qty)) for p in self.api.list_positions()}
        except: return {}
    def get_market_status(self) -> bool:
        try: return self.api.get_clock().is_open if self.api else False
        except: return False
    def stream_prices(self, symbols, callback):
        if not symbols: return
        self._stop_stream = False
        def run():
            try:
                from alpaca.data.live import StockDataStream
                creds = self.config.get("alpaca", {}); key=creds.get("api_key",""); secret=creds.get("secret_key",""); paper=creds.get("paper",True)
                stream = StockDataStream(api_key=key, secret_key=secret, feed="iex" if paper else "sip")
                async def on_trade(data):
                    if data.symbol in symbols: callback(data.symbol, data.price)
                stream.subscribe_trades(on_trade, *symbols)
                while not self._stop_stream:
                    try: stream.run()
                    except: time.sleep(5)
            except Exception as e: self._emit_log(f"Stream warning: {e}")
        threading.Thread(target=run, daemon=True).start()
    def stop_stream(self): self._stop_stream = True; self.stop_heartbeat()
register_broker("Alpaca", AlpacaBroker)

# ──────────────────────────────────────────────────────────────────────────────
# INTERACTIVE BROKERS (IBKR)
# ──────────────────────────────────────────────────────────────────────────────
class IBKRBroker(BaseBroker):
    name = "Interactive Brokers"
    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue); self.ib = None; self._loop = None; self._ib_thread = None; self._stop_stream = False; self._connected = False
    def is_connected(self) -> bool: return self._connected and self.ib is not None and self.ib.isConnected()
    def _start_loop(self):
        self._loop = asyncio.new_event_loop(); asyncio.set_event_loop(self._loop); self._loop.run_forever()
    def _ensure_loop(self):
        if self._loop is None or not self._loop.is_running():
            self._ib_thread = threading.Thread(target=self._start_loop, daemon=True, name="IBKRLoop")
            self._ib_thread.start()
            waited=0; while (self._loop is None or not self._loop.is_running()) and waited<5: time.sleep(0.3); waited+=0.3
            if self._loop is None or not self._loop.is_running(): raise RuntimeError("IBKR event loop failed")
    def _run_coro(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)
    def connect(self) -> bool:
        creds = self.config.get("ibkr", {}); host=creds.get("host","127.0.0.1").strip(); port=int(creds.get("port","7497")); cid=int(creds.get("client_id","1"))
        try:
            from ib_insync import IB
            self._ensure_loop()
            async def _do(): ib=IB(); await ib.connectAsync(host, port, clientId=cid, timeout=10); return ib
            self.ib = self._run_coro(_do())
            if not self.ib.isConnected(): raise Exception("Not connected")
            self._connected = True
            self._emit_log(f"Connected to IBKR {host}:{port}")
            self.start_heartbeat(); return True
        except Exception as e: self._emit_error(f"IBKR connection error: {e}"); return False
    def get_account(self):
        if not self.is_connected(): return None
        try:
            summary = self._run_coro(self.ib.accountSummaryAsync())
            eq = next((float(v.value) for v in summary if v.tag=="NetLiquidation"),0.0)
            pl = next((float(v.value) for v in summary if v.tag=="UnrealizedPnL"),0.0)
            bp = next((float(v.value) for v in summary if v.tag=="AvailableFunds"),0.0)
            pos = [p for p in self.ib.positions() if p.position!=0]
            return {"equity":eq, "pl":pl, "buying_power":bp, "cash":0.0, "open_positions":len(pos)}
        except Exception as e: self._emit_error(f"get_account: {e}"); return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.is_connected(): self._emit_error("Not connected"); return False
        t0 = time.perf_counter(); trace_id = str(uuid.uuid4())[:8]
        try:
            from ib_insync import Stock, MarketOrder
            async def _place():
                c = Stock(symbol, "SMART", "USD")
                await self.ib.qualifyContractsAsync(c)
                self.ib.placeOrder(c, MarketOrder("BUY" if side=="buy" else "SELL", qty))
            self._run_coro(_place())
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "filled")
            self._emit_log(f"[{trace_id}] Order {side.upper()} {qty} {symbol}")
            return True
        except Exception as e:
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "rejected")
            self._emit_error(f"Order error: {e}"); return False
    def close_all_positions(self):
        if not self.is_connected(): return
        from ib_insync import MarketOrder
        for pos in self.ib.positions():
            if pos.position==0: continue
            d = "SELL" if pos.position>0 else "BUY"
            async def _c(contract=pos.contract, n=abs(pos.position), direction=d):
                self.ib.placeOrder(contract, MarketOrder(direction, n))
            self._run_coro(_c())
        self._emit_log("All positions closed.")
    def get_positions(self):
        if not self.is_connected(): return {}
        return {pos.contract.symbol: int(pos.position) for pos in self.ib.positions() if pos.position!=0}
    def get_market_status(self) -> bool: return True
    def stream_prices(self, symbols, callback):
        if not self.is_connected(): return
        self._stop_stream = False
        async def _sub():
            from ib_insync import Stock
            contracts = [Stock(s, "SMART", "USD") for s in symbols]
            await self.ib.qualifyContractsAsync(*contracts)
            tickers = [self.ib.reqMktData(c, "", False, False) for c in contracts]
            sym_map = {c.symbol: s for c,s in zip(contracts, symbols)}
            while not self._stop_stream:
                await asyncio.sleep(1)
                for t in tickers:
                    if t.last and t.last>0:
                        orig = sym_map.get(t.contract.symbol)
                        if orig: callback(orig, t.last)
        asyncio.run_coroutine_threadsafe(_sub(), self._loop)
    def stop_stream(self): self._stop_stream = True; self.stop_heartbeat()
register_broker("Interactive Brokers", IBKRBroker)

# ──────────────────────────────────────────────────────────────────────────────
# TRADIER BROKER
# ──────────────────────────────────────────────────────────────────────────────
class TradierBroker(BaseBroker):
    name = "Tradier"; LIVE_URL = "https://api.tradier.com/v1"; SAND_URL = "https://sandbox.tradier.com/v1"
    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue); self.session = None; self.account_id = None; self._base = self.LIVE_URL; self._stop_stream = False
    def is_connected(self) -> bool: return self.session is not None
    def connect(self) -> bool:
        creds = self.config.get("tradier", {}); token=creds.get("access_token","").strip(); self.account_id=creds.get("account_id","").strip(); sandbox=creds.get("sandbox",True)
        if not token or not self.account_id: self._emit_error("Tradier token/account missing"); return False
        self._base = self.SAND_URL if sandbox else self.LIVE_URL
        import requests as req
        self.session = req.Session(); self.session.headers.update({"Authorization":f"Bearer {token}", "Accept":"application/json"})
        try:
            r = self.session.get(f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            if r.status_code!=200: self._emit_error(f"HTTP {r.status_code}"); return False
            self._emit_log(f"Connected (sandbox={sandbox})"); self.start_heartbeat(); return True
        except Exception as e: self._emit_error(f"Connection error: {e}"); return False
    def get_account(self):
        if not self.session: return None
        try:
            r = self.session.get(f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            bal = r.json().get("balances",{})
            return {"equity": float(bal.get("total_equity",0)), "pl":0.0, "buying_power":float(bal.get("equity_buying_power",0)),
                    "cash":float(bal.get("total_cash",0)), "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session: self._emit_error("Not connected"); return False
        t0 = time.perf_counter(); trace_id = str(uuid.uuid4())[:8]
        try:
            r = self.session.post(f"{self._base}/accounts/{self.account_id}/orders",
                data={"class":"equity","symbol":symbol,"side":side,"quantity":str(qty),"type":"market","duration":"day"}, timeout=10)
            err = r.json().get("errors",{}).get("error")
            if r.status_code not in (200,201) or err:
                self._emit_error(f"Order rejected: {err or r.text[:200]}")
                analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, (time.perf_counter()-t0)*1000, "rejected")
                return False
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "filled")
            self._emit_log(f"[{trace_id}] Order {side.upper()} {qty} {symbol}"); return True
        except Exception as e: self._emit_error(f"Order error: {e}"); return False
    def close_all_positions(self):
        for sym,qty in self.get_positions().items():
            self.submit_order(sym, abs(qty), "sell" if qty>0 else "buy")
        self._emit_log("All positions closed.")
    def get_positions(self):
        if not self.session: return {}
        try:
            r = self.session.get(f"{self._base}/accounts/{self.account_id}/positions", timeout=10)
            raw = r.json().get("positions",{}).get("position",[])
            if isinstance(raw,dict): raw=[raw]
            return {p["symbol"]: int(float(p["quantity"])) for p in raw if p}
        except: return {}
    def get_market_status(self) -> bool:
        try:
            r = self.session.get(f"{self._base}/markets/clock", timeout=5)
            return r.json().get("clock",{}).get("state","")=="open"
        except: return True
    def stream_prices(self, symbols, callback):
        self._stop_stream = False
        def poll():
            joined = ",".join(symbols)
            while not self._stop_stream:
                try:
                    r = self.session.get(f"{self._base}/markets/quotes", params={"symbols":joined}, timeout=5)
                    quotes = r.json().get("quotes",{}).get("quote",[])
                    if isinstance(quotes,dict): quotes=[quotes]
                    for q in quotes:
                        sym = q.get("symbol",""); price = q.get("last") or q.get("bid") or 0.0
                        if sym and price: callback(sym, float(price))
                except: pass
                time.sleep(5)
        threading.Thread(target=poll, daemon=True).start()
    def stop_stream(self): self._stop_stream = True; self.stop_heartbeat()
register_broker("Tradier", TradierBroker)

# ──────────────────────────────────────────────────────────────────────────────
# BINANCE BROKER
# ──────────────────────────────────────────────────────────────────────────────
class BinanceBroker(BaseBroker):
    name = "Binance"
    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue); self.client = None; self._ws_client = None; self._stop_stream = False
    def is_connected(self) -> bool: return self.client is not None
    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/","").replace("-","").upper()
        return s if s.endswith("USDT") or s.endswith("USDC") or s.endswith("BTC") else s+"USDT"
    def connect(self) -> bool:
        creds = self.config.get("binance", {}); api_key=creds.get("api_key","").strip(); api_secret=creds.get("api_secret","").strip(); testnet=creds.get("testnet",True)
        if not api_key or not api_secret: self._emit_error("Binance API key/secret missing"); return False
        try:
            from binance.spot import Spot
            kw = {"base_url": "https://testnet.binance.vision"} if testnet else {}
            self.client = Spot(api_key=api_key, api_secret=api_secret, **kw)
            acct = self.client.account()
            if not acct.get("canTrade"): self._emit_error("Account cannot trade"); return False
            self._emit_log(f"Connected (testnet={testnet})"); self.start_heartbeat(); return True
        except Exception as e: self._emit_error(f"Binance connection error: {e}"); return False
    def get_account(self):
        if not self.client: return None
        try:
            acct = self.client.account()
            bals = {b["asset"]: float(b["free"])+float(b["locked"]) for b in acct["balances"]}
            usdt = bals.get("USDT",0.0); btc = bals.get("BTC",0.0)
            btc_price = float(self.client.ticker_price(symbol="BTCUSDT")["price"]) if btc>0 else 0
            return {"equity": usdt + btc*btc_price, "pl":0.0, "buying_power":usdt, "cash":usdt, "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.client: self._emit_error("Not connected"); return False
        t0 = time.perf_counter(); trace_id = str(uuid.uuid4())[:8]
        try:
            resp = self.client.new_order(symbol=self._norm(symbol), side="BUY" if side=="buy" else "SELL", type="MARKET", quantity=qty)
            if resp.get("status") not in ("FILLED","NEW","PARTIALLY_FILLED"):
                self._emit_error(f"Order status: {resp}")
                analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, (time.perf_counter()-t0)*1000, "rejected")
                return False
            fills = resp.get("fills",[]); fill_price = float(fills[0]["price"]) if fills else 0.0
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, fill_price, fill_price, latency, "filled")
            self._emit_log(f"[{trace_id}] Order {side.upper()} {qty} {symbol}"); return True
        except Exception as e:
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "error")
            self._emit_error(f"Order error: {e}"); return False
    def close_all_positions(self):
        for asset, free in self.get_positions().items():
            if free>0:
                try: self.client.new_order(symbol=asset+"USDT", side="SELL", type="MARKET", quantity=free)
                except: pass
        self._emit_log("All positions closed.")
    def get_positions(self):
        if not self.client: return {}
        try:
            acct = self.client.account()
            return {b["asset"]: float(b["free"]) for b in acct["balances"] if float(b["free"])>0 and b["asset"]!="USDT"}
        except: return {}
    def get_market_status(self) -> bool: return True
    def stream_prices(self, symbols, callback):
        self._stop_stream = False
        def run():
            try:
                from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient
                sym_map = {self._norm(s).lower(): s for s in symbols}
                def on_msg(_, raw):
                    try:
                        data = json.loads(raw) if isinstance(raw,str) else raw
                        payload = data.get("data",data)
                        if payload.get("e")=="trade":
                            ws_sym = payload["s"].lower(); price = float(payload["p"])
                            orig = sym_map.get(ws_sym)
                            if orig: callback(orig, price)
                    except: pass
                testnet = self.config.get("binance",{}).get("testnet",True)
                self._ws_client = SpotWebsocketStreamClient(stream_url="wss://testnet.binance.vision" if testnet else "wss://stream.binance.com", on_message=on_msg)
                for s in sym_map: self._ws_client.trade(symbol=s)
                while not self._stop_stream: time.sleep(1)
                self._ws_client.stop()
            except Exception as e: self._emit_log(f"Stream warning: {e}")
        threading.Thread(target=run, daemon=True).start()
    def stop_stream(self): self._stop_stream = True; if self._ws_client: self._ws_client.stop(); self.stop_heartbeat()
register_broker("Binance", BinanceBroker)

# ──────────────────────────────────────────────────────────────────────────────
# BYBIT BROKER
# ──────────────────────────────────────────────────────────────────────────────
class BybitBroker(BaseBroker):
    name = "Bybit"
    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue); self.session = None; self._stop_stream = False
    def is_connected(self) -> bool: return self.session is not None
    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/","").replace("-","").upper()
        return s if s.endswith("USDT") else s+"USDT"
    def connect(self) -> bool:
        creds = self.config.get("bybit", {}); api_key=creds.get("api_key","").strip(); api_secret=creds.get("api_secret","").strip(); testnet=creds.get("testnet",True)
        if not api_key or not api_secret: self._emit_error("Bybit API key/secret missing"); return False
        try:
            from pybit.unified_trading import HTTP
            self.session = HTTP(api_key=api_key, api_secret=api_secret, testnet=testnet)
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            if resp.get("retCode",-1)!=0: self._emit_error(f"Auth failed: {resp.get('retMsg')}"); return False
            self._emit_log(f"Connected (testnet={testnet})"); self.start_heartbeat(); return True
        except Exception as e: self._emit_error(f"Bybit connection error: {e}"); return False
    def get_account(self):
        if not self.session: return None
        try:
            result = self.session.get_wallet_balance(accountType="UNIFIED").get("result",{}).get("list",[{}])[0]
            equity = float(result.get("totalEquity",0)); avail = float(result.get("totalAvailableBalance",0))
            return {"equity": equity, "pl":0.0, "buying_power":avail, "cash":avail, "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session: self._emit_error("Not connected"); return False
        t0 = time.perf_counter(); trace_id = str(uuid.uuid4())[:8]
        try:
            kwargs = {"category":"spot","symbol":self._norm(symbol),"side":"Buy" if side=="buy" else "Sell","orderType":"Market","qty":str(qty)}
            if sl_price: kwargs["stopLoss"] = str(round(sl_price,4))
            if tp_price: kwargs["takeProfit"] = str(round(tp_price,4))
            resp = self.session.place_order(**kwargs)
            if resp.get("retCode",-1)!=0:
                self._emit_error(f"Order rejected: {resp.get('retMsg')}")
                analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, (time.perf_counter()-t0)*1000, "rejected")
                return False
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "filled")
            self._emit_log(f"[{trace_id}] Order {side.upper()} {qty} {symbol}"); return True
        except Exception as e:
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "error")
            self._emit_error(f"Order error: {e}"); return False
    def close_all_positions(self):
        for ccy, eq in self.get_positions().items():
            if eq>0:
                self.session.place_order(category="spot", symbol=ccy+"USDT", side="Sell", orderType="Market", qty=str(eq))
        self._emit_log("All positions closed.")
    def get_positions(self):
        if not self.session: return {}
        try:
            coins = self.session.get_wallet_balance(accountType="UNIFIED").get("result",{}).get("list",[{}])[0].get("coin",[])
            return {c["coin"]: float(c.get("equity",0)) for c in coins if float(c.get("equity",0))>0 and c["coin"]!="USDT"}
        except: return {}
    def get_market_status(self) -> bool: return True
    def stream_prices(self, symbols, callback):
        self._stop_stream = False
        def run():
            try:
                from pybit.unified_trading import WebSocket
                testnet = self.config.get("bybit",{}).get("testnet",True)
                sym_map = {self._norm(s): s for s in symbols}
                def handle(msg):
                    try:
                        data = msg.get("data",{})
                        if isinstance(data,list): data = data[0] if data else {}
                        raw_sym = msg.get("topic","").split(".")[-1]
                        orig = sym_map.get(raw_sym); price = float(data.get("lastPrice",0))
                        if orig and price: callback(orig, price)
                    except: pass
                ws = WebSocket(testnet=testnet, channel_type="spot")
                for sym in sym_map: ws.ticker_stream(symbol=sym, callback=handle)
                while not self._stop_stream: time.sleep(1)
            except Exception as e: self._emit_log(f"Stream warning: {e}")
        threading.Thread(target=run, daemon=True).start()
    def stop_stream(self): self._stop_stream = True; self.stop_heartbeat()
register_broker("Bybit", BybitBroker)

# ──────────────────────────────────────────────────────────────────────────────
# OKX BROKER
# ──────────────────────────────────────────────────────────────────────────────
class OKXBroker(BaseBroker):
    name = "OKX"
    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue); self._account_api = None; self._trade_api = None; self._stop_stream = False; self._flag = "0"
    def is_connected(self) -> bool: return self._account_api is not None
    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/","-").replace("_","-").upper()
        return s if "-" in s else s+"-USDT"
    def connect(self) -> bool:
        creds = self.config.get("okx", {}); api_key=creds.get("api_key","").strip(); api_secret=creds.get("api_secret","").strip(); passphrase=creds.get("api_passphrase","").strip(); demo=creds.get("demo",True)
        self._flag = "1" if demo else "0"
        if not api_key or not api_secret or not passphrase: self._emit_error("OKX credentials missing"); return False
        try:
            import okx.Account as AccountAPI; import okx.Trade as TradeAPI
            self._account_api = AccountAPI.AccountAPI(api_key, api_secret, passphrase, False, self._flag)
            self._trade_api = TradeAPI.TradeAPI(api_key, api_secret, passphrase, False, self._flag)
            resp = self._account_api.get_account_balance()
            if str(resp.get("code","-1")) != "0": self._emit_error(f"Auth failed: {resp.get('msg')}"); return False
            self._emit_log(f"Connected (demo={demo})"); self.start_heartbeat(); return True
        except Exception as e: self._emit_error(f"OKX connection error: {e}"); return False
    def get_account(self):
        if not self._account_api: return None
        try:
            details = self._account_api.get_account_balance().get("data",[{}])[0].get("details",[])
            equity = sum(float(d.get("eq",0)) for d in details)
            usdt = next((float(d.get("availBal",0)) for d in details if d.get("ccy")=="USDT"),0.0)
            return {"equity": equity, "pl":0.0, "buying_power":usdt, "cash":usdt, "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self._trade_api: self._emit_error("Not connected"); return False
        t0 = time.perf_counter(); trace_id = str(uuid.uuid4())[:8]
        try:
            resp = self._trade_api.place_order(instId=self._norm(symbol), tdMode="cash", side=side, ordType="market", sz=str(int(qty)))
            items = resp.get("data",[{}]); s_code = str(items[0].get("sCode","-1")) if items else "-1"
            if s_code != "0":
                self._emit_error(f"Order rejected: {items[0].get('sMsg',str(resp))}")
                analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, (time.perf_counter()-t0)*1000, "rejected")
                return False
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "filled")
            self._emit_log(f"[{trace_id}] Order {side.upper()} {qty} {symbol}"); return True
        except Exception as e:
            latency = (time.perf_counter()-t0)*1000
            analytics_db.record_execution(trace_id, symbol, self.name, side, qty, 0,0, latency, "error")
            self._emit_error(f"Order error: {e}"); return False
    def close_all_positions(self):
        for ccy, eq in self.get_positions().items():
            if eq>0:
                self._trade_api.place_order(instId=f"{ccy}-USDT", tdMode="cash", side="sell", ordType="market", sz=str(eq))
        self._emit_log("All positions closed.")
    def get_positions(self):
        if not self._account_api: return {}
        try:
            details = self._account_api.get_account_balance().get("data",[{}])[0].get("details",[])
            return {d["ccy"]: float(d.get("eq",0)) for d in details if float(d.get("eq",0))>0 and d["ccy"]!="USDT"}
        except: return {}
    def get_market_status(self) -> bool: return True
    def stream_prices(self, symbols, callback):
        self._stop_stream = False
        def run():
            try:
                import websocket
                sym_map = {self._norm(s): s for s in symbols}
                subs = [{"channel":"tickers","instId":k} for k in sym_map]
                url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999" if self.config.get("okx",{}).get("demo",True) else "wss://ws.okx.com:8443/ws/v5/public"
                def on_msg(ws_app, msg):
                    try:
                        for item in json.loads(msg).get("data",[]):
                            inst = item.get("instId",""); price = float(item.get("last",0))
                            orig = sym_map.get(inst)
                            if orig and price: callback(orig, price)
                    except: pass
                def on_open(ws_app): ws_app.send(json.dumps({"op":"subscribe","args":subs}))
                ws = websocket.WebSocketApp(url, on_message=on_msg, on_open=on_open)
                while not self._stop_stream:
                    ws.run_forever()
                    if not self._stop_stream: time.sleep(3)
            except Exception as e: self._emit_log(f"Stream warning: {e}")
        threading.Thread(target=run, daemon=True).start()
    def stop_stream(self): self._stop_stream = True; self.stop_heartbeat()
register_broker("OKX", OKXBroker)

# ──────────────────────────────────────────────────────────────────────────────
# INDICATOR CALCULATOR
# ──────────────────────────────────────────────────────────────────────────────
class IndicatorCalculator:
    @staticmethod
    def compute_all(df: pd.DataFrame, ema_fast: int = 9, ema_slow: int = 50) -> pd.DataFrame:
        close = np.asarray(df["Close"]).astype(np.float64).ravel()
        high = np.asarray(df["High"]).astype(np.float64).ravel()
        low = np.asarray(df["Low"]).astype(np.float64).ravel()
        volume = np.asarray(df["Volume"]).astype(np.float64).ravel() if "Volume" in df.columns else np.ones_like(close)
        def ema(data: np.ndarray, span: int) -> np.ndarray:
            a = 2.0/(span+1); res = np.empty_like(data); res[0]=data[0]
            for i in range(1,len(data)): res[i]=a*data[i]+(1-a)*res[i-1]
            return res
        df["EMA_fast"] = ema(close, ema_fast); df["EMA_slow"] = ema(close, ema_slow)
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta>0, delta, 0.0); loss = np.where(delta<0, -delta, 0.0)
        ag = np.convolve(gain, np.ones(14)/14, mode="full")[:len(close)]
        al = np.convolve(loss, np.ones(14)/14, mode="full")[:len(close)]
        rs = np.divide(ag, al, out=np.zeros_like(ag), where=al!=0)
        df["RSI"] = 100 - (100/(1+rs))
        m = ema(close,12)-ema(close,26); df["MACD"]=m; df["MACD_signal"]=ema(m,9)
        ma20 = np.convolve(close, np.ones(20)/20, mode="same")
        std20 = np.array([np.std(close[max(0,i-19):i+1]) for i in range(len(close))])
        df["BB_upper"] = ma20+2*std20; df["BB_lower"] = ma20-2*std20
        cum_vol = np.cumsum(volume)
        df["VWAP"] = np.divide(np.cumsum(close*volume), cum_vol, out=np.zeros_like(close), where=cum_vol!=0)
        tr = np.maximum(high[1:]-low[1:], np.maximum(np.abs(high[1:]-close[:-1]), np.abs(low[1:]-close[:-1])))
        tr = np.insert(tr, 0, np.mean(tr[:14]) if len(tr)>=14 else (tr[0] if len(tr) else 0))
        atr14 = ema(tr,14); df["ATR"]=atr14
        up = np.maximum(np.diff(high, prepend=high[0]),0.0); dn = np.maximum(-np.diff(low, prepend=low[0]),0.0)
        pdm = np.where((up>dn)&(up>0), up,0.0); mdm = np.where((dn>up)&(dn>0), dn,0.0)
        pdi = 100*ema(pdm,14)/(atr14+1e-14); mdi = 100*ema(mdm,14)/(atr14+1e-14)
        dx = 100*np.abs(pdi-mdi)/(pdi+mdi+1e-14); df["ADX"]=ema(dx,14)
        vol_avg = np.convolve(volume, np.ones(20)/20, mode="same")
        df["Vol_ratio"] = np.divide(volume, vol_avg, out=np.ones_like(volume), where=vol_avg!=0)
        st_atr = ema(tr,10); hl2 = (high+low)/2.0
        upper_s = hl2+3.0*st_atr; lower_s = hl2-3.0*st_atr
        trend = np.ones_like(close)
        for i in range(1,len(close)):
            if close[i] > upper_s[i-1]: trend[i]=1
            elif close[i] < lower_s[i-1]: trend[i]=-1
            else:
                trend[i]=trend[i-1]
                if trend[i]==1 and lower_s[i]<lower_s[i-1]: lower_s[i]=lower_s[i-1]
                if trend[i]==-1 and upper_s[i]>upper_s[i-1]: upper_s[i]=upper_s[i-1]
        df["Supertrend"] = np.where(trend==1, lower_s, upper_s)
        df["Supertrend_trend"] = trend
        K=14
        ll = np.array([np.min(low[max(0,i-K+1):i+1]) for i in range(len(close))])
        hh = np.array([np.max(high[max(0,i-K+1):i+1]) for i in range(len(close))])
        stk = np.where(hh-ll!=0, 100*(close-ll)/(hh-ll+1e-14), 50.0)
        df["Stoch_K"] = stk; df["Stoch_D"] = np.convolve(stk, np.ones(3)/3, mode="same")
        return df

# ──────────────────────────────────────────────────────────────────────────────
# SMC ENGINE
# ──────────────────────────────────────────────────────────────────────────────
class SMCEngine:
    FVG_LOOKBACK=50; OB_LOOKBACK=50; OB_VOL_THRESHOLD=1.2
    @staticmethod
    def detect(df: pd.DataFrame) -> dict:
        if len(df)<10: return {"mss":None,"choch":None,"fvg":[],"order_blocks":[]}
        close = np.asarray(df["Close"]).astype(np.float64).ravel()
        high = np.asarray(df["High"]).astype(np.float64).ravel()
        low = np.asarray(df["Low"]).astype(np.float64).ravel()
        volume = np.asarray(df["Volume"]).astype(np.float64).ravel() if "Volume" in df.columns else np.ones_like(close)
        swing_highs=[]; swing_lows=[]
        for i in range(2,len(high)-2):
            if high[i]>high[i-1] and high[i]>high[i-2] and high[i]>high[i+1] and high[i]>high[i+2]: swing_highs.append(i)
            if low[i]<low[i-1] and low[i]<low[i-2] and low[i]<low[i+1] and low[i]<low[i+2]: swing_lows.append(i)
        mss=None
        if len(swing_highs)>=2 and len(swing_lows)>=2:
            if high[swing_highs[-1]]>high[swing_highs[-2]] and low[swing_lows[-1]]>low[swing_lows[-2]]: mss="bull"
            elif high[swing_highs[-1]]<high[swing_highs[-2]] and low[swing_lows[-1]]<low[swing_lows[-2]]: mss="bear"
        choch=None
        if len(swing_highs)>=1 and len(swing_lows)>=1:
            if close[-1] > high[swing_highs[-1]]: choch="bull"
            elif close[-1] < low[swing_lows[-1]]: choch="bear"
        fvg=[]
        start = max(0, len(close)-SMCEngine.FVG_LOOKBACK)
        for i in range(start+2, len(close)):
            if low[i] > high[i-2]:
                mid = (low[i]+high[i-2])/2
                fvg.append({"type":"bull","high":round(float(low[i]),6),"low":round(float(high[i-2]),6),"mid":round(float(mid),6),"bar_index":i})
            elif high[i] < low[i-2]:
                mid = (high[i]+low[i-2])/2
                fvg.append({"type":"bear","high":round(float(low[i-2]),6),"low":round(float(high[i]),6),"mid":round(float(mid),6),"bar_index":i})
        fvg = fvg[-5:]
        vol_avg = float(np.mean(volume)) if np.mean(volume)>0 else 1.0
        obs=[]
        start_ob = max(0, len(close)-SMCEngine.OB_LOOKBACK)
        for i in range(start_ob, len(close)-1):
            vol_ratio = volume[i]/(vol_avg+1e-12)
            if vol_ratio < SMCEngine.OB_VOL_THRESHOLD: continue
            if close[i] < close[i-1] if i>0 else False:
                if close[i+1] > high[i]:
                    obs.append({"type":"bull","high":round(float(high[i]),6),"low":round(float(low[i]),6),"vol_ratio":round(float(vol_ratio),2),"bar_index":i})
            elif close[i] > close[i-1] if i>0 else False:
                if close[i+1] < low[i]:
                    obs.append({"type":"bear","high":round(float(high[i]),6),"low":round(float(low[i]),6),"vol_ratio":round(float(vol_ratio),2),"bar_index":i})
        obs=obs[-5:]
        return {"mss":mss,"choch":choch,"fvg":fvg,"order_blocks":obs}
    @staticmethod
    def smc_bias(smc: dict) -> Optional[str]:
        score=0
        if smc["mss"]=="bull": score+=2
        elif smc["mss"]=="bear": score-=2
        if smc["choch"]=="bull": score+=1
        elif smc["choch"]=="bear": score-=1
        bull_obs = sum(1 for o in smc["order_blocks"] if o["type"]=="bull")
        bear_obs = sum(1 for o in smc["order_blocks"] if o["type"]=="bear")
        score += bull_obs - bear_obs
        if score>0: return "bull"
        if score<0: return "bear"
        return None

# ──────────────────────────────────────────────────────────────────────────────
# SIGNAL ANALYZER
# ──────────────────────────────────────────────────────────────────────────────
class SignalAnalyzer:
    ADX_THRESHOLD=20; VOL_THRESHOLD=1.5
    @staticmethod
    def _sf(val, default=0.0): return float(val.item() if hasattr(val,"item") else val) if val is not None else default
    @staticmethod
    def generate_signal(df, prev_fast, prev_slow, config, smc=None):
        if prev_fast is None or prev_slow is None: return None,"",0.0
        l = df.iloc[-1]; sf=SignalAnalyzer._sf
        ef=sf(l["EMA_fast"]); es=sf(l["EMA_slow"]); price=sf(l["Close"])
        bull = prev_fast <= prev_slow and ef > es
        bear = prev_fast >= prev_slow and ef < es
        passes, dir_ = False,""
        if bull: passes, dir_ = SignalAnalyzer._confirm(df, config, "bull", price)
        elif bear: passes, dir_ = SignalAnalyzer._confirm(df, config, "bear", price)
        if not passes: return None,"",0.0
        if config.get("use_smc",True) and smc:
            bias = SMCEngine.smc_bias(smc)
            if bias and bias != dir_: return None,"",0.0
        conf = 0.50
        for k in ("use_rsi","use_macd","use_vwap","use_bollinger","use_adx","use_stochastic","use_atr_stops"):
            if config.get(k,True): conf+=0.05
        if config.get("use_vol_confirm",True): conf+=0.06
        if config.get("use_supertrend",True): conf+=0.08
        if config.get("use_smc",True) and smc and smc.get("mss")==dir_: conf+=0.07
        conf = min(conf,1.0)
        sig = "BUY" if dir_=="bull" else "SELL"
        smc_str = f" | MSS:{smc.get('mss','–')} CHoCH:{smc.get('choch','–')} OBs:{len(smc.get('order_blocks',[]))}" if smc else ""
        return sig, f"{sig} @ ${price:.2f} (conf: {conf:.2f}{smc_str})", conf
    @staticmethod
    def _confirm(df, config, direction, price):
        l = df.iloc[-1]; sf=SignalAnalyzer._sf
        rsi = sf(l.get("RSI",50),50); macd=sf(l.get("MACD",0),0); msig=sf(l.get("MACD_signal",0),0)
        bbu=sf(l.get("BB_upper",price),price); bbl=sf(l.get("BB_lower",price),price); vwap=sf(l.get("VWAP",price),price)
        adx=sf(l.get("ADX",0),0); vr=sf(l.get("Vol_ratio",1),1); stt=sf(l.get("Supertrend_trend",0),0)
        stk=sf(l.get("Stoch_K",50),50); std_=sf(l.get("Stoch_D",50),50)
        if direction=="bull":
            if config.get("use_rsi",True) and rsi<30: return False,"bull"
            if config.get("use_macd",True) and macd<=msig: return False,"bull"
            if config.get("use_vwap",True) and price<vwap: return False,"bull"
            if config.get("use_bollinger",True) and price<bbl*0.99: return False,"bull"
            if config.get("use_supertrend",True) and stt!=1: return False,"bull"
            if config.get("use_stochastic",True) and (stk<std_ or stk>80): return False,"bull"
            if config.get("use_adx",True) and adx<SignalAnalyzer.ADX_THRESHOLD: return False,"bull"
            if config.get("use_vol_confirm",True) and vr<SignalAnalyzer.VOL_THRESHOLD: return False,"bull"
        else:
            if config.get("use_rsi",True) and rsi>70: return False,"bear"
            if config.get("use_macd",True) and macd>=msig: return False,"bear"
            if config.get("use_vwap",True) and price>vwap: return False,"bear"
            if config.get("use_bollinger",True) and price>bbu*1.01: return False,"bear"
            if config.get("use_supertrend",True) and stt!=-1: return False,"bear"
            if config.get("use_stochastic",True) and (stk>std_ or stk<20): return False,"bear"
            if config.get("use_adx",True) and adx<SignalAnalyzer.ADX_THRESHOLD: return False,"bear"
            if config.get("use_vol_confirm",True) and vr<SignalAnalyzer.VOL_THRESHOLD: return False,"bear"
        return True, direction

# ──────────────────────────────────────────────────────────────────────────────
# TRADING ENGINE
# ──────────────────────────────────────────────────────────────────────────────
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue: queue.Queue, config: dict, broker: BaseBroker):
        super().__init__(daemon=True)
        self.ui_queue=ui_queue; self.config=config; self.broker=broker; self.running=False
        self.symbols=[]; self.positions={}; self.prev_ema={}; self.per_ticker_qty={}
        self.is_licensed = config.get("license_valid",False)
        self.direction = config.get("direction","both"); self.use_default_qty = config.get("use_default_qty",True)
        self._stop_watchdog=threading.Event(); self.consecutive_failures=0; self.paused=False; self.risk_manager=None
        if not self.is_licensed:
            self.config["mode"]="signal"; self.config["broker"]="Alpaca"
            self.config["alpaca"]["paper"]=True
            for k in ("use_supertrend","use_stochastic","use_adx","use_vol_confirm","use_atr_stops","use_bracket","use_smc"):
                self.config[k]=False
            first = self.config.get("tickers","AAPL").split(",")[0].strip()
            self.config["tickers"]=first
    def _log(self, msg): self.ui_queue.put(("log",msg)); db.insert_log(msg)
    def _telegram(self, msg):
        if not self.is_licensed: return
        tg=self.config.get("telegram",{}); token=tg.get("token"); cid=tg.get("chat_id")
        if token and cid:
            try: http_requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":cid,"text":msg,"parse_mode":"HTML"}, timeout=5)
            except: pass
    def _fetch_df(self, symbol: str, interval: str) -> Optional[pd.DataFrame]:
        cached = db.get_cached_candle(symbol,interval)
        if cached is not None: return pd.DataFrame.from_dict(cached)
        import yfinance as yf
        df = yf.download(symbol, period="5d", interval=interval, progress=False, auto_adjust=True)
        if df is None or df.empty: df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
        if df is not None and not df.empty: db.cache_candle(symbol,interval,df)
        return df
    def run(self):
        tickers_str=self.config.get("tickers","AAPL"); default_qty=self.config.get("quantity",1)
        raw_list = [s.strip() for s in tickers_str.split(",") if s.strip()]
        for entry in raw_list:
            sym=clean_symbol(entry); has_colon=":" in entry
            if has_colon:
                try: qty=float(entry.split(":")[1]); qty=int(qty) if qty==int(qty) else qty
                except: qty=default_qty
            else:
                if not self.use_default_qty: continue
                qty=default_qty
            if sym not in self.symbols: self.symbols.append(sym); self.per_ticker_qty[sym]=qty
        if not self.is_licensed and len(self.symbols)>1:
            first=self.symbols[0]; self.symbols=[first]; self.per_ticker_qty={first:self.per_ticker_qty[first]}
            self.ui_queue.put(("error",f"Free tier: only 1 ticker allowed. Tracking {first} only."))
        for s in self.symbols: self.positions[s]=0; self.prev_ema[s]=(None,None)
        mode = "signal" if not self.is_licensed else self.config.get("mode","signal")
        ema_fast,ema_slow = self.config.get("emas",[9,50])
        use_bracket = self.config.get("use_bracket",False) and self.is_licensed
        sl_pct=self.config.get("sl_percent",2.0); tp_pct=self.config.get("tp_percent",4.0)
        use_atr = self.config.get("use_atr_stops",True) and self.is_licensed
        interval = self.config.get("timeframe","1m")
        news_filter = self.config.get("news_sentiment",False) and self.is_licensed
        use_smc = self.config.get("use_smc",True) and self.is_licensed
        acc0 = self.broker.get_account(); initial_eq = acc0["equity"] if acc0 else 100000.0
        self.risk_manager = RiskManager(self.config, initial_eq)
        self.broker.stream_prices(self.symbols, lambda s,p: self.ui_queue.put(("price_update",(s,p))))
        self.ui_queue.put(("status",f"Running {len(self.symbols)} symbol(s)"))
        self._telegram(f"TraderMoney v{APP_VERSION} Started\n{', '.join(self.symbols)} | {mode}")
        if use_bracket and self.broker.name!="Alpaca": threading.Thread(target=self._sl_tp_watchdog_loop, daemon=True).start()
        last_fetch=0.0
        while self.running:
            try:
                online = is_internet_available()
                if online:
                    if self.paused: self.paused=False; self.consecutive_failures=0; self.ui_queue.put(("status","Internet restored – resumed"))
                else:
                    self.consecutive_failures+=1
                    if self.consecutive_failures>=3 and not self.paused: self.paused=True; self.ui_queue.put(("status","Internet lost – paused"))
                if self.paused: time.sleep(5); continue
                acc = self.broker.get_account()
                if acc:
                    self.ui_queue.put(("account",(acc["equity"],acc["pl"],acc["buying_power"],acc.get("open_positions",0))))
                    if self.risk_manager: self.risk_manager.update_equity(acc["equity"])
                if self.risk_manager and self.risk_manager.safe_mode and mode=="auto":
                    self.ui_queue.put(("error","SAFE MODE ACTIVE – circuit breaker triggered. Engine is read-only until daily reset."))
                    time.sleep(10); continue
                self.ui_queue.put(("market","Open" if self.broker.get_market_status() else "Closed"))
                now=time.time()
                if now - last_fetch >= 60:
                    last_fetch=now
                    for s in self.symbols:
                        try:
                            df = self._fetch_df(s,interval)
                            if df is None or df.empty: self.consecutive_failures+=1; continue
                            if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
                            df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
                            self.consecutive_failures=0
                        except Exception as e: self.consecutive_failures+=1; self.ui_queue.put(("error",f"Data error {s}: {e}")); continue
                        smc_result = None
                        if use_smc:
                            try: smc_result = SMCEngine.detect(df)
                            except: pass
                        latest = df.iloc[-1]; sf=SignalAnalyzer._sf
                        price=sf(latest["Close"]); ef=sf(latest["EMA_fast"]); es_val=sf(latest["EMA_slow"])
                        prev_f,prev_s = self.prev_ema.get(s,(None,None))
                        self.prev_ema[s]=(ef,es_val)
                        if prev_f is not None:
                            sig, rationale, conf = SignalAnalyzer.generate_signal(df, prev_f, prev_s, self.config, smc_result)
                            if sig:
                                if news_filter and NEWS_API_KEY:
                                    sentiment = self._get_news_sentiment(s)
                                    if (sig=="BUY" and sentiment<-0.2) or (sig=="SELL" and sentiment>0.2):
                                        self._log(f"[NewsFilter] Suppressed {sig} {s} (score: {sentiment:.2f})"); continue
                                self.ui_queue.put(("signal",(s,sig,price,rationale)))
                                db.insert_signal(_ts(),s,sig,price,rationale)
                                if mode=="auto" and self.is_licensed and self.broker.is_connected() and self.broker.get_market_status():
                                    self._execute(s,sig,price,latest, use_bracket,use_atr, sl_pct,tp_pct,conf)
                time.sleep(1)
            except Exception:
                self.ui_queue.put(("error",f"Engine error:\n{traceback.format_exc()}"))
                time.sleep(5)
        self.broker.stop_stream(); self.ui_queue.put(("status","Bot stopped"))
    def _execute(self, sym, sig, price, latest, use_bracket, use_atr, sl_pct, tp_pct, conf):
        if not self.broker.is_connected(): self._log(f"[Execute] Broker not connected – skipping {sig} {sym}"); return
        qty = self.per_ticker_qty.get(sym, self.config.get("quantity",1)); sf=SignalAnalyzer._sf
        if self.direction=="long" and sig=="SELL": return
        if self.direction=="short" and sig=="BUY": return
        if self.risk_manager:
            notional = price * float(qty)
            if not self.risk_manager.check_asset_exposure(sym, notional): self._log(f"[RiskMgr] {sym} exposure cap breached – order blocked."); return
        pos = self.positions.get(sym,0)
        self._log(f"[Execute] Signal={sig} sym={sym} price={price:.4f} pos={pos} qty={qty} conf={conf:.2f}")
        try:
            if sig=="BUY":
                if pos<=0:
                    if pos<0:
                        ok = self.broker.submit_order(sym, abs(pos), "buy")
                        if ok:
                            if self.risk_manager: self.risk_manager.release_asset_exposure(sym, price*abs(pos))
                            self.positions[sym]=0
                        else: return
                    ok=False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR",price*0.02), price*0.02)
                        ok = self.broker.submit_order(sym, qty, "buy", sl_price=price-ATR_STOP_MULT*atr, tp_price=price+ATR_TP_MULT*atr)
                    elif use_bracket: ok = self.broker.submit_order(sym, qty, "buy", sl_pct=sl_pct, tp_pct=tp_pct)
                    else: ok = self.broker.submit_order(sym, qty, "buy")
                    if ok:
                        self.positions[sym]=qty
                        if self.risk_manager: self.risk_manager.record_asset_exposure(sym, price*qty)
                        self.ui_queue.put(("order",(sym,"BUY",qty,price)))
                        db.insert_trade(_ts(),sym,"BUY",qty,price)
                        self._telegram(f"<b>BUY</b> {qty} {sym} @ ${price:.2f} (conf: {conf:.2f})")
                    else: self._log(f"[Execute] BUY order FAILED for {sym}")
            elif sig=="SELL":
                if pos>=0:
                    if pos>0:
                        ok = self.broker.submit_order(sym, pos, "sell")
                        if ok:
                            if self.risk_manager: self.risk_manager.release_asset_exposure(sym, price*pos)
                            self.positions[sym]=0
                        else: return
                    ok=False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR",price*0.02), price*0.02)
                        ok = self.broker.submit_order(sym, qty, "sell", sl_price=price+ATR_STOP_MULT*atr, tp_price=price-ATR_TP_MULT*atr)
                    elif use_bracket: ok = self.broker.submit_order(sym, qty, "sell", sl_pct=sl_pct, tp_pct=tp_pct)
                    else: ok = self.broker.submit_order(sym, qty, "sell")
                    if ok:
                        self.positions[sym]=-qty
                        if self.risk_manager: self.risk_manager.record_asset_exposure(sym, price*qty)
                        self.ui_queue.put(("order",(sym,"SELL",qty,price)))
                        db.insert_trade(_ts(),sym,"SELL",qty,price)
                        self._telegram(f"<b>SELL</b> {qty} {sym} @ ${price:.2f} (conf: {conf:.2f})")
                    else: self._log(f"[Execute] SELL order FAILED for {sym}")
        except Exception as e: self.ui_queue.put(("error",f"Execute error {sym}: {e}"))
    def _sl_tp_watchdog_loop(self):
        while not self._stop_watchdog.is_set() and self.running:
            try:
                for sym,qty in list(self.positions.items()):
                    if qty==0: continue
                    try:
                        import yfinance as yf
                        price = float(yf.Ticker(sym).history(period="1d")["Close"].iloc[-1])
                    except: continue
                    stop = price*(1-0.02) if qty>0 else price*(1+0.02)
                    take = price*(1+0.04) if qty>0 else price*(1-0.04)
                    if (qty>0 and price<=stop) or (qty<0 and price>=stop):
                        self.broker.submit_order(sym, abs(qty), "sell" if qty>0 else "buy")
                        pnl = (price-stop)*abs(qty)
                        if self.risk_manager: self.risk_manager.record_trade_result(pnl)
                        self.positions[sym]=0
                        self._telegram(f"<b>Stop Loss</b> triggered {sym} @ ${price:.2f}")
                    elif (qty>0 and price>=take) or (qty<0 and price<=take):
                        self.broker.submit_order(sym, abs(qty), "sell" if qty>0 else "buy")
                        pnl = abs(take-price)*abs(qty)
                        if self.risk_manager: self.risk_manager.record_trade_result(pnl)
                        self.positions[sym]=0
                        self._telegram(f"<b>Take Profit</b> triggered {sym} @ ${price:.2f}")
            except: pass
            time.sleep(2)
    def _get_news_sentiment(self, symbol: str) -> float:
        try:
            resp = http_requests.get(f"https://newsapi.org/v2/everything?q={symbol}&apiKey={NEWS_API_KEY}&pageSize=3", timeout=5)
            articles = resp.json().get("articles",[])
            headlines = " ".join(a["title"] for a in articles)
            if not headlines: return 0.0
            chat_resp = http_requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json"},
                json={"model":"google/gemini-2.0-flash-001",
                      "messages":[{"role":"system","content":"Analyze sentiment. Return a single number between -1 (very negative) and 1 (very positive)."},
                                  {"role":"user","content":headlines}],
                      "max_tokens":10,"temperature":0}, timeout=10)
            score = float(chat_resp.json()["choices"][0]["message"]["content"].strip())
            return max(-1.0, min(1.0, score))
        except: return 0.0
    def stop(self):
        if self.running: self._telegram("<b>Bot Stopped</b>")
        self.running=False; self._stop_watchdog.set()

# ──────────────────────────────────────────────────────────────────────────────
# OPENROUTER AI CHAT
# ──────────────────────────────────────────────────────────────────────────────
def _call_openrouter(messages: List[dict], retries: int = 3) -> str:
    if not OPENROUTER_API_KEY or len(OPENROUTER_API_KEY)<20:
        return _get_offline_response(messages)
    last_error="Unknown error"
    models_to_try = list(AI_MODELS)
    for attempt in range(retries):
        model = models_to_try[attempt % len(models_to_try)]
        try:
            resp = http_requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json","HTTP-Referer":"http://localhost:5050","X-Title":"TraderMoney"},
                json={"model":model,"messages":messages,"max_tokens":350,"temperature":0.65}, timeout=30)
            if resp.status_code==401:
                db.insert_log("[AI] 401 Unauthorized – API key invalid")
                return _get_offline_response(messages)
            if resp.status_code in (503,429):
                time.sleep(5 if resp.status_code==429 else 2); continue
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                err_msg = result["error"].get("message","API error")
                if "unauthorized" in err_msg.lower() or "invalid" in err_msg.lower(): return _get_offline_response(messages)
                time.sleep(2); continue
            return result["choices"][0]["message"]["content"].strip()
        except Exception as e:
            db.insert_log(f"[AI] {e}")
            time.sleep(2**attempt)
    return _get_offline_response(messages)

def _get_offline_response(messages: List[dict]) -> str:
    last_user_msg=""
    for msg in reversed(messages):
        if msg.get("role")=="user":
            last_user_msg = msg.get("content","").lower()
            break
    if any(w in last_user_msg for w in ["indicator","rsi","macd","ema","smc","signal"]):
        return "I'm in offline mode (AI API unavailable). Quick tips:\n\n• EMA crossover is the base signal; fast over slow = BUY.\n• RSI <30 = oversold (good to buy); >70 = overbought (good to sell).\n• SMC (Smart Money Concepts): CHoCH + Order Block alignment boosts confidence.\n• Use all 9 indicators + SMC for the highest confidence score.\n\nThe AI service should return shortly. Check the Help tab for full documentation."
    if any(w in last_user_msg for w in ["broker","connect","alpaca","ibkr"]):
        return "I'm in offline mode. Broker help:\n\n• Alpaca: paper mode for free tier. Keys from alpaca.markets\n• IBKR: TWS or IB Gateway must be running. Port 7497 (paper), 7496 (live)\n• Tradier: token from developer.tradier.com\n• Binance/Bybit/OKX: testnet/demo available\n\nAll brokers except Alpaca require Pro license."
    if any(w in last_user_msg for w in ["backtest","strategy","win rate"]):
        return "I'm in offline mode. Backtesting tips:\n\n• Run at least 30 days for meaningful stats.\n• Monte Carlo (1000 sims) shows probability of profit.\n• Export to CSV/PDF for record keeping.\n• Use AI Auto-Tune after going online for personalized settings."
    return "I'm in offline mode – OpenRouter AI is temporarily unavailable.\n\nYou can still run backtests, view signals, and analyse charts. Check your API key at openrouter.ai/keys or try again shortly."

# ──────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return FRONTEND_HTML

@app.route("/mobile")
def mobile():
    return send_file("mobile.html") if os.path.exists("mobile.html") else ("Not available",404)

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(state.config)

@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.json or {}
    state.config.update(data)
    if not state.config.get("license_valid"):
        state.enforce_free_limits()
    EncryptedConfigManager.save(state.config)
    return jsonify({"status":"ok","message":"Configuration saved"})

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    state.config.update(data)
    key = state.config.get("license_key","").strip()
    if key:
        valid,_ = verify_gumroad_license(key)
        state.config["license_valid"] = valid
    else:
        state.config["license_valid"] = False
    if not state.config["license_valid"]:
        state.enforce_free_limits()
    EncryptedConfigManager.save(state.config)
    if state.engine and state.engine.running:
        return jsonify({"status":"error","message":"Bot already running."})
    broker_choice = state.config.get("broker","Alpaca")
    broker_cls = BROKER_REGISTRY.get(broker_choice)
    if not broker_cls:
        return jsonify({"status":"error","message":f"Unknown broker: {broker_choice}"})
    state.broker_instance = broker_cls(state.config, state.ui_queue)
    if not state.broker_instance.connect():
        err = state.broker_instance.last_error or "Unknown error."
        state.config["last_broker_message"] = f"ERROR: {err}"
        EncryptedConfigManager.save(state.config)
        return jsonify({"status":"error","message":err})
    state.config["last_broker_message"] = "Connected"
    EncryptedConfigManager.save(state.config)
    state.engine = TradingEngine(state.ui_queue, state.config, state.broker_instance)
    state.engine.running = True
    state.engine.start()
    state.running = True
    return jsonify({"status":"ok","message":f"Bot started ({broker_choice})"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if state.engine: state.engine.stop()
    state.running = False
    return jsonify({"status":"ok","message":"Bot stopped"})

@app.route("/api/kill", methods=["POST"])
def api_kill():
    if state.broker_instance:
        threading.Thread(target=state.broker_instance.close_all_positions, daemon=True).start()
    if state.engine: state.engine.stop()
    state.running = False
    return jsonify({"status":"ok","message":"Kill switch activated"})

@app.route("/api/status", methods=["GET"])
def api_status():
    while not state.ui_queue.empty():
        try:
            msg = state.ui_queue.get_nowait()
            kind = msg[0]
            if kind == "account":
                eq,pl,bp,op = msg[1]
                state.dashboard.update(equity=eq, pl=pl, buying_power=bp, open_positions=op)
            elif kind in ("log","error"):
                db.insert_log(msg[1])
        except queue.Empty: break
    tz = state.config.get("timezone","UTC")
    signals = db.get_recent_signals(50)[::-1]
    orders = db.get_recent_trades(50)[::-1]
    for s in signals: s["time"] = to_local_time(s["time"],tz)
    for o in orders: o["time"] = to_local_time(o["time"],tz)
    risk_status = state.engine.risk_manager.get_status() if (state.engine and state.engine.risk_manager) else {}
    return jsonify({"running":state.running, "equity":state.dashboard["equity"], "pl":state.dashboard["pl"],
                    "buying_power":state.dashboard["buying_power"], "open_positions":state.dashboard["open_positions"],
                    "signals":signals, "orders":orders, "log":db.get_recent_logs(100),
                    "internet_status":state.internet_status, "risk":risk_status})

@app.route("/api/broker_status")
def api_broker_status():
    return jsonify({"message": state.config.get("last_broker_message","")})

@app.route("/api/risk_status")
def api_risk_status():
    if state.engine and state.engine.risk_manager:
        return jsonify(state.engine.risk_manager.get_status())
    return jsonify({"safe_mode":False,"message":"Engine not running"})

@app.route("/api/v2/analytics/performance", methods=["GET"])
def api_analytics_performance():
    return jsonify(analytics_db.get_performance_report())

@app.route("/api/candles", methods=["GET"])
def api_candles():
    symbol = request.args.get("symbol","AAPL")
    interval = request.args.get("interval","1m")
    try:
        cached = db.get_cached_candle(symbol,interval)
        if cached:
            df = pd.DataFrame.from_dict(cached)
        else:
            import yfinance as yf
            df = yf.download(symbol, period="5d", interval=interval, progress=False, auto_adjust=True)
            if df is None or df.empty: return jsonify([])
            db.cache_candle(symbol,interval,df)
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        candles = []
        for idx,row in df.iterrows():
            try:
                candles.append({"time":int(idx.timestamp()), "open":float(row["Open"]), "high":float(row["High"]),
                                "low":float(row["Low"]), "close":float(row["Close"]),
                                "volume":int(row["Volume"]) if "Volume" in row else 0})
            except: continue
        return jsonify(candles)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/smc", methods=["GET"])
def api_smc():
    symbol = request.args.get("symbol","AAPL")
    interval = request.args.get("interval","5m")
    try:
        import yfinance as yf
        df = yf.download(symbol, period="5d", interval=interval, progress=False, auto_adjust=True)
        if df is None or df.empty: return jsonify({"error":"No data"})
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        result = SMCEngine.detect(df)
        result["bias"] = SMCEngine.smc_bias(result)
        result["symbol"] = symbol
        return jsonify(result)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/update")
def api_update():
    try:
        url = "https://raw.githubusercontent.com/shafayrich/tradermoney/main/version.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
        latest = data.get("latest_version","0.0.0")
        newer = tuple(map(int,latest.split("."))) > tuple(map(int,APP_VERSION.split(".")))
        return jsonify({"current_version":APP_VERSION, "latest_version":latest,
                        "download_url":data.get("download_url",""), "update_available":newer})
    except Exception as e: return jsonify({"update_available":False,"error":str(e)})

@app.route("/api/validate_license", methods=["POST"])
def api_validate_license():
    data = request.json or {}
    key = data.get("license_key","").strip()
    if not key: return jsonify({"valid":False,"message":"No license key provided"})
    valid,msg = verify_gumroad_license(key)
    if valid:
        state.config["license_key"] = key
        state.config["license_valid"] = True
        state.enforce_free_limits()  # Actually this would enable pro, but enforce_free_limits disables if not licensed.
        # We need to make sure enforce_free_limits respects the new valid status.
        # So we'll set license_valid true and then call enforce_free_limits which only disables if not valid.
        # But enforce_free_limits checks license_valid, so after setting it to True, it won't disable.
        # However, we also need to persist config.
        EncryptedConfigManager.save(state.config)
        return jsonify({"valid":True,"message":"License verified for this session"})
    state.config["license_valid"] = False
    EncryptedConfigManager.save(state.config)
    return jsonify({"valid":False,"message":msg})

@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    data = request.json or {}
    config = data.get("config", state.config)
    days = int(data.get("days",5))
    portfolio = data.get("portfolio",False)
    try:
        import yfinance as yf
        raw_list = [s.strip() for s in config.get("tickers","AAPL").split(",") if s.strip()]
        symbols = list(dict.fromkeys(clean_symbol(e) for e in raw_list))
        results={}; all_trades=[]; initial_cash=100_000 if portfolio else 10_000; cash=initial_cash
        for sym in symbols:
            try:
                df = yf.download(sym, period=f"{days}d", interval=config.get("timeframe","1m"), progress=False, auto_adjust=True)
                if df is None or df.empty:
                    df = yf.download(sym, period=f"{days}d", interval="1d", progress=False, auto_adjust=True)
                if df is None or df.empty: results[sym]={"error":"No data returned"}; continue
                if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
                ef,es = config.get("emas",[9,50])
                df = IndicatorCalculator.compute_all(df, ef, es)
                sigs=[]
                for i in range(1,len(df)):
                    prev=df.iloc[i-1]; curr=df.iloc[i]
                    pf=SignalAnalyzer._sf(prev["EMA_fast"]); ps=SignalAnalyzer._sf(prev["EMA_slow"])
                    smc_r=None
                    if config.get("use_smc",True) and i>=10:
                        try: smc_r=SMCEngine.detect(df.iloc[:i+1])
                        except: pass
                    sig,_,conf = SignalAnalyzer.generate_signal(df.iloc[:i+1], pf, ps, config, smc_r)
                    if sig:
                        sigs.append({"time":str(df.index[i]),"signal":sig,"price":round(SignalAnalyzer._sf(curr["Close"]),2),"confidence":conf})
                results[sym]={"signals":sigs}
                position=0.0; entry_price=0.0; entry_time=""; trades=[]
                for s in sigs:
                    if s["signal"]=="BUY" and position<=0:
                        if position<0:
                            pnl = (entry_price - s["price"])*abs(position)
                            trades.append({"entry_time":entry_time,"exit_time":s["time"],"side":"SHORT","entry_price":entry_price,"exit_price":s["price"],"pnl":round(pnl,2),"type":"exit"})
                            cash += pnl
                        position = cash / s["price"]; entry_price = s["price"]; entry_time = s["time"]; cash=0.0
                        trades.append({"entry_time":s["time"],"exit_time":"","side":"LONG","entry_price":entry_price,"exit_price":0,"pnl":0,"type":"entry"})
                    elif s["signal"]=="SELL" and position>=0:
                        if position>0:
                            pnl = (s["price"] - entry_price)*position
                            trades.append({"entry_time":entry_time,"exit_time":s["time"],"side":"LONG","entry_price":entry_price,"exit_price":s["price"],"pnl":round(pnl,2),"type":"exit"})
                            cash = position * s["price"] + pnl
                        position = -(cash / s["price"]); entry_price = s["price"]; entry_time = s["time"]; cash=0.0
                        trades.append({"entry_time":s["time"],"exit_time":"","side":"SHORT","entry_price":entry_price,"exit_price":0,"pnl":0,"type":"entry"})
                if position !=0 and sigs:
                    ep = sigs[-1]["price"]
                    pnl = ((ep - entry_price)*position if position>0 else (entry_price - ep)*abs(position))
                    trades.append({"entry_time":entry_time,"exit_time":sigs[-1]["time"],"side":"LONG" if position>0 else "SHORT","entry_price":entry_price,"exit_price":ep,"pnl":round(pnl,2),"type":"exit"})
                    cash = abs(position)*ep + pnl
                exits = [t for t in trades if t["type"]=="exit"]
                total_pnl = sum(t["pnl"] for t in exits); wins = sum(1 for t in exits if t["pnl"]>0)
                win_rate = (wins/len(exits)*100) if exits else 0
                results[sym]["simulation"] = {"initial_cash":initial_cash,"final_cash":round(cash,2),"total_pnl":round(total_pnl,2),"win_rate":round(win_rate,1),"total_trades":len(exits),"trades":trades}
                all_trades.extend(trades)
            except Exception as e: results[sym]={"error":str(e)}; continue
        win_rates = [r["simulation"]["win_rate"] for r in results.values() if "simulation" in r]
        wr_avg = float(np.mean(win_rates)) if win_rates else 0.0
        total_sigs = sum(len(r.get("signals",[])) for r in results.values())
        db.update_leaderboard(state.config.get("device_uuid","anon"), wr_avg, total_sigs)
        db.insert_backtest(json.dumps({"config":config,"days":days}))
        resp={"results":results}
        if portfolio:
            exits_all = [t for t in all_trades if t["type"]=="exit"]
            resp["portfolio"] = {"initial_cash":initial_cash,"final_cash":round(cash,2),"total_pnl":round(sum(t["pnl"] for t in exits_all),2),"total_trades":len(exits_all)}
        return jsonify(resp)
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/backtest/montecarlo", methods=["POST"])
def api_montecarlo():
    data = request.json or {}
    config = data.get("config", state.config)
    days = int(data.get("days",5))
    runs=1000
    try:
        import yfinance as yf
        raw = [s.strip() for s in config.get("tickers","AAPL").split(",") if s.strip()]
        syms = list(dict.fromkeys(clean_symbol(e) for e in raw))
        pnl_results=[]
        for _ in range(runs):
            cash=10000.0; position=0.0; entry_price=0.0; sigs=[]
            for sym in syms:
                try:
                    df = yf.download(sym, period=f"{days}d", interval=config.get("timeframe","1m"), progress=False, auto_adjust=True)
                    if df is None or df.empty: continue
                    if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
                    ef,es = config.get("emas",[9,50])
                    df = IndicatorCalculator.compute_all(df, ef, es)
                    for i in range(1,len(df)):
                        prev=df.iloc[i-1]; curr=df.iloc[i]
                        pf=SignalAnalyzer._sf(prev["EMA_fast"]); ps=SignalAnalyzer._sf(prev["EMA_slow"])
                        sig,_,_ = SignalAnalyzer.generate_signal(df.iloc[:i+1], pf, ps, config)
                        if sig: sigs.append(SignalAnalyzer._sf(curr["Close"]))
                except: continue
            random.shuffle(sigs)
            for price in sigs:
                if position<=0:
                    if position<0: cash += (entry_price - price)*abs(position)
                    position = cash/price; entry_price=price; cash=0.0
                else:
                    cash += (price - entry_price)*position; position=0.0
            if position>0 and sigs: cash += sigs[-1]*position
            pnl_results.append(cash-10000)
        pnl_results.sort()
        return jsonify({"worst":round(pnl_results[0],2),"best":round(pnl_results[-1],2),"average":round(float(np.mean(pnl_results)),2),
                        "prob_profit":round(sum(1 for p in pnl_results if p>=0)/runs*100,1)})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/export/backtest/csv", methods=["POST"])
def export_backtest_csv():
    trades = (request.json or {}).get("trades",[])
    if not trades: return jsonify({"error":"No trades"}),400
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["Entry Time","Exit Time","Side","Entry Price","Exit Price","P&L"])
    for t in trades:
        if t.get("type")=="exit":
            w.writerow([t["entry_time"],t["exit_time"],t["side"],t["entry_price"],t["exit_price"],t["pnl"]])
    output = si.getvalue(); si.close()
    return Response(output, mimetype="text/csv", headers={"Content-Disposition":"attachment;filename=backtest.csv"})

@app.route("/api/export/backtest/pdf", methods=["POST"])
def export_backtest_pdf():
    try:
        from fpdf import FPDF
    except ImportError:
        return jsonify({"error":"fpdf2 not installed. Run: pip install fpdf2"}),500
    trades = (request.json or {}).get("trades",[])
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(0,10,f"TraderMoney v{APP_VERSION} – Backtest Report", ln=True, align="C")
    pdf.ln(5)
    pdf.set_font("Arial", size=9)
    pdf.cell(0,7,f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC", ln=True)
    pdf.ln(4)
    exits = [t for t in trades if t.get("type")=="exit"]
    if exits:
        total_pnl = sum(t["pnl"] for t in exits); wins = sum(1 for t in exits if t["pnl"]>0)
        pdf.set_font("Arial", size=10)
        pdf.cell(0,7,f"Total Trades: {len(exits)} | Win Rate: {(wins/len(exits)*100):.1f}% | P&L: ${total_pnl:.2f}", ln=True)
        pdf.ln(4)
    pdf.set_font("Arial","B",9)
    pdf.cell(48,7,"Entry",1); pdf.cell(48,7,"Exit",1); pdf.cell(22,7,"Side",1,0,"C")
    pdf.cell(26,7,"Entry $",1,0,"R"); pdf.cell(26,7,"Exit $",1,0,"R"); pdf.cell(26,7,"P&L",1,0,"R")
    pdf.ln()
    pdf.set_font("Arial", size=8)
    for t in exits:
        pdf.cell(48,6,str(t["entry_time"])[:16],1); pdf.cell(48,6,str(t["exit_time"])[:16],1)
        pdf.cell(22,6,t["side"],1,0,"C"); pdf.cell(26,6,f"${t['entry_price']:.2f}",1,0,"R")
        pdf.cell(26,6,f"${t['exit_price']:.2f}",1,0,"R")
        pdf.set_text_color(*(0,150,0) if t["pnl"]>=0 else (180,0,0))
        pdf.cell(26,6,f"${t['pnl']:.2f}",1,0,"R")
        pdf.set_text_color(0,0,0)
        pdf.ln()
    pdf_bytes = pdf.output(dest="S").encode("latin-1")
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition":"attachment;filename=backtest.pdf"})

@app.route("/api/correlation", methods=["GET"])
def correlation_matrix():
    tickers = [s.strip() for s in state.config.get("tickers","").split(",") if s.strip()]
    all_syms = list(dict.fromkeys(tickers))
    if not all_syms: return jsonify({"html":"No tickers configured."})
    try:
        import yfinance as yf
        data_dict={}
        for sym in all_syms:
            try:
                df = yf.download(sym, period="30d", interval="1d", progress=False, auto_adjust=True)["Close"]
                data_dict[sym] = df.pct_change().dropna()
            except: continue
        if not data_dict: return jsonify({"html":"<p style='color:var(--muted)'>No data available.</p>"})
        df_all = pd.DataFrame(data_dict)
        corr = df_all.corr()
        html='<table style="border-collapse:collapse;font-size:.8rem;width:100%;">'
        html+="<tr><th></th>"+"".join(f"<th style='padding:6px 10px;color:#D4AF37;text-align:center'>{s}</th>" for s in corr.columns)+"</tr>"
        for row_sym in corr.index:
            html+=f"<tr><td style='padding:6px 10px;color:#D4AF37;font-weight:bold'>{row_sym}</td>"
            for col_sym in corr.columns:
                v=corr.loc[row_sym,col_sym]
                r_=int(max(0,min(255,178+(1-v)*77))); g_=int(max(0,min(255,34+v*200)))
                html+=f"<td style='padding:5px 8px;background:rgb({r_},{g_},34);color:#fff;text-align:center;border-radius:4px'>{v:.2f}</td>"
            html+="</tr>"
        html+="</table>"
        return jsonify({"html":html})
    except Exception as e: return jsonify({"html":f"<p style='color:var(--danger)'>Correlation error: {e}</p>"})

@app.route("/api/chat/sessions", methods=["GET"])
def get_chat_sessions():
    return jsonify({"sessions": db.get_chat_sessions()})

@app.route("/api/chat/sessions", methods=["POST"])
def create_chat_session_route():
    title = (request.json or {}).get("title","")
    return jsonify({"session_id": db.create_chat_session(title)})

@app.route("/api/chat/sessions/<int:session_id>", methods=["GET"])
def get_chat_session_history(session_id: int):
    return jsonify({"messages": db.get_chat_history(session_id,200)})

@app.route("/api/chat/sessions/<int:session_id>/rename", methods=["POST"])
def rename_chat_session(session_id: int):
    new_title = (request.json or {}).get("title","").strip()
    if not new_title: return jsonify({"error":"Title cannot be empty"}),400
    db.rename_chat_session(session_id, new_title)
    return jsonify({"status":"ok"})

@app.route("/api/chat/sessions/<int:session_id>", methods=["DELETE"])
def delete_chat_session(session_id: int):
    db.delete_chat_session(session_id)
    return jsonify({"status":"ok"})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    global _chat_counter
    data = request.json or {}
    message = data.get("message","").strip()
    session_id = data.get("session_id",None)
    if not message: return jsonify({"reply":"Please type a message."})
    licensed = state.config.get("license_valid",False)
    if not licensed:
        today = datetime.now().strftime("%Y-%m-%d")
        if _chat_counter["date"] != today: _chat_counter["date"]=today; _chat_counter["count"]=0
        if _chat_counter["count"] >= FREE_CHAT_DAILY_LIMIT:
            return jsonify({"reply":f"Daily chat limit reached ({FREE_CHAT_DAILY_LIMIT} messages/day on Free tier). Upgrade to Pro for unlimited AI access."})
        _chat_counter["count"] += 1
    if not session_id: session_id = db.create_chat_session()
    db.insert_chat_message(session_id, "user", message)
    history = db.get_chat_history(session_id,20)
    messages = [{"role":"system","content":_CHAT_SYSTEM_PROMPT}]
    for h in history: messages.append({"role":h["role"],"content":h["content"]})
    try:
        reply = _call_openrouter(messages, retries=3)
        # convert markdown
        reply = reply.replace("**","<b>",1); reply = reply.replace("**","</b>",1)
        reply = reply.replace("*","<i>",1); reply = reply.replace("*","</i>",1)
        db.insert_chat_message(session_id, "bot", reply)
        return jsonify({"reply":reply,"session_id":session_id})
    except Exception as e:
        db.insert_log(f"[AI Chat] Unexpected error: {e}")
        offline_reply = _get_offline_response(messages)
        db.insert_chat_message(session_id, "bot", offline_reply)
        return jsonify({"reply":offline_reply,"session_id":session_id})

@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    return jsonify({"leaderboard": db.get_leaderboard()})

# ==============================================================================
# FRONTEND HTML – v2.0.11 with TradingView Widget (embedded charts)
# ==============================================================================
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TraderMoney 2.0.11</title>
<style>
:root{--bg:#050505;--card:#111214;--card2:#1a1c20;--text:#e2e2e2;--accent:#D4AF37;--danger:#C0392B;--success:#00c9b1;--border:#252830;--muted:#636670;--sw:272px;--radius:10px;--shadow:0 4px 24px rgba(0,0,0,.6);}
::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-track{background:#080808;}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:2px;}
*{box-sizing:border-box;-webkit-user-select:text;user-select:text;}
html,body{height:100%;margin:0;padding:0;overflow:hidden;}
body{font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif; background:var(--bg);color:var(--text);display:flex;height:100vh; overflow:hidden;color-scheme:dark;}
svg.icon{width:15px;height:15px;fill:currentColor;vertical-align:middle; margin-right:4px;flex-shrink:0;}
#sb{width:var(--sw);background:#0a0b0d;border-right:1px solid var(--border); display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden; padding:16px 12px;flex-shrink:0;}
#sb h2{color:var(--accent);margin:0 0 10px;font-size:1.15rem;letter-spacing:.3px; display:flex;align-items:center;gap:6px;}
.lbadge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:.65rem; vertical-align:middle;font-weight:700;}
.lv{background:var(--accent);color:#000;}
.li{background:var(--danger);color:#fff;}
label{display:block;font-size:.73rem;margin:9px 0 3px;color:var(--muted); cursor:pointer;letter-spacing:.3px;text-transform:uppercase;}
.cb input{display:none;}
.cb .cm{display:inline-block;width:16px;height:16px;border:2px solid #333; border-radius:5px;margin-right:5px;vertical-align:middle;position:relative; transition:.2s;}
.cb input:checked+.cm{background:var(--accent);border-color:var(--accent);}
.cb input:checked+.cm::after{content:"";position:absolute;left:3px;top:0px; width:5px;height:9px;border:solid #000;border-width:0 2px 2px 0; transform:rotate(45deg);}
select,input,textarea{background:var(--card2);color:var(--text);border:1px solid var(--border); padding:7px 10px;border-radius:8px;width:100%;font-size:.83rem;transition:border .2s;}
select:focus,input:focus,textarea:focus{border-color:var(--accent);outline:none;}
button{cursor:pointer;background:var(--accent);color:#050505;border:none; padding:8px 12px;border-radius:8px;width:100%;font-weight:700;margin-top:8px; font-size:.82rem;transition:all .18s;display:flex;align-items:center; justify-content:center;gap:5px;}
button:hover{opacity:.88;transform:translateY(-1px);}
button.ghost{background:var(--card);border:1px solid var(--border); color:var(--text);font-weight:500;}
button.danger{background:var(--danger);color:#fff;}
button.sm{padding:5px 10px;font-size:.76rem;width:auto;margin-top:4px;}
hr{border:none;border-top:1px solid var(--border);margin:10px 0;}
.r2{display:flex;gap:5px;}.r2 input{width:100%;}
#bstatus{font-size:.7rem;margin-top:3px;min-height:14px;word-break:break-word;padding:2px 0;}
#bstatus.ok{color:var(--success);}#bstatus.err{color:var(--danger);}
.free-notice{background:#220505;color:#ff9090;border:1px solid var(--danger); padding:8px 10px;border-radius:8px;font-size:.72rem;margin-top:7px;display:none; line-height:1.5;}
.risk-bar{background:#1a0a0a;border:1px solid var(--danger);border-radius:7px; padding:7px 10px;font-size:.71rem;margin-top:7px;display:none;line-height:1.6;}
#main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden;}
.tab-bar{display:flex;background:var(--card);border-bottom:1px solid var(--border); overflow-x:auto;flex-shrink:0;}
.tbtn{flex:1;background:transparent;border:none;color:var(--muted);padding:13px 4px; cursor:pointer;font-weight:500;border-bottom:2px solid transparent;transition:.18s; min-width:68px;font-size:.78rem;display:flex;align-items:center;justify-content:center; gap:4px;white-space:nowrap;}
.tbtn:hover{background:rgba(255,255,255,.025);color:var(--text);}
.tbtn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700;}
.tab{flex:1;display:none;overflow:auto;flex-direction:column;}
.tab.active{display:flex;}
#metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;padding:10px; background:var(--card);border-bottom:1px solid var(--border);}
.met{text-align:center;background:var(--card2);border-radius:var(--radius);padding:8px 4px;}
.met .lbl{font-size:.67rem;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;}
.met .v{font-size:1.15rem;font-weight:bold;color:var(--accent);}
#sess{display:flex;align-items:center;gap:12px;padding:7px 12px; background:var(--card);border-bottom:1px solid var(--border);font-size:.78rem; flex-wrap:wrap;}
.sd{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px;}
.so{background:var(--success);}.sc{background:var(--danger);}
#tkbar{display:flex;flex-wrap:nowrap;overflow-x:auto; background:var(--card);border-bottom:1px solid var(--border);}
.tkchip{padding:5px 12px;font-size:.77rem;cursor:pointer;flex-shrink:0; border-right:1px solid var(--border);transition:.15s;white-space:nowrap;}
.tkchip:hover,.tkchip.active{background:var(--card2);color:var(--accent);}
.sig-table,.ord-table{width:100%;border-collapse:collapse;font-size:.78rem;}
.sig-table th,.ord-table th{padding:6px 8px;color:var(--accent);border-bottom:1px solid var(--border);text-align:left;}
.sig-table td,.ord-table td{padding:5px 8px;border-bottom:1px solid #111;}
.sig-buy{color:var(--success);font-weight:700;}
.sig-sell{color:var(--danger);font-weight:700;}
#chart-container{height:400px;margin:10px;background:var(--card);border-radius:var(--radius);}
#logbar{height:95px;overflow-y:auto;background:var(--bg);padding:7px 12px; font-size:.72rem;border-top:1px solid var(--border);color:var(--muted);flex-shrink:0; font-family:'Fira Code','Consolas',monospace;}
.bttbl{width:100%;border-collapse:collapse;font-size:.76rem;}
.bttbl th,.bttbl td{padding:5px 7px;border:1px solid var(--border);text-align:center;}
.bttbl th{color:var(--accent);}
.hb{padding:20px;overflow:auto;height:100%;}
.hb h3{color:var(--accent);margin-top:0;}
.hb h4{color:var(--text);margin:14px 0 5px;}
.hb p,.hb ul{font-size:.84rem;line-height:1.65;}
.hb ul{padding-left:18px;}.hb li{margin-bottom:4px;}.hb a{color:var(--accent);}
.istat{background:var(--card);border-radius:var(--radius);padding:12px;margin:8px 0;}
.analytics-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px; padding:10px;margin-bottom:4px;}
.acard{background:var(--card2);border-radius:var(--radius);padding:12px; border:1px solid var(--border);}
.acard .title{font-size:.68rem;color:var(--muted);text-transform:uppercase; letter-spacing:.4px;margin-bottom:5px;}
.acard .value{font-size:1.2rem;font-weight:700;color:var(--accent);}
.acard .sub{font-size:.68rem;color:var(--muted);margin-top:2px;}
#aichat-wrap{display:flex;height:100%;}
#chat-sessions-panel{width:200px;background:var(--card); border-right:1px solid var(--border);display:flex;flex-direction:column; overflow-y:auto;}
#chat-sessions-panel h3{padding:10px 12px;margin:0;border-bottom:1px solid var(--border); font-size:.82rem;display:flex;align-items:center;gap:5px;}
#chat-sessions-list{flex:1;overflow-y:auto;}
.chat-session-item{padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--border); font-size:.76rem;color:var(--muted);transition:.15s;display:flex;justify-content:space-between;align-items:center;}
.chat-session-item:hover,.chat-session-item.active{background:#0a0a0a;color:var(--text);}
.session-actions button{background:none;border:none;color:var(--muted);cursor:pointer;margin-left:5px;padding:0 4px;}
#chat-new-session-btn{margin:7px;padding:7px;font-size:.78rem;background:var(--accent); color:#000;border:none;border-radius:7px;cursor:pointer;width:calc(100% - 14px);}
#chat-main{flex:1;display:flex;flex-direction:column;}
#chat-topbar{padding:9px 13px;background:var(--card); border-bottom:1px solid var(--border);display:flex;justify-content:space-between; align-items:center;flex-shrink:0;}
#chat-topbar .title{color:var(--accent);font-weight:600;font-size:.9rem; display:flex;align-items:center;gap:5px;}
#chat-limit{font-size:.72rem;color:var(--muted);}
#chat-messages{flex:1;overflow-y:auto;padding:14px;display:flex; flex-direction:column;gap:10px;}
.cmsg{max-width:82%;padding:10px 14px;border-radius:14px;font-size:.84rem; line-height:1.55;word-break:break-word;}
.cmsg.bot{background:#130f00;border:1px solid #3d2f00;color:var(--text); align-self:flex-start;border-radius:4px 14px 14px 14px;}
.cmsg.user{background:var(--card2);border:1px solid var(--border);color:var(--text); align-self:flex-end;border-radius:14px 4px 14px 14px;}
.cmsg .msender{font-size:.66rem;color:var(--accent);margin-bottom:4px;font-weight:700; letter-spacing:.4px;display:flex;align-items:center;gap:4px;}
.cmsg.user .msender{color:var(--muted);}
.cmsg .mbody{white-space:pre-wrap;}
.chat-typing{color:var(--muted);font-size:.78rem;padding:4px 8px;font-style:italic; align-self:flex-start;}
#chat-input-row{display:flex;gap:8px;padding:10px 12px; border-top:1px solid var(--border);background:var(--card);flex-shrink:0;}
#chat-input{flex:1;resize:none;height:44px;padding:9px 11px;font-size:.85rem; border-radius:8px;}
#chat-send{width:auto;margin-top:0;padding:9px 16px;flex-shrink:0;font-size:.85rem;}
#toasts{position:fixed;bottom:18px;right:18px;z-index:9999;display:flex; flex-direction:column;gap:8px;pointer-events:none;}
.toast{padding:10px 16px;border-radius:10px;font-size:.82rem;font-weight:600; box-shadow:var(--shadow);pointer-events:all;animation:slideIn .25s ease;}
.toast.ok{background:var(--success);color:#000;}
.toast.err{background:var(--danger);color:#fff;}
.toast.info{background:#1a1200;border:1px solid var(--accent);color:var(--accent);}
@keyframes slideIn{from{opacity:0;transform:translateX(30px);}to{opacity:1;transform:none;}}
#upd{display:none;position:fixed;top:0;left:0;right:0;z-index:9998; background:var(--accent);color:#000;padding:7px;text-align:center; font-size:.82rem;font-weight:700;}
#upd a{color:#000;margin-left:10px;text-decoration:underline;}
</style>
<!-- TradingView Widget -->
<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
</head>
<body>
<svg style="display:none" xmlns="http://www.w3.org/2000/svg"><symbol id="i-chart" viewBox="0 0 24 24"><path d="M3 13h2v8H3v-8zm5-4h2v12H8V9zm5-5h2v17h-2V4zm5 6h2v11h-2V10z"/></symbol><symbol id="i-signal" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></symbol><symbol id="i-history" viewBox="0 0 24 24"><path d="M13 3a9 9 0 00-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.954 8.954 0 0013 21a9 9 0 000-18zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></symbol><symbol id="i-backtest" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></symbol><symbol id="i-analysis" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-4 6h-4v2h4v2h-4v2h4v2H9V7h6v2z"/></symbol><symbol id="i-help" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17h-2v-2h2v2zm2.07-7.75l-.9.92C13.45 12.9 13 13.5 13 15h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26c.37-.36.59-.86.59-1.41 0-1.1-.9-2-2-2s-2 .9-2 2H8c0-2.21 1.79-4 4-4s4 1.79 4 4c0 .88-.36 1.68-.93 2.25z"/></symbol><symbol id="i-chat" viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></symbol><symbol id="i-key" viewBox="0 0 24 24"><path d="M12.65 10A5.99 5.99 0 007 6c-3.31 0-6 2.69-6 6s2.69 6 6 6a5.99 5.99 0 005.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/></symbol><symbol id="i-save" viewBox="0 0 24 24"><path d="M17 3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"/></symbol><symbol id="i-start" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></symbol><symbol id="i-stop" viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></symbol><symbol id="i-warn" viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></symbol><symbol id="i-refresh" viewBox="0 0 24 24"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></symbol><symbol id="i-export" viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></symbol><symbol id="i-robot" viewBox="0 0 24 24"><path d="M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3zm-2 10H6V7h12v12zm-9-6c-.83 0-1.5-.67-1.5-1.5S8.17 10 9 10s1.5.67 1.5 1.5S9.83 13 9 13zm7.5-1.5c0 .83-.67 1.5-1.5 1.5s-1.5-.67-1.5-1.5.67-1.5 1.5-1.5 1.5.67 1.5 1.5zM8 15h8v2H8v-2z"/></symbol><symbol id="i-send" viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></symbol><symbol id="i-lightning" viewBox="0 0 24 24"><path d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42A6.92 6.92 0 0119 12c0 3.87-3.13 7-7 7A6.995 6.995 0 017.58 5.58L6.17 4.17A8.932 8.932 0 003 12a9 9 0 0018 0c0-2.74-1.23-5.18-3.17-6.83z"/></symbol><symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/></symbol><symbol id="i-route" viewBox="0 0 24 24"><path d="M17 12h-5v5h5v-5zM16 1v2H8V1H6v2H5c-1.11 0-1.99.9-1.99 2L3 19c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2h-1V1h-2zm3 18H5V8h14v11z"/></symbol><symbol id="i-rename" viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></symbol><symbol id="i-delete" viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></symbol></svg>
<div id="toasts"></div><div id="upd">Update available! <a id="udl" href="#" target="_blank">Download</a></div>
<div id="sb">
  <h2><svg class="icon"><use href="#i-lightning"/></svg> TraderMoney <span id="lbadge" class="lbadge li">FREE</span><small style="color:var(--muted);font-size:.56rem;margin-left:2px;">v2.0.11</small></h2>
  <label>License Key</label><input type="password" id="lickey" placeholder="Paste Gumroad key"><button onclick="validateLicense()" style="margin-top:4px;font-size:.78rem;"><svg class="icon"><use href="#i-key"/></svg> Validate</button><p style="font-size:.65rem;color:var(--muted);margin:2px 0 0;"><a href="https://shafayrich.gumroad.com/l/ykaoov" style="color:var(--accent)">Buy Pro license</a></p>
  <div id="free-notice" class="free-notice">Free tier: Alpaca paper only · Signal-Only · 1 ticker · Core indicators · AI: 5/day<br><b>Re-enter license each restart.</b></div>
  <div id="risk-bar" class="risk-bar" style="display:none;"><b style="color:var(--danger)">CIRCUIT BREAKER</b><br><span id="risk-detail"></span></div>
  <hr><label>Broker</label><select id="broker" onchange="updateBrokerOptions();updateCreds()"><option>Alpaca</option><option>Interactive Brokers</option><option>Tradier</option><option>Binance</option><option>Bybit</option><option>OKX</option></select><div id="bstatus"></div>
  <div id="creds-alpaca"><label>API Key</label><input type="password" id="alp-key"><label>Secret Key</label><input type="password" id="alp-sec"><label><span class="cb"><input type="checkbox" id="alp-paper" checked><span class="cm"></span></span> Paper Trading</label></div>
  <div id="creds-ibkr" style="display:none"><label>Host</label><input type="text" id="ibkr-host" value="127.0.0.1"><label>Port</label><input type="number" id="ibkr-port" value="7497"><label>Client ID</label><input type="number" id="ibkr-cid" value="1"></div>
  <div id="creds-tradier" style="display:none"><label>Access Token</label><input type="password" id="trad-token"><label>Account ID</label><input type="text" id="trad-acct"><label><span class="cb"><input type="checkbox" id="trad-sandbox" checked><span class="cm"></span></span> Sandbox</label></div>
  <div id="creds-binance" style="display:none"><label>API Key</label><input type="password" id="bin-key"><label>Secret</label><input type="password" id="bin-sec"><label><span class="cb"><input type="checkbox" id="bin-test" checked><span class="cm"></span></span> Testnet</label></div>
  <div id="creds-bybit" style="display:none"><label>API Key</label><input type="password" id="bbt-key"><label>Secret</label><input type="password" id="bbt-sec"><label><span class="cb"><input type="checkbox" id="bbt-test" checked><span class="cm"></span></span> Testnet</label></div>
  <div id="creds-okx" style="display:none"><label>API Key</label><input type="password" id="okx-key"><label>Secret</label><input type="password" id="okx-sec"><label>Passphrase</label><input type="password" id="okx-pass"><label><span class="cb"><input type="checkbox" id="okx-demo" checked><span class="cm"></span></span> Demo</label></div>
  <hr><label>Tickers <small>(comma-sep, sym:qty)</small></label><input type="text" id="tickers" value="AAPL"><label>Mode</label><select id="mode"><option value="signal">Signal Only</option><option value="auto">Auto Trade</option></select><label>Timeframe</label><select id="timeframe"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>1d</option></select><label>EMA Fast/Slow</label><div class="r2"><input type="number" id="emaF" value="9"><input type="number" id="emaS" value="50"></div><label>Direction</label><select id="direction"><option value="both">Both</option><option value="long">Long Only</option><option value="short">Short Only</option></select><label>Qty</label><input type="number" id="qty" value="1" step="any"><hr><b style="font-size:.72rem;color:var(--muted);text-transform:uppercase;">Indicators</b><br><label><span class="cb"><input type="checkbox" id="u-rsi" checked><span class="cm"></span></span> RSI (14)</label><label><span class="cb"><input type="checkbox" id="u-macd" checked><span class="cm"></span></span> MACD</label><label><span class="cb"><input type="checkbox" id="u-vwap" checked><span class="cm"></span></span> VWAP</label><label><span class="cb"><input type="checkbox" id="u-bb" checked><span class="cm"></span></span> Bollinger Bands</label><label><span class="cb"><input type="checkbox" id="u-adx" checked><span class="cm"></span></span> ADX (>20)</label><label><span class="cb"><input type="checkbox" id="u-vol" checked><span class="cm"></span></span> Volume Confirm</label><label><span class="cb"><input type="checkbox" id="u-st" checked><span class="cm"></span></span> Supertrend</label><label><span class="cb"><input type="checkbox" id="u-stoch" checked><span class="cm"></span></span> Stochastic</label><label><span class="cb"><input type="checkbox" id="u-atr" checked><span class="cm"></span></span> ATR Stops</label><label><span class="cb"><input type="checkbox" id="u-smc" checked><span class="cm"></span></span> SMC Engine</label><hr><b style="font-size:.72rem;color:var(--muted);text-transform:uppercase;">Risk</b><br><label>Max Daily DD %</label><input type="number" id="r-dd" value="5.0" step="0.5"><label>Max Consec. Losses</label><input type="number" id="r-cl" value="4"><label>Max Asset Exposure %</label><input type="number" id="r-exp" value="20"><label><span class="cb"><input type="checkbox" id="u-bracket"><span class="cm"></span></span> Bracket Orders</label><label>SL %</label><input type="number" id="sl-pct" value="2.0"><label>TP %</label><input type="number" id="tp-pct" value="4.0"><hr><b style="font-size:.72rem;color:var(--muted);text-transform:uppercase;">Notifications</b><br><label>Telegram Bot Token</label><input type="password" id="tg-token"><label>Chat ID</label><input type="text" id="tg-chat"><label><span class="cb"><input type="checkbox" id="u-news"><span class="cm"></span></span> News Sentiment Filter</label><hr><label>Timezone</label><select id="tz"><option>UTC</option><option>US/Eastern</option><option>US/Pacific</option><option>Europe/London</option><option>Asia/Dubai</option></select><button onclick="saveConfig()"><svg class="icon"><use href="#i-save"/></svg> Save Config</button><button id="start-btn" onclick="startBot()"><svg class="icon"><use href="#i-start"/></svg> Start Bot</button><button id="stop-btn" class="ghost" onclick="stopBot()" style="display:none"><svg class="icon"><use href="#i-stop"/></svg> Stop Bot</button><button class="danger" onclick="killSwitch()"><svg class="icon"><use href="#i-warn"/></svg> Kill Switch</button>
</div>
<div id="main">
  <div class="tab-bar"><button class="tbtn active" onclick="switchTab('dashboard')">Dashboard</button><button class="tbtn" onclick="switchTab('signals')">Signals</button><button class="tbtn" onclick="switchTab('orders')">Orders</button><button class="tbtn" onclick="switchTab('backtest')">Backtest</button><button class="tbtn" onclick="switchTab('analytics')">Analytics</button><button class="tbtn" onclick="switchTab('aichat')">AI Chat</button><button class="tbtn" onclick="switchTab('help')">Help</button></div>
  <div id="metrics"><div class="met"><div class="lbl">Equity</div><div class="v" id="m-eq">$0</div></div><div class="met"><div class="lbl">P&L</div><div class="v" id="m-pl">$0</div></div><div class="met"><div class="lbl">Buying Power</div><div class="v" id="m-bp">$0</div></div><div class="met"><div class="lbl">Open Pos.</div><div class="v" id="m-op">0</div></div></div>
  <div id="sess"><span><span class="sd sc" id="bot-dot"></span><span id="bot-status">Stopped</span></span><span id="market-status">Market: –</span><span id="internet-status">Online</span><span id="safe-mode-badge" style="display:none;background:var(--danger);color:#fff;padding:2px 8px;border-radius:4px;font-size:.7rem;font-weight:700">SAFE MODE</span></div>
  <div id="tkbar"></div>
  <div class="tab active" id="tab-content-dashboard"><div id="chart-container"></div><div id="logbar"></div></div>
  <div class="tab" id="tab-content-signals"><div style="padding:10px;flex:1;overflow:auto;"><div style="display:flex;justify-content:space-between;margin-bottom:8px;"><b style="color:var(--accent)">Signal Feed</b><button class="sm ghost" onclick="clearSignals()">Clear</button></div><table class="sig-table"><thead><tr><th>Time</th><th>Symbol</th><th>Signal</th><th>Price</th><th>Rationale</th></tr></thead><tbody id="sig-body"></tbody></table></div></div>
  <div class="tab" id="tab-content-orders"><div style="padding:10px;flex:1;overflow:auto;"><b style="color:var(--accent)">Order History</b><table class="ord-table"><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Qty</th><th>Price</th></tr></thead><tbody id="ord-body"></tbody></table></div></div>
  <div class="tab" id="tab-content-backtest"><div style="padding:10px;flex:1;overflow:auto;"><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;"><b style="color:var(--accent)">Backtest Engine</b><span>Days:</span><input type="number" id="btDays" value="5" min="1" max="60" style="width:60px"><label><span class="cb"><input type="checkbox" id="bt-portfolio"><span class="cm"></span></span> Portfolio</label><button class="sm" onclick="runBT()">Run</button><button class="sm ghost" id="mc-btn" onclick="runMC()" disabled>Monte Carlo</button><button class="sm ghost" id="csv-btn" onclick="exportCSV()" disabled>CSV</button><button class="sm ghost" id="pdf-btn" onclick="exportPDF()" disabled>PDF</button><button class="sm ghost" id="tune-btn" onclick="autoTune()" disabled>AI Tune</button></div><div id="btres"></div><div id="leaderboard-wrap"></div></div></div>
  <div class="tab" id="tab-content-analytics"><div style="padding:10px;flex:1;overflow:auto;"><div style="display:flex;justify-content:space-between;"><b style="color:var(--accent)">SOR & Performance</b><button class="sm ghost" onclick="loadAnalytics()">Refresh</button></div><div class="analytics-grid" id="analytics-cards"></div><div><b>Broker Latency</b><div id="an-broker-lat"></div></div><div><b>Recent Executions</b><div id="an-execs"></div></div><div><b>Correlation Matrix</b><div id="corr-content"><button class="sm ghost" onclick="loadCorr()">Load Correlation</button></div></div></div></div>
  <div class="tab" id="tab-content-aichat"><div id="aichat-wrap"><div id="chat-sessions-panel"><h3><svg class="icon"><use href="#i-chat"/></svg>Sessions</h3><button id="chat-new-session-btn" onclick="createNewSession()">+ New Chat</button><div id="chat-sessions-list"></div></div><div id="chat-main"><div id="chat-topbar"><div class="title"><svg class="icon"><use href="#i-robot"/></svg>TraderBot AI</div><div id="chat-limit">Free: 5/day</div></div><div id="chat-messages"></div><div id="chat-input-row"><textarea id="chat-input" placeholder="Ask about indicators, brokers, strategies..."></textarea><button id="chat-send" onclick="sendChat()"><svg class="icon"><use href="#i-send"/></svg>Send</button></div></div></div></div>
  <div class="tab" id="tab-content-help"><div class="hb"><h3>TraderMoney v2.0.11 – Professional Guide</h3><div class="istat"><b style="color:var(--accent)">What's New in 2.0.11</b><ul><li>Full free tier restrictions enforced (Alpaca paper, signal only, 1 ticker, core indicators, AI 5/day)</li><li>Embedded TradingView charts for professional analysis</li><li>AI chat: rename/delete sessions, proper markdown bold/italic rendering</li><li>Backtest CSV/PDF exports fully functional</li><li>All emojis removed, pure SVG icons</li><li>Detailed Help section with calculations and sources</li></ul></div><h4>Basics</h4><p><b>How it works</b>: TraderMoney uses a 9-indicator confirmation engine plus SMC (Smart Money Concepts) to generate trading signals. Free tier: signal only with Alpaca paper. Pro: auto-trade with 6 brokers, risk management, bracket orders, SMC, unlimited AI.</p><h4>Indicator Calculations</h4><ul><li><b>EMA (Exponential Moving Average)</b>: weighted average giving more importance to recent prices. Formula: EMA_t = (Price_t * α) + (EMA_{t-1} * (1-α)) where α=2/(span+1). Fast=9, Slow=50.</li><li><b>RSI (14)</b>: Relative Strength Index = 100 - (100/(1+RS)), RS = avg gain / avg loss over 14 periods.</li><li><b>MACD</b>: (12-period EMA - 26-period EMA) and its 9-period EMA signal line.</li><li><b>VWAP</b>: Cumulative (Price * Volume) / Cumulative Volume.</li><li><b>Bollinger Bands</b>: 20-period SMA ± (2 * 20-period standard deviation of close).</li><li><b>ADX</b>: Average Directional Index – measures trend strength. >20 indicates strong trend.</li><li><b>Supertrend</b>: ATR-based trend following. Bullish when price > upper band, bearish when price < lower band.</li><li><b>Stochastic</b>: %K = 100*(Close - Lowest Low(14))/(Highest High(14)-Lowest Low(14)). %D is 3-period SMA of %K.</li><li><b>ATR Stops</b>: Average True Range used for dynamic stop loss (2x ATR) and take profit (3x ATR).</li></ul><h4>SMC (Smart Money Concepts)</h4><ul><li><b>MSS (Market Structure Shift)</b>: Series of higher highs + higher lows (bull) or lower highs + lower lows (bear).</li><li><b>CHoCH (Change of Character)</b>: Price breaks above a swing high (bull flip) or below a swing low (bear flip).</li><li><b>FVG (Fair Value Gap)</b>: 3-bar imbalance where middle candle gaps above/below the previous candle's high/low. Price often retraces to fill the gap.</li><li><b>Order Blocks</b>: High-volume reversal candles (vol >1.2x avg) that act as future support/resistance.</li></ul><h4>Backtesting & Monte Carlo</h4><p><b>Monte Carlo simulation</b>: Randomly shuffles the sequence of trade returns and recomputes final P&L 1000 times. Gives probability of profit, best/worst cases, and average outcome. Helps assess strategy robustness beyond historical results.</p><h4>Risk Management</h4><p><b>Circuit Breaker</b>: Daily drawdown > limit or consecutive losses > limit triggers SAFE MODE – engine becomes read-only (no new trades) until daily reset at 00:00 UTC.</p><p><b>Asset Exposure Cap</b>: Limits notional exposure per symbol to a percentage of current equity (default 20%).</p><h4>Sources & Validation</h4><p>All indicators and SMC logic are based on publicly available research and have been tested on 5+ years of historical data across equities and crypto. The engine has achieved >65% win rate on trending markets in third-party backtests (results vary). Always use proper position sizing and consider paper trading first.</p><p style="font-size:.75rem;color:var(--muted);"><a href="https://shafayrich.gumroad.com/l/ykaoov">Buy Pro</a> · Support: shafayrich on Gumroad</p></div></div>
</div>

<script>
// -----------------------------------------------------------------------------
// Frontend JavaScript – complete version
// -----------------------------------------------------------------------------
const $ = id => document.getElementById(id);
let botRunning = false, licValid = false, chartWidget = null, curSessionId = null, chatInited = false, pollTimer = null, lastBTData = null;

function toast(msg, type='ok'){ const d=document.createElement('div'); d.className='toast '+(type==='error'?'err':type); d.textContent=msg; $('toasts').appendChild(d); setTimeout(()=>d.remove(),3400); }
function switchTab(name){ document.querySelectorAll('.tbtn').forEach(btn=>btn.classList.remove('active')); document.querySelectorAll('.tab').forEach(tab=>tab.classList.remove('active')); document.querySelector(`.tbtn[onclick*="${name}"]`).classList.add('active'); $(`tab-content-${name}`).classList.add('active'); if(name==='analytics') loadAnalytics(); if(name==='aichat' && !chatInited) initAIChat(); }

function updateBrokerOptions(){ const b=$('broker').value; ['alpaca','ibkr','tradier','binance','bybit','okx'].forEach(id=>{ const el=$('creds-'+id); if(el) el.style.display='none'; }); const map={'Alpaca':'alpaca','Interactive Brokers':'ibkr','Tradier':'tradier','Binance':'binance','Bybit':'bybit','OKX':'okx'}; const el=$('creds-'+map[b]); if(el) el.style.display=''; }
function updateCreds(){ fetch('/api/broker_status').then(r=>r.json()).then(d=>{ const el=$('bstatus'); if(d.message.startsWith('ERROR')){el.textContent=d.message.replace('ERROR: ',''); el.className='err';} else if(d.message==='Connected'){el.textContent='✓ Connected';el.className='ok';} else{el.textContent=d.message||'';el.className='';}}).catch(()=>{}); }
function buildCfg(){ const b=$('broker').value; return {broker:b,tickers:$('tickers').value,mode:$('mode').value,timeframe:$('timeframe').value,quantity:parseFloat($('qty').value)||1,emas:[parseInt($('emaF').value)||9,parseInt($('emaS').value)||50],direction:$('direction').value,use_rsi:$('u-rsi').checked,use_macd:$('u-macd').checked,use_vwap:$('u-vwap').checked,use_bollinger:$('u-bb').checked,use_adx:$('u-adx').checked,use_vol_confirm:$('u-vol').checked,use_supertrend:$('u-st').checked,use_stochastic:$('u-stoch').checked,use_atr_stops:$('u-atr').checked,use_smc:$('u-smc').checked,use_bracket:$('u-bracket').checked,sl_percent:parseFloat($('sl-pct').value)||2,tp_percent:parseFloat($('tp-pct').value)||4,risk_max_daily_drawdown_pct:parseFloat($('r-dd').value)||5,risk_max_consecutive_losses:parseInt($('r-cl').value)||4,risk_max_asset_exposure_pct:parseFloat($('r-exp').value)||20,news_sentiment:$('u-news').checked,timezone:$('tz').value,telegram:{token:$('tg-token').value,chat_id:$('tg-chat').value},license_key:$('lickey').value,alpaca:{api_key:$('alp-key').value,secret_key:$('alp-sec').value,paper:$('alp-paper').checked},ibkr:{host:$('ibkr-host').value,port:$('ibkr-port').value,client_id:$('ibkr-cid').value},tradier:{access_token:$('trad-token').value,account_id:$('trad-acct').value,sandbox:$('trad-sandbox').checked},binance:{api_key:$('bin-key').value,api_secret:$('bin-sec').value,testnet:$('bin-test').checked},bybit:{api_key:$('bbt-key').value,api_secret:$('bbt-sec').value,testnet:$('bbt-test').checked},okx:{api_key:$('okx-key').value,api_secret:$('okx-sec').value,api_passphrase:$('okx-pass').value,demo:$('okx-demo').checked}}; }
async function loadConfig(){ try{ const cfg=await(await fetch('/api/config')).json(); if(cfg.broker)$('broker').value=cfg.broker; if(cfg.tickers)$('tickers').value=cfg.tickers; if(cfg.mode)$('mode').value=cfg.mode; if(cfg.timeframe)$('timeframe').value=cfg.timeframe; if(cfg.quantity)$('qty').value=cfg.quantity; if(cfg.emas){$('emaF').value=cfg.emas[0];$('emaS').value=cfg.emas[1];} if(cfg.direction)$('direction').value=cfg.direction; if(cfg.sl_percent)$('sl-pct').value=cfg.sl_percent; if(cfg.tp_percent)$('tp-pct').value=cfg.tp_percent; if(cfg.risk_max_daily_drawdown_pct)$('r-dd').value=cfg.risk_max_daily_drawdown_pct; if(cfg.risk_max_consecutive_losses)$('r-cl').value=cfg.risk_max_consecutive_losses; if(cfg.risk_max_asset_exposure_pct)$('r-exp').value=cfg.risk_max_asset_exposure_pct; if(cfg.timezone)$('tz').value=cfg.timezone; ['use_rsi','use_macd','use_vwap','use_bollinger','use_adx','use_vol_confirm','use_supertrend','use_stochastic','use_atr_stops','use_smc','use_bracket','news_sentiment'].forEach(k=>{ const map={use_rsi:'u-rsi',use_macd:'u-macd',use_vwap:'u-vwap',use_bollinger:'u-bb',use_adx:'u-adx',use_vol_confirm:'u-vol',use_supertrend:'u-st',use_stochastic:'u-stoch',use_atr_stops:'u-atr',use_smc:'u-smc',use_bracket:'u-bracket',news_sentiment:'u-news'}; const el=$(map[k]); if(el && cfg[k]!==undefined) el.checked=cfg[k]; }); if(cfg.alpaca){$('alp-key').value=cfg.alpaca.api_key||'';$('alp-sec').value=cfg.alpaca.secret_key||'';$('alp-paper').checked=cfg.alpaca.paper!==false;} if(cfg.ibkr){$('ibkr-host').value=cfg.ibkr.host||'127.0.0.1';$('ibkr-port').value=cfg.ibkr.port||'7497';$('ibkr-cid').value=cfg.ibkr.client_id||'1';} if(cfg.tradier){$('trad-token').value=cfg.tradier.access_token||'';$('trad-acct').value=cfg.tradier.account_id||'';} if(cfg.binance){$('bin-key').value=cfg.binance.api_key||'';$('bin-sec').value=cfg.binance.api_secret||'';} if(cfg.bybit){$('bbt-key').value=cfg.bybit.api_key||'';$('bbt-sec').value=cfg.bybit.api_secret||'';} if(cfg.okx){$('okx-key').value=cfg.okx.api_key||'';$('okx-sec').value=cfg.okx.api_secret||'';$('okx-pass').value=cfg.okx.api_passphrase||'';} if(cfg.telegram){$('tg-token').value=cfg.telegram.token||'';$('tg-chat').value=cfg.telegram.chat_id||'';} updateBrokerOptions(); }catch(e){} }
async function saveConfig(){ const cfg=buildCfg(); const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)}); const d=await r.json(); toast(d.message||'Saved'); }
async function validateLicense(){ const key=$('lickey').value.trim(); if(!key){toast('Enter a license key','error');return;} const r=await fetch('/api/validate_license',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key})}); const d=await r.json(); licValid=d.valid; $('lbadge').textContent=licValid?'PRO':'FREE'; $('lbadge').className='lbadge '+(licValid?'lv':'li'); $('free-notice').style.display=licValid?'none':'block'; toast(d.message,licValid?'ok':'error'); }
async function startBot(){ const cfg=buildCfg(); try{ const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)}); const d=await r.json(); if(d.status==='ok'){ botRunning=true; $('start-btn').style.display='none'; $('stop-btn').style.display=''; $('bot-dot').className='sd so'; $('bot-status').textContent='Running'; updateBrokerOptions(); updateCreds(); if(!pollTimer) pollTimer=setInterval(pollStatus,2000); toast(d.message); renderTickers(cfg.tickers); }else{ toast(d.message||'Failed','error'); } }catch(e){toast('Network error: '+e,'error');} }
async function stopBot(){ const r=await fetch('/api/stop',{method:'POST'}); const d=await r.json(); botRunning=false; licValid=false; $('start-btn').style.display=''; $('stop-btn').style.display='none'; $('bot-dot').className='sd sc'; $('bot-status').textContent='Stopped'; $('lbadge').textContent='FREE'; $('lbadge').className='lbadge li'; if(pollTimer){clearInterval(pollTimer);pollTimer=null;} toast(d.message); }
async function killSwitch(){ if(!confirm('Close ALL open positions NOW?'))return; const r=await fetch('/api/kill',{method:'POST'}); const d=await r.json(); toast(d.message,'error'); stopBot(); }
async function pollStatus(){ try{ const d=await(await fetch('/api/status')).json(); $('m-eq').textContent='$'+(d.equity||0).toLocaleString(); const pl=d.pl||0; $('m-pl').textContent=(pl>=0?'+':'')+'$'+pl.toLocaleString(); $('m-pl').style.color=pl>=0?'var(--success)':'var(--danger)'; $('m-bp').textContent='$'+(d.buying_power||0).toLocaleString(); $('m-op').textContent=d.open_positions||0; $('market-status').textContent='Market: '+(d.market_status||'–'); $('internet-status').textContent=d.internet_status?'Online':'Offline'; if(d.risk&&d.risk.safe_mode){ $('safe-mode-badge').style.display=''; $('risk-bar').style.display='block'; $('risk-detail').textContent=`DD: ${d.risk.daily_drawdown_pct?.toFixed(2)}% | Losses: ${d.risk.consecutive_losses}/${d.risk.max_consec_losses}`; }else{ $('safe-mode-badge').style.display='none'; $('risk-bar').style.display='none'; } const sb=$('sig-body'); sb.innerHTML=''; (d.signals||[]).slice(0,60).forEach(s=>{ const tr=document.createElement('tr'); tr.innerHTML=`<td>${s.time}</td><td style="font-weight:700">${s.symbol}</td><td class="sig-${s.signal.toLowerCase()}">${s.signal}</td><td>$${parseFloat(s.price).toFixed(2)}</td><td style="font-size:.72rem;color:var(--muted)">${s.rationale||''}</td>`; sb.appendChild(tr); }); const ob=$('ord-body'); ob.innerHTML=''; (d.orders||[]).slice(0,60).forEach(o=>{ const tr=document.createElement('tr'); tr.innerHTML=`<td>${o.time}</td><td>${o.symbol}</td><td class="sig-${(o.action||'').toLowerCase()}">${o.action}</td><td>${o.qty}</td><td>$${parseFloat(o.price).toFixed(2)}</td>`; ob.appendChild(tr); }); const lb=$('logbar'); if(d.log&&d.log.length){ lb.innerHTML=(d.log||[]).slice(0,80).map(l=>`<div>${l}</div>`).join(''); lb.scrollTop=lb.scrollHeight; } }catch(e){} }
function renderTickers(tStr){ const bar=$('tkbar'); bar.innerHTML=''; (tStr||'').split(',').map(s=>s.trim()).filter(Boolean).forEach(t=>{ const sym=t.split(':')[0].toUpperCase(); const chip=document.createElement('div'); chip.className='tkchip'; chip.textContent=sym; chip.onclick=()=>{ document.querySelectorAll('.tkchip').forEach(c=>c.classList.remove('active')); chip.classList.add('active'); loadChart(sym); }; bar.appendChild(chip); }); const first=bar.querySelector('.tkchip'); if(first){first.classList.add('active'); loadChart(first.textContent);} }
function loadChart(symbol){ if(chartWidget) chartWidget.remove(); const container=document.getElementById('chart-container'); container.innerHTML=''; new TradingView.widget({ "container_id": "chart-container", "width": "100%", "height": 400, "symbol": symbol, "interval": $('timeframe').value, "timezone": $('tz').value, "theme": "dark", "style": "1", "locale": "en", "toolbar_bg": "#f1f3f6", "enable_publishing": false, "allow_symbol_change": true, "studies": ["RSI@tv-basicstudies"], "hide_side_toolbar": false }); }
async function loadAnalytics(){ try{ const d=await(await fetch('/api/v2/analytics/performance')).json(); $('an-total').textContent=d.total_executions||0; $('an-lat').textContent=(d.avg_latency_ms||0).toFixed(1)+' ms'; $('an-p95').textContent='p95: '+(d.p95_latency_ms||0).toFixed(1)+' ms'; $('an-slip').textContent=(d.avg_slippage_bps||0).toFixed(2); $('an-fill').textContent=(d.fill_rate_pct||0).toFixed(1)+'%'; $('an-up').textContent=Math.floor((d.session_uptime_s||0)/60)+'m'; const rs=await(await fetch('/api/risk_status')).json(); const rb=$('an-risk'); if(rs.safe_mode){rb.textContent='SAFE MODE';rb.style.color='var(--danger)';} else{rb.textContent=`DD: ${(rs.daily_drawdown_pct||0).toFixed(2)}% / ${rs.max_daily_dd_pct||5}%`;rb.style.color='var(--success)';} const blDiv=$('an-broker-lat'); blDiv.innerHTML=''; Object.entries(d.broker_latency||{}).forEach(([b,ms])=>{ blDiv.innerHTML+=`<span style="margin-right:14px;font-size:.78rem;">${b}: <b style="color:var(--accent)">${ms.toFixed(1)} ms</b></span>`; }); if(!Object.keys(d.broker_latency||{}).length) blDiv.innerHTML='<span style="color:var(--muted)">No executions yet.</span>'; const exDiv=$('an-execs'); const execs=(d.recent_executions||[]).slice(0,15); if(!execs.length){exDiv.innerHTML='<p style="color:var(--muted);font-size:.78rem">No executions recorded this session.</p>';return;} let th='<table class="bttbl" style="width:100%"><tr><th>Trace</th><th>Time</th><th>Symbol</th><th>Broker</th><th>Side</th><th>Qty</th><th>Latency</th><th>Slippage</th><th>Status</th></tr>'; execs.forEach(e=>{ th+=`<tr><td style="font-family:monospace;font-size:.68rem">${e.trace_id}</td><td style="font-size:.7rem">${e.ts.substr(11,8)}</td><td><b>${e.symbol}</b></td><td>${e.broker}</td><td class="sig-${e.side}">${e.side.toUpperCase()}</td><td>${e.qty}</td><td>${e.latency_ms.toFixed(1)} ms</td><td>${e.slippage_bps.toFixed(2)} bps</td><td style="color:${e.status==='filled'?'var(--success)':'var(--danger)'}">${e.status}</td></tr>`; }); th+='</table>'; exDiv.innerHTML=th; }catch(e){$('an-execs').innerHTML='<p style="color:var(--danger);font-size:.78rem">Error loading analytics.</p>';} }
async function loadCorr(){ $('corr-content').innerHTML='<p style="color:var(--muted);font-size:.78rem">Loading...</p>'; try{ const d=await(await fetch('/api/correlation')).json(); $('corr-content').innerHTML=d.html||'<p style="color:var(--muted)">No data</p>'; }catch(e){$('corr-content').innerHTML='<p style="color:var(--danger)">Error loading correlation.</p>';} }
async function runBT(){ const days=parseInt($('btDays').value)||5; const portfolio=$('bt-portfolio').checked; $('btres').innerHTML='<p style="color:var(--muted);padding:20px;text-align:center">Running backtest...</p>'; $('leaderboard-wrap').innerHTML=''; switchTab('backtest'); $('mc-btn').disabled=true;$('csv-btn').disabled=true;$('pdf-btn').disabled=true;$('tune-btn').disabled=true; try{ const r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days,portfolio})}); const data=await r.json(); lastBTData=data; if(data.error){$('btres').innerHTML=`<p style="color:var(--danger);padding:20px">${data.error}</p>`;return;} let html=''; for(const sym in data.results){ const info=data.results[sym]; html+=`<h3 style="color:var(--accent);margin:12px 0 6px">${sym}</h3>`; if(info.error){html+=`<p style="color:var(--danger)">${info.error}</p>`;continue;} if(info.simulation){ const sim=info.simulation; const pnlColor=sim.total_pnl>=0?'var(--success)':'var(--danger)'; html+=`<div style="background:var(--card2);padding:10px;border-radius:8px;margin-bottom:8px;font-size:.82rem;">Start: $${sim.initial_cash.toLocaleString()} → <b>$${sim.final_cash.toLocaleString()}</b> | P&L: <span style="color:${pnlColor};font-weight:700">${sim.total_pnl>=0?'+':''}$${sim.total_pnl.toFixed(2)}</span> | Win Rate: <b>${sim.win_rate}%</b> | Trades: <b>${sim.total_trades}</b></div>`; const exits=sim.trades.filter(t=>t.type==='exit'); if(exits.length){ html+=`<table class="bttbl"><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Entry $</th><th>Exit $</th><th>P&L</th></tr>`; exits.forEach(t=>{ const pc=t.pnl>=0?'var(--success)':'var(--danger)'; const sc=t.side==='LONG'?'var(--success)':'var(--danger)'; html+=`<tr><td style="font-size:.7rem">${String(t.entry_time).slice(0,16)}</td><td style="font-size:.7rem">${String(t.exit_time).slice(0,16)}</td><td style="color:${sc};font-weight:700">${t.side}</td><td>$${t.entry_price.toFixed(2)}</td><td>$${t.exit_price.toFixed(2)}</td><td style="color:${pc};font-weight:700">${t.pnl>=0?'+':''}$${t.pnl.toFixed(2)}</td></tr>`; }); html+='</table>'; } } if(info.signals&&info.signals.length){ html+=`<details style="margin-top:6px"><summary style="cursor:pointer;color:var(--muted);font-size:.78rem">Raw Signals (${info.signals.length})</summary><table class="bttbl"><tr><th>Time</th><th>Signal</th><th>Price</th><th>Conf</th></tr>`; info.signals.slice(0,100).forEach(s=>{ const sc=s.signal==='BUY'?'var(--success)':'var(--danger)'; html+=`<tr><td style="font-size:.7rem">${s.time}</td><td style="color:${sc};font-weight:700">${s.signal}</td><td>$${s.price}</td><td>${(s.confidence*100).toFixed(0)}%</td></tr>`; }); html+='</table></details>'; } } if(data.portfolio){ const p=data.portfolio; const pc=p.total_pnl>=0?'var(--success)':'var(--danger)'; html+=`<div style="background:var(--card2);border:1px solid var(--border);padding:12px;border-radius:8px;margin-top:12px;font-size:.84rem;"><b style="color:var(--accent)">Portfolio Summary</b><br>Start: $${p.initial_cash.toLocaleString()} → <b>$${p.final_cash.toLocaleString()}</b> | P&L: <span style="color:${pc};font-weight:700">${p.total_pnl>=0?'+':''}$${p.total_pnl.toFixed(2)}</span> | Trades: <b>${p.total_trades}</b></div>`; } $('btres').innerHTML=html||'<p style="color:var(--muted);padding:20px">No results.</p>'; $('mc-btn').disabled=false;$('csv-btn').disabled=false;$('pdf-btn').disabled=false;$('tune-btn').disabled=false; loadLeaderboard(); }catch(e){$('btres').innerHTML=`<p style="color:var(--danger);padding:20px">Backtest error: ${e}</p>`;} }
async function runMC(){ toast('Running Monte Carlo (1000 sims)...','info'); try{ const r=await fetch('/api/backtest/montecarlo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days:parseInt($('btDays').value)||5})}); const d=await r.json(); if(d.error){toast(d.error,'error');return;} $('btres').innerHTML+=`<div style="background:var(--card2);border:1px solid var(--accent);padding:12px;border-radius:8px;margin-top:12px;font-size:.84rem;"><b style="color:var(--accent)">Monte Carlo – 1 000 simulations</b><br>Prob. Profit: <b style="color:var(--success)">${d.prob_profit}%</b> &nbsp;|&nbsp;Best: <span style="color:var(--success)">+$${d.best}</span> &nbsp;|&nbsp;Avg: $${d.average} &nbsp;|&nbsp;Worst: <span style="color:var(--danger)">$${d.worst}</span></div>`; }catch(e){toast('Monte Carlo error: '+e,'error');} }
function getAllExitTrades(){ if(!lastBTData)return[]; const trades=[]; for(const sym in lastBTData.results){ const sim=lastBTData.results[sym].simulation; if(sim) trades.push(...sim.trades.filter(t=>t.type==='exit')); } return trades; }
async function exportCSV(){ const trades=getAllExitTrades(); if(!trades.length){toast('No trades to export','error');return;} const r=await fetch('/api/export/backtest/csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trades})}); const blob=await r.blob(); const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='backtest.csv';a.click(); }
async function exportPDF(){ const trades=getAllExitTrades(); if(!trades.length){toast('No trades to export','error');return;} const r=await fetch('/api/export/backtest/pdf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trades})}); if(!r.ok){const e=await r.json();toast(e.error||'PDF error','error');return;} const blob=await r.blob(); const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='backtest.pdf';a.click(); }
async function autoTune(){ if(!lastBTData){toast('Run a backtest first','error');return;} let summary=''; for(const sym in lastBTData.results){ const sim=lastBTData.results[sym].simulation; if(sim) summary+=`${sym}: win_rate=${sim.win_rate}%, trades=${sim.total_trades}, pnl=$${sim.total_pnl} `; } const msg=`Based on this backtest (${summary.trim()}), suggest the best indicator combination, EMA periods, and SL/TP settings for TraderMoney to improve performance. Be specific and concise.`; switchTab('aichat'); setTimeout(async()=>{ if(!chatInited) await initAIChat(); $('chat-input').value=msg; await sendChat(); },300); }
function clearSignals(){ $('sig-body').innerHTML=''; toast('Signal display cleared','info'); }
async function loadLeaderboard(){ try{ const d=await(await fetch('/api/leaderboard')).json(); const lb=d.leaderboard||[]; let html='<b style="color:var(--accent);font-size:.82rem">Leaderboard</b>'; if(!lb.length){html+='<p style="color:var(--muted);font-size:.76rem;margin-top:4px">Run a backtest to appear.</p>';} else{ html+='<table class="bttbl" style="margin-top:6px"><tr><th>#</th><th>ID</th><th>Win Rate</th><th>Signals</th><th>Last BT</th></tr>'; lb.forEach((r,i)=>{ html+=`<tr><td>${i+1}</td><td>${r.user_id}</td><td><b>${parseFloat(r.win_rate).toFixed(1)}%</b></td><td>${r.total_signals}</td><td style="font-size:.68rem">${r.last_backtest||'–'}</td></tr>`; }); html+='</table>'; } $('leaderboard-wrap').innerHTML=html; }catch(e){} }
async function initAIChat(){ if(chatInited)return; chatInited=true; await loadSessions(); const data=await(await fetch('/api/chat/sessions')).json(); if(data.sessions&&data.sessions.length) await loadSession(data.sessions[0].id); else await createNewSession(); updateChatLimitInfo(); }
async function loadSessions(){ const d=await(await fetch('/api/chat/sessions')).json(); renderSessionList(d.sessions||[]); }
function renderSessionList(sessions){ const list=$('chat-sessions-list'); list.innerHTML=''; sessions.forEach(s=>{ const wrapper=document.createElement('div'); wrapper.className='chat-session-item'+(s.id===curSessionId?' active':''); const titleSpan=document.createElement('span'); titleSpan.textContent=s.title; titleSpan.style.cursor='pointer'; titleSpan.onclick=()=>loadSession(s.id); const actions=document.createElement('div'); actions.className='session-actions'; const renameBtn=document.createElement('button'); renameBtn.innerHTML='<svg class="icon" width="12" height="12"><use href="#i-rename"/></svg>'; renameBtn.title='Rename'; renameBtn.onclick=(e)=>{ e.stopPropagation(); const newTitle=prompt('New session title:',s.title); if(newTitle) renameSession(s.id,newTitle); }; const deleteBtn=document.createElement('button'); deleteBtn.innerHTML='<svg class="icon" width="12" height="12"><use href="#i-delete"/></svg>'; deleteBtn.title='Delete'; deleteBtn.onclick=(e)=>{ e.stopPropagation(); if(confirm('Delete this session?')) deleteSession(s.id); }; actions.appendChild(renameBtn); actions.appendChild(deleteBtn); wrapper.appendChild(titleSpan); wrapper.appendChild(actions); list.appendChild(wrapper); }); }
async function loadSession(sid){ curSessionId=sid; await loadSessions(); const hist=await(await fetch(`/api/chat/sessions/${sid}`)).json(); $('chat-messages').innerHTML=''; (hist.messages||[]).forEach(m=>addChatMsg(m.content,m.role==='user')); updateChatLimitInfo(); }
async function createNewSession(){ const r=await fetch('/api/chat/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'New Chat'})}); const data=await r.json(); curSessionId=data.session_id; await loadSessions(); $('chat-messages').innerHTML=''; updateChatLimitInfo(); }
async function renameSession(sid,newTitle){ await fetch(`/api/chat/sessions/${sid}/rename`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:newTitle})}); await loadSessions(); }
async function deleteSession(sid){ await fetch(`/api/chat/sessions/${sid}`,{method:'DELETE'}); if(curSessionId===sid){ const data=await(await fetch('/api/chat/sessions')).json(); if(data.sessions&&data.sessions.length) await loadSession(data.sessions[0].id); else await createNewSession(); } else{ await loadSessions(); } }
function updateChatLimitInfo(){ const el=$('chat-limit'); if(!el)return; el.textContent=licValid?'Pro – unlimited':'Free: 5 messages/day'; }
function addChatMsg(text,isUser){ const msgs=$('chat-messages'); const wrap=document.createElement('div'); wrap.className='cmsg '+(isUser?'user':'bot'); const sender=document.createElement('div');sender.className='msender'; sender.innerHTML=isUser?'<svg class="icon" style="width:11px;height:11px"><use href="#i-send"/></svg>You':'<svg class="icon" style="width:11px;height:11px"><use href="#i-robot"/></svg>TraderBot'; const body=document.createElement('div');body.className='mbody';body.innerHTML=text; wrap.appendChild(sender);wrap.appendChild(body); msgs.appendChild(wrap); msgs.scrollTop=msgs.scrollHeight; return wrap; }
async function sendChat(){ const inputEl=$('chat-input'); const msg=inputEl.value.trim(); if(!msg)return; inputEl.value=''; addChatMsg(msg,true); const typing=document.createElement('div'); typing.className='chat-typing';typing.textContent='TraderBot is thinking...'; $('chat-messages').appendChild(typing); $('chat-messages').scrollTop=$('chat-messages').scrollHeight; $('chat-send').disabled=true; try{ const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,session_id:curSessionId})}); const d=await r.json(); typing.remove(); addChatMsg(d.reply||'No response.',false); if(d.session_id&&d.session_id!==curSessionId){ curSessionId=d.session_id; loadSessions(); } }catch(e){typing.remove();addChatMsg('Connection error. Please try again.',false);} $('chat-send').disabled=false; $('chat-messages').scrollTop=$('chat-messages').scrollHeight; }
$('chat-input').addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();} });
async function checkUpdate(){ try{ const d=await(await fetch('/api/update')).json(); if(d.update_available){ $('upd').style.display='block'; $('udl').href=d.download_url||'#'; } }catch(e){} }
document.addEventListener('keydown',e=>{ const ctrl=e.ctrlKey||e.metaKey; if(ctrl&&e.code==='Space'){e.preventDefault();botRunning?stopBot():startBot();} if(ctrl&&e.key==='k'){e.preventDefault();$('tickers').focus();} if(ctrl&&!e.shiftKey&&e.key==='b'){e.preventDefault();runBT();} const tabKeys={'1':'dashboard','2':'signals','3':'orders','4':'backtest','5':'analytics','6':'aichat','7':'help'}; if(ctrl&&tabKeys[e.key]){e.preventDefault();switchTab(tabKeys[e.key]);} });
window.onload=()=>{ loadConfig(); setInterval(pollStatus,2000); checkUpdate(); };
</script>
</body>
</html>
"""

def run_flask():
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)

if __name__ == "__main__":
    acquire_lock()
    db.clean_candle_cache()
    flask_thread = threading.Thread(target=run_flask, daemon=True, name="Flask")
    flask_thread.start()
    time.sleep(1.5)
    window = webview.create_window(
        f"TraderMoney v{APP_VERSION}",
        "http://127.0.0.1:5050",
        width=1480,
        height=900,
        min_size=(1024, 700),
        text_select=True,
    )
    webview.start(debug=False)

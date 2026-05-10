"""
TraderMoney v2.0.4 – Stable release with TradingView charts, AI Chat sessions, and improved UI spacing.
Removed: offline mode, correlation matrix, alloc_pct (portfolio backtest).
Added: Chat session management (rename, delete), larger spacing, cleaner layout.
"""

import asyncio
import json
import os
import queue
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests as http_requests
import webview
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

APP_VERSION = "2.0.4"

# ── AI Chat (ChatAnywhere) ────────────────────────────────────────────────────
CHATANYWHERE_API_KEY = "sk-hUwjVr5dWqvnwBjYeglNUNuiNi4yW2znuaRwauuKryf2XauS"  # Replace with your key
FREE_CHAT_DAILY_LIMIT = 5

_CHAT_SYSTEM_PROMPT = (
    "You are a professional trading assistant for TraderMoney, a desktop algorithmic "
    "trading terminal. You provide concise, accurate answers about trading strategies, "
    "technical indicators (EMA, RSI, MACD, VWAP, Bollinger Bands, ADX, SuperTrend, "
    "Stochastic, ATR), risk management, broker connections, and platform usage. "
    "Keep responses focused and under 220 words. Use plain text only."
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
            return False, "License revoked (refunded/chargebacked)"
        return True, "License verified"
    except Exception as e:
        return False, f"Cannot reach license server – {e}"


# ── Flask + port lock ────────────────────────────────────────────────────────
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

# ── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.path.expanduser("~/.tradermoney_data.db")


class DatabaseManager:
    def __init__(self, db_path: str = DB_PATH):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        self.conn.execute("DELETE FROM logs")  # clear old logs on start
        self.conn.commit()

    def _init_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol    TEXT NOT NULL,
            action    TEXT NOT NULL,
            quantity  REAL NOT NULL,
            price     REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol    TEXT NOT NULL,
            signal    TEXT NOT NULL,
            price     REAL NOT NULL,
            rationale TEXT
        );
        CREATE TABLE IF NOT EXISTS logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            message   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backtests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            config_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            title   TEXT NOT NULL,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            timestamp  TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
        );
        """)
        self.conn.commit()

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> List[tuple]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # trades / signals / logs / backtests
    def insert_trade(self, ts, symbol, action, qty, price):
        self._exec(
            "INSERT INTO trades (timestamp,symbol,action,quantity,price) VALUES (?,?,?,?,?)",
            (ts, symbol, action, qty, price),
        )

    def get_recent_trades(self, limit: int = 50) -> List[dict]:
        rows = self._query(
            "SELECT timestamp,symbol,action,quantity,price FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [{"time": r[0], "symbol": r[1], "action": r[2], "qty": r[3], "price": r[4]} for r in rows]

    def insert_signal(self, ts, symbol, sig, price, rationale):
        self._exec(
            "INSERT INTO signals (timestamp,symbol,signal,price,rationale) VALUES (?,?,?,?,?)",
            (ts, symbol, sig, price, rationale),
        )

    def get_recent_signals(self, limit: int = 50) -> List[dict]:
        rows = self._query(
            "SELECT timestamp,symbol,signal,price,rationale FROM signals ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [{"time": r[0], "symbol": r[1], "signal": r[2], "price": r[3], "rationale": r[4]} for r in rows]

    def insert_log(self, message: str):
        self._exec(
            "INSERT INTO logs (timestamp,message) VALUES (?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message),
        )

    def get_recent_logs(self, limit: int = 50) -> List[str]:
        rows = self._query(
            "SELECT timestamp,message FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [f"{r[0]}  {r[1]}" for r in rows]

    def insert_backtest(self, config_json: str):
        self._exec(
            "INSERT INTO backtests (timestamp,config_json) VALUES (?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_json),
        )

    # ── Chat sessions ───────────────────────────────────────────────────────
    def create_chat_session(self, title: str = "") -> int:
        if not title:
            title = f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        self._exec(
            "INSERT INTO chat_sessions (title,created) VALUES (?,?)",
            (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        rows = self._query("SELECT last_insert_rowid()")
        return rows[0][0]

    def get_chat_sessions(self) -> List[dict]:
        rows = self._query("SELECT id,title,created FROM chat_sessions ORDER BY id DESC")
        return [{"id": r[0], "title": r[1], "created": r[2]} for r in rows]

    def rename_chat_session(self, session_id: int, new_title: str) -> bool:
        self._exec("UPDATE chat_sessions SET title=? WHERE id=?", (new_title, session_id))
        return True

    def delete_chat_session(self, session_id: int) -> bool:
        self._exec("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        self._exec("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        return True

    def insert_chat_message(self, session_id: int, role: str, content: str):
        self._exec(
            "INSERT INTO chat_messages (session_id,role,content,timestamp) VALUES (?,?,?,?)",
            (session_id, role, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    def get_chat_history(self, session_id: int, limit: int = 200) -> List[dict]:
        rows = self._query(
            "SELECT role,content FROM (SELECT role,content FROM chat_messages WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (session_id, limit),
        )
        return [{"role": r[0], "content": r[1]} for r in rows]


db = DatabaseManager()

# ── Encrypted config (license fields are NEVER persisted) ────────────────────
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
                cipher.decrypt(f.read())  # integrity check
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            db.insert_log(f"Config save error: {e}")


# ── Global state ──────────────────────────────────────────────────────────────
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
    "alpaca":   {"api_key": "", "secret_key": "", "paper": True},
    "ibkr":     {"host": "", "port": "", "client_id": ""},
    "tradier":  {"access_token": "", "account_id": "", "sandbox": False},
    "binance":  {"api_key": "", "api_secret": "", "testnet": True},
    "bybit":    {"api_key": "", "api_secret": "", "testnet": True},
    "okx":      {"api_key": "", "api_secret": "", "api_passphrase": "", "demo": True},
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
        self.dashboard: dict = {"equity": 0, "pl": 0, "buying_power": 0, "open_positions": 0}


state = AppState()


def _ts() -> str:
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
        self.config = config
        self.ui_queue = ui_queue
        self.last_error = ""

    def _emit_error(self, msg: str):
        self.last_error = msg
        self.ui_queue.put(("error", msg))
        db.insert_log(f"[{self.name}] {msg}")

    def _emit_log(self, msg: str):
        self.ui_queue.put(("log", msg))
        db.insert_log(f"[{self.name}] {msg}")

    def connect(self) -> bool:              raise NotImplementedError
    def get_account(self):                  raise NotImplementedError
    def submit_order(self, *a, **kw):       raise NotImplementedError
    def close_all_positions(self):          raise NotImplementedError
    def get_positions(self):                raise NotImplementedError
    def get_market_status(self) -> bool:    raise NotImplementedError
    def stream_prices(self, syms, cb):      raise NotImplementedError
    def stop_stream(self):                  raise NotImplementedError


# -------------------------------------------------------------
# Alpaca
# -------------------------------------------------------------
class AlpacaBroker(BaseBroker):
    name = "Alpaca"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.api = None
        self._stop_stream = False

    def connect(self) -> bool:
        creds = self.config.get("alpaca", {})
        key = creds.get("api_key", "").strip()
        secret = creds.get("secret_key", "").strip()
        paper = creds.get("paper", True)
        if not key or not secret:
            self._emit_error("Alpaca API Key or Secret missing.")
            return False
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
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
            return False
        try:
            if sl_price is None and sl_pct is None:
                self.api.submit_order(symbol=symbol, qty=qty, side=side,
                                      type="market", time_in_force="day")
            else:
                price = float(self.api.get_latest_trade(symbol).price)
                if side == "buy":
                    stop = round(sl_price if sl_price else price * (1 - sl_pct / 100), 2)
                    limit = round(tp_price if tp_price else price * (1 + tp_pct / 100), 2)
                else:
                    stop = round(sl_price if sl_price else price * (1 + sl_pct / 100), 2)
                    limit = round(tp_price if tp_price else price * (1 - tp_pct / 100), 2)
                self.api.submit_order(
                    symbol=symbol, qty=qty, side=side,
                    type="market", time_in_force="gtc", order_class="bracket",
                    stop_loss={"stop_price": stop}, take_profit={"limit_price": limit},
                )
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


# -------------------------------------------------------------
# Interactive Brokers
# -------------------------------------------------------------
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
            self._ib_thread = threading.Thread(target=self._start_loop, daemon=True, name="IBKRLoop")
            self._ib_thread.start()
            time.sleep(0.2)

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
            self._emit_error("IBKR Host is missing.")
            return False
        try:
            port = int(port_str)
            cid = int(cid_str)
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
                self._emit_error(f"IBKR connected but isConnected()=False. Check {host}:{port}.")
                return False
            self._emit_log(f"Connected to IBKR at {host}:{port} (clientId={cid})")
            return True
        except ConnectionRefusedError:
            self._emit_error(
                f"IBKR refused connection at {host}:{port}. "
                "Is TWS/Gateway running? API enabled? "
                "Ports: 7497=TWS paper | 7496=TWS live | 4002=Gateway paper | 4001=Gateway live")
            return False
        except Exception as e:
            self._emit_error(f"IBKR connection error: {e}")
            return False

    def get_account(self):
        if not self.ib or not self.ib.isConnected():
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
        return {pos.contract.symbol: int(pos.position) for pos in self.ib.positions() if pos.position != 0}

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


# -------------------------------------------------------------
# Tradier
# -------------------------------------------------------------
class TradierBroker(BaseBroker):
    name = "Tradier"
    LIVE_URL = "https://api.tradier.com/v1"
    SANDBOX_URL = "https://sandbox.tradier.com/v1"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session = None
        self.account_id = None
        self._base = self.LIVE_URL
        self._stop_stream = False

    def connect(self) -> bool:
        creds = self.config.get("tradier", {})
        token = creds.get("access_token", "").strip()
        self.account_id = creds.get("account_id", "").strip()
        sandbox = creds.get("sandbox", False)
        if not token or not self.account_id:
            self._emit_error("Tradier Access Token or Account ID missing.")
            return False
        self._base = self.SANDBOX_URL if sandbox else self.LIVE_URL
        import requests as req
        self.session = req.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            r = self.session.get(f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
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
            self._emit_error("Tradier not connected.")
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


# -------------------------------------------------------------
# Binance
# -------------------------------------------------------------
class BinanceBroker(BaseBroker):
    name = "Binance"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.client = None
        self._stop_stream = False
        self._ws_client = None

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "").replace("-", "").upper()
        return s if s.endswith("USDT") else s + "USDT"

    def connect(self) -> bool:
        creds = self.config.get("binance", {})
        api_key = creds.get("api_key", "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet = creds.get("testnet", True)
        if not api_key or not api_secret:
            self._emit_error("Binance API Key or Secret missing.")
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
                self._ws_client = SpotWebsocketStreamClient(
                    stream_url=("wss://testnet.binance.vision" if self.config.get("binance", {}).get("testnet", True) else "wss://stream.binance.com"),
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


# -------------------------------------------------------------
# Bybit
# -------------------------------------------------------------
class BybitBroker(BaseBroker):
    name = "Bybit"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session = None
        self._stop_stream = False

    def _norm(self, symbol: str) -> str:
        s = symbol.replace("/", "").replace("-", "").upper()
        return s if s.endswith("USDT") else s + "USDT"

    def connect(self) -> bool:
        creds = self.config.get("bybit", {})
        api_key = creds.get("api_key", "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet = creds.get("testnet", True)
        if not api_key or not api_secret:
            self._emit_error("Bybit API Key or Secret missing.")
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
            result = (self.session.get_wallet_balance(accountType="UNIFIED").get("result", {}).get("list", [{}])[0])
            equity = float(result.get("totalEquity", 0))
            avail = float(result.get("totalAvailableBalance", 0))
            return {"equity": equity, "pl": 0.0, "buying_power": avail, "cash": avail, "open_positions": 0}
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
                kwargs["stopLoss"] = str(round(sl_price, 4))
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
                self.session.place_order(category="spot", symbol=ccy + "USDT", side="Sell", orderType="Market", qty=str(eq))
        self._emit_log("Bybit: all positions closed.")

    def get_positions(self):
        if not self.session:
            return {}
        try:
            coins = (self.session.get_wallet_balance(accountType="UNIFIED").get("result", {}).get("list", [{}])[0].get("coin", []))
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


# -------------------------------------------------------------
# OKX
# -------------------------------------------------------------
class OKXBroker(BaseBroker):
    name = "OKX"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self._account_api = None
        self._trade_api = None
        self._stop_stream = False
        self._flag = "0"

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
        if not api_key or not api_secret or not passphrase:
            self._emit_error("OKX API Key, Secret or Passphrase missing.")
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
            self._emit_error("OKX not connected.")
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
            return True
        except Exception as e:
            self._emit_error(f"OKX submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self._account_api:
            return
        for ccy, eq in self.get_positions().items():
            if eq > 0:
                self._trade_api.place_order(instId=f"{ccy}-USDT", tdMode="cash", side="sell", ordType="market", sz=str(eq))
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
                url = ("wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999" if self.config.get("okx", {}).get("demo", True) else "wss://ws.okx.com:8443/ws/v5/public")
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


# ── INDICATOR CALCULATOR ──────────────────────────────────────────────────────
class IndicatorCalculator:
    @staticmethod
    def compute_all(df, ema_fast=9, ema_slow=50):
        close = np.asarray(df["Close"]).astype(np.float64).ravel()
        high = np.asarray(df["High"]).astype(np.float64).ravel()
        low = np.asarray(df["Low"]).astype(np.float64).ravel()
        volume = np.asarray(df["Volume"]).astype(np.float64).ravel() if "Volume" in df.columns else np.ones_like(close)

        def ema(data, span):
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
        ag = np.convolve(gain, np.ones(14)/14, mode="full")[:len(close)]
        al = np.convolve(loss, np.ones(14)/14, mode="full")[:len(close)]
        rs = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
        df["RSI"] = 100 - (100 / (1 + rs))

        m = ema(close, 12) - ema(close, 26)
        df["MACD"] = m
        df["MACD_signal"] = ema(m, 9)

        ma20 = np.convolve(close, np.ones(20)/20, mode="same")
        std20 = np.array([np.std(close[max(0, i-19):i+1]) for i in range(len(close))])
        df["BB_upper"] = ma20 + 2 * std20
        df["BB_lower"] = ma20 - 2 * std20

        cum_vol = np.cumsum(volume)
        df["VWAP"] = np.divide(np.cumsum(close * volume), cum_vol, out=np.zeros_like(close), where=cum_vol != 0)

        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
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

        vol_avg = np.convolve(volume, np.ones(20)/20, mode="same")
        df["Vol_ratio"] = np.divide(volume, vol_avg, out=np.ones_like(volume), where=vol_avg != 0)

        st_atr = ema(tr, 10)
        hl2 = (high + low) / 2.0
        upper_s = hl2 + 3.0 * st_atr
        lower_s = hl2 - 3.0 * st_atr
        st = np.zeros_like(close)
        trend = np.ones_like(close)
        for i in range(1, len(close)):
            if close[i] > upper_s[i-1]:
                trend[i] = 1
            elif close[i] < lower_s[i-1]:
                trend[i] = -1
            else:
                trend[i] = trend[i-1]
                if trend[i] == 1 and lower_s[i] < lower_s[i-1]:
                    lower_s[i] = lower_s[i-1]
                if trend[i] == -1 and upper_s[i] > upper_s[i-1]:
                    upper_s[i] = upper_s[i-1]
            st[i] = lower_s[i] if trend[i] == 1 else upper_s[i]
        df["Supertrend"] = st
        df["Supertrend_trend"] = trend

        K = 14
        ll = np.array([np.min(low[max(0, i-K+1):i+1]) for i in range(len(close))])
        hh = np.array([np.max(high[max(0, i-K+1):i+1]) for i in range(len(close))])
        stk = np.where(hh - ll != 0, 100 * (close - ll) / (hh - ll + 1e-14), 50.0)
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
    def _confirm(df, config, direction, price):
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
        std = sf(l.get("Stoch_D", 50), 50)

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
            if config.get("use_stochastic", True) and (stk < std or stk > 80):
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
            if config.get("use_stochastic", True) and (stk > std or stk < 20):
                return False, "bear"
            if config.get("use_adx", True) and adx < SignalAnalyzer.ADX_THRESHOLD:
                return False, "bear"
            if config.get("use_vol_confirm", True) and vr < SignalAnalyzer.VOL_THRESHOLD:
                return False, "bear"
        return True, direction


# ── TRADING ENGINE ────────────────────────────────────────────────────────────
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue, config, broker):
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

        if not self.is_licensed:
            self.config["mode"] = "signal"
            self.config["broker"] = "Alpaca"
            self.config["direction"] = "both"
            self.direction = "both"
            if "alpaca" in self.config:
                self.config["alpaca"]["paper"] = True
            for k in ("use_supertrend", "use_stochastic", "use_adx", "use_vol_confirm", "use_atr_stops", "use_bracket"):
                self.config[k] = False
            first = self.config.get("tickers", "AAPL").split(",")[0].strip()
            self.config["tickers"] = first

    def _telegram(self, msg):
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
            self.ui_queue.put(("error", f"Free tier: only 1 ticker allowed. Tracking {first} only."))

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

        self.broker.stream_prices(self.symbols, lambda s, p: self.ui_queue.put(("price_update", (s, p))))
        self.ui_queue.put(("status", f"Running {len(self.symbols)} symbol(s)"))
        self._telegram(f"TraderMoney started\n{', '.join(self.symbols)} | {mode}")

        if use_bracket and self.broker.name != "Alpaca":
            threading.Thread(target=self._sl_tp_watchdog, daemon=True).start()

        last_fetch = 0.0
        while self.running:
            try:
                acc = self.broker.get_account()
                if acc:
                    self.ui_queue.put(("account", (acc["equity"], acc["pl"], acc["buying_power"], acc.get("open_positions", 0))))
                self.ui_queue.put(("market", "Open" if self.broker.get_market_status() else "Closed"))

                now = time.time()
                if now - last_fetch >= 60:
                    last_fetch = now
                    for s in self.symbols:
                        try:
                            import yfinance as yf
                            import pandas as pd
                            df = yf.download(s, period="5d", interval=interval, progress=False, auto_adjust=True)
                            if df is None or df.empty:
                                continue
                            if isinstance(df.columns, pd.MultiIndex):
                                df.columns = df.columns.get_level_values(0)
                            df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
                        except Exception as e:
                            self.ui_queue.put(("error", f"Data error {s}: {e}"))
                            continue

                        latest = df.iloc[-1]
                        sf = SignalAnalyzer._sf
                        price = sf(latest["Close"])
                        ef = sf(latest["EMA_fast"])
                        es = sf(latest["EMA_slow"])
                        prev_f, prev_s = self.prev_ema.get(s, (None, None))
                        self.prev_ema[s] = (ef, es)

                        if prev_f is not None:
                            sig, rationale, conf = SignalAnalyzer.generate_signal(df, prev_f, prev_s, self.config)
                            if sig:
                                self.ui_queue.put(("signal", (s, sig, price, rationale)))
                                db.insert_signal(_ts(), s, sig, price, rationale)
                                if mode == "auto" and self.is_licensed and self.broker.get_market_status():
                                    self._execute(s, sig, price, latest, use_bracket, use_atr, sl_pct, tp_pct, conf)
                time.sleep(1)
            except Exception:
                self.ui_queue.put(("error", f"Engine error:\n{traceback.format_exc()}"))
                time.sleep(5)

        self.broker.stop_stream()
        self.ui_queue.put(("status", "Bot stopped"))

    def _execute(self, sym, sig, price, latest, use_bracket, use_atr, sl_pct, tp_pct, conf):
        try:
            qty = self.per_ticker_qty.get(sym, self.config.get("quantity", 1))
            sf = SignalAnalyzer._sf
            if self.direction == "long" and sig == "SELL":
                return
            if self.direction == "short" and sig == "BUY":
                return
            pos = self.positions.get(sym, 0)
            if sig == "BUY":
                if pos <= 0:
                    if pos < 0:
                        self.broker.submit_order(sym, abs(pos), "buy")
                        self.positions[sym] = 0
                    ok = False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR", price * 0.02), price * 0.02)
                        ok = self.broker.submit_order(sym, qty, "buy",
                                                      sl_price=price - ATR_STOP_MULT * atr,
                                                      tp_price=price + ATR_TP_MULT * atr)
                    elif use_bracket:
                        ok = self.broker.submit_order(sym, qty, "buy", sl_pct=sl_pct, tp_pct=tp_pct)
                    else:
                        ok = self.broker.submit_order(sym, qty, "buy")
                    if ok:
                        self.positions[sym] = qty
                        self.ui_queue.put(("order", (sym, "BUY", qty, price)))
                        db.insert_trade(_ts(), sym, "BUY", qty, price)
                        self._telegram(f"BUY {qty} {sym} @ ${price:.2f} (conf: {conf:.2f})")
            elif sig == "SELL":
                if pos >= 0:
                    if pos > 0:
                        self.broker.submit_order(sym, pos, "sell")
                        self.positions[sym] = 0
                    ok = False
                    if use_bracket and use_atr:
                        atr = sf(latest.get("ATR", price * 0.02), price * 0.02)
                        ok = self.broker.submit_order(sym, qty, "sell",
                                                      sl_price=price + ATR_STOP_MULT * atr,
                                                      tp_price=price - ATR_TP_MULT * atr)
                    elif use_bracket:
                        ok = self.broker.submit_order(sym, qty, "sell", sl_pct=sl_pct, tp_pct=tp_pct)
                    else:
                        ok = self.broker.submit_order(sym, qty, "sell")
                    if ok:
                        self.positions[sym] = -qty
                        self.ui_queue.put(("order", (sym, "SELL", qty, price)))
                        db.insert_trade(_ts(), sym, "SELL", qty, price)
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
                    stop = price * (1 - 0.02) if qty > 0 else price * (1 + 0.02)
                    take = price * (1 + 0.04) if qty > 0 else price * (1 - 0.04)
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

    if state.engine and state.engine.running:
        return jsonify({"status": "error", "message": "Bot already running."})

    if not state.config.get("license_valid"):
        state.config["broker"] = "Alpaca"
        state.config["mode"] = "signal"
        state.config["direction"] = "both"
        if "alpaca" not in state.config or not isinstance(state.config["alpaca"], dict):
            state.config["alpaca"] = dict(_DEFAULT_CONFIG["alpaca"])
        state.config["alpaca"]["paper"] = True
        for k in ("use_supertrend", "use_stochastic", "use_adx", "use_vol_confirm", "use_atr_stops", "use_bracket"):
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
    return jsonify({"status": "ok", "message": "Bot stopped"})


@app.route("/api/kill", methods=["POST"])
def api_kill():
    if state.broker_instance:
        threading.Thread(target=state.broker_instance.close_all_positions, daemon=True).start()
    if state.engine:
        state.engine.stop()
    state.running = False
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
    return jsonify({
        "running": state.running,
        "equity": state.dashboard["equity"],
        "pl": state.dashboard["pl"],
        "buying_power": state.dashboard["buying_power"],
        "open_positions": state.dashboard["open_positions"],
        "signals": db.get_recent_signals(50)[::-1],
        "orders": db.get_recent_trades(50)[::-1],
        "log": db.get_recent_logs(100),
    })


@app.route("/api/broker_status", methods=["GET"])
def api_broker_status():
    return jsonify({"message": state.config.get("last_broker_message", "")})


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
    else:
        state.config["license_valid"] = False
        return jsonify({"valid": False, "message": msg})


@app.route("/api/update", methods=["GET"])
def api_update():
    try:
        url = "https://raw.githubusercontent.com/shafayrich/tradermoney/main/version.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
        latest = data.get("latest_version", "0.0.0")
        newer = tuple(map(int, latest.split("."))) > tuple(map(int, APP_VERSION.split(".")))
        return jsonify({
            "current_version": APP_VERSION,
            "latest_version": latest,
            "download_url": data.get("download_url", ""),
            "update_available": newer,
        })
    except Exception as e:
        return jsonify({"update_available": False, "error": str(e)})


# ── BACKTEST (simple signal detection, no portfolio simulation) ──────────────
@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    data = request.json or {}
    config = data.get("config", state.config)
    days = int(data.get("days", 5))
    try:
        import yfinance as yf
        import pandas as pd

        raw_list = [s.strip() for s in config.get("tickers", "AAPL").split(",") if s.strip()]
        symbols = list(dict.fromkeys(clean_symbol(e) for e in raw_list))
        ef, es = config.get("emas", [9, 50])
        interval = config.get("timeframe", "1m")
        results = {}

        for sym in symbols:
            try:
                df = yf.download(sym, period=f"{days}d", interval=interval, progress=False, auto_adjust=True)
                if df is None or df.empty and interval != "1d":
                    df = yf.download(sym, period=f"{days}d", interval="1d", progress=False, auto_adjust=True)
                if df is None or df.empty:
                    results[sym] = {"error": "No data returned"}
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = IndicatorCalculator.compute_all(df, ef, es)
                sigs = []
                for i in range(1, len(df)):
                    prev = df.iloc[i - 1]
                    curr = df.iloc[i]
                    pf = SignalAnalyzer._sf(prev["EMA_fast"])
                    ps = SignalAnalyzer._sf(prev["EMA_slow"])
                    sig, rat, conf = SignalAnalyzer.generate_signal(df.iloc[:i + 1], pf, ps, config)
                    if sig:
                        sf = SignalAnalyzer._sf
                        sigs.append({
                            "time": str(df.index[i]),
                            "signal": sig,
                            "price": round(sf(curr["Close"]), 2),
                            "rationale": rat,
                            "confidence": conf,
                            "indicators": {
                                "RSI": round(sf(curr.get("RSI", 50), 50), 1),
                                "MACD": round(sf(curr.get("MACD", 0), 0), 4),
                                "MACD_signal": round(sf(curr.get("MACD_signal", 0), 0), 4),
                                "VWAP": round(sf(curr.get("VWAP", 0), 0), 2),
                                "BB_lower": round(sf(curr.get("BB_lower", 0), 0), 2),
                                "BB_upper": round(sf(curr.get("BB_upper", 0), 0), 2),
                                "ADX": round(sf(curr.get("ADX", 0), 0), 1),
                                "Vol_ratio": round(sf(curr.get("Vol_ratio", 1), 1), 2),
                                "Supertrend_trend": int(sf(curr.get("Supertrend_trend", 0), 0)),
                                "Stoch_K": round(sf(curr.get("Stoch_K", 50), 50), 1),
                                "Stoch_D": round(sf(curr.get("Stoch_D", 50), 50), 1),
                            },
                        })
                results[sym] = {"signals": sigs}
            except Exception as e:
                results[sym] = {"error": str(e)}

        db.insert_backtest(json.dumps({"config": config, "results": results}))
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── AI CHAT SESSIONS ──────────────────────────────────────────────────────────
@app.route("/api/chat/sessions", methods=["GET"])
def get_chat_sessions():
    return jsonify({"sessions": db.get_chat_sessions()})


@app.route("/api/chat/sessions", methods=["POST"])
def create_chat_session():
    title = (request.json or {}).get("title", "")
    session_id = db.create_chat_session(title)
    return jsonify({"session_id": session_id})


@app.route("/api/chat/sessions/<int:session_id>", methods=["PUT"])
def rename_chat_session(session_id):
    new_title = (request.json or {}).get("title", "").strip()
    if not new_title:
        return jsonify({"error": "Title cannot be empty"}), 400
    db.rename_chat_session(session_id, new_title)
    return jsonify({"status": "ok"})


@app.route("/api/chat/sessions/<int:session_id>", methods=["DELETE"])
def delete_chat_session(session_id):
    db.delete_chat_session(session_id)
    return jsonify({"status": "ok"})


@app.route("/api/chat/sessions/<int:session_id>/history", methods=["GET"])
def get_chat_history(session_id):
    history = db.get_chat_history(session_id, 200)
    return jsonify({"messages": history})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    global _chat_counter
    data = request.json or {}
    message = data.get("message", "").strip()
    session_id = data.get("session_id")
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
                "reply": f"Daily chat limit reached ({FREE_CHAT_DAILY_LIMIT} messages/day on Free tier). Upgrade to Pro for unlimited AI access."
            })
        _chat_counter["count"] += 1

    if not session_id:
        session_id = db.create_chat_session()
    else:
        # verify session exists
        pass

    db.insert_chat_message(session_id, "user", message)

    if not CHATANYWHERE_API_KEY or CHATANYWHERE_API_KEY.startswith("sk-YOUR"):
        return jsonify({"reply": "ChatAnywhere API key not configured. Please update CHATANYWHERE_API_KEY in app.py"})

    # Build conversation context
    history = db.get_chat_history(session_id, limit=20)
    messages = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})

    try:
        resp = http_requests.post(
            "https://api.chatanywhere.tech/v1/chat/completions",
            headers={"Authorization": f"Bearer {CHATANYWHERE_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-3.5-turbo", "messages": messages, "max_tokens": 350, "temperature": 0.65},
            timeout=30,
        )
        result = resp.json()
        if "error" in result:
            err_msg = result["error"].get("message", "Unknown ChatAnywhere error")
            db.insert_log(f"[AI Chat] Error: {err_msg}")
            return jsonify({"reply": f"AI error: {err_msg}"})
        reply = result["choices"][0]["message"]["content"].strip()
        db.insert_chat_message(session_id, "bot", reply)
        return jsonify({"reply": reply, "session_id": session_id})
    except Exception as e:
        db.insert_log(f"[AI Chat] Exception: {e}")
        return jsonify({"reply": f"AI service unavailable: {e}"})


# ── FRONTEND HTML (TradingView charts + Chat sessions) ───────────────────────
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TraderMoney 2.0.4</title>
<style>
:root{--bg:#050505;--card:#1A1A1A;--text:#e2e2e2;--accent:#D4AF37;--danger:#B22222;--border:#2A2E38;--muted:#7a7d86;--sw:300px;--radius:12px;}
::-webkit-scrollbar{width:4px;}::-webkit-scrollbar-track{background:#080808;}::-webkit-scrollbar-thumb{background:#111;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden;color-scheme:dark;}
#sb{width:var(--sw);background:#0c0c0c;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding:22px 18px;flex-shrink:0;gap:8px;}
#sb h2{color:var(--accent);margin:0 0 6px;font-size:1.3rem;letter-spacing:.3px;}
.lbadge{display:inline-block;padding:2px 12px;border-radius:30px;font-size:.7rem;margin-left:6px;vertical-align:middle;}
.lv{background:var(--accent);color:#000;}.li{background:var(--danger);color:#fff;}
label{display:block;font-size:.75rem;margin:10px 0 4px;color:var(--muted);cursor:pointer;letter-spacing:.3px;}
.cb input{display:none;}
.cb .cm{display:inline-block;width:18px;height:18px;border:2px solid #444;border-radius:6px;margin-right:8px;vertical-align:middle;position:relative;transition:.2s;}
.cb input:checked+.cm{background:var(--accent);border-color:var(--accent);}
.cb input:checked+.cm::after{content:"";position:absolute;left:4px;top:1px;width:5px;height:9px;border:solid #000;border-width:0 2px 2px 0;transform:rotate(45deg);}
select, input[type="text"], input[type="password"], input[type="number"], textarea{background:#1A1A1A;color:var(--text);border:1px solid #333;border-radius:10px;padding:8px 12px;width:100%;font-size:.85rem;transition:border .2s;}
select{appearance:none;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 12 12'><polygon fill='%23D4AF37' points='0,4 12,4 6,10'/></svg>");background-repeat:no-repeat;background-position:right 12px center;background-size:12px;}
select:focus, input:focus, textarea:focus{border-color:var(--accent);outline:none;}
button{cursor:pointer;background:var(--accent);color:#050505;border:none;padding:10px 14px;border-radius:10px;width:100%;font-weight:600;margin-top:12px;font-size:.85rem;transition:all .2s;}
button:hover{opacity:.88;transform:translateY(-1px);}
button.ghost{background:var(--card);border:1px solid var(--border);color:var(--text);}
button.danger{background:var(--danger);color:#fff;}
hr{border-color:var(--border);margin:12px 0;}
.r2{display:flex;gap:6px;} .r2 input{width:100%;}
#bstatus{font-size:.72rem;margin-top:4px;min-height:16px;word-break:break-word;}
#bstatus.ok{color:#00c9b1;}#bstatus.err{color:var(--danger);}
#main{flex:1;display:flex;flex-direction:column;min-width:0;}
.tab-bar{display:flex;background:var(--card);border-bottom:1px solid var(--border);overflow-x:auto;gap:4px;padding:0 8px;}
.tbtn{background:transparent;border:none;color:var(--text);padding:14px 16px;cursor:pointer;font-weight:500;border-bottom:2px solid transparent;transition:.2s;font-size:.85rem;}
.tbtn:hover{background:rgba(255,255,255,.03);}
.tbtn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700;}
.tab{flex:1;display:none;overflow:hidden;flex-direction:column;}
.tab.active{display:flex;}
#metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--border);}
.met{text-align:center;} .met .v{font-size:1.3rem;font-weight:bold;color:var(--accent);}
#sess{display:flex;align-items:center;gap:16px;padding:10px 16px;background:var(--card);border-bottom:1px solid var(--border);font-size:.8rem;flex-wrap:wrap;}
.sd{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;}
.so{background:#00c9b1;}.sc{background:var(--danger);}
#tkbar{display:flex;flex-wrap:nowrap;overflow-x:auto;background:var(--card);border-bottom:1px solid var(--border);gap:2px;padding:4px 8px;}
.tkbtn{padding:8px 16px;background:transparent;border:none;color:var(--text);cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:.2s;font-size:.85rem;}
.tkbtn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700;}
#chart-c{flex:1;min-height:0;}
.sitem{display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--border);font-size:.85rem;}
.buy{color:var(--accent);}.sell{color:var(--danger);}
.empty-placeholder{color:var(--muted);text-align:center;padding:40px;font-size:.9rem;}
#toasts{position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;}
.toast{padding:12px 20px;border-radius:14px;font-weight:500;box-shadow:0 4px 18px rgba(0,0,0,.5);animation:si .25s ease;max-width:400px;font-size:.9rem;border:1px solid #333;}
.toast.success{background:var(--accent);color:#000;}.toast.error{background:var(--danger);color:#fff;}
@keyframes si{from{transform:translateX(110%);opacity:0}to{transform:translateX(0);opacity:1}}
#upd{display:none;position:fixed;bottom:20px;right:20px;z-index:9999;background:var(--accent);color:#000;padding:12px 20px;border-radius:12px;font-weight:bold;font-size:.9rem;}
#upd a{color:#000;text-decoration:underline;}
.btp{flex:1;display:flex;flex-direction:column;}
.btr{flex:1;overflow-y:auto;overflow-x:auto;padding:16px;}
.ph{color:var(--muted);text-align:center;padding:40px;font-size:.9rem;}
.bttbl{width:100%;border-collapse:collapse;font-size:.8rem;margin-bottom:20px;}
.bttbl th,.bttbl td{padding:6px 8px;border:1px solid var(--border);text-align:center;}
.bttbl th{color:var(--accent);}
#logbar{height:110px;overflow-y:auto;background:var(--bg);padding:10px 14px;font-size:.75rem;border-top:1px solid var(--border);color:var(--muted);flex-shrink:0;}
.hb{padding:24px;overflow-y:auto;height:100%;}
.hb h3{color:var(--accent);margin-top:0;}.hb h4{color:var(--text);margin:16px 0 6px;}
.hb p,.hb ul{font-size:.85rem;line-height:1.65;}.hb ul{padding-left:20px;margin:6px 0;}.hb li{margin-bottom:5px;}
.hb a{color:var(--accent);}
.istat{background:var(--card);border-radius:var(--radius);padding:14px;margin:12px 0;}
.free-notice{background:#2a0505;color:#ff9090;border:1px solid var(--danger);padding:10px 14px;border-radius:10px;font-size:.8rem;margin-top:12px;display:none;line-height:1.5;}
/* AI Chat with sessions sidebar */
#chat-layout{display:flex;height:100%;}
#chat-sessions{width:220px;background:var(--card);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
#chat-sessions h3{color:var(--accent);font-size:.9rem;padding:14px;margin:0;border-bottom:1px solid var(--border);}
#sessions-list{flex:1;overflow-y:auto;}
.session-item{padding:10px 14px;cursor:pointer;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;font-size:.8rem;}
.session-item:hover{background:#0a0a0a;}
.session-item.active{background:#0a0a0a;color:var(--accent);}
.session-title{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-right:8px;}
.session-actions{display:flex;gap:6px;}
.session-actions button{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:1rem;padding:2px;margin:0;width:auto;}
.session-actions button:hover{color:var(--accent);}
#new-chat-btn{background:var(--accent);color:#000;border:none;padding:10px;margin:12px;border-radius:8px;cursor:pointer;font-weight:600;}
#chat-area{flex:1;display:flex;flex-direction:column;}
#chat-topbar{padding:12px 16px;background:var(--card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;}
#chat-topbar .title{color:var(--accent);font-weight:600;font-size:.95rem;}
#chat-limit{font-size:.75rem;color:var(--muted);}
#chat-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px;}
.cmsg{max-width:80%;padding:10px 16px;border-radius:16px;font-size:.88rem;line-height:1.55;word-break:break-word;}
.cmsg.bot{background:#1a1200;border:1px solid #4a3800;align-self:flex-start;border-radius:4px 16px 16px 16px;}
.cmsg.user{background:#1e1e1e;border:1px solid #333;align-self:flex-end;border-radius:16px 4px 16px 16px;}
.cmsg .msender{font-size:.7rem;color:var(--accent);margin-bottom:5px;font-weight:700;}
.cmsg.user .msender{color:var(--muted);}
.cmsg .mbody{white-space:pre-wrap;}
.chat-typing{color:var(--muted);font-size:.8rem;padding:5px 10px;font-style:italic;align-self:flex-start;}
#chat-input-row{display:flex;gap:10px;padding:14px;border-top:1px solid var(--border);background:var(--card);align-items:center;}
#chat-input{flex:1;resize:none;height:48px;padding:10px 14px;font-size:.88rem;}
#chat-send{width:auto;margin:0;padding:10px 20px;}
</style>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
</head>
<body>
<div id="toasts"></div>
<div id="upd">Update available! <a id="udl" href="#" target="_blank">Download</a></div>

<!-- SIDEBAR -->
<div id="sb">
  <h2>TraderMoney <span id="lbadge" class="lbadge li">FREE</span></h2>
  <label>License Key</label>
  <input type="password" id="lickey" placeholder="Paste Gumroad key">
  <button onclick="validateLicense()">Validate</button>
  <p style="font-size:.68rem;color:var(--muted);margin:4px 0 0;"><a href="https://shafayrich.gumroad.com/l/ykaoov" style="color:var(--accent)">Buy license ↗</a></p>
  <div id="free-notice" class="free-notice">Free tier: Alpaca paper only · Signal-Only · 1 ticker · Core indicators only (RSI, MACD, VWAP, Bollinger). AI Chat: 5 messages/day.<br><b>License NOT saved – re‑enter each session.</b></div>
  <hr>
  <label>Broker</label><select id="broker" onchange="onBrokerChange()"></select>
  <div id="bstatus" class="ok"></div>
  <div id="creds"></div>
  <label>Telegram Token</label><input type="password" id="tgt">
  <label>Telegram Chat ID</label><input id="tgc">
  <label>Tickers (e.g. AAPL:5)</label><input id="tickers" value="AAPL">
  <label>Timeframe</label><select id="tf"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>1d</option></select>
  <label>EMA periods</label><div class="r2"><input id="emaf" value="9"><input id="emas" value="50"></div>
  <label><span class="cb"><input type="checkbox" id="udefqty" checked onchange="toggleDefQty()"><span class="cm"></span></span> Use fallback quantity</label>
  <div id="defqty-box"><label>Default Qty</label><input id="qty" value="1" type="number"></div>
  <label>Mode</label><select id="mode"><option value="signal">Signal Only</option><option value="auto">Auto Trade</option></select>
  <label>Direction</label><select id="dir"><option value="both">Both</option><option value="long">Long Only</option><option value="short">Short Only</option></select>
  <label><span class="cb"><input type="checkbox" id="ubracket"><span class="cm"></span></span> Bracket SL/TP</label>
  <div class="r2"><input id="slp" value="2" placeholder="SL %"><input id="tpp" value="4" placeholder="TP %"></div>
  <label><span class="cb"><input type="checkbox" id="uatr" checked><span class="cm"></span></span> ATR Stops</label>
  <label style="margin-top:12px;font-weight:bold;color:var(--accent)">Indicators</label>
  <label><span class="cb"><input type="checkbox" id="ursi"   checked><span class="cm"></span></span> RSI</label>
  <label><span class="cb"><input type="checkbox" id="umacd"  checked><span class="cm"></span></span> MACD</label>
  <label><span class="cb"><input type="checkbox" id="uvwap"  checked><span class="cm"></span></span> VWAP</label>
  <label><span class="cb"><input type="checkbox" id="uboll"  checked><span class="cm"></span></span> Bollinger</label>
  <label><span class="cb"><input type="checkbox" id="uadx"   checked><span class="cm"></span></span> ADX <span style="font-size:.65rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="uvol"   checked><span class="cm"></span></span> Volume <span style="font-size:.65rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="ust"    checked><span class="cm"></span></span> SuperTrend <span style="font-size:.65rem;color:var(--accent)">[PRO]</span></label>
  <label><span class="cb"><input type="checkbox" id="ustoch" checked><span class="cm"></span></span> Stochastic <span style="font-size:.65rem;color:var(--accent)">[PRO]</span></label>
  <button onclick="saveConfig()">Save</button>
  <button class="ghost" onclick="refreshTickers()">Refresh Tickers</button>
  <button style="background:var(--accent);" id="startBtn" onclick="startBot()">▶ Start Bot</button>
  <button class="ghost" id="stopBtn" onclick="stopBot()">■ Stop Bot</button>
  <button class="danger" onclick="killSwitch()">⚠ Kill Switch</button>
  <button class="ghost" onclick="resetDef()">↺ Reset</button>
  <button class="ghost" onclick="checkUpdate()">🔄 Check Updates</button>
  <button class="ghost" onclick="runBT()">📊 Backtest All</button>
  <div style="margin-top:8px;font-size:.75rem;color:var(--muted);">Backtest days: <input type="number" id="btDays" value="5" min="1" max="365" style="width:75px;display:inline-block;"></div>
</div>

<!-- MAIN AREA -->
<div id="main">
  <div class="tab-bar" id="tabbar">
    <button class="tbtn active" data-tab="charts">Charts</button>
    <button class="tbtn" data-tab="signals">Signals</button>
    <button class="tbtn" data-tab="history">History</button>
    <button class="tbtn" data-tab="backtest">Backtest</button>
    <button class="tbtn" data-tab="help">Help</button>
    <button class="tbtn" data-tab="aichat">AI Chat</button>
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
      <span><span class="sd" id="ds"></span>SYD</span><span><span class="sd" id="dt"></span>TKY</span>
      <span><span class="sd" id="dl"></span>LDN</span><span><span class="sd" id="dn"></span>NYC</span>
      <span><span class="sd so"></span>CRYPTO</span>
      <span id="utc-clock" style="margin-left:auto;font-size:.75rem;">UTC: --</span>
    </div>
    <div id="chart-c"></div>
  </div>

  <!-- Signals -->
  <div id="tab-signals" class="tab"><div id="siglist" style="overflow-y:auto;flex:1;"></div><div id="sigempty" class="empty-placeholder">No signals yet.</div></div>
  <!-- History -->
  <div id="tab-history" class="tab"><div id="histlist" style="overflow-y:auto;flex:1;"></div><div id="hstempty" class="empty-placeholder">No orders yet.</div></div>

  <!-- Backtest -->
  <div id="tab-backtest" class="tab">
    <div class="btp"><div style="padding:12px;"><button class="ghost" style="width:auto;padding:9px 24px;" onclick="runBT()">▶ Run Backtest</button></div><div id="btres" class="btr"><p class="ph">Click Run Backtest.</p></div></div>
  </div>

  <!-- Help -->
  <div id="tab-help" class="tab">
    <div class="hb">
      <h3>Indicator &amp; Trading Guide</h3>
      <div class="istat"><p><b>Pure EMA Crossover:</b> ~32%<br><b>+RSI:</b> ~40% | <b>+MACD:</b> ~45% | <b>+VWAP:</b> ~48%<br><b>+Bollinger:</b> ~50% | <b>+ADX ≥20:</b> ~55%<br><b>+Volume 1.5x:</b> ~58% | <b>+SuperTrend:</b> ~62% | <b>+Stochastic:</b> ~65%<br><b>ATR stops</b> improve profit factor by ~0.4</p></div>
      <h4>Short Selling Logic</h4>
      <table class="bttbl"><tr><th>Indicator</th><th>Long condition</th><th>Short condition</th></tr><tr><td>RSI</td><td>RSI ≥ 30</td><td>RSI ≤ 70</td></tr><tr><td>MACD</td><td>MACD > signal</td><td>MACD < signal</td></tr><tr><td>VWAP</td><td>Price > VWAP</td><td>Price < VWAP</td></tr><tr><td>SuperTrend</td><td>Trend = 1</td><td>Trend = -1</td></tr><tr><td>Stochastic</td><td>%K > %D, %K<80</td><td>%K < %D, %K>20</td></tr></table>
      <h4>Broker Connection</h4>
      <ul><li><b>Alpaca:</b> API Key + Secret, paper checkbox.</li><li><b>IBKR:</b> TWS/Gateway API enabled, ports 7497/7496/4002/4001.</li><li><b>Tradier:</b> Access Token + Account ID, sandbox option.</li><li><b>Binance/Bybit/OKX:</b> API Key + Secret, testnet/demo checkbox.</li></ul>
      <h4>AI Chat</h4><p>Upgrade to Pro for unlimited messages. Free tier: 5 per day.</p>
    </div>
  </div>

  <!-- AI Chat with Sessions -->
  <div id="tab-aichat" class="tab">
    <div id="chat-layout">
      <div id="chat-sessions">
        <h3>💬 Chats</h3>
        <div id="sessions-list"></div>
        <button id="new-chat-btn" onclick="createNewSession()">+ New Chat</button>
      </div>
      <div id="chat-area">
        <div id="chat-topbar"><span class="title">🤖 TraderBot AI</span><span id="chat-limit"></span></div>
        <div id="chat-messages"></div>
        <div id="chat-input-row">
          <textarea id="chat-input" placeholder="Ask about indicators, strategies..."></textarea>
          <button id="chat-send" onclick="sendChat()">Send</button>
        </div>
      </div>
    </div>
  </div>

  <div id="logbar"></div>
</div>

<script src="https://s3.tradingview.com/tv.js"></script>
<script>
'use strict';
const $ = id => document.getElementById(id);
let cfg={}, licValid=false, curSym='', allTickers=[], chart=null, lastChart='';
let currentSessionId = null;

// Utilities
function cs(raw){ return raw.split(':')[0].trim().toUpperCase(); }
function fmt(n,d=2){ return Number(n).toLocaleString(undefined,{maximumFractionDigits:d}); }
function toast(msg,type='info'){
  let t=document.createElement('div'); t.className='toast '+type; t.textContent=msg;
  $('toasts').appendChild(t); setTimeout(()=>t.remove(),3800);
}
function gv(id,fb=''){ let e=$(id); return e?e.value:fb; }
function gc(id){ let e=$(id); return e?e.checked:false; }
function sv(id,v){ let e=$(id); if(e) e.value=v; }
function sc(id,v){ let e=$(id); if(e) e.checked=!!v; }
function lockCb(id,locked){
  let el=$(id); if(!el) return; el.disabled=locked;
  let lbl=el.closest('label'); if(lbl){ lbl.style.opacity=locked?'0.4':'1'; lbl.style.pointerEvents=locked?'none':''; }
}
function applyFreeTierUI(){
  updateBrokerOptions(); $('broker').disabled=true; sv('broker','Alpaca'); cfg.broker='Alpaca';
  sv('mode','signal'); $('mode').disabled=true; sv('dir','both'); $('dir').disabled=true;
  ['ubracket','uatr','uadx','uvol','ust','ustoch'].forEach(id=>{ sc(id,false); lockCb(id,true); });
  $('free-notice').style.display='block'; $('lbadge').textContent='FREE'; $('lbadge').className='lbadge li';
}
function applyProUI(){
  updateBrokerOptions(); $('broker').disabled=false; $('mode').disabled=false; $('dir').disabled=false;
  ['ubracket','uatr','uadx','uvol','ust','ustoch'].forEach(id=>lockCb(id,false));
  $('free-notice').style.display='none'; $('lbadge').textContent='PRO'; $('lbadge').className='lbadge lv';
}

// Tabs
document.querySelectorAll('.tbtn').forEach(b=>{
  b.addEventListener('click',function(){
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.tbtn').forEach(x=>x.classList.remove('active'));
    $('tab-'+this.dataset.tab).classList.add('active');
    this.classList.add('active');
    if(this.dataset.tab==='charts' && chart) setTimeout(()=>chart.resize&&chart.resize(),80);
    if(this.dataset.tab==='aichat') initAIChat();
  });
});
Sortable.create($('tabbar'),{animation:120,handle:'.tbtn'});

// Markets clock
function updSess(){
  let n=new Date(), d=n.getUTCDay(), wk=d===0||d===6, h=n.getUTCHours()+n.getUTCMinutes()/60;
  let o=ok=>ok?'sd so':'sd sc';
  $('ds').className=o(!wk&&(h>=22||h<5)); $('dt').className=o(!wk&&(h>=23||h<6));
  $('dl').className=o(!wk&&h>=8&&h<16.5); $('dn').className=o(!wk&&h>=13.5&&h<20);
  $('utc-clock').textContent='UTC: '+n.toISOString().slice(11,19);
}
setInterval(updSess,30000); updSess();

// Broker credentials
function pw(id,l){ return `<label>${l}</label><input type="password" id="${id}">`; }
function tx(id,l,v=''){ return `<label>${l}</label><input id="${id}" value="${v}">`; }
function cbHTML(id,l,chk=false){ return `<label><span class="cb"><input type="checkbox" id="${id}" ${chk?'checked':''}><span class="cm"></span></span> ${l}</label>`; }
function saveCurrentBrokerCreds(){
  const b=cfg.broker||'Alpaca';
  if(b==='Alpaca'){ cfg.alpaca=cfg.alpaca||{}; cfg.alpaca.api_key=gv('ak',''); cfg.alpaca.secret_key=gv('ask',''); cfg.alpaca.paper=gc('apaper'); }
  else if(b==='Interactive Brokers'){ cfg.ibkr=cfg.ibkr||{}; cfg.ibkr.host=gv('ih',''); cfg.ibkr.port=gv('ip',''); cfg.ibkr.client_id=gv('icid',''); }
  else if(b==='Tradier'){ cfg.tradier=cfg.tradier||{}; cfg.tradier.access_token=gv('trat',''); cfg.tradier.account_id=gv('traid',''); cfg.tradier.sandbox=gc('trsb'); }
  else if(b==='Binance'){ cfg.binance=cfg.binance||{}; cfg.binance.api_key=gv('bnk',''); cfg.binance.api_secret=gv('bns',''); cfg.binance.testnet=gc('bnt'); }
  else if(b==='Bybit'){ cfg.bybit=cfg.bybit||{}; cfg.bybit.api_key=gv('bbk',''); cfg.bybit.api_secret=gv('bbs',''); cfg.bybit.testnet=gc('bbtn'); }
  else if(b==='OKX'){ cfg.okx=cfg.okx||{}; cfg.okx.api_key=gv('ok',''); cfg.okx.api_secret=gv('os',''); cfg.okx.api_passphrase=gv('op',''); cfg.okx.demo=gc('od'); }
}
function populateCredsFields(){
  const b=cfg.broker||'Alpaca';
  if(b==='Alpaca'&&cfg.alpaca){ sv('ak',cfg.alpaca.api_key||''); sv('ask',cfg.alpaca.secret_key||''); sc('apaper',cfg.alpaca.paper!==false); }
  else if(b==='Interactive Brokers'&&cfg.ibkr){ sv('ih',cfg.ibkr.host||''); sv('ip',cfg.ibkr.port||''); sv('icid',cfg.ibkr.client_id||''); }
  else if(b==='Tradier'&&cfg.tradier){ sv('trat',cfg.tradier.access_token||''); sv('traid',cfg.tradier.account_id||''); sc('trsb',cfg.tradier.sandbox===true); }
  else if(b==='Binance'&&cfg.binance){ sv('bnk',cfg.binance.api_key||''); sv('bns',cfg.binance.api_secret||''); sc('bnt',cfg.binance.testnet!==false); }
  else if(b==='Bybit'&&cfg.bybit){ sv('bbk',cfg.bybit.api_key||''); sv('bbs',cfg.bybit.api_secret||''); sc('bbtn',cfg.bybit.testnet!==false); }
  else if(b==='OKX'&&cfg.okx){ sv('ok',cfg.okx.api_key||''); sv('os',cfg.okx.api_secret||''); sv('op',cfg.okx.api_passphrase||''); sc('od',cfg.okx.demo!==false); }
}
function updateCreds(){
  saveCurrentBrokerCreds(); const b=cfg.broker||'Alpaca', c=$('creds'); c.innerHTML='';
  if(b==='Alpaca') c.innerHTML=pw('ak','API Key')+pw('ask','Secret Key')+cbHTML('apaper','Paper Trading',true);
  else if(b==='Interactive Brokers') c.innerHTML=tx('ih','Host')+tx('ip','Port')+tx('icid','Client ID');
  else if(b==='Tradier') c.innerHTML=pw('trat','Access Token')+tx('traid','Account ID')+cbHTML('trsb','Sandbox',false);
  else if(b==='Binance') c.innerHTML=pw('bnk','API Key')+pw('bns','API Secret')+cbHTML('bnt','Testnet',true);
  else if(b==='Bybit') c.innerHTML=pw('bbk','API Key')+pw('bbs','API Secret')+cbHTML('bbtn','Testnet',true);
  else if(b==='OKX') c.innerHTML=pw('ok','API Key')+pw('os','API Secret')+pw('op','Passphrase')+cbHTML('od','Demo',true);
  populateCredsFields();
}
function updateBrokerOptions(){
  const sel=$('broker'), cur=cfg.broker||'Alpaca'; sel.innerHTML='';
  const addOpt=(v,l)=>{ let o=document.createElement('option'); o.value=v; o.textContent=l; sel.appendChild(o); };
  addOpt('Alpaca','Alpaca');
  if(licValid){ addOpt('Interactive Brokers','Interactive Brokers'); addOpt('Tradier','Tradier'); addOpt('Binance','Binance'); addOpt('Bybit','Bybit'); addOpt('OKX','OKX'); }
  sel.value=licValid?cur:'Alpaca';
}
function onBrokerChange(){ cfg.broker=$('broker').value; updateCreds(); }
function toggleDefQty(){ $('defqty-box').style.display=gc('udefqty')?'block':'none'; }
function buildCfg(){
  saveCurrentBrokerCreds();
  return {
    broker:cfg.broker||'Alpaca', tickers:gv('tickers','AAPL'), timeframe:gv('tf','1m'),
    emas:[parseInt(gv('emaf','9')),parseInt(gv('emas','50'))], quantity:parseInt(gv('qty','1'))||1,
    mode:gv('mode','signal'), direction:gv('dir','both'), use_default_qty:gc('udefqty'),
    use_bracket:gc('ubracket'), sl_percent:parseFloat(gv('slp','2')), tp_percent:parseFloat(gv('tpp','4')),
    use_atr_stops:gc('uatr'), telegram:{token:gv('tgt'),chat_id:gv('tgc')},
    use_rsi:gc('ursi'), use_macd:gc('umacd'), use_vwap:gc('uvwap'), use_bollinger:gc('uboll'),
    use_adx:gc('uadx'), use_vol_confirm:gc('uvol'), use_supertrend:gc('ust'), use_stochastic:gc('ustoch'),
    license_key:gv('lickey',''), alpaca:cfg.alpaca||{}, ibkr:cfg.ibkr||{}, tradier:cfg.tradier||{},
    binance:cfg.binance||{}, bybit:cfg.bybit||{}, okx:cfg.okx||{},
  };
}
function initUI(c){
  if(!c) return;
  licValid=false; cfg.alpaca=c.alpaca||{}; cfg.ibkr=c.ibkr||{}; cfg.tradier=c.tradier||{}; cfg.binance=c.binance||{}; cfg.bybit=c.bybit||{}; cfg.okx=c.okx||{}; cfg.broker='Alpaca';
  applyFreeTierUI();
  sv('tickers',c.tickers||'AAPL'); sv('tf',c.timeframe||'1m'); sv('emaf',c.emas?c.emas[0]:9); sv('emas',c.emas?c.emas[1]:50);
  sc('udefqty',c.use_default_qty!==false); toggleDefQty(); sv('qty',c.quantity||1);
  if(c.telegram){ sv('tgt',c.telegram.token||''); sv('tgc',c.telegram.chat_id||''); }
  sv('slp',c.sl_percent||2); sv('tpp',c.tp_percent||4);
  sc('ursi',c.use_rsi!==false); sc('umacd',c.use_macd!==false); sc('uvwap',c.use_vwap!==false); sc('uboll',c.use_bollinger!==false);
  sv('lickey',''); updateCreds();
  let raw=(c.tickers||'AAPL').split(',').map(s=>s.trim()).filter(s=>s);
  if(raw.length){ setTickers(raw); loadChart(cs(raw[0])); }
}
function setTickers(list){ allTickers=list; let bar=$('tkbar'); bar.innerHTML=''; list.forEach(raw=>{ let sym=cs(raw), btn=document.createElement('button'); btn.className='tkbtn'+(sym===curSym?' active':''); btn.textContent=sym; btn.onclick=()=>{ curSym=sym; updTk(); if(lastChart!==sym) loadChart(sym); }; bar.appendChild(btn); }); }
function updTk(){ document.querySelectorAll('.tkbtn').forEach(b=>b.classList.toggle('active',cs(b.textContent)===curSym)); }
function loadChart(sym){
  let s=cs(sym); if(s===lastChart) return; lastChart=s; $('chart-c').innerHTML='';
  if(typeof TradingView==='undefined'){ setTimeout(()=>loadChart(s),150); return; }
  chart=new TradingView.widget({autosize:true,symbol:s,interval:'1',timezone:'Etc/UTC',theme:'Dark',style:'1',locale:'en',toolbar_bg:'#0A0C0F',enable_publishing:false,allow_symbol_change:true,container_id:'chart-c'});
  curSym=s;
}
async function loadConfig(){ try{ let r=await fetch('/api/config'); cfg=await r.json(); initUI(cfg); loadHistory(); }catch(e){ toast('Config load failed','error'); } }
function loadHistory(){ fetch('/api/status').then(r=>r.json()).then(d=>{ renderSignals(d.signals); renderOrders(d.orders); }).catch(()=>{}); }
async function saveConfig(){ cfg=buildCfg(); await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)}); toast('Config saved (license not persisted)','success'); }
const DEF={ broker:'Alpaca', tickers:'AAPL', mode:'signal', direction:'both', use_default_qty:true, quantity:1, emas:[9,50], use_bracket:false, sl_percent:2, tp_percent:4, timeframe:'1m', telegram:{}, use_rsi:true, use_macd:true, use_vwap:true, use_bollinger:true, use_adx:true, use_vol_confirm:true, use_supertrend:true, use_stochastic:true, use_atr_stops:true, alpaca:{api_key:'',secret_key:'',paper:true}, ibkr:{host:'',port:'',client_id:''}, tradier:{access_token:'',account_id:'',sandbox:false}, binance:{api_key:'',api_secret:'',testnet:true}, bybit:{api_key:'',api_secret:'',testnet:true}, okx:{api_key:'',api_secret:'',api_passphrase:'',demo:true} };
function resetDef(){ cfg=JSON.parse(JSON.stringify(DEF)); licValid=false; applyFreeTierUI(); sv('lickey',''); initUI(cfg); saveConfig(); toast('Reset to defaults','success'); }
async function startBot(){
  let btn=$('startBtn'); btn.textContent='Starting...'; btn.disabled=true; cfg=buildCfg();
  if(!licValid){ cfg.broker='Alpaca'; cfg.mode='signal'; cfg.direction='both'; if(cfg.alpaca) cfg.alpaca.paper=true; ['use_supertrend','use_stochastic','use_adx','use_vol_confirm','use_atr_stops','use_bracket'].forEach(k=>cfg[k]=false); let tickers=cfg.tickers.split(','); cfg.tickers=tickers[0].trim(); }
  let r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)}); let d=await r.json();
  btn.textContent='▶ Start Bot'; btn.disabled=false; toast(d.message,d.status==='ok'?'success':'error');
  if(d.status!=='ok'){ $('bstatus').textContent=d.message; $('bstatus').className='err'; }
}
async function stopBot(){ let btn=$('stopBtn'); btn.textContent='Stopping...'; btn.disabled=true; await fetch('/api/stop',{method:'POST'}); btn.textContent='■ Stop Bot'; btn.disabled=false; toast('Bot stopped','success'); }
async function killSwitch(){ await fetch('/api/kill',{method:'POST'}); toast('Kill switch activated','error'); }
async function refreshTickers(){ let r=await fetch('/api/config'), c=await r.json(); sv('tickers',c.tickers); let raw=c.tickers.split(',').map(s=>s.trim()).filter(s=>s); if(raw.length){ setTickers(raw); loadChart(cs(raw[0])); } toast('Tickers refreshed','success'); }
async function validateLicense(){
  let key=gv('lickey').trim(); if(!key){ toast('Enter a license key','error'); return; }
  let r=await fetch('/api/validate_license',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key})}); let d=await r.json();
  if(d.valid){ licValid=true; applyProUI(); toast('Pro unlocked for this session','success'); } else { licValid=false; applyFreeTierUI(); toast(d.message,'error'); }
}
async function checkUpdate(){ try{ let d=await(await fetch('/api/update')).json(); if(d.update_available){ $('upd').style.display='block'; $('udl').href=d.download_url; } else toast('Up to date!','success'); } catch(e){} }
setTimeout(checkUpdate,2500);
async function pollBS(){ try{ let d=await(await fetch('/api/broker_status')).json(); let bs=$('bstatus'); if(d.message){ bs.textContent=d.message; bs.className=d.message.startsWith('Connected')?'ok':'err'; } } catch(e){} }
setInterval(pollBS,2500); pollBS();
function renderSignals(sigs){ let sl=$('siglist'),se=$('sigempty'); sl.innerHTML=''; se.style.display='none'; let has=false; (sigs||[]).forEach(s=>{ has=true; let div=document.createElement('div'); div.className='sitem '+(s.signal==='BUY'?'buy':'sell'); div.innerHTML=`<span>${s.time} <b>${s.signal}</b> ${s.symbol} @ $${s.price}</span><span>${s.rationale||''}</span>`; sl.appendChild(div); }); if(!has) se.style.display='block'; }
function renderOrders(ords){ let hl=$('histlist'),he=$('hstempty'); hl.innerHTML=''; he.style.display='none'; let has=false; (ords||[]).forEach(o=>{ has=true; let div=document.createElement('div'); div.className='sitem '+(o.action==='BUY'?'buy':'sell'); div.innerHTML=`<span>${o.time} <b>${o.action}</b> ${o.qty} ${o.symbol} @ $${o.price}</span>`; hl.appendChild(div); }); if(!has) he.style.display='block'; }
async function pollStatus(){
  try{ let d=await(await fetch('/api/status')).json(); $('v-eq').textContent='$'+fmt(d.equity); $('v-bp').textContent='$'+fmt(d.buying_power); let pct=d.equity?(d.pl/d.equity*100):0; $('v-pl').innerHTML=`<span style="color:${pct>=0?'var(--accent)':'var(--danger)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</span>`; $('v-pos').textContent=d.open_positions; renderSignals(d.signals); renderOrders(d.orders); $('logbar').innerHTML=(d.log||[]).join('<br>'); } catch(e){}
}
setInterval(pollStatus,1500);
async function runBT(){
  let days=parseInt($('btDays').value)||5; toast('Running backtest...','info'); $('btres').innerHTML='<p class="ph">Loading...</p>'; document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active')); document.querySelectorAll('.tbtn').forEach(x=>x.classList.remove('active')); $('tab-backtest').classList.add('active'); document.querySelector('[data-tab="backtest"]').classList.add('active');
  try{ let r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildCfg(),days:days})}); let data=await r.json(); if(data.error){ toast('Backtest error: '+data.error,'error'); $('btres').innerHTML='<p class="ph">Error: '+data.error+'</p>'; return; } let html='',total=0; for(let sym in data.results){ let info=data.results[sym]; html+=`<h4 style="color:var(--accent)">${sym}</h4>`; if(info.error){ html+=`<p style="color:var(--danger)">Error: ${info.error}</p>`; continue; } let sigs=info.signals||[]; total+=sigs.length; if(!sigs.length){ html+='<p class="ph">No signals found.</p>'; continue; } html+=`<table class="bttbl"><tr><th>Time</th><th>Sig</th><th>Price</th><th>RSI</th><th>MACD</th><th>MACDsig</th><th>VWAP</th><th>BB L/U</th><th>ADX</th><th>VolR</th><th>Trend</th><th>%K/%D</th><th>Conf</th></tr>`; sigs.forEach(s=>{ let i=s.indicators; html+=`<tr><td>${s.time.slice(11,19)||s.time.slice(0,19)}</td><td class="${s.signal==='BUY'?'buy':'sell'}">${s.signal}</td><td>$${s.price}</td><td>${i.RSI}</td><td>${i.MACD}</td><td>${i.MACD_signal}</td><td>$${i.VWAP}</td><td>${i.BB_lower}/${i.BB_upper}</td><td>${i.ADX}</td><td>${i.Vol_ratio}x</td><td>${i.Supertrend_trend===1?'Bull':'Bear'}</td><td>${i.Stoch_K}/${i.Stoch_D}</td><td>${(s.confidence*100).toFixed(0)}%</td></tr>`; }); html+='</table>'; } if(total===0) html='<p class="ph">No signals generated.</p>'; $('btres').innerHTML=html; } catch(e){ toast('Backtest failed: '+e,'error'); }
}

// AI Chat with Sessions
let chatInited=false;
async function initAIChat(){ if(chatInited) return; chatInited=true; await loadSessions(); updateChatLimitInfo(); }
async function loadSessions(){
  try{ let r=await fetch('/api/chat/sessions'); let d=await r.json(); renderSessionsList(d.sessions||[]); if(d.sessions && d.sessions.length>0 && !currentSessionId) await loadSession(d.sessions[0].id); else if(d.sessions.length===0) await createNewSession(); } catch(e){ toast('Failed to load sessions','error'); }
}
function renderSessionsList(sessions){
  let list=$('sessions-list'); list.innerHTML='';
  sessions.forEach(s=>{
    let div=document.createElement('div'); div.className='session-item'+(currentSessionId===s.id?' active':'');
    let titleSpan=document.createElement('span'); titleSpan.className='session-title'; titleSpan.textContent=s.title; titleSpan.title=s.title; titleSpan.onclick=()=>loadSession(s.id);
    let actions=document.createElement('div'); actions.className='session-actions';
    let renameBtn=document.createElement('button'); renameBtn.innerHTML='✎'; renameBtn.title='Rename'; renameBtn.onclick=(e)=>{ e.stopPropagation(); renameSession(s.id); };
    let delBtn=document.createElement('button'); delBtn.innerHTML='🗑'; delBtn.title='Delete'; delBtn.onclick=(e)=>{ e.stopPropagation(); deleteSession(s.id); };
    actions.appendChild(renameBtn); actions.appendChild(delBtn);
    div.appendChild(titleSpan); div.appendChild(actions);
    list.appendChild(div);
  });
}
async function loadSession(sid){
  currentSessionId=sid; await loadSessions(); try{ let r=await fetch(`/api/chat/sessions/${sid}/history`); let d=await r.json(); $('chat-messages').innerHTML=''; (d.messages||[]).forEach(m=>addChatMsg(m.content,m.role==='user')); updateChatLimitInfo(); } catch(e){ toast('Failed to load chat history','error'); }
}
async function createNewSession(){
  let r=await fetch('/api/chat/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'New Chat'})}); let d=await r.json(); currentSessionId=d.session_id; await loadSessions(); $('chat-messages').innerHTML=''; updateChatLimitInfo();
}
async function renameSession(sid){
  let newTitle=prompt('Enter new chat name:','Chat'); if(!newTitle) return;
  await fetch(`/api/chat/sessions/${sid}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:newTitle})});
  if(currentSessionId===sid) await loadSession(sid); else await loadSessions();
}
async function deleteSession(sid){
  if(!confirm('Delete this chat?')) return;
  await fetch(`/api/chat/sessions/${sid}`,{method:'DELETE'});
  if(currentSessionId===sid){ currentSessionId=null; $('chat-messages').innerHTML=''; }
  await loadSessions();
}
function updateChatLimitInfo(){
  let el=$('chat-limit'); if(!el) return; el.textContent=licValid?'Pro – unlimited':'Free: 5/day';
}
function addChatMsg(text,isUser){
  let msgs=$('chat-messages'); let wrap=document.createElement('div'); wrap.className='cmsg '+(isUser?'user':'bot');
  let sender=document.createElement('div'); sender.className='msender'; sender.textContent=isUser?'You':'TraderBot';
  let body=document.createElement('div'); body.className='mbody'; body.textContent=text;
  wrap.appendChild(sender); wrap.appendChild(body); msgs.appendChild(wrap); msgs.scrollTop=msgs.scrollHeight; return wrap;
}
async function sendChat(){
  let inputEl=$('chat-input'); let msg=inputEl.value.trim(); if(!msg) return; inputEl.value=''; addChatMsg(msg,true);
  let typing=document.createElement('div'); typing.className='chat-typing'; typing.textContent='TraderBot is thinking...'; $('chat-messages').appendChild(typing); $('chat-messages').scrollTop=$('chat-messages').scrollHeight;
  let sendBtn=$('chat-send'); sendBtn.disabled=true;
  try{ let r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,session_id:currentSessionId})}); let d=await r.json(); typing.remove(); addChatMsg(d.reply||'No response.',false); if(d.session_id && d.session_id!==currentSessionId){ currentSessionId=d.session_id; await loadSessions(); } } catch(e){ typing.remove(); addChatMsg('Connection error. Please try again.',false); }
  sendBtn.disabled=false; $('chat-messages').scrollTop=$('chat-messages').scrollHeight;
}
$('chat-input').addEventListener('keydown',function(e){ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendChat(); } });

// Bootstrap
updateBrokerOptions(); updateCreds(); loadConfig();
</script>
</body>
</html>
"""


# ── Entry point ───────────────────────────────────────────────────────────────
def run_flask():
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)


if __name__ == "__main__":
    acquire_lock()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.2)
    window = webview.create_window(
        "TraderMoney 2.0.4",
        "http://127.0.0.1:5050",
        width=1480,
        height=900,
        min_size=(1000, 720),
    )
    webview.start()

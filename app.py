"""
TraderMoney v1.0.44 – All Brokers Fixed, Full Error Surfacing, SQLite, Encrypted Config.

Required pip packages (install before running):
    pip install flask flask-cors pywebview numpy requests cryptography yfinance
    pip install alpaca-trade-api                  # Alpaca
    pip install ib_insync                          # Interactive Brokers (also needs TWS/IB Gateway running)
    pip install python-binance                     # Binance
    pip install pybit                              # Bybit
    pip install okx                                # OKX

Optional:
    pip install python-telegram-bot               # Telegram notifications
"""

import json, os, queue, signal, sys, socket, sqlite3, threading, time, traceback, atexit, urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import requests as http_requests
import webview
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

APP_VERSION = "1.0.44"

# ─────────────────────────────────────────────────────────────────────────────
# GUMROAD LICENSE
# ─────────────────────────────────────────────────────────────────────────────
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
            return False, "License has been revoked (refunded/chargebacked)"
        return True, "License verified"
    except Exception as e:
        return False, f"Cannot reach license server – {e}"

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP + PORT LOCK
# ─────────────────────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE (SQLite WAL)
# ─────────────────────────────────────────────────────────────────────────────
DB_PATH = os.path.expanduser("~/.tradermoney_data.db")

class DatabaseManager:
    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._init_tables()

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
        """)
        self.conn.commit()

    def _exec(self, sql, params=()):
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def insert_trade(self, time_str, symbol, action, qty, price):
        self._exec("INSERT INTO trades (timestamp,symbol,action,quantity,price) VALUES (?,?,?,?,?)",
                   (time_str, symbol, action, qty, price))

    def get_recent_trades(self, limit=50):
        with self._lock:
            cur = self.conn.execute(
                "SELECT timestamp,symbol,action,quantity,price FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [{"time": r[0], "symbol": r[1], "action": r[2], "qty": r[3], "price": r[4]}
                    for r in cur.fetchall()]

    def insert_signal(self, time_str, symbol, sig, price, rationale):
        self._exec("INSERT INTO signals (timestamp,symbol,signal,price,rationale) VALUES (?,?,?,?,?)",
                   (time_str, symbol, sig, price, rationale))

    def get_recent_signals(self, limit=50):
        with self._lock:
            cur = self.conn.execute(
                "SELECT timestamp,symbol,signal,price,rationale FROM signals ORDER BY id DESC LIMIT ?", (limit,))
            return [{"time": r[0], "symbol": r[1], "signal": r[2], "price": r[3], "rationale": r[4]}
                    for r in cur.fetchall()]

    def insert_log(self, message):
        self._exec("INSERT INTO logs (timestamp,message) VALUES (?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))

    def get_recent_logs(self, limit=50):
        with self._lock:
            cur = self.conn.execute(
                "SELECT timestamp,message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
            return [f"{r[0]}  {r[1]}" for r in cur.fetchall()]

    def insert_backtest(self, config_json):
        self._exec("INSERT INTO backtests (timestamp,config_json) VALUES (?,?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_json))

db = DatabaseManager()

# ─────────────────────────────────────────────────────────────────────────────
# ENCRYPTED CONFIG (Fernet safe-write)
# ─────────────────────────────────────────────────────────────────────────────
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
    def load():
        try:
            cipher = _get_fernet()
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "rb") as f:
                    return json.loads(cipher.decrypt(f.read()).decode())
        except Exception:
            pass
        return {}

    @staticmethod
    def save(config):
        try:
            cipher = _get_fernet()
            plain = json.dumps(config, indent=2).encode()
            tmp = CONFIG_FILE + ".tmp"
            encrypted = cipher.encrypt(plain)
            with open(tmp, "wb") as f:
                f.write(encrypted)
            # verify integrity before replacing
            with open(tmp, "rb") as f:
                cipher.decrypt(f.read())
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            db.insert_log(f"Config save error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────────────────────────────────────
ATR_STOP_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER   = 3.0

class AppState:
    def __init__(self):
        self.config = EncryptedConfigManager.load() or {
            "broker": "Alpaca", "tickers": "AAPL", "mode": "signal", "quantity": 1,
            "emas": [9, 50], "use_bracket": False, "sl_percent": 2.0, "tp_percent": 4.0,
            "timeframe": "1m", "telegram": {},
            "use_rsi": True, "use_macd": True, "use_vwap": True, "use_bollinger": True,
            "use_adx": True, "use_vol_confirm": True,
            "use_supertrend": True, "use_stochastic": True, "use_atr_stops": True,
            "license_key": "", "license_valid": False, "last_broker_message": ""
        }
        self.ui_queue        = queue.Queue()
        self.engine          = None
        self.broker_instance = None
        self.running         = False
        self.dashboard       = {
            "equity": 0, "pl": 0, "buying_power": 0, "open_positions": 0,
            "signals": [], "orders": [], "log": [], "ema_values": {}
        }

state = AppState()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def clean_symbol(raw: str) -> str:
    """Strip any :qty suffix and normalise to uppercase."""
    return raw.split(":")[0].strip().upper()

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ─────────────────────────────────────────────────────────────────────────────
# BASE BROKER
# ─────────────────────────────────────────────────────────────────────────────
BROKER_REGISTRY: Dict[str, Any] = {}

def register_broker(name, cls):
    BROKER_REGISTRY[name] = cls

class BaseBroker:
    """All broker adapters must extend this class."""
    name = "Base"

    def __init__(self, config: dict, ui_queue: queue.Queue):
        self.config     = config
        self.ui_queue   = ui_queue
        self.last_error = ""

    def _emit_error(self, msg: str):
        self.last_error = msg
        self.ui_queue.put(("error", msg))
        db.insert_log(f"❌ [{self.name}] {msg}")

    def _emit_log(self, msg: str):
        self.ui_queue.put(("log", msg))
        db.insert_log(f"[{self.name}] {msg}")

    # ── abstract interface ──────────────────────────────────────────────────
    def connect(self) -> bool:                                          raise NotImplementedError
    def get_account(self) -> Optional[Dict[str, float]]:               raise NotImplementedError
    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None,
                     sl_price=None, tp_price=None) -> bool:            raise NotImplementedError
    def close_all_positions(self):                                      raise NotImplementedError
    def get_positions(self) -> Dict[str, int]:                         raise NotImplementedError
    def get_market_status(self) -> bool:                               raise NotImplementedError
    def stream_prices(self, symbols: List[str], callback):             raise NotImplementedError
    def stop_stream(self):                                             raise NotImplementedError

# ─────────────────────────────────────────────────────────────────────────────
# ALPACA
# ─────────────────────────────────────────────────────────────────────────────
class AlpacaBroker(BaseBroker):
    name = "Alpaca"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.api          = None
        self._stop_stream = False

    def connect(self) -> bool:
        creds  = self.config.get("alpaca", {})
        key    = creds.get("api_key",    "").strip()
        secret = creds.get("secret_key", "").strip()
        paper  = creds.get("paper", True)

        if not key:
            self._emit_error("Alpaca API key is missing – enter it in the sidebar.")
            return False
        if not secret:
            self._emit_error("Alpaca Secret key is missing – enter it in the sidebar.")
            return False

        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        try:
            import alpaca_trade_api as tradeapi
            self.api = tradeapi.REST(key, secret, base_url, api_version="v2")
            acc = self.api.get_account()
            if acc.status != "ACTIVE":
                self._emit_error(f"Alpaca account status is '{acc.status}' – not ACTIVE.")
                return False
            self._emit_log(f"Connected (paper={paper})")
            return True
        except ImportError:
            self._emit_error("alpaca-trade-api not installed. Run: pip install alpaca-trade-api")
            return False
        except Exception as e:
            msg = str(e)
            if "403" in msg or "unauthorized" in msg.lower():
                self._emit_error(f"Alpaca auth failed – check API key/secret. Paper={paper}. Detail: {msg}")
            else:
                self._emit_error(f"Alpaca connection error: {msg}")
            return False

    def get_account(self):
        if not self.api:
            return None
        try:
            acc = self.api.get_account()
            positions = self.api.list_positions()
            return {
                "equity":        float(acc.equity),
                "pl":            float(acc.equity) - float(acc.last_equity),
                "buying_power":  float(acc.buying_power),
                "cash":          float(acc.cash),
                "open_positions": len(positions),
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
                trade = self.api.get_latest_trade(symbol)
                price = float(trade.price)
                stop  = round(sl_price, 2) if sl_price is not None else \
                        round(price * (1 - sl_pct / 100) if side == "buy" else
                              price * (1 + sl_pct / 100), 2)
                limit = round(tp_price, 2) if tp_price is not None else \
                        round(price * (1 + tp_pct / 100) if side == "buy" else
                              price * (1 - tp_pct / 100), 2)
                self.api.submit_order(
                    symbol=symbol, qty=qty, side=side,
                    type="market", time_in_force="gtc",
                    order_class="bracket",
                    stop_loss={"stop_price": stop},
                    take_profit={"limit_price": limit},
                )
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
            return {p.symbol: int(p.qty) for p in self.api.list_positions()}
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
                import alpaca_trade_api as tradeapi
                creds  = self.config.get("alpaca", {})
                key    = creds.get("api_key")
                secret = creds.get("secret_key")
                paper  = creds.get("paper", True)
                ws = ("wss://paper-api.alpaca.markets/stream" if paper
                      else "wss://api.alpaca.markets/stream")
                stream = tradeapi.Stream(key, secret, base_url=ws, data_feed="iex")

                async def on_trade(t):
                    if t.symbol in symbols:
                        callback(t.symbol, t.price)

                stream.subscribe_trades(on_trade, *symbols)
                while not self._stop_stream:
                    try:
                        stream.run()
                    except Exception as e:
                        self._emit_log(f"Stream error, retrying: {e}")
                        time.sleep(5)
            except Exception as e:
                self._emit_error(f"Stream thread crashed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("Alpaca", AlpacaBroker)

# ─────────────────────────────────────────────────────────────────────────────
# INTERACTIVE BROKERS (ib_insync)
# ─────────────────────────────────────────────────────────────────────────────
class IBKRBroker(BaseBroker):
    name = "Interactive Brokers"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.ib           = None
        self._stop_stream = False

    def connect(self) -> bool:
        creds = self.config.get("ibkr", {})
        host  = creds.get("host", "").strip()
        port  = creds.get("port", "7497")
        cid   = creds.get("client_id", "1")

        if not host:
            self._emit_error("IBKR host is missing – enter the TWS/Gateway IP (usually 127.0.0.1).")
            return False
        try:
            port = int(port)
            cid  = int(cid)
        except ValueError:
            self._emit_error("IBKR port and client_id must be integers.")
            return False

        try:
            from ib_insync import IB, util
            util.startLoop()   # required for non-async environments
            self.ib = IB()
            self.ib.connect(host, port, clientId=cid, timeout=10)
            if not self.ib.isConnected():
                self._emit_error(
                    f"IBKR connected but isConnected()=False. "
                    f"Check TWS/Gateway is running on {host}:{port}.")
                return False
            self._emit_log(f"Connected to IBKR on {host}:{port} (clientId={cid})")
            return True
        except ImportError:
            self._emit_error("ib_insync not installed. Run: pip install ib_insync")
            return False
        except ConnectionRefusedError:
            self._emit_error(
                f"IBKR connection refused at {host}:{port}. "
                f"Is TWS or IB Gateway running? Is API enabled in TWS settings?")
            return False
        except Exception as e:
            self._emit_error(
                f"IBKR connection error: {e}  "
                f"Make sure TWS/Gateway is running, API connections are enabled, "
                f"and the correct port is used (7497=TWS paper, 7496=TWS live, "
                f"4002=Gateway paper, 4001=Gateway live).")
            return False

    def get_account(self):
        if not self.ib or not self.ib.isConnected():
            return None
        try:
            summary = self.ib.accountSummary()
            eq  = next((float(v.value) for v in summary if v.tag == "NetLiquidation"), 0.0)
            pl  = next((float(v.value) for v in summary if v.tag == "UnrealizedPnL"), 0.0)
            bp  = next((float(v.value) for v in summary if v.tag == "AvailableFunds"), 0.0)
            pos = len(self.ib.positions())
            return {"equity": eq, "pl": pl, "buying_power": bp, "cash": 0.0,
                    "open_positions": pos}
        except Exception as e:
            self._emit_error(f"IBKR get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.ib or not self.ib.isConnected():
            self._emit_error("IBKR not connected – cannot submit order.")
            return False
        try:
            from ib_insync import Stock, MarketOrder, BracketOrder
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            if (sl_price or sl_pct) and (tp_price or tp_pct):
                # Build bracket order
                price = self.ib.reqMktData(contract, "", False, False).last or 0
                self.ib.cancelMktData(contract)
                stop  = sl_price if sl_price else price * (1 - sl_pct / 100)
                limit = tp_price if tp_price else price * (1 + tp_pct / 100)
                bracket = self.ib.bracketOrder(
                    "BUY" if side == "buy" else "SELL", qty,
                    round(price * 1.001, 2),
                    round(limit, 2),
                    round(stop, 2)
                )
                for o in bracket:
                    self.ib.placeOrder(contract, o)
            else:
                order = MarketOrder("BUY" if side == "buy" else "SELL", qty)
                self.ib.placeOrder(contract, order)
            return True
        except Exception as e:
            self._emit_error(f"IBKR order error: {e}")
            return False

    def close_all_positions(self):
        if not self.ib or not self.ib.isConnected():
            return
        try:
            from ib_insync import MarketOrder
            for pos in self.ib.positions():
                if pos.position == 0:
                    continue
                direction = "SELL" if pos.position > 0 else "BUY"
                self.ib.placeOrder(pos.contract, MarketOrder(direction, abs(pos.position)))
            self._emit_log("Kill switch: all IBKR positions closed.")
        except Exception as e:
            self._emit_error(f"IBKR kill switch: {e}")

    def get_positions(self):
        if not self.ib or not self.ib.isConnected():
            return {}
        try:
            return {pos.contract.symbol: int(pos.position) for pos in self.ib.positions()
                    if pos.position != 0}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        # IBKR doesn't expose a simple clock; assume open unless we know otherwise
        return True

    def stream_prices(self, symbols, callback):
        if not self.ib or not self.ib.isConnected():
            return
        try:
            from ib_insync import Stock
            self._stop_stream = False
            contracts = [Stock(s, "SMART", "USD") for s in symbols]
            for c in contracts:
                self.ib.qualifyContracts(c)
            tickers_map = {c.symbol: self.ib.reqMktData(c, "", False, False) for c in contracts}

            def run():
                while not self._stop_stream:
                    self.ib.sleep(1)
                    for sym, ticker in tickers_map.items():
                        if ticker.last and ticker.last > 0:
                            callback(sym, ticker.last)
            threading.Thread(target=run, daemon=True).start()
        except Exception as e:
            self._emit_error(f"IBKR stream error: {e}")

    def stop_stream(self):
        self._stop_stream = True

register_broker("Interactive Brokers", IBKRBroker)

# ─────────────────────────────────────────────────────────────────────────────
# TRADIER
# ─────────────────────────────────────────────────────────────────────────────
class TradierBroker(BaseBroker):
    name = "Tradier"

    BASE_URL      = "https://api.tradier.com/v1"
    SANDBOX_URL   = "https://sandbox.tradier.com/v1"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session    = None
        self.token      = None
        self.account_id = None
        self._base      = self.BASE_URL
        self._stop_stream = False

    def connect(self) -> bool:
        creds = self.config.get("tradier", {})
        self.token      = creds.get("access_token", "").strip()
        self.account_id = creds.get("account_id",   "").strip()
        sandbox         = creds.get("sandbox", False)

        if not self.token:
            self._emit_error("Tradier Access Token is missing – enter it in the sidebar.")
            return False
        if not self.account_id:
            self._emit_error("Tradier Account ID is missing – enter it in the sidebar.")
            return False

        self._base = self.SANDBOX_URL if sandbox else self.BASE_URL
        try:
            import requests as req
            self.session = req.Session()
            self.session.headers.update({
                "Authorization": f"Bearer {self.token}",
                "Accept":        "application/json",
            })
            r = self.session.get(
                f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            if r.status_code == 401:
                self._emit_error(
                    "Tradier auth failed (401) – check your access token. "
                    "Sandbox tokens won't work on production and vice-versa.")
                return False
            if r.status_code == 404:
                self._emit_error(
                    f"Tradier account '{self.account_id}' not found (404) – verify your Account ID.")
                return False
            if r.status_code != 200:
                self._emit_error(
                    f"Tradier connection returned HTTP {r.status_code}: {r.text[:200]}")
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
            r = self.session.get(
                f"{self._base}/accounts/{self.account_id}/balances", timeout=10)
            r.raise_for_status()
            bal = r.json().get("balances", {})
            equity = float(bal.get("total_equity", 0))
            bp     = float(bal.get("equity_buying_power", 0))
            return {"equity": equity, "pl": 0.0, "buying_power": bp,
                    "cash": float(bal.get("cash", {}).get("cash_available", 0)),
                    "open_positions": 0}
        except Exception as e:
            self._emit_error(f"Tradier get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session:
            self._emit_error("Tradier not connected.")
            return False
        try:
            payload = {
                "class":    "equity",
                "symbol":   symbol,
                "side":     side,          # "buy" or "sell"
                "quantity": str(qty),
                "type":     "market",
                "duration": "day",
            }
            r = self.session.post(
                f"{self._base}/accounts/{self.account_id}/orders",
                data=payload, timeout=10)
            data = r.json()
            if r.status_code not in (200, 201) or data.get("order", {}).get("status") == "error":
                errors = data.get("errors", {}).get("error", str(data))
                self._emit_error(f"Tradier order rejected: {errors}")
                return False
            return True
        except Exception as e:
            self._emit_error(f"Tradier submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self.session:
            return
        try:
            positions = self.get_positions()
            for sym, qty in positions.items():
                if qty > 0:
                    self.submit_order(sym, qty, "sell")
                elif qty < 0:
                    self.submit_order(sym, abs(qty), "buy")
            self._emit_log("Tradier: all positions closed.")
        except Exception as e:
            self._emit_error(f"Tradier kill switch: {e}")

    def get_positions(self):
        if not self.session:
            return {}
        try:
            r = self.session.get(
                f"{self._base}/accounts/{self.account_id}/positions", timeout=10)
            r.raise_for_status()
            data = r.json()
            raw  = data.get("positions", {}).get("position", [])
            if isinstance(raw, dict):   # single position comes as dict
                raw = [raw]
            return {p["symbol"]: int(float(p["quantity"])) for p in raw if p}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        try:
            r = self.session.get(f"{self._base}/markets/clock", timeout=5)
            data = r.json()
            return data.get("clock", {}).get("state", "") == "open"
        except Exception:
            return True

    def stream_prices(self, symbols, callback):
        # Tradier streaming requires a websocket session token (advanced setup).
        # We fall back to polling via REST every 5 s.
        self._stop_stream = False

        def poll():
            syms_joined = ",".join(symbols)
            while not self._stop_stream:
                try:
                    r = self.session.get(
                        f"{self._base}/markets/quotes",
                        params={"symbols": syms_joined}, timeout=5)
                    quotes = r.json().get("quotes", {}).get("quote", [])
                    if isinstance(quotes, dict):
                        quotes = [quotes]
                    for q in quotes:
                        sym   = q.get("symbol", "")
                        price = q.get("last", 0.0)
                        if sym and price:
                            callback(sym, float(price))
                except Exception:
                    pass
                time.sleep(5)

        threading.Thread(target=poll, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("Tradier", TradierBroker)

# ─────────────────────────────────────────────────────────────────────────────
# BINANCE
# ─────────────────────────────────────────────────────────────────────────────
class BinanceBroker(BaseBroker):
    name = "Binance"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.client       = None
        self._stop_stream = False

    def connect(self) -> bool:
        creds      = self.config.get("binance", {})
        api_key    = creds.get("api_key",    "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet    = creds.get("testnet", True)

        if not api_key:
            self._emit_error("Binance API Key is missing – enter it in the sidebar.")
            return False
        if not api_secret:
            self._emit_error("Binance API Secret is missing – enter it in the sidebar.")
            return False
        try:
            from binance.client import Client
            from binance.exceptions import BinanceAPIException
            self.client = Client(api_key, api_secret, testnet=testnet)
            # force a live call to verify credentials
            self.client.get_account()
            self._emit_log(f"Connected (testnet={testnet})")
            return True
        except ImportError:
            self._emit_error("python-binance not installed. Run: pip install python-binance")
            return False
        except Exception as e:
            msg = str(e)
            if "APIError(code=-2014)" in msg or "APIError(code=-2015)" in msg or \
               "Invalid API-key" in msg.lower() or "signature" in msg.lower():
                self._emit_error(
                    f"Binance auth failed – invalid API key or secret. "
                    f"Testnet={testnet}. Detail: {msg}")
            else:
                self._emit_error(f"Binance connection error: {msg}")
            return False

    def get_account(self):
        if not self.client:
            return None
        try:
            acc      = self.client.get_account()
            balances = {b["asset"]: float(b["free"]) + float(b["locked"])
                        for b in acc["balances"]}
            # Approximate equity as USDT + BTC value (simplified)
            usdt  = balances.get("USDT", 0)
            btc   = balances.get("BTC", 0)
            try:
                btc_price = float(self.client.get_symbol_ticker(symbol="BTCUSDT")["price"])
            except Exception:
                btc_price = 0
            equity = usdt + btc * btc_price
            return {"equity": equity, "pl": 0.0, "buying_power": usdt,
                    "cash": usdt, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"Binance get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.client:
            self._emit_error("Binance not connected.")
            return False
        try:
            # Normalise symbol: AAPL → not valid; BTC/USD or BTC → BTCUSDT
            sym = symbol.replace("/", "").replace("-", "").upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            if side == "buy":
                self.client.order_market_buy(symbol=sym, quantity=qty)
            else:
                self.client.order_market_sell(symbol=sym, quantity=qty)
            return True
        except Exception as e:
            self._emit_error(f"Binance order ({symbol} {side}): {e}")
            return False

    def close_all_positions(self):
        # Binance spot has no "positions" in the traditional sense; 
        # sell all non-USDT/BNB balances.
        if not self.client:
            return
        try:
            acc = self.client.get_account()
            for b in acc["balances"]:
                asset = b["asset"]
                free  = float(b["free"])
                if asset in ("USDT", "BNB") or free <= 0:
                    continue
                sym = asset + "USDT"
                try:
                    self.client.order_market_sell(symbol=sym, quantity=free)
                except Exception:
                    pass
            self._emit_log("Binance: all positions closed.")
        except Exception as e:
            self._emit_error(f"Binance kill switch: {e}")

    def get_positions(self):
        if not self.client:
            return {}
        try:
            acc = self.client.get_account()
            return {b["asset"]: float(b["free"]) for b in acc["balances"]
                    if float(b["free"]) > 0 and b["asset"] != "USDT"}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        return True  # Crypto is 24/7

    def stream_prices(self, symbols, callback):
        self._stop_stream = False

        def run():
            try:
                from binance import ThreadedWebsocketManager
                twm = ThreadedWebsocketManager(
                    api_key    = self.config["binance"]["api_key"],
                    api_secret = self.config["binance"]["api_secret"],
                )
                twm.start()
                sym_set = set(symbols)

                def handle_msg(msg):
                    if msg.get("e") == "trade":
                        raw_sym = msg["s"]  # e.g. BTCUSDT
                        price   = float(msg["p"])
                        # match against user symbols
                        for s in sym_set:
                            normalised = s.replace("/", "").replace("-", "").upper()
                            if not normalised.endswith("USDT"):
                                normalised += "USDT"
                            if raw_sym == normalised:
                                callback(s, price)

                keys = []
                for s in symbols:
                    sym = s.replace("/", "").replace("-", "").upper()
                    if not sym.endswith("USDT"):
                        sym += "USDT"
                    key = twm.start_trade_socket(callback=handle_msg, symbol=sym)
                    keys.append(key)

                while not self._stop_stream:
                    time.sleep(1)
                twm.stop()
            except Exception as e:
                self._emit_error(f"Binance stream error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("Binance", BinanceBroker)

# ─────────────────────────────────────────────────────────────────────────────
# BYBIT  (pybit ≥ 5.x unified trading API)
# ─────────────────────────────────────────────────────────────────────────────
class BybitBroker(BaseBroker):
    name = "Bybit"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self.session      = None
        self._stop_stream = False

    def connect(self) -> bool:
        creds      = self.config.get("bybit", {})
        api_key    = creds.get("api_key",    "").strip()
        api_secret = creds.get("api_secret", "").strip()
        testnet    = creds.get("testnet", True)

        if not api_key:
            self._emit_error("Bybit API Key is missing – enter it in the sidebar.")
            return False
        if not api_secret:
            self._emit_error("Bybit API Secret is missing – enter it in the sidebar.")
            return False
        try:
            from pybit.unified_trading import HTTP
            self.session = HTTP(
                api_key    = api_key,
                api_secret = api_secret,
                testnet    = testnet,
            )
            # Test call
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            if resp.get("retCode", -1) != 0:
                msg = resp.get("retMsg", "Unknown error")
                self._emit_error(
                    f"Bybit auth failed: {msg}. "
                    f"Testnet={testnet}. Make sure your key has read permissions.")
                return False
            self._emit_log(f"Connected (testnet={testnet})")
            return True
        except ImportError:
            self._emit_error(
                "pybit not installed or wrong version. "
                "Run: pip install pybit  (needs v5.x)")
            return False
        except Exception as e:
            self._emit_error(f"Bybit connection error: {e}")
            return False

    def get_account(self):
        if not self.session:
            return None
        try:
            resp    = self.session.get_wallet_balance(accountType="UNIFIED")
            result  = resp.get("result", {}).get("list", [{}])[0]
            equity  = float(result.get("totalEquity", 0))
            balance = float(result.get("totalAvailableBalance", 0))
            return {"equity": equity, "pl": 0.0, "buying_power": balance,
                    "cash": balance, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"Bybit get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self.session:
            self._emit_error("Bybit not connected.")
            return False
        try:
            sym = symbol.replace("/", "").replace("-", "").upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            kwargs = dict(
                category  = "spot",
                symbol    = sym,
                side      = "Buy" if side == "buy" else "Sell",
                orderType = "Market",
                qty       = str(qty),
            )
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
        # Bybit spot: sell all non-USDT coins
        if not self.session:
            return
        try:
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            coins = resp.get("result", {}).get("list", [{}])[0].get("coin", [])
            for c in coins:
                asset = c["coin"]
                free  = float(c.get("availableToWithdraw", 0))
                if asset == "USDT" or free <= 0:
                    continue
                sym = asset + "USDT"
                self.session.place_order(
                    category="spot", symbol=sym,
                    side="Sell", orderType="Market", qty=str(free))
            self._emit_log("Bybit: all positions closed.")
        except Exception as e:
            self._emit_error(f"Bybit kill switch: {e}")

    def get_positions(self):
        if not self.session:
            return {}
        try:
            resp  = self.session.get_wallet_balance(accountType="UNIFIED")
            coins = resp.get("result", {}).get("list", [{}])[0].get("coin", [])
            return {c["coin"]: float(c.get("equity", 0))
                    for c in coins if float(c.get("equity", 0)) > 0 and c["coin"] != "USDT"}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        return True  # Crypto 24/7

    def stream_prices(self, symbols, callback):
        self._stop_stream = False

        def run():
            try:
                from pybit.unified_trading import WebSocket
                sym_map = {}
                for s in symbols:
                    sym = s.replace("/", "").replace("-", "").upper()
                    if not sym.endswith("USDT"):
                        sym += "USDT"
                    sym_map[sym] = s

                def handle(msg):
                    data = msg.get("data", {})
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    sym  = msg.get("topic", "").split(".")[-1]
                    orig = sym_map.get(sym)
                    if orig and "lastPrice" in data:
                        callback(orig, float(data["lastPrice"]))

                ws = WebSocket(testnet=self.config.get("bybit", {}).get("testnet", True),
                               channel_type="spot")
                for sym in sym_map:
                    ws.ticker_stream(symbol=sym, callback=handle)

                while not self._stop_stream:
                    time.sleep(1)
            except Exception as e:
                self._emit_error(f"Bybit stream error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("Bybit", BybitBroker)

# ─────────────────────────────────────────────────────────────────────────────
# OKX
# ─────────────────────────────────────────────────────────────────────────────
class OKXBroker(BaseBroker):
    name = "OKX"

    def __init__(self, config, ui_queue):
        super().__init__(config, ui_queue)
        self._account_api = None
        self._trade_api   = None
        self._stop_stream = False

    def connect(self) -> bool:
        creds      = self.config.get("okx", {})
        api_key    = creds.get("api_key",        "").strip()
        api_secret = creds.get("api_secret",     "").strip()
        passphrase = creds.get("api_passphrase", "").strip()
        demo       = creds.get("demo", True)
        flag       = "1" if demo else "0"

        if not api_key:
            self._emit_error("OKX API Key is missing – enter it in the sidebar.")
            return False
        if not api_secret:
            self._emit_error("OKX API Secret is missing – enter it in the sidebar.")
            return False
        if not passphrase:
            self._emit_error("OKX API Passphrase is missing – enter it in the sidebar.")
            return False
        try:
            import okx.Account as AccountAPI
            import okx.Trade   as TradeAPI
            self._account_api = AccountAPI.AccountAPI(
                api_key, api_secret, passphrase, False, flag)
            self._trade_api   = TradeAPI.TradeAPI(
                api_key, api_secret, passphrase, False, flag)
            # Verify credentials with a live call
            resp = self._account_api.get_account_balance()
            code = resp.get("code", "-1")
            if code != "0":
                msg = resp.get("msg", "Unknown error")
                self._emit_error(
                    f"OKX auth failed (code {code}): {msg}. "
                    f"Demo={demo}. Check key, secret, and passphrase.")
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
            resp    = self._account_api.get_account_balance()
            details = resp.get("data", [{}])[0].get("details", [])
            equity  = sum(float(d.get("eq", 0)) for d in details)
            usdt    = next((float(d.get("availBal", 0))
                            for d in details if d.get("ccy") == "USDT"), 0.0)
            return {"equity": equity, "pl": 0.0, "buying_power": usdt,
                    "cash": usdt, "open_positions": 0}
        except Exception as e:
            self._emit_error(f"OKX get_account: {e}")
            return None

    def submit_order(self, symbol, qty, side, order_type="market",
                     sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool:
        if not self._trade_api:
            self._emit_error("OKX not connected.")
            return False
        try:
            # OKX inst-id format: BTC-USDT  (not BTCUSDT)
            sym = symbol.replace("/", "-").replace("_", "-").upper()
            if "-" not in sym:
                sym = sym + "-USDT"
            resp = self._trade_api.place_order(
                instId  = sym,
                tdMode  = "cash",
                side    = side,         # "buy" or "sell"
                ordType = "market",
                sz      = str(int(qty)),
            )
            code = resp.get("data", [{}])[0].get("sCode", "-1")
            if code != "0":
                msg = resp.get("data", [{}])[0].get("sMsg", str(resp))
                self._emit_error(f"OKX order rejected: {msg}")
                return False
            return True
        except Exception as e:
            self._emit_error(f"OKX submit_order: {e}")
            return False

    def close_all_positions(self):
        if not self._account_api:
            return
        try:
            resp    = self._account_api.get_account_balance()
            details = resp.get("data", [{}])[0].get("details", [])
            for d in details:
                ccy  = d.get("ccy")
                avail = float(d.get("availBal", 0))
                if ccy == "USDT" or avail <= 0:
                    continue
                sym = ccy + "-USDT"
                self._trade_api.place_order(
                    instId="spot", tdMode="cash",
                    side="sell", ordType="market", sz=str(avail))
            self._emit_log("OKX: all positions closed.")
        except Exception as e:
            self._emit_error(f"OKX kill switch: {e}")

    def get_positions(self):
        if not self._account_api:
            return {}
        try:
            resp    = self._account_api.get_account_balance()
            details = resp.get("data", [{}])[0].get("details", [])
            return {d["ccy"]: float(d.get("eq", 0))
                    for d in details
                    if float(d.get("eq", 0)) > 0 and d["ccy"] != "USDT"}
        except Exception:
            return {}

    def get_market_status(self) -> bool:
        return True  # Crypto 24/7

    def stream_prices(self, symbols, callback):
        self._stop_stream = False

        def run():
            try:
                import websocket, json as _json
                sym_map = {}
                for s in symbols:
                    inst = s.replace("/", "-").replace("_", "-").upper()
                    if "-" not in inst:
                        inst = inst + "-USDT"
                    sym_map[inst] = s

                subs = [{"channel": "tickers", "instId": k} for k in sym_map]
                url  = ("wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
                        if self.config.get("okx", {}).get("demo", True)
                        else "wss://ws.okx.com:8443/ws/v5/public")

                def on_message(ws_app, msg):
                    try:
                        data = _json.loads(msg)
                        if "data" in data:
                            for item in data["data"]:
                                inst  = item.get("instId", "")
                                price = float(item.get("last", 0))
                                orig  = sym_map.get(inst)
                                if orig and price:
                                    callback(orig, price)
                    except Exception:
                        pass

                def on_open(ws_app):
                    ws_app.send(_json.dumps({"op": "subscribe", "args": subs}))

                ws = websocket.WebSocketApp(url, on_message=on_message, on_open=on_open)
                while not self._stop_stream:
                    ws.run_forever()
                    if not self._stop_stream:
                        time.sleep(3)
            except ImportError:
                self._emit_error("websocket-client not installed. Run: pip install websocket-client")
            except Exception as e:
                self._emit_error(f"OKX stream error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_stream(self):
        self._stop_stream = True

register_broker("OKX", OKXBroker)

# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────
class IndicatorCalculator:
    @staticmethod
    def compute_all(df, ema_fast=9, ema_slow=50):
        import pandas as pd

        close  = np.asarray(df["Close"]).astype(np.float64).ravel()
        high   = np.asarray(df["High"]).astype(np.float64).ravel()
        low    = np.asarray(df["Low"]).astype(np.float64).ravel()
        volume = (np.asarray(df["Volume"]).astype(np.float64).ravel()
                  if "Volume" in df.columns else np.ones_like(close))

        def ema(data, span):
            alpha = 2 / (span + 1)
            res   = np.zeros_like(data)
            res[0] = data[0]
            for i in range(1, len(data)):
                res[i] = alpha * data[i] + (1 - alpha) * res[i - 1]
            return res

        # EMA
        df["EMA_fast"] = ema(close, ema_fast)
        df["EMA_slow"] = ema(close, ema_slow)

        # RSI
        delta   = np.diff(close, prepend=close[0])
        gain    = np.where(delta > 0, delta, 0.0)
        loss    = np.where(delta < 0, -delta, 0.0)
        ag      = np.convolve(gain, np.ones(14) / 14, mode="full")[:len(close)]
        al      = np.convolve(loss, np.ones(14) / 14, mode="full")[:len(close)]
        rs      = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
        df["RSI"] = 100 - (100 / (1 + rs))

        # MACD
        ema12 = ema(close, 12)
        ema26 = ema(close, 26)
        macd  = ema12 - ema26
        df["MACD"]        = macd
        df["MACD_signal"] = ema(macd, 9)

        # Bollinger Bands
        ma20  = np.convolve(close, np.ones(20) / 20, mode="same")
        std20 = np.array([np.std(close[max(0, i - 19):i + 1]) for i in range(len(close))])
        df["BB_upper"] = ma20 + 2 * std20
        df["BB_lower"] = ma20 - 2 * std20

        # VWAP
        cum_vol = np.cumsum(volume)
        cum_pv  = np.cumsum(close * volume)
        df["VWAP"] = np.divide(cum_pv, cum_vol, out=np.zeros_like(cum_pv), where=cum_vol != 0)

        # ATR
        tr     = np.maximum(high[1:] - low[1:],
                 np.maximum(np.abs(high[1:] - close[:-1]),
                            np.abs(low[1:]  - close[:-1])))
        tr     = np.insert(tr, 0, np.mean(tr[:14]) if len(tr) >= 14 else (tr[0] if len(tr) else 0))
        atr14  = ema(tr, 14)
        df["ATR"] = atr14

        # ADX
        up       = np.maximum(np.diff(high,  prepend=high[0]),  0.0)
        dn       = np.maximum(np.diff(low[::-1], prepend=low[0])[::-1], 0.0)
        plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
        plus_di  = 100 * ema(plus_dm, 14)  / (atr14 + 1e-14)
        minus_di = 100 * ema(minus_dm, 14) / (atr14 + 1e-14)
        dx       = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-14)
        df["ADX"] = ema(dx, 14)

        # Volume ratio
        vol_avg20 = np.convolve(volume, np.ones(20) / 20, mode="same")
        df["Vol_ratio"] = np.divide(volume, vol_avg20,
                                    out=np.ones_like(volume), where=vol_avg20 != 0)

        # SuperTrend (ATR period=10, factor=3)
        atr_st = ema(tr, 10)
        hl2    = (high + low) / 2.0
        upper  = hl2 + 3.0 * atr_st
        lower  = hl2 - 3.0 * atr_st
        supertrend = np.zeros_like(close)
        trend       = np.ones_like(close)
        for i in range(1, len(close)):
            if close[i] > upper[i - 1]:
                trend[i] = 1
            elif close[i] < lower[i - 1]:
                trend[i] = -1
            else:
                trend[i] = trend[i - 1]
                if trend[i] == 1  and lower[i] < lower[i - 1]: lower[i] = lower[i - 1]
                if trend[i] == -1 and upper[i] > upper[i - 1]: upper[i] = upper[i - 1]
            supertrend[i] = lower[i] if trend[i] == 1 else upper[i]
        df["Supertrend"]       = supertrend
        df["Supertrend_trend"] = trend

        # Stochastic (14, 3)
        K = 14
        lowest_low    = np.array([np.min(low[max(0, i - K + 1):i + 1])  for i in range(len(close))])
        highest_high  = np.array([np.max(high[max(0, i - K + 1):i + 1]) for i in range(len(close))])
        stoch_k = np.where(highest_high - lowest_low != 0,
                           100 * (close - lowest_low) / (highest_high - lowest_low + 1e-14),
                           50.0)
        stoch_d = np.convolve(stoch_k, np.ones(3) / 3, mode="same")
        df["Stoch_K"] = stoch_k
        df["Stoch_D"] = stoch_d
        return df

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL ANALYZER
# ─────────────────────────────────────────────────────────────────────────────
class SignalAnalyzer:
    ADX_TREND_THRESHOLD    = 20
    VOLUME_RATIO_THRESHOLD = 1.5

    @staticmethod
    def _sf(val, default=0.0) -> float:
        """Safe float from a potentially array-like value."""
        try:
            v = val.item() if hasattr(val, "item") else val
            return float(v)
        except Exception:
            return default

    @staticmethod
    def generate_signal(df, prev_ema_fast, prev_ema_slow, config):
        if prev_ema_fast is None or prev_ema_slow is None:
            return None, ""
        latest  = df.iloc[-1]
        ema_f   = SignalAnalyzer._sf(latest["EMA_fast"])
        ema_s   = SignalAnalyzer._sf(latest["EMA_slow"])
        price   = SignalAnalyzer._sf(latest["Close"])
        bull_xo = prev_ema_fast <= prev_ema_slow and ema_f > ema_s
        bear_xo = prev_ema_fast >= prev_ema_slow and ema_f < ema_s
        if bull_xo:
            if SignalAnalyzer._confirm(df, config, "bull", price):
                return "BUY",  f"BUY @ ${price:.2f}"
        if bear_xo:
            if SignalAnalyzer._confirm(df, config, "bear", price):
                return "SELL", f"SELL @ ${price:.2f}"
        return None, ""

    @staticmethod
    def _confirm(df, config, direction, price) -> bool:
        l        = df.iloc[-1]
        sf       = SignalAnalyzer._sf
        rsi      = sf(l.get("RSI",             50), 50)
        macd     = sf(l.get("MACD",             0),  0)
        macd_sig = sf(l.get("MACD_signal",      0),  0)
        bb_up    = sf(l.get("BB_upper",      price), price)
        bb_lo    = sf(l.get("BB_lower",      price), price)
        vwap     = sf(l.get("VWAP",          price), price)
        adx      = sf(l.get("ADX",               0), 0)
        vol_r    = sf(l.get("Vol_ratio",          1), 1)
        st_trend = sf(l.get("Supertrend_trend",   0), 0)
        stoch_k  = sf(l.get("Stoch_K",           50), 50)
        stoch_d  = sf(l.get("Stoch_D",           50), 50)

        if direction == "bull":
            if config.get("use_rsi",       True) and rsi < 30:                      return False
            if config.get("use_macd",      True) and macd <= macd_sig:              return False
            if config.get("use_vwap",      True) and price < vwap:                  return False
            if config.get("use_bollinger", True) and price < bb_lo * 0.99:          return False
            if config.get("use_supertrend",True) and st_trend != 1:                 return False
            if config.get("use_stochastic",True) and (stoch_k < stoch_d or stoch_k > 80): return False
        else:
            if config.get("use_rsi",       True) and rsi > 70:                      return False
            if config.get("use_macd",      True) and macd >= macd_sig:              return False
            if config.get("use_vwap",      True) and price > vwap:                  return False
            if config.get("use_bollinger", True) and price > bb_up * 1.01:          return False
            if config.get("use_supertrend",True) and st_trend != -1:                return False
            if config.get("use_stochastic",True) and (stoch_k > stoch_d or stoch_k < 20): return False

        if config.get("use_adx",        True) and adx < SignalAnalyzer.ADX_TREND_THRESHOLD:    return False
        if config.get("use_vol_confirm",True) and vol_r < SignalAnalyzer.VOLUME_RATIO_THRESHOLD: return False
        return True

# ─────────────────────────────────────────────────────────────────────────────
# TRADING ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue, config, broker):
        super().__init__(daemon=True)
        self.ui_queue        = ui_queue
        self.config          = config
        self.broker          = broker
        self.running         = False
        self.symbols: List[str] = []
        self.positions       = {}
        self.prev_ema        = {}
        self.per_ticker_qty  = {}
        self.is_licensed     = config.get("license_valid", False)

    def _send_telegram(self, message: str):
        tg    = self.config.get("telegram", {})
        token = tg.get("token")
        cid   = tg.get("chat_id")
        if token and cid:
            try:
                http_requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": cid, "text": message, "parse_mode": "HTML"},
                    timeout=5)
            except Exception:
                pass

    def run(self):
        tickers_str = self.config.get("tickers", "AAPL")
        raw_list    = [s.strip() for s in tickers_str.split(",") if s.strip()]
        default_qty = self.config.get("quantity", 1)

        for entry in raw_list:
            sym = clean_symbol(entry)
            qty = default_qty
            if ":" in entry:
                try:
                    raw_qty = float(entry.split(":")[1])
                    qty     = int(raw_qty) if raw_qty == int(raw_qty) else raw_qty
                except Exception:
                    pass
            if sym not in self.symbols:
                self.symbols.append(sym)
                self.per_ticker_qty[sym] = qty

        if not self.is_licensed and len(self.symbols) > 1:
            first         = self.symbols[0]
            self.symbols  = [first]
            self.per_ticker_qty = {first: self.per_ticker_qty[first]}
            self.ui_queue.put(("error",
                "Free license: limited to 1 ticker. Tracking " + first + " only."))

        for sym in self.symbols:
            self.positions[sym] = 0
            self.prev_ema[sym]  = (None, None)

        mode          = "signal" if not self.is_licensed else self.config.get("mode", "signal")
        ema_fast, ema_slow = self.config.get("emas", [9, 50])
        use_bracket   = self.config.get("use_bracket", False)
        sl_pct        = self.config.get("sl_percent", 2.0)
        tp_pct        = self.config.get("tp_percent", 4.0)
        use_atr_stops = self.config.get("use_atr_stops", True)
        interval      = self.config.get("timeframe", "1m")

        if not self.is_licensed:
            for key in ("use_supertrend", "use_stochastic", "use_adx",
                        "use_vol_confirm", "use_atr_stops", "use_bracket"):
                self.config[key] = False

        self.broker.stream_prices(self.symbols, self._on_price_update)
        self.ui_queue.put(("status", f"✅ Running {len(self.symbols)} symbols"))
        self._send_telegram(
            f"🤖 TraderMoney started\n"
            f"Symbols: {', '.join(self.symbols)}\n"
            f"Mode: {mode}")

        last_hist = 0.0
        while self.running:
            try:
                acc = self.broker.get_account()
                if acc:
                    self.ui_queue.put(
                        ("account", (acc["equity"], acc["pl"],
                                     acc["buying_power"], acc.get("open_positions", 0))))
                is_open = self.broker.get_market_status()
                self.ui_queue.put(("market", "🟢 Open" if is_open else "🔴 Closed"))

                now = time.time()
                if now - last_hist >= 60:
                    last_hist  = now
                    ema_update = {}
                    for sym in self.symbols:
                        try:
                            import yfinance as yf
                            import pandas   as pd
                            df = yf.download(sym, period="5d", interval=interval,
                                             progress=False, auto_adjust=True)
                            if df is None or df.empty:
                                self.ui_queue.put(("log", f"No data for {sym}"))
                                continue
                            if isinstance(df.columns, pd.MultiIndex):
                                df.columns = df.columns.get_level_values(0)
                            df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
                        except Exception as e:
                            self.ui_queue.put(("error", f"Data/indicator error {sym}: {e}"))
                            continue

                        latest  = df.iloc[-1]
                        price   = SignalAnalyzer._sf(latest["Close"])
                        ema_f   = SignalAnalyzer._sf(latest["EMA_fast"])
                        ema_s   = SignalAnalyzer._sf(latest["EMA_slow"])
                        ema_update[sym] = {"fast": round(ema_f, 2), "slow": round(ema_s, 2)}

                        prev_f, prev_s = self.prev_ema.get(sym, (None, None))
                        self.prev_ema[sym] = (ema_f, ema_s)

                        if prev_f is not None:
                            sig, rationale = SignalAnalyzer.generate_signal(
                                df, prev_f, prev_s, self.config)
                            if sig:
                                self.ui_queue.put(("signal", (sym, sig, price, rationale)))
                                db.insert_signal(_ts(), sym, sig, price, rationale)
                                self._send_telegram(
                                    f"📡 <b>{sig}</b> {sym} @ ${price:.2f}\n{rationale}")
                                if mode == "auto" and self.is_licensed and is_open:
                                    qty = self.per_ticker_qty.get(sym, default_qty)
                                    self._execute_signal(
                                        sym, sig, qty, price, latest,
                                        use_bracket, use_atr_stops, sl_pct, tp_pct)

                    if ema_update:
                        self.ui_queue.put(("ema_update", ema_update))

                time.sleep(1)

            except Exception:
                self.ui_queue.put(("error",
                    f"Engine loop error:\n{traceback.format_exc()}"))
                time.sleep(5)

        self.broker.stop_stream()
        self.ui_queue.put(("status", "⏹️ Bot stopped"))

    def _execute_signal(self, sym, sig, qty, price, latest,
                        use_bracket, use_atr_stops, sl_pct, tp_pct):
        try:
            if sig == "BUY" and self.positions.get(sym, 0) == 0:
                success = False
                if use_bracket and use_atr_stops:
                    atr_val  = SignalAnalyzer._sf(latest.get("ATR", price * 0.02), price * 0.02)
                    sl_price = price - ATR_STOP_MULTIPLIER * atr_val
                    tp_price = price + ATR_TP_MULTIPLIER   * atr_val
                    success  = self.broker.submit_order(
                        sym, qty, "buy", sl_price=sl_price, tp_price=tp_price)
                elif use_bracket:
                    success = self.broker.submit_order(
                        sym, qty, "buy", sl_pct=sl_pct, tp_pct=tp_pct)
                else:
                    success = self.broker.submit_order(sym, qty, "buy")
                if success:
                    self.positions[sym] = qty
                    self.ui_queue.put(("order", (sym, "BUY", qty, price)))
                    db.insert_trade(_ts(), sym, "BUY", qty, price)
                    self._send_telegram(f"✅ BUY {qty} {sym} @ ${price:.2f}")

            elif sig == "SELL" and self.positions.get(sym, 0) > 0:
                pos_qty = self.positions[sym]
                success = self.broker.submit_order(sym, pos_qty, "sell")
                if success:
                    self.positions[sym] = 0
                    self.ui_queue.put(("order", (sym, "SELL", pos_qty, price)))
                    db.insert_trade(_ts(), sym, "SELL", pos_qty, price)
                    self._send_telegram(f"✅ SELL {pos_qty} {sym} @ ${price:.2f}")
        except Exception as e:
            self.ui_queue.put(("error", f"Execute signal error {sym}: {e}"))

    def _on_price_update(self, sym, price):
        self.ui_queue.put(("price_update", (sym, price)))

    def stop(self):
        self.running = False

# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return FRONTEND_HTML

@app.route("/mobile")
def mobile():
    try:
        return send_file("mobile.html")
    except Exception:
        return "<h1>Mobile dashboard not found</h1>", 404

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(state.config)

@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.json or {}
    state.config.update(data)
    EncryptedConfigManager.save(state.config)
    return jsonify({"status": "ok", "message": "Configuration saved"})

@app.route("/api/start", methods=["POST"])
def start_bot():
    data = request.json or {}
    state.config.update(data)
    EncryptedConfigManager.save(state.config)

    if state.engine and state.engine.running:
        return jsonify({"status": "error", "message": "Bot is already running. Stop it first."})

    broker_name = state.config.get("broker", "Alpaca")
    broker_cls  = BROKER_REGISTRY.get(broker_name)
    if not broker_cls:
        return jsonify({"status": "error",
                        "message": f"Broker '{broker_name}' is not supported."})

    state.broker_instance = broker_cls(state.config, state.ui_queue)
    if not state.broker_instance.connect():
        error_text = state.broker_instance.last_error or "Unknown connection failure."
        state.config["last_broker_message"] = f"❌ {error_text}"
        EncryptedConfigManager.save(state.config)
        return jsonify({"status": "error",
                        "message": f"Broker connection failed: {error_text}"})

    state.config["last_broker_message"] = "✅ Connected"
    EncryptedConfigManager.save(state.config)

    state.engine         = TradingEngine(state.ui_queue, state.config, state.broker_instance)
    state.engine.running = True
    state.engine.start()
    state.running        = True
    return jsonify({"status": "ok", "message": f"Bot started with {broker_name}"})

@app.route("/api/stop", methods=["POST"])
def stop_bot():
    if state.engine:
        state.engine.stop()
    state.running = False
    return jsonify({"status": "ok", "message": "Bot stopped"})

@app.route("/api/kill", methods=["POST"])
def kill_switch():
    if state.broker_instance:
        threading.Thread(
            target=state.broker_instance.close_all_positions, daemon=True).start()
    if state.engine:
        state.engine.stop()
    state.running = False
    return jsonify({"status": "ok", "message": "Kill switch activated – closing all positions"})

@app.route("/api/status", methods=["GET"])
def get_status():
    while not state.ui_queue.empty():
        try:
            msg = state.ui_queue.get_nowait()
            kind = msg[0]
            if kind == "account":
                eq, pl, bp, op = msg[1]
                state.dashboard.update(
                    equity=eq, pl=pl, buying_power=bp, open_positions=op)
            elif kind in ("signal", "order", "price_update", "status", "market"):
                pass   # logged to DB already; UI polls via /api/status
            elif kind in ("log", "error"):
                db.insert_log(msg[1])
            elif kind == "ema_update":
                state.dashboard["ema_values"] = msg[1]
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
        "ema_values":     state.dashboard.get("ema_values", {}),
        "log":            db.get_recent_logs(50),
    })

@app.route("/api/broker_status", methods=["GET"])
def broker_status():
    return jsonify({"message": state.config.get("last_broker_message", "")})

@app.route("/api/validate_license", methods=["POST"])
def validate_license():
    data    = request.json or {}
    key     = data.get("license_key", "").strip()
    if not key:
        return jsonify({"valid": False, "message": "No license key provided"})
    valid, msg = verify_gumroad_license(key)
    if valid:
        state.config["license_valid"] = True
        state.config["license_key"]   = key
        EncryptedConfigManager.save(state.config)
    else:
        state.config["license_valid"] = False
    return jsonify({"valid": valid, "message": msg})

@app.route("/api/update", methods=["GET"])
def check_update():
    try:
        url  = "https://raw.githubusercontent.com/shafayrich/tradermoney/main/version.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
        latest  = data.get("latest_version", "0.0.0")
        is_new  = (tuple(map(int, latest.split("."))) >
                   tuple(map(int, APP_VERSION.split("."))))
        return jsonify({
            "current_version":  APP_VERSION,
            "latest_version":   latest,
            "download_url":     data.get("download_url", ""),
            "update_available": is_new,
        })
    except Exception as e:
        return jsonify({"update_available": False, "error": str(e)})

@app.route("/api/backtest", methods=["POST"])
def backtest():
    data   = request.json or {}
    config = data.get("config", state.config)
    days   = int(data.get("days", 5))
    try:
        import yfinance as yf
        import pandas   as pd

        tickers_str = config.get("tickers", "AAPL")
        raw_list    = [s.strip() for s in tickers_str.split(",") if s.strip()]
        symbols     = []
        for entry in raw_list:
            sym = clean_symbol(entry)
            if sym and sym not in symbols:
                symbols.append(sym)

        ema_fast, ema_slow = config.get("emas", [9, 50])
        all_results = {}
        for symbol in symbols:
            try:
                df = yf.download(symbol, period=f"{days}d",
                                 interval=config.get("timeframe", "1m"),
                                 progress=False, auto_adjust=True)
                if df is None or df.empty:
                    all_results[symbol] = {"error": "No market data returned"}
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
                signals = []
                for i in range(1, len(df)):
                    prev    = df.iloc[i - 1]
                    curr    = df.iloc[i]
                    prev_f  = SignalAnalyzer._sf(prev["EMA_fast"])
                    prev_s  = SignalAnalyzer._sf(prev["EMA_slow"])
                    sig, rationale = SignalAnalyzer.generate_signal(
                        df.iloc[:i + 1], prev_f, prev_s, config)
                    if sig:
                        sf = SignalAnalyzer._sf
                        signals.append({
                            "time":      str(df.index[i]),
                            "signal":    sig,
                            "price":     round(sf(curr["Close"]), 2),
                            "rationale": rationale,
                            "indicators": {
                                "RSI":             round(sf(curr.get("RSI",          50), 50), 1),
                                "MACD":            round(sf(curr.get("MACD",          0),  0), 4),
                                "MACD_signal":     round(sf(curr.get("MACD_signal",   0),  0), 4),
                                "VWAP":            round(sf(curr.get("VWAP",          0),  0), 2),
                                "BB_upper":        round(sf(curr.get("BB_upper",      0),  0), 2),
                                "BB_lower":        round(sf(curr.get("BB_lower",      0),  0), 2),
                                "ADX":             round(sf(curr.get("ADX",           0),  0), 1),
                                "Vol_ratio":       round(sf(curr.get("Vol_ratio",     1),  1), 2),
                                "Supertrend_trend":int(sf(curr.get("Supertrend_trend",0),  0)),
                                "Stoch_K":         round(sf(curr.get("Stoch_K",      50), 50), 1),
                                "Stoch_D":         round(sf(curr.get("Stoch_D",      50), 50), 1),
                            }
                        })
                all_results[symbol] = {"signals": signals}
            except Exception as e:
                all_results[symbol] = {"error": str(e)}

        db.insert_backtest(json.dumps({"config": config, "results": all_results}))
        return jsonify({"results": all_results})
    except Exception as e:
        return jsonify({"error": str(e)})

# ─────────────────────────────────────────────────────────────────────────────
# FRONTEND HTML
# ─────────────────────────────────────────────────────────────────────────────
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TraderMoney</title>
<style>
  :root {
    --bg:#050505; --card:#1A1A1A; --text:#e2e2e2; --accent:#D4AF37; --accent2:#6A0DAD;
    --danger:#B22222; --border:#2A2E38; --btn:#D4AF37; --muted:#7a7d86; --sw:260px;
  }
  ::-webkit-scrollbar{width:4px;} ::-webkit-scrollbar-track{background:#080808;} ::-webkit-scrollbar-thumb{background:#111;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden;}

  /* ── SIDEBAR ─────────────────────────────────────────────── */
  #sidebar{width:var(--sw);background:#0b0b0b;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding:18px 14px;}
  #sidebar h2{color:var(--accent);margin:0 0 12px;font-size:1.2rem;letter-spacing:.4px;}
  .lic-badge{display:inline-block;padding:2px 9px;border-radius:12px;font-size:.68rem;margin-left:6px;vertical-align:middle;}
  .lic-valid{background:var(--accent);color:#000;} .lic-invalid{background:var(--danger);color:#fff;}
  label{display:block;font-size:.78rem;margin:10px 0 4px;color:var(--muted);}
  input,select{background:#1A1A1A;color:var(--text);border:1px solid #252525;padding:7px 9px;border-radius:7px;width:100%;font-size:.88rem;transition:border .2s;}
  input:focus,select:focus{border-color:var(--accent);outline:none;}
  button{cursor:pointer;background:var(--btn);color:#050505;border:none;padding:8px 12px;border-radius:7px;width:100%;font-weight:600;margin-top:10px;font-size:.88rem;}
  button:hover{opacity:.88;} button.danger{background:var(--danger);color:#fff;}
  button.ghost{background:var(--card);border:1px solid var(--border);color:var(--text);}
  hr{border-color:var(--border);margin:12px 0;}
  #broker-status{font-size:.76rem;margin-top:4px;min-height:16px;word-break:break-word;}
  #broker-status.ok{color:#00c9b1;} #broker-status.err{color:var(--danger);}
  .row2{display:flex;gap:6px;}  .row2 input{width:100%;}

  /* ── MAIN ────────────────────────────────────────────────── */
  #main{flex:1;display:flex;flex-direction:column;min-width:0;}
  .tab-header{display:flex;background:var(--card);border-bottom:1px solid var(--border);flex-wrap:wrap;}
  .tab-btn{flex:1;background:transparent;border:none;color:var(--text);padding:13px 8px;cursor:pointer;font-weight:500;letter-spacing:.3px;transition:.2s;border-bottom:2px solid transparent;min-width:80px;}
  .tab-btn:hover{background:rgba(255,255,255,.03);}
  .tab-btn.active{border-bottom-color:var(--accent2);color:var(--accent);font-weight:700;}
  .tab-content{flex:1;display:none;overflow:hidden;flex-direction:column;}
  .tab-content.active{display:flex;}

  /* ── METRICS BAR ─────────────────────────────────────────── */
  #metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px;background:var(--card);border-bottom:1px solid var(--border);}
  .metric{text-align:center;} .metric .val{font-size:1.15rem;font-weight:bold;color:var(--accent);}

  /* ── SESSIONS BAR ────────────────────────────────────────── */
  #sessions{display:flex;align-items:center;gap:14px;padding:8px 12px;background:var(--card);border-bottom:1px solid var(--border);font-size:.83rem;}
  .sdot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px;}
  .open{background:#00c9b1;} .closed{background:var(--danger);}

  /* ── TICKER TABS ─────────────────────────────────────────── */
  #ticker-tabs{display:flex;background:var(--card);border-bottom:1px solid var(--border);overflow-x:auto;}
  .tkbtn{padding:7px 14px;background:transparent;border:none;color:var(--text);cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:.2s;}
  .tkbtn.active{border-bottom-color:var(--accent2);color:var(--accent);font-weight:700;}

  /* ── CHART ───────────────────────────────────────────────── */
  #chart-container{flex:1;min-height:0;}

  /* ── SIGNALS / HISTORY LIST ──────────────────────────────── */
  .sig-item{display:flex;justify-content:space-between;padding:9px 12px;border-bottom:1px solid var(--border);font-size:.84rem;}
  .buy{color:var(--accent);} .sell{color:var(--danger);}

  /* ── EMA MONITOR ─────────────────────────────────────────── */
  .ema-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;padding:10px;overflow-y:auto;}
  .ema-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px;text-align:center;}
  .ema-card .tk{font-weight:bold;color:var(--accent);}
  .ema-card .ev{font-size:1.05rem;margin-top:4px;} .ema-card .el{font-size:.7rem;color:var(--muted);}

  /* ── BACKTEST ────────────────────────────────────────────── */
  .bt-panel{flex:1;display:flex;flex-direction:column;}
  .bt-results{flex:1;overflow-y:auto;padding:10px;}
  .ph{color:var(--muted);text-align:center;padding:40px 20px;}
  .bt-table{width:100%;border-collapse:collapse;font-size:.82rem;margin-bottom:18px;}
  .bt-table th,.bt-table td{padding:5px 7px;border:1px solid var(--border);text-align:center;}
  .bt-table th{color:var(--accent);}

  /* ── LOG ─────────────────────────────────────────────────── */
  #log{height:110px;overflow-y:auto;background:var(--bg);padding:8px 12px;font-size:.76rem;border-top:1px solid var(--border);color:var(--muted);flex-shrink:0;}

  /* ── TOASTS ──────────────────────────────────────────────── */
  #toasts{position:fixed;top:18px;right:18px;z-index:9999;display:flex;flex-direction:column;gap:7px;}
  .toast{padding:10px 18px;border-radius:6px;color:#fff;font-weight:500;box-shadow:0 4px 14px rgba(0,0,0,.4);animation:si .3s ease;max-width:320px;}
  .toast.success{background:var(--accent);color:#000;} .toast.error{background:var(--danger);} .toast.info{background:var(--accent2);}
  @keyframes si{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}

  /* ── UPDATE BANNER ───────────────────────────────────────── */
  #upd-banner{display:none;position:fixed;bottom:18px;right:18px;z-index:9999;background:var(--accent);color:#000;padding:13px 18px;border-radius:8px;font-weight:bold;}
  #upd-banner a{color:#000;text-decoration:underline;cursor:pointer;}

  /* ── HELP ────────────────────────────────────────────────── */
  .help-body{padding:20px;overflow-y:auto;height:100%;}
  .help-body h3{color:var(--accent2);margin-top:0;} .help-body h4{color:var(--text);margin:14px 0 5px;}
  .help-body p,.help-body ul{font-size:.88rem;line-height:1.65;} .help-body ul{padding-left:18px;} .help-body li{margin-bottom:5px;}
  .help-body a{color:var(--accent);}
  .ind-stats{background:var(--card);border-radius:8px;padding:14px;margin:8px 0;}
</style>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
</head>
<body>
<div id="toasts"></div>
<div id="upd-banner"><span>🔔 New version! <a id="upd-link" href="#" target="_blank">Download</a></span></div>

<!-- ═══ SIDEBAR ═══════════════════════════════════════════════════════════ -->
<div id="sidebar">
  <h2>💸 TraderMoney <span id="lic-badge" class="lic-badge lic-invalid">FREE</span></h2>

  <label>License Key</label>
  <input type="password" id="license-key" placeholder="Paste Gumroad key">
  <button onclick="validateLicense()" style="margin-top:5px;font-size:.82rem;">🔑 Validate</button>
  <p style="font-size:.7rem;color:var(--muted);margin:3px 0 0;"><a href="https://shafayrich.gumroad.com/l/ykaoov" target="_blank" style="color:var(--accent);">Buy a license ↗</a></p>

  <hr>
  <label>Broker</label>
  <select id="broker-select" onchange="updateCredFields()">
    <option>Alpaca</option>
    <option>Interactive Brokers</option>
    <option>Tradier</option>
    <option>Binance</option>
    <option>Bybit</option>
    <option>OKX</option>
  </select>
  <div id="broker-status" class="ok"></div>
  <div id="cred-entries"></div>

  <label>Telegram Bot Token (opt)</label><input type="password" id="tg-token">
  <label>Telegram Chat ID (opt)</label><input id="tg-chat">

  <label>Tickers – e.g. AAPL:5, BTC:0.01</label>
  <input id="tickers" value="AAPL">

  <label>Timeframe</label>
  <select id="timeframe">
    <option>1m</option><option>5m</option><option>15m</option>
    <option>30m</option><option>1h</option><option>1d</option>
  </select>

  <label>EMA Periods</label>
  <div class="row2"><input id="ema-fast" value="9" placeholder="Fast"><input id="ema-slow" value="50" placeholder="Slow"></div>

  <label>Default Quantity</label>
  <input id="quantity" value="1" type="number">

  <label>Mode</label>
  <select id="mode">
    <option value="signal">Signal Only</option>
    <option value="auto">Auto Trade</option>
  </select>

  <label><input type="checkbox" id="use-bracket"> Enable Bracket (SL/TP)</label>
  <div class="row2"><input id="sl-percent" value="2" placeholder="SL %"><input id="tp-percent" value="4" placeholder="TP %"></div>
  <label><input type="checkbox" id="use-atr-stops" checked> ATR Dynamic Stops</label>

  <label style="margin-top:13px;font-weight:bold;color:var(--accent);">Indicators</label>
  <label><input type="checkbox" id="use-rsi"        checked> RSI (14)</label>
  <label><input type="checkbox" id="use-macd"       checked> MACD (12,26,9)</label>
  <label><input type="checkbox" id="use-vwap"       checked> VWAP</label>
  <label><input type="checkbox" id="use-bollinger"  checked> Bollinger (20,2)</label>
  <label><input type="checkbox" id="use-adx"        checked> ADX (14)</label>
  <label><input type="checkbox" id="use-vol-confirm"checked> Volume (1.5x avg)</label>
  <label><input type="checkbox" id="use-supertrend" checked> SuperTrend (10,3)</label>
  <label><input type="checkbox" id="use-stochastic" checked> Stochastic (14,3)</label>

  <button onclick="saveConfig()">💾 Save</button>
  <button class="ghost" onclick="refreshTickers()">🔄 Refresh Tickers</button>
  <button style="background:var(--accent);color:#050505;" onclick="startBot()">▶️ Start</button>
  <button class="ghost" onclick="stopBot()">⏹️ Stop</button>
  <button class="danger" onclick="killSwitch()">⚠️ Kill Switch</button>
  <button class="ghost" style="margin-top:6px;" onclick="resetDefaults()">↺ Reset Defaults</button>
  <button class="ghost" style="margin-top:18px;" onclick="checkUpdates()">🔄 Check for Updates</button>
  <button style="background:var(--accent2);color:#fff;margin-top:8px;" onclick="runBacktest()">🧪 Backtest All Tickers</button>
</div>

<!-- ═══ MAIN ════════════════════════════════════════════════════════════════ -->
<div id="main">
  <div class="tab-header" id="tab-header">
    <button class="tab-btn active" data-tab="charts">Charts</button>
    <button class="tab-btn" data-tab="signals">Signals</button>
    <button class="tab-btn" data-tab="history">History</button>
    <button class="tab-btn" data-tab="ema">EMA Monitor</button>
    <button class="tab-btn" data-tab="backtest">Backtest</button>
    <button class="tab-btn" data-tab="help">Help</button>
  </div>

  <!-- Charts -->
  <div id="tab-charts" class="tab-content active">
    <div id="ticker-tabs"></div>
    <div id="metrics">
      <div class="metric"><div class="val" id="equity">—</div><div>Equity</div></div>
      <div class="metric"><div class="val" id="bp">—</div><div>Buying Power</div></div>
      <div class="metric"><div class="val" id="pl">—</div><div>Daily P&L</div></div>
      <div class="metric"><div class="val" id="positions">—</div><div>Positions</div></div>
    </div>
    <div id="sessions">
      <span style="color:var(--accent)">🌍 Sessions:</span>
      <span><span class="sdot" id="d-syd"></span>SYD</span>
      <span><span class="sdot" id="d-tky"></span>TKY</span>
      <span><span class="sdot" id="d-ldn"></span>LDN</span>
      <span><span class="sdot" id="d-nyc"></span>NYC</span>
      <span><span class="sdot open"></span>CRYPTO 24/7</span>
    </div>
    <div id="chart-container" style="flex:1;min-height:0;"></div>
  </div>

  <!-- Signals -->
  <div id="tab-signals" class="tab-content">
    <div id="signals-list" style="overflow-y:auto;flex:1;"></div>
  </div>

  <!-- History -->
  <div id="tab-history" class="tab-content">
    <div id="history-list" style="overflow-y:auto;flex:1;"></div>
  </div>

  <!-- EMA Monitor -->
  <div id="tab-ema" class="tab-content">
    <div class="ema-grid" id="ema-monitor">
      <span style="color:var(--muted);padding:10px;">Waiting for data…</span>
    </div>
  </div>

  <!-- Backtest -->
  <div id="tab-backtest" class="tab-content">
    <div class="bt-panel">
      <div style="padding:10px;">
        <button style="background:var(--accent2);color:#fff;width:auto;padding:9px 20px;"
                onclick="runBacktest()">🧪 Run Backtest on All Tickers</button>
      </div>
      <div id="bt-results" class="bt-results">
        <p class="ph">Click <strong>🧪 Backtest All Tickers</strong> to run.<br>Results appear here.</p>
      </div>
    </div>
  </div>

  <!-- Help -->
  <div id="tab-help" class="tab-content">
    <div class="help-body">
      <h3>📊 Indicator Win Rate Guide</h3>
      <div class="ind-stats">
        <p><b>Pure EMA Crossover (9/50):</b> ~32% win rate (very noisy).</p>
        <p><b>+ RSI:</b> ~40% &nbsp;|&nbsp; <b>+ MACD:</b> ~45% &nbsp;|&nbsp; <b>+ VWAP:</b> ~48%</p>
        <p><b>+ Bollinger:</b> ~50% &nbsp;|&nbsp; <b>+ ADX ≥ 20:</b> ~55%</p>
        <p><b>+ Volume (1.5×):</b> ~58% &nbsp;|&nbsp; <b>+ SuperTrend:</b> ~62%</p>
        <p><b>+ Stochastic:</b> ~65% &nbsp;|&nbsp; <b>+ ATR stops:</b> profit factor +0.4</p>
      </div>
      <h4>🏦 Broker Notes</h4>
      <ul>
        <li><b>Alpaca:</b> Paper (default) or live. Enter API Key + Secret from alpaca.markets.</li>
        <li><b>Interactive Brokers:</b> TWS or IB Gateway must be running. Enable API in TWS settings.
          Default ports: 7497 (TWS paper), 7496 (TWS live), 4002 (Gateway paper), 4001 (Gateway live).</li>
        <li><b>Tradier:</b> Use your Access Token + Account ID from developer.tradier.com.
          Toggle Sandbox for paper trading.</li>
        <li><b>Binance:</b> API key + secret from binance.com/en/my/settings/api-management.
          Enable Testnet checkbox for paper trading.</li>
        <li><b>Bybit:</b> API key + secret from bybit.com. Enable Testnet for paper trading.
          Requires pybit v5.</li>
        <li><b>OKX:</b> Key + secret + passphrase from okx.com. Enable Demo for paper trading.</li>
      </ul>
      <h4>🔑 License</h4>
      <p>Purchase at <a href="https://shafayrich.gumroad.com/l/ykaoov" target="_blank">Gumroad ↗</a>.
         Paste key in the sidebar and click Validate. Unlocks Auto Trade, multi-ticker, and advanced indicators.</p>
      <h4>📡 Telegram Alerts</h4>
      <p>Create a bot with @BotFather, copy the token and your Chat ID into the sidebar.</p>
    </div>
  </div>

  <div id="log"></div>
</div>

<script src="https://s3.tradingview.com/tv.js"></script>
<script>
// ── state ──────────────────────────────────────────────────────────────────
let cfg={}, licValid=false, curSym='', tickers=[], chartWidget=null, lastChartSym='';

// ── helpers ────────────────────────────────────────────────────────────────
const $=id=>document.getElementById(id);
function cleanSym(r){return r.split(':')[0].trim().toUpperCase();}
function toast(msg,type='info'){
  let c=$('toasts'),t=document.createElement('div');
  t.className='toast '+type; t.textContent=msg; c.appendChild(t);
  setTimeout(()=>t.remove(),3500);
}

// ── tab switching ──────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(b=>{
  b.addEventListener('click',function(){
    document.querySelectorAll('.tab-content').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(x=>x.classList.remove('active'));
    $('tab-'+this.dataset.tab).classList.add('active');
    this.classList.add('active');
    if(this.dataset.tab==='charts' && chartWidget) setTimeout(()=>chartWidget.resize&&chartWidget.resize(),80);
  });
});
Sortable.create($('tab-header'),{animation:130,handle:'.tab-btn'});

// ── market sessions ────────────────────────────────────────────────────────
function updateSessions(){
  const now=new Date(), day=now.getUTCDay(), wk=day===0||day===6,
        h=now.getUTCHours()+now.getUTCMinutes()/60;
  const cls=(ok)=>ok?'sdot open':'sdot closed';
  $('d-syd').className=cls(!wk&&(h>=20||h<5));
  $('d-tky').className=cls(!wk&&(h>=23||h<6));
  $('d-ldn').className=cls(!wk&&h>=8&&h<16.5);
  $('d-nyc').className=cls(!wk&&h>=13&&h<20);
}
setInterval(updateSessions,60000); updateSessions();

// ── broker credential fields ────────────────────────────────────────────────
function updateCredFields(){
  const b=$('broker-select').value, c=$('cred-entries');
  const pw=(id,ph)=>`<label>${ph}</label><input type="password" id="${id}" placeholder="${ph}">`;
  const tx=(id,ph,v='')=>`<label>${ph}</label><input id="${id}" placeholder="${ph}" value="${v}">`;
  const cb=(id,lbl,chk)=>`<label style="margin-top:8px"><input type="checkbox" id="${id}" ${chk?'checked':''}> ${lbl}</label>`;
  if(b==='Alpaca') c.innerHTML=
    pw('alpaca-key','API Key')+pw('alpaca-secret','Secret Key')+
    cb('alpaca-paper','Paper Trading',true);
  else if(b==='Interactive Brokers') c.innerHTML=
    tx('ibkr-host','Host','127.0.0.1')+tx('ibkr-port','Port','7497')+
    tx('ibkr-client-id','Client ID','1');
  else if(b==='Tradier') c.innerHTML=
    pw('tradier-token','Access Token')+tx('tradier-account-id','Account ID')+
    cb('tradier-sandbox','Sandbox (Paper)',false);
  else if(b==='Binance') c.innerHTML=
    pw('binance-key','API Key')+pw('binance-secret','API Secret')+
    cb('binance-testnet','Testnet (Paper)',true);
  else if(b==='Bybit') c.innerHTML=
    pw('bybit-key','API Key')+pw('bybit-secret','API Secret')+
    cb('bybit-testnet','Testnet (Paper)',true);
  else if(b==='OKX') c.innerHTML=
    pw('okx-key','API Key')+pw('okx-secret','API Secret')+
    pw('okx-passphrase','Passphrase')+
    cb('okx-demo','Demo Trading',true);
}

// ── config helpers ─────────────────────────────────────────────────────────
function gv(id,fb=''){const el=$(id);return el?el.value:fb;}
function gc(id){const el=$(id);return el?el.checked:false;}

function buildConfig(){
  const b=$('broker-select').value;
  return {
    broker:b, tickers:gv('tickers','AAPL'),
    timeframe:gv('timeframe','1m'),
    emas:[parseInt(gv('ema-fast','9')),parseInt(gv('ema-slow','50'))],
    quantity:parseInt(gv('quantity','1'))||1,
    mode:gv('mode','signal'),
    use_bracket:gc('use-bracket'), sl_percent:parseFloat(gv('sl-percent','2')),
    tp_percent:parseFloat(gv('tp-percent','4')), use_atr_stops:gc('use-atr-stops'),
    telegram:{token:gv('tg-token'),chat_id:gv('tg-chat')},
    use_rsi:gc('use-rsi'), use_macd:gc('use-macd'), use_vwap:gc('use-vwap'),
    use_bollinger:gc('use-bollinger'), use_adx:gc('use-adx'),
    use_vol_confirm:gc('use-vol-confirm'), use_supertrend:gc('use-supertrend'),
    use_stochastic:gc('use-stochastic'), license_key:gv('license-key',''),
    alpaca:b==='Alpaca'?{api_key:gv('alpaca-key'),secret_key:gv('alpaca-secret'),paper:gc('alpaca-paper')}:{},
    ibkr:b==='Interactive Brokers'?{host:gv('ibkr-host','127.0.0.1'),port:gv('ibkr-port','7497'),client_id:gv('ibkr-client-id','1')}:{},
    tradier:b==='Tradier'?{access_token:gv('tradier-token'),account_id:gv('tradier-account-id'),sandbox:gc('tradier-sandbox')}:{},
    binance:b==='Binance'?{api_key:gv('binance-key'),api_secret:gv('binance-secret'),testnet:gc('binance-testnet')}:{},
    bybit:b==='Bybit'?{api_key:gv('bybit-key'),api_secret:gv('bybit-secret'),testnet:gc('bybit-testnet')}:{},
    okx:b==='OKX'?{api_key:gv('okx-key'),api_secret:gv('okx-secret'),api_passphrase:gv('okx-passphrase'),demo:gc('okx-demo')}:{},
  };
}

function initUI(c){
  if(!c)return;
  $('broker-select').value=c.broker||'Alpaca';
  $('tickers').value=c.tickers||'AAPL';
  $('ema-fast').value=c.emas?c.emas[0]:9;
  $('ema-slow').value=c.emas?c.emas[1]:50;
  $('quantity').value=c.quantity||1;
  $('mode').value=c.mode||'signal';
  if(c.telegram){$('tg-token').value=c.telegram.token||'';$('tg-chat').value=c.telegram.chat_id||'';}
  $('use-bracket').checked=!!c.use_bracket;
  $('sl-percent').value=c.sl_percent||2; $('tp-percent').value=c.tp_percent||4;
  $('use-atr-stops').checked=c.use_atr_stops!==false;
  $('use-rsi').checked=c.use_rsi!==false; $('use-macd').checked=c.use_macd!==false;
  $('use-vwap').checked=c.use_vwap!==false; $('use-bollinger').checked=c.use_bollinger!==false;
  $('use-adx').checked=c.use_adx!==false; $('use-vol-confirm').checked=c.use_vol_confirm!==false;
  $('use-supertrend').checked=c.use_supertrend!==false; $('use-stochastic').checked=c.use_stochastic!==false;
  if(c.license_key)$('license-key').value=c.license_key;
  if(c.license_valid){licValid=true;$('lic-badge').textContent='PRO';$('lic-badge').className='lic-badge lic-valid';}
  updateCredFields();
  // restore saved cred values
  const b=c.broker||'Alpaca';
  if(b==='Alpaca'&&c.alpaca){if($('alpaca-key'))$('alpaca-key').value=c.alpaca.api_key||'';if($('alpaca-secret'))$('alpaca-secret').value=c.alpaca.secret_key||'';if($('alpaca-paper'))$('alpaca-paper').checked=c.alpaca.paper!==false;}
  if(b==='Interactive Brokers'&&c.ibkr){if($('ibkr-host'))$('ibkr-host').value=c.ibkr.host||'127.0.0.1';if($('ibkr-port'))$('ibkr-port').value=c.ibkr.port||'7497';if($('ibkr-client-id'))$('ibkr-client-id').value=c.ibkr.client_id||'1';}
  if(b==='Tradier'&&c.tradier){if($('tradier-token'))$('tradier-token').value=c.tradier.access_token||'';if($('tradier-account-id'))$('tradier-account-id').value=c.tradier.account_id||'';}
  if(b==='Binance'&&c.binance){if($('binance-key'))$('binance-key').value=c.binance.api_key||'';if($('binance-secret'))$('binance-secret').value=c.binance.api_secret||'';}
  if(b==='Bybit'&&c.bybit){if($('bybit-key'))$('bybit-key').value=c.bybit.api_key||'';if($('bybit-secret'))$('bybit-secret').value=c.bybit.api_secret||'';}
  if(b==='OKX'&&c.okx){if($('okx-key'))$('okx-key').value=c.okx.api_key||'';if($('okx-secret'))$('okx-secret').value=c.okx.api_secret||'';if($('okx-passphrase'))$('okx-passphrase').value=c.okx.api_passphrase||'';}
  // set tickers + chart
  let raw=c.tickers.split(',').map(s=>s.trim()).filter(s=>s);
  if(raw.length){setTickers(raw);loadChart(cleanSym(raw[0]));}
}

// ── ticker tabs + chart ────────────────────────────────────────────────────
function setTickers(list){
  tickers=list;
  let bar=$('ticker-tabs'); bar.innerHTML='';
  list.forEach(raw=>{
    let sym=cleanSym(raw),btn=document.createElement('button');
    btn.className='tkbtn'+(sym===curSym?' active':''); btn.textContent=raw;
    btn.onclick=()=>{curSym=sym;updateTkTabs();if(lastChartSym!==sym)loadChart(sym);};
    bar.appendChild(btn);
  });
}
function updateTkTabs(){
  document.querySelectorAll('.tkbtn').forEach(b=>b.classList.toggle('active',cleanSym(b.textContent)===curSym));
}
function loadChart(sym){
  let s=cleanSym(sym);
  if(s===lastChartSym)return; lastChartSym=s;
  $('chart-container').innerHTML='';
  if(typeof TradingView==='undefined'){setTimeout(()=>loadChart(s),150);return;}
  chartWidget=new TradingView.widget({autosize:true,symbol:s,interval:'1',timezone:'Etc/UTC',
    theme:'Dark',style:'1',locale:'en',toolbar_bg:'#0A0C0F',enable_publishing:false,
    hide_side_toolbar:false,allow_symbol_change:true,container_id:'chart-container'});
  curSym=s;
}

// ── load & save ────────────────────────────────────────────────────────────
async function loadConfig(){
  try{let r=await fetch('/api/config');cfg=await r.json();initUI(cfg);}
  catch(e){toast('Could not load config','error');}
}
async function saveConfig(){
  cfg=buildConfig();
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  toast('Configuration saved','success');
}
const DEF={broker:'Alpaca',tickers:'AAPL',mode:'signal',quantity:1,emas:[9,50],use_bracket:false,
  sl_percent:2,tp_percent:4,timeframe:'1m',telegram:{},use_rsi:true,use_macd:true,use_vwap:true,
  use_bollinger:true,use_adx:true,use_vol_confirm:true,use_supertrend:true,use_stochastic:true,
  use_atr_stops:true,license_key:'',license_valid:false};
function resetDefaults(){cfg=JSON.parse(JSON.stringify(DEF));initUI(cfg);saveConfig();toast('Reset to defaults','success');}

// ── bot controls ───────────────────────────────────────────────────────────
async function startBot(){
  cfg=buildConfig();
  let r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  let d=await r.json();
  toast(d.message,d.status==='ok'?'success':'error');
  if(d.status!=='ok'){
    // surface error in broker status field too
    let bs=$('broker-status');
    bs.textContent=d.message; bs.className='err';
  }
}
async function stopBot(){
  await fetch('/api/stop',{method:'POST'});
  toast('Bot stopped','success');
}
async function killSwitch(){
  await fetch('/api/kill',{method:'POST'});
  toast('Kill switch – closing all positions','error');
}
async function refreshTickers(){
  let r=await fetch('/api/config');let c=await r.json();
  $('tickers').value=c.tickers;
  let raw=c.tickers.split(',').map(s=>s.trim()).filter(s=>s);
  if(raw.length){setTickers(raw);loadChart(cleanSym(raw[0]));}
  toast('Tickers refreshed','success');
}

// ── license ────────────────────────────────────────────────────────────────
async function validateLicense(){
  let key=$('license-key').value.trim();
  if(!key){toast('Enter a license key','error');return;}
  let r=await fetch('/api/validate_license',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key})});
  let d=await r.json();
  let badge=$('lic-badge');
  if(d.valid){licValid=true;badge.textContent='PRO';badge.className='lic-badge lic-valid';toast('✅ License verified – Pro unlocked','success');}
  else{licValid=false;badge.textContent='FREE';badge.className='lic-badge lic-invalid';toast('❌ '+d.message,'error');}
}

// ── updates ────────────────────────────────────────────────────────────────
async function checkUpdates(){
  try{
    let r=await fetch('/api/update');let d=await r.json();
    if(d.update_available){$('upd-banner').style.display='block';$('upd-link').href=d.download_url;}
    else toast('✅ You are up to date!','success');
  }catch(e){}
}
setTimeout(checkUpdates,3000);

// ── poll broker status ─────────────────────────────────────────────────────
async function pollBrokerStatus(){
  try{
    let r=await fetch('/api/broker_status');let d=await r.json();
    let bs=$('broker-status');
    if(d.message){
      bs.textContent=d.message;
      bs.className=d.message.startsWith('✅')||d.message==='Connected'?'ok':'err';
    }
  }catch(e){}
}
setInterval(pollBrokerStatus,2500); pollBrokerStatus();

// ── poll status ────────────────────────────────────────────────────────────
async function pollStatus(){
  try{
    let d=await(await fetch('/api/status')).json();
    $('equity').textContent='$'+Number(d.equity).toLocaleString(undefined,{maximumFractionDigits:2});
    $('bp').textContent    ='$'+Number(d.buying_power).toLocaleString(undefined,{maximumFractionDigits:2});
    let pct=d.equity?(d.pl/d.equity*100):0;
    $('pl').innerHTML=`<span style="color:${pct>=0?'var(--accent)':'var(--danger)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</span>`;
    $('positions').textContent=d.open_positions;

    // signals
    let sl=$('signals-list'); sl.innerHTML='';
    (d.signals||[]).forEach(s=>{
      let div=document.createElement('div');
      div.className='sig-item '+(s.signal==='BUY'?'buy':'sell');
      div.innerHTML=`<span>${s.time} &nbsp;<b>${s.signal}</b> ${s.symbol} @ $${s.price}</span><span>${s.rationale||''}</span>`;
      sl.appendChild(div);
    });

    // history
    let hl=$('history-list'); hl.innerHTML='';
    (d.orders||[]).forEach(o=>{
      let div=document.createElement('div');
      div.className='sig-item '+(o.action==='BUY'?'buy':'sell');
      div.innerHTML=`<span>${o.time} &nbsp;<b>${o.action}</b> ${o.qty} ${o.symbol} @ $${o.price}</span>`;
      hl.appendChild(div);
    });

    // ema monitor
    let em=$('ema-monitor');
    if(d.ema_values&&Object.keys(d.ema_values).length){
      em.innerHTML=Object.entries(d.ema_values).map(([sym,v])=>
        `<div class="ema-card"><div class="tk">${sym}</div>
         <div class="ev"><span class="el">Fast:</span> ${v.fast}</div>
         <div class="ev"><span class="el">Slow:</span> ${v.slow}</div></div>`
      ).join('');
    }

    // log
    $('log').innerHTML=(d.log||[]).join('<br>');
  }catch(e){}
}
setInterval(pollStatus,1500);

// ── backtest ───────────────────────────────────────────────────────────────
async function runBacktest(){
  toast('Running backtest…','info');
  // switch to backtest tab
  document.querySelectorAll('.tab-content').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(x=>x.classList.remove('active'));
  $('tab-backtest').classList.add('active');
  document.querySelector('[data-tab="backtest"]').classList.add('active');
  try{
    let r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildConfig(),days:5})});
    let data=await r.json();
    if(data.error){toast('Backtest error: '+data.error,'error');return;}
    let html='',total=0;
    for(let sym in data.results){
      let info=data.results[sym];
      html+=`<h4 style="color:var(--accent)">${sym}</h4>`;
      if(info.error){html+=`<p style="color:var(--danger)">Error: ${info.error}</p>`;continue;}
      let sigs=info.signals||[]; total+=sigs.length;
      if(!sigs.length){html+='<p style="color:var(--muted)">No signals found.</p>';continue;}
      html+=`<table class="bt-table"><tr>
        <th>Time</th><th>Signal</th><th>Price</th><th>RSI</th>
        <th>MACD</th><th>MacSig</th><th>VWAP</th><th>BB L/U</th>
        <th>ADX</th><th>VolR</th><th>Trend</th><th>%K/%D</th><th>Note</th></tr>`;
      sigs.forEach(s=>{
        let i=s.indicators;
        html+=`<tr>
          <td>${s.time.slice(11,19)||s.time.slice(0,19)}</td>
          <td class="${s.signal==='BUY'?'buy':'sell'}">${s.signal}</td>
          <td>$${s.price}</td><td>${i.RSI}</td><td>${i.MACD}</td><td>${i.MACD_signal}</td>
          <td>$${i.VWAP}</td><td>${i.BB_lower}/${i.BB_upper}</td>
          <td>${i.ADX}</td><td>${i.Vol_ratio}×</td>
          <td>${i.Supertrend_trend===1?'🟢 Bull':'🔴 Bear'}</td>
          <td>${i.Stoch_K}/${i.Stoch_D}</td>
          <td>${s.rationale}</td></tr>`;
      });
      html+='</table>';
    }
    if(total===0) html='<p class="ph">No signals generated. Try toggling indicators or extending days.</p>';
    $('bt-results').innerHTML=html;
  }catch(e){toast('Backtest failed: '+e,'error');}
}

// ── boot ───────────────────────────────────────────────────────────────────
updateCredFields();
loadConfig();
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def run_flask():
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)

if __name__ == "__main__":
    acquire_lock()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.2)   # give Flask time to bind
    window = webview.create_window(
        "TraderMoney – Solar Eclipse",
        "http://127.0.0.1:5050",
        width=1340, height=820,
        min_size=(920, 640),
    )
    webview.start()

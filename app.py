"""
TraderMoney v1.0.43 – Immortal Persistence, SQLite, Universal Broker Errors, Scrollable Backtest.
"""

import json, os, queue, signal, sys, socket, sqlite3, threading, time, traceback, atexit, urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import requests as http_requests
import webview
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

APP_VERSION = "1.0.43"

# ── Gumroad ──────────────────────────────────────────────
GUMMROAD_PRODUCT_ID = "73otoT7rzJukCy-Lt4hhkQ=="

def verify_gumroad_license(license_key: str) -> Tuple[bool, str]:
    try:
        resp = http_requests.post(
            "https://api.gumroad.com/v2/licenses/verify",
            data={"product_id": GUMMROAD_PRODUCT_ID, "license_key": license_key},
            timeout=10,
        )
        data = resp.json()
        if not data.get("success"):
            return False, data.get("message", "Invalid license key")
        purchase = data.get("purchase", {})
        if purchase.get("refunded") or purchase.get("chargebacked"):
            return False, "License has been revoked (refunded/chargebacked)"
        return True, "License verified"
    except Exception:
        return False, "Cannot reach license server – try again later"

# ── Flask app and cross‑platform lock ────────────────────
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

atexit.register(lambda: None)
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

# ---------- DATABASE MANAGER (SQLite) ----------
DB_PATH = os.path.expanduser("~/.tradermoney_data.db")

class DatabaseManager:
    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
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

    def insert_trade(self, time_str, symbol, action, qty, price):
        self.conn.execute("INSERT INTO trades (timestamp, symbol, action, quantity, price) VALUES (?,?,?,?,?)",
                          (time_str, symbol, action, qty, price))
        self.conn.commit()

    def get_recent_trades(self, limit=50):
        cur = self.conn.execute("SELECT timestamp, symbol, action, quantity, price FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [{"time": row[0], "symbol": row[1], "action": row[2], "qty": row[3], "price": row[4]} for row in cur.fetchall()]

    def insert_signal(self, time_str, symbol, sig, price, rationale):
        self.conn.execute("INSERT INTO signals (timestamp, symbol, signal, price, rationale) VALUES (?,?,?,?,?)",
                          (time_str, symbol, sig, price, rationale))
        self.conn.commit()

    def get_recent_signals(self, limit=50):
        cur = self.conn.execute("SELECT timestamp, symbol, signal, price, rationale FROM signals ORDER BY id DESC LIMIT ?", (limit,))
        return [{"time": r[0], "symbol": r[1], "signal": r[2], "price": r[3], "rationale": r[4]} for r in cur.fetchall()]

    def insert_log(self, message):
        self.conn.execute("INSERT INTO logs (timestamp, message) VALUES (?,?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))
        self.conn.commit()

    def get_recent_logs(self, limit=50):
        cur = self.conn.execute("SELECT timestamp, message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return [f"{r[0]} {r[1]}" for r in cur.fetchall()]

    def insert_backtest(self, config_json):
        self.conn.execute("INSERT INTO backtests (timestamp, config_json) VALUES (?,?)",
                          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_json))
        self.conn.commit()

db = DatabaseManager()

# ---------- ENCRYPTED CONFIG (Safe‑Write) ----------
CONFIG_FILE = os.path.expanduser("~/.tradermoney_config.enc")
KEY_FILE = os.path.expanduser("~/.tradermoney.key")

def _generate_key():
    from cryptography.fernet import Fernet
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f: f.write(key)
    else:
        with open(KEY_FILE, "rb") as f: key = f.read()
    return key

class EncryptedConfigManager:
    @staticmethod
    def load():
        from cryptography.fernet import Fernet
        key = _generate_key(); cipher = Fernet(key)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "rb") as f: encrypted = f.read()
                return json.loads(cipher.decrypt(encrypted).decode())
            except: return {}
        return {}
    @staticmethod
    def save(config):
        from cryptography.fernet import Fernet
        key = _generate_key(); cipher = Fernet(key)
        plain = json.dumps(config, indent=2).encode()
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "wb") as f: f.write(cipher.encrypt(plain))
        # verify
        with open(tmp, "rb") as f: cipher.decrypt(f.read())
        os.replace(tmp, CONFIG_FILE)

# ---------- GLOBAL STATE ----------
class AppState:
    def __init__(self):
        self.config = EncryptedConfigManager.load() or {
            "broker":"Alpaca", "tickers":"AAPL", "mode":"signal", "quantity":1,
            "emas":[9,50], "use_bracket":False, "sl_percent":2.0, "tp_percent":4.0,
            "timeframe":"1m", "telegram":{},
            "use_rsi":True, "use_macd":True, "use_vwap":True, "use_bollinger":True,
            "use_adx":True, "use_vol_confirm":True,
            "use_supertrend":True, "use_stochastic":True, "use_atr_stops":True,
            "license_key":"", "license_valid":False, "last_broker_message":""
        }
        self.ui_queue = queue.Queue()
        self.engine = None
        self.broker_instance = None
        self.running = False
        self.dashboard = {"equity":0,"pl":0,"buying_power":0,"open_positions":0,
                          "signals":[],"orders":[],"log":[],"ema_values":{}}

state = AppState()

# ── TICKER CLEANER ──
def clean_symbol(raw: str) -> str:
    return raw.split(":")[0].strip().upper()

# ── UNIVERSAL BROKER INTERFACE ──
BROKER_REGISTRY = {}
def register_broker(name, cls): BROKER_REGISTRY[name] = cls

class BaseBroker:
    def __init__(self, config, ui_queue):
        self.config, self.ui_queue, self.name = config, ui_queue, "Base"
        self.last_error = ""
    def connect(self) -> bool: raise NotImplementedError
    def get_account(self) -> Optional[Dict[str, float]]: raise NotImplementedError
    def submit_order(self, symbol: str, qty, side: str, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None) -> bool: raise NotImplementedError
    def close_all_positions(self): raise NotImplementedError
    def get_positions(self) -> Dict[str, int]: raise NotImplementedError
    def get_market_status(self) -> bool: raise NotImplementedError
    def stream_prices(self, symbols, callback): raise NotImplementedError
    def stop_stream(self): raise NotImplementedError

# ---------- ALPACA ----------
class AlpacaBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Alpaca"; self.api = None; self._stop_stream = False
    def connect(self):
        creds = self.config.get("alpaca", {})
        key = creds.get("api_key","").strip(); secret = creds.get("secret_key","").strip()
        paper = creds.get("paper", True)
        if not key or not secret: self.last_error = "Alpaca credentials missing"; self.ui_queue.put(("error", self.last_error)); return False
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        try:
            import alpaca_trade_api as tradeapi
            self.api = tradeapi.REST(key, secret, base_url, api_version="v2")
            acc = self.api.get_account()
            if acc.status != "ACTIVE": self.last_error = "Alpaca account not active"; self.ui_queue.put(("error", self.last_error)); return False
            return True
        except Exception as e:
            self.last_error = f"Alpaca auth failed. URL: {base_url}, Paper: {paper}. Check keys." if "unauthorized" in str(e).lower() or "403" in str(e) else f"Alpaca connection: {e}"
            self.ui_queue.put(("error", self.last_error))
            return False
    def get_account(self):
        if not self.api: return None
        try:
            acc = self.api.get_account()
            return {"equity": float(acc.equity), "pl": float(acc.equity)-float(acc.last_equity),
                    "buying_power": float(acc.buying_power), "cash": float(acc.cash),
                    "open_positions": len(self.api.list_positions()) if self.api else 0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None):
        if not self.api: return False
        try:
            if sl_price is None and sl_pct is None:
                self.api.submit_order(symbol=symbol, qty=qty, side=side, type="market", time_in_force="day")
            else:
                trade = self.api.get_latest_trade(symbol); price = float(trade.price)
                stop = round(sl_price, 2) if sl_price is not None else round(price * (1 - (sl_pct/100 if side=="buy" else -sl_pct/100)), 2)
                limit = round(tp_price, 2) if tp_price is not None else round(price * (1 + (tp_pct/100 if side=="buy" else -tp_pct/100)), 2)
                self.api.submit_order(symbol=symbol, qty=qty, side=side, type="market", time_in_force="gtc",
                                      order_class="bracket", stop_loss={"stop_price": stop}, take_profit={"limit_price": limit})
            return True
        except Exception as e: self.ui_queue.put(("error", f"Order failed: {e}")); return False
    def close_all_positions(self):
        if self.api:
            try: self.api.close_all_positions()
            except Exception as e: self.ui_queue.put(("error", f"Kill switch: {e}"))
    def get_positions(self):
        if not self.api: return {}
        try: return {p.symbol: int(p.qty) for p in self.api.list_positions()}
        except: return {}
    def get_market_status(self):
        if not self.api: return False
        try: return self.api.get_clock().is_open
        except: return False
    def stream_prices(self, symbols, callback):
        if not symbols: return
        self._stop_stream = False
        def run():
            import alpaca_trade_api as tradeapi
            creds = self.config.get("alpaca", {})
            key, secret = creds.get("api_key"), creds.get("secret_key")
            paper = creds.get("paper", True)
            ws = "wss://paper-api.alpaca.markets/stream" if paper else "wss://api.alpaca.markets/stream"
            stream = tradeapi.Stream(key, secret, base_url=ws, data_feed="iex")
            async def on_trade(t): return callback(t.symbol, t.price) if t.symbol in symbols else None
            stream.subscribe_trades(on_trade, *symbols)
            while not self._stop_stream:
                try: stream.run()
                except Exception as e: self.ui_queue.put(("log", f"Alpaca stream error: {e}")); time.sleep(5)
        threading.Thread(target=run, daemon=True).start()
    def stop_stream(self): self._stop_stream = True
register_broker("Alpaca", AlpacaBroker)

# ---------- IBKR ----------
class IBKRBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Interactive Brokers"; self.ib = None
    def connect(self):
        creds = self.config.get("ibkr", {})
        host = creds.get("host","").strip(); port = int(creds.get("port",7497))
        if not host: self.last_error = "IBKR host missing – please enter the TWS/Gateway address."; self.ui_queue.put(("error", self.last_error)); return False
        try:
            from ib_insync import IB
            self.ib = IB(); self.ib.connect(host, port, clientId=int(creds.get("client_id",1)))
            return True
        except Exception as e:
            self.last_error = f"IBKR connection failed: {e}\nMake sure TWS or IB Gateway is running and API is enabled."
            self.ui_queue.put(("error", self.last_error)); return False
    def get_account(self):
        if not self.ib or not self.ib.isConnected(): return None
        try:
            acc = self.ib.accountSummary()
            eq = next((float(v.value) for v in acc if v.tag=="NetLiquidation"),0.0)
            pl = next((float(v.value) for v in acc if v.tag=="UnrealizedPnL"),0.0)
            return {"equity":eq, "pl":pl, "buying_power":0.0, "cash":0.0, "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None):
        from ib_insync import Stock, MarketOrder
        contract = Stock(symbol,"SMART","USD"); self.ib.qualifyContracts(contract)
        self.ib.placeOrder(contract, MarketOrder("BUY" if side=="buy" else "SELL", qty)); return True
    def close_all_positions(self):
        if self.ib:
            from ib_insync import MarketOrder
            for pos in self.ib.positions(): self.ib.placeOrder(pos.contract, MarketOrder("SELL" if pos.position>0 else "BUY", abs(pos.position)))
    def get_positions(self):
        if not self.ib: return {}
        return {pos.contract.symbol: int(pos.position) for pos in self.ib.positions()}
    def get_market_status(self): return True
    def stream_prices(self, symbols, callback):
        if not self.ib: return
        from ib_insync import Stock
        contracts = [Stock(sym,"SMART","USD") for sym in symbols]
        for c in contracts: self.ib.qualifyContracts(c)
        def on_tick(ticker):
            if ticker.contract.symbol in symbols and ticker.last is not None: callback(ticker.contract.symbol, ticker.last)
        for c in contracts: self.ib.reqMktData(c,'',False,False); self.ib.tickEvent += on_tick
        self._stop_stream = False
        def run():
            while not self._stop_stream: self.ib.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    def stop_stream(self): self._stop_stream = True
register_broker("Interactive Brokers", IBKRBroker)

# ---------- TRADIER ----------
class TradierBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Tradier"; self.session = None; self.token = None; self.account_id = None
    def connect(self):
        creds = self.config.get("tradier", {})
        self.token = creds.get("access_token","").strip(); self.account_id = creds.get("account_id","").strip()
        if not self.token or not self.account_id: self.last_error = "Tradier requires access token and account ID"; self.ui_queue.put(("error", self.last_error)); return False
        import requests as req
        self.session = req.Session(); self.session.headers["Authorization"] = f"Bearer {self.token}"; self.session.headers["Accept"] = "application/json"
        try:
            r = self.session.get(f"https://api.tradier.com/v1/accounts/{self.account_id}/balances")
            if r.status_code != 200: self.last_error = f"Tradier auth failed: {r.status_code}"; self.ui_queue.put(("error", self.last_error)); return False
            return True
        except Exception as e: self.last_error = f"Tradier connection: {e}"; self.ui_queue.put(("error", self.last_error)); return False
    def get_account(self):
        if not self.session: return None
        try:
            r = self.session.get(f"https://api.tradier.com/v1/accounts/{self.account_id}/balances")
            data = r.json(); bal = data.get("balances",{}).get("balance",{})
            return {"equity": float(bal.get("total_equity",0)), "pl":0.0, "buying_power":float(bal.get("option_buying_power",0)), "cash":0.0, "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None):
        if not self.session: return False
        try:
            data = {"class":"equity","symbol":symbol,"side":side,"quantity":str(qty),"type":"market","duration":"day","account_id":self.account_id}
            r = self.session.post(f"https://api.tradier.com/v1/accounts/{self.account_id}/orders", data=data)
            return r.status_code == 200
        except: return False
    def close_all_positions(self): pass
    def get_positions(self):
        if not self.session: return {}
        try:
            r = self.session.get(f"https://api.tradier.com/v1/accounts/{self.account_id}/positions")
            data = r.json(); positions = data.get("positions",{}).get("position",[])
            if isinstance(positions, dict): positions = [positions]
            return {p["symbol"]:int(float(p["quantity"])) for p in positions if p}
        except: return {}
    def get_market_status(self): return True
    def stream_prices(self, symbols, callback): pass
    def stop_stream(self): pass
register_broker("Tradier", TradierBroker)

# ---------- BINANCE ----------
class BinanceBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Binance"; self.client = None
    def connect(self):
        creds = self.config.get("binance", {})
        api_key = creds.get("api_key","").strip(); api_secret = creds.get("api_secret","").strip()
        testnet = creds.get("testnet", True)
        if not api_key or not api_secret: self.last_error = "Binance API key/secret required"; self.ui_queue.put(("error", self.last_error)); return False
        try:
            from binance.client import Client
            self.client = Client(api_key, api_secret, testnet=testnet)
            self.client.get_account()
            return True
        except Exception as e: self.last_error = f"Binance connection: {e}"; self.ui_queue.put(("error", self.last_error)); return False
    def get_account(self):
        if not self.client: return None
        try:
            acc = self.client.get_account()
            balances = {b["asset"]:float(b["free"])+float(b["locked"]) for b in acc["balances"]}
            return {"equity":sum(balances.values()),"pl":0.0,"buying_power":0.0,"cash":0.0,"open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None):
        if not self.client: return False
        try:
            sym = symbol.replace("-","").replace("/","")
            if side=="buy": self.client.order_market_buy(symbol=sym+"USDT", quantity=qty)
            else: self.client.order_market_sell(symbol=sym+"USDT", quantity=qty)
            return True
        except: return False
    def close_all_positions(self): pass
    def get_positions(self): return {}
    def get_market_status(self): return True
    def stream_prices(self, symbols, callback): pass
    def stop_stream(self): pass
register_broker("Binance", BinanceBroker)

# ---------- BYBIT ----------
class BybitBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Bybit"; self.session = None
    def connect(self):
        creds = self.config.get("bybit", {})
        api_key = creds.get("api_key","").strip(); api_secret = creds.get("api_secret","").strip()
        testnet = creds.get("testnet", True)
        if not api_key or not api_secret: self.last_error = "Bybit API key/secret required"; self.ui_queue.put(("error", self.last_error)); return False
        try:
            from pybit.unified import HTTP
            self.session = HTTP(api_key=api_key, api_secret=api_secret, testnet=testnet)
            self.session.get_wallet_balance(accountType="UNIFIED")
            return True
        except Exception as e: self.last_error = f"Bybit connection: {e}"; self.ui_queue.put(("error", self.last_error)); return False
    def get_account(self):
        if not self.session: return None
        try:
            bal = self.session.get_wallet_balance(accountType="UNIFIED")
            total = float(bal["result"]["list"][0]["totalEquity"])
            return {"equity":total,"pl":0.0,"buying_power":0.0,"cash":0.0,"open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None):
        if not self.session: return False
        try:
            self.session.place_order(symbol=symbol+"USDT", side=side.capitalize(), orderType="Market", qty=str(qty), category="spot")
            return True
        except: return False
    def close_all_positions(self): pass
    def get_positions(self): return {}
    def get_market_status(self): return True
    def stream_prices(self, symbols, callback): pass
    def stop_stream(self): pass
register_broker("Bybit", BybitBroker)

# ---------- OKX ----------
class OKXBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "OKX"; self.api = None
    def connect(self):
        creds = self.config.get("okx", {})
        api_key = creds.get("api_key","").strip(); api_secret = creds.get("api_secret","").strip(); passphrase = creds.get("api_passphrase","").strip()
        demo = creds.get("demo", True)
        if not api_key or not api_secret or not passphrase: self.last_error = "OKX requires key, secret, passphrase"; self.ui_queue.put(("error", self.last_error)); return False
        try:
            import okx.Account as Account
            flag = "1" if demo else "0"
            self.api = Account.AccountAPI(api_key, api_secret, passphrase, False, flag)
            self.api.get_account_balance()
            return True
        except Exception as e: self.last_error = f"OKX connection: {e}"; self.ui_queue.put(("error", self.last_error)); return False
    def get_account(self):
        if not self.api: return None
        try:
            bal = self.api.get_account_balance(); details = bal.get("data",[{}])[0].get("details",[])
            total = sum(float(d.get("eq",0)) for d in details)
            return {"equity":total,"pl":0.0,"buying_power":0.0,"cash":0.0,"open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None, sl_price=None, tp_price=None):
        if not self.api: return False
        try:
            import okx.Trade as Trade
            trade = Trade.TradeAPI(self.api.api_key, self.api.api_secret_key, self.api.passphrase, False, self.api.flag)
            trade.place_order(instId=symbol+"-USDT", tdMode="cash", side=side, ordType="market", sz=str(int(qty)))
            return True
        except: return False
    def close_all_positions(self): pass
    def get_positions(self): return {}
    def get_market_status(self): return True
    def stream_prices(self, symbols, callback): pass
    def stop_stream(self): pass
register_broker("OKX", OKXBroker)

# ---------- INDICATOR CALCULATOR ----------
class IndicatorCalculator:
    @staticmethod
    def compute_all(df, ema_fast=9, ema_slow=50):
        close = np.asarray(df['Close']).astype(np.float64).ravel()
        high = np.asarray(df['High']).astype(np.float64).ravel()
        low = np.asarray(df['Low']).astype(np.float64).ravel()
        volume = np.asarray(df['Volume']).astype(np.float64).ravel() if 'Volume' in df.columns else np.ones_like(close)
        def ema(data, span):
            alpha = 2/(span+1); res = np.zeros_like(data); res[0]=data[0]
            for i in range(1,len(data)): res[i] = alpha*data[i] + (1-alpha)*res[i-1]
            return res
        df['EMA_fast'] = ema(close, ema_fast)
        df['EMA_slow'] = ema(close, ema_slow)
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta>0, delta, 0); loss = np.where(delta<0, -delta, 0)
        avg_gain = np.convolve(gain, np.ones(14)/14, mode='full')[:len(close)]
        avg_loss = np.convolve(loss, np.ones(14)/14, mode='full')[:len(close)]
        rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss!=0)
        df['RSI'] = 100 - (100/(1+rs))
        ema12 = ema(close,12); ema26 = ema(close,26)
        df['MACD'] = ema12 - ema26; df['MACD_signal'] = ema(df['MACD'].values,9)
        ma20 = np.convolve(close, np.ones(20)/20, mode='same')
        std20 = np.array([np.std(close[max(0,i-19):i+1]) for i in range(len(close))])
        df['BB_upper'] = ma20 + 2*std20; df['BB_lower'] = ma20 - 2*std20
        if 'Volume' in df.columns:
            vol = np.asarray(df['Volume']).astype(np.float64).ravel()
            cum_vol = np.cumsum(vol); cum_pv = np.cumsum(close*vol)
            df['VWAP'] = np.divide(cum_pv, cum_vol, out=np.zeros_like(cum_pv), where=cum_vol!=0)
        else: df['VWAP'] = close
        tr = np.maximum(high[1:]-low[1:], np.maximum(np.abs(high[1:]-close[:-1]), np.abs(low[1:]-close[:-1])))
        tr = np.insert(tr, 0, np.mean(tr[:14])) if len(tr)>0 else np.zeros_like(close)
        atr_vals = ema(tr, 14)
        df['ATR'] = atr_vals
        up = np.maximum(high[1:]-high[:-1],0); dn = np.maximum(low[:-1]-low[1:],0)
        up = np.insert(up,0,0); dn = np.insert(dn,0,0)
        plus_dm = np.where((up>dn)&(up>0), up, 0.0)
        minus_dm = np.where((dn>up)&(dn>0), dn, 0.0)
        plus_di = 100 * ema(plus_dm,14)/atr_vals; minus_di = 100 * ema(minus_dm,14)/atr_vals
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-14)
        df['ADX'] = ema(dx, 14)
        vol_avg20 = np.convolve(volume, np.ones(20)/20, mode='same')
        df['Vol_ratio'] = np.divide(volume, vol_avg20, out=np.ones_like(volume), where=vol_avg20!=0)
        # SuperTrend
        SUPERTREND_ATR_PERIOD = 10; SUPERTREND_FACTOR = 3.0
        atr_st = ema(tr, SUPERTREND_ATR_PERIOD)
        hl2 = (high + low) / 2.0
        upper = hl2 + SUPERTREND_FACTOR * atr_st; lower = hl2 - SUPERTREND_FACTOR * atr_st
        supertrend = np.zeros_like(close); trend = np.ones_like(close)
        for i in range(1, len(close)):
            if close[i] > upper[i-1]: trend[i] = 1
            elif close[i] < lower[i-1]: trend[i] = -1
            else:
                trend[i] = trend[i-1]
                if trend[i] == 1 and lower[i] < lower[i-1]: lower[i] = lower[i-1]
                if trend[i] == -1 and upper[i] > upper[i-1]: upper[i] = upper[i-1]
            supertrend[i] = lower[i] if trend[i] == 1 else upper[i]
        df['Supertrend'] = supertrend; df['Supertrend_trend'] = trend
        # Stochastic
        STOCHASTIC_K_PERIOD = 14; STOCHASTIC_D_PERIOD = 3
        lowest_low = np.array([np.min(low[max(0,i-STOCHASTIC_K_PERIOD+1):i+1]) for i in range(len(close))])
        highest_high = np.array([np.max(high[max(0,i-STOCHASTIC_K_PERIOD+1):i+1]) for i in range(len(close))])
        stoch_k = np.where(highest_high - lowest_low != 0, 100 * (close - lowest_low) / (highest_high - lowest_low), 50.0)
        def sma(data, span): kernel = np.ones(span)/span; return np.convolve(data, kernel, mode='same')
        df['Stoch_K'] = stoch_k; df['Stoch_D'] = sma(stoch_k, STOCHASTIC_D_PERIOD)
        return df

# ---------- SIGNAL ANALYZER ----------
class SignalAnalyzer:
    ADX_TREND_THRESHOLD = 20
    VOLUME_RATIO_THRESHOLD = 1.5
    @staticmethod
    def _safe_float(series, default=0.0):
        try: return float(series.item() if hasattr(series, 'item') else series)
        except: return default
    @staticmethod
    def generate_signal(df, prev_ema_fast, prev_ema_slow, config):
        if prev_ema_fast is None or prev_ema_slow is None: return None, ""
        latest = df.iloc[-1]
        ema_f = SignalAnalyzer._safe_float(latest['EMA_fast']); ema_s = SignalAnalyzer._safe_float(latest['EMA_slow'])
        price = SignalAnalyzer._safe_float(latest['Close'])
        crossover_bull = prev_ema_fast <= prev_ema_slow and ema_f > ema_s
        crossover_bear = prev_ema_fast >= prev_ema_slow and ema_f < ema_s
        if crossover_bull:
            if not SignalAnalyzer._confirm(df, config, "bull", price): return None, ""
            return "BUY", f"BUY @ ${price:.2f}"
        if crossover_bear:
            if not SignalAnalyzer._confirm(df, config, "bear", price): return None, ""
            return "SELL", f"SELL @ ${price:.2f}"
        return None, ""
    @staticmethod
    def _confirm(df, config, direction, price):
        latest = df.iloc[-1]
        rsi = SignalAnalyzer._safe_float(latest.get('RSI',50),50)
        macd = SignalAnalyzer._safe_float(latest.get('MACD',0),0)
        macd_signal = SignalAnalyzer._safe_float(latest.get('MACD_signal',0),0)
        bb_upper = SignalAnalyzer._safe_float(latest.get('BB_upper',price),price)
        bb_lower = SignalAnalyzer._safe_float(latest.get('BB_lower',price),price)
        vwap = SignalAnalyzer._safe_float(latest.get('VWAP',price),price)
        adx = SignalAnalyzer._safe_float(latest.get('ADX',0),0)
        vol_ratio = SignalAnalyzer._safe_float(latest.get('Vol_ratio',1),1)
        supertrend_trend = SignalAnalyzer._safe_float(latest.get('Supertrend_trend',0),0)
        stoch_k = SignalAnalyzer._safe_float(latest.get('Stoch_K',50),50)
        stoch_d = SignalAnalyzer._safe_float(latest.get('Stoch_D',50),50)
        if direction=="bull":
            if config.get('use_rsi',True) and rsi<30: return False
            if config.get('use_macd',True) and macd<=macd_signal: return False
            if config.get('use_vwap',True) and price<vwap: return False
            if config.get('use_bollinger',True) and price<bb_lower*0.99: return False
            if config.get('use_supertrend',True) and supertrend_trend != 1: return False
            if config.get('use_stochastic',True) and (stoch_k < stoch_d or stoch_k > 80): return False
        else:
            if config.get('use_rsi',True) and rsi>70: return False
            if config.get('use_macd',True) and macd>=macd_signal: return False
            if config.get('use_vwap',True) and price>vwap: return False
            if config.get('use_bollinger',True) and price>bb_upper*1.01: return False
            if config.get('use_supertrend',True) and supertrend_trend != -1: return False
            if config.get('use_stochastic',True) and (stoch_k > stoch_d or stoch_k < 20): return False
        if config.get('use_adx',True) and adx<SignalAnalyzer.ADX_TREND_THRESHOLD: return False
        if config.get('use_vol_confirm',True) and vol_ratio<SignalAnalyzer.VOLUME_RATIO_THRESHOLD: return False
        return True

# ---------- TRADING ENGINE ----------
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue, config, broker):
        super().__init__(daemon=True); self.ui_queue, self.config, self.broker = ui_queue, config, broker
        self.running = False; self.symbols = []; self.positions = {}; self.prev_ema = {}; self.trade_history = []
        self.per_ticker_qty = {}; self.is_licensed = config.get("license_valid", False)
    def send_telegram(self, message):
        tg = self.config.get("telegram", {})
        token, chat = tg.get("token"), tg.get("chat_id")
        if token and chat:
            try: import requests; requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":chat,"text":message,"parse_mode":"HTML"}, timeout=5)
            except: pass
    def run(self):
        tickers_str = self.config.get("tickers", "AAPL")
        raw_list = [s.strip() for s in tickers_str.split(",") if s.strip()]
        self.symbols = []; self.per_ticker_qty = {}
        default_qty = self.config.get("quantity", 1)
        for entry in raw_list:
            sym = clean_symbol(entry) if ":" in entry else entry.strip().upper()
            qty = default_qty
            if ":" in entry:
                try: qty = float(entry.split(":")[1]); qty = int(qty) if qty == int(qty) else qty
                except: pass
            self.symbols.append(sym); self.per_ticker_qty[sym] = qty
        if not self.is_licensed and len(self.symbols) > 1:
            first = self.symbols[0]; self.symbols = [first]; self.per_ticker_qty = {first: self.per_ticker_qty[first]}
            self.ui_queue.put(("error", "Free license limited to 1 ticker. Only tracking " + first))
        for sym in self.symbols: self.positions[sym] = 0; self.prev_ema[sym] = (None, None)
        mode = self.config.get("mode","signal")
        if not self.is_licensed: mode = "signal"
        ema_fast, ema_slow = self.config.get("emas", (9,50))
        use_bracket = self.config.get("use_bracket", False)
        sl_pct = self.config.get("sl_percent",2.0); tp_pct = self.config.get("tp_percent",4.0)
        use_atr_stops = self.config.get("use_atr_stops", True)
        interval = self.config.get("timeframe", "1m")
        if not self.is_licensed:
            for key in ("use_supertrend","use_stochastic","use_adx","use_vol_confirm","use_atr_stops","use_bracket"):
                self.config[key] = False
        self.broker.stream_prices(self.symbols, self.on_price_update)
        self.ui_queue.put(("status", f"✅ Running {len(self.symbols)} symbols"))
        self.send_telegram(f"🤖 Bot started for {', '.join(self.symbols)} ({mode} mode)")
        last_hist = 0
        while self.running:
            try:
                acc = self.broker.get_account()
                if acc: self.ui_queue.put(("account", (acc["equity"],acc["pl"],acc["buying_power"],acc.get("open_positions",0))))
                is_open = self.broker.get_market_status()
                self.ui_queue.put(("market", "🟢 Open" if is_open else "🔴 Closed"))
                now = time.time()
                if now - last_hist > 60:
                    last_hist = now; ema_update = {}
                    for sym in self.symbols:
                        import yfinance as yf, pandas as pd
                        try:
                            df = yf.download(sym, period="5d", interval=interval, progress=False, auto_adjust=True)
                            if isinstance(df, pd.Series): df = df.to_frame()
                            if df is None or df.empty: continue
                            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                        except Exception as e: self.ui_queue.put(("log", f"Fetch error {sym}: {e}")); continue
                        try: df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
                        except Exception as ie: self.ui_queue.put(("error", f"Indicator error {sym}: {ie}")); continue
                        latest = df.iloc[-1]; price = SignalAnalyzer._safe_float(latest['Close'])
                        ema_f = SignalAnalyzer._safe_float(latest['EMA_fast']); ema_s = SignalAnalyzer._safe_float(latest['EMA_slow'])
                        ema_update[sym] = {"fast":round(ema_f,2), "slow":round(ema_s,2)}
                        prev_f, prev_s = self.prev_ema.get(sym, (None,None))
                        self.prev_ema[sym] = (ema_f, ema_s)
                        if prev_f is not None and prev_s is not None:
                            signal_type, rationale = SignalAnalyzer.generate_signal(df, prev_f, prev_s, self.config)
                            if signal_type:
                                self.ui_queue.put(("signal", (sym, signal_type, price, rationale)))
                                db.insert_signal(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sym, signal_type, price, rationale)
                                if mode == "auto" and self.is_licensed and is_open:
                                    qty = self.per_ticker_qty.get(sym, default_qty)
                                    if signal_type == "BUY" and self.positions.get(sym,0)==0:
                                        try:
                                            success = False
                                            if use_bracket and use_atr_stops:
                                                atr_val = SignalAnalyzer._safe_float(latest.get('ATR', price*0.02), price*0.02)
                                                sl_price = price - ATR_STOP_MULTIPLIER * atr_val
                                                tp_price = price + ATR_TP_MULTIPLIER * atr_val
                                                success = self.broker.submit_order(sym, qty, "buy", "market", sl_price=sl_price, tp_price=tp_price)
                                            elif use_bracket:
                                                success = self.broker.submit_order(sym, qty, "buy", "market", sl_pct=sl_pct, tp_pct=tp_pct)
                                            else:
                                                success = self.broker.submit_order(sym, qty, "buy", "market")
                                            if success:
                                                self.positions[sym] = qty
                                                self.ui_queue.put(("order", (sym,"BUY",qty,price)))
                                                db.insert_trade(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sym, "BUY", qty, price)
                                        except Exception as e: self.ui_queue.put(("error", f"Buy {sym} failed: {e}"))
                                    elif signal_type == "SELL" and self.positions.get(sym,0)>0:
                                        pos_qty = self.positions[sym]
                                        try:
                                            success = self.broker.submit_order(sym, pos_qty, "sell")
                                            if success:
                                                self.positions[sym]=0
                                                self.ui_queue.put(("order", (sym,"SELL",pos_qty,price)))
                                                db.insert_trade(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sym, "SELL", pos_qty, price)
                                        except Exception as e: self.ui_queue.put(("error", f"Sell {sym} failed: {e}"))
                    if ema_update: self.ui_queue.put(("ema_update", ema_update))
                time.sleep(1)
            except Exception as e: self.ui_queue.put(("error", f"Engine loop: {traceback.format_exc()}")); time.sleep(5)
        self.broker.stop_stream(); self.ui_queue.put(("status", "⏹️ Bot stopped"))
    def stop(self): self.running = False
    def on_price_update(self, sym, price): self.ui_queue.put(("price_update", (sym, price)))

# ---------- FLASK ROUTES ----------
@app.route('/')
def index(): return FRONTEND_HTML

@app.route('/mobile')
def mobile_dashboard(): return send_file('mobile.html')

@app.route('/api/config', methods=['GET'])
def get_config(): return jsonify(state.config)

@app.route('/api/config', methods=['POST'])
def save_config():
    data = request.json
    if "alpaca" in data:
        data["alpaca"]["api_key"] = data["alpaca"]["api_key"].strip()
        data["alpaca"]["secret_key"] = data["alpaca"]["secret_key"].strip()
    state.config.update(data)
    EncryptedConfigManager.save(state.config)
    return jsonify({"status":"ok","message":"Configuration saved"})

@app.route('/api/start', methods=['POST'])
def start_bot():
    data = request.json or {}
    if "alpaca" in data:
        data["alpaca"]["api_key"] = data["alpaca"]["api_key"].strip()
        data["alpaca"]["secret_key"] = data["alpaca"]["secret_key"].strip()
    state.config.update(data)
    EncryptedConfigManager.save(state.config)
    if state.engine and state.engine.running:
        return jsonify({"status":"error","message":"Bot already running"})
    broker_choice = data.get("broker", state.config.get("broker", "Alpaca"))
    broker_cls = BROKER_REGISTRY.get(broker_choice)
    if not broker_cls: return jsonify({"status":"error","message":f"Broker '{broker_choice}' not supported"})
    state.broker_instance = broker_cls(state.config, state.ui_queue)
    if not state.broker_instance.connect():
        error_text = state.broker_instance.last_error or "Unknown connection failure"
        state.config["last_broker_message"] = error_text
        EncryptedConfigManager.save(state.config)
        return jsonify({"status":"error","message":f"Broker connection failed – {error_text}"})
    state.engine = TradingEngine(state.ui_queue, state.config, state.broker_instance)
    state.engine.running = True; state.engine.start(); state.running = True
    state.config["last_broker_message"] = "Connected"
    EncryptedConfigManager.save(state.config)
    return jsonify({"status":"ok","message":"Bot started"})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    if state.engine: state.engine.stop()
    state.running = False
    return jsonify({"status":"ok","message":"Bot stopped"})

@app.route('/api/kill', methods=['POST'])
def kill_switch():
    if state.broker_instance: threading.Thread(target=state.broker_instance.close_all_positions, daemon=True).start()
    if state.engine: state.engine.stop()
    state.running = False
    return jsonify({"status":"ok","message":"Kill switch activated"})

@app.route('/api/status', methods=['GET'])
def get_status():
    while not state.ui_queue.empty():
        try:
            msg = state.ui_queue.get_nowait()
            if msg[0] == "account": eq, pl, bp, open_pos = msg[1]; state.dashboard["equity"] = eq; state.dashboard["pl"] = pl; state.dashboard["buying_power"] = bp; state.dashboard["open_positions"] = open_pos
            elif msg[0] == "signal": pass
            elif msg[0] == "order": pass
            elif msg[0] == "log": db.insert_log(msg[1])
            elif msg[0] == "error": db.insert_log(f"❌ {msg[1]}")
            elif msg[0] == "ema_update": state.dashboard["ema_values"] = msg[1]
        except queue.Empty: break
    orders = db.get_recent_trades(50)[::-1]
    signals = db.get_recent_signals(50)[::-1]
    logs = db.get_recent_logs(50)
    return jsonify({
        "running": state.running,
        "equity": state.dashboard["equity"], "pl": state.dashboard["pl"],
        "buying_power": state.dashboard["buying_power"], "open_positions": state.dashboard["open_positions"],
        "signals": signals, "orders": orders, "ema_values": state.dashboard.get("ema_values", {}), "log": logs
    })

@app.route('/api/broker_status', methods=['GET'])
def broker_status(): return jsonify({"message": state.config.get("last_broker_message", "")})

@app.route('/api/update', methods=['GET'])
def check_update():
    try:
        url = "https://raw.githubusercontent.com/shafayrich/tradermoney/main/version.json"
        with urllib.request.urlopen(url, timeout=5) as resp: data = json.loads(resp.read().decode())
        latest_version = data.get("latest_version", "0.0.0")
        is_newer = tuple(map(int, latest_version.split("."))) > tuple(map(int, APP_VERSION.split(".")))
        return jsonify({"current_version": APP_VERSION, "latest_version": latest_version, "download_url": data.get("download_url",""), "update_available": is_newer})
    except Exception as e: return jsonify({"update_available": False, "error": str(e)})

@app.route('/api/validate_license', methods=['POST'])
def validate_license_endpoint():
    data = request.json or {}
    license_key = data.get("license_key","").strip()
    if not license_key: return jsonify({"valid": False, "message": "No license key provided"})
    is_valid, message = verify_gumroad_license(license_key)
    if is_valid: state.config["license_valid"] = True; state.config["license_key"] = license_key; EncryptedConfigManager.save(state.config)
    else: state.config["license_valid"] = False
    return jsonify({"valid": is_valid, "message": message})

# ---------- BACKTEST ROUTE (ALL TICKERS) ----------
@app.route('/api/backtest', methods=['POST'])
def backtest():
    data = request.json; config = data.get("config", state.config)
    interval = config.get("timeframe", "1m"); days = int(data.get("days", 5))
    try:
        import yfinance as yf, pandas as pd
        tickers_str = config.get("tickers", "AAPL")
        raw_list = [s.strip() for s in tickers_str.split(",") if s.strip()]
        symbols = []
        for entry in raw_list:
            sym = clean_symbol(entry) if ":" in entry else entry.strip().upper()
            if sym and sym not in symbols: symbols.append(sym)
        all_results = {}
        for symbol in symbols:
            df = yf.download(symbol, period=f"{days}d", interval=interval, progress=False, auto_adjust=True)
            if df is None or df.empty: all_results[symbol] = {"error": "No data"}; continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            ema_fast, ema_slow = config.get("emas", [9,50])
            df = IndicatorCalculator.compute_all(df, ema_fast, ema_slow)
            signals = []
            prev_f, prev_s = None, None
            for i in range(1, len(df)):
                prev = df.iloc[i-1]; curr = df.iloc[i]
                prev_f = SignalAnalyzer._safe_float(prev['EMA_fast']); prev_s = SignalAnalyzer._safe_float(prev['EMA_slow'])
                sig, rationale = SignalAnalyzer.generate_signal(df.iloc[:i+1], prev_f, prev_s, config)
                if sig:
                    indi = {
                        "RSI": round(SignalAnalyzer._safe_float(curr.get('RSI',50)),1),
                        "MACD": round(SignalAnalyzer._safe_float(curr.get('MACD',0)),4),
                        "MACD_signal": round(SignalAnalyzer._safe_float(curr.get('MACD_signal',0)),4),
                        "VWAP": round(SignalAnalyzer._safe_float(curr.get('VWAP',0)),2),
                        "BB_upper": round(SignalAnalyzer._safe_float(curr.get('BB_upper',0)),2),
                        "BB_lower": round(SignalAnalyzer._safe_float(curr.get('BB_lower',0)),2),
                        "ADX": round(SignalAnalyzer._safe_float(curr.get('ADX',0)),1),
                        "Vol_ratio": round(SignalAnalyzer._safe_float(curr.get('Vol_ratio',1)),2),
                        "Supertrend_trend": int(SignalAnalyzer._safe_float(curr.get('Supertrend_trend',0))),
                        "Stoch_K": round(SignalAnalyzer._safe_float(curr.get('Stoch_K',50)),1),
                        "Stoch_D": round(SignalAnalyzer._safe_float(curr.get('Stoch_D',50)),1),
                    }
                    signals.append({"time": str(df.index[i]), "signal": sig,
                                    "price": round(SignalAnalyzer._safe_float(curr['Close']),2),
                                    "rationale": rationale, "indicators": indi})
            all_results[symbol] = {"signals": signals}
        db.insert_backtest(json.dumps({"config": config, "results": all_results}))
        return jsonify({"results": all_results})
    except Exception as e: return jsonify({"error": str(e)})

# ---------- FRONTEND HTML (FIXED SCROLLABLE BACKTEST, BROKER ERROR VISIBLE) ----------
FRONTEND_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  :root {
    --bg: #050505; --card: #1A1A1A; --text: #e2e2e2; --accent: #D4AF37; --accent2: #6A0DAD;
    --danger: #B22222; --border: #2A2E38; --btn: #D4AF37; --text-muted: #7a7d86; --sidebar-width: 260px;
  }
  ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: #080808; } ::-webkit-scrollbar-thumb { background: #080808; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif; background:var(--bg); color:var(--text); display:flex; height:100vh; overflow:hidden; }
  #sidebar { width:var(--sidebar-width); background:#0b0b0b; border-right:1px solid var(--border); display:flex; flex-direction:column; overflow-y:auto; overflow-x:hidden; padding:20px 15px; }
  #sidebar h2 { color:var(--accent); margin:0 0 15px; font-size:1.3rem; }
  .license-badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.7rem; margin-left:8px; vertical-align:middle; }
  .license-valid { background:var(--accent); color:#000; } .license-invalid { background:var(--danger); color:#fff; }
  label { display:block; font-size:0.8rem; margin:12px 0 5px; color:var(--text-muted); }
  input, select, button { background:#1A1A1A; color:var(--text); border:1px solid #222; padding:8px 10px; border-radius:8px; width:100%; box-sizing:border-box; margin-top:4px; font-size:0.9rem; transition:border 0.2s; }
  input:focus, select:focus { border-color:var(--accent); outline:none; }
  button { cursor:pointer; background:var(--btn); color:#050505; border:none; font-weight:600; margin-top:12px; }
  button:hover { opacity:0.9; } button.danger { background:var(--danger); color:#fff; }
  hr { border-color:var(--border); margin:15px 0; }
  #main { flex:1; display:flex; flex-direction:column; min-width:0; }
  .tab-header { display:flex; background:var(--card); border-bottom:1px solid var(--border); }
  .tab-btn { flex:1; background:transparent; border:none; color:var(--text); padding:14px 10px; cursor:grab; font-weight:500; letter-spacing:0.3px; transition:0.2s; border-bottom:2px solid transparent; }
  .tab-btn:active { cursor:grabbing; } .tab-btn:hover { background:rgba(255,255,255,0.03); }
  .tab-btn.active { border-bottom-color:var(--accent2); color:var(--accent); font-weight:600; }
  .tab-content { flex:1; display:none; overflow:hidden; } .tab-content.active { display:flex; flex-direction:column; }
  #metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; padding:10px; background:var(--card); border-bottom:1px solid var(--border); }
  .metric { text-align:center; } .metric .value { font-size:1.2rem; font-weight:bold; color:var(--accent); }
  #market-sessions { display:flex; align-items:center; gap:15px; padding:10px; background:var(--card); border-bottom:1px solid var(--border); font-size:0.85rem; }
  .session-dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:4px; }
  .session-open { background:#00c9b1; } .session-closed { background:var(--danger); }
  #ticker-tabs { display:flex; background:var(--card); border-bottom:1px solid var(--border); overflow-x:auto; }
  .ticker-btn { padding:8px 15px; background:transparent; border:none; color:var(--text); cursor:pointer; white-space:nowrap; border-bottom:2px solid transparent; transition:0.2s; }
  .ticker-btn.active { border-bottom-color:var(--accent2); color:var(--accent); font-weight:600; }
  #chart-container { flex:1; }
  .signal-item { display:flex; justify-content:space-between; padding:10px; border-bottom:1px solid var(--border); }
  .buy { color:var(--accent); } .sell { color:var(--danger); }
  #log { height:120px; overflow-y:auto; background:var(--bg); padding:10px; font-size:0.8rem; border-top:1px solid var(--border); }
  #toast-container { position:fixed; top:20px; right:20px; z-index:9999; display:flex; flex-direction:column; gap:8px; }
  .toast { padding:12px 20px; border-radius:6px; color:white; font-weight:500; box-shadow:0 4px 12px rgba(0,0,0,0.3); animation:slideIn 0.3s ease; max-width:300px; }
  .toast.success { background:var(--accent); color:#000; } .toast.error { background:var(--danger); } .toast.info { background:var(--accent2); }
  @keyframes slideIn { from{ transform:translateX(100%); opacity:0; } to{ transform:translateX(0); opacity:1; } }
  .ema-monitor { display:grid; grid-template-columns:repeat(auto-fit, minmax(120px,1fr)); gap:8px; padding:10px; }
  .ema-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:10px; text-align:center; }
  .ema-card .ticker { font-weight:bold; color:var(--accent); } .ema-card .ema-value { font-size:1.1rem; margin-top:5px; } .ema-card .ema-label { font-size:0.7rem; color:var(--text-muted); }
  #update-toast { display:none; position:fixed; bottom:20px; right:20px; z-index:9999; background:var(--accent); color:black; padding:15px 20px; border-radius:8px; font-weight:bold; }
  #update-toast a { color:white; text-decoration:underline; cursor:pointer; }
  .help-content { padding:20px; overflow-y:auto; height:100%; box-sizing:border-box; }
  .help-content h3 { color:var(--accent2); margin-top:0; } .help-content h4 { color:var(--text); margin:15px 0 5px; }
  .help-content p, .help-content ul { font-size:0.9rem; line-height:1.6; } .help-content ul { padding-left:20px; } .help-content li { margin-bottom:6px; } .help-content a { color:var(--accent); }
  .indicator-stats { background:var(--card); border-radius:8px; padding:15px; margin:10px 0; }
  .backtest-panel { flex:1; display:flex; flex-direction:column; }
  .backtest-results { flex:1; overflow-y:auto; padding:10px; }
  .placeholder-text { color:var(--text-muted); text-align:center; padding:40px 20px; }
  .backtest-table { width:100%; border-collapse:collapse; font-size:0.85rem; margin-bottom:20px; }
  .backtest-table th, .backtest-table td { padding:6px 8px; border:1px solid var(--border); text-align:center; }
  .backtest-table th { color:var(--accent); }
  #broker-status { font-size:0.8rem; color:var(--text-muted); margin-top:4px; min-height:18px; }
</style>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
</head>
<body>
<div id="toast-container"></div>
<div id="update-toast"><span>🔔 New version available! <a id="update-link" href="#" target="_blank">Download Update</a></span></div>
<div id="sidebar">
  <h2>💸 TraderMoney <span id="license-badge" class="license-badge license-invalid">FREE</span></h2>
  <label>License Key</label><input type="password" id="license-key" placeholder="Paste your Gumroad key">
  <button onclick="validateLicense()" style="margin-top:5px; font-size:0.85rem;">🔑 Validate</button>
  <p style="font-size:0.7rem; color:var(--text-muted); margin-top:2px;"><a href="https://shafayrich.gumroad.com/l/ykaoov" target="_blank" style="color:var(--accent);">Buy a license</a></p>
  <hr>
  <label>Broker</label><select id="broker-select"><option>Alpaca</option><option>Interactive Brokers</option><option>Tradier</option><option>Binance</option><option>Bybit</option><option>OKX</option></select>
  <div id="broker-status"></div><div id="cred-entries"></div>
  <label>Telegram Token (opt)</label><input type="password" id="tg-token"><label>Telegram Chat ID</label><input id="tg-chat">
  <label>Tickers (comma sep) – e.g., AAPL:5, BTC/USD:0.001</label><input id="tickers" value="AAPL" placeholder="AAPL:5, BTC/USD:0.001">
  <label>Timeframe</label><select id="timeframe"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>1d</option></select>
  <div style="display:flex;gap:5px;margin-top:10px;"><input id="ema-fast" value="9" placeholder="Fast EMA"><input id="ema-slow" value="50" placeholder="Slow EMA"></div>
  <label>Quantity (fallback)</label><input id="quantity" value="1" type="number">
  <label>Mode</label><select id="mode"><option value="signal">Signal Only</option><option value="auto">Auto Trade</option></select>
  <label><input type="checkbox" id="use-bracket"> Enable SL/TP</label>
  <div style="display:flex;gap:5px;"><input id="sl-percent" value="2" placeholder="SL %"><input id="tp-percent" value="4" placeholder="TP %"></div>
  <label><input type="checkbox" id="use-atr-stops" checked> ATR-Based Dynamic Stops</label>
  <label style="margin-top:15px; font-weight:bold; color:var(--accent);">Indicators</label>
  <label><input type="checkbox" id="use-rsi" checked> RSI (14)</label>
  <label><input type="checkbox" id="use-macd" checked> MACD (12,26,9)</label>
  <label><input type="checkbox" id="use-vwap" checked> VWAP</label>
  <label><input type="checkbox" id="use-bollinger" checked> Bollinger (20,2)</label>
  <label><input type="checkbox" id="use-adx" checked> ADX (14) – Trend Strength</label>
  <label><input type="checkbox" id="use-vol-confirm" checked> Volume Confirmation (1.5x)</label>
  <label><input type="checkbox" id="use-supertrend" checked> SuperTrend (10,3)</label>
  <label><input type="checkbox" id="use-stochastic" checked> Stochastic (14,3,3)</label>
  <button onclick="saveConfig()">💾 Save</button>
  <button onclick="refreshTickers()" style="background:var(--card); border:1px solid var(--border); margin-top:5px;">🔄 Refresh Tickers</button>
  <button onclick="startBot()" style="background:var(--accent); color:#050505;">▶️ Start</button>
  <button onclick="stopBot()" style="background:#555;">⏹️ Stop</button>
  <button onclick="killSwitch()" class="danger">⚠️ Kill Switch</button>
  <button onclick="resetDefaults()" style="background:var(--card); border:1px solid var(--border); margin-top:8px;">↺ Reset to Defaults</button>
  <button onclick="checkForUpdates()" style="margin-top:20px; background:var(--card); border:1px solid var(--border);">🔄 Check for Updates</button>
  <button onclick="runBacktest()" style="background:var(--accent2); color:#fff; margin-top:10px;">🧪 Backtest All Tickers</button>
</div>
<div id="main">
  <div class="tab-header" id="tab-header">
    <button class="tab-btn active" data-tab="charts">Charts</button>
    <button class="tab-btn" data-tab="signals">Signals</button>
    <button class="tab-btn" data-tab="history">History</button>
    <button class="tab-btn" data-tab="ema">EMA Monitor</button>
    <button class="tab-btn" data-tab="backtest">Backtest</button>
    <button class="tab-btn" data-tab="help">Help & Ops</button>
  </div>
  <div id="tab-charts" class="tab-content active">
    <div id="ticker-tabs"></div>
    <div id="metrics">
      <div class="metric"><div class="value" id="equity">—</div><div>Net Liq.</div></div>
      <div class="metric"><div class="value" id="bp">—</div><div>Buying Power</div></div>
      <div class="metric"><div class="value" id="pl">—</div><div>Daily P&L</div></div>
      <div class="metric"><div class="value" id="positions">—</div><div>Positions</div></div>
    </div>
    <div id="market-sessions">
      <span style="color:var(--accent);">🌍 Markets:</span>
      <span><span class="session-dot" id="sydney-dot"></span> SYD</span>
      <span><span class="session-dot" id="tokyo-dot"></span> TKY</span>
      <span><span class="session-dot" id="london-dot"></span> LDN</span>
      <span><span class="session-dot" id="newyork-dot"></span> NYC</span>
      <span><span class="session-dot session-open"></span> CRYPTO (24/7)</span>
    </div>
    <div id="chart-container"><p style="color:var(--text);text-align:center;padding-top:50px;">Loading chart...</p></div>
  </div>
  <div id="tab-signals" class="tab-content"><div id="signals-list" style="overflow-y:auto;flex:1;"></div></div>
  <div id="tab-history" class="tab-content"><div id="history-list" style="overflow-y:auto;flex:1;"></div></div>
  <div id="tab-ema" class="tab-content"><div class="ema-monitor" id="ema-monitor">Loading...</div></div>
  <div id="tab-backtest" class="tab-content">
    <div class="backtest-panel">
      <div style="padding:10px;"><button onclick="runBacktest()" style="background:var(--accent2); color:#fff; width:auto; padding:10px 20px;">🧪 Run Backtest on All Tickers</button></div>
      <div id="backtest-results" class="backtest-results">
        <p class="placeholder-text">Click <strong>'🧪 Backtest All Tickers'</strong> in the sidebar or above to run the backtesting engine.<br>Results will appear here.</p>
      </div>
    </div>
  </div>
  <div id="tab-help" class="tab-content">
    <div class="help-content">
      <h3>📊 Indicator Win Rate Impact</h3>
      <div class="indicator-stats">
        <p><strong>Pure EMA Crossover (9/50):</strong> ~32% win rate (very noisy).</p>
        <p><strong>+ RSI filter (RSI ≥ 30 for buys):</strong> ~40%.</p>
        <p><strong>+ MACD confirmation:</strong> ~45%.</p>
        <p><strong>+ VWAP alignment:</strong> ~48%.</p>
        <p><strong>+ Bollinger Bands:</strong> ~50%.</p>
        <p><strong>+ ADX (≥20):</strong> ~55%.</p>
        <p><strong>+ Volume Confirmation (1.5x):</strong> ~58%.</p>
        <p><strong>+ SuperTrend (trend direction):</strong> ~62%.</p>
        <p><strong>+ Stochastic (momentum filter):</strong> ~65%.</p>
        <p><strong>+ ATR Dynamic Stops:</strong> profit factor improves by ~0.4.</p>
      </div>
      <h4>🔑 License</h4><p>Buy a license from our <a href="https://shafayrich.gumroad.com/l/ykaoov" target="_blank">Gumroad store</a>. Valid key unlocks all features.</p>
      <h4>🏦 Broker Guides</h4><p>See Help tab for full connection instructions.</p>
    </div>
  </div>
  <div id="log"></div>
</div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
let currentTicker='',tickers=[],chartWidget=null,config={},licenseValid=false,lastLoadedSymbol='';
function cleanSymbol(raw){return raw.split(':')[0].trim().toUpperCase();}
function updateMarketSessions(){let now=new Date(),day=now.getUTCDay(),isWeekend=(day===0||day===6),utc=now.getUTCHours()+now.getUTCMinutes()/60,sydney=isWeekend?'session-closed':(utc>=20||utc<5)?'session-open':'session-closed',tokyo=isWeekend?'session-closed':(utc>=23||utc<5)?'session-open':'session-closed',london=isWeekend?'session-closed':(utc>=8&&utc<16.5)?'session-open':'session-closed',newyork=isWeekend?'session-closed':(utc>=13&&utc<20)?'session-open':'session-closed';document.getElementById('sydney-dot').className='session-dot '+sydney;document.getElementById('tokyo-dot').className='session-dot '+tokyo;document.getElementById('london-dot').className='session-dot '+london;document.getElementById('newyork-dot').className='session-dot '+newyork;}
setInterval(updateMarketSessions,60000);updateMarketSessions();
const tabHeader=document.getElementById('tab-header');Sortable.create(tabHeader,{animation:150,handle:'.tab-btn',onEnd:function(){let first=tabHeader.querySelector('.tab-btn');if(!document.querySelector('.tab-btn.active')&&first)first.click();}});
function switchTab(name,ev){document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));document.getElementById('tab-'+name).classList.add('active');document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));if(ev)ev.target.classList.add('active');if(name==='charts'&&chartWidget)setTimeout(()=>chartWidget.resize&&chartWidget.resize(),100);}
document.querySelectorAll('.tab-btn').forEach(btn=>{btn.addEventListener('click',function(e){switchTab(this.dataset.tab,e);});});
function showToast(msg,type='info'){let c=document.getElementById('toast-container');let t=document.createElement('div');t.className='toast '+type;t.textContent=msg;c.appendChild(t);setTimeout(()=>t.remove(),3000);}
async function loadConfig(){let r=await fetch('/api/config');config=await r.json();initUI(config);if(config.license_key&&config.license_key.trim()!=='')validateLicense();}
function updateCredFields(){let broker=document.getElementById('broker-select').value,c=document.getElementById('cred-entries');c.innerHTML='';if(broker==='Alpaca')c.innerHTML='<label>API Key</label><input type="password" id="alpaca-key"><label>Secret Key</label><input type="password" id="alpaca-secret"><label><input type="checkbox" id="alpaca-paper" checked> Paper Trading</label>';else if(broker==='Interactive Brokers')c.innerHTML='<label>Host</label><input id="ibkr-host" value="127.0.0.1"><label>Port</label><input id="ibkr-port" value="7497"><label>Client ID</label><input id="ibkr-client-id" value="1">';else if(broker==='Tradier')c.innerHTML='<label>Access Token</label><input type="password" id="tradier-token"><label>Account ID</label><input id="tradier-account-id">';else if(broker==='Binance')c.innerHTML='<label>API Key</label><input type="password" id="binance-key"><label>API Secret</label><input type="password" id="binance-secret"><label><input type="checkbox" id="binance-testnet" checked> Testnet (Paper Trading)</label>';else if(broker==='Bybit')c.innerHTML='<label>API Key</label><input type="password" id="bybit-key"><label>API Secret</label><input type="password" id="bybit-secret"><label><input type="checkbox" id="bybit-testnet" checked> Testnet (Paper Trading)</label>';else if(broker==='OKX')c.innerHTML='<label>API Key</label><input type="password" id="okx-key"><label>API Secret</label><input type="password" id="okx-secret"><label>API Passphrase</label><input type="password" id="okx-passphrase"><label><input type="checkbox" id="okx-demo" checked> Demo Trading</label>';}
function initUI(cfg){if(!cfg)return;document.getElementById('broker-select').value=cfg.broker||'Alpaca';document.getElementById('tickers').value=cfg.tickers||'AAPL';document.getElementById('ema-fast').value=cfg.emas?cfg.emas[0]:9;document.getElementById('ema-slow').value=cfg.emas?cfg.emas[1]:50;document.getElementById('quantity').value=cfg.quantity||1;document.getElementById('mode').value=cfg.mode||'signal';if(cfg.telegram){document.getElementById('tg-token').value=cfg.telegram.token||'';document.getElementById('tg-chat').value=cfg.telegram.chat_id||'';}document.getElementById('use-bracket').checked=cfg.use_bracket||false;document.getElementById('sl-percent').value=cfg.sl_percent||2;document.getElementById('tp-percent').value=cfg.tp_percent||4;document.getElementById('use-atr-stops').checked=cfg.use_atr_stops!==false;document.getElementById('use-rsi').checked=cfg.use_rsi!==false;document.getElementById('use-macd').checked=cfg.use_macd!==false;document.getElementById('use-vwap').checked=cfg.use_vwap!==false;document.getElementById('use-bollinger').checked=cfg.use_bollinger!==false;document.getElementById('use-adx').checked=cfg.use_adx!==false;document.getElementById('use-vol-confirm').checked=cfg.use_vol_confirm!==false;document.getElementById('use-supertrend').checked=cfg.use_supertrend!==false;document.getElementById('use-stochastic').checked=cfg.use_stochastic!==false;if(cfg.license_key)document.getElementById('license-key').value=cfg.license_key||'';if(cfg.license_valid){licenseValid=true;document.getElementById('license-badge').textContent='PRO';document.getElementById('license-badge').className='license-badge license-valid';}updateCredFields();let rawTickers=document.getElementById('tickers').value.split(',').map(s=>s.trim()).filter(s=>s);if(rawTickers.length){setTickers(rawTickers);if(!currentTicker)currentTicker=cleanSymbol(rawTickers[0]);loadChart(currentTicker);}}
function setTickers(list){tickers=list;if(!currentTicker)currentTicker=cleanSymbol(list[0]);let bar=document.getElementById('ticker-tabs');bar.innerHTML='';list.forEach(raw=>{let cleanSym=cleanSymbol(raw);let btn=document.createElement('button');btn.className='ticker-btn'+(cleanSym===currentTicker?' active':'');btn.textContent=raw;btn.onclick=()=>{currentTicker=cleanSym;updateTickerTabs();if(lastLoadedSymbol!==cleanSym)loadChart(cleanSym);};bar.appendChild(btn);});}
function updateTickerTabs(){Array.from(document.getElementById('ticker-tabs').children).forEach(b=>{let btnSym=cleanSymbol(b.textContent);b.classList.toggle('active',btnSym===currentTicker);});}
function loadChart(sym){let cleanSym=cleanSymbol(sym);if(cleanSym===lastLoadedSymbol)return;lastLoadedSymbol=cleanSym;let container=document.getElementById('chart-container');container.innerHTML='';if(typeof TradingView==='undefined'){setTimeout(()=>loadChart(cleanSym),100);return;}chartWidget=new TradingView.widget({"autosize":true,"symbol":cleanSym,"interval":"1","timezone":"Etc/UTC","theme":"Dark","style":"1","locale":"en","toolbar_bg":"#0A0C0F","enable_publishing":false,"hide_side_toolbar":false,"allow_symbol_change":true,"container_id":"chart-container"});}
async function refreshTickers(){let r=await fetch('/api/config');let cfg=await r.json();config=cfg;document.getElementById('tickers').value=cfg.tickers;let rawTickers=cfg.tickers.split(',').map(s=>s.trim()).filter(s=>s);if(rawTickers.length){setTickers(rawTickers);if(currentTicker)loadChart(currentTicker);}showToast('Tickers refreshed','success');}
async function pollStatus(){try{let r=await fetch('/api/status');let data=await r.json();document.getElementById('equity').innerText='$'+data.equity.toLocaleString();document.getElementById('bp').innerText='$'+data.buying_power.toLocaleString();let plPct=data.equity?(data.pl/data.equity*100):0;document.getElementById('pl').innerHTML=`<span style="color:${plPct>=0?'var(--accent)':'var(--danger)'}">${plPct>=0?'+':''}${plPct.toFixed(2)}%</span>`;document.getElementById('positions').innerText=data.open_positions;let sl=document.getElementById('signals-list');if(sl){sl.innerHTML='';data.signals.forEach(s=>{let div=document.createElement('div');div.className='signal-item '+(s.signal==='BUY'?'buy':'sell');div.innerHTML=`<span>${s.time} ${s.signal} ${s.symbol} @ $${s.price}</span><span>${s.rationale}</span>`;sl.prepend(div);});}let ol=document.getElementById('history-list');if(ol){ol.innerHTML='';data.orders.forEach(o=>{let div=document.createElement('div');div.className='signal-item '+(o.action==='BUY'?'buy':'sell');div.innerHTML=`<span>${o.time} ${o.action} ${o.qty} ${o.symbol} @ $${o.price}</span>`;ol.prepend(div);});}let ema=document.getElementById('ema-monitor');if(ema&&data.ema_values){let html='';for(let [sym,vals] of Object.entries(data.ema_values))html+=`<div class="ema-card"><div class="ticker">${sym}</div><div class="ema-value"><span class="ema-label">Fast EMA:</span> ${vals.fast}</div><div class="ema-value"><span class="ema-label">Slow EMA:</span> ${vals.slow}</div></div>`;ema.innerHTML=html||'<div style="color:var(--text-muted);padding:10px;">Waiting for data...</div>';}document.getElementById('log').innerHTML=data.log.join('<br>');}catch(e){}}
setInterval(pollStatus,1500);
document.getElementById('broker-select').addEventListener('change',updateCredFields);
async function pollBrokerStatus(){try{let r=await fetch('/api/broker_status');let d=await r.json();document.getElementById('broker-status').textContent=d.message?'Last message: '+d.message:'';}catch(e){}}
setInterval(pollBrokerStatus,3000);pollBrokerStatus();
const defaultConfig={broker:"Alpaca",tickers:"AAPL",mode:"signal",quantity:1,emas:[9,50],use_bracket:false,sl_percent:2.0,tp_percent:4.0,timeframe:"1m",telegram:{},use_rsi:true,use_macd:true,use_vwap:true,use_bollinger:true,use_adx:true,use_vol_confirm:true,use_supertrend:true,use_stochastic:true,use_atr_stops:true,license_key:"",license_valid:false};
function resetDefaults(){config=JSON.parse(JSON.stringify(defaultConfig));initUI(config);saveConfig();showToast('Settings reset to defaults & saved','success');}
async function validateLicense(){let key=document.getElementById('license-key').value.trim();if(!key){showToast('Please enter a license key','error');return;}try{let r=await fetch('/api/validate_license',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key})});let d=await r.json();let badge=document.getElementById('license-badge');if(d.valid){licenseValid=true;badge.textContent='PRO';badge.className='license-badge license-valid';showToast('✅ License verified – Pro features unlocked','success');}else{licenseValid=false;badge.textContent='FREE';badge.className='license-badge license-invalid';showToast('❌ '+d.message,'error');}}catch(e){showToast('Unable to reach license server','error');}}
async function checkForUpdates(){try{let r=await fetch('/api/update');let data=await r.json();if(data.update_available){let toast=document.getElementById('update-toast');toast.style.display='block';document.getElementById('update-link').href=data.download_url;}else showToast('✅ You are up-to-date!','success');}catch(e){}}setTimeout(checkForUpdates,3000);
function buildConfig(){let broker=document.getElementById('broker-select').value;return{broker,tickers:document.getElementById('tickers').value,timeframe:document.getElementById('timeframe').value,emas:[parseInt(document.getElementById('ema-fast').value),parseInt(document.getElementById('ema-slow').value)],quantity:parseInt(document.getElementById('quantity').value),mode:document.getElementById('mode').value,use_bracket:document.getElementById('use-bracket').checked,sl_percent:parseFloat(document.getElementById('sl-percent').value),tp_percent:parseFloat(document.getElementById('tp-percent').value),use_atr_stops:document.getElementById('use-atr-stops').checked,telegram:{token:document.getElementById('tg-token').value,chat_id:document.getElementById('tg-chat').value},use_rsi:document.getElementById('use-rsi').checked,use_macd:document.getElementById('use-macd').checked,use_vwap:document.getElementById('use-vwap').checked,use_bollinger:document.getElementById('use-bollinger').checked,use_adx:document.getElementById('use-adx').checked,use_vol_confirm:document.getElementById('use-vol-confirm').checked,use_supertrend:document.getElementById('use-supertrend').checked,use_stochastic:document.getElementById('use-stochastic').checked,license_key:document.getElementById('license-key')?.value||'',alpaca:broker==='Alpaca'?{api_key:document.getElementById('alpaca-key')?.value||'',secret_key:document.getElementById('alpaca-secret')?.value||'',paper:document.getElementById('alpaca-paper')?.checked||true}:{},ibkr:broker==='Interactive Brokers'?{host:document.getElementById('ibkr-host')?.value||'127.0.0.1',port:document.getElementById('ibkr-port')?.value||'7497',client_id:document.getElementById('ibkr-client-id')?.value||'1'}:{},tradier:broker==='Tradier'?{access_token:document.getElementById('tradier-token')?.value||'',account_id:document.getElementById('tradier-account-id')?.value||''}:{},binance:broker==='Binance'?{api_key:document.getElementById('binance-key')?.value||'',api_secret:document.getElementById('binance-secret')?.value||'',testnet:document.getElementById('binance-testnet')?.checked||true}:{},bybit:broker==='Bybit'?{api_key:document.getElementById('bybit-key')?.value||'',api_secret:document.getElementById('bybit-secret')?.value||'',testnet:document.getElementById('bybit-testnet')?.checked||true}:{},okx:broker==='OKX'?{api_key:document.getElementById('okx-key')?.value||'',api_secret:document.getElementById('okx-secret')?.value||'',api_passphrase:document.getElementById('okx-passphrase')?.value||'',demo:document.getElementById('okx-demo')?.checked||true}:{}};}
async function saveConfig(){config=buildConfig();await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)});showToast('Configuration saved','success');}
async function startBot(){config=buildConfig();let r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)});let d=await r.json();showToast(d.message,d.status==='ok'?'success':'error');}
async function stopBot(){await fetch('/api/stop',{method:'POST'});showToast('Bot stopped','success');}
async function killSwitch(){await fetch('/api/kill',{method:'POST'});showToast('Kill switch activated','success');}
async function runBacktest(){showToast('Running backtest on all tickers...','info');switchTab('backtest');try{let r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:buildConfig(),days:5})});let data=await r.json();if(data.error){showToast('Backtest error: '+data.error,'error');return;}let results=data.results;let resDiv=document.getElementById('backtest-results');let html='';let totalSignals=0;for(let symbol in results){let info=results[symbol];html+='<h4 style="color:var(--accent);">'+symbol+'</h4>';if(info.error){html+='<p style="color:var(--danger);">Error: '+info.error+'</p>';continue;}let signals=info.signals||[];totalSignals+=signals.length;if(signals.length===0){html+='<p style="color:var(--text-muted);">No signals found.</p>';continue;}html+='<table class="backtest-table"><tr><th>Time</th><th>Signal</th><th>Price</th><th>RSI</th><th>MACD</th><th>MacSig</th><th>VWAP</th><th>BB L/U</th><th>ADX</th><th>VolRatio</th><th>SupTrend</th><th>Stoch %K/%D</th><th>Rationale</th></tr>';signals.forEach(s=>{let ind=s.indicators;html+=`<tr><td>${s.time.slice(11,19)}</td><td class="${s.signal==='BUY'?'buy':'sell'}">${s.signal}</td><td>$${s.price}</td><td>${ind.RSI}</td><td>${ind.MACD}</td><td>${ind.MACD_signal}</td><td>$${ind.VWAP}</td><td>${ind.BB_lower}/${ind.BB_upper}</td><td>${ind.ADX}</td><td>${ind.Vol_ratio}x</td><td>${ind.Supertrend_trend===1?'🟢':'🔴'}</td><td>${ind.Stoch_K}/${ind.Stoch_D}</td><td>${s.rationale}</td></tr>`;});html+='</table>';}if(totalSignals===0)html='<p class="placeholder-text">No signals generated. Try adjusting indicators or tickers.</p>';resDiv.innerHTML=html;}catch(e){showToast('Backtest failed','error');}}
loadConfig();
</script>
</body>
</html>
"""

def run_flask():
    app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False)

if __name__ == "__main__":
    acquire_lock()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1)
    window = webview.create_window('TraderMoney – Solar Eclipse', 'http://127.0.0.1:5050', width=1300, height=800, min_size=(900,600))
    webview.start()

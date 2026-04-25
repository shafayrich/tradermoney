"""
TraderMoney v16.0 – 6-broker support, auto‑update checker.
"""

import json, os, queue, signal, sys, socket, threading, time, traceback, atexit, urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import webview
from flask import Flask, request, jsonify
from flask_cors import CORS

# ---------- App Version (bump this on each release) ----------
APP_VERSION = "1.0.7"

# ---------- Flask App ----------
app = Flask(__name__)
CORS(app)

# ---------- PORT-BASED SINGLE INSTANCE LOCK (cross‑platform) ----------
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

# ---------- CONSTANTS ----------
CONFIG_FILE = os.path.expanduser("~/.tradermoney_config.enc")
KEY_FILE = os.path.expanduser("~/.tradermoney.key")
DEFAULT_EMAS = (9, 50)
DEFAULT_TICKERS = "AAPL"
DEFAULT_QUANTITY = 1
DEFAULT_TIMEFRAME = "1m"
ADX_TREND_THRESHOLD = 20
VOLUME_RATIO_THRESHOLD = 1.5

# ---------- ENCRYPTED CONFIG ----------
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
        with open(CONFIG_FILE, "wb") as f: f.write(cipher.encrypt(plain))

# ---------- GLOBAL STATE ----------
class AppState:
    def __init__(self):
        self.config = EncryptedConfigManager.load() or {
            "broker":"Alpaca", "tickers":"AAPL", "mode":"signal", "quantity":1,
            "emas":[9,50], "use_bracket":False, "sl_percent":2.0, "tp_percent":4.0,
            "timeframe":"1m", "telegram":{},
            "use_rsi":True, "use_macd":True, "use_vwap":True, "use_bollinger":True,
            "use_adx":True, "use_vol_confirm":True
        }
        self.ui_queue = queue.Queue()
        self.engine = None
        self.broker_instance = None
        self.running = False
        self.dashboard = {
            "equity": 0, "pl": 0, "buying_power": 0, "open_positions": 0,
            "signals": [], "orders": [], "log": [],
            "ema_values": {}
        }

state = AppState()

# ---------- BROKER REGISTRY ----------
BROKER_REGISTRY = {}

def register_broker(name, cls):
    BROKER_REGISTRY[name] = cls

# ---------- BASE BROKER ----------
class BaseBroker:
    def __init__(self, config, ui_queue): self.config, self.ui_queue, self.name = config, ui_queue, "Base"
    def connect(self) -> bool: raise NotImplementedError
    def get_account(self) -> Optional[Dict[str, float]]: raise NotImplementedError
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None) -> bool: raise NotImplementedError
    def close_all_positions(self): raise NotImplementedError
    def get_positions(self) -> Dict[str, int]: raise NotImplementedError
    def get_market_status(self) -> bool: raise NotImplementedError
    def stream_prices(self, symbols, callback): raise NotImplementedError
    def stop_stream(self): raise NotImplementedError

# ---------- ALPACA BROKER ----------
class AlpacaBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Alpaca"; self.api = None; self._stop_stream = False
    def connect(self):
        creds = self.config.get("alpaca", {})
        key = creds.get("api_key", "").strip(); secret = creds.get("secret_key", "").strip()
        paper = creds.get("paper", True)
        if not key or not secret: self.ui_queue.put(("error", "Alpaca credentials missing")); return False
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        try:
            import alpaca_trade_api as tradeapi
            self.api = tradeapi.REST(key, secret, base_url, api_version="v2")
            acc = self.api.get_account()
            if acc.status != "ACTIVE": self.ui_queue.put(("error", "Alpaca account not active")); return False
            return True
        except Exception as e:
            msg = str(e)
            if "unauthorized" in msg.lower() or "403" in msg: self.ui_queue.put(("error", f"Alpaca auth failed. URL: {base_url}, Paper: {paper}. Check keys."))
            else: self.ui_queue.put(("error", f"Alpaca connection: {msg}"))
            return False
    def get_account(self):
        if not self.api: return None
        try:
            acc = self.api.get_account()
            return {"equity": float(acc.equity), "pl": float(acc.equity)-float(acc.last_equity),
                    "buying_power": float(acc.buying_power), "cash": float(acc.cash),
                    "open_positions": len(self.api.list_positions()) if self.api else 0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None):
        if not self.api: return False
        try:
            if sl_pct is None: self.api.submit_order(symbol=symbol, qty=qty, side=side, type="market", time_in_force="day")
            else:
                trade = self.api.get_latest_trade(symbol); price = float(trade.price)
                stop = round(price * (1 - (sl_pct/100 if side=="buy" else -sl_pct/100)), 2)
                limit = round(price * (1 + (tp_pct/100 if side=="buy" else -tp_pct/100)), 2)
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
            creds = self.config.get("alpaca", {});
            key, secret = creds.get("api_key"), creds.get("secret_key")
            paper = creds.get("paper", True)
            ws = "wss://paper-api.alpaca.markets/stream" if paper else "wss://api.alpaca.markets/stream"
            stream = tradeapi.Stream(key, secret, base_url=ws, data_feed="iex")
            async def on_trade(t):
                if t.symbol in symbols: callback(t.symbol, t.price)
            stream.subscribe_trades(on_trade, *symbols)
            while not self._stop_stream:
                try: stream.run()
                except Exception as e: self.ui_queue.put(("log", f"Alpaca stream error: {e}")); time.sleep(5)
        threading.Thread(target=run, daemon=True).start()
    def stop_stream(self): self._stop_stream = True
register_broker("Alpaca", AlpacaBroker)

# ---------- INTERACTIVE BROKERS ----------
class IBKRBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Interactive Brokers"; self.ib = None
    def connect(self):
        try:
            from ib_insync import IB
            self.ib = IB()
            cfg = self.config.get("ibkr", {})
            self.ib.connect(cfg.get("host","127.0.0.1"), int(cfg.get("port",7497)), clientId=int(cfg.get("client_id",1)))
            return True
        except Exception as e: self.ui_queue.put(("error", f"IBKR connect: {e}")); return False
    def get_account(self):
        if not self.ib or not self.ib.isConnected(): return None
        try:
            acc = self.ib.accountSummary()
            eq = next((float(v.value) for v in acc if v.tag=="NetLiquidation"),0.0)
            pl = next((float(v.value) for v in acc if v.tag=="UnrealizedPnL"),0.0)
            return {"equity":eq, "pl":pl, "buying_power":0.0, "cash":0.0, "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None):
        from ib_insync import Stock, MarketOrder
        contract = Stock(symbol,"SMART","USD"); self.ib.qualifyContracts(contract)
        action = "BUY" if side=="buy" else "SELL"
        self.ib.placeOrder(contract, MarketOrder(action, qty)); return True
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

# ---------- TRADIER BROKER ----------
class TradierBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Tradier"; self.session = None; self.token = None; self.account_id = None
    def connect(self):
        creds = self.config.get("tradier", {})
        self.token = creds.get("access_token", "").strip(); self.account_id = creds.get("account_id", "").strip()
        if not self.token or not self.account_id: self.ui_queue.put(("error", "Tradier requires access token and account ID")); return False
        import requests
        self.session = requests.Session(); self.session.headers["Authorization"] = f"Bearer {self.token}"; self.session.headers["Accept"] = "application/json"
        try:
            r = self.session.get(f"https://api.tradier.com/v1/accounts/{self.account_id}/balances")
            if r.status_code != 200: self.ui_queue.put(("error", f"Tradier auth failed: {r.status_code}")); return False
            return True
        except Exception as e: self.ui_queue.put(("error", f"Tradier connection: {e}")); return False
    def get_account(self):
        if not self.session: return None
        try:
            r = self.session.get(f"https://api.tradier.com/v1/accounts/{self.account_id}/balances")
            data = r.json(); bal = data.get("balances", {}).get("balance", {})
            return {"equity": float(bal.get("total_equity",0)), "pl": 0.0, "buying_power": float(bal.get("option_buying_power",0)), "cash": 0.0, "open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None):
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

# ---------- BINANCE BROKER ----------
class BinanceBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Binance"; self.client = None
    def connect(self):
        creds = self.config.get("binance", {})
        api_key = creds.get("api_key","").strip(); api_secret = creds.get("api_secret","").strip()
        testnet = creds.get("testnet", True)
        if not api_key or not api_secret: self.ui_queue.put(("error","Binance API key/secret required")); return False
        try:
            from binance.client import Client
            self.client = Client(api_key, api_secret, testnet=testnet)
            self.client.get_account()
            return True
        except Exception as e: self.ui_queue.put(("error",f"Binance connection: {e}")); return False
    def get_account(self):
        if not self.client: return None
        try:
            acc = self.client.get_account()
            balances = {b["asset"]:float(b["free"])+float(b["locked"]) for b in acc["balances"]}
            return {"equity":sum(balances.values()),"pl":0.0,"buying_power":0.0,"cash":0.0,"open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None):
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

# ---------- BYBIT BROKER ----------
class BybitBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "Bybit"; self.session = None
    def connect(self):
        creds = self.config.get("bybit", {})
        api_key = creds.get("api_key","").strip(); api_secret = creds.get("api_secret","").strip()
        testnet = creds.get("testnet", True)
        if not api_key or not api_secret: self.ui_queue.put(("error","Bybit API key/secret required")); return False
        try:
            from pybit.unified import HTTP
            self.session = HTTP(api_key=api_key, api_secret=api_secret, testnet=testnet)
            self.session.get_wallet_balance(accountType="UNIFIED")
            return True
        except Exception as e: self.ui_queue.put(("error",f"Bybit connection: {e}")); return False
    def get_account(self):
        if not self.session: return None
        try:
            bal = self.session.get_wallet_balance(accountType="UNIFIED")
            total = float(bal["result"]["list"][0]["totalEquity"])
            return {"equity":total,"pl":0.0,"buying_power":0.0,"cash":0.0,"open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None):
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

# ---------- OKX BROKER ----------
class OKXBroker(BaseBroker):
    def __init__(self, config, ui_queue): super().__init__(config, ui_queue); self.name = "OKX"; self.api = None
    def connect(self):
        creds = self.config.get("okx", {})
        api_key = creds.get("api_key","").strip(); api_secret = creds.get("api_secret","").strip(); passphrase = creds.get("api_passphrase","").strip()
        demo = creds.get("demo", True)
        if not api_key or not api_secret or not passphrase: self.ui_queue.put(("error","OKX requires key, secret, passphrase")); return False
        try:
            import okx.Account as Account
            flag = "1" if demo else "0"
            self.api = Account.AccountAPI(api_key, api_secret, passphrase, False, flag)
            self.api.get_account_balance()
            return True
        except Exception as e: self.ui_queue.put(("error",f"OKX connection: {e}")); return False
    def get_account(self):
        if not self.api: return None
        try:
            bal = self.api.get_account_balance(); details = bal.get("data",[{}])[0].get("details",[])
            total = sum(float(d.get("eq",0)) for d in details)
            return {"equity":total,"pl":0.0,"buying_power":0.0,"cash":0.0,"open_positions":0}
        except: return None
    def submit_order(self, symbol, qty, side, order_type="market", sl_pct=None, tp_pct=None):
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

# ---------- INDICATOR CALCULATOR (unchanged) ----------
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
        atr = ema(tr, 14)
        up = np.maximum(high[1:]-high[:-1],0); dn = np.maximum(low[:-1]-low[1:],0)
        up = np.insert(up,0,0); dn = np.insert(dn,0,0)
        plus_dm = np.where((up>dn)&(up>0), up, 0.0)
        minus_dm = np.where((dn>up)&(dn>0), dn, 0.0)
        plus_di = 100 * ema(plus_dm,14)/atr; minus_di = 100 * ema(minus_dm,14)/atr
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-14)
        df['ADX'] = ema(dx, 14)
        vol_avg20 = np.convolve(volume, np.ones(20)/20, mode='same')
        df['Vol_ratio'] = np.divide(volume, vol_avg20, out=np.ones_like(volume), where=vol_avg20!=0)
        return df

# ---------- SIGNAL ANALYZER (unchanged) ----------
class SignalAnalyzer:
    @staticmethod
    def _safe_float(series, default=0.0):
        try:
            val = series.item() if hasattr(series, 'item') else series; return float(val)
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
        if direction=="bull":
            if config.get('use_rsi',True) and rsi<30: return False
            if config.get('use_macd',True) and macd<=macd_signal: return False
            if config.get('use_vwap',True) and price<vwap: return False
            if config.get('use_bollinger',True) and price<bb_lower*0.99: return False
        else:
            if config.get('use_rsi',True) and rsi>70: return False
            if config.get('use_macd',True) and macd>=macd_signal: return False
            if config.get('use_vwap',True) and price>vwap: return False
            if config.get('use_bollinger',True) and price>bb_upper*1.01: return False
        if config.get('use_adx',True) and adx<ADX_TREND_THRESHOLD: return False
        if config.get('use_vol_confirm',True) and vol_ratio<VOLUME_RATIO_THRESHOLD: return False
        return True

# ---------- TRADING ENGINE (unchanged) ----------
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue, config, broker):
        super().__init__(daemon=True); self.ui_queue, self.config, self.broker = ui_queue, config, broker
        self.running = False; self.symbols = []; self.positions = {}; self.prev_ema = {}; self.trade_history = []
    def send_telegram(self, message):
        tg = self.config.get("telegram", {})
        token, chat = tg.get("token"), tg.get("chat_id")
        if token and chat:
            try:
                import requests; requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":chat,"text":message,"parse_mode":"HTML"}, timeout=5)
            except: pass
    def run(self):
        tickers_str = self.config.get("tickers", DEFAULT_TICKERS)
        self.symbols = [s.strip().upper() for s in tickers_str.split(",") if s.strip()] or ["AAPL"]
        for sym in self.symbols: self.positions[sym]=0; self.prev_ema[sym]=(None,None)
        mode = self.config.get("mode","signal"); qty = self.config.get("quantity",DEFAULT_QUANTITY)
        ema_fast, ema_slow = self.config.get("emas", DEFAULT_EMAS)
        use_bracket = self.config.get("use_bracket", False)
        sl_pct = self.config.get("sl_percent",2.0); tp_pct = self.config.get("tp_percent",4.0)
        interval = self.config.get("timeframe", DEFAULT_TIMEFRAME)
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
                    last_hist = now
                    ema_update = {}
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
                                self.send_telegram(f"<b>{signal_type} Signal</b> – {sym} @ ${price:.2f}")
                                if mode == "auto" and is_open:
                                    if signal_type == "BUY" and self.positions.get(sym,0)==0:
                                        try:
                                            success = self.broker.submit_order(sym, qty, "buy", "market", sl_pct if use_bracket else None, tp_pct if use_bracket else None)
                                            if success:
                                                self.positions[sym]=qty
                                                self.ui_queue.put(("order", (sym,"BUY",qty,price)))
                                                self.send_telegram(f"✅ Bought {qty} {sym} @ ${price:.2f}")
                                        except Exception as e: self.ui_queue.put(("error", f"Buy {sym} failed: {e}"))
                                    elif signal_type == "SELL" and self.positions.get(sym,0)>0:
                                        pos_qty = self.positions[sym]
                                        try:
                                            success = self.broker.submit_order(sym, pos_qty, "sell")
                                            if success:
                                                self.positions[sym]=0
                                                self.ui_queue.put(("order", (sym,"SELL",pos_qty,price)))
                                                self.send_telegram(f"✅ Sold {pos_qty} {sym} @ ${price:.2f}")
                                        except Exception as e: self.ui_queue.put(("error", f"Sell {sym} failed: {e}"))
                    if ema_update: self.ui_queue.put(("ema_update", ema_update))
                time.sleep(1)
            except Exception as e: self.ui_queue.put(("error", f"Engine loop: {traceback.format_exc()}")); time.sleep(5)
        self.broker.stop_stream(); self.ui_queue.put(("status", "⏹️ Bot stopped")); self.send_telegram("🛑 Bot stopped")
    def stop(self): self.running = False
    def on_price_update(self, sym, price): self.ui_queue.put(("price_update", (sym, price)))

# ---------- FLASK ROUTES ----------
@app.route('/')
def index():
    return FRONTEND_HTML

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(state.config)

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
    broker_choice = state.config.get("broker", "Alpaca")
    broker_cls = BROKER_REGISTRY.get(broker_choice)
    if not broker_cls:
        return jsonify({"status":"error","message":f"Broker '{broker_choice}' not supported"})
    state.broker_instance = broker_cls(state.config, state.ui_queue)
    if not state.broker_instance.connect():
        return jsonify({"status":"error","message":"Broker connection failed"})
    state.engine = TradingEngine(state.ui_queue, state.config, state.broker_instance)
    state.engine.running = True; state.engine.start(); state.running = True
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
            if msg[0] == "account":
                eq, pl, bp, open_pos = msg[1]
                state.dashboard["equity"] = eq; state.dashboard["pl"] = pl; state.dashboard["buying_power"] = bp; state.dashboard["open_positions"] = open_pos
            elif msg[0] == "signal":
                sym, sig, price, rationale = msg[1]
                state.dashboard["signals"].append({"time":datetime.now().strftime("%H:%M:%S"),"symbol":sym,"signal":sig,"price":price,"rationale":rationale})
            elif msg[0] == "order":
                sym, action, qty, price = msg[1]
                state.dashboard["orders"].append({"time":datetime.now().strftime("%H:%M:%S"),"symbol":sym,"action":action,"qty":qty,"price":price})
            elif msg[0] == "log": state.dashboard["log"].append(msg[1])
            elif msg[0] == "error": state.dashboard["log"].append(f"❌ {msg[1]}")
            elif msg[0] == "ema_update": state.dashboard["ema_values"] = msg[1]
        except queue.Empty: break
    for key in ["signals","orders","log"]:
        if len(state.dashboard[key])>50: state.dashboard[key] = state.dashboard[key][-50:]
    return jsonify({"running":state.running, **state.dashboard})

@app.route('/api/update', methods=['GET'])
def check_update():
    """Return latest version info from GitHub repository."""
    try:
        # Replace with your actual repo URL
        url = "https://raw.githubusercontent.com/shafayrich/tradermoney/main/version.json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        latest_version = data.get("latest_version", "0.0.0")
        download_url = data.get("download_url", "")
        is_newer = tuple(map(int, latest_version.split("."))) > tuple(map(int, APP_VERSION.split(".")))
        return jsonify({
            "current_version": APP_VERSION,
            "latest_version": latest_version,
            "download_url": download_url,
            "update_available": is_newer
        })
    except Exception as e:
        return jsonify({
            "current_version": APP_VERSION,
            "latest_version": APP_VERSION,
            "download_url": "",
            "update_available": False,
            "error": str(e)
        })

# ---------- FRONTEND HTML (unchanged except for update toast/button) ----------
FRONTEND_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  :root { --bg:#0A0C0F; --card:#1A1F26; --text:#E0E0E0; --accent:#00C9B1; --danger:#FF4B4B; --border:#2A3440; --btn:#00A896; --text-muted:#6B7280; }
  body { margin:0; font-family:-apple-system, sans-serif; background:var(--bg); color:var(--text); display:flex; height:100vh; overflow:hidden; }
  #sidebar { width:260px; background:#11151A; border-right:1px solid var(--border); padding:20px 15px; overflow-y:auto; }
  #sidebar h2 { color:var(--accent); margin:0 0 15px; }
  label { font-size:0.85rem; display:block; margin-top:10px; }
  input, select, button { background:var(--card); color:var(--text); border:1px solid var(--border); padding:8px; border-radius:6px; width:100%; box-sizing:border-box; margin-top:5px; font-size:0.9rem; }
  button { cursor:pointer; background:var(--btn); border:none; font-weight:600; margin-top:15px; transition: all 0.2s ease; }
  button:hover { opacity:0.9; transform: translateY(-1px); }
  button:active { transform: translateY(0); }
  .danger { background:var(--danger); }
  #main { flex:1; display:flex; flex-direction:column; }
  .tab-header { display:flex; background:var(--card); border-bottom:1px solid var(--border); }
  .tab-btn { flex:1; background:transparent; border:none; color:var(--text); padding:14px 10px; cursor:grab; font-weight:500; letter-spacing:0.3px; transition: all 0.2s ease; border-bottom:2px solid transparent; }
  .tab-btn:active { cursor:grabbing; }
  .tab-btn:hover { background: rgba(255,255,255,0.03); }
  .tab-btn.active { border-bottom-color: var(--accent); color: var(--accent); font-weight:600; }
  .tab-content { flex:1; display:none; overflow:hidden; }
  .tab-content.active { display:flex; flex-direction:column; }
  #metrics { display:grid; grid-template-columns: repeat(4,1fr); gap:10px; padding:10px; background:var(--card); border-bottom:1px solid var(--border); }
  .metric { text-align:center; } .metric .value { font-size:1.2rem; font-weight:bold; color:var(--accent); }
  #ticker-tabs { display:flex; background:var(--card); border-bottom:1px solid var(--border); overflow-x:auto; }
  .ticker-btn { padding:8px 15px; background:transparent; border:none; color:var(--text); cursor:pointer; white-space:nowrap; border-bottom:2px solid transparent; transition: 0.2s; }
  .ticker-btn.active { border-bottom-color: var(--accent); color:var(--accent); font-weight:600; }
  #chart-container { flex:1; }
  .signal-item { display:flex; justify-content:space-between; padding:10px; border-bottom:1px solid var(--border); }
  .buy { color:var(--accent); } .sell { color:var(--danger); }
  #log { height:120px; overflow-y:auto; background:var(--bg); padding:10px; font-size:0.8rem; border-top:1px solid var(--border); }
  #toast-container { position:fixed; top:20px; right:20px; z-index:9999; display:flex; flex-direction:column; gap:8px; }
  .toast { padding:12px 20px; border-radius:6px; color:white; font-weight:500; box-shadow:0 4px 12px rgba(0,0,0,0.3); animation: slideIn 0.3s ease; max-width:300px; }
  .toast.success { background: var(--accent); }
  .toast.error { background: var(--danger); }
  .toast.info { background: #3b82f6; }
  @keyframes slideIn { from { transform: translateX(100%); opacity:0; } to { transform: translateX(0); opacity:1; } }
  .ema-monitor { display:grid; grid-template-columns: repeat(auto-fit, minmax(120px,1fr)); gap:8px; padding:10px; }
  .ema-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:10px; text-align:center; }
  .ema-card .ticker { font-weight:bold; color:var(--accent); }
  .ema-card .ema-value { font-size:1.1rem; margin-top:5px; }
  .ema-card .ema-label { font-size:0.7rem; color:var(--text-muted); }
  .help-content { padding:20px; overflow-y:auto; height:100%; box-sizing:border-box; }
  .help-content h2 { color:var(--accent); margin-top:0; }
  .help-content h3 { color:var(--text); margin:20px 0 10px; border-bottom:1px solid var(--border); padding-bottom:5px; }
  .help-content p, .help-content ul { font-size:0.9rem; line-height:1.6; }
  .help-content ul { padding-left:20px; }
  .help-content li { margin-bottom:6px; }
  .help-content code { background:var(--card); padding:2px 6px; border-radius:4px; font-size:0.85rem; }
  #update-toast { display:none; position:fixed; bottom:20px; right:20px; z-index:9999; background:var(--accent); color:black; padding:15px 20px; border-radius:8px; font-weight:bold; }
  #update-toast a { color:white; text-decoration:underline; cursor:pointer; }
</style>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
</head>
<body>
<div id="toast-container"></div>
<div id="update-toast" id="update-toast-element"><span>🔔 New version available! <a id="update-link" href="#" target="_blank">Download Update</a></span></div>
<div id="sidebar">
  <h2>💸 TraderMoney</h2>
  <label>Broker</label>
  <select id="broker-select"><option>Alpaca</option><option>Interactive Brokers</option><option>Tradier</option><option>Binance</option><option>Bybit</option><option>OKX</option></select>
  <div id="cred-entries"></div>
  <label>Telegram Token (opt)</label>
  <input type="password" id="tg-token">
  <label>Telegram Chat ID</label>
  <input id="tg-chat">
  <label>Tickers (comma sep)</label>
  <input id="tickers" value="AAPL">
  <label>Timeframe</label>
  <select id="timeframe"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>1d</option></select>
  <div style="display:flex;gap:5px;margin-top:10px;"><input id="ema-fast" value="9"><input id="ema-slow" value="50"></div>
  <label>Quantity</label>
  <input id="quantity" value="1" type="number">
  <label>Mode</label>
  <select id="mode"><option value="signal">Signal Only</option><option value="auto">Auto Trade</option></select>
  <label><input type="checkbox" id="use-bracket"> Enable SL/TP</label>
  <div style="display:flex;gap:5px;"><input id="sl-percent" value="2"><input id="tp-percent" value="4"></div>
  <label>Indicators</label>
  <label><input type="checkbox" id="use-rsi" checked> RSI</label>
  <label><input type="checkbox" id="use-macd" checked> MACD</label>
  <label><input type="checkbox" id="use-vwap" checked> VWAP</label>
  <label><input type="checkbox" id="use-bollinger" checked> Bollinger</label>
  <label><input type="checkbox" id="use-adx" checked> ADX (Trend Strength)</label>
  <label><input type="checkbox" id="use-vol-confirm" checked> Volume Confirmation</label>
  <button onclick="saveConfig()">💾 Save</button>
  <button onclick="startBot()">▶️ Start</button>
  <button onclick="stopBot()" style="background:#555;">⏹️ Stop</button>
  <button onclick="killSwitch()" class="danger">⚠️ Kill Switch</button>
  <button onclick="checkForUpdates()" style="margin-top:20px; background:var(--card); border:1px solid var(--border);">🔄 Check for Updates</button>
</div>
<div id="main">
  <div class="tab-header" id="tab-header">
    <button class="tab-btn active" data-tab="charts">Charts</button>
    <button class="tab-btn" data-tab="signals">Signals</button>
    <button class="tab-btn" data-tab="history">History</button>
    <button class="tab-btn" data-tab="ema">EMA Monitor</button>
    <button class="tab-btn" data-tab="help">Help</button>
  </div>
  <div id="tab-charts" class="tab-content active">
    <div id="ticker-tabs"></div>
    <div id="metrics">
      <div class="metric"><div class="value" id="equity">—</div><div>Net Liq.</div></div>
      <div class="metric"><div class="value" id="bp">—</div><div>Buying Power</div></div>
      <div class="metric"><div class="value" id="pl">—</div><div>Daily P&L</div></div>
      <div class="metric"><div class="value" id="positions">—</div><div>Positions</div></div>
    </div>
    <div id="chart-container"><p style="color:var(--text);text-align:center;padding-top:50px;">Loading chart...</p></div>
  </div>
  <div id="tab-signals" class="tab-content"><div id="signals-list" style="overflow-y:auto;flex:1;"></div></div>
  <div id="tab-history" class="tab-content"><div id="history-list" style="overflow-y:auto;flex:1;"></div></div>
  <div id="tab-ema" class="tab-content"><div class="ema-monitor" id="ema-monitor">Loading...</div></div>
  <div id="tab-help" class="tab-content">
    <div class="help-content">
      <h2>📘 TraderMoney Help & Documentation</h2>
      <p>Welcome to TraderMoney, your institutional‑grade trading terminal with 6 integrated broker connections.</p>
      <h3>🚀 Quick Start</h3>
      <ol>
        <li><strong>Choose a Broker</strong> from the dropdown (Alpaca is the simplest for paper trading).</li>
        <li>Enter your <strong>credentials</strong> (they will be encrypted and saved locally).</li>
        <li>Add <strong>Tickers</strong> (comma separated, e.g. <code>AAPL, MSFT, TSLA</code>).</li>
        <li>Set a <strong>Timeframe</strong> (lower = more frequent signals).</li>
        <li>Select <strong>Signal Only</strong> (alerts + Telegram notifications) or <strong>Auto Trade</strong> (executes real orders).</li>
        <li>Click <strong>💾 Save</strong>, then <strong>▶️ Start</strong>.</li>
      </ol>
      <h3>📊 Trading Logic & Signal Pipeline</h3>
      <p>The bot looks for <strong>EMA crossover</strong> events (fast EMA crosses above/below slow EMA). Every potential signal then passes through multiple confirmation filters:</p>
      <ul>
        <li><strong>EMA Crossover</strong> – the core entry trigger (default 9 & 50).</li>
        <li><strong>RSI</strong> – bullish only if RSI ≥ 30; bearish only if RSI ≤ 70.</li>
        <li><strong>MACD</strong> – must agree with crossover direction.</li>
        <li><strong>VWAP</strong> – price must be above VWAP for buys, below for sells.</li>
        <li><strong>Bollinger Bands</strong> – price relative to upper/lower bands.</li>
        <li><strong>ADX (Trend Strength)</strong> – ADX must be ≥ 20 for a clear trend.</li>
        <li><strong>Volume Confirmation</strong> – current volume must exceed 1.5× the 20‑period average.</li>
      </ul>
      <p><em>All filters can be toggled individually in the sidebar.</em></p>
      <h3>🔔 Telegram Alerts</h3>
      <p>Enter your bot token and chat ID in the sidebar. You will receive start/stop notifications and every signal in real time.</p>
      <h3>💾 Data & Security</h3>
      <p>All credentials are encrypted with <code>Fernet</code> and stored in <code>~/.tradermoney_config.enc</code>.</p>
      <h3>🔄 Auto‑Updates</h3>
      <p>TraderMoney checks for new versions on startup. If a new version is available, a toast notification appears with a direct download link. Click it to open your browser and grab the latest version.</p>
    </div>
  </div>
  <div id="log"></div>
</div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
let currentTicker = '', tickers = [], chartWidget = null, config = {};

// Auto‑update checker
async function checkForUpdates() {
  try {
    const r = await fetch('/api/update');
    const data = await r.json();
    if (data.update_available) {
      const toast = document.getElementById('update-toast');
      toast.style.display = 'block';
      document.getElementById('update-link').href = data.download_url;
    } else {
      showToast('✅ You are up‑to‑date!', 'success');
    }
  } catch(e) { console.log('Update check failed', e); }
}

// Check on load
setTimeout(checkForUpdates, 3000);

const tabHeader = document.getElementById('tab-header');
Sortable.create(tabHeader, {
  animation: 150,
  handle: '.tab-btn',
  onEnd: function () {
    const first = tabHeader.querySelector('.tab-btn');
    if (!document.querySelector('.tab-btn.active') && first) first.click();
  }
});

function switchTab(name, ev) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (ev) ev.target.classList.add('active');
  if (name === 'charts' && chartWidget) setTimeout(() => chartWidget.resize && chartWidget.resize(), 100);
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', function(e) { switchTab(this.dataset.tab, e); });
});

function playTradeSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator(); const gain = ctx.createGain();
    osc.type = 'sine'; osc.frequency.setValueAtTime(800, ctx.currentTime);
    gain.gain.setValueAtTime(0.3, ctx.currentTime); gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.15);
    osc.connect(gain); gain.connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime + 0.15);
  } catch(e) {}
}

function showToast(msg, type='info') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div'); t.className = `toast ${type}`; t.textContent = msg;
  c.appendChild(t); setTimeout(() => t.remove(), 3000);
}

async function loadConfig() {
  const r = await fetch('/api/config'); config = await r.json(); initUI(config);
}

function updateCredFields() {
  const broker = document.getElementById('broker-select').value;
  const c = document.getElementById('cred-entries'); c.innerHTML = '';
  if (broker === 'Alpaca') {
    c.innerHTML = `<label>API Key</label><input type="password" id="alpaca-key"><label>Secret Key</label><input type="password" id="alpaca-secret"><label><input type="checkbox" id="alpaca-paper" checked> Paper Trading</label>`;
  } else if (broker === 'Interactive Brokers') {
    c.innerHTML = `<label>Host</label><input id="ibkr-host" value="127.0.0.1"><label>Port</label><input id="ibkr-port" value="7497"><label>Client ID</label><input id="ibkr-client-id" value="1">`;
  } else if (broker === 'Tradier') {
    c.innerHTML = `<label>Access Token</label><input type="password" id="tradier-token"><label>Account ID</label><input id="tradier-account-id">`;
  } else if (broker === 'Binance') {
    c.innerHTML = `<label>API Key</label><input type="password" id="binance-key"><label>API Secret</label><input type="password" id="binance-secret"><label><input type="checkbox" id="binance-testnet" checked> Testnet (Paper Trading)</label>`;
  } else if (broker === 'Bybit') {
    c.innerHTML = `<label>API Key</label><input type="password" id="bybit-key"><label>API Secret</label><input type="password" id="bybit-secret"><label><input type="checkbox" id="bybit-testnet" checked> Testnet (Paper Trading)</label>`;
  } else if (broker === 'OKX') {
    c.innerHTML = `<label>API Key</label><input type="password" id="okx-key"><label>API Secret</label><input type="password" id="okx-secret"><label>API Passphrase</label><input type="password" id="okx-passphrase"><label><input type="checkbox" id="okx-demo" checked> Demo Trading</label>`;
  }
  // pre‑fill from saved config
  if (config.alpaca) {
    const ak = document.getElementById('alpaca-key'); if (ak) ak.value = config.alpaca.api_key || '';
    const sk = document.getElementById('alpaca-secret'); if (sk) sk.value = config.alpaca.secret_key || '';
    const pp = document.getElementById('alpaca-paper'); if (pp) pp.checked = config.alpaca.paper !== false;
  }
  if (config.ibkr) {
    const h = document.getElementById('ibkr-host'); if (h) h.value = config.ibkr.host || '';
    const p = document.getElementById('ibkr-port'); if (p) p.value = config.ibkr.port || '';
    const ci = document.getElementById('ibkr-client-id'); if (ci) ci.value = config.ibkr.client_id || '';
  }
  if (config.tradier) {
    const t = document.getElementById('tradier-token'); if (t) t.value = config.tradier.access_token || '';
    const a = document.getElementById('tradier-account-id'); if (a) a.value = config.tradier.account_id || '';
  }
  if (config.binance) {
    const k = document.getElementById('binance-key'); if (k) k.value = config.binance.api_key || '';
    const s = document.getElementById('binance-secret'); if (s) s.value = config.binance.api_secret || '';
    const tn = document.getElementById('binance-testnet'); if (tn) tn.checked = config.binance.testnet !== false;
  }
  if (config.bybit) {
    const k = document.getElementById('bybit-key'); if (k) k.value = config.bybit.api_key || '';
    const s = document.getElementById('bybit-secret'); if (s) s.value = config.bybit.api_secret || '';
    const tn = document.getElementById('bybit-testnet'); if (tn) tn.checked = config.bybit.testnet !== false;
  }
  if (config.okx) {
    const k = document.getElementById('okx-key'); if (k) k.value = config.okx.api_key || '';
    const s = document.getElementById('okx-secret'); if (s) s.value = config.okx.api_secret || '';
    const p = document.getElementById('okx-passphrase'); if (p) p.value = config.okx.api_passphrase || '';
    const d = document.getElementById('okx-demo'); if (d) d.checked = config.okx.demo !== false;
  }
}

async function initUI(cfg) {
  if (!cfg) return;
  document.getElementById('broker-select').value = cfg.broker || 'Alpaca';
  document.getElementById('tickers').value = cfg.tickers || 'AAPL';
  document.getElementById('ema-fast').value = cfg.emas ? cfg.emas[0] : 9;
  document.getElementById('ema-slow').value = cfg.emas ? cfg.emas[1] : 50;
  document.getElementById('quantity').value = cfg.quantity || 1;
  document.getElementById('mode').value = cfg.mode || 'signal';
  if (cfg.telegram) {
    document.getElementById('tg-token').value = cfg.telegram.token || '';
    document.getElementById('tg-chat').value = cfg.telegram.chat_id || '';
  }
  document.getElementById('use-bracket').checked = cfg.use_bracket || false;
  document.getElementById('sl-percent').value = cfg.sl_percent || 2;
  document.getElementById('tp-percent').value = cfg.tp_percent || 4;
  document.getElementById('use-rsi').checked = cfg.use_rsi !== false;
  document.getElementById('use-macd').checked = cfg.use_macd !== false;
  document.getElementById('use-vwap').checked = cfg.use_vwap !== false;
  document.getElementById('use-bollinger').checked = cfg.use_bollinger !== false;
  document.getElementById('use-adx').checked = cfg.use_adx !== false;
  document.getElementById('use-vol-confirm').checked = cfg.use_vol_confirm !== false;
  updateCredFields();
  const t = document.getElementById('tickers').value.split(',').map(s=>s.trim()).filter(s=>s);
  if (t.length) { setTickers(t); if (!currentTicker) currentTicker = t[0]; loadChart(currentTicker); }
}

function setTickers(list) {
  tickers = list; if (!currentTicker) currentTicker = list[0];
  const bar = document.getElementById('ticker-tabs'); bar.innerHTML = '';
  tickers.forEach(sym => {
    const btn = document.createElement('button');
    btn.className = 'ticker-btn' + (sym === currentTicker ? ' active' : '');
    btn.textContent = sym;
    btn.onclick = () => { currentTicker = sym; updateTickerTabs(); loadChart(sym); };
    bar.appendChild(btn);
  });
}

function updateTickerTabs() {
  Array.from(document.getElementById('ticker-tabs').children).forEach(b => b.classList.toggle('active', b.textContent === currentTicker));
}

function loadChart(sym) {
  const container = document.getElementById('chart-container'); container.innerHTML = '';
  if (typeof TradingView === 'undefined') { setTimeout(() => loadChart(sym), 100); return; }
  chartWidget = new TradingView.widget({
    "autosize": true, "symbol": sym, "interval": "1", "timezone": "Etc/UTC", "theme": "Dark", "style": "1",
    "locale": "en", "toolbar_bg": "#0A0C0F", "enable_publishing": false, "hide_side_toolbar": false,
    "allow_symbol_change": true, "container_id": "chart-container"
  });
}

async function pollStatus() {
  try {
    const r = await fetch('/api/status'); const data = await r.json();
    document.getElementById('equity').innerText = '$' + data.equity.toLocaleString();
    document.getElementById('bp').innerText = '$' + data.buying_power.toLocaleString();
    const plPct = data.equity ? (data.pl / data.equity * 100) : 0;
    document.getElementById('pl').innerHTML = `<span style="color:${plPct>=0?'var(--accent)':'var(--danger)'}">${plPct>=0?'+':''}${plPct.toFixed(2)}%</span>`;
    document.getElementById('positions').innerText = data.open_positions;
    const sl = document.getElementById('signals-list');
    if (sl) {
      sl.innerHTML = '';
      data.signals.forEach(s => {
        const div = document.createElement('div'); div.className = 'signal-item ' + (s.signal==='BUY'?'buy':'sell');
        div.innerHTML = `<span>${s.time} ${s.signal} ${s.symbol} @ $${s.price}</span><span>${s.rationale}</span>`;
        sl.prepend(div);
      });
    }
    const ol = document.getElementById('history-list');
    if (ol) {
      ol.innerHTML = '';
      data.orders.forEach(o => {
        const div = document.createElement('div'); div.className = 'signal-item ' + (o.action==='BUY'?'buy':'sell');
        div.innerHTML = `<span>${o.time} ${o.action} ${o.qty} ${o.symbol} @ $${o.price}</span>`;
        ol.prepend(div); playTradeSound();
      });
    }
    const ema = document.getElementById('ema-monitor');
    if (ema && data.ema_values) {
      let html = '';
      for (const [sym, vals] of Object.entries(data.ema_values)) {
        html += `<div class="ema-card"><div class="ticker">${sym}</div>
          <div class="ema-value"><span class="ema-label">Fast EMA:</span> ${vals.fast}</div>
          <div class="ema-value"><span class="ema-label">Slow EMA:</span> ${vals.slow}</div></div>`;
      }
      ema.innerHTML = html || '<div style="color:var(--text-muted);padding:10px;">Waiting for data...</div>';
    }
    document.getElementById('log').innerHTML = data.log.join('<br>');
  } catch(e) {}
}
setInterval(pollStatus, 1000);

document.getElementById('broker-select').addEventListener('change', updateCredFields);

function buildConfig() {
  const broker = document.getElementById('broker-select').value;
  return {
    broker, tickers: document.getElementById('tickers').value,
    timeframe: document.getElementById('timeframe').value,
    emas: [parseInt(document.getElementById('ema-fast').value), parseInt(document.getElementById('ema-slow').value)],
    quantity: parseInt(document.getElementById('quantity').value),
    mode: document.getElementById('mode').value,
    use_bracket: document.getElementById('use-bracket').checked,
    sl_percent: parseFloat(document.getElementById('sl-percent').value),
    tp_percent: parseFloat(document.getElementById('tp-percent').value),
    telegram: { token: document.getElementById('tg-token').value, chat_id: document.getElementById('tg-chat').value },
    use_rsi: document.getElementById('use-rsi').checked,
    use_macd: document.getElementById('use-macd').checked,
    use_vwap: document.getElementById('use-vwap').checked,
    use_bollinger: document.getElementById('use-bollinger').checked,
    use_adx: document.getElementById('use-adx').checked,
    use_vol_confirm: document.getElementById('use-vol-confirm').checked,
    alpaca: broker === 'Alpaca' ? {
      api_key: document.getElementById('alpaca-key')?.value || '', secret_key: document.getElementById('alpaca-secret')?.value || '',
      paper: document.getElementById('alpaca-paper')?.checked || true
    } : {},
    ibkr: broker === 'Interactive Brokers' ? {
      host: document.getElementById('ibkr-host')?.value || '127.0.0.1', port: document.getElementById('ibkr-port')?.value || '7497',
      client_id: document.getElementById('ibkr-client-id')?.value || '1'
    } : {},
    tradier: broker === 'Tradier' ? {
      access_token: document.getElementById('tradier-token')?.value || '', account_id: document.getElementById('tradier-account-id')?.value || ''
    } : {},
    binance: broker === 'Binance' ? {
      api_key: document.getElementById('binance-key')?.value || '', api_secret: document.getElementById('binance-secret')?.value || '',
      testnet: document.getElementById('binance-testnet')?.checked || true
    } : {},
    bybit: broker === 'Bybit' ? {
      api_key: document.getElementById('bybit-key')?.value || '', api_secret: document.getElementById('bybit-secret')?.value || '',
      testnet: document.getElementById('bybit-testnet')?.checked || true
    } : {},
    okx: broker === 'OKX' ? {
      api_key: document.getElementById('okx-key')?.value || '', api_secret: document.getElementById('okx-secret')?.value || '',
      api_passphrase: document.getElementById('okx-passphrase')?.value || '', demo: document.getElementById('okx-demo')?.checked || true
    } : {}
  };
}

async function saveConfig() { config = buildConfig(); await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(config)}); showToast('Configuration saved','success'); }
async function startBot() { config = buildConfig(); const r = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(config)}); const d = await r.json(); showToast(d.message, d.status==='ok'?'success':'error'); }
async function stopBot() { await fetch('/api/stop', {method:'POST'}); showToast('Bot stopped','success'); }
async function killSwitch() { await fetch('/api/kill', {method:'POST'}); showToast('Kill switch activated','success'); }

loadConfig();
</script>
</body>
</html>
"""

# ---------- MAIN ----------
def run_flask():
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)

if __name__ == "__main__":
    acquire_lock()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1)
    window = webview.create_window('TraderMoney', 'http://127.0.0.1:5050', width=1300, height=800, min_size=(900,600))
    webview.start()

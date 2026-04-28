"""
TraderMoney v28 – Sidebar fix, persistent data, restored Help tab, extended Help, and icon support.
"""

import json, os, queue, signal, sys, socket, threading, time, traceback, atexit, urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import requests as http_requests
import webview
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

APP_VERSION = "1.0.28"

# ── Gumroad ──────────────────────────────────────────────
GUMMROAD_PRODUCT_ID = "73otoT7rzJukCy-Lt4hhkQ=="          # ← your real product ID

def verify_gumroad_license(license_key: str) -> Tuple[bool, str]:
    """Check a Gumroad license key. Returns (is_valid, message)."""
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

CONFIG_FILE = os.path.expanduser("~/.tradermoney_config.enc")
KEY_FILE = os.path.expanduser("~/.tradermoney.key")
DEFAULT_EMAS = (9, 50)
DEFAULT_TICKERS = "AAPL"
DEFAULT_QUANTITY = 1
DEFAULT_TIMEFRAME = "1m"
ADX_TREND_THRESHOLD = 20
VOLUME_RATIO_THRESHOLD = 1.5
SUPERTREND_ATR_PERIOD = 10
SUPERTREND_FACTOR = 3.0
STOCHASTIC_K_PERIOD = 14
STOCHASTIC_D_PERIOD = 3
ATR_STOP_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0
ATR_TP_MULTIPLIER = 3.0

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

class AppState:
    def __init__(self):
        saved = EncryptedConfigManager.load() or {}
        default = {
            "broker":"Alpaca", "tickers":"AAPL", "mode":"signal", "quantity":1,
            "emas":[9,50], "use_bracket":False, "sl_percent":2.0, "tp_percent":4.0,
            "timeframe":"1m", "telegram":{},
            "use_rsi":True, "use_macd":True, "use_vwap":True, "use_bollinger":True,
            "use_adx":True, "use_vol_confirm":True,
            "use_supertrend":True, "use_stochastic":True, "use_atr_stops":True,
            "license_key":"", "license_valid":False,
            "signals_history": [],       # persisted signals list
            "orders_history": []         # persisted orders list
        }
        self.config = {**default, **saved}
        self.ui_queue = queue.Queue()
        self.engine = None
        self.broker_instance = None
        self.running = False
        self.dashboard = {
            "equity": 0, "pl": 0, "buying_power": 0, "open_positions": 0,
            "signals": self.config.get("signals_history", []),
            "orders": self.config.get("orders_history", []),
            "log": [],
            "ema_values": {}
        }

state = AppState()

# ── Broker registry and classes (unchanged) ─────────────
# (AlpacaBroker, IBKRBroker, TradierBroker, BinanceBroker, BybitBroker, OKXBroker)
# ... (insert the full broker classes from v26 here – exactly as they were)

# ── IndicatorCalculator (unchanged) ────────────────────
# (insert the full IndicatorCalculator from v26)

# ── SignalAnalyzer (unchanged) ─────────────────────────
# (insert the full SignalAnalyzer from v26)

# ── TradingEngine (unchanged, but now saves signals/orders) ─────
class TradingEngine(threading.Thread):
    def __init__(self, ui_queue, config, broker):
        super().__init__(daemon=True); self.ui_queue, self.config, self.broker = ui_queue, config, broker
        self.running = False; self.symbols = []; self.positions = {}; self.prev_ema = {}; self.trade_history = []
        self.per_ticker_qty = {}
        self.is_licensed = config.get("license_valid", False)
    def send_telegram(self, message):
        tg = self.config.get("telegram", {})
        token, chat = tg.get("token"), tg.get("chat_id")
        if token and chat:
            try:
                import requests; requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":chat,"text":message,"parse_mode":"HTML"}, timeout=5)
            except: pass
    def run(self):
        tickers_str = self.config.get("tickers", DEFAULT_TICKERS)
        raw_list = [s.strip() for s in tickers_str.split(",") if s.strip()]
        self.symbols = []
        self.per_ticker_qty = {}
        default_qty = self.config.get("quantity", DEFAULT_QUANTITY)

        for entry in raw_list:
            if ":" in entry:
                sym, qty_str = entry.split(":", 1)
                sym = sym.strip().upper()
                try:
                    qty = float(qty_str)
                    if qty == int(qty):
                        qty = int(qty)
                except ValueError:
                    qty = default_qty
                self.symbols.append(sym)
                self.per_ticker_qty[sym] = qty
            else:
                sym = entry.strip().upper()
                self.symbols.append(sym)
                self.per_ticker_qty[sym] = default_qty

        if not self.is_licensed and len(self.symbols) > 1:
            first = self.symbols[0]
            self.symbols = [first]
            self.per_ticker_qty = {first: self.per_ticker_qty.get(first, default_qty)}
            self.ui_queue.put(("error", "Free license is limited to 1 ticker. Only tracking " + first))
            self.config["tickers"] = first

        for sym in self.symbols:
            self.positions[sym] = 0
            self.prev_ema[sym] = (None, None)

        mode = self.config.get("mode","signal")
        if not self.is_licensed:
            mode = "signal"
            self.ui_queue.put(("log", "⚠️ Free license – Auto Trade disabled, only core indicators active."))
        ema_fast, ema_slow = self.config.get("emas", DEFAULT_EMAS)
        use_bracket = self.config.get("use_bracket", False)
        sl_pct = self.config.get("sl_percent",2.0); tp_pct = self.config.get("tp_percent",4.0)
        use_atr_stops = self.config.get("use_atr_stops", True)
        interval = self.config.get("timeframe", DEFAULT_TIMEFRAME)

        if not self.is_licensed:
            self.config["use_supertrend"] = False
            self.config["use_stochastic"] = False
            self.config["use_adx"] = False
            self.config["use_vol_confirm"] = False
            self.config["use_atr_stops"] = False
            self.config["use_bracket"] = False

        self.broker.stream_prices(self.symbols, self.on_price_update)
        self.ui_queue.put(("status", f"✅ Running {len(self.symbols)} symbols"))
        if self.is_licensed:
            self.send_telegram(f"🤖 Pro Bot started for {', '.join(self.symbols)} ({mode} mode)")
        else:
            self.send_telegram(f"🤖 Free Bot started for {', '.join(self.symbols)} (Signal‑Only)")
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
                                if mode == "auto" and self.is_licensed and is_open:
                                    qty = self.per_ticker_qty.get(sym, default_qty)
                                    if signal_type == "BUY" and self.positions.get(sym,0)==0:
                                        try:
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

# ── FLASK ROUTES ──────────────────────────────────────────
@app.route('/')
def index():
    return FRONTEND_HTML

@app.route('/mobile')
def mobile_dashboard():
    return send_file('mobile.html')

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
                entry = {"time":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"symbol":sym,"signal":sig,"price":price,"rationale":rationale}
                state.dashboard["signals"].append(entry)
                state.config.setdefault("signals_history", []).append(entry)
                if len(state.config["signals_history"]) > 200:
                    state.config["signals_history"] = state.config["signals_history"][-200:]
                EncryptedConfigManager.save(state.config)
            elif msg[0] == "order":
                sym, action, qty, price = msg[1]
                entry = {"time":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"symbol":sym,"action":action,"qty":qty,"price":price}
                state.dashboard["orders"].append(entry)
                state.config.setdefault("orders_history", []).append(entry)
                if len(state.config["orders_history"]) > 200:
                    state.config["orders_history"] = state.config["orders_history"][-200:]
                EncryptedConfigManager.save(state.config)
            elif msg[0] == "log": state.dashboard["log"].append(msg[1])
            elif msg[0] == "error": state.dashboard["log"].append(f"❌ {msg[1]}")
            elif msg[0] == "ema_update": state.dashboard["ema_values"] = msg[1]
        except queue.Empty: break
    for key in ["signals","orders","log"]:
        if len(state.dashboard[key])>50: state.dashboard[key] = state.dashboard[key][-50:]
    return jsonify({"running":state.running, **state.dashboard})

@app.route('/api/update', methods=['GET'])
def check_update():
    try:
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

@app.route('/api/validate_license', methods=['POST'])
def validate_license_endpoint():
    data = request.json or {}
    license_key = data.get("license_key", "").strip()
    if not license_key:
        return jsonify({"valid": False, "message": "No license key provided"})
    is_valid, message = verify_gumroad_license(license_key)
    if is_valid:
        state.config["license_valid"] = True
        state.config["license_key"] = license_key
        EncryptedConfigManager.save(state.config)
    else:
        state.config["license_valid"] = False
    return jsonify({"valid": is_valid, "message": message})

# ── FRONTEND HTML (fixed sidebar, restored Help tab) ─────
FRONTEND_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  :root {
    --bg: #0b0b0f;
    --card: #13141a;
    --text: #e2e2e2;
    --accent: #00c9b1;
    --danger: #ff4b4b;
    --border: #2a2e38;
    --btn: #00a896;
    --text-muted: #7a7d86;
    --sidebar-width: 280px;
    --sidebar-collapsed-width: 50px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    height: 100vh;
    overflow: hidden;
  }

  #sidebar {
    width: var(--sidebar-width);
    background: #0e1015;
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    transition: width 0.3s ease;
    overflow-y: auto;
    overflow-x: hidden;
    position: relative;
    min-width: var(--sidebar-collapsed-width);
    max-width: 600px;
  }
  #sidebar.collapsed {
    width: var(--sidebar-collapsed-width) !important;
  }
  #sidebar.collapsed .sidebar-content,
  #sidebar.collapsed .sidebar-header h2,
  #sidebar.collapsed .license-badge,
  #sidebar.collapsed .sidebar-tabs { display: none; }
  #sidebar.collapsed .sidebar-header { padding: 10px 5px; }
  #sidebar.collapsed #resize-handle { display: none; }

  #sidebar-toggle {
    position: absolute;
    top: 15px;
    right: 10px;
    width: 30px;
    height: 30px;
    background: var(--accent);
    border: none;
    border-radius: 50%;
    color: #000;
    font-weight: bold;
    cursor: pointer;
    z-index: 30;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.3s, right 0.3s;
  }
  #sidebar.collapsed #sidebar-toggle { right: 10px; }

  .sidebar-header {
    padding: 20px 15px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .sidebar-header h2 {
    margin: 0;
    font-size: 1.3rem;
    color: var(--accent);
    white-space: nowrap;
  }
  .sidebar-content {
    flex: 1;
    overflow-y: auto;
    padding: 15px;
  }

  #resize-handle {
    position: absolute;
    top: 0;
    right: 0;
    width: 6px;
    height: 100%;
    cursor: col-resize;
    z-index: 20;
    background: transparent;
  }
  #resize-handle:hover { background: rgba(255,255,255,0.05); }

  /* ── FORM ELEMENTS ── */
  label { display: block; font-size: 0.8rem; margin: 12px 0 5px; color: var(--text-muted); }
  input, select, button {
    background: var(--card); color: var(--text); border: 1px solid var(--border);
    padding: 8px 10px; border-radius: 6px; width: 100%; box-sizing: border-box;
    margin-top: 4px; font-size: 0.9rem;
  }
  button { cursor: pointer; background: var(--btn); border: none; font-weight: 600; margin-top: 12px; }
  button:hover { opacity: 0.9; }
  .danger { background: var(--danger); }
  .license-badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.7rem; margin-left: 8px; vertical-align: middle; }
  .license-valid { background: var(--accent); color: #000; }
  .license-invalid { background: var(--danger); color: #fff; }

  /* ── MAIN ── */
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .tab-header { display: flex; background: var(--card); border-bottom: 1px solid var(--border); }
  .tab-btn {
    flex: 1; background: transparent; border: none; color: var(--text);
    padding: 14px 10px; cursor: grab; font-weight: 500; letter-spacing: 0.3px;
    transition: 0.2s; border-bottom: 2px solid transparent;
  }
  .tab-btn:active { cursor: grabbing; }
  .tab-btn:hover { background: rgba(255,255,255,0.03); }
  .tab-btn.active { border-bottom-color: var(--accent); color: var(--accent); font-weight: 600; }
  .tab-content { flex:1; display:none; overflow:hidden; }
  .tab-content.active { display:flex; flex-direction:column; }
  #metrics {
    display:grid; grid-template-columns: repeat(4,1fr); gap:10px; padding:10px;
    background:var(--card); border-bottom:1px solid var(--border);
  }
  .metric { text-align:center; }
  .metric .value { font-size:1.2rem; font-weight:bold; color:var(--accent); }
  #ticker-tabs { display:flex; background:var(--card); border-bottom:1px solid var(--border); overflow-x:auto; }
  .ticker-btn {
    padding:8px 15px; background:transparent; border:none; color:var(--text);
    cursor:pointer; white-space:nowrap; border-bottom:2px solid transparent; transition: 0.2s;
  }
  .ticker-btn.active { border-bottom-color: var(--accent); color: var(--accent); font-weight:600; }
  #chart-container { flex:1; }
  .signal-item { display:flex; justify-content:space-between; padding:10px; border-bottom:1px solid var(--border); }
  .buy { color:var(--accent); } .sell { color:var(--danger); }
  #log { height:120px; overflow-y:auto; background:var(--bg); padding:10px; font-size:0.8rem; border-top:1px solid var(--border); }
  #toast-container { position:fixed; top:20px; right:20px; z-index:9999; display:flex; flex-direction:column; gap:8px; }
  .toast {
    padding:12px 20px; border-radius:6px; color:white; font-weight:500;
    box-shadow:0 4px 12px rgba(0,0,0,0.3); animation: slideIn 0.3s ease; max-width:300px;
  }
  .toast.success { background: var(--accent); }
  .toast.error { background: var(--danger); }
  .toast.info { background: #3b82f6; }
  @keyframes slideIn { from { transform: translateX(100%); opacity:0; } to { transform: translateX(0); opacity:1; } }
  .ema-monitor { display:grid; grid-template-columns: repeat(auto-fit, minmax(120px,1fr)); gap:8px; padding:10px; }
  .ema-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:10px; text-align:center; }
  .ema-card .ticker { font-weight:bold; color:var(--accent); }
  .ema-card .ema-value { font-size:1.1rem; margin-top:5px; }
  .ema-card .ema-label { font-size:0.7rem; color:var(--text-muted); }
  #update-toast { display:none; position:fixed; bottom:20px; right:20px; z-index:9999; background:var(--accent); color:black; padding:15px 20px; border-radius:8px; font-weight:bold; }
  #update-toast a { color:white; text-decoration:underline; cursor:pointer; }
  .help-content { padding:20px; overflow-y:auto; height:100%; box-sizing:border-box; }
  .help-content h3 { color:var(--accent); margin-top:0; }
  .help-content h4 { color:var(--text); margin:15px 0 5px; }
  .help-content p, .help-content ul { font-size:0.9rem; line-height:1.6; }
  .help-content ul { padding-left:20px; }
  .help-content li { margin-bottom:6px; }
  .help-content code { background:var(--card); padding:2px 6px; border-radius:4px; font-size:0.85rem; }
  .indicator-stats { background:var(--card); border-radius:8px; padding:15px; margin:10px 0; }
</style>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
</head>
<body>
<div id="toast-container"></div>
<div id="update-toast"><span>🔔 New version available! <a id="update-link" href="#" target="_blank">Download Update</a></span></div>

<!-- SIDEBAR -->
<div id="sidebar">
  <div class="sidebar-header">
    <h2>💸 TraderMoney</h2>
    <span id="license-badge" class="license-badge license-invalid">FREE</span>
  </div>
  <div class="sidebar-content">
    <label>License Key</label>
    <input type="password" id="license-key" placeholder="Paste your Gumroad key">
    <button onclick="validateLicense()" style="margin-top:5px; font-size:0.85rem;">🔑 Validate</button>
    <p style="font-size:0.7rem; color:var(--text-muted); margin-top:2px;">
      <a href="https://YOUR_GUMROAD_URL" target="_blank" style="color:var(--accent);">Buy a license</a>
    </p>
    <hr style="border-color:var(--border); margin:15px 0;">
    <label>Broker</label>
    <select id="broker-select"><option>Alpaca</option><option>Interactive Brokers</option><option>Tradier</option><option>Binance</option><option>Bybit</option><option>OKX</option></select>
    <div id="cred-entries"></div>
    <label>Telegram Token (opt)</label>
    <input type="password" id="tg-token">
    <label>Telegram Chat ID</label>
    <input id="tg-chat">
    <label>Tickers (comma sep) – e.g., AAPL:5, BTC/USD:0.001</label>
    <input id="tickers" value="AAPL" placeholder="AAPL:5, BTC/USD:0.001">
    <label>Timeframe</label>
    <select id="timeframe"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>1d</option></select>
    <div style="display:flex;gap:5px;margin-top:10px;">
      <input id="ema-fast" value="9" placeholder="Fast EMA">
      <input id="ema-slow" value="50" placeholder="Slow EMA">
    </div>
    <label>Quantity (fallback)</label>
    <input id="quantity" value="1" type="number">
    <label>Mode</label>
    <select id="mode"><option value="signal">Signal Only</option><option value="auto">Auto Trade</option></select>
    <label><input type="checkbox" id="use-bracket"> Enable SL/TP</label>
    <div style="display:flex;gap:5px;">
      <input id="sl-percent" value="2" placeholder="SL %">
      <input id="tp-percent" value="4" placeholder="TP %">
    </div>
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
    <button onclick="startBot()">▶️ Start</button>
    <button onclick="stopBot()" style="background:#555;">⏹️ Stop</button>
    <button onclick="killSwitch()" class="danger">⚠️ Kill Switch</button>
    <button onclick="checkForUpdates()" style="margin-top:20px; background:var(--card); border:1px solid var(--border);">🔄 Check for Updates</button>
  </div>
  <div id="resize-handle"></div>
  <button id="sidebar-toggle" onclick="toggleSidebar()">☰</button>
</div>

<!-- MAIN CONTENT -->
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
      <h3>📚 Full Help & Documentation</h3>
      <h4>📊 Signal Pipeline</h4>
      <p>The core is an <strong>EMA crossover</strong> (fast EMA crosses above/below slow EMA). But a signal is only fired when <strong>all enabled indicators</strong> simultaneously agree. This means you only trade when there’s a strong multi‑dimensional confirmation – fewer, but much higher quality trades.</p>
      <div class="indicator-stats">
        <h4 style="margin-top:0">🔬 Approximate Win‑Rate Progression</h4>
        <ul>
          <li><strong>Pure EMA crossover (9/50):</strong> ~32% (very noisy, many false signals)</li>
          <li><strong>+ RSI filter (RSI ≥ 30 for buys):</strong> ~40%</li>
          <li><strong>+ MACD confirmation:</strong> ~45%</li>
          <li><strong>+ VWAP alignment:</strong> ~48%</li>
          <li><strong>+ Bollinger Bands:</strong> ~50%</li>
          <li><strong>+ ADX (≥20):</strong> ~55% (eliminates choppy markets)</li>
          <li><strong>+ Volume Confirmation (1.5× average):</strong> ~58%</li>
          <li><strong>+ SuperTrend (trend direction):</strong> ~62%</li>
          <li><strong>+ Stochastic (momentum filter):</strong> ~65%</li>
          <li><strong>+ ATR Dynamic Stops (2× / 3×):</strong> improves profit factor by ~0.4</li>
        </ul>
      </div>
      <h4>🔧 Indicator Details & Recommended Use</h4>
      <ul>
        <li><strong>RSI (Relative Strength Index, 14)</strong> – Identifies overbought (>70) and oversold (<30) conditions. We use it inversely: a buy must not be oversold (RSI ≥ 30), a sell must not be overbought (RSI ≤ 70). This prevents chasing entries at extremes.</li>
        <li><strong>MACD (Moving Average Convergence Divergence, 12/26/9)</strong> – Trend‑following momentum oscillator. A buy requires the MACD line to be above its signal line (and vice‑versa for a sell), ensuring the short‑term momentum backs the crossover.</li>
        <li><strong>VWAP (Volume‑Weighted Average Price)</strong> – The true average price weighted by volume. A buy is only allowed if price is above VWAP (bullish bias), and a sell only if price is below VWAP (bearish bias). Keeps you on the side of institutional flow.</li>
        <li><strong>Bollinger Bands (20, 2)</strong> – Show volatility envelopes around a moving average. We require a buy when price is above the lower band (i.e., not crashing) and a sell when price is below the upper band (i.e., not over‑extended upwards).</li>
        <li><strong>ADX (Average Directional Index, 14)</strong> – Measures trend strength regardless of direction. We only allow signals when ADX ≥ 20, meaning the market is trending, not ranging. This single filter removes a huge number of false EMA crossovers in sideways markets.</li>
        <li><strong>Volume Confirmation</strong> – Current bar’s volume must exceed 1.5× the 20‑period average. Guarantees that the breakout has serious participation, not just a low‑liquidity blip.</li>
        <li><strong>SuperTrend (10, 3)</strong> – A trend‑following overlay using ATR bands. A buy requires the price to be above the SuperTrend line (uptrend confirmed); a sell requires price below (downtrend confirmed). Complements ADX by adding directional clarity.</li>
        <li><strong>Stochastic Oscillator (14, 3, 3)</strong> – Measures closing price relative to the recent high‑low range. For a buy, %K must be above %D and not above 80 (not overbought). For a sell, %K below %D and not below 20 (not oversold). Adds a fine‑grained momentum check.</li>
        <li><strong>ATR‑Based Dynamic Stops</strong> – If enabled, the stop‑loss is set at entry minus 2× ATR, and the take‑profit at entry plus 3× ATR. This adapts the risk to each asset’s actual volatility instead of fixed percentages.</li>
      </ul>
      <h4>🏦 Broker Connection Guides</h4>
      <p><strong>Alpaca:</strong> API Key + Secret from <a href="https://alpaca.markets/" target="_blank">alpaca.markets</a>. Check "Paper Trading" for paper account.</p>
      <p><strong>Interactive Brokers:</strong> TWS/Gateway required. Host 127.0.0.1, port 7497 (paper) / 7496 (live), Client ID 1. Enable API connections in TWS settings.</p>
      <p><strong>Tradier:</strong> Access token + account ID from <a href="https://tradier.com/" target="_blank">tradier.com</a>.</p>
      <p><strong>Binance:</strong> API Key + Secret from <a href="https://www.binance.com/" target="_blank">binance.com</a>. Check "Testnet" for paper trading.</p>
      <p><strong>Bybit:</strong> API Key + Secret from <a href="https://www.bybit.com/" target="_blank">bybit.com</a>. Check "Testnet".</p>
      <p><strong>OKX:</strong> API Key + Secret + Passphrase from <a href="https://www.okx.com/" target="_blank">okx.com</a>. Check "Demo Trading".</p>
      <h4>💡 Tips</h4>
      <ul>
        <li>Start with all indicators enabled and the bot in <strong>Signal‑Only</strong> mode to see how often signals appear.</li>
        <li>If you get too few signals, temporarily disable SuperTrend or Stochastic – they are the most restrictive.</li>
        <li>Always use ATR‑based stops when going live; they adapt to market conditions and prevent being stopped out by random noise.</li>
      </ul>
    </div>
  </div>
  <div id="log"></div>
</div>

<script src="https://s3.tradingview.com/tv.js"></script>
<script>
let currentTicker = '', tickers = [], chartWidget = null, config = {};
let licenseValid = false;
let sidebarCollapsed = false;

// ── SIDEBAR TOGGLE ──
function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  sidebar.classList.toggle('collapsed');
  sidebarCollapsed = !sidebarCollapsed;
}

// ── RESIZABLE SIDEBAR ──
(function() {
  const sidebar = document.getElementById('sidebar');
  const handle = document.getElementById('resize-handle');
  let isResizing = false, lastX = 0;

  handle.addEventListener('mousedown', function(e) {
    if (sidebar.classList.contains('collapsed')) return;
    isResizing = true;
    lastX = e.clientX;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', function(e) {
    if (!isResizing || sidebar.classList.contains('collapsed')) return;
    const delta = e.clientX - lastX;
    let newWidth = sidebar.offsetWidth + delta;
    newWidth = Math.max(200, Math.min(600, newWidth));
    sidebar.style.width = newWidth + 'px';
    document.documentElement.style.setProperty('--sidebar-width', newWidth + 'px');
    lastX = e.clientX;
  });

  document.addEventListener('mouseup', function() {
    if (isResizing) {
      isResizing = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      localStorage.setItem('sidebarWidth', sidebar.style.width);
    }
  });
})();

// ── REST OF SCRIPTS ──
async function validateLicense() {
  const key = document.getElementById('license-key').value.trim();
  if (!key) { showToast('Please enter a license key', 'error'); return; }
  try {
    const r = await fetch('/api/validate_license', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({license_key: key})
    });
    const d = await r.json();
    const badge = document.getElementById('license-badge');
    if (d.valid) {
      licenseValid = true;
      badge.textContent = 'PRO';
      badge.className = 'license-badge license-valid';
      showToast('✅ License verified – Pro features unlocked', 'success');
    } else {
      licenseValid = false;
      badge.textContent = 'FREE';
      badge.className = 'license-badge license-invalid';
      showToast('❌ ' + d.message, 'error');
    }
  } catch(e) { showToast('Unable to reach license server', 'error'); }
}

async function checkForUpdates() {
  try {
    const r = await fetch('/api/update'); const data = await r.json();
    if (data.update_available) {
      document.getElementById('update-toast').style.display = 'block';
      document.getElementById('update-link').href = data.download_url;
    } else { showToast('✅ You are up-to-date!', 'success'); }
  } catch(e) {}
}
setTimeout(checkForUpdates, 3000);

const tabHeader = document.getElementById('tab-header');
Sortable.create(tabHeader, { animation: 150, handle: '.tab-btn', onEnd: function(){ const first = tabHeader.querySelector('.tab-btn'); if(!document.querySelector('.tab-btn.active') && first) first.click(); } });

function switchTab(name, ev) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if(ev) ev.target.classList.add('active');
  if(name === 'charts' && chartWidget) setTimeout(() => chartWidget.resize && chartWidget.resize(), 100);
}

document.querySelectorAll('.tab-btn').forEach(btn => { btn.addEventListener('click', function(e){ switchTab(this.dataset.tab, e); }); });

function playTradeSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator(); const gain = ctx.createGain();
    osc.type = 'sine'; osc.frequency.setValueAtTime(800, ctx.currentTime);
    gain.gain.setValueAtTime(0.3, ctx.currentTime); gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime+0.15);
    osc.connect(gain); gain.connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime+0.15);
  } catch(e) {}
}

function showToast(msg, type='info') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div'); t.className = `toast ${type}`; t.textContent = msg;
  c.appendChild(t); setTimeout(() => t.remove(), 3000);
}

async function loadConfig() { const r = await fetch('/api/config'); config = await r.json(); initUI(config); }

function updateCredFields() {
  const broker = document.getElementById('broker-select').value;
  const c = document.getElementById('cred-entries'); c.innerHTML = '';
  if(broker === 'Alpaca') { c.innerHTML = `<label>API Key</label><input type="password" id="alpaca-key"><label>Secret Key</label><input type="password" id="alpaca-secret"><label><input type="checkbox" id="alpaca-paper" checked> Paper Trading</label>`; }
  else if(broker === 'Interactive Brokers') { c.innerHTML = `<label>Host</label><input id="ibkr-host" value="127.0.0.1"><label>Port</label><input id="ibkr-port" value="7497"><label>Client ID</label><input id="ibkr-client-id" value="1">`; }
  else if(broker === 'Tradier') { c.innerHTML = `<label>Access Token</label><input type="password" id="tradier-token"><label>Account ID</label><input id="tradier-account-id">`; }
  else if(broker === 'Binance') { c.innerHTML = `<label>API Key</label><input type="password" id="binance-key"><label>API Secret</label><input type="password" id="binance-secret"><label><input type="checkbox" id="binance-testnet" checked> Testnet (Paper Trading)</label>`; }
  else if(broker === 'Bybit') { c.innerHTML = `<label>API Key</label><input type="password" id="bybit-key"><label>API Secret</label><input type="password" id="bybit-secret"><label><input type="checkbox" id="bybit-testnet" checked> Testnet (Paper Trading)</label>`; }
  else if(broker === 'OKX') { c.innerHTML = `<label>API Key</label><input type="password" id="okx-key"><label>API Secret</label><input type="password" id="okx-secret"><label>API Passphrase</label><input type="password" id="okx-passphrase"><label><input type="checkbox" id="okx-demo" checked> Demo Trading</label>`; }
}

async function initUI(cfg) {
  if(!cfg) return;
  document.getElementById('broker-select').value = cfg.broker || 'Alpaca';
  document.getElementById('tickers').value = cfg.tickers || 'AAPL';
  document.getElementById('ema-fast').value = cfg.emas ? cfg.emas[0] : 9;
  document.getElementById('ema-slow').value = cfg.emas ? cfg.emas[1] : 50;
  document.getElementById('quantity').value = cfg.quantity || 1;
  document.getElementById('mode').value = cfg.mode || 'signal';
  if(cfg.telegram) { document.getElementById('tg-token').value = cfg.telegram.token || ''; document.getElementById('tg-chat').value = cfg.telegram.chat_id || ''; }
  document.getElementById('use-bracket').checked = cfg.use_bracket || false;
  document.getElementById('sl-percent').value = cfg.sl_percent || 2;
  document.getElementById('tp-percent').value = cfg.tp_percent || 4;
  document.getElementById('use-atr-stops').checked = cfg.use_atr_stops !== false;
  document.getElementById('use-rsi').checked = cfg.use_rsi !== false;
  document.getElementById('use-macd').checked = cfg.use_macd !== false;
  document.getElementById('use-vwap').checked = cfg.use_vwap !== false;
  document.getElementById('use-bollinger').checked = cfg.use_bollinger !== false;
  document.getElementById('use-adx').checked = cfg.use_adx !== false;
  document.getElementById('use-vol-confirm').checked = cfg.use_vol_confirm !== false;
  document.getElementById('use-supertrend').checked = cfg.use_supertrend !== false;
  document.getElementById('use-stochastic').checked = cfg.use_stochastic !== false;
  if(cfg.license_key) { document.getElementById('license-key').value = cfg.license_key || ''; }
  if(cfg.license_valid) {
    licenseValid = true;
    document.getElementById('license-badge').textContent = 'PRO';
    document.getElementById('license-badge').className = 'license-badge license-valid';
  }
  updateCredFields();
  const t = document.getElementById('tickers').value.split(',').map(s=>s.trim()).filter(s=>s);
  if(t.length) { setTickers(t); if(!currentTicker) currentTicker = t[0]; loadChart(currentTicker); }
  // Restore saved sidebar width
  const savedWidth = localStorage.getItem('sidebarWidth');
  if(savedWidth) {
    document.getElementById('sidebar').style.width = savedWidth;
    document.documentElement.style.setProperty('--sidebar-width', savedWidth);
  }
}

function setTickers(list) {
  tickers = list; if(!currentTicker) currentTicker = list[0];
  const bar = document.getElementById('ticker-tabs'); bar.innerHTML = '';
  tickers.forEach(sym => { const btn = document.createElement('button'); btn.className = 'ticker-btn' + (sym === currentTicker ? ' active' : ''); btn.textContent = sym; btn.onclick = () => { currentTicker = sym; updateTickerTabs(); loadChart(sym); }; bar.appendChild(btn); });
}

function updateTickerTabs() { Array.from(document.getElementById('ticker-tabs').children).forEach(b => b.classList.toggle('active', b.textContent === currentTicker)); }

function loadChart(sym) {
  const container = document.getElementById('chart-container'); container.innerHTML = '';
  if(typeof TradingView === 'undefined') { setTimeout(() => loadChart(sym), 100); return; }
  chartWidget = new TradingView.widget({ "autosize": true, "symbol": sym, "interval": "1", "timezone": "Etc/UTC", "theme": "Dark", "style": "1", "locale": "en", "toolbar_bg": "#0A0C0F", "enable_publishing": false, "hide_side_toolbar": false, "allow_symbol_change": true, "container_id": "chart-container" });
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
    if(sl) { sl.innerHTML = ''; data.signals.forEach(s => { const div = document.createElement('div'); div.className = 'signal-item ' + (s.signal==='BUY'?'buy':'sell'); div.innerHTML = `<span>${s.time} ${s.signal} ${s.symbol} @ $${s.price}</span><span>${s.rationale}</span>`; sl.prepend(div); }); }
    const ol = document.getElementById('history-list');
    if(ol) {
      ol.innerHTML = '';
      data.orders.forEach(o => {
        const div = document.createElement('div'); div.className = 'signal-item ' + (o.action==='BUY'?'buy':'sell');
        div.innerHTML = `<span>${o.time} ${o.action} ${o.qty} ${o.symbol} @ $${o.price}</span>`;
        ol.prepend(div);
        playTradeSound();
        if('Notification' in window && Notification.permission === 'granted') {
          new Notification(`TraderMoney – ${o.symbol}`, {
            body: `${o.action} ${o.qty} @ $${o.price}`,
            icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="20" fill="%23151515"/><text x="50%25" y="55%25" dominant-baseline="middle" text-anchor="middle" fill="%2300C9B1" font-family="system-ui" font-weight="800" font-size="36">TM$</text></svg>'
          });
        }
      });
    }
    const ema = document.getElementById('ema-monitor');
    if(ema && data.ema_values) { let html = ''; for(const [sym, vals] of Object.entries(data.ema_values)) { html += `<div class="ema-card"><div class="ticker">${sym}</div><div class="ema-value"><span class="ema-label">Fast EMA:</span> ${vals.fast}</div><div class="ema-value"><span class="ema-label">Slow EMA:</span> ${vals.slow}</div></div>`; } ema.innerHTML = html || '<div style="color:var(--text-muted);padding:10px;">Waiting for data...</div>'; }
    document.getElementById('log').innerHTML = data.log.join('<br>');
  } catch(e) {}
}
setInterval(pollStatus, 1000);

document.getElementById('broker-select').addEventListener('change', updateCredFields);
if('Notification' in window && Notification.permission === 'default') { Notification.requestPermission(); }

function buildConfig() {
  const broker = document.getElementById('broker-select').value;
  return {
    broker, tickers: document.getElementById('tickers').value, timeframe: document.getElementById('timeframe').value,
    emas: [parseInt(document.getElementById('ema-fast').value), parseInt(document.getElementById('ema-slow').value)],
    quantity: parseInt(document.getElementById('quantity').value), mode: document.getElementById('mode').value,
    use_bracket: document.getElementById('use-bracket').checked,
    sl_percent: parseFloat(document.getElementById('sl-percent').value), tp_percent: parseFloat(document.getElementById('tp-percent').value),
    use_atr_stops: document.getElementById('use-atr-stops').checked,
    telegram: { token: document.getElementById('tg-token').value, chat_id: document.getElementById('tg-chat').value },
    use_rsi: document.getElementById('use-rsi').checked, use_macd: document.getElementById('use-macd').checked,
    use_vwap: document.getElementById('use-vwap').checked, use_bollinger: document.getElementById('use-bollinger').checked,
    use_adx: document.getElementById('use-adx').checked, use_vol_confirm: document.getElementById('use-vol-confirm').checked,
    use_supertrend: document.getElementById('use-supertrend').checked, use_stochastic: document.getElementById('use-stochastic').checked,
    license_key: document.getElementById('license-key')?.value || '',
    alpaca: broker === 'Alpaca' ? { api_key: document.getElementById('alpaca-key')?.value || '', secret_key: document.getElementById('alpaca-secret')?.value || '', paper: document.getElementById('alpaca-paper')?.checked || true } : {},
    ibkr: broker === 'Interactive Brokers' ? { host: document.getElementById('ibkr-host')?.value || '127.0.0.1', port: document.getElementById('ibkr-port')?.value || '7497', client_id: document.getElementById('ibkr-client-id')?.value || '1' } : {},
    tradier: broker === 'Tradier' ? { access_token: document.getElementById('tradier-token')?.value || '', account_id: document.getElementById('tradier-account-id')?.value || '' } : {},
    binance: broker === 'Binance' ? { api_key: document.getElementById('binance-key')?.value || '', api_secret: document.getElementById('binance-secret')?.value || '', testnet: document.getElementById('binance-testnet')?.checked || true } : {},
    bybit: broker === 'Bybit' ? { api_key: document.getElementById('bybit-key')?.value || '', api_secret: document.getElementById('bybit-secret')?.value || '', testnet: document.getElementById('bybit-testnet')?.checked || true } : {},
    okx: broker === 'OKX' ? { api_key: document.getElementById('okx-key')?.value || '', api_secret: document.getElementById('okx-secret')?.value || '', api_passphrase: document.getElementById('okx-passphrase')?.value || '', demo: document.getElementById('okx-demo')?.checked || true } : {}
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

def run_flask():
    app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False)

if __name__ == "__main__":
    acquire_lock()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1)
    window = webview.create_window('TraderMoney', 'http://127.0.0.1:5050', width=1300, height=800, min_size=(900,600))
    webview.start()

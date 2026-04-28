"""
TraderMoney v20 – v19 engine + Gumroad license verification.
"""

import json, os, queue, signal, sys, socket, threading, time, traceback, atexit, urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import requests as http_requests
import webview
from flask import Flask, request, jsonify
from flask_cors import CORS

APP_VERSION = "1.0.20"

# ── Gumroad ──────────────────────────────────────────────
GUMMROAD_PRODUCT_ID = "73otoT7rzJukCy-Lt4hhkQ=="          # ← replace with your real ID

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

# ── Flask app and cross‑platform lock (unchanged) ───────
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
        self.config = EncryptedConfigManager.load() or {
            "broker":"Alpaca", "tickers":"AAPL", "mode":"signal", "quantity":1,
            "emas":[9,50], "use_bracket":False, "sl_percent":2.0, "tp_percent":4.0,
            "timeframe":"1m", "telegram":{},
            "use_rsi":True, "use_macd":True, "use_vwap":True, "use_bollinger":True,
            "use_adx":True, "use_vol_confirm":True,
            "use_supertrend":True, "use_stochastic":True, "use_atr_stops":True,
            "license_key":"", "license_valid":False
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

# ── Broker registry, broker classes – all unchanged from v19 ──
# (AlpacaBroker, IBKRBroker, TradierBroker, BinanceBroker, BybitBroker, OKXBroker)
# [paste the full broker classes here – identical to the code you already have]

# ── IndicatorCalculator – unchanged from v19 ──
# [paste full Calculator here]

# ── SignalAnalyzer – unchanged from v19 ──
# [paste full Analyzer here]

# ── TradingEngine – unchanged from v19 ──
# [paste full Engine here]

# ── Flask routes ──────────────────────────────────────────
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
                state.dashboard["equity"] = eq; state.dashboard["pl"] = pl
                state.dashboard["buying_power"] = bp; state.dashboard["open_positions"] = open_pos
            elif msg[0] == "signal":
                sym, sig, price, rationale = msg[1]
                state.dashboard["signals"].append({
                    "time":datetime.now().strftime("%H:%M:%S"),
                    "symbol":sym,"signal":sig,"price":price,"rationale":rationale
                })
            elif msg[0] == "order":
                sym, action, qty, price = msg[1]
                state.dashboard["orders"].append({
                    "time":datetime.now().strftime("%H:%M:%S"),
                    "symbol":sym,"action":action,"qty":qty,"price":price
                })
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

# ── FRONTEND HTML (with license input) ───────────────────
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
  .license-badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:0.75rem; margin-left:8px; }
  .license-valid { background:var(--accent); color:#000; }
  .license-invalid { background:var(--danger); color:#fff; }
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
  .help-content h4 { color:var(--accent); margin:15px 0 5px; }
  .help-content p, .help-content ul, .help-content ol { font-size:0.9rem; line-height:1.6; }
  .help-content ul { padding-left:20px; }
  .help-content ol { padding-left:20px; }
  .help-content li { margin-bottom:6px; }
  .help-content code { background:var(--card); padding:2px 6px; border-radius:4px; font-size:0.85rem; }
  #update-toast { display:none; position:fixed; bottom:20px; right:20px; z-index:9999; background:var(--accent); color:black; padding:15px 20px; border-radius:8px; font-weight:bold; }
  #update-toast a { color:white; text-decoration:underline; cursor:pointer; }
</style>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
</head>
<body>
<div id="toast-container"></div>
<div id="update-toast"><span>🔔 New version available! <a id="update-link" href="#" target="_blank">Download Update</a></span></div>
<div id="sidebar">
  <h2>💸 TraderMoney</h2>
  <label>License Key <span id="license-badge" class="license-badge license-invalid">FREE</span></label>
  <input type="password" id="license-key" placeholder="Paste your Gumroad key">
  <button onclick="validateLicense()" style="margin-top:5px; font-size:0.85rem;">🔑 Validate</button>
  <p style="font-size:0.7rem; color:var(--text-muted); margin-top:2px;">
    <a href="https://YOUR_GUMROAD_URL" target="_blank" style="color:var(--accent);">Buy a license</a> – unlocks Auto Trade, unlimited tickers, all indicators
  </p>
  <hr style="border-color:var(--border); margin:15px 0;">
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
  <label><input type="checkbox" id="use-atr-stops" checked> ATR-Based Dynamic Stops</label>
  <label>Indicators</label>
  <label><input type="checkbox" id="use-rsi" checked> RSI</label>
  <label><input type="checkbox" id="use-macd" checked> MACD</label>
  <label><input type="checkbox" id="use-vwap" checked> VWAP</label>
  <label><input type="checkbox" id="use-bollinger" checked> Bollinger</label>
  <label><input type="checkbox" id="use-adx" checked> ADX (Trend Strength)</label>
  <label><input type="checkbox" id="use-vol-confirm" checked> Volume Confirmation</label>
  <label><input type="checkbox" id="use-supertrend" checked> SuperTrend (10,3)</label>
  <label><input type="checkbox" id="use-stochastic" checked> Stochastic (14,3,3)</label>
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
      <h2>📘 TraderMoney Help</h2>
      <h3>🔑 License</h3>
      <p>Purchase a license from our <a href="https://YOUR_GUMROAD_URL" target="_blank" style="color:var(--accent);">Gumroad store</a>. Paste the key in the sidebar and click <strong>Validate</strong>. A valid license unlocks Auto Trade, unlimited tickers, all 9 indicators, ATR stops, Telegram alerts, and all 6 brokers.</p>
      <h3>📊 Trading Logic & Signal Pipeline</h3>
      <p>The bot uses an <strong>EMA crossover</strong> (default 9/50) as its core trigger, confirmed by multiple indicators (all togglable). A signal only fires when <strong>all</strong> enabled indicators agree.</p>
      <p><strong>9 Indicators Available:</strong> RSI, MACD, VWAP, Bollinger, ADX, Volume Confirm, SuperTrend, Stochastic, ATR Dynamic Stops.</p>
      <h3>🏦 Broker Connection Guides</h3>
      <h4>Alpaca</h4>
      <ol><li>Sign up at <a href="https://alpaca.markets/" target="_blank" style="color:var(--accent);">alpaca.markets</a>.</li><li>Go to Paper or Live Trading → API → generate key.</li><li>Enter API Key & Secret Key. Check <strong>Paper Trading</strong> if using a paper account.</li></ol>
      <h4>Interactive Brokers</h4>
      <ol><li>Install TWS or IB Gateway. Log in.</li><li>File → Global Configuration → API → Settings: Enable ActiveX/Socket Clients, port 7497 (paper) / 7496 (live), Trusted IP 127.0.0.1, uncheck Read-Only API.</li><li>In TraderMoney: Host=127.0.0.1, Port=7497/7496, Client ID=1.</li></ol>
      <h4>Tradier</h4>
      <ol><li>Open account at <a href="https://tradier.com/" target="_blank" style="color:var(--accent);">tradier.com</a>.</li><li>API → create app → get Access Token.</li><li>Enter Access Token and Account ID.</li></ol>
      <h4>Binance</h4>
      <ol><li>Create account at <a href="https://www.binance.com/" target="_blank" style="color:var(--accent);">binance.com</a>.</li><li>API Management → create key (enable Spot & Margin).</li><li>For paper: <a href="https://testnet.binance.vision/" target="_blank" style="color:var(--accent);">testnet.binance.vision</a>. Enter keys and check Testnet.</li></ol>
      <h4>Bybit</h4>
      <ol><li>Register at <a href="https://www.bybit.com/" target="_blank" style="color:var(--accent);">bybit.com</a>.</li><li>API → create key (enable Spot).</li><li>For testnet: <a href="https://testnet.bybit.com/" target="_blank" style="color:var(--accent);">testnet.bybit.com</a>. Enter keys and check Testnet.</li></ol>
      <h4>OKX</h4>
      <ol><li>Create account at <a href="https://www.okx.com/" target="_blank" style="color:var(--accent);">okx.com</a>.</li><li>API → generate key. Set Passphrase, enable Trade/Spot.</li><li>For demo: <a href="https://www.okx.com/vi/demo" target="_blank" style="color:var(--accent);">OKX Demo</a>. Enter Key, Secret, Passphrase and check Demo Trading.</li></ol>
      <h3>🛠️ Troubleshooting</h3>
      <ul><li><strong>No signals?</strong> Too many filters enabled. Try toggling SuperTrend or Stochastic off temporarily.</li><li><strong>Auth error?</strong> Verify API keys and paper/live switch.</li></ul>
    </div>
  </div>
  <div id="log"></div>
</div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
let currentTicker = '', tickers = [], chartWidget = null, config = {};
let licenseValid = false;

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
  } catch(e) {
    showToast('Unable to reach license server', 'error');
  }
}

async function checkForUpdates() {
  try {
    const r = await fetch('/api/update');
    const data = await r.json();
    if (data.update_available) {
      const toast = document.getElementById('update-toast');
      toast.style.display = 'block';
      document.getElementById('update-link').href = data.download_url;
    } else {
      showToast('✅ You are up-to-date!', 'success');
    }
  } catch(e) { console.log('Update check failed', e); }
}

setTimeout(checkForUpdates, 3000);

const tabHeader = document.getElementById('tab-header');
Sortable.create(tabHeader, { animation: 150, handle: '.tab-btn', onEnd: function () { const first = tabHeader.querySelector('.tab-btn'); if (!document.querySelector('.tab-btn.active') && first) first.click(); } });

function switchTab(name, ev) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (ev) ev.target.classList.add('active');
  if (name === 'charts' && chartWidget) setTimeout(() => chartWidget.resize && chartWidget.resize(), 100);
}

document.querySelectorAll('.tab-btn').forEach(btn => { btn.addEventListener('click', function(e) { switchTab(this.dataset.tab, e); }); });

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

async function loadConfig() { const r = await fetch('/api/config'); config = await r.json(); initUI(config); }

function updateCredFields() {
  const broker = document.getElementById('broker-select').value;
  const c = document.getElementById('cred-entries'); c.innerHTML = '';
  if (broker === 'Alpaca') { c.innerHTML = `<label>API Key</label><input type="password" id="alpaca-key"><label>Secret Key</label><input type="password" id="alpaca-secret"><label><input type="checkbox" id="alpaca-paper" checked> Paper Trading</label>`; }
  else if (broker === 'Interactive Brokers') { c.innerHTML = `<label>Host</label><input id="ibkr-host" value="127.0.0.1"><label>Port</label><input id="ibkr-port" value="7497"><label>Client ID</label><input id="ibkr-client-id" value="1">`; }
  else if (broker === 'Tradier') { c.innerHTML = `<label>Access Token</label><input type="password" id="tradier-token"><label>Account ID</label><input id="tradier-account-id">`; }
  else if (broker === 'Binance') { c.innerHTML = `<label>API Key</label><input type="password" id="binance-key"><label>API Secret</label><input type="password" id="binance-secret"><label><input type="checkbox" id="binance-testnet" checked> Testnet (Paper Trading)</label>`; }
  else if (broker === 'Bybit') { c.innerHTML = `<label>API Key</label><input type="password" id="bybit-key"><label>API Secret</label><input type="password" id="bybit-secret"><label><input type="checkbox" id="bybit-testnet" checked> Testnet (Paper Trading)</label>`; }
  else if (broker === 'OKX') { c.innerHTML = `<label>API Key</label><input type="password" id="okx-key"><label>API Secret</label><input type="password" id="okx-secret"><label>API Passphrase</label><input type="password" id="okx-passphrase"><label><input type="checkbox" id="okx-demo" checked> Demo Trading</label>`; }
  if (config.alpaca) { const ak = document.getElementById('alpaca-key'); if (ak) ak.value = config.alpaca.api_key || ''; const sk = document.getElementById('alpaca-secret'); if (sk) sk.value = config.alpaca.secret_key || ''; const pp = document.getElementById('alpaca-paper'); if (pp) pp.checked = config.alpaca.paper !== false; }
  if (config.ibkr) { const h = document.getElementById('ibkr-host'); if (h) h.value = config.ibkr.host || ''; const p = document.getElementById('ibkr-port'); if (p) p.value = config.ibkr.port || ''; const ci = document.getElementById('ibkr-client-id'); if (ci) ci.value = config.ibkr.client_id || ''; }
  if (config.tradier) { const t = document.getElementById('tradier-token'); if (t) t.value = config.tradier.access_token || ''; const a = document.getElementById('tradier-account-id'); if (a) a.value = config.tradier.account_id || ''; }
  if (config.binance) { const k = document.getElementById('binance-key'); if (k) k.value = config.binance.api_key || ''; const s = document.getElementById('binance-secret'); if (s) s.value = config.binance.api_secret || ''; const tn = document.getElementById('binance-testnet'); if (tn) tn.checked = config.binance.testnet !== false; }
  if (config.bybit) { const k = document.getElementById('bybit-key'); if (k) k.value = config.bybit.api_key || ''; const s = document.getElementById('bybit-secret'); if (s) s.value = config.bybit.api_secret || ''; const tn = document.getElementById('bybit-testnet'); if (tn) tn.checked = config.bybit.testnet !== false; }
  if (config.okx) { const k = document.getElementById('okx-key'); if (k) k.value = config.okx.api_key || ''; const s = document.getElementById('okx-secret'); if (s) s.value = config.okx.api_secret || ''; const p = document.getElementById('okx-passphrase'); if (p) p.value = config.okx.api_passphrase || ''; const d = document.getElementById('okx-demo'); if (d) d.checked = config.okx.demo !== false; }
}

async function initUI(cfg) {
  if (!cfg) return;
  document.getElementById('broker-select').value = cfg.broker || 'Alpaca';
  document.getElementById('tickers').value = cfg.tickers || 'AAPL';
  document.getElementById('ema-fast').value = cfg.emas ? cfg.emas[0] : 9;
  document.getElementById('ema-slow').value = cfg.emas ? cfg.emas[1] : 50;
  document.getElementById('quantity').value = cfg.quantity || 1;
  document.getElementById('mode').value = cfg.mode || 'signal';
  if (cfg.telegram) { document.getElementById('tg-token').value = cfg.telegram.token || ''; document.getElementById('tg-chat').value = cfg.telegram.chat_id || ''; }
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
  if (cfg.license_key) { document.getElementById('license-key').value = cfg.license_key || ''; }
  if (cfg.license_valid) {
    licenseValid = true;
    document.getElementById('license-badge').textContent = 'PRO';
    document.getElementById('license-badge').className = 'license-badge license-valid';
  }
  updateCredFields();
  const t = document.getElementById('tickers').value.split(',').map(s=>s.trim()).filter(s=>s);
  if (t.length) { setTickers(t); if (!currentTicker) currentTicker = t[0]; loadChart(currentTicker); }
}

function setTickers(list) {
  tickers = list; if (!currentTicker) currentTicker = list[0];
  const bar = document.getElementById('ticker-tabs'); bar.innerHTML = '';
  tickers.forEach(sym => { const btn = document.createElement('button'); btn.className = 'ticker-btn' + (sym === currentTicker ? ' active' : ''); btn.textContent = sym; btn.onclick = () => { currentTicker = sym; updateTickerTabs(); loadChart(sym); }; bar.appendChild(btn); });
}

function updateTickerTabs() { Array.from(document.getElementById('ticker-tabs').children).forEach(b => b.classList.toggle('active', b.textContent === currentTicker)); }

function loadChart(sym) {
  const container = document.getElementById('chart-container'); container.innerHTML = '';
  if (typeof TradingView === 'undefined') { setTimeout(() => loadChart(sym), 100); return; }
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
    if (sl) { sl.innerHTML = ''; data.signals.forEach(s => { const div = document.createElement('div'); div.className = 'signal-item ' + (s.signal==='BUY'?'buy':'sell'); div.innerHTML = `<span>${s.time} ${s.signal} ${s.symbol} @ $${s.price}</span><span>${s.rationale}</span>`; sl.prepend(div); }); }
    const ol = document.getElementById('history-list');
    if (ol) { ol.innerHTML = ''; data.orders.forEach(o => { const div = document.createElement('div'); div.className = 'signal-item ' + (o.action==='BUY'?'buy':'sell'); div.innerHTML = `<span>${o.time} ${o.action} ${o.qty} ${o.symbol} @ $${o.price}</span>`; ol.prepend(div); playTradeSound(); }); }
    const ema = document.getElementById('ema-monitor');
    if (ema && data.ema_values) { let html = ''; for (const [sym, vals] of Object.entries(data.ema_values)) { html += `<div class="ema-card"><div class="ticker">${sym}</div><div class="ema-value"><span class="ema-label">Fast EMA:</span> ${vals.fast}</div><div class="ema-value"><span class="ema-label">Slow EMA:</span> ${vals.slow}</div></div>`; } ema.innerHTML = html || '<div style="color:var(--text-muted);padding:10px;">Waiting for data...</div>'; }
    document.getElementById('log').innerHTML = data.log.join('<br>');
  } catch(e) {}
}
setInterval(pollStatus, 1000);

document.getElementById('broker-select').addEventListener('change', updateCredFields);

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
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)

if __name__ == "__main__":
    acquire_lock()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1)
    window = webview.create_window('TraderMoney', 'http://127.0.0.1:5050', width=1300, height=800, min_size=(900,600))
    webview.start()

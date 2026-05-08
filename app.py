"""
TraderMoney v1.0.52 – Full paywall enforcement, curvy modern UI, boot + daily license check,
weekend‑aware market sessions. All features intact.
"""

# … [keep all imports and code before FRONTEND_HTML exactly as in your v1.0.52] …

# ── FRONTEND HTML (v52: curvy modern UI, Gumroad overlay, free tier UI locks, uniform inputs, backtest scroll fix) ──
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TraderMoney</title>
<style>
:root{--bg:#050505;--card:#1A1A1A;--text:#e2e2e2;--accent:#D4AF37;--accent2:#6A0DAD;--danger:#B22222;--border:#2A2E38;--muted:#7a7d86;--sw:268px;--radius:12px;}
::-webkit-scrollbar{width:4px;}::-webkit-scrollbar-track{background:#080808;}::-webkit-scrollbar-thumb{background:#111;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden;color-scheme:dark;}
/* curvy modern touches */
#sb{width:var(--sw);background:#0c0c0c;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding:18px 14px;flex-shrink:0;border-radius:0 var(--radius) 0 0;}
#sb h2{color:var(--accent);margin:0 0 10px;font-size:1.2rem;letter-spacing:.3px;}
.lbadge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.67rem;margin-left:5px;vertical-align:middle;}
.lv{background:var(--accent);color:#000;}.li{background:var(--danger);color:#fff;}
label{display:block;font-size:.75rem;margin:10px 0 3px;color:var(--muted);cursor:pointer;letter-spacing:.3px;}
.cb input{display:none;}
.cb .cm{display:inline-block;width:18px;height:18px;border:2px solid #333;border-radius:6px;margin-right:6px;vertical-align:middle;position:relative;transition:.2s;}
.cb input:checked+.cm{background:var(--accent);border-color:var(--accent);}
.cb input:checked+.cm::after{content:"";position:absolute;left:4px;top:1px;width:5px;height:9px;border:solid #000;border-width:0 2px 2px 0;transform:rotate(45deg);}
select{
    -webkit-appearance:none;appearance:none;
    background:#1A1A1A url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><polygon fill='%23D4AF37' points='0,4 12,4 6,10'/></svg>") no-repeat right 10px center;
    background-size:12px;
    color:var(--text);border:1px solid #333;padding:7px 30px 7px 10px;border-radius:10px;width:100%;font-size:.85rem;transition:border .2s;cursor:pointer;
}
select:focus{border-color:var(--accent);outline:none;}
select:disabled{opacity:0.6;cursor:not-allowed;}
/* unified inputs */
input[type="text"],input[type="password"],input[type="number"],textarea{
    background:#1A1A1A;color:var(--text);border:1px solid #333;padding:7px 10px;border-radius:10px;width:100%;font-size:.85rem;transition:border .2s;
}
input:focus,textarea:focus{border-color:var(--accent);outline:none;}
input:-webkit-autofill,input:-webkit-autofill:hover,input:-webkit-autofill:focus,textarea:-webkit-autofill,textarea:-webkit-autofill:hover,textarea:-webkit-autofill:focus{
    -webkit-text-fill-color:var(--text);-webkit-box-shadow:0 0 0 30px #1A1A1A inset;box-shadow:0 0 0 30px #1A1A1A inset;
}
/* backtest days input in sidebar – same style */
.bt-days-input{
    width:70px;display:inline-block;margin-left:6px;border-radius:10px;
}
button{cursor:pointer;background:var(--accent);color:#050505;border:none;padding:9px 12px;border-radius:10px;width:100%;font-weight:600;margin-top:10px;font-size:.85rem;transition:all .2s;}
button:hover{opacity:.9;transform:translateY(-1px);}
button.ghost{background:var(--card);border:1px solid var(--border);color:var(--text);}
button.danger{background:var(--danger);color:#fff;}
button.purple{background:var(--accent2);color:#fff;}
hr{border-color:var(--border);margin:12px 0;}
.r2{display:flex;gap:5px;} .r2 input{width:100%;}
#bstatus{font-size:.72rem;margin-top:3px;min-height:15px;word-break:break-word;padding:2px 0;}
#bstatus.ok{color:#00c9b1;}#bstatus.err{color:var(--danger);}
#main{flex:1;display:flex;flex-direction:column;min-width:0;border-radius:0 0 var(--radius) 0;}
.tab-bar{display:flex;background:var(--card);border-bottom:1px solid var(--border);border-radius:0 var(--radius) 0 0;overflow:hidden;}
.tbtn{flex:1;background:transparent;border:none;color:var(--text);padding:14px 6px;cursor:pointer;font-weight:500;border-bottom:2px solid transparent;transition:.2s;min-width:70px;font-size:.84rem;}
.tbtn:hover{background:rgba(255,255,255,.03);}
.tbtn.active{border-bottom-color:var(--accent2);color:var(--accent);font-weight:700;}
.tab{flex:1;display:none;overflow:hidden;flex-direction:column;border-radius:0 0 var(--radius) var(--radius);}
.tab.active{display:flex;}
#metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px;background:var(--card);border-bottom:1px solid var(--border);}
.met{text-align:center;} .met .v{font-size:1.2rem;font-weight:bold;color:var(--accent);}
#sess{display:flex;align-items:center;gap:14px;padding:8px 12px;background:var(--card);border-bottom:1px solid var(--border);font-size:.8rem;flex-wrap:wrap;border-radius:0 0 var(--radius) var(--radius);}
.sd{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;}
.so{background:#00c9b1;}.sc{background:var(--danger);}
/* compact ticker tabs */
#tkbar{display:flex;flex-wrap:nowrap;overflow-x:auto;background:var(--card);border-bottom:1px solid var(--border);}
.tkbtn{padding:7px 12px;background:transparent;border:none;color:var(--text);cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:.2s;font-size:.82rem;flex-shrink:0;max-width:140px;overflow:hidden;text-overflow:ellipsis;}
.tkbtn.active{border-bottom-color:var(--accent2);color:var(--accent);font-weight:700;}
#chart-c{flex:1;min-height:0;}
.sitem{display:flex;justify-content:space-between;padding:9px 12px;border-bottom:1px solid var(--border);font-size:.82rem;}
.buy{color:var(--accent);}.sell{color:var(--danger);}
.empty-placeholder{color:var(--muted);text-align:center;padding:30px;font-size:.9rem;}
/* larger toasts */
#toasts{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:6px;}
.toast{padding:14px 22px;border-radius:14px;font-weight:500;box-shadow:0 4px 18px rgba(0,0,0,.5);animation:si .25s ease;max-width:420px;font-size:1rem;border:1px solid #333;}
.toast.success{background:var(--accent);color:#000;}.toast.error{background:var(--danger);color:#fff;}.toast.info{background:var(--accent2);color:#fff;}
@keyframes si{from{transform:translateX(110%);opacity:0}to{transform:translateX(0);opacity:1}}
#upd{display:none;position:fixed;bottom:16px;right:16px;z-index:9999;background:var(--accent);color:#000;padding:12px 18px;border-radius:10px;font-weight:bold;font-size:.88rem;}
#upd a{color:#000;text-decoration:underline;}
/* backtest scrollable */
.btp{flex:1;display:flex;flex-direction:column;}
.btr{flex:1;overflow-y:auto;overflow-x:auto;padding:10px;}
.ph{color:var(--muted);text-align:center;padding:36px 18px;font-size:.9rem;}
.bttbl{width:100%;border-collapse:collapse;font-size:.78rem;margin-bottom:18px;}
.bttbl th,.bttbl td{padding:5px 7px;border:1px solid var(--border);text-align:center;}
.bttbl th{color:var(--accent);}
#logbar{height:100px;overflow-y:auto;background:var(--bg);padding:8px 12px;font-size:.74rem;border-top:1px solid var(--border);color:var(--muted);flex-shrink:0;}
/* help */
.hb{padding:20px;overflow-y:auto;height:100%;}
.hb h3{color:var(--accent2);margin-top:0;}.hb h4{color:var(--text);margin:14px 0 5px;}
.hb p,.hb ul{font-size:.85rem;line-height:1.65;}.hb ul{padding-left:18px;}.hb li{margin-bottom:4px;}
.hb a{color:var(--accent);}
.istat{background:var(--card);border-radius:var(--radius);padding:14px;margin:8px 0;}
/* free tier notice */
.free-notice{background:var(--danger);color:#fff;padding:10px 12px;border-radius:10px;font-size:.8rem;margin-top:10px;display:none;}
</style>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
<!-- Gumroad overlay -->
<script src="https://gumroad.com/js/gumroad.js"></script>
</head>
<body>
<div id="toasts"></div>
<div id="upd">Update available! <a id="udl" href="#" target="_blank">Download</a></div>
<div id="sb">
  <h2>TraderMoney <span id="lbadge" class="lbadge li">FREE</span></h2>
  <label>License Key</label><input type="password" id="lickey" placeholder="Paste Gumroad key">
  <button onclick="validateLicense()" style="margin-top:4px;font-size:.8rem;">Validate</button>
  <p style="font-size:.67rem;color:var(--muted);margin:3px 0 0;"><a href="https://shafayrich.gumroad.com/l/ykaoov" style="color:var(--accent);">Buy license ↗</a></p>
  <div id="free-notice" class="free-notice">Free tier: Alpaca paper only, Signal-Only, 1 ticker, core indicators.</div>
  <hr>
  <label>Broker</label><select id="broker" onchange="updateCreds()"><option>Alpaca</option><option>Interactive Brokers</option><option>Tradier</option><option>Binance</option><option>Bybit</option><option>OKX</option></select>
  <div id="bstatus" class="ok"></div><div id="creds"></div>
  <label>Telegram Token</label><input type="password" id="tgt"><label>Telegram Chat ID</label><input id="tgc">
  <label>Tickers (e.g. AAPL:5)</label><input id="tickers" value="AAPL">
  <label>Timeframe</label><select id="tf"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>1d</option></select>
  <label>EMA periods</label><div class="r2"><input id="emaf" value="9"><input id="emas" value="50"></div>
  <label><span class="cb"><input type="checkbox" id="udefqty" checked onchange="toggleDefQty()"><span class="cm"></span></span> Use fallback quantity</label>
  <div id="defqty-box"><label>Default Qty</label><input id="qty" value="1" type="number"></div>
  <label>Mode</label><select id="mode"><option value="signal">Signal Only</option><option value="auto">Auto Trade</option></select>
  <label>Direction</label><select id="dir"><option value="both">Both</option><option value="long">Long Only</option><option value="short">Short Only</option></select>
  <label><span class="cb"><input type="checkbox" id="ubracket"><span class="cm"></span></span> Bracket SL/TP</label>
  <div class="r2"><input id="slp" value="2"><input id="tpp" value="4"></div>
  <label><span class="cb"><input type="checkbox" id="uatr" checked><span class="cm"></span></span> ATR Stops</label>
  <label style="margin-top:12px;font-weight:bold;color:var(--accent)">Indicators</label>
  <label><span class="cb"><input type="checkbox" id="ursi" checked><span class="cm"></span></span> RSI</label>
  <label><span class="cb"><input type="checkbox" id="umacd" checked><span class="cm"></span></span> MACD</label>
  <label><span class="cb"><input type="checkbox" id="uvwap" checked><span class="cm"></span></span> VWAP</label>
  <label><span class="cb"><input type="checkbox" id="uboll" checked><span class="cm"></span></span> Bollinger</label>
  <label><span class="cb"><input type="checkbox" id="uadx" checked><span class="cm"></span></span> ADX</label>
  <label><span class="cb"><input type="checkbox" id="uvol" checked><span class="cm"></span></span> Volume</label>
  <label><span class="cb"><input type="checkbox" id="ust" checked><span class="cm"></span></span> SuperTrend</label>
  <label><span class="cb"><input type="checkbox" id="ustoch" checked><span class="cm"></span></span> Stochastic</label>
  <button onclick="saveConfig()">Save</button>
  <button class="ghost" onclick="refreshTickers()">Refresh Tickers</button>
  <button style="background:var(--accent);color:#050505;" id="startBtn" onclick="startBot()">&#9654; Start Bot</button>
  <button class="ghost" id="stopBtn" onclick="stopBot()">&#9632; Stop Bot</button>
  <button class="danger" onclick="killSwitch()">&#9650; Kill Switch</button>
  <button class="ghost" style="margin-top:5px" onclick="resetDef()">&#8634; Reset</button>
  <button class="ghost" style="margin-top:16px" onclick="checkUpdate()">Check Updates</button>
  <button class="purple" style="margin-top:7px" onclick="runBT()">&#9874; Backtest All</button>
  <div style="margin-top:9px;font-size:.75rem;color:var(--muted);">
    <span>Backtest days:</span>
    <input type="number" id="btDays" value="5" min="1" max="365" class="bt-days-input">
  </div>
</div>
<div id="main">
  <div class="tab-bar" id="tabbar">
    <button class="tbtn active" data-tab="charts">Charts</button>
    <button class="tbtn" data-tab="signals">Signals</button>
    <button class="tbtn" data-tab="history">History</button>
    <button class="tbtn" data-tab="backtest">Backtest</button>
    <button class="tbtn" data-tab="help">Help</button>
  </div>
  <!-- … [rest of tabs exactly as in v1.0.52 – keep them identical] … -->
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
  <div id="tab-signals" class="tab">
    <div id="siglist" style="overflow-y:auto;flex:1;"></div>
    <div id="sigempty" class="empty-placeholder" style="display:none;">No signals yet.</div>
  </div>
  <div id="tab-history" class="tab">
    <div id="histlist" style="overflow-y:auto;flex:1;"></div>
    <div id="hstempty" class="empty-placeholder" style="display:none;">No orders yet.</div>
  </div>
  <div id="tab-backtest" class="tab">
    <div class="btp">
      <div style="padding:10px"><button class="purple" style="width:auto;padding:9px 20px" onclick="runBT()">&#9874; Run Backtest on All Tickers</button></div>
      <div id="btres" class="btr"><p class="ph">Click <b>Backtest All</b> to run.<br>Results appear here.</p></div>
    </div>
  </div>
  <div id="tab-help" class="tab">
    <div class="hb">
      <h3>Indicator &amp; Short Selling Guide</h3>
      <div class="istat">
        <p><b>Pure EMA Crossover:</b> ~32%</p>
        <p><b>+ RSI:</b> ~40% | <b>+ MACD:</b> ~45% | <b>+ VWAP:</b> ~48%</p>
        <p><b>+ Bollinger:</b> ~50% | <b>+ ADX >=20:</b> ~55%</p>
        <p><b>+ Volume 1.5x:</b> ~58% | <b>+ SuperTrend:</b> ~62% | <b>+ Stochastic:</b> ~65%</p>
        <p><b>ATR stops</b> improve profit factor by ~0.4</p>
      </div>
      <h4>Short Selling Logic</h4>
      <table class="bttbl"><tr><th>Indicator</th><th>Long condition</th><th>Short condition</th></tr>
      <tr><td>RSI</td><td>RSI >= 30</td><td>RSI <= 70</td></tr>
      <tr><td>MACD</td><td>MACD > signal</td><td>MACD < signal</td></tr>
      <tr><td>VWAP</td><td>Price > VWAP</td><td>Price < VWAP</td></tr>
      <tr><td>SuperTrend</td><td>Trend = 1</td><td>Trend = -1</td></tr>
      <tr><td>Stochastic</td><td>%K > %D, %K<80</td><td>%K < %D, %K>20</td></tr></table>
      <h4>Confidence Score</h4>
      <p>Each confirming indicator adds 0.05-0.08 to a base 0.50, max 1.0. Scores appear in signal rationales.</p>
      <h4>Broker Connection Guide</h4>
      <ul>
        <li><b>Alpaca:</b> API Key + Secret from alpaca.markets. Tick "Paper" for paper trading.</li>
        <li><b>Interactive Brokers:</b> TWS/Gateway running, API enabled. Ports: 7497=TWS paper | 7496=TWS live | 4002=Gateway paper | 4001=Gateway live.</li>
        <li><b>Tradier:</b> Access Token + Account ID from developer.tradier.com. Sandbox checkbox.</li>
        <li><b>Binance:</b> API Key + Secret from binance.com. Testnet checkbox.</li>
        <li><b>Bybit:</b> API Key + Secret from bybit.com. Testnet. Uses pybit v5.</li>
        <li><b>OKX:</b> Key + Secret + Passphrase from okx.com. Demo checkbox.</li>
      </ul>
      <h4>FAQ</h4>
      <p><b>Why no signals?</b> Too many filters enabled. Try toggling SuperTrend or Stochastic off.</p>
      <p><b>How to test safely?</b> Use Paper Trading with a virtual balance, or Signal Only mode.</p>
      <p><b>What timeframe is best?</b> 1m-5m for scalping, 15m-1h for swing, 1d for long term.</p>
    </div>
  </div>
  <div id="logbar"></div>
</div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
'use strict';
const $=id=>document.getElementById(id);let cfg={},licValid=false,curSym='',allTickers=[],chart=null,lastChart='';
function cs(raw){return raw.split(':')[0].trim().toUpperCase();}
function fmt(n,d=2){return Number(n).toLocaleString(undefined,{maximumFractionDigits:d});}
function toast(msg,type='info'){let t=document.createElement('div');t.className='toast '+type;t.textContent=msg;$('toasts').appendChild(t);setTimeout(()=>t.remove(),3800);}
function gv(id,fb=''){let e=$(id);return e?e.value:fb;}
function gc(id){let e=$(id);return e?e.checked:false;}
function sv(id,v){let e=$(id);if(e)e.value=v;}
function sc(id,v){let e=$(id);if(e)e.checked=!!v;}
document.querySelectorAll('.tbtn').forEach(b=>{b.addEventListener('click',function(){document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.tbtn').forEach(x=>x.classList.remove('active'));$('tab-'+this.dataset.tab).classList.add('active');this.classList.add('active');if(this.dataset.tab==='charts'&&chart)setTimeout(()=>chart.resize&&chart.resize(),80);});});
Sortable.create($('tabbar'),{animation:120,handle:'.tbtn'});
function updSess(){let n=new Date(),d=n.getUTCDay(),wk=d===0||d===6,h=n.getUTCHours()+n.getUTCMinutes()/60,o=ok=>ok?'sd so':'sd sc';$('ds').className=o(!wk&&(h>=22||h<5));$('dt').className=o(!wk&&(h>=23||h<6));$('dl').className=o(!wk&&h>=8&&h<16.5);$('dn').className=o(!wk&&h>=13.5&&h<20);$('utc-clock').textContent='UTC: '+n.toISOString().slice(11,19);}
setInterval(updSess,30000);updSess();
function pw(id,l){return`<label>${l}</label><input type="password" id="${id}">`;}
function tx(id,l,v=''){return`<label>${l}</label><input id="${id}" value="${v}">`;}
function cbHTML(id,l,chk=false){return`<label><span class="cb"><input type="checkbox" id="${id}" ${chk?'checked':''}><span class="cm"></span></span> ${l}</label>`;}
function updateCreds(){let b=$('broker').value,c=$('creds');if(b==='Alpaca')c.innerHTML=pw('ak','API Key')+pw('ask','Secret Key')+cbHTML('apaper','Paper Trading',true);else if(b==='Interactive Brokers')c.innerHTML=tx('ih','Host','127.0.0.1')+tx('ip','Port','7497')+tx('icid','Client ID','1');else if(b==='Tradier')c.innerHTML=pw('trat','Access Token')+tx('traid','Account ID')+cbHTML('trsb','Sandbox',false);else if(b==='Binance')c.innerHTML=pw('bnk','API Key')+pw('bns','API Secret')+cbHTML('bnt','Testnet',true);else if(b==='Bybit')c.innerHTML=pw('bbk','API Key')+pw('bbs','API Secret')+cbHTML('bbtn','Testnet',true);else if(b==='OKX')c.innerHTML=pw('ok','API Key')+pw('os','API Secret')+pw('op','Passphrase')+cbHTML('od','Demo',true);}
function toggleDefQty(){document.getElementById('defqty-box').style.display=gc('udefqty')?'block':'none';}
function buildCfg(){let b=$('broker').value;return{broker:b,tickers:gv('tickers','AAPL'),timeframe:gv('tf','1m'),emas:[parseInt(gv('emaf','9')),parseInt(gv('emas','50'))],quantity:parseInt(gv('qty','1'))||1,mode:gv('mode','signal'),direction:gv('dir','both'),use_default_qty:gc('udefqty'),use_bracket:gc('ubracket'),sl_percent:parseFloat(gv('slp','2')),tp_percent:parseFloat(gv('tpp','4')),use_atr_stops:gc('uatr'),telegram:{token:gv('tgt'),chat_id:gv('tgc')},use_rsi:gc('ursi'),use_macd:gc('umacd'),use_vwap:gc('uvwap'),use_bollinger:gc('uboll'),use_adx:gc('uadx'),use_vol_confirm:gc('uvol'),use_supertrend:gc('ust'),use_stochastic:gc('ustoch'),license_key:gv('lickey',''),alpaca:b==='Alpaca'?{api_key:gv('ak'),secret_key:gv('ask'),paper:gc('apaper')}:{},ibkr:b==='Interactive Brokers'?{host:gv('ih','127.0.0.1'),port:gv('ip','7497'),client_id:gv('icid','1')}:{},tradier:b==='Tradier'?{access_token:gv('trat'),account_id:gv('traid'),sandbox:gc('trsb')}:{},binance:b==='Binance'?{api_key:gv('bnk'),api_secret:gv('bns'),testnet:gc('bnt')}:{},bybit:b==='Bybit'?{api_key:gv('bbk'),api_secret:gv('bbs'),testnet:gc('bbtn')}:{},okx:b==='OKX'?{api_key:gv('ok'),api_secret:gv('os'),api_passphrase:gv('op'),demo:gc('od')}:{}};}
function initUI(c){if(!c)return;licValid=c.license_valid||false;sv('broker',c.broker||'Alpaca');updateCreds();sv('tickers',c.tickers||'AAPL');sv('tf',c.timeframe||'1m');sv('emaf',c.emas?c.emas[0]:9);sv('emas',c.emas?c.emas[1]:50);sc('udefqty',c.use_default_qty!==false);toggleDefQty();sv('qty',c.quantity||1);sv('mode',c.mode||'signal');sv('dir',c.direction||'both');if(c.telegram){sv('tgt',c.telegram.token||'');sv('tgc',c.telegram.chat_id||'');}sc('ubracket',c.use_bracket);sv('slp',c.sl_percent||2);sv('tpp',c.tp_percent||4);sc('uatr',c.use_atr_stops!==false);sc('ursi',c.use_rsi!==false);sc('umacd',c.use_macd!==false);sc('uvwap',c.use_vwap!==false);sc('uboll',c.use_bollinger!==false);sc('uadx',c.use_adx!==false);sc('uvol',c.use_vol_confirm!==false);sc('ust',c.use_supertrend!==false);sc('ustoch',c.use_stochastic!==false);if(c.license_key)sv('lickey',c.license_key);
  // enforce UI locks based on license
  if(licValid){
    $('lbadge').textContent='PRO'; $('lbadge').className='lbadge lv';
    $('free-notice').style.display='none';
    $('broker').disabled = false;
    $('mode').disabled = false;
    $('dir').disabled = false;
  } else {
    $('lbadge').textContent='FREE'; $('lbadge').className='lbadge li';
    $('free-notice').style.display='block';
    $('broker').value='Alpaca'; $('broker').disabled = true;
    $('mode').value='signal'; $('mode').disabled = true;
    $('dir').value='both'; $('dir').disabled = true;
    updateCreds();
  }
  // … rest of credential initialization unchanged …
  if(c.broker==='Alpaca'&&c.alpaca){sv('ak',c.alpaca.api_key||'');sv('ask',c.alpaca.secret_key||'');sc('apaper',c.alpaca.paper!==false);}
  if(c.broker==='Interactive Brokers'&&c.ibkr){sv('ih',c.ibkr.host||'127.0.0.1');sv('ip',c.ibkr.port||'7497');sv('icid',c.ibkr.client_id||'1');}
  if(c.broker==='Tradier'&&c.tradier){sv('trat',c.tradier.access_token||'');sv('traid',c.tradier.account_id||'');sc('trsb',c.tradier.sandbox||false);}
  if(c.broker==='Binance'&&c.binance){sv('bnk',c.binance.api_key||'');sv('bns',c.binance.api_secret||'');sc('bnt',c.binance.testnet!==false);}
  if(c.broker==='Bybit'&&c.bybit){sv('bbk',c.bybit.api_key||'');sv('bbs',c.bybit.api_secret||'');sc('bbtn',c.bybit.testnet!==false);}
  if(c.broker==='OKX'&&c.okx){sv('ok',c.okx.api_key||'');sv('os',c.okx.api_secret||'');sv('op',c.okx.api_passphrase||'');sc('od',c.okx.demo!==false);}
  let raw=c.tickers.split(',').map(s=>s.trim()).filter(s=>s);if(raw.length){setTickers(raw);loadChart(cs(raw[0]));}
}
// … [keep rest of JS functions exactly as in v1.0.52] …
async function runBT(){ /* unchanged */ }
// …
Notification.requestPermission();  // if you still want desktop notifications
updateCreds();loadConfig();
</script>
</body>
</html>
"""

# … [keep the rest of app.py exactly as before, including run_flask() and __main__] …


def run_flask():
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)


if __name__ == "__main__":
    acquire_lock()
    # Daily license check thread
    def daily_license_check():
        while True:
            time.sleep(86400)  # 24 hours
            key = state.config.get("license_key", "").strip()
            if key:
                valid, _ = verify_gumroad_license(key)
                state.config["license_valid"] = valid
                EncryptedConfigManager.save(state.config)

    threading.Thread(target=daily_license_check, daemon=True).start()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.2)
    window = webview.create_window("TraderMoney", "http://127.0.0.1:5050", width=1360, height=840, min_size=(940, 660))
    webview.start()

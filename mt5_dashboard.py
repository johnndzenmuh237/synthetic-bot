"""
mt5_dashboard.py - a small control panel for the MetaTrader 5 bot (mt5_bot.py).

Run it on your Windows PC (next to MT5):      python mt5_dashboard.py
It opens http://127.0.0.1:5050 in your browser. There you type your MT5 account number, server and password,
press Connect (demo or live is detected automatically), choose the settings and press Start.

  * Demo and Live are handled by exactly the same code. The only difference: for a REAL account you must tick a
    confirmation box before Start.
  * The password is used once to log in and is kept only in memory - never saved, never logged.
  * The page is only reachable from this computer (127.0.0.1).
Separate from the Deriv/Render bot: nothing here touches app.py or your Deriv bot.
"""
import collections
import json
import logging
import os
import queue
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, abort

import mt5_bot
from mt5_bot import Mt5Bot, Settings, TIMEFRAMES, SESSION_CHOICES, MAGIC

SAVED_FILE = "mt5_saved.json"            # remembers account number + server only (never the password)
GROUPS = ("synthetic", "crypto", "forex")

log = logging.getLogger("mt5bot")
_lines = collections.deque(maxlen=300)


class _Tail(logging.Handler):
    def emit(self, record):
        _lines.append("{} {}".format(time.strftime("%H:%M:%S"), record.getMessage()))


def _setup_logging():
    log.setLevel(logging.INFO)
    if not any(isinstance(h, _Tail) for h in log.handlers):
        log.addHandler(_Tail())
        try:
            fh = logging.FileHandler("mt5_bot.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            log.addHandler(fh)
        except OSError:
            pass


class Command(object):
    def __init__(self, name, payload=None):
        self.name, self.payload = name, payload or {}
        self.done = threading.Event()
        self.result = {"ok": False, "error": "no answer"}


class Controller(object):
    """Owns the MT5 connection. EVERY MetaTrader5 call happens on one worker thread (the library is not thread-safe)."""

    def __init__(self, mt5=None, poll=2.0):
        self.mt5 = mt5
        self.poll = poll
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.thread = None
        self.quit = False
        self.bot = None
        self.account = None
        self.running = False
        self.positions = []
        self.symbols = 0
        self.error = ""

    # ── public (called from web requests) ────────────────────
    def call(self, name, payload=None, timeout=90):
        if self.thread is None or not self.thread.is_alive():
            self.quit = False
            self.thread = threading.Thread(target=self._loop, name="mt5-worker", daemon=True)
            self.thread.start()
        cmd = Command(name, payload)
        self.q.put(cmd)
        if not cmd.done.wait(timeout):
            return {"ok": False, "error": "MT5 did not answer in time. Is the MT5 terminal open?"}
        return cmd.result

    def status(self):
        with self.lock:
            return {"connected": self.account is not None, "running": self.running, "account": self.account,
                    "positions": list(self.positions), "symbols": self.symbols, "error": self.error,
                    "log": list(_lines)[-80:], "saved": load_saved(), "choices": {
                        "tf": sorted(TIMEFRAMES, key=TIMEFRAMES.get), "hours": list(SESSION_CHOICES), "groups": list(GROUPS)}}

    def shutdown(self):
        self.quit = True
        self.q.put(Command("noop"))

    # ── worker thread ────────────────────────────────────────
    def _loop(self):
        while not self.quit:
            try:
                cmd = self.q.get(timeout=self.poll)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                try:
                    cmd.result = getattr(self, "_do_" + cmd.name)(cmd.payload)
                except Exception as e:
                    log.exception("command %s failed", cmd.name)
                    cmd.result = {"ok": False, "error": str(e)}
                cmd.done.set()
            self._tick()
        self._do_disconnect({})

    def _tick(self):
        if self.account is None:
            return
        try:
            if self.running and self.bot is not None:
                self.bot.step()
                if self.bot.done:
                    self.running, self.bot = False, None
            self._refresh()
        except Exception as e:
            log.exception("pass failed: %s", e)
            with self.lock:
                self.error = "Last pass failed: {}".format(e)

    def _refresh(self):
        m = self.mt5
        acct = m.account_info()
        if acct is None:
            with self.lock:
                self.error = "The MT5 terminal is not logged in any more."
            return
        rows = []
        for p in (m.positions_get() or []):
            if getattr(p, "magic", MAGIC) != MAGIC:
                continue
            rows.append({"symbol": p.symbol, "side": "BUY" if p.type == m.POSITION_TYPE_BUY else "SELL",
                         "lots": p.volume, "open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": round(p.profit, 2)})
        with self.lock:
            self.account = self._account(acct)
            self.positions = rows
            self.error = ""

    def _account(self, acct):
        real = acct.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_REAL
        return {"login": acct.login, "server": acct.server, "mode": "LIVE" if real else "DEMO",
                "balance": round(float(acct.balance), 2), "equity": round(float(acct.equity), 2),
                "currency": acct.currency}

    # ── commands ─────────────────────────────────────────────
    def _do_noop(self, p):
        return {"ok": True}

    def _do_connect(self, p):
        m = self.mt5
        self._do_disconnect({})
        try:
            login = int(str(p.get("login", "")).strip())
        except ValueError:
            return {"ok": False, "error": "The account number must be digits only."}
        server, password = str(p.get("server", "")).strip(), str(p.get("password", ""))
        if not server or not password:
            return {"ok": False, "error": "Enter the server and the password."}
        if not m.initialize():
            return {"ok": False, "error": "Cannot reach the MT5 terminal ({}). Open MT5 first.".format(m.last_error())}
        if not m.login(login, password=password, server=server):
            err = m.last_error()
            m.shutdown()
            return {"ok": False, "error": "MT5 login failed ({}). Check the number, password and server name.".format(err)}
        acct = m.account_info()
        if acct is None:
            m.shutdown()
            return {"ok": False, "error": "MT5 gave no account information."}
        save_login(login, server)
        with self.lock:
            self.account = self._account(acct)
            self.error = ""
        log.info("Connected: account %s on %s | %s | balance %.2f %s", acct.login, acct.server,
                 self.account["mode"], acct.balance, acct.currency)
        return {"ok": True, "account": self.account}

    def _do_start(self, p):
        if self.account is None:
            return {"ok": False, "error": "Connect first."}
        if self.running:
            return {"ok": False, "error": "The bot is already running."}
        s, err = parse_settings(p)
        if err:
            return {"ok": False, "error": err}
        if self.account["mode"] == "LIVE" and not p.get("confirm_real") and not s.dry_run:
            return {"ok": False, "error": "This is a REAL-money account. Tick the confirmation box to start."}
        bot = Mt5Bot(self.mt5, s)
        if not bot.discover():
            return {"ok": False, "error": "No tradable symbols found for those markets. Check the groups and Market Watch in MT5."}
        with self.lock:
            self.bot, self.running, self.symbols = bot, True, len(bot.symbols)
        log.info("Bot STARTED on %s | %s | %d symbols | risk %s%% | RR %s | %sh%s", self.account["mode"], s.tf,
                 len(bot.symbols), s.risk_pct if not s.lot else "fixed lot {}".format(s.lot), s.rr, s.hours,
                 " | DRY RUN" if s.dry_run else "")
        return {"ok": True}

    def _do_stop(self, p):
        if self.running:
            log.info("Bot STOPPED. Open trades keep their stop-loss and take-profit in MT5.")
        with self.lock:
            self.running, self.bot = False, None
        return {"ok": True}

    def _do_disconnect(self, p):
        self._do_stop({})
        if self.account is not None:
            try:
                self.mt5.shutdown()
            except Exception:
                pass
            log.info("Disconnected from MT5.")
        with self.lock:
            self.account, self.positions, self.symbols = None, [], 0
        return {"ok": True}


def parse_settings(p):
    """-> (Settings, error text). Validates everything the page sends."""
    try:
        tf = str(p.get("tf", "M5"))
        if tf not in TIMEFRAMES:
            return None, "Pick a timeframe from the list."
        groups = tuple(g for g in p.get("groups", GROUPS) if g in GROUPS)
        if not groups:
            return None, "Pick at least one market (synthetic, crypto, forex)."
        hours = str(p.get("hours", "24"))
        if hours not in SESSION_CHOICES:
            return None, "Pick a trading duration from the list."
        risk, lot = float(p.get("risk_pct", 1.0)), float(p.get("lot", 0) or 0)
        rr, mx = float(p.get("rr", 2.0)), int(p.get("max_positions", 5))
    except (TypeError, ValueError):
        return None, "One of the numbers is not valid."
    if not 0.01 <= risk <= 10:
        return None, "Risk per trade must be between 0.01% and 10%."
    if lot < 0 or lot > 100:
        return None, "Fixed lot must be 0 (use % risk) or a positive size."
    if not 0.5 <= rr <= 10:
        return None, "Reward:risk must be between 0.5 and 10."
    if not 1 <= mx <= 50:
        return None, "Max open trades must be between 1 and 50."
    return Settings(tf=tf, groups=groups, risk_pct=risk, lot=lot, rr=rr, max_positions=mx, hours=hours,
                    dry_run=bool(p.get("dry_run"))), ""


def load_saved():
    try:
        with open(SAVED_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {"login": str(d.get("login", "")), "server": str(d.get("server", ""))}
    except Exception:
        return {"login": "", "server": ""}


def save_login(login, server):
    try:
        with open(SAVED_FILE, "w", encoding="utf-8") as f:
            json.dump({"login": str(login), "server": server}, f)
    except OSError:
        pass


# ── web app ──────────────────────────────────────────────────
def create_app(controller):
    app = Flask(__name__)

    @app.before_request
    def _local_only():
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)                                   # blocks DNS-rebinding tricks
        if request.method == "POST" and not request.is_json:
            abort(400)                                   # a web page on another site cannot send JSON here

    @app.route("/")
    def index():
        return PAGE

    @app.route("/api/status")
    def status():
        return jsonify(controller.status())

    @app.route("/api/connect", methods=["POST"])
    def connect():
        return jsonify(controller.call("connect", request.get_json(silent=True) or {}))

    @app.route("/api/start", methods=["POST"])
    def start():
        return jsonify(controller.call("start", request.get_json(silent=True) or {}))

    @app.route("/api/stop", methods=["POST"])
    def stop():
        return jsonify(controller.call("stop"))

    @app.route("/api/disconnect", methods=["POST"])
    def disconnect():
        return jsonify(controller.call("disconnect"))

    return app


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MT5 Bot Control</title>
<style>
:root{--bg:#0f1420;--card:#171e2e;--ink:#e8ecf4;--mute:#8d99b3;--line:#2a3550;--ok:#2ecc8f;--bad:#ff5c6c;--warn:#ffb020;--acc:#4c8dff}
@media (prefers-color-scheme: light){:root{--bg:#f2f4f9;--card:#fff;--ink:#1a2233;--mute:#5d6a85;--line:#d9dfec}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,Segoe UI,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
label{display:block;font-size:13px;color:var(--mute);margin-bottom:4px}
input,select{width:100%;padding:9px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
button{padding:10px 18px;border-radius:8px;border:0;font:inherit;font-weight:600;cursor:pointer;color:#fff;background:var(--acc)}
button.go{background:var(--ok);color:#06281a}button.stop{background:var(--bad)}button.ghost{background:transparent;border:1px solid var(--line);color:var(--ink)}
button:disabled{opacity:.45;cursor:not-allowed}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:700;font-size:13px}
.demo{background:rgba(46,204,143,.18);color:var(--ok)}.live{background:rgba(255,92,108,.2);color:var(--bad)}
.stat{font-size:22px;font-weight:700}.mute{color:var(--mute)}.err{color:var(--bad);margin-top:10px;min-height:1.2em}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line)}
.pos{color:var(--ok)}.neg{color:var(--bad)}pre{margin:0;max-height:260px;overflow:auto;font:12.5px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap}
.chk{display:flex;gap:8px;align-items:flex-start;margin-top:12px;padding:10px;border:1px solid var(--bad);border-radius:8px;color:var(--bad)}.chk input{width:auto;margin-top:3px}
.hide{display:none}
</style></head><body><div class="wrap">
<h1>MT5 Bot Control</h1><div class="sub">EMA 21/50 pullback strategy &middot; synthetic, crypto and forex &middot; runs on this computer</div>

<div class="card" id="loginCard"><h2>1 &middot; Connect your MT5 account</h2>
<div class="grid">
<div><label>Account number</label><input id="login" inputmode="numeric" autocomplete="off" placeholder="e.g. 40012345"></div>
<div><label>Server</label><input id="server" autocomplete="off" placeholder="exactly as shown in MT5"></div>
<div><label>Password</label><input id="password" type="password" autocomplete="off" placeholder="not saved anywhere"></div></div>
<div class="row"><button id="btnConnect">Connect</button><span class="mute">Demo or live is detected automatically.</span></div>
<div class="err" id="err1"></div></div>

<div class="card hide" id="acctCard"><h2>Account</h2>
<div class="grid"><div><span id="mode" class="badge demo">DEMO</span><div class="mute" id="acctline" style="margin-top:6px"></div></div>
<div><div class="mute">Balance</div><div class="stat" id="bal">-</div></div>
<div><div class="mute">Equity</div><div class="stat" id="eq">-</div></div>
<div><div class="mute">Status</div><div class="stat" id="state">Idle</div></div></div>
<div class="row"><button class="ghost" id="btnDisc">Disconnect / switch account</button></div></div>

<div class="card hide" id="setCard"><h2>2 &middot; Settings</h2>
<div class="grid">
<div><label>Markets</label><label style="color:var(--ink)"><input type="checkbox" class="grp" value="synthetic" checked style="width:auto"> Synthetic</label>
<label style="color:var(--ink)"><input type="checkbox" class="grp" value="crypto" checked style="width:auto"> Crypto</label>
<label style="color:var(--ink)"><input type="checkbox" class="grp" value="forex" checked style="width:auto"> Forex</label></div>
<div><label>Timeframe</label><select id="tf"></select></div>
<div><label>Trading duration (hours)</label><select id="hours"></select></div>
<div><label>Risk per trade (% of equity)</label><input id="risk" type="number" step="0.1" value="1"></div>
<div><label>Fixed lot (0 = use % risk)</label><input id="lot" type="number" step="0.01" value="0"></div>
<div><label>Take-profit (x the risk)</label><input id="rr" type="number" step="0.1" value="2"></div>
<div><label>Max open trades</label><input id="maxpos" type="number" value="5"></div>
<div><label>Mode</label><select id="dry"><option value="0">Place trades</option><option value="1">Dry run (signals only)</option></select></div></div>
<div class="chk hide" id="liveBox"><input type="checkbox" id="confirm"><div><b>This is a REAL-money account.</b> I understand the bot will place real trades with real money.</div></div>
<div class="row"><button class="go" id="btnStart">Start bot</button><button class="stop" id="btnStop">Stop bot</button>
<span class="mute">Stopping leaves open trades protected by their stop-loss and take-profit.</span></div>
<div class="err" id="err2"></div></div>

<div class="card hide" id="posCard"><h2>Open trades</h2><div id="pos" class="mute">None</div></div>
<div class="card"><h2>Activity</h2><pre id="log">-</pre></div>
</div>
<script>
const $=id=>document.getElementById(id);let filled=false,inited=false;
async function api(p,body){const r=await fetch(p,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});return r.json()}
function fillSelect(el,vals,def){el.innerHTML=vals.map(v=>'<option'+(v==def?' selected':'')+'>'+v+'</option>').join('')}
function money(v,c){return (v<0?'-':'')+Math.abs(v).toFixed(2)+' '+(c||'')}
async function refresh(){
 let s;try{s=await api('/api/status')}catch(e){return}
 if(!inited){fillSelect($('tf'),s.choices.tf,'M5');fillSelect($('hours'),s.choices.hours,'24');inited=true}
 if(!filled){$('login').value=s.saved.login;$('server').value=s.saved.server;filled=true}
 const c=s.connected;$('acctCard').classList.toggle('hide',!c);$('setCard').classList.toggle('hide',!c);$('posCard').classList.toggle('hide',!c);$('loginCard').classList.toggle('hide',c);
 if(c){const a=s.account;$('mode').textContent=a.mode;$('mode').className='badge '+(a.mode=='LIVE'?'live':'demo');
  $('acctline').textContent='Account '+a.login+' · '+a.server;$('bal').textContent=money(a.balance,a.currency);$('eq').textContent=money(a.equity,a.currency);
  $('state').textContent=s.running?'Running ('+s.symbols+' symbols)':'Idle';$('liveBox').classList.toggle('hide',a.mode!='LIVE');
  $('btnStart').disabled=s.running;$('btnStop').disabled=!s.running;
  $('pos').innerHTML=s.positions.length?'<table><tr><th>Symbol</th><th>Side</th><th>Lots</th><th>Open</th><th>SL</th><th>TP</th><th>Profit</th></tr>'+s.positions.map(p=>'<tr><td>'+p.symbol+'</td><td>'+p.side+'</td><td>'+p.lots+'</td><td>'+p.open+'</td><td>'+p.sl+'</td><td>'+p.tp+'</td><td class="'+(p.profit>=0?'pos':'neg')+'">'+p.profit.toFixed(2)+'</td></tr>').join('')+'</table>':'None';}
 if(s.error&&c)$('err2').textContent=s.error;
 $('log').textContent=s.log.length?s.log.join('\\n'):'-';
}
$('btnConnect').onclick=async()=>{$('err1').textContent='Connecting...';$('btnConnect').disabled=true;
 const r=await api('/api/connect',{login:$('login').value,server:$('server').value,password:$('password').value});
 $('btnConnect').disabled=false;$('err1').textContent=r.ok?'':r.error;if(r.ok)$('password').value='';refresh()};
$('btnStart').onclick=async()=>{$('err2').textContent='';
 const groups=[...document.querySelectorAll('.grp:checked')].map(x=>x.value);
 const r=await api('/api/start',{groups,tf:$('tf').value,hours:$('hours').value,risk_pct:$('risk').value,lot:$('lot').value,rr:$('rr').value,max_positions:$('maxpos').value,dry_run:$('dry').value=='1',confirm_real:$('confirm').checked});
 if(!r.ok)$('err2').textContent=r.error;refresh()};
$('btnStop').onclick=async()=>{await api('/api/stop',{});refresh()};
$('btnDisc').onclick=async()=>{await api('/api/disconnect',{});refresh()};
refresh();setInterval(refresh,2000);
</script></body></html>"""


def main():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit("Install it first:  pip install MetaTrader5   (Windows only)")
    _setup_logging()
    ctl = Controller(mt5)
    app = create_app(ctl)
    port = int(os.getenv("MT5_DASH_PORT", 5050))
    url = "http://127.0.0.1:{}".format(port)
    print("MT5 Bot Control is running at {}  (press Ctrl+C to quit)".format(url))
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    finally:
        ctl.shutdown()


if __name__ == "__main__":
    main()
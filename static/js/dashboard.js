"use strict";

// ── Icons (bundled SVG sprite in templates/_icons.html — no emojis) ──
function ic(name, cls) {
  return '<svg class="icon ' + (cls || "") + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

// ── State ────────────────────────────────────────────────────
var INIT   = window.__INIT__ || { saved: {}, limits: {} };
var LIMITS = INIT.limits || {};
var MIN_LOT = LIMITS.min_stake || 0.35;
var MAX_LOT = LIMITS.max_stake || 500;
var MAX_POS = LIMITS.max_positions || 10;

var state = {
  symbol: INIT.saved.symbol || "R_50", mode: INIT.saved.mode || "demo",
  botRunning: false, workerState: "stopped", activeTf: "M1",
  lotSize: INIT.saved.lot_size || 1.0, maxPositions: INIT.saved.max_positions || 1,
  charts: {}, candles: {}, ema: { fast: 5, slow: 13 }, realizedTotal: null,
  saved: INIT.saved || null, applied: null, balance: null, resizeObs: null
};

var SYMBOL_NAMES = {
  "R_10":"Volatility 10 Index","R_25":"Volatility 25 Index",
  "R_50":"Volatility 50 Index","R_75":"Volatility 75 Index",
  "R_100":"Volatility 100 Index",
  "1HZ10V":"Volatility 10 (1s) Index","1HZ25V":"Volatility 25 (1s) Index",
  "1HZ50V":"Volatility 50 (1s) Index","1HZ75V":"Volatility 75 (1s) Index",
  "1HZ100V":"Volatility 100 (1s) Index",
  "BOOM500":"Boom 500 Index","CRASH500":"Crash 500 Index"
};
var STRATEGY_NAMES = {};
var LOSS_NAMES = {};

function $(id) { return document.getElementById(id); }

// ── Mobile sidebar ───────────────────────────────────────────
function toggleSidebar() {
  $("sidebar").classList.toggle("open");
  $("sidebar-overlay").classList.toggle("open");
}
function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("sidebar-overlay").classList.remove("open");
}

// ── SocketIO ─────────────────────────────────────────────────
var socket = io();

socket.on("connect", function() {
  socket.emit("request_snapshot");   // ask the server to (re)send charts / status / trades
});

socket.on("bot_status", function(d) { handleStatus(d); });

socket.on("trade_error", function(d) { showError(d.message); });

socket.on("balance_update", function(d) { renderBalance(d); });

socket.on("candles_init", function(d) {
  // The server only sends candles for the symbol the bot is really running.
  if (d.symbol !== state.symbol) setSymbolUI(d.symbol);
  if (d.ema) { state.ema = d.ema; updateLegend(); }
  state.candles[d.tf] = d.candles;
  initChart(d.tf, d.candles);
  showChart(state.activeTf);
});

socket.on("candle_update", function(d) {
  if (d.symbol !== state.symbol) return;
  updateCandle(d.tf, d.candle);
});

socket.on("signal", function(sig) { addSignal(sig); });

socket.on("trade_closed", function(d) {
  var sign = d.pnl >= 0 ? "+" : "";
  showToast("Trade #" + d.trade_id + " " + d.status + " " + sign + "$" + parseFloat(d.pnl).toFixed(2));
});

socket.on("trades_update", function(d) {
  renderTrades(d.trades);
  renderStats(d.stats);
});

socket.on("analysis_update", function(d) { renderAnalysis(d); });

// ── Status (always the REAL state reported by the server) ────
var ALIVE_STATES = ["starting", "running", "reconnecting", "paused", "stopping", "error"];

function handleStatus(d) {
  var st = d.state || "stopped";
  if (d.worker_alive === false && st === "error") { applyState("error", d.message, d.detail, false); return; }
  applyState(st, d.message, d.detail, st !== "stopped");
  if (d.account_type || d.account_id) renderVerified(d.account_type, d.account_id, d.mode || state.mode);
}

function applyState(st, msg, detail, alive) {
  state.workerState = st;
  state.botRunning  = !!alive && st !== "stopped";
  $("status-dot").className = "status-dot " + st;
  var label = msg || st;
  if (st === "paused" && detail) label = "Paused for risk protection — " + detail;
  $("status-text").textContent = label;

  if (st === "error") {
    showError(msg || "Bot error");
  } else if (st === "reconnecting" && detail) {
    showError("Retrying — reason: " + detail);
  } else {
    dismissError();
  }
  setBotBtn(state.botRunning, st === "stopping");
  $("btn-update").style.display   = state.botRunning ? "block" : "none";
  $("running-info").style.display = state.botRunning ? "flex" : "none";
  $("exec-status").innerHTML = ic(st === "running" ? "check" : st === "paused" ? "pause" : "clock") + " " +
    esc(st === "running" ? "Monitoring closed M1 candles" :
        st === "paused" ? "Paused — risk limit" :
        st === "stopped" ? "Bot not running" : st.charAt(0).toUpperCase() + st.slice(1));
  updateRunningBadges();
  refreshPending();
}

function setBotBtn(running, stopping) {
  $("bot-btn-icon").innerHTML = ic(running ? "stop" : "play");
  $("bot-btn-text").textContent = stopping ? "Stopping..." : (running ? "Stop Bot" : "Activate Bot");
  $("bot-btn").classList.toggle("running", running);
  $("bot-btn").disabled = !!stopping;
}

function updateRunningBadges() {
  var cfg = state.applied || getConfig();
  $("ri-lot").textContent  = "Stake: " + Number(cfg.lot_size).toFixed(2);
  $("ri-pos").textContent  = "Max: " + cfg.max_positions;
  $("ri-mode").textContent = String(cfg.mode).toUpperCase();
}

function showError(msg) {
  $("error-text").innerHTML = ic("alert") + " " + esc(msg);
  $("error-banner").style.display = "flex";
}
function dismissError() { $("error-banner").style.display = "none"; }

// ── Balance / accounting panel ───────────────────────────────
function money(v, signed) {
  var n = parseFloat(v || 0);
  return (signed && n > 0 ? "+" : "") + (n < 0 ? "-" : "") + "$" + Math.abs(n).toFixed(2);
}
function colorize(el, v) {
  var n = parseFloat(v || 0);
  el.style.color = n > 0 ? "var(--green)" : n < 0 ? "var(--red)" : "";
}

function renderBalance(d) {
  var bal = parseFloat(d.balance || 0);
  state.balance = bal;
  $("balance-display").textContent = (d.currency || "USD") + " " + bal.toFixed(2);
  var demo = (d.account_type || (d.mode === "live" ? "real" : "demo")) === "demo";
  $("balance-kind").textContent = demo ? "Demo balance (virtual funds)" : "Live account balance";
  $("balance-note").textContent = d.balance_pending ? "Balance refresh pending — showing the last confirmed value" : "";
  var pct = parseFloat(d.daily_pnl || 0);
  var el = $("daily-pnl");
  el.textContent = "Day: " + (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
  el.style.color = pct >= 0 ? "var(--green)" : "var(--red)";
  if (d.account_id) $("account-id-display").textContent = d.account_id;
  $("realized-today").textContent = money(d.realized_today, true); colorize($("realized-today"), d.realized_today);
  $("realized-total").textContent = money(d.realized_total, true); colorize($("realized-total"), d.realized_total);
  $("floating-pnl").textContent   = money(d.floating_pnl, true);   colorize($("floating-pnl"), d.floating_pnl);
  $("open-positions").textContent = (d.open_positions || 0) + (d.open_stake ? " · $" + parseFloat(d.open_stake).toFixed(2) : "");
  renderVerified(d.account_type, d.account_id, d.mode);
  state.realizedTotal = parseFloat(d.realized_total || 0);
  renderBalanceMath();
  updateLotWarning();
}

// "Before bot trades + realized P&L = current balance" (Deriv's balance already
// includes every settled win/loss; this line only explains where it came from).
function renderBalanceMath() {
  var el = $("balance-math");
  if (!el) return;
  if (state.balance == null || state.realizedTotal == null) { el.textContent = ""; return; }
  var cur = state.balance, r = state.realizedTotal, start = cur - r;
  el.textContent = "$" + start.toFixed(2) + " " + (r < 0 ? "- $" : "+ $") + Math.abs(r).toFixed(2) +
                   " = $" + cur.toFixed(2);
}

function renderVerified(type, id, mode) {
  var el = $("account-verified");
  if (!type) { el.textContent = "Account not verified yet"; el.className = "verified-line"; return; }
  var live = type === "real";
  el.innerHTML = ic("shield") + " Verified: " + (live ? "LIVE ACCOUNT" : "DEMO ACCOUNT") + (id ? " — " + esc(id) : "");
  el.className = "verified-line " + (live ? "live" : "demo");
  $("mode-badge").textContent = live ? "LIVE" : "DEMO";
  if (mode && ((mode === "live") !== live)) showError("Account type mismatch — the bot will not trade until this is resolved.");
}

// ── Settings: stake ──────────────────────────────────────────
function adjustLot(delta) {
  var cur  = parseFloat($("lot-input").value) || 1.0;
  var next = Math.round((cur + delta) * 100) / 100;
  next = Math.max(MIN_LOT, Math.min(MAX_LOT, next));
  $("lot-input").value = next.toFixed(2);
  state.lotSize = next;
  updateLotDisplay();
}

function setLot(val) {
  val = Math.max(MIN_LOT, Math.min(MAX_LOT, parseFloat(val)));
  $("lot-input").value = val.toFixed(2);
  state.lotSize = val;
  updateLotDisplay();
}

function validateLot() {
  var val = parseFloat($("lot-input").value);
  if (isNaN(val) || val < MIN_LOT) val = MIN_LOT;
  if (val > MAX_LOT) val = MAX_LOT;
  val = Math.round(val * 100) / 100;
  $("lot-input").value = val.toFixed(2);
  state.lotSize = val;
  updateLotDisplay();
}

function updateLotDisplay() {
  $("lot-usd-label").textContent = "= $" + state.lotSize.toFixed(2) + " / trade";
  document.querySelectorAll(".preset-btn").forEach(function(b) {
    b.classList.toggle("active", Math.abs(parseFloat(b.textContent) - state.lotSize) < 1e-9);
  });
  updateTotalRisk();
  updateLotWarning();
  refreshPending();
}

function updateLotWarning() {
  var el = $("lot-warning");
  if (state.balance && state.lotSize > state.balance * 0.05)
    el.innerHTML = ic("alert", "icon-gold") + " Stake is more than 5% of the account balance.";
  else el.textContent = "";
}

// ── Settings: max open positions ─────────────────────────────
function adjustPositions(delta) {
  var cur  = parseInt($("positions-input").value, 10) || 1;
  setPositions(cur + delta);
}

function setPositions(val) {
  val = Math.max(1, Math.min(MAX_POS, parseInt(val, 10) || 1));
  $("positions-input").value = val;
  state.maxPositions = val;
  document.querySelectorAll(".pos-preset").forEach(function(b) {
    b.classList.toggle("active", parseInt(b.textContent, 10) === val);
  });
  updateTotalRisk();
  refreshPending();
}

function validatePositions() { setPositions($("positions-input").value); }

function updateTotalRisk() {
  var total = Math.round(state.lotSize * state.maxPositions * 100) / 100;
  $("total-risk-label").textContent =
    "Max exposure: $" + state.lotSize.toFixed(2) + " × " + state.maxPositions + " = $" + total.toFixed(2);
}

// ── Saved / Applied / Pending ────────────────────────────────
function getConfig() {
  return {
    symbol:        state.symbol,
    mode:          state.mode,
    lot_size:      parseFloat($("lot-input").value) || state.lotSize,
    max_positions: parseInt($("positions-input").value, 10) || state.maxPositions,
    strategy:      $("strategy-select").value,
    loss_policy:   $("loss-policy-select").value,
    close_on_stop: $("close-on-stop").checked,
    account_id:    $("account-select").value || ""
  };
}

var CFG_LABELS = { symbol: "pair", mode: "account mode", lot_size: "stake", max_positions: "max positions",
                   strategy: "strategy", loss_policy: "loss policy", close_on_stop: "close on stop",
                   account_id: "account" };
var CFG_KEYS = ["symbol", "mode", "lot_size", "max_positions", "strategy", "loss_policy", "close_on_stop", "account_id"];

function describe(cfg) {
  if (!cfg) return "—";
  return String(cfg.mode).toUpperCase() + " · " + cfg.symbol + " · stake " + Number(cfg.lot_size).toFixed(2) +
         " · max " + cfg.max_positions + " · " + (STRATEGY_NAMES[cfg.strategy] || cfg.strategy);
}

function diffKeys(a, b) {
  if (!a || !b) return [];
  return CFG_KEYS.filter(function(k) {
    var x = a[k], y = b[k];
    if (k === "lot_size") return Math.abs(parseFloat(x) - parseFloat(y)) > 1e-9;
    if (k === "account_id") return (x || "") !== (y || "");
    return x !== y;
  });
}

function refreshPending() {
  if (!$("cfg-saved")) return;
  var cur = getConfig();
  $("cfg-saved").textContent   = describe(state.saved);
  $("cfg-applied").textContent = state.botRunning && state.applied ? describe(state.applied) : "Bot not running";
  var parts = [];
  var unsaved = diffKeys(cur, state.saved);
  if (unsaved.length) parts.push("Unsaved: " + unsaved.map(function(k) { return CFG_LABELS[k]; }).join(", "));
  if (state.botRunning && state.applied) {
    var notApplied = diffKeys(state.saved, state.applied);
    if (notApplied.length) parts.push("Saved but not applied: " + notApplied.map(function(k) { return CFG_LABELS[k]; }).join(", "));
  }
  $("cfg-pending").textContent = parts.length ? parts.join(" | ") : "None";
  $("cfg-pending-line").classList.toggle("has-pending", parts.length > 0);
}

function onSettingChanged() { refreshPending(); }

function applyPayload(d) {
  if (d.saved)   state.saved = d.saved;
  state.applied = d.applied || null;
  refreshPending();
  updateRunningBadges();
}

function post(url, body) {
  return fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  }).then(function(r) { return r.json().catch(function() { return { ok: false, error: "Unexpected server response" }; }); });
}

function saveSettings() {
  post("/api/settings", getConfig()).then(function(d) {
    if (d.ok) { applyPayload(d); showToast("Settings saved"); }
    else showError(d.error || "Could not save settings");
  });
}

function updateSettings() {
  if (!state.botRunning) return;
  post("/api/bot/update", getConfig()).then(function(d) {
    if (d.ok) { applyPayload(d); showToast("Changes applied to the running bot"); }
    else showError(d.error || "Could not apply changes");
  });
}

// ── Charts ───────────────────────────────────────────────────
function updateLegend() {
  $("legend-a").textContent = "EMA " + state.ema.fast;
  $("legend-b").textContent = "EMA " + state.ema.slow;
}

function initChart(tf, candles) {
  var pane = $("chart-" + tf);
  if (!pane) return;
  var ph = $("chart-placeholder");
  if (ph) ph.style.display = "none";

  if (!state.charts[tf]) {
    var box = $("chart-container");
    var chart = LightweightCharts.createChart(pane, {
      width:  box.clientWidth || 600,
      height: box.clientHeight || 300,
      layout: { background: { color: "#111827" }, textColor: "#94a3b8" },
      grid:   { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1e293b" },
      timeScale: { borderColor: "#1e293b", timeVisible: true, secondsVisible: false },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
      handleScale:  { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    });
    var cs = chart.addCandlestickSeries({
      upColor: "#10b981", downColor: "#ef4444",
      borderUpColor: "#10b981", borderDownColor: "#ef4444",
      wickUpColor: "#10b981", wickDownColor: "#ef4444",
    });
    var eFast = chart.addLineSeries({ color: "#ef4444", lineWidth: 1, title: "EMA" + state.ema.fast });
    var eSlow = chart.addLineSeries({ color: "#a855f7", lineWidth: 1, title: "EMA" + state.ema.slow });
    state.charts[tf] = { chart: chart, cs: cs, eFast: eFast, eSlow: eSlow };

    if (!state.resizeObs && window.ResizeObserver) {
      state.resizeObs = new ResizeObserver(function() { Object.keys(state.charts).forEach(sizeChart); });
      state.resizeObs.observe(box);
    }
  }
  populateChart(tf, candles);
}

function sizeChart(tf) {
  var c = state.charts[tf], box = $("chart-container");
  if (c && box.clientWidth > 0) c.chart.applyOptions({ width: box.clientWidth, height: box.clientHeight });
}

function populateChart(tf, candles) {
  if (!state.charts[tf] || !candles || !candles.length) return;
  var c = state.charts[tf];
  c.cs.setData(candles.map(function(x) {
    return { time: x.epoch, open: +x.open, high: +x.high, low: +x.low, close: +x.close };
  }));
  c.eFast.setData(computeEma(candles, state.ema.fast));
  c.eSlow.setData(computeEma(candles, state.ema.slow));
}

function updateCandle(tf, candle) {
  var c = state.charts[tf];
  if (!c) return;
  c.cs.update({ time: candle.epoch, open: +candle.open, high: +candle.high, low: +candle.low, close: +candle.close });
  var store = state.candles[tf] || [];
  if (store.length && store[store.length - 1].epoch === candle.epoch) store[store.length - 1] = candle;
  else { store.push(candle); state.candles[tf] = store; }
  var f = computeEma(store, state.ema.fast), sl = computeEma(store, state.ema.slow);
  if (f.length)  c.eFast.update(f[f.length - 1]);
  if (sl.length) c.eSlow.update(sl[sl.length - 1]);
}

function computeEma(candles, period) {
  var k = 2 / (period + 1), ema = null, out = [];
  for (var i = 0; i < candles.length; i++) {
    var p = parseFloat(candles[i].close);
    ema = ema === null ? p : p * k + ema * (1 - k);
    if (i >= period - 1) out.push({ time: candles[i].epoch, value: +ema.toFixed(5) });
  }
  return out;
}

function showChart(tf) {
  ["M1", "M5"].forEach(function(t) {
    var pane = $("chart-" + t);
    if (pane) pane.classList.toggle("active", t === tf);
  });
  if (state.charts[tf]) { sizeChart(tf); state.charts[tf].chart.timeScale().fitContent(); }
}

function switchChart(tf) {
  state.activeTf = tf;
  document.querySelectorAll(".tf-tab").forEach(function(b) { b.classList.toggle("active", b.textContent === tf); });
  showChart(tf);
  if (!state.charts[tf]) showToast(tf + " chart loading...");
}

function resetCharts() {
  Object.keys(state.charts).forEach(function(tf) { try { state.charts[tf].chart.remove(); } catch (e) {} });
  state.charts = {}; state.candles = {};
  ["M1", "M5"].forEach(function(t) { var p = $("chart-" + t); if (p) p.innerHTML = ""; });
  var ph = $("chart-placeholder");
  if (ph) ph.style.display = "flex";
}

function setSymbolUI(sym) {
  if (state.symbol !== sym) resetCharts();
  state.symbol = sym;
  $("active-symbol-label").textContent = sym;
  $("active-name-label").textContent   = SYMBOL_NAMES[sym] || sym;
  document.querySelectorAll(".pair-btn").forEach(function(b) {
    b.classList.toggle("active", b.getAttribute("data-symbol") === sym);
  });
}

// ── Analysis ─────────────────────────────────────────────────
function renderAnalysis(d) {
  var L = d.labels || { a: "EMA", b: "EMA", c: "Close" };
  ["m5", "m1"].forEach(function(tf) {
    var data = d[tf] || { direction: "NONE", a: 0, b: 0, c: 0 };
    $(tf + "-la").textContent = L.a; $(tf + "-lb").textContent = L.b; $(tf + "-lc").textContent = L.c;
    $(tf + "-a").textContent = data.a; $(tf + "-b").textContent = data.b; $(tf + "-c").textContent = data.c;
    var dir = data.direction;
    var el = $(tf + "-direction");
    el.textContent = dir === "BUY" ? "BULLISH" : dir === "SELL" ? "BEARISH" : "NEUTRAL";
    el.className = "tf-direction " + dir.toLowerCase();
    $(tf + "-trend").innerHTML = dir === "BUY" ? ic("up", "icon-green") + " Fast EMA above slow EMA"
                              : dir === "SELL" ? ic("down", "icon-red") + " Fast EMA below slow EMA"
                              : ic("minus") + " No clear direction";
  });
  if (d.strategy) $("strategy-title").textContent = d.strategy;
  if (d.rules) {
    var box = $("exec-rules"), status = $("exec-status");
    box.querySelectorAll(".exec-rule.rule").forEach(function(n) { n.remove(); });
    d.rules.forEach(function(r) {
      var n = document.createElement("div");
      n.className = "exec-rule rule";
      n.innerHTML = ic("check") + " " + esc(r);
      box.insertBefore(n, status);
    });
  }
}

// ── Signal feed ──────────────────────────────────────────────
function addSignal(sig) {
  var list = $("signal-list");
  var empty = list.querySelector(".empty-feed");
  if (empty) empty.remove();
  var dir = String(sig.direction).toLowerCase();
  var el  = document.createElement("div");
  el.className = "signal-card " + dir;
  el.innerHTML =
    '<div class="signal-header">' +
    '<span class="signal-dir ' + dir + '">' + ic(dir === "buy" ? "up" : "down") + " " + esc(sig.direction) + '</span>' +
    '<span class="signal-sym">' + esc(sig.symbol) + '</span>' +
    '<span class="signal-time">' + new Date().toLocaleTimeString() + '</span></div>' +
    '<div class="signal-reason">' + esc(sig.reason) + '</div>' +
    '<div class="signal-meta"><span>Stake $' + parseFloat(sig.stake).toFixed(2) + '</span>' +
    '<span>' + (sig.taken ? "Order placed" : "Not traded: " + esc(sig.note || "")) + '</span></div>';
  list.insertBefore(el, list.firstChild);
  while (list.children.length > 25) list.removeChild(list.lastChild);
}

// ── Trades ───────────────────────────────────────────────────
function renderTrades(trades) {
  var list = $("trade-list");
  if (!trades || !trades.length) { list.innerHTML = '<div class="empty-feed">No trades yet</div>'; return; }
  list.innerHTML = trades.map(function(t) {
    var done   = t.pnl != null;
    var pnlStr = done ? (t.pnl >= 0 ? "+" : "-") + "$" + Math.abs(parseFloat(t.pnl)).toFixed(2) : "—";
    var pnlCls = done ? (t.pnl > 0 ? "green" : t.pnl < 0 ? "red" : "") : "";
    var tip    = t.exit_reason ? ' title="' + esc(t.exit_reason) + '"' : "";
    return '<div class="trade-row"' + tip + '>' +
      '<span class="trade-dir ' + esc(String(t.direction).toLowerCase()) + '">' + esc(t.direction) + '</span>' +
      '<span class="trade-sym">' + esc(t.symbol) + '</span>' +
      '<span class="trade-stake">$' + parseFloat(t.stake).toFixed(2) + '</span>' +
      '<span class="trade-pnl ' + pnlCls + '">' + pnlStr + '</span>' +
      '<span class="trade-status ' + esc(String(t.status).toLowerCase()) + '">' + esc(t.status) + '</span></div>';
  }).join("");
}

function renderStats(stats) {
  if (!state.botRunning) { state.realizedTotal = parseFloat(stats.total_pnl || 0); renderBalanceMath(); }
  $("stat-total").textContent  = stats.total  || 0;
  $("stat-wins").textContent   = stats.wins   || 0;
  $("stat-losses").textContent = stats.losses || 0;
  var pnl = parseFloat(stats.total_pnl || 0);
  var el  = $("stat-pnl");
  el.textContent = (pnl >= 0 ? "+" : "-") + "$" + Math.abs(pnl).toFixed(2);
  el.style.color = pnl >= 0 ? "var(--green)" : "var(--red)";
}

// ── Controls ─────────────────────────────────────────────────
function selectPair(sym, btn) {
  if (state.botRunning) { showToast("Stop the bot first to change the pair."); return; }
  setSymbolUI(sym);
  resetCharts();
  refreshPending();
}

function setMode(mode) {
  if (state.botRunning && mode !== state.mode) { showToast("Stop the bot first to switch between DEMO and LIVE."); return; }
  state.mode = mode;
  $("btn-demo").classList.toggle("active", mode === "demo");
  $("btn-live").classList.toggle("active", mode === "live");
  $("mode-badge").textContent = mode.toUpperCase();
  $("mode-warning").style.display = mode === "live" ? "block" : "none";
  renderVerified(null);
  loadAccounts();
  refreshPending();
}

function loadAccounts() {
  var sel = $("account-select");
  var want = (state.saved && state.saved.account_id) || sel.value || "";
  fetch("/api/accounts?mode=" + state.mode).then(function(r) { return r.json(); }).then(function(d) {
    sel.innerHTML = '<option value="">Automatic (first matching account)</option>';
    if (!d.ok) { $("account-verified").textContent = "Could not list accounts: " + d.error; return; }
    d.accounts.forEach(function(a) {
      var o = document.createElement("option");
      o.value = a.account_id;
      o.textContent = a.account_id + " · " + (a.currency || "") + " " + (a.balance != null ? parseFloat(a.balance).toFixed(2) : "");
      sel.appendChild(o);
    });
    if (want) sel.value = want;
    showIdleBalance(d.accounts, sel.value);
    refreshPending();
  }).catch(function() { $("account-verified").textContent = "Could not list accounts"; });
}

// Bot not running: still show the real Deriv balance of the account (read-only REST call).
function showIdleBalance(accounts, selectedId) {
  if (state.botRunning || !accounts || !accounts.length) return;
  var a = accounts.filter(function(x) { return x.account_id === selectedId; })[0] || accounts[0];
  if (a.balance == null) return;
  state.balance = parseFloat(a.balance);
  $("balance-display").textContent = (a.currency || "USD") + " " + state.balance.toFixed(2);
  $("balance-kind").textContent = state.mode === "live" ? "Live account balance" : "Demo balance (virtual funds)";
  $("account-id-display").textContent = a.account_id;
  $("balance-note").textContent = "Read from Deriv — bot not running";
  renderBalanceMath();
}

function onAccountChange() {
  if (state.botRunning) { showToast("Stop the bot first to change the account."); $("account-select").value = (state.applied && state.applied.account_id) || ""; return; }
  refreshPending();
}

function toggleBot() {
  if (state.botRunning) { stopBot(); return; }
  validateLot(); validatePositions();
  if (state.mode === "live") { openLiveModal(); return; }
  startBot(false);
}

function startBot(confirmLive) {
  dismissError();
  closeSidebar();
  var body = getConfig();
  if (confirmLive) body.confirm_live = true;
  showToast("Activating on " + state.symbol + " (" + state.mode.toUpperCase() + ")...");
  applyState("starting", "Starting bot...", "", true);
  post("/api/bot/start", body).then(function(d) {
    if (d.ok) { applyPayload(d); return; }
    applyState("stopped", "Bot stopped", "", false);
    if (d.code === "live_confirmation_required") { openLiveModal(); return; }
    showError(d.error || "Could not start the bot");
  });
}

function stopBot() {
  applyState("stopping", "Stopping bot...", "", true);
  post("/api/bot/stop").then(function(d) {
    if (d.stopped) {
      state.applied = null;
      applyState("stopped", "Bot stopped", "", false);
      showToast(d.close_on_stop ? "Bot stopped — open trades were closed" : "Bot stopped — open trades will run to expiry");
    } else {
      showToast("Bot is still stopping...");
    }
  });
}

// ── Live confirmation ────────────────────────────────────────
function openLiveModal() {
  var cfg = getConfig();
  $("live-modal-stake").textContent = "$" + cfg.lot_size.toFixed(2);
  $("live-modal-pos").textContent   = cfg.max_positions;
  $("live-modal-check").checked = false;
  $("live-modal-ok").disabled = true;
  $("live-modal").classList.add("open");
}
function closeLiveModal() { $("live-modal").classList.remove("open"); }
function confirmLive() { closeLiveModal(); startBot(true); }
$("live-modal-check").addEventListener("change", function() { $("live-modal-ok").disabled = !this.checked; });

// ── Init ─────────────────────────────────────────────────────
(function init() {
  document.querySelectorAll("#strategy-select option").forEach(function(o) { STRATEGY_NAMES[o.value] = o.textContent; });
  setLot(state.lotSize);
  setPositions(state.maxPositions);
  $("mode-warning").style.display = state.mode === "live" ? "block" : "none";
  applyState("stopped", "Stopped", "", false);
  refreshPending();

  fetch("/api/trades").then(function(r) { return r.json(); }).then(function(d) {
    renderTrades(d.trades); renderStats(d.stats);
  });

  fetch("/api/bot/status").then(function(r) { return r.json(); }).then(function(d) {
    if (d.saved) state.saved = d.saved;
    if (d.applied) state.applied = d.applied;
    var st = d.status || {};
    if (d.running && d.applied) {
      // A bot is already running: show ITS pair/mode, and the saved stake/limits.
      state.mode = d.applied.mode;
      setSymbolUI(d.applied.symbol);
      $("btn-demo").classList.toggle("active", state.mode === "demo");
      $("btn-live").classList.toggle("active", state.mode === "live");
      $("mode-warning").style.display = state.mode === "live" ? "block" : "none";
      applyState(st.state, st.message, st.detail, true);
      renderVerified(st.account_type, st.account_id, state.mode);
    } else if (st.state === "error") {
      applyState("error", st.message, st.detail, false);
    }
    loadAccounts();
    refreshPending();
  });
})();

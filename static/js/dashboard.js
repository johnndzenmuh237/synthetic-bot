"use strict";

// ── State ────────────────────────────────────────────────────
var state = {
  symbol: "R_50", mode: "demo",
  botRunning: false, activeTf: "M1",
  lotSize: 1.00, maxPositions: 1,
  charts: {}, candles: {}
};

var MIN_LOT = 0.35;
var MAX_LOT = 500;

var SYMBOL_NAMES = {
  "R_10":"Volatility 10 Index","R_25":"Volatility 25 Index",
  "R_50":"Volatility 50 Index","R_75":"Volatility 75 Index",
  "R_100":"Volatility 100 Index",
  "1HZ10V":"Volatility 10 (1s) Index","1HZ25V":"Volatility 25 (1s) Index",
  "1HZ50V":"Volatility 50 (1s) Index","1HZ75V":"Volatility 75 (1s) Index",
  "1HZ100V":"Volatility 100 (1s) Index"
};

// ── Mobile sidebar ───────────────────────────────────────────
function toggleSidebar() {
  var s = document.getElementById("sidebar");
  var o = document.getElementById("sidebar-overlay");
  s.classList.toggle("open");
  o.classList.toggle("open");
}
function closeSidebar() {
  document.getElementById("sidebar").classList.remove("open");
  document.getElementById("sidebar-overlay").classList.remove("open");
}

// ── SocketIO ─────────────────────────────────────────────────
var socket = io();

socket.on("connect", function() {
  console.log("WS connected");
  socket.emit("request_snapshot");   // ask server to (re)send charts/status/trades
});

socket.on("trade_error", function(d) { showError(d.message); });

socket.on("bot_status", function(d) {
  updateStatus(d.state, d.message);
  if (d.state === "error") showError(d.message);
  else { dismissError(); if (d.state !== "stopped") showToast(d.message); }
});

socket.on("balance_update", function(d) {
  var bal = parseFloat(d.balance || 0);
  document.getElementById("balance-display").textContent =
    (d.currency || "USD") + " " + bal.toFixed(2);
  var pnl = parseFloat(d.daily_pnl || 0);
  var el  = document.getElementById("daily-pnl");
  el.textContent = "P&L: " + (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + "%";
  el.style.color = pnl >= 0 ? "var(--green)" : "var(--red)";
  if (d.account_id)
    document.getElementById("account-id-display").textContent = d.account_id;
});

socket.on("candles_init", function(d) {
  // The server only sends candles for the symbol the bot is actually running,
  // so adopt it instead of dropping data when the page default differs.
  if (d.symbol !== state.symbol) setSymbolUI(d.symbol);
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
  showToast("Trade #" + d.trade_id + " " + d.status +
            " " + sign + "$" + parseFloat(d.pnl).toFixed(2));
});

socket.on("trades_update", function(d) {
  renderTrades(d.trades);
  renderStats(d.stats);
});

socket.on("analysis_update", function(d) {
  updateAnalysis("h4", d.h4);
  updateAnalysis("h1", d.h1);
  document.getElementById("exec-status").textContent = "✅ Monitoring M1 (trend: M5)";
});

// ── Lot Size ─────────────────────────────────────────────────
function adjustLot(delta) {
  var cur  = parseFloat(document.getElementById("lot-input").value) || 1.0;
  var next = Math.round((cur + delta) * 100) / 100;
  next = Math.max(MIN_LOT, Math.min(MAX_LOT, next));
  document.getElementById("lot-input").value = next.toFixed(2);
  state.lotSize = next;
  updateLotDisplay();
}

function setLot(val) {
  val = Math.max(MIN_LOT, Math.min(MAX_LOT, parseFloat(val)));
  document.getElementById("lot-input").value = val.toFixed(2);
  state.lotSize = val;
  updateLotDisplay();
  document.querySelectorAll(".preset-btn").forEach(function(b) {
    b.classList.toggle("active", parseFloat(b.textContent) === val);
  });
}

function validateLot() {
  var val = parseFloat(document.getElementById("lot-input").value);
  if (isNaN(val) || val < MIN_LOT) val = MIN_LOT;
  if (val > MAX_LOT) val = MAX_LOT;
  val = Math.round(val * 100) / 100;
  document.getElementById("lot-input").value = val.toFixed(2);
  state.lotSize = val;
  updateLotDisplay();
}

function updateLotDisplay() {
  document.getElementById("lot-usd-label").textContent =
    "= $" + state.lotSize.toFixed(2) + " / trade";
  updateTotalRisk();
}

// ── Max Positions ────────────────────────────────────────────
function adjustPositions(delta) {
  var cur  = parseInt(document.getElementById("positions-input").value) || 1;
  var next = Math.max(1, Math.min(10, cur + delta));
  document.getElementById("positions-input").value = next;
  state.maxPositions = next;
  updateTotalRisk();
}

function setPositions(val) {
  val = Math.max(1, Math.min(10, parseInt(val)));
  document.getElementById("positions-input").value = val;
  state.maxPositions = val;
  updateTotalRisk();
  document.querySelectorAll(".pos-preset").forEach(function(b) {
    b.classList.toggle("active", parseInt(b.textContent) === val);
  });
}

function validatePositions() {
  var val = parseInt(document.getElementById("positions-input").value);
  if (isNaN(val) || val < 1) val = 1;
  if (val > 10) val = 10;
  document.getElementById("positions-input").value = val;
  state.maxPositions = val;
  updateTotalRisk();
}

function updateTotalRisk() {
  var lot   = state.lotSize || 1;
  var pos   = state.maxPositions || 1;
  var total = Math.round(lot * pos * 100) / 100;
  document.getElementById("total-risk-label").textContent =
    "Total per signal: $" + lot.toFixed(2) + " × " + pos + " = $" + total.toFixed(2);
}

// ── Update while running ─────────────────────────────────────
function updateSettings() {
  if (!state.botRunning) return;
  fetch("/api/bot/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lot_size: state.lotSize, max_positions: state.maxPositions }),
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.ok) {
      showToast("Settings updated ✓");
      updateRunningBadges();
    } else showToast("Error: " + d.error);
  });
}

// ── Charts ───────────────────────────────────────────────────
// One pane per timeframe (M1 / M5) inside #chart-container. Only the active
// pane is visible. Lines drawn: EMA10 (red) and EMA20 (purple) = the strategy EMAs.
var EMA_FAST = 10, EMA_SLOW = 20;

function initChart(tf, candles) {
  var pane = document.getElementById("chart-" + tf);
  if (!pane) return;
  var ph = document.getElementById("chart-placeholder");
  if (ph) ph.style.display = "none";

  if (!state.charts[tf]) {
    var box = document.getElementById("chart-container");
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
    var eFast = chart.addLineSeries({ color: "#ef4444", lineWidth: 1, title: "EMA" + EMA_FAST });
    var eSlow = chart.addLineSeries({ color: "#a855f7", lineWidth: 1, title: "EMA" + EMA_SLOW });
    state.charts[tf] = { chart: chart, cs: cs, eFast: eFast, eSlow: eSlow };

    new ResizeObserver(function() { sizeChart(tf); })
      .observe(document.getElementById("chart-container"));
  }
  populateChart(tf, candles);
}

function sizeChart(tf) {
  var c = state.charts[tf];
  var box = document.getElementById("chart-container");
  if (c && box.clientWidth > 0)
    c.chart.applyOptions({ width: box.clientWidth, height: box.clientHeight });
}

function populateChart(tf, candles) {
  if (!state.charts[tf] || !candles || !candles.length) return;
  var c = state.charts[tf];
  c.cs.setData(candles.map(function(x) {
    return { time: x.epoch, open: +x.open, high: +x.high, low: +x.low, close: +x.close };
  }));
  c.eFast.setData(computeEma(candles, EMA_FAST));
  c.eSlow.setData(computeEma(candles, EMA_SLOW));
}

function updateCandle(tf, candle) {
  var c = state.charts[tf];
  if (!c) return;
  c.cs.update({ time: candle.epoch, open: +candle.open,
                high: +candle.high, low: +candle.low, close: +candle.close });
  var store = state.candles[tf] || [];
  if (store.length && store[store.length-1].epoch === candle.epoch)
    store[store.length-1] = candle;
  else { store.push(candle); state.candles[tf] = store; }
  var f = computeEma(store, EMA_FAST), sl = computeEma(store, EMA_SLOW);
  if (f.length)  c.eFast.update(f[f.length-1]);
  if (sl.length) c.eSlow.update(sl[sl.length-1]);
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
    var pane = document.getElementById("chart-" + t);
    if (pane) pane.classList.toggle("active", t === tf);
  });
  if (state.charts[tf]) {
    sizeChart(tf);
    state.charts[tf].chart.timeScale().fitContent();
  }
}

function switchChart(tf) {
  state.activeTf = tf;
  document.querySelectorAll(".tf-tab").forEach(function(b) {
    b.classList.toggle("active", b.textContent === tf);
  });
  showChart(tf);
  if (!state.charts[tf]) showToast(tf + " chart loading...");
}

function resetCharts() {
  Object.keys(state.charts).forEach(function(tf) {
    try { state.charts[tf].chart.remove(); } catch (e) {}
  });
  state.charts = {}; state.candles = {};
  ["M1", "M5"].forEach(function(t) {
    var pane = document.getElementById("chart-" + t);
    if (pane) pane.innerHTML = "";
  });
  var ph = document.getElementById("chart-placeholder");
  if (ph) ph.style.display = "flex";
}

// Keep sidebar/top-bar labels in sync with a symbol
function setSymbolUI(sym) {
  if (state.symbol !== sym) resetCharts();
  state.symbol = sym;
  document.getElementById("active-symbol-label").textContent = sym;
  document.getElementById("active-name-label").textContent   = SYMBOL_NAMES[sym] || sym;
  document.querySelectorAll(".pair-btn").forEach(function(b) {
    b.classList.toggle("active", b.getAttribute("data-symbol") === sym);
  });
}

// ── Analysis ─────────────────────────────────────────────────
function updateAnalysis(tf, data) {
  var dirEl   = document.getElementById(tf + "-direction");
  var trendEl = document.getElementById(tf + "-trend");
  if (!dirEl) return;
  var d = data.direction;
  dirEl.textContent = d === "NONE" ? "Consolidating" : d;
  dirEl.className   = "tf-direction " + d.toLowerCase();
  document.getElementById(tf + "-e21").textContent  = data.ema21;
  document.getElementById(tf + "-e50").textContent  = data.ema50;
  document.getElementById(tf + "-e100").textContent = data.ema100;
  trendEl.textContent = data.trending
    ? (d === "BUY" ? "🟢 Uptrend" : d === "SELL" ? "🔴 Downtrend" : "⚪ Flat")
    : "⚠️ Consolidating — waiting";
  trendEl.style.color = data.trending ? "var(--t2)" : "var(--gold)";
}

// ── Signal Feed ──────────────────────────────────────────────
function addSignal(sig) {
  var list = document.getElementById("signal-list");
  var empty = list.querySelector(".empty-feed");
  if (empty) empty.remove();
  var now  = new Date().toLocaleTimeString();
  var el   = document.createElement("div");
  el.className = "signal-card " + sig.direction.toLowerCase();
  el.innerHTML =
    '<div class="signal-header">' +
    '<span class="signal-dir ' + sig.direction.toLowerCase() + '">' + sig.direction + '</span>' +
    '<span class="signal-sym">' + sig.symbol + '</span>' +
    '<span class="signal-time">' + now + '</span></div>' +
    '<div class="signal-reason">' + sig.reason + '</div>' +
    '<div class="signal-meta">' +
    '<span>Lot:' + parseFloat(sig.lot_size).toFixed(2) + '</span>' +
    '<span>$' + parseFloat(sig.stake).toFixed(2) + '</span>' +
    '<span>Pos:' + sig.max_positions + '</span></div>';
  list.insertBefore(el, list.firstChild);
  while (list.children.length > 25) list.removeChild(list.lastChild);
}

// ── Trades ───────────────────────────────────────────────────
function renderTrades(trades) {
  var list = document.getElementById("trade-list");
  if (!trades || !trades.length) {
    list.innerHTML = '<div class="empty-feed">No trades yet</div>'; return;
  }
  list.innerHTML = trades.map(function(t) {
    var lot    = t.lot_size ? parseFloat(t.lot_size).toFixed(2) : "1.00";
    var pnlStr = t.pnl != null ? (t.pnl >= 0 ? "+" : "") + "$" + parseFloat(t.pnl).toFixed(2) : "OPEN";
    var pnlCls = t.pnl != null ? (t.pnl >= 0 ? "green" : "red") : "";
    return '<div class="trade-row">' +
      '<span class="trade-dir ' + t.direction.toLowerCase() + '">' + t.direction + '</span>' +
      '<span class="trade-sym">' + t.symbol + '</span>' +
      '<span class="trade-lot">L' + lot + '</span>' +
      '<span class="trade-stake">$' + parseFloat(t.stake).toFixed(2) + '</span>' +
      '<span class="trade-pnl ' + pnlCls + '">' + pnlStr + '</span>' +
      '<span class="trade-status ' + t.status.toLowerCase() + '">' + t.status + '</span></div>';
  }).join("");
}

function renderStats(stats) {
  document.getElementById("stat-total").textContent  = stats.total   || 0;
  document.getElementById("stat-wins").textContent   = stats.wins    || 0;
  document.getElementById("stat-losses").textContent = stats.losses  || 0;
  var pnl = parseFloat(stats.total_pnl || 0);
  var el  = document.getElementById("stat-pnl");
  el.textContent = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
  el.style.color = pnl >= 0 ? "var(--green)" : "var(--red)";
}

// ── Status ───────────────────────────────────────────────────
function updateStatus(s, msg) {
  document.getElementById("status-dot").className   = "status-dot " + s;
  document.getElementById("status-text").textContent = msg || s;
  if (s === "running") {
    state.botRunning = true;
    setBotBtn(true);
    document.getElementById("btn-update").style.display   = "block";
    document.getElementById("running-info").style.display = "flex";
    updateRunningBadges();
  } else if (s === "stopped" || s === "error") {
    state.botRunning = false;
    setBotBtn(false);
    document.getElementById("btn-update").style.display   = "none";
    document.getElementById("running-info").style.display = "none";
  }
}

function setBotBtn(running) {
  document.getElementById("bot-btn-icon").textContent = running ? "⏹" : "▶";
  document.getElementById("bot-btn-text").textContent = running ? "Stop Bot" : "Start Bot";
  document.getElementById("bot-btn").classList.toggle("running", running);
}

function updateRunningBadges() {
  document.getElementById("ri-lot").textContent  = "Lot:" + state.lotSize.toFixed(2);
  document.getElementById("ri-pos").textContent  = "Pos:" + state.maxPositions;
  document.getElementById("ri-mode").textContent = state.mode.toUpperCase();
}

function showError(msg) {
  document.getElementById("error-text").textContent = "⚠️ " + msg;
  document.getElementById("error-banner").style.display = "flex";
}
function dismissError() {
  document.getElementById("error-banner").style.display = "none";
}

// ── Controls ─────────────────────────────────────────────────
function selectPair(sym, btn) {
  if (state.botRunning) { showToast("Stop the bot first before switching pairs."); return; }
  state.symbol = sym;
  document.querySelectorAll(".pair-btn").forEach(function(b) { b.classList.remove("active"); });
  btn.classList.add("active");
  document.getElementById("active-symbol-label").textContent = sym;
  document.getElementById("active-name-label").textContent   = SYMBOL_NAMES[sym] || sym;
  resetCharts();
}

function setMode(mode) {
  state.mode = mode;
  document.getElementById("btn-demo").classList.toggle("active", mode === "demo");
  document.getElementById("btn-live").classList.toggle("active", mode === "live");
  document.getElementById("mode-badge").textContent = mode.toUpperCase();
  document.getElementById("mode-warning").style.display = mode === "live" ? "block" : "none";
}

function toggleBot() {
  if (state.botRunning) {
    fetch("/api/bot/stop", { method: "POST" }).then(function() {
      state.botRunning = false;
      setBotBtn(false);
      updateStatus("stopped", "Bot stopped");
      document.getElementById("btn-update").style.display   = "none";
      document.getElementById("running-info").style.display = "none";
      showToast("Bot stopped.");
    });
  } else {
    state.lotSize      = parseFloat(document.getElementById("lot-input").value) || 1.0;
    state.maxPositions = parseInt(document.getElementById("positions-input").value) || 1;
    dismissError();
    showToast("Starting on " + state.symbol + " (" + state.mode.toUpperCase() + ")...");
    closeSidebar();

    fetch("/api/bot/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol:        state.symbol,
        mode:          state.mode,
        lot_size:      state.lotSize,
        max_positions: state.maxPositions,
      }),
    }).then(function(r) { return r.json(); }).then(function(d) {
      if (!d.ok) showError(d.error || "Could not start bot");
    });
  }
}

// ── Init ─────────────────────────────────────────────────────
// Sync state with the pair button the page highlights by default
(function() {
  var b = document.querySelector(".pair-btn.active");
  if (b) {
    state.symbol = b.getAttribute("data-symbol");
    document.getElementById("active-symbol-label").textContent = state.symbol;
    document.getElementById("active-name-label").textContent   = SYMBOL_NAMES[state.symbol] || state.symbol;
  }
})();

fetch("/api/trades").then(function(r) { return r.json(); }).then(function(d) {
  renderTrades(d.trades); renderStats(d.stats);
});

fetch("/api/bot/status").then(function(r) { return r.json(); }).then(function(d) {
  if (d.running) {
    state.botRunning   = true;
    state.symbol       = d.symbol;
    state.mode         = d.mode;
    state.lotSize      = d.lot_size      || 1.0;
    state.maxPositions = d.max_positions || 1;
    setMode(d.mode);
    document.getElementById("lot-input").value       = state.lotSize.toFixed(2);
    document.getElementById("positions-input").value = state.maxPositions;
    document.getElementById("active-symbol-label").textContent = d.symbol;
    document.getElementById("active-name-label").textContent   = SYMBOL_NAMES[d.symbol] || d.symbol;
    updateLotDisplay(); updateTotalRisk();
    setBotBtn(true);
    document.getElementById("btn-update").style.display   = "block";
    document.getElementById("running-info").style.display = "flex";
    updateStatus("running", "Running on " + d.symbol);
  }
});

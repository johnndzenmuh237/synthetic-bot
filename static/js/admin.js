/* Admin page: every account, its connection/bot state, and activate / deactivate controls. */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var users = [];
  var BASE = ($("adm-root") || {}).getAttribute ? $("adm-root").getAttribute("data-base") : "";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; });
  }
  function when(s) {
    if (!s) return "-";
    var d = new Date(String(s).replace(" ", "T") + "Z");
    return isNaN(d) ? esc(s) : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function money(v) { v = Number(v || 0); return (v > 0 ? "+" : "") + v.toFixed(2); }
  function cls(v) { v = Number(v); return v > 0 ? "pos" : (v < 0 ? "neg" : ""); }
  function api(url, opts) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, opts || {})).then(function (r) {
      if (r.redirected || r.status === 404) { location.href = BASE + "/login"; throw new Error("login"); }
      return r.json();
    });
  }

  function summary(s) {
    $("adm-summary").innerHTML =
      '<div class="ht"><span>Accounts</span><b>' + s.total + "</b></div>" +
      '<div class="ht"><span>Active</span><b class="pos">' + s.active + "</b></div>" +
      '<div class="ht"><span>Deactivated</span><b class="neg">' + s.deactivated + "</b></div>" +
      '<div class="ht"><span>Bots running</span><b>' + s.bots_running + " / " + s.max_bots + "</b></div>";
  }

  function render() {
    var q = $("adm-search").value.trim().toLowerCase(), f = $("adm-filter").value;
    var h = "<thead><tr><th>User</th><th>Status</th><th>Tokens</th><th>Bot</th><th>Trades</th>" +
            "<th>Demo P/L</th><th>Live P/L</th><th>Last login</th><th>Joined</th><th>Actions</th></tr></thead><tbody>";
    var shown = 0;
    users.forEach(function (u) {
      if (q && u.username.toLowerCase().indexOf(q) < 0) return;
      if (f === "active" && !u.is_active) return;
      if (f === "off" && u.is_active) return;
      if (f === "running" && !u.bot_running) return;
      shown++;
      var bot = u.bot_running ? '<span class="st st-OPEN">' + esc(u.bot.mode) + " &middot; " + esc(u.bot.symbol) + "</span>" : '<span class="st">stopped</span>';
      var toks = (u.has_demo_token ? '<span class="cb on">Demo</span>' : '<span class="cb off">Demo</span>') +
                 (u.has_live_token ? '<span class="cb on">Live</span>' : '<span class="cb off">Live</span>');
      var act = u.is_admin ? '<span class="st st-act">admin</span>' :
        (u.is_active ? '<button class="adm-btn adm-off" data-act="off" data-id="' + u.id + '" type="button">Deactivate</button>'
                     : '<button class="adm-btn adm-on" data-act="on" data-id="' + u.id + '" type="button">Activate</button>');
      h += "<tr><td data-label='User'><b>" + esc(u.username) + "</b>" + (u.is_admin ? " (admin)" : "") + "</td>" +
        "<td data-label='Status'>" + (u.is_active ? '<span class="st st-WIN">Active</span>' : '<span class="st st-LOSS">Deactivated</span>') + "</td>" +
        "<td data-label='Tokens'>" + toks + "</td><td data-label='Bot'>" + bot + "</td>" +
        "<td data-label='Trades'>" + u.trades + (u.open_trades ? " (" + u.open_trades + " open)" : "") + "</td>" +
        "<td data-label='Demo P/L' class='" + cls(u.pnl_demo) + "'>" + money(u.pnl_demo) + "</td>" +
        "<td data-label='Live P/L' class='" + cls(u.pnl_live) + "'>" + money(u.pnl_live) + "</td>" +
        "<td data-label='Last login'>" + when(u.last_login) + "</td><td data-label='Joined'>" + when(u.created_at) + "</td>" +
        "<td data-label='Actions'><button class='adm-btn' data-act='view' data-id='" + u.id + "' type='button'>View</button> " + act + "</td></tr>";
    });
    if (!shown) h += '<tr><td colspan="10" class="empty">No users match.</td></tr>';
    $("adm-table").innerHTML = h + "</tbody>";
  }

  function load() {
    $("adm-msg").textContent = "Loading...";
    api(BASE + "/api/users").then(function (d) {
      $("adm-msg").textContent = "";
      if (!d.ok) { $("adm-msg").textContent = d.error || "Could not load users"; return; }
      users = d.users; summary(d.summary); render();
    }).catch(function (e) { if (e.message !== "login") $("adm-msg").textContent = "Could not load: " + e.message; });
  }

  function setActive(u, active) {
    var body = { active: active };
    if (!active) {
      var msg = "Deactivate " + u.username + "?\n\nTheir bot is stopped and they cannot log in or trade until you activate them again.";
      if (!confirm(msg)) return;
      if (u.open_trades && confirm("They have " + u.open_trades + " open trade(s).\n\nOK = close them now.\nCancel = leave them to run to expiry.")) body.close_trades = true;
    }
    $("adm-msg").textContent = "Working...";
    api(BASE + "/api/users/" + u.id + "/active", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (d) { $("adm-msg").textContent = d.ok ? "" : (d.error || "Failed"); load(); });
  }

  function detail(u) {
    $("adm-detail-title").textContent = u.username;
    $("adm-detail-body").textContent = "Loading...";
    $("adm-detail").classList.add("open");
    api(BASE + "/api/users/" + u.id).then(function (d) {
      if (!d.ok) { $("adm-detail-body").textContent = d.error; return; }
      var h = '<p class="conn-help">Demo: ' + d.stats_demo.total + " closed, P/L " + money(d.stats_demo.total_pnl) +
              " &middot; Live: " + d.stats_live.total + " closed, P/L " + money(d.stats_live.total_pnl) + "</p>";
      h += "<h4>Recent trades</h4><div class='hist-scroll'><table class='hist-table'><thead><tr><th>Opened</th><th>Mode</th><th>Symbol</th><th>Side</th><th>Result</th><th>P/L</th></tr></thead><tbody>";
      d.trades.slice(0, 15).forEach(function (t) {
        h += "<tr><td data-label='Opened'>" + when(t.opened_at) + "</td><td data-label='Mode'>" + esc(t.mode) + "</td><td data-label='Symbol'>" + esc(t.symbol) +
             "</td><td data-label='Side'>" + esc(t.direction) + "</td><td data-label='Result'><span class='st st-" + esc(t.status) + "'>" + esc(t.status) +
             "</span></td><td data-label='P/L' class='" + cls(t.pnl) + "'>" + (t.pnl == null ? "-" : money(t.pnl)) + "</td></tr>";
      });
      if (!d.trades.length) h += "<tr><td colspan='6' class='empty'>No trades yet.</td></tr>";
      h += "</tbody></table></div><h4>Recent activity</h4><div class='hist-scroll'><table class='hist-table'><thead><tr><th>When</th><th>Action</th><th>Details</th></tr></thead><tbody>";
      d.activity.slice(0, 15).forEach(function (a) {
        h += "<tr><td data-label='When'>" + when(a.created_at) + "</td><td data-label='Action'><span class='st st-act'>" + esc(String(a.kind).replace(/_/g, " ")) +
             "</span></td><td data-label='Details'>" + esc(a.detail) + "</td></tr>";
      });
      $("adm-detail-body").innerHTML = h + "</tbody></table></div>";
    });
  }

  $("adm-table").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-act]"); if (!b) return;
    var u = users.filter(function (x) { return String(x.id) === b.getAttribute("data-id"); })[0]; if (!u) return;
    var a = b.getAttribute("data-act");
    if (a === "view") detail(u); else setActive(u, a === "on");
  });
  $("adm-search").oninput = render; $("adm-filter").onchange = render; $("adm-refresh").onclick = load;
  $("adm-detail-close").onclick = function () { $("adm-detail").classList.remove("open"); };
  load(); setInterval(load, 30000);
})();
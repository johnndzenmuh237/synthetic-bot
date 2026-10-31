/* History page: all trades, daily/monthly/yearly totals and the account activity log. */
(function () {
  var tab = "trades", page = 1;
  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  /* Server times are UTC "YYYY-MM-DD HH:MM:SS" - show them in the viewer's local time. */
  function when(s) {
    if (!s) return "-";
    var d = new Date(String(s).replace(" ", "T") + "Z");
    if (isNaN(d)) return esc(s);
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit",
                                         hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function money(v) {
    if (v == null || v === "") return "-";
    var n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(2);
  }
  function cls(v) { v = Number(v); return v > 0 ? "pos" : (v < 0 ? "neg" : ""); }

  function filters() {
    var q = [];
    [["mode", "f-mode"], ["symbol", "f-symbol"], ["status", "f-status"], ["from", "f-from"], ["to", "f-to"]]
      .forEach(function (p) { var v = $(p[1]).value; if (v) q.push(p[0] + "=" + encodeURIComponent(v)); });
    return q;
  }
  function get(url, cb) {
    $("hist-msg").textContent = "Loading...";
    fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (r.status === 401 || r.redirected) { location.href = "/login"; throw new Error("login"); }
      return r.json();
    }).then(function (d) { $("hist-msg").textContent = ""; cb(d); })
      .catch(function (e) { if (e.message !== "login") $("hist-msg").textContent = "Could not load: " + e.message; });
  }

  function totals(t) {
    var rate = t.closed ? Math.round(100 * t.wins / t.closed) : 0;
    $("hist-totals").innerHTML =
      '<div class="ht"><span>Total profit / loss</span><b class="' + cls(t.pnl) + '">' + money(t.pnl) + "</b></div>" +
      '<div class="ht"><span>Closed trades</span><b>' + t.closed + "</b></div>" +
      '<div class="ht"><span>Wins</span><b class="pos">' + t.wins + "</b></div>" +
      '<div class="ht"><span>Losses</span><b class="neg">' + t.losses + "</b></div>" +
      '<div class="ht"><span>Win rate</span><b>' + rate + "%</b></div>";
  }

  function pager(d) {
    var el = $("hist-pager");
    if (!d || d.pages <= 1) { el.innerHTML = ""; return; }
    el.innerHTML = '<button type="button" id="pg-prev"' + (d.page <= 1 ? " disabled" : "") + '>&laquo; Newer</button>' +
      "<span>Page " + d.page + " of " + d.pages + " &middot; " + d.total + " rows</span>" +
      '<button type="button" id="pg-next"' + (d.page >= d.pages ? " disabled" : "") + ">Older &raquo;</button>";
    $("pg-prev").onclick = function () { page--; load(); };
    $("pg-next").onclick = function () { page++; load(); };
  }

  function loadTrades() {
    get("/api/history?" + filters().concat(["page=" + page, "per=50"]).join("&"), function (d) {
      totals(d.totals);
      var h = "<thead><tr><th>Opened</th><th>Closed</th><th>Account</th><th>Symbol</th><th>Side</th>" +
              "<th>Stake</th><th>Result</th><th>Profit / loss</th><th>Why it closed</th></tr></thead><tbody>";
      d.trades.forEach(function (t) {
        h += "<tr><td>" + when(t.opened_at) + "</td><td>" + when(t.closed_at) + "</td><td>" + esc(t.mode) +
             "</td><td>" + esc(t.symbol) + "</td><td>" + esc(t.direction) + "</td><td>" + esc(t.stake) +
             '</td><td><span class="st st-' + esc(t.status) + '">' + esc(t.status) + "</span></td>" +
             '<td class="' + cls(t.pnl) + '">' + money(t.pnl) + "</td><td>" + esc(t.exit_reason || "") + "</td></tr>";
      });
      if (!d.trades.length) h += '<tr><td colspan="9" class="empty">No trades match these filters yet.</td></tr>';
      $("hist-table").innerHTML = h + "</tbody>";
      pager(d);
    });
  }

  function loadSummary() {
    get("/api/history/summary?" + filters().concat(["period=" + tab]).join("&"), function (d) {
      var closed = d.rows.reduce(function (a, r) { return a + r.trades; }, 0);
      var wins = d.rows.reduce(function (a, r) { return a + r.wins; }, 0);
      var losses = d.rows.reduce(function (a, r) { return a + r.losses; }, 0);
      totals({ pnl: d.pnl, closed: closed, wins: wins, losses: losses });
      var label = { day: "Day", month: "Month", year: "Year" }[tab];
      var h = "<thead><tr><th>" + label + "</th><th>Trades</th><th>Wins</th><th>Losses</th>" +
              "<th>Total staked</th><th>Profit / loss</th></tr></thead><tbody>";
      d.rows.forEach(function (r) {
        h += "<tr><td>" + esc(r.period) + "</td><td>" + r.trades + '</td><td class="pos">' + r.wins +
             '</td><td class="neg">' + r.losses + "</td><td>" + r.staked.toFixed(2) + '</td><td class="' +
             cls(r.pnl) + '"><b>' + money(r.pnl) + "</b></td></tr>";
      });
      if (!d.rows.length) h += '<tr><td colspan="6" class="empty">No closed trades in this range yet.</td></tr>';
      $("hist-table").innerHTML = h + "</tbody>";
      $("hist-pager").innerHTML = "";
    });
  }

  function loadActivity() {
    get("/api/activity?page=" + page + "&per=50", function (d) {
      $("hist-totals").innerHTML = '<div class="ht"><span>Recorded actions</span><b>' + d.total + "</b></div>";
      var h = "<thead><tr><th>When</th><th>Action</th><th>Details</th></tr></thead><tbody>";
      d.activity.forEach(function (a) {
        h += "<tr><td>" + when(a.created_at) + '</td><td><span class="st st-act">' +
             esc(String(a.kind).replace(/_/g, " ")) + "</span></td><td>" + esc(a.detail) + "</td></tr>";
      });
      if (!d.activity.length) h += '<tr><td colspan="3" class="empty">Nothing recorded yet.</td></tr>';
      $("hist-table").innerHTML = h + "</tbody>";
      pager(d);
    });
  }

  function load() {
    $("csv-link").href = "/history.csv" + (filters().length ? "?" + filters().join("&") : "");
    if (tab === "trades") loadTrades();
    else if (tab === "activity") loadActivity();
    else loadSummary();
  }

  document.querySelectorAll("#hist-tabs button").forEach(function (b) {
    b.onclick = function () {
      document.querySelectorAll("#hist-tabs button").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active"); tab = b.getAttribute("data-tab"); page = 1; load();
    };
  });
  $("f-apply").onclick = function () { page = 1; load(); };
  $("f-clear").onclick = function () {
    ["f-symbol", "f-status", "f-from", "f-to"].forEach(function (id) { $(id).value = ""; });
    $("f-mode").value = ""; page = 1; load();
  };
  load();
})();
/* Deriv connection panel: each user saves their OWN demo / live token. Tokens are sent once over HTTPS,
   checked with Deriv, stored encrypted on the server and never sent back to the browser. */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var toast = function (m) { if (window.showToast) window.showToast(m); };

  function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Server error" }; }); });
  }
  function status(mode, t) {
    var el = $("tok-" + mode + "-status"); if (!el) return;
    var s = t[mode];
    if (s.source === "own") el.innerHTML = '<span class="ok">Connected</span> &middot; ' + s.hint;
    else if (s.source === "server") el.innerHTML = '<span class="ok">Using the server\'s ' + mode + ' token (admin)</span>';
    else el.innerHTML = '<span class="bad">Not connected</span>';
    $("tok-" + mode + "-remove").style.display = s.source === "own" ? "" : "none";
  }
  function render(t) {
    status("demo", t); status("live", t);
    $("conn-badges").innerHTML =
      '<span class="cb ' + (t.demo.connected ? "on" : "off") + '">Demo</span>' +
      '<span class="cb ' + (t.live.connected ? "on" : "off") + '">Live</span>';
    var ban = $("connect-banner");
    if (ban) ban.hidden = !!(t.demo.connected || t.live.connected);
    var box = $("conn-box");
    if (!t.demo.connected && !t.live.connected && box && !box.hasAttribute("data-seen")) { box.open = true; }
    if (box) box.setAttribute("data-seen", "1");
  }
  function load() {
    fetch("/api/tokens", { credentials: "same-origin" }).then(function (r) { return r.json(); })
      .then(function (d) { if (d.ok) render(d.tokens); }).catch(function () {});
  }
  function wire(mode) {
    $("tok-" + mode + "-save").onclick = function () {
      var inp = $("tok-" + mode), btn = this, token = inp.value.trim();
      var el = $("tok-" + mode + "-status");
      if (!token) { el.innerHTML = '<span class="bad">Paste your token first</span>'; return; }
      btn.disabled = true; el.textContent = "Checking with Deriv...";
      post("/api/tokens", { mode: mode, token: token }).then(function (d) {
        btn.disabled = false;
        if (d.ok) {
          inp.value = ""; render(d.tokens);
          if ($("gate-root")) { el.innerHTML = '<span class="ok">Connected - opening your dashboard...</span>'; setTimeout(function () { location.href = "/dashboard"; }, 900); return; }
          el.innerHTML += ' &middot; account ' + (d.account.account_id || "") +
            (d.account.balance != null ? " (" + Number(d.account.balance).toFixed(2) + " " + (d.account.currency || "") + ")" : "");
          toast(mode.toUpperCase() + " token connected");
        } else {
          el.innerHTML = '<span class="bad"></span>'; el.firstChild.textContent = d.error || "Could not save the token";
        }
      }).catch(function () { btn.disabled = false; el.innerHTML = '<span class="bad">Network error</span>'; });
    };
    $("tok-" + mode + "-remove").onclick = function () {
      if (!confirm("Remove your " + mode + " token?")) return;
      post("/api/tokens/remove", { mode: mode }).then(function (d) {
        if (d.ok) { render(d.tokens); toast(mode.toUpperCase() + " token removed"); }
        else { var el = $("tok-" + mode + "-status"); el.innerHTML = '<span class="bad"></span>'; el.firstChild.textContent = d.error; }
      });
    };
  }
  var cb = $("connect-banner-btn");
  if (cb) cb.onclick = function () {
    var box = $("conn-box"); if (box) box.open = true;
    if (window.toggleSidebar && window.innerWidth <= 768) {
      var sb = document.querySelector(".sidebar");
      if (sb && !sb.classList.contains("open")) window.toggleSidebar();
    }
    if (box) setTimeout(function () { box.scrollIntoView({ behavior: "smooth", block: "center" }); }, 250);
  };
  wire("demo"); wire("live"); load();
})();
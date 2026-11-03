/* Install-as-app support. Registers this app's service worker and shows an "Install app" button when the
   browser offers installation (Chrome / Edge / Android). On iPhone/iPad it shows the "Add to Home Screen" hint.
   The trading app and the admin console load this with different data-sw / data-scope, so they install separately. */
(function () {
  var me = document.currentScript || {};
  var sw = me.getAttribute && me.getAttribute("data-sw");
  var scope = me.getAttribute && me.getAttribute("data-scope");
  if (sw && "serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register(sw, { scope: scope || "/" }).catch(function () {});
    });
  }
  function device() {
    var u = navigator.userAgent || "";
    if (/iphone|ipod/i.test(u)) return "ios";
    if (/ipad/i.test(u) || (/macintosh/i.test(u) && navigator.maxTouchPoints > 1)) return "ipad";
    if (/android/i.test(u)) return /mobile/i.test(u) ? "android" : "tablet";
    return "desktop";
  }
  var STEPS = {
    android: ["Tap the browser menu <b>&#8942;</b> (top right)", "Tap <b>Install app</b> or <b>Add to Home screen</b>", "Tap <b>Install</b> &mdash; the icon appears on your home screen"],
    tablet: ["Tap the browser menu <b>&#8942;</b> (top right)", "Tap <b>Install app</b> or <b>Add to Home screen</b>", "Tap <b>Install</b> &mdash; the icon appears on your home screen"],
    ios: ["Open this page in <b>Safari</b>", "Tap the <b>Share</b> button (square with an arrow)", "Choose <b>Add to Home Screen</b>, then <b>Add</b>"],
    ipad: ["Open this page in <b>Safari</b>", "Tap the <b>Share</b> button (top of the screen)", "Choose <b>Add to Home Screen</b>, then <b>Add</b>"],
    desktop: ["Look at the right end of the address bar", "Click the <b>install</b> icon (a screen with a down arrow)", "Click <b>Install</b> &mdash; it opens as its own window"]
  };
  function help() {
    if (document.getElementById("pwa-help")) return;
    var o = document.createElement("div"); o.id = "pwa-help"; o.className = "pwa-help";
    var li = STEPS[device()].map(function (t) { return "<li>" + t + "</li>"; }).join("");
    o.innerHTML = '<div class="pwa-card" role="dialog" aria-modal="true" aria-labelledby="pwa-h">' +
      '<div class="pwa-ico"><svg class="icon" aria-hidden="true"><use href="#i-save"/></svg></div>' +
      '<h3 id="pwa-h">Install this app</h3>' +
      '<p>Add it to your device for one-tap access, like any other app.</p><ol>' + li + '</ol>' +
      '<button type="button" class="pwa-ok">Got it</button></div>';
    o.addEventListener("click", function (e) { if (e.target === o || e.target.className === "pwa-ok") o.remove(); });
    document.body.appendChild(o);
  }
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  var deferred = null;
  function each(sel, fn) { [].forEach.call(document.querySelectorAll(sel), fn); }
  function show(on) { each("[data-install]", function (b) { b.hidden = !on; }); }
  if (standalone) { show(false); return; }
  var ua = navigator.userAgent || "";
  var ios = (/iphone|ipad|ipod/i.test(ua) || (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1)) && !window.MSStream;
  if (!ios) show(true);                       // always offer it; if the browser has not offered the install prompt, explain how
  window.addEventListener("beforeinstallprompt", function (e) { e.preventDefault(); deferred = e; show(true); });
  window.addEventListener("appinstalled", function () { deferred = null; show(false); each("[data-install-ios]", function (b) { b.hidden = true; }); });
  each("[data-install]", function (b) {
    b.addEventListener("click", function () {
      if (!deferred) {
        help();
        return;
      }
      deferred.prompt();
      deferred.userChoice.then(function () { deferred = null; show(false); });
    });
  });
  if (ios) each("[data-install-ios]", function (b) { b.hidden = false; });
})();
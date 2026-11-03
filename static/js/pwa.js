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
  function help() {
    if (document.getElementById("pwa-help")) return;
    var o = document.createElement("div"); o.id = "pwa-help"; o.className = "pwa-help";
    o.innerHTML = '<div class="pwa-card" role="dialog" aria-modal="true" aria-labelledby="pwa-h">' +
      '<div class="pwa-ico"><svg class="icon" aria-hidden="true"><use href="#i-save"/></svg></div>' +
      '<h3 id="pwa-h">Install this app</h3>' +
      '<p>Add it to your device for one-tap access, like any other app.</p>' +
      '<ol><li>Open your browser menu <b>&#8942;</b> (top right)</li>' +
      '<li>Choose <b>Install app</b> or <b>Add to Home screen</b></li>' +
      '<li>Confirm &mdash; the icon appears on your home screen</li></ol>' +
      '<p class="pwa-small">On a computer, use the install icon at the right end of the address bar.</p>' +
      '<button type="button" class="pwa-ok">Got it</button></div>';
    function close() { o.remove(); }
    o.addEventListener("click", function (e) { if (e.target === o || (e.target.className || "") === "pwa-ok") close(); });
    document.body.appendChild(o);
  }
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  var deferred = null;
  function each(sel, fn) { [].forEach.call(document.querySelectorAll(sel), fn); }
  function show(on) { each("[data-install]", function (b) { b.hidden = !on; }); }
  if (standalone) { show(false); return; }
  var ua = navigator.userAgent || "";
  var ios = /iphone|ipad|ipod/i.test(ua) && !window.MSStream;
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
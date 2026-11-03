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
        alert("To install: open your browser menu (the three dots, top right) and choose \"Install app\" or \"Add to Home screen\". " +
              "On a computer, look for the install icon at the right end of the address bar.");
        return;
      }
      deferred.prompt();
      deferred.userChoice.then(function () { deferred = null; show(false); });
    });
  });
  if (ios) each("[data-install-ios]", function (b) { b.hidden = false; });
})();
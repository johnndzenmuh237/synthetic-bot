function showToast(msg, duration) {
  duration = duration || 3500;
  var t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(function() { t.classList.remove("show"); }, duration);
}

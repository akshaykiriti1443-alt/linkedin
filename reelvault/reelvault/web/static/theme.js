/* Applies the member's saved theme before the page paints (no flash). Runs as a plain script; CSP allows 'self'. */
(function () {
  try {
    var t = localStorage.getItem("rv.theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow the system */ }
})();

/* Runs as a plain script in <head>, before the page paints (CSP allows 'self').
   1. Embed mode (D86): inside the OneX page, ReelVault hides its own navigation and follows OneX's theme.
      Embed is on only when this page is framed AND (?embed=1&parent=<origin>, or the parent this tab already saw).
      A page opened directly (not framed) is never in embed mode, even with ?embed=1.
      Only the app page ("/") takes the parent from its address: that address comes from the /sso/onex redirect,
      which carries the parent the server checked. Other framed pages (the /sso/onex error page) only take the theme
      and never store a parent. Where the browser tells us who frames us (location.ancestorOrigins), the parent must
      be that origin.
   2. Otherwise: the member's saved theme (localStorage "rv.theme"), no flash. */
(function () {
  var d = document.documentElement;
  var ORIGIN_RE = /^https?:\/\/[a-z0-9.-]+(:[0-9]{1,5})?$/;
  var framed = true;
  try { framed = window.self !== window.top; } catch (e) { framed = true; }
  var q = null, parent = "";
  try { q = new URLSearchParams(location.search); } catch (e) { q = null; }
  var onApp = location.pathname === "/";
  /* The real parent origin when the browser says it ("null" when hidden by a referrer policy: then unknown). */
  function ancestorOk(p) {
    try {
      var a = location.ancestorOrigins;
      if (!a || !a.length) return true;
      var real = a[0];
      return !ORIGIN_RE.test(real) || real === p;
    } catch (e) { return true; }
  }
  function themeFrom() {
    var t = q ? q.get("theme") : null;
    if (t !== "light" && t !== "dark") { try { t = sessionStorage.getItem("rv.embed.theme"); } catch (e) { t = null; } }
    if (t === "light" || t === "dark") d.setAttribute("data-theme", t);
  }
  if (framed && !onApp) {
    themeFrom();
    return; /* framed but not the app: never read or write "rv.theme", never store a parent */
  }
  if (framed) {
    var p = q ? q.get("parent") : null;
    if (q && q.get("embed") === "1" && p && ORIGIN_RE.test(p) && ancestorOk(p)) parent = p;
    else {
      try {
        var saved = sessionStorage.getItem("rv.embed");
        if (saved && ORIGIN_RE.test(saved) && ancestorOk(saved)) parent = saved;
        else if (saved) sessionStorage.removeItem("rv.embed");
      } catch (e) { parent = ""; }
    }
  }
  if (parent) {
    d.setAttribute("data-embed", "1");
    d.setAttribute("data-parent", parent);
    try { sessionStorage.setItem("rv.embed", parent); } catch (e) { /* storage blocked: this load still works */ }
    themeFrom();
    return; /* never read or write localStorage "rv.theme" inside OneX */
  }
  try {
    var s = localStorage.getItem("rv.theme");
    if (s === "light" || s === "dark") d.setAttribute("data-theme", s);
  } catch (e) { /* storage blocked: follow the system */ }
})();

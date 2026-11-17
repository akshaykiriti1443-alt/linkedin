/* The /sso/onex error page inside the OneX page (D86): tells OneX what went wrong so it shows its own message.
   Plain script, no secrets: only the error code and ReelVault's plain sentence. */
(function () {
  var framed = true;
  try { framed = window.self !== window.top; } catch (e) { framed = true; }
  if (!framed || !document.body) return;
  var b = document.body;
  var parent = b.getAttribute("data-rv-parent") || "";
  if (!/^https?:\/\/[a-z0-9.-]+(:[0-9]{1,5})?$/.test(parent)) return;
  var code = b.getAttribute("data-rv-error") || "";
  if (!/^[a-z_]{1,32}$/.test(code)) return;
  var message = (b.getAttribute("data-rv-message") || "").slice(0, 300);
  try { window.parent.postMessage({type: "rv:error", code: code, message: message}, parent); } catch (e) { /* parent gone */ }
})();

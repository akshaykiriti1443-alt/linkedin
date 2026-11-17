/* Embed mode: ReelVault inside the OneX page (DESIGN-SAAS.md D86, INTEGRATE-ONEX.md).
   theme.js decides it before paint and marks <html data-embed="1" data-parent="<OneX origin>">; this module only
   reads those two attributes. Messages go to the OneX origin only (never "*") and carry no secrets.
     up   (frame -> OneX): rv:ready, rv:route, rv:height, rv:toast, rv:signed-out  (rv:error: embed-error.js)
     down (OneX -> frame): onex:theme, onex:navigate */
const d = document.documentElement;
const ORIGIN_RE = /^https?:\/\/[a-z0-9.-]+(:[0-9]{1,5})?$/;
const parentAttr = d.getAttribute("data-parent") || "";

export const EMBED = d.getAttribute("data-embed") === "1" && ORIGIN_RE.test(parentAttr) && window.parent !== window;
export const PARENT = EMBED ? parentAttr : "";

/** The hash routes OneX may open. Identical to ONEX_ROUTE_RE in server/api_auth.py and OneX's reelvaultRoutes.ts. */
export const ROUTE_RE = /^(home|onboarding|competitors|competitor|discover|reels|reel|working|format|banks|create|outputs|output|settings|help|privacy)(\/[A-Za-z0-9_-]{1,64}){0,2}(\?[A-Za-z0-9_.~%+=&-]{1,200})?$/;
export const isRoute = r => typeof r === "string" && r.length > 0 && r.length <= 300 && ROUTE_RE.test(r);

/** Posts {type, ...fields} to the OneX page. Does nothing outside embed mode. */
export function post(type, fields) {
  if (!EMBED) return;
  try { window.parent.postMessage(Object.assign({}, fields || {}, {type}), PARENT); } catch (e) { /* parent gone */ }
}

let lastHeight = -1;
let frame = 0;
function measure() {
  frame = 0;
  const hgt = Math.min(100000, Math.max(0, Math.ceil(document.body.getBoundingClientRect().height)));
  if (Math.abs(hgt - lastHeight) < 1) return;
  lastHeight = hgt;
  post("rv:height", {height: hgt});
}
/** Sends rv:height (at most once per animation frame). force: send even when the height did not change. */
export function sendHeight(force = false) {
  if (!EMBED || !document.body) return;
  if (force) lastHeight = -1;
  if (!frame) frame = requestAnimationFrame(measure);
}

const isPlain = m => !!m && typeof m === "object" && !Array.isArray(m) &&
  (Object.getPrototypeOf(m) === Object.prototype || Object.getPrototypeOf(m) === null);

let started = false;
/** Starts the height observer and the listener for OneX's messages. onRoute(route) runs after onex:navigate. */
export function start({onRoute} = {}) {
  if (!EMBED || started) return;
  started = true;
  if (window.ResizeObserver && document.body) new ResizeObserver(() => sendHeight()).observe(document.body);
  window.addEventListener("message", e => {
    if (e.source !== window.parent || e.origin !== PARENT) return;
    const m = e.data;
    if (!isPlain(m) || typeof m.type !== "string" || !m.type.startsWith("onex:")) return;
    if (m.type === "onex:theme") {
      if (m.theme !== "light" && m.theme !== "dark") return;
      d.setAttribute("data-theme", m.theme);
      try { sessionStorage.setItem("rv.embed.theme", m.theme); } catch (err) { /* fine */ }
    } else if (m.type === "onex:navigate") {
      if (!isRoute(m.route)) return;
      if (location.hash.replace(/^#/, "") !== m.route) location.replace("#" + m.route);
      if (onRoute) onRoute(m.route);
    }
  });
}

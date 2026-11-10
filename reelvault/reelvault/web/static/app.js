/* ReelVault Members web app (DESIGN-SAAS.md section 9 and WP7). Plain JS modules, no build step, no CDN.
   Hash routes (#home, #reel/<id>, ...). Every page is built with h(), which only ever sets text, never HTML,
   so competitor captions and other third-party text can't inject markup. */

// ============================================================================================ DOM helpers
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    kids.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "selected" || k === "disabled" || k === "hidden" || k === "required" ||
             k === "multiple" || k === "readOnly") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids) {
    if (k === null || k === undefined || k === false || k === "") continue;
    if (Array.isArray(k)) add(el, k);
    else el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
// Fix round 1: views hand `cond ? h(...) : null` (and empty bullet lists) to el.append(), and the native append
// printed the word "null" on the page. Every append in this app skips null, undefined and false and flattens
// arrays, like h() and clear() always did. (FormData.append is not touched.)
for (const P of [Element.prototype, DocumentFragment.prototype]) {
  const native = P.append;
  P.append = function (...kids) {
    return native.apply(this, kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false));
  };
}
export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const arr = x => (Array.isArray(x) ? x : []);
export const nice = s => { s = String(s ?? "").replace(/_/g, " ").trim(); return s ? s[0].toUpperCase() + s.slice(1) : ""; };
export function clear(el, ...kids) { el.replaceChildren(); add(el, kids); return el; }

/** "Click **Add**" -> text with <b> parts. No other markup is ever made. */
export function md(text) {
  const out = document.createDocumentFragment();
  String(text ?? "").split(/(\*\*[^*]+\*\*)/g).forEach(part => {
    if (/^\*\*[^*]+\*\*$/.test(part)) out.append(h("b", part.slice(2, -2)));
    else if (part) out.append(document.createTextNode(part));
  });
  return out;
}

// ============================================================================================ formatting
export function num(n) {
  if (n === null || n === undefined || n === "") return "—";
  const v = Number(n);
  if (!isFinite(v)) return "—";
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (Math.abs(v) >= 1e4) return Math.round(v / 1e3) + "K";
  return v.toLocaleString("en");
}
export const full = n => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en"));
export const x = s => (s === null || s === undefined ? "—" : `${Number(s).toFixed(1).replace(/\.0$/, "")}x`);
export const mmss = t => { t = Math.max(0, Math.round(Number(t) || 0)); return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0"); };
export function day(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? "—" : d.toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric"});
}
export function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(s)) return "—";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
export function vfClass(id) {
  const n = parseInt(String(id || "").replace(/\D/g, ""), 10);
  return "vf h" + (isNaN(n) ? 0 : n % 8);
}
export const PURPOSES = [["remake", "Remake it better"], ["format", "Use the format"], ["hook", "Borrow the hook"], ["learn", "Learn it"]];
export const PURPOSE_LABEL = Object.fromEntries(PURPOSES);
export const KINDS = [["reel_script", "Reel scripts"], ["meta_ads", "Meta ads"], ["content_plan", "30-day plan"],
  ["competitor_report", "Competitor report"]];
export const KIND_LABEL = Object.fromEntries(KINDS);
export const SAVES_LINE = "Instagram hides saves, and usually shares, for accounts you don't own. We rank on views, likes and comments.";

// ============================================================================================ state + API
export const S = {me: null, copy: null, jobs: [], ext: null, view: null, vfLabels: {}};

function csrf() { return S.me && S.me.csrf_token; }

/** Calls the JSON API. Never throws: returns {ok, status, data, error, code}. */
export async function api(method, url, body, {quiet = false} = {}) {
  const opts = {method, headers: {Accept: "application/json"}, credentials: "same-origin"};
  if (method !== "GET" && method !== "HEAD") {
    if (csrf()) opts.headers["X-CSRF-Token"] = csrf();
    if (body instanceof FormData) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body || {}); }
  }
  let res;
  try { res = await fetch(url, opts); } catch (e) {
    return {ok: false, status: 0, data: null, error: "We can't reach ReelVault right now. Check your internet and try again."};
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  const out = {ok: res.ok, status: res.status, data, error: null, code: data && data.code};
  if (!res.ok) {
    out.error = (data && data.error) || `Something went wrong (code ${res.status}). Try again.`;
    if (res.status === 401 && !quiet && !/^\/api\/auth\//.test(url) && S.me) {
      S.me = null;
      toast("You were signed out. Log in again.");
      go("#login");
    }
    if (res.status === 429 && res.headers.get("Retry-After") && !out.error) out.error = "Too many tries. Wait a minute and try again.";
  }
  return out;
}
export const get = (u, o) => api("GET", u, null, o);
export const post = (u, b) => api("POST", u, b);
export const put = (u, b) => api("PUT", u, b);
export const patch = (u, b) => api("PATCH", u, b);
export const del = (u, b) => api("DELETE", u, b);
export const qs = obj => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj || {})) if (v !== null && v !== undefined && v !== "" && v !== false) p.set(k, v === true ? "1" : v);
  const s = p.toString();
  return s ? "?" + s : "";
};

export async function refreshMe() {
  const r = await get("/api/me", {quiet: true});
  if (r.ok) S.me = r.data;
  else if (r.status === 401) S.me = null;
  else if (r.status !== 0) toast(r.error);
  return S.me;
}
export async function loadCopy() {
  if (S.copy) return S.copy;
  const r = await get("/api/copy");
  S.copy = r.ok ? (r.data.copy || r.data) : {};
  return S.copy;
}

// ============================================================================================ small UI parts
let toastTimer = 0;
export function toast(text) {
  const t = $("#toast");
  if (!t) return;
  t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}
export function msg() { return h("p", {class: "msg", role: "status", "aria-live": "polite"}); }
export function say(el, r, good) {
  if (!el) return;
  const err = r && (r.error || (r.ok === false && "Something went wrong."));
  el.className = "msg " + (err ? "err" : "good");
  el.textContent = err || good || "";
}
/** Disables the button while fn runs. */
export async function busy(btn, fn) {
  if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
  try { return await fn(); } finally { if (btn && btn.isConnected) { btn.disabled = false; btn.removeAttribute("aria-busy"); } }
}
export function copyText(text, btn) {
  const label = btn ? btn.dataset.label || btn.textContent : "";
  if (btn) btn.dataset.label = label;
  const done = ok => {
    if (!btn) return;
    btn.textContent = ok ? "Copied" : "Select and copy";
    btn.classList.toggle("done", ok);
    setTimeout(() => { btn.textContent = label; btn.classList.remove("done"); }, 1600);
  };
  const fallback = () => {
    const ta = h("textarea", {readonly: true, style: {position: "fixed", opacity: "0", left: "0", top: "0"}});
    ta.value = text; document.body.append(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove(); done(ok);
  };
  try {
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(() => done(true), fallback);
    else fallback();
  } catch (e) { fallback(); }
}
export const copyBtn = (text, label = "Copy") => h("button", {type: "button", class: "copy", onclick: e => copyText(text, e.currentTarget)}, label);
export const copyLine = (text, label = "Copy") => h("div", {class: "copyline"}, h("code", text), copyBtn(text, label));
export function empty(text, ...actions) { return h("div", {class: "empty"}, h("p", md(text)), actions.length ? h("div", {class: "row"}, actions) : null); }
export const savesLine = text => h("p", {class: "saves"}, text || SAVES_LINE);
export const linkBtn = (href, text, cls = "btn") => h("a", {class: cls, href}, text);
export function bandChip(r) {
  if (!r || !r.band || r.band === "unscored") return null;
  return h("span", {class: "bandchip b-" + r.band, title: r.why || ""}, r.band_label || nice(r.band));
}
export function vfTag(id, label) { return id ? h("span", {class: vfClass(id)}, label || S.vfLabels[id] || id) : null; }
/** Labels for visual-format ids (from /api/taxonomy), loaded once after sign-in. */
async function loadLabels() {
  if (Object.keys(S.vfLabels).length) return;
  const r = await get("/api/taxonomy", {quiet: true});
  if (r.ok) for (const v of arr(r.data.visual_format)) S.vfLabels[v.id] = v.label;
}
export function pill(text, kind) { return h("span", {class: "pill" + (kind ? " " + kind : "")}, text); }
export function field(label, input, hint) {
  return h("label", {class: "field"}, h("span", {class: "lab"}, label), input, hint ? h("span", {class: "hint"}, hint) : null);
}
export function select(options, value, attrs = {}) {
  return h("select", attrs, options.map(([v, t]) => h("option", {value: v, selected: String(v) === String(value ?? "")}, t)));
}
export function checkbox(label, checked, attrs = {}) {
  const i = h("input", Object.assign({type: "checkbox", checked}, attrs));
  return [h("label", {class: "check"}, i, label), i];
}
export function confirmText(text) { return window.confirm(text); }
export function tabs(items, current, onPick) {
  return h("div", {class: "tabs", role: "tablist"}, items.map(([id, label, href]) => href
    ? h("a", {href, role: "tab", "aria-selected": String(id === current)}, label)
    : h("button", {type: "button", role: "tab", "aria-selected": String(id === current), onclick: () => onPick(id)}, label)));
}
export function backLink(href, text) { return h("a", {class: "back", href}, "← " + text); }
export function thumbStyle(url) { return url ? {backgroundImage: `url("${String(url).replace(/["\\\n]/g, "")}")`} : {}; }

/** One reel card (ReelSummary, 4.1). */
export function reelCard(r, {actions = true} = {}) {
  const t = r.tags || {};
  return h("article", {class: "rcard"},
    h("a", {href: "#reel/" + r.id},
      h("div", {class: "cover", style: thumbStyle(r.thumb)},
        r.thumb ? null : h("span", {class: "noimg"}, "No cover yet"),
        h("span", {class: "band"}, bandChip(r)),
        r.duration ? h("span", {class: "dur"}, mmss(r.duration)) : null),
      h("div", {class: "body"},
        h("div", {class: "row"}, h("span", {class: "score"}, x(r.score)), h("span", {class: "sub"}, "normal views")),
        h("div", {class: "title"}, r.caption_first_line || "(no caption)"),
        h("div", {class: "sub"}, `@${(r.account || {}).handle || "?"} · ${num(r.views)} views · ${num(r.likes)} likes`),
        t.visual_format ? h("div", null, vfTag(t.visual_format, t.format ? t.format.name : t.visual_format_label)) :
          (r.states && r.states.tags !== "done" ? h("div", {class: "sub"}, "Tags coming") : null))),
    actions ? h("div", {class: "acts"}, buildOn({reel: r})) : null);
}

/** Remake this · Use this format · Borrow this hook (8.0). refs: {reel} | {reel_id} | {format_id} | {vf} */
export function buildOn(refs, {only = null, size = "sm"} = {}) {
  const reel = refs.reel;
  const reelId = reel ? reel.id : refs.reel_id;
  const allowed = only || (reel && Array.isArray(reel.actions) ? reel.actions : ["remake", "use_format", "borrow_hook"]);
  const btn = (action, text, cls, body) => allowed.includes(action) ? h("button", {
    type: "button", class: cls + " " + size, title: text,
    onclick: e => quick(e.currentTarget, Object.assign({action}, body)),
  }, text) : null;
  const fmtBody = refs.format_id ? {format_id: refs.format_id} : refs.vf ? {vf: refs.vf} : {reel_id: reelId};
  return h("div", {class: "buildon"},
    reelId ? btn("remake", "Remake this", "b-remake", {reel_id: reelId}) : null,
    (reelId || refs.format_id || refs.vf) ? btn("use_format", "Use this format", "b-format", fmtBody) : null,
    reelId ? btn("borrow_hook", "Borrow this hook", "b-hook", {reel_id: reelId}) : null);
}
export async function quick(btn, body) {
  await busy(btn, async () => {
    const r = await post("/api/outputs/quick", body);
    if (!r.ok) { toast(r.error); if (r.status === 400 && /profile/i.test(r.error)) go("#settings/profile"); return; }
    if (r.data.claude_prompt) sessionStore("rv.claude_prompt." + r.data.output.id, r.data.claude_prompt);
    toast("Started. We'll show it here when it's ready.");
    go("#output/" + r.data.output.id);
  });
}
export function sessionStore(k, v) {
  try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch (e) { return null; }
  return null;
}
export function localStore(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { return null; }
  return null;
}

/** One line for a Job (4.1), with Cancel / Try again. */
export function jobLine(j, {onChange} = {}) {
  const p = j.progress || {};
  const pct = p.total ? Math.min(100, Math.round(100 * (p.done || 0) / p.total)) : null;
  const active = ["queued", "running", "waiting", "paused"].includes(j.state);
  const text = j.error_message || p.message || JOB_TEXT[j.kind] || nice(j.kind);
  const box = h("div", {class: "stack tight", dataset: {job: j.id}},
    h("div", {class: "row"},
      j.state === "running" ? h("span", {class: "spinner", "aria-hidden": "true"}) : null,
      h("span", {class: "bold"}, JOB_TEXT[j.kind] || nice(j.kind)),
      pill(STATE_TEXT[j.state] || j.state, j.state === "failed" ? "bad" : j.state === "paused" ? "warn" : j.state === "done" ? "ok" : "busy"),
      h("span", {class: "spacer"}),
      (j.state === "failed" || j.state === "paused") ? h("button", {type: "button", class: "ghost sm", onclick: async e => {
        const r = await busy(e.currentTarget, () => post(`/api/jobs/${j.id}/retry`, {}));
        toast(r.ok ? "Trying again." : r.error); if (onChange) onChange(); pollSoon();
      }}, "Try again") : null,
      active && !INTERNAL_KINDS.has(j.kind) ? h("button", {type: "button", class: "ghost sm", onclick: async e => {
        const r = await busy(e.currentTarget, () => post(`/api/jobs/${j.id}/cancel`, {}));
        toast(r.ok ? "Stopped." : r.error); if (onChange) onChange(); pollSoon();
      }}, "Stop") : null),
    text && text !== (JOB_TEXT[j.kind] || "") && !(j.eta && j.eta.text && j.eta.text.startsWith(text.replace(/[.\s]+$/, "")))
      ? h("p", {class: "hint"}, text) : null,
    pct !== null && active ? h("div", {class: "progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100}, h("i", {style: {width: pct + "%"}})) : null,
    j.eta && j.eta.text ? h("p", {class: "hint"}, j.eta.text) : null,
    j.run_after && j.state === "queued" ? h("p", {class: "hint"}, startsText(j.run_after)) : null);
  return box;
}
// Housekeeping jobs ReelVault starts by itself: no Stop button (fix round 2).
const INTERNAL_KINDS = new Set(["refresh_insights", "score_account", "pick_sample"]);
/** "Starts in a minute" / "Starts at 14:05" today, the date only on another day (fix round 2). */
export function startsText(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return "";
  const mins = (t - Date.now()) / 60000;
  if (mins <= 1.5) return "Starts in a minute";
  if (mins < 60) return `Starts in ${Math.round(mins)} minutes`;
  if (t.toDateString() === new Date().toDateString()) return "Starts at " + t.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  return "Starts " + day(iso);
}
export const JOB_TEXT = {
  ig_pull: "Reading reels", ig_profile: "Reading a profile", ig_videos: "Getting videos", ig_discover: "Finding accounts",
  ig_check: "Checking accounts", page_read: "Reading your page", fetch_video: "Getting a video", process_reel: "Measuring a reel",
  score_account: "Finding outliers", pick_sample: "Picking reels to study", refresh_insights: "Updating what works",
  tag_reels: "Tagging reels", retag_topics: "Updating topics", analyze_reel: "Breaking down a reel", name_formats: "Naming formats",
  make_output: "Writing", import_source: "Reading your link", draft_profile: "Drafting your profile", suggest_topics: "Suggesting topics",
};
export const STATE_TEXT = {queued: "Queued", running: "Working", waiting: "Waiting", paused: "Needs you", done: "Done", failed: "Failed", canceled: "Stopped"};

// ============================================================================================ job polling (4.11)
let pollTimer = 0;
let lastActive = new Set();
export function pollSoon(ms = 300) { clearTimeout(pollTimer); pollTimer = setTimeout(pollJobs, ms); }
async function pollJobs() {
  clearTimeout(pollTimer);
  if (!S.me || S.me.status !== "active" || document.hidden) { pollTimer = setTimeout(pollJobs, 30000); return; }
  const r = await get("/api/jobs?active=1", {quiet: true});
  if (r.ok) {
    S.jobs = arr(r.data.jobs);
    const now = new Set(S.jobs.map(j => j.id));
    const finished = [...lastActive].filter(id => !now.has(id));
    lastActive = now;
    renderNavJobs();
    runHooks({jobs: S.jobs, finished});
  }
  const running = S.jobs.some(j => j.state === "queued" || j.state === "running");
  pollTimer = setTimeout(pollJobs, running ? 3000 : (S.jobs.length ? 10000 : 30000));
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollSoon(100); });

/** Page hooks: one of each per page, replaced on re-render, dropped when the page changes. */
const hooks = {token: null, jobs: null, done: null};
/** fn({jobs, finished}) after every poll. */
export function onJobs(fn) { hooks.token = S.view; hooks.jobs = fn; }
/** fn({jobs, finished}) when any job finishes. */
export function onJobsDone(fn) { hooks.token = S.view; hooks.done = fn; }
function runHooks(detail) {
  if (hooks.token !== S.view) { hooks.jobs = hooks.done = null; return; }
  try { if (hooks.jobs) hooks.jobs(detail); if (hooks.done && detail.finished.length) hooks.done(detail); } catch (e) { console.warn(e); }
}
/** Calls fn(stop) every ms while the same page is shown. Call stop() before re-drawing the page. */
export function every(ms, fn) {
  const token = S.view;
  let id = 0;
  const stop = () => clearInterval(id);
  id = setInterval(() => { if (S.view !== token) { stop(); return; } if (!document.hidden) fn(stop); }, ms);
  return stop;
}

// ============================================================================================ routing
const ROUTES = {
  join: ["auth", "join"], login: ["auth", "login"], reset: ["auth", "reset"], verify: ["auth", "verify"], privacy: ["help", "privacy"], help: ["help", "help"],
  home: ["home", "home"], onboarding: ["onboarding", "onboarding"],
  competitors: ["competitors", "list"], competitor: ["competitors", "detail"], discover: ["competitors", "discover"],
  reels: ["reels", "list"], reel: ["reels", "detail"],
  working: ["working", "working"], format: ["working", "format"], banks: ["working", "banks"],
  create: ["outputs", "create"], outputs: ["outputs", "list"], output: ["outputs", "detail"],
  settings: ["settings", "settings"], admin: ["admin", "admin"],
};
const PUBLIC = new Set(["join", "login", "reset", "verify", "privacy", "help"]);

export function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
export function parseHash() {
  const raw = (location.hash || "").replace(/^#/, "");
  const [path, q] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return {name: parts[0] || "", parts: parts.slice(1), query: new URLSearchParams(q || "")};
}

function markReady() { document.documentElement.dataset.view = "ready"; document.documentElement.dataset.ready = location.hash; }
async function route() {
  const r = parseHash();
  const token = Symbol(r.name);
  S.view = token;
  document.documentElement.dataset.view = "loading";
  if (!r.name) { go(S.me ? "#home" : "#login"); return; }
  if (S.me === null && !PUBLIC.has(r.name)) await refreshMe();
  if (S.view !== token) return;
  const me = S.me;
  if (!PUBLIC.has(r.name) && !me) { go("#login" + (r.name ? "?back=" + encodeURIComponent(location.hash) : "")); return; }
  if (me && me.reconfirm_needed && !PUBLIC.has(r.name)) { shell(null, true); const m = await import("./views/auth.js"); m.reconfirm(mainEl()); markReady(); return; }
  if ((r.name === "login" || r.name === "join") && me && !r.query.get("next")) { go("#home"); return; }
  if (r.name === "admin" && me && me.role !== "admin") { go("#home"); return; }
  const target = ROUTES[r.name];
  if (!target) { shell(r.name); clear(mainEl(), h("h1", "Page not found"), empty("We can't find that page.", linkBtn("#home", "Go home"))); markReady(); return; }
  shell(r.name, PUBLIC.has(r.name) && !me);
  if (me && me.status === "active") await loadLabels();
  const main = mainEl();
  try {
    const mod = await import(`./views/${target[0]}.js`);
    if (S.view !== token) return;
    await mod[target[1]](main, {parts: r.parts, query: r.query, token});
  } catch (e) {
    console.warn(e);
    if (S.view === token) clear(main, empty("This page didn't load. Reload the page to try again.", h("button", {type: "button", onclick: () => location.reload()}, "Reload")));
  }
  if (S.view === token) {
    document.documentElement.dataset.view = "ready";
    document.documentElement.dataset.ready = location.hash;
    const h1 = $("h1", main);
    document.title = (h1 ? h1.textContent + " · " : "") + "ReelVault Members";
  }
}
window.addEventListener("hashchange", () => { closeMenu(); route(); });

// ============================================================================================ shell (nav + main)
const ICONS = {
  home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  competitors: "M8 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21v-1a6 6 0 0 1 12 0v1M16 3.5a4 4 0 0 1 0 7.5M22 21v-1a6 6 0 0 0-4-5.6",
  reels: "M4 3h16v18H4zM4 8h16M9 3l2 5M15 3l2 5M10 12l5 3-5 3z",
  working: "M3 21h18M6 17V10M11 17V5M16 17v-9M21 17v-4",
  create: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  outputs: "M6 2h9l5 5v15H6zM14 2v6h6M9 13h8M9 17h8",
  settings: "M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4zM12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M5.5 18.5l1.8-1.8M16.7 7.3l1.8-1.8",
  admin: "M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z",
};
const NAV = [["home", "Home"], ["competitors", "Competitors"], ["reels", "Reels"], ["working", "What works"],
  ["create", "Create"], ["outputs", "Outputs"], ["settings", "Settings"]];
const NAV_OF = {competitor: "competitors", discover: "competitors", reel: "reels", format: "working", banks: "working",
  output: "outputs", onboarding: "home"};

function icon(d) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", d); svg.append(p);
  return svg;
}
function logo() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  for (const [k, v] of Object.entries({x: 3, y: 2.5, width: 18, height: 19, rx: 4})) r.setAttribute(k, v);
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", "M10 8.5v7l6-3.5z"); p.setAttribute("class", "fill");
  svg.append(r, p);
  return h("span", {class: "logo", "aria-hidden": "true"}, svg);
}
export function mainEl() { return $("#main"); }
function closeMenu() { const n = $(".nav"); if (n) { n.classList.remove("open"); const b = $(".menu-btn"); if (b) b.setAttribute("aria-expanded", "false"); } }

let shellKind = "";
function shell(name, plain = false) {
  const root = $("#root");
  if (plain || !S.me) {
    if (shellKind !== "plain") { clear(root, h("main", {id: "main", class: "plain"})); shellKind = "plain"; }
    return;
  }
  const navName = NAV_OF[name] || name;
  if (shellKind !== "app") {
    const nav = h("nav", {class: "nav", "aria-label": "Main"},
      h("div", {class: "navtop"},
        h("a", {class: "brand", href: "#home"}, logo(), h("span", {class: "brand-name"}, "ReelVault")),
        h("button", {type: "button", class: "ghost sm menu-btn", "aria-expanded": "false", "aria-controls": "navlinks",
          onclick: e => { const n = $(".nav"); n.classList.toggle("open"); e.currentTarget.setAttribute("aria-expanded", String(n.classList.contains("open"))); }}, "Menu")),
      h("div", {class: "navlinks", id: "navlinks"}),
      h("div", {class: "navfoot"},
        h("a", {class: "navjob", id: "navjob", href: "#home", hidden: true}),
        h("span", {id: "navme"}),
        h("div", {class: "row"},
          h("a", {href: "#help"}, "Help"), h("a", {href: "#privacy"}, "Privacy"),
          h("button", {type: "button", class: "link", onclick: logout}, "Log out"))));
    clear(root, h("div", {class: "app"}, nav, h("main", {id: "main", class: "main"})));
    shellKind = "app";
    pollSoon(50);
  }
  const links = $("#navlinks");
  const me = S.me;
  const pending = me.admin ? me.admin.pending : 0;
  clear(links, NAV.map(([id, label]) => h("a", {href: "#" + id, "aria-current": navName === id ? "page" : null}, icon(ICONS[id]), h("span", label))),
    me.role === "admin" ? [h("div", {class: "sep"}), h("a", {href: "#admin/members", "aria-current": navName === "admin" ? "page" : null},
      icon(ICONS.admin), h("span", "Admin"), pending ? h("span", {class: "badge", title: `${pending} waiting for approval`}, String(pending)) : null)] : null);
  $("#navme").textContent = me.email;
  renderNavJobs();
}
function renderNavJobs() {
  const el = $("#navjob");
  if (!el) return;
  const n = S.jobs.filter(j => j.state === "running" || j.state === "queued").length;
  el.hidden = !n;
  clear(el, h("span", {class: "jobdot"}), n === 1 ? "Working on 1 job" : `Working on ${n} jobs`);
}
async function logout() {
  await post("/api/auth/logout", {});
  S.me = null; S.copy = null; S.jobs = []; shellKind = "";
  go("#login");
}

// ============================================================================================ start
route();

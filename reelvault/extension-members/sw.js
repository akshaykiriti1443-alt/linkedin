// ReelVault Members Reader: service worker (DESIGN-SAAS.md 5.2-5.7).
// Loop: every 30 s (alarm) ask the paired ReelVault server for work (server-only call; idle Chrome makes ZERO
// Instagram calls). One job at a time, one Instagram request at a time, in the extension's own pinned reading tab.
// Fetch engine (DESIGN-FETCH.md): a pull reads LOGGED OUT first (no cookie is sent: reader.js igAnonFetch,
// strategies.js, ladder.js), then the paced logged-in pull fills in what is missing.
import {SERVERS, BUILD} from "./config.js";
import {IG, CDN_DOMAINS, isCdnUrl, WARNING_RE, CHALLENGE_RE, LO_IDS, LI_OF} from "./endpoints.js";
import {parseIg} from "./bigint.js";
import {validateJob, parsePairLink, isNewer, IG_KINDS, KINDS, isPk, isCursor, isLsd, isHandle} from "./validate.js";
import {DEFAULTS, merge, Budget, BudgetStop} from "./pacing.js";
import {trimMedia, trimUser, isReel, keepForTarget, videoOf, coverOf, keysTree} from "./trim.js";
import {igFetch, igAnonFetch, readIds, readSkool, readLinkedIn} from "./reader.js";
import {loRequest, loInterpret, cleanDocIds, probeFields, pkFromCode} from "./strategies.js";
import {runLoStage, LoFail, HARD_BLOCKS} from "./ladder.js";

const VERSION = chrome.runtime.getManifest().version;
const local = chrome.storage.local;
const session = chrome.storage.session;
const SIX_H = 6 * 3600_000;
const VIDEO_MAX = 50 * 1024 * 1024;
const COVER_MAX = 2 * 1024 * 1024;
const PAGE_CAP = 12;
const DEBUG_ENDPOINTS = ["clipsUser", "profileInfo", "currentUser", "chaining"];
// 5.7 cooldowns, used locally only when the stop could not be reported (the server's answer wins otherwise).
const LOCAL_COOLDOWN_S = {rate_limited: 24 * 3600, warning: 24 * 3600, challenge: 24 * 3600, signed_out: 30 * 60,
                          http_error: 15 * 60};
const SERVER_BACKOFF_S = [60, 300, 900];   // waits before a stored job is resumed after ReelVault failed it
// Logged-out blocks (DESIGN-FETCH.md table 2.3), applied here at once. The server computes the same and its later
// time wins. Hours: [first, repeat]. "rate" doubles from 6 h up to 48 h.
const HOUR_MS = 3600_000;
const LO_ALL_OFF_H = {rate: [6, 48], challenge: [24, 48], walls: [2, 6]};
const LO_ONE_OFF_H = {login_wall: [6, 24], changed: [24, 24], empty: [6, 6]};
const LI_WAIT_MIN = {rate: 15, challenge: 60};   // no logged-in call this soon after a logged-out block
const NO_VIEWS_OFF_H = 24;
const PROBE_CALLS_MAX = 12;
const FETCH_LOG_MAX = 60;

// ============================================================================================ small helpers

const get = async k => (await local.get(k))[k];
const put = obj => local.set(obj);
const sid = v => (v === null || v === undefined || v === "") ? null : String(v);

async function sleep(ms) {                         // chopped into <= 10 s pieces (MV3 keep-alive, 5.3)
  const end = Date.now() + ms;
  while (Date.now() < end) await new Promise(r => setTimeout(r, Math.min(10_000, end - Date.now())));
}

function badge(text) {
  chrome.action.setBadgeText({text}).catch(() => {});
  if (text) chrome.action.setBadgeBackgroundColor({color: text === "!" ? "#b91c1c" : "#2563eb"}).catch(() => {});
}

class IgStop extends Error {                       // a 5.7 stop: reported to the server with POST /fail
  constructor(code, extra = {}) { super(code); this.code = code; Object.assign(this, extra); }
}
class Dropped extends Error {}                     // the server took the job back (409): drop local state
class Canceled extends Error {}                    // progress said cancel
class ServerDown extends Error {}                  // network error to ReelVault: keep the job, try later
class Unpaired extends Error {}                    // no token, or the token was revoked (401)
class TabError extends Error {}

let effPacing = DEFAULTS;
let fetchCfg = null;                 // hello.fetch of a server that knows the fetch engine, else null
let docIds = cleanDocIds(null);      // GraphQL doc ids: the server's (hello.fetch.doc_ids) or the built-in ones
async function loadPacing() {
  effPacing = merge(DEFAULTS, await get("server_pacing"), BUILD);
  const h = await get("hello");
  fetchCfg = h && h.fetch && typeof h.fetch === "object" ? h.fetch : null;
  docIds = cleanDocIds(fetchCfg && fetchCfg.doc_ids);
  loMem = await loState();
}

const budget = new Budget({get: () => get("budget"), set: v => put({budget: v})},
                          {sleep, limits: () => effPacing});

async function take(kind, opts) {
  try { await budget.take(kind, opts); }
  catch (e) {
    if (e instanceof BudgetStop) throw new IgStop("budget", {cooldown_s: Math.ceil(e.waitMs / 1000), message: e.what});
    throw e;
  }
}

// ============================================================================================ logged-out state

// chrome.storage.local.lo (DESIGN-FETCH.md 3.3). Times are ms. A logged-out block lives here only: it never sets
// the logged-in cooldown and never changes the sign-in state.
const LO_FRESH = {cooldown_until: 0, cooldown_reason: null, blocks: 0, strategy_off: {}, li_not_before: 0,
                  detail_off_until: 0, repeats: {}, empties: {}, doc_ok: {}};
let loMem = {...LO_FRESH};
async function loState() {
  const v = (await get("lo")) || {};
  return {...LO_FRESH, ...v, strategy_off: {...(v.strategy_off || {})}, repeats: {...(v.repeats || {})},
          empties: {...(v.empties || {})}, doc_ok: {...(v.doc_ok || {})}};
}
async function saveLo(lo) { loMem = lo; await put({lo}); }
const toMs = v => typeof v === "number" && Number.isFinite(v) ? v : (typeof v === "string" ? (Date.parse(v) || 0) : 0);
const toIso = v => (v && v > Date.now()) ? new Date(v).toISOString() : null;

// What the server says about this device's logged-out state (hello, lease, items, fail). The later time wins.
async function mergeServerLo(v) {
  if (!v || typeof v !== "object") return;
  const lo = await loState();
  const cu = toMs(v.cooldown_until ?? v.lo_cooldown_until);
  if (cu > lo.cooldown_until) {
    lo.cooldown_until = cu;
    lo.cooldown_reason = String(v.cooldown_reason ?? v.lo_cooldown_reason ?? lo.cooldown_reason ?? "rate").slice(0, 40);
  }
  if (v.strategy_off && typeof v.strategy_off === "object") {
    for (const [k, t] of Object.entries(v.strategy_off)) {
      if (LO_IDS.includes(k)) lo.strategy_off[k] = Math.max(lo.strategy_off[k] || 0, toMs(t));
    }
  }
  const nb = toMs(v.li_not_before);
  if (nb > lo.li_not_before) lo.li_not_before = nb;
  await saveLo(lo);
}

function loReport(lo = loMem) {      // the "lo" part of the lease call
  const off = {};
  for (const [k, t] of Object.entries(lo.strategy_off)) if (t > Date.now()) off[k] = new Date(t).toISOString();
  const cooling = lo.cooldown_until > Date.now();
  return {cooldown_until: toIso(lo.cooldown_until), ...(cooling ? {cooldown_reason: lo.cooldown_reason || "rate"} : {}), strategy_off: off};
}

// Table 2.3, applied at once after one logged-out answer.
async function mirrorLo(strategy, res) {
  const lo = await loState(), now = Date.now();
  let dirty = false;
  const offOne = ([first, repeat]) => {
    const n = lo.repeats[strategy] = (lo.repeats[strategy] || 0) + 1;
    lo.strategy_off[strategy] = Math.max(lo.strategy_off[strategy] || 0, now + (n > 1 ? repeat : first) * HOUR_MS);
    dirty = true;
  };
  if (res.status === "ok") {                         // "reset after the first ok"
    if (lo.blocks || lo.repeats.challenge || lo.repeats[strategy] || lo.empties[strategy]) dirty = true;
    lo.blocks = 0; delete lo.repeats.challenge; delete lo.repeats[strategy]; delete lo.empties[strategy];
  } else if (res.code === "rate") {
    lo.blocks += 1;
    const h = Math.min(LO_ALL_OFF_H.rate[1], LO_ALL_OFF_H.rate[0] * 2 ** (lo.blocks - 1));
    lo.cooldown_until = Math.max(lo.cooldown_until, now + h * HOUR_MS); lo.cooldown_reason = "rate";
    lo.li_not_before = Math.max(lo.li_not_before, now + LI_WAIT_MIN.rate * 60_000);
    dirty = true;
  } else if (res.code === "challenge") {
    const n = lo.repeats.challenge = (lo.repeats.challenge || 0) + 1;
    lo.cooldown_until = Math.max(lo.cooldown_until, now + LO_ALL_OFF_H.challenge[n > 1 ? 1 : 0] * HOUR_MS);
    lo.cooldown_reason = "challenge";
    lo.li_not_before = Math.max(lo.li_not_before, now + LI_WAIT_MIN.challenge * 60_000);
    dirty = true;
  } else if (LO_ONE_OFF_H[res.code] && res.code !== "empty") {
    offOne(LO_ONE_OFF_H[res.code]);
  } else if (res.code === "empty") {                 // one gated post only skips that reel; 3 in a row switch off
    lo.empties[strategy] = (lo.empties[strategy] || 0) + 1;
    if (lo.empties[strategy] >= 3) { delete lo.empties[strategy]; offOne(LO_ONE_OFF_H.empty); }
    dirty = true;
  }
  if (dirty) await saveLo(lo);
}

// The ladder's wider blocks: a login wall on 2+ strategies in one job, and 3 details in a row without views.
async function loBlock(kind, read) {
  const lo = await loState(), now = Date.now();
  if (kind === "walls") {
    const n = lo.repeats.walls = (lo.repeats.walls || 0) + 1;
    lo.cooldown_until = Math.max(lo.cooldown_until, now + LO_ALL_OFF_H.walls[n > 1 ? 1 : 0] * HOUR_MS);
    lo.cooldown_reason = "login_wall";
  } else if (kind === "no_views" && read === "auto") {
    lo.detail_off_until = Math.max(lo.detail_off_until, now + NO_VIEWS_OFF_H * HOUR_MS);
  } else return;
  await saveLo(lo);
}

// ============================================================================================ fetch log

let curLayer = null;                 // "lo" | "li": which way the running job reads right now (for the popup)
async function setLayer(layer) {     // the popup line "Reading without your login" follows at once
  if (curLayer === layer) return;
  curLayer = layer;
  const u = await get("ui_job");
  if (u && curJob && u.kind === curJob.job.kind) await put({ui_job: {...u, layer}});
}

// Every Instagram call of the running job, for Fetch health: {strategy, status, code, http, ms, items, at}.
function logCall(strategy, status, code, http, ms, items = 0) {
  if (!curJob || !strategy) return;
  const log = (curJob.state.log ??= []);
  log.push({strategy, status, code: code || null, http: Number.isInteger(http) ? http : 0, ms: Math.max(0, Math.round(ms || 0)),
            items, at: Math.floor(Date.now() / 1000)});
  if (log.length > FETCH_LOG_MAX) log.splice(0, log.length - FETCH_LOG_MAX);
}
function lastLogItems(n) {
  const log = curJob && curJob.state.log;
  if (log && log.length) log[log.length - 1].items = n;
}
function pendingLog() {              // only a server that knows the fetch engine gets the log
  const log = fetchCfg && curJob && curJob.state.log;
  return log && log.length ? log.slice(0, FETCH_LOG_MAX) : null;
}

// ============================================================================================ ReelVault server

async function api(path, body, {timeout = 60_000} = {}) {
  const pair = await get("pair");
  if (!pair || !SERVERS.includes(pair.server)) throw new Unpaired();   // only servers baked into this build
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeout);
  let r;
  try {
    r = await fetch(pair.server + "/api/ext/v2" + path, {method: "POST", credentials: "omit", signal: ctl.signal,
      headers: {"Authorization": "Bearer " + pair.token, "Content-Type": "application/json"},
      body: JSON.stringify(body || {})});
  } catch (e) { throw new ServerDown(String(e && e.message || e)); }
  finally { clearTimeout(t); }
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (r.status === 401) { await put({revoked: true}); badge("!"); throw new Unpaired(); }
  return {status: r.status, data};
}

async function apiOk(path, body) {
  const r = await api(path, body);
  if (r.status === 409 || r.status === 404) throw new Dropped();
  if (r.status === 413) return r;
  if (r.status >= 500) throw new ServerDown("server " + r.status);
  if (r.status !== 200) throw new IgStop("parse_error", {message: `ReelVault refused an upload: ${(r.data && r.data.error) || r.status}`});
  return r;
}

let curJob = null;     // {job, state}
let lastBeat = 0;

// An upload of the running job. It carries the fetch log since the last upload.
async function post(path, body) {
  const log = pendingLog();
  const r = await apiOk(path, log ? {...body, fetch_log: log} : body);
  if (log && curJob && curJob.state.log) curJob.state.log.splice(0, log.length);
  return r;
}

async function progress(done, total, message, extra = {}) {
  if (!curJob) return;
  const igs = await get("ig");
  const r = await api(`/jobs/${curJob.job.id}/progress`,
                      {done, total, message, ...(igs ? {ig: igs} : {}), budget: await budget.report(), ...extra});
  if (r.status === 409 || r.status === 404) throw new Dropped();
  if (r.data && r.data.cancel) throw new Canceled(r.data.reason || "");
  lastBeat = Date.now();
  await put({ui_job: {kind: curJob.job.kind, message, done, total, layer: curLayer, at: Date.now()}});
}

async function beatIfDue() {
  if (curJob && Date.now() - lastBeat > 50_000) {
    const p = (await get("ui_job")) || {};
    await progress(p.done || 0, p.total || 0, p.message || "Working");
  }
}

async function waitWithBeat(ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) { await sleep(Math.min(20_000, end - Date.now())); await beatIfDue(); }
}

async function saveState() {
  if (curJob) await put({job: {job: curJob.job, state: curJob.state, saved_at: Date.now()}});
}

// ============================================================================================ reading tab (5.3)

async function waitComplete(tabId, ms = 30_000) {
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { done(); reject(new TabError("timeout")); }, ms);
    function onUpd(id, info) { if (id === tabId && info.status === "complete") { done(); resolve(); } }
    function onRem(id) { if (id === tabId) { done(); reject(new TabError("The tab was closed")); } }
    function done() { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(onUpd); chrome.tabs.onRemoved.removeListener(onRem); }
    chrome.tabs.onUpdated.addListener(onUpd);
    chrome.tabs.onRemoved.addListener(onRem);
    chrome.tabs.get(tabId).then(t => { if (t.status === "complete") { done(); resolve(); } },
                                () => { done(); reject(new TabError("No tab with id")); });
  });
}

async function readingTab() {
  const {readingTabId} = await session.get("readingTabId");
  if (readingTabId) {
    try {
      const t = await chrome.tabs.get(readingTabId);
      if (t.url && t.url.startsWith(IG.origin + "/") && !t.discarded) {
        if (t.status !== "complete") await waitComplete(readingTabId);
        return readingTabId;
      }
      chrome.tabs.remove(readingTabId).catch(() => {});
    } catch { /* gone: make a new one */ }
  }
  const url = IG.origin + IG.readingPage;
  const wins = await chrome.windows.getAll({windowTypes: ["normal"]});
  let tabId;
  if (!wins.length) {
    const w = await chrome.windows.create({url, focused: false, state: "minimized"});
    tabId = (w.tabs && w.tabs[0]) ? w.tabs[0].id : (await chrome.tabs.query({windowId: w.id}))[0].id;
  } else {
    const t = await chrome.tabs.create({windowId: wins[0].id, url, active: false, pinned: true});
    tabId = t.id;
  }
  await chrome.tabs.update(tabId, {autoDiscardable: false});   // Memory Saver must not discard it
  await session.set({readingTabId: tabId});
  await waitComplete(tabId);
  return tabId;
}

const TAB_PROBLEM = /No tab with id|Frame .* was removed|The tab was closed|No frame with id|Cannot access contents|error page|Receiving end does not exist/i;

async function inTab(func, args = []) {
  for (let tries = 0; ; tries++) {
    try {
      const tabId = await readingTab();
      const [res] = await chrome.scripting.executeScript({target: {tabId}, func, args, world: "ISOLATED"});
      const result = res && res.result;
      if (!result) throw new TabError("No frame with id (empty result)");
      if (result.page && !result.page.startsWith(IG.origin + "/")) throw new TabError("The tab was closed (navigated away)");
      return result;
    } catch (e) {
      const tabProblem = e instanceof TabError || TAB_PROBLEM.test(String(e && e.message || e));
      if (!tabProblem) throw e;
      await session.remove("readingTabId");
      if (tries >= 2) throw new IgStop("http_error", {message: "Couldn't keep the reading tab open"});
    }
  }
}

async function closeReadingTab() {
  const {readingTabId} = await session.get("readingTabId");
  if (readingTabId) { await chrome.tabs.remove(readingTabId).catch(() => {}); await session.remove("readingTabId"); }
}

// ============================================================================================ Instagram calls

function classify(r, path, {allowNonJson = false} = {}) {
  // Every JSON endpoint of the IG table, /api/ and /web/ alike, gets the 5.7 rules (fix round 2: a plain-text
  // 401 from /web/search/topsearch/ used to read as "not found").
  const apiPath = path.startsWith("/api/") || path.startsWith("/web/");
  const where = String(r.url || "");
  let wherePath = where;
  try { wherePath = new URL(where).pathname; } catch { /* keep the raw string */ }
  // Redirects are followed (reader.js), so r.url is where Instagram sent us (5.7).
  if (/\/accounts\/suspended/.test(wherePath)) throw new IgStop("challenge", {http_status: r.status, message: "Instagram sent /accounts/suspended/"});
  if (/\/challenge\//.test(wherePath)) throw new IgStop("challenge", {http_status: r.status, message: "redirect to /challenge/"});
  if (/\/accounts\/login/.test(wherePath)) throw new IgStop("signed_out", {http_status: r.status, message: "redirect to login"});
  // A redirect we can't see, or to a place we don't know: treat it as a checkpoint (24 h, no Try again), not as
  // "signed out" (30 min). Being careful costs a day; guessing wrong could cost the account.
  if (r.type === "opaqueredirect" || (r.status >= 300 && r.status < 400) || (r.redirected && apiPath)) {
    throw new IgStop("challenge", {http_status: r.status, message: "redirect to an unknown page"});
  }
  if (r.status === 429) throw new IgStop("rate_limited", {http_status: 429});
  if (r.status === 0 || r.status >= 500) return {retry: true, status: r.status};
  let data = null, isJson = false;
  if (r.text && /^\s*[{[]/.test(r.text)) { try { data = parseIg(r.text); isJson = true; } catch { /* not JSON */ } }
  if (isJson && data && typeof data === "object" && !Array.isArray(data)) {
    const msg = `${data.message || ""} ${data.error_type || ""} ${data.checkpoint_url || ""}`;
    if (data.status === "fail" || data.spam === true) {
      if (/suspended/.test(msg)) throw new IgStop("challenge", {message: "Instagram sent /accounts/suspended/"});
      if (CHALLENGE_RE.test(msg) || data.checkpoint_url) throw new IgStop("challenge", {http_status: r.status});
      if (WARNING_RE.test(msg)) throw new IgStop("warning", {http_status: r.status, message: msg.trim().slice(0, 200)});
      if (/login_required/.test(msg) || data.require_login) throw new IgStop("signed_out", {http_status: r.status});
    }
  }
  if ((r.status === 401 || r.status === 403) && apiPath) throw new IgStop("signed_out", {http_status: r.status});
  if (apiPath && r.status === 200 && !isJson) {
    const login = /accounts\/login|loginForm|LoginAndSignupPage|"login_page"/i.test(r.text || "");
    if (!allowNonJson || login) throw new IgStop("signed_out", {http_status: 200, message: "page instead of data"});
    return {status: 200, data: null, isJson: false, nonJson: true, text: ""};
  }
  return {status: r.status, data, isJson, text: isJson ? "" : (r.text || "")};
}

// One Instagram call: budget + random gap first, then the request in the reading tab, then the 5.7 rules.
async function ig(name, args = [], {kind = "ig", page = false, form = null, allowNonJson = false, referrer = null, li = null} = {}) {
  const ep = IG[name];
  const path = ep.path(...args);
  const liId = li || LI_OF[name] || null;
  // A plausible instagram.com page as the Referer, not the robots.txt reading page (fix round 2).
  const spec = {method: ep.method, path, appId: IG.appId, asbdId: IG.asbdId, referrer: referrer || IG.origin + "/"};
  if (ep.form) spec.form = ep.form(...(form || []));
  for (let attempt = 0; ; attempt++) {
    await beatIfDue();
    await take(kind, {page: page && attempt === 0});
    await setLayer("li");
    const t0 = Date.now();
    const r = await inTab(igFetch, [spec]);
    let res;
    try { res = classify(r, path, {allowNonJson}); }
    catch (e) {
      if (e instanceof IgStop) logCall(liId, "blocked", e.code, e.http_status || r.status, Date.now() - t0);
      throw e;
    }
    const fine = res.status >= 200 && res.status < 300;
    logCall(liId, fine ? "ok" : "error", fine ? null : "http_error", res.status, Date.now() - t0);
    if (!res.retry) return res;
    if (attempt >= 2) throw new IgStop("http_error", {http_status: res.status || null});
    await waitWithBeat(effPacing.retry_ms[Math.min(attempt, effPacing.retry_ms.length - 1)]);
  }
}

// One LOGGED-OUT call: the logged-out budget and gap first, then the request in the reading tab with NO cookies
// (reader.js igAnonFetch), then table 2.3. a: {handle, code, pk, pageId, cursor, lsd, doc}. Returns the result of
// strategies.js loInterpret, or {status: "skipped", code} when nothing was sent.
async function loCall(strategy, a = {}, {page = false, probe = false} = {}) {
  const skipped = code => ({status: "skipped", code, http: 0, items: 0});
  const now = Date.now();
  const cd = await get("cooldown");
  // A logged-in block also stops logged-out reads on this device.
  if (cd && cd.until > now && cd.reason !== "signed_out") return skipped("cooling");
  const lo = loMem;
  if (lo.cooldown_until > now && (!probe || HARD_BLOCKS.includes(lo.cooldown_reason))) return skipped("cooling");
  const ids = docIds[strategy] || [];
  const bump = a.doc ? 1 : 0;
  const idx = ids.length ? ((lo.doc_ok[strategy] || 0) + bump) % ids.length : 0;
  const spec = loRequest(strategy, a, {lsd: a.lsd, docId: ids[idx]});
  if (spec.skip) { logCall(strategy, "skipped", spec.skip, 0, 0); return skipped(spec.skip); }
  let res, calls = 0;
  for (let attempt = 0; ; attempt++) {
    await beatIfDue();
    // Open (or find) the reading tab first, so the random gap runs right before the request goes out.
    try { await readingTab(); } catch { /* inTab below opens it again or stops with http_error */ }
    try { await budget.take(page ? "lo_page" : "lo"); }
    catch (e) {
      if (!(e instanceof BudgetStop)) throw e;
      if (e.what.startsWith("lo_")) return {...skipped("budget"), calls};   // only the logged-out budget is used up
      throw new IgStop("budget", {cooldown_s: Math.ceil(e.waitMs / 1000), message: e.what});
    }
    await setLayer("lo");
    const t0 = Date.now();
    const r = await inTab(igAnonFetch, [spec]);
    calls += 1;
    res = loInterpret(strategy, r);
    res.ms = Date.now() - t0;
    if (!res.retry || attempt >= 1) break;         // 5xx or network: once more after the retry wait
    logCall(strategy, res.status, res.code, res.http, res.ms);
    await waitWithBeat(effPacing.retry_ms[0]);
  }
  res.calls = calls;
  // The newest lsd token of this browser, in memory only (chrome.storage.session): a video job has no page of its
  // own to take it from. It is never uploaded.
  if (res.lsd && isLsd(res.lsd)) await session.set({lo_lsd: {v: res.lsd, at: Date.now()}});
  res.more_ids = res.code === "changed" && bump === 0 && ids.length > 1;
  logCall(strategy, res.status, res.code, res.http, res.ms, res.items || 0);
  if (!res.more_ids) await mirrorLo(strategy, res);            // a rotated doc id gets one try with the next id
  if ((res.status === "ok" || res.status === "partial") && bump && (loMem.doc_ok[strategy] || 0) !== idx) {   // keep the id that works
    const cur = await loState(); cur.doc_ok[strategy] = idx; await saveLo(cur);
  }
  if (res.status === "ok" && probe && (loMem.strategy_off[strategy] || 0) > 0) {   // the re-test switched it back on
    const cur = await loState(); delete cur.strategy_off[strategy]; delete cur.repeats[strategy]; await saveLo(cur);
  }
  return res;
}

async function maybeDebug(endpoint, data) {
  if (!DEBUG_ENDPOINTS.includes(endpoint) || !data || typeof data !== "object") return;
  const sent = (await get("debug_sent")) || {};
  if (sent[endpoint]) return;
  sent[endpoint] = Date.now();
  await put({debug_sent: sent});
  await api("/debug-sample", {endpoint, keys_tree: keysTree(data)}).catch(() => {});
}

function checkUser(t) {
  if (!isPk(t.pk) || !t.username) throw new IgStop("parse_error", {raw_keys: Object.keys(t)});
  if (t.is_private) throw new IgStop("private");
  return t;
}

// Handle -> IgUser (5.4 ig_profile): web_profile_info, then topsearch, then the profile page HTML.
async function resolveUser(handle, kind = "ig") {
  let r = await ig("profileInfo", [handle], {kind});
  await maybeDebug("profileInfo", r.data);
  const u = r.status === 200 && r.data && r.data.data && r.data.data.user;
  if (u) return checkUser(trimUser(u));
  let pk = null;
  r = await ig("topsearch", [handle], {kind});
  if (r.status === 200 && r.data) {
    const hit = (r.data.users || []).map(x => x && x.user).find(x => x && x.username === handle);
    if (hit) pk = sid(hit.pk);
  }
  if (!pk) {
    r = await ig("profilePage", [handle], {kind});
    const m = r.status === 200 && (r.text || "").match(/"profile_id":"(\d{1,20})"/);
    if (m) pk = m[1];
  }
  if (!isPk(pk)) throw new IgStop("not_found");
  r = await ig("userInfo", [pk], {kind});
  if (r.status === 200 && r.data && r.data.user) return checkUser(trimUser(r.data.user));
  throw new IgStop("not_found");
}

// Sign-in check (5.3): only at pairing and at the start of an Instagram job when the cached result is old.
async function signInCheck() {
  const now = Date.now();
  try {
    const ids = await inTab(readIds, []);
    let user = null;
    if (isPk(ids.ds_user_id)) {
      const r = await ig("userInfo", [ids.ds_user_id], {li: "li_session"});
      if (r.status === 200 && r.data && r.data.user) user = r.data.user;
    }
    if (!user) {
      const r = await ig("currentUser", [], {li: "li_session"});
      await maybeDebug("currentUser", r.data);
      if (r.status === 200 && r.data && r.data.user) user = r.data.user;
    }
    const uid = user ? sid(user.pk !== undefined ? user.pk : user.pk_id) : null;
    const res = (user && isPk(uid)) ? {signed_in: true, user_id: uid, username: String(user.username || "").toLowerCase(), checked_at: now}
                                    : {signed_in: false, checked_at: now};
    await put({ig: res});
    return res;
  } catch (e) {
    if (e instanceof IgStop && e.code === "signed_out") {
      const res = {signed_in: false, checked_at: now};
      await put({ig: res});
      return res;
    }
    // Any other stop is remembered too (fix round 2): the 30-minute re-check guard then applies, and the popup
    // can say what really happened instead of "signed out".
    if (e instanceof IgStop) await put({ig: {signed_in: false, checked_at: now, error: e.code}});
    throw e;
  }
}

// Before the first logged-in call of a job. A logged-out stage does not need it (DESIGN-FETCH.md 2.1).
async function ensureSignedIn() {
  let cd = await get("cooldown");
  if (cd && cd.until > Date.now() && cd.reason === "signed_out") {
    try { await hello(); } catch { /* keep the local word */ }   // "Try again" in ReelVault clears it
    cd = await get("cooldown");
    if (cd && cd.until > Date.now() && cd.reason === "signed_out") throw new IgStop("signed_out");   // no call
  }
  let igs = await get("ig");
  if (!igs || !igs.signed_in || Date.now() - (igs.checked_at || 0) > SIX_H) igs = await signInCheck();
  if (!igs.signed_in) throw new IgStop("signed_out");
}

// A 5.7 stop outside a job (the sign-in check at pairing or the account re-check): stop reading Instagram for the
// 5.7 time here, and tell the server so its lease answers and the web UI agree (fix round 2).
const NO_JOB_STOPS = ["rate_limited", "warning", "challenge", "http_error"];
async function stopWithoutJob(e) {
  if (!(e instanceof IgStop) || !NO_JOB_STOPS.includes(e.code)) return false;
  const until = Date.now() + LOCAL_COOLDOWN_S[e.code] * 1000;
  await put({cooldown: {until, reason: e.code, local_only: true}});
  badge("");
  const stop = {code: e.code};
  if (Number.isInteger(e.http_status)) stop.http_status = e.http_status;
  if (typeof e.message === "string" && e.message !== e.code) stop.message = e.message.slice(0, 300);
  try {
    const r = await api("/hello", {version: VERSION, stop});
    if (r.status === 200 && r.data && r.data.cooldown_until) {
      await put({cooldown: {until: Date.parse(r.data.cooldown_until), reason: r.data.cooldown_reason || e.code}});
    }
  } catch { /* the local stop stays until the server hears of it */ }
  return true;
}

// ============================================================================================ jobs

// What the ladder (ladder.js) needs from the worker for one job.
function loIo(job) {
  const p = job.payload;
  return {
    call: loCall,
    upload: async body => {
      const up = (await post(`/jobs/${job.id}/items`, body)).data || {};
      if (up.lo) await mergeServerLo(up.lo);
      const n = (curJob.state.collected || 0) + (up.accepted || 0);
      await progress(n, p.target, `Read ${n} of ${p.target} reels`);
      return up;
    },
    save: saveState,
    off: s => (loMem.strategy_off[s] || 0) > Date.now(),
    detailOff: () => loMem.detail_off_until > Date.now(),
    block: kind => loBlock(kind, p.read),
    limits: () => effPacing,
  };
}

async function runPull(job) {
  const p = job.payload, st = curJob.state;
  st.stage ??= p.plan.lo ? "lo" : "li";
  if (st.stage === "lo") {                       // stage 1: logged out, no cookies (DESIGN-FETCH.md 2.1)
    let out;
    try { out = await runLoStage(job, st, loIo(job)); }
    catch (e) {
      if (e instanceof LoFail) throw new IgStop(e.code, {lo: e.lo, http_status: e.http_status});
      throw e;
    }
    if (out.end !== "resume") return {stop_reason: out.stop_reason, result: {recovery: null}};
    const rs = out.resume;                       // stage 2: the logged-in top-up, from the top
    if (!rs || typeof rs !== "object" || !["clips", "feed"].includes(rs.source)
        || (rs.max_id !== null && rs.max_id !== undefined && !isCursor(rs.max_id))) {
      throw new IgStop("bad_job", {message: "bad resume"});
    }
    st.stage = "li";
    st.req = {source: rs.source, max_id: rs.max_id ?? null, saved: false};
    if (st.lo && isPk(st.lo.pk)) st.pk = st.lo.pk;
    st.account_sent = !!(st.lo && st.lo.accountSent);
    await saveState();
    await progress(st.collected || 0, p.target, "Reading with your Instagram login");
  }
  await ensureSignedIn();                        // the sign-in check runs when the logged-in stage starts
  st.pages ??= 0; st.page_size ??= "50"; st.clips_bad ??= 0; st.collected ??= 0;
  let account = st.account || null;
  if (!st.pk) {
    if (p.ig_pk) st.pk = p.ig_pk;
    else { account = await resolveUser(p.handle); st.pk = account.pk; st.account = account; }
  }
  const pk = st.pk;
  if (!st.req) {
    st.req = p.mode === "next" ? {source: p.cursor_source, max_id: p.max_id, saved: !!p.max_id}
                               : {source: "clips", max_id: null, saved: false};
  }
  await saveState();
  for (;;) {
    if (st.pages >= PAGE_CAP) return {stop_reason: "page_cap", result: {recovery: st.recovery || null}};
    const req = st.req;
    const referrer = p.handle ? `${IG.origin}/${encodeURIComponent(p.handle)}/reels/` : null;
    const res = req.source === "clips"
      ? await ig("clipsUser", [], {form: [pk, req.max_id, st.page_size], page: st.pages > 0, allowNonJson: true, referrer})
      : await ig("feedUser", [pk, req.max_id], {page: st.pages > 0, referrer});
    const bodyText = JSON.stringify(res.data || "") + (res.text || "");
    // Cursor recovery (5.4 step 4): never a cooldown, never an "error" for the member.
    if (req.source === "clips" && res.status === 400 && /page_size/.test(bodyText) && st.page_size !== "12") {
      st.page_size = "12"; st.recovery = st.recovery || "page_size_12"; await saveState(); continue;
    }
    const jsonFail = res.isJson && res.data && res.data.status === "fail";
    if (req.saved && (res.status >= 400 || jsonFail)) {
      if (!st.triedA && isPk(p.oldest_pk)) {
        st.triedA = true; st.recovery = "feed_oldest_pk";
        st.req = {source: "feed", max_id: `${p.oldest_pk}_${pk}`, saved: true};
      } else {
        st.recovery = "top_skip_known"; st.req = {source: "clips", max_id: null, saved: false};
      }
      await saveState(); continue;
    }
    if (req.source === "clips" && (res.status === 404 || res.status === 405 || res.nonJson)) {
      st.clips_bad += 1;
      if (st.clips_bad >= 2 || res.nonJson) {
        const fromOldest = p.mode === "next" && isPk(p.oldest_pk);
        st.req = {source: "feed", max_id: fromOldest ? `${p.oldest_pk}_${pk}` : null, saved: false};
      }
      await saveState(); continue;
    }
    if (res.status === 404) throw new IgStop("not_found", {http_status: 404});
    if (res.status >= 400 || jsonFail) throw new IgStop("http_error", {http_status: res.status});
    const d = res.data || {};
    let raw, paging;
    if (req.source === "clips") {
      if (!Array.isArray(d.items) || !d.paging_info || typeof d.paging_info !== "object") {
        throw new IgStop("parse_error", {raw_keys: Object.keys(d).slice(0, 50)});
      }
      raw = d.items.map(it => it && it.media).filter(Boolean);
      paging = {max_id: sid(d.paging_info.max_id), more_available: !!d.paging_info.more_available};
    } else {
      if (!Array.isArray(d.items)) throw new IgStop("parse_error", {raw_keys: Object.keys(d).slice(0, 50)});
      raw = d.items.filter(isReel);
      paging = {max_id: sid(d.next_max_id), more_available: !!d.more_available};
    }
    if (paging.max_id && !isCursor(paging.max_id)) throw new IgStop("parse_error", {raw_keys: ["paging.max_id"]});
    await maybeDebug(req.source === "clips" ? "clipsUser" : "feedUser", d);
    const items = raw.filter(m => keepForTarget(m, pk)).map(trimMedia);
    lastLogItems(items.length);
    const body = {page: st.pages + 1, items,
                  paging: {...paging, source: req.source, request_max_id: req.max_id || null}};
    if (account && !st.account_sent) body.account = account;
    const up = (await post(`/jobs/${job.id}/items`, body)).data;
    if (up && up.lo) await mergeServerLo(up.lo);
    st.account_sent = true;
    st.pages += 1;
    st.collected += up.accepted || 0;
    if (up.resume) {
      const rs = up.resume;
      if (!["clips", "feed"].includes(rs.source) || (rs.max_id !== null && !isCursor(rs.max_id))) {
        throw new IgStop("bad_job", {message: "bad resume"});
      }
      st.req = {source: rs.source, max_id: rs.max_id, saved: false};
      if (rs.skip_known) st.recovery = "top_skip_known";
      else if (rs.source === "feed") st.recovery = st.recovery || "feed_oldest_pk";
      await saveState();
      await progress(st.collected, p.target, `Read ${st.collected} of ${p.target} reels`, {cursor: rs.max_id ? {max_id: rs.max_id} : undefined});
      continue;
    }
    if (up.stop) {
      const why = up.known_hit ? "known_reel" : (st.collected < p.target && st.pages >= PAGE_CAP ? "page_cap" : "target_reached");
      return {stop_reason: why, result: {recovery: st.recovery || null}};
    }
    if (!paging.more_available) return {stop_reason: "no_more", result: {recovery: st.recovery || null}};
    st.req = {source: req.source, max_id: paging.max_id, saved: false};
    await saveState();
    await progress(st.collected, p.target, `Read ${st.collected} of ${p.target} reels`,
                   paging.max_id ? {cursor: {max_id: paging.max_id}} : {});
  }
}

// A profile without the login: the profile page, then web_profile_info, both with no cookies. null = use the login.
async function loUser(job) {
  const p = job.payload, st = curJob.state;
  st.lo ??= {lsd: null};
  for (const s of ["lo_profile_page", "lo_web_profile"]) {
    if (!p.plan.strategies.includes(s) || (loMem.strategy_off[s] || 0) > Date.now()) continue;
    const r = await loCall(s, {handle: p.handle, lsd: st.lo.lsd});
    await saveState();
    if (r.status === "skipped") { st.lo.skip = r.code; return null; }     // nothing was sent
    if (r.code === "not_found") throw new IgStop("not_found", {http_status: 404});
    if (r.private) throw new IgStop("private");
    if (r.status === "blocked" && HARD_BLOCKS.includes(r.code)) {
      throw new IgStop("lo_blocked", {lo: {class: r.code, strategy: s, http: r.http || 0}});
    }
    if (r.user && isPk(r.user.pk)) return r.user;
    // A real answer without the profile: a login wall unless Instagram said which block it was.
    st.lo.last = {class: r.status === "blocked" && r.code ? r.code : "login_wall", strategy: s, http: r.http || 0};
  }
  return null;
}

async function runProfile(job) {
  const p = job.payload;
  curJob.state.lo ??= {lsd: null};
  let u = p.plan.lo ? await loUser(job) : null;
  if (!u) {
    if (!p.plan.li) {                            // the member chose "never use my login"
      const last = curJob.state.lo && curJob.state.lo.last;
      if (last) throw new IgStop("lo_blocked", {lo: last});      // only a real Instagram answer is a block
      throw new IgStop("budget", {cooldown_s: loWaitS(), message: "lo_skipped " + ((curJob.state.lo || {}).skip || "")});
    }
    await ensureSignedIn();
    u = await resolveUser(p.handle);
  }
  if (p.purpose === "competitor") await post(`/jobs/${job.id}/items`, {account: u, items: []});
  else await post("/ig-user", {job_id: job.id, user: u});
  return {stop_reason: "no_more", result: {found: true}};
}

async function installRefererRule() {
  // A fetch from the SW can't set Referer itself; this session rule does it for our own CDN requests only (5.4).
  await chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: [1], addRules: [{id: 1, priority: 1,
    action: {type: "modifyHeaders", requestHeaders: [{header: "referer", operation: "set", value: IG.origin + "/"}]},
    condition: {requestDomains: CDN_DOMAINS, initiatorDomains: [chrome.runtime.id],
                resourceTypes: ["xmlhttprequest", "other", "media"]}}]});
}

async function download(url, max) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 120_000);
  try {
    const r = await fetch(url, {credentials: "omit", signal: ctl.signal, cache: "no-store"});
    if (!r.ok) return {error: "http_" + r.status};
    const cl = Number(r.headers.get("content-length") || 0);
    if (cl > max) { ctl.abort(); return {error: "too_big"}; }
    const reader = r.body.getReader();
    const chunks = [];
    let n = 0;
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      n += value.length;
      if (n > max) { ctl.abort(); return {error: "too_big"}; }
      chunks.push(value);
      await beatIfDue();
    }
    return {blob: new Blob(chunks, {type: r.headers.get("content-type") || "application/octet-stream"})};
  } catch (e) {
    return {error: "network"};
  } finally { clearTimeout(t); }
}

async function uploadVideo(job, reelId, blob, cover) {
  const pair = await get("pair");
  const fd = new FormData();
  fd.append("reel_id", reelId);
  fd.append("file", blob, "video.mp4");
  if (cover) fd.append("cover", cover, "cover.jpg");
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 120_000);
  try {
    const r = await fetch(`${pair.server}/api/ext/v2/jobs/${job.id}/video`,
      {method: "POST", body: fd, signal: ctl.signal, credentials: "omit", headers: {"Authorization": "Bearer " + pair.token}});
    if (r.status === 401) { await put({revoked: true}); throw new Unpaired(); }
    return r.status;
  } catch (e) {
    if (e instanceof Unpaired) throw e;
    throw new ServerDown(String(e && e.message || e));
  } finally { clearTimeout(t); }
}

// A fresh video URL without the login (DESIGN-FETCH.md 3.3, P2): lo_post_gql(code), no cookies, logged-out budget.
// Returns the media, or null when the login has to do it (or the reel is skipped). Throws lo_blocked on a hard block.
const LSD_MAX_AGE = 6 * 3600_000;
// st.lo_real: the last REAL logged-out answer that stopped the logged-out way ({class, strategy, http}). A skipped
// call (no lsd, cooling, budget, switched off) sent nothing to Instagram, so it never becomes a block report.
async function loVideoMedia(job, it) {
  const p = job.payload, st = curJob.state;
  if (!p.plan || !p.plan.lo || st.lo_off) return null;
  const offUntil = loMem.strategy_off.lo_post_gql || 0;
  if (st.lo_used >= Math.min(p.plan.lo_budget, 30) || offUntil > Date.now()) {
    st.lo_off = true; st.lo_skip = offUntil > Date.now() ? "cooling" : "budget"; return null;
  }
  if (!st.lsd) {
    const {lo_lsd} = await session.get("lo_lsd");
    if (lo_lsd && isLsd(lo_lsd.v) && Date.now() - lo_lsd.at < LSD_MAX_AGE) st.lsd = lo_lsd.v;
  }
  if (!st.lsd) {                     // no fresh token (Chrome restarted, or 6 h passed): one profile page read first
    const got = await loFreshLsd(job, it);
    if (got === "stop") return null;
  }
  const r = await loCall("lo_post_gql", {code: it.code, lsd: st.lsd});
  if (r.status === "skipped") { st.lo_off = true; st.lo_skip = r.code; return null; }   // nothing was sent
  st.lo_used = (st.lo_used || 0) + (r.calls || 1);
  if (r.lsd) st.lsd = r.lsd;
  if (r.status === "blocked" && HARD_BLOCKS.includes(r.code)) {
    throw new IgStop("lo_blocked", {lo: {class: r.code, strategy: "lo_post_gql", http: r.http || 0}});
  }
  if (r.status === "blocked") st.lo_real = {class: r.code || "login_wall", strategy: "lo_post_gql", http: r.http || 0};
  const m = r.media;
  if ((r.status === "ok" || r.status === "partial") && m && sid(m.pk) === it.ig_pk && videoOf(m)) {
    lastLogItems(1);
    return m;
  }
  if (r.code === "empty" || r.code === "not_found") return null;        // a gated post: the login may still read it
  st.lo_off = true;                                                      // after the first block: the login only
  return null;
}

// A fresh lsd token without the login: the profile page of the reel's account (cookie-less, like any visitor).
// Returns "ok" (st.lsd is set) or "stop" (st.lo_off is set; st.lo_real when Instagram really answered).
async function loFreshLsd(job, it) {
  const p = job.payload, st = curJob.state;
  const s = "lo_profile_page";
  if (!isHandle(it.handle) || !p.plan.strategies.includes(s) || (loMem.strategy_off[s] || 0) > Date.now()) {
    st.lo_off = true; st.lo_skip = "no_lsd"; return "stop";
  }
  const r = await loCall(s, {handle: it.handle});
  if (r.status === "skipped") { st.lo_off = true; st.lo_skip = r.code; return "stop"; }
  st.lo_used = (st.lo_used || 0) + (r.calls || 1);
  if (r.status === "blocked" && HARD_BLOCKS.includes(r.code)) {
    throw new IgStop("lo_blocked", {lo: {class: r.code, strategy: s, http: r.http || 0}});
  }
  if (r.lsd && isLsd(r.lsd)) { st.lsd = r.lsd; return "ok"; }
  st.lo_off = true;
  if (r.status === "blocked") st.lo_real = {class: r.code || "login_wall", strategy: s, http: r.http || 0};
  else st.lo_skip = "no_lsd";                     // the page loaded but had no token: not a block
  return "stop";
}

// How long to wait before a logged-out try makes sense again (a skipped job never reports a block).
function loWaitS() {
  const now = Date.now();
  const until = Math.max(loMem.cooldown_until || 0, loMem.strategy_off.lo_post_gql || 0);
  return Math.max(900, Math.min(6 * 3600, Math.ceil((until - now) / 1000) || 3600));
}

async function runVideos(job) {
  const items = job.payload.items, st = curJob.state, plan = job.payload.plan;
  st.i ??= 0; st.failed ??= []; st.uploaded ??= 0; st.media_info ??= 0; st.lo_used ??= 0;
  await installRefererRule();
  for (; st.i < items.length; st.i++) {
    const it = items[st.i];
    let m = await loVideoMedia(job, it);
    if (!m) {
      if (plan && !plan.li) {                    // "never use my login": skip this reel, or stop when nothing works
        if (st.lo_off && !st.uploaded && !st.failed.length) {
          // A real block from Instagram: report it as it was. Nothing sent: wait, with no block (no strike).
          if (st.lo_real) throw new IgStop("lo_blocked", {lo: st.lo_real});
          throw new IgStop("budget", {cooldown_s: loWaitS(), message: "lo_skipped " + (st.lo_skip || "")});
        }
        st.failed.push({reel_id: it.reel_id, reason: "login_needed"}); await saveState(); continue;   // tried again later
      }
      if (!st.signed_checked) { await ensureSignedIn(); st.signed_checked = true; }   // the login starts here
      if (st.media_info >= effPacing.media_info_max) throw new IgStop("budget", {cooldown_s: 3600, message: "media_info"});
      const r = await ig("mediaInfo", [it.ig_pk]);
      st.media_info += 1;
      if (r.status === 400 || r.status === 404) { st.failed.push({reel_id: it.reel_id, reason: "not_found"}); continue; }
      m = r.data && Array.isArray(r.data.items) ? r.data.items[0] : null;
      if (r.status !== 200 || !m) throw new IgStop("parse_error", {raw_keys: Object.keys(r.data || {}).slice(0, 50)});
      const mpk = sid(String(m.id || "").split("_")[0]) || sid(m.pk);
      if (mpk !== it.ig_pk) { st.failed.push({reel_id: it.reel_id, reason: "mismatch"}); continue; }
    }
    const v = videoOf(m);
    if (!v || !isCdnUrl(v.url)) { st.failed.push({reel_id: it.reel_id, reason: "no_video"}); continue; }
    await take("cdn");
    const got = await download(v.url, VIDEO_MAX);
    // 5.7: a CDN 429 is the same stop as an API 429 (24 h). Three CDN 403s in a row also stop. The reels that
    // are left stay waiting for a later try on the server (finish/fail never mark them failed for good).
    if (got.error === "http_429") throw new IgStop("rate_limited", {http_status: 429, message: "cdn 429"});
    st.cdn403 = got.error === "http_403" ? (st.cdn403 || 0) + 1 : 0;
    if (st.cdn403 >= 3) throw new IgStop("rate_limited", {http_status: 403, message: "cdn 403 x3"});
    if (got.error) { st.failed.push({reel_id: it.reel_id, reason: got.error}); await saveState(); continue; }
    let cover = null;
    const c = job.payload.want_cover ? coverOf(m) : null;
    if (c && isCdnUrl(c.url)) {
      await take("cdn");
      const cg = await download(c.url, COVER_MAX);
      if (!cg.error) cover = cg.blob;
    }
    const status = await uploadVideo(job, it.reel_id, got.blob, cover);
    if (status === 409) throw new Dropped();
    if (status === 200) st.uploaded += 1;
    else st.failed.push({reel_id: it.reel_id, reason: status === 413 ? "too_big" : "upload_" + status});
    await saveState();
    await progress(st.i + 1, items.length, `Saved ${st.uploaded} of ${items.length} videos`);
  }
  return {stop_reason: "no_more", result: {uploaded: st.uploaded, failed: st.failed, media_info: st.media_info}};
}

async function runDiscover(job) {
  const p = job.payload;
  const users = new Map();
  const add = (u, source, seed) => {
    if (!u || typeof u !== "object" || users.size >= 30) return;
    const t = trimUser(u);
    if (!isPk(t.pk) || !t.username || t.is_private || users.has(t.username)) return;
    users.set(t.username, {...t, source, ...(seed ? {seed_handle: seed} : {})});
  };
  const bestEffort = async fn => {
    try { await fn(); } catch (e) {
      if (e instanceof IgStop && ["http_error", "parse_error", "not_found"].includes(e.code)) return;
      throw e;
    }
  };
  if (p.mode === "search") {
    for (const q of p.queries) {
      const r = await ig("topsearch", [q], {kind: "discover"});
      if (r.status === 200 && r.data) for (const x of r.data.users || []) add(x && x.user, "search");
      await bestEffort(async () => {
        const s = await ig("keywordSerp", [q], {kind: "discover"});
        if (s.status !== 200 || !s.data) return;
        for (const sec of ((s.data.media_grid || {}).sections || [])) {
          for (const mm of ((sec && sec.layout_content) || {}).medias || []) add(mm && mm.media && mm.media.user, "search");
        }
      });
    }
  } else {
    for (const seed of p.seeds) {
      let pk = seed.pk, info = null;
      if (!pk) { info = await resolveUser(seed.handle, "discover").catch(e => { if (e.code === "not_found" || e.code === "private") return null; throw e; }); pk = info && info.pk; }
      if (!pk) continue;
      let found = 0;
      await bestEffort(async () => {
        const r = await ig("chaining", [pk], {kind: "discover"});
        await maybeDebug("chaining", r.data);
        if (r.status === 200 && r.data) for (const u of r.data.users || []) { add(u, "similar", seed.handle); found++; }
      });
      if (!found) {
        const q = ((info && (info.full_name || info.category)) || seed.handle).replace(/[^A-Za-z0-9 &\-'.]/g, " ").replace(/\s+/g, " ").trim().slice(0, 60);
        if (q) {
          const r = await ig("topsearch", [q], {kind: "discover"});
          if (r.status === 200 && r.data) for (const x of r.data.users || []) add(x && x.user, "similar", seed.handle);
        }
      }
    }
  }
  return {stop_reason: "no_more", result: {users: [...users.values()]}};
}

async function runCheck(job) {
  const cands = job.payload.candidates, st = curJob.state;
  st.i ??= 0;
  for (; st.i < cands.length; st.i++) {
    const c = cands[st.i];
    let u;
    try { u = await resolveUser(c.handle, "discover"); }
    catch (e) { if (e instanceof IgStop && (e.code === "not_found" || e.code === "private")) continue; throw e; }
    const r = await ig("clipsUser", [], {form: [u.pk, null, "12"], kind: "discover", allowNonJson: true});
    let items = [];
    if (r.status === 200 && r.data && Array.isArray(r.data.items)) {
      items = r.data.items.map(x => x && x.media).filter(Boolean).filter(m => keepForTarget(m, u.pk)).slice(0, 12).map(trimMedia);
    }
    await apiOk(`/jobs/${job.id}/items`, {candidate: {cand_id: c.cand_id}, account: u, items,
                                         paging: {source: "clips", max_id: null, more_available: true}});
    await saveState();
    await progress(st.i + 1, cands.length, `Checked ${st.i + 1} of ${cands.length} accounts`);
  }
  return {stop_reason: "no_more", result: {checked: cands.length}};
}

// ---------------------------------------------------------------------------------------------- ig_probe (3.4)
// "Test my connection": one call per row, logged-out pacing, at most 12 calls. Each row is posted when it ends.

function probeRows(p) {
  const h0 = p.handles[0], h1 = p.handles[1] || null;
  const rows = [["lo_profile_page", h0], ["lo_reels_page", h0], ["lo_reels_gql", h0], ["lo_web_profile", h0],
                ["lo_posts_gql", h0], ["lo_post_gql", h0], ["lo_media_gql", h0], ["lo_embed", h0]];
  if (h1) rows.push(["lo_profile_page", h1]);
  rows.push(["li_session", null], ["li_clips", h1 || h0]);
  return rows.filter(([s]) => p.want.includes(s));
}

async function probeLo(s, h, P) {
  const c = (P.ctx[h] ??= {});
  const row = (status, code, res = {}) => ({strategy: s, layer: "logged_out", handle: h, status, code: code || null,
    http: res.http || 0, ms: res.ms || 0, items: res.items || 0,
    fields: status === "ok" || status === "partial" ? probeFields(res) : []});
  const now = Date.now();
  if (P.over) return row("skipped", "budget");
  // No logged-out row during a rate or challenge cooldown. Per-strategy switch-offs are ignored: this is the re-test.
  if (P.loEnded || (loMem.cooldown_until > now && HARD_BLOCKS.includes(loMem.cooldown_reason))) return row("skipped", "cooling");
  if (P.calls >= PROBE_CALLS_MAX) return row("skipped", "budget");
  const reel = c.reel || c.maybe || null;
  const args = {handle: h, lsd: c.lsd, pageId: c.pageId, cursor: s === "lo_posts_gql" ? c.cursor : null,
                code: reel ? reel.code : null,
                pk: s === "lo_media_gql" ? (reel ? (reel.pk || pkFromCode(reel.code)) : null) : c.pk};
  const res = await loCall(s, args, {probe: true});
  if (res.status === "skipped") return row("skipped", res.code);
  P.calls += res.calls || 1;
  if (res.lsd) c.lsd = res.lsd;
  if (isPk(res.pk) && !c.pk) c.pk = res.pk;
  if (s === "lo_profile_page") { c.pageId = res.pageId || null; c.cursor = res.cursor || null; }
  if (!c.reel) {                                   // the first reel found, for the three detail rows
    const st = (res.stubs || []).find(x => x.reel);
    const m = (res.medias || [])[0];
    const n = (res.nodes || []).find(x => x.is_video);
    if (st) c.reel = {code: st.code, pk: st.pk};
    else if (m) c.reel = {code: m.code, pk: isPk(sid(m.pk)) ? sid(m.pk) : null};
    else if (n) c.reel = {code: n.shortcode, pk: isPk(sid(n.id)) ? sid(n.id) : null};
  }
  if (!c.maybe && res.stubs && res.stubs.length) c.maybe = {code: res.stubs[0].code, pk: res.stubs[0].pk};
  if (HARD_BLOCKS.includes(res.code)) P.loEnded = true;         // a rate result ends the logged-out rows
  return row(res.status, res.code, res);
}

async function probeLi(s, h, P) {
  const t0 = Date.now();
  const row = (status, code, x = {}) => ({strategy: s, layer: "logged_in", handle: s === "li_session" ? null : h, status,
    code: code || null, http: x.http || 0, ms: x.calls ? Date.now() - t0 : 0, items: x.items || 0, fields: x.fields || []});
  if (P.over) return {row: row("skipped", "budget")};   // the 4th probe of the day posts skipped rows
  const cd = await get("cooldown");
  if (cd && cd.until > t0) {                       // no logged-in call during a stop
    const out = cd.reason === "signed_out";
    return {row: row(out && s === "li_session" ? "blocked" : "skipped", out ? "signed_out" : "cooling")};
  }
  if (loMem.li_not_before > t0) return {row: row("skipped", "cooling")};   // the login waits after a logged-out block
  try {
    let igs = await get("ig");
    if (s === "li_session") {                      // a test checks fresh: one call, never the 6-hour cache
      if (P.over || P.calls >= PROBE_CALLS_MAX) return {row: row("skipped", "budget")};
      igs = await signInCheck(); P.calls += 1;
      return {row: igs.signed_in ? row("ok", null, {http: 200, calls: 1, fields: ["signed_in"]})
                                 : row("blocked", "signed_out", {http: 200, calls: 1})};
    }
    if (!igs || !igs.signed_in) return {row: row("skipped", "signed_out")};
    if (P.over || P.calls >= PROBE_CALLS_MAX) return {row: row("skipped", "budget")};
    let pk = (P.ctx[h] || {}).pk;
    if (!isPk(pk)) {                               // the logged-out rows found nobody: one logged-in lookup
      const r = await ig("profileInfo", [h]);
      P.calls += 1;
      const u = r.status === 200 && r.data && r.data.data && r.data.data.user;
      pk = u ? trimUser(u).pk : null;
    }
    if (!isPk(pk)) return {row: row("skipped", "no_user")};
    if (P.calls >= PROBE_CALLS_MAX) return {row: row("skipped", "budget")};
    const r = await ig("clipsUser", [], {form: [pk, null, "12"], allowNonJson: true,
                                         referrer: `${IG.origin}/${encodeURIComponent(h)}/reels/`});
    P.calls += 1;
    const ok = r.status === 200 && r.data && Array.isArray(r.data.items);
    const medias = ok ? r.data.items.map(x => x && x.media).filter(Boolean).filter(m => keepForTarget(m, pk)) : [];
    lastLogItems(medias.length);
    return {row: row(!ok ? "error" : medias.length ? "ok" : "partial", !ok ? "http_error" : medias.length ? null : "empty",
                     {http: r.status, calls: 1, items: medias.length, fields: probeFields({medias})})};
  } catch (e) {
    if (!(e instanceof IgStop)) throw e;
    if (e.code === "budget") return {row: row("skipped", "budget")};
    if (e.code === "signed_out") {
      await put({ig: {signed_in: false, checked_at: Date.now()}});
      return {row: row("blocked", "signed_out", {http: e.http_status || 0, calls: 1})};
    }
    const soft = ["http_error", "parse_error", "not_found", "private"].includes(e.code);
    // A real Instagram stop (slow down, checkpoint) is reported like in any job, after the row is posted.
    return {row: row(soft ? "error" : "blocked", e.code, {http: e.http_status || 0, calls: 1}), stop: soft ? null : e};
  }
}

async function runProbe(job) {
  const p = job.payload, st = curJob.state;
  const P = (st.probe ??= {i: 0, calls: 0, ctx: {}, loEnded: false, counted: false, over: false});
  if (!P.counted) {                                // 3 probes per day per device
    try { await budget.take("probe"); }
    catch (e) { if (!(e instanceof BudgetStop)) throw e; P.over = true; }
    P.counted = true;
    await saveState();
  }
  const rows = probeRows(p);
  while (P.i < rows.length) {
    const [s, h] = rows[P.i];
    const out = s.startsWith("lo_") ? {row: await probeLo(s, h, P)} : await probeLi(s, h, P);
    await post(`/jobs/${job.id}/probe`, {rows: [out.row]});
    P.i += 1;
    await saveState();
    await progress(P.i, rows.length, `Tested ${P.i} of ${rows.length} ways to read`);
    if (out.stop) throw out.stop;
  }
  return {stop_reason: "no_more", result: {rows: rows.length}};
}

// ---------------------------------------------------------------------------------------------- page_read (5.4)

const grants = new Map();   // job id -> resolve(granted)

async function askGrant(job, host) {
  const url = chrome.runtime.getURL(`grant.html?job=${encodeURIComponent(job.id)}&host=${encodeURIComponent(host)}`);
  const tab = await chrome.tabs.create({url, active: true});
  badge("!");
  const answer = new Promise(resolve => {
    grants.set(job.id, resolve);
    const onRem = id => { if (id === tab.id) { chrome.tabs.onRemoved.removeListener(onRem); resolve(false); } };
    chrome.tabs.onRemoved.addListener(onRem);
  });
  const deadline = Date.now() + 10 * 60_000;
  let result = null;
  answer.then(v => { result = v; });
  while (result === null && Date.now() < deadline) { await sleep(500); await beatIfDue(); }
  grants.delete(job.id);
  chrome.tabs.remove(tab.id).catch(() => {});
  return result === true;
}

async function runPageRead(job) {
  const p = job.payload;
  const origin = new URL(p.url).origin;
  const host = new URL(p.url).host;
  await take("page_read");
  if (!(await chrome.permissions.contains({origins: [origin + "/*"]}))) {
    const ok = await askGrant(job, host);
    if (!ok) throw new IgStop("permission_denied");
  }
  const wins = await chrome.windows.getAll({windowTypes: ["normal"]});
  const tab = await chrome.tabs.create({...(wins.length ? {windowId: wins[0].id} : {}), url: p.url, active: false});
  let out;
  try {
    await waitComplete(tab.id, 45_000);
    await sleep(2000);
    const [res] = await chrome.scripting.executeScript({target: {tabId: tab.id}, world: "ISOLATED",
      func: p.kind === "linkedin_self" ? readLinkedIn : readSkool});
    out = res && res.result;
  } finally { chrome.tabs.remove(tab.id).catch(() => {}); }
  if (!out) throw new IgStop("http_error", {message: "The page did not load"});
  const loginLike = /\/(login|signin|signup|uas\/login|authwall|checkpoint)/i.test(out.final_url || "");
  if (p.kind === "linkedin_self") {
    if (!/^https:\/\/www\.linkedin\.com\/in\/[^/?#]+\/?$/.test(out.final_url || "")) {
      throw new IgStop(loginLike ? "signed_out" : "parse_error", {message: "LinkedIn did not show your profile"});
    }
    await apiOk(`/jobs/${job.id}/page`, {final_url: out.final_url, title: out.title, text: out.text});
  } else {
    if (loginLike || !(out.final_url || "").startsWith("https://www.skool.com/")) throw new IgStop("signed_out");
    if (!out.next_data) throw new IgStop("parse_error", {message: "No page data"});
    await apiOk(`/jobs/${job.id}/page`, {final_url: out.final_url, title: out.title, next_data: out.next_data});
  }
  return {stop_reason: "no_more", result: {status: "ok"}};
}

// ---------------------------------------------------------------------------------------------- one job

let running = false;
let keepAlive = null;

async function runJob(raw, resumed) {
  running = true;
  const v = validateJob(raw);
  if (!v.ok) {                                   // no Instagram call for a job that fails 5.4.0
    await api(`/jobs/${raw && raw.id}/fail`, {code: "bad_job", message: `field ${v.field}`}).catch(() => {});
    await local.remove("job");
    running = false;
    return;
  }
  const job = v.job;
  curJob = {job, state: (resumed && resumed.state) || {}};
  // The way this job starts reading: logged out when the plan says so (or a stored job is in that stage).
  const plan = job.payload && job.payload.plan;
  curLayer = !IG_KINDS.includes(job.kind) ? null : job.kind === "ig_probe" || (plan && plan.lo && curJob.state.stage !== "li") ? "lo" : "li";
  lastBeat = 0;
  keepAlive = setInterval(() => session.set({hb: Date.now()}), 10_000);   // keep the SW alive for the whole job
  badge("…");
  await saveState();
  let keepLocal = false;
  try {
    const total = job.kind === "ig_pull" ? job.payload.target
      : job.kind === "ig_probe" ? probeRows(job.payload).length
      : (job.payload.items || job.payload.candidates || [1]).length;
    // The sign-in check: only for a job that starts with a logged-in call. A logged-out stage and the probe run
    // without it (DESIGN-FETCH.md 2.1, 3.4); runPull and runProfile check when they reach the login.
    const loFirst = !!(job.payload.plan && job.payload.plan.lo) && ["ig_pull", "ig_profile", "ig_videos"].includes(job.kind);
    if (IG_KINDS.includes(job.kind) && job.kind !== "ig_probe" && !loFirst
        && !(job.kind === "ig_pull" && curJob.state.stage === "lo")) await ensureSignedIn();
    await progress(0, total, resumed ? "Going on where it stopped" : "Starting");
    let out;
    switch (job.kind) {
      case "ig_pull": out = await runPull(job); break;
      case "ig_profile": out = await runProfile(job); break;
      case "ig_videos": out = await runVideos(job); break;
      case "ig_discover": out = await runDiscover(job); break;
      case "ig_check": out = await runCheck(job); break;
      case "ig_probe": out = await runProbe(job); break;
      case "page_read": out = await runPageRead(job); break;
    }
    await post(`/jobs/${job.id}/done`, out);
    badge("");
  } catch (e) {
    if (e instanceof Dropped || e instanceof Canceled) {
      badge(e instanceof Canceled && e.message === "confirm_account" ? "!" : "");
    } else if (e instanceof IgStop) {
      if (e.code === "signed_out" && job.kind !== "page_read") await put({ig: {signed_in: false, checked_at: Date.now()}});
      const body = {code: e.code};
      if (Number.isInteger(e.http_status)) body.http_status = e.http_status;
      if (typeof e.message === "string" && e.message !== e.code) body.message = e.message.slice(0, 300);
      if (Array.isArray(e.raw_keys)) body.raw_keys = e.raw_keys.slice(0, 50).map(String);
      if (Number.isFinite(e.cooldown_s)) body.cooldown_s = e.cooldown_s;
      if (job.kind === "ig_videos" && curJob && curJob.state) {   // the server counts these Instagram calls too
        body.media_info = curJob.state.media_info || 0;
      }
      const loBlocked = e.code === "lo_blocked";   // a logged-out block: never a logged-in cooldown
      if (loBlocked && e.lo) body.lo = {class: String(e.lo.class), strategy: String(e.lo.strategy), http: e.lo.http || 0};
      const log = pendingLog();
      if (log) body.fetch_log = log;
      let until = null;
      try {
        const r = await api(`/jobs/${job.id}/fail`, body);
        if (r.status >= 500) throw new ServerDown("server " + r.status);
        if (r.data && r.data.lo) await mergeServerLo(r.data.lo);
        until = !loBlocked && r.data && r.data.cooldown_until ? Date.parse(r.data.cooldown_until) : null;
      } catch (e2) {
        // ReelVault is away: still stop reading Instagram for the 5.7 time. The lease runs out on the server and
        // the job comes back later, so the local copy is dropped.
        if (loBlocked && e2 instanceof ServerDown) e2 = null;      // the local logged-out cooldown is already set
        else if (!(e2 instanceof ServerDown) || !LOCAL_COOLDOWN_S[e.code]) throw e2;
        if (!e2) { badge(""); return; }
        until = Date.now() + LOCAL_COOLDOWN_S[e.code] * 1000;
        await put({cooldown: {until, reason: e.code, local_only: true}});
        until = null;
      }
      if (until) await put({cooldown: {until, reason: e.code}});
      badge(["signed_out", "permission_denied"].includes(e.code) ? "!" : "");
    } else if (e instanceof ServerDown) {
      // The server is away or refused an upload: resume from storage later, with a growing wait (1, 5, 15 min),
      // then let the server's lease expiry take the job back. Without the wait every 30-second tick re-read the
      // same Instagram page (fix round 2).
      const fails = ((resumed && resumed.server_fails) || 0) + 1;
      if (fails <= SERVER_BACKOFF_S.length) {
        keepLocal = true;
        await put({job: {job, state: curJob ? curJob.state : {}, saved_at: Date.now(), server_fails: fails,
                         resume_after: Date.now() + SERVER_BACKOFF_S[fails - 1] * 1000}});
      }
    } else if (e instanceof Unpaired) {
      badge("!");
    } else {
      console.error("ReelVault job crashed", e);
      await api(`/jobs/${job.id}/fail`, {code: "http_error", message: "extension error: " + String(e && e.message || e).slice(0, 200)}).catch(() => {});
    }
  } finally {
    clearInterval(keepAlive);
    keepAlive = null;
    curJob = null;
    curLayer = null;
    if (!keepLocal) await local.remove("job");
    await local.remove("ui_job");
    chrome.alarms.create("closetab", {delayInMinutes: 2});   // close the reading tab 2 min after the last job
    running = false;
  }
}

// ============================================================================================ loop (5.3)

let ticking = false;
let tickN = 0;

async function hello() {
  const r = await api("/hello", {version: VERSION, ...((await get("ig")) ? {ig: await get("ig")} : {})});
  if (r.status !== 200 || !r.data) return null;
  const upd = {server_pacing: r.data.pacing || null, hello: {...r.data, at: Date.now()}};
  if (r.data.cooldown_until) upd.cooldown = {until: Date.parse(r.data.cooldown_until), reason: r.data.cooldown_reason || ""};
  await put(upd);
  if (r.data.lo) await mergeServerLo(r.data.lo);
  if (!r.data.cooldown_until) {                   // the server's word wins, except a stop it never heard of
    const cd = await get("cooldown");
    if (!cd || !cd.local_only || cd.until <= Date.now()) await local.remove("cooldown");
  }
  await loadPacing();
  return r.data;
}

async function tick(why = "alarm") {
  if (running || ticking) return;
  ticking = true;
  try {
    const pair = await get("pair");
    if (!pair) { badge(""); return; }
    if (await get("revoked")) { badge("!"); return; }
    await loadPacing();
    if (tickN++ % 5 === 0 || why === "now") {
      const was = await get("cooldown");
      const h = await hello();
      // "Try again" in ReelVault cleared a signed-out stop: check the sign-in now (one call), so the server knows
      // the member is back before it hands out work that needs the login.
      if (h && !h.cooldown_until && was && was.reason === "signed_out" && was.until > Date.now()) await recheckSignIn();
    }
    const stored = await get("job");
    if (stored && stored.job) {
      if (stored.resume_after && stored.resume_after > Date.now()) return;   // waiting after a server failure
      await runJob(stored.job, stored);
    }
    for (let i = 0; i < 25; i++) {               // keep going while the server has work
      const cd = await get("cooldown");
      const inCooldown = cd && cd.until > Date.now();
      loMem = await loState();
      // During an Instagram stop only page reads. Signed out: logged-out pulls and the probe may still run (the
      // server only hands out jobs that need no login). After a logged-out block the login waits too.
      const kinds = (inCooldown && cd.reason !== "signed_out") || loMem.li_not_before > Date.now() ? ["page_read"]
        : inCooldown ? ["ig_pull", "ig_probe", "page_read"] : KINDS;
      const r = await api("/jobs/lease", {kinds, budget: await budget.report(), lo: loReport()});
      if (r.status !== 200 || !r.data) break;
      if (r.data.lo) await mergeServerLo(r.data.lo);
      await put({lease: {reason: r.data.reason || null, at: Date.now()}});
      if (!r.data.job) {
        badge(["confirm_account", "update"].includes(r.data.reason) ? "!" : "");
        if (r.data.reason === "confirm_account") await recheckAccount();
        break;
      }
      await runJob(r.data.job, null);
    }
  } catch (e) {
    if (!(e instanceof Unpaired || e instanceof ServerDown)) console.warn("ReelVault tick failed", e);
  } finally { ticking = false; }
}

// The server holds Instagram jobs while the account the extension reads with is not the confirmed one (5.2). If
// the member switched back in Chrome, only a new sign-in check can show it: at most every 30 min, and only while
// work is waiting (like the signed_out re-check of 5.7), so an idle Chrome still makes no Instagram call.
async function recheckAccount() {
  const h = await get("hello"), igs = await get("ig"), cd = await get("cooldown");
  if (!h || !h.has_work) return;
  if (cd && cd.until > Date.now()) return;                  // no Instagram call during a stop (fix round 2)
  if (igs && Date.now() - (igs.checked_at || 0) < 30 * 60_000) return;
  try { await signInCheck(); await hello(); }
  catch (e) { if (!(e instanceof IgStop)) throw e; await stopWithoutJob(e); }
}

async function recheckSignIn() {
  const igs = await get("ig"), cd = await get("cooldown");
  if ((igs && igs.signed_in) || (cd && cd.until > Date.now())) return;
  try { await signInCheck(); await hello(); }
  catch (e) { if (!(e instanceof IgStop)) throw e; await stopWithoutJob(e); }
}

function deviceName() {
  const ua = navigator.userAgentData;
  const brand = ua && (ua.brands || []).map(b => b.brand).find(b => /Edge|Brave|Chrome|Chromium/.test(b) && !/Not/.test(b));
  const platform = (ua && ua.platform) || "computer";
  return `${(brand || "Chrome").replace("Google ", "").replace("Microsoft ", "")} on ${platform}`.slice(0, 60);
}

async function claim(link) {
  const pl = parsePairLink(link, SERVERS);
  if (!pl.ok) return {ok: false, error: pl.error === "not_ours"
    ? "This link is not from your ReelVault. Don't paste links other people send you."
    : "This doesn't look like a ReelVault link. Copy it again from ReelVault > Settings > Chrome extension."};
  let r;
  try {
    r = await fetch(pl.origin + "/api/ext/v2/pair/claim", {method: "POST", credentials: "omit",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: pl.code, name: deviceName(), version: VERSION, kind: "extension"})});
  } catch { return {ok: false, error: "ReelVault didn't answer. Check your internet, then try again."}; }
  let data = null;
  try { data = await r.json(); } catch { /* ignore */ }
  if (r.status !== 200 || !data || !data.token) {
    return {ok: false, error: (data && data.error) || "That code didn't work. Make a new link in ReelVault and paste it again."};
  }
  // An Instagram stop (cooldown) and the budget survive a new pairing (5.7): Disconnect + Connect must not
  // clear "ReelVault waits until tomorrow".
  const keep = await local.get(["budget", "debug_sent", "cooldown", "ig", "lo"]);
  await local.clear();
  if (data.cooldown_until) keep.cooldown = {until: Date.parse(data.cooldown_until), reason: data.cooldown_reason || ""};
  if (!(keep.cooldown && keep.cooldown.until > Date.now())) delete keep.ig;   // the last check, only during a stop
  await put({...keep, pair: {server: pl.origin, host: pl.host, token: data.token, device_id: data.device_id,
                             user_name: (data.user && data.user.name) || ""}});
  let igs = {signed_in: false};
  try { await hello(); } catch { /* pacing stays at the safe defaults */ }
  const cd = await get("cooldown");
  if (cd && cd.until > Date.now() && cd.reason !== "signed_out") {
    igs = (await get("ig")) || {signed_in: false, waiting: true};   // no Instagram call during a stop
  } else {
    try { igs = await signInCheck(); } catch (e) {
      if (await stopWithoutJob(e)) igs = {signed_in: false, waiting: true, error: e.code};
      else console.warn("sign-in check failed", e);
    }
  }
  try { await hello(); } catch { /* the next tick says hello */ }
  setTimeout(() => tick("now"), 500);
  return {ok: true, name: (data.user && data.user.name) || "", ig: igs, host: pl.host};
}

async function statusForPopup() {
  const s = await local.get(["pair", "ig", "cooldown", "lease", "hello", "ui_job", "revoked"]);
  const L = effPacing;
  const lo = await loState(), now = Date.now();
  return {paired: !!s.pair && !s.revoked, revoked: !!s.revoked, host: s.pair && s.pair.host, server: s.pair && s.pair.server,
          name: s.pair && s.pair.user_name, ig: s.ig || null, budget: await budget.report(),
          limits: {hour_max: L.hour_max, day_max: L.day_max, cdn_day_max: L.cdn_day_max, lo_day_max: L.lo_day_max},
          lo: {cooldown: lo.cooldown_until > now ? {until: lo.cooldown_until, reason: lo.cooldown_reason} : null,
               login_waits_until: lo.li_not_before > now ? lo.li_not_before : null},
          cooldown: s.cooldown && s.cooldown.until > Date.now() ? s.cooldown : null,
          reason: s.lease && s.lease.reason, job: s.ui_job || null, hello: s.hello || null, version: VERSION};
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !String(sender.url || "").startsWith(`chrome-extension://${chrome.runtime.id}/`)) return false;
  (async () => {
    switch (msg && msg.type) {
      case "status": return statusForPopup();
      case "claim": return claim(msg.link);
      case "disconnect": {                        // deletes local storage and calls nothing else (5.2)
        const keep = await local.get(["budget", "cooldown", "ig", "debug_sent", "lo"]);   // a stop outlives Disconnect (5.7)
        if (!(keep.cooldown && keep.cooldown.until > Date.now())) { delete keep.cooldown; delete keep.ig; }
        await local.clear(); await put(keep); await closeReadingTab(); badge("");
        return {ok: true};
      }
      case "grant_result": {
        const f = grants.get(msg.job);
        if (f) f(!!msg.granted);
        return {ok: !!f};
      }
      case "tick": tick("now"); return {ok: true};
      default: return {ok: false};
    }
  })().then(sendResponse, e => sendResponse({ok: false, error: String(e && e.message || e)}));
  return true;
});

chrome.alarms.onAlarm.addListener(a => {
  if (a.name === "tick") tick("alarm");
  if (a.name === "closetab" && !running) closeReadingTab();
});

async function ensureAlarm() {
  if (!(await chrome.alarms.get("tick"))) chrome.alarms.create("tick", {periodInMinutes: 0.5});
}
chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); tick("start"); });
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); tick("start"); });
ensureAlarm();
setTimeout(() => tick("start"), 1000);   // a restarted SW resumes a stored job here

// For the automated tests and debugging in chrome://extensions (service worker console only).
self.rv = {tick, hello, statusForPopup, budget, validateJob, effPacing: () => effPacing,
           busy: () => running || ticking, lastLease: () => get("lease"), closeReadingTab, loState,
           docIds: () => docIds};

// ReelVault Members Reader: service worker (DESIGN-SAAS.md 5.2-5.7).
// Loop: every 30 s (alarm) ask the paired ReelVault server for work (server-only call; idle Chrome makes ZERO
// Instagram calls). One job at a time, one Instagram request at a time, in the extension's own pinned reading tab.
import {SERVERS, BUILD} from "./config.js";
import {IG, CDN_DOMAINS, isCdnUrl, WARNING_RE, CHALLENGE_RE} from "./endpoints.js";
import {parseIg} from "./bigint.js";
import {validateJob, parsePairLink, IG_KINDS, KINDS, isPk, isCursor} from "./validate.js";
import {DEFAULTS, merge, Budget, BudgetStop} from "./pacing.js";
import {trimMedia, trimUser, isReel, keepForTarget, videoOf, coverOf, keysTree} from "./trim.js";
import {igFetch, readIds, readSkool, readLinkedIn} from "./reader.js";

const VERSION = chrome.runtime.getManifest().version;
const local = chrome.storage.local;
const session = chrome.storage.session;
const SIX_H = 6 * 3600_000;
const VIDEO_MAX = 50 * 1024 * 1024;
const COVER_MAX = 2 * 1024 * 1024;
const PAGE_CAP = 12;
const DEBUG_ENDPOINTS = ["clipsUser", "profileInfo", "currentUser", "chaining"];

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
async function loadPacing() { effPacing = merge(DEFAULTS, await get("server_pacing"), BUILD); }

const budget = new Budget({get: () => get("budget"), set: v => put({budget: v})},
                          {sleep, limits: () => effPacing});

async function take(kind, opts) {
  try { await budget.take(kind, opts); }
  catch (e) {
    if (e instanceof BudgetStop) throw new IgStop("budget", {cooldown_s: Math.ceil(e.waitMs / 1000), message: e.what});
    throw e;
  }
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

async function progress(done, total, message, extra = {}) {
  if (!curJob) return;
  const igs = await get("ig");
  const r = await api(`/jobs/${curJob.job.id}/progress`,
                      {done, total, message, ...(igs ? {ig: igs} : {}), budget: await budget.report(), ...extra});
  if (r.status === 409 || r.status === 404) throw new Dropped();
  if (r.data && r.data.cancel) throw new Canceled(r.data.reason || "");
  lastBeat = Date.now();
  await put({ui_job: {kind: curJob.job.kind, message, done, total, at: Date.now()}});
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
  const apiPath = path.startsWith("/api/");
  const where = String(r.url || "");
  if (r.type === "opaqueredirect" || (r.status >= 300 && r.status < 400)) throw new IgStop("signed_out", {http_status: r.status, message: "redirect"});
  if (/\/accounts\/suspended/.test(where)) throw new IgStop("challenge", {message: "Instagram sent /accounts/suspended/"});
  if (/\/challenge\//.test(where)) throw new IgStop("challenge", {http_status: r.status});
  if (/\/accounts\/login/.test(where)) throw new IgStop("signed_out", {http_status: r.status});
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
async function ig(name, args = [], {kind = "ig", page = false, form = null, allowNonJson = false} = {}) {
  const ep = IG[name];
  const path = ep.path(...args);
  const spec = {method: ep.method, path, appId: IG.appId, asbdId: IG.asbdId};
  if (ep.form) spec.form = ep.form(...(form || []));
  for (let attempt = 0; ; attempt++) {
    await beatIfDue();
    await take(kind, {page: page && attempt === 0});
    const r = await inTab(igFetch, [spec]);
    const res = classify(r, path, {allowNonJson});
    if (!res.retry) return res;
    if (attempt >= 2) throw new IgStop("http_error", {http_status: res.status || null});
    await waitWithBeat(effPacing.retry_ms[Math.min(attempt, effPacing.retry_ms.length - 1)]);
  }
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
      const r = await ig("userInfo", [ids.ds_user_id]);
      if (r.status === 200 && r.data && r.data.user) user = r.data.user;
    }
    if (!user) {
      const r = await ig("currentUser", []);
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
    throw e;
  }
}

// ============================================================================================ jobs

async function runPull(job) {
  const p = job.payload, st = curJob.state;
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
    const res = req.source === "clips"
      ? await ig("clipsUser", [], {form: [pk, req.max_id, st.page_size], page: st.pages > 0, allowNonJson: true})
      : await ig("feedUser", [pk, req.max_id], {page: st.pages > 0});
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
    const body = {page: st.pages + 1, items,
                  paging: {...paging, source: req.source, request_max_id: req.max_id || null}};
    if (account && !st.account_sent) body.account = account;
    const up = (await apiOk(`/jobs/${job.id}/items`, body)).data;
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

async function runProfile(job) {
  const u = await resolveUser(job.payload.handle);
  if (job.payload.purpose === "competitor") await apiOk(`/jobs/${job.id}/items`, {account: u, items: []});
  else await apiOk("/ig-user", {job_id: job.id, user: u});
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

async function runVideos(job) {
  const items = job.payload.items, st = curJob.state;
  st.i ??= 0; st.failed ??= []; st.uploaded ??= 0; st.media_info ??= 0;
  await installRefererRule();
  for (; st.i < items.length; st.i++) {
    const it = items[st.i];
    if (st.media_info >= effPacing.media_info_max) throw new IgStop("budget", {cooldown_s: 3600, message: "media_info"});
    const r = await ig("mediaInfo", [it.ig_pk]);
    st.media_info += 1;
    if (r.status === 400 || r.status === 404) { st.failed.push({reel_id: it.reel_id, reason: "not_found"}); continue; }
    const m = r.data && Array.isArray(r.data.items) ? r.data.items[0] : null;
    if (r.status !== 200 || !m) throw new IgStop("parse_error", {raw_keys: Object.keys(r.data || {}).slice(0, 50)});
    const mpk = sid(String(m.id || "").split("_")[0]) || sid(m.pk);
    if (mpk !== it.ig_pk) { st.failed.push({reel_id: it.reel_id, reason: "mismatch"}); continue; }
    const v = videoOf(m);
    if (!v || !isCdnUrl(v.url)) { st.failed.push({reel_id: it.reel_id, reason: "no_video"}); continue; }
    await take("cdn");
    const got = await download(v.url, VIDEO_MAX);
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
  return {stop_reason: "no_more", result: {uploaded: st.uploaded, failed: st.failed}};
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
  lastBeat = 0;
  keepAlive = setInterval(() => session.set({hb: Date.now()}), 10_000);   // keep the SW alive for the whole job
  badge("…");
  await saveState();
  let keepLocal = false;
  try {
    const total = job.kind === "ig_pull" ? job.payload.target : (job.payload.items || job.payload.candidates || [1]).length;
    if (IG_KINDS.includes(job.kind)) {
      let igs = await get("ig");
      if (!igs || !igs.signed_in || Date.now() - (igs.checked_at || 0) > SIX_H) igs = await signInCheck();
      if (!igs.signed_in) throw new IgStop("signed_out");
    }
    await progress(0, total, resumed ? "Going on where it stopped" : "Starting");
    let out;
    switch (job.kind) {
      case "ig_pull": out = await runPull(job); break;
      case "ig_profile": out = await runProfile(job); break;
      case "ig_videos": out = await runVideos(job); break;
      case "ig_discover": out = await runDiscover(job); break;
      case "ig_check": out = await runCheck(job); break;
      case "page_read": out = await runPageRead(job); break;
    }
    await apiOk(`/jobs/${job.id}/done`, out);
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
      try {
        const r = await api(`/jobs/${job.id}/fail`, body);
        const until = r.data && r.data.cooldown_until;
        if (until) await put({cooldown: {until: Date.parse(until), reason: e.code}});
      } catch (e2) { if (e2 instanceof ServerDown) keepLocal = false; }
      badge(["signed_out", "permission_denied"].includes(e.code) ? "!" : "");
    } else if (e instanceof ServerDown) {
      keepLocal = true;                           // the server is away: resume from storage on a later tick
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
  if (!r.data.cooldown_until) await local.remove("cooldown");
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
    if (tickN++ % 5 === 0 || why === "now") await hello();
    const stored = await get("job");
    if (stored && stored.job) await runJob(stored.job, stored);
    for (let i = 0; i < 25; i++) {               // keep going while the server has work
      const cd = await get("cooldown");
      const inCooldown = cd && cd.until > Date.now();
      const r = await api("/jobs/lease", {kinds: inCooldown ? ["page_read"] : KINDS, budget: await budget.report()});
      if (r.status !== 200 || !r.data) break;
      await put({lease: {reason: r.data.reason || null, at: Date.now()}});
      if (!r.data.job) { badge(r.data.reason === "confirm_account" ? "!" : ""); break; }
      await runJob(r.data.job, null);
    }
  } catch (e) {
    if (!(e instanceof Unpaired || e instanceof ServerDown)) console.warn("ReelVault tick failed", e);
  } finally { ticking = false; }
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
  const keep = await local.get(["budget", "debug_sent"]);
  await local.clear();
  await put({...keep, pair: {server: pl.origin, host: pl.host, token: data.token, device_id: data.device_id,
                             user_name: (data.user && data.user.name) || ""}});
  let igs = {signed_in: false};
  try { await hello(); } catch { /* pacing stays at the safe defaults */ }
  try { igs = await signInCheck(); } catch (e) { console.warn("sign-in check failed", e); }
  try { await hello(); } catch { /* the next tick says hello */ }
  setTimeout(() => tick("now"), 500);
  return {ok: true, name: (data.user && data.user.name) || "", ig: igs, host: pl.host};
}

async function statusForPopup() {
  const s = await local.get(["pair", "ig", "cooldown", "lease", "hello", "ui_job", "revoked"]);
  const L = effPacing;
  return {paired: !!s.pair && !s.revoked, revoked: !!s.revoked, host: s.pair && s.pair.host, server: s.pair && s.pair.server,
          name: s.pair && s.pair.user_name, ig: s.ig || null, budget: await budget.report(),
          limits: {hour_max: L.hour_max, day_max: L.day_max, cdn_day_max: L.cdn_day_max},
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
        const keep = await local.get(["budget"]);
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
self.rv = {tick, hello, statusForPopup, budget, validateJob, effPacing: () => effPacing};

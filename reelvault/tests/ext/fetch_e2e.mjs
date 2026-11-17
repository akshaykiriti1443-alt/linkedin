// End-to-end test of the fetch engine in the extension (DESIGN-FETCH.md 3.5).
//
//   node --test tests/ext/fetch_e2e.mjs                (or: python -m pytest tests/ext/test_ext_e2e.py)
//
// Chromium + the unpacked extension + the fake Instagram (tests/ext/fake_ig_server.py, with its logged-out answers
// and its cookie detector) + a small ReelVault CONTRACT SERVER inside this file. The contract server answers the
// extension API exactly as DESIGN-FETCH.md sections 4.1 and 4.2 say (plan at lease time, logged-out uploads,
// stage_done -> stop or resume, lo_blocked, probe rows). So these tests prove the EXTENSION side of the contract
// and do not wait for the real server. tests/ext/ext_e2e.mjs keeps testing the logged-in pull with the real app.
//
// Proven here: an auto pull reads logged out with NO cookies; a partial read is topped up to 100 with the login;
// blocks fall back; logged_out_only never uses the login; logged_in_only never reads logged out; the probe posts
// 11 rows; one Instagram request at a time across both layers; the lsd token and cursors never reach ReelVault.
//
// Env: RV_PYTHON, RV_E2E_PORT_BASE (default 9140: contract server = base+5, fake Instagram = base+7), RV_E2E_TMP.
import test, {before, after} from "node:test";
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdtempSync, mkdirSync, cpSync, readFileSync, writeFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import http from "node:http";
import https from "node:https";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";

const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require("playwright")); } catch {
  ({chromium} = createRequire(path.join(process.env.NODE_PATH || "/usr/lib/node_modules", "x.js"))("playwright"));
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const PY = process.env.RV_PYTHON || "python3";
const BASE = Number(process.env.RV_E2E_PORT_BASE || 9140);
const RV_PORT = BASE + 5, IG_PORT = BASE + 7;
const RV = `http://127.0.0.1:${RV_PORT}`;
const TMP = process.env.RV_E2E_TMP || mkdtempSync(path.join(tmpdir(), "rv-fetch-e2e-"));
const PK = "25025320", VIEWER = "61000777";
const CSRF = "csrfTOKENabc123", SESSIONID = "SESSIONsecret987", LSD = "AVqFixtureLsd01";
const LO_IDS = ["lo_profile_page", "lo_reels_page", "lo_reels_gql", "lo_web_profile", "lo_posts_gql", "lo_post_gql", "lo_media_gql", "lo_embed"];
// Short test gaps (only a BUILD = "test" extension takes them). The proportions of DESIGN-FETCH.md 2.4 stay:
// logged-out gaps are longer than logged-in gaps, the layer gap is the longest.
const PACING = {gap_ms: [50, 100], page_gap_ms: [50, 100], cdn_gap_ms: [50, 100], retry_ms: [200, 400], hour_max: 120, day_max: 400,
                media_info_max: 30, cdn_day_max: 150, discover_day_max: 60, page_read_day_max: 5,
                lo_gap_ms: [90, 160], lo_page_gap_ms: [150, 220], layer_gap_ms: [700, 900], lo_hour_max: 60, lo_day_max: 200,
                lo_detail_max: 36, probe_day_max: 3};

const procs = [];
let ctx, sw, extId, server;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const nowS = () => Date.now() / 1000;
const iso = ms => new Date(ms).toISOString();

// ============================================================================================== contract server

// What the real server does per DESIGN-FETCH.md 4.1-4.2, kept as small as the contract allows.
const C = {jobs: [], requests: [], reels: new Map(), signedIn: true, off: [], docIds: null, n: 0,
           liWaitMs: 1500, loReply: null, cooldown: null};
const B32 = "abcdefghijklmnopqrstuvwxyz234567";
const rid = prefix => {                 // a unique rv id: the counter in base32 letters, padded to 16
  let n = C.n++, s = "";
  do { s = B32[n % 32] + s; n = Math.floor(n / 32); } while (n);
  return `${prefix}_${s.padStart(16, "a")}`;
};

function addJob(kind, payload, extra = {}) {
  const j = {id: rid("job"), kind, payload, state: "waiting", run_after: 0, uploads: [], rows: [], fails: [], result: null,
             skip_lo: false, layers: {logged_out: {reels: 0, calls: 0}, logged_in: {reels: 0, pages: 0}}, mine: new Set(), ...extra};
  C.jobs.push(j);
  return j;
}
const addPull = (read = "auto", mode = "first", extra = {}) =>
  addJob("ig_pull", {account_id: "acc_abcdefghijklmnop", handle: "scaleit.demo", mode, target: 100, ...extra}, {read});

function planFor(j, body) {             // 4.1: the plan is written at lease time
  const lo = body.lo || {};
  const cooling = lo.cooldown_until && Date.parse(lo.cooldown_until) > Date.now();
  const off = [...C.off, ...Object.keys(lo.strategy_off || {})];
  const strategies = LO_IDS.filter(s => !off.includes(s));
  const read = j.read || "auto";
  const p = {lo: read !== "logged_in_only" && !cooling && !j.skip_lo && !(j.payload.mode === "next" && read === "auto") && strategies.length > 0,
             li: read !== "logged_out_only" && C.signedIn,
             lo_budget: read === "logged_out_only" ? 80 : j.payload.mode === "refresh" ? 16 : 40, strategies};
  if (j.kind === "ig_videos") { p.lo = p.lo && strategies.includes("lo_post_gql"); p.lo_budget = 30; }
  if (!p.lo) { p.lo_budget = 0; p.strategies = []; }
  return p;
}

function lease(body) {
  const kinds = Array.isArray(body.kinds) ? body.kinds : [];
  for (const j of C.jobs) {
    if (j.state !== "waiting" || !kinds.includes(j.kind) || j.run_after > Date.now()) continue;
    const payload = {...j.payload};
    if (["ig_pull", "ig_profile", "ig_videos"].includes(j.kind) && !j.raw) {
      const plan = j.plan || planFor(j, body);
      if (!plan.lo && !plan.li) continue;                       // it waits (4.1)
      payload.read = j.read || "auto"; payload.plan = plan; j.leasedPlan = plan;
    }
    j.state = "running"; j.leases = (j.leases || 0) + 1;
    return {job: {id: j.id, kind: j.kind, payload, lease_until: iso(Date.now() + 300_000), attempt: j.leases}};
  }
  return {job: null, poll_after_s: 30};
}

function ingest(j, body) {              // 4.2 and 2.2
  j.uploads.push(body);
  const src = body.paging && body.paging.source;
  const target = j.payload.target || 100;
  let accepted = 0;
  for (const it of body.items || []) {
    if (j.mine.size >= target && !C.reels.has(src === "lo" && it.shape === "gql" ? it.id : it.pk)) continue;   // stop at the target
    if (src === "lo") {
      const pk = it.shape === "gql" ? it.id : it.pk;
      const views = it.shape === "gql" ? (it.video_play_count ?? null) : (it.play_count ?? null);
      const partial = views === null && !it.like_and_view_counts_disabled;
      const known = C.reels.get(pk);
      if (!known) { C.reels.set(pk, {pk, layer: "logged_out", strategy: it.strategy, partial, shape: it.shape}); accepted++; j.mine.add(pk); j.layers.logged_out.reels++; }
      else if (known.partial && !partial) known.partial = false;
    } else {
      const known = C.reels.get(it.pk);
      if (known) { known.partial = false; if (known.layer === "logged_out") known.topped = true; }   // a logged-in upsert always wins
      else { C.reels.set(it.pk, {pk: it.pk, layer: "logged_in", partial: false}); accepted++; j.mine.add(it.pk); j.layers.logged_in.reels++; }
    }
  }
  if (src === "lo") j.layers.logged_out.calls += (body.fetch_log || []).filter(l => l.strategy.startsWith("lo_")).length;
  else j.layers.logged_in.pages++;
  const reached = j.mine.size >= target;
  if (src === "lo" && body.paging.stage_done) {
    j.lo_stop = body.paging.lo_stop;
    const anyPartial = [...j.mine].some(pk => C.reels.get(pk).partial);
    const plan = j.leasedPlan || {};
    if ((j.read || "auto") === "auto" && plan.li && (!reached || anyPartial)) {
      return {ok: true, accepted, known_hit: false, stop: false,
              resume: {source: "clips", max_id: null, skip_known: true, layer: "logged_in"}};
    }
    return {ok: true, accepted, known_hit: false, stop: true};
  }
  const partialLeft = [...j.mine].some(pk => C.reels.get(pk).partial);
  return {ok: true, accepted, known_hit: false, stop: src === "lo" ? reached : reached && !partialLeft};
}

function fail(j, body) {
  j.fails.push(body);
  if (body.code === "lo_blocked") {
    const cls = (body.lo && body.lo.class) || "rate";
    const until = Date.now() + (cls === "challenge" ? 24 : 6) * 3600_000;
    const lo = {cooldown_until: iso(until), cooldown_reason: cls, strategy_off: {}};
    if ((j.read || "auto") === "auto" && C.signedIn) {            // back to waiting; the login waits (15 min in the contract)
      j.state = "waiting"; j.skip_lo = true; j.run_after = Date.now() + C.liWaitMs;
    } else j.state = "closed";
    return {ok: true, lo};
  }
  j.state = "failed"; j.error_code = body.code;
  return {ok: true};
}

function route(method, url, body) {
  const m = url.match(/^\/api\/ext\/v2(\/.*)$/);
  if (!m || method !== "POST") return [404, {error: "not found"}];
  const p = m[1];
  if (p === "/hello") {
    return [200, {ok: true, min_version: "2.0.0", latest_version: "2.1.0", update_url: RV + "/x.zip", pacing: PACING, ig_confirmed: true,
                  ig_username: "member.reader", has_work: C.jobs.some(j => j.state === "waiting"),
                  fetch: {layers: ["logged_out", "logged_in"], strategies: LO_IDS, off: C.off, doc_ids: C.docIds || {}, probe_handle: "instagram"},
                  ...(C.loReply ? {lo: C.loReply} : {}),
                  // The device's logged-in stop, as the real hello sends it (a "Try again" clears it on the server).
                  ...(C.cooldown && C.cooldown.until > Date.now() ? {cooldown_until: iso(C.cooldown.until), cooldown_reason: C.cooldown.reason} : {})}];
  }
  if (p === "/jobs/lease") return [200, lease(body)];
  if (p === "/debug-sample" || p === "/ig-user") return [200, {ok: true}];
  const jm = p.match(/^\/jobs\/([a-z0-9_]+)\/(progress|items|probe|done|fail|video)$/);
  const j = jm && C.jobs.find(x => x.id === jm[1]);
  if (!j) return [404, {error: "not found"}];
  if (j.state !== "running") return [409, {error: "lease gone"}];
  switch (jm[2]) {
    case "progress": j.progress = body; return [200, {ok: true, cancel: false}];
    case "items": return [200, ingest(j, body)];
    case "probe": j.rows.push(...(body.rows || [])); j.uploads.push(body); return [200, {ok: true}];
    case "done": j.state = "done"; j.result = body; return [200, {ok: true}];
    case "fail": return [200, fail(j, body)];
    case "video": j.videos = (j.videos || 0) + 1; return [200, {ok: true}];   // multipart: only counted here
  }
  return [404, {error: "not found"}];
}

function startContractServer() {
  server = http.createServer((req, res) => {
    // The extension's service worker calls from chrome-extension://<id>: answer the CORS preflight like the app does.
    const cors = {"Access-Control-Allow-Origin": req.headers.origin || "*", "Access-Control-Allow-Headers": "authorization, content-type",
                  "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Max-Age": "600"};
    if (req.method === "OPTIONS") { res.writeHead(204, cors); res.end(); return; }
    let raw = "";
    req.on("data", c => { raw += c; });
    req.on("end", () => {
      let body = {};
      try { body = raw ? JSON.parse(raw) : {}; } catch { body = {}; }
      const auth = req.headers.authorization === "Bearer rvd_contract_test";
      const [status, out] = auth ? route(req.method, req.url, body) : [401, {error: "unauthorized"}];
      C.requests.push({method: req.method, path: req.url, headers: req.headers, body: raw, json: body, status, t: Date.now()});
      const text = JSON.stringify(out);
      res.writeHead(status, {...cors, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(text)});
      res.end(text);
    });
  });
  return new Promise(resolve => server.listen(RV_PORT, "127.0.0.1", resolve));
}

// ============================================================================================== helpers

async function until(fn, {ms = 30_000, every = 120, what = "condition"} = {}) {
  const end = Date.now() + ms;
  while (Date.now() < end) { const v = await fn(); if (v) return v; await sleep(every); }
  throw new Error(`timed out waiting for ${what}`);
}

function igCtl(p, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : "";
    const req = https.request({host: "127.0.0.1", port: IG_PORT, path: p, method: body ? "POST" : "GET", rejectUnauthorized: false,
      headers: {"Content-Type": "application/json", "Content-Length": Buffer.byteLength(data), ...headers}},
    res => { let t = ""; res.on("data", c => { t += c; }); res.on("end", () => { try { resolve({status: res.statusCode, data: JSON.parse(t)}); } catch { resolve({status: res.statusCode, data: t}); } }); });
    req.on("error", reject);
    req.end(data);
  });
}
const scenario = (...names) => igCtl("/__ctl/scenario", {set: names});
const igLog = async () => (await igCtl("/__ctl/log")).data.log;
const leaks = async () => (await igCtl("/__ctl/log")).data.leaks;
// robots.txt is the reading tab's own page; Chrome asks for /favicon.ico by itself when it opens that tab.
const since = (log, t0) => log.filter(e => e.t >= t0 && e.host === "www.instagram.com" && !["/robots.txt", "/favicon.ico"].includes(e.path));
const isLo = e => !!e.lo;
const swEval = (fn, arg) => sw.evaluate(fn, arg);
const tickNow = () => swEval(() => { self.rv.tick("now"); return true; });
const idle = () => until(() => swEval(() => self.rv.busy()).then(b => !b), {what: "the extension to finish its work"});
const storage = key => swEval(async k => (await chrome.storage.local.get(k))[k], key);

async function runUntil(j, states = ["done", "failed", "closed"], ms = 90_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (states.includes(j.state)) { await idle(); return j; }
    await tickNow();
    await sleep(300);
  }
  throw new Error(`job ${j.kind} stayed ${j.state}: ${JSON.stringify({fails: j.fails, lo_stop: j.lo_stop, uploads: j.uploads.length, progress: j.progress})}`);
}

async function reset({signedIn = true} = {}) {
  await idle();
  for (const j of C.jobs) if (["waiting", "running"].includes(j.state)) j.state = "canceled";
  C.reels.clear(); C.off = []; C.docIds = null; C.signedIn = signedIn; C.loReply = null; C.liWaitMs = 1500; C.cooldown = null;
  await scenario();
  await swEval(async signed => {
    await chrome.storage.local.remove(["cooldown", "job", "budget", "lo", "ui_job"]);
    await chrome.storage.local.set({ig: signed ? {signed_in: true, user_id: "61000777", username: "member.reader", checked_at: Date.now()}
                                               : {signed_in: false, checked_at: Date.now()}});
    return true;
  }, signedIn);
}

// The member is signed out of Instagram: the server and the extension both hold the 30-minute signed_out stop.
async function signedOutStop() {
  const until = Date.now() + 30 * 60_000;
  C.cooldown = {until, reason: "signed_out"};
  await swEval(async u => { await chrome.storage.local.set({cooldown: {until: u, reason: "signed_out"}}); return true; }, until);
}

function spawnPy(args, name) {
  const p = spawn(PY, args, {cwd: ROOT, env: {...process.env, PYTHONUNBUFFERED: "1"}, stdio: ["ignore", "pipe", "pipe"]});
  let out = "";
  p.stdout.on("data", d => { out += d; });
  p.stderr.on("data", d => { out += d; if (process.env.RV_E2E_VERBOSE) process.stderr.write(`[${name}] ${d}`); });
  p.on("exit", code => { if (code && code !== 0 && !p.stopping) process.stderr.write(`[${name}] exited ${code}\n${out}\n`); });
  procs.push(p);
  return p;
}

function copyExtension(name) {
  const dir = path.join(TMP, name);
  rmSync(dir, {recursive: true, force: true});
  cpSync(path.join(ROOT, "extension-members"), dir, {recursive: true});
  const cfg = readFileSync(path.join(dir, "config.js"), "utf8")
    .replace(/^export const SERVERS = .*$/m, `export const SERVERS = ${JSON.stringify([RV])};`)
    .replace(/^export const BUILD = .*$/m, `export const BUILD = "test";`);
  writeFileSync(path.join(dir, "config.js"), cfg);
  return dir;
}

// Every logged-out request: no cookie, no CSRF token, no claim header, same-origin, from the reading tab.
function assertAnon(e) {
  assert.deepEqual(e.cookies, {}, `${e.path} carried cookies`);
  for (const h of ["cookie", "x-csrftoken", "x-ig-www-claim", "authorization"]) assert.equal(e.headers[h], undefined, `${e.path} sent ${h}`);
  assert.equal(e.headers["sec-fetch-site"], "same-origin");
  assert.ok(!/robots\.txt/.test(e.headers.referer || ""), "no robots.txt Referer");
  assert.ok(!e.leak);
}
function assertSerial(log) {            // one Instagram request at a time, across both layers
  const s = [...log].sort((a, b) => a.t - b.t);
  for (let i = 1; i < s.length; i++) assert.ok(s[i].t >= s[i - 1].t_end, `${s[i].path} started before ${s[i - 1].path} ended`);
  return s;
}
function assertNoSecrets(j) {           // the lsd token, cursors and cookies never reach ReelVault
  for (const q of C.requests.filter(r => r.path.includes(j.id))) {
    assert.ok(!q.body.includes(LSD), `${q.path} holds the lsd token`);
    assert.ok(!q.body.includes(SESSIONID) && !q.body.includes(CSRF), `${q.path} holds a cookie value`);
    assert.ok(!/"(sessionid|csrftoken|cookie|lsd|end_cursor|after)"\s*:/i.test(q.body), `${q.path} has a forbidden field`);
    assert.equal(q.headers.cookie, undefined);
  }
}

// ============================================================================================== setup

before(async () => {
  mkdirSync(TMP, {recursive: true});
  const extDir = copyExtension("ext-fetch");
  rmSync(path.join(TMP, "profile-fetch"), {recursive: true, force: true});
  await startContractServer();
  spawnPy([path.join(HERE, "fake_ig_server.py"), "--port", String(IG_PORT), "--cert-dir", path.join(TMP, "cert-fetch")], "fake-ig");
  await until(async () => { try { return (await igCtl("/__ctl/log")).data.log !== undefined; } catch { return false; } },
              {what: "the fake Instagram to start"});
  const hosts = ["www.instagram.com", "scontent.cdninstagram.com"];
  ctx = await chromium.launchPersistentContext(path.join(TMP, "profile-fetch"), {
    channel: "chromium", headless: true, ignoreHTTPSErrors: true,
    args: [`--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`, "--no-proxy-server",
           `--host-resolver-rules=${hosts.map(h => `MAP ${h} 127.0.0.1:${IG_PORT}`).join(", ")}`, "--ignore-certificate-errors"]});
  sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker", {timeout: 30_000});
  extId = new URL(sw.url()).host;
  // The member is signed in to Instagram in this Chrome. A logged-out read must not send any of these.
  await ctx.addCookies([
    {name: "sessionid", value: SESSIONID, domain: ".instagram.com", path: "/", secure: true, httpOnly: true},
    {name: "ds_user_id", value: VIEWER, domain: ".instagram.com", path: "/", secure: true},
    {name: "csrftoken", value: CSRF, domain: ".instagram.com", path: "/", secure: true}]);
  // Paired with the contract server (the pairing flow itself is tested in ext_e2e.mjs).
  await swEval(async a => {
    await chrome.storage.local.set({pair: {server: a.rv, host: new URL(a.rv).host, token: "rvd_contract_test", device_id: "dev_contract", user_name: "Mia Member"},
                                    ig: {signed_in: true, user_id: a.viewer, username: "member.reader", checked_at: Date.now()}});
    return true;
  }, {rv: RV, viewer: VIEWER});
  await swEval(() => self.rv.hello().then(() => true));
});

after(async () => {
  try { if (ctx) await ctx.close(); } catch { /* already gone */ }
  if (server) server.close();
  for (const p of procs) { p.stopping = true; p.kill("SIGTERM"); }
  await sleep(300);
  for (const p of procs) if (p.exitCode === null) p.kill("SIGKILL");
  if (!process.env.RV_E2E_KEEP && !process.env.RV_E2E_TMP) rmSync(TMP, {recursive: true, force: true});
});

// ============================================================================================== the detector itself

test("the fake Instagram's cookie detector: a logged-out request with a cookie or CSRF token gets 400 cookie_leak", async () => {
  const a = await igCtl("/scaleit.demo/reels/", null, {Host: "www.instagram.com", Cookie: "sessionid=x"});
  assert.equal(a.status, 400);
  assert.equal(a.data.message, "cookie_leak");
  const b = await igCtl("/api/graphql", {}, {Host: "www.instagram.com", "X-CSRFToken": "x"});
  assert.equal(b.data.message, "cookie_leak");
  const c = await igCtl("/scaleit.demo/", null, {Host: "www.instagram.com", Cookie: "csrftoken=x"});
  assert.equal(c.data.message, "cookie_leak", "a request without X-IG-WWW-Claim is a logged-out request");
  assert.equal(await leaks(), 3);
  const ok = await igCtl("/scaleit.demo/", null, {Host: "www.instagram.com"});
  assert.equal(ok.status, 200);
  assert.match(ok.data, /xig_user_by_username/);
  await igCtl("/__ctl/reset", {});
  assert.equal(await leaks(), 0);
});

test("hello: the extension is 2.1.0 and takes the server's logged-out pacing and doc ids", async () => {
  const hello = C.requests.find(r => r.path.endsWith("/hello"));
  assert.equal(hello.json.version, "2.1.0");
  const eff = await swEval(() => self.rv.effPacing());
  assert.deepEqual(eff.lo_gap_ms, PACING.lo_gap_ms);
  assert.deepEqual(eff.layer_gap_ms, PACING.layer_gap_ms);
  assert.equal(eff.lo_day_max, 200);
  assert.deepEqual(await swEval(() => self.rv.docIds().lo_reels_gql), ["29285972954340351", "7845543455542541"]);
  const mf = JSON.parse(readFileSync(path.join(ROOT, "extension-members", "manifest.json"), "utf8"));
  assert.equal(mf.description, "Reads public reels of the accounts you choose. It reads without your login first. Your Instagram login never leaves this browser.");
  assert.deepEqual(mf.permissions, ["scripting", "storage", "alarms", "declarativeNetRequestWithHostAccess"], "no new permission");
});

// ============================================================================================== logged-out success

let firstPull;

test("auto pull, logged out works: 100 reels with zero cookies and zero logged-in calls", async () => {
  await reset();
  const t0 = nowS();
  const j = await runUntil(addPull("auto"));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.equal(j.result.stop_reason, "target_reached");
  assert.deepEqual(j.leasedPlan, {lo: true, li: true, lo_budget: 40, strategies: LO_IDS});
  const log = since(await igLog(), t0);
  assert.deepEqual(log.map(e => e.lo), ["profile_page", "reels_page", ...Array(8).fill("gql")],
                   `profile, reels page, 8 reels pages of 12: ${log.map(e => `${e.method} ${e.path}`).join(", ")}`);
  log.forEach(assertAnon);
  assert.ok(!log.some(e => e.path.startsWith("/api/v1/")), "no sign-in check and no logged-in call for a logged-out pull");
  assert.equal(await leaks(), 0);
  assert.equal(j.mine.size, 100);
  assert.ok([...C.reels.values()].every(r => r.layer === "logged_out" && !r.partial));
  const timeline = (await igCtl("/__ctl/timeline")).data.pks;
  assert.deepEqual([...j.mine], timeline.slice(0, 100), "the 100 newest reels, in order, with exact 19-digit ids");
  firstPull = {j, t0, log};
});

test("the logged-out requests: reading tab, right paths, headers, forms and referrers", async () => {
  const {log} = firstPull;
  const [page, reels, ...gql] = log;
  assert.equal(page.path, "/scaleit.demo/");
  assert.equal(page.method, "GET");
  assert.match(page.headers.accept, /^text\/html,application\/xhtml\+xml/);
  assert.equal(page.headers.referer, "https://www.instagram.com/");
  assert.equal(page.headers["x-ig-app-id"], undefined, "a page load sends no API header");
  assert.equal(reels.path, "/scaleit.demo/reels/");
  for (const [i, e] of gql.entries()) {
    assert.equal(e.path, "/graphql/query");
    assert.equal(e.method, "POST");
    assert.equal(e.headers.origin, "https://www.instagram.com");
    assert.equal(e.headers.referer, "https://www.instagram.com/scaleit.demo/");
    assert.equal(e.headers["x-ig-app-id"], "936619743392459");
    assert.equal(e.headers["x-asbd-id"], "359341");
    assert.equal(e.headers["x-fb-lsd"], LSD);
    assert.equal(e.headers["x-fb-friendly-name"], "PolarisProfileReelsTabContentQuery_connection");
    assert.equal(e.headers["cache-control"], "no-cache", "cache no-store: Chrome asks for a fresh copy");
    assert.deepEqual(Object.keys(e.form).sort(), ["doc_id", "fb_api_caller_class", "fb_api_req_friendly_name", "lsd", "server_timestamps", "variables"]);
    assert.equal(e.form.doc_id, "29285972954340351");
    const v = JSON.parse(e.form.variables);
    assert.equal(v.data.target_user_id, PK);
    assert.equal(v.first, 12);
    assert.ok(v.after, `reels page ${i + 2} uses the cursor of the page before`);
  }
  assert.equal(new Set(gql.map(e => JSON.parse(e.form.variables).after)).size, 8);
});

test("pacing: one request at a time, logged-out gaps, counted in both budgets", async () => {
  const s = assertSerial(firstPull.log);
  for (let i = 1; i < s.length; i++) {
    const gap = (s[i].t - s[i - 1].t) * 1000;
    const min = i >= 2 ? PACING.lo_page_gap_ms[0] : PACING.lo_gap_ms[0];
    assert.ok(gap >= min - 5, `gap ${gap.toFixed(0)} ms before ${s[i].path} is under ${min} ms`);
  }
  const report = await swEval(() => self.rv.budget.report());
  assert.deepEqual(report, {hour_used: 10, day_used: 10, cdn_day_used: 0, lo_hour_used: 10, lo_day_used: 10});
  const leaseBody = C.requests.filter(r => r.path.endsWith("/jobs/lease")).at(-1).json;
  assert.ok(leaseBody.kinds.includes("ig_probe") && leaseBody.kinds.includes("ig_pull"));
  assert.equal(leaseBody.budget.lo_day_used, 10);
  assert.deepEqual(leaseBody.lo, {cooldown_until: null, strategy_off: {}});
});

test("uploads: layer, account once, shapes, fetch log; the lsd token and cursors never reach ReelVault", async () => {
  const {j} = firstPull;
  assert.equal(j.uploads.length, 9, "the server said stop at the target: no stage_done call");
  const [a, b] = j.uploads;
  assert.equal(a.layer, "logged_out");
  assert.equal(a.account.pk, PK);
  assert.equal(a.account.followers, 182000);
  assert.equal(b.account, undefined);
  assert.deepEqual(a.paging, {source: "lo", strategy: "lo_reels_page", more_available: true, stage_done: false, lo_stop: null});
  assert.equal(b.paging.strategy, "lo_reels_gql");
  for (const u of j.uploads) {
    assert.equal(u.items.length, 12);
    assert.equal(u.page, undefined);
    for (const it of u.items) {
      assert.equal(it.shape, "v1");
      assert.match(it.pk, /^\d{19}$/);
      assert.equal(it.owner.pk, PK);
      assert.equal(typeof it.play_count, "number");
      assert.equal(it.taken_at, null, "a sparse reel: the server derives the date from the pk");
    }
  }
  const logs = [...j.uploads.flatMap(u => u.fetch_log || []), ...(j.result.fetch_log || [])];
  assert.deepEqual(logs.map(l => l.strategy), ["lo_profile_page", "lo_reels_page", ...Array(8).fill("lo_reels_gql")]);
  for (const l of logs) {
    assert.deepEqual(Object.keys(l).sort(), ["at", "code", "http", "items", "ms", "status", "strategy"]);
    assert.deepEqual([l.status, l.code, l.http, l.items], ["ok", null, 200, 12]);
    assert.ok(l.ms >= 0 && Math.abs(l.at - Date.now() / 1000) < 600);
  }
  assert.deepEqual(j.result.result, {recovery: null});
  assertNoSecrets(j);
});

test("the popup says which way it reads, and counts the reads without login", async () => {
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#status:not([hidden])");
  assert.equal(await popup.textContent("#s-budget"), "Instagram reads today: 10 of 400 · without login: 10 of 200 · this hour: 10 of 120 · videos: 0 of 150");
  assert.equal(await popup.isVisible("#s-read"), false);
  await popup.close();

  await reset();
  await scenario("lo_slow");
  const j = addPull("auto");
  const t0 = nowS();
  await tickNow();
  await until(async () => since(await igLog(), t0).length >= 2, {what: "the logged-out stage to start"});
  const p2 = await ctx.newPage();
  await p2.goto(`chrome-extension://${extId}/popup.html`);
  await p2.waitForSelector("#s-read:not([hidden])");
  assert.equal(await p2.textContent("#s-read"), "Reading without your login.");
  await p2.close();
  await scenario();
  await runUntil(j);

  await reset();
  await scenario("slow");
  const k = addPull("logged_in_only");
  const t1 = nowS();
  await tickNow();
  await until(async () => since(await igLog(), t1).length >= 1, {what: "the logged-in pull to start"});
  const p3 = await ctx.newPage();
  await p3.goto(`chrome-extension://${extId}/popup.html`);
  await p3.waitForSelector("#s-read:not([hidden])");
  assert.equal(await p3.textContent("#s-read"), "Reading with your Instagram login.");
  await p3.close();
  await scenario();
  await runUntil(k);
});

// ============================================================================================== partial + top-up

test("auto pull, logged out gives part: the login tops it up to 100 and fills in the views", async () => {
  await reset();
  await scenario("lo_reels_blocked", "lo_no_views");
  const t0 = nowS();
  const j = await runUntil(addPull("auto"));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.equal(j.result.stop_reason, "target_reached");
  const log = assertSerial(since(await igLog(), t0));
  const lo = log.filter(isLo), li = log.filter(e => !isLo(e));
  assert.deepEqual(lo.map(e => e.lo), ["profile_page", "reels_page", "gql", "web_profile", "gql", "gql", "gql", "gql"],
                   "profile, reels page (no data), reels query (401), newest 12, more stubs, then 3 details without views");
  lo.forEach(assertAnon);
  assert.ok(log.indexOf(li[0]) > log.indexOf(lo.at(-1)), "every logged-out call comes before the first logged-in call");
  assert.deepEqual(li.map(e => e.path), ["/api/v1/clips/user/", "/api/v1/clips/user/"], "the cached sign-in check costs no call");
  for (const e of li) {
    assert.equal(e.cookies.sessionid, SESSIONID, "the logged-in top-up sends the member's cookies");
    assert.equal(e.headers["x-csrftoken"], CSRF);
    assert.equal(e.headers["x-ig-www-claim"], "0");
  }
  assert.equal(li[0].form.max_id, undefined, "the top-up starts from the top");
  const gap = (li[0].t - lo.at(-1).t_end) * 1000;
  assert.ok(gap >= PACING.layer_gap_ms[0] - 5, `the layer gap was ${gap.toFixed(0)} ms`);
  assert.equal(await leaks(), 0);

  assert.equal(j.lo_stop, "no_views");
  assert.equal(j.mine.size, 100);
  assert.deepEqual([j.layers.logged_out.reels, j.layers.logged_in.reels, j.layers.logged_in.pages], [13, 87, 2]);
  const reels = [...C.reels.values()];
  assert.equal(reels.filter(r => r.partial).length, 0, "the login cleared every partial reel");
  assert.equal(reels.filter(r => r.topped).length, 13, "known reels got their numbers updated and did not count as new");
  const timeline = (await igCtl("/__ctl/timeline")).data.pks;
  assert.deepEqual(new Set(j.mine), new Set(timeline.slice(0, 100)));
  const up = j.uploads;
  const gql = up[0];
  assert.deepEqual([gql.paging.strategy, gql.items.length, gql.items[0].shape], ["lo_web_profile", 10, "gql"]);
  assert.ok(gql.items.every(i => i.is_video && typeof i.video_view_count === "number" && i.video_play_count === null && i.owner.id === PK));
  const det = up[1];
  assert.deepEqual([det.paging.strategy, det.items.length], ["lo_post_gql", 3]);
  assert.ok(det.items.every(i => i.shape === "v1" && i.play_count === null && i.view_count === null && i.caption && i.video && i.video.url));
  assert.deepEqual(up[2].paging, {source: "lo", strategy: "lo_post_gql", more_available: false, stage_done: true, lo_stop: "no_views"});
  assert.deepEqual(up[2].items, []);
  assert.ok(up.slice(3).every(u => u.paging.source === "clips" && u.layer === undefined && u.items.length === 50));
  const logs = up.flatMap(u => u.fetch_log || []).concat(j.result.fetch_log || []);
  assert.deepEqual(logs.map(l => [l.strategy, l.status, l.code]), [
    ["lo_profile_page", "ok", null], ["lo_reels_page", "partial", "no_data"], ["lo_reels_gql", "blocked", "login_wall"],
    ["lo_web_profile", "ok", null], ["lo_posts_gql", "ok", null], ["lo_post_gql", "partial", "no_views"],
    ["lo_post_gql", "partial", "no_views"], ["lo_post_gql", "partial", "no_views"], ["li_clips", "ok", null], ["li_clips", "ok", null]]);
  assert.deepEqual(logs.slice(-2).map(l => l.items), [50, 50]);
  assertNoSecrets(j);

  const loState = await swEval(() => self.rv.loState());
  assert.ok(loState.strategy_off.lo_reels_gql - Date.now() > 5.9 * 3600_000, "the walled strategy is off for 6 h on this device");
  assert.ok(loState.detail_off_until - Date.now() > 23.9 * 3600_000, "details are off for pulls for 24 h after 3 without views");
  assert.equal(loState.cooldown_until, 0, "one walled strategy does not stop the layer");
  assert.equal(await storage("cooldown"), undefined, "a logged-out block never sets the logged-in cooldown");
  assert.equal((await storage("ig")).signed_in, true);

  // The next lease tells the server; its plan leaves the switched-off strategy out and the pull skips details.
  await scenario("lo_reels_blocked");
  C.reels.clear();
  const t1 = nowS();
  const k = await runUntil(addPull("auto"));
  const leaseBody = C.requests.filter(r => r.path.endsWith("/jobs/lease") && r.json.lo).findLast(r => r.t < Date.parse(k.result ? iso(Date.now()) : 0) || true).json;
  assert.ok(Object.keys(leaseBody.lo.strategy_off).includes("lo_reels_gql"));
  assert.ok(!k.leasedPlan.strategies.includes("lo_reels_gql"));
  const lo2 = since(await igLog(), t1).filter(isLo);
  assert.deepEqual(lo2.map(e => e.lo), ["profile_page", "reels_page", "web_profile"], "no reels query, no detail call");
  assert.equal(k.lo_stop, "no_views");
  assert.equal(k.mine.size, 100);
});

// ============================================================================================== blocked -> fallback

test("login wall on every logged-out read: two calls, then the logged-in pull reads all 100", async () => {
  await reset();
  await scenario("lo_login_wall");
  const t0 = nowS();
  const j = await runUntil(addPull("auto"));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  const log = assertSerial(since(await igLog(), t0));
  const lo = log.filter(isLo);
  assert.deepEqual(lo.map(e => e.lo), ["profile_page", "web_profile"]);
  lo.forEach(assertAnon);
  const login = log.filter(e => e.path.startsWith("/accounts/login"));
  assert.equal(login.length, 1, "the redirect to the login page was followed once");
  assert.deepEqual(login[0].cookies, {}, "even the redirected request carries no cookie");
  assert.equal(j.lo_stop, "blocked");
  assert.deepEqual([j.layers.logged_out.reels, j.layers.logged_in.reels], [0, 100]);
  assert.deepEqual(j.uploads[0].paging, {source: "lo", strategy: "lo_web_profile", more_available: false, stage_done: true, lo_stop: "blocked"});
  assert.deepEqual(j.uploads[0].fetch_log.map(l => [l.strategy, l.status, l.code, l.http]),
                   [["lo_profile_page", "blocked", "login_wall", 200], ["lo_web_profile", "blocked", "login_wall", 401]]);
  const li = log.filter(e => e.path.startsWith("/api/v1/") && !isLo(e));
  assert.equal(li[0].path, "/api/v1/users/web_profile_info/", "no logged-out user: the logged-in pull looks the account up");
  assert.equal(li[0].cookies.sessionid, SESSIONID);
  assert.equal(li.filter(e => e.path === "/api/v1/clips/user/").length, 2);
  assert.equal(await leaks(), 0);

  const loState = await swEval(() => self.rv.loState());
  const hours = (loState.cooldown_until - Date.now()) / 3600_000;
  assert.ok(hours > 1.9 && hours <= 2, `a login wall on 2 strategies stops logged-out reads for 2 h (${hours.toFixed(2)})`);
  assert.equal(loState.cooldown_reason, "login_wall");
  assert.equal(await storage("cooldown"), undefined);

  // The next pull: the lease reports the cooldown, the plan has no logged-out stage, zero logged-out calls.
  await scenario();
  C.reels.clear();
  const t1 = nowS();
  const k = await runUntil(addPull("auto"));
  assert.equal(k.leasedPlan.lo, false);
  assert.equal(since(await igLog(), t1).filter(isLo).length, 0);
  assert.equal(k.mine.size, 100);
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#s-lo:not([hidden])");
  assert.match(await popup.textContent("#s-lo"), /^Instagram asks for a login right now\. ReelVault reads without your login again after /);
  await popup.close();
});

test("rate limit on a logged-out read: lo_blocked, no Instagram call during the wait, then the login goes on", async () => {
  await reset();
  await scenario("lo_rate_after_3");                  // profile, reels page, one reels page, then 429
  const t0 = nowS();
  const j = addPull("auto");
  C.liWaitMs = 4000;
  await until(async () => { await tickNow(); return j.fails.length > 0; }, {what: "the lo_blocked report", every: 300});
  await idle();
  const fail = j.fails[0];
  assert.deepEqual({code: fail.code, lo: fail.lo}, {code: "lo_blocked", lo: {class: "rate", strategy: "lo_reels_gql", http: 429}});
  assert.equal(fail.fetch_log.at(-1).code, "rate");
  assert.equal(j.mine.size, 24, "what was read before the block is kept");
  assert.equal(j.uploads.filter(u => u.paging.stage_done).length, 0);
  let log = since(await igLog(), t0);
  assert.deepEqual(log.map(e => e.lo), ["profile_page", "reels_page", "gql", "gql"], "the 429 was not retried");
  log.forEach(assertAnon);

  const loState = await swEval(() => self.rv.loState());
  const hours = (loState.cooldown_until - Date.now()) / 3600_000;
  assert.ok(hours > 5.9 && hours <= 6, `logged-out reads stop for 6 h (${hours.toFixed(2)})`);
  assert.equal(loState.cooldown_reason, "rate");
  const waitMin = (loState.li_not_before - Date.now()) / 60_000;
  assert.ok(waitMin > 14 && waitMin <= 15, `the login waits 15 minutes (${waitMin.toFixed(1)})`);
  assert.equal(await storage("cooldown"), undefined, "the logged-in cooldown is untouched");
  assert.equal((await storage("ig")).signed_in, true, "signed_in is untouched");

  // The server would hand the job out again after its wait. The extension keeps its own 15 minutes.
  await sleep(C.liWaitMs + 300);
  const n0 = C.requests.length;
  for (let i = 0; i < 3; i++) { await tickNow(); await sleep(300); await idle(); }
  const leases = C.requests.slice(n0).filter(r => r.path.endsWith("/jobs/lease"));
  assert.ok(leases.length >= 3);
  assert.ok(leases.every(r => r.json.kinds.join() === "page_read"), "during the wait only page reads are asked for");
  assert.ok(leases.every(r => Date.parse(r.json.lo.cooldown_until) > Date.now()), "the lease reports the logged-out cooldown");
  assert.equal(j.state, "waiting");
  assert.equal(since(await igLog(), t0).length, log.length, "no Instagram call of either layer during the wait");

  // 15 minutes later (the test moves the clock by clearing the wait): the same pull goes on with the login only.
  await scenario();
  await swEval(async () => { const {lo} = await chrome.storage.local.get("lo"); await chrome.storage.local.set({lo: {...lo, li_not_before: 0}}); return true; });
  await runUntil(j);
  assert.equal(j.state, "done");
  assert.deepEqual(j.leasedPlan.lo, false);
  log = since(await igLog(), t0);
  assert.equal(log.filter(isLo).length, 4, "no logged-out call after the block");
  assert.equal(j.mine.size, 100);
  assert.equal(await leaks(), 0);
  const still = await swEval(() => self.rv.loState());
  assert.ok(still.cooldown_until - Date.now() > 5.8 * 3600_000, "the logged-out cooldown stays");
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#s-lo:not([hidden])");
  assert.match(await popup.textContent("#s-lo"), /^Instagram asks ReelVault to wait\. ReelVault reads without your login again after /);
  await popup.close();
});

// ============================================================================================== read modes

test("logged_out_only: never a logged-in call, even with a partial result; it works while signed out", async () => {
  await reset({signedIn: false});
  await signedOutStop();
  await scenario("lo_reels_blocked");
  const t0 = nowS();
  const j = await runUntil(addPull("logged_out_only"));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.deepEqual(j.leasedPlan, {lo: true, li: false, lo_budget: 80, strategies: LO_IDS});
  const log = assertSerial(since(await igLog(), t0));
  assert.ok(log.every(isLo), `every request is a logged-out read: ${log.filter(e => !isLo(e)).map(e => e.path)}`);
  assert.ok(log.length > 30, `${log.length} logged-out reads: ${log.map(e => e.lo).join(" ")}`);
  log.forEach(assertAnon);
  assert.ok(!log.some(e => e.path.startsWith("/api/v1/clips/") || e.path.includes("/info/") || e.path.includes("current_user")));
  assert.equal(await leaks(), 0);
  const kinds = log.map(e => e.lo);
  assert.deepEqual(kinds.slice(0, 4), ["profile_page", "reels_page", "gql", "web_profile"]);
  const posts = log.filter(e => e.form && e.form.fb_api_req_friendly_name === "PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery_connection");
  assert.equal(posts.length, 3, "at most 3 more pages of stubs");
  const details = log.filter(e => e.path === "/api/graphql" && !e.form.fb_api_req_friendly_name);
  assert.ok(details.length <= 36 && details.length >= 25, `${details.length} detail calls`);
  for (const e of details) {
    const code = JSON.parse(e.form.variables).shortcode;
    assert.equal(e.headers.referer, `https://www.instagram.com/reel/${code}/`);
    assert.equal(e.form.doc_id, "27128499623469141");
    assert.equal(e.headers["x-fb-friendly-name"], undefined);
  }
  assert.equal(j.uploads.at(-1).paging.stage_done, true);
  assert.ok(["no_more", "budget"].includes(j.lo_stop), j.lo_stop);
  assert.equal(j.result.stop_reason, j.lo_stop === "no_more" ? "no_more" : "page_cap");
  assert.ok(j.mine.size >= 30 && j.mine.size < 100, `${j.mine.size} reels: fewer than 100 is fine in this mode`);
  assert.ok([...C.reels.values()].every(r => r.layer === "logged_out"));
  assert.ok(j.uploads.flatMap(u => u.items).every(i => i.shape === "gql" || !!i.video), "details carry the video URL");
  const lease = C.requests.filter(r => r.path.endsWith("/jobs/lease")).at(-1).json;
  assert.deepEqual(lease.kinds, ["ig_pull", "ig_probe", "page_read"], "signed out: only jobs that need no login are asked for");
  assert.equal((await storage("ig")).signed_in, false);
  assertNoSecrets(j);
});

test("a job that breaks the member's read mode is refused before any Instagram call", async () => {
  await reset();
  const t0 = nowS();
  const bad = [
    addJob("ig_pull", {account_id: "acc_abcdefghijklmnop", handle: "scaleit.demo", mode: "first", target: 100, read: "logged_out_only",
                       plan: {lo: true, li: true, lo_budget: 80, strategies: LO_IDS}}, {raw: true}),
    addJob("ig_pull", {account_id: "acc_abcdefghijklmnop", handle: "scaleit.demo", mode: "first", target: 100, read: "logged_in_only",
                       plan: {lo: true, li: true, lo_budget: 40, strategies: LO_IDS}}, {raw: true}),
    addJob("ig_pull", {account_id: "acc_abcdefghijklmnop", handle: "scaleit.demo", mode: "first", target: 100, read: "auto",
                       plan: {lo: true, li: true, lo_budget: 40, strategies: ["lo_profile_page", "lo_../../x"]}}, {raw: true}),
    addJob("ig_probe", {probe_id: "prb_abcdefghijklmnop", handles: ["../evil"]}, {raw: true}),
  ];
  for (const j of bad) {
    await runUntil(j);
    assert.equal(j.state, "failed");
    assert.equal(j.fails[0].code, "bad_job");
  }
  assert.equal(since(await igLog(), t0).length, 0);
});

test("logged_in_only: today's pull, zero logged-out requests", async () => {
  await reset();
  const t0 = nowS();
  const j = await runUntil(addPull("logged_in_only"));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.deepEqual(j.leasedPlan.lo, false);
  const log = assertSerial(since(await igLog(), t0));
  assert.equal(log.filter(isLo).length, 0);
  assert.deepEqual(log.map(e => e.path), ["/api/v1/users/web_profile_info/", "/api/v1/clips/user/", "/api/v1/clips/user/"]);
  assert.ok(log.every(e => e.cookies.sessionid === SESSIONID && e.headers["x-ig-www-claim"] === "0"));
  assert.equal(j.mine.size, 100);
  assert.ok(j.uploads.every(u => u.paging.source === "clips" && u.layer === undefined));
  assert.deepEqual(j.uploads.flatMap(u => u.fetch_log || []).concat(j.result.fetch_log || []).map(l => l.strategy), ["li_profile", "li_clips", "li_clips"]);
  assert.deepEqual(await swEval(() => self.rv.budget.report()), {hour_used: 3, day_used: 3, cdn_day_used: 0, lo_hour_used: 0, lo_day_used: 0});
});

test("auto, next 100: the plan has no logged-out stage (a logged-in cursor cannot go on logged out)", async () => {
  await reset();
  const t0 = nowS();
  const j = await runUntil(addPull("auto", "next"));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.equal(j.leasedPlan.lo, false);
  assert.equal(since(await igLog(), t0).filter(isLo).length, 0);
});

test("refresh: the user and one list page, logged out", async () => {
  await reset();
  const t0 = nowS();
  const j = addPull("auto", "refresh");
  j.read = "logged_out_only";
  await runUntil(j);
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.deepEqual(since(await igLog(), t0).map(e => e.lo), ["profile_page", "reels_page"]);
  assert.equal(j.leasedPlan.lo_budget, 80);
  assert.equal(j.lo_stop, "refresh_page");
  assert.equal(j.mine.size, 12);
});

test("a rotated doc id: one try with the next id from the server's list, and the working id is kept", async () => {
  await reset();
  await scenario("lo_docid_rotated");
  const t0 = nowS();
  const j = await runUntil(addPull("auto"));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  const gql = since(await igLog(), t0).filter(e => e.path === "/graphql/query");
  assert.deepEqual(gql.slice(0, 3).map(e => e.form.doc_id), ["29285972954340351", "7845543455542541", "7845543455542541"]);
  assert.equal(new Set(gql.slice(1).map(e => e.form.doc_id)).size, 1);
  assert.equal(j.mine.size, 100);
  assert.deepEqual((await swEval(() => self.rv.loState())).strategy_off, {}, "a doc id that works on the second try switches nothing off");
  const log = j.uploads.flatMap(u => u.fetch_log || []);
  assert.deepEqual(log.slice(1, 4).map(l => [l.strategy, l.status, l.code]), [["lo_reels_page", "ok", null], ["lo_reels_gql", "blocked", "changed"], ["lo_reels_gql", "ok", null]]);
  await reset();
  const t1 = nowS();
  await swEval(async () => { await chrome.storage.local.set({lo: {doc_ok: {lo_reels_gql: 1}}}); return true; });
  await runUntil(addPull("auto"));
  assert.ok(since(await igLog(), t1).filter(e => e.path === "/graphql/query").every(e => e.form.doc_id === "7845543455542541"),
            "later pulls start with the id that worked");
});

test("ig_profile reads the profile logged out when the plan allows it", async () => {
  await reset();
  const t0 = nowS();
  const j = addJob("ig_profile", {handle: "scaleit.demo", purpose: "competitor"}, {read: "auto"});
  await runUntil(j);
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  const log = since(await igLog(), t0);
  assert.deepEqual(log.map(e => e.lo), ["profile_page"]);
  log.forEach(assertAnon);
  assert.equal(j.uploads[0].account.pk, PK);
  assert.equal(j.uploads[0].account.followers, 182000);
  await scenario("lo_login_wall");
  const t1 = nowS();
  const k = addJob("ig_profile", {handle: "scaleit.demo", purpose: "competitor"}, {read: "auto"});
  await runUntil(k);
  assert.equal(k.state, "done", JSON.stringify(k.fails));
  const l2 = since(await igLog(), t1).filter(e => !e.path.startsWith("/accounts/"));
  assert.deepEqual(l2.map(e => e.lo || e.path), ["profile_page", "web_profile", "/api/v1/users/web_profile_info/"], "walled: the login looks it up");
  assert.equal(k.uploads[0].account.pk, PK);
});

test("ig_videos: fresh video URLs without the login first; after a login wall the login reads the rest", async () => {
  await reset();
  const tl = (await igCtl("/__ctl/timeline")).data.lo["scaleit.demo"];
  const items = [0, 1, 2].map(i => ({reel_id: rid("rel"), ig_pk: tl.reels[i], code: tl.codes[i]}));
  await runUntil(addJob("ig_profile", {handle: "scaleit.demo", purpose: "competitor"}, {read: "auto"}));   // a page read: lsd
  const t0 = nowS();
  const j = await runUntil(addJob("ig_videos", {items, refresh_urls: true, want_cover: false}, {read: "auto"}));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.deepEqual(j.leasedPlan, {lo: true, li: true, lo_budget: 30, strategies: LO_IDS});
  const log = assertSerial((await igLog()).filter(e => e.t >= t0 && e.path !== "/favicon.ico" && e.path !== "/robots.txt"));
  const ig = log.filter(e => e.host === "www.instagram.com"), cdn = log.filter(e => e.host === "scontent.cdninstagram.com");
  assert.deepEqual(ig.map(e => [e.lo, JSON.parse(e.form.variables).shortcode]), items.map(it => ["gql", it.code]),
                   "one logged-out detail call per reel, no sign-in check, no mediaInfo");
  ig.forEach(assertAnon);
  assert.equal(cdn.length, 3);
  assert.equal(j.videos, 3);
  assert.deepEqual(j.result.result, {uploaded: 3, failed: [], media_info: 0});
  assert.deepEqual(j.result.fetch_log.map(l => [l.strategy, l.status, l.items]), Array(3).fill(["lo_post_gql", "ok", 1]));
  assertNoSecrets(j);

  await scenario("lo_login_wall");
  const t1 = nowS();
  const k = await runUntil(addJob("ig_videos", {items, refresh_urls: true, want_cover: false}, {read: "auto"}));
  assert.equal(k.state, "done", JSON.stringify(k.fails));
  const l2 = since(await igLog(), t1);
  assert.deepEqual(l2.map(e => e.lo || e.path), ["gql", ...items.map(it => `/api/v1/media/${it.ig_pk}/info/`)],
                   "one walled call, then the login reads every video URL");
  assertAnon(l2[0]);
  assert.ok(l2.slice(1).every(e => e.cookies.sessionid === SESSIONID));
  assert.ok((l2[1].t - l2[0].t_end) * 1000 >= PACING.layer_gap_ms[0] - 5, "the layer gap before the first logged-in call");
  assert.deepEqual(k.result.result, {uploaded: 3, failed: [], media_info: 3});
  assert.equal(await leaks(), 0);
});

test("ig_videos, logged-out only, no lsd in this browser: one profile page read for a token, never a made-up block", async () => {
  await reset();
  await swEval(async () => { await chrome.storage.session.remove("lo_lsd"); return true; });   // Chrome restarted
  const tl = (await igCtl("/__ctl/timeline")).data.lo["scaleit.demo"];
  const items = [0, 1, 2].map(i => ({reel_id: rid("rel"), ig_pk: tl.reels[i], code: tl.codes[i], handle: "scaleit.demo"}));
  const t0 = nowS();
  const j = await runUntil(addJob("ig_videos", {items, refresh_urls: true, want_cover: false}, {read: "logged_out_only"}));
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.equal(j.leasedPlan.li, false);
  const ig = since(await igLog(), t0);
  ig.forEach(assertAnon);
  assert.deepEqual(ig.map(e => e.lo), ["profile_page", "gql", "gql", "gql"], "a page for the lsd token, then one detail call per reel");
  assert.deepEqual(j.result.result, {uploaded: 3, failed: [], media_info: 0});
  assert.equal(j.videos, 3);
  assertNoSecrets(j);

  // An older job without the account handle: nothing can get a token. No request goes out and no block is reported:
  // the job waits (budget) and the reels stay for a later try.
  await swEval(async () => { await chrome.storage.session.remove("lo_lsd"); return true; });
  const bare = items.map(({handle, ...it}) => it);
  const t1 = nowS();
  const k = await runUntil(addJob("ig_videos", {items: bare, refresh_urls: true, want_cover: false}, {read: "logged_out_only"}));
  assert.equal(since(await igLog(), t1).length, 0, "no request reached Instagram");
  assert.equal(k.fails.length, 1);
  assert.equal(k.fails[0].code, "budget", JSON.stringify(k.fails));
  assert.ok(k.fails[0].cooldown_s >= 900);
  assert.equal(k.fails[0].lo, undefined, "no block class");
  assert.deepEqual((k.fails[0].fetch_log || []).map(l => [l.strategy, l.status]), []);
  const lo = (await storage("lo")) || {};
  assert.ok(!(lo.strategy_off || {}).lo_post_gql && !(lo.cooldown_until > Date.now()), "no switch-off and no cooldown");

  // A logged-out cooldown is running: the video job skips without a request and without a strike.
  await swEval(async () => { const {lo} = await chrome.storage.local.get("lo");
    await chrome.storage.local.set({lo: {...lo, cooldown_until: Date.now() + 3600_000, cooldown_reason: "login_wall"}}); return true; });
  const t2 = nowS();
  const m = await runUntil(addJob("ig_videos", {items, refresh_urls: true, want_cover: false}, {read: "logged_out_only", plan: {lo: true, li: false, lo_budget: 30, strategies: LO_IDS}}));
  assert.equal(since(await igLog(), t2).length, 0);
  assert.deepEqual(m.fails.map(f => f.code), ["budget"]);
  // The same for a profile job while the logged-out reads cool down: it waits, no made-up login wall.
  const t3 = nowS();
  const pj = await runUntil(addJob("ig_profile", {handle: "scaleit.demo", purpose: "competitor"},
                                   {read: "logged_out_only", plan: {lo: true, li: false, lo_budget: 2, strategies: LO_IDS}}));
  assert.equal(since(await igLog(), t3).length, 0);
  assert.deepEqual(pj.fails.map(f => [f.code, f.lo]), [["budget", undefined]]);
});

// ============================================================================================== probe

const PROBE_ORDER = [["lo_profile_page", "instagram"], ["lo_reels_page", "instagram"], ["lo_reels_gql", "instagram"],
                     ["lo_web_profile", "instagram"], ["lo_posts_gql", "instagram"], ["lo_post_gql", "instagram"],
                     ["lo_media_gql", "instagram"], ["lo_embed", "instagram"], ["lo_profile_page", "scaleit.demo"],
                     ["li_session", null], ["li_clips", "scaleit.demo"]];
const addProbe = () => addJob("ig_probe", {probe_id: "prb_abcdefghijklmnop", handles: ["instagram", "scaleit.demo"]});

test("Test my connection: 11 rows, one per way to read, each posted when it ends", async () => {
  await reset();
  const t0 = nowS();
  const j = await runUntil(addProbe());
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.equal(j.uploads.length, 11, "one POST /probe per row");
  assert.ok(j.uploads.every(u => u.rows.length === 1));
  assert.deepEqual(j.rows.map(r => [r.strategy, r.handle]), PROBE_ORDER);
  for (const r of j.rows) {
    assert.deepEqual(Object.keys(r).sort(), ["code", "fields", "handle", "http", "items", "layer", "ms", "status", "strategy"]);
    assert.equal(r.layer, r.strategy.startsWith("lo_") ? "logged_out" : "logged_in");
    assert.deepEqual([r.status, r.code], ["ok", null], `${r.strategy}: ${JSON.stringify(r)}`);
  }
  const f = Object.fromEntries(j.rows.slice(0, 8).map(r => [r.strategy, r]));
  assert.deepEqual(f.lo_profile_page.fields, ["user", "followers", "reel_list", "lsd"]);
  assert.equal(f.lo_profile_page.items, 12);
  assert.deepEqual(f.lo_reels_page.fields, ["reel_list", "lsd", "views", "likes", "comments"]);
  assert.deepEqual(f.lo_reels_gql.fields, ["reel_list", "views", "likes", "comments"]);
  assert.deepEqual(f.lo_web_profile.fields, ["user", "followers", "reel_list", "views", "likes", "comments", "taken_at", "caption", "video_url", "duration"]);
  assert.deepEqual(f.lo_posts_gql.fields, ["reel_list"]);
  for (const s of ["lo_post_gql", "lo_media_gql", "lo_embed"]) {
    assert.deepEqual(f[s].fields, ["views", "likes", "comments", "taken_at", "caption", "video_url", "duration"]);
    assert.equal(f[s].items, 1);
  }
  assert.deepEqual(j.rows[9].fields, ["signed_in"]);
  assert.ok(j.rows[9].ms > 0, "a test checks the sign-in fresh: one call, never the 6-hour cache");
  assert.deepEqual([j.rows[10].items, j.rows[10].fields[0]], [12, "reel_list"]);

  const log = assertSerial(since(await igLog(), t0));
  assert.equal(log.length, 11, "9 logged-out calls, the fresh sign-in check and one logged-in page: at most 12 calls");
  const lo = log.slice(0, 9);
  lo.forEach(assertAnon);
  assert.deepEqual(lo.map(e => e.lo), ["profile_page", "reels_page", "gql", "web_profile", "gql", "gql", "gql", "embed", "profile_page"]);
  const firstReel = (await igCtl("/__ctl/timeline")).data.lo.instagram.codes[0];
  assert.equal(JSON.parse(lo[5].form.variables).shortcode, firstReel, "the detail rows read the first reel found");
  assert.equal(lo[7].path, `/reel/${firstReel}/embed/captioned/`);
  assert.equal(JSON.parse(lo[2].form.variables).after, null, "the reels query starts at the top");
  assert.equal(log[9].cookies.sessionid, SESSIONID, "the sign-in check runs with the login");
  assert.ok(!isLo(log[9]));
  const clips = log[10];
  assert.equal(clips.path, "/api/v1/clips/user/");
  assert.equal(clips.form.page_size, "12");
  assert.equal(clips.form.target_user_id, PK);
  assert.equal(clips.cookies.sessionid, SESSIONID);
  assert.ok((log[9].t - lo.at(-1).t_end) * 1000 >= PACING.layer_gap_ms[0] - 5, "the layer gap before the first logged-in call");
  assert.equal(await leaks(), 0);
  assert.equal(j.result.result.rows, 11);
  assertNoSecrets(j);
});

test("probe on a blocked connection: rows say blocked or works in part; a rate limit ends the logged-out rows", async () => {
  await reset();
  await scenario("lo_reels_blocked", "lo_no_views", "lo_web_refused", "lo_no_embedded");
  let j = await runUntil(addProbe());
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.deepEqual(j.rows.map(r => [r.strategy, r.status, r.code]), [
    ["lo_profile_page", "partial", "no_data"], ["lo_reels_page", "partial", "no_data"], ["lo_reels_gql", "blocked", "login_wall"],
    ["lo_web_profile", "blocked", "refused"], ["lo_posts_gql", "skipped", "no_user"], ["lo_post_gql", "skipped", "no_reel"],
    ["lo_media_gql", "skipped", "no_reel"], ["lo_embed", "skipped", "no_reel"], ["lo_profile_page", "partial", "no_data"],
    ["li_session", "ok", null], ["li_clips", "ok", null]]);
  assert.deepEqual(j.rows[0].fields, ["lsd"]);
  assert.deepEqual(j.rows[2].fields, []);
  assert.equal(j.rows[2].http, 401);

  await reset();
  await scenario("lo_rate_after_2");
  const t0 = nowS();
  j = await runUntil(addProbe());
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.deepEqual(j.rows.map(r => [r.status, r.code]), [["ok", null], ["ok", null], ["blocked", "rate"],
    ...Array(6).fill(["skipped", "cooling"]), ["skipped", "cooling"], ["skipped", "cooling"]],
    "after the rate limit: no more logged-out rows, and the login waits too");
  assert.equal(since(await igLog(), t0).length, 3);
  const loState = await swEval(() => self.rv.loState());
  assert.equal(loState.cooldown_reason, "rate");

  // During the cooldown a new probe runs no logged-out row at all.
  await swEval(async () => { const {lo} = await chrome.storage.local.get("lo"); await chrome.storage.local.set({lo: {...lo, li_not_before: 0}}); return true; });
  await scenario();
  const t1 = nowS();
  j = await runUntil(addProbe());
  assert.deepEqual(j.rows.slice(0, 9).map(r => [r.status, r.code]), Array(9).fill(["skipped", "cooling"]));
  assert.deepEqual(j.rows.slice(9).map(r => r.status), ["ok", "ok"]);
  assert.equal(since(await igLog(), t1).filter(isLo).length, 0);

  // 3 probes per day on this device: the 4th posts skipped rows and makes no call. (reset() above cleared the
  // day's count, so only 2 probes count since; set the third.)
  await swEval(async () => { const {budget} = await chrome.storage.local.get("budget"); await chrome.storage.local.set({budget: {...budget, probe_day_used: 3}}); return true; });
  const t2 = nowS();
  j = await runUntil(addProbe());
  assert.ok(j.rows.every(r => r.status === "skipped"), JSON.stringify(j.rows.map(r => [r.status, r.code])));
  assert.equal(since(await igLog(), t2).length, 0);
});

test("probe while signed out: the logged-out rows run, the logged-in rows say signed out, no logged-in call", async () => {
  await reset({signedIn: false});
  await signedOutStop();
  const t0 = nowS();
  const j = await runUntil(addProbe());
  assert.equal(j.state, "done", JSON.stringify(j.fails));
  assert.ok(j.rows.slice(0, 9).every(r => r.status === "ok"));
  assert.deepEqual(j.rows.slice(9).map(r => [r.strategy, r.status, r.code]), [["li_session", "blocked", "signed_out"], ["li_clips", "skipped", "signed_out"]]);
  const log = since(await igLog(), t0);
  assert.ok(log.every(isLo));
  log.forEach(assertAnon);
});

// ============================================================================================== the whole run

test("the whole run: the cookie detector never fired and ReelVault never got a cookie", async () => {
  await idle();
  assert.equal(await leaks(), 0, "no logged-out request carried a cookie or a CSRF token");
  const all = (await igLog()).filter(e => e.host === "www.instagram.com");
  const lo = all.filter(isLo);
  assert.ok(lo.length > 100, `${lo.length} logged-out requests in this run`);
  for (const e of lo) {
    assert.deepEqual(e.cookies, {}, e.path);
    assert.equal(e.headers.cookie, undefined);
    assert.equal(e.headers["x-csrftoken"], undefined);
    assert.equal(e.headers["x-ig-www-claim"], undefined);
  }
  assert.ok(C.requests.length > 200);
  for (const q of C.requests) {
    assert.equal(q.headers.cookie, undefined, `${q.path} had a Cookie header`);
    assert.ok(!q.body.includes(SESSIONID) && !q.body.includes(CSRF) && !q.body.includes(LSD), `${q.path} body holds a secret`);
    assert.equal(q.headers.authorization, "Bearer rvd_contract_test");
  }
});

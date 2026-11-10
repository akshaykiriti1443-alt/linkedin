// End-to-end test of the ReelVault Members extension (DESIGN-SAAS.md 12.3). WP2.
//
//   node --test tests/ext/ext_e2e.mjs                (or: python -m pytest tests/ext/test_ext_e2e.py)
//
// Starts the fake Instagram (tests/ext/fake_ig_server.py, HTTPS, self-signed) and the real ReelVault app
// (tests/ext/run_server.py, RV_TEST_PACING=1), loads the unpacked extension into Chromium (Playwright for Node),
// maps www.instagram.com / scontent.cdninstagram.com / www.skool.com / www.linkedin.com to the fake server with
// --host-resolver-rules, and drives the whole loop: pairing in the popup, the account confirm gate, pulls of
// 100, the next 100 from the saved cursor, recoveries, videos, page reads, stops and cooldowns.
//
// Env: RV_PYTHON (python with the [server] deps; default python3), RV_E2E_PORT_BASE (default 9140: fake
// Instagram = base+3, ReelVault = base+1), RV_E2E_TMP (scratch folder; default a new temp folder), RV_E2E_KEEP=1.
import test, {before, after} from "node:test";
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdtempSync, mkdirSync, cpSync, readFileSync, writeFileSync, rmSync, existsSync, statSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
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
const RV_PORT = BASE + 1, IG_PORT = BASE + 3;
const RV = `http://127.0.0.1:${RV_PORT}`;
const TMP = process.env.RV_E2E_TMP || mkdtempSync(path.join(tmpdir(), "rv-ext-e2e-"));
const PK = "25025320";              // the competitor in the fake timeline (make_fixtures.TARGET)
const VIEWER = "61000777";          // the signed-in member in the fake Instagram (make_fixtures.VIEWER)
const CSRF = "csrfTOKENabc123";
const SESSIONID = "SESSIONsecret987";
const MIN_GAP_MS = 50;              // TEST_PACING gap_ms[0]; page gap adds page_gap_ms[0] = 50 more

const procs = [];
let ctx, sw, extId, extDir, session;

// ============================================================================================== small helpers

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function until(fn, {ms = 30_000, every = 150, what = "condition"} = {}) {
  const end = Date.now() + ms;
  let last;
  while (Date.now() < end) {
    last = await fn();
    if (last) return last;
    await sleep(every);
  }
  throw new Error(`timed out waiting for ${what}`);
}

function igCtl(p, body) {          // control routes of the fake Instagram (self-signed HTTPS)
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : "";
    const req = https.request({host: "127.0.0.1", port: IG_PORT, path: p, method: body ? "POST" : "GET",
      rejectUnauthorized: false, headers: {"Content-Type": "application/json", "Content-Length": Buffer.byteLength(data)}},
    res => { let t = ""; res.on("data", c => { t += c; }); res.on("end", () => { try { resolve(JSON.parse(t)); } catch (e) { reject(e); } }); });
    req.on("error", reject);
    req.end(data);
  });
}
const scenario = (...names) => igCtl("/__ctl/scenario", {set: names});
const igLog = async () => (await igCtl("/__ctl/log")).log;
const igApi = log => log.filter(e => e.host === "www.instagram.com" && (e.path.startsWith("/api/") || e.path.startsWith("/web/")));

async function rv(p, body, {method} = {}) {   // ReelVault test-control routes and the web UI session API
  const headers = {"Content-Type": "application/json"};
  if (session) { headers.Cookie = session.cookie; headers["X-CSRF-Token"] = session.csrf; }
  const r = await fetch(RV + p, {method: method || (body === undefined ? "GET" : "POST"), headers,
                                 body: body === undefined ? undefined : JSON.stringify(body)});
  const t = await r.text();
  let data; try { data = JSON.parse(t); } catch { data = t; }
  return {status: r.status, data};
}
const state = async () => (await rv("/__test/state")).data;
const job = async id => (await state()).jobs.find(j => j.id === id);

function spawnPy(args, name) {
  const p = spawn(PY, args, {cwd: ROOT, env: {...process.env, PYTHONUNBUFFERED: "1"}, stdio: ["ignore", "pipe", "pipe"]});
  let out = "";
  p.stdout.on("data", d => { out += d; });
  p.stderr.on("data", d => { out += d; if (process.env.RV_E2E_VERBOSE) process.stderr.write(`[${name}] ${d}`); });
  p.on("exit", code => { if (code && code !== 0 && !p.stopping) process.stderr.write(`[${name}] exited ${code}\n${out}\n`); });
  p.output = () => out;
  procs.push(p);
  return p;
}

async function swEval(fn, arg) { return sw.evaluate(fn, arg); }

async function tickNow() {          // what the 30 s alarm does, without waiting for it
  await swEval(() => { self.rv.tick("now"); return true; });
}

// Run the queued work: tick until the job is finished (or in the wanted state).
async function runUntil(jobId, states = ["done", "failed", "canceled"], ms = 60_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const j = await job(jobId);
    if (j && states.includes(j.state)) return j;
    await tickNow();
    await sleep(400);
  }
  const j = await job(jobId);
  throw new Error(`job ${jobId} stayed ${j && j.state}: ${JSON.stringify(j && {err: j.error_message, code: j.error_code, progress: j.progress})}`);
}

// A new Instagram job is already "waiting" (for the extension); a stopped one is waiting again with a message.
async function runUntilStopped(jobId, ms = 30_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const j = await job(jobId);
    if (j && j.state === "waiting" && j.error_message) return j;
    if (j && ["done", "failed", "canceled"].includes(j.state)) throw new Error(`job ${jobId} ended ${j.state}`);
    await tickNow();
    await sleep(300);
  }
  throw new Error(`job ${jobId} never stopped`);
}

async function idle() {             // wait until the extension runs nothing
  await until(() => swEval(() => self.rv.busy()).then(b => !b), {what: "the extension to finish its work"});
}

async function resetAll() {
  await idle();
  await rv("/__test/reset", {});
  await scenario();
  await swEval(async () => { await chrome.storage.local.remove(["cooldown", "job", "budget"]); return true; });
  await rv("/__test/clear_cooldown", {});
}

async function newAccount(handle = "scaleit.demo") {
  return (await rv("/__test/account", {handle})).data.account_id;
}

async function pull(accountId, mode = "first") {
  const r = await rv("/__test/pull", {account_id: accountId, mode});
  assert.equal(r.status, 200, JSON.stringify(r.data));
  return r.data.job_id;
}

const since = (log, t0) => log.filter(e => e.t >= t0);
const nowS = () => Date.now() / 1000;

// Our own copy of the extension, with config.js for this test server. BUILD "test" is the only build that takes the
// server's short test gaps (pacing.js merge); store and ZIP builds always keep the stricter numbers.
function copyExtension(name, editManifest) {
  const dir = path.join(TMP, name);
  rmSync(dir, {recursive: true, force: true});
  cpSync(path.join(ROOT, "extension-members"), dir, {recursive: true});
  const cfg = readFileSync(path.join(dir, "config.js"), "utf8")
    .replace(/^export const SERVERS = .*$/m, `export const SERVERS = ${JSON.stringify([RV])};`)
    .replace(/^export const BUILD = .*$/m, `export const BUILD = "test";`);
  writeFileSync(path.join(dir, "config.js"), cfg);
  if (editManifest) {
    const mf = JSON.parse(readFileSync(path.join(dir, "manifest.json"), "utf8"));
    editManifest(mf);
    writeFileSync(path.join(dir, "manifest.json"), JSON.stringify(mf, null, 2));
  }
  return dir;
}

async function launch(dir, profile) {
  const hosts = ["www.instagram.com", "scontent.cdninstagram.com", "www.skool.com", "www.linkedin.com"];
  const c = await chromium.launchPersistentContext(path.join(TMP, profile), {
    channel: "chromium", headless: true, ignoreHTTPSErrors: true,
    args: [`--disable-extensions-except=${dir}`, `--load-extension=${dir}`, "--no-proxy-server",
           `--host-resolver-rules=${hosts.map(h => `MAP ${h} 127.0.0.1:${IG_PORT}`).join(", ")}`,
           "--ignore-certificate-errors"]});
  const w = c.serviceWorkers()[0] || await c.waitForEvent("serviceworker", {timeout: 30_000});
  return {ctx: c, sw: w, extId: new URL(w.url()).host};
}

async function pairInPopup() {     // the member's steps: Connect the extension (web UI), paste the link, Connect
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#pair:not([hidden])");
  const pr = await rv("/api/ext/pair", {});
  assert.equal(pr.status, 200, JSON.stringify(pr.data));
  await popup.fill("#link", pr.data.pair_link);
  await popup.click("#next");
  await popup.waitForSelector("#confirm:not([hidden])");
  await popup.click("#connect");
  await popup.waitForSelector("#connected:not([hidden])", {timeout: 30_000});
  return popup;
}

// ============================================================================================== setup

before(async () => {
  mkdirSync(TMP, {recursive: true});
  extDir = copyExtension("ext");
  for (const d of ["data", "profile", "profile2"]) rmSync(path.join(TMP, d), {recursive: true, force: true});
  spawnPy([path.join(HERE, "fake_ig_server.py"), "--port", String(IG_PORT), "--cert-dir", path.join(TMP, "cert")], "fake-ig");
  spawnPy([path.join(HERE, "run_server.py"), "--port", String(RV_PORT), "--data-dir", path.join(TMP, "data")], "reelvault");
  await until(async () => { try { return (await igCtl("/__ctl/log")).log !== undefined; } catch { return false; } },
              {what: "the fake Instagram to start"});
  await until(async () => { try { return (await fetch(RV + "/healthz")).ok; } catch { return false; } },
              {ms: 60_000, what: "the ReelVault test server to start"});

  ({ctx, sw, extId} = await launch(extDir, "profile"));
  // What Instagram sets when the member is signed in. sessionid is HttpOnly (page scripts can't read it).
  await ctx.addCookies([
    {name: "sessionid", value: SESSIONID, domain: ".instagram.com", path: "/", secure: true, httpOnly: true},
    {name: "ds_user_id", value: VIEWER, domain: ".instagram.com", path: "/", secure: true},
    {name: "csrftoken", value: CSRF, domain: ".instagram.com", path: "/", secure: true}]);
  // The member signs in to the web UI (real session API).
  const r = await fetch(RV + "/api/auth/login", {method: "POST", headers: {"Content-Type": "application/json"},
                                                 body: JSON.stringify({email: "member@example.com", password: "member-pass-123"})});
  assert.equal(r.status, 200);
  const me = await r.json();
  const cookie = (r.headers.getSetCookie() || []).map(c => c.split(";")[0]).join("; ");
  session = {cookie, csrf: me.user.csrf_token};
});

after(async () => {
  try { if (ctx) await ctx.close(); } catch { /* already gone */ }
  for (const p of procs) { p.stopping = true; p.kill("SIGTERM"); }
  await sleep(300);
  for (const p of procs) if (p.exitCode === null) p.kill("SIGKILL");
  if (!process.env.RV_E2E_KEEP && !process.env.RV_E2E_TMP) rmSync(TMP, {recursive: true, force: true});
});

// ============================================================================================== pairing and trust

test("before pairing the extension calls nothing", async () => {
  await tickNow();
  await sleep(500);
  assert.equal((await igLog()).length, 0, "no Instagram request while not paired");
  const srv = (await rv("/__test/requests")).data.requests;
  assert.equal(srv.length, 0, "no server call while not paired");
});

test("pairing in the popup: foreign links refused, real link connects and checks Instagram once", async () => {
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#pair:not([hidden])");
  await popup.fill("#link", "https://evil.example.com/pair#7KQ4-M2XP");
  await popup.click("#next");
  await popup.waitForSelector("#error:not([hidden])");
  assert.match(await popup.textContent("#error"), /not from your ReelVault/);
  assert.equal((await rv("/__test/requests")).data.requests.length, 0, "a foreign link makes no call at all");

  const pr = await rv("/api/ext/pair", {});
  assert.equal(pr.status, 200, JSON.stringify(pr.data));
  assert.ok(pr.data.pair_link.startsWith(RV + "/pair#"));
  await popup.fill("#link", pr.data.pair_link);
  await popup.click("#next");
  await popup.waitForSelector("#confirm:not([hidden])");
  assert.equal(await popup.textContent("#confirm-host"), `127.0.0.1:${RV_PORT}`);
  await popup.click("#connect");
  await popup.waitForSelector("#connected:not([hidden])", {timeout: 30_000});
  assert.equal(await popup.textContent("#hello-line"), "Connected to ReelVault as Mia Member.");
  assert.equal(await popup.textContent("#ig-line"), "ReelVault will read Instagram as @member.reader.");
  await popup.close();

  const api = igApi(await igLog());
  assert.deepEqual(api.map(e => e.path), [`/api/v1/users/${VIEWER}/info/`], "sign-in check = one call (ds_user_id readable)");
  const st = await until(async () => { const s = await state(); return s.device && s.device.ig_user_id && s; },
                         {what: "hello with the sign-in result"});
  assert.equal(st.device.ig_user_id, VIEWER);
  assert.equal(st.device.ig_username, "member.reader");
});

test("Instagram jobs wait until the member confirms the account", async () => {
  const acc = await newAccount();
  const jid = await pull(acc);
  const before = (await igLog()).length;
  await tickNow();
  await until(async () => (await swEval(() => self.rv.lastLease())) ?.reason === "confirm_account", {what: "lease refused"});
  assert.equal((await job(jid)).state, "waiting");
  assert.equal((await igLog()).length, before, "no Instagram call before the confirm");
  const status = (await rv("/api/ext/status")).data;
  assert.equal(status.device.ig_username, "member.reader");
  assert.equal(status.device.ig_confirmed, false);
  const bad = await rv("/api/ext/confirm-account", {ig_user_id: "123"});
  assert.equal(bad.status, 400);
  const ok = await rv("/api/ext/confirm-account", {ig_user_id: VIEWER});
  assert.equal(ok.status, 200, JSON.stringify(ok.data));
  await rv("/__test/cancel_all", {});
});

// ============================================================================================== pulls

let accA, pull1;

test("first pull: exactly 100 reels over pages of 50, cursor saved, pinned reading tab on robots.txt", async () => {
  await resetAll();
  accA = await newAccount();
  const t0 = nowS();
  const jid = await pull(accA);
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  assert.equal(j.result.stop_reason, "target_reached");
  const s = await state();
  assert.equal(s.reels.length, 100);
  const p = s.pulls.find(x => x.job_id === jid);
  assert.equal(p.reels_new, 100);
  assert.equal(p.pages, 2);
  const a = s.accounts.find(x => x.id === accA);
  assert.equal(a.ig_pk, PK);
  assert.equal(a.cursor_source, "clips");
  assert.ok(a.cursor_max_id, "cursor saved");
  assert.equal(a.cursor_done, false);
  assert.equal(a.status, "active");
  assert.ok(s.jobs.some(x => x.kind === "score_account" && x.payload.account_id === accA), "score_account queued");
  pull1 = {jid, cursor: a.cursor_max_id, t0};

  const tab = await swEval(async () => {
    const {readingTabId} = await chrome.storage.session.get("readingTabId");
    const t = await chrome.tabs.get(readingTabId);
    return {url: t.url, pinned: t.pinned, autoDiscardable: t.autoDiscardable, active: t.active};
  });
  assert.equal(tab.url, "https://www.instagram.com/robots.txt");
  assert.equal(tab.pinned, true);
  assert.equal(tab.autoDiscardable, false);
  assert.equal(tab.active, false);
});

test("Instagram requests come from the reading tab with the member's cookies, CSRF header and app id", async () => {
  const api = igApi(since(await igLog(), pull1.t0));
  const clips = api.filter(e => e.path === "/api/v1/clips/user/");
  assert.equal(clips.length, 2);
  for (const e of api) {
    assert.equal(e.headers["x-ig-app-id"], "936619743392459");
    assert.equal(e.headers["x-csrftoken"], CSRF);
    assert.equal(e.cookies.sessionid, SESSIONID, "the browser attaches the HttpOnly session cookie itself");
    assert.equal(e.headers["sec-fetch-site"], "same-origin");
    assert.ok(!/robots\.txt/.test(e.headers.referer || ""), "no robots.txt Referer: " + e.headers.referer);
  }
  for (const e of clips) {
    assert.equal(e.headers.referer, "https://www.instagram.com/scaleit.demo/reels/");
    assert.equal(e.method, "POST");
    assert.equal(e.headers.origin, "https://www.instagram.com");
    assert.equal(e.form.target_user_id, PK);
    assert.equal(e.form.page_size, "50");
    assert.equal(e.form.include_feed_video, "true");
  }
  assert.equal(clips[0].form.max_id, undefined, "a first pull starts at the top");
  assert.ok(clips[1].form.max_id, "page 2 uses the cursor of page 1");
});

test("pacing: one Instagram request at a time, with the gaps the server sent", async () => {
  const api = igApi(since(await igLog(), pull1.t0)).sort((a, b) => a.t - b.t);
  assert.ok(api.length >= 3);
  for (let i = 1; i < api.length; i++) {
    assert.ok(api[i].t >= api[i - 1].t_end, `request ${i} started before ${i - 1} ended`);
    const gap = (api[i].t - api[i - 1].t) * 1000;
    const min = api[i].path === "/api/v1/clips/user/" && api[i - 1].path === "/api/v1/clips/user/" ? 2 * MIN_GAP_MS : MIN_GAP_MS;
    assert.ok(gap >= min - 5, `gap ${gap.toFixed(0)} ms before ${api[i].path} is under ${min} ms`);
  }
  const eff = await swEval(() => self.rv.effPacing());
  assert.deepEqual(eff.gap_ms, [50, 100], "a test build takes the server's test gaps");
  assert.equal(eff.hour_max, 120);
  const report = await swEval(() => self.rv.budget.report());
  assert.ok(report.hour_used >= api.length, "every call is counted in the local budget");
});

test("next pull continues from the saved cursor with the next (older) 100", async () => {
  const t0 = nowS();
  const jid = await pull(accA, "next");
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  const s = await state();
  assert.equal(s.reels.length, 200);
  assert.equal(new Set(s.reels.map(r => r.ig_pk)).size, 200, "no reel twice");
  assert.equal(s.pulls.find(x => x.job_id === jid).reels_new, 100);
  const clips = igApi(since(await igLog(), t0)).filter(e => e.path === "/api/v1/clips/user/");
  assert.equal(clips[0].form.max_id, pull1.cursor, "the first page of the next pull uses the saved cursor");
  const timeline = (await igCtl("/__ctl/timeline")).pks;
  const have = new Set(s.reels.map(r => r.ig_pk));
  assert.ok(timeline.slice(0, 200).every(pk => have.has(pk)), "reels 1-200 of the timeline, newest first");
  assert.ok(!timeline.slice(200).some(pk => have.has(pk)));
});

test("refresh reads from the top and stops at the first known reel; the cursor does not move", async () => {
  const before = (await state()).accounts.find(x => x.id === accA).cursor_max_id;
  const jid = await pull(accA, "refresh");
  const j = await runUntil(jid);
  assert.equal(j.result.stop_reason, "known_reel");
  const s = await state();
  assert.equal(s.pulls.find(x => x.job_id === jid).reels_new, 0);
  assert.equal(s.pulls.find(x => x.job_id === jid).pages, 1);
  assert.equal(s.accounts.find(x => x.id === accA).cursor_max_id, before);
});

test("the popup shows the server, the Instagram account, today's budget and the keep-Chrome-open line", async () => {
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#status:not([hidden])");
  assert.equal(await popup.textContent("#s-host"), `127.0.0.1:${RV_PORT}`);
  assert.equal(await popup.textContent("#s-ig"), "ReelVault reads Instagram as @member.reader.");
  const used = (await swEval(() => self.rv.budget.report()));
  assert.equal(await popup.textContent("#s-budget"),
               `Instagram reads today: ${used.day_used} of 400 · this hour: ${used.hour_used} of 120 · videos: 0 of 150`);
  assert.equal(await popup.textContent("#s-job"), "Nothing to read right now.");
  assert.match(await popup.textContent(".keep"), /^Keep Chrome open\. ReelVault reads in a pinned Instagram tab/);
  assert.equal(await popup.isVisible("#s-update"), false, "no update line when the server has the same version");
  await popup.close();
});

// ============================================================================================== videos

test("video job: smallest version >= 480 px, Referer set by the DNR rule, no cookies, 60 MB file refused", async () => {
  await idle();
  await scenario("big_video");
  const t0 = nowS();
  const r = (await rv("/__test/videos", {n: 3})).data;
  assert.ok(r.job_id);
  const j = await runUntil(r.job_id);
  assert.equal(j.state, "done", JSON.stringify(j));
  assert.equal(j.result.uploaded, 2);
  assert.deepEqual(j.result.failed.map(f => f.reason), ["too_big"]);
  const s = await state();
  const reels = r.reel_ids.map(id => s.reels.find(x => x.id === id));
  assert.deepEqual(reels.map(x => x.media_state), ["have", "failed", "have"]);
  for (const x of [reels[0], reels[2]]) {
    const f = path.join(TMP, "data", "users", s.uid, x.video_path);
    assert.ok(existsSync(f) && statSync(f).size > 4000, `video saved at ${x.video_path}`);
  }
  const log = since(await igLog(), t0);
  const cdn = log.filter(e => e.host === "scontent.cdninstagram.com" && e.path.endsWith(".mp4"));
  assert.equal(cdn.length, 3);
  for (const e of cdn) {
    assert.equal(e.headers.referer, "https://www.instagram.com/");
    assert.ok(e.path.endsWith("_480.mp4"), `picked ${e.path}`);
    assert.deepEqual(e.cookies, {}, "CDN downloads go without cookies");
  }
  const infos = igApi(log).filter(e => /^\/api\/v1\/media\/\d+\/info\/$/.test(e.path));
  assert.equal(infos.length, 3);
  // fix round 2: API calls carry an instagram.com page as Referer, never the robots.txt reading page
  for (const e of infos) assert.equal(e.headers.referer, "https://www.instagram.com/");
  const up = (await rv("/__test/requests")).data.requests.filter(q => q.method === "POST" && q.path.endsWith("/video"));
  assert.equal(up.length, 2, "only the two good videos are uploaded: " + JSON.stringify(up.map(q => [q.path, q.status, q.body_len])));
  await scenario();
});

test("CDN 429 during a video job: stop at once (24 h), reels kept for a later try, not failed", async () => {
  await freshWithFirstPull();
  await scenario("cdn_429");
  const t0 = nowS();
  const r = (await rv("/__test/videos", {n: 4})).data;
  assert.ok(r.job_id);
  await runUntilStopped(r.job_id);
  await idle();
  const s = await state();
  assert.equal(s.device.state.cooldown_reason, "rate_limited");
  const log = since(await igLog(), t0);
  assert.equal(log.filter(e => e.host === "scontent.cdninstagram.com" && e.path.endsWith(".mp4")).length, 1,
               "one CDN 429, then no more downloads");
  assert.equal(igApi(log).filter(e => /^\/api\/v1\/media\/\d+\/info\/$/.test(e.path)).length, 1);
  const reels = r.reel_ids.map(id => s.reels.find(x => x.id === id));
  assert.ok(reels.every(x => x.media_state !== "failed"), JSON.stringify(reels.map(x => x.media_state)));
  await scenario();
  await rv("/__test/cancel_all", {});
});

// ============================================================================================== ids, collabs, suggested

test("bigint pk stored exactly, collab kept and marked, suggested reels from others dropped", async () => {
  await resetAll();
  await scenario("bigint_pk", "collab", "suggested_reels");
  const acc = await newAccount();
  const j = await runUntil(await pull(acc));
  assert.equal(j.state, "done", JSON.stringify(j));
  const s = await state();
  assert.ok(s.reels.some(x => x.ig_pk === "3456789012345678901"), "the 19-digit pk is stored as the exact string");
  const collab = s.reels.filter(x => x.is_collab);
  assert.equal(collab.length, 1);
  const timeline = new Set((await igCtl("/__ctl/timeline")).pks);
  const foreign = s.reels.filter(x => !x.is_collab && !timeline.has(x.ig_pk) && x.ig_pk !== "3456789012345678901");
  assert.deepEqual(foreign, [], "no suggested reel from another creator was stored");
  assert.equal(s.reels.length, 100);
  await scenario();
});

// ============================================================================================== cursor recoveries (5.4 step 4)

async function freshWithFirstPull() {
  await resetAll();
  const acc = await newAccount();
  const j = await runUntil(await pull(acc));
  assert.equal(j.state, "done");
  return acc;
}

test("page_size 400: the same call again with page_size 12, kept for the job", async () => {
  await resetAll();
  await scenario("page_size_400");
  const acc = await newAccount();
  const t0 = nowS();
  const jid = await pull(acc);
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  const s = await state();
  assert.equal(s.reels.length, 100);
  const p = s.pulls.find(x => x.job_id === jid);
  assert.equal(p.recovery, "page_size_12");
  assert.equal(p.pages, 9);
  const sizes = igApi(since(await igLog(), t0)).filter(e => e.path === "/api/v1/clips/user/").map(e => e.form.page_size);
  assert.equal(sizes[0], "50");
  assert.ok(sizes.slice(1).every(x => x === "12"));
  assert.equal(s.device.state.cooldown_until, null, "no cooldown for a recovery");
});

test("stale cursor: the server spots the restart and the pull resumes through feed/user without double counting", async () => {
  const acc = await freshWithFirstPull();
  const a0 = (await state()).accounts.find(x => x.id === acc);
  await scenario("stale_cursor");
  const t0 = nowS();
  const jid = await pull(acc, "next");
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  const s = await state();
  assert.equal(s.reels.length, 200);
  assert.equal(new Set(s.reels.map(x => x.ig_pk)).size, 200);
  const p = s.pulls.find(x => x.job_id === jid);
  assert.equal(p.reels_new, 100);
  assert.equal(p.recovery, "feed_oldest_pk");
  const feed = igApi(since(await igLog(), t0)).filter(e => e.path.startsWith("/api/v1/feed/user/"));
  assert.ok(feed.length >= 1);
  assert.equal(feed[0].query.max_id, `${a0.oldest_pk}_${PK}`);
  assert.equal(s.accounts.find(x => x.id === acc).cursor_source, "feed");
  await scenario();
});

test("cursor error: a 400 on the saved cursor switches to feed/user from the oldest reel", async () => {
  const acc = await freshWithFirstPull();
  const a0 = (await state()).accounts.find(x => x.id === acc);
  await scenario("cursor_error");
  const t0 = nowS();
  const jid = await pull(acc, "next");
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  const s = await state();
  assert.equal(s.reels.length, 200);
  assert.equal(s.pulls.find(x => x.job_id === jid).recovery, "feed_oldest_pk");
  const api = igApi(since(await igLog(), t0));
  assert.equal(api.filter(e => e.path === "/api/v1/clips/user/").length, 1, "the bad cursor is tried once");
  assert.equal(api.find(e => e.path.startsWith("/api/v1/feed/user/")).query.max_id, `${a0.oldest_pk}_${PK}`);
  assert.equal(s.device.state.cooldown_until, null);
  await scenario();
});

test("clips/user answers 404 twice: the reader switches to feed/user", async () => {
  await resetAll();
  await scenario("clips_404");
  const acc = await newAccount();
  const t0 = nowS();
  const j = await runUntil(await pull(acc));
  assert.equal(j.state, "done", JSON.stringify(j));
  assert.equal((await state()).reels.length, 100);
  const api = igApi(since(await igLog(), t0));
  assert.equal(api.filter(e => e.path === "/api/v1/clips/user/").length, 2);
  assert.ok(api.filter(e => e.path.startsWith("/api/v1/feed/user/")).length >= 9);
  await scenario();
});

// ============================================================================================== stops and cooldowns (5.7)

async function igCallsWhileTicking(n = 4) {
  const t0 = nowS();
  for (let i = 0; i < n; i++) { await tickNow(); await sleep(300); await idle(); }
  return igApi(since(await igLog(), t0)).length + since(await igLog(), t0).filter(e => e.host !== "www.instagram.com").length;
}

test("429 on page 2: stop at once, 24 h cooldown, cursor kept, no more calls; later the pull goes on", async () => {
  await resetAll();
  await scenario("429_on_page2");
  const acc = await newAccount();
  const jid = await pull(acc);
  await runUntilStopped(jid);
  await idle();
  let s = await state();
  let j = s.jobs.find(x => x.id === jid);
  assert.equal(j.state, "waiting");
  assert.match(j.error_message, /slow down/);
  assert.equal(s.device.state.cooldown_reason, "rate_limited");
  const hours = (Date.parse(s.device.state.cooldown_until) - Date.now()) / 3600_000;
  assert.ok(hours > 23.5 && hours <= 24, `cooldown ${hours} h`);
  assert.ok(Date.parse(j.run_after) - Date.now() > 23 * 3600_000);
  const a = s.accounts.find(x => x.id === acc);
  assert.ok(a.cursor_max_id, "the cursor of page 1 is kept");
  assert.equal(s.reels.length, 50);
  const p = s.pulls.find(x => x.job_id === jid);
  assert.equal(p.stop_reason, "rate_limited");
  assert.equal(p.finished_at, null, "the pull stays resumable");
  const local = await swEval(async () => (await chrome.storage.local.get("cooldown")).cooldown);
  assert.equal(local.reason, "rate_limited");
  assert.equal(await igCallsWhileTicking(), 0, "no Instagram call during the cooldown");
  const leases = (await rv("/__test/requests")).data.requests.filter(q => q.path.endsWith("/jobs/lease")).slice(-2);
  assert.ok(leases.every(q => JSON.parse(q.body).kinds.join() === "page_read"), "during a cooldown only page_read is asked for");

  await scenario();
  await rv("/__test/clear_cooldown", {});
  await swEval(async () => { await chrome.storage.local.remove("cooldown"); return true; });
  j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  s = await state();
  assert.equal(s.reels.length, 100, "it went on from the kept cursor to 100 in total");
  assert.equal(s.pulls.find(x => x.job_id === jid).reels_new, 100);
});

test("429 while ReelVault can't take the report: the extension keeps its own 24 h stop anyway", async () => {
  await resetAll();
  await scenario("429_on_page2");
  await rv("/__test/flags", {fail_503: true});
  try {
    const acc = await newAccount();
    const jid = await pull(acc);
    await tickNow();
    const cd = await until(() => swEval(async () => (await chrome.storage.local.get("cooldown")).cooldown),
                           {what: "a local cooldown"});
    await idle();
    assert.equal(cd.reason, "rate_limited");
    assert.equal(cd.local_only, true);
    assert.ok(cd.until - Date.now() > 23.5 * 3600_000);
    assert.equal((await state()).device.state.cooldown_until, null, "the server never heard of it");
    assert.equal(await igCallsWhileTicking(3), 0, "no Instagram call, even after hello says there is no cooldown");
    assert.equal((await job(jid)).state, "running", "the server gets the job back when its lease runs out");
  } finally { await rv("/__test/flags", {fail_503: false}); }
});

test("login wall: a redirect means signed out; Try again in the web UI lets the job go on", async () => {
  await resetAll();
  const acc = await newAccount();
  await scenario("login_redirect");
  const jid = await pull(acc);
  await runUntilStopped(jid);
  await until(async () => (await state()).device.state.cooldown_reason === "signed_out", {what: "signed_out cooldown"});
  await idle();
  const s = await state();
  assert.equal(s.device.state.signed_in, false);
  assert.match(s.jobs.find(x => x.id === jid).error_message, /signed out of Instagram/);
  const st = (await rv("/api/ext/status")).data;
  assert.equal(st.device.signed_in, false);
  assert.match(st.device.cooldown_text, /Sign in at instagram.com/);
  assert.equal((await swEval(() => self.rv.statusForPopup())).ig.signed_in, false);
  assert.equal(await igCallsWhileTicking(2), 0, "no Instagram call while signed out");

  await scenario();                                // the member signs in again, then presses Try again
  const r = await rv("/api/ext/try-again", {});
  assert.equal(r.status, 200, JSON.stringify(r.data));
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  assert.equal((await state()).device.state.signed_in, true);
});

for (const [sc, code, reason, hours] of [["checkpoint", "challenge", "challenge", 24], ["feedback_required", "warning", "warning", 24],
                                          ["html_200", "signed_out", "signed_out", 0.5]]) {
  test(`${sc}: stop with ${code}, cooldown ${hours} h`, async () => {
    await resetAll();
    const acc = await newAccount();
    await scenario(sc);
    const jid = await pull(acc);
    await runUntilStopped(jid);
    await idle();
    const s = await state();
    assert.equal(s.device.state.cooldown_reason, reason);
    const h = (Date.parse(s.device.state.cooldown_until) - Date.now()) / 3600_000;
    assert.ok(h > hours - 0.1 && h <= hours, `cooldown ${h} h`);
    const fails = (await rv("/__test/requests")).data.requests.filter(q => q.path.endsWith("/fail"));
    assert.equal(JSON.parse(fails.at(-1).body).code, code);
    await scenario();
  });
}

// Fix round 1 (5.7): redirects are followed, so where Instagram sends us decides the stop.
test("302 to /challenge/: a 24 h checkpoint stop, not signed out; Try again does not clear it", async () => {
  await resetAll();
  const acc = await newAccount();
  await scenario("challenge_redirect");
  const jid = await pull(acc);
  await runUntilStopped(jid);
  await idle();
  const s = await state();
  assert.equal(s.device.state.cooldown_reason, "challenge");
  const h = (Date.parse(s.device.state.cooldown_until) - Date.now()) / 3600_000;
  assert.ok(h > 23.9 && h <= 24, `cooldown ${h} h`);
  assert.match(s.jobs.find(x => x.id === jid).error_message, /confirm it's you/);
  const fails = (await rv("/__test/requests")).data.requests.filter(q => q.path.endsWith("/fail"));
  assert.equal(JSON.parse(fails.at(-1).body).code, "challenge");
  assert.equal((await rv("/api/ext/try-again", {})).status, 400, "Try again is only for signed out");
  assert.equal((await state()).device.state.cooldown_reason, "challenge");
  await scenario();
});

test("302 to /accounts/suspended/: every Instagram job stops", async () => {
  await resetAll();
  const acc = await newAccount();
  await scenario("suspended_redirect");
  const jid = await pull(acc);
  await until(async () => (await state()).device.state.cooldown_reason === "suspended", {what: "suspended stop"});
  await idle();
  const j = await job(jid);
  assert.equal(j.state, "paused", JSON.stringify(j));
  assert.match(j.error_message, /limited your account/);
  assert.equal(await igCallsWhileTicking(2), 0);
  await scenario();
  await rv("/__test/cancel_all", {});
});

test("429, then Disconnect and Connect again in the popup: still stopped, zero Instagram calls", async () => {
  await resetAll();
  await scenario("429_on_page2");
  const acc = await newAccount();
  const jid = await pull(acc);
  await runUntilStopped(jid);
  await idle();
  assert.equal((await state()).device.state.cooldown_reason, "rate_limited");
  await scenario();
  const b = await rv("/__test/pull", {account_id: await newAccount("dental.growth.co"), mode: "first"});
  assert.equal(b.status, 200, JSON.stringify(b.data));
  const t0 = nowS();
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#status:not([hidden])");
  await popup.click("#disconnect");
  await popup.close();
  const cdAfterDisconnect = await swEval(async () => (await chrome.storage.local.get("cooldown")).cooldown);
  assert.equal(cdAfterDisconnect.reason, "rate_limited", "Disconnect keeps the stop");
  (await pairInPopup()).close();
  const s = await state();
  assert.equal(s.device.state.cooldown_reason, "rate_limited", "the new pairing carries the stop");
  if (s.device.ig_user_id) await rv("/api/ext/confirm-account", {ig_user_id: VIEWER});
  const st = (await rv("/api/ext/status")).data;
  assert.ok(st.device.cooldown_until, "the web UI still shows the wait");
  assert.equal(await igCallsWhileTicking(4), 0, "no Instagram call after re-pairing");
  assert.equal(igApi(since(await igLog(), t0)).length, 0, "not even the pairing sign-in check");
  await rv("/__test/cancel_all", {});
  await rv("/__test/clear_cooldown", {});
  await swEval(async () => { await chrome.storage.local.remove("cooldown"); return true; });
  const ig = await swEval(async () => (await chrome.storage.local.get("ig")).ig);
  if (!ig || !ig.signed_in) await swEval(async () => { await chrome.storage.local.set({ig: {signed_in: true, user_id: "61000777", username: "member.reader", checked_at: Date.now()}}); return true; });
  await swEval(() => self.rv.hello());
  await rv("/api/ext/confirm-account", {ig_user_id: VIEWER});
});

test("ds_user_id HttpOnly: the sign-in check falls back to current_user", async () => {
  await resetAll();
  await ctx.addCookies([{name: "ds_user_id", value: VIEWER, domain: ".instagram.com", path: "/", secure: true, httpOnly: true}]);
  await swEval(async () => { await chrome.storage.local.set({ig: {signed_in: true, user_id: "61000777", username: "member.reader", checked_at: 0}}); return true; });
  const acc = await newAccount();
  const t0 = nowS();
  const j = await runUntil(await pull(acc));
  assert.equal(j.state, "done", JSON.stringify(j));
  const paths = igApi(since(await igLog(), t0)).map(e => e.path);
  assert.equal(paths[0], "/api/v1/accounts/current_user/");
  assert.ok(!paths.includes(`/api/v1/users/${VIEWER}/info/`));
  const ig = await swEval(async () => (await chrome.storage.local.get("ig")).ig);
  assert.equal(ig.user_id, VIEWER);
  assert.ok(Date.now() - ig.checked_at < 60_000);
  await ctx.addCookies([{name: "ds_user_id", value: VIEWER, domain: ".instagram.com", path: "/", secure: true}]);
});

test("another Instagram account in Chrome: the job goes back to waiting until the member confirms or switches back", async () => {
  await resetAll();
  const PARTNER = "55501234";                      // make_fixtures.PARTNER: the member signed in with another account
  await ctx.addCookies([{name: "ds_user_id", value: PARTNER, domain: ".instagram.com", path: "/", secure: true}]);
  await swEval(async () => { const {ig} = await chrome.storage.local.get("ig"); await chrome.storage.local.set({ig: {...ig, checked_at: 0}}); return true; });
  const acc = await newAccount();
  const t0 = nowS();
  const jid = await pull(acc);
  await tickNow();
  await until(async () => (await state()).device.ig_user_id === PARTNER, {what: "the new account reported"});
  await idle();
  let s = await state();
  assert.equal(s.jobs.find(x => x.id === jid).state, "waiting");
  assert.equal(s.device.ig_username, "partner.studio");
  assert.ok(!igApi(since(await igLog(), t0)).some(e => e.path === "/api/v1/clips/user/"), "nothing read as the wrong account");
  const st = (await rv("/api/ext/status")).data;
  assert.equal(st.device.ig_confirmed, false);
  assert.equal(await swEval(() => chrome.action.getBadgeText({})), "!");

  // The member switches back in Chrome. Half an hour later, with the job still waiting, the extension checks again.
  await ctx.addCookies([{name: "ds_user_id", value: VIEWER, domain: ".instagram.com", path: "/", secure: true}]);
  const t1 = nowS();
  for (let i = 0; i < 3; i++) { await tickNow(); await sleep(300); await idle(); }
  assert.equal(igApi(since(await igLog(), t1)).length, 0, "no re-check within 30 min of the last one");
  await swEval(async () => { const {ig} = await chrome.storage.local.get("ig"); await chrome.storage.local.set({ig: {...ig, checked_at: Date.now() - 31 * 60_000}}); return true; });
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  s = await state();
  assert.equal(s.device.ig_user_id, VIEWER);
  assert.equal(s.reels.length, 100);
});

// ============================================================================================== tab and service worker restarts (5.3)

const readingPages = () => ctx.pages().filter(p => p.url().startsWith("https://www.instagram.com/"));

test("closing the reading tab mid-job: a new tab, the same call again, no cooldown", async () => {
  await resetAll();
  await scenario("slow");
  const acc = await newAccount();
  const t0 = nowS();
  const jid = await pull(acc);
  await tickNow();
  await until(async () => igApi(since(await igLog(), t0)).some(e => e.path === "/api/v1/clips/user/"), {what: "the first page"});
  const old = await swEval(async () => (await chrome.storage.session.get("readingTabId")).readingTabId);
  const pages = readingPages();
  assert.equal(pages.length, 1, "exactly one reading tab");
  await pages[0].close();
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  const s = await state();
  assert.equal(s.reels.length, 100);
  assert.equal(s.device.state.cooldown_until, null);
  const now = await swEval(async () => (await chrome.storage.session.get("readingTabId")).readingTabId);
  assert.notEqual(now, old, "a new reading tab");
  await scenario();
});

test("service worker stopped mid-job: the next start resumes the stored job", async () => {
  await resetAll();
  await scenario("slow");
  const acc = await newAccount();
  const t0 = nowS();
  const jid = await pull(acc);
  await tickNow();
  await until(async () => (await state()).reels.length >= 50, {what: "page 1 stored"});
  const stored = await swEval(async () => (await chrome.storage.local.get("job")).job);
  assert.equal(stored.job.id, jid, "the running job is kept in chrome.storage.local");
  await swEval(() => { self.__oldInstance = true; return true; });
  const page = await ctx.newPage();
  await page.goto(`chrome-extension://${extId}/popup.html`);
  const cdp = await ctx.newCDPSession(page);
  const {targetInfos} = await cdp.send("Target.getTargets");
  const target = targetInfos.find(x => x.type === "service_worker" && x.url.includes(extId));
  await cdp.send("Target.closeTarget", {targetId: target.targetId});   // what Chrome does to an idle worker
  // Any extension event starts it again (the 30 s alarm would too); here: the member opens the popup.
  await page.evaluate(() => chrome.runtime.sendMessage({type: "status"}));
  const fresh = await until(async () => {
    try { return {old: await swEval(() => self.__oldInstance === true)}; } catch { return false; }
  }, {what: "the service worker to start again"});
  assert.equal(fresh.old, false, "a new service worker instance (memory gone, storage kept)");
  await page.close();
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  const s = await state();
  assert.equal(s.reels.length, 100);
  assert.equal(new Set(s.reels.map(x => x.ig_pk)).size, 100);
  assert.equal(s.device.state.cooldown_until, null);
  const clips = igApi(since(await igLog(), t0)).filter(e => e.path === "/api/v1/clips/user/");
  assert.ok(clips.length >= 2 && clips.length <= 3, `${clips.length} clips pages`);
  await scenario();
});

// ============================================================================================== budget (5.6)

test("budget: 120 calls per sliding hour, enforced by the extension and by the server", async () => {
  await resetAll();
  const acc = await newAccount();
  // 119 Instagram calls in the last hour, as the extension's own counter has them.
  await swEval(async () => {
    const t = Date.now(), calls = Array.from({length: 119}, (_, i) => t - 50 * 60_000 + i * 1000);
    const d = new Date(t), day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    await chrome.storage.local.set({budget: {calls, day, day_used: 119, cdn_day_used: 0, disc_day_used: 0,
                                             page_read_day_used: 0, last_call_at: 0, last_cdn_at: 0}});
    return true;
  });
  const t0 = nowS();
  const jid = await pull(acc);
  const j = await runUntilStopped(jid);
  assert.match(j.error_message, /safe reading limit/);
  // Call 120 is the profile lookup (the account has no pk yet); call 121, the first page, is not made.
  assert.deepEqual(igApi(since(await igLog(), t0)).map(e => e.path), ["/api/v1/users/web_profile_info/"]);
  assert.equal((await state()).reels.length, 0);
  const wait = (Date.parse(j.run_after) - Date.now()) / 60_000;   // the oldest of the 119 calls was 50 min ago
  assert.ok(wait > 8 && wait <= 10.5, `waits ${wait.toFixed(1)} min for the sliding hour`);
  assert.equal((await state()).device.state.cooldown_until, null, "a budget stop is not an Instagram cooldown");
  const t1 = nowS();
  await tickNow(); await sleep(500); await idle();
  assert.equal((await swEval(() => self.rv.lastLease())).reason, "budget", "the server refuses Instagram work too");
  assert.equal(igApi(since(await igLog(), t1)).length, 0);
  const st = (await rv("/api/ext/status")).data;
  assert.equal(st.budget.hour_used, 120);
  assert.equal(st.budget.hour_max, 120);
});

// ============================================================================================== trust (5.4.0)

test("a job with a bad handle, pk or page link is refused with bad_job before any Instagram call", async () => {
  await resetAll();
  const acc = await newAccount();
  await rv("/__test/trust", {off: true});          // act like a compromised server
  try {
    const bad = [
      {kind: "ig_pull", payload: {account_id: acc, handle: "../evil", mode: "first", target: 100}},
      {kind: "ig_pull", payload: {account_id: acc, handle: "scaleit.demo", mode: "first", ig_pk: 25025320}},
      {kind: "ig_discover", payload: {run_id: "drun_abcdefghijklmnop", mode: "search", queries: ["x?target_id=1&y"]}},
      {kind: "ig_check", payload: {run_id: "drun_abcdefghijklmnop", candidates: [{cand_id: "cand_abcdefghijklmnop", handle: "a/../b"}]}},
      {kind: "ig_videos", payload: {items: [{reel_id: "reel_abcdefghijklmnop", ig_pk: "1/../../x", code: "C9xAbcde"}]}},
      {kind: "page_read", payload: {source_id: "src_abcdefghijklmnop", kind: "skool_profile", url: "https://evil.example.com/@x"}},
    ];
    const t0 = nowS();
    for (const b of bad) {
      const jid = (await rv("/__test/raw_job", b)).data.job_id;
      const j = await runUntil(jid, ["failed", "done", "canceled"], 20_000);
      assert.equal(j.state, "failed", JSON.stringify(b));
      assert.equal(j.error_code, "bad_job");
    }
    assert.equal(since(await igLog(), t0).length, 0, "no request reached Instagram, the CDN, Skool or LinkedIn");
  } finally { await rv("/__test/trust", {off: false}); }
});

// ============================================================================================== isolated world, profiles, discovery

test("the reader runs in the ISOLATED world: a page script that patches fetch never sees the call", async () => {
  await resetAll();
  await swEval(() => self.rv.closeReadingTab().then(() => true));
  await scenario("page_trap");                     // robots.txt becomes an HTML page whose script wraps window.fetch
  const acc = await newAccount();
  const t0 = nowS();
  const jid = (await rv("/__test/profile", {handle: "scaleit.demo", account_id: acc})).data.job_id;
  const j = await runUntil(jid);
  assert.equal(j.state, "done", JSON.stringify(j));
  const log = since(await igLog(), t0);
  assert.ok(log.some(e => e.path === "/api/v1/users/web_profile_info/"));
  assert.ok(!log.some(e => e.path === "/__trap"), "the page's patched fetch was never used");
  const a = (await state()).accounts.find(x => x.id === acc);
  assert.equal(a.ig_pk, PK);
  assert.equal(a.followers, 182000);
  assert.equal(a.full_name, "Demo Creator");
  await swEval(() => self.rv.closeReadingTab().then(() => true));
  await scenario();
});

test("discovery: search finds accounts, then a light check reads one page of 12 per candidate", async () => {
  await resetAll();
  const t0 = nowS();
  const r = (await rv("/__test/discover", {mode: "search", queries: ["facebook ads agency"]})).data;
  const j = await runUntil(r.job_id);
  assert.equal(j.state, "done", JSON.stringify(j));
  const check = await until(async () => (await state()).jobs.find(x => x.kind === "ig_check" && x.payload.run_id === r.run_id),
                           {what: "ig_check queued"});
  const jc = await runUntil(check.id);
  assert.equal(jc.state, "done", JSON.stringify(jc));
  const s = await state();
  assert.ok(s.candidates.length >= 2);
  assert.ok(s.candidates.every(c => c.status === "checked"), JSON.stringify(s.candidates.map(c => c.status)));
  assert.equal(s.reels.length, 0, "checked candidates' reels are not stored as tracked reels");
  const api = igApi(since(await igLog(), t0));
  const pages = api.filter(e => e.path === "/api/v1/clips/user/");
  assert.equal(pages.length, s.candidates.length);
  assert.ok(pages.every(e => e.form.page_size === "12"));
  assert.ok(api.some(e => e.path === "/web/search/topsearch/" && e.query.query === "facebook ads agency"));
});

test("the first answers of a device are sent once as key trees (values stripped) for Instagram health", async () => {
  const keys = (await state()).debug_samples.map(k => k.split(".").pop()).sort();
  for (const ep of ["clipsUser", "profileInfo"]) assert.ok(keys.includes(ep), `${ep} sample in ${keys}`);
  const sent = (await rv("/__test/requests")).data.requests.filter(q => q.method === "POST" && q.path.endsWith("/debug-sample"));
  assert.equal(new Set(sent.map(q => JSON.parse(q.body).endpoint)).size, sent.length, "once per endpoint");
  for (const q of sent) {
    assert.ok(!q.body.includes("scaleit.demo") && !q.body.includes(PK), "no values, only keys and type names");
  }
});

// ============================================================================================== page_read (the member's own pages)

test("page_read without permission: a full tab asks in plain words; Not now fails the job, nothing is opened", async () => {
  await resetAll();
  const r = (await rv("/__test/page_read", {kind: "skool_profile", url: "https://www.skool.com/@mia-member"})).data;
  const t0 = nowS();
  const grantSeen = ctx.waitForEvent("page", {predicate: p => p.url().includes("/grant.html"), timeout: 30_000});
  await tickNow();
  const grant = await grantSeen;
  await grant.waitForLoadState();
  assert.equal(await grant.textContent("#ask"),
               "ReelVault wants to read your own Skool profile page (www.skool.com) once, to fill in your profile.");
  assert.equal(await swEval(() => chrome.action.getBadgeText({})), "!");
  await grant.click("#no");
  const j = await runUntil(r.job_id, ["done", "failed"], 30_000);
  assert.equal(j.state, "failed");
  assert.equal(j.error_code, "permission_denied");
  const src = (await state()).sources.find(x => x.id === r.source_id);
  assert.equal(src.status, "needs_paste");
  assert.match(src.error, /Paste the text instead/);
  assert.equal(since(await igLog(), t0).filter(e => e.host === "www.skool.com").length, 0, "Skool was not opened");
  assert.equal(await swEval(() => chrome.permissions.contains({origins: ["https://www.skool.com/*"]})), false);
});

// ============================================================================================== idle, cookies, disconnect

test("idle Chrome makes zero Instagram requests (20 loop ticks plus a real 30 s alarm)", async () => {
  await resetAll();
  await sleep(500);
  const t0 = nowS();
  const leases0 = (await rv("/__test/requests")).data.requests.filter(q => q.path.endsWith("/jobs/lease")).length;
  for (let i = 0; i < 20; i++) { await tickNow(); await sleep(200); }  // what 10 minutes of alarms do
  await idle();
  const leases1 = (await rv("/__test/requests")).data.requests.filter(q => q.path.endsWith("/jobs/lease")).length;
  assert.ok(leases1 - leases0 >= 15, `the loop asked the server for work (${leases1 - leases0} leases)`);
  await until(async () => (await rv("/__test/requests")).data.requests.filter(q => q.path.endsWith("/jobs/lease")).length > leases1,
              {ms: 40_000, every: 1000, what: "a lease from the real alarm"});
  await idle();
  assert.deepEqual(since(await igLog(), t0), [], "no request to Instagram, its CDN, Skool or LinkedIn while idle");
  const hellos = (await rv("/__test/requests")).data.requests.filter(q => q.path.endsWith("/hello"));
  assert.ok(hellos.length >= 4, "hello is a server-only heartbeat");
});

test("no request to ReelVault ever carried an Instagram cookie or the session/CSRF values", async () => {
  const reqs = (await rv("/__test/requests")).data.requests;
  assert.ok(reqs.length > 100);
  for (const q of reqs) {
    assert.equal(q.headers.cookie, undefined, `${q.path} had a Cookie header`);
    assert.ok(!q.body.includes(SESSIONID) && !q.body.includes(CSRF), `${q.path} body holds a cookie value`);
    assert.ok(!/"(sessionid|csrftoken|cookie)"\s*:/i.test(q.body), `${q.path} body has a cookie field`);
    if (q.method === "POST" && !q.path.endsWith("/pair/claim")) assert.match(q.headers.authorization || "", /^Bearer rvd_/);
  }
});

test("Disconnect in the web UI revokes the token: the extension stops and the popup says Connect again", async () => {
  await resetAll();
  const r = await rv("/api/ext/device", undefined, {method: "DELETE"});
  assert.equal(r.status, 200);
  await tickNow();
  await until(() => swEval(async () => !!(await chrome.storage.local.get("revoked")).revoked), {what: "the 401"});
  const popup = await ctx.newPage();
  await popup.goto(`chrome-extension://${extId}/popup.html`);
  await popup.waitForSelector("#pair:not([hidden])");
  assert.equal(await popup.textContent("#error"), "This browser was disconnected from ReelVault. Connect again.");
  await popup.close();
  const n = (await rv("/__test/requests")).data.requests.length;
  await tickNow(); await sleep(500);
  assert.equal((await rv("/__test/requests")).data.requests.length, n, "a revoked extension calls nothing");
  assert.equal((await rv("/api/ext/status")).data.paired, false);
});

// ============================================================================================== page_read with the permission granted

test("page_read with the permission granted: Skool __NEXT_DATA__, LinkedIn /in/me/ text, Skool login page", async () => {
  await ctx.close();
  // The state after the member pressed Allow: a copy whose manifest already holds the two optional hosts (Chrome's
  // native permission prompt can't be clicked in a headless browser).
  const dir = copyExtension("ext2", mf => { mf.host_permissions.push(...mf.optional_host_permissions); delete mf.optional_host_permissions; });
  ({ctx, sw, extId} = await launch(dir, "profile2"));
  const popup = await pairInPopup();
  await popup.close();
  await resetAll();
  assert.equal(await swEval(() => chrome.permissions.contains({origins: ["https://www.skool.com/*"]})), true);

  const sk = (await rv("/__test/page_read", {kind: "skool_profile", url: "https://www.skool.com/@mia-member"})).data;
  let j = await runUntil(sk.job_id, ["done", "failed", "paused"], 40_000);
  assert.equal(j.state, "done", JSON.stringify(j));
  let src = (await state()).sources.find(x => x.id === sk.source_id);
  assert.equal(src.status, "ok");
  assert.equal(src.via, "extension");
  assert.match(src.text, /Meta ads agency for dentists/);
  const up = (await rv("/__test/requests")).data.requests.find(q => q.path.endsWith(`/jobs/${sk.job_id}/page`));
  assert.ok(!up.body.includes('"secret"') && !up.body.includes("buildId"), "only props.pageProps.{currentGroup, currentUser}");
  assert.equal(ctx.pages().filter(p => p.url().includes("skool.com")).length, 0, "the page tab is closed again");

  const li = (await rv("/__test/page_read", {kind: "linkedin_self", url: "https://www.linkedin.com/in/me/"})).data;
  j = await runUntil(li.job_id, ["done", "failed", "paused"], 40_000);
  assert.equal(j.state, "done", JSON.stringify(j));
  src = (await state()).sources.find(x => x.id === li.source_id);
  assert.equal(src.status, "ok");
  assert.match(src.text, /Mia Member Founder, Dentist Ads Co\./);
  assert.equal(src.meta.final_url, "https://www.linkedin.com/in/mia-member/");

  const out = (await rv("/__test/page_read", {kind: "skool_profile", url: "https://www.skool.com/@nobody"})).data;
  j = await runUntil(out.job_id, ["done", "failed", "paused"], 40_000);
  assert.equal(j.state, "paused");
  assert.equal(j.error_message, "Sign in to Skool (or LinkedIn) in Chrome, then press Try again.");
  assert.equal((await state()).sources.find(x => x.id === out.source_id).status, "needs_paste");
  assert.equal((await state()).device.state.cooldown_until, null, "a page read never puts Instagram in a cooldown");
});

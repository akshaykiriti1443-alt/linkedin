// Integration journey for ReelVault Members (integration pass, DESIGN-SAAS.md 15.1 D76): the whole member path on a
// dev server (web + worker), the REAL extension in Chromium (Playwright), a fake Instagram with real synthetic videos
// (journey_ig.py), a fake OpenAI-compatible AI (journey_server.py), then the Claude-connector path through the
// official mcp client (journey_mcp.py) and the desktop worker with the real Claude Code CLI (journey_desktop.py).
//
//   RV_PYTHON=<python with the [server] deps> RV_MODELS_DIR=<filled models folder, deploy/fetch_models.sh> \
//   NODE_PATH=$(npm root -g) node tests/journey/journey.mjs
//
// Env: RV_JOURNEY_OUT (default <tmp>/rv-journey: data, logs, screenshots), RV_JOURNEY_PORT_BASE (default 9300:
// app +1, fake Instagram +3, fake AI +4, website +5, OAuth loopback +6), ONLY_SETUP=1 (stop after scoring),
// SKIP_AI_PATHS=1 (no connector/desktop part), KEEP=1 (leave the servers running). Not part of pytest: about
// 15-30 minutes, needs Chromium + Playwright for Node, and the desktop step uses this computer's Claude Code login.
// Everything is made-up data on 127.0.0.1; the only outside call is the desktop step's own `claude -p`.
import {spawn} from "node:child_process";
import {mkdirSync, rmSync, cpSync, readFileSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";

const require = createRequire(path.join(process.env.NODE_PATH || "", "x.js"));
const {chromium} = require("playwright");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const PY = process.env.RV_PYTHON || "python3";
const OUT = process.env.RV_JOURNEY_OUT || path.join(tmpdir(), "rv-journey");
const RUN = path.join(OUT, "run");
const SHOTS = path.join(OUT, "screenshots");
const B = Number(process.env.RV_JOURNEY_PORT_BASE || 9300);
const PORT = B + 1, IG = B + 3, AI = B + 4, SITE = B + 5;
const BASE_URL = `http://localhost:${PORT}`;
const VIEWER = "61000777";
const KEEP = !!process.env.KEEP;
const ONLY_SETUP = !!process.env.ONLY_SETUP;

const procs = [];
const results = [];
let info, admin, ctx, sw, extId, page;

const sleep = ms => new Promise(r => setTimeout(r, ms));
function log(...a) { console.log(new Date().toISOString().slice(11, 19), ...a); }
function ok(step, detail) { results.push({step, ok: true, detail}); log("PASS", step, detail ? JSON.stringify(detail).slice(0, 400) : ""); }
function bad(step, detail) { results.push({step, ok: false, detail}); log("FAIL", step, JSON.stringify(detail).slice(0, 2000)); }
async function check(step, fn, {soft = false} = {}) {
  try { const d = await fn(); ok(step, d); return d; } catch (e) {
    bad(step, String(e && e.stack || e));
    try { if (page) await shot(page, "FAIL-" + step.replace(/[^a-z0-9]+/gi, "-").slice(0, 40)); } catch { /* */ }
    if (!soft) throw new Error("stopped at: " + step);
    return null;
  }
}
async function until(fn, {ms = 60_000, every = 500, what = "condition"} = {}) {
  const end = Date.now() + ms;
  let last;
  while (Date.now() < end) {
    try { last = await fn(); } catch (e) { last = null; }
    if (last) return last;
    await sleep(every);
  }
  throw new Error(`timed out waiting for ${what}`);
}

function spawnPy(args, name) {
  const p = spawn(PY, args, {cwd: REPO, env: {...process.env, PYTHONUNBUFFERED: "1", RV_JOURNEY_PORT_BASE: String(B)}, stdio: ["ignore", "pipe", "pipe"]});
  let out = "";
  p.stdout.on("data", d => { out += d; });
  p.stderr.on("data", d => { out += d; writeFileSync(path.join(RUN, name + ".log"), out); });
  p.output = () => out;
  procs.push(p);
  return p;
}

async function shot(p, name) {
  await p.screenshot({path: path.join(SHOTS, name + ".png"), fullPage: true});
}
async function ready(p, re) {
  await p.waitForFunction(src => {
    const d = document.documentElement.dataset;
    return d.view === "ready" && d.ready === location.hash && new RegExp(src).test(location.hash);
  }, re ? re.source : "", {timeout: 30000});
}
async function view(p, hash) {
  const target = BASE_URL + "/" + hash;
  if (p.url() === target) await p.reload(); else await p.goto(target);   // same URL + hash: goto would not redraw
  await ready(p);
  await p.waitForTimeout(200);
}
async function login(p, email, password) {
  await view(p, "#login");
  await p.fill("#login-email", email);
  await p.fill("#login-password", password);
  await p.click("form button[type=submit]");
  await ready(p, /^#(?!login)/);
}
async function api(p, method, url, body) {   // the member's own session (cookie + CSRF from /api/me)
  return p.evaluate(async ([method, url, body]) => {
    const me = await (await fetch("/api/me")).json();
    const r = await fetch(url, {method, headers: {"Content-Type": "application/json", "X-CSRF-Token": me.csrf_token || (me.user || {}).csrf_token},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const t = await r.text();
    let d; try { d = JSON.parse(t); } catch { d = t; }
    return {status: r.status, data: d};
  }, [method, url, body]);
}
const getj = (p, url) => api(p, "GET", url);

async function tick() { try { await sw.evaluate(() => { self.rv.tick("now"); return true; }); } catch { /* sw restarting */ } }
async function extBusy() { try { return await sw.evaluate(() => self.rv.busy()); } catch { return true; } }

/** Tick the extension until no Instagram job is waiting/running for the member (or until `done()` says so). */
async function runExtension(what, done, ms = 15 * 60_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const j = await getj(page, "/api/jobs?active=1");
    const ext = (j.data.jobs || []).filter(x => x.executor === "extension");
    if (done ? await done(ext) : ext.length === 0) return ext;
    if (!(await extBusy())) await tick();
    await sleep(1500);
  }
  const j = await getj(page, "/api/jobs?active=1");
  throw new Error(`${what}: extension work still waiting: ${JSON.stringify(j.data).slice(0, 1500)}`);
}

function copyExtension() {
  const dir = path.join(RUN, "ext");
  rmSync(dir, {recursive: true, force: true});
  cpSync(path.join(REPO, "extension-members"), dir, {recursive: true});
  const cfg = readFileSync(path.join(dir, "config.js"), "utf8")
    .replace(/^export const SERVERS = .*$/m, `export const SERVERS = ${JSON.stringify([BASE_URL])};`)
    .replace(/^export const BUILD = .*$/m, `export const BUILD = "test";`);
  writeFileSync(path.join(dir, "config.js"), cfg);
  return dir;
}

async function setup() {
  rmSync(RUN, {recursive: true, force: true});
  mkdirSync(RUN, {recursive: true});
  if (!process.env.RV_MODELS_DIR) log("RV_MODELS_DIR is not set: signals run without CED-tiny, YuNet and whisper models");
  mkdirSync(SHOTS, {recursive: true});
  spawnPy([path.join(HERE, "journey_ig.py"), "--port", String(IG), "--cert-dir", path.join(RUN, "cert")], "fake-ig");
  const srv = spawnPy([path.join(HERE, "journey_server.py"), "--port", String(PORT), "--data-dir", path.join(RUN, "data"),
    "--ai-port", String(AI), "--site-port", String(SITE), "--ig-port", String(IG)], "server");
  info = await until(() => { const m = srv.output().match(/READY (\{.*\})\n/); return m && JSON.parse(m[1]); }, {ms: 120_000, what: "server READY"});
  await until(async () => (await fetch(BASE_URL + "/healthz")).ok, {ms: 60_000, what: "healthz"});
  const extDir = copyExtension();
  const hosts = ["www.instagram.com", "scontent.cdninstagram.com", "www.skool.com", "www.linkedin.com"];
  ctx = await chromium.launchPersistentContext(path.join(RUN, "profile"), {
    channel: "chromium", headless: true, ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900},
    args: [`--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`, "--no-proxy-server",
      `--host-resolver-rules=${hosts.map(h => `MAP ${h} 127.0.0.1:${IG}`).join(", ")}`, "--ignore-certificate-errors"]});
  sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker", {timeout: 30_000});
  extId = new URL(sw.url()).host;
  ctx.on("serviceworker", w => { sw = w; });
  await ctx.addCookies([
    {name: "sessionid", value: "SESSIONsecret987", domain: ".instagram.com", path: "/", secure: true, httpOnly: true},
    {name: "ds_user_id", value: VIEWER, domain: ".instagram.com", path: "/", secure: true},
    {name: "csrftoken", value: "csrfTOKENabc123", domain: ".instagram.com", path: "/", secure: true}]);
  admin = await (await chromium.launch()).newContext({viewport: {width: 1280, height: 900}});
  log("setup done", info.port, extId);
}

async function teardown() {
  writeFileSync(path.join(OUT, "journey-results.json"), JSON.stringify(results, null, 2));
  if (KEEP) { log("KEEP=1: servers left running"); return; }
  try { await ctx.close(); } catch { /* */ }
  try { await admin.browser().close(); } catch { /* */ }
  for (const p of procs) p.kill("SIGTERM");
  await sleep(1500);
  for (const p of procs) if (p.exitCode === null) p.kill("SIGKILL");
}

function errorsOf(p) {
  p.errs = [];
  p.on("console", m => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text())) p.errs.push(`console: ${m.text()}`); });
  p.on("pageerror", e => p.errs.push(`pageerror: ${e.message}`));
  p.on("response", r => { if (r.url().startsWith(BASE_URL) && r.status() >= 500) p.errs.push(`HTTP ${r.status()} ${r.url()}`); });
}

// ============================================================================================== the journey
let inviteUrl, accounts = {};
const EMAIL = "dana@clinicgrowth.example", PASS = "dana-long-pass-42";

async function journey() {
  // 1. admin: invite + settings
  const ap = await admin.newPage();
  errorsOf(ap);
  await check("admin signs in", async () => { await login(ap, info.admin_email, info.admin_password); return ap.url(); });
  inviteUrl = await check("admin makes a single-use invite link", async () => {
    await view(ap, "#admin/invites");
    await ap.fill("#invite-label", "Dana from the coaching call");
    await ap.click("#make-invite");
    const t = await until(async () => { const s = await ap.textContent("main"); const m = s.match(/https?:\/\/[^\s#]+#join\/[A-Z0-9]{4}(?:-[A-Z0-9]{4})+/); return m && m[0]; }, {what: "invite url"});
    await shot(ap, "01-admin-invite");
    return t;
  });
  await check("admin raises pulls per day to 10 and media per member to 300 for the test", async () => {
    await view(ap, "#admin/settings");
    await ap.fill("#set-max_pulls_per_day", "10");
    await ap.fill("#set-member_media_per_day", "300");
    await ap.click("#save-settings");
    await sleep(800);
    const s = await getj(ap, "/api/admin/settings");
    if (s.data.max_pulls_per_day !== 10 || s.data.member_media_per_day !== 300) throw new Error(JSON.stringify(s.data).slice(0, 300));
    return s.data.max_pulls_per_day;
  });

  // 2. member signs up with the invite
  page = await ctx.newPage();
  errorsOf(page);
  await check("member signs up with the invite link (auto-approved)", async () => {
    await page.goto(inviteUrl.replace(/^https?:\/\/[^/]+/, BASE_URL));
    await ready(page);
    await page.fill("#join-name", "Dana Clinic");
    await page.fill("#join-email", EMAIL);
    await page.fill("#join-password", PASS);
    await page.fill("#join-skool", "https://www.skool.com/@mia-member");
    await shot(page, "02-signup");
    await page.click("form button[type=submit]");
    await ready(page, /^#onboarding/);
    return page.url();
  });

  // 3. onboarding step 1: website + 2 role models
  await check("step 1: add website (local test page) and 2 role models", async () => {
    await page.fill("#src-website", info.site);
    await page.click("text=Add website");
    await until(async () => { const r = await getj(page, "/api/profile"); const s = (r.data.sources || []).find(x => x.kind === "website"); return s && s.status === "ok" && s; }, {ms: 60_000, what: "website source ok"});
    await page.fill("#seed-1", "@dental.growth.co");
    await page.fill("#seed-2", "@smile.marketing");
    await shot(page, "03-onboarding-1");
    await page.click("#step1-next");
    await ready(page, /^#onboarding\/2$/);
    const r = await getj(page, "/api/profile");
    const w = r.data.sources.find(x => x.kind === "website");
    return {website: w.status, chars: (w.text || w.preview || "").length, sources: r.data.sources.map(s => `${s.kind}:${s.status}`)};
  });

  // 4. step 2: pair the REAL extension through its popup, confirm the account
  await check("step 2: pair the real extension and confirm the Instagram account", async () => {
    await page.click("#get-pair-link");
    const link = await until(async () => { const t = await page.evaluate(() => document.querySelector("#pair-box").innerText + " " + [...document.querySelectorAll("#pair-box input")].map(i => i.value).join(" ")); const m = t.match(/https?:\/\/\S+\/pair#[A-Z0-9-]+/); return m && m[0]; }, {what: "pair link"});
    const popup = await ctx.newPage();
    await popup.goto(`chrome-extension://${extId}/popup.html`);
    await popup.waitForSelector("#pair:not([hidden])");
    await popup.fill("#link", link);
    await popup.click("#next");
    await popup.waitForSelector("#confirm:not([hidden])");
    await popup.click("#connect");
    await popup.waitForSelector("#connected:not([hidden])", {timeout: 30_000});
    const igLine = await popup.textContent("#ig-line");
    await shot(popup, "04-popup-connected");
    await popup.close();
    await page.waitForSelector("#ext-confirm >> text=@member.reader", {timeout: 30_000});
    await shot(page, "05-onboarding-2-confirm");
    await page.click("#confirm-account");
    await page.waitForSelector("text=Keep Chrome open", {timeout: 15_000});
    return {link, igLine};
  });
  await check("first pulls: 100 reels each for both role models through the extension", async () => {
    await runExtension("first pulls", ext => !ext.some(j => j.kind === "ig_pull"), 10 * 60_000);
    const r = await getj(page, "/api/accounts");
    accounts = Object.fromEntries(r.data.accounts.map(a => [a.handle, a]));
    const counts = r.data.accounts.map(a => [a.handle, a.reels_count]);
    for (const a of r.data.accounts) if ((a.reels_count || 0) < 100) throw new Error("fewer than 100 reels: " + JSON.stringify(a));
    await shot(page, "06-onboarding-2-after-pulls");
    return counts;
  });
  await check("step 2 -> 3", async () => { await page.click("#step2-next"); await ready(page, /^#onboarding\/3$/); return true; });

  // 5. step 3: API key (fake OpenAI-compatible server)
  await check("step 3: save an API key (fake AI)", async () => {
    await page.click('button[data-mode="api"]');
    await page.selectOption("#key-provider-all", "openai_compat");
    await page.selectOption("#key-preset-all", "custom");
    await page.fill("#key-base-all", info.ai_base);
    await page.fill("#key-model-text-all", "llava-vision-test");
    await page.fill("#key-value-all", "sk-test-1234567890abcd");
    await page.click("#key-save-all");
    await page.waitForSelector("#step3-next:has-text('Continue')", {timeout: 30_000});
    await shot(page, "07-onboarding-3-ai");
    await page.click("#step3-next");
    await ready(page, /^#onboarding\/4$/);
    return true;
  });

  // 6. step 4: questionnaire + approve
  await check("step 4: questionnaire, review and approve", async () => {
    await page.fill("#ans-q1", "Dental clinics that want 30 new patients a month");
    await page.fill("#ans-q3-0", "Ads stop working after two weeks");
    await page.fill("#ans-q3-1", "Leads never pick up the phone");
    await page.fill("#ans-q3-2", "Agencies vanish after the first month");
    await page.fill("#ans-q7", "Done-for-you Meta ads for clinics, 2,500 a month");
    await page.check('[data-q="q12"] input[value="on_camera"]');
    await page.fill("#ans-q12-hours", "5");
    await shot(page, "08-onboarding-4-questions");
    await page.click("#step4-review");
    await page.waitForSelector("#review");
    await page.click("#approve-profile");
    await page.click("#step4-next");
    await ready(page, /^#onboarding\/5$/);
    const p = await getj(page, "/api/profile");
    return {status: p.data.status, version: p.data.version};
  });
  await check("step 5: first look shows outliers within minutes", async () => {
    await page.waitForSelector("h1:has-text('Your first look:')", {timeout: 60_000}).catch(async () => { await page.reload(); await page.waitForSelector("h1:has-text('Your first look:')", {timeout: 60_000}); });
    await shot(page, "09-onboarding-5-first-look");
    const h1 = await page.textContent("main h1");
    await page.click("#onboarding-finish");
    await ready(page, /^#home$/);
    return h1;
  });

  // 7. third competitor from the Competitors page
  await check("add a 3rd competitor on the Competitors page and pull it", async () => {
    await view(page, "#competitors");
    await page.fill("#add-handles", "@clinic.ads.lab");
    await page.click("#add-competitors");
    await sleep(1000);
    await runExtension("3rd pull", ext => !ext.some(j => j.kind === "ig_pull"), 10 * 60_000);
    const r = await getj(page, "/api/accounts");
    accounts = Object.fromEntries(r.data.accounts.map(a => [a.handle, a]));
    await view(page, "#competitors");
    await shot(page, "10-competitors");
    return r.data.accounts.map(a => [a.handle, a.reels_count]);
  });

  // 8. next 100 for each competitor from the competitor page
  await check("pull the next 100 for each competitor (saved cursor)", async () => {
    for (const a of Object.values(accounts)) {
      await view(page, "#competitor/" + a.id);
      await page.click("text=Pull the next 100 reels");
      await sleep(600);
    }
    await runExtension("next pulls", ext => !ext.some(j => j.kind === "ig_pull"), 15 * 60_000);
    const r = await getj(page, "/api/accounts");
    const out = [];
    for (const a of r.data.accounts) {
      const d = await getj(page, "/api/accounts/" + a.id);
      out.push({handle: a.handle, reels: a.reels_count, pulls: (d.data.account.pulls || []).map(p => `${p.mode}:${p.stop_reason}:${p.reels_new}`)});
      if ((a.reels_count) < 200) throw new Error("expected 200 reels: " + JSON.stringify(out));
    }
    await view(page, "#competitor/" + r.data.accounts[0].id);
    await shot(page, "11-competitor-after-next");
    return out;
  });

  await check("scoring: outliers found per account", async () => {
    await until(async () => { const r = await getj(page, "/api/reels?band=breakout,big,outlier&limit=200"); return r.data.counts && r.data.counts.outlier + (r.data.counts.big || 0) + (r.data.counts.breakout || 0) >= 9 && r; }, {ms: 120_000, what: "outliers"});
    const r = await getj(page, "/api/reels?band=breakout,big,outlier&limit=200&sort=score");
    await view(page, "#reels?band=breakout,big,outlier");
    await shot(page, "12-outliers");
    return {counts: r.data.counts, top: r.data.reels.slice(0, 5).map(x => [x.account.handle, x.score, x.band, x.views])};
  });
  if (ONLY_SETUP) return;

  // 9. media: videos through the extension, then signals on the server, tags by the fake AI
  await check("media + signals + tags for the priority set and the baseline sample", async () => {
    const end = Date.now() + 40 * 60_000;
    let last;
    while (Date.now() < end) {
      if (!(await extBusy())) await tick();
      const r = await getj(page, "/api/reels?limit=1");
      const jobsr = await getj(page, "/api/jobs?active=1");
      const act = jobsr.data.jobs || [];
      last = {pipeline: r.data.pipeline, active: act.length, kinds: [...new Set(act.map(j => j.kind + ":" + j.state))]};
      if (act.length === 0) break;
      await sleep(4000);
      if (Math.random() < 0.08) log("media", JSON.stringify(last));
    }
    const st = await getj(page, "/api/home");
    return {last, home: Object.keys(st.data)};
  });

  await check("reel page shows signals: cuts, sound map, visual stats", async () => {
    const r = await getj(page, "/api/reels?band=breakout,big,outlier&sort=score&limit=20");
    let got = null;
    for (const x of r.data.reels) {
      const d = await getj(page, "/api/reels/" + x.id);
      if (d.data.reel.signals && d.data.reel.quick) { got = d.data.reel; break; }
    }
    if (!got) throw new Error("no outlier with signals and quick tags");
    await view(page, `#reel/${got.id}`);
    await shot(page, "13-reel");
    for (const t of ["format", "sound", "transcript"]) { await view(page, `#reel/${got.id}?tab=${t}`); await shot(page, `13-reel-${t}`); }
    const s = got.signals;
    return {id: got.id, vf: got.quick.visual_format, cuts: (s.cuts || []).length, sfx: ((s.sound || {}).events || (s.sound || {}).sfx || []).length,
      faces: (s.visual || {}).face_share ?? (s.visual || {}).faces, local_vf: (s.visual || {}).local_vf, transcript: ((s.transcript || {}).text || "").slice(0, 60)};
  });

  // 10. insights with lift
  await check("insights: visual format lift", async () => {
    // the debounced refresh after the last tags (60 s): rows for several visual formats, not the early snapshot
    const r = await until(async () => { const x = await getj(page, "/api/insights?dimension=visual_format"); return x.data.rows && x.data.rows.length >= 3 && x; }, {ms: 300_000, every: 3000, what: "insights rows for 3+ formats"});
    await view(page, "#working");
    await shot(page, "14-working");
    return r.data.rows.slice(0, 6).map(x => [x.value || x.key, x.label, x.lift, x.n ?? x.reels, x.evidence]);
  });
  await check("sub-format playbooks", async () => {
    const f = await until(async () => { const x = await getj(page, "/api/formats"); return x.data.formats && x.data.formats.length && x; }, {ms: 180_000, what: "formats"});
    const first = f.data.formats[0];
    const d = await getj(page, "/api/formats/" + first.id);
    await view(page, "#format/" + first.id);
    await shot(page, "15-format-playbook");
    await view(page, "#home");
    await shot(page, "16-home");
    return {n: f.data.formats.length, first: [first.name, first.lift, first.vf], guide: !!(d.data.format.guide), examples: (d.data.format.examples || []).length};
  });

  // 11. deep analysis of the best outliers (for the scripts' weaknesses and the cue sheet)
  await check("deep analysis of the top outliers", async () => {
    const r = await api(page, "POST", "/api/reels/analyze-best", {limit: 3});
    const ids = r.data.reel_ids || [];
    await until(async () => { if (!(await extBusy())) await tick(); for (const id of ids) { const d = await getj(page, "/api/reels/" + id); if (d.data.reel.states.deep !== "done") return false; } return true; }, {ms: 600_000, every: 2000, what: "deep analyses"});
    const d = await getj(page, "/api/reels/" + ids[0]);
    return {count: r.data.count, reel_ids: ids, weaknesses: ((d.data.reel.deep || {}).weaknesses || []).length};
  });

  // 12. outputs from the UI
  const outs = {};
  for (const [kind, extra] of [["reel_script", async () => { await page.selectOption("#out-count", "1"); }],
    ["meta_ads", async () => { await page.fill("#out-offer", "free ad audit"); }],
    ["content_plan", async () => {}], ["competitor_report", async () => {}]]) {
    await check(`output: ${kind}`, async () => {
      await view(page, "#create/" + kind);
      await extra();
      await page.click("#out-submit");
      await ready(page, /^#output\//);
      const id = page.url().split("#output/")[1];
      await until(async () => { const o = await getj(page, "/api/outputs/" + id); return o.data.output && o.data.output.status === "done"; }, {ms: 300_000, what: kind + " done"});
      await view(page, "#output/" + id);
      await shot(page, `17-output-${kind}`);
      outs[kind] = id;
      const o = await getj(page, "/api/outputs/" + id);
      return {id, title: o.data.output.title, md_chars: (o.data.output.markdown || "").length};
    });
  }
  await check("downloads: md, txt (editor brief + sound cue sheet), csv, print", async () => {
    const res = {};
    for (const [kind, id] of Object.entries(outs)) {
      for (const f of ["md", "txt", "csv", "print"]) {
        const r = await page.evaluate(async u => { const x = await fetch(u); return {s: x.status, t: (await x.text())}; }, `/api/outputs/${id}/download?format=${f}`);
        res[`${kind}.${f}`] = r.s;
        if (kind === "reel_script" && f === "txt") {
          writeFileSync(path.join(RUN, "editor-brief.txt"), r.t);
          if (!/sound/i.test(r.t)) throw new Error("editor brief has no sound cue sheet:\n" + r.t.slice(0, 800));
        }
      }
    }
    return res;
  });
  await check("Remake this on an outlier", async () => {
    const r = await getj(page, "/api/reels?band=breakout,big,outlier&sort=score&limit=1");
    await view(page, `#reel/${r.data.reels[0].id}`);
    await page.click(".reel-right .buildon >> text=Remake this");
    await ready(page, /^#output\//);
    const id = page.url().split("#output/")[1];
    await until(async () => { const o = await getj(page, "/api/outputs/" + id); return o.data.output.status === "done"; }, {ms: 300_000, what: "remake"});
    return id;
  });
  await check("no console errors or 5xx in the member's pages", async () => { if (page.errs.length) throw new Error(page.errs.join("\n")); return 0; });
  await check("no console errors or 5xx in the admin's pages", async () => { if (ap.errs.length) throw new Error(ap.errs.join("\n")); return 0; });
  writeFileSync(path.join(OUT, "journey-state.json"), JSON.stringify({info, email: EMAIL, password: PASS, accounts, outs}, null, 2));
  if (!process.env.SKIP_AI_PATHS) await aiPaths();
}

/** Run a python helper while the extension keeps working (videos for the new reels); returns its SUMMARY. */
async function runHelper(args, name, env = {}, ms = 30 * 60_000) {
  const p = spawn(PY, args, {cwd: REPO, env: {...process.env, PYTHONUNBUFFERED: "1", RV_JOURNEY_PORT_BASE: String(B), ...env}, stdio: ["ignore", "pipe", "pipe"]});
  let out = "";
  p.stdout.on("data", d => { out += d; });
  p.stderr.on("data", d => { out += d; });
  const end = Date.now() + ms;
  while (p.exitCode === null && Date.now() < end) {
    if (!(await extBusy())) await tick();
    await sleep(2000);
  }
  if (p.exitCode === null) p.kill("SIGTERM");
  writeFileSync(path.join(RUN, name + ".log"), out);
  const m = out.match(/SUMMARY (\{.*\})/);
  return {code: p.exitCode, summary: m ? JSON.parse(m[1]) : null, tail: out.slice(-3000)};
}

async function newWorkOn(n) {   // n unprocessed ("rest") reels with the best scores -> deep reads (media -> tags -> deep)
  const r = await getj(page, "/api/reels?sort=score&limit=200");
  const rest = r.data.reels.filter(x => x.tier === "rest" && x.states.media === "none").slice(0, n);
  for (const x of rest) await api(page, "POST", `/api/reels/${x.id}/analyze`, {});
  return rest.map(x => x.id);
}

async function aiPaths() {
  // ---- Claude connector: the official mcp client does OAuth and the AI work through the tools
  await check("switch to the Claude connector in Settings > AI", async () => {
    await view(page, "#settings/ai");
    await page.click('button[data-mode="claude_connector"]');
    await page.click("#use-connector");
    await until(async () => (await getj(page, "/api/ai")).data.mode === "claude_connector", {what: "mode"});
    await view(page, "#settings/ai");
    await shot(page, "20-settings-ai-connector");
    return (await getj(page, "/api/ai")).data.connector;
  });
  const mcpReels = await check("queue new work for Claude: 2 deep reads + a Meta ads output", async () => {
    const ids = await newWorkOn(2);
    await view(page, "#create/meta_ads");
    await page.fill("#out-offer", "free ad audit");
    await page.click("#out-submit");
    await ready(page, /^#output\//);
    const id = page.url().split("#output/")[1];
    await page.waitForSelector("#waiting-claude", {timeout: 20_000});
    await shot(page, "21-output-waiting-for-claude");
    return {reels: ids, output: id};
  });
  await check("Claude connector: OAuth + tools via the official mcp client", async () => {
    const r = await runHelper([path.join(HERE, "journey_mcp.py"), "--base", BASE_URL, "--email", EMAIL, "--password", PASS,
      "--seconds", "1200", "--idle-rounds", "24"], "mcp");
    if (!r.summary) throw new Error("no summary:\n" + r.tail);
    if (r.summary.errors.length) throw new Error(JSON.stringify(r.summary).slice(0, 3000));
    return r.summary;
  });
  await check("connector work landed: output written, reels tagged and analyzed", async () => {
    const o = await getj(page, "/api/outputs/" + mcpReels.output);
    if (o.data.output.status !== "done") throw new Error("output " + o.data.output.status);
    const st = [];
    for (const id of mcpReels.reels) { const d = await getj(page, "/api/reels/" + id); st.push(d.data.reel.states); }
    await view(page, "#output/" + mcpReels.output);
    await shot(page, "22-output-by-claude-connector");
    if (!st.every(x => x.tags === "done" && x.deep === "done")) throw new Error(JSON.stringify(st));
    const ai = await getj(page, "/api/ai");
    return {states: st, connector: ai.data.connector};
  });

  // ---- Desktop app: members_worker with the real Claude Code CLI
  const code = await check("switch to the desktop app and get a pairing code", async () => {
    await view(page, "#settings/ai");
    await page.click('button[data-mode="desktop"]');
    await page.click("#ai-desktop button:has-text('Get a code')");   // not the bold "Get a code" in the steps
    const c = await until(async () => { const t = await page.textContent("#ai-desktop"); const m = t.match(/[A-Z0-9]{4}-[A-Z0-9]{4}/); return m && m[0]; }, {what: "desktop code"});
    await page.click("#use-desktop");
    await until(async () => (await getj(page, "/api/ai")).data.mode === "desktop", {what: "desktop mode"});
    await shot(page, "23-settings-ai-desktop");
    return c;
  });
  const deskReels = await check("queue one deep read for the desktop app", async () => newWorkOn(1));
  await check("wait until the desktop reel's signals are done (server)", async () => {
    await until(async () => { if (!(await extBusy())) await tick(); const d = await getj(page, "/api/reels/" + deskReels[0]); return d.data.reel.states.signals === "done"; }, {ms: 600_000, what: "signals"});
    return true;
  });
  await check("desktop worker with the real Claude Code CLI: tags + deep read", async () => {
    const r = await runHelper([path.join(HERE, "journey_desktop.py"), "--server", BASE_URL, "--code", code, "--max-jobs", "4", "--idle-s", "60"],
      "desktop", {REELVAULT_HOME: path.join(RUN, "desktop-home")}, 20 * 60_000);
    if (!r.summary) throw new Error("no summary:\n" + r.tail);
    const d = await getj(page, "/api/reels/" + deskReels[0]);
    const q = d.data.reel.quick || {};
    if (d.data.reel.states.tags !== "done" || d.data.reel.states.deep !== "done") throw new Error(JSON.stringify({states: d.data.reel.states, jobs: r.summary.jobs}));
    await view(page, `#reel/${deskReels[0]}?tab=why`);
    await shot(page, "24-reel-by-desktop-claude");
    return {jobs: r.summary.jobs, vf: q.visual_format, subject: q.visual_subject, weak: ((d.data.reel.deep || {}).weaknesses || []).length};
  }, {soft: true});
  await check("no console errors or 5xx after the AI paths", async () => { if (page.errs.length) throw new Error(page.errs.join("\n")); return 0; });
}

try {
  await setup();
  await journey();
} catch (e) {
  bad("journey crashed", String(e && e.stack || e));
} finally {
  await teardown();
  const f = results.filter(r => !r.ok);
  log(`DONE: ${results.length - f.length} passed, ${f.length} failed`);
  process.exit(f.length ? 1 : 0);
}

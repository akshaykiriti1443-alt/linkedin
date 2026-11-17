// Web UI tests for ReelVault Members (WP7, DESIGN-SAAS.md 12 and section 13 "WP7").
// Runs the real app with seeded data (tests/web/serve.py) and Chromium through Playwright for Node.
//   node --test tests/web/ui.test.mjs        (NODE_PATH=$(npm root -g) when Playwright is installed globally)
// Ports: RV_WEB_PORT_BASE (default 9240): app base+1, fake extension control base+2, fake AI base+11 (or RV_WEB_AI_PORT).
import test from "node:test";
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createServer} from "node:http";
import {mkdtempSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";

const require = createRequire(path.join(process.env.NODE_PATH || "", "x.js"));
let chromium;
try { ({chromium} = require("playwright")); } catch { ({chromium} = createRequire(import.meta.url)("playwright")); }

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const BASE = Number(process.env.RV_WEB_PORT_BASE || 9240);
const PORT = BASE + 1, CTL = BASE + 2, AI = Number(process.env.RV_WEB_AI_PORT || BASE + 11);
const URL = `http://localhost:${PORT}`;   // = PUBLIC_URL, so the CSRF origin check passes
const PY = process.env.RV_PYTHON || "python3";
const ONEX = `http://localhost:${BASE + 5}`;   // plays OneX (serve.py sets ONEX_URL to it); Playwright serves its page
const LIGHT_BG = "rgb(251, 249, 248)", DARK_BG = "rgb(13, 11, 26)";   // OneX's --background

let server, info, dataDir, browser;

async function startServer() {
  dataDir = mkdtempSync(path.join(tmpdir(), "rv-web-"));
  server = spawn(PY, [path.join(HERE, "serve.py"), "--port", String(PORT), "--data-dir", dataDir, "--fake-ai-port", String(AI),
    "--control-port", String(CTL)], {cwd: ROOT, stdio: ["ignore", "pipe", "pipe"]});
  let log = "";
  server.stderr.on("data", d => { log += d; });
  info = await new Promise((resolve, reject) => {
    let buf = "";
    const t = setTimeout(() => reject(new Error("serve.py did not start:\n" + log.slice(-4000))),
      Number(process.env.RV_WEB_START_MS || 180000));   // slow machines (seeding without ffmpeg on Windows) can raise it
    server.stdout.on("data", d => {
      buf += d;
      const m = buf.match(/READY (\{.*\})\r?\n/);   // \r: Python on Windows writes CRLF to a pipe
      if (m) { clearTimeout(t); resolve(JSON.parse(m[1])); }
    });
    server.on("exit", c => { clearTimeout(t); reject(new Error(`serve.py exited ${c}:\n${log.slice(-4000)}`)); });
  });
  // wait for uvicorn
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(URL + "/healthz")).ok) return; } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 200));
  }
  throw new Error("server did not answer /healthz");
}

async function ctl(pathname, body) {
  const r = await fetch(`http://127.0.0.1:${CTL}${pathname}`, {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body)});
  const d = await r.json();
  assert.equal(r.status, 200, JSON.stringify(d));
  return d;
}

/** A page that records console errors and failed requests to our own server. */
async function newPage(width, scheme) {
  const ctx = await browser.newContext({viewport: {width, height: 900}, colorScheme: scheme});
  const page = await ctx.newPage();
  page.errors = [];
  // A 4xx answer is a normal, handled outcome (for example a refused form); Chrome still logs it. 5xx is caught below.
  page.on("console", m => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text())) page.errors.push(`console: ${m.text()}`); });
  page.on("pageerror", e => page.errors.push(`pageerror: ${e.message}`));
  page.on("response", r => { if (r.url().startsWith(URL) && r.status() >= 500) page.errors.push(`HTTP ${r.status()} ${r.url()}`); });
  return page;
}
/** Waits until the SPA finished drawing the page for the current hash (app.js sets data-ready = location.hash). */
async function ready(page, re) {
  await page.waitForFunction(src => {
    const d = document.documentElement.dataset;
    return d.view === "ready" && d.ready === location.hash && new RegExp(src).test(location.hash);
  }, re ? re.source : "", {timeout: 20000});
}
async function view(page, hash) {
  await page.goto(URL + "/" + hash);
  await ready(page);
  await page.waitForTimeout(150);
}
async function login(page, email, password) {
  await view(page, "#login");
  await page.fill("#login-email", email);
  await page.fill("#login-password", password);
  await page.click("form button[type=submit]");
  await ready(page, /^#(?!login)/);
}
async function checkScreen(page, label) {
  const [sw, iw, h1, broken, stray] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth,
    (document.querySelector("main h1") || {}).textContent || "", /didn't load/.test(document.body.innerText),
    ((document.querySelector("main") || document.body).innerText.match(/\b(null|undefined|NaN)\b/) || [])[0] || ""]);
  assert.equal(stray, "", `${label}: the page shows the word "${stray}"`);
  assert.ok(sw <= iw, `${label}: sideways scroll (${sw} > ${iw})`);
  assert.ok(h1.trim().length > 0, `${label}: no page title`);
  assert.ok(!broken, `${label}: the page did not load`);
  assert.deepEqual(page.errors, [], `${label}: errors`);
}

// The fake OneX is a real server on 127.0.0.1 (a page Playwright fulfils has no address, and Chrome's local network
// access checks then block a localhost frame inside it). It serves oneXHtml, which each embed test sets.
let oneX, oneXHtml = "";
test.before(async () => {
  await startServer();
  oneX = createServer((_req, res) => { res.writeHead(200, {"content-type": "text/html; charset=utf-8"}); res.end(oneXHtml); });
  await new Promise(r => oneX.listen(BASE + 5, "127.0.0.1", r));
  browser = await chromium.launch();
});
test.after(async () => {
  if (oneX) oneX.close();
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    server.kill("SIGTERM");
    await new Promise(r => { server.on("exit", r); setTimeout(r, 8000); });
  }
  // Windows keeps the SQLite file locked for a moment after the server exits: retry, then leave it to the OS.
  if (dataDir) { try { rmSync(dataDir, {recursive: true, force: true, maxRetries: 10, retryDelay: 500}); } catch { /* temp folder */ } }
});

const MEMBER_ROUTES = ["#home", "#competitors", "#reels", "#reels?band=breakout,big,outlier&sort=newest", "#working",
  "#working?dimension=hook_type&window=recent", "#banks/hooks", "#banks/formats", "#banks/ctas", "#banks/sounds", "#banks/music",
  "#create", "#create/reel_script", "#create/meta_ads", "#create/content_plan", "#create/competitor_report", "#outputs",
  "#settings/ai", "#settings/extension", "#settings/profile", "#settings/topics", "#settings/account",
  "#onboarding/1", "#onboarding/2", "#onboarding/3", "#onboarding/4", "#onboarding/5", "#privacy", "#help", "#help/extension-zip",
  "#help/second-account", "#help/desktop-app", "#nothing-here"];
const ADMIN_ROUTES = ["#admin/members", "#admin/members?status=pending", "#admin/invites", "#admin/settings", "#admin/copy",
  "#admin/health", "#admin/support"];

for (const [width, scheme] of [[1280, "light"], [1280, "dark"], [390, "light"], [390, "dark"]]) {
  test(`every member and admin screen at ${width}px ${scheme}: no errors, no sideways scroll`, async () => {
    const page = await newPage(width, scheme);
    await view(page, "#login");
    await checkScreen(page, "login");
    await view(page, "#join/" + info.community_code);
    await checkScreen(page, "join");
    await login(page, info.member_email, info.member_password);
    const ids = await page.evaluate(async () => {
      const g = async u => (await fetch(u)).json();
      return {acc: (await g("/api/accounts")).accounts.map(a => a.id), fmt: (await g("/api/formats")).formats.map(f => f.id)};
    });
    const routes = [...MEMBER_ROUTES, `#competitor/${ids.acc[0]}`, `#discover/${info.discover_run}`, `#format/${ids.fmt[0]}`,
      ...info.top_reels.slice(0, 2).map(r => `#reel/${r}`), ...["why", "format", "hook", "sound", "weak", "script", "transcript"].map(t => `#reel/${info.top_reels[0]}?tab=${t}`),
      ...Object.values(info.outputs).map(o => `#output/${o}`)];
    for (const r of routes) {
      await view(page, r);
      await checkScreen(page, `${width} ${scheme} ${r}`);
    }
    // dark/light really applies: the page background follows the scheme
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert.equal(bg, scheme === "dark" ? DARK_BG : LIGHT_BG, `body background ${bg} for ${scheme}`);
    // standalone keeps its own navigation; embed mode never turns on outside a frame
    assert.equal(await page.locator("nav.nav").count(), 1);
    assert.equal(await page.evaluate(() => document.documentElement.dataset.embed || ""), "");
    await page.context().close();
    const admin = await newPage(width, scheme);
    await login(admin, info.admin_email, info.admin_password);
    for (const r of ADMIN_ROUTES) {
      await view(admin, r);
      await checkScreen(admin, `${width} ${scheme} admin ${r}`);
    }
    await admin.context().close();
  });
}

test("a new member signs up, waits, gets approved and goes through all 5 onboarding steps", async () => {
  const email = `new${Date.now()}@example.com`;
  const page = await newPage(390, "light");
  // signup -> pending
  await view(page, "#join/" + info.community_code);
  await page.fill("#join-name", "New Member");
  await page.fill("#join-email", email);
  await page.fill("#join-password", "a-long-pass-123");
  await page.fill("#join-skool", "https://www.skool.com/@new-member");
  await page.click("form button[type=submit]");
  await page.waitForSelector("text=You're on the list");
  assert.match(await page.textContent("main"), /admin will approve you soon/);
  // admin approves
  const admin = await newPage(1280, "dark");
  await login(admin, info.admin_email, info.admin_password);
  await view(admin, "#admin/members?status=pending");
  const row = admin.locator(".card", {hasText: email});
  await row.getByRole("button", {name: "Approve"}).click();
  await admin.waitForSelector(`.card:has-text("${email}")`, {state: "detached"});   // no longer waiting
  await view(admin, "#admin/members?q=" + encodeURIComponent(email));
  await admin.waitForSelector(`.card:has-text("${email}") >> text=Active`);
  // the member logs in and lands on step 1
  await login(page, email, "a-long-pass-123");
  assert.match(page.url(), /#onboarding\/1/);
  await page.fill("#seed-1", "@other.creator");
  await page.fill("#own-ig", "@new.member");
  await page.fill("#src-website", "not a link");
  await page.click("text=Add website");
  await page.waitForSelector("text=doesn't look like a website link");
  await page.click("#step1-next");
  await ready(page, /^#onboarding\/2$/);
  // step 2: pair through the fake extension, confirm the account
  await page.click("#get-pair-link");
  const code = (await page.textContent("#pair-box b")).trim();
  assert.match(code, /^[A-Z0-9]{4}-[A-Z0-9]{4}$/);
  const dev = await ctl("/claim", {code});
  await page.waitForSelector("#ext-confirm >> text=@member.reader", {timeout: 15000});
  await page.click("#confirm-account");
  await page.waitForSelector("text=Keep Chrome open");
  await page.click("#read-links");
  await page.waitForSelector("text=Reading 1 link");
  await page.click("#find-similar");
  await ready(page, /^#discover\//);
  // the extension does the Instagram work: pulls, bio, discovery, then the account checks
  await ctl("/work", {device_id: dev.device_id});
  await page.waitForTimeout(1500);
  await ctl("/work", {device_id: dev.device_id});
  await page.waitForSelector("text=Top 10 we found", {timeout: 20000});
  const boxes = page.locator('[data-cand] input[type=checkbox]');
  assert.ok(await boxes.count() >= 1);
  await boxes.first().check();
  await page.click("#track-selected");
  await ready(page, /^#onboarding\/3$/);
  // step 3: an API key (the fake OpenAI-compatible server)
  await page.click('button[data-mode="api"]');
  await page.selectOption("#key-provider-all", "openai_compat");
  await page.selectOption("#key-preset-all", "custom");
  await page.fill("#key-base-all", `http://127.0.0.1:${AI}/v1`);
  await page.fill("#key-model-text-all", "llava-vision-test");
  await page.fill("#key-value-all", "sk-test-1234567890abcd");
  await page.click("#key-save-all");
  await page.waitForSelector("#step3-next:has-text('Continue')", {timeout: 20000});
  await page.click("#step3-next");
  await ready(page, /^#onboarding\/4$/);
  // step 4: 4 quick questions, review, approve (exact question copy, 9.1)
  assert.match(await page.textContent("main"), /Who do you help\?/);
  assert.match(await page.textContent("main"), /Their top 3 problems, in their words/);
  assert.match(await page.textContent("main"), /What do you sell\?/);
  assert.match(await page.textContent("main"), /What can you actually make\?/);
  await page.click("#step4-review");
  await page.waitForSelector("text=Answer questions 1, 3, 7 and 12 first.");
  await page.fill("#ans-q1", "I help dental clinics get 30 new patients a month");
  await page.fill("#ans-q3-0", "Ads stop working after two weeks");
  await page.fill("#ans-q3-1", "Leads never pick up");
  await page.fill("#ans-q3-2", "Agencies vanish");
  await page.fill("#ans-q7", "Done-for-you Meta ads, 2,500 a month");
  await page.check('[data-q="q12"] input[value="on_camera"]');
  await page.fill("#ans-q12-hours", "5");
  await page.click("#step4-review");
  await page.waitForSelector("#review");
  await page.click("#approve-profile");
  await page.click("#step4-next");
  // step 5: the first look from the pulled competitor (metrics only, minutes after the pull)
  await ready(page, /^#onboarding\/5$/);
  await page.waitForSelector("h1:has-text('Your first look:')", {timeout: 30000}).catch(async () => {
    await page.reload(); await page.waitForSelector("h1:has-text('Your first look:')", {timeout: 30000});
  });
  assert.ok(await page.locator(".rcard").count() >= 1);
  assert.ok(await page.locator(".rcard >> text=Remake this").count() >= 1);
  assert.match(await page.textContent("main"), /Instagram hides saves/);
  await page.click("#onboarding-finish");
  await ready(page, /^#home$/);
  assert.match(await page.textContent("main"), /Your first look|New outliers/);
  // the setup is complete: profile, AI, Chrome, competitors
  const me = await page.evaluate(async () => (await fetch("/api/me")).json());
  assert.deepEqual([me.setup.profile, me.setup.ai, me.setup.extension, me.setup.competitors >= 2], ["approved", true, "paired", true]);
  const prof = await page.evaluate(async () => (await fetch("/api/profile")).json());
  const bio = prof.sources.find(s => s.kind === "instagram_bio");
  assert.equal(bio.status, "ok");
  assert.deepEqual(page.errors.filter(e => !/HTTP 4/.test(e)), []);
  await admin.context().close();
  await page.context().close();
});

test("build on what works: format playbook -> Write 3 scripts, reel -> Remake this, Fix tags, downloads", async () => {
  const page = await newPage(1280, "light");
  await login(page, info.member_email, info.member_password);
  // Home: format playbooks are the main card
  await view(page, "#home");
  const card = page.locator(".fcard").first();
  await card.locator("text=Open the playbook").click();
  await ready(page, /^#format\//);
  assert.match(await page.textContent("main"), /Can you make this\?/);
  await page.click("#write-3");
  await ready(page, /^#output\//);
  await page.waitForSelector(".ocard >> text=Copy the script", {timeout: 60000});
  // Reel page: Remake this
  await view(page, `#reel/${info.top_reels[0]}`);
  for (const b of ["Remake this", "Use this format", "Borrow this hook"]) assert.ok(await page.locator(`.reel-right .buildon >> text=${b}`).count() >= 1, b);
  await page.click(".reel-right .buildon >> text=Remake this");
  await ready(page, /^#output\//);
  await page.waitForSelector(".ocard >> text=Copy the script", {timeout: 60000});
  const outId = page.url().split("#output/")[1];
  for (const f of ["txt", "csv", "md", "print"]) {
    const st = await page.evaluate(async u => (await fetch(u)).status, `/api/outputs/${outId}/download?format=${f}`);
    assert.equal(st, 200, f);
  }
  // Fix tags
  await view(page, `#reel/${info.top_reels[0]}?tab=format`);
  await page.click("#fix-tags summary");
  await page.selectOption("#fix-hook", "question");
  await page.click("#fix-save");
  await page.waitForSelector("text=Saved. What works updates in a minute.");
  const reel = await page.evaluate(async id => (await fetch("/api/reels/" + id)).json(), info.top_reels[0]);
  assert.equal(reel.reel.overrides.hook_type, "question");
  // Reels filters keep working from the URL
  await view(page, "#reels?band=breakout");
  assert.ok(await page.locator(".rcard").count() >= 1);
  assert.equal(await page.locator(".rcard .bandchip:not(.b-breakout)").count(), 0);
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

test("signed-out OAuth round trip: login goes to /oauth/authorize only", async () => {
  const page = await newPage(1280, "light");
  await view(page, "#login?next=" + encodeURIComponent("https://evil.example/steal"));
  await page.fill("#login-email", info.member_email);
  await page.fill("#login-password", info.member_password);
  await page.click("form button[type=submit]");
  await ready(page, /^#(?!login)/);
  assert.ok(page.url().startsWith(URL), "never leaves the site for a foreign next");
  await page.context().close();
});

// ============================================================================================ embed mode (D86)
/** A page at the fake OneX origin that frames ReelVault and records what the frame posts (with its origin). */
function oneXPage(src) {
  return `<!doctype html><html><head><meta charset="utf-8"><title>OneX</title></head><body style="margin:0">
<script>
window.msgs = [];
addEventListener("message", e => {
  const f = document.getElementById("rv");
  if (!f || e.source !== f.contentWindow) return;
  window.msgs.push({origin: e.origin, data: e.data});
  if (e.data && e.data.type === "rv:height") f.style.height = Math.max(e.data.height, 320) + "px";
});
</script>
<iframe id="rv" title="ReelVault" src="${src}" style="display:block;width:100%;height:640px;border:0"></iframe>
</body></html>`;
}
async function openInOneX(page, query, hash) {
  oneXHtml = oneXPage(`${URL}/?${query}${hash}`);
  await page.goto(ONEX + "/app/w/test/reelvault");
  const handle = await page.waitForSelector("#rv");
  return handle.contentFrame();
}
const msgs = page => page.evaluate(() => window.msgs);
async function waitMsg(page, type, pred = () => true) {
  await page.waitForFunction(t => window.msgs.some(m => m.data && m.data.type === t), type, {timeout: 20000});
  return (await msgs(page)).filter(m => m.data.type === type).find(m => pred(m.data));
}

test("embed: inside OneX there is no ReelVault nav, messages go to the OneX origin only, the theme follows OneX", async () => {
  const page = await newPage(1280, "light");
  await login(page, info.member_email, info.member_password);
  // record every localStorage key each document reads or writes (the frame must never touch "rv.theme")
  await page.context().addInitScript(() => {
    const P = Storage.prototype, g = P.getItem, s = P.setItem, r = P.removeItem;
    window.__ls = []; window.__lsGet = g;
    const mine = st => { try { return st === window.localStorage; } catch (e) { return false; } };
    P.getItem = function (k) { if (mine(this)) window.__ls.push("get:" + k); return g.call(this, k); };
    P.setItem = function (k, v) { if (mine(this)) window.__ls.push("set:" + k); return s.call(this, k, v); };
    P.removeItem = function (k) { if (mine(this)) window.__ls.push("remove:" + k); return r.call(this, k); };
  });
  const frame = await openInOneX(page, "embed=1&parent=" + encodeURIComponent(ONEX) + "&theme=dark", "#reels");
  const ready = await waitMsg(page, "rv:ready");
  assert.equal(ready.data.route, "reels");
  assert.ok(ready.data.title.length > 0);
  await page.waitForFunction(() => window.msgs.some(m => m.data && m.data.type === "rv:height" && m.data.height > 300), null, {timeout: 20000});
  for (const m of (await msgs(page)).filter(x => x.data.type === "rv:height")) {
    assert.ok(Number.isInteger(m.data.height) && m.data.height >= 0 && m.data.height <= 100000, JSON.stringify(m.data));
  }
  for (const m of await msgs(page)) {
    assert.equal(m.origin, URL, "every message comes from ReelVault");
    assert.ok(/^rv:/.test(m.data.type), m.data.type);
    assert.ok(!JSON.stringify(m.data).includes(info.member_email), "no email in a message");
  }
  // no ReelVault chrome: no nav, brand, account links or toast; html is auto-height
  assert.equal(await frame.locator(".nav").count(), 0);
  assert.equal(await frame.locator("text=Log out").count(), 0);
  assert.equal(await frame.evaluate(() => [document.documentElement.dataset.embed, document.documentElement.dataset.parent,
    getComputedStyle(document.documentElement).overflowY].join(" ")), `1 ${ONEX} hidden`);
  assert.ok(await frame.locator("#main h1").count() >= 1);
  // the theme from the URL, then OneX's toggle (onex:theme), never ReelVault's own saved theme
  assert.equal(await frame.evaluate(() => getComputedStyle(document.body).backgroundColor), DARK_BG);
  await page.evaluate(o => document.getElementById("rv").contentWindow.postMessage({type: "onex:theme", theme: "light"}, o), URL);
  await frame.waitForFunction(() => document.documentElement.dataset.theme === "light");
  assert.equal(await frame.evaluate(() => getComputedStyle(document.body).backgroundColor), LIGHT_BG);
  assert.equal(await frame.evaluate(() => window.__lsGet.call(localStorage, "rv.theme")), null);
  // onex:navigate replaces the hash (no new history entry) and the frame answers with rv:route
  const len = await frame.evaluate(() => history.length);
  await page.evaluate(o => document.getElementById("rv").contentWindow.postMessage({type: "onex:navigate", route: "outputs"}, o), URL);
  const routed = await waitMsg(page, "rv:route", d => d.route === "outputs");
  assert.ok(routed, "rv:route for outputs");
  assert.equal(await frame.evaluate(() => location.hash), "#outputs");
  assert.equal(await frame.evaluate(() => history.length), len);
  // a bad route and a foreign message type are ignored
  await page.evaluate(o => { const w = document.getElementById("rv").contentWindow;
    w.postMessage({type: "onex:navigate", route: "admin/members"}, o); w.postMessage({type: "rv:ready"}, o); }, URL);
  await page.waitForTimeout(400);
  assert.equal(await frame.evaluate(() => location.hash), "#outputs");
  // a click inside the frame posts rv:route; Settings hides Look, password, Delete and Log out
  await frame.evaluate(() => { location.hash = "#settings/account"; });
  await waitMsg(page, "rv:route", d => d.route === "settings/account");
  await frame.waitForSelector("text=Your data");
  for (const t of ["Change your password", "Delete my account", "Like my device"]) assert.equal(await frame.locator(`text=${t}`).count(), 0, t);
  assert.deepEqual((await frame.evaluate(() => window.__ls)).filter(k => k.endsWith(":rv.theme")), [],
    "the frame never reads or writes localStorage rv.theme, not even on Settings");
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

test("embed: a wrong parent gets no messages; a direct visit is never in embed mode", async () => {
  const page = await newPage(1280, "light");
  await login(page, info.member_email, info.member_password);
  const frame = await openInOneX(page, "embed=1&parent=" + encodeURIComponent("http://localhost:9998"), "#home");
  await frame.waitForFunction(() => document.documentElement.dataset.view === "ready", null, {timeout: 20000});
  await page.waitForTimeout(800);
  assert.deepEqual(await msgs(page), [], "messages only go to the parent the frame was told about");
  // Chrome tells the frame its real parent (location.ancestorOrigins): a parent that is not it never turns embed on
  assert.equal(await frame.evaluate(() => [document.documentElement.dataset.embed || "",
    sessionStorage.getItem("rv.embed") || ""].join("|")), "|");
  const top = await newPage(1280, "light");
  await login(top, info.member_email, info.member_password);
  await top.goto(`${URL}/?embed=1&parent=${encodeURIComponent(ONEX)}#home`);
  await ready(top, /^#home$/);
  assert.equal(await top.evaluate(() => document.documentElement.dataset.embed || ""), "");
  assert.equal(await top.locator("nav.nav").count(), 1);
  await top.context().close();
  await page.context().close();
});

test("embed: signed out inside OneX asks OneX to sign in again and never shows the login form", async () => {
  const page = await newPage(390, "dark");
  const frame = await openInOneX(page, "embed=1&parent=" + encodeURIComponent(ONEX), "#reels");
  await waitMsg(page, "rv:signed-out");
  assert.equal(await frame.locator("#login-email").count(), 0);
  await frame.waitForSelector("text=Signing you in");
  assert.equal((await msgs(page)).filter(m => m.data.type === "rv:signed-out").length, 1);
  // the SSO error page inside OneX tells OneX what went wrong (rv:error)
  await page.evaluate(src => { window.msgs = []; document.getElementById("rv").src = src; },
    `${URL}/sso/onex?t=nonsense&embed=1&parent=${encodeURIComponent(ONEX)}`);
  const err = await waitMsg(page, "rv:error");
  assert.equal(err.origin, URL);
  assert.equal(err.data.code, "bad_link");
  assert.equal(err.data.message, "This sign-in link is not valid.");
  // a parent the server did not accept never reaches the frame's memory: the error page takes only the theme,
  // and its rv:error goes to the checked OneX origin
  await page.evaluate(src => { window.msgs = []; document.getElementById("rv").src = src; },
    `${URL}/sso/onex?embed=1&theme=dark&parent=${encodeURIComponent("https://typo.example")}`);
  const err2 = await waitMsg(page, "rv:error");
  assert.equal(err2.data.code, "bad_link", "no ticket at all is a bad link, not a JSON error");
  const rvFrame = (await page.waitForSelector("#rv")).contentFrame();
  const seen = await (await rvFrame).evaluate(() => [document.documentElement.dataset.parent || "",
    document.documentElement.dataset.theme || "", sessionStorage.getItem("rv.embed") || ""]);
  assert.equal(seen[0], "");
  assert.equal(seen[1], "dark");
  assert.notEqual(seen[2], "https://typo.example");
  await page.context().close();
});

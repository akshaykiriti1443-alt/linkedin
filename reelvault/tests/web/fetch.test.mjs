// Web UI tests for the fetch engine screens (DESIGN-FETCH.md section 5), with every API answer mocked.
// The real web files are served by a tiny static server; page.route() answers /api/* with the contract shapes.
//   node --test tests/web/fetch.test.mjs     (NODE_PATH=$(npm root -g) when Playwright is installed globally)
// Screens: Settings card, pull lines (competitors list + detail), "Test my connection", admin "Fetch health",
// at 1280 px and 390 px, light and dark: no console errors, no sideways scroll. RV_WEB_SHOTS=<dir> saves screenshots.
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import {readFile, mkdir} from "node:fs/promises";
import path from "node:path";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";

const require = createRequire(path.join(process.env.NODE_PATH || "", "x.js"));
let chromium;
try { ({chromium} = require("playwright")); } catch { ({chromium} = createRequire(import.meta.url)("playwright")); }

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(HERE, "..", "..", "reelvault", "web");
const SHOTS = process.env.RV_WEB_SHOTS || "";
const TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html", ".json": "application/json"};

let server, URL, browser;

test.before(async () => {
  server = http.createServer(async (req, res) => {
    const u = new globalThis.URL(req.url, "http://x");
    let file = null;
    if (u.pathname === "/") file = path.join(WEB, "index.html");
    else if (u.pathname.startsWith("/static/")) {
      file = path.normalize(path.join(WEB, decodeURIComponent(u.pathname)));
      if (!file.startsWith(path.join(WEB, "static") + path.sep)) file = null;
    }
    if (!file) { res.writeHead(404, {"content-type": "application/json"}); res.end('{"error":"not here"}'); return; }
    try {
      const body = await readFile(file);
      res.writeHead(200, {"content-type": TYPES[path.extname(file)] || "application/octet-stream"});
      res.end(body);
    } catch { res.writeHead(404); res.end(); }
  });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  URL = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch();
});
test.after(async () => {
  if (browser) await browser.close();
  if (server) await new Promise(r => server.close(r));
});

// ============================================================================================ mocked API (contract shapes)
const iso = (min = 0) => new Date(Date.now() - min * 60000).toISOString();
const ME = role => ({id: role === "admin" ? "usr_admin" : "usr_member", email: role === "admin" ? "owner@reelvault.local" : "member@example.com",
  name: "Test", role, status: "active", csrf_token: "csrf-test", admin: {pending: 0},
  limits: {max_accounts: 10, pulls_per_day: 5, outputs_per_day: 20}, setup: {profile: "approved", ai: true, extension: "paired", competitors: 2}});
const SETTINGS = {read_mode: "auto", default: "auto", options: [
  {id: "auto", label: "Safest first", recommended: true, text: "ReelVault reads without your login first. It uses your login only for what is missing."},
  {id: "logged_in_only", label: "Logged-in only", text: "ReelVault always reads with your Instagram login. It stays slow and careful."},
  {id: "logged_out_only", label: "Logged-out only", text: "ReelVault never uses your login. You may get fewer reels and no view counts."}]};
const STATUS = {read_mode: "auto", device: {connected: true, version: "2.1.0", needs_update: false}, layers: [
  {id: "logged_out", label: "Without your login", state: "ok", detail: "Works. Last read 2 hours ago.", cooldown_until: null, last_ok_at: iso(120), last_blocked_at: null},
  {id: "logged_in", label: "With your Instagram login", state: "cooling", detail: "Instagram asked us to slow down.", cooldown_until: iso(-45), last_ok_at: iso(300), last_blocked_at: iso(10)}],
  last_probe: {id: "prb_aaaaaaaaaaaaaaaa", at: iso(60 * 26), verdict: {code: "best", title: "The safest way works."}}};
const EXT = {paired: true, latest_version: "2.1.0", download_url: "/download/x.zip", budget: {hour_used: 14, hour_max: 120, day_used: 60, day_max: 400, cdn_day_used: 3, cdn_day_max: 150},
  device: {id: "dev_x", name: "Chrome", version: "2.1.0", ig_username: "member.reader", ig_confirmed: true, signed_in: true, online: true, last_seen_at: iso(1), last_seen_text: "The extension is on."}};
const ACC = id => ({id, handle: id === "acc_aaaaaaaaaaaaaaaa" ? "scaleit.demo" : "other.creator", full_name: "Demo Studio", status: "active", followers: 48200,
  reels_count: 100, outliers: 7, baseline_views: 12000, last_pull_at: iso(90), pulling: false, cursor_done: false, role: "competitor",
  pulls: [{id: "pul_aaaaaaaaaaaaaaaa", mode: "first", reels_new: 100, reels_updated: 0, stop_reason: "target_reached", started_at: iso(100), finished_at: iso(90)},
    {id: "pul_bbbbbbbbbbbbbbbb", mode: "refresh", reels_new: 4, reels_updated: 12, stop_reason: "known_reel", started_at: iso(30), finished_at: iso(28)}]});
const PULLS = {pulls: [
  {id: "pul_bbbbbbbbbbbbbbbb", account_id: "acc_aaaaaaaaaaaaaaaa", handle: "scaleit.demo", mode: "refresh", state: "done", started_at: iso(30), finished_at: iso(28), reels_new: 4,
    read: {mode: "auto", layer: "logged_out", logged_out: {reels: 4, calls: 3, stop: "refresh_page"}, logged_in: {reels: 0, pages: 0}, message: "Read without your Instagram login."}},
  {id: "pul_aaaaaaaaaaaaaaaa", account_id: "acc_aaaaaaaaaaaaaaaa", handle: "scaleit.demo", mode: "first", state: "done", started_at: iso(100), finished_at: iso(90), reels_new: 100,
    read: {mode: "auto", layer: "mixed", logged_out: {reels: 24, calls: 18, stop: "no_views"}, logged_in: {reels: 76, pages: 3}, message: "Read 24 reels without your login. Your login filled in the other 76."}},
  {id: "pul_cccccccccccccccc", account_id: "acc_bbbbbbbbbbbbbbbb", handle: "other.creator", mode: "first", state: "done", started_at: iso(200), finished_at: iso(190), reels_new: 100,
    read: {mode: "auto", layer: "logged_in", logged_out: {reels: 0, calls: 2, stop: "blocked"}, logged_in: {reels: 100, pages: 9}, message: "Instagram asks for a login right now. ReelVault used yours."}}],
  next_cursor: null};
const ROW = (strategy, layer, label, status, status_label, gives, note, ms, handle = "instagram") => ({strategy, layer, label, handle, status, status_label, gives, note, ms});
const ROWS = [
  ROW("lo_profile_page", "logged_out", "Profile page", "ok", "Works", "Followers and the newest posts", null, 640),
  ROW("lo_reels_page", "logged_out", "Reels page", "partial", "Works in part", "The newest reels with view counts", "The page has no data inside.", 712),
  ROW("lo_reels_gql", "logged_out", "Reels list", "blocked", "Blocked", "More reels with view counts", "Instagram asks for a login here.", 210),
  ROW("lo_web_profile", "logged_out", "Profile data", "ok", "Works", "Followers and 12 posts with view counts", null, 455),
  ROW("lo_posts_gql", "logged_out", "Posts list", "ok", "Works", "Older posts", null, 530),
  ROW("lo_post_gql", "logged_out", "Reel detail", "ok", "Works", "One reel with likes, date, caption and video", null, 388),
  ROW("lo_media_gql", "logged_out", "Reel detail, second way", "ok", "Works", "One reel with likes, date, caption and video", null, 402),
  ROW("lo_embed", "logged_out", "Embed page", "error", "Error", "One reel with views and video", "Instagram didn't answer.", 5012),
  ROW("lo_profile_page", "logged_out", "Profile page", "ok", "Works", "Followers and the newest posts", null, 610, "scaleit.demo"),
  ROW("li_session", "logged_in", "Sign-in check", "ok", "Works", "Your Instagram login works in Chrome", null, 12, null),
  ROW("li_clips", "logged_in", "Reels list", "skipped", "Skipped", "Reels with view counts", "ReelVault waits right now.", null, "scaleit.demo")];
const VERDICTS = {
  best: {code: "best", title: "The safest way works.", text: "ReelVault reads reels without your Instagram login on this connection.", next: "You don't need to do anything."},
  partial: {code: "partial", title: "The safest way works in part.", text: "ReelVault reads profiles and videos without your login. It uses your login for view counts.", next: "Stay signed in to Instagram in Chrome."},
  login_needed: {code: "login_needed", title: "Instagram asks for a login here.", text: "ReelVault reads with your Instagram login. It stays slow and careful.", next: "Stay signed in to Instagram in Chrome."},
  wait: {code: "wait", title: "Instagram asks ReelVault to wait.", text: "Instagram slows down reads from this connection for now.", next: "ReelVault goes on by itself. Run the test again in a few hours."},
  lo_only_blocked: {code: "lo_only_blocked", title: "Instagram asks for a login here. You chose to read without it.", text: "ReelVault reads no reels this way right now.", next: "Try again later, or pick Safest first in Settings."},
  signed_out: {code: "signed_out", title: "You're signed out of Instagram in Chrome.", text: "Instagram asks for a login here. Chrome has none right now.", next: "Sign in at instagram.com in Chrome. Then run the test again."},
  nothing: {code: "nothing", title: "Instagram blocks reads right now.", text: "Instagram blocks reads with and without your login on this connection.", next: "ReelVault waits by itself. Run the test again in a few hours."}};
const probe = (state, n, verdict) => ({probe_id: "prb_bbbbbbbbbbbbbbbb", job_id: "job_x", state, started_at: state === "waiting" ? null : iso(2),
  finished_at: state === "done" ? iso(0) : null, handles: ["instagram", "scaleit.demo"], progress: {done: n, total: 11}, rows: ROWS.slice(0, n),
  verdict: verdict ? VERDICTS[verdict] : null, message: state === "waiting" ? "Waiting for Chrome. The test starts within 30 seconds." :
    state === "failed" ? "The test did not finish. Open Chrome, then run it again." : undefined});
const HEALTH = {window_h: 48, strategies: [
  ...ROWS.slice(0, 8).map((r, i) => ({id: r.strategy, layer: "logged_out", label: r.label, verified: ["source", "guess", "guess", "source", "source", "source", "source", "guess"][i],
    enabled: r.strategy !== "lo_embed", can_switch: true, off: r.strategy === "lo_embed" ? {until: iso(-600), reason: "auto"} : null,
    ok: 212 - i * 20, partial: 9, blocked: i * 3, error: 1, rate: Math.round((212 - i * 20) / (222 - i * 17) * 100) / 100, devices: 4,
    last_ok_at: iso(5 + i), last_blocked_at: i ? iso(60 * i) : null, last_code: i ? "login_wall" : null})),
  {id: "li_clips", layer: "logged_in", label: "Reels list", verified: "built", enabled: true, can_switch: false, off: null, ok: 300, partial: 0, blocked: 1, error: 0, rate: 1, devices: 5,
    last_ok_at: iso(2), last_blocked_at: iso(900), last_code: "rate"}],
  layers: [{id: "logged_out", pulls: 31, reels: 820}, {id: "logged_in", pulls: 12, reels: 1100}, {id: "mixed", pulls: 9, reels: 870}],
  devices: [{device_id: "dev_aaaaaaaaaaaaaaaa", user_email: "member@example.com", version: "2.1.0", lo_state: "ok", lo_cooldown_until: null, li_cooldown_until: null, last_probe_verdict: "best", last_seen_at: iso(3)},
    {device_id: "dev_bbbbbbbbbbbbbbbb", user_email: "a.very.long.email.address.for.testing@some-long-domain.example.com", version: "2.0.0", lo_state: "off", lo_cooldown_until: null, li_cooldown_until: null, last_probe_verdict: null, last_seen_at: iso(400)},
    {device_id: "dev_cccccccccccccccc", user_email: "third@example.com", version: "2.1.0", lo_state: "cooling", lo_cooldown_until: iso(-300), li_cooldown_until: null, last_probe_verdict: "partial", last_seen_at: iso(20)}],
  recent_blocks: [{at: iso(15), strategy: "lo_reels_gql", code: "login_wall", http: 401, device_id: "dev_aaaaaaaaaaaaaaaa"}, {at: iso(80), strategy: "lo_post_gql", code: "rate", http: 429, device_id: "dev_cccccccccccccccc"}],
  settings: {default_read_mode: "auto", doc_ids: {lo_post_gql: ["27128499623469141"], lo_reels_gql: ["29285972954340351", "7845543455542541"]}, probe_handle: "instagram"}};

/** Routes /api/* and /healthz to the mock. `over` changes single answers: {"GET /api/fetch/probe/latest": [404, {...}]}. Records calls. */
async function mockApi(page, role, over = {}) {
  page.calls = [];
  const answers = Object.assign({
    "GET /api/me": [200, ME(role)], "GET /api/taxonomy": [200, {visual_format: []}], "GET /api/jobs": [200, {jobs: []}],
    "GET /api/ext/status": [200, EXT], "GET /api/fetch/settings": [200, SETTINGS], "PUT /api/fetch/settings": body => [200, Object.assign({}, SETTINGS, {read_mode: body.read_mode})],
    "GET /api/fetch/status": [200, STATUS], "GET /api/fetch/pulls": [200, PULLS],
    "GET /api/fetch/probe/latest": [404, {error: "You have not run the test yet.", code: "not_found"}],
    "POST /api/fetch/probe": [201, {probe_id: "prb_bbbbbbbbbbbbbbbb", job_id: "job_x", state: "waiting", message: "Waiting for Chrome. The test starts within 30 seconds."}],
    "GET /api/accounts": [200, {accounts: [ACC("acc_aaaaaaaaaaaaaaaa"), ACC("acc_bbbbbbbbbbbbbbbb")], limits: {max: 10, pulls_today: 1, pulls_max: 5}, extension: {paired: true, online: true}}],
    "GET /api/accounts/acc_aaaaaaaaaaaaaaaa": [200, {account: ACC("acc_aaaaaaaaaaaaaaaa")}],
    "GET /api/admin/stats": [200, {members: 3, pending: 0, active_7d: 2, reels: 300, storage_mb: 120, disk_free_mb: 9000, jobs_failed_24h: 0, load: {}, worker: {at: iso(1), host: "pc"}, last_backup: {at: iso(60)}}],
    "GET /api/admin/instagram-health": [200, {fails: [], recoveries: {}, pk_conflicts: 0, fields_seen: {}}],
    "GET /api/admin/community-formats": [200, {enabled: false, message: "Off."}], "GET /healthz": [200, {version: "test", db: "sqlite"}],
    "GET /api/admin/fetch-health": [200, HEALTH], "POST /api/admin/fetch-health/strategy": [200, {ok: true}], "PUT /api/admin/fetch-health/settings": [200, {ok: true}],
  }, over);
  await page.route(u => u.pathname.startsWith("/api/") || u.pathname === "/healthz", async route => {
    const req = route.request();
    const u = new globalThis.URL(req.url());
    let body = null;
    try { body = req.postDataJSON(); } catch { body = null; }
    const key = `${req.method()} ${u.pathname}`;
    page.calls.push({key, query: u.search, body});
    let a = answers[key];
    if (typeof a === "function") a = a(body, page.calls);
    if (!a) { page.errors.push(`unmocked ${key}`); a = [404, {error: "Not mocked", code: "not_found"}]; }
    await route.fulfill({status: a[0], contentType: "application/json", body: JSON.stringify(a[1])});
  });
}

async function newPage(width, scheme) {
  const ctx = await browser.newContext({viewport: {width, height: 900}, colorScheme: scheme});
  const page = await ctx.newPage();
  page.errors = [];
  page.on("console", m => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text())) page.errors.push(`console: ${m.text()}`); });
  page.on("pageerror", e => page.errors.push(`pageerror: ${e.message}`));
  page.on("console", m => { if (m.type() === "warning") console.log(`[page warning] ${m.text()}`); });   // app.js logs view crashes here
  return page;
}
async function view(page, hash) {
  await page.goto(URL + "/" + hash);
  await page.waitForFunction(() => document.documentElement.dataset.view === "ready" && document.documentElement.dataset.ready === location.hash, null, {timeout: 20000});
  await page.waitForTimeout(150);
}
async function checkScreen(page, label) {
  const [sw, iw, stray, broken] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth,
    ((document.querySelector("main") || document.body).innerText.match(/\b(null|undefined|NaN|\[object Object\])\b/) || [])[0] || "",
    /didn't load/.test(document.body.innerText)]);
  assert.equal(stray, "", `${label}: the page shows "${stray}"`);
  assert.ok(sw <= iw, `${label}: sideways scroll (${sw} > ${iw})`);
  assert.ok(!broken, `${label}: the page did not load`);
  assert.deepEqual(page.errors, [], `${label}: errors`);
}
async function shot(page, name) {
  if (!SHOTS) return;
  await mkdir(SHOTS, {recursive: true});
  await page.screenshot({path: path.join(SHOTS, name + ".png"), fullPage: true});
}

// ============================================================================================ tests
for (const [width, scheme] of [[1280, "light"], [1280, "dark"], [390, "light"], [390, "dark"]]) {
  test(`fetch screens at ${width}px ${scheme}: no errors, no sideways scroll`, async () => {
    const tag = `${width}-${scheme}`;
    const page = await newPage(width, scheme);
    await mockApi(page, "member", {"GET /api/fetch/probe/latest": [200, probe("done", 11, "partial")]});
    for (const [hash, name] of [["#settings/extension", "settings"], ["#test-connection", "test"], ["#competitors", "competitors"], ["#competitor/acc_aaaaaaaaaaaaaaaa", "competitor"]]) {
      await view(page, hash);
      await checkScreen(page, `${tag} ${hash}`);
      await shot(page, `${name}-${tag}`);
    }
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert.equal(bg === "rgb(16, 18, 26)", scheme === "dark", `body background ${bg} for ${scheme}`);
    await page.context().close();
    const admin = await newPage(width, scheme);
    await mockApi(admin, "admin");
    await view(admin, "#admin/health");
    await admin.waitForSelector("#fetch-health");
    await admin.click("#fetch-settings summary");
    await checkScreen(admin, `${tag} admin health`);
    await admin.locator("#fetch-health").screenshot(SHOTS ? {path: path.join(SHOTS, `admin-fetch-health-${tag}.png`)} : {});
    await admin.context().close();
  });
}

test("Settings card: three ways, Recommended on Safest first, one line per layer, Save sends the choice", async () => {
  const page = await newPage(1280, "light");
  await mockApi(page, "member");
  await view(page, "#settings/extension");
  const card = page.locator("#fetch-card");
  await card.waitFor();
  assert.match(await card.locator("h2").first().textContent(), /How ReelVault reads Instagram/);
  assert.equal(await card.locator('input[name="read-mode"]').count(), 3);
  assert.ok(await page.isChecked("#read-auto"));
  assert.match(await card.locator('label[for="read-auto"]').textContent(), /Safest first\s*Recommended/);
  assert.equal(await card.locator('label[for="read-logged_in_only"] .pill').count(), 0);
  // one line per layer, from GET /api/fetch/status
  const lo = card.locator('[data-layer="logged_out"]');
  assert.match(await lo.textContent(), /Without your login\s*Works\s*Works\. Last read 2 hours ago\./);
  const li = card.locator('[data-layer="logged_in"]');
  assert.match(await li.textContent(), /With your Instagram login\s*Waiting\s*Instagram asked us to slow down\. It goes on by itself/);
  assert.match(await card.locator("#last-test").textContent(), /Last test 1 day ago: The safest way works\./);
  assert.equal(await card.locator("#test-connection-link").getAttribute("href"), "#test-connection");
  // pick "Logged-out only" by clicking its row, then Save
  await card.locator('label[for="read-logged_out_only"]').click();
  await page.click("#fetch-save");
  await page.waitForSelector("#fetch-card .msg.good");
  assert.match(await page.textContent("#fetch-card .msg.good"), /Saved\./);
  const put = page.calls.find(c => c.key === "PUT /api/fetch/settings");
  assert.deepEqual(put.body, {read_mode: "logged_out_only"});
  assert.ok(page.calls.filter(c => c.key === "GET /api/fetch/status").length >= 2, "status reloads after Save");
  // the link opens the test page
  await page.click("#test-connection-link");
  await page.waitForFunction(() => location.hash === "#test-connection" && document.documentElement.dataset.ready === "#test-connection");
  assert.equal(await page.textContent("main h1"), "Test my connection");
  assert.equal(await page.getAttribute('.navlinks a[aria-current="page"]', "href"), "#settings");
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

test("Settings card: a server error shows a plain message; an older server hides the card", async () => {
  const page = await newPage(390, "dark");
  await mockApi(page, "member", {"GET /api/fetch/settings": [500, {error: "Something went wrong on our side. Try again in a minute."}]});
  await view(page, "#settings/extension");
  assert.match(await page.textContent("#fetch-card"), /Something went wrong on our side/);
  await page.context().close();
  const old = await newPage(390, "light");
  await mockApi(old, "member", {"GET /api/fetch/settings": [404, {error: "Not found"}], "GET /api/fetch/status": [404, {error: "Not found"}]});
  await view(old, "#settings/extension");
  assert.equal(await old.locator("#fetch-card").count(), 0);
  assert.ok(await old.locator("#ext-status").count() === 1, "the extension card still shows");
  assert.deepEqual(old.errors, []);
  await old.context().close();
});

test("Test my connection: no test yet -> Run the test -> waiting -> running -> verdict and table", async () => {
  const page = await newPage(1280, "light");
  let polls = 0;
  await mockApi(page, "member", {"GET /api/fetch/probe/prb_bbbbbbbbbbbbbbbb": () => { polls += 1; return [200, polls === 1 ? probe("running", 4) : probe("done", 11, "best")]; }});
  await view(page, "#test-connection");
  assert.match(await page.textContent("main"), /You have not run the test yet\./);
  assert.match(await page.textContent("#test-line"), /^This test makes about 11 slow requests\. It takes 2 minutes\.$/);
  assert.equal(await page.textContent("#run-test"), "Run the test");
  assert.equal(await page.locator("#verdict section").count(), 0);
  await page.click("#run-test");
  await page.waitForSelector("#probe-status >> text=Waiting for Chrome. The test starts within 30 seconds.");
  assert.ok(await page.isDisabled("#run-test"), "no second test while one runs");
  assert.ok(page.calls.some(c => c.key === "POST /api/fetch/probe"));
  // poll 1: running, 4 of 11
  await page.waitForSelector("#probe-status >> text=Testing: 4 of 11 done.", {timeout: 8000});
  assert.equal(await page.getAttribute("#probe-status .progress", "aria-valuenow"), "36");
  assert.equal(await page.locator("#probe-rows tbody tr[data-strategy]").count(), 4);
  // poll 2: done, verdict best (green), both groups
  await page.waitForSelector('#verdict [data-verdict="best"]', {timeout: 8000});
  const v = page.locator("#verdict section");
  assert.match(await v.getAttribute("class"), /\bok\b/);
  assert.match(await v.textContent(), /The safest way works\.ReelVault reads reels without your Instagram login on this connection\.You don't need to do anything\./);
  const heads = await page.locator("#probe-rows thead th").allTextContents();
  assert.deepEqual(heads, ["What ReelVault tried", "Account", "Result", "What it gives", "Time"]);
  assert.deepEqual(await page.locator("#probe-rows tr.group").allTextContents(), ["Without your login", "With your Instagram login"]);
  assert.equal(await page.locator("#probe-rows tbody tr[data-strategy]").count(), 11);
  const blocked = page.locator('#probe-rows tr[data-strategy="lo_reels_gql"]');
  assert.match(await blocked.textContent(), /Reels list@instagramBlockedInstagram asks for a login here\.More reels with view counts0\.2 s/);
  assert.match(await blocked.locator(".pill").getAttribute("class"), /\bbad\b/);
  assert.match(await page.locator('#probe-rows tr[data-strategy="lo_reels_page"] .pill').getAttribute("class"), /\bwarn\b/);
  assert.match(await page.locator('#probe-rows tr[data-strategy="li_session"]').textContent(), /Sign-in check—Works/);
  assert.match(await page.locator('#probe-rows tr[data-strategy="lo_embed"]').textContent(), /5 s$/);
  assert.equal(await page.textContent("#run-test"), "Run the test again");
  assert.ok(!(await page.isDisabled("#run-test")));
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

test("Test my connection: the other verdicts, a failed test, a running test on open, and the limits", async () => {
  for (const [code, cls] of [["partial", "warn"], ["wait", "warn"], ["login_needed", "warn"], ["lo_only_blocked", "warn"],
                             ["signed_out", "bad"], ["nothing", "bad"]]) {
    const page = await newPage(390, "light");
    await mockApi(page, "member", {"GET /api/fetch/probe/latest": [200, probe("done", 11, code)]});
    await view(page, "#test-connection");
    const v = page.locator("#verdict section");
    assert.equal(await v.getAttribute("data-verdict"), code);
    assert.match(await v.getAttribute("class"), new RegExp(`\\b${cls}\\b`));
    assert.match(await v.textContent(), new RegExp(VERDICTS[code].title.replace(/[.']/g, ".")));
    await checkScreen(page, "verdict " + code);
    await page.context().close();
  }
  // failed
  const f = await newPage(1280, "dark");
  await mockApi(f, "member", {"GET /api/fetch/probe/latest": [200, probe("failed", 3)]});
  await view(f, "#test-connection");
  assert.match(await f.textContent("#probe-status .msg.err"), /The test did not finish\. Open Chrome, then run it again\./);
  assert.equal(await f.locator("#verdict section").count(), 0);
  await f.context().close();
  // a test that runs when the page opens is followed
  const r = await newPage(1280, "light");
  await mockApi(r, "member", {"GET /api/fetch/probe/latest": [200, probe("running", 2)], "GET /api/fetch/probe/prb_bbbbbbbbbbbbbbbb": [200, probe("done", 11, "best")]});
  await view(r, "#test-connection");
  assert.ok(await r.isDisabled("#run-test"));
  await r.waitForSelector('#verdict [data-verdict="best"]', {timeout: 8000});
  await r.context().close();
  // no extension, or one too old: a red box with a way forward, and the test can't start
  for (const [device, code, btn] of [[{connected: false, version: null, needs_update: false}, "no_ext", "Connect Chrome"],
                                     [{connected: true, version: "2.0.0", needs_update: true}, "update", "Get the update"]]) {
    const p = await newPage(390, "light");
    await mockApi(p, "member", {"GET /api/fetch/status": [200, {...STATUS, device}], "GET /api/fetch/probe/latest": [404, {error: "You have not run the test yet."}]});
    await view(p, "#test-connection");
    await p.waitForSelector(`#verdict [data-verdict="${code}"]`);
    assert.match(await p.getAttribute("#verdict section", "class"), /\bbad\b/);
    assert.equal(await p.textContent("#connect-chrome"), btn);
    assert.equal(await p.getAttribute("#connect-chrome", "href"), "#settings/extension");
    assert.ok(await p.isDisabled("#run-test"), "no test without a working extension");
    await checkScreen(p, "test without extension " + code);
    assert.deepEqual(p.errors, []);
    await p.context().close();
  }
  // limits: 3 tests a day, no extension
  for (const [status, err] of [[429, "You ran 3 tests today. Try again tomorrow."], [409, "Connect the Chrome extension first."]]) {
    const p = await newPage(390, "dark");
    await mockApi(p, "member", {"POST /api/fetch/probe": [status, {error: err, code: status === 429 ? "limit_reached" : "busy"}]});
    await view(p, "#test-connection");
    await p.click("#run-test");
    await p.waitForSelector("main .msg.err");
    assert.equal(await p.textContent("main .msg.err"), err);
    assert.ok(!(await p.isDisabled("#run-test")));
    await p.context().close();
  }
});

test("Pull lines: one grey line per competitor and per pull, from GET /api/fetch/pulls", async () => {
  const page = await newPage(1280, "light");
  await mockApi(page, "member");
  await view(page, "#competitors");
  const first = page.locator('[data-account="acc_aaaaaaaaaaaaaaaa"] .readline');
  assert.equal(await first.textContent(), "Read without your Instagram login.");   // the newest pull of that account
  assert.equal(await page.textContent('[data-account="acc_bbbbbbbbbbbbbbbb"] .readline'), "Instagram asks for a login right now. ReelVault used yours.");
  const color = await first.evaluate(el => [getComputedStyle(el).color, getComputedStyle(el.closest(".card").querySelector("a.bold")).color]);
  assert.notEqual(color[0], color[1], "the line is grey, not link-coloured");
  await view(page, "#competitor/acc_aaaaaaaaaaaaaaaa");
  const cells = await page.locator("main table tbody tr td:last-child").allTextContents();
  assert.deepEqual(cells, ["Read 100 reelsRead 24 reels without your login. Your login filled in the other 76.", "Caught upRead without your Instagram login."]);
  assert.ok(page.calls.some(c => c.key === "GET /api/fetch/pulls" && /account_id=acc_aaaaaaaaaaaaaaaa/.test(c.query)));
  assert.deepEqual(page.errors, []);
  await page.context().close();
  // an older server without /api/fetch/pulls: the pages still work, without the lines
  const old = await newPage(390, "light");
  await mockApi(old, "member", {"GET /api/fetch/pulls": [404, {error: "Not found"}]});
  await view(old, "#competitors");
  assert.equal(await old.locator(".readline").count(), 0);
  assert.equal(await old.locator("[data-account]").count(), 2);
  assert.deepEqual(old.errors, []);
  await old.context().close();
});

test("Admin Fetch health: per-way numbers, layer totals, devices, blocks, on/off switch and the doc_id editor", async () => {
  const page = await newPage(1280, "dark");
  await mockApi(page, "admin");
  await view(page, "#admin/health");
  const card = page.locator("#fetch-health");
  await card.waitFor();
  assert.match(await card.locator("h2").textContent(), /Fetch health/);
  assert.match(await card.textContent(), /Last 48 hours/);
  assert.equal(await card.locator("#fetch-strategies tbody tr").count(), 9);
  const row = card.locator('#fetch-strategies tr[data-strategy="lo_post_gql"]');
  assert.match(await row.textContent(), /Reel detail.*lo_post_gql.*Seen in open-source tools.*Without login/);
  assert.match(await row.textContent(), /\d+%\d+ of \d+/);
  assert.match(await card.locator('#fetch-strategies tr[data-strategy="lo_embed"]').textContent(), /Off.*by itself/);
  assert.match(await card.locator('#fetch-strategies tr[data-strategy="li_clips"]').textContent(), /With login.*—$/);
  assert.match(await card.locator('#fetch-layers [data-layer="mixed"]').textContent(), /9 pulls\s*Both · 870 reels/);
  assert.equal(await card.locator("#fetch-devices tbody tr").count(), 3);
  assert.match(await card.locator("#fetch-devices").textContent(), /Old extension.*No test yet/);
  assert.match(await card.locator("#fetch-blocks").textContent(), /lo_reels_gql · Login wall \(HTTP 401\)/);
  // switch a way off
  await card.locator('input[data-strategy="lo_reels_gql"]').uncheck();
  await page.waitForFunction(() => document.querySelector("#toast") && !document.querySelector("#toast").hidden);
  assert.match(await page.textContent("#toast"), /Reels list is off for everyone\./);
  assert.deepEqual(page.calls.find(c => c.key === "POST /api/admin/fetch-health/strategy").body, {strategy: "lo_reels_gql", enabled: false});
  await page.waitForSelector("#fetch-health");
  // doc_id editor: a bad id is refused in the page, a good one is saved
  await page.click("#fetch-settings summary");
  assert.equal(await page.inputValue("#docid-lo_reels_gql"), "29285972954340351, 7845543455542541");
  await page.fill("#docid-lo_posts_gql", "12345");
  await page.click("#fetch-settings-save");
  await page.waitForSelector("#fetch-settings .msg.err >> text=A doc_id has 10 to 24 digits.");
  assert.ok(!page.calls.some(c => c.key === "PUT /api/admin/fetch-health/settings"));
  await page.fill("#docid-lo_posts_gql", "28904005159232690");
  await page.fill("#fetch-probe-handle", "@Natgeo");
  await page.selectOption("#fetch-default-mode", "logged_in_only");
  await page.click("#fetch-settings-save");
  await page.waitForSelector("#fetch-settings .msg.good");
  assert.deepEqual(page.calls.find(c => c.key === "PUT /api/admin/fetch-health/settings").body, {default_read_mode: "logged_in_only", probe_handle: "Natgeo",
    doc_ids: {lo_posts_gql: ["28904005159232690"], lo_reels_gql: ["29285972954340351", "7845543455542541"], lo_post_gql: ["27128499623469141"], lo_media_gql: []}});
  assert.deepEqual(page.errors, []);
  await page.context().close();
});

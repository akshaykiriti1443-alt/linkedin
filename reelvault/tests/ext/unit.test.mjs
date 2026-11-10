// Unit tests for the extension's pure modules. Run: node --test tests/ext/unit.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {quoteBigInts, parseIg} from "../../extension-members/bigint.js";
import {validateJob, parsePairLink} from "../../extension-members/validate.js";
import {DEFAULTS, merge, Budget, BudgetStop, localDay} from "../../extension-members/pacing.js";
import {trimMedia, trimUser, keepForTarget, smallest, keysTree} from "../../extension-members/trim.js";
import {IG, isCdnUrl, WARNING_RE} from "../../extension-members/endpoints.js";

const FIX = new URL("../fixtures/ig/", import.meta.url);
const fixture = name => readFileSync(new URL(name, FIX), "utf8");
const ID = "job_abcdefghijklmnop";
const ACC = "acc_abcdefghijklmnop";

test("bigint: 19-digit pks survive as exact strings; captions untouched", () => {
  const d = parseIg(fixture("bigint_pk.json"));
  assert.equal(d.items[0].media.pk, "3456789012345678901");
  assert.notEqual(String(JSON.parse(fixture("bigint_pk.json")).items[0].media.pk), "3456789012345678901");
  const t = '{"a": 1234567890123456789, "c": "x \\"k\\": 1234567890123456789 y", "n": 12}';
  const o = JSON.parse(quoteBigInts(t));
  assert.equal(o.a, "1234567890123456789");
  assert.equal(o.c, 'x "k": 1234567890123456789 y');
  assert.equal(o.n, 12);
});

test("validate: good jobs pass and are copied with known fields only", () => {
  const r = validateJob({id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "scaleit.demo", mode: "next",
    max_id: "QVFabc_-=", cursor_source: "clips", oldest_pk: "3456789012345678901", target: 100, evil: "x"}});
  assert.ok(r.ok);
  assert.equal(r.job.payload.evil, undefined);
  assert.equal(r.job.payload.max_id, "QVFabc_-=");
  assert.ok(validateJob({id: ID, kind: "ig_videos", payload: {items: [{reel_id: "reel_abcdefghijklmnop", ig_pk: "1", code: "C9xAbcde"}]}}).ok);
  assert.ok(validateJob({id: ID, kind: "page_read", payload: {source_id: "src_abcdefghijklmnop", kind: "skool_profile", url: "https://www.skool.com/@mia-m"}}).ok);
  assert.ok(validateJob({id: ID, kind: "page_read", payload: {source_id: "src_abcdefghijklmnop", kind: "linkedin_self", url: "https://www.linkedin.com/in/me/"}}).ok);
  assert.ok(validateJob({id: ID, kind: "ig_discover", payload: {run_id: "drun_abcdefghijklmnop", mode: "search", queries: ["facebook ads agency"]}}).ok);
});

test("validate: bad values never reach a path", () => {
  const bad = [
    {id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "../evil", mode: "first"}},
    {id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "ok", mode: "first", ig_pk: 3456789012345678901}},
    {id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "ok", mode: "first", ig_pk: "12a"}},
    {id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "ok", mode: "next", max_id: "a/b?c"}},
    {id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "ok", mode: "first", target: 500}},
    {id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "Upper", mode: "first"}},
    {id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "ok", mode: "delete"}},
    {id: ID, kind: "ig_post", payload: {}},
    {id: "job_1", kind: "ig_profile", payload: {handle: "ok", purpose: "competitor"}},
    {id: ID, kind: "ig_profile", payload: {handle: "ok", purpose: "dm"}},
    {id: ID, kind: "ig_videos", payload: {items: new Array(31).fill({reel_id: "reel_abcdefghijklmnop", ig_pk: "1", code: "C9xAbcde"})}},
    {id: ID, kind: "ig_videos", payload: {items: [{reel_id: "reel_abcdefghijklmnop", ig_pk: "1", code: "C9/../x"}]}},
    {id: ID, kind: "ig_discover", payload: {run_id: "drun_abcdefghijklmnop", mode: "search", queries: ["a", "b", "c", "d"]}},
    {id: ID, kind: "ig_discover", payload: {run_id: "drun_abcdefghijklmnop", mode: "search", queries: ["<script>"]}},
    {id: ID, kind: "ig_discover", payload: {run_id: "drun_abcdefghijklmnop", mode: "similar", seeds: [{handle: "a"}, {handle: "b"}, {handle: "c"}]}},
    {id: ID, kind: "ig_check", payload: {run_id: "drun_abcdefghijklmnop", candidates: new Array(16).fill({cand_id: "cand_abcdefghijklmnop", handle: "x"})}},
    {id: ID, kind: "page_read", payload: {source_id: "src_abcdefghijklmnop", kind: "skool_profile", url: "https://evil.example/@x"}},
    {id: ID, kind: "page_read", payload: {source_id: "src_abcdefghijklmnop", kind: "linkedin_self", url: "https://www.linkedin.com/in/someone-else/"}},
    {id: ID, kind: "page_read", payload: {source_id: "src_abcdefghijklmnop", kind: "skool_profile", url: "https://www.skool.com/@x/../../y"}},
  ];
  for (const j of bad) assert.equal(validateJob(j).ok, false, JSON.stringify(j).slice(0, 120));
});

test("pair links only for servers baked into the build", () => {
  const S = ["https://app.example.com"];
  assert.deepEqual(parsePairLink("https://app.example.com/pair#7kq4-m2xp", S),
                   {ok: true, origin: "https://app.example.com", host: "app.example.com", code: "7KQ4-M2XP"});
  assert.equal(parsePairLink("https://app.example.com.evil.io/pair#7KQ4-M2XP", S).error, "not_ours");
  assert.equal(parsePairLink("http://app.example.com/pair#7KQ4-M2XP", S).error, "not_ours");
  assert.equal(parsePairLink("https://app.example.com/other#7KQ4-M2XP", S).error, "no_code");
  assert.equal(parsePairLink("not a link", S).error, "not_link");
});

test("pacing: stricter of local and server unless this is a test build", () => {
  const fast = {...DEFAULTS, gap_ms: [50, 100], hour_max: 500, day_max: 300};
  const m = merge(DEFAULTS, fast, "zip");
  assert.deepEqual(m.gap_ms, [4000, 9000]);
  assert.equal(m.hour_max, 120);
  assert.equal(m.day_max, 300);
  const t = merge(DEFAULTS, fast, "test");
  assert.deepEqual(t.gap_ms, [50, 100]);
  assert.equal(merge(DEFAULTS, {gap_ms: "x", hour_max: -1}, "zip").hour_max, 120);
});

function fakeBudget(limits = DEFAULTS) {
  let data, now = 1_700_000_000_000, slept = 0;
  const b = new Budget({get: async () => data, set: async v => { data = JSON.parse(JSON.stringify(v)); }},
                       {now: () => now, sleep: async ms => { slept += ms; now += ms; }, rnd: () => 0.5, limits: () => limits});
  return {b, tick: ms => { now += ms; }, slept: () => slept};
}

test("budget: random gaps between calls, 120 per sliding hour, page gap on top", async () => {
  const {b, tick, slept} = fakeBudget();
  await b.take("ig");
  assert.equal(slept(), 0);
  await b.take("ig");
  assert.equal(slept(), 6500);                 // 4000 + (9000-4000)*0.5
  await b.take("ig", {page: true});
  assert.equal(slept(), 6500 + 6500 + 9000);   // + page gap 6000 + 6000*0.5
  for (let i = 3; i < 120; i++) await b.take("ig");
  await assert.rejects(b.take("ig"), e => e instanceof BudgetStop && e.what === "hour" && e.waitMs > 0);
  assert.equal((await b.report()).hour_used, 120);
  tick(3600_000);
  await b.take("ig");
  assert.equal((await b.report()).hour_used, 1);
});

test("budget: day, cdn and discovery caps", async () => {
  const {b} = fakeBudget({...DEFAULTS, day_max: 3, cdn_day_max: 1, discover_day_max: 2, gap_ms: [0, 0], cdn_gap_ms: [0, 0]});
  await b.take("discover"); await b.take("discover");
  await assert.rejects(b.take("discover"), e => e.what === "discover_day");
  await b.take("ig");
  await assert.rejects(b.take("ig"), e => e.what === "day");
  await b.take("cdn");
  await assert.rejects(b.take("cdn"), e => e.what === "cdn_day");
  const r = await b.report();
  assert.deepEqual(r, {hour_used: 3, day_used: 3, cdn_day_used: 1});
  assert.match(localDay(Date.now()), /^\d{4}-\d{2}-\d{2}$/);
});

test("trim: ids are strings, smallest video >= 480, suggested reels filtered, collab kept", () => {
  const page = parseIg(fixture("clips_user_page1.json"));
  const m = trimMedia(page.items[0].media);
  assert.equal(typeof m.pk, "string");
  assert.equal(m.video.width, 480);
  assert.equal(m.cover.width, 480);
  assert.equal(m.owner.pk, "25025320");
  assert.ok(Array.isArray(m.clips_tab_pinned_user_ids));
  const sugg = parseIg(fixture("with_suggested.json")).items.map(i => i.media);
  assert.equal(sugg.filter(x => keepForTarget(x, "25025320")).length, 6);
  const collab = parseIg(fixture("collab.json")).items.map(i => i.media);
  assert.equal(collab.filter(x => keepForTarget(x, "25025320")).length, 3);
  assert.deepEqual(smallest([{url: "a", width: 360}, {url: "b", width: 300}]), {url: "a", width: 360, height: null});
  const u = trimUser(parseIg(fixture("web_profile_info.json")).data.user);
  assert.equal(u.pk, "25025320");
  assert.equal(u.followers, 182000);
  assert.equal(keysTree({a: [1, 2], b: {c: "x"}, d: null, e: 1.5}).a[0], "int");
});

test("endpoint table builds only Instagram paths", () => {
  assert.equal(IG.profileInfo.path("a b&c"), "/api/v1/users/web_profile_info/?username=a%20b%26c");
  assert.equal(IG.feedUser.path("1", "2_3"), "/api/v1/feed/user/1/?count=12&max_id=2_3");
  assert.deepEqual(IG.clipsUser.form("1", null, "12"), {target_user_id: "1", page_size: "12", include_feed_video: "true"});
  for (const [k, v] of Object.entries(IG)) if (v && v.path) assert.ok(v.path("1", "2").startsWith("/"), k);
  assert.ok(isCdnUrl("https://scontent-lhr8-1.cdninstagram.com/v/x.mp4"));
  assert.ok(!isCdnUrl("https://evil.example/cdninstagram.com.mp4"));
  assert.ok(WARNING_RE.test("feedback_required"));
});

// Unit tests for the fetch engine's pure modules (DESIGN-FETCH.md 3.5): strategies.js (request specs, parsers,
// block classes), ladder.js (the logged-out stage), and the logged-out parts of validate.js, pacing.js, trim.js.
// Every parser runs against every fixture of tests/fixtures/ig/logged_out/.
// Run: node --test tests/ext/fetch_unit.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {parseIg} from "../../extension-members/bigint.js";
import {LO, LO_IDS, LI_IDS, LO_DOC_IDS, LI_OF, IG} from "../../extension-members/endpoints.js";
import {validateJob, isLoCursor, isLsd, isDocId} from "../../extension-members/validate.js";
import {DEFAULTS, merge, Budget, BudgetStop} from "../../extension-members/pacing.js";
import {trimMedia, trimGql, trimXigUser, keepForTargetGql} from "../../extension-members/trim.js";
import {loRequest, loInterpret, classifyAnon, cleanDocIds, pkFromCode, codeFromPk, parseProfilePage, parseReelsPage,
        parseEmbed, embeddedData, pageLsd, probeFields, hasViews} from "../../extension-members/strategies.js";
import {runLoStage, LoFail, freshLo} from "../../extension-members/ladder.js";

const FIX = new URL("../fixtures/ig/logged_out/", import.meta.url);
const fx = name => readFileSync(new URL(name, FIX), "utf8");
const MANIFEST = JSON.parse(fx("manifest.json"));
const TARGET = MANIFEST.target;                       // {pk, username, page_id}
const LSD = MANIFEST.lsd;
const ORIGIN = "https://www.instagram.com";
const resp = (name, status = 200, extra = {}) => ({status, type: "basic", url: ORIGIN + "/x/", redirected: false,
                                                   text: name ? fx(name) : "", ...extra});
const LOGIN_REDIRECT = () => resp("login_page.html", 200, {url: ORIGIN + "/accounts/login/?next=%2Fscaleit.demo%2F", redirected: true});
const ID = "job_abcdefghijklmnop", ACC = "acc_abcdefghijklmnop";

// ============================================================================================== ids and tables

test("strategy table: eight logged-out strategies in ladder order, paths only on instagram.com", () => {
  assert.deepEqual(LO_IDS, ["lo_profile_page", "lo_reels_page", "lo_reels_gql", "lo_web_profile", "lo_posts_gql",
                            "lo_post_gql", "lo_media_gql", "lo_embed"]);
  assert.deepEqual(LI_IDS, ["li_session", "li_clips", "li_feed", "li_profile", "li_media"]);
  for (const id of LO_IDS) assert.ok(LO[id].path("a.b", "x").startsWith("/"), id);
  assert.equal(LO.lo_profile_page.path("a b/../c"), "/a%20b%2F..%2Fc/");
  assert.equal(LO.lo_reels_gql.path(), "/graphql/query");
  assert.equal(LO.lo_posts_gql.path(), "/api/graphql");
  assert.deepEqual(LO_DOC_IDS, MANIFEST.doc_ids, "the built-in doc ids are the ones of DESIGN-FETCH.md section 1");
  assert.equal(LI_OF.clipsUser, "li_clips");
  assert.ok(Object.values(LI_OF).every(v => LI_IDS.includes(v)));
});

test("shortcode <-> media pk", () => {
  assert.equal(pkFromCode("DfMCbOTgAAA"), "4020599234213445632");
  assert.equal(codeFromPk("4020599234213445632"), "DfMCbOTgAAA");
  assert.equal(pkFromCode("../etc"), null);
  assert.equal(pkFromCode("AAAAAAAAAAAAAAAAAAAA"), null, "a 20-char code is not a media id");
});

test("doc ids come from the server when they are valid, else the built-in ones", () => {
  assert.deepEqual(cleanDocIds(null), LO_DOC_IDS);
  const c = cleanDocIds({lo_post_gql: ["12345678901234567", "x", 5], lo_reels_gql: "nope", lo_evil: ["12345678901234567"]});
  assert.deepEqual(c.lo_post_gql, ["12345678901234567"]);
  assert.deepEqual(c.lo_reels_gql, LO_DOC_IDS.lo_reels_gql);
  assert.equal(c.lo_evil, undefined);
  assert.ok(isDocId("28904005159232689") && !isDocId("123") && !isDocId("28904005159232689&x=1"));
});

// ============================================================================================== request specs

test("request specs: pages, API and GraphQL, with the contract's headers, forms and referrers", () => {
  const ctx = {lsd: LSD, docId: "27128499623469141"};
  const page = loRequest("lo_profile_page", {handle: "scaleit.demo"}, ctx);
  assert.deepEqual(page, {method: "GET", path: "/scaleit.demo/", referrer: ORIGIN + "/",
                          headers: {Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}});
  assert.equal(loRequest("lo_reels_page", {handle: "scaleit.demo"}).path, "/scaleit.demo/reels/");
  assert.equal(loRequest("lo_embed", {code: "DfMCbOTgAAA"}).path, "/reel/DfMCbOTgAAA/embed/captioned/");
  const api = loRequest("lo_web_profile", {handle: "scaleit.demo"});
  assert.equal(api.path, "/api/v1/users/web_profile_info/?username=scaleit.demo");
  assert.deepEqual(api.headers, {"X-IG-App-ID": "936619743392459", "X-ASBD-ID": "359341",
                                 "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"});
  assert.equal(api.referrer, ORIGIN + "/scaleit.demo/");

  const post = loRequest("lo_post_gql", {code: "DfMCbOTgAAA", handle: "scaleit.demo"}, ctx);
  assert.equal(post.method, "POST");
  assert.equal(post.path, "/api/graphql");
  assert.deepEqual(post.headers, {"X-IG-App-ID": "936619743392459", "X-ASBD-ID": "359341", "X-FB-LSD": LSD, "Accept": "*/*"});
  assert.deepEqual(Object.keys(post.form), ["lsd", "fb_api_caller_class", "server_timestamps", "variables", "doc_id"]);
  assert.equal(post.form.variables, '{"shortcode":"DfMCbOTgAAA","__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider":false}');
  assert.equal(post.form.doc_id, "27128499623469141");
  assert.equal(post.referrer, ORIGIN + "/reel/DfMCbOTgAAA/");

  const reels = loRequest("lo_reels_gql", {pk: TARGET.pk, cursor: null, handle: "scaleit.demo"}, {lsd: LSD, docId: "29285972954340351"});
  assert.equal(reels.path, "/graphql/query");
  assert.equal(reels.headers["X-FB-Friendly-Name"], "PolarisProfileReelsTabContentQuery_connection");
  assert.equal(reels.form.fb_api_req_friendly_name, "PolarisProfileReelsTabContentQuery_connection");
  assert.deepEqual(JSON.parse(reels.form.variables), {after: null, before: null, data: {include_feed_video: true, page_size: 12,
    target_user_id: TARGET.pk}, first: 12, last: null, id: TARGET.pk});
  assert.equal(reels.referrer, ORIGIN + "/scaleit.demo/");

  const posts = loRequest("lo_posts_gql", {pageId: TARGET.page_id, cursor: "QVFabc", handle: "scaleit.demo"}, {lsd: LSD, docId: "28904005159232689"});
  assert.deepEqual(JSON.parse(posts.form.variables), {id: TARGET.page_id, after: "QVFabc", before: null, first: 12, last: null,
    __relay_internal__pv__PolarisSEOIsCrawlerrelayprovider: false});
  const media = loRequest("lo_media_gql", {pk: "4020599234213445632", code: "DfMCbOTgAAA"}, {lsd: LSD, docId: "27130156389949648"});
  assert.equal(media.form.variables, '{"media_id":"4020599234213445632"}');
  assert.equal(media.headers["X-FB-Friendly-Name"], "PolarisLoggedOutDesktopWWWPostRootContentQuery");
});

test("request specs never carry a cookie, a CSRF token or X-IG-WWW-Claim, and bad values never reach a path", () => {
  const ctx = {lsd: LSD, docId: "27128499623469141"};
  const good = {handle: "scaleit.demo", code: "DfMCbOTgAAA", pk: TARGET.pk, pageId: TARGET.page_id, cursor: "QVFabc"};
  for (const id of LO_IDS) {
    const s = loRequest(id, good, ctx);
    assert.ok(!s.skip, id);
    const names = Object.keys(s.headers).map(k => k.toLowerCase());
    for (const bad of ["cookie", "x-csrftoken", "x-ig-www-claim", "authorization"]) assert.ok(!names.includes(bad), `${id} sends ${bad}`);
    assert.ok(!JSON.stringify(s).toLowerCase().includes("csrf"), id);
    assert.ok(s.referrer.startsWith(ORIGIN + "/"));
  }
  assert.deepEqual(loRequest("lo_profile_page", {handle: "../evil"}), {skip: "bad_value"});
  assert.deepEqual(loRequest("lo_web_profile", {handle: "A&b=1"}), {skip: "bad_value"});
  assert.deepEqual(loRequest("lo_post_gql", {code: "x/../y"}, ctx), {skip: "no_reel"});
  assert.deepEqual(loRequest("lo_embed", {code: null}), {skip: "no_reel"});
  assert.deepEqual(loRequest("lo_reels_gql", {pk: "12a"}, ctx), {skip: "no_user"});
  assert.deepEqual(loRequest("lo_reels_gql", {pk: "12", cursor: "a b"}, ctx), {skip: "bad_value"});
  assert.deepEqual(loRequest("lo_posts_gql", {pageId: TARGET.page_id, cursor: null}, ctx), {skip: "no_cursor"});
  assert.deepEqual(loRequest("lo_post_gql", {code: "DfMCbOTgAAA"}, {lsd: null, docId: "27128499623469141"}), {skip: "no_lsd"});
  assert.deepEqual(loRequest("lo_post_gql", {code: "DfMCbOTgAAA"}, {lsd: "x y", docId: "27128499623469141"}), {skip: "no_lsd"});
  assert.deepEqual(loRequest("lo_post_gql", {code: "DfMCbOTgAAA"}, {lsd: LSD, docId: "1; drop"}), {skip: "bad_value"});
  assert.deepEqual(loRequest("li_clips", good, ctx), {skip: "bad_value"}, "only logged-out strategies have a spec");
  assert.ok(isLoCursor("QVF+/=:.|-_") && !isLoCursor("") && !isLoCursor("a b") && !isLoCursor("x".repeat(601)));
  assert.ok(isLsd(LSD) && !isLsd("abc") && !isLsd("a".repeat(65)));
});

// ============================================================================================== parsers x fixtures

const CLASS_TO = {success: ["ok", null], private: ["ok", null], partial: ["partial", null], login_wall: ["blocked", "login_wall"],
                  rate_limit: ["blocked", "rate"], changed: ["blocked", "changed"], empty: ["partial", "empty"]};
const PAGE = ["lo_profile_page", "lo_reels_page", "lo_embed"];
const API = ["lo_web_profile"];
const GQL = ["lo_reels_gql", "lo_posts_gql", "lo_post_gql", "lo_media_gql"];

test("every fixture of the manifest parses to the class the manifest names", () => {
  let n = 0;
  for (const f of MANIFEST.files) {
    if (f.class === "contract") continue;
    const strategies = LO_IDS.includes(f.strategy) ? [f.strategy]
      : f.strategy === "any page strategy" ? PAGE : f.strategy === "any API strategy" ? [...API, ...GQL]
      : f.strategy === "any GraphQL strategy" ? GQL : f.strategy === "any strategy" ? LO_IDS : null;
    assert.ok(strategies, `unknown strategy "${f.strategy}" for ${f.file}`);
    for (const s of strategies) {
      const r = f.file === "login_page.html" ? LOGIN_REDIRECT() : resp(f.file, f.http);
      const out = loInterpret(s, r);
      const [status, code] = CLASS_TO[f.class];
      assert.equal(out.status, status, `${f.file} as ${s}: ${JSON.stringify({status: out.status, code: out.code})}`);
      if (code) assert.equal(out.code, code, `${f.file} as ${s}`);
      assert.equal(out.http, f.http);
      n++;
    }
  }
  assert.ok(n >= 40, `${n} fixture x strategy checks`);
});

test("profile page: user, page id (not the pk), lsd, 12 stubs, photos known, private and no-data pages", () => {
  const r = loInterpret("lo_profile_page", resp("profile_page.html"));
  assert.equal(r.pk, TARGET.pk);
  assert.equal(r.pageId, TARGET.page_id);
  assert.notEqual(r.pageId, r.pk);
  assert.equal(r.lsd, LSD);
  assert.equal(r.user.pk, TARGET.pk, "the page id never becomes the user pk");
  assert.equal(r.user.followers, 182000);
  assert.equal(r.user.media_count, 640);
  assert.equal(r.stubs.length, 12);
  assert.equal(r.stubs.filter(s => s.photo).length, 2);
  assert.ok(r.stubs.every(s => /^\d{19}$/.test(s.pk)), "19-digit pks stay exact strings");
  assert.ok(r.more && isLoCursor(r.cursor));
  assert.deepEqual(probeFields(r), ["user", "followers", "reel_list", "lsd"]);

  const codes = loInterpret("lo_profile_page", resp("profile_page_codes_only.html"));
  assert.equal(codes.stubs.length, 12);
  assert.ok(codes.stubs.every(s => s.pk === null && !s.photo && !s.reel), "only `code` is sure: the type is unknown");

  const priv = loInterpret("lo_profile_page", resp("profile_page_private.html"));
  assert.equal(priv.private, true);
  assert.equal(priv.stubs.length, 0);

  const none = loInterpret("lo_profile_page", resp("profile_page_no_data.html"));
  assert.deepEqual([none.status, none.code], ["partial", "no_data"]);
  assert.equal(none.pk, TARGET.pk, "the profile_id fallback");
  assert.equal(none.lsd, LSD);
  assert.equal(none.user, null);
  assert.deepEqual(probeFields(none), ["lsd"]);

  assert.equal(embeddedData(fx("profile_page.html")).length, 2);
  assert.equal(pageLsd(fx("login_page.html")), null);
  assert.equal(parseProfilePage("<html></html>").user, null);
});

test("reels page and reels query: 12 sparse reels with play counts, cursor, wrapped shape", () => {
  const p = loInterpret("lo_reels_page", resp("reels_page.html"));
  assert.equal(p.medias.length, 12);
  assert.ok(p.medias.every(m => typeof m.play_count === "number" && hasViews(m)));
  assert.equal(p.lsd, LSD);
  assert.ok(p.more && p.cursor);
  assert.equal(parseReelsPage(fx("profile_page.html")).hasList, false, "a profile page is not a reels list");
  const a = loInterpret("lo_reels_gql", resp("reels_gql_page1.json"));
  const b = loInterpret("lo_reels_gql", resp("reels_gql_page2_last.json"));
  const w = loInterpret("lo_reels_gql", resp("reels_gql_wrapped.json"));
  assert.equal(a.cursor, p.cursor);
  assert.deepEqual([b.cursor, b.more], [null, false]);
  assert.deepEqual(w.medias.map(m => m.code), a.medias.map(m => m.code), "gallery-dl's fetch__XDTUserDict wrapper");
  assert.equal(new Set([...a.medias, ...b.medias].map(m => m.pk)).size, 24);
  assert.ok(a.medias.every(m => /^\d{19}$/.test(m.pk)));
  assert.deepEqual(probeFields(a), ["reel_list", "views", "likes", "comments"]);
  const withPrefix = {...resp("reels_gql_page1.json")};
  withPrefix.text = "for (;;);" + withPrefix.text;
  assert.equal(loInterpret("lo_reels_gql", withPrefix).medias.length, 12, "anything before the first { is stripped");
});

test("web_profile_info, posts query, post and media detail, embed", () => {
  const w = loInterpret("lo_web_profile", resp("web_profile_info_ok.json"));
  assert.equal(w.user.pk, TARGET.pk);
  assert.equal(w.nodes.length, 12);
  assert.equal(w.nodes.filter(n => n.is_video).length, 10);
  assert.ok(keepForTargetGql(w.nodes[0], TARGET.pk) && !keepForTargetGql(w.nodes[0], "1"));
  const p1 = loInterpret("lo_posts_gql", resp("posts_gql_page1.json"));
  const p2 = loInterpret("lo_posts_gql", resp("posts_gql_page2_last.json"));
  assert.deepEqual([p1.stubs.length, p2.stubs.length, p2.more], [12, 6, false]);
  const ok = loInterpret("lo_post_gql", resp("post_gql_ok.json"));
  assert.equal(ok.media.code, "DfMCbOTgAAA");
  assert.equal(ok.media.pk, "4020599234213445632");
  assert.deepEqual(probeFields(ok), ["views", "likes", "comments", "taken_at", "caption", "video_url", "duration"]);
  const nv = loInterpret("lo_post_gql", resp("post_gql_no_views.json"));
  assert.deepEqual([nv.status, nv.code], ["partial", "no_views"]);
  assert.ok(nv.media, "a reel without views is still read (the login fills in the views)");
  assert.deepEqual(probeFields(nv), ["likes", "comments", "taken_at", "caption", "video_url", "duration"]);
  const photo = loInterpret("lo_post_gql", resp("post_gql_photo.json"));
  assert.deepEqual([photo.status, photo.notReel], ["ok", true]);
  const md = loInterpret("lo_media_gql", resp("media_gql_ok.json"));
  assert.equal(md.media.play_count, undefined, "yt-dlp's query gives view_count only");
  assert.equal(typeof md.media.view_count, "number");
  const em = loInterpret("lo_embed", resp("embed_ok.html"));
  assert.equal(em.node.shortcode, "DfMCbOTgAAA");
  assert.equal(parseEmbed("<html>__additionalDataLoaded('extra',{bad json});</html>").found, false);
});

test("uploads match the contract example: v1 item, sparse reels item, gql item and the account", () => {
  const up = JSON.parse(fx("upload_items.json"));
  const full = loInterpret("lo_post_gql", resp("post_gql_ok.json")).media;
  assert.deepEqual({shape: "v1", strategy: "lo_post_gql", ...trimMedia(full)}, up.items[0]);
  const noViews = loInterpret("lo_post_gql", resp("post_gql_no_views.json")).media;
  assert.deepEqual({shape: "v1", strategy: "lo_post_gql", ...trimMedia(noViews)}, up.items[1]);
  const sparse = loInterpret("lo_reels_gql", resp("reels_gql_page1.json")).medias[2];
  assert.deepEqual({shape: "v1", strategy: "lo_reels_gql", ...trimMedia(sparse)}, up.items[2]);
  const node = loInterpret("lo_web_profile", resp("web_profile_info_ok.json")).nodes.find(n => n.shortcode === up.items[3].shortcode);
  assert.deepEqual({shape: "gql", strategy: "lo_web_profile", ...trimGql(node)}, up.items[3]);
  assert.deepEqual(loInterpret("lo_profile_page", resp("profile_page.html")).user, up.account);
  const x = trimXigUser({pk: undefined, id: "17841400025025320", username: "a", all_media_count: 3}, null);
  assert.equal(x.pk, null, "without a pk the page id is not used");
  const g = trimGql({id: 5, shortcode: "DfBrh7jgAAD", edge_media_preview_like: {count: 7}, edge_media_to_parent_comment: {count: 2},
                     pinned_for_users: [{id: "1"}], coauthor_producers: [{id: 9, username: "p"}]});
  assert.deepEqual([g.id, g.like_count, g.comment_count, g.pinned, g.coauthors], ["5", 7, 2, true, [{id: "9", username: "p"}]]);
});

// ============================================================================================== block classes

test("classifyAnon: every signal of table 2.3", () => {
  const J = (o, status, extra) => ({status, type: "basic", url: ORIGIN + "/api/x", redirected: false, text: JSON.stringify(o), ...extra});
  const cls = (r, s = "lo_post_gql") => { const c = classifyAnon(r, s); return [c.status, c.code]; };
  assert.deepEqual(cls(J({}, 429)), ["blocked", "rate"]);
  assert.deepEqual(cls(resp("wait_401.json", 401)), ["blocked", "rate"]);
  assert.deepEqual(cls(J({message: "Please wait a few minutes before you try again.", status: "fail"}, 403)), ["blocked", "rate"]);
  assert.deepEqual(cls(J({message: "feedback_required", status: "fail"}, 400)), ["blocked", "rate"]);
  assert.deepEqual(cls(J({message: "checkpoint_required", checkpoint_url: "/challenge/1/", status: "fail"}, 400)), ["blocked", "challenge"]);
  assert.deepEqual(cls(J({message: "challenge_required", status: "fail"}, 400)), ["blocked", "challenge"]);
  assert.deepEqual(cls({status: 200, url: ORIGIN + "/challenge/123/abc/", redirected: true, text: "<html>"}, "lo_profile_page"), ["blocked", "challenge"]);
  assert.deepEqual(cls(LOGIN_REDIRECT(), "lo_profile_page"), ["blocked", "login_wall"]);
  assert.deepEqual(cls({status: 200, url: ORIGIN + "/", redirected: true, text: "<html>home</html>"}, "lo_reels_page"), ["blocked", "login_wall"]);
  assert.deepEqual(cls({status: 200, url: ORIGIN + "/scaleit.demo/", redirected: false, text: "<html>profile</html>"}, "lo_profile_page"), ["ok", null]);
  assert.deepEqual(cls(resp("require_login_401.json", 401)), ["blocked", "login_wall"]);
  assert.deepEqual(cls(J({message: "x"}, 403)), ["blocked", "login_wall"]);
  assert.deepEqual(cls(J({message: "login_required", require_login: true, status: "fail"}, 200)), ["blocked", "login_wall"]);
  assert.deepEqual(cls({status: 200, url: ORIGIN + "/api/graphql", text: "<!DOCTYPE html><html>login</html>"}), ["blocked", "login_wall"],
                   "a page where JSON is due");
  assert.deepEqual(cls(J({message: "bad", status: "fail"}, 400), "lo_web_profile"), ["blocked", "refused"]);
  assert.deepEqual(cls(J({message: "bad", status: "fail"}, 400), "lo_post_gql"), ["error", "http_error"]);
  assert.deepEqual(cls({status: 404, url: ORIGIN + "/nobody/", text: "<html>not found</html>"}, "lo_profile_page"), ["error", "not_found"]);
  const five = classifyAnon({status: 502, url: ORIGIN + "/x", text: ""}, "lo_profile_page");
  assert.deepEqual([five.status, five.code, five.retry], ["error", "http_error", true]);
  const net = classifyAnon({status: 0, type: "error", url: "/x", text: ""}, "lo_post_gql");
  assert.deepEqual([net.status, net.code, net.retry], ["error", "network", true]);
  assert.deepEqual(cls(resp("gql_error.json")), ["ok", null], "the transport is fine; the parser calls it changed");
  assert.deepEqual([loInterpret("lo_post_gql", resp("gql_error.json")).code, loInterpret("lo_media_gql", J({data: {}}, 200)).code,
                    loInterpret("lo_posts_gql", J({data: {node: null}}, 200)).code, loInterpret("lo_web_profile", J({data: {user: null}}, 200)).code],
                   ["changed", "changed", "empty", "empty"]);
});

// ============================================================================================== validate

const pullJob = extra => ({id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "scaleit.demo", mode: "first", ...extra}});

test("validate: read mode and plan; a job without a plan runs logged-in only, as before", () => {
  const old = validateJob(pullJob({}));
  assert.ok(old.ok);
  assert.deepEqual(old.job.payload.plan, {lo: false, li: true, lo_budget: 0, strategies: []});
  assert.equal(old.job.payload.read, "auto");
  const v = validateJob(pullJob({read: "auto", plan: {lo: true, li: true, lo_budget: 40, strategies: ["lo_profile_page", "lo_post_gql", "li_clips"], rx: true, evil: "/x"}}));
  assert.ok(v.ok);
  assert.deepEqual(v.job.payload.plan, {lo: true, li: true, lo_budget: 40, strategies: ["lo_profile_page", "lo_post_gql", "li_clips"]},
                   "unknown plan keys are ignored");
  assert.ok(validateJob(pullJob({read: "logged_out_only", plan: {lo: true, li: false, lo_budget: 80, strategies: LO_IDS}})).ok);
  assert.ok(validateJob({id: ID, kind: "ig_profile", payload: {handle: "ok", purpose: "competitor", plan: {lo: true, li: true, lo_budget: 2, strategies: ["lo_profile_page"]}}}).job.payload.plan.lo);
  const bad = [
    {read: "fast"},
    {plan: {lo: false, li: false}},
    {plan: {lo: true, li: true, lo_budget: 81}},
    {plan: {lo: true, li: true, lo_budget: -1}},
    {plan: {lo: "yes", li: true}},
    {plan: {lo: true, li: true, strategies: ["lo_evil"]}},
    {plan: {lo: true, li: true, strategies: "lo_profile_page"}},
    {plan: [true, true]},
    {read: "logged_in_only", plan: {lo: true, li: true, lo_budget: 40}},
    {read: "logged_out_only", plan: {lo: true, li: true, lo_budget: 40}},
  ];
  for (const b of bad) {
    const r = validateJob(pullJob(b));
    assert.equal(r.ok, false, JSON.stringify(b));
    assert.ok(["read", "plan"].includes(r.field));
  }
});

test("validate: the probe job", () => {
  const ok = validateJob({id: ID, kind: "ig_probe", payload: {probe_id: "prb_abcdefghijklmnop", handles: ["instagram", "scaleit.demo"]}});
  assert.ok(ok.ok);
  assert.deepEqual(ok.job.payload.want, [...LO_IDS, "li_session", "li_clips"]);
  assert.ok(validateJob({id: ID, kind: "ig_probe", payload: {probe_id: "prb_abcdefghijklmnop", handles: ["instagram"], want: ["lo_profile_page", "li_clips"]}}).ok);
  for (const p of [{probe_id: "x", handles: ["instagram"]}, {probe_id: "prb_abcdefghijklmnop", handles: []},
                   {probe_id: "prb_abcdefghijklmnop", handles: ["a", "b", "c"]}, {probe_id: "prb_abcdefghijklmnop", handles: ["../x"]},
                   {probe_id: "prb_abcdefghijklmnop", handles: ["instagram"], want: ["lo_evil"]}]) {
    assert.equal(validateJob({id: ID, kind: "ig_probe", payload: p}).ok, false, JSON.stringify(p));
  }
});

// ============================================================================================== pacing

function fakeBudget(limits = DEFAULTS) {
  let data, now = 1_700_000_000_000, slept = 0;
  const b = new Budget({get: async () => data, set: async v => { data = JSON.parse(JSON.stringify(v)); }},
                       {now: () => now, sleep: async ms => { slept += ms; now += ms; }, rnd: () => 0.5, limits: () => limits});
  return {b, tick: ms => { now += ms; }, slept: () => slept};
}

test("pacing: logged-out gaps are slower, the page gap is 8-15 s, and the stricter of local and server wins", () => {
  assert.deepEqual(DEFAULTS.lo_gap_ms, [5000, 11000]);
  assert.deepEqual(DEFAULTS.lo_page_gap_ms, [8000, 15000]);
  assert.deepEqual(DEFAULTS.layer_gap_ms, [20000, 40000]);
  assert.deepEqual([DEFAULTS.lo_hour_max, DEFAULTS.lo_day_max, DEFAULTS.lo_detail_max, DEFAULTS.probe_day_max], [60, 200, 36, 3]);
  assert.ok(DEFAULTS.lo_gap_ms[0] >= DEFAULTS.gap_ms[0] && DEFAULTS.lo_gap_ms[1] >= DEFAULTS.gap_ms[1], "stricter or equal");
  const fast = {lo_gap_ms: [50, 100], lo_page_gap_ms: [50, 100], layer_gap_ms: [100, 200], lo_hour_max: 500, lo_day_max: 100};
  const zip = merge(DEFAULTS, fast, "zip");
  assert.deepEqual([zip.lo_gap_ms, zip.layer_gap_ms, zip.lo_hour_max, zip.lo_day_max], [[5000, 11000], [20000, 40000], 60, 100]);
  assert.deepEqual(merge(DEFAULTS, fast, "test").lo_gap_ms, [50, 100], "only a test build takes short gaps");
  assert.deepEqual(merge(DEFAULTS, {gap_ms: [50, 100]}, "test").lo_gap_ms, [5000, 11000], "a server without the keys changes nothing");
});

test("budget: logged-out calls count in their own budget AND in the hour and day budgets", async () => {
  const {b, slept} = fakeBudget();
  await b.take("lo");
  assert.equal(slept(), 0);
  await b.take("lo");
  assert.equal(slept(), 8000);                       // 5000 + (11000-5000)*0.5
  await b.take("lo_page");
  assert.equal(slept(), 8000 + 11500);               // the list-page gap: 8000 + 7000*0.5
  assert.deepEqual(await b.report(), {hour_used: 3, day_used: 3, cdn_day_used: 0, lo_hour_used: 3, lo_day_used: 3});
  await b.take("ig");
  assert.equal(slept(), 8000 + 11500 + 30000, "the first logged-in call waits the 20-40 s layer gap");
  await b.take("ig");
  assert.equal(slept(), 8000 + 11500 + 30000 + 6500, "then the normal logged-in gap again");
  assert.deepEqual(await b.report(), {hour_used: 5, day_used: 5, cdn_day_used: 0, lo_hour_used: 3, lo_day_used: 3});
  await b.take("lo");
  assert.equal(slept(), 8000 + 11500 + 30000 + 6500 + 8000, "a logged-out call after a logged-in one waits the logged-out gap");
});

test("budget: 60 logged-out calls per hour, 200 per day, 3 probes per day; the general limits still stop", async () => {
  const fast = {...DEFAULTS, lo_gap_ms: [0, 0], lo_page_gap_ms: [0, 0], gap_ms: [0, 0], layer_gap_ms: [0, 0]};
  let w = fakeBudget(fast);
  for (let i = 0; i < 60; i++) await w.b.take("lo");
  await assert.rejects(w.b.take("lo"), e => e instanceof BudgetStop && e.what === "lo_hour" && e.waitMs > 0);
  await w.b.take("ig");                              // the login budget still has room: 61 of 120
  assert.equal((await w.b.report()).hour_used, 61);
  w.tick(3600_000);
  await w.b.take("lo");
  w = fakeBudget({...fast, lo_hour_max: 1000, lo_day_max: 5, hour_max: 1000});
  for (let i = 0; i < 5; i++) await w.b.take("lo");
  await assert.rejects(w.b.take("lo_page"), e => e.what === "lo_day");
  w = fakeBudget({...fast, hour_max: 2});
  await w.b.take("lo"); await w.b.take("ig");
  await assert.rejects(w.b.take("lo"), e => e.what === "hour", "a logged-out call also needs room in the hour budget");
  w = fakeBudget(fast);
  for (let i = 0; i < 3; i++) await w.b.take("probe");
  await assert.rejects(w.b.take("probe"), e => e.what === "probe_day");
});

// ============================================================================================== ladder

const parsed = name => parseIg(fx(name));
const T = JSON.stringify;

// A small logged-out Instagram made of the fixtures. over[strategy] = (args, n) => response | fixture name.
function world({over = {}, off = [], replies = [], limits = DEFAULTS} = {}) {
  const calls = [], uploads = [], blocks = [];
  const count = {}, docOk = {};          // docOk: the id that worked last, per strategy (as sw.js keeps it in lo.doc_ok)
  const detail = (file, key, a) => {
    const d = parsed(file);
    const m = key === "post" ? d.data.xdt_api__v1__media__shortcode__web_info.items[0] : d.data.xig_polaris_media.if_not_gated_logged_out;
    m.code = a.code; m.pk = pkFromCode(a.code); m.id = `${m.pk}_${TARGET.pk}`;
    return {...resp(null), text: T(d)};
  };
  const base = {
    lo_profile_page: () => resp("profile_page.html"),
    lo_reels_page: () => resp("reels_page.html"),
    lo_reels_gql: a => resp(a.cursor ? "reels_gql_page2_last.json" : "reels_gql_page1.json"),
    lo_web_profile: () => resp("web_profile_info_ok.json"),
    lo_posts_gql: () => resp("posts_gql_page1.json"),
    lo_post_gql: a => detail("post_gql_ok.json", "post", a),
    lo_media_gql: a => detail("media_gql_ok.json", "media", {...a, code: codeFromPk(a.pk)}),
    lo_embed: a => ({...resp(null), text: fx("embed_ok.html").replaceAll("DfMCbOTgAAA", a.code).replaceAll("4020599234213445632", pkFromCode(a.code))}),
  };
  const io = {
    call: async (strategy, a, opts = {}) => {
      const ids = LO_DOC_IDS[strategy] || [];
      const idx = ids.length ? ((docOk[strategy] || 0) + (a.doc ? 1 : 0)) % ids.length : 0;
      const spec = loRequest(strategy, a, {lsd: a.lsd, docId: ids.length ? ids[idx] : undefined});
      if (spec.skip) return {status: "skipped", code: spec.skip, http: 0, items: 0};
      const n = count[strategy] = (count[strategy] || 0) + 1;
      calls.push({strategy, a: {...a}, opts, spec});
      let r = over[strategy] ? over[strategy](a, n, calls.length, spec) : base[strategy](a);
      if (typeof r === "string") r = resp(r);
      if (r === undefined) r = base[strategy](a);
      const res = loInterpret(strategy, r);
      res.more_ids = res.code === "changed" && !a.doc && ids.length > 1;
      if ((res.status === "ok" || res.status === "partial") && a.doc) docOk[strategy] = idx;
      return res;
    },
    upload: async body => {
      uploads.push(JSON.parse(T(body)));
      const rep = replies.length ? replies.shift() : null;
      const out = typeof rep === "function" ? rep(body) : rep;
      return out || {ok: true, accepted: body.items.length, known_hit: false, stop: false};
    },
    save: async () => {},
    off: s => off.includes(s),
    detailOff: () => off.includes("details"),
    block: async (kind, s) => { blocks.push([kind, s]); },
    limits: () => limits,
  };
  return {io, calls, uploads, blocks, names: () => calls.map(c => c.strategy)};
}

const pull = (extra = {}, plan = {}) => ({id: ID, kind: "ig_pull", payload: {account_id: ACC, handle: "scaleit.demo", mode: "first",
  target: 100, ig_pk: null, read: "auto", ...extra, plan: {lo: true, li: true, lo_budget: 40, strategies: LO_IDS.slice(), ...plan}}});
const RESUME = {ok: true, accepted: 0, stop: false, resume: {source: "clips", max_id: null, skip_known: true, layer: "logged_in"}};
const atDone = (w, reply) => { w.io.upload = (orig => async body => (body.paging.stage_done ? (w.uploads.push(JSON.parse(T(body))), reply) : orig(body)))(w.io.upload); };
const wall401 = () => resp("require_login_401.json", 401);
const noData = () => resp("profile_page_no_data.html");

test("ladder: the bulk path works. Profile page, reels page, then reels pages until the list ends", async () => {
  const w = world();
  atDone(w, RESUME);
  const st = {};
  const out = await runLoStage(pull(), st, w.io);
  assert.deepEqual(w.names(), ["lo_profile_page", "lo_reels_page", "lo_reels_gql"]);
  assert.equal(w.calls[2].a.cursor, loInterpret("lo_reels_page", resp("reels_page.html")).cursor, "the query goes on from the page's cursor");
  assert.deepEqual(w.calls[2].opts, {page: true}, "list pages use the page gap");
  assert.equal(w.calls[2].a.lsd, LSD, "the lsd token of the page of this job");
  assert.equal(w.uploads.length, 3);
  const [a, b, done] = w.uploads;
  assert.equal(a.layer, "logged_out");
  assert.deepEqual(a.account, JSON.parse(fx("upload_items.json")).account, "the account goes with the first items call");
  assert.equal(b.account, undefined);
  assert.deepEqual(a.paging, {source: "lo", strategy: "lo_reels_page", more_available: true, stage_done: false, lo_stop: null});
  assert.deepEqual([a.items.length, b.items.length], [12, 12]);
  assert.ok([...a.items, ...b.items].every(i => i.shape === "v1" && i.owner.pk === TARGET.pk && typeof i.play_count === "number"));
  assert.equal(new Set([...a.items, ...b.items].map(i => i.pk)).size, 24);
  assert.deepEqual(done.items, []);
  assert.deepEqual(done.paging, {source: "lo", strategy: "lo_reels_gql", more_available: false, stage_done: true, lo_stop: "no_more"});
  assert.deepEqual(out, {end: "resume", resume: RESUME.resume, lo_stop: "no_more"});
  assert.equal(st.collected, 24);
  assert.equal(st.lo.used, 3);
  for (const u of w.uploads) {
    const s = T(u);
    assert.ok(!s.includes(LSD), "the lsd token is never uploaded");
    assert.ok(!s.includes(st.lo.b.cursor || "QVFbG9y"), "GraphQL cursors are never uploaded");
    assert.equal(u.paging.max_id, undefined);
  }
});

test("ladder: the server says stop at the target. The stage ends with done, no stage_done call", async () => {
  const w = world({replies: [null, b => ({ok: true, accepted: b.items.length, stop: true})]});
  const st = {};
  const out = await runLoStage(pull({target: 24}), st, w.io);
  assert.deepEqual(out, {end: "stop", stop_reason: "target_reached", lo_stop: null});
  assert.equal(w.uploads.length, 2);
  const k = world({replies: [() => ({ok: true, accepted: 3, known_hit: true, stop: true})]});
  assert.equal((await runLoStage(pull({mode: "refresh"}), {}, k.io)).stop_reason, "known_reel");
});

test("ladder: bulk blocked, then the newest 12 with views, then details", async () => {
  const w = world({over: {lo_reels_page: noData, lo_reels_gql: wall401}});
  atDone(w, {ok: true, stop: true});
  const st = {};
  const out = await runLoStage(pull({read: "logged_out_only"}, {li: false, lo_budget: 80}), st, w.io);
  const names = w.names();
  assert.deepEqual(names.slice(0, 5), ["lo_profile_page", "lo_reels_page", "lo_reels_gql", "lo_web_profile", "lo_posts_gql"]);
  const details = w.calls.filter(c => c.strategy === "lo_post_gql");
  const page2 = loInterpret("lo_posts_gql", resp("posts_gql_page1.json")).stubs;
  assert.deepEqual(details.map(c => c.a.code), page2.filter(s => !s.photo).map(s => s.code),
                   "one detail call per reel stub: reels already read with views and known photos are skipped");
  assert.ok(details.every(c => c.spec.referrer === `${ORIGIN}/reel/${c.a.code}/`));
  assert.equal(names.filter(n => n === "lo_posts_gql").length, 1, "the fixture's posts list ends after one page");
  const gql = w.uploads[0];
  assert.deepEqual(gql.paging.strategy, "lo_web_profile");
  assert.ok(gql.items.length === 10 && gql.items.every(i => i.shape === "gql" && i.is_video));
  const v1 = w.uploads.slice(1, -1).flatMap(u => u.items);
  assert.equal(v1.length, details.length);
  assert.ok(w.uploads.slice(1, -1).every(u => u.items.length <= 6), "details upload every 6 items");
  assert.ok(v1.every(i => i.shape === "v1" && i.strategy === "lo_post_gql"));
  assert.equal(w.uploads.at(-1).paging.lo_stop, "no_more");
  assert.deepEqual(out, {end: "stop", stop_reason: "no_more", lo_stop: "no_more"});
  assert.deepEqual(w.blocks, [], "one login wall is not a block of the whole layer");
});

test("ladder: stubs with only a code. Photos are found by the detail call and dropped", async () => {
  const photoCodes = loInterpret("lo_profile_page", resp("profile_page.html")).stubs.filter(s => s.photo).map(s => s.code);
  const w = world({over: {lo_profile_page: () => resp("profile_page_codes_only.html"), lo_reels_page: noData, lo_reels_gql: wall401,
    lo_web_profile: () => ({...resp(null, 400), text: T({message: "bad", status: "fail"})}),
    lo_posts_gql: () => resp("posts_gql_page2_last.json"),
    lo_post_gql: a => (photoCodes.includes(a.code) ? {...resp(null), text: fx("post_gql_photo.json")} : undefined)}});
  atDone(w, {ok: true, stop: true});
  const st = {};
  await runLoStage(pull(), st, w.io);
  const firstTwelve = w.calls.filter(c => c.strategy === "lo_post_gql").slice(0, 12).map(c => c.a.code);
  assert.equal(firstTwelve.length, 12, "the type is unknown: every stub gets a detail call");
  const items = w.uploads.flatMap(u => u.items);
  assert.ok(items.length >= 10 && !items.some(i => photoCodes.includes(i.code)), "photos are never uploaded");
});

test("ladder: details without views. Auto mode stops after 3 and lets the login fill in the views", async () => {
  const noViews = a => { const d = parsed("post_gql_no_views.json"); const m = d.data.xdt_api__v1__media__shortcode__web_info.items[0];
                         m.code = a.code; m.pk = pkFromCode(a.code); m.id = `${m.pk}_${TARGET.pk}`; return {...resp(null), text: T(d)}; };
  const over = {lo_reels_page: noData, lo_reels_gql: wall401, lo_web_profile: () => ({...resp(null, 400), text: T({message: "bad", status: "fail"})}),
                lo_post_gql: noViews};
  const w = world({over});
  atDone(w, RESUME);
  const out = await runLoStage(pull(), {}, w.io);
  assert.equal(w.calls.filter(c => c.strategy === "lo_post_gql").length, 3);
  assert.deepEqual(w.blocks, [["no_views", "lo_post_gql"]]);
  assert.equal(w.uploads.at(-1).paging.lo_stop, "no_views");
  const items = w.uploads.flatMap(u => u.items);
  assert.equal(items.length, 3, "the reels without views are still uploaded (partial)");
  assert.ok(items.every(i => i.play_count === null && i.view_count === null && i.caption && i.video));
  assert.equal(out.end, "resume");

  const only = world({over});                          // "never use my login": keep reading what there is
  atDone(only, {ok: true, stop: true});
  const o2 = await runLoStage(pull({read: "logged_out_only"}, {li: false, lo_budget: 80}), {}, only.io);
  assert.ok(only.calls.filter(c => c.strategy === "lo_post_gql").length >= 15);
  assert.deepEqual(only.blocks, []);
  assert.equal(o2.end, "stop");

  const offNow = world({over, off: ["details"]});      // the switch-off from an earlier pull: no detail call at all
  atDone(offNow, RESUME);
  await runLoStage(pull(), {}, offNow.io);
  assert.equal(offNow.calls.filter(c => c.strategy === "lo_post_gql").length, 0);
  assert.equal(offNow.uploads.at(-1).paging.lo_stop, "no_views");
});

test("ladder: a login wall everywhere ends the stage after two calls", async () => {
  const w = world({over: {lo_profile_page: LOGIN_REDIRECT, lo_web_profile: wall401}});
  atDone(w, RESUME);
  const out = await runLoStage(pull(), {}, w.io);
  assert.deepEqual(w.names(), ["lo_profile_page", "lo_web_profile"]);
  assert.deepEqual(w.blocks, [["walls", "lo_web_profile"]], "a login wall on 2 strategies switches the layer off");
  assert.equal(w.uploads.length, 1);
  assert.deepEqual(w.uploads[0].paging, {source: "lo", strategy: "lo_web_profile", more_available: false, stage_done: true, lo_stop: "blocked"});
  assert.equal(w.uploads[0].account, undefined);
  assert.equal(out.end, "resume");
});

test("ladder: the profile page is walled but web_profile_info answers. User and newest 12, then the ladder goes on", async () => {
  const w = world({over: {lo_profile_page: LOGIN_REDIRECT}});
  atDone(w, RESUME);
  const st = {};
  await runLoStage(pull(), st, w.io);
  assert.deepEqual(w.names().slice(0, 3), ["lo_profile_page", "lo_web_profile", "lo_reels_page"]);
  assert.equal(st.lo.pk, TARGET.pk);
  assert.equal(w.uploads[0].account.pk, TARGET.pk);
  assert.equal(w.uploads[0].items[0].shape, "gql");
  assert.equal(w.names().filter(n => n === "lo_web_profile").length, 1, "web_profile_info is used once per pull");
});

test("ladder: a rate limit on call 2 fails the job with lo_blocked; what is in hand is uploaded first", async () => {
  const w = world({over: {lo_reels_page: () => resp("rate_429.json", 429)}});
  await assert.rejects(runLoStage(pull(), {}, w.io), e => e instanceof LoFail && e.code === "lo_blocked"
    && T(e.lo) === T({class: "rate", strategy: "lo_reels_page", http: 429}));
  assert.deepEqual(w.names(), ["lo_profile_page", "lo_reels_page"]);
  assert.equal(w.uploads.length, 0, "no stage_done after a hard block: the fail call reports it");

  // 4 details read, then "Please wait" on the 5th: the 4 are uploaded before the job fails.
  const k = world({over: {lo_reels_page: noData, lo_reels_gql: wall401, lo_web_profile: () => ({...resp(null, 400), text: T({message: "bad", status: "fail"})}),
                          lo_post_gql: (a, n) => (n === 5 ? resp("wait_401.json", 401) : undefined)}});
  await assert.rejects(runLoStage(pull(), {}, k.io), e => e.code === "lo_blocked" && e.lo.class === "rate" && e.lo.strategy === "lo_post_gql" && e.lo.http === 401);
  assert.equal(k.uploads.length, 1);
  assert.equal(k.uploads[0].items.length, 4);
  assert.equal(k.uploads[0].paging.stage_done, false);

  const c = world({over: {lo_profile_page: () => ({status: 200, url: ORIGIN + "/challenge/1/x/", redirected: true, text: "<html>"})}});
  await assert.rejects(runLoStage(pull(), {}, c.io), e => e.code === "lo_blocked" && e.lo.class === "challenge");
});

test("ladder: not found and private fail the pull", async () => {
  const nf = world({over: {lo_profile_page: () => ({status: 404, url: ORIGIN + "/nobody/", text: "<html>"})}});
  await assert.rejects(runLoStage(pull(), {}, nf.io), e => e instanceof LoFail && e.code === "not_found");
  const pv = world({over: {lo_profile_page: () => resp("profile_page_private.html")}});
  await assert.rejects(runLoStage(pull(), {}, pv.io), e => e instanceof LoFail && e.code === "private");
  assert.deepEqual(pv.names(), ["lo_profile_page"]);
  assert.equal(pv.uploads.length, 0);
});

test("ladder: refresh reads the user and ONE list result", async () => {
  const w = world();
  atDone(w, {ok: true, stop: true});
  const out = await runLoStage(pull({mode: "refresh"}, {lo_budget: 16}), {}, w.io);
  assert.deepEqual(w.names(), ["lo_profile_page", "lo_reels_page"]);
  assert.equal(w.uploads.at(-1).paging.lo_stop, "refresh_page");
  assert.equal(out.end, "stop");

  const c = world({over: {lo_reels_page: noData, lo_reels_gql: wall401}});
  atDone(c, RESUME);
  await runLoStage(pull({mode: "refresh"}, {lo_budget: 16}), {}, c.io);
  assert.deepEqual(c.names(), ["lo_profile_page", "lo_reels_page", "lo_reels_gql", "lo_web_profile"], "else the newest 12 with views");
  assert.equal(c.uploads.at(-1).paging.lo_stop, "refresh_page");

  const d = world({over: {lo_reels_page: noData, lo_reels_gql: wall401, lo_web_profile: () => ({...resp(null, 400), text: T({message: "bad", status: "fail"})})}});
  atDone(d, RESUME);
  await runLoStage(pull({mode: "refresh"}, {lo_budget: 16}), {}, d.io);
  assert.equal(d.calls.filter(x => x.strategy === "lo_post_gql").length, 10, "else details of the first page only (max 12)");
  assert.equal(d.names().includes("lo_posts_gql"), false);
  assert.equal(d.uploads.at(-1).paging.lo_stop, "refresh_page");
});

test("ladder: the budget ends the stage", async () => {
  const w = world();
  atDone(w, RESUME);
  const st = {};
  await runLoStage(pull({}, {lo_budget: 2}), st, w.io);
  assert.deepEqual(w.names(), ["lo_profile_page", "lo_reels_page"]);
  assert.equal(w.uploads.at(-1).paging.lo_stop, "budget");
  assert.equal(st.lo.used, 2);

  const d = world({over: {lo_reels_page: noData, lo_reels_gql: wall401}, limits: {...DEFAULTS, lo_detail_max: 4}});
  atDone(d, RESUME);
  await runLoStage(pull(), {}, d.io);
  assert.equal(d.calls.filter(x => x.strategy === "lo_post_gql").length, 4, "at most lo_detail_max detail calls");
  assert.equal(d.uploads.at(-1).paging.lo_stop, "budget");

  const sk = world({over: {lo_reels_gql: () => undefined}});   // the logged-out hour budget is used up: nothing is sent
  const orig = sk.io.call;
  sk.io.call = async (s, a, o) => (s === "lo_reels_gql" ? {status: "skipped", code: "budget", http: 0, items: 0} : orig(s, a, o));
  atDone(sk, RESUME);
  const st2 = {};
  await runLoStage(pull(), st2, sk.io);
  assert.equal(sk.uploads.at(-1).paging.lo_stop, "budget");
  assert.equal(st2.lo.used, 2, "a call that was not sent costs no budget");
});

test("ladder: strategies that are off or not in the plan are never called", async () => {
  const w = world({off: ["lo_reels_page", "lo_reels_gql"]});
  atDone(w, {ok: true, stop: true});
  await runLoStage(pull({}, {strategies: LO_IDS.filter(s => s !== "lo_web_profile" && s !== "lo_embed")}), {}, w.io);
  const used = new Set(w.names());
  assert.deepEqual([...used].sort(), ["lo_posts_gql", "lo_post_gql", "lo_profile_page"].sort());

  const none = world();
  atDone(none, RESUME);
  await runLoStage(pull({}, {strategies: ["li_clips"]}), {}, none.io);
  assert.deepEqual(none.names(), []);
  assert.equal(none.uploads[0].paging.lo_stop, "no_strategy");
});

test("ladder: a rotated doc id gets one try with the next id; a gated post only skips that reel", async () => {
  const dead = LO_DOC_IDS.lo_reels_gql[0];
  const w = world({over: {lo_reels_page: noData, lo_reels_gql: (a, n, k, spec) => (spec.form.doc_id === dead ? resp("gql_error.json") : undefined)}});
  atDone(w, RESUME);
  const st = {};
  await runLoStage(pull(), st, w.io);
  const q = w.calls.filter(c => c.strategy === "lo_reels_gql");
  assert.deepEqual(q.map(c => c.a.doc), [0, 1, 0], "after the retry works, the worker's kept id is used without a bump");
  assert.deepEqual(q.map(c => c.spec.form.doc_id), ["29285972954340351", "7845543455542541", "7845543455542541"]);
  assert.equal(st.collected, 24);

  const stubs = loInterpret("lo_posts_gql", resp("posts_gql_page1.json")).stubs.filter(s => !s.photo);
  const g = world({over: {lo_reels_page: noData, lo_reels_gql: wall401, lo_post_gql: a => (a.code === stubs[1].code ? resp("post_gql_empty.json") : undefined)}});
  atDone(g, {ok: true, stop: true});
  await runLoStage(pull(), {}, g.io);
  const codes = g.uploads.flatMap(u => u.items).filter(i => i.shape === "v1").map(i => i.code);
  assert.deepEqual(codes, stubs.filter((s, i) => i !== 1).map(s => s.code));
  assert.ok(!g.names().includes("lo_media_gql"), "one gated post does not switch the strategy");

  const sw = world({over: {lo_reels_page: noData, lo_reels_gql: wall401, lo_post_gql: () => resp("gql_error.json")}});
  atDone(sw, {ok: true, stop: true});
  await runLoStage(pull(), {}, sw.io);
  assert.equal(sw.calls.filter(c => c.strategy === "lo_post_gql").length, 1, "changed: the rest goes to the next detail strategy");
  const med = sw.calls.filter(c => c.strategy === "lo_media_gql");
  assert.equal(med.length, stubs.length);
  assert.deepEqual(med.map(c => c.a.pk), stubs.map(s => s.pk));
  assert.ok(sw.uploads.flatMap(u => u.items).filter(i => i.strategy === "lo_media_gql").every(i => i.shape === "v1" && typeof i.view_count === "number"));
});

test("ladder: a restarted worker resumes the stage from the stored state", async () => {
  const st = {};
  const a = world({over: {lo_reels_page: noData, lo_reels_gql: wall401}});
  let n = 0;
  const orig = a.io.call;
  a.io.call = async (s, x, o) => { if (++n === 8) throw new Error("worker stopped"); return orig(s, x, o); };
  await assert.rejects(runLoStage(pull(), st, a.io), /worker stopped/);
  const saved = JSON.parse(T(st));                     // what chrome.storage.local holds
  assert.equal(saved.lo.step, "D");
  const b = world({over: {lo_reels_page: noData, lo_reels_gql: wall401}});
  atDone(b, {ok: true, stop: true});
  await runLoStage(pull(), saved, b.io);
  assert.ok(!b.names().some(s => ["lo_profile_page", "lo_reels_page", "lo_web_profile"].includes(s)), "steps A to C are not read twice");
  const codes = [...a.uploads, ...b.uploads].flatMap(u => u.items).map(i => i.code || i.shortcode);
  assert.equal(new Set(codes).size, codes.length, "no reel is uploaded twice");
  assert.equal(freshLo().step, "A");
});

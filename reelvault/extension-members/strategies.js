// Logged-out read strategies (DESIGN-FETCH.md 3.2): request specs and pure parsers. No chrome.* here, so node
// can test every parser against the fixtures in tests/fixtures/ig/logged_out/.
// A request spec goes to reader.js igAnonFetch (credentials "omit": no cookie, no CSRF token, ever).
// Paths are built only from the LO table in endpoints.js, with values that passed validate.js.
import {IG, LO, LO_DOC_IDS, LO_IDS} from "./endpoints.js";
import {parseIg} from "./bigint.js";
import {isHandle, isCode, isPk, isLoCursor, isLsd, isDocId} from "./validate.js";
import {trimUser, trimXigUser, isReel} from "./trim.js";

const sid = v => (v === null || v === undefined || v === "") ? null : String(v);
const isObj = v => !!v && typeof v === "object" && !Array.isArray(v);
const num = v => typeof v === "number" && Number.isFinite(v);

// ---------------------------------------------------------------------------------------------- shortcode <-> pk
const ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

// A media pk is its shortcode read as a base-64 number (lo_media_gql asks by pk when a stub only has a code).
export function pkFromCode(code) {
  if (!isCode(code) || code.length > 11) return null;
  let n = 0n;
  for (const ch of code) n = n * 64n + BigInt(ALPHA.indexOf(ch));
  const s = n.toString();
  return isPk(s) ? s : null;
}

export function codeFromPk(pk) {
  if (!isPk(pk)) return null;
  let n = BigInt(pk), out = "";
  while (n > 0n) { out = ALPHA[Number(n % 64n)] + out; n /= 64n; }
  return out || null;
}

// ---------------------------------------------------------------------------------------------- doc ids

// hello.fetch.doc_ids -> {strategy: [id, ...]}. Bad or missing entries fall back to the built-in defaults.
export function cleanDocIds(fromServer) {
  const out = {};
  for (const [k, def] of Object.entries(LO_DOC_IDS)) {
    const v = isObj(fromServer) ? fromServer[k] : null;
    const ids = Array.isArray(v) ? v.filter(isDocId).slice(0, 4) : [];
    out[k] = ids.length ? ids : def.slice();
  }
  return out;
}

// ---------------------------------------------------------------------------------------------- request specs

const API_HEADERS = () => ({"X-IG-App-ID": IG.appId, "X-ASBD-ID": IG.asbdId, "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"});

function variables(strategy, a) {
  switch (strategy) {
    case "lo_posts_gql":
      return {id: a.pageId, after: a.cursor, before: null, first: 12, last: null,
              __relay_internal__pv__PolarisSEOIsCrawlerrelayprovider: false};
    case "lo_reels_gql":
      return {after: a.cursor ?? null, before: null,
              data: {include_feed_video: true, page_size: 12, target_user_id: a.pk}, first: 12, last: null, id: a.pk};
    case "lo_post_gql":
      return {shortcode: a.code, __relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider: false};
    case "lo_media_gql":
      return {media_id: a.pk};
  }
  return {};
}

// What each strategy needs before it may run. Returns null when all is there, else the skip code.
function missing(strategy, a) {
  const handle = isHandle(a.handle), code = isCode(a.code), pk = isPk(a.pk);
  switch (strategy) {
    case "lo_profile_page": case "lo_reels_page": case "lo_web_profile": return handle ? null : "bad_value";
    case "lo_reels_gql": return !pk ? "no_user" : (a.cursor !== null && a.cursor !== undefined && !isLoCursor(a.cursor)) ? "bad_value" : null;
    case "lo_posts_gql": return !isPk(a.pageId) ? "no_user" : !isLoCursor(a.cursor) ? "no_cursor" : null;
    case "lo_post_gql": case "lo_embed": return code ? null : "no_reel";
    case "lo_media_gql": return pk ? null : "no_reel";
  }
  return "bad_value";
}

// One request spec for igAnonFetch: {method, path, headers, form?, referrer} or {skip: code}.
// a: {handle?, code?, pk?, pageId?, cursor?}; ctx: {lsd, docId}.
export function loRequest(strategy, a = {}, ctx = {}) {
  const ep = LO[strategy];
  if (!LO_IDS.includes(strategy) || !ep) return {skip: "bad_value"};
  const miss = missing(strategy, a);
  if (miss) return {skip: miss};
  const origin = IG.origin;
  const profile = isHandle(a.handle) ? `${origin}/${encodeURIComponent(a.handle)}/` : origin + "/";
  const reel = isCode(a.code) ? `${origin}/reel/${encodeURIComponent(a.code)}/` : profile;
  if (ep.kind === "page") {
    const arg = strategy === "lo_embed" ? a.code : a.handle;
    return {method: "GET", path: ep.path(arg), headers: {"Accept": LO.pageAccept}, referrer: origin + "/"};
  }
  if (ep.kind === "api") {
    return {method: "GET", path: ep.path(a.handle), headers: API_HEADERS(), referrer: profile};
  }
  // GraphQL: the lsd token of the newest page of this job, never a CSRF token.
  if (!isLsd(ctx.lsd)) return {skip: "no_lsd"};
  if (!isDocId(ctx.docId)) return {skip: "bad_value"};
  const headers = {"X-IG-App-ID": IG.appId, "X-ASBD-ID": IG.asbdId, "X-FB-LSD": ctx.lsd, "Accept": "*/*"};
  const form = {lsd: ctx.lsd, fb_api_caller_class: "RelayModern"};
  if (ep.friendly) { headers["X-FB-Friendly-Name"] = ep.friendly; form.fb_api_req_friendly_name = ep.friendly; }
  form.server_timestamps = "true";
  form.variables = JSON.stringify(variables(strategy, a));
  form.doc_id = ctx.docId;
  const detail = strategy === "lo_post_gql" || strategy === "lo_media_gql";
  return {method: "POST", path: ep.path(), headers, form, referrer: detail ? reel : profile};
}

// ---------------------------------------------------------------------------------------------- block classes

const B = (status, code, http, extra = {}) => ({status, code, http, ...extra});

// The JSON body of an answer, or null. Anything before the first "{" is stripped ("for (;;);").
function jsonBody(text) {
  const t = String(text || "");
  const lead = t.slice(0, 40).trimStart();
  if (!lead || lead[0] === "<") return null;
  const i = t.indexOf("{");
  if (i < 0 || i > 40) return null;
  try { const d = parseIg(t.slice(i)); return isObj(d) ? d : null; } catch { return null; }
}

// Table 2.3: what one logged-out answer means. Returns {status: ok|blocked|error, code, http} and, when the
// transport is fine, the parsed JSON (data) or the page text (text). This is separate from the logged-in rules
// in sw.js: a logged-out block never sets the logged-in cooldown and never changes the sign-in state.
export function classifyAnon(r, strategy) {
  const ep = LO[strategy] || {kind: "api"};
  const http = Number.isInteger(r && r.status) ? r.status : 0;
  let where = "";
  try { where = new URL(String(r.url || ""), IG.origin).pathname; } catch { where = ""; }
  if (/\/challenge\//.test(where)) return B("blocked", "challenge", http);
  if (/\/accounts\/login/.test(where)) return B("blocked", "login_wall", http);
  if (r.redirected && (where === "/" || /^\/accounts\//.test(where))) return B("blocked", "login_wall", http);
  if (r.type === "opaqueredirect" || (http >= 300 && http < 400)) return B("blocked", "login_wall", http);
  if (http === 429) return B("blocked", "rate", http);
  if (http === 0 || http >= 500) return B("error", http ? "http_error" : "network", http, {retry: true});
  const text = String(r.text || "");
  const data = jsonBody(text);
  const msg = data ? `${data.message || ""} ${data.error_type || ""} ${data.feedback_title || ""}` : text.slice(0, 300);
  if (/checkpoint_required|challenge_required/i.test(msg) || (data && data.checkpoint_url)) return B("blocked", "challenge", http);
  if (/please wait a few minutes/i.test(msg) && (http === 401 || http === 403 || (data && data.status === "fail"))) return B("blocked", "rate", http);
  if (/feedback_required/i.test(msg) || (data && data.spam === true)) return B("blocked", "rate", http);
  if (http === 401 || http === 403) return B("blocked", "login_wall", http);
  if (data && (data.require_login === true || /login_required/i.test(msg))) return B("blocked", "login_wall", http);
  if (http === 404) return B("error", "not_found", http);
  if (http === 400 && strategy === "lo_web_profile") return B("blocked", "refused", http);
  if (http >= 400) return B("error", "http_error", http);
  if (ep.kind !== "page") {
    if (!data) return B("blocked", "login_wall", http);            // a page where JSON is due
    if (data.status === "fail") return B("error", "http_error", http);
    return B("ok", null, http, {data});
  }
  return B("ok", null, http, {text});
}

// ---------------------------------------------------------------------------------------------- page parsers

const SCRIPT_RE = /<script type="application\/json"[^>]*>([\s\S]*?)<\/script>/g;
const LSD_RE = /"LSD",\[\],\{[^{}]*"token":"([^"]+)"/;
const PROFILE_ID_RE = /"profile_id":"(\d{1,20})"/;

// Every embedded query result of a page: each <script type="application/json"> blob, walked for
// __bbox.result.data (the path yt-dlp and instaloader read).
export function embeddedData(html) {
  const out = [];
  const walk = (v, depth) => {
    if (depth > 40 || v === null || typeof v !== "object") return;
    if (Array.isArray(v)) { for (const x of v) walk(x, depth + 1); return; }
    const bb = v.__bbox;
    if (isObj(bb) && isObj(bb.result) && isObj(bb.result.data)) out.push(bb.result.data);
    for (const x of Object.values(v)) walk(x, depth + 1);
  };
  for (const m of String(html || "").matchAll(SCRIPT_RE)) {
    if (!m[1].includes('"__bbox"') || !m[1].includes('"result"')) continue;   // skip the many blobs without data
    let blob;
    try { blob = parseIg(m[1]); } catch { continue; }
    walk(blob, 0);
  }
  return out;
}

export function pageLsd(html) {
  const m = String(html || "").match(LSD_RE);
  return m && isLsd(m[1]) ? m[1] : null;
}

function stubOf(node) {
  if (!isObj(node) || !isCode(node.code)) return null;
  let pk = sid(node.pk);
  if (!isPk(pk)) pk = typeof node.id === "string" ? node.id.split("_")[0] : null;
  const known = node.media_type !== undefined && node.media_type !== null;
  // A photo for sure: a media type that is not video and not marked as a reel. Unknown type: read the detail.
  const photo = known && node.media_type !== 2 && node.product_type !== "clips";
  return {code: node.code, pk: isPk(pk) ? pk : null, photo, reel: isReel(node)};
}

function connection(conn) {
  const edges = isObj(conn) && Array.isArray(conn.edges) ? conn.edges : null;
  if (!edges) return null;
  const pi = isObj(conn.page_info) ? conn.page_info : {};
  const cursor = isLoCursor(pi.end_cursor) ? pi.end_cursor : null;
  return {nodes: edges.map(e => e && e.node).filter(isObj), cursor, more: !!pi.has_next_page && !!cursor};
}

// The first object (depth-first, at most maxDepth levels down) that has an `edges` array and passes `ok`.
function findConnection(v, maxDepth, ok = () => true, depth = 0) {
  if (!isObj(v) || depth > maxDepth) return null;
  if (Array.isArray(v.edges) && ok(v)) return v;
  for (const x of Object.values(v)) {
    const hit = isObj(x) ? findConnection(x, maxDepth, ok, depth + 1) : null;
    if (hit) return hit;
  }
  return null;
}

// GET /<handle>/ -> {user (raw xig_user_by_username, merged), pk, pageId, lsd, stubs, cursor, more, private}.
export function parseProfilePage(html) {
  const datas = embeddedData(html);
  let xig = null;
  for (const d of datas) if (isObj(d.xig_user_by_username)) xig = {...(xig || {}), ...d.xig_user_by_username};
  const m = String(html || "").match(PROFILE_ID_RE);
  let pk = xig ? sid(xig.pk) : null;
  if (!isPk(pk)) pk = m ? m[1] : null;
  const pageId = xig && isPk(sid(xig.id)) ? sid(xig.id) : null;
  const conn = xig ? connection(xig.polaris_ordered_timeline_connection) : null;
  return {user: xig, pk: isPk(pk) ? pk : null, pageId, lsd: pageLsd(html),
          stubs: conn ? conn.nodes.map(stubOf).filter(Boolean) : [], cursor: conn ? conn.cursor : null,
          more: conn ? conn.more : false, hasList: !!conn, private: !!(xig && xig.is_private)};
}

// GET /<handle>/reels/ -> the first embedded connection whose nodes have media.code.
export function parseReelsPage(html) {
  const base = parseProfilePage(html);
  let conn = null;
  for (const d of embeddedData(html)) {
    conn = findConnection(d, 8, c => c.edges.some(e => e && e.node && isObj(e.node.media) && isCode(e.node.media.code)));
    if (conn) break;
  }
  const c = conn ? connection(conn) : null;
  return {user: base.user, pk: base.pk, pageId: base.pageId, lsd: base.lsd,
          medias: c ? c.nodes.map(n => n.media).filter(isObj) : [], cursor: c ? c.cursor : null, more: c ? c.more : false,
          hasList: !!c};
}

const EMBED_RE = /__additionalDataLoaded\('extra',(\{.*?\})\);/s;

// GET /reel/<code>/embed/captioned/ -> {node} (a legacy GraphQL node) or {node: null}.
export function parseEmbed(html) {
  const m = String(html || "").match(EMBED_RE);
  if (!m) return {found: false, node: null};
  let d;
  try { d = parseIg(m[1]); } catch { return {found: false, node: null}; }
  return {found: true, node: isObj(d) && isObj(d.shortcode_media) ? d.shortcode_media : null};
}

// ---------------------------------------------------------------------------------------------- views

// A reel "has views" when a view number is there, or when the owner hides counts (then nobody gets one).
export const hasViews = m => !!m && (num(m.play_count) || num(m.view_count) || num(m.ig_play_count) || !!m.like_and_view_counts_disabled);
export const hasViewsGql = n => !!n && (num(n.video_play_count) || num(n.video_view_count) || !!n.like_and_view_counts_disabled);

// ---------------------------------------------------------------------------------------------- one answer -> result

// What the ladder and the probe work with:
// {status: ok|partial|blocked|error, code, http, retry?, user (IgUser)?, pk?, pageId?, lsd?, private?,
//  stubs?: [{code, pk, photo, reel}], medias?: [raw v1 media], nodes?: [raw legacy nodes], media?, node?, notReel?,
//  cursor?, more?, items: count for the fetch log}
export function loInterpret(strategy, r) {
  const c = classifyAnon(r, strategy);
  if (c.status !== "ok") return {...c, items: 0};
  const http = c.http;
  const d = c.data;
  const gqlErr = () => (Array.isArray(d.errors) && d.errors.length && !isObj(d.data));
  const res = (status, code, extra = {}) => ({status, code, http, items: 0, ...extra});
  switch (strategy) {
    case "lo_profile_page": {
      const p = parseProfilePage(c.text);
      const user = p.user && p.pk ? trimXigUser(p.user, p.pk) : null;
      const out = {user, pk: p.pk, pageId: p.pageId, lsd: p.lsd, stubs: p.stubs, cursor: p.cursor, more: p.more,
                   private: p.private, items: p.stubs.length};
      if (!p.user) return res("partial", "no_data", out);           // the page loads, no embedded data
      return res("ok", null, out);
    }
    case "lo_reels_page": {
      const p = parseReelsPage(c.text);
      const out = {pk: p.pk, lsd: p.lsd, medias: p.medias, cursor: p.cursor, more: p.more, items: p.medias.length};
      if (!p.hasList) return res("partial", "no_data", out);
      if (!p.medias.length) return res("partial", "empty", out);
      return res("ok", null, out);
    }
    case "lo_reels_gql": {
      if (gqlErr() || !isObj(d.data)) return res("blocked", "changed");
      const conn = findConnection(d.data, 4);
      if (!conn) return res("blocked", "changed");
      const cn = connection(conn);
      const medias = cn.nodes.map(n => (isObj(n.media) ? n.media : n)).filter(m => isObj(m) && isCode(m.code));
      const out = {medias, cursor: cn.cursor, more: cn.more, items: medias.length};
      return medias.length ? res("ok", null, out) : res("partial", "empty", out);
    }
    case "lo_web_profile": {
      if (!isObj(d.data) || !("user" in d.data)) return res("blocked", "changed");
      const u = d.data.user;
      if (!isObj(u)) return res("partial", "empty");
      const user = trimUser(u);
      const edges = (isObj(u.edge_owner_to_timeline_media) && Array.isArray(u.edge_owner_to_timeline_media.edges))
        ? u.edge_owner_to_timeline_media.edges : [];
      const nodes = edges.map(e => e && e.node).filter(n => isObj(n) && isCode(n.shortcode));
      return res("ok", null, {user: isPk(user.pk) ? user : null, pk: isPk(user.pk) ? user.pk : null,
                              private: !!user.is_private, nodes, items: nodes.length});
    }
    case "lo_posts_gql": {
      if (gqlErr() || !isObj(d.data) || !("node" in d.data)) return res("blocked", "changed");
      if (!isObj(d.data.node)) return res("partial", "empty");
      const cn = connection(d.data.node.polaris_ordered_timeline_connection);
      if (!cn) return res("blocked", "changed");
      const stubs = cn.nodes.map(stubOf).filter(Boolean);
      const out = {stubs, cursor: cn.cursor, more: cn.more, items: stubs.length};
      return stubs.length ? res("ok", null, out) : res("partial", "empty", out);
    }
    case "lo_post_gql": {
      const key = "xdt_api__v1__media__shortcode__web_info";
      if (gqlErr() || !isObj(d.data) || !(key in d.data)) return res("blocked", "changed");
      const items = isObj(d.data[key]) ? d.data[key].items : null;
      const m = Array.isArray(items) ? items[0] : null;
      if (!isObj(m)) return res("partial", "empty");                 // a gated post: skip this reel only
      return mediaResult(m, res);
    }
    case "lo_media_gql": {
      if (gqlErr() || !isObj(d.data) || !("xig_polaris_media" in d.data)) return res("blocked", "changed");
      const m = isObj(d.data.xig_polaris_media) ? d.data.xig_polaris_media.if_not_gated_logged_out : null;
      if (!isObj(m)) return res("partial", "empty");
      return mediaResult(m, res);
    }
    case "lo_embed": {
      const e = parseEmbed(c.text);
      if (!e.found) return res("partial", "no_data");
      if (!e.node) return res("partial", "empty");
      if (!e.node.is_video) return res("ok", null, {node: e.node, notReel: true, items: 1});
      return hasViewsGql(e.node) ? res("ok", null, {node: e.node, items: 1}) : res("partial", "no_views", {node: e.node, items: 1});
    }
  }
  return res("error", "bad_value");
}

function mediaResult(m, res) {
  if (!isReel(m)) return res("ok", null, {media: m, notReel: true, items: 1});
  return hasViews(m) ? res("ok", null, {media: m, items: 1}) : res("partial", "no_views", {media: m, items: 1});
}

// ---------------------------------------------------------------------------------------------- probe fields

const FIELD_ORDER = ["user", "followers", "reel_list", "lsd", "views", "likes", "comments", "taken_at", "caption",
                     "video_url", "duration", "signed_in"];

// The words a probe row reports (DESIGN-FETCH.md 3.4): what this answer gave.
export function probeFields(res) {
  const f = new Set();
  if (res.user && res.user.pk) f.add("user");
  if (res.user && res.user.followers !== null && res.user.followers !== undefined) f.add("followers");
  if ((res.stubs && res.stubs.length) || (res.medias && res.medias.length) || (res.nodes && res.nodes.length)) f.add("reel_list");
  if (res.lsd) f.add("lsd");
  const m = res.media || (res.medias && res.medias[0]) || null;
  if (m && !res.notReel) {
    if (num(m.play_count) || num(m.view_count) || num(m.ig_play_count)) f.add("views");
    if (num(m.like_count)) f.add("likes");
    if (num(m.comment_count)) f.add("comments");
    if (num(m.taken_at)) f.add("taken_at");
    if (m.caption && (typeof m.caption === "string" || m.caption.text)) f.add("caption");
    if (Array.isArray(m.video_versions) && m.video_versions.some(v => v && v.url)) f.add("video_url");
    if (num(m.video_duration)) f.add("duration");
  }
  const n = res.node || (res.nodes && res.nodes.find(x => x && x.is_video)) || null;
  if (n && !res.notReel) {
    const cnt = k => isObj(n[k]) && num(n[k].count);
    if (num(n.video_play_count) || num(n.video_view_count)) f.add("views");
    if (cnt("edge_liked_by") || cnt("edge_media_preview_like")) f.add("likes");
    if (cnt("edge_media_to_comment") || cnt("edge_media_to_parent_comment")) f.add("comments");
    if (num(n.taken_at_timestamp)) f.add("taken_at");
    const ce = isObj(n.edge_media_to_caption) && Array.isArray(n.edge_media_to_caption.edges) ? n.edge_media_to_caption.edges[0] : null;
    if (ce && ce.node && ce.node.text) f.add("caption");
    if (typeof n.video_url === "string" && n.video_url) f.add("video_url");
    if (num(n.video_duration)) f.add("duration");
  }
  return FIELD_ORDER.filter(k => f.has(k));
}

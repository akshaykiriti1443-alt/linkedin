// Every job is checked before ANY Instagram call (DESIGN-SAAS.md 5.4.0). A server can never steer the extension
// to another endpoint or smuggle a path: values that fail these rules make the job fail with "bad_job".
export const KINDS = ["ig_pull", "ig_profile", "ig_videos", "ig_discover", "ig_check", "page_read"];
export const IG_KINDS = ["ig_pull", "ig_profile", "ig_videos", "ig_discover", "ig_check"];
const PK = /^\d{1,20}$/;
const HANDLE = /^[a-z0-9._]{1,30}$/;
const CODE = /^[A-Za-z0-9_-]{5,20}$/;
const RVID = /^[a-z]{2,5}_[a-z2-7]{16}$/;
const CURSOR = /^[A-Za-z0-9_=-]{1,200}$/;
const QUERY = /^[A-Za-z0-9 &\-'.]{1,60}$/;
const PAGE_URL = /^https:\/\/www\.skool\.com\/(@[A-Za-z0-9-]{1,60}|[a-z0-9-]{1,80}\/about)$/;
export const LINKEDIN_SELF = "https://www.linkedin.com/in/me/";

const str = v => typeof v === "string";
export const isPk = v => str(v) && PK.test(v);
export const isHandle = v => str(v) && HANDLE.test(v);
export const isCode = v => str(v) && CODE.test(v);
export const isRvId = v => str(v) && RVID.test(v);
export const isCursor = v => str(v) && CURSOR.test(v);
export const isQuery = v => str(v) && QUERY.test(v);
export const isPageUrl = (kind, u) => kind === "linkedin_self" ? u === LINKEDIN_SELF : (str(u) && PAGE_URL.test(u));
const optPk = v => v === undefined || v === null || isPk(v);
const int = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;

class Bad extends Error { constructor(field) { super(field); this.field = field; } }
const need = (ok, field) => { if (!ok) throw new Bad(field); };

// Returns {ok: true, job: <clean copy with only known fields>} or {ok: false, field}.
export function validateJob(job) {
  try {
    need(job && typeof job === "object", "job");
    need(isRvId(job.id), "id");
    need(KINDS.includes(job.kind), "kind");
    const p = job.payload;
    need(p && typeof p === "object" && !Array.isArray(p), "payload");
    let c;
    switch (job.kind) {
      case "ig_pull": {
        need(isRvId(p.account_id), "account_id");
        need(isHandle(p.handle), "handle");
        need(["first", "next", "refresh"].includes(p.mode), "mode");
        const src = p.cursor_source ?? "clips";
        need(["clips", "feed"].includes(src), "cursor_source");
        need(optPk(p.ig_pk) && optPk(p.oldest_pk), "pk");
        need(p.max_id === undefined || p.max_id === null || isCursor(p.max_id), "max_id");
        const target = p.target ?? 100;
        need(int(target, 1, 100), "target");
        c = {account_id: p.account_id, handle: p.handle, mode: p.mode, cursor_source: src, target,
             ig_pk: p.ig_pk ?? null, oldest_pk: p.oldest_pk ?? null, max_id: p.max_id ?? null};
        break;
      }
      case "ig_profile":
        need(isHandle(p.handle), "handle");
        need(["competitor", "member_bio"].includes(p.purpose), "purpose");
        need(p.account_id === undefined || isRvId(p.account_id), "account_id");
        need(p.source_id === undefined || isRvId(p.source_id), "source_id");
        c = {handle: p.handle, purpose: p.purpose};
        break;
      case "ig_videos":
        need(Array.isArray(p.items) && int(p.items.length, 1, 30), "items");
        for (const it of p.items) need(it && isRvId(it.reel_id) && isPk(it.ig_pk) && isCode(it.code), "items");
        c = {items: p.items.map(it => ({reel_id: it.reel_id, ig_pk: it.ig_pk, code: it.code})),
             refresh_urls: p.refresh_urls !== false, want_cover: !!p.want_cover};
        break;
      case "ig_discover": {
        need(isRvId(p.run_id), "run_id");
        need(["search", "similar"].includes(p.mode), "mode");
        const qs = p.queries ?? [], ss = p.seeds ?? [];
        need(Array.isArray(qs) && qs.length <= 3 && qs.every(isQuery), "queries");
        need(Array.isArray(ss) && ss.length <= 2 && ss.every(s => s && isHandle(s.handle) && optPk(s.pk)), "seeds");
        need(p.mode === "search" ? qs.length > 0 : ss.length > 0, p.mode === "search" ? "queries" : "seeds");
        c = {run_id: p.run_id, mode: p.mode, queries: qs.slice(), seeds: ss.map(s => ({handle: s.handle, pk: s.pk ?? null}))};
        break;
      }
      case "ig_check":
        need(isRvId(p.run_id), "run_id");
        need(Array.isArray(p.candidates) && int(p.candidates.length, 1, 15), "candidates");
        for (const x of p.candidates) need(x && isRvId(x.cand_id) && isHandle(x.handle) && optPk(x.pk), "candidates");
        c = {run_id: p.run_id, candidates: p.candidates.map(x => ({cand_id: x.cand_id, handle: x.handle, pk: x.pk ?? null}))};
        break;
      case "page_read":
        need(isRvId(p.source_id), "source_id");
        need(["skool_profile", "skool_group", "linkedin_self"].includes(p.kind), "kind");
        need(isPageUrl(p.kind, p.url), "url");
        c = {source_id: p.source_id, kind: p.kind, url: p.url};
        break;
    }
    return {ok: true, job: {id: job.id, kind: job.kind, payload: c}};
  } catch (e) {
    if (e instanceof Bad) return {ok: false, field: e.field};
    return {ok: false, field: "job"};
  }
}

// A pair link "https://HOST/pair#7KQ4-M2XP" -> {origin, code} when the origin is an exact entry of SERVERS.
export function parsePairLink(text, servers) {
  let u;
  try { u = new URL(String(text || "").trim()); } catch { return {ok: false, error: "not_link"}; }
  const code = decodeURIComponent((u.hash || "").slice(1)).trim().toUpperCase();
  if (!servers.includes(u.origin)) return {ok: false, error: "not_ours", origin: u.origin};
  if (u.pathname !== "/pair" || !/^[0-9A-Z]{4}-?[0-9A-Z]{4}$/.test(code)) return {ok: false, error: "no_code"};
  return {ok: true, origin: u.origin, host: u.host, code};
}

// "2.0.10" is newer than "2.0.9". Used for the "Update available" line (5.0 (b)); bad input is never newer.
export function isNewer(a, b) {
  const parse = v => (typeof v === "string" && /^\d{1,4}(\.\d{1,5}){0,3}$/.test(v)) ? v.split(".").map(Number) : null;
  const x = parse(a), y = parse(b);
  if (!x || !y) return false;
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] || 0) - (y[i] || 0);
    if (d) return d > 0;
  }
  return false;
}

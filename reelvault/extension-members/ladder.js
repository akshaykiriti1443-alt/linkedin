// The logged-out stage of one pull (DESIGN-FETCH.md 2.1): steps A (user), B (bulk reels with views), C (newest 12
// with views), D (one detail call per reel), E (stage end). Pure logic: the service worker injects the calls, so
// node can run every path against the fixtures.
//
// io = {
//   call(strategy, args, {page}) -> result of strategies.js loInterpret, or {status: "skipped", code}
//   upload(body) -> the server's reply {accepted, known_hit, stop, resume}
//   save()       -> persist job state (st)
//   off(strategy) -> true when this strategy is switched off on this device
//   detailOff()  -> true when detail calls are switched off for pulls (3 details in a row without views)
//   block(kind, strategy) -> tell the worker about "walls" (login wall on 2+ strategies) and "no_views"
//   limits()     -> pacing numbers (lo_detail_max)
// }
import {LO_DETAIL_IDS} from "./endpoints.js";
import {isPk} from "./validate.js";
import {trimMedia, trimGql, isReel, keepForTarget, keepForTargetGql} from "./trim.js";
import {pkFromCode, hasViews, hasViewsGql} from "./strategies.js";

export const HARD_BLOCKS = ["rate", "challenge"];   // these stop every logged-out read (table 2.3)
export const LO_STOPS = ["target", "no_more", "budget", "blocked", "no_views", "no_strategy", "refresh_page"];
const POSTS_PAGES_MAX = 3;
const REFRESH_DETAILS = 12;
const UPLOAD_EVERY = 6;

export class LoFail extends Error {                 // the pull fails: not_found | private | lo_blocked
  constructor(code, extra = {}) { super(code); this.code = code; Object.assign(this, extra); }
}
class StageEnd extends Error { constructor(why) { super(why); this.why = why; } }
class ServerStop extends Error { constructor(reply) { super("stop"); this.reply = reply; } }

export function freshLo() {
  return {step: "A", used: 0, details: 0, noViews: 0, viewsSeen: 0, empties: 0, lists: 0, bulkViews: 0,
          lsd: null, pageId: null, pk: null, account: null, accountSent: false, webUsed: false,
          todo: [], have: [], sent: [], pend: [], walls: [], doc: {}, docTried: {}, dchain: 0, last: null,
          b: {page: false, started: false, cursor: null, more: false, pages: 0},
          p: {cursor: null, more: false, pages: 0}};
}

// Runs the stage. Returns {end: "stop", stop_reason, lo_stop} (send done) or {end: "resume", resume, lo_stop}
// (go on with the logged-in top-up). Throws LoFail for not_found, private and a hard block.
export async function runLoStage(job, st, io) {
  const p = job.payload, plan = p.plan;
  const L = (st.lo ??= freshLo());
  st.collected ??= 0;
  const S = new Set(plan.strategies);
  const refresh = p.mode === "refresh";
  const usable = s => S.has(s) && !io.off(s);
  const left = () => plan.lo_budget - L.used;
  const listDone = () => refresh && L.lists >= 1;     // a refresh reads ONE list result

  async function call(strategy, args, opts = {}) {
    if (left() <= 0) throw new StageEnd("budget");
    L.used += 1; L.last = strategy;
    const r = await io.call(strategy, {...args, lsd: L.lsd, doc: L.doc[strategy] || 0}, opts);
    if (r.status === "skipped") {                     // nothing was sent: it costs no budget
      L.used -= 1;
      if (r.code === "budget") throw new StageEnd("budget");
      if (r.code === "cooling") throw new StageEnd("blocked");
      return r;
    }
    if (r.lsd) L.lsd = r.lsd;
    await io.save();
    if (r.status === "blocked" && HARD_BLOCKS.includes(r.code)) {
      throw new LoFail("lo_blocked", {lo: {class: r.code, strategy, http: r.http || 0}});
    }
    if (r.code === "login_wall" && !L.walls.includes(strategy)) {
      L.walls.push(strategy);
      if (L.walls.length >= 2) { await io.block("walls", strategy); throw new StageEnd("blocked"); }
    }
    if (r.code === "changed" && r.more_ids && !L.docTried[strategy]) {   // a rotated doc id: try the next id once
      L.docTried[strategy] = true; L.doc[strategy] = (L.doc[strategy] || 0) + 1;
      const r2 = await call(strategy, args, opts);
      // The next id works: the worker keeps it as this device's first choice, so later calls ask without a bump.
      if (r2.status === "ok" || r2.status === "partial") L.doc[strategy] = 0;
      return r2;
    }
    return r;
  }

  async function upload(items, strategy, more) {
    const body = {layer: "logged_out", items,
                  paging: {source: "lo", strategy, more_available: !!more, stage_done: false, lo_stop: null}};
    if (L.account && !L.accountSent) body.account = L.account;
    const up = (await io.upload(body)) || {};
    if (body.account) L.accountSent = true;
    st.collected += up.accepted || 0;
    await io.save();
    if (up.stop) throw new ServerStop(up);
    return up;
  }

  const mark = (code, views) => {
    if (!L.sent.includes(code)) L.sent.push(code);
    if (views && !L.have.includes(code)) L.have.push(code);
  };

  // Reels in the private-API shape (reels tab nodes): keep the target's reels, trim, upload.
  async function sendV1(medias, strategy, more) {
    const items = [];
    let views = 0;
    for (const m of medias) {
      if (!m || !keepForTarget(m, L.pk) || !isReel(m)) continue;
      const v = hasViews(m);
      if (v) views += 1;
      mark(m.code, v);
      items.push({shape: "v1", strategy, ...trimMedia(m)});
    }
    if (items.length) await upload(items, strategy, more);
    return {n: items.length, views};
  }

  async function sendGql(nodes, strategy) {
    const items = [];
    for (const n of nodes) {
      if (!n || !n.is_video || !keepForTargetGql(n, L.pk)) continue;
      mark(n.shortcode, hasViewsGql(n));
      items.push({shape: "gql", strategy, ...trimGql(n)});
    }
    if (items.length) await upload(items, strategy, false);
    return items.length;
  }

  const takeUser = r => {
    if (r.user && isPk(r.user.pk)) { L.account = L.accountSent ? null : r.user; L.pk = r.user.pk; }
    else if (isPk(r.pk)) L.pk = r.pk;
    if (isPk(r.pageId)) L.pageId = r.pageId;
  };

  async function flush(strategy) {
    if (!L.pend.length) return;
    const items = L.pend;
    L.pend = [];
    await upload(items, strategy || L.last, L.todo.length > 0 || L.p.more);
  }

  // ------------------------------------------------------------------------------------------ A: the user
  async function stepA() {
    let pageOk = false;
    if (usable("lo_profile_page")) {
      const r = await call("lo_profile_page", {handle: p.handle});
      if (r.code === "not_found") throw new LoFail("not_found", {http_status: 404});
      if (r.private) throw new LoFail("private");
      if (r.status === "ok" || r.status === "partial") {
        pageOk = true;
        takeUser(r);
        L.todo = (r.stubs || []).map(s => ({code: s.code, pk: s.pk, photo: !!s.photo}));
        L.p.cursor = r.cursor || null; L.p.more = !!r.more;
      }
    }
    if (!L.pk && usable("lo_web_profile")) {          // login wall or no user: ask web_profile_info, no cookies
      L.webUsed = true;
      const w = await call("lo_web_profile", {handle: p.handle});
      if (w.code === "not_found") throw new LoFail("not_found", {http_status: 404});
      if (w.private) throw new LoFail("private");
      if (w.status === "ok") {
        takeUser(w);
        if (L.pk) { L.lists += 1; await sendGql(w.nodes || [], "lo_web_profile"); }
      }
    }
    // The page loaded but named nobody: the pk the server already knows will do.
    if (!L.pk && pageOk && isPk(p.ig_pk)) L.pk = p.ig_pk;
    if (!L.pk) throw new StageEnd(L.used ? "blocked" : "no_strategy");
  }

  // ------------------------------------------------------------------------------------------ B: bulk
  async function stepB() {
    const b = L.b;
    if (!b.page) {
      b.page = true;
      if (usable("lo_reels_page") && !listDone()) {
        const r = await call("lo_reels_page", {handle: p.handle});
        if (r.status === "ok") {
          b.started = true; b.cursor = r.cursor || null; b.more = !!r.more; L.lists += 1;
          const s = await sendV1(r.medias || [], "lo_reels_page", b.more);
          L.bulkViews += s.views;
        }
      }
    }
    while (usable("lo_reels_gql") && !listDone()) {
      if (b.started && (!b.more || L.bulkViews === 0)) break;      // the list ended, or it carries no views
      const r = await call("lo_reels_gql", {pk: L.pk, cursor: b.started ? b.cursor : null, handle: p.handle},
                           {page: b.pages > 0 || b.started});
      if (r.status !== "ok") { b.blocked = true; break; }          // blocked, changed, empty or no lsd
      b.started = true; b.pages += 1; b.cursor = r.cursor || null; b.more = !!r.more; L.lists += 1;
      const s = await sendV1(r.medias || [], "lo_reels_gql", b.more);
      L.bulkViews += s.views;
      if (!s.n) break;                                             // a page without one reel of the target
    }
  }

  // ------------------------------------------------------------------------------------------ C: newest 12 with views
  async function stepC() {
    if (L.webUsed || !usable("lo_web_profile") || listDone()) return;
    L.webUsed = true;
    const w = await call("lo_web_profile", {handle: p.handle});
    if (w.status === "ok") { L.lists += 1; await sendGql(w.nodes || [], "lo_web_profile"); }
  }

  // ------------------------------------------------------------------------------------------ D: details
  async function stepD() {
    if (io.detailOff()) return "no_views";
    const chain = LO_DETAIL_IDS.filter(usable);
    const max = Math.min(refresh ? REFRESH_DETAILS : io.limits().lo_detail_max, io.limits().lo_detail_max);
    const stopEarly = p.read === "auto" && plan.li;                // the login can fill in the views
    for (;;) {
      while (L.todo.length && (L.todo[0].photo || L.have.includes(L.todo[0].code))) L.todo.shift();
      if (!L.todo.length) {
        if (refresh || !L.p.more || L.p.pages >= POSTS_PAGES_MAX || !usable("lo_posts_gql")) break;
        L.p.pages += 1;
        const r = await call("lo_posts_gql", {pageId: L.pageId, cursor: L.p.cursor, handle: p.handle}, {page: true});
        if (r.status !== "ok") { L.p.more = false; L.p.blocked = true; break; }
        L.todo = (r.stubs || []).filter(s => !L.sent.includes(s.code)).map(s => ({code: s.code, pk: s.pk, photo: !!s.photo}));
        L.p.cursor = r.cursor || null; L.p.more = !!r.more;
        continue;
      }
      if (L.details >= max) return "budget";
      if (st.collected + L.pend.length >= p.target) return "target";
      const s = chain[L.dchain];
      if (!s) return L.details ? "blocked" : "no_strategy";
      const stub = L.todo[0];
      const r = await call(s, {code: stub.code, pk: stub.pk || pkFromCode(stub.code), handle: p.handle});
      if (r.status === "skipped") { L.dchain += 1; continue; }     // no lsd for GraphQL: the next strategy
      L.details += 1;
      const got = r.media || r.node || null;
      if ((r.status === "ok" || r.code === "no_views") && got) {
        L.todo.shift(); L.empties = 0;
        const v1 = !!r.media;
        const mine = v1 ? keepForTarget(got, L.pk) : keepForTargetGql(got, L.pk);
        if (!r.notReel && mine) {
          const views = v1 ? hasViews(got) : hasViewsGql(got);
          mark(stub.code, views);
          L.pend.push(v1 ? {shape: "v1", strategy: s, ...trimMedia(got)} : {shape: "gql", strategy: s, ...trimGql(got)});
          if (views) { L.viewsSeen += 1; L.noViews = 0; } else L.noViews += 1;
        }
      } else if (r.code === "empty" || r.code === "not_found") {   // a gated or deleted post: skip this reel only
        L.todo.shift();
        if (r.code === "empty" && ++L.empties >= 3) { L.empties = 0; L.dchain += 1; }
      } else {
        L.dchain += 1; L.empties = 0;                              // blocked or changed: the next strategy, same reel
        continue;
      }
      if (L.pend.length >= UPLOAD_EVERY) await flush(s);
      if (L.noViews >= 3) {                                        // 3 details in a row without views
        if (stopEarly) { await io.block("no_views", s); await flush(s); return "no_views"; }
        L.noViews = 0;                                             // no login to fall back on: keep what there is
      }
    }
    return L.p.blocked ? "blocked" : (L.p.more ? "budget" : "no_more");
  }

  // ------------------------------------------------------------------------------------------ the stage
  let loStop = null, hard = null;
  try {
    if (!plan.strategies.some(s => s.startsWith("lo_"))) throw new StageEnd("no_strategy");
    if (L.step === "A") { await stepA(); L.step = "B"; await io.save(); }
    if (L.step === "B") { await stepB(); L.step = "C"; await io.save(); }
    if (L.bulkViews > 0) {
      loStop = refresh ? "refresh_page" : L.b.blocked ? "blocked" : "no_more";
    } else {
      if (L.step === "C") { await stepC(); L.step = "D"; await io.save(); }
      if (listDone()) loStop = "refresh_page";
      else {
        loStop = await stepD();
        await flush();
        if (refresh && L.details > 0) loStop = "refresh_page";
      }
    }
  } catch (e) {
    if (e instanceof ServerStop) return finish(e.reply);
    if (e instanceof LoFail && e.code === "lo_blocked") hard = e;   // upload what is in hand, then fail
    else if (!(e instanceof StageEnd)) throw e;
    else loStop = e.why;
    try { await flush(); } catch (e2) { if (e2 instanceof ServerStop) return finish(e2.reply); throw e2; }
    if (hard) throw hard;
  }

  // ------------------------------------------------------------------------------------------ E: stage end
  if (st.collected >= p.target) loStop = "target";
  const body = {layer: "logged_out", items: [],
                paging: {source: "lo", strategy: L.last, more_available: false, stage_done: true, lo_stop: loStop}};
  if (L.account && !L.accountSent) body.account = L.account;
  const up = (await io.upload(body)) || {};
  if (body.account) L.accountSent = true;
  L.step = "E"; L.stop = loStop;
  await io.save();
  if (up.resume && plan.li && !up.stop) return {end: "resume", resume: up.resume, lo_stop: loStop};
  return finish(up, loStop);

  function finish(reply, why = null) {
    const stop_reason = reply && reply.known_hit ? "known_reel" : st.collected >= p.target ? "target_reached"
      : why === "no_more" ? "no_more" : "page_cap";
    return {end: "stop", stop_reason, lo_stop: why};
  }
}

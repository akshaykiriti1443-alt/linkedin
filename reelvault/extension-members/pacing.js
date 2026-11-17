// Pacing rules (DESIGN-SAAS.md 5.6). The extension enforces them itself AND the server checks them.
// One Instagram request at a time; random gaps; sliding hour and calendar-day budgets kept in chrome.storage.local.
export const DEFAULTS = {
  gap_ms: [4000, 9000], page_gap_ms: [6000, 12000], cdn_gap_ms: [2000, 5000], retry_ms: [30000, 120000],
  hour_max: 120, day_max: 400, media_info_max: 30, cdn_day_max: 150, discover_day_max: 60, page_read_day_max: 5,
  // Logged-out reads (DESIGN-FETCH.md 2.4): slower than the logged-in gaps, with their own hour and day budgets.
  // Every logged-out call ALSO counts in hour_max and day_max.
  lo_gap_ms: [5000, 11000], lo_page_gap_ms: [8000, 15000], layer_gap_ms: [20000, 40000],
  lo_hour_max: 60, lo_day_max: 200, lo_detail_max: 36, probe_day_max: 3,
};

// The stricter of local and server values. A "test" build (made only for the automated tests) takes the server's
// numbers as they are, so tests can run with short gaps; store and ZIP builds can never be sped up by a server.
export function merge(local, server, build) {
  const out = {...local};
  if (!server || typeof server !== "object") return out;
  for (const [k, v] of Object.entries(local)) {
    const s = server[k];
    if (Array.isArray(v)) {
      if (!(Array.isArray(s) && s.length === 2 && s.every(n => Number.isFinite(n) && n >= 0))) continue;
      out[k] = build === "test" ? [s[0], Math.max(s[0], s[1])] : [Math.max(v[0], s[0]), Math.max(v[1], s[1])];
    } else if (Number.isFinite(s) && s >= 0) {
      out[k] = build === "test" ? s : Math.min(v, s);
    }
  }
  return out;
}

export const randBetween = ([a, b], rnd = Math.random) => Math.round(a + (b - a) * rnd());

export function localDay(ts) {
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const HOUR = 3600_000;

export class BudgetStop extends Error {
  constructor(what, waitMs) { super(`budget: ${what}`); this.what = what; this.waitMs = waitMs; }
}

// store: {get(): Promise<object|undefined>, set(obj): Promise<void>} (chrome.storage.local in the extension)
export class Budget {
  constructor(store, {now = () => Date.now(), sleep = ms => new Promise(r => setTimeout(r, ms)), rnd = Math.random,
                      limits = () => DEFAULTS} = {}) {
    this.store = store; this.now = now; this.sleep = sleep; this.rnd = rnd; this.limits = limits;
  }
  async load() {
    const s = (await this.store.get()) || {};
    const t = this.now();
    const day = localDay(t);
    const st = {calls: [], day, day_used: 0, cdn_day_used: 0, disc_day_used: 0, page_read_day_used: 0,
                last_call_at: 0, last_cdn_at: 0, lo_calls: [], lo_day_used: 0, last_lo_at: 0, probe_day_used: 0, ...s};
    st.calls = (st.calls || []).filter(x => t - x < HOUR);
    st.lo_calls = (st.lo_calls || []).filter(x => t - x < HOUR);
    if (st.day !== day) {
      Object.assign(st, {day, day_used: 0, cdn_day_used: 0, disc_day_used: 0, page_read_day_used: 0, lo_day_used: 0,
                         probe_day_used: 0});
    }
    return st;
  }
  async save(st) { await this.store.set(st); }
  async report() {
    const st = await this.load();
    return {hour_used: st.calls.length, day_used: st.day_used, cdn_day_used: st.cdn_day_used,
            lo_hour_used: st.lo_calls.length, lo_day_used: st.lo_day_used};
  }
  msToNextDay(t = this.now()) {
    const d = new Date(t); d.setHours(24, 0, 5, 0); return d.getTime() - t;
  }
  // Throws BudgetStop when the call would break a limit.
  // kind: "ig" | "discover" | "cdn" | "page_read" | "lo" | "lo_page" (logged-out call / list page) | "probe".
  async check(kind) {
    const L = this.limits(), st = await this.load(), t = this.now();
    if (kind === "cdn") {
      if (st.cdn_day_used >= L.cdn_day_max) throw new BudgetStop("cdn_day", this.msToNextDay(t));
      return st;
    }
    if (kind === "page_read") {
      if (st.page_read_day_used >= L.page_read_day_max) throw new BudgetStop("page_read_day", this.msToNextDay(t));
      return st;
    }
    if (kind === "probe") {
      if (st.probe_day_used >= L.probe_day_max) throw new BudgetStop("probe_day", this.msToNextDay(t));
      return st;
    }
    if (kind === "lo" || kind === "lo_page") {   // the logged-out limits first: they only end the logged-out stage
      if (st.lo_calls.length >= L.lo_hour_max) throw new BudgetStop("lo_hour", Math.max(1000, HOUR - (t - st.lo_calls[0])));
      if (st.lo_day_used >= L.lo_day_max) throw new BudgetStop("lo_day", this.msToNextDay(t));
    }
    if (st.calls.length >= L.hour_max) throw new BudgetStop("hour", Math.max(1000, HOUR - (t - st.calls[0])));
    if (st.day_used >= L.day_max) throw new BudgetStop("day", this.msToNextDay(t));
    if (kind === "discover" && st.disc_day_used >= L.discover_day_max) throw new BudgetStop("discover_day", this.msToNextDay(t));
    return st;
  }
  // Wait for the random gap since the last call of this type, then count the call. page=true adds the page gap.
  async take(kind, {page = false} = {}) {
    const L = this.limits();
    let st = await this.check(kind);
    if (kind === "page_read") {
      st.page_read_day_used += 1; await this.save(st); return;
    }
    if (kind === "probe") {
      st.probe_day_used += 1; await this.save(st); return;
    }
    const isCdn = kind === "cdn";
    const isLo = kind === "lo" || kind === "lo_page";
    const last = isCdn ? st.last_cdn_at : st.last_call_at;   // last_call_at: the last Instagram call of EITHER layer
    let gap;
    if (isLo) {
      gap = randBetween(L.lo_gap_ms, this.rnd);
      if (kind === "lo_page") gap = Math.max(gap, randBetween(L.lo_page_gap_ms, this.rnd));
    } else {
      gap = randBetween(isCdn ? L.cdn_gap_ms : L.gap_ms, this.rnd);
      if (page) gap += randBetween(L.page_gap_ms, this.rnd);
    }
    let wait = last + gap - this.now();
    // Layer gap: the first logged-in call waits 20-40 s after the last logged-out call.
    if (!isCdn && !isLo && st.last_lo_at) wait = Math.max(wait, st.last_lo_at + randBetween(L.layer_gap_ms, this.rnd) - this.now());
    if (wait > 0) await this.sleep(wait);
    st = await this.check(kind);  // re-read after the wait (another tab of the SW cannot run, but stay safe)
    const t = this.now();
    if (isCdn) { st.cdn_day_used += 1; st.last_cdn_at = t; }
    else {
      st.calls.push(t); st.day_used += 1; st.last_call_at = t;
      if (kind === "discover") st.disc_day_used += 1;
      if (isLo) { st.lo_calls.push(t); st.lo_day_used += 1; st.last_lo_at = t; }
      else st.last_lo_at = 0;                     // the layer gap applies once, to the first logged-in call
    }
    await this.save(st);
  }
}

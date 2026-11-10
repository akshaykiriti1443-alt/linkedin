/* #competitors, #competitor/<id>, #discover/<run_id> (4.8). */
import {h, clear, msg, say, busy, get, post, patch, del, arr, S, go, field, toast, pollSoon, onJobsDone, every,
  num, full, day, ago, empty, savesLine, pill, jobLine, backLink, checkbox, linkBtn} from "../app.js";
import {signatureCard, dataNote} from "./parts.js";

const STATUS = {new: ["New", ""], scheduled: ["Starts later", "busy"], active: ["Active", "ok"], paused: ["Paused", "warn"],
  not_found: ["Not found", "bad"], private: ["Private: we never read it", "bad"], error: ["Problem", "bad"]};
const STOP = {target_reached: "Read 100 reels", no_more: "Reached their oldest reel", known_reel: "Caught up", page_cap: "Stopped at the page limit",
  rate_limited: "Instagram asked us to slow down", signed_out: "Chrome was signed out of Instagram", challenge: "Instagram asked for a check",
  error: "Something went wrong", canceled: "Stopped"};

// "Reading now" only while Chrome really reads; otherwise say what it waits for (fix round 1).
function pullPill(a, ext) {
  if (!a.pulling) return null;
  const st = (a.pull_job || {}).state;
  if (st === "running") return pill("Reading now", "busy");
  if (ext && (!ext.paired || !ext.online)) return pill("Waiting for Chrome", "warn");
  return pill(a.scheduled_for ? "Starts later" : "Up next", "busy");
}

function accountRow(a, ext) {
  const [st, kind] = STATUS[a.status] || [a.status, ""];
  return h("div", {class: "card", dataset: {account: a.id}},
    h("div", {class: "row"}, h("a", {href: "#competitor/" + a.id, class: "bold"}, "@" + a.handle), a.full_name ? h("span", {class: "muted"}, a.full_name) : null,
      pill(st, kind), pullPill(a, ext)),
    h("p", {class: "small muted"}, [a.followers !== null && a.followers !== undefined ? `${num(a.followers)} followers` : null,
      `${num(a.reels_count || 0)} reels`, `${num(a.outliers || 0)} outliers`, a.baseline_views ? `normal reel ${num(a.baseline_views)} views` : null,
      a.last_pull_at ? `read ${ago(a.last_pull_at)}` : "not read yet"].filter(Boolean).join(" · ")),
    h("div", {class: "row"}, h("a", {class: "btn ghost sm", href: "#reels?account=" + a.id}, "See reels"), h("a", {class: "btn ghost sm", href: "#competitor/" + a.id}, "Details")));
}

export async function list(main, {token}) {
  const r = await get("/api/accounts");
  if (S.view !== token) return;
  if (!r.ok) { clear(main, h("h1", "Competitors"), empty(r.error)); return; }
  const d = r.data;
  const ext = d.extension || {paired: true, online: true};
  const lim = d.limits || {};
  const reload = () => list(main, {token});
  const handles = h("textarea", {id: "add-handles", placeholder: "@alexhormozi\nhttps://www.instagram.com/someone/", rows: 3});
  const out = msg();
  const addBtn = h("button", {type: "button", id: "add-competitors", onclick: async e => {
    const hs = handles.value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    if (!hs.length) { say(out, {error: "Add at least one account."}); return; }
    const res = await busy(e.currentTarget, () => post("/api/accounts", {handles: hs, pull: true}));
    if (!res.ok) { say(out, res); return; }
    handles.value = "";
    toast(res.data.schedule_note || (ext.paired ? "Added. Chrome reads them next." : "Added. Connect Chrome so ReelVault can read them."));
    pollSoon(); reload();
  }}, "Add and pull 100 reels each");
  const selfIn = h("input", {id: "self-handle", placeholder: "@yourname"});
  const dOut = msg();
  const query = h("input", {id: "discover-search", placeholder: "for example: dental marketing"});
  const discover = async (btn, body) => {
    const res = await busy(btn, () => post("/api/discover", body));
    if (!res.ok) { say(dOut, res); return; }
    pollSoon(); go("#discover/" + res.data.run_id);
  };
  const accounts = arr(d.accounts);
  const seeds = accounts.slice(0, 2).map(a => a.handle);
  const waiting = accounts.some(a => a.pulling);
  const chromeBanner = !waiting ? null : !ext.paired
    ? h("div", {class: "banner warn", id: "connect-chrome"}, "Connect Chrome to read these reels. ", h("a", {href: "#settings/extension"}, "Connect Chrome"))
    : !ext.online ? h("div", {class: "banner warn", id: "open-chrome"}, "Chrome is not open. Open Chrome to keep reading. ", h("a", {href: "#settings/extension"}, "Check the extension"))
    : null;
  onJobsDone(reload);
  clear(main, h("div", {class: "view"},
    h("div", {class: "headrow"}, h("h1", "Competitors"), h("span", {class: "muted"}, `${accounts.length} of ${lim.max || 10} · ${lim.pulls_today || 0} of ${lim.pulls_max || 5} pulls today`)),
    chromeBanner,
    accounts.length ? h("div", {class: "stack"}, accounts.map(a => accountRow(a, ext))) :
      empty("No competitors yet. Add 1-2 accounts whose reels work in your niche, and we'll find more."),
    h("section", {class: "panel"}, h("h2", "Add competitors"),
      h("p", {class: "hint"}, "Type @names or paste profile links, one per line. We read 100 reels from each, newest first. Private accounts are never read."),
      field("Instagram accounts", handles), out, h("div", {class: "row"}, addBtn)),
    h("section", {class: "panel"}, h("h2", "Find the top accounts in my niche"),
      h("p", {class: "hint"}, "We search Instagram from your browser, check each account's last 12 reels, and show you the best ones to track."),
      h("div", {class: "row"},
        h("button", {type: "button", id: "discover-niche", onclick: e => discover(e.currentTarget, {mode: "niche"})}, "Find the top accounts in my niche"),
        seeds.length ? h("button", {type: "button", class: "ghost", onclick: e => discover(e.currentTarget, {mode: "similar", seeds})}, "Find accounts like " + seeds.map(s => "@" + s).join(" and ")) : null),
      h("div", {class: "row"}, h("label", {class: "sr", for: "discover-search"}, "Search"), query,
        h("button", {type: "button", class: "ghost", onclick: e => query.value.trim() && discover(e.currentTarget, {mode: "search", query: query.value.trim()})}, "Search")), dOut),
    h("section", {class: "panel"}, h("h2", "Your account"),
      d.self ? accountRow(d.self) : [h("p", {class: "hint"}, "Add your own Instagram to compare yourself with your competitors. It never counts in their numbers."),
        h("div", {class: "row"}, h("label", {class: "sr", for: "self-handle"}, "Your Instagram"), selfIn, h("button", {type: "button", class: "ghost", onclick: async e => {
          if (!selfIn.value.trim()) return;
          const res = await busy(e.currentTarget, () => post("/api/accounts", {handles: [selfIn.value.trim()], pull: true, role: "self"}));
          toast(res.ok ? "Added. Chrome reads it next." : res.error); if (res.ok) reload();
        }}, "Add my account"))]),
    savesLine(d.saves_line)));
}

export async function detail(main, {parts, token}) {
  const id = parts[0];
  const r = await get("/api/accounts/" + encodeURIComponent(id || ""));
  if (S.view !== token) return;
  if (!r.ok) { clear(main, backLink("#competitors", "Competitors"), h("h1", "Competitor"), empty(r.error)); return; }
  const a = r.data.account;
  const reload = () => detail(main, {parts, token});
  const out = msg();
  const pull = async (btn, mode) => {
    const res = await busy(btn, () => post(`/api/accounts/${a.id}/pull`, {mode}));
    if (!res.ok) { say(out, res); return; }
    toast(res.data.job && res.data.job.run_after ? "You've done 5 pulls today. This one starts tomorrow." : "Started. Keep Chrome open.");
    pollSoon(); reload();
  };
  const note = h("textarea", {id: "account-note", rows: 2}); note.value = a.note || "";
  const [delLabel, delReels] = checkbox("Also delete its reels", true);
  // Only THIS account's pull (the member's other pulls belong on their own pages).
  const pullId = (a.pull_job || {}).id;
  const myJobs = pullId ? [arr(S.jobs).find(j => j.id === pullId) || a.pull_job].filter(j => j.state !== "done") : [];
  onJobsDone(reload);
  const [st, kind] = STATUS[a.status] || [a.status, ""];
  clear(main, h("div", {class: "view"},
    backLink("#competitors", "Competitors"),
    h("div", {class: "headrow"}, h("h1", "@" + a.handle), pill(st, kind), a.role === "self" ? pill("Your account", "busy") : null),
    h("section", {class: "panel"},
      a.full_name ? h("p", {class: "bold"}, a.full_name) : null,
      a.bio ? h("p", {class: "muted", style: {whiteSpace: "pre-wrap"}}, a.bio) : null,
      h("div", {class: "nums"},
        h("div", null, h("b", num(a.followers)), "followers"), h("div", null, h("b", num(a.reels_count)), "reels read"),
        h("div", null, h("b", num(a.outliers)), "outliers"), h("div", null, h("b", num(a.baseline_views)), "views on a normal reel")),
      r.data.normal_line ? h("p", r.data.normal_line) : null,
      h("p", null, h("a", {href: a.url || `https://www.instagram.com/${a.handle}/`, target: "_blank", rel: "noopener noreferrer"}, "Open on Instagram")),
      out,
      h("div", {class: "row"},
        h("button", {type: "button", disabled: a.cursor_done || a.pulling, onclick: e => pull(e.currentTarget, "next")}, a.cursor_done ? "You have all their reels" : "Pull the next 100 reels"),
        h("button", {type: "button", class: "ghost", disabled: a.pulling, onclick: e => pull(e.currentTarget, "refresh")}, "Check for new reels"),
        h("a", {class: "btn ghost", href: "#reels?account=" + a.id}, "See reels"),
        h("button", {type: "button", class: "ghost", onclick: async e => {
          const res = await busy(e.currentTarget, () => post("/api/reels/analyze-best", {account_id: a.id, limit: 5}));
          toast(res.ok ? (res.data.count ? `Breaking down ${res.data.count} top reels.` : "The top reels are already broken down.") : res.error); pollSoon();
        }}, "Break down the 5 best reels")),
      myJobs.length ? h("div", {class: "stack"}, myJobs.map(j => jobLine(j, {onChange: reload}))) : null),
    r.data.signature ? h("section", {class: "stack"}, h("h2", "Their signature"), signatureCard(r.data.signature)) : null,
    h("section", {class: "panel"}, h("h2", "Pulls"),
      arr(a.pulls).length ? h("div", {class: "tablewrap"}, h("table", null,
        h("thead", null, h("tr", null, h("th", "Started"), h("th", "Kind"), h("th", {class: "num"}, "New reels"), h("th", "Result"))),
        h("tbody", null, arr(a.pulls).map(p => h("tr", null, h("td", day(p.started_at || p.run_after)),
          h("td", {first: "First 100", next: "Next 100", refresh: "New reels"}[p.mode] || p.mode),
          h("td", {class: "num"}, full(p.reels_new)), h("td", p.finished_at ? (STOP[p.stop_reason] || p.stop_reason || "Done") : (p.run_after ? "Starts " + day(p.run_after) : "Running"))))))) :
        h("p", {class: "muted"}, "No pulls yet.")),
    h("section", {class: "panel"}, h("h2", "Your note"), note, h("div", {class: "row"},
      h("button", {type: "button", class: "ghost", onclick: async e => { const res = await busy(e.currentTarget, () => patch(`/api/accounts/${a.id}`, {note: note.value})); toast(res.ok ? "Saved." : res.error); }}, "Save note"),
      h("button", {type: "button", class: "ghost", onclick: async e => { const res = await busy(e.currentTarget, () => patch(`/api/accounts/${a.id}`, {status: a.status === "paused" ? "active" : "paused"})); toast(res.ok ? "Saved." : res.error); reload(); }}, a.status === "paused" ? "Resume" : "Pause"))),
    dataNote(r.data.data_note),
    h("section", {class: "panel"}, h("h2", "Stop tracking"), delLabel, h("div", {class: "row"}, h("button", {type: "button", class: "danger", onclick: async e => {
      if (!confirm(`Stop tracking @${a.handle}?`)) return;
      const res = await busy(e.currentTarget, () => del(`/api/accounts/${a.id}`, {delete_reels: delReels.checked}));
      if (!res.ok) { toast(res.error); return; }
      toast("Removed."); go("#competitors");
    }}, "Stop tracking @" + a.handle)))));
}

export async function discover(main, {parts, query, token}) {
  const runId = parts[0];
  const [r, ar] = await Promise.all([get("/api/discover/" + encodeURIComponent(runId || "")), get("/api/accounts")]);
  if (S.view !== token) return;
  if (!r.ok) { clear(main, backLink("#competitors", "Competitors"), h("h1", "Competitor"), empty(r.error)); return; }
  const d = r.data;
  const lim = ar.ok ? ar.data.limits || {} : {};
  const have = ar.ok ? arr(ar.data.accounts).length : 0;
  const room = Math.max(0, (lim.max || 10) - have);
  const cands = arr(d.candidates).filter(c => c.status !== "tracked" && c.status !== "dismissed").slice(0, 10);
  const tracked = arr(d.candidates).filter(c => c.status === "tracked");
  const out = msg();
  const boxes = [];
  const count = h("span", {class: "muted"});
  const recount = () => {
    const n = boxes.filter(b => b.checked).length;
    count.textContent = `${n} ticked · room for ${room} more`;
    boxes.forEach(b => { b.disabled = !b.checked && n >= room; });
  };
  const fromOnb = query.get("from") === "onboarding";
  const running = !["done", "failed", "canceled"].includes(d.state);
  clear(main, h("div", {class: "view"},
    backLink(fromOnb ? "#onboarding/2" : "#competitors", fromOnb ? "Setup" : "Competitors"),
    h("h1", "Top accounts we found"),
    running ? h("div", {class: "banner"}, h("span", {class: "row"}, h("span", {class: "spinner", "aria-hidden": "true"}), d.state_text || "Searching from your Chrome…"),
      h("span", {class: "small"}, "Keep Chrome open.")) : null,
    d.state === "failed" ? h("div", {class: "banner bad"}, d.state_text || "The search didn't finish. Try again later.") : null,
    cands.length ? h("p", "Top 10 we found. Tick the ones to track.") : (!running ? empty("We didn't find new accounts this time. Try a search with other words.", linkBtn("#competitors", "Back to competitors")) : null),
    h("div", {class: "stack"}, cands.map(c => {
      const [label, box] = checkbox("Track @" + c.handle, false, {value: c.id, onchange: recount});
      boxes.push(box);
      return h("div", {class: "card", dataset: {cand: c.id}},
        h("div", {class: "row"}, label, c.full_name ? h("span", {class: "muted"}, c.full_name) : null, c.is_verified ? pill("Verified") : null,
          c.not_posting ? pill("Not posting lately", "warn") : null, h("span", {class: "spacer"}),
          h("a", {href: c.url || `https://www.instagram.com/${c.handle}/`, target: "_blank", rel: "noopener noreferrer", class: "small"}, "Open on Instagram")),
        h("p", {class: "small"}, c.why || `${num(c.followers)} followers`),
        c.seed_handle ? h("p", {class: "hint"}, `Similar to @${c.seed_handle}`) : null);
    })),
    tracked.length ? h("p", {class: "muted"}, "Already tracking: " + tracked.map(c => "@" + c.handle).join(", ")) : null,
    cands.length ? h("div", {class: "row"}, count, h("span", {class: "spacer"}), h("button", {type: "button", id: "track-selected", onclick: async e => {
      const ids = boxes.filter(b => b.checked).map(b => b.value);
      if (!ids.length) { say(out, {error: "Tick at least one account."}); return; }
      const res = await busy(e.currentTarget, () => post(`/api/discover/${d.id || runId}/track`, {candidate_ids: ids}));
      if (!res.ok) { say(out, res); return; }
      toast(res.data.schedule_note || "Added. Chrome reads them next.");
      pollSoon(); go(fromOnb ? "#onboarding/3" : "#competitors");
    }}, "Track the ticked accounts")) : null,
    h("p", {class: "hint"}, "We'll read 5 accounts today and 5 tomorrow to keep your Instagram safe."),
    out));
  recount();
  if (running) every(4000, async stop => { const s = await get("/api/discover/" + encodeURIComponent(runId), {quiet: true}); if (s.ok && s.data.state !== d.state) { stop(); discover(main, {parts, query, token}); } });
}

/* The fetch engine screens (DESIGN-FETCH.md section 5):
   - readCard(): the Settings card "How ReelVault reads Instagram" (read mode + one line per layer),
   - readLine() / loadReads(): the grey "how this pull was read" line under a competitor's pull status,
   - testConnection(): the page #test-connection ("Test my connection": Run the test, verdict, table).
   All text comes from the server where the contract says so; everything is set as text, never as HTML. */
import {h, clear, msg, say, busy, get, put, post, arr, S, toast, pill, ago, every, backLink} from "../app.js";

const LAYER_PILL = {ok: ["Works", "ok"], partial: ["Works in part", "warn"], blocked: ["Blocked", "bad"], cooling: ["Waiting", "warn"], off: ["Off", ""],
  unknown: ["Not tried yet", ""]};
const STATUS_KIND = {ok: "ok", partial: "warn", blocked: "bad", error: "bad", skipped: ""};
const VERDICT_KIND = {best: "ok", partial: "warn", wait: "warn", login_needed: "warn", lo_only_blocked: "warn",
  signed_out: "bad", nothing: "bad", no_ext: "bad", update: "bad"};
const GROUPS = [["logged_out", "Without your login"], ["logged_in", "With your Instagram login"]];
export const TEST_LINE = "This test makes about 11 slow requests. It takes 2 minutes.";

/** "Waiting until 14:05" for a cooldown time, or "" when there is none. */
function untilText(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  if (isNaN(t)) return "";
  const same = t.toDateString() === new Date().toDateString();
  const time = t.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  return same ? `It goes on by itself at ${time}.` : `It goes on by itself on ${t.toLocaleDateString("en-GB", {day: "numeric", month: "short"})} at ${time}.`;
}

// ============================================================================================ Settings card (5.1)
/** Draws the card into box. Returns false when the server has no fetch engine (the card then stays hidden). */
export async function readCard(box) {
  const [s, st] = await Promise.all([get("/api/fetch/settings", {quiet: true}), get("/api/fetch/status", {quiet: true})]);
  if (s.status === 404) { clear(box); box.hidden = true; return false; }
  box.hidden = false;
  if (!s.ok) { clear(box, h("section", {class: "panel", id: "fetch-card"}, h("h2", "How ReelVault reads Instagram"), h("p", {class: "msg err"}, s.error))); return true; }
  const d = s.data;
  let chosen = d.read_mode || d.default || "auto";
  const out = msg();
  const radios = arr(d.options).map(o => {
    const input = h("input", {type: "radio", name: "read-mode", value: o.id, id: "read-" + o.id, checked: o.id === chosen,
      onchange: () => { chosen = o.id; }});
    return h("label", {class: "radio-row", for: "read-" + o.id},
      input,
      h("span", {class: "stack tight"},
        h("span", {class: "row"}, h("b", o.label), o.recommended ? pill("Recommended", "ok") : null),
        h("span", {class: "hint"}, o.text)));
  });
  const layers = h("div", {class: "stack tight", id: "fetch-layers"});
  const drawStatus = data => {
    if (!data) { clear(layers, h("p", {class: "hint"}, "ReelVault can't show the status right now.")); return; }
    clear(layers,
      arr(data.layers).map(l => {
        const [label, kind] = LAYER_PILL[l.state] || [l.state, ""];
        return h("div", {class: "layer", dataset: {layer: l.id, state: l.state}},
          h("span", {class: "lamp " + (kind || "off"), "aria-hidden": "true"}),
          h("div", {class: "t"},
            h("div", {class: "row"}, h("b", l.label), pill(label, kind)),
            h("p", {class: "small muted"}, [l.detail, l.state === "cooling" ? untilText(l.cooldown_until) : ""].filter(Boolean).join(" "))));
      }),
      data.last_probe && data.last_probe.verdict ? h("p", {class: "small", id: "last-test"},
        `Last test ${ago(data.last_probe.at)}: ${data.last_probe.verdict.title || ""}`) :
        h("p", {class: "small muted", id: "last-test"}, "You have not run the test yet."));
  };
  drawStatus(st.ok ? st.data : null);
  clear(box, h("section", {class: "panel", id: "fetch-card"},
    h("h2", "How ReelVault reads Instagram"),
    h("p", {class: "hint"}, "ReelVault can read public reels without your Instagram login. Pick how it reads."),
    h("fieldset", {class: "radios"}, h("legend", {class: "sr"}, "How ReelVault reads Instagram"), radios),
    out,
    h("div", {class: "row"}, h("button", {type: "button", id: "fetch-save", onclick: async e => {
      const r = await busy(e.currentTarget, () => put("/api/fetch/settings", {read_mode: chosen}));
      say(out, r, "Saved. The next pull reads this way.");
      if (r.ok) { const n = await get("/api/fetch/status", {quiet: true}); drawStatus(n.ok ? n.data : null); }
    }}, "Save")),
    h("div", {class: "divider"}),
    h("h3", "What works right now"),
    layers,
    h("div", {class: "row"}, h("a", {class: "btn ghost", href: "#test-connection", id: "test-connection-link"}, "Test my connection"))));
  return true;
}

// ============================================================================================ pull line (5.2)
/** {account_id: newest pull read info} from GET /api/fetch/pulls. Empty when the server has no fetch engine. */
export async function loadReads(accountId) {
  const r = await get("/api/fetch/pulls" + (accountId ? `?account_id=${encodeURIComponent(accountId)}&limit=50` : "?limit=50"), {quiet: true});
  const byAccount = {}, byPull = {};
  if (!r.ok || !r.data) return {byAccount, byPull};
  for (const p of arr(r.data.pulls)) {
    if (!p || !p.read || !p.read.message) continue;
    byPull[p.id] = p;
    if (!byAccount[p.account_id]) byAccount[p.account_id] = p;
  }
  return {byAccount, byPull};
}
/** One grey line: "Read 24 reels without your login. Your login filled in the other 76." */
export function readLine(p) {
  if (!p || !p.read || !p.read.message) return null;
  return h("p", {class: "small muted readline", dataset: {layer: p.read.layer || "", pull: p.id || ""}}, p.read.message);
}

// ============================================================================================ #test-connection (5.3)
export async function testConnection(main, {token}) {
  const out = msg();
  const verdictBox = h("div", {id: "verdict"});
  const statusBox = h("div", {class: "stack tight", id: "probe-status"});
  const tableBox = h("div", {id: "probe-rows"});
  let stop = () => {};
  let blocked = false;          // no extension (or too old): the test can't run, the box above says why
  const runBtn = h("button", {type: "button", id: "run-test", class: "big", onclick: async e => {
    const r = await busy(e.currentTarget, () => post("/api/fetch/probe", {}));
    if (!r.ok) { say(out, r); return; }
    say(out, {ok: true}, "");
    toast("The test starts. Keep Chrome open.");
    draw(r.data);
    watch(r.data.probe_id);
  }}, "Run the test");
  const draw = p => {
    const active = p && (p.state === "waiting" || p.state === "running");
    runBtn.disabled = !!active || blocked;
    runBtn.textContent = active ? "The test is running" : p ? "Run the test again" : "Run the test";
    if (!blocked) drawVerdict(verdictBox, p);
    drawStatusLine(statusBox, p);
    drawRows(tableBox, p);
  };
  const watch = id => {
    stop();
    stop = every(3000, async halt => {
      const r = await get("/api/fetch/probe/" + encodeURIComponent(id), {quiet: true});
      if (!r.ok) { if (r.status === 404) halt(); return; }
      draw(r.data);
      if (r.data.state === "done" || r.data.state === "failed") {
        halt();
        toast(r.data.state === "done" ? "The test is done." : "The test did not finish.");
      }
    });
  };
  clear(main, h("div", {class: "view"},
    backLink("#settings/extension", "Chrome extension"),
    h("h1", "Test my connection"),
    h("p", {class: "muted"}, "This test checks which ways of reading Instagram work on this computer. It runs in your Chrome, on your own internet. It only reads public pages. It never posts, likes or follows."),
    verdictBox,
    h("section", {class: "panel"},
      h("div", {class: "row"}, runBtn),
      h("p", {class: "hint", id: "test-line"}, TEST_LINE),
      out, statusBox),
    tableBox,
    h("details", {class: "panel", id: "verdict-help"}, h("summary", "What the result means"),
      h("ul", {class: "stack tight"},
        h("li", h("b", "The safest way works. "), "ReelVault reads reels without your login. You don't need to do anything."),
        h("li", h("b", "The safest way works in part. "), "ReelVault reads some reels without your login. With \"Safest first\", your login fills in view counts."),
        h("li", h("b", "Instagram asks ReelVault to wait. "), "ReelVault goes on by itself. Run the test again in a few hours."),
        h("li", h("b", "Instagram asks for a login here. "), "ReelVault reads with your Instagram login. It stays slow and careful."),
        h("li", h("b", "Instagram asks for a login here. You chose to read without it. "), "Try again later, or pick Safest first in Settings."),
        h("li", h("b", "You're signed out of Instagram in Chrome. "), "Sign in at instagram.com in Chrome. Then run the test again."),
        h("li", h("b", "Instagram blocks reads right now. "), "ReelVault waits by itself. Run the test again in a few hours.")),
      h("p", {class: "hint"}, "Without your login, Instagram shows less. View counts are often missing. Your login fills them in. Your saved reels stay private either way."))));
  const [r, st] = await Promise.all([get("/api/fetch/probe/latest", {quiet: true}), get("/api/fetch/status", {quiet: true})]);
  if (S.view !== token) return;
  const dev = st.ok && st.data ? st.data.device || {} : {};
  if (st.ok && (!dev.connected || dev.needs_update)) {
    blocked = true;
    const v = dev.connected
      ? {code: "update", title: "Update the Chrome extension first.", text: "This version can't run the test.", btn: "Get the update"}
      : {code: "no_ext", title: "Connect the Chrome extension first.", text: "The test runs in your Chrome. Connect it, then come back here.", btn: "Connect Chrome"};
    clear(verdictBox, h("section", {class: "verdict bad", dataset: {verdict: v.code}, role: "status"},
      h("h2", v.title), h("p", v.text),
      h("div", {class: "row"}, h("a", {class: "btn", href: "#settings/extension", id: "connect-chrome"}, v.btn))));
  }
  if (r.ok) {
    draw(r.data);
    if (r.data.state === "waiting" || r.data.state === "running") watch(r.data.probe_id);
  } else {
    draw(null);
    if (r.status !== 404) say(out, r);
  }
}

function drawVerdict(box, p) {
  const v = p && p.verdict;
  if (!v || !v.code) { clear(box); return; }
  const kind = VERDICT_KIND[v.code] || "warn";
  clear(box, h("section", {class: "verdict " + kind, dataset: {verdict: v.code}, role: "status"},
    h("h2", v.title || ""),
    v.text ? h("p", v.text) : null,
    v.next ? h("p", {class: "bold"}, v.next) : null,
    p.finished_at ? h("p", {class: "small muted"}, "Tested " + ago(p.finished_at) + ".") : null));
}

function drawStatusLine(box, p) {
  if (!p) { clear(box, h("p", {class: "small muted"}, "You have not run the test yet.")); return; }
  const g = p.progress || {};
  const pct = g.total ? Math.min(100, Math.round(100 * (g.done || 0) / g.total)) : 0;
  if (p.state === "waiting") {
    clear(box, h("p", {class: "row"}, h("span", {class: "spinner", "aria-hidden": "true"}), p.message || "Waiting for Chrome. The test starts within 30 seconds."));
  } else if (p.state === "running") {
    clear(box, h("p", {class: "row"}, h("span", {class: "spinner", "aria-hidden": "true"}), `Testing: ${g.done || 0} of ${g.total || 11} done.`),
      h("div", {class: "progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100}, h("i", {style: {width: pct + "%"}})));
  } else if (p.state === "failed") {
    clear(box, h("p", {class: "msg err"}, p.message || "The test did not finish. Open Chrome, then run it again."));
  } else {
    clear(box);
  }
}

function secs(ms) {
  if (ms === null || ms === undefined || ms === "") return "—";
  const v = Number(ms);
  if (!isFinite(v)) return "—";
  return v < 100 ? "under 0.1 s" : `${Math.round(v / 100) / 10} s`;
}

// A row that sent no request (skipped, or no time and no answer) has no time to show.
function ranNot(r) {
  return r.status === "skipped" || (!Number(r.ms) && !Number(r.http));
}

function drawRows(box, p) {
  const rows = p ? arr(p.rows) : [];
  if (!rows.length) { clear(box); return; }
  const body = [];
  for (const [layer, title] of GROUPS) {
    const mine = rows.filter(r => (r.layer || "logged_out") === layer);
    if (!mine.length) continue;
    body.push(h("tr", {class: "group"}, h("th", {colspan: 5, scope: "colgroup"}, title)));
    for (const r of mine) {
      body.push(h("tr", {dataset: {strategy: r.strategy || "", status: r.status || ""}},
        h("td", {"data-label": "Tried"}, h("b", r.label || r.strategy || "—")),
        h("td", {"data-label": "Account"}, r.handle ? "@" + r.handle : "—"),
        h("td", {"data-label": "Result"}, h("div", {class: "stack tight"}, pill(r.status_label || r.status || "—", STATUS_KIND[r.status] ?? ""),
          r.note ? h("span", {class: "small muted"}, r.note) : null)),
        h("td", {"data-label": "Gives"}, r.gives || "—"),
        h("td", {class: "num", "data-label": "Time"}, ranNot(r) ? "—" : secs(r.ms))));
    }
  }
  clear(box, h("section", {class: "panel"}, h("h2", "What ReelVault tried"),
    h("div", {class: "tablewrap"}, h("table", {class: "probe"},
      h("thead", null, h("tr", null, h("th", "What ReelVault tried"), h("th", "Account"), h("th", "Result"), h("th", "What it gives"), h("th", {class: "num"}, "Time"))),
      h("tbody", null, body)))));
}

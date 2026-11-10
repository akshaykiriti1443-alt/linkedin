/* #create (four big buttons + short forms), #outputs (purpose filters), #output/<id> (live state, cards, downloads) (4.10, 8). */
import {h, clear, msg, say, busy, get, post, patch, del, arr, S, go, field, select, checkbox, toast, pollSoon, onJobsDone,
  every, num, x, mmss, nice, day, empty, pill, vfTag, buildOn, quick, backLink, qs, copyBtn, copyLine, jobLine,
  PURPOSES, PURPOSE_LABEL, KINDS, KIND_LABEL, sessionStore, linkBtn} from "../app.js";
// Download labels say what the file really is for each kind (fix round 1).
const DL = {reel_script: ["Editor brief (.txt)", "Shot list (.csv)"], meta_ads: ["Ad scripts (.txt)", "Shot list (.csv)"],
  content_plan: ["Plan (.txt)", "Plan (.csv)"], competitor_report: ["Report (.txt)", "Reels to remake (.csv)"]};

const GOALS = [["", "Any goal"], ["leads", "Get leads"], ["followers", "Get followers"], ["sales", "Make sales"], ["community", "Grow my community"]];

// ------------------------------------------------------------------------------------------------ #create
export async function create(main, {parts, token}) {
  const kind = KINDS.some(k => k[0] === parts[0]) ? parts[0] : "";
  const [pr, ar] = await Promise.all([get("/api/profile"), get("/api/accounts")]);
  if (S.view !== token) return;
  const prof = pr.ok ? pr.data : null;
  const approved = prof && prof.status === "approved";
  const view = h("div", {class: "view"}, h("h1", "Create"));
  if (!approved) view.append(h("div", {class: "banner warn"}, h("span", "Approve your profile first. Every script is written for your customer."), linkBtn("#settings/profile", "Finish my profile", "btn sm")));
  const BIG = [["reel_script", "Reel scripts", "Scripts in a format that works, with an editor brief and a sound cue sheet."],
    ["meta_ads", "Meta ads", "15 s and 30 s ad scripts, plus a 5-hook test."], ["content_plan", "30-day plan", "What to post every day for a month."],
    ["competitor_report", "Competitor report", "What works for them, and the reels to remake first."]];
  view.append(h("div", {class: "bigbtns"}, BIG.map(([id, title, sub]) => h("a", {class: "choice", href: "#create/" + id, "aria-pressed": String(kind === id), id: "create-" + id},
    h("b", title), h("span", {class: "small"}, sub)))));
  if (kind) view.append(form(kind, prof, ar.ok ? arr(ar.data.accounts) : []));
  clear(main, view);
}

function form(kind, prof, accounts) {
  const pains = arr(((prof || {}).digest || {}).top_pains);
  const out = msg();
  const fields = [];
  const get_ = {};
  const add = (key, label, el, hint) => { fields.push(field(label, el, hint)); get_[key] = el; };
  const forSel = select([["me", "Me"]], "me", {id: "out-for"});
  add("brand_id", "For", forSel, "Client brands come later.");
  if (kind === "reel_script" || kind === "meta_ads") {
    add("count", kind === "reel_script" ? "How many scripts" : "How many ad sets", select((kind === "reel_script" ? [1, 2, 3, 4, 5] : [1, 2, 3]).map(n => [n, String(n)]), kind === "reel_script" ? 3 : 1, {id: "out-count"}));
    add("pain", "Which customer problem", select([["", "Rotate through them"], ...pains.map(p => [p, p])], "", {id: "out-pain"}));
    add("goal", "Goal", select(GOALS, "", {id: "out-goal"}));
  }
  if (kind === "reel_script") {
    add("purpose", "What to build on", select([["", "Pick for me"], ...PURPOSES.filter(p => p[0] !== "learn")], "", {id: "out-purpose"}));
    add("topic", "Topic (optional)", h("input", {id: "out-topic", maxlength: 500, placeholder: "Why your Meta ads stop working after 2 weeks"}));
    add("length", "Length", select([["", "Like the source reels"], ["15", "15 seconds"], ["30", "30 seconds"], ["45", "45 seconds"], ["60", "60 seconds"]], "", {id: "out-length"}));
  }
  if (kind === "meta_ads") add("offer", "What the ad sells", h("input", {id: "out-offer", placeholder: "free audit"}));
  if (kind === "content_plan") {
    const d = new Date(); d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
    add("start_date", "Start on", h("input", {type: "date", id: "out-start", value: d.toISOString().slice(0, 10)}));
    add("posts_per_week", "Reels a week", select([["", "Pick from my hours"], ...[2, 3, 4, 5, 6, 7].map(n => [n, String(n)])], "", {id: "out-ppw"}));
    const [l, i] = checkbox("Add carousels, stories and lives too", false, {id: "out-other"});
    fields.push(l); get_.include_other_types = i;
  }
  let accBoxes = [];
  if (kind === "competitor_report" && accounts.length) {
    accBoxes = accounts.map(a => checkbox("@" + a.handle, true, {value: a.id}));
    fields.push(h("div", {class: "stack tight"}, h("span", {class: "label"}, "Competitors"), h("div", {class: "checks"}, accBoxes.map(b => b[0]))));
  }
  add("notes", "Anything else? (optional)", h("textarea", {id: "out-notes", maxlength: 1000, rows: 3}));
  const label = {reel_script: "Write the scripts", meta_ads: "Write the ads", content_plan: "Make my plan", competitor_report: "Write the report"}[kind];
  return h("section", {class: "panel", id: "create-form"}, h("h2", KIND_LABEL[kind]), fields, out,
    h("div", {class: "row"}, h("button", {type: "button", class: "big", id: "out-submit", onclick: async e => {
      const v = k => get_[k] ? (get_[k].type === "checkbox" ? get_[k].checked : get_[k].value) : undefined;
      const options = {};
      for (const k of ["count", "pain", "goal", "topic", "length", "offer", "start_date", "posts_per_week", "include_other_types", "notes"]) {
        const val = v(k);
        if (val === undefined || val === "" || val === null) continue;
        options[k] = ["count", "posts_per_week"].includes(k) ? Number(val) : val;
      }
      if (kind === "reel_script") options.based_on = {auto: true};
      if (accBoxes.length) { const ids = accBoxes.filter(b => b[1].checked).map(b => b[1].value); if (ids.length && ids.length < accBoxes.length) options.accounts = ids; }
      const body = {kind, brand_id: v("brand_id") || "me", options};
      if (v("purpose")) body.purpose = v("purpose");
      const r = await busy(e.currentTarget, () => post("/api/outputs", body));
      if (!r.ok) { say(out, r); return; }
      if (r.data.claude_prompt) sessionStore("rv.claude_prompt." + r.data.output.id, r.data.claude_prompt);
      pollSoon(); go("#output/" + r.data.output.id);
    }}, label)));
}

// ------------------------------------------------------------------------------------------------ #outputs
export async function list(main, {query, token}) {
  const kind = query.get("kind") || "";
  const purpose = query.get("purpose") || "";
  const r = await get("/api/outputs" + qs({kind, limit: 100}));
  if (S.view !== token) return;
  const view = h("div", {class: "view"}, h("div", {class: "headrow"}, h("h1", "Outputs"), h("a", {class: "btn", href: "#create"}, "Create something")));
  const kindSel = select([["", "Everything"], ...KINDS], kind, {"aria-label": "Kind", onchange: e => go("#outputs" + qs({kind: e.target.value, purpose}))});
  view.append(h("div", {class: "filters"}, kindSel, h("div", {class: "chips", role: "group", "aria-label": "Purpose"}, PURPOSES.map(([id, labelP]) =>
    h("button", {type: "button", class: "pchip p-" + id, "aria-pressed": String(purpose === id), onclick: () => go("#outputs" + qs({kind, purpose: purpose === id ? "" : id}))}, labelP)))));
  if (!r.ok) { view.append(empty(r.error)); clear(main, view); return; }
  let rows = arr(r.data.outputs);
  if (purpose) rows = rows.filter(o => o.purpose === purpose);
  if (!rows.length) view.append(empty(kind || purpose ? "Nothing here with these filters." : "No outputs yet. Click Remake this on any outlier, or create something.", linkBtn("#create", "Create something")));
  else view.append(h("section", {class: "panel olist"}, rows.map(o => h("div", {class: "item"},
    h("div", {class: "stack tight"}, h("a", {class: "t", href: "#output/" + o.id}, (o.starred ? "★ " : "") + (o.title || KIND_LABEL[o.kind])),
      h("div", {class: "row"}, h("span", {class: "small muted"}, (o.kind_label || KIND_LABEL[o.kind]) + " · " + day(o.created_at)), o.purpose ? h("span", {class: "pchip p-" + o.purpose, "aria-pressed": "true"}, PURPOSE_LABEL[o.purpose]) : null)),
    statusPill(o)))));
  onJobsDone(() => list(main, {query, token}));
  clear(main, view);
}
function statusPill(o) {
  const st = o.status;
  if (st === "done") return pill("Ready", "ok");
  if (st === "failed") return pill("Didn't work", "bad");
  if (st === "waiting") return pill((o.job || {}).executor === "mcp" ? "Waiting for Claude" : (o.job || {}).executor === "desktop" ? "Waiting for the desktop app" : "Waiting", "warn");
  return pill("Writing", "busy");
}

// ------------------------------------------------------------------------------------------------ #output/<id>
export async function detail(main, {parts, token}) {
  const id = parts[0];
  const r = await get("/api/outputs/" + encodeURIComponent(id || ""));
  if (S.view !== token) return;
  if (!r.ok) { clear(main, backLink("#outputs", "Outputs"), h("h1", "Output"), empty(r.error)); return; }
  const o = r.data.output;
  const reload = () => detail(main, {parts, token});
  const view = h("div", {class: "view"}, backLink("#outputs", "Outputs"));
  const titleIn = h("input", {value: o.title || "", "aria-label": "Title", id: "out-title"});
  const headRow = h("div", {class: "headrow"}, h("h1", o.title || KIND_LABEL[o.kind]), statusPill(o));
  view.append(headRow, h("p", {class: "muted"}, `${o.kind_label || KIND_LABEL[o.kind]} · ${day(o.created_at)}` + (o.purpose ? ` · ${PURPOSE_LABEL[o.purpose]}` : "") +
    ((o.made_by || {}).model ? ` · written by ${o.made_by.model}` : "")));
  const job = o.job || {};
  if (o.status !== "done" && o.status !== "failed") {
    const prompt = sessionStore("rv.claude_prompt." + o.id) || `Write my ReelVault output ${o.id}`;
    if (job.executor === "mcp" || o.status === "waiting" && (job.executor === "mcp")) {
      view.append(h("div", {class: "wait", id: "waiting-claude"}, h("h2", "Waiting for Claude"),
        h("p", "Open Claude and paste this. Your Claude writes it and this page fills in by itself."), copyLine(prompt, "Copy the prompt"),
        h("p", {class: "hint"}, "No Claude connector yet? ", h("a", {href: "#settings/ai"}, "Connect it in Settings > AI"), ".")));
    } else if (job.executor === "desktop") {
      view.append(h("div", {class: "wait"}, h("h2", "Waiting for the desktop app"), h("p", "Open the ReelVault desktop app on your computer. It writes this while it runs.")));
    } else {
      view.append(h("div", {class: "wait"}, h("p", {class: "row"}, h("span", {class: "spinner", "aria-hidden": "true"}), "Writing it now. This page fills in by itself.")));
    }
    if (o.job) view.append(h("section", {class: "panel"}, jobLine(o.job, {onChange: reload})));
    every(3000, async stop => { const s = await get("/api/outputs/" + encodeURIComponent(o.id), {quiet: true}); if (s.ok && s.data.output.status !== o.status) { stop(); reload(); } });
  } else if (o.status === "failed") {
    view.append(h("div", {class: "banner bad"}, h("span", (job.error_message) || "This one didn't work."), h("button", {type: "button", class: "sm", onclick: e => redo(e.currentTarget, o, "")}, "Try again")));
  }
  if (o.status === "done" && o.data) {
    const notes = h("input", {id: "redo-notes", placeholder: "Make it shorter", "aria-label": "What to change"});
    view.append(h("section", {class: "panel noprint"},
      h("div", {class: "row"},
        h("span", {class: "label"}, "Download"),
        ...[["txt", (DL[o.kind] || DL.reel_script)[0]], ["csv", (DL[o.kind] || DL.reel_script)[1]], ["md", "Markdown (.md)"], ["print", "Print or save as PDF"]].map(([f, t]) =>
          h("a", {class: "btn ghost sm", href: `/api/outputs/${o.id}/download?format=${f}`, target: f === "print" ? "_blank" : null, rel: f === "print" ? "noopener" : null, download: f === "print" ? null : true}, t))),
      h("div", {class: "row"},
        h("button", {type: "button", class: "ghost sm", "aria-pressed": String(!!o.starred), onclick: async e => {
          const res = await busy(e.currentTarget, () => patch(`/api/outputs/${o.id}`, {starred: !o.starred}));
          if (res.ok) { o.starred = !o.starred; e.currentTarget.textContent = o.starred ? "★ Starred" : "Star it"; } else toast(res.error);
        }}, o.starred ? "★ Starred" : "Star it"),
        h("details", {class: "fold"}, h("summary", "Rename"), h("div", {class: "row"}, titleIn, h("button", {type: "button", class: "ghost sm", onclick: async e => {
          const res = await busy(e.currentTarget, () => patch(`/api/outputs/${o.id}`, {title: titleIn.value.trim()}));
          toast(res.ok ? "Saved." : res.error); if (res.ok) headRow.querySelector("h1").textContent = titleIn.value.trim();
        }}, "Save")))),
      h("div", {class: "row"}, notes, h("button", {type: "button", class: "ghost sm", onclick: e => redo(e.currentTarget, o, notes.value.trim())}, "Write it again with this change"))));
    const nums = o.numbers || {};
    const render = {reel_script: renderScripts, meta_ads: renderAds, content_plan: renderPlan, competitor_report: renderReport}[o.kind];
    view.append(render ? render(o.data, nums, o) : h("pre", JSON.stringify(o.data, null, 2)));
  }
  view.append(h("div", {class: "row noprint"}, h("span", {class: "spacer"}), h("button", {type: "button", class: "danger sm", onclick: async e => {
    if (!confirm("Delete this output?")) return;
    const res = await busy(e.currentTarget, () => del(`/api/outputs/${o.id}`));
    if (res.ok) { toast("Deleted."); go("#outputs"); } else toast(res.error);
  }}, "Delete")));
  onJobsDone(() => { if (o.status !== "done") reload(); });
  clear(main, view);
}

async function redo(btn, o, notes) {
  const r = await busy(btn, () => post(`/api/outputs/${o.id}/redo`, notes ? {notes} : {}));
  if (!r.ok) { toast(r.error); return; }
  if (r.data.claude_prompt) sessionStore("rv.claude_prompt." + r.data.output.id, r.data.claude_prompt);
  pollSoon(); go("#output/" + r.data.output.id);
}

// ------------------------------------------------------------------------------------------------ renderers
const reelLink = (nums, id, text) => {
  const rr = (nums.reels || {})[id];
  return h("a", {href: "#reel/" + id}, text || (rr ? `@${rr.handle} (${x(rr.score)})` : "the source reel"));
};
const beats = list => h("div", {class: "beats"}, arr(list).map(b => h("div", {class: "beat"}, h("span", {class: "ts"}, `${mmss(b.t_start)}-${mmss(b.t_end)}`),
  h("div", null, h("span", b.say), b.on_screen_text ? h("span", {class: "v"}, "On screen: " + b.on_screen_text) : null, b.visual ? h("span", {class: "v"}, "We see: " + b.visual) : null))));
const ul = (title, items) => arr(items).length ? h("div", {class: "blk"}, h("span", {class: "label"}, title), h("ul", arr(items).map(i => h("li", typeof i === "string" ? i : JSON.stringify(i))))) : null;
const refLink = rf => rf && rf.url ? h("a", {href: rf.url, target: "_blank", rel: "noopener noreferrer"}, `${rf.start || "0:00"}-${rf.end || ""}`) : null;

function scriptText(s) {
  return [s.title, "", ...arr(s.beats).map(b => `[${mmss(b.t_start)}-${mmss(b.t_end)}] ${b.say}` + (b.on_screen_text ? `  (on screen: ${b.on_screen_text})` : "")),
    "", s.cta && s.cta.text ? "CTA: " + s.cta.text : "", "", s.caption || "", arr(s.hashtags).join(" ")].join("\n").trim();
}

function renderScripts(data, nums) {
  return h("div", {class: "stack"}, arr(data.scripts).map((s, n) => {
    const eb = s.editor_brief || {};
    const mu = eb.music || {};
    return h("article", {class: "ocard", dataset: {script: n}},
      h("div", {class: "row"}, h("span", {class: "label"}, `Script ${n + 1}`), vfTag(s.format), s.length_s ? pill(`${s.length_s} s`) : null, h("span", {class: "spacer"}), copyBtn(scriptText(s), "Copy the script")),
      h("h2", s.title),
      h("p", {class: "muted"}, [s.angle, s.pain ? "For: " + s.pain : null].filter(Boolean).join(" · ")),
      arr(s.based_on).length ? h("p", {class: "small"}, "Built on ", arr(s.based_on).map((id, i) => [i ? ", " : "", reelLink(nums, id)])) : null,
      s.hook ? h("div", {class: "part hook"}, h("span", {class: "label"}, "Hook"), h("p", {class: "quote"}, s.hook.spoken),
        s.hook.on_screen_text ? h("p", {class: "small"}, "On screen: " + s.hook.on_screen_text) : null, s.hook.visual ? h("p", {class: "small muted"}, "We see: " + s.hook.visual) : null) : null,
      h("div", {class: "part meat"}, h("span", {class: "label"}, "Script"), beats(s.beats)),
      s.cta ? h("div", {class: "part cta"}, h("span", {class: "label"}, "Call to action"), h("p", s.cta.text), s.cta.keyword ? h("p", {class: "small"}, "Keyword: " + s.cta.keyword) : null) : null,
      s.caption ? h("div", {class: "blk"}, h("span", {class: "label"}, "Caption"), h("div", {class: "copyline"}, h("span", {style: {whiteSpace: "pre-wrap"}}, s.caption + (arr(s.hashtags).length ? "\n" + arr(s.hashtags).join(" ") : "")), copyBtn(s.caption + (arr(s.hashtags).length ? "\n" + arr(s.hashtags).join(" ") : "")))) : null,
      arr(s.upgrades).length ? h("div", {class: "blk"}, h("span", {class: "label"}, "Better than the original"), h("ul", arr(s.upgrades).map(u => h("li", h("b", u.change), u.why ? " — " + u.why : "")))) : null,
      h("details", {class: "fold"}, h("summary", "Editor brief"), h("div", {class: "stack"},
        h("dl", {class: "kv"}, h("dt", "Format"), h("dd", eb.format_recipe || eb.format || "—"), h("dt", "Size and length"), h("dd", `${eb.aspect || "9:16"} · ${eb.length_s || s.length_s || "?"} s`),
          h("dt", "Pace"), h("dd", eb.pace || "—"), h("dt", "Captions"), h("dd", [nice(eb.caption_style), eb.caption_look].filter(Boolean).join(" · ") || "—"),
          h("dt", "Look"), h("dd", eb.color_look || "—"), h("dt", "Music"), h("dd", [nice(mu.type), mu.mood, mu.bpm_range ? mu.bpm_range + " bpm" : "", mu.level].filter(Boolean).join(" · ") || "—"),
          arr(mu.options).length ? [h("dt", "Tracks"), h("dd", arr(mu.options).join(", "))] : null),
        ul("B-roll to find or film", eb.broll), ul("Graphics", eb.graphics), ul("Proof to show", eb.proof_inserts), ul("Do", eb.dos), ul("Don't", eb.donts),
        arr(eb.references).length ? h("div", {class: "blk"}, h("span", {class: "label"}, "Watch these moments"), h("ul", arr(eb.references).map(rf => h("li", refLink(rf), " ", rf.watch_for || "")))) : null)),
      arr(s.sound_cue_sheet).length ? h("details", {class: "fold"}, h("summary", "Sound cue sheet"), h("div", {class: "tablewrap"}, h("table", null,
        h("thead", null, h("tr", null, h("th", "When"), h("th", "Sound"), h("th", "Why"), h("th", "Level"), h("th", "Where to get it"), h("th", "Example"))),
        h("tbody", null, arr(s.sound_cue_sheet).map(c => h("tr", null, h("td", c.t + (c.until ? "-" + c.until : "")), h("td", c.what), h("td", c.purpose), h("td", nice(c.level)), h("td", c.source_hint), h("td", refLink(c.ref) || "—"))))))) : null,
      ul("Why it should work", s.why_it_should_work),
      arr(s.too_close).length ? h("div", {class: "banner warn"}, h("div", null, h("b", "Too close to the original: "), arr(s.too_close).map(t => `${t.line} (${t.why})`).join("; "))) : null);
  }));
}

function renderAds(data, nums) {
  const adPart = (title, ad) => ad ? h("div", {class: "part meat"}, h("span", {class: "label"}, title), beats(ad.beats), ad.cta ? h("p", {class: "small"}, h("b", "CTA: "), ad.cta) : null, ad.editor_notes ? h("p", {class: "small muted"}, ad.editor_notes) : null) : null;
  return h("div", {class: "stack"}, arr(data.ads).map((a, n) => h("article", {class: "ocard"},
    h("div", {class: "row"}, h("span", {class: "label"}, `Ad set ${n + 1}`), a.awareness ? pill(nice(a.awareness)) : null),
    h("h2", a.angle || "Ad"), a.offer ? h("p", {class: "muted"}, "Offer: " + a.offer) : null,
    adPart("15-second ad", a.ad_15), adPart("30-second ad", a.ad_30),
    a.hook_test ? h("div", {class: "part hook"}, h("span", {class: "label"}, "5-hook test"), h("ol", arr(a.hook_test.hooks).map(hk => h("li", h("b", hk.spoken), hk.on_screen_text ? ` · on screen: ${hk.on_screen_text}` : "", hk.first_frame ? ` · first frame: ${hk.first_frame}` : ""))),
      ul("How to set it up", a.hook_test.setup), ul("How to read the results", a.hook_test.read_results)) : null,
    h("div", {class: "stack tight"}, h("span", {class: "label"}, "Ad text"),
      a.primary_text ? h("div", {class: "copyline"}, h("span", a.primary_text), copyBtn(a.primary_text)) : null,
      a.headline ? h("div", {class: "copyline"}, h("b", a.headline), copyBtn(a.headline)) : null,
      a.description ? h("div", {class: "copyline"}, h("span", a.description), copyBtn(a.description)) : null,
      a.cta_button ? h("p", {class: "small"}, "Button: " + nice(String(a.cta_button).toLowerCase())) : null),
    arr(a.based_on).length ? h("p", {class: "small"}, "Built on ", arr(a.based_on).map((id, i) => [i ? ", " : "", reelLink(nums, id)])) : null,
    arr(a.policy_notes).length ? h("div", {class: "banner warn"}, h("div", null, h("b", "Meta ad rules: "), arr(a.policy_notes).join(" "))) : null)));
}

function renderPlan(data) {
  return h("div", {class: "stack"},
    arr(data.pillars).length ? h("article", {class: "ocard"}, h("h2", "Your content pillars"), h("ul", arr(data.pillars).map(p => h("li", h("b", `${p.label} (${p.share_pct}%)`), p.why ? " — " + p.why : "")))) : null,
    arr(data.weeks).map(w => h("article", {class: "ocard"}, h("h2", `Week ${w.week}${w.theme ? ": " + w.theme : ""}`),
      h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", "Day"), h("th", "Post"), h("th", "Format"), h("th", "Hook"), h("th", "Idea"), h("th", "Effort"))),
        h("tbody", null, arr(w.days).map(d => h("tr", null, h("td", day(d.date)), h("td", nice(d.post_type) + (d.topic ? ` · ${d.topic}` : "")), h("td", vfTag(d.format) || "—"),
          h("td", d.hook || ""), h("td", d.idea || ""), h("td", nice(d.effort))))))))),
    h("article", {class: "ocard"}, ul("Batch it", data.batching_tips), ul("What to measure", data.measure)));
}

function renderReport(data, nums, o) {
  const accRows = arr(nums.accounts);
  return h("div", {class: "stack"},
    h("article", {class: "ocard"}, h("h2", "The short version"), h("ul", arr(data.headline).map(l => h("li", l))),
      accRows.length ? h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", "Competitor"), h("th", {class: "num"}, "Followers"), h("th", {class: "num"}, "Normal reel"), h("th", {class: "num"}, "Outliers"), h("th", "Best reel"))),
        h("tbody", null, accRows.map(a => h("tr", null, h("td", "@" + a.handle), h("td", {class: "num"}, num(a.followers)), h("td", {class: "num"}, num(a.normal_views)), h("td", {class: "num"}, num(a.outliers)),
          h("td", a.best_reel ? h("a", {href: "#reel/" + a.best_reel.id}, x(a.best_reel.score)) : "—")))))) : null),
    arr(data.remake_queue).length ? h("article", {class: "ocard"}, h("div", {class: "panel-head"}, h("h2", "The reels to remake first"),
      h("button", {type: "button", id: "start-top3", onclick: e => quick(e.currentTarget, {action: "start_top3", report_id: o.id})}, "Write scripts for the top 3")),
      h("ol", {class: "steps-ol"}, arr(data.remake_queue).map(q => h("li", h("div", {class: "stack tight"}, h("div", null, reelLink(nums, q.reel_id, "Open the reel"), q.angle ? " · " + q.angle : ""), h("span", {class: "small muted"}, q.why), buildOn({reel_id: q.reel_id}, {only: ["remake"]})))))) : null,
    arr(data.working).length ? h("article", {class: "ocard"}, h("h2", "What works"), h("ul", arr(data.working).map(w => h("li", h("b", w.finding), w.how_to_use ? " — " + w.how_to_use : "")))) : null,
    arr(data.recommended_formats).length ? h("article", {class: "ocard"}, h("h2", "Formats for you"), h("ul", arr(data.recommended_formats).map(f => h("li", vfTag(f.format), " ", f.why || "", f.first_script_idea ? h("div", {class: "small muted"}, "First idea: " + f.first_script_idea) : null)))) : null,
    h("article", {class: "ocard"}, h("h2", "Each competitor"), arr(data.competitors).map(c => h("div", {class: "card"}, h("b", "@" + c.handle), h("p", c.what_they_do || ""),
      c.what_works ? h("p", {class: "small"}, "What works: " + c.what_works) : null, ul("Best hooks", c.best_hooks), c.cta_pattern ? h("p", {class: "small"}, "Calls to action: " + c.cta_pattern) : null,
      c.sound_pattern ? h("p", {class: "small"}, "Sound: " + c.sound_pattern) : null, ul("Gaps you can fill", c.gaps)))),
    h("article", {class: "ocard"}, ul("Overused", data.overused), ul("Opportunities", data.opportunities), ul("Sound design", data.sound_design)),
    arr(data.customer_gaps).length ? h("article", {class: "ocard"}, h("h2", "What your customers need that nobody covers"), h("ul", arr(data.customer_gaps).map(g => h("li", h("b", g.pain), ": ", g.gap, g.idea ? h("div", {class: "small muted"}, "Idea: " + g.idea) : null)))) : null,
    arr(data.pain_coverage).length ? h("article", {class: "ocard"}, h("h2", "Your customers' problems: who covers them?"), h("ul", arr(data.pain_coverage).map(p => h("li", p.pain + ": ", p.covered ? pill("Covered") : pill("Not covered: your chance", "ok"))))) : null,
    arr(data.next_steps).length ? h("article", {class: "ocard"}, h("h2", "Next steps"), h("ol", arr(data.next_steps).map(s => h("li", s)))) : null,
    h("p", {class: "saves"}, "Instagram hides saves, and usually shares, for accounts you don't own. We rank on views, likes and comments."));
}

/* Profile: questionnaire editors (9.1), sources (9.2), draft + approve (9.3), topics (3.6). Used by onboarding
   step 1 and 4 and by #settings/profile and #settings/topics. */
import {h, clear, msg, say, busy, get, post, put, del, api, arr, nice, field, checkbox, toast, jobLine, S,
  onJobsDone, pollSoon, empty, day, every} from "../app.js";

export const SOURCE_LABEL = {skool_group: "Skool group page", skool_profile: "Skool profile", website: "Website",
  linkedin_pdf: "LinkedIn PDF", linkedin_text: "LinkedIn text", linkedin_page: "LinkedIn", instagram_bio: "Instagram bio", paste: "Pasted text"};
const FROM = {skool_group: "From your Skool group", skool_profile: "From your Skool profile", website: "From your website",
  linkedin_pdf: "From your LinkedIn", linkedin_text: "From your LinkedIn", linkedin_page: "From your LinkedIn",
  instagram_bio: "From your Instagram bio", paste: "From your pasted text"};
const STATUS = {queued: ["Reading", "busy"], waiting: ["Waiting for Chrome", "busy"], ok: ["Read", "ok"], needs_paste: ["Needs your text", "warn"], failed: ["Didn't work", "bad"]};

export function isEmpty(a) {
  if (!a) return true;
  const v = a.value;
  if (Array.isArray(v)) return !v.some(s => String(s || "").trim());
  return !String(v ?? "").trim();
}
function confLabel(c) { return c >= 0.8 ? "High" : c >= 0.5 ? "Check this" : "Guess"; }

/** One question. Returns {el, read(), dirty()}. read() gives the PUT /api/profile/answers value. */
export function questionEditor(q, a, sources) {
  a = a || {};
  let dirty = false;
  const mark = () => { dirty = true; };
  const box = h("div", {class: "stack", dataset: {q: q.id}});
  const head = h("div", {class: "stack tight"},
    h("div", {class: "row"}, h("span", {class: "label"}, `Question ${q.id.slice(1)}`), q.required ? h("span", {class: "pill"}, "Needed") : null),
    h("h3", q.label), h("p", {class: "hint"}, q.help));
  box.append(head);
  let read;
  const val = a.value;
  if (q.kind === "text") {
    const i = h("input", {value: typeof val === "string" ? val : "", id: "ans-" + q.id, oninput: mark});
    box.append(i); read = () => ({value: i.value});
  } else if (q.kind === "long") {
    const t = h("textarea", {id: "ans-" + q.id, oninput: mark}); t.value = typeof val === "string" ? val : "";
    box.append(t); read = () => ({value: t.value});
  } else if (q.kind === "list" && q.count) {
    const items = Array.isArray(val) ? val : [];
    const ins = Array.from({length: q.count}, (_, n) => h("input", {value: items[n] || "", id: `ans-${q.id}-${n}`, "aria-label": `${q.label} ${n + 1}`, oninput: mark, placeholder: `${n + 1}.`}));
    box.append(h("div", {class: "stack tight"}, ins)); read = () => ({value: ins.map(i => i.value.trim()).filter(Boolean)});
  } else if (q.kind === "list") {
    const t = h("textarea", {id: "ans-" + q.id, oninput: mark, placeholder: q.handles ? "@account_one\n@account_two" : "One per line"});
    t.value = (Array.isArray(val) ? val : []).map(v => q.handles ? "@" + v : v).join("\n");
    box.append(t, h("p", {class: "hint"}, "One per line.")); read = () => ({value: t.value.split("\n").map(s => s.trim()).filter(Boolean)});
  } else if (q.kind === "choice") {
    const picks = new Set(Array.isArray(val) ? val : []);
    const boxes = arr(q.choices).map(c => { const [l, i] = checkbox(c.label, picks.has(c.id), {value: c.id, onchange: mark}); return [l, i]; });
    box.append(h("div", {class: "checks"}, boxes.map(b => b[0])));
    const text = h("input", {value: a.text || "", id: "ans-" + q.id + "-text", oninput: mark, placeholder: q.id === "q10" ? "Always: ... Never: ... Style like @..." : "Anything else"});
    box.append(field(q.id === "q10" ? "Words you always or never use, and creators you like" : "Anything else", text));
    let extra = () => ({});
    if (q.id === "q12") {
      const hours = h("input", {type: "number", min: 0, max: 80, step: 1, value: a.hours ?? "", id: "ans-q12-hours", oninput: mark});
      const editor = h("select", {id: "ans-q12-editor", onchange: mark},
        h("option", {value: "", selected: a.editor === null || a.editor === undefined}, "Choose"),
        h("option", {value: "yes", selected: a.editor === true}, "Yes, I have an editor"),
        h("option", {value: "no", selected: a.editor === false}, "No, I edit myself"));
      const langs = h("input", {value: arr(a.languages).join(", "), id: "ans-q12-languages", oninput: mark, placeholder: "en"});
      box.append(h("div", {class: "grid3"}, field("Hours a week for reels", hours), field("Do you have an editor?", editor), field("Languages", langs)));
      extra = () => ({hours: hours.value === "" ? null : Number(hours.value), editor: editor.value === "" ? null : editor.value === "yes",
        languages: langs.value.split(/[,;/]/).map(s => s.trim()).filter(Boolean)});
    }
    read = () => Object.assign({value: boxes.filter(b => b[1].checked).map(b => b[1].value), text: text.value}, extra());
  }
  // Where a pre-filled answer came from, and the "This is right" tick.
  const src = a.source && a.source !== "member" ? sources[a.source] : null;
  if (a.source && a.source !== "member" && !isEmpty(a)) {
    const chip = h("div", {class: "row"},
      h("span", {class: "pill busy"}, (src ? FROM[src.kind] || "From your links" : "From your links") + " · " + (a.confirmed ? "checked" : "check it")),
      h("span", {class: "pill"}, confLabel(a.confidence || 0)));
    if (!a.confirmed) {
      chip.append(h("button", {type: "button", class: "ghost sm", onclick: async e => {
        const r = await busy(e.currentTarget, () => put("/api/profile/answers", {answers: {[q.id]: {confirmed: true}}}));
        if (r.ok) { e.currentTarget.replaceWith(h("span", {class: "pill ok"}, "Checked")); toast("Saved."); } else toast(r.error);
      }}, "This is right"));
    }
    if (a.note) chip.append(h("span", {class: "hint"}, a.note));
    box.insertBefore(chip, head.nextSibling);
  }
  if (a.suggestion && isEmpty(a)) {
    const sug = Array.isArray(a.suggestion) ? a.suggestion.join("; ") : String(a.suggestion);
    box.append(h("div", {class: "card"}, h("p", {class: "hint"}, "A guess from your links. Use it only if it's right."), h("p", sug),
      h("div", {class: "row"}, h("button", {type: "button", class: "ghost sm", onclick: () => {
        const first = box.querySelector("input:not([type=checkbox]),textarea");
        if (!first) return;
        if (q.kind === "list" && q.count) arr(a.suggestion).forEach((s, n) => { const i = box.querySelector(`#ans-${q.id}-${n}`); if (i) i.value = s; });
        else first.value = sug.replace(/; /g, q.kind === "list" ? "\n" : "; ");
        mark(); toast("Added. Edit it, then save.");
      }}, "Use this guess"))));
  }
  return {el: box, read, dirty: () => dirty, id: q.id};
}

/** Saves the questions that changed. Returns the API result (or {ok:true} when nothing changed). */
export async function saveEditors(editors) {
  const answers = {};
  for (const e of editors) if (e.dirty()) answers[e.id] = e.read();
  if (!Object.keys(answers).length) return {ok: true, data: null, nothing: true};
  return put("/api/profile/answers", {answers});
}

// --------------------------------------------------------------------------------------------- sources
export function sourceList(sources, onChange) {
  if (!sources.length) return h("p", {class: "muted"}, "No links yet.");
  return h("div", {class: "stack"}, sources.map(s => sourceRow(s, onChange)));
}
function sourceRow(s, onChange) {
  const [stText, stKind] = STATUS[s.status] || [nice(s.status), ""];
  const pasteBox = h("div", {class: "stack", hidden: !(s.status === "needs_paste" || s.status === "failed")});
  const ta = h("textarea", {"aria-label": "Paste the text for " + (SOURCE_LABEL[s.kind] || s.kind)});
  const out = msg();
  pasteBox.append(ta, out, h("div", {class: "row"}, h("button", {type: "button", class: "sm", onclick: async e => {
    const r = await busy(e.currentTarget, () => post(`/api/profile/sources/${s.id}/paste`, {text: ta.value}));
    if (!r.ok) { say(out, r); return; }
    toast("Saved."); onChange && onChange();
  }}, "Save this text")));
  const canRead = !["paste", "linkedin_text"].includes(s.kind) && (s.status === "needs_paste" || s.status === "failed");
  return h("div", {class: "card", dataset: {source: s.id}},
    h("div", {class: "row"}, h("b", SOURCE_LABEL[s.kind] || nice(s.kind)), h("span", {class: "pill " + stKind}, stText),
      s.label || s.title ? h("span", {class: "muted small"}, s.label || s.title) : null, h("span", {class: "spacer"}),
      canRead ? h("button", {type: "button", class: "ghost sm", onclick: async e => {
        const r = await busy(e.currentTarget, () => post(`/api/profile/sources/${s.id}/read`, {}));
        toast(r.ok ? "Reading it now." : r.error); if (r.ok) { pollSoon(); onChange && onChange(); }
      }}, "Read it again") : null,
      h("button", {type: "button", class: "ghost sm", onclick: () => { pasteBox.hidden = !pasteBox.hidden; }}, "Paste text"),
      h("button", {type: "button", class: "ghost sm", "aria-label": "Delete this link", onclick: async e => {
        const r = await busy(e.currentTarget, () => del(`/api/profile/sources/${s.id}`));
        toast(r.ok ? "Deleted." : r.error); onChange && onChange();
      }}, "Delete")),
    s.url ? h("p", {class: "small muted"}, s.url) : null,
    s.error ? h("p", {class: "small"}, s.error) : null,
    s.status === "ok" && s.preview ? h("p", {class: "small muted"}, s.preview.slice(0, 200) + (s.chars > 200 ? "…" : "")) : null,
    arr(s.found).length ? h("div", {class: "row"}, h("span", {class: "small"}, "We found more links:"), arr(s.found).map(f =>
      h("button", {type: "button", class: "ghost sm", onclick: async e => {
        const kind = f.kind === "skool" ? "skool" : f.kind;
        const r = await busy(e.currentTarget, () => post("/api/profile/sources", {kind, url: f.url}));
        toast(r.ok ? "Added." : r.error); onChange && onChange();
      }}, `Add ${SOURCE_LABEL[f.kind] || "link"}: ${f.url.replace(/^https?:\/\/(www\.)?/, "").slice(0, 40)}`))) : null,
    s.job && s.status !== "ok" ? jobLine(s.job) : null,
    pasteBox);
}

/** The "add a link" box: Skool, website, LinkedIn (read / PDF / paste), Instagram bio, paste. */
export function addSourceBox(onAdded, {instagram = true} = {}) {
  const out = msg();
  const run = async (btn, body) => {
    const r = await busy(btn, () => body instanceof FormData ? api("POST", "/api/profile/sources", body) : post("/api/profile/sources", body));
    say(out, r, r.ok ? (r.data.source.status === "needs_paste" ? r.data.source.error || "Paste the text for this one." : "Added. We're reading it.") : "");
    if (r.ok) { pollSoon(); onAdded && onAdded(r.data); }
    return r;
  };
  const skool = h("input", {placeholder: "skool.com/@you or skool.com/your-group/about", id: "src-skool"});
  const site = h("input", {placeholder: "yourwebsite.com", id: "src-website"});
  const ig = h("input", {placeholder: "@yourname", id: "src-instagram"});
  const pdf = h("input", {type: "file", accept: "application/pdf", id: "src-pdf"});
  const paste = h("textarea", {id: "src-paste", placeholder: "Your About text, a sales page, anything that describes you and your customer."});
  const pasteLabel = h("input", {id: "src-paste-label", placeholder: "What is it? (for example: My sales page)"});
  return h("div", {class: "stack"},
    h("div", {class: "row"}, h("label", {class: "sr", for: "src-skool"}, "Skool link"), skool,
      h("button", {type: "button", class: "ghost", onclick: e => skool.value.trim() && run(e.currentTarget, {kind: "skool", url: skool.value.trim()}).then(r => r.ok && (skool.value = ""))}, "Add Skool link")),
    h("div", {class: "row"}, h("label", {class: "sr", for: "src-website"}, "Website"), site,
      h("button", {type: "button", class: "ghost", onclick: e => site.value.trim() && run(e.currentTarget, {kind: "website", url: site.value.trim()}).then(r => r.ok && (site.value = ""))}, "Add website")),
    !instagram ? null : h("div", {class: "row"}, h("label", {class: "sr", for: "src-instagram"}, "Your Instagram"), ig,
      h("button", {type: "button", class: "ghost", onclick: e => ig.value.trim() && run(e.currentTarget, {kind: "instagram_bio", url: ig.value.trim()}).then(r => r.ok && (ig.value = ""))}, "Add my Instagram bio")),
    h("div", {class: "card"}, h("b", "LinkedIn"),
      h("p", {class: "hint"}, "We never open LinkedIn from our server. Your own Chrome reads your profile when you click, or you upload the PDF (on a computer: Me > View profile > Resources > Save to PDF), or paste it."),
      h("div", {class: "row"}, h("button", {type: "button", class: "ghost sm", onclick: e => run(e.currentTarget, {kind: "linkedin_page"})}, "Read my LinkedIn"),
        h("label", {class: "sr", for: "src-pdf"}, "LinkedIn PDF"), pdf,
        h("button", {type: "button", class: "ghost sm", onclick: e => {
          if (!pdf.files[0]) { say(out, {error: "Choose your LinkedIn PDF first."}); return; }
          const fd = new FormData(); fd.append("kind", "linkedin_pdf"); fd.append("file", pdf.files[0]);
          run(e.currentTarget, fd).then(r => r.ok && (pdf.value = ""));
        }}, "Upload the PDF"))),
    h("details", {class: "fold"}, h("summary", "Paste text instead"),
      h("div", {class: "stack"}, field("Label", pasteLabel), field("Text", paste),
        h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: e => paste.value.trim() && run(e.currentTarget, {kind: "paste", text: paste.value, label: pasteLabel.value.trim()}).then(r => { if (r.ok) { paste.value = ""; pasteLabel.value = ""; } })}, "Save the text")))),
    out);
}

// --------------------------------------------------------------------------------------------- digest
export function digestView(d) {
  if (!d) return null;
  const caps = d.capabilities || {};
  const can = Object.entries({on_camera: "on camera", voice: "voice", screen: "screen recordings", broll: "B-roll",
    hands: "hands demos", second_person: "a second person", location: "on location", editor: "an editor"}).filter(([k]) => caps[k]).map(([, v]) => v);
  const li = list => arr(list).length ? h("ul", arr(list).map(s => h("li", s))) : h("p", {class: "muted"}, "—");
  return h("dl", {class: "kv"},
    h("dt", "Niche"), h("dd", d.niche_label || "—"),
    h("dt", "Who you help"), h("dd", d.who_we_help || "—"),
    h("dt", "Their top problems"), h("dd", li(d.top_pains)),
    h("dt", "What they want"), h("dd", d.dream_outcome || "—"),
    h("dt", "What you sell"), h("dd", d.offer || "—"),
    h("dt", "Your proof"), h("dd", li(d.proof)),
    h("dt", "Your voice"), h("dd", arr((d.voice || {}).tone).map(nice).join(", ") || "—"),
    h("dt", "Main call to action"), h("dd", d.main_cta || "—"),
    h("dt", "You can make"), h("dd", (can.join(", ") || "—") + (caps.hours_per_week ? ` · ${caps.hours_per_week} hours a week` : "")),
    h("dt", "Hard no's"), h("dd", li(d.hard_nos)));
}

// --------------------------------------------------------------------------------------------- #settings/profile
export async function profilePage(main, {token}) {
  const r = await get("/api/profile");
  if (S.view !== token) return;
  if (!r.ok) { clear(main, settingsTabs("profile"), h("h1", "Your profile"), empty(r.error)); return; }
  const p = r.data;
  const reload = () => profilePage(main, {token});
  const sources = Object.fromEntries(arr(p.sources).map(s => [s.id, s]));
  const editors = arr(p.questions).map(q => questionEditor(q, p.answers[q.id], sources));
  const out = msg();
  const statusText = {empty: "Not started", drafting: "Drafting", draft: "Draft: not approved yet", approved: `Approved (version ${p.version})`}[p.status] || p.status;
  const saveBtn = h("button", {type: "button", onclick: async e => {
    const res = await busy(e.currentTarget, () => saveEditors(editors));
    say(out, res, res.nothing ? "Nothing changed." : "Saved.");
    if (res.ok && !res.nothing) reload();
  }}, "Save my answers");
  const approveBtn = h("button", {type: "button", class: "ghost", onclick: async e => {
    const s = await busy(e.currentTarget, () => saveEditors(editors));
    if (!s.ok) { say(out, s); return; }
    const a = await busy(e.currentTarget, () => post("/api/profile/approve", {}));
    if (!a.ok) { say(out, a); return; }
    toast("Approved. Your outputs now use this profile."); reload();
  }}, "This is right: approve");
  const draftBox = h("div", {class: "stack"});
  if (p.drafting_job) {
    draftBox.append(jobLine(p.drafting_job));
    every(3000, async stop => {   // live progress of the draft
      const j = await get("/api/jobs/" + p.drafting_job.id, {quiet: true});
      if (!j.ok) { stop(); return; }
      clear(draftBox, jobLine(j.data));
      if (!["queued", "running", "waiting", "paused"].includes(j.data.state)) { stop(); if (!main.querySelector("input:focus,textarea:focus")) reload(); }
    });
  }
  onJobsDone(() => { if (!main.querySelector("input:focus,textarea:focus")) reload(); });
  clear(main, h("div", {class: "view"}, settingsTabs("profile"),
    h("div", {class: "headrow"}, h("h1", "Your profile"), h("span", {class: "pill " + (p.status === "approved" ? "ok" : "warn")}, statusText)),
    h("p", {class: "muted"}, "Your scripts and plans are written for your customer. Answer what you can. Questions 1, 3, 7 and 12 are needed before outputs."),
    h("section", {class: "panel"}, h("h2", "Your links"), h("p", {class: "hint"}, "We read these to fill in your answers. Text from links is only used as information about you."),
      sourceList(arr(p.sources), reload), addSourceBox(reload),
      h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: async e => {
        const d = await busy(e.currentTarget, () => post("/api/profile/draft", {}));
        if (!d.ok) { toast(d.error); return; }
        toast("Drafting your answers from your links."); pollSoon(); reload();
      }}, "Fill in my answers from my links")), draftBox),
    h("section", {class: "panel"}, h("h2", "Questions"), editors.map(e => [e.el, h("div", {class: "divider"})]),
      out, h("div", {class: "row"}, saveBtn, approveBtn)),
    p.digest ? h("section", {class: "panel"}, h("h2", "What your AI gets"), h("p", {class: "hint"}, `Approved ${day(p.approved_at)}.`), digestView(p.digest)) : null));
}

// --------------------------------------------------------------------------------------------- #settings/topics
export async function topicsPage(main, {token}) {
  const r = await get("/api/topics");
  if (S.view !== token) return;
  if (!r.ok) { clear(main, settingsTabs("topics"), h("h1", "Your topics"), empty(r.error)); return; }
  const d = r.data;
  const reload = () => topicsPage(main, {token});
  const rows = h("div", {class: "stack", id: "topic-rows"});
  const out = msg();
  const addRow = t => {
    const label = h("input", {value: t.label || "", "aria-label": "Topic name", maxlength: 40, placeholder: "Topic name"});
    const desc = h("input", {value: t.description || "", "aria-label": "What the reel shows", maxlength: 200, placeholder: "What a reel on this topic shows or teaches"});
    const row = h("div", {class: "card", dataset: {id: t.id || "", slug: t.slug || ""}},
      h("div", {class: "row"}, label, h("button", {type: "button", class: "ghost sm", onclick: () => row.remove()}, "Remove")), desc);
    row._read = () => ({id: t.id || undefined, slug: t.slug || undefined, label: label.value.trim(), description: desc.value.trim(), examples: arr(t.examples)});
    rows.append(row);
  };
  arr(d.topics).forEach(addRow);
  const sugg = arr(d.suggested);
  onJobsDone(() => { if (!main.querySelector("input:focus,textarea:focus")) reload(); });
  clear(main, h("div", {class: "view"}, settingsTabs("topics"),
    h("h1", "Your topics"),
    h("p", {class: "muted"}, "Topics sort reels by what they show or teach. You can have 0 to 20. With none, reels go to Unsorted. That's fine."),
    d.unsorted_reels ? h("p", null, h("a", {href: "#reels?topic=unsorted"}, `${d.unsorted_reels} reels are in Unsorted`), ".") : null,
    sugg.length ? h("section", {class: "panel"}, h("h2", "Suggested topics"),
      h("div", {class: "stack"}, sugg.map(t => h("div", {class: "card"}, h("div", {class: "row"}, h("b", t.label), t.seen_in ? h("span", {class: "pill"}, `seen in ${t.seen_in} reels`) : null,
        h("span", {class: "spacer"}),
        h("button", {type: "button", class: "sm", onclick: async e => { const x = await busy(e.currentTarget, () => post("/api/topics/accept", {ids: [t.id], dismiss: []})); toast(x.ok ? "Added. We'll re-sort your reels." : x.error); reload(); }}, "Add"),
        h("button", {type: "button", class: "ghost sm", onclick: async e => { const x = await busy(e.currentTarget, () => post("/api/topics/accept", {ids: [], dismiss: [t.id]})); toast(x.ok ? "Dismissed." : x.error); reload(); }}, "No thanks")),
      t.description ? h("p", {class: "small muted"}, t.description) : null)))) : null,
    h("section", {class: "panel"}, h("h2", "Topics"), rows,
      h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: () => addRow({})}, "Add a topic")), out,
      h("div", {class: "row"}, h("button", {type: "button", id: "save-topics", onclick: async e => {
        const list = [...rows.children].map(c => c._read()).filter(t => t.label);
        const x = await busy(e.currentTarget, () => put("/api/topics", {topics: list}));
        say(out, x, x.ok ? (x.data.retag_job ? "Saved. We're re-sorting your reels." : "Saved.") : "");
        if (x.ok) { pollSoon(); setTimeout(reload, 600); }
      }}, "Save topics"))),
    h("section", {class: "panel"}, h("h2", "Get suggestions"),
      h("p", {class: "hint"}, "Your AI can suggest topics from your profile, or from your competitors' reels once 30 or more are tagged."),
      h("div", {class: "row"},
        h("button", {type: "button", class: "ghost", onclick: async e => { const x = await busy(e.currentTarget, () => post("/api/topics/suggest", {from: "profile"})); toast(x.ok ? "Asked your AI. Suggestions show up here." : x.error); pollSoon(); }}, "Suggest from my profile"),
        h("button", {type: "button", class: "ghost", onclick: async e => { const x = await busy(e.currentTarget, () => post("/api/topics/suggest", {from: "reels"})); toast(x.ok ? "Asked your AI. Suggestions show up here." : x.error); pollSoon(); }}, "Suggest from reels")))));
}

export function settingsTabs(cur) {
  return h("div", {class: "tabs", role: "tablist"}, [["ai", "AI"], ["extension", "Chrome extension"], ["profile", "Profile"], ["topics", "Topics"], ["account", "Account"]]
    .map(([id, label]) => h("a", {href: "#settings/" + id, role: "tab", "aria-selected": String(id === cur)}, label)));
}

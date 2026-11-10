/* #working (what works), #format/<id> (format playbook, 6.9), #banks/{hooks,formats,ctas,sounds,music} (4.9). */
import {h, clear, get, arr, S, go, select, toast, num, x, mmss, nice, empty, savesLine, pill, vfTag, reelCard, buildOn,
  quick, backLink, qs, tabs, thumbStyle, copyBtn, linkBtn} from "../app.js";
import {formatCard, workingCard, signatureCard, dataNote, DIM_LABEL, canMakeLine, evidencePill} from "./parts.js";

const DIMS = ["visual_format", "sub_format", "hook_type", "cta_type", "sound", "topic", "length", "cut_pace", "caption_style",
  "text_hook", "headline_box", "face_bubble", "broll_share", "setting"];

function bankTabs(cur) {
  return tabs([["working", "What works", "#working"], ["hooks", "Hooks", "#banks/hooks"], ["formats", "Formats", "#banks/formats"],
    ["ctas", "Calls to action", "#banks/ctas"], ["sounds", "Sounds", "#banks/sounds"], ["music", "Music", "#banks/music"]], cur);
}

export async function working(main, {query, token}) {
  const dim = DIMS.includes(query.get("dimension")) ? query.get("dimension") : "visual_format";
  const win = query.get("window") === "recent" ? "recent" : "all";
  const [sr, ir] = await Promise.all([get("/api/insights/summary"), get("/api/insights" + qs({dimension: dim, window: win}))]);
  if (S.view !== token) return;
  const view = h("div", {class: "view"}, bankTabs("working"), h("h1", "What works"));
  if (!sr.ok) { view.append(empty(sr.error)); clear(main, view); return; }
  const s = sr.data;
  if (!s.snapshot_id && !arr(s.cards).length) {
    view.append(empty("Nothing to show yet. What works appears once some of your competitors' reels are tagged." +
      ((s.pipeline || {}).eta ? " " + s.pipeline.eta + "." : ""), linkBtn("#competitors", "Add competitors")));
    clear(main, view); return;
  }
  if ((s.pipeline || {}).left_to_tag || (s.pipeline || {}).failed_tags) view.append(h("p", null, pill(s.pipeline.line || `${num(s.pipeline.left_to_tag)} reels left to tag`, "busy")));
  if (arr(s.formats).length) {
    // Fix round 2 (same rule as Home, D85): only formats that beat normal are "to copy"; the rest sit in a closed
    // "Formats to avoid" list with no Write-scripts button.
    const wins = list => arr(list).filter(f => f.lift == null || f.lift >= 1);
    const avoid = arr(s.formats).filter(f => f.lift != null && f.lift < 1);
    const grid = h("div", {class: "grid3", id: "format-cards"}, wins(s.formats).length ? wins(s.formats).map(formatCard)
      : h("p", {class: "muted"}, "No format beats normal yet. More tagged reels will show what works."));
    const onlyMine = h("input", {type: "checkbox", id: "can-make-only", onchange: async e => {
      const f = await get("/api/formats" + (e.target.checked ? "?can_make=1" : ""));
      if (!f.ok) { toast(f.error); return; }
      const list = wins(f.data.formats);
      clear(grid, list.length ? list.map(formatCard) : h("p", {class: "muted"}, "None of these fit what you said you can make. Change it in Settings > Profile, question 12."));
    }});
    view.append(h("section", {class: "stack"}, h("div", {class: "panel-head"}, h("h2", "Formats to copy"),
      h("label", {class: "check"}, onlyMine, "Only formats I can make")), grid,
      avoid.length ? h("details", {class: "panel", id: "formats-avoid"}, h("summary", `Formats to avoid (${avoid.length})`),
        h("p", {class: "hint"}, "These did worse than normal for your competitors."),
        h("ul", avoid.map(f => h("li", h("a", {href: f.href || "#format/" + f.id}, f.name || f.vf_label), " · ", f.line || "")))) : null));
  }
  if (arr(s.cards).length) view.append(h("section", {class: "stack"}, h("h2", "What makes reels outliers"), h("div", {class: "grid3"}, arr(s.cards).map(workingCard))));
  if (s.you_vs_them) {
    const y = s.you_vs_them;
    view.append(h("section", {class: "panel"}, h("h2", "You vs them"), h("p", y.line || ""),
      arr(y.formats).length ? h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", "Format"), h("th", {class: "num"}, "You"), h("th", {class: "num"}, "Them"))),
        h("tbody", null, arr(y.formats).map(f => h("tr", null, h("td", vfTag(f.vf, f.label || f.vf)), h("td", {class: "num"}, Math.round((f.you || 0) * 100) + "%"), h("td", {class: "num"}, Math.round((f.them || 0) * 100) + "%")))))) : null));
  }
  // Per-dimension table with Rising/Fading.
  const d = ir.ok ? ir.data : null;
  const dimSel = select(DIMS.map(k => [k, DIM_LABEL[k] || nice(k)]), dim, {"aria-label": "Look at", id: "dim-select",
    onchange: e => go("#working" + qs({dimension: e.target.value, window: win === "recent" ? "recent" : ""}))});
  const winSel = select([["all", "Last 12 months"], ["recent", "Last 90 days"]], win, {"aria-label": "Time", onchange: e => go("#working" + qs({dimension: dim, window: e.target.value === "recent" ? "recent" : ""}))});
  const table = h("section", {class: "panel", id: "lift-table"}, h("div", {class: "panel-head"}, h("h2", "Compare one thing at a time"), h("div", {class: "row"}, dimSel, winSel)));
  if (d && arr(d.rows).length) {
    table.append(h("p", {class: "hint"}, "“Times more likely” = how much more often reels with this became outliers, compared with all tagged reels."),
      h("div", {class: "tablewrap"}, h("table", null,
        h("thead", null, h("tr", null, h("th", DIM_LABEL[dim] || nice(dim)), h("th", {class: "num"}, "Times more likely"), h("th", {class: "num"}, "Reels"), h("th", {class: "num"}, "Competitors"), h("th", "Evidence"), h("th", "Trend"), h("th", "Best reels"))),
        h("tbody", null, arr(d.rows).map(rw => h("tr", null,
          h("td", dim === "visual_format" ? vfTag(rw.value, rw.label) : dim === "sub_format" ? h("a", {href: "#format/" + rw.value}, rw.label || rw.value) : (rw.label || nice(rw.value))),
          h("td", {class: "num"}, x(rw.lift)), h("td", {class: "num"}, num(rw.n)), h("td", {class: "num"}, num(rw.k)), h("td", nice(rw.evidence)),
          h("td", rw.trend === "rising" ? pill("Rising", "ok") : rw.trend === "fading" ? pill("Fading", "warn") : "—"),
          h("td", h("div", {class: "row"}, arr(rw.best_reels).slice(0, 3).map((id, n) => h("a", {href: "#reel/" + id}, `#${n + 1}`))))))))));
    const g = d.grid || {};
    const gl = (title, ids) => arr(ids).length ? h("div", {class: "card"}, h("b", title), h("p", arr(ids).map(id => (arr(d.rows).find(rw => rw.value === id) || {}).label || nice(id)).join(", "))) : null;
    table.append(h("div", {class: "grid3"}, gl("Working and common", g.working_common), gl("Opportunity: works, few use it", g.opportunity), gl("Overused: common, doesn't stand out", g.overused)));
    if (arr(d.combos).length) {
      table.append(h("h3", "Combos that work together"), h("ul", arr(d.combos).slice(0, 8).map(c => h("li", `${c.a_label || nice(c.a)} + ${c.b_label || nice(c.b)}: ${x(c.lift)} more likely (${c.n} reels)`))));
    }
  } else table.append(h("p", {class: "muted"}, ir.ok ? "Not enough tagged reels for this yet." : ir.error));
  view.append(table);
  if (arr(s.per_competitor).length) view.append(h("section", {class: "stack"}, h("h2", "Each competitor's signature"), h("div", {class: "grid2"}, arr(s.per_competitor).map(signatureCard))));
  view.append(dataNote(s.data_note), savesLine((s.data_note || {}).saves_line));
  clear(main, view);
}

export async function format(main, {parts, token}) {
  const r = await get("/api/formats/" + encodeURIComponent(parts[0] || ""));
  if (S.view !== token) return;
  if (!r.ok) { clear(main, backLink("#working", "What works"), h("h1", "Format"), empty(r.error)); return; }
  const f = r.data.format;
  const g = f.guide || {};
  const lst = (title, items) => arr(items).length ? h("section", {class: "panel"}, h("h2", title), h("ol", {class: "steps-ol"}, arr(items).map(i => h("li", i)))) : null;
  const examples = arr(f.examples);
  clear(main, h("div", {class: "view"}, backLink("#working", "What works"),
    h("div", {class: "row"}, vfTag(f.vf, f.vf_label), ...evidencePill(f.evidence, f.trend)),
    h("h1", f.name || f.vf_label),
    h("p", {class: "line"}, f.line || ""),
    h("div", {class: "row"},
      h("button", {type: "button", class: "big", id: "write-3", onclick: e => quick(e.currentTarget, {action: "use_format", format_id: f.id})}, "Write 3 scripts in this format"),
      h("button", {type: "button", class: "ghost", onclick: e => quick(e.currentTarget, {action: "use_format", vf: f.vf})}, "Use this format")),
    h("section", {class: "panel" + (f.can_make && !f.can_make.ok ? "" : " hl")}, h("h2", "Can you make this?"),
      h("div", {class: "row"}, canMakeLine(f.can_make)),
      arr(f.needs).length ? h("p", {class: "hint"}, "It needs: " + arr(f.needs).map(n => nice(n).toLowerCase()).join(", ") + ".") : null,
      g.time_to_make ? h("p", {class: "hint"}, "Time to make: " + ({under_1h: "under 1 hour", "1_3h": "1-3 hours", half_day: "about half a day"}[g.time_to_make] || g.time_to_make) + ".") : null,
      f.can_make && !f.can_make.ok ? h("p", null, h("a", {href: "#settings/profile"}, "Change what you can make (question 12)")) : null),
    g.what_it_is ? h("section", {class: "panel"}, h("h2", "What it is"), h("p", g.what_it_is), list2(g.why_it_works, "Why it works")) :
      h("section", {class: "panel"}, h("p", {class: "muted"}, "Your AI is still writing this playbook. The examples below are ready now.")),
    examples.length ? h("section", {class: "stack"}, h("h2", "Outlier examples"), h("div", {class: "cards"}, examples.map(ex => exampleCard(ex)))) : null,
    lst("How to shoot it", g.shoot_steps), lst("How to edit it", g.edit_steps), lst("The sound", g.sound_recipe),
    arr(f.music).length ? h("section", {class: "panel"}, h("h2", "Music the examples used"), h("ul", arr(f.music).map(m => h("li", `${m.title}${m.artist ? " · " + m.artist : ""}`,
      m.example_reel ? [" (", h("a", {href: "#reel/" + m.example_reel.id}, "example"), ")"] : null)))) : null,
    savesLine()));
}
function list2(items, title) { return arr(items).length ? h("div", {class: "blk"}, h("span", {class: "label"}, title), h("ul", arr(items).map(i => h("li", i)))) : null; }
function exampleCard(ex) {
  const card = reelCard(ex);
  const hookFrame = arr(ex.frames).find(fr => fr.kind === "hook") || arr(ex.frames)[0];
  if (arr(ex.frames).length) {
    card.insertBefore(h("div", {class: "frames", style: {padding: "6px 8px 0"}}, arr(ex.frames).slice(0, 4).map(fr => h("a", {href: `#reel/${ex.id}?tab=hook`, style: thumbStyle(fr.url),
      "aria-label": `Frame at ${mmss(fr.t)}`, title: `Frame at ${mmss(fr.t)}`}, h("span", mmss(fr.t))))), card.lastChild);
  }
  card.querySelector(".acts").prepend(h("a", {class: "btn ghost sm", href: `#reel/${ex.id}?tab=hook`}, hookFrame ? "Play at the hook" : "See the hook"));
  if (ex.url && /^https:\/\/(www\.)?instagram\.com\//.test(ex.url)) {
    card.querySelector(".acts").append(h("a", {class: "btn ghost sm", href: ex.url, target: "_blank", rel: "noopener noreferrer"}, "Open on Instagram"));
  }
  return card;
}

// ------------------------------------------------------------------------------------------------ banks
export async function banks(main, {parts, query, token}) {
  const which = ["hooks", "formats", "ctas", "sounds", "music"].includes(parts[0]) ? parts[0] : "hooks";
  const params = which === "hooks" ? {format: query.get("format"), topic: query.get("topic"), level: query.get("level"), min_score: query.get("min_score")} : {};
  const r = await get(`/api/banks/${which}` + qs(params));
  if (S.view !== token) return;
  const view = h("div", {class: "view"}, bankTabs(which), h("h1", {hooks: "Hook bank", formats: "Format bank", ctas: "Calls to action", sounds: "Sound patterns", music: "Music the outliers used"}[which]));
  if (!r.ok) { view.append(empty(r.error)); clear(main, view); return; }
  const d = r.data;
  const none = text => view.append(empty(text));
  if (which === "hooks") {
    const levelSel = select([["", "All hooks"], ["deep", "Full breakdowns only"], ["quick", "Quick tags only"]], query.get("level") || "", {"aria-label": "Which hooks", onchange: e => go("#banks/hooks" + qs({level: e.target.value}))});
    view.append(h("div", {class: "filters"}, levelSel));
    const rows = arr(d.hooks);
    if (!rows.length) none("No hooks yet. Hooks show up once your competitors' outliers are tagged.");
    else view.append(h("div", {class: "stack"}, rows.map(hk => h("article", {class: "card"},
      h("div", {class: "row"}, h("span", {class: "score"}, x(hk.score)), h("a", {href: "#reel/" + hk.reel_id}, "@" + hk.handle), hk.hook_type ? pill(nice(hk.hook_type)) : null, pill(hk.level === "deep" ? "Full breakdown" : "Quick tag")),
      hk.spoken ? h("p", {class: "quote"}, "“" + hk.spoken + "”") : null,
      hk.on_screen_text ? h("p", {class: "small"}, h("b", "On screen: "), hk.on_screen_text) : null,
      hk.visual ? h("p", {class: "small muted"}, hk.visual) : null,
      hk.formula ? h("div", {class: "copyline"}, h("div", {class: "formula"}, hk.formula), copyBtn(hk.formula)) : null,
      hk.why ? h("p", {class: "small"}, h("b", "Why it works: "), hk.why) : null,
      buildOn({reel_id: hk.reel_id}, {only: arr(hk.actions).length ? [...arr(hk.actions), "use_format"] : null})))));
  } else if (which === "formats") {
    const rows = arr(d.formats);
    if (!rows.length) none("No formats yet. They show up once your competitors' reels are tagged.");
    else view.append(h("div", {class: "stack"}, rows.map(fm => h("article", {class: "card"},
      h("div", {class: "row"}, vfTag(fm.id, fm.label), ...evidencePill(fm.evidence, fm.trend), canMakeLine(fm.can_make)),
      h("p", {class: "line"}, `${likely(fm.lift)} · ${num(fm.reels)} reels`),
      fm.recipe ? h("p", {class: "small"}, fm.recipe) : null,
      arr(fm.sub_formats).length ? h("div", {class: "row"}, arr(fm.sub_formats).map(id => h("a", {class: "btn ghost sm", href: "#format/" + id}, "Open the playbook"))) : null,
      h("div", {class: "row"}, arr(fm.best_reels).slice(0, 3).map((id, n) => h("a", {href: "#reel/" + id}, `Example ${n + 1}`))),
      buildOn({vf: fm.id, reel_id: arr(fm.best_reels)[0]})))));
  } else if (which === "ctas") {
    const rows = arr(d.ctas);
    if (!rows.length) none("No calls to action yet.");
    else view.append(h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", {class: "num"}, "Score"), h("th", "Call to action"), h("th", "Type"), h("th", "Where"), h("th", "Reel"))),
      h("tbody", null, rows.map(c => h("tr", null, h("td", {class: "num"}, x(c.score)), h("td", c.text + (c.keyword ? ` (keyword: ${c.keyword})` : "")), h("td", c.cta_label || nice(c.cta_type)),
        h("td", nice(c.placement)), h("td", h("a", {href: "#reel/" + c.reel_id}, "@" + c.handle))))))));
  } else if (which === "sounds") {
    const rows = arr(d.patterns);
    if (!rows.length) none("No sound patterns yet.");
    else view.append(h("div", {class: "stack"}, rows.map(p => h("article", {class: "card"},
      h("div", {class: "row"}, h("b", p.label), ...evidencePill(p.evidence, p.trend)),
      h("p", {class: "line"}, `${likely(p.lift)} · ${num(p.reels)} reels`),
      arr(p.example_cues).length ? h("ul", arr(p.example_cues).slice(0, 5).map(c => h("li", null, `${nice(c.label)} at ${mmss(c.t)} in `, h("a", {href: `#reel/${c.reel_id}?tab=sound`}, "this reel")))) : null,
      h("div", {class: "row"}, arr(p.best_reels).slice(0, 3).map((id, n) => h("a", {href: `#reel/${id}?tab=sound`}, `Example ${n + 1}`)))))));
  } else {
    const rows = arr(d.tracks);
    if (!rows.length) none("No music yet. It shows up after your first pulls.");
    else view.append(h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", "Track"), h("th", "Type"), h("th", {class: "num"}, "Outliers"), h("th", {class: "num"}, "Reels"), h("th", "Example"))),
      h("tbody", null, rows.map(m => h("tr", null, h("td", m.title + (m.artist ? " · " + m.artist : "")), h("td", {licensed: "Licensed music", original: "Original audio", reused_original: "Reused audio"}[m.type] || nice(m.type)),
        h("td", {class: "num"}, num(m.outliers)), h("td", {class: "num"}, num(m.reels)), h("td", m.example_reel ? h("a", {href: "#reel/" + m.example_reel.id}, "Open") : "—")))))));
  }
  view.append(savesLine());
  clear(main, view);
}

function likely(lift) {   // 0.3x is "less likely", never "0.3x more likely"; under 0.05x is "rarely", never "0x"
  if (lift != null && lift < 0.05) return "Rarely an outlier";
  return lift != null && lift < 1 ? `Less likely to be an outlier (${x(lift)})` : `${x(lift)} more likely to be an outlier`;
}

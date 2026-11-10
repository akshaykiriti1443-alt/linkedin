/* Shared cards used by several pages: FormatCard, WorkingCard, data note, Signature (6.7.5). */
import {h, md, arr, x, num, day, nice, thumbStyle, buildOn, quick, pill, savesLine, vfTag} from "../app.js";

export function canMakeLine(cm) {
  if (!cm) return null;
  return cm.ok ? pill("You can make this", "ok") : h("span", {class: "pill warn", title: cm.reason || ""}, cm.reason || "Needs something you said you can't do");
}
export function evidencePill(ev, trend) {
  return [ev ? pill(nice(ev) + " evidence", ev === "strong" ? "ok" : ev === "thin" ? "" : "busy") : null,
    trend === "rising" ? pill("Rising", "ok") : trend === "fading" ? pill("Fading", "warn") : null];
}
export function thumbs(list) {
  return h("div", {class: "thumbs"}, arr(list).slice(0, 6).map(r => h("a", {href: "#reel/" + r.id, style: thumbStyle(r.thumb),
    title: r.score ? x(r.score) + " normal views" : "Open the reel", "aria-label": "Open the reel"}, r.score ? h("span", x(r.score)) : null)));
}

/** A named sub-format: the Home page's main card (6.9). */
export function formatCard(f) {
  return h("article", {class: "fcard"},
    h("div", {class: "row"}, vfTag(f.vf, f.vf_label), ...evidencePill(f.evidence, f.trend)),
    h("a", {class: "name", href: f.href || "#format/" + f.id}, f.name || f.vf_label || "A format that works"),
    h("p", {class: "line"}, f.line || ""),
    thumbs(f.examples),
    h("div", {class: "row"}, canMakeLine(f.can_make)),
    h("div", {class: "row"},
      h("button", {type: "button", class: "sm", onclick: e => quick(e.currentTarget, {action: "use_format", format_id: f.id})}, "Write 3 scripts in this format"),
      h("a", {class: "btn ghost sm", href: f.href || "#format/" + f.id}, "Open the playbook")));
}

/** One "what works" finding (WorkingCard). */
export function workingCard(c) {
  const isVf = c.dimension === "visual_format";
  return h("article", {class: "card"},
    h("div", {class: "row"}, h("span", {class: "label"}, DIM_LABEL[c.dimension] || nice(c.dimension)), ...evidencePill(c.evidence, c.trend)),
    h("h3", c.title),
    h("p", {class: "line"}, c.line),
    thumbs(c.best_reels),
    h("div", {class: "row"}, canMakeLine(c.can_make)),
    isVf ? buildOn({vf: c.value}, {only: ["use_format"]}) :
      c.best_reels && c.best_reels[0] ? buildOn({reel_id: c.best_reels[0].id}) : null);
}

export const DIM_LABEL = {
  visual_format: "Visual format", sub_format: "Named format", sound: "Sound", hook_type: "Hook", cta_type: "Call to action",
  topic: "Topic", length: "Length", cut_pace: "Cut pace", caption_style: "Captions", text_hook: "Text hook",
  headline_box: "Headline box", face_bubble: "Face bubble", broll_share: "B-roll", setting: "Setting",
};

export function dataNote(n) {
  if (!n) return null;
  const ex = n.excluded || {};
  const bits = [];
  if (n.reels_pulled !== undefined) bits.push(`${num(n.reels_pulled)} reels pulled`);
  if (n.reels_scored !== undefined) bits.push(`${num(n.reels_scored)} scored`);
  if (n.tagged !== undefined) bits.push(`${num(n.tagged)} tagged`);
  if (n.priority !== undefined) bits.push(`${num(n.priority)} top reels and ${num(n.sample || 0)} normal reels studied`);
  const exText = Object.entries(ex).filter(([, v]) => v).map(([k, v]) => `${v} ${EXCLUDED[k] || nice(k).toLowerCase()}`);
  return h("details", {class: "fold card"},
    h("summary", "About this data"),
    h("div", {class: "stack tight"},
      bits.length ? h("p", bits.join(" · ")) : null,
      n.date_range ? h("p", `Reels from ${day(n.date_range[0])} to ${day(n.date_range[1])}.`) : null,
      exText.length ? h("p", "Left out: " + exText.join(", ") + ".") : null,
      n.sample_line ? h("p", n.sample_line) : null,
      savesLine(n.saves_line)));
}
const EXCLUDED = {too_new: "too new to judge", pinned: "pinned", hidden: "with hidden counts", hidden_counts: "with hidden counts",
  collab: "collabs", untagged_priority: "top reels not tagged yet", unverified_visual: "with unchecked visuals",
  older_than_window: "older than a year"};

export function signatureCard(s) {
  return h("article", {class: "card"},
    h("h3", h("a", {href: "#competitor/" + s.account_id}, "@" + s.handle)),
    h("p", {class: "line"}, md(s.line || "")),
    h("dl", {class: "kv"},
      s.top_format ? [h("dt", "Posts most"), h("dd", `${s.top_format_label || s.top_format} (${Math.round((s.top_format_share || 0) * 100)}%)`)] : null,
      s.top_sub_format_name ? [h("dt", "Top format"), h("dd", h("a", {href: "#format/" + s.top_sub_format}, s.top_sub_format_name))] : null,
      s.best_hook_type ? [h("dt", "Best hook"), h("dd", nice(s.best_hook_type))] : null,
      s.median_length_s ? [h("dt", "Typical length"), h("dd", `${s.median_length_s} s`)] : null,
      s.cut_pace ? [h("dt", "Cut pace"), h("dd", nice(s.cut_pace))] : null,
      arr(s.top_tracks).length ? [h("dt", "Music"), h("dd", arr(s.top_tracks).map(t => t.title + (t.artist ? ` (${t.artist})` : "")).join(", "))] : null));
}

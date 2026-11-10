/* #reels (filters, purpose labels) and #reel/<id> (video, frames, tabs, Fix tags, build-on buttons) (4.9). */
import {h, clear, msg, say, busy, get, post, patch, arr, S, field, select, toast, pollSoon, onJobsDone, num, full,
  day, x, mmss, nice, empty, savesLine, pill, bandChip, vfTag, reelCard, buildOn, backLink, qs, tabs, thumbStyle,
  PURPOSES, copyBtn} from "../app.js";

let taxo = null;
export async function taxonomy() {
  if (taxo) return taxo;
  const r = await get("/api/taxonomy");
  taxo = r.ok ? r.data : {visual_format: [], sound_design: [], hook_types: [], cta_types: [], caption_styles: [], broll_share: [], setting: [], framing: []};
  return taxo;
}
const lab = (list, id) => { const f = arr(list).find(i => i.id === id); return f ? f.label : nice(id); };

const BANDS = [["", "All reels"], ["breakout,big,outlier", "Outliers (3x or more)"], ["breakout", "Breakouts (10x or more)"],
  ["big", "Big outliers"], ["above", "Above normal"], ["normal", "Normal"], ["below", "Below normal"], ["growing", "Still growing"]];
const SORTS = [["score", "Best first"], ["newest", "Newest first"], ["views", "Most views"]];
const FILTER_KEYS = ["account", "band", "format", "sub_format", "sound", "topic", "hook_type", "q", "starred", "recent", "purpose", "sort", "min_score"];

export async function list(main, {query, token}) {
  const f = Object.fromEntries(FILTER_KEYS.map(k => [k, query.get(k) || ""]));
  const [t, ar, fr, tr] = await Promise.all([taxonomy(), get("/api/accounts"), get("/api/formats"), get("/api/topics")]);
  if (S.view !== token) return;
  const accounts = ar.ok ? [...arr(ar.data.accounts), ...(ar.data.self ? [ar.data.self] : [])] : [];
  const formats = fr.ok ? arr(fr.data.formats) : [];
  const topics = tr.ok ? arr(tr.data.topics) : [];
  const grid = h("div", {class: "cards", id: "reel-grid"});
  const counts = h("p", {class: "muted", id: "reel-counts"});
  const more = h("div", {class: "row"});
  const chip = h("div");
  const setF = (k, v) => {
    f[k] = v;
    const hash = "#reels" + qs(Object.fromEntries(Object.entries(f).filter(([, val]) => val)));
    history.replaceState(null, "", hash);
    load(null);
  };
  const sel = (k, options, label) => {
    const s = select(options, f[k], {"aria-label": label, onchange: e => setF(k, e.target.value)});
    return s;
  };
  const search = h("input", {type: "search", placeholder: "Search captions", value: f.q, "aria-label": "Search captions"});
  let tmr = 0;
  search.addEventListener("input", () => { clearTimeout(tmr); tmr = setTimeout(() => setF("q", search.value.trim()), 400); });
  const purposeChips = h("div", {class: "chips", role: "group", "aria-label": "Purpose"}, PURPOSES.map(([id, label]) =>
    h("button", {type: "button", class: "pchip p-" + id, "aria-pressed": String(f.purpose === id), onclick: e => {
      const on = f.purpose !== id;
      purposeChips.querySelectorAll(".pchip").forEach(b => b.setAttribute("aria-pressed", "false"));
      e.currentTarget.setAttribute("aria-pressed", String(on));
      setF("purpose", on ? id : "");
    }}, label)));
  const filters = h("div", {class: "filters"},
    search,
    sel("account", [["", "All competitors"], ...accounts.map(a => [a.id, "@" + a.handle + (a.role === "self" ? " (you)" : "")])], "Competitor"),
    sel("band", BANDS, "How well it did"),
    sel("format", [["", "Any format"], ...arr(t.visual_format).map(v => [v.id, v.label])], "Visual format"),
    formats.length ? sel("sub_format", [["", "Any named format"], ...formats.map(fm => [fm.id, fm.name || fm.vf_label])], "Named format") : null,
    sel("sound", [["", "Any sound"], ...arr(t.sound_design).map(v => [v.id, v.label])], "Sound"),
    sel("topic", [["", "Any topic"], ...topics.map(tp => [tp.slug, tp.label]), ["unsorted", "Unsorted"]], "Topic"),
    sel("hook_type", [["", "Any hook"], ...arr(t.hook_types).map(v => [v.id, v.label])], "Hook"),
    sel("sort", SORTS, "Sort"),
    h("label", {class: "check"}, h("input", {type: "checkbox", checked: f.recent === "1", onchange: e => setF("recent", e.target.checked ? "1" : "")}), "Last 90 days"),
    h("label", {class: "check"}, h("input", {type: "checkbox", checked: f.starred === "1", onchange: e => setF("starred", e.target.checked ? "1" : "")}), "Kept only"));
  let cursor = null;
  const load = async c => {
    const params = Object.assign({}, Object.fromEntries(Object.entries(f).filter(([, v]) => v)), {limit: 40});
    if (c) params.cursor = c;
    const r = await get("/api/reels" + qs(params));
    if (S.view !== token) return;
    if (!r.ok) { clear(grid, empty(r.error)); return; }
    const d = r.data;
    if (!c) clear(grid);
    arr(d.reels).forEach(rr => grid.append(reelCard(rr)));
    if (!c && !arr(d.reels).length) {
      grid.append(accounts.length ? empty("No reels match these filters. Try fewer filters.") :
        empty("No competitors yet. Add 1-2 accounts whose reels work in your niche, and we'll find more.", h("a", {class: "btn", href: "#competitors"}, "Add competitors")));
    }
    const k = d.counts || {};
    counts.textContent = `${full(k.total || 0)} reels · ${full((k.outlier || 0) + (k.big || 0) + (k.breakout || 0))} outliers`;
    const pl = d.pipeline || {};
    clear(chip, pl.left_to_tag || pl.failed_tags ? h("span", {class: "pill busy"}, pl.line || `Tags coming: ${num(pl.left_to_tag)} reels left` + (pl.eta ? ` · ${pl.eta}` : "")) : null);
    cursor = d.next_cursor;
    clear(more, cursor ? h("button", {type: "button", class: "ghost", onclick: e => busy(e.currentTarget, () => load(cursor))}, "Show more reels") : null);
  };
  clear(main, h("div", {class: "view"},
    h("div", {class: "headrow"}, h("h1", "Reels"), chip),
    filters, purposeChips, counts, grid, more, savesLine()));
  await load(null);
}

// ============================================================================================ reel page
const TABS = [["why", "Why it worked"], ["format", "Format"], ["hook", "Hook"], ["sound", "Sound"], ["weak", "Weak spots"], ["script", "Script"], ["transcript", "Transcript"]];

export async function detail(main, {parts, query, token}) {
  const id = parts[0];
  const [r, t] = await Promise.all([get("/api/reels/" + encodeURIComponent(id || "")), taxonomy()]);
  if (S.view !== token) return;
  if (!r.ok) { clear(main, backLink("#reels", "Reels"), h("h1", "Reel"), empty(r.error)); return; }
  const reel = r.data.reel;
  const reload = () => detail(main, {parts, query, token});
  const deep = reel.deep || null;
  const quickT = reel.quick || null;
  const sig = reel.signals || null;
  const media = reel.media || {};
  const eff = reel.effective || {};
  // Media
  let video = null;
  if (media.video) {
    video = h("video", {class: "media", controls: true, preload: "metadata", playsinline: true, poster: media.cover || reel.thumb || null, src: media.video});
  }
  const seek = sec => {
    if (!video) { toast("The video isn't here yet. Open it on Instagram."); return; }
    video.currentTime = Math.max(0, Number(sec) || 0);
    video.play().catch(() => {});
    video.scrollIntoView({block: "nearest"});
  };
  const ts = sec => (sec === null || sec === undefined || sec === "") ? null :
    (video ? h("button", {type: "button", class: "ts", onclick: () => seek(sec), title: "Play from here"}, mmss(sec)) : h("span", {class: "ts"}, mmss(sec)));
  const cover = video || h("div", {class: "media", style: thumbStyle(media.cover || reel.thumb), role: "img", "aria-label": "Reel cover"});
  const frames = arr(media.frames);
  const left = h("div", {class: "reel-left"}, cover,
    frames.length ? h("div", {class: "frames", "aria-label": "Key frames"}, frames.map(fr => h("button", {type: "button", class: fr.kind === "hook" ? "hook" : "",
      style: thumbStyle(fr.url), title: `Frame at ${mmss(fr.t)}`, "aria-label": `Frame at ${mmss(fr.t)}`, onclick: () => seek(fr.t)}, h("span", mmss(fr.t))))) : null,
    h("a", {class: "btn ghost", href: reel.url, target: "_blank", rel: "noopener noreferrer"}, "Open on Instagram"),
    !video && reel.states && reel.states.media !== "have" ? h("p", {class: "hint"}, "The video is not saved here yet. Breaking it down gets it first.") : null);
  // Header
  const starBtn = h("button", {type: "button", class: "ghost sm", "aria-pressed": String(!!reel.starred), onclick: async e => {
    const res = await busy(e.currentTarget, () => patch(`/api/reels/${reel.id}`, {starred: !reel.starred}));
    if (res.ok) { reel.starred = !reel.starred; e.currentTarget.textContent = reel.starred ? "Kept" : "Keep this reel"; e.currentTarget.setAttribute("aria-pressed", String(reel.starred)); toast(reel.starred ? "Kept. We won't delete its video." : "Not kept any more."); }
    else toast(res.error);
  }}, reel.starred ? "Kept" : "Keep this reel");
  const deepState = (reel.states || {}).deep;
  const analyzeBtn = !deep ? h("button", {type: "button", class: "ghost", id: "analyze-reel", disabled: deepState === "queued" || deepState === "waiting", onclick: async e => {
    const res = await busy(e.currentTarget, () => post(`/api/reels/${reel.id}/analyze`, {}));
    toast(res.ok ? "Breaking it down. This takes a few minutes." : res.error); if (res.ok) { pollSoon(); reload(); }
  }}, deepState === "queued" || deepState === "waiting" ? "Breakdown coming" : "Get the full breakdown") : null;
  const tg = reel.tags || {};
  const head = h("section", {class: "panel"},
    h("div", {class: "row"}, h("a", {href: "#competitor/" + (reel.account || {}).id}, "@" + (reel.account || {}).handle), bandChip(reel),
      reel.is_collab ? pill("Collab") : null, reel.tier === "priority" ? pill("Top reel") : reel.tier === "sample" ? pill("Normal reel (sample)") : null),
    h("h1", (deep && deep.title) || reel.caption_first_line || "Reel"),
    h("p", {class: "line"}, reel.why || ""),
    h("div", {class: "nums"},
      h("div", null, h("b", x(reel.score)), "normal views"), h("div", null, h("b", num(reel.views)), "views"),
      h("div", null, h("b", num(reel.likes)), "likes"), h("div", null, h("b", num(reel.comments)), "comments"),
      reel.shares !== null && reel.shares !== undefined ? h("div", null, h("b", num(reel.shares)), "shares") : null,
      h("div", null, h("b", reel.duration ? mmss(reel.duration) : "—"), "long"), h("div", null, h("b", day(reel.taken_at)), "posted")),
    h("div", {class: "row"}, vfTag(tg.visual_format, tg.visual_format_label), tg.format ? h("a", {href: "#format/" + tg.format.id}, tg.format.name) : null,
      tg.sound_label ? pill(tg.sound_label) : null, tg.hook_type ? pill(lab(t.hook_types, tg.hook_type)) : null,
      arr(tg.topics).map(tp => pill(tp)), tg.unverified ? pill("Visual tag not checked", "warn") : null),
    buildOn({reel}, {size: ""}),
    h("div", {class: "row"}, starBtn, analyzeBtn),
    savesLine(r.data.saves_line));
  // Tabs
  const cur = TABS.some(tb => tb[0] === query.get("tab")) ? query.get("tab") : "why";
  const body = h("section", {class: "panel", id: "reel-tab"});
  const bar = tabs(TABS.map(([i, l]) => [i, l]), cur, pick => {
    history.replaceState(null, "", `#reel/${reel.id}?tab=${pick}`);
    bar.querySelectorAll("[role=tab]").forEach((b, n) => b.setAttribute("aria-selected", String(TABS[n][0] === pick)));
    drawTab(pick);
  });
  const noDeep = what => h("div", {class: "stack"}, h("p", {class: "muted"}, `The ${what} comes with the full breakdown.`), analyzeBtn ? analyzeBtn.cloneNode(true) : null);
  const drawTab = which => {
    clear(body);
    if (which === "why") {
      if (deep) body.append(h("p", deep.summary || ""), bullets("Why it worked", deep.why_it_worked), bullets("What to steal", deep.what_to_steal), bullets("What to avoid", deep.what_to_avoid),
        deep.skeleton && deep.skeleton.structure_name ? h("p", null, h("b", "Structure: "), deep.skeleton.structure_name) : null);
      else body.append(quickT && quickT.one_line ? h("p", quickT.one_line) : null, noDeep("full story"));
    } else if (which === "format") {
      const fmt = (deep && deep.format) || {};
      const ed = (deep && deep.edit) || {};
      const es = (sig && sig.edit) || {};
      const vs = (sig && sig.visual) || {};
      body.append(h("dl", {class: "kv"},
        h("dt", "Visual format"), h("dd", vfTag(eff.visual_format, lab(t.visual_format, eff.visual_format)) || "—"),
        tg.format ? [h("dt", "Named format"), h("dd", h("a", {href: "#format/" + tg.format.id}, tg.format.name))] : null,
        h("dt", "Headline box"), h("dd", eff.headline_box === true ? "Yes" : eff.headline_box === false ? "No" : "—"),
        h("dt", "Captions"), h("dd", eff.caption_style ? lab(t.caption_styles, eff.caption_style) + (ed.caption_look ? ` (${ed.caption_look})` : "") : "—"),
        h("dt", "Face bubble"), h("dd", eff.face_bubble === true ? "Yes" : eff.face_bubble === false ? "No" : "—"),
        h("dt", "B-roll"), h("dd", eff.broll_share ? nice(eff.broll_share) : "—"),
        h("dt", "Setting"), h("dd", eff.setting ? nice(eff.setting) : "—"),
        h("dt", "Framing"), h("dd", eff.framing ? nice(eff.framing) : "—"),
        es.cuts_per_10s !== undefined ? [h("dt", "Cuts"), h("dd", `${es.cuts_per_10s} every 10 s · a shot lasts about ${es.avg_shot_s} s`)] : null,
        vs.text_frames_share !== undefined && vs.text_frames_share !== null ? [h("dt", "Text on screen"), h("dd", `in ${Math.round(vs.text_frames_share * 100)}% of frames`)] : null,
        vs.face_share !== undefined && vs.face_share !== null ? [h("dt", "Face on screen"), h("dd", `in ${Math.round(vs.face_share * 100)}% of frames`)] : null),
        fmt.why ? h("p", null, h("b", "How we know: "), fmt.why) : null, fmt.recipe ? h("p", null, h("b", "Recipe: "), fmt.recipe) : null,
        buildOn({reel}, {only: ["use_format"]}));
    } else if (which === "hook") {
      const hk = (deep && deep.skeleton && deep.skeleton.hook) || (quickT && quickT.hook_bank) || null;
      if (!hk) { body.append(noDeep("hook")); return; }
      body.append(h("div", {class: "stack"},
        hk.spoken ? h("p", {class: "quote"}, "“" + hk.spoken + "”") : null,
        h("dl", {class: "kv"}, hk.on_screen_text ? [h("dt", "On screen"), h("dd", hk.on_screen_text)] : null, hk.visual ? [h("dt", "What we see"), h("dd", hk.visual)] : null,
          hk.hook_type ? [h("dt", "Hook type"), h("dd", lab(t.hook_types, hk.hook_type))] : null,
          hk.ends_at_seconds ? [h("dt", "Hook ends at"), h("dd", ts(hk.ends_at_seconds))] : null),
        hk.why_it_works ? h("p", null, h("b", "Why it works: "), hk.why_it_works) : null,
        deep && deep.hook_formula ? h("div", {class: "stack tight"}, h("span", {class: "label"}, "Formula"), h("div", {class: "copyline"}, h("div", {class: "formula"}, deep.hook_formula), copyBtn(deep.hook_formula))) : null,
        buildOn({reel}, {only: ["borrow_hook"]})));
    } else if (which === "sound") {
      body.append(soundTab(reel, sig, deep, t, seek, ts));
    } else if (which === "weak") {
      const w = deep ? arr(deep.weaknesses) : [];
      if (!deep) { body.append(noDeep("list of weak spots")); return; }
      body.append(w.length ? h("div", {class: "stack"}, w.map(wk => h("div", {class: "weak"}, h("div", {class: "row"}, h("b", WEAK_LABEL[wk.kind] || nice(wk.kind)), ts(wk.at_s)), h("p", wk.note)))) :
        h("p", {class: "muted"}, "No weak spots found."), h("p", {class: "hint"}, "Remake this fixes these weak spots in your version."), buildOn({reel}, {only: ["remake"]}));
    } else if (which === "script") {
      const sk = deep && deep.skeleton;
      if (!sk) { body.append(noDeep("script outline")); return; }
      body.append(h("div", {class: "stack"},
        sk.hook ? h("div", {class: "part hook"}, h("span", {class: "label"}, "Hook"), h("div", {class: "row"}, ts(0), h("span", sk.hook.spoken || ""))) : null,
        arr(sk.meat).length ? h("div", {class: "part meat"}, h("span", {class: "label"}, "Body"), arr(sk.meat).map(m => h("div", {class: "beat"}, ts(m.start_seconds), h("div", null, m.point, h("span", {class: "v"}, [m.visual, m.purpose].filter(Boolean).join(" · ")))))) : null,
        sk.cta ? h("div", {class: "part cta"}, h("span", {class: "label"}, "Call to action"), h("p", sk.cta.text || ""), sk.cta.keyword ? h("p", {class: "hint"}, "Keyword: " + sk.cta.keyword) : null) : null,
        deep.fill_in_template ? h("div", {class: "stack tight"}, h("span", {class: "label"}, "Fill-in template"), h("div", {class: "copyline"}, h("div", {class: "formula"}, deep.fill_in_template), copyBtn(deep.fill_in_template))) : null));
    } else if (which === "transcript") {
      const tr = (sig && sig.transcript) || {};
      const segs = arr(tr.segments);
      body.append(segs.length ? h("div", {class: "beats"}, segs.map(sg => h("div", {class: "beat"}, ts(sg.start), h("span", sg.text)))) :
        tr.text ? h("p", tr.text) : h("p", {class: "muted"}, sig ? "No speech found in this reel." : "The transcript comes once the reel is measured."),
      h("div", {class: "stack tight"}, h("span", {class: "label"}, "Caption"), h("p", {style: {whiteSpace: "pre-wrap"}}, reel.caption || "")));
    }
  };
  drawTab(cur);
  onJobsDone(() => { if (!main.querySelector("input:focus,textarea:focus,select:focus") && !(video && !video.paused)) reload(); });
  clear(main, h("div", {class: "view"}, backLink("#reels", "Reels"),
    h("div", {class: "reel"}, left, h("div", {class: "reel-right"}, head, h("div", {class: "stack"}, bar, body), fixTags(reel, t, reload)))));
}

function bullets(title, items) {
  items = arr(items);
  if (!items.length) return null;
  return h("div", {class: "blk"}, h("span", {class: "label"}, title), h("ul", items.map(i => h("li", i))));
}

function soundTab(reel, sig, deep, t, seek, ts) {
  const box = h("div", {class: "stack"});
  const snd = (sig && sig.sound) || {};
  const dur = Number((sig && sig.edit && sig.edit.duration) || snd.duration || reel.duration || 0);
  const ds = (deep && deep.sound) || {};
  const au = reel.audio || {};
  box.append(h("dl", {class: "kv"},
    h("dt", "Sound pattern"), h("dd", ds.primary ? lab(t.sound_design, ds.primary) : (reel.tags || {}).sound_label || "—"),
    arr(ds.secondary).length ? [h("dt", "Also"), h("dd", arr(ds.secondary).map(s => lab(t.sound_design, s)).join(", "))] : null,
    h("dt", "Audio"), h("dd", audioLine(au))),
    ds.takeaway ? h("p", null, h("b", "Takeaway: "), ds.takeaway) : null);
  if (sig && dur > 0) {
    const tl = h("div", {class: "timeline", role: "group", "aria-label": "Sound cues on the timeline"});
    arr(sig.cuts).forEach(c => tl.append(h("span", {class: "cut", style: {left: (100 * c / dur) + "%"}})));
    const cues = [...arr(snd.events).map(e => ({t: e.t, label: e.label, kind: "sfx"})), ...arr(ds.cues).map(c => ({t: c.t ?? c.at_s ?? c.start, label: c.what || c.kind, kind: c.kind === "music" ? "music" : "sfx"}))]
      .filter(c => typeof c.t === "number" && c.t >= 0 && c.t <= dur);
    cues.forEach(c => tl.append(h("button", {type: "button", class: "cue " + c.kind, style: {left: (100 * c.t / dur) + "%"}, title: `${nice(c.label)} at ${mmss(c.t)}`, "aria-label": `${nice(c.label)} at ${mmss(c.t)}: play`, onclick: () => seek(c.t)})));
    arr(snd.silences).forEach(s => { const st = s.start ?? s[0]; if (typeof st === "number") tl.append(h("span", {class: "cue silence", style: {left: (100 * st / dur) + "%"}, title: "Silence"})); });
    box.append(h("span", {class: "label"}, "Timeline"), tl,
      h("div", {class: "legend"}, h("span", null, h("i", {style: {background: "var(--muted)"}}), "cuts"), h("span", null, h("i", {style: {background: "var(--p-hook)"}}), "sound effects"),
        h("span", null, h("i", {style: {background: "var(--p-format)"}}), "music")),
      cues.length ? h("div", {class: "beats"}, cues.slice(0, 30).map(c => h("div", {class: "beat"}, ts(c.t), h("span", nice(c.label))))) : h("p", {class: "muted"}, "No sound effects found."));
  } else {
    box.append(h("p", {class: "muted"}, "The sound map comes once the reel is measured."));
  }
  return box;
}

function fixTags(reel, t, reload) {
  const eff = reel.effective || {};
  const ov = reel.overrides || {};
  const out = msg();
  const yn = v => v === true ? "yes" : v === false ? "no" : "";
  const vf = select([["", "—"], ...arr(t.visual_format).map(v => [v.id, v.label])], eff.visual_format, {id: "fix-vf"});
  const snd = select([["", "—"], ...arr(t.sound_design).map(v => [v.id, v.label])], eff.sound_primary, {id: "fix-sound"});
  const hook = select([["", "—"], ...arr(t.hook_types).map(v => [v.id, v.label])], eff.hook_type, {id: "fix-hook"});
  const cta = select([["", "—"], ...arr(t.cta_types).map(v => [v.id, v.label])], eff.cta_type, {id: "fix-cta"});
  const cap = select([["", "—"], ...arr(t.caption_styles).map(v => [v.id, v.label])], eff.caption_style, {id: "fix-captions"});
  const hb = select([["", "—"], ["yes", "Yes"], ["no", "No"]], yn(eff.headline_box), {id: "fix-headline"});
  const fb = select([["", "—"], ["yes", "Yes"], ["no", "No"]], yn(eff.face_bubble), {id: "fix-bubble"});
  const br = select([["", "—"], ...arr(t.broll_share).map(v => [v.id, v.label])], eff.broll_share, {id: "fix-broll"});
  const se = select([["", "—"], ...arr(t.setting).map(v => [v.id, v.label])], eff.setting, {id: "fix-setting"});
  const fr = select([["", "—"], ...arr(t.framing).map(v => [v.id, v.label])], eff.framing, {id: "fix-framing"});
  const topics = h("input", {id: "fix-topics", value: arr(eff.topics).join(", "), placeholder: "topic-slug, another-topic"});
  const save = async btn => {
    const o = {};
    const pick = (k, el, conv = v => v) => { const v = el.value; const cur = eff[k]; const nv = v === "" ? null : conv(v); if (nv !== null && nv !== cur) o[k] = nv; };
    pick("visual_format", vf); pick("sound_primary", snd); pick("hook_type", hook); pick("cta_type", cta); pick("caption_style", cap);
    pick("headline_box", hb, v => v === "yes"); pick("face_bubble", fb, v => v === "yes"); pick("broll_share", br); pick("setting", se); pick("framing", fr);
    const tps = topics.value.split(",").map(s => s.trim()).filter(Boolean);
    if (tps.join(",") !== arr(eff.topics).join(",")) o.topics = tps;
    if (!Object.keys(o).length) { say(out, {ok: true}, "Nothing changed."); return; }
    const r = await busy(btn, () => patch(`/api/reels/${reel.id}`, {overrides: o}));
    say(out, r, "Saved. What works updates in a minute.");
    if (r.ok) setTimeout(reload, 700);
  };
  return h("details", {class: "fold panel", id: "fix-tags"}, h("summary", "Fix tags"),
    h("div", {class: "stack"}, h("p", {class: "hint"}, "Wrong tag? Fix it here. Your fix wins over the AI and counts in What works." + (Object.keys(ov).length ? " You fixed some tags already." : "")),
      h("div", {class: "grid2"}, field("Visual format", vf), field("Sound", snd), field("Hook", hook), field("Call to action", cta), field("Captions", cap),
        field("Headline box", hb), field("Face bubble", fb), field("B-roll", br), field("Setting", se), field("Framing", fr)),
      field("Topics", topics, "Use the topic short names from Settings > Topics, with commas between them."),
      out, h("div", {class: "row"}, h("button", {type: "button", id: "fix-save", onclick: e => save(e.currentTarget)}, "Save my fixes"))));
}

function audioLine(au) {   // "Original audio (original audio)" read twice; say the kind only once
  if (!au || !au.title) return "—";
  const kind = au.type === "licensed" ? "licensed music" : au.is_reused ? "reused audio" : "original audio";
  const name = au.title + (au.artist ? " · " + au.artist : "");
  return au.title.trim().toLowerCase() === kind ? name : `${name} (${kind})`;
}

const WEAK_LABEL = {slow_hook: "Slow hook", weak_visual_hook: "Weak first frame", no_proof: "No proof",
  generic_cta: "Weak call to action", long_middle: "Long middle", no_rehook: "Nothing pulls you back in",
  unclear_offer: "Unclear offer", hard_to_follow: "Hard to follow", poor_audio: "Poor sound", dated_look: "Looks dated",
  other: "Other"};

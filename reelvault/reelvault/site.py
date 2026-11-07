"""Build vault/index.html: one self-contained page to browse, filter and copy from."""

from __future__ import annotations

import json
from pathlib import Path

from .library import Library


def _slim(rec: dict) -> dict:
    """Drop bulky fields the page does not need."""
    tr = rec.get("transcript", {})
    return {
        "id": rec["shortcode"],
        "folder": rec["folder"].replace("\\", "/"),
        "meta": {k: rec["meta"].get(k) for k in ("owner", "url", "caption", "likes", "views")},
        "duration": rec.get("duration"),
        "thumb": rec["frames"][min(1, len(rec["frames"]) - 1)]["file"] if rec.get("frames") else None,
        "transcript": tr.get("text", ""),
        "segments": tr.get("segments", []),
        "a": rec["analysis"],
    }


def build(lib: Library) -> Path:
    data = [_slim(r) for r in lib.all_done()]
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = lib.root / "index.html"
    out.write_text(PAGE.replace("/*__DATA__*/[]", blob), encoding="utf-8")
    return out


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reel Vault</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--accent:#d6336c;--hook:#e8590c;--meat:#2f9e44;--cta:#1c7ed6;--chip:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--panel:#1d1d20;--ink:#ececef;--muted:#9a9aa2;--line:#2c2c31;--chip:#26262b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
h1{font-size:18px;margin:0 0 8px}h1 small{color:var(--muted);font-weight:400}
.bar{display:flex;flex-wrap:wrap;gap:8px}
input,select{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
input[type=search]{flex:1 1 220px}
main{padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column}
.card:hover{border-color:var(--accent)}
.thumb{aspect-ratio:9/12;background:#000 center/cover no-repeat}
.body{padding:10px 12px;display:flex;flex-direction:column;gap:6px}
.title{font-weight:600;line-height:1.3}.sub{color:var(--muted);font-size:13px}
.hookline{font-size:13px;border-left:3px solid var(--hook);padding-left:8px}
.chips{display:flex;flex-wrap:wrap;gap:4px}.chip{background:var(--chip);border-radius:99px;padding:1px 8px;font-size:12px;color:var(--muted)}
.empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px 0}
dialog{width:min(1100px,96vw);max-height:92vh;border:1px solid var(--line);border-radius:14px;padding:0;background:var(--panel);color:var(--ink)}
dialog::backdrop{background:#0008}
.dlg{display:grid;grid-template-columns:320px 1fr;min-height:0}
@media (max-width:760px){.dlg{grid-template-columns:1fr}}
.left{padding:16px;border-right:1px solid var(--line)}
video{width:100%;border-radius:10px;background:#000;max-height:60vh}
.right{padding:16px 20px;overflow:auto;max-height:90vh}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:12px;position:sticky;top:-16px;background:var(--panel);padding:8px 0}
.tabs button{font:inherit;border:1px solid var(--line);background:var(--chip);color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer}
.tabs button.on{background:var(--ink);color:var(--panel)}
.sec{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.sec h3{margin:0 0 6px;font-size:14px;text-transform:uppercase;letter-spacing:.04em}
.hook h3{color:var(--hook)}.meat h3{color:var(--meat)}.cta h3{color:var(--cta)}
.beat{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:6px 0;border-top:1px dashed var(--line)}
.beat:first-of-type{border-top:0}.ts{color:var(--muted);font-variant-numeric:tabular-nums;cursor:pointer}
pre{white-space:pre-wrap;background:var(--chip);padding:10px;border-radius:8px;font:13px/1.5 ui-monospace,Menlo,monospace}
.copy{float:right;font-size:12px;border:1px solid var(--line);background:none;color:var(--muted);border-radius:6px;cursor:pointer;padding:2px 8px}
.x{position:absolute;right:10px;top:10px;z-index:3;width:32px;height:32px;line-height:1;font-size:20px;background:var(--chip);border:1px solid var(--line);border-radius:50%;color:var(--ink);cursor:pointer}
ul,ol{margin:4px 0;padding-left:20px}a{color:var(--accent)}
</style>
</head>
<body>
<header>
  <h1>Reel Vault <small id="count"></small></h1>
  <div class="bar">
    <input type="search" id="q" placeholder="Search titles, transcripts, hooks, tags…">
    <select id="f-cat"><option value="">All categories</option></select>
    <select id="f-hook"><option value="">All hook types</option></select>
    <select id="f-fmt"><option value="">All formats</option></select>
    <select id="f-cta"><option value="">All CTA types</option></select>
    <select id="f-st"><option value="">All structures</option></select>
  </div>
</header>
<main id="grid"></main>
<dialog id="dlg"><button class="x" onclick="dlg.close()" aria-label="Close">×</button><div class="dlg" id="dlgbody"></div></dialog>
<script>
const DATA = /*__DATA__*/[];
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const src = (r, f) => encodeURI(r.folder + "/" + f);
const filters = [["#f-cat", r => r.a.category], ["#f-hook", r => r.a.skeleton.hook.hook_type],
  ["#f-fmt", r => r.a.content_format], ["#f-cta", r => r.a.skeleton.cta.cta_type], ["#f-st", r => r.a.skeleton.structure_name]];
for (const [sel, get] of filters) {
  const counts = {}; DATA.forEach(r => counts[get(r)] = (counts[get(r)] || 0) + 1);
  Object.entries(counts).sort((a, b) => b[1] - a[1]).forEach(([v, n]) => $(sel).insertAdjacentHTML("beforeend", `<option value="${esc(v)}">${esc(v)} (${n})</option>`));
  $(sel).onchange = render;
}
$("#q").oninput = render;
const hay = r => [r.a.title, r.a.summary, r.a.tags.join(" "), r.transcript, r.meta.caption, r.meta.owner, r.a.skeleton.hook.spoken,
  r.a.skeleton.hook.on_screen_text, r.a.visuals.on_screen_text.join(" "), r.a.subcategory].join(" ").toLowerCase();
function render() {
  const q = $("#q").value.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const rows = DATA.filter(r => filters.every(([sel, get]) => !$(sel).value || get(r) === $(sel).value) && q.every(w => hay(r).includes(w)));
  $("#count").textContent = `${rows.length} of ${DATA.length} reels`;
  $("#grid").innerHTML = rows.length ? rows.map(r => `
    <article class="card" onclick="openReel('${r.id}')">
      <div class="thumb" style="background-image:url('${r.thumb ? src(r, r.thumb) : ""}')"></div>
      <div class="body">
        <div class="title">${esc(r.a.title)}</div>
        <div class="sub">@${esc(r.meta.owner)} · ${esc(r.a.category)}</div>
        <div class="hookline">${esc(r.a.skeleton.hook.spoken || r.a.skeleton.hook.on_screen_text)}</div>
        <div class="chips"><span class="chip">${esc(r.a.skeleton.hook.hook_type)}</span><span class="chip">${esc(r.a.content_format)}</span><span class="chip">${esc(r.a.skeleton.cta.cta_type)}</span></div>
      </div></article>`).join("") : `<div class="empty">No reels match. ${DATA.length ? "Clear a filter." : "Run <code>reelvault run</code> to fill the vault."}</div>`;
}
const list = xs => xs && xs.length ? `<ul>${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "<p>—</p>";
const copyBtn = t => `<button class="copy" data-t="${esc(t)}" onclick="navigator.clipboard.writeText(this.dataset.t);this.textContent='Copied'">Copy</button>`;
const seek = t => `onclick="document.querySelector('#vid').currentTime=${t};document.querySelector('#vid').play()"`;
const TABS = {
  Skeleton: r => { const s = r.a.skeleton, h = s.hook, c = s.cta; return `
    <p><b>Structure:</b> ${esc(s.structure_name)} · <b>Pacing:</b> ${esc(s.pacing)}</p>
    <div class="sec hook"><h3>Hook · ${esc(h.hook_type)} · 0–${h.ends_at_seconds}s ${copyBtn(h.spoken || h.on_screen_text)}</h3>
      <p><b>Said:</b> ${esc(h.spoken || "—")}<br><b>On screen:</b> ${esc(h.on_screen_text || "—")}<br><b>Visual:</b> ${esc(h.visual)}</p>
      <p><i>${esc(h.why_it_works)}</i></p></div>
    <div class="sec meat"><h3>Meat · ${s.meat.length} beats</h3>${s.meat.map(b => `
      <div class="beat"><span class="ts" ${seek(b.start_seconds)}>${b.start_seconds.toFixed(0)}s</span>
      <div><b>${esc(b.purpose)}:</b> ${esc(b.point)}<br><span class="sub">Visual: ${esc(b.visual)}</span></div></div>`).join("")}</div>
    <div class="sec cta"><h3>CTA · ${esc(c.cta_type)} · ${esc(c.placement)} ${copyBtn(c.text)}</h3><p>${esc(c.text || "—")}${c.keyword ? ` <span class="chip">keyword: ${esc(c.keyword)}</span>` : ""}</p></div>
    <div class="sec"><h3>Retention devices</h3>${list(s.retention_devices)}</div>
    <div class="sec"><h3>Why it performed</h3><p>${esc(r.a.why_it_performed)}</p></div>`; },
  "Summary & plan": r => `
    <div class="sec"><h3>Summary</h3><p>${esc(r.a.summary)}</p></div>
    <div class="sec"><h3>Key takeaways</h3>${list(r.a.key_takeaways)}</div>
    <div class="sec"><h3>Plan of action</h3><ol>${r.a.action_plan.map(s => `<li><b>${esc(s.step)}</b> <span class="chip">${esc(s.effort)}</span><br><span class="sub">${esc(s.why)}</span></li>`).join("")}</ol></div>
    <div class="sec"><h3>Quotes worth saving</h3>${list(r.a.notable_quotes)}</div>`,
  Visuals: r => { const v = r.a.visuals; return `
    <div class="sec"><h3>What the visuals say</h3><p>${esc(v.what_the_visuals_say)}</p></div>
    <div class="sec"><h3>Style</h3><p>${esc(v.visual_style)}</p></div>
    <div class="sec"><h3>Editing techniques</h3>${list(v.editing_techniques)}</div>
    <div class="sec"><h3>On-screen text</h3>${list(v.on_screen_text)}</div>`; },
  "Scripts for you": r => `
    <div class="sec"><h3>Fill-in template ${copyBtn(r.a.fill_in_template)}</h3><pre>${esc(r.a.fill_in_template)}</pre></div>
    <div class="sec"><h3>Hook variations</h3>${list(r.a.hook_variations)}</div>
    ${r.a.script_ideas.map(x => `<div class="sec"><h3>Idea: ${esc(x.title)}</h3><p><b>Hook:</b> ${esc(x.hook)}</p>${list(x.outline)}<p><b>CTA:</b> ${esc(x.cta)}</p></div>`).join("")}
    <div class="sec"><h3>Ready-to-shoot script ${copyBtn(r.a.ready_script)}</h3><pre>${esc(r.a.ready_script)}</pre></div>`,
  Transcript: r => `
    <div class="sec"><h3>Transcript ${copyBtn(r.transcript)}</h3>${r.segments.length ? r.segments.map(s => `<div class="beat"><span class="ts" ${seek(s.start)}>${s.start.toFixed(0)}s</span><div>${esc(s.text)}</div></div>`).join("") : "<p>No speech.</p>"}</div>
    <div class="sec"><h3>Original caption</h3><p style="white-space:pre-wrap">${esc(r.meta.caption || "—")}</p></div>`,
};
function openReel(id) {
  const r = DATA.find(x => x.id === id);
  $("#dlgbody").innerHTML = `
    <div class="left"><video id="vid" controls playsinline src="${src(r, "video.mp4")}" poster="${r.thumb ? src(r, r.thumb) : ""}"></video>
      <h2 style="font-size:17px;margin:10px 0 4px">${esc(r.a.title)}</h2>
      <div class="sub">@${esc(r.meta.owner)} · ${r.duration ?? "?"}s<br>${esc(r.a.category)} › ${esc(r.a.subcategory)}</div>
      <div class="chips" style="margin:8px 0">${r.a.tags.map(t => `<span class="chip">#${esc(t)}</span>`).join("")}</div>
      ${r.meta.url ? `<a href="${esc(r.meta.url)}" target="_blank" rel="noopener">Open on Instagram ↗</a> · ` : ""}<a href="${src(r, "note.md")}">note.md</a></div>
    <div class="right"><div class="tabs">${Object.keys(TABS).map((t, i) => `<button class="${i ? "" : "on"}" data-tab="${t}">${t}</button>`).join("")}</div><div id="tab"></div></div>`;
  const show = t => { $("#tab").innerHTML = TABS[t](r); document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t)); };
  document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => show(b.dataset.tab));
  show("Skeleton");
  $("#dlg").showModal();
}
$("#dlg").addEventListener("close", () => { const v = $("#vid"); if (v) v.pause(); });
render();
</script>
</body>
</html>
"""

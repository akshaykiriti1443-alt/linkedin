/* #home: first look, format playbooks as the main card, what works, new outliers, setup and jobs (4.11, 9). */
import {h, clear, md, get, post, S, arr, reelCard, jobLine, empty, linkBtn, savesLine, busy, toast, refreshMe,
  onJobs, onJobsDone, localStore, num} from "../app.js";
import {formatCard, workingCard} from "./parts.js";

export async function home(main, {token}) {
  const [hr, er, pr] = await Promise.all([get("/api/home"), get("/api/ext/status"), get("/api/profile")]);
  if (S.view !== token) return;
  if (!hr.ok) { clear(main, h("h1", "Home"), empty(hr.error)); return; }
  const d = hr.data;
  const ext = er.ok ? er.data : null;
  const prof = pr.ok ? pr.data : null;
  const me = S.me;
  const view = h("div", {class: "view"});
  view.append(h("div", {class: "headrow"}, h("h1", "Home"), h("span", {class: "muted"}, "Hi " + (me.name || "there") + ".")));

  // Admin: a new community code must be posted in the Skool Classroom.
  const cb = me.admin && me.admin.code_banner;
  if (cb && cb.code) {
    const b = h("div", {class: "banner warn"}, h("span", md(`New community code: **${cb.code}**. Post it in the Skool Classroom so members can join.`)),
      h("div", {class: "row"}, h("a", {class: "btn ghost sm", href: "#admin/invites"}, "Invites"),
        h("button", {type: "button", class: "sm", onclick: async e => {
          const r = await busy(e.currentTarget, () => post("/api/admin/invites/community/ack", {}));
          if (r.ok) { b.remove(); refreshMe(); } else toast(r.error);
        }}, "I posted it")));
    view.append(b);
  }
  if (d.next_step && d.next_step.href) {
    view.append(h("div", {class: "banner"}, h("span", "Next step: " + d.next_step.label), h("a", {class: "btn sm", href: d.next_step.href}, d.next_step.label)));
  }

  // Status lines: extension and the tagging pipeline.
  const lines = h("div", {class: "stack tight"});
  if (ext && ext.paired && ext.device) {
    const dv = ext.device;
    lines.append(h("p", {class: dv.online ? "muted" : "bold"}, dv.last_seen_text || "", dv.online ? "" : [" ", h("a", {href: "#settings/extension"}, "Check the extension")]));
    if (dv.cooldown_text) lines.append(h("p", {class: "bold"}, dv.cooldown_text));
  } else if (ext) {
    lines.append(h("p", "Connect Chrome to read reels. ", h("a", {href: "#settings/extension"}, "Connect Chrome")));
  }
  const pl = d.pipeline || {};
  if (pl.left_to_tag || pl.failed_tags) lines.append(h("p", {class: "muted"}, pl.line || `${num(pl.left_to_tag)} reels left to tag` + (pl.eta ? ` · ${pl.eta}` : "")));
  if (lines.childElementCount) view.append(h("div", {class: "panel"}, lines));

  // Main card: format playbooks.
  const formats = arr(d.formats);
  if (formats.length) {
    view.append(h("section", {class: "stack"},
      h("div", {class: "panel-head"}, h("h2", "Formats that work in your niche"), h("a", {href: "#working"}, "See everything that works")),
      h("div", {class: "grid3"}, formats.slice(0, 3).map(formatCard))));
  } else if (me.setup.competitors) {
    view.append(h("section", {class: "panel"}, h("h2", "Formats that work in your niche"),
      h("p", {class: "muted"}, "Your format playbooks show up here once enough reels are tagged." + (pl.eta ? " " + pl.eta + "." : ""))));
  }

  // First look: metrics-only outliers minutes after the first pull.
  // Once format playbooks exist they are the main card; the first look then only shows while tags are coming.
  for (const fl of arr(d.first_look).filter(f => !arr(d.formats).length || f.tags_coming)) {
    if (!arr(fl.reels).length) continue;
    view.append(h("section", {class: "panel hl"},
      h("div", {class: "panel-head"}, h("h2", fl.line || "Your first look"),
        fl.tags_coming ? h("span", {class: "pill busy"}, "Tags and formats ready " + (pl.eta ? pl.eta.replace(/^Ready /, "") : "soon")) : null),
      h("div", {class: "cards"}, arr(fl.reels).slice(0, 8).map(r => reelCard(r))),
      h("div", {class: "row"}, h("a", {class: "btn ghost sm", href: "#reels?account=" + fl.account.id}, "See all reels from @" + fl.account.handle)),
      savesLine(d.saves_line)));
  }

  const cards = arr(d.cards);
  if (cards.length) {
    view.append(h("section", {class: "stack"}, h("h2", "What works for your competitors"),
      h("div", {class: "grid3"}, cards.slice(0, 3).map(workingCard))));
  }

  const outs = arr(d.new_outliers);
  if (outs.length) {
    view.append(h("section", {class: "stack"},
      h("div", {class: "panel-head"}, h("h2", "New outliers"), h("a", {href: "#reels?band=outlier,big,breakout"}, "See all outliers")),
      h("div", {class: "cards"}, outs.slice(0, 8).map(r => reelCard(r))), savesLine(d.saves_line)));
  }

  // Setup checklist (what is left, never blocking).
  view.append(checklist(d.setup || me.setup, prof, ext));   // /api/home is fresh; S.me can be minutes old

  // Work in progress.
  const jobsBox = h("section", {class: "panel", id: "home-jobs"});
  const drawJobs = list => {
    clear(jobsBox, h("h2", "Work in progress"));
    if (!list.length) jobsBox.append(h("p", {class: "muted"}, "Nothing is running right now."));
    else list.forEach(j => jobsBox.append(jobLine(j)));
  };
  drawJobs(arr(d.jobs));
  view.append(jobsBox);
  onJobs(e => drawJobs(e.jobs));
  onJobsDone(() => { if (S.view === token && !main.querySelector("input:focus,textarea:focus")) home(main, {token}); });

  if (!formats.length && !outs.length && !arr(d.first_look).length && !me.setup.competitors) {
    view.insertBefore(empty("No competitors yet. Add 1-2 accounts whose reels work in your niche, and we'll find more.",
      linkBtn("#competitors", "Add competitors"), linkBtn("#onboarding/1", "Start the setup", "btn ghost")), jobsBox);
  }
  clear(main, view);
}

function checklist(setup, prof, ext) {
  const items = [];
  const t = (ok, text, href, cta) => items.push(h("div", {class: "todo" + (ok ? " ok" : "")}, h("span", {class: "lamp"}),
    h("div", {class: "t"}, h("span", {class: ok ? "muted" : "bold"}, text)), ok ? null : h("a", {class: "btn ghost sm", href}, cta)));
  t(setup.competitors > 0, setup.competitors ? `${setup.competitors} competitor${setup.competitors === 1 ? "" : "s"} tracked` : "Add 1-2 competitors", "#competitors", "Add competitors");
  // Paired but not seen lately: say so (the top banner already says "open Chrome"), not a green "connected".
  const offline = setup.extension === "paired" && ext && ext.device && ext.device.online === false;
  if (offline) t(false, "Chrome is paired but not open. Open Chrome to keep reading.", "#settings/extension", "Check Chrome");
  else t(setup.extension === "paired", setup.extension === "paired" ? "Chrome is connected" : setup.extension === "cooldown" ? "Instagram asked us to slow down. Reading resumes soon." : "Connect Chrome", "#settings/extension", "Connect Chrome");
  t(!!setup.ai, setup.ai ? "Your AI is connected" : "Connect your AI", "#settings/ai", "Connect your AI");
  t(setup.profile === "approved", setup.profile === "approved" ? "Your profile is approved" : "Answer 4 quick questions and approve your profile", "#settings/profile", "Finish your profile");
  if (prof) {
    const left = Object.entries(prof.answers || {}).filter(([q, a]) => q !== "q14" && isEmpty(a)).length;
    if (left && setup.profile === "approved") t(false, `Improve your scripts: answer ${Math.min(3, left)} more questions`, "#settings/profile", "Answer more");
  }
  if (ext && ext.device && ext.device.update_available) t(false, ext.device.update_text || "A new version of the extension is ready.", "#settings/extension", "Update");
  const allDone = setup.competitors > 0 && setup.extension === "paired" && setup.ai && setup.profile === "approved";
  if (allDone && items.length <= 4) localStore("rv.onb." + S.me.id, "done");
  return h("section", {class: "panel"}, h("h2", allDone ? "Your setup" : "Finish your setup"), h("div", null, items));
}
function isEmpty(a) {
  if (!a) return true;
  const v = a.value;
  if (Array.isArray(v)) return !v.some(s => String(s || "").trim());
  return !String(v || "").trim();
}

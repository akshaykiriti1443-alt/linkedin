/* #admin/{members,invites,settings,copy,health,support} (4.3). Admin only; the server checks too. */
import {h, clear, md, msg, say, busy, get, post, put, patch, del, api, arr, S, go, field, select, checkbox, toast, num, x,
  day, ago, nice, empty, pill, tabs, qs, copyLine, loadCopy, refreshMe, KIND_LABEL} from "../app.js";

const TABS = [["members", "Members"], ["invites", "Invites"], ["settings", "Settings"], ["copy", "Copy"], ["health", "Health"], ["support", "Support"]];
function adminTabs(cur) {
  const pending = (S.me.admin || {}).pending || 0;
  return tabs(TABS.map(([id, l]) => [id, id === "members" && pending ? `${l} (${pending} waiting)` : l, "#admin/" + id]), cur);
}

export async function admin(main, ctx) {
  const which = TABS.some(t => t[0] === ctx.parts[0]) ? ctx.parts[0] : "members";
  const view = h("div", {class: "view"}, adminTabs(which));
  clear(main, view);
  await {members, invites, settings: settingsPage, copy: copyPage, health, support}[which](view, ctx);
}

// ------------------------------------------------------------------------------------------------ members
async function members(view, {query, token}) {
  const status = query.get("status") || "";
  const q = query.get("q") || "";
  const r = await get("/api/admin/users" + qs({status, q, limit: 200}));
  if (S.view !== token) return;
  const redraw = async () => { await refreshMe(); clear(view, adminTabs("members")); members(view, {query, token}); };
  const statusSel = select([["", "Everyone"], ["pending", "Waiting for approval"], ["active", "Active"], ["disabled", "Turned off"]], status,
    {"aria-label": "Show", onchange: e => go("#admin/members" + qs({status: e.target.value, q}))});
  const search = h("input", {type: "search", value: q, placeholder: "Search name or email", "aria-label": "Search members",
    onkeydown: e => { if (e.key === "Enter") go("#admin/members" + qs({status, q: search.value.trim()})); }});
  view.append(h("h1", "Members"), h("div", {class: "filters"}, search, statusSel));
  if (!r.ok) { view.append(empty(r.error)); return; }
  const users = arr(r.data.users);
  const rows = users.map(u => {
    const out = msg();
    const actions = h("div", {class: "row"});
    const act = (label, fn, cls = "ghost sm") => h("button", {type: "button", class: cls, onclick: async e => { const res = await busy(e.currentTarget, fn); if (res && res.ok === false) say(out, res); else redraw(); }}, label);
    if (u.status === "pending") {
      actions.append(act("Approve", () => post(`/api/admin/users/${u.id}/approve`, {}), "sm"),
        act("Say no", () => confirm(`Delete the signup of ${u.email}?`) ? post(`/api/admin/users/${u.id}/reject`, {}) : null));
    } else if (u.id !== S.me.id) {
      actions.append(u.status === "active" ? act("Turn off", () => confirm(`Turn off ${u.email}? They are signed out everywhere.`) ? patch(`/api/admin/users/${u.id}`, {status: "disabled"}) : null)
        : act("Turn on", () => patch(`/api/admin/users/${u.id}`, {status: "active"})),
      act(u.role === "admin" ? "Make member" : "Make admin", () => confirm(`Change the role of ${u.email}?`) ? patch(`/api/admin/users/${u.id}`, {role: u.role === "admin" ? "member" : "admin"}) : null));
    }
    actions.append(h("button", {type: "button", class: "ghost sm", onclick: async e => {
      const res = await busy(e.currentTarget, () => post(`/api/admin/users/${u.id}/reset-link`, {}));
      if (!res.ok) { say(out, res); return; }
      clear(out); out.className = "stack"; out.append(h("p", {class: "small"}, `Send this to ${u.email}. It works until ${day(res.data.expires_at)} and is shown only once.`), copyLine(res.data.url));
    }}, "Make a reset link"));
    if (u.support_access) actions.append(h("a", {class: "btn ghost sm", href: "#admin/support?user=" + u.id}, "Look at their account"));
    if (u.id !== S.me.id) actions.append(h("button", {type: "button", class: "danger sm", onclick: async e => {
      const typed = prompt(`Type ${u.email} to delete this member and all their data.`);
      if (typed === null) return;
      const res = await busy(e.currentTarget, () => del(`/api/admin/users/${u.id}`, {confirm: typed.trim()}));
      if (!res.ok) say(out, res); else redraw();
    }}, "Delete"));
    return h("div", {class: "card", dataset: {user: u.id}},
      h("div", {class: "row"}, h("b", u.name || u.email), h("span", {class: "muted"}, u.email),
        pill({pending: "Waiting", active: "Active", disabled: "Turned off"}[u.status] || u.status, u.status === "active" ? "ok" : u.status === "pending" ? "warn" : "bad"),
        u.role === "admin" ? pill("Admin", "busy") : null, u.support_access ? pill("Support access on", "warn") : null),
      h("p", {class: "small muted"}, [u.skool_url, `joined ${day(u.created_at)}`, `last seen ${ago(u.last_seen_at)}`, `${num(u.accounts)} competitors`, `${num(u.reels)} reels`,
        `${num(u.storage_mb)} MB`, u.ai_mode ? "AI: " + nice(u.ai_mode) : "no AI yet", u.disabled_reason ? "off: " + nice(u.disabled_reason) : null].filter(Boolean).join(" · ")),
      actions, out);
  });
  view.append(users.length ? h("div", {class: "stack"}, rows) : empty("Nobody here."));
  view.append(syncPanel(redraw));
}

function syncPanel(redraw) {
  const file = h("input", {type: "file", accept: ".csv,text/csv", id: "sync-file"});
  const emails = h("textarea", {id: "sync-emails", rows: 3, placeholder: "Or paste emails, one per line"});
  const out = msg();
  const result = h("div", {class: "stack"});
  return h("section", {class: "panel", id: "sync-members"}, h("h2", "Check who is still in the community"),
    h("p", {class: "hint"}, "Upload the member export from Skool (a CSV with an email column), or paste emails. We show you who is not in Skool any more. Nothing changes until you click Turn off."),
    field("Skool member export", file), emails, out,
    h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: async e => {
      let r;
      if (file.files[0]) { const fd = new FormData(); fd.append("file", file.files[0]); r = await busy(e.currentTarget, () => api("POST", "/api/admin/sync-members", fd)); }
      else r = await busy(e.currentTarget, () => post("/api/admin/sync-members", {emails: emails.value.split(/[\s,;]+/).filter(Boolean)}));
      if (!r.ok) { say(out, r); return; }
      const d = r.data;
      const boxes = arr(d.not_in_skool).map(u => checkbox(`${u.name || ""} ${u.email} (last seen ${ago(u.last_seen_at)})`, true, {value: u.id}));
      const [allowL, allow] = checkbox(`Also let in the ${d.new_in_skool} people in Skool who have no account yet`, false);
      clear(result, h("p", `${d.in_both} members are in both. ${arr(d.not_in_skool).length} are not in Skool any more. ${d.new_in_skool} are in Skool without an account.`),
        boxes.length ? h("div", {class: "stack tight"}, boxes.map(b => b[0])) : null, d.new_in_skool ? allowL : null,
        h("div", {class: "row"}, h("button", {type: "button", class: boxes.length ? "danger" : "", onclick: async ev => {
          const ids = boxes.filter(b => b[1].checked).map(b => b[1].value);
          const a = await busy(ev.currentTarget, () => post(`/api/admin/sync-members/${d.preview_id}/apply`, {disable_ids: ids, allowlist_new: allow.checked}));
          if (!a.ok) { toast(a.error); return; }
          toast(`Turned off ${a.data.disabled}. Let in ${a.data.allowlisted}.`); redraw();
        }}, boxes.length ? "Turn off the ticked members" : "Save")));
    }}, "Check")), result);
}

// ------------------------------------------------------------------------------------------------ invites
async function invites(view, {token}) {
  const [r, al] = await Promise.all([get("/api/admin/invites"), get("/api/admin/allowlist")]);
  if (S.view !== token) return;
  const redraw = async () => { await refreshMe(); clear(view, adminTabs("invites")); invites(view, {token}); };
  view.append(h("h1", "Invites"));
  if (!r.ok) { view.append(empty(r.error)); return; }
  const d = r.data;
  const c = d.community || {};
  const codeIn = h("input", {id: "community-code", placeholder: "Leave empty to make one"});
  const codeOut = h("div", {class: "stack"});
  if (d.new_code && d.new_code.code) {
    codeOut.append(h("div", {class: "banner warn"}, h("span", md(`New community code: **${d.new_code.code}**. Post it in the Skool Classroom.`)),
      h("button", {type: "button", class: "sm", onclick: async e => { const a = await busy(e.currentTarget, () => post("/api/admin/invites/community/ack", {})); if (a.ok) redraw(); else toast(a.error); }}, "I posted it")),
      copyLine(d.new_code.code, "Copy the code"));
  }
  view.append(h("section", {class: "panel", id: "community-invite"}, h("h2", "Community code"),
    h("p", c.code_hint ? `The current code ends in ${c.code_hint}. Made ${day(c.created_at)}. Used ${num(c.used_count)} times.` : "There is no community code yet."),
    d.signup_url ? h("p", {class: "small"}, "Sign-up page: ", h("code", d.signup_url)) : null,
    codeOut,
    h("div", {class: "row"}, codeIn, h("button", {type: "button", id: "rotate-code", onclick: async e => {
      if (c.code_hint && !confirm("Make a new code? The old one stops working right away.")) return;
      const a = await busy(e.currentTarget, () => post("/api/admin/invites/community", codeIn.value.trim() ? {code: codeIn.value.trim()} : {}));
      if (!a.ok) { toast(a.error); return; }
      clear(codeOut, h("p", "The new code is shown only once:"), copyLine(a.data.code, "Copy the code"), copyLine(a.data.signup_url, "Copy the sign-up link"));
      refreshMe();
    }}, "Make a new code"))));
  const label = h("input", {id: "invite-label", placeholder: "For example: Jane from the coaching call"});
  const uses = h("input", {type: "number", id: "invite-uses", min: 1, max: 100, value: 1});
  const daysIn = h("input", {type: "number", id: "invite-days", min: 1, max: 90, value: 14});
  const invOut = h("div", {class: "stack"});
  view.append(h("section", {class: "panel"}, h("h2", "Single-use invite links"),
    h("div", {class: "grid3"}, field("Label", label), field("Uses", uses), field("Works for (days)", daysIn)), invOut,
    h("div", {class: "row"}, h("button", {type: "button", class: "ghost", id: "make-invite", onclick: async e => {
      const a = await busy(e.currentTarget, () => post("/api/admin/invites", {label: label.value.trim(), uses: Number(uses.value) || 1, expires_days: Number(daysIn.value) || 14}));
      if (!a.ok) { toast(a.error); return; }
      clear(invOut, h("p", "Send this link. It is shown only once:"), copyLine(a.data.url, "Copy the link"));
      setTimeout(redraw, 4000);
    }}, "Make an invite link")),
    arr(d.single).length ? h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", "Label"), h("th", {class: "num"}, "Uses left"), h("th", {class: "num"}, "Used"), h("th", "Ends"), h("th", ""))),
      h("tbody", null, arr(d.single).map(i => h("tr", null, h("td", i.label || "—"), h("td", {class: "num"}, i.uses_left ?? "∞"), h("td", {class: "num"}, num(i.used_count)),
        h("td", i.revoked_at ? "Stopped" : day(i.expires_at)), h("td", i.revoked_at ? "" : h("button", {type: "button", class: "ghost sm", onclick: async e => {
          const a = await busy(e.currentTarget, () => del(`/api/admin/invites/${i.id}`)); toast(a.ok ? "Stopped." : a.error); redraw();
        }}, "Stop it"))))))) : h("p", {class: "muted"}, "No invite links yet.")));
  const emails = h("textarea", {id: "allow-emails", rows: 3, placeholder: "One email per line"});
  const list = al.ok ? arr(al.data.emails) : [];
  view.append(h("section", {class: "panel"}, h("h2", "Allowlist"), h("p", {class: "hint"}, "People with these emails can sign up with the community code and get in at once."),
    emails, h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: async e => {
      const a = await busy(e.currentTarget, () => post("/api/admin/allowlist", {emails: emails.value.split(/[\s,;]+/).filter(Boolean)}));
      toast(a.ok ? "Saved." : a.error); if (a.ok) redraw();
    }}, "Add to the allowlist")),
    list.length ? h("ul", list.map(em => h("li", em, " ", h("button", {type: "button", class: "link", onclick: async () => {
      const a = await del(`/api/admin/allowlist/${encodeURIComponent(em)}`); toast(a.ok ? "Removed." : a.error); redraw();
    }}, "Remove")))) : h("p", {class: "muted"}, "The allowlist is empty.")));
}

// ------------------------------------------------------------------------------------------------ settings
const SETTING_HELP = {
  signup_mode: ["Who gets in with the community code", [["pending", "They wait for my approval (people on the allowlist get in at once)"], ["active", "They get in at once"]]],
  community_code_days: "Make a new community code every (days)", notify_admin_email: "Email me about new signups",
  expire_inactive_days: "Turn off members who don't log in for (days, 0 = never)", reconfirm_days: "Ask members for the current code every (days, 0 = never)",
  skool_fetch: "Let the server read Skool pages when a member has no extension", max_accounts_per_member: "Competitors per member",
  max_pulls_per_day: "Pulls per member per day", outputs_per_day: "Outputs per member per day", member_media_per_day: "Reels a member can process per day",
  storage_cap_mb: "Storage per member (MB)", video_keep_days: "Keep videos for (days)", allow_local_ai_urls: "Allow AI on this server's own network (self-hosted only)",
  baseline_sample_per_account: "Normal reels studied per competitor", insight_window_days: "What works looks back (days)", recent_window_days: "Recent means the last (days)",
  community_stats: "Show me anonymous top formats across members",
};
async function settingsPage(view, {token}) {
  const r = await get("/api/admin/settings");
  if (S.view !== token) return;
  view.append(h("h1", "Settings"));
  if (!r.ok) { view.append(empty(r.error)); return; }
  const st = r.data;
  const inputs = {};
  const rows = Object.entries(st).filter(([k]) => k !== "copy_overrides").map(([k, v]) => {
    const help = SETTING_HELP[k];
    const label = Array.isArray(help) ? help[0] : help || nice(k);
    let el;
    if (Array.isArray(help)) el = select(help[1].some(o => o[0] === v) ? help[1] : [...help[1], [v, nice(v)]], v, {id: "set-" + k});
    else if (typeof v === "boolean") { const [l, i] = checkbox(label, v, {id: "set-" + k}); inputs[k] = i; return l; }
    else if (typeof v === "number") el = h("input", {type: "number", value: v, id: "set-" + k});
    else el = h("input", {value: v ?? "", id: "set-" + k});
    inputs[k] = el;
    return field(label, el);
  });
  const out = msg();
  view.append(h("section", {class: "panel"}, h("div", {class: "grid2"}, rows), out, h("div", {class: "row"}, h("button", {type: "button", id: "save-settings", onclick: async e => {
    const body = {};
    for (const [k, el] of Object.entries(inputs)) {
      const v = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
      if (v !== st[k]) body[k] = v;
    }
    if (!Object.keys(body).length) { say(out, {ok: true}, "Nothing changed."); return; }
    const a = await busy(e.currentTarget, () => patch("/api/admin/settings", body));
    say(out, a, "Saved.");
    if (a.ok) Object.assign(st, a.data);
  }}, "Save settings"))));
}

// ------------------------------------------------------------------------------------------------ copy
async function copyPage(view, {token}) {
  const [r, def] = await Promise.all([get("/api/admin/copy"), loadCopy()]);
  if (S.view !== token) return;
  view.append(h("h1", "Copy"), h("p", {class: "muted"}, "Change what members read on Settings > AI, without a new release. Use {MCP_URL} where the connector address goes. Leave a box empty to use the default."));
  if (!r.ok) { view.append(empty(r.error)); return; }
  const ov = r.data || {};
  const KEYS = [["connector_title", "Title", false], ["connector_intro", "Intro", false], ["connector_steps", "Connector steps (one per line)", true],
    ["connector_notes", "Notes (one per line)", true], ["connector_estimate_line", "Estimate line", false], ["connector_prompts", "Prompts to copy (one per line)", true],
    ["claude_code_command", "Claude Code command", false], ["desktop_steps", "Desktop app steps (one per line)", true], ["key_steps", "API key steps (one per line)", true]];
  const els = {};
  const rows = KEYS.map(([k, label, isList]) => {
    const t = h("textarea", {id: "copy-" + k, rows: isList ? 5 : 2, placeholder: Array.isArray(def[k]) ? def[k].join("\n") : def[k] || ""});
    t.value = ov[k] === undefined ? "" : Array.isArray(ov[k]) ? ov[k].join("\n") : String(ov[k]);
    els[k] = [t, isList];
    return field(label, t);
  });
  const out = msg();
  view.append(h("section", {class: "panel"}, rows, out, h("div", {class: "row"}, h("button", {type: "button", id: "save-copy", onclick: async e => {
    const body = {};
    for (const [k, [t, isList]] of Object.entries(els)) {
      const v = t.value.trim();
      if (!v) continue;
      body[k] = isList ? v.split("\n").map(s => s.trim()).filter(Boolean) : v;
    }
    const a = await busy(e.currentTarget, () => put("/api/admin/copy", body));
    say(out, a, "Saved. Members see it now.");
    if (a.ok) S.copy = null;
  }}, "Save copy"))));
}

// ------------------------------------------------------------------------------------------------ health
async function health(view, {token}) {
  const [s, ig, cf, hz] = await Promise.all([get("/api/admin/stats"), get("/api/admin/instagram-health"), get("/api/admin/community-formats"), get("/healthz")]);
  if (S.view !== token) return;
  view.append(h("h1", "Health"));
  if (s.ok) {
    const d = s.data; const l = d.load || {}; const w = d.worker || {};
    view.append(h("section", {class: "panel"}, h("div", {class: "nums"},
      h("div", null, h("b", num(d.members)), "members"), h("div", null, h("b", num(d.pending)), "waiting"), h("div", null, h("b", num(d.active_7d)), "active this week"),
      h("div", null, h("b", num(d.reels)), "reels"), h("div", null, h("b", num(d.storage_mb) + " MB"), "stored"), h("div", null, h("b", num(d.disk_free_mb) + " MB"), "disk free"),
      h("div", null, h("b", num(d.jobs_failed_24h)), "failed jobs (24 h)")),
    h("h2", "Server load"), h("p", `${num(l.media_queued)} reels waiting for media work · ${num(l.budget_used_today)} of ${num(l.budget_today)} used today · about ${l.sec_per_reel || "?"} s a reel.`),
    h("p", w.at ? `Worker seen ${ago(w.at)} on ${w.host || "?"}.` : h("b", "The worker is not running. Jobs will wait.")),
    h("p", d.last_backup ? `Last backup ${ago(d.last_backup.at || d.last_backup)}.` : h("b", "No backup yet.")),
    d.last_backup && d.last_backup.copy_ok === false ? h("p", {class: "msg err"}, "The off-site copy failed. Check BACKUP_COPY_CMD in deploy/.env, or copy the backups from the server (deploy/DEPLOY.md, Backups).") : null));
  } else view.append(empty(s.error));
  if (hz.ok) view.append(h("p", {class: "small muted"}, `Version ${hz.data.version} · database ${hz.data.db}.`));
  if (ig.ok) {
    const d = ig.data;
    view.append(h("section", {class: "panel"}, h("h2", "Instagram reading"),
      h("p", {class: "small"}, `${arr(d.fails).length} recent problems · ${Object.keys(d.recoveries || {}).length} cursor recoveries · ${num(d.pk_conflicts)} id conflicts.`),
      arr(d.fails).length ? h("ul", arr(d.fails).slice(0, 10).map(f => h("li", {class: "small"}, typeof f === "string" ? f : `${f.code || ""} ${f.at ? ago(f.at) : ""} ${f.message || ""}`))) : null,
      h("p", {class: "small muted"}, "Fields seen: " + Object.entries(d.fields_seen || {}).map(([k, v]) => `${k} ${v === null ? "not yet" : v ? "yes" : "no"}`).join(", "))));
  }
  view.append(h("section", {class: "panel"}, h("h2", "Top formats across members"),
    cf.ok && cf.data.enabled === false ? h("p", {class: "muted"}, cf.data.message) :
    cf.ok ? (arr(cf.data.formats).length ? h("ul", arr(cf.data.formats).map(f => h("li", `${f.label}: ${f.members_using} members, ${x(f.lift_median)} typical lift`))) : h("p", {class: "muted"}, "Not enough members yet (each row needs 5 or more).")) :
      h("p", {class: "muted"}, cf.error)));
}

// ------------------------------------------------------------------------------------------------ support
async function support(view, {query, token}) {
  const uid = query.get("user") || "";
  const tab = query.get("view") || "reels";
  const r = await get("/api/admin/users" + qs({limit: 200}));
  if (S.view !== token) return;
  view.append(h("h1", "Support"), h("p", {class: "muted"}, "You can look at a member's account only while they have turned on support access. Every look is logged and shown to them."));
  const users = r.ok ? arr(r.data.users).filter(u => u.support_access) : [];
  if (!users.length) { view.append(empty("No member has turned on support access.")); return; }
  view.append(h("div", {class: "filters"}, select([["", "Pick a member"], ...users.map(u => [u.id, `${u.name || ""} ${u.email}`])], uid, {"aria-label": "Member", onchange: e => go("#admin/support" + qs({user: e.target.value}))})));
  if (!uid) return;
  view.append(tabs([["reels", "Reels", `#admin/support?user=${uid}&view=reels`], ["outputs", "Outputs", `#admin/support?user=${uid}&view=outputs`], ["insights", "What works", `#admin/support?user=${uid}&view=insights`]], tab));
  const d = await get(`/api/admin/support/${encodeURIComponent(uid)}/${tab}`);
  if (S.view !== token) return;
  if (!d.ok) { view.append(empty(d.error)); return; }
  if (tab === "reels") {
    view.append(h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", "Reel"), h("th", {class: "num"}, "Score"), h("th", {class: "num"}, "Views"), h("th", "Format"), h("th", "Tags"))),
      h("tbody", null, arr(d.data.reels).map(rr => h("tr", null, h("td", `@${(rr.account || {}).handle} · ${rr.caption_first_line || ""}`), h("td", {class: "num"}, x(rr.score)), h("td", {class: "num"}, num(rr.views)),
        h("td", ((rr.tags || {}).visual_format_label) || "—"), h("td", (rr.states || {}).tags || "—")))))));
  } else if (tab === "outputs") {
    view.append(h("div", {class: "tablewrap"}, h("table", null, h("thead", null, h("tr", null, h("th", "Output"), h("th", "Kind"), h("th", "Status"), h("th", "Made"))),
      h("tbody", null, arr(d.data.outputs).map(o => h("tr", null, h("td", o.title || ""), h("td", KIND_LABEL[o.kind] || o.kind), h("td", nice(o.status)), h("td", day(o.created_at))))))));
  } else {
    const s = d.data;
    view.append(h("section", {class: "panel"}, arr(s.cards).length ? h("ul", arr(s.cards).map(c => h("li", h("b", c.title), " — ", c.line))) : h("p", {class: "muted"}, "Nothing yet."),
      arr(s.formats).length ? h("ul", arr(s.formats).map(f => h("li", `${f.name}: ${f.line}`))) : null));
  }
}

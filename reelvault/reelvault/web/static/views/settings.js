/* #settings/{ai,extension,profile,topics,account}. */
import {h, clear, msg, say, busy, get, post, del, S, field, toast, day, localStore, pill, refreshMe} from "../app.js";
import {profilePage, topicsPage, settingsTabs} from "./profile.js";
import {aiSetup} from "./ai.js";
import {extensionSetup} from "./extension.js";

export async function settings(main, ctx) {
  const which = ctx.parts[0] || "ai";
  if (which === "profile") return profilePage(main, ctx);
  if (which === "topics") return topicsPage(main, ctx);
  if (which === "extension") {
    const box = h("div", {class: "stack"});
    clear(main, h("div", {class: "view"}, settingsTabs("extension"), h("h1", "Chrome extension"), box));
    await extensionSetup(box, {});
    return;
  }
  if (which === "account") return account(main, ctx);
  const box = h("div", {class: "stack"});
  clear(main, h("div", {class: "view"}, settingsTabs("ai"), h("h1", "Your AI"), box));
  await aiSetup(box, {});
}

async function account(main, {token}) {
  const sa = await get("/api/me/support-access");
  if (S.view !== token) return;
  const me = S.me;
  const reload = () => account(main, {token});
  // Password
  const cur = h("input", {type: "password", autocomplete: "current-password", id: "pw-current"});
  const nw = h("input", {type: "password", autocomplete: "new-password", id: "pw-new"});
  const pwOut = msg();
  // Theme
  const theme = localStore("rv.theme") || "system";
  const setTheme = v => {
    if (v === "system") { localStore("rv.theme", null); document.documentElement.removeAttribute("data-theme"); }
    else { localStore("rv.theme", v); document.documentElement.setAttribute("data-theme", v); }
  };
  const themeSel = h("div", {class: "chips", role: "radiogroup", "aria-label": "Look"}, [["system", "Like my device"], ["light", "Light"], ["dark", "Dark"]].map(([v, l]) =>
    h("button", {type: "button", class: "pchip p-format", role: "radio", "aria-checked": String(theme === v), "aria-pressed": String(theme === v), onclick: e => {
      setTheme(v); themeSel.querySelectorAll("button").forEach(b => { b.setAttribute("aria-pressed", "false"); b.setAttribute("aria-checked", "false"); });
      e.currentTarget.setAttribute("aria-pressed", "true"); e.currentTarget.setAttribute("aria-checked", "true");
    }}, l)));
  // Support access
  const s = sa.ok ? sa.data : {active: false};
  const days = h("select", {id: "support-days", "aria-label": "For how long"}, [1, 3, 7].map(n => h("option", {value: n, selected: n === 7}, n === 1 ? "1 day" : `${n} days`)));
  // Delete
  const signOutAll = h("input", {type: "checkbox", id: "pw-sign-out-all", checked: true});
  const delPw = h("input", {type: "password", autocomplete: "current-password", id: "delete-password"});
  const delOut = msg();
  clear(main, h("div", {class: "view"}, settingsTabs("account"), h("h1", "Your account"),
    h("section", {class: "panel"}, h("dl", {class: "kv"}, h("dt", "Name"), h("dd", me.name || "—"), h("dt", "Email"), h("dd", me.email),
      h("dt", "Limits"), h("dd", `${me.limits.max_accounts} competitors · ${me.limits.pulls_per_day} pulls a day · ${me.limits.outputs_per_day} outputs a day`))),
    h("section", {class: "panel"}, h("h2", "Look"), themeSel),
    h("section", {class: "panel"}, h("h2", "Change your password"), h("div", {class: "grid2"}, field("Current password", cur), field("New password", nw, "At least 10 characters.")), pwOut,
      h("label", {class: "check"}, signOutAll, " Also sign out Claude, the Chrome extension and the desktop app"),
      h("div", {class: "row"}, h("button", {type: "button", onclick: async e => {
        const r = await busy(e.currentTarget, () => post("/api/auth/password", {current: cur.value, new: nw.value, sign_out_everywhere: signOutAll.checked}));
        say(pwOut, r, r.ok && r.data && r.data.message ? `Changed. ${r.data.message}` : "Changed. You're signed out on your other devices.");
        if (r.ok) { cur.value = ""; nw.value = ""; await refreshMe(); }
      }}, "Change my password"))),
    h("section", {class: "panel", id: "support-access"}, h("div", {class: "panel-head"}, h("h2", "Let the admin see your account"), s.active ? pill("On", "warn") : pill("Off")),
      h("p", {class: "hint"}, "Turn this on when you ask for help. The admin can then look at your reels, outputs and what works, but can't change anything. It turns off by itself."),
      s.active ? h("p", `On until ${day(s.expires_at)}.`) : null,
      s.last_viewed_at ? h("p", {class: "small"}, `The admin looked at your account on ${day(s.last_viewed_at)}.`) : null,
      h("div", {class: "row"}, s.active ? h("button", {type: "button", class: "ghost", onclick: async e => {
        const r = await busy(e.currentTarget, () => del("/api/me/support-access"));
        toast(r.ok ? "Turned off." : r.error); reload();
      }}, "Turn it off") : [days, h("button", {type: "button", class: "ghost", onclick: async e => {
        const r = await busy(e.currentTarget, () => post("/api/me/support-access", {days: Number(days.value)}));
        toast(r.ok ? "Turned on." : r.error); reload();
      }}, "Turn it on")])),
    h("section", {class: "panel"}, h("h2", "Your data"), h("p", {class: "hint"}, "Download everything: your profile, competitors, reel numbers, analyses and outputs."),
      h("div", {class: "row"}, h("a", {class: "btn ghost", href: "/api/me/export", download: true}, "Download my data (ZIP)"))),
    h("section", {class: "panel"}, h("h2", "Delete my account"), h("p", "This deletes your account, every reel, every output and every file. It can't be undone."),
      field("Type your password to confirm", delPw), delOut,
      h("div", {class: "row"}, h("button", {type: "button", class: "danger", onclick: async e => {
        if (!confirm("Delete your account and everything in it? This can't be undone.")) return;
        const r = await busy(e.currentTarget, () => del("/api/me", {password: delPw.value}));
        if (!r.ok) { say(delOut, r); return; }
        S.me = null; toast("Your account is deleted."); location.hash = "#login"; location.reload();
      }}, "Delete my account forever")))));
}

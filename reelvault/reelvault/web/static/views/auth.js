/* #join[/code], #login, #reset/<token>, and the re-confirm screen (4.2). */
import {h, clear, md, msg, say, busy, post, get, S, go, field, refreshMe, toast, localStore} from "../app.js";

const CHROME_NOTE = "You'll need Chrome, Edge or Brave on a computer to read Instagram reels.";
const PENDING = "Thanks! The community admin will approve you soon. You'll get an email if email is set up; otherwise try logging in later.";

function brandHead(title, sub) {
  return [h("p", {class: "label"}, "ReelVault Members"), h("h1", title), sub ? h("p", {class: "muted"}, sub) : null];
}

/** After sign-in: the OAuth round trip (WP6) does a full page load, but ONLY to /oauth/authorize?... */
function afterSignIn(query) {
  const next = query && query.get("next");
  if (next && next.startsWith("/oauth/authorize?")) { location.href = next; return; }
  const back = query && query.get("back");
  const me = S.me;
  const fresh = me && me.setup && me.setup.profile === "empty" && !me.setup.competitors && !me.setup.ai &&
    me.setup.extension === "none" && localStore("rv.onb." + me.id) !== "done";
  if (fresh && me.role !== "admin") { go("#onboarding/1"); return; }
  go(back && back.startsWith("#") && !/^#(login|join)/.test(back) ? back : "#home");
}

export async function login(main, {query}) {
  const email = h("input", {type: "email", autocomplete: "email", required: true, id: "login-email"});
  const pw = h("input", {type: "password", autocomplete: "current-password", required: true, id: "login-password"});
  const out = msg();
  const resetBox = h("div", {class: "stack", hidden: true});
  const form = h("form", {class: "panel", onsubmit: async e => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    const r = await busy(btn, () => post("/api/auth/login", {email: email.value.trim(), password: pw.value}));
    if (!r.ok) { say(out, r); return; }
    S.me = r.data.user;
    await refreshMe();
    afterSignIn(query);
  }},
  field("Email", email), field("Password", pw), out,
  h("div", {class: "row"}, h("button", {type: "submit"}, "Log in"), h("span", {class: "spacer"}),
    h("button", {type: "button", class: "link", onclick: () => { resetBox.hidden = !resetBox.hidden; }}, "Forgot your password?")));
  const remail = h("input", {type: "email", autocomplete: "email"});
  const rout = msg();
  resetBox.append(h("p", "Type your email. If email is set up on this server, we'll send you a reset link. If not, ask the community admin for one."),
    field("Email", remail), rout,
    h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: async e => {
      const r = await busy(e.currentTarget, () => post("/api/auth/reset-request", {email: remail.value.trim()}));
      say(rout, r, "If that email has an account, a link is on its way. Check your inbox.");
    }}, "Send me a reset link")));
  form.append(resetBox);
  clear(main, h("div", {class: "view"}, brandHead("Log in"), form,
    h("p", {class: "muted"}, "New here? ", h("a", {href: "#join"}, "Join with your community code"), "."),
    h("p", {class: "hint"}, h("a", {href: "#privacy"}, "Privacy"), " · ", h("a", {href: "#help"}, "Help"))));
  email.focus();
}

export async function join(main, {parts, query}) {
  const code = h("input", {autocomplete: "off", required: true, id: "join-code", value: parts[0] || "", placeholder: "XXXX-XXXX"});
  const name = h("input", {autocomplete: "name", required: true, id: "join-name"});
  const email = h("input", {type: "email", autocomplete: "email", required: true, id: "join-email"});
  const pw = h("input", {type: "password", autocomplete: "new-password", required: true, minlength: 10, id: "join-password"});
  const skool = h("input", {type: "url", autocomplete: "url", id: "join-skool", placeholder: "https://www.skool.com/@your-name"});
  const out = msg();
  const codeNote = h("p", {class: "hint"});
  if (parts[0]) {
    get("/api/invite/" + encodeURIComponent(parts[0])).then(r => {
      if (r.ok && r.data.valid) codeNote.textContent = "Your invite is ready" + (r.data.label ? `: ${r.data.label}` : "") + ".";
      else codeNote.textContent = "That invite link doesn't work any more. Ask in the community for the current code.";
    });
  }
  const form = h("form", {class: "panel", onsubmit: async e => {
    e.preventDefault();
    if (pw.value.length < 10) { say(out, {error: "Use at least 10 characters for your password."}); return; }
    const btn = form.querySelector("button[type=submit]");
    const r = await busy(btn, () => post("/api/auth/signup", {name: name.value.trim(), email: email.value.trim(), password: pw.value,
      skool_url: skool.value.trim() || null, code: code.value.trim()}));
    if (!r.ok) { say(out, r); return; }
    if (r.data.status === "pending") {
      clear(main, h("div", {class: "view"}, brandHead("You're on the list"), h("div", {class: "panel"}, h("p", PENDING),
        h("a", {class: "btn ghost", href: "#login"}, "Go to log in"))));
      return;
    }
    S.me = r.data.user;
    await refreshMe();
    toast("Welcome to ReelVault Members.");
    const next = query.get("next");
    if (next && next.startsWith("/oauth/authorize?")) { location.href = next; return; }
    go("#onboarding/1");
  }},
  field("Community access code", code, "Find it in the Skool Classroom, or use your invite link."), codeNote,
  field("Your name", name), field("Email", email), field("Password", pw, "At least 10 characters."),
  field("Your Skool profile link (optional)", skool, "We use it to learn about you. You can change it later."),
  out, h("div", {class: "row"}, h("button", {type: "submit"}, "Create my account")));
  clear(main, h("div", {class: "view"}, brandHead("Join ReelVault Members", "For members of the community only."),
    h("div", {class: "banner"}, CHROME_NOTE), form,
    h("p", {class: "muted"}, "Already have an account? ", h("a", {href: "#login"}, "Log in"), ".")));
  (parts[0] ? name : code).focus();
}

export async function reset(main, {parts}) {
  const pw = h("input", {type: "password", autocomplete: "new-password", required: true, id: "reset-password"});
  const pw2 = h("input", {type: "password", autocomplete: "new-password", required: true, id: "reset-password2"});
  const out = msg();
  const form = h("form", {class: "panel", onsubmit: async e => {
    e.preventDefault();
    if (pw.value !== pw2.value) { say(out, {error: "The two passwords are not the same."}); return; }
    const r = await busy(form.querySelector("button"), () => post("/api/auth/reset", {token: parts[0] || "", new: pw.value}));
    if (!r.ok) { say(out, r); return; }
    clear(main, h("div", {class: "view"}, brandHead("Password changed"), h("div", {class: "panel"},
      h("p", "Your new password works now. Log in with it."), h("a", {class: "btn", href: "#login"}, "Log in"))));
  }}, field("New password", pw, "At least 10 characters."), field("Type it again", pw2), out,
  h("div", {class: "row"}, h("button", {type: "submit"}, "Save my new password")));
  clear(main, h("div", {class: "view"}, brandHead("Pick a new password"), form));
}

/** Shown instead of every page while the member must re-confirm with the current community code. */
export async function reconfirm(main) {
  const code = h("input", {autocomplete: "off", id: "reconfirm-code", placeholder: "XXXX-XXXX"});
  const out = msg();
  const form = h("form", {class: "panel", onsubmit: async e => {
    e.preventDefault();
    const r = await busy(form.querySelector("button"), () => post("/api/auth/reconfirm", {code: code.value.trim()}));
    if (!r.ok) { say(out, r); return; }
    await refreshMe();
    toast("Thanks. You're all set.");
    location.hash = "#home";
    location.reload();
  }}, h("p", md("To keep ReelVault for members only, type the **current community code**. You'll find it in the Skool Classroom.")),
  field("Community code", code), out, h("div", {class: "row"}, h("button", {type: "submit"}, "Continue"),
    h("button", {type: "button", class: "ghost", onclick: async () => { await post("/api/auth/logout", {}); S.me = null; location.hash = "#login"; location.reload(); }}, "Log out")));
  clear(main, h("div", {class: "view"}, brandHead("Are you still in the community?"), form));
}

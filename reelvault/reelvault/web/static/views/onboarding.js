/* #onboarding/<1-5>: value first (section 9). Links + role models, Chrome, AI, 4 quick questions + approve, first look.
   Every step has "Skip for now" except approve. */
import {h, clear, md, msg, say, busy, get, post, put, arr, S, go, field, toast, refreshMe, pollSoon, onJobsDone,
  reelCard, jobLine, savesLine, localStore, empty, every} from "../app.js";
import {questionEditor, saveEditors, sourceList, addSourceBox, digestView, SOURCE_LABEL} from "./profile.js";
import {extensionSetup, KEEP_OPEN} from "./extension.js";
import {aiSetup} from "./ai.js";

const STEPS = [["1", "Your links"], ["2", "Connect Chrome"], ["3", "Connect your AI"], ["4", "4 quick questions"], ["5", "Your first look"]];
const QUICK = ["q1", "q3", "q7", "q12"];

export async function onboarding(main, {parts, token}) {
  const step = STEPS.some(s => s[0] === parts[0]) ? parts[0] : "1";
  await refreshMe();
  if (S.view !== token) return;
  const me = S.me;
  const done = {"1": me.setup.competitors > 0, "2": me.setup.extension === "paired", "3": !!me.setup.ai, "4": me.setup.profile === "approved", "5": false};
  const stepper = h("ol", {class: "stepper", "aria-label": "Setup steps"}, STEPS.map(([n, label]) => h("li", null,
    h("a", {href: "#onboarding/" + n, class: done[n] ? "done" : "", "aria-current": n === step ? "step" : null}, h("span", `Step ${n}`), h("span", label)))));
  const body = h("div", {class: "stack"});
  const next = n => go(n === "done" ? "#home" : "#onboarding/" + n);
  const skip = n => h("button", {type: "button", class: "link", onclick: () => next(n)}, "Skip for now");
  clear(main, h("div", {class: "view"}, h("p", {class: "label"}, "Set up ReelVault"), stepper, body));
  if (step === "1") await step1(body, next, skip, token);
  else if (step === "2") await step2(body, next, skip, token);
  else if (step === "3") await step3(body, next, skip);
  else if (step === "4") await step4(body, next, skip, token);
  else await step5(body, token);
}

// ------------------------------------------------------------------------------------------------ 1
async function step1(body, next, skip, token) {
  const [pr, ar] = await Promise.all([get("/api/profile"), get("/api/accounts")]);
  if (S.view !== token) return;
  const p = pr.ok ? pr.data : {sources: [], answers: {}};
  const accounts = ar.ok ? arr(ar.data.accounts) : [];
  const self = ar.ok ? ar.data.self : null;
  const srcBox = h("div", {class: "stack"});
  const drawSources = async () => {
    const r = await get("/api/profile");
    if (r.ok) clear(srcBox, sourceList(arr(r.data.sources), drawSources));
  };
  clear(srcBox, sourceList(arr(p.sources), drawSources));
  const seed1 = h("input", {id: "seed-1", placeholder: "@account_one"});
  const seed2 = h("input", {id: "seed-2", placeholder: "@account_two"});
  const own = h("input", {id: "own-ig", placeholder: "@yourname", value: self ? "@" + self.handle : ""});
  const out = msg();
  const skoolPrefill = S.me.skool_url || "";
  const box = addSourceBox(drawSources, {instagram: false});   // the own-Instagram box below adds the bio
  if (skoolPrefill) { const i = box.querySelector("#src-skool"); if (i) i.value = skoolPrefill; }
  body.append(
    h("h1", "Your links and 1-2 role models"),
    h("section", {class: "panel"}, h("h2", "Tell us about you"),
      h("p", "Paste anything that describes you: your Skool profile, your Skool group's About page, website, LinkedIn, Instagram."),
      h("p", {class: "hint"}, "Your website is read right away. Skool, LinkedIn and your Instagram bio are read by your own Chrome after step 2, or you can paste the text."),
      box, srcBox),
    h("section", {class: "panel"}, h("h2", "1-2 Instagram accounts whose reels work in your niche"),
      h("p", {class: "hint"}, "We'll find more like them. No idea yet? Skip this. We can find the top accounts in your niche from your profile."),
      accounts.length ? h("p", {class: "muted"}, "Already tracking: " + accounts.map(a => "@" + a.handle).join(", ")) : null,
      h("div", {class: "grid2"}, field("Role model 1", seed1), field("Role model 2", seed2)),
      field("Your own Instagram (optional)", own, "We read your own reels too, but never mix them into your competitors' numbers.")),
    out,
    h("div", {class: "row"}, h("button", {type: "button", class: "big", id: "step1-next", onclick: async e => {
      await busy(e.currentTarget, async () => {
        const seeds = [seed1.value, seed2.value].map(s => s.trim()).filter(Boolean);
        if (seeds.length) {
          const r = await post("/api/accounts", {handles: seeds, pull: true});
          if (!r.ok) { say(out, r); return; }
          if (r.data.schedule_note) toast(r.data.schedule_note);
          await put("/api/profile/answers", {answers: {q14: {value: seeds}}});
        }
        const o = own.value.trim();
        if (o && !(self && ("@" + self.handle) === o)) {
          const r = await post("/api/accounts", {handles: [o], pull: true, role: "self"});
          if (!r.ok) { say(out, r); return; }
          const src = await post("/api/profile/sources", {kind: "instagram_bio", url: o});
          if (!src.ok && src.status !== 429) toast(src.error);
        }
        pollSoon();
        next("2");
      });
    }}, "Save and continue"), skip("2")));
}

// ------------------------------------------------------------------------------------------------ 2
async function step2(body, next, skip, token) {
  const box = h("div", {class: "stack"});
  const after = h("div", {class: "stack"});
  body.append(h("h1", "Connect Chrome"), h("p", {class: "muted"}, "ReelVault reads Instagram through your own browser, with your own login."), box, after);
  const ready = async () => {
    const [st, pr, ar] = await Promise.all([get("/api/ext/status"), get("/api/profile"), get("/api/accounts")]);
    if (S.view !== token) return;
    clear(after);
    if (!st.ok || !st.data.paired || !(st.data.device || {}).ig_confirmed) {
      after.append(h("div", {class: "row"}, skip("3")));
      return;
    }
    after.append(h("div", {class: "banner ok"}, KEEP_OPEN));
    const waiting = arr(pr.ok ? pr.data.sources : []).filter(s => s.status === "needs_paste" && ["skool_group", "skool_profile", "linkedin_page", "instagram_bio"].includes(s.kind));
    if (waiting.length) {
      after.append(h("section", {class: "panel"}, h("h2", "Read your links now"),
        h("p", "Your Chrome can read these now: " + waiting.map(s => SOURCE_LABEL[s.kind]).join(", ") + "."),
        h("div", {class: "row"}, h("button", {type: "button", id: "read-links", onclick: async e => {
          await busy(e.currentTarget, async () => {
            let ok = 0;
            for (const s of waiting) { const r = await post(`/api/profile/sources/${s.id}/read`, {}); if (r.ok) ok++; else toast(r.error); }
            if (ok) toast(`Reading ${ok} link${ok > 1 ? "s" : ""} in your Chrome.`);
            pollSoon(); ready();
          });
        }}, "Read my links now"))));
    }
    const accounts = ar.ok ? arr(ar.data.accounts) : [];
    const seeds = accounts.slice(0, 2).map(a => a.handle);
    const out = msg();
    const query = h("input", {id: "discover-query", placeholder: "for example: facebook ads agency"});
    const start = async (btn, bodyReq) => {
      const r = await busy(btn, () => post("/api/discover", bodyReq));
      if (!r.ok) { say(out, r); return; }
      pollSoon(); go("#discover/" + r.data.run_id + "?from=onboarding");
    };
    after.append(h("section", {class: "panel"}, h("h2", "Find more accounts like these"),
      h("p", "We look for accounts like your role models, check their last 12 reels, and show you the top 10. You tick the ones to track (up to 10)."),
      h("p", {class: "hint"}, "We'll read 5 accounts today and 5 tomorrow to keep your Instagram safe."),
      h("div", {class: "row"},
        seeds.length ? h("button", {type: "button", id: "find-similar", onclick: e => start(e.currentTarget, {mode: "similar", seeds})}, "Find more accounts like " + seeds.map(s => "@" + s).join(" and ")) : null,
        h("button", {type: "button", class: seeds.length ? "ghost" : "", onclick: e => start(e.currentTarget, {mode: "niche"})}, "Find the top accounts in my niche")),
      h("div", {class: "row"}, query, h("button", {type: "button", class: "ghost", onclick: e => query.value.trim() && start(e.currentTarget, {mode: "search", query: query.value.trim()})}, "Search")),
      out),
      h("div", {class: "row"}, h("button", {type: "button", class: "big", id: "step2-next", onclick: () => next("3")}, "Continue"), skip("3")));
  };
  await extensionSetup(box, {onboarding: true, onReady: ready});
  await ready();
  every(6000, async stop => {
    const s = await get("/api/ext/status", {quiet: true});
    if (s.ok && s.data.paired && (s.data.device || {}).ig_confirmed && !after.querySelector("#step2-next")) { stop(); await extensionSetup(box, {onboarding: true, onReady: ready}); ready(); }
  });
}

// ------------------------------------------------------------------------------------------------ 3
async function step3(body, next, skip) {
  const box = h("div", {class: "stack"});
  const foot = h("div", {class: "row"});
  body.append(h("h1", "Connect your AI"),
    h("p", {class: "muted"}, "You bring your own AI. Pick the easiest way for you. You can change it later in Settings."), box, foot);
  const drawFoot = () => clear(foot, h("button", {type: "button", class: "big", id: "step3-next", onclick: () => next("4")}, S.me.setup.ai ? "Continue" : "Continue without AI"), skip("4"));
  await aiSetup(box, {onboarding: true, onDone: mode => {
    drawFoot();
    if (mode === "claude_connector") {
      foot.before(h("div", {class: "banner"}, h("span", md("To fill in your profile, open Claude and say: **Draft my ReelVault profile**"))));
    }
  }});
  drawFoot();
}

// ------------------------------------------------------------------------------------------------ 4
async function step4(body, next, skip, token) {
  const r = await get("/api/profile");
  if (S.view !== token) return;
  if (!r.ok) { body.append(h("h1", "4 quick questions"), empty(r.error)); return; }
  const p = r.data;
  const sources = Object.fromEntries(arr(p.sources).map(s => [s.id, s]));
  const qs = arr(p.questions).filter(q => QUICK.includes(q.id));
  const editors = qs.map(q => questionEditor(q, p.answers[q.id], sources));
  const out = msg();
  const okSources = arr(p.sources).filter(s => s.status === "ok").length;
  const draftBox = h("div", {class: "stack"});
  if (p.drafting_job) draftBox.append(jobLine(p.drafting_job));
  onJobsDone(() => { if (!body.querySelector("input:focus,textarea:focus")) go("#onboarding/4"); });
  const review = h("div", {class: "stack"});
  body.append(h("h1", "4 quick questions"),
    h("p", {class: "muted"}, "About your END customer. Short answers are fine. Answers from your links say where they came from: check them."),
    okSources ? h("section", {class: "panel"}, h("p", `We read ${okSources} of your links.`),
      p.drafting_job ? null : h("div", {class: "row"}, h("button", {type: "button", class: "ghost", id: "draft-profile", onclick: async e => {
        const d = await busy(e.currentTarget, () => post("/api/profile/draft", {}));
        if (!d.ok) { toast(d.error); return; }
        toast("Your AI is filling in your answers."); pollSoon(); go("#onboarding/4");
      }}, "Fill in my answers from my links")), draftBox) : null,
    h("section", {class: "panel"}, editors.map(e => [e.el, h("div", {class: "divider"})])),
    out,
    h("div", {class: "row"}, h("button", {type: "button", class: "big", id: "step4-review", onclick: async e => {
      const s = await busy(e.currentTarget, () => saveEditors(editors));
      if (!s.ok) { say(out, s); return; }
      const fresh = s.data || p;
      const missing = arr(fresh.missing_required);
      if (missing.length) { say(out, {error: "Answer questions 1, 3, 7 and 12 first."}); return; }
      showReview(review, fresh, next);
      review.scrollIntoView({behavior: "smooth", block: "start"});
    }}, "Review and approve"), skip("5")),
    review);
  if (p.status === "approved") review.append(h("div", {class: "banner ok"}, h("span", "Your profile is approved."), h("button", {type: "button", class: "sm", onclick: () => next("5")}, "Continue")));
}

function showReview(box, p, next) {
  const a = p.answers || {};
  const val = q => { const v = (a[q] || {}).value; return Array.isArray(v) ? v.join("; ") : v || ""; };
  const out = msg();
  clear(box, h("section", {class: "panel hl", id: "review"},
    h("h2", "Is this right?"),
    h("p", {class: "hint"}, "Every script and plan uses this. You can change it any time in Settings > Profile."),
    h("dl", {class: "kv"},
      h("dt", "Who you help"), h("dd", val("q1")),
      h("dt", "Their top 3 problems"), h("dd", val("q3")),
      h("dt", "What you sell"), h("dd", val("q7")),
      h("dt", "What you can make"), h("dd", val("q12") + ((a.q12 || {}).hours ? ` · ${a.q12.hours} hours a week` : ""))),
    out,
    h("div", {class: "row"}, h("button", {type: "button", class: "big", id: "approve-profile", onclick: async e => {
      const r = await busy(e.currentTarget, () => post("/api/profile/approve", {}));
      if (!r.ok) { say(out, r); return; }
      await refreshMe();
      post("/api/topics/suggest", {from: "profile"}).then(t => { if (t.ok) pollSoon(); });
      clear(box, h("section", {class: "panel hl"}, h("h2", "Approved"), digestView(r.data.digest),
        h("p", {class: "hint"}, "We're also suggesting topics from your profile. Zero topics is fine."),
        h("div", {class: "row"}, h("button", {type: "button", class: "big", id: "step4-next", onclick: () => next("5")}, "See your first look"))));
    }}, "This is right"), h("button", {type: "button", class: "ghost", onclick: () => clear(box)}, "Change something"))));
}

// ------------------------------------------------------------------------------------------------ 5
async function step5(body, token) {
  const draw = async () => {
    const r = await get("/api/home");
    if (S.view !== token) return;
    const d = r.ok ? r.data : {};
    const fl = arr(d.first_look).filter(f => arr(f.reels).length);
    const pulls = arr(d.jobs).filter(j => j.kind === "ig_pull" || j.kind === "score_account");
    const eta = (d.pipeline || {}).eta;
    const total = fl.reduce((n, f) => n + (Number(f.outliers) || 0), 0);
    const handles = fl.map(f => "@" + ((f.account || {}).handle || f.account || "")).filter(x => x.length > 1);
    clear(body, h("h1", fl.length > 1 && handles.length === fl.length
      ? `Your first look: ${total} outliers from ${handles.slice(0, -1).join(", ")} and ${handles[handles.length - 1]}`
      : fl.length ? fl[0].line : "Your first look"));
    if (!fl.length) {
      body.append(h("section", {class: "panel"},
        h("p", "Your first look shows up here a few minutes after Chrome reads your first competitor."),
        pulls.length ? pulls.map(j => jobLine(j)) : h("p", {class: "muted"}, "No pull is running. Add a competitor and connect Chrome to start."),
        h("div", {class: "row"}, h("a", {class: "btn ghost", href: "#competitors"}, "Add competitors"), h("a", {class: "btn ghost", href: "#onboarding/2"}, "Connect Chrome"))));
    }
    for (const f of fl) {
      body.append(h("section", {class: "panel hl"}, h("div", {class: "panel-head"}, h("h2", f.line),
        h("span", {class: "pill busy"}, "Tags and formats ready " + (eta ? eta.replace(/^Ready /, "") : "soon"))),
      h("div", {class: "cards"}, arr(f.reels).slice(0, 8).map(x => reelCard(x))), savesLine(d.saves_line)));
    }
    body.append(h("section", {class: "panel"}, h("h2", "Later, when you have a minute"),
      h("ul", null, h("li", h("a", {href: "#settings/profile"}, "Improve your scripts: answer the other 10 questions")),
        h("li", h("a", {href: "#settings/topics"}, "Check your topics")), h("li", h("a", {href: "#competitors"}, "Add more competitors")))),
    h("div", {class: "row"}, h("button", {type: "button", class: "big", id: "onboarding-finish", onclick: () => { localStore("rv.onb." + S.me.id, "done"); go("#home"); }}, "Go to Home")));
  };
  await draw();
  onJobsDone(() => draw());
}

/* ReelVault 0.4 UI. Plain JS, no build step. Talks to the local app's JSON API (see DESIGN.md). */
"use strict";

// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const PURPOSES = [["learn", "Learn it"], ["hook", "Borrow the hook"], ["format", "Use the format"], ["remake", "Remake it better"]];
const PLABEL = Object.fromEntries(PURPOSES);
const PIDS = PURPOSES.map(p => p[0]);
const nice = s => { s = String(s || "").replace(/_/g, " ").trim(); return s ? s[0].toUpperCase() + s.slice(1) : ""; };
const arr = x => Array.isArray(x) ? x : [];
const mmss = t => { t = Math.max(0, Math.round(Number(t) || 0)); return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0"); };
const cssUrl = u => `url(&quot;${esc(String(u).replace(/["\\\n]/g, c => "\\" + c))}&quot;)`;

async function api(url, opts) {
  let res;
  try { res = await fetch(url, opts); } catch (e) {
    return {error: "Can't reach ReelVault. Is the app still open?", offline: true};
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (data === null || typeof data !== "object") {
    return {error: res.ok ? "ReelVault sent an answer this page can't read. Try again." : `Something went wrong (code ${res.status}). Try again.`};
  }
  if (!res.ok && !Array.isArray(data) && !data.error) data.error = `Something went wrong (code ${res.status}). Try again.`;
  return data;
}
const get = url => api(url);
const post = (url, body) => api(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});

function say(sel, r, good) {
  const m = typeof sel === "string" ? $(sel) : sel;
  if (!m) return;
  m.className = "msg " + (r && r.error ? "err" : "good");
  m.textContent = (r && r.error) || good || "";
}
function note(sel, text) { const m = $(sel); if (m) { m.className = "msg"; m.textContent = text; } }

let toastTimer = 0;
function toast(text) {
  const t = $("#toast"); t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

function store(key, val) {
  try { if (val === undefined) return sessionStorage.getItem(key); sessionStorage.setItem(key, val); } catch (e) { return null; }
  return null;
}

// Copy text to the clipboard. Falls back to a hidden textarea, then to selecting the text for Ctrl+C.
function copyText(text, btn, selectEl) {
  const label = btn ? btn.dataset.label || btn.textContent : "";
  if (btn) btn.dataset.label = label;
  const done = () => {
    if (btn) { btn.textContent = "Copied"; btn.classList.add("done"); setTimeout(() => { btn.textContent = label; btn.classList.remove("done"); }, 1600); }
  };
  const selectIt = () => {
    if (selectEl) {
      const r = document.createRange(); r.selectNodeContents(selectEl);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    }
    if (btn) btn.textContent = "Press Ctrl+C";
  };
  const fallback = () => {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    ok ? done() : selectIt();
  };
  try {
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  } catch (e) { fallback(); }
}
const copyBtn = (text, label = "Copy") => text ? `<button type="button" class="copy" data-copy="${esc(text)}">${esc(label)}</button>` : "";

// ---------------------------------------------------------------- state
const S = {
  status: null, offlineCount: 0, quitting: false, updating: false,
  pollSeq: 0, appliedSeq: 0, lastDoneId: undefined,
  waits: {},                           // job id -> {kind, seq, reel?}: jobs this page started and waits for
  reels: null, categories: [], creators: [], reelsError: "",
  todos: null, banks: {},              // banks["hooks:0"] = [...]
  update: null,
  reel: null, reelId: null,
  sortSel: {}, sortShow: 6, todoAll: false, todoDone: {},
  lib: {q: "", cat: "", owner: "", all: false},
  studio: {all: false, waiting: false, shown: "", wantHook: "", wantFormat: "", hooks: [], formats: []},
  profileLoaded: false, view: null,
};

// ---------------------------------------------------------------- data loading
async function loadReels() {
  const r = await get("/api/reels");
  if (r.error) { S.reelsError = r.error; S.reels = S.reels || []; return; }
  S.reelsError = "";
  S.reels = arr(r.reels);
  S.categories = arr(r.categories).length ? arr(r.categories) : [...new Set(S.reels.map(x => x.category).filter(Boolean))].sort();
  S.creators = arr(r.creators).length ? arr(r.creators) : [...new Set(S.reels.map(x => x.owner).filter(Boolean))].sort();
}
async function loadTodos() {
  const r = await get("/api/todos");
  S.todos = r.error ? [] : (Array.isArray(r) ? r : arr(r.todos));
  S.todosError = r.error || "";
}
async function loadBank(kind, all) {
  const key = kind + ":" + (all ? 1 : 0);
  if (S.banks[key]) return S.banks[key];
  const r = await get(`/api/banks/${kind}${all ? "?all=1" : ""}`);
  const list = r.error ? [] : arr(r[kind]);
  if (!r.error) S.banks[key] = list;
  return list;
}
const reelById = id => (S.reels || []).find(r => r.id === id);

// ---------------------------------------------------------------- routing
const VIEWS = ["home", "library", "reel", "studio", "settings"];
function parseRoute() {
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, "")) || "home";
  const i = h.indexOf("/");
  const view = i < 0 ? h : h.slice(0, i), arg = i < 0 ? "" : h.slice(i + 1);
  return VIEWS.includes(view) ? {view, arg} : {view: "home", arg: ""};
}
function route() {
  const {view, arg} = parseRoute();
  const changed = view !== S.view || (view === "reel" && arg !== S.reelId);
  if (/^#library/.test(S.curHash || "")) S.prevHash = S.curHash;
  if (view === "home" || view === "studio" || view === "settings") S.prevHash = "";
  S.curHash = location.hash;
  S.view = view;
  VIEWS.forEach(v => { $("#v-" + v).hidden = v !== view; });
  const navView = view === "reel" ? "library" : view;
  $$("[data-nav]").forEach(a => { if (a.dataset.nav === navView) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  document.title = {home: "Home", library: "Library", reel: "Reel", studio: "Studio", settings: "Settings"}[view] + " · ReelVault";
  if (changed) window.scrollTo(0, 0);
  if (view === "home") renderHome();
  else if (view === "library") renderLibrary(arg);
  else if (view === "reel") renderReel(arg, changed);
  else if (view === "studio") renderStudio();
  else if (view === "settings") { if (S.status) renderSettings(S.status); }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- purpose chips
function pchip(p, {on = false, data = "", guess = false, isStatic = false} = {}) {
  if (isStatic) return `<span class="pchip static p-${p} on${guess ? " guess" : ""}"${guess ? ' title="AI guess. Not confirmed yet."' : ""}>${esc(PLABEL[p] || p)}</span>`;
  return `<button type="button" class="pchip p-${p}" aria-pressed="${on}" ${data}>${esc(PLABEL[p] || p)}</button>`;
}
function reelPurposes(r) {
  const ps = arr(r.purposes).filter(p => PIDS.includes(p));
  if (!ps.length && r.purpose_guess && PIDS.includes(r.purpose_guess.purpose)) return [r.purpose_guess.purpose];
  return ps;
}
const staticChips = r => {
  const ps = reelPurposes(r);
  return ps.length ? `<div class="chips">${ps.map(p => pchip(p, {isStatic: true, guess: !r.purpose_confirmed})).join("")}</div>` : "";
};

// ---------------------------------------------------------------- status poll
async function refresh() {
  if (S.quitting) return;
  const seq = ++S.pollSeq;
  const s = await get("/api/status");
  if (seq < S.appliedSeq) return;  // an older answer arrived after a newer one: it is out of date
  S.appliedSeq = seq;
  if (s.offline || s.error) {
    if (++S.offlineCount >= 2 && !S.updating) $("#closed-banner").hidden = false;
    if (S.updating) watchUpdate(null);
    return;
  }
  S.offlineCount = 0;
  $("#closed-banner").hidden = true;
  if (S.updating) { watchUpdate(s); return; }
  S.status = s;
  adoptJob(s);

  $$("[data-version]").forEach(e => { e.textContent = s.version ? "v" + s.version : ""; });
  $$("[data-version-plain]").forEach(e => { e.textContent = s.version || ""; });
  const busy = !!s.job;
  $("#jobdot").hidden = !busy;
  const nj = $("#navjob"); nj.textContent = busy ? "Working: " + s.job : "Idle"; nj.classList.toggle("on", busy);
  const jp = $("#job"); jp.textContent = busy ? "Working: " + s.job : "Idle"; jp.className = "pill" + (busy ? " busy" : "");
  $$("#links-go,#retry,#remix,#guess-old,[data-remake-go]").forEach(b => { b.disabled = busy; });
  const igOk = !!(s.instagram || {}).connected;
  $("#run").disabled = busy || !igOk;
  $("#run-need-ig").hidden = igOk;

  renderStatusHome(s);
  renderSettings(s);
  if (s.last_update) showLastUpdate(s.last_update);
  if (S.view === "studio") renderStudioStatus(s);
  if (S.view === "reel") renderRemakeProgress();

  await checkJobs(s, seq);
}

// Each background job has its own id. The page remembers the ids it started (S.waits) and reads how each
// one ended from status.finished, so a slow or late status answer can never look like "the job ended".
function waitFor(r, kind, extra) {
  if (r && r.job_id) S.waits[r.job_id] = Object.assign({kind, seq: S.pollSeq}, extra || {});
}
// A job is running that this page didn't start (for example the page was reloaded): follow it too.
function adoptJob(s) {
  if (!s.job_id || S.waits[s.job_id]) return;
  const tag = String(s.job_tag || ""), kind = tag.split(":")[0];
  if (kind === "remake") S.waits[s.job_id] = {kind, reel: tag.slice(kind.length + 1), seq: S.pollSeq};
  else if (["run", "links", "retry", "guess", "remix"].includes(kind)) S.waits[s.job_id] = {kind, seq: S.pollSeq};
  if (kind === "remix") S.studio.waiting = true;
}
async function checkJobs(s, seq) {
  const done = arr(s.finished), ended = [];
  for (const id of Object.keys(S.waits)) {
    const w = S.waits[id], f = done.find(j => String(j.id) === id);
    if (f) ended.push([w, f]);
    else if (String(s.job_id) === id || seq <= w.seq) continue;  // still running, or this answer is older than the job
    else ended.push([w, {ok: false, message: "ReelVault closed before it finished."}]);
    delete S.waits[id];
  }
  const lastDone = done.length ? done[done.length - 1].id : 0;
  const someEnded = S.lastDoneId !== undefined && lastDone !== S.lastDoneId;
  S.lastDoneId = lastDone;
  if (someEnded || ended.length) await onJobFinished(ended);
}

async function onJobFinished(ended) {
  S.banks = {};
  await Promise.all([loadReels(), loadTodos()]);
  for (const [w, f] of ended) await jobEnded(w, f);
  if (S.view !== "reel") S.reel = null;  // open the reel fresh next time
  if (S.view === "home") renderHome();
  else if (S.view === "library") renderLibrary(parseRoute().arg);
  else if (S.view === "reel") await refreshReel();
  else if (S.view === "studio") renderStudio();
}

const sentence = m => { m = String(m || "").trim(); return !m || /[.!?]$/.test(m) ? m : m + "."; };
const whyStopped = f => sentence(f.message) || "Open Activity on the Home page to see why.";
function reelsDone(r) {
  if (!r || typeof r !== "object") return "Done.";
  const bits = [];
  if (typeof r.new === "number") bits.push(r.new ? `Found ${r.new} new reel${r.new === 1 ? "" : "s"}.` : "No new reels found.");
  if (r.ok) bits.push(`Analyzed ${r.ok}.`);
  if (r.bad) bits.push(`${r.bad} failed. Click Retry failed reels to try again.`);
  return ["Done.", ...bits].join(" ");
}
async function jobEnded(w, f) {
  const msgBox = {run: "#run-msg", links: "#links-msg", retry: "#retry-msg"}[w.kind];
  if (msgBox) {
    if (f.ok) say(msgBox, {}, reelsDone(f.result));
    else say(msgBox, {error: "Stopped. " + whyStopped(f)});
  } else if (w.kind === "guess") {
    if (f.ok) say("#guess-msg", {}, "Done. Check the guesses under Sort new reels.");
    else say("#guess-msg", {error: "The guess didn't finish. " + whyStopped(f)});
  } else if (w.kind === "remake") {
    if (f.ok) toast("Your better version is ready.");
    else S.remakeFailed = {id: w.reel, message: f.message || ""};
  } else if (w.kind === "remix") {
    S.studio.waiting = false;
    if (f.ok && f.result && f.result.script) {
      note("#remix-msg", "Done. Your scripts are below.");
      await showScript(f.result.script);
    } else {
      say("#remix-msg", {error: "The scripts didn't finish. " + whyStopped(f) + " Then try again."});
    }
  }
}

// ---------------------------------------------------------------- HOME
function lightRow(name, ok, okText, noText, fix, neutral) {
  return `<div class="light${ok ? " ok" : ""}"><span class="lamp"${neutral ? ' style="background:var(--muted)"' : ""}></span>
    <span class="name">${esc(name)}</span><span class="pill ${ok ? "ok" : neutral ? "" : "no"}">${esc(ok ? okText : noText)}</span>
    ${!ok && fix ? fix : ""}</div>`;
}
let lastLights = "";
function renderStatusHome(s) {
  const ig = s.instagram || {}, cl = s.claude || {}, ex = s.extension || {}, c = s.counts || {};
  const setup = '<a href="#settings">Set up</a>';
  const html = [
    lightRow("Claude", cl.loggedIn, "Signed in", cl.installed ? "Not signed in" : "Not installed", cl.installed ? setup : '<span class="small muted">Run the installer again.</span>'),
    lightRow("Instagram", ig.connected, ig.username ? "Connected as @" + ig.username : "Connected", "Not connected", setup),
    ex.seen || !ig.connected
      ? lightRow("Chrome extension", ex.seen, "Added", "Not added yet", setup)
      : lightRow("Chrome extension", false, "", "Not needed now", "", true),
    lightRow("Video tools", s.ffmpeg, "Ready", "Missing", '<span class="small muted">Run the installer again.</span>'),
  ].join("");
  if (html !== lastLights) { $("#lights").innerHTML = html; lastLights = html; }
  for (const k of ["done", "pending", "failed"]) $("#c-" + k).textContent = c[k] || 0;
  $("#retry").hidden = !c.failed;
  const log = $("#log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  const text = arr(s.log).length ? s.log.join("\n") : "Nothing yet.";
  if (log.textContent !== text) { log.textContent = text; if (atEnd) log.scrollTop = log.scrollHeight; }
}

function renderHome() {
  if (S.status) renderStatusHome(S.status);
  renderSort();
  renderTodos();
}

function renderSort() {
  const box = $("#sortlist");
  if (S.reels === null) { box.innerHTML = '<p class="muted">Loading...</p>'; return; }
  const unsorted = S.reels.filter(r => !r.purpose_confirmed && (r.purpose_guess || arr(r.purposes).length));
  const noGuess = S.reels.filter(r => !r.purpose_confirmed && !r.purpose_guess);  // same rule as the backend's batch guess
  $("#sort-count").textContent = unsorted.length ? unsorted.length + " to sort" : "All sorted";
  $("#sort-count").className = "pill" + (unsorted.length ? " no" : " ok");
  $("#sort-count").hidden = !S.reels.length;
  $("#guess-row").hidden = !noGuess.length;
  $("#guess-hint").textContent = noGuess.length ? `${noGuess.length} older reel${noGuess.length > 1 ? "s have" : " has"} no guess yet.` : "";
  if (!S.reels.length) {
    box.innerHTML = `<p class="muted">${S.reelsError ? esc(S.reelsError) : "No reels yet. Click <b>Get new reels</b> above to start."}</p>`;
    $("#sort-more-row").hidden = true; return;
  }
  if (!unsorted.length) {
    box.innerHTML = '<p class="muted">All sorted. New reels show up here after they are analyzed.</p>';
    $("#sort-more-row").hidden = true; return;
  }
  const show = unsorted.slice(0, S.sortShow);
  box.innerHTML = show.map(r => {
    if (!S.sortSel[r.id]) S.sortSel[r.id] = reelPurposes(r);
    const sel = S.sortSel[r.id], g = r.purpose_guess || {};
    return `<div class="sortitem" data-sort="${esc(r.id)}">
      <a class="mini" href="#reel/${encodeURIComponent(r.id)}" style="background-image:${r.thumb ? cssUrl(r.thumb) : "none"}" aria-label="Open ${esc(r.title)}"></a>
      <div class="info">
        <a class="t" href="#reel/${encodeURIComponent(r.id)}">${esc(r.title || r.id)}</a>
        <div class="chips">${PIDS.map(p => pchip(p, {on: sel.includes(p), data: `data-sortchip="${p}"`})).join("")}</div>
        ${g.reason ? `<div class="reason">AI guess: ${esc(PLABEL[g.purpose] || "")}. ${esc(g.reason)}</div>` : ""}
      </div>
      <div class="act"><button type="button" data-sortok ${sel.length ? "" : "disabled"}>Looks right</button></div>
    </div>`;
  }).join("");
  $("#sort-more-row").hidden = unsorted.length <= S.sortShow;
  $("#sort-more").textContent = `Show more (${unsorted.length - S.sortShow} left)`;
}

function renderTodos() {
  const ul = $("#todos");
  if (S.todos === null) { ul.innerHTML = '<li class="muted">Loading...</li>'; return; }
  const open = S.todos.filter(t => !t.done || S.todoDone[t.reel_id + ":" + t.index] !== undefined);
  const left = open.filter(t => !(S.todoDone[t.reel_id + ":" + t.index])).length;
  $("#todo-count").textContent = left ? left + " open" : "";
  $("#todo-count").hidden = !left;
  if (!open.length) {
    ul.innerHTML = `<li class="muted">${S.todosError ? esc(S.todosError) : "No open steps. Mark a reel as <b>Learn it</b> to get steps here."}</li>`;
    $("#todo-more-row").hidden = true; return;
  }
  const show = S.todoAll ? open : open.slice(0, 6);
  ul.innerHTML = show.map(t => {
    const key = t.reel_id + ":" + t.index, done = !!S.todoDone[key];
    return `<li class="todo${done ? " done" : ""}"><input type="checkbox" data-todo="${esc(t.reel_id)}" data-index="${t.index}" ${done ? "checked" : ""} aria-label="Done: ${esc(t.step)}">
      <div><div class="step">${esc(t.step)}${t.effort ? `<span class="effort">${esc(nice(t.effort))}</span>` : ""}</div>
      ${t.why ? `<div class="why">${esc(t.why)}</div>` : ""}
      <div class="from muted">From <a href="#reel/${encodeURIComponent(t.reel_id)}">${esc(t.title || "a reel")}</a></div></div></li>`;
  }).join("");
  $("#todo-more-row").hidden = S.todoAll || open.length <= 6;
  $("#todo-more").textContent = `Show all steps (${open.length})`;
}

async function tickTodo(reelId, index, done, box) {
  const r = await post(`/api/reels/${encodeURIComponent(reelId)}/todo`, {index, done});
  if (r.error) { if (box) box.checked = !done; toast(r.error); return false; }
  S.todoDone[reelId + ":" + index] = done;
  if (S.reel && S.reelId === reelId) {
    const ex = S.reel.extra = S.reel.extra || {};
    const set = new Set(arr(ex.todo_done)); done ? set.add(index) : set.delete(index); ex.todo_done = [...set];
  }
  return true;
}

// ---------------------------------------------------------------- LIBRARY
function fillSelect(sel, values, first) {
  const el = $(sel), cur = el.value;
  const html = `<option value="">${esc(first)}</option>` + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; el.value = values.includes(cur) ? cur : ""; }
}
const words = () => S.lib.q.toLowerCase().split(/\s+/).filter(Boolean);
const matches = (hay, extra) => {
  const h = hay.join(" ").toLowerCase();
  return words().every(w => h.includes(w)) && (!S.lib.cat || extra.cat === S.lib.cat) && (!S.lib.owner || extra.owner === S.lib.owner);
};
const reelHay = r => [r.title, r.owner, r.summary, r.category, r.subcategory, arr(r.tags).join(" "), (r.hook || {}).spoken,
  (r.hook || {}).on_screen_text, r.structure_name, r.content_format, reelPurposes(r).map(p => PLABEL[p]).join(" ")];

function reelCard(r) {
  return `<a class="rcard" href="#reel/${encodeURIComponent(r.id)}">
    <div class="cover" style="background-image:${r.thumb ? cssUrl(r.thumb) : "none"}">${r.thumb ? "" : '<span class="noimg">No cover</span>'}
      ${r.duration ? `<span class="dur">${mmss(r.duration)}</span>` : ""}</div>
    <div class="body"><div class="title">${esc(r.title || r.id)}</div>
      <div class="sub">@${esc(r.owner || "unknown")}${r.category ? " · " + esc(r.category) : ""}</div>
      ${staticChips(r)}</div></a>`;
}

async function renderLibrary(tab) {
  tab = ["learn", "hooks", "formats", "remakes"].includes(tab) ? tab : "all";
  $$("#lib-tabs a").forEach(a => a.setAttribute("aria-selected", String(a.dataset.tab === tab)));
  const body = $("#lib-body");
  if (S.reels === null) { body.innerHTML = '<p class="muted">Loading...</p>'; return; }
  fillSelect("#lib-cat", S.categories, "All topics");
  fillSelect("#lib-owner", S.creators.map(String), "All creators");
  const isBank = tab === "hooks" || tab === "formats";
  $("#lib-all-wrap").hidden = !isBank;
  $("#lib-all").checked = S.lib.all;
  $("#lib-all-label").textContent = tab === "hooks" ? "Show hooks from every reel" : "Show formats from every reel";

  if (S.reelsError && !S.reels.length) { body.innerHTML = `<div class="empty">${esc(S.reelsError)}</div>`; $("#lib-count").textContent = ""; return; }

  if (!isBank) {
    let list = S.reels;
    if (tab === "learn") list = list.filter(r => reelPurposes(r).includes("learn"));
    if (tab === "remakes") list = list.filter(r => r.has_remake);
    const total = list.length;
    list = list.filter(r => matches(reelHay(r), {cat: r.category, owner: r.owner}));
    $("#lib-count").textContent = total === list.length ? `${total} reel${total === 1 ? "" : "s"}` : `${list.length} of ${total} reels`;
    if (!list.length) {
      const why = !S.reels.length ? "No reels yet. Go to <a href=\"#home\">Home</a> and click <b>Get new reels</b>."
        : total ? "No reels match. Clear the search or a filter."
        : tab === "learn" ? "No reels to learn from yet. Open a reel and tap <b>Learn it</b>."
        : tab === "remakes" ? "No remakes yet. Open a reel and click <b>Make it better</b>." : "No reels here yet.";
      body.innerHTML = `<div class="empty">${why}</div>`; return;
    }
    body.innerHTML = `<div class="cards">${list.map(reelCard).join("")}</div>`;
    return;
  }

  const kind = tab, all = S.lib.all;
  const key = kind + ":" + (all ? 1 : 0);
  if (!S.banks[key]) body.innerHTML = '<p class="muted">Loading...</p>';
  const items = await loadBank(kind, all);
  if (parseRoute().arg !== tab || S.lib.all !== all) return;  // user moved on
  const meta = id => reelById(id) || {};
  const shown = items.map((x, i) => [x, i]).filter(([x]) => {
    const m = meta(x.reel_id);
    const hay = kind === "hooks" ? [x.title, x.owner, x.spoken, x.on_screen_text, x.hook_type, x.why_it_works, arr(x.variations).join(" ")]
      : [x.title, x.owner, x.structure_name, arr(x.beats).join(" "), x.pacing, x.template];
    return matches(hay, {cat: m.category, owner: x.owner || m.owner});
  });
  const noun = kind === "hooks" ? "hook" : "format";
  $("#lib-count").textContent = shown.length === items.length ? `${items.length} ${noun}${items.length === 1 ? "" : "s"}` : `${shown.length} of ${items.length} ${noun}s`;
  if (!shown.length) {
    body.innerHTML = `<div class="empty">${items.length ? "Nothing matches. Clear the search or a filter."
      : !S.reels.length ? "No reels yet. Go to <a href=\"#home\">Home</a> and click <b>Get new reels</b>."
      : kind === "hooks" ? "No hooks saved yet. Open a reel and tap <b>Borrow the hook</b>. Or tick <b>Show hooks from every reel</b>."
      : "No formats saved yet. Open a reel and tap <b>Use the format</b>. Or tick <b>Show formats from every reel</b>."}</div>`;
    return;
  }
  body.innerHTML = `<div class="bank">${shown.map(([x]) => kind === "hooks" ? hookCard(x) : formatCard(x)).join("")}</div>`;
}

const srcLine = x => `<div class="src">From <a href="#reel/${encodeURIComponent(x.reel_id)}">${esc(x.title || "this reel")}</a>${x.owner ? " · @" + esc(x.owner) : ""}</div>`;

function hookCard(x) {
  const line = x.spoken || x.on_screen_text || "";
  const vars = arr(x.variations);
  return `<article class="bcard p-hook">
    <div class="row-tight">${x.hook_type ? `<span class="tag">${esc(nice(x.hook_type))}</span>` : ""}</div>
    <div class="big">${esc(line ? "“" + line + "”" : "No spoken hook")}</div>
    ${x.spoken && x.on_screen_text ? `<div class="small muted">On screen: ${esc(x.on_screen_text)}</div>` : ""}
    ${x.why_it_works ? `<p class="small">${esc(x.why_it_works)}</p>` : ""}
    ${x.formula ? `<div class="blk"><span class="label">Formula ${copyBtn(x.formula)}</span><div class="formula">${esc(x.formula)}</div></div>` : ""}
    ${vars.length ? `<details class="fold"><summary>${vars.length} more hooks like this</summary><div class="stack">${vars.map(v => `<div class="copyrow"><span>${esc(v)}</span>${copyBtn(v)}</div>`).join("")}</div></details>` : ""}
    ${srcLine(x)}
    <div class="acts">${copyBtn(line, "Copy hook")}<button type="button" class="ghost small-btn" data-use-hook="${esc(x.reel_id)}">Use in Studio</button></div>
  </article>`;
}

function beatChips(beats) {
  return arr(beats).length ? `<div class="beatchips">${arr(beats).map(b => `<span class="tag">${esc(nice(b))}</span>`).join('<span class="arrow">›</span>')}</div>` : "";
}
function formatCard(x) {
  const ideas = arr(x.script_ideas);
  return `<article class="bcard p-format">
    <div class="big">${esc(x.structure_name || "Untitled format")}</div>
    ${beatChips(x.beats)}
    ${x.pacing ? `<p class="small muted">${esc(x.pacing)}</p>` : ""}
    ${x.template ? `<div class="blk"><span class="label">Template ${copyBtn(x.template)}</span><pre class="tpl">${esc(x.template)}</pre></div>` : ""}
    ${ideas.length ? `<details class="fold"><summary>${ideas.length} video idea${ideas.length > 1 ? "s" : ""}</summary><div class="stack">${ideas.map(i => `<div class="copyrow"><span>${esc(typeof i === "string" ? i : i.title)}${i.hook ? `<br><span class="small muted">Hook: ${esc(i.hook)}</span>` : ""}</span></div>`).join("")}</div></details>` : ""}
    ${srcLine(x)}
    <div class="acts">${copyBtn(x.template, "Copy template")}<button type="button" class="ghost small-btn" data-use-format="${esc(x.reel_id)}">Use in Studio</button></div>
  </article>`;
}

// ---------------------------------------------------------------- REEL PAGE
async function renderReel(id, opened) {
  const body = $("#reel-body");
  $("#reel-back").href = S.prevHash || "#library";
  if (!id) { body.innerHTML = '<div class="empty">No reel picked. <a href="#library">Go to the library.</a></div>'; return; }
  if (S.reelId === id && S.reel && opened) { renderReelPage(); refreshReel(); return; }  // show it now, then check for news
  if (S.reelId !== id || !S.reel) {
    S.reelId = id; S.reel = null;
    body.innerHTML = '<p class="muted">Loading...</p>';
    const r = await get("/api/reels/" + encodeURIComponent(id));
    if (S.reelId !== id) return;
    if (r.error) { body.innerHTML = `<div class="empty">Couldn't open this reel. ${esc(r.error)} <a href="#library">Back to the library.</a></div>`; return; }
    S.reel = r;
  }
  renderReelPage();
}

// Load the open reel again. Repaint only what changed, so a playing video or a half-typed form is kept.
async function refreshReel() {
  const id = S.reelId;
  if (!id || !S.reel) return;
  const r = await get("/api/reels/" + encodeURIComponent(id));
  if (S.reelId !== id || r.error || !S.reel) { renderRemakeProgress(); return; }
  const changed = JSON.stringify(r) !== JSON.stringify(S.reel);
  S.reel = r;
  if (changed && S.view === "reel" && $("#reel-right")) {
    paintReelChips();
    if (r.purpose_confirmed) { const gn = $("#guessnote"); if (gn) gn.remove(); }
    renderReelRight();
  } else renderRemakeProgress();
}

const A = r => r.analysis || r;
function renderReelPage() {
  const r = S.reel; if (!r) return;
  const a = A(r), meta = r.meta || {}, media = r.media || {};
  const id = S.reelId;
  const thumb = media.thumb || (reelById(id) || {}).thumb;
  const url = meta.url || r.url || (reelById(id) || {}).url || "";
  const owner = meta.owner || r.owner || "";
  const ps = reelPurposes(r);
  const g = r.purpose_guess || {};
  let mediaHtml;
  if (media.video) mediaHtml = `<video id="vid" class="media" controls playsinline preload="metadata" src="${esc(media.video)}"${thumb ? ` poster="${esc(thumb)}"` : ""}></video>`;
  else if (thumb) mediaHtml = `<a class="medialink" href="${esc(url)}" target="_blank" rel="noopener"><img class="media" src="${esc(thumb)}" alt="Reel cover"><span class="play">Watch on Instagram ↗</span></a>`;
  else mediaHtml = `<div class="media nomedia">No video or cover saved for this reel.</div>`;
  const dur = r.duration || meta.duration;
  $("#reel-body").innerHTML = `<div class="reel">
    <aside class="reel-left">
      ${mediaHtml}
      <h1>${esc(a.title || id)}</h1>
      <div class="sub">@${esc(owner)}${dur ? " · " + mmss(dur) : ""}${meta.posted_at ? " · " + esc(meta.posted_at) : ""}</div>
      ${a.category ? `<div class="sub">${esc(a.category)}${a.subcategory ? " › " + esc(a.subcategory) : ""}</div>` : ""}
      <span class="label" style="margin-top:6px">Why you saved it</span>
      <div class="chips" id="reel-chips">${PIDS.map(p => pchip(p, {on: ps.includes(p), data: `data-reelchip="${p}"`})).join("")}</div>
      <div class="msg" id="reel-chips-msg"></div>
      ${!r.purpose_confirmed && g.purpose ? `<div class="guessnote" id="guessnote">AI guess: <b>${esc(PLABEL[g.purpose] || g.purpose)}</b>. ${esc(g.reason || "")}
        <div class="row-tight" style="margin-top:6px"><button type="button" class="small-btn" data-reelconfirm>Looks right</button></div></div>` : ""}
      ${arr(a.tags).length ? `<div class="chips">${a.tags.map(t => `<span class="tag">#${esc(t)}</span>`).join("")}</div>` : ""}
      ${url ? `<a class="btn ghost" href="${esc(url)}" target="_blank" rel="noopener">Watch on Instagram ↗</a>` : ""}
    </aside>
    <div class="reel-right" id="reel-right"></div>
  </div>`;
  renderReelRight();
}

function renderReelRight() {
  const r = S.reel, box = $("#reel-right"); if (!r || !box) return;
  const ps = reelPurposes(r);
  const secs = PIDS.filter(p => ps.includes(p)).map(p => SECTION[p](r));
  box.innerHTML = (secs.length ? secs.join("") : `<div class="empty">Tap why you saved this reel. The right tools show up here.</div>`) + moreSection(r, ps);
  renderRemakeProgress();
}

const blk = (label, inner, copy) => inner ? `<div class="blk"><span class="label">${esc(label)}${copy ? " " + copyBtn(copy) : ""}</span>${inner}</div>` : "";
const ul = xs => arr(xs).length ? `<ul>${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
const hasVideo = () => !!(S.reel && S.reel.media && S.reel.media.video);
const ts = t => hasVideo() ? `<button type="button" class="ts" data-seek="${Number(t) || 0}" title="Play from here">${mmss(t)}</button>` : `<span class="ts">${mmss(t)}</span>`;
// The hook formula: the first sentence of the template's first line (the same rule as the Hooks tab).
const formulaOf = s => (String(s || "").split("\n").map(x => x.trim()).find(Boolean) || "").split(/(?<=[.!?])\s+/)[0];

const SECTION = {
  learn(r) {
    const a = A(r), done = new Set(arr((r.extra || {}).todo_done));
    const steps = arr(a.action_plan);
    const long = (a.summary || "").length > 240;
    return `<section class="psec p-learn"><h2>Learn it</h2>
      ${a.summary ? blk("In short", `<p class="${long ? "clamp" : ""}" id="sum">${esc(a.summary)}</p>${long ? '<button type="button" class="linkbtn" data-unclamp>Show all</button>' : ""}`) : ""}
      ${blk("Key takeaways", ul(a.key_takeaways))}
      ${steps.length ? blk("Do this", `<ul class="checklist">${steps.map((s, i) => `<li class="${done.has(i) ? "done" : ""}"><label>
        <input type="checkbox" data-reeltodo="${i}" ${done.has(i) ? "checked" : ""}><span><span class="s">${esc(s.step)}</span>${s.effort ? `<span class="effort">${esc(nice(s.effort))}</span>` : ""}<span class="w">${esc(s.why || "")}</span></span></label></li>`).join("")}</ul>`) : ""}
      ${arr(a.notable_quotes).length ? blk("Quotes worth saving", a.notable_quotes.map(q => `<div class="copyrow"><span class="quote">${esc(q)}</span>${copyBtn(q)}</div>`).join("")) : ""}
      ${arr(a.claims_to_check).length ? blk("Check before you trust", `<div class="warnbox"><p class="small" style="margin-bottom:6px">Check these numbers and claims before you repeat them.</p>${ul(a.claims_to_check)}</div>`) : ""}
      ${a.client_explainer ? blk("Explain it to a client", `<p>${esc(a.client_explainer)}</p>`, a.client_explainer) : ""}
    </section>`;
  },
  hook(r) {
    const a = A(r), h = (a.skeleton || {}).hook || {};
    const line = h.spoken || h.on_screen_text || "";
    const formula = formulaOf(a.fill_in_template);
    return `<section class="psec p-hook"><h2>Borrow the hook</h2>
      <div class="hookcard"><span class="label">The hook${h.ends_at_seconds ? " · first " + Math.round(h.ends_at_seconds) + "s" : ""} ${copyBtn(line)}</span>
        <div class="said">${esc(line ? "“" + line + "”" : "No spoken hook")}</div>
        <dl class="kv">
          ${h.on_screen_text ? `<dt>On screen</dt><dd>${esc(h.on_screen_text)}</dd>` : ""}
          ${h.visual ? `<dt>What you see</dt><dd>${esc(h.visual)}</dd>` : ""}
          ${h.hook_type ? `<dt>Type</dt><dd>${esc(nice(h.hook_type))}</dd>` : ""}
        </dl></div>
      ${blk("Why it works", h.why_it_works ? `<p>${esc(h.why_it_works)}</p>` : "")}
      ${formula ? blk("Hook formula", `<div class="formula">${esc(formula)}</div>`, formula) : ""}
      ${arr(a.hook_variations).length ? blk("Hooks like this for you", a.hook_variations.map(v => `<div class="copyrow"><span>${esc(v)}</span>${copyBtn(v)}</div>`).join("")) : ""}
    </section>`;
  },
  format(r) {
    const a = A(r), sk = a.skeleton || {};
    return `<section class="psec p-format"><h2>Use the format</h2>
      ${blk("Structure", `<div class="formula" style="font-family:var(--display)">${esc(sk.structure_name || "")}</div>`)}
      ${arr(sk.meat).length ? blk("Beat map", `<div class="beats">${sk.meat.map(b => `<div class="beat">${ts(b.start_seconds)}<div>${b.purpose ? `<span class="tag">${esc(nice(b.purpose))}</span> ` : ""}${esc(b.point)}<span class="v">On screen: ${esc(b.visual || "")}</span></div></div>`).join("")}</div>`) : ""}
      ${blk("Pacing", sk.pacing ? `<p>${esc(sk.pacing)}</p>` : "")}
      ${blk("What keeps people watching", ul(sk.retention_devices))}
      ${a.fill_in_template ? blk("Fill-in template", `<pre>${esc(a.fill_in_template)}</pre>`, a.fill_in_template) : ""}
      ${arr(a.script_ideas).length ? blk("Video ideas with this format", a.script_ideas.map(i => `<div class="idea"><b>${esc(i.title)}</b>
        ${i.hook ? `<div><span class="muted">Hook:</span> ${esc(i.hook)}</div>` : ""}${arr(i.outline).length ? `<ol>${i.outline.map(o => `<li>${esc(o)}</li>`).join("")}</ol>` : ""}
        ${i.cta ? `<div><span class="muted">Call to action:</span> ${esc(i.cta)}</div>` : ""}</div>`).join("")) : ""}
    </section>`;
  },
  remake(r) {
    const remakes = arr((r.extra || {}).remakes);
    const pick = Math.min(S.remakePick ?? remakes.length - 1, remakes.length - 1);
    const m = remakes[pick];
    let inner = "";
    if (m) {
      const cap = [m.caption, arr(m.hashtags).map(t => t.startsWith("#") ? t : "#" + t).join(" ")].filter(Boolean).join("\n\n");
      inner = `${remakes.length > 1 ? `<label class="field"><span>Version</span><select id="remake-pick">${remakes.map((x, i) => `<option value="${i}" ${i === pick ? "selected" : ""}>Version ${i + 1}: for ${esc(x.made_for || "me")}${x.created_at ? " · " + esc(String(x.created_at).slice(0, 10)) : ""}</option>`).join("")}</select></label>`
          : `<p class="small muted">Made for ${esc(m.made_for || "me")}${m.created_at ? " · " + esc(String(m.created_at).slice(0, 10)) : ""}</p>`}
        ${blk("The core idea", m.core_idea ? `<p>${esc(m.core_idea)}</p>` : "")}
        ${blk("What's weak in the original", ul(m.whats_weak))}
        ${blk("Your angle", m.your_angle ? `<p>${esc(m.your_angle)}</p>` : "")}
        ${m.script ? blk("Script", `<pre id="remake-script">${esc(m.script)}</pre>`, m.script) : ""}
        ${arr(m.shot_list).length ? blk("Shot list", `<ol>${m.shot_list.map(s => `<li>${esc(s)}</li>`).join("")}</ol>`) : ""}
        ${cap ? blk("Caption", `<pre>${esc(cap)}</pre>`, cap) : ""}
        ${arr(m.too_close).length ? blk("Too close to the original", `<div class="warnbox stack"><p class="small">Change these lines so it's your own.</p>${m.too_close.map(f => `<div class="flag">${esc(f.line)}<span class="why">${esc(f.why)}</span></div>`).join("")}</div>`)
          : `<p class="small" style="color:var(--ok)">Nothing is too close to the original.</p>`}
        <details class="fold"><summary>Make another version</summary><div class="stack">${remakeForm(r)}</div></details>`;
    } else {
      inner = `<p>Get your own, stronger version of this reel. Claude keeps the idea, fixes what's weak and writes it for you.</p>${remakeForm(r)}`;
    }
    return `<section class="psec p-remake" id="remake-sec"><h2>Remake it better</h2>
      <div class="progress" id="remake-progress" hidden><span class="spinner"></span><span>Claude is writing your better version. This takes a minute or two.</span></div>
      <div class="msg" id="remake-msg"></div>
      <div id="remake-body" class="stack">${inner}</div></section>`;
  },
};

function remakeForm() {
  return `<div class="remakeform">
    <div class="forrow"><span class="muted">For:</span>
      <label><input type="radio" name="rm-for" value="me" checked> Me</label>
      <label><input type="radio" name="rm-for" value="client"> A client</label>
      <input id="rm-client" placeholder="Client name" aria-label="Client name" hidden style="flex:1 1 160px;width:auto"></div>
    <textarea id="rm-notes" placeholder="Anything Claude should know? (optional)" style="min-height:70px"></textarea>
    <div class="row"><button type="button" data-remake-go ${S.status && S.status.job ? "disabled" : ""}>Make it better</button></div>
  </div>`;
}

function renderRemakeProgress() {
  const p = $("#remake-progress"); if (!p) return;
  const waiting = Object.values(S.waits).some(w => w.kind === "remake" && w.reel === S.reelId);
  p.hidden = !waiting;
  const b = $("#remake-body"); if (b) b.hidden = waiting;
  if (S.remakeFailed && S.remakeFailed.id === S.reelId) {
    const m = S.remakeFailed.message;
    S.remakeFailed = null;
    say("#remake-msg", {error: "The remake didn't finish." + (m ? " " + sentence(m) : "") + " Try again."});
  }
}

function moreSection(r, ps) {
  const a = A(r), sk = a.skeleton || {}, h = sk.hook || {}, c = sk.cta || {}, v = a.visuals || {}, meta = r.meta || {};
  const tr = r.transcript || {}, segs = arr(tr.segments);
  const parts = [];
  if (!ps.includes("learn") && a.summary) parts.push(moresec("Summary", `<p>${esc(a.summary)}</p>`));
  parts.push(moresec("Full skeleton", `
    ${sk.structure_name ? `<p><b>Structure:</b> ${esc(sk.structure_name)}${sk.pacing ? ` · <b>Pacing:</b> ${esc(sk.pacing)}` : ""}</p>` : ""}
    <div class="part hook"><div class="label">Hook${h.hook_type ? " · " + esc(nice(h.hook_type)) : ""}${h.ends_at_seconds != null ? " · 0–" + Math.round(h.ends_at_seconds) + "s" : ""}</div>
      <div><span class="muted">Said:</span> ${esc(h.spoken || "-")}</div><div><span class="muted">On screen:</span> ${esc(h.on_screen_text || "-")}</div>
      <div><span class="muted">Visual:</span> ${esc(h.visual || "-")}</div>${h.why_it_works ? `<div class="muted small" style="margin-top:4px">${esc(h.why_it_works)}</div>` : ""}</div>
    <div class="part meat"><div class="label">Body · ${arr(sk.meat).length} beats</div><div class="beats">${arr(sk.meat).map(b => `<div class="beat">${ts(b.start_seconds)}<div><b>${esc(nice(b.purpose))}:</b> ${esc(b.point)}<span class="v">On screen: ${esc(b.visual || "")}</span></div></div>`).join("")}</div></div>
    <div class="part cta"><div class="label">Call to action${c.cta_type ? " · " + esc(nice(c.cta_type)) : ""}${c.placement ? " · " + esc(c.placement) : ""} ${copyBtn(c.text)}</div>
      <div>${esc(c.text || "-")}${c.keyword ? ` <span class="tag">Keyword: ${esc(c.keyword)}</span>` : ""}</div></div>
    ${arr(sk.retention_devices).length ? `<div><span class="label">Retention devices</span>${ul(sk.retention_devices)}</div>` : ""}`));
  parts.push(moresec("Visuals", `
    ${v.what_the_visuals_say ? `<p><b>What the visuals say:</b> ${esc(v.what_the_visuals_say)}</p>` : ""}
    ${v.visual_style ? `<p><b>Style:</b> ${esc(v.visual_style)}</p>` : ""}
    ${arr(v.editing_techniques).length ? `<div><span class="label">Editing</span>${ul(v.editing_techniques)}</div>` : ""}
    ${arr(v.on_screen_text).length ? `<div><span class="label">On-screen text</span>${ul(v.on_screen_text)}</div>` : ""}
    ${arr((r.media || {}).frames).length ? `<div class="chips">${r.media.frames.slice(0, 8).map(f => `<img src="${esc(f)}" alt="" loading="lazy" style="width:64px;height:96px;object-fit:cover;border-radius:6px;background:var(--chip)">`).join("")}</div>` : ""}`));
  if (!ps.includes("hook") && arr(a.hook_variations).length) parts.push(moresec("Hook ideas", a.hook_variations.map(x => `<div class="copyrow"><span>${esc(x)}</span>${copyBtn(x)}</div>`).join("")));
  if (!ps.includes("format") && arr(a.script_ideas).length) parts.push(moresec("Video ideas", a.script_ideas.map(i => `<div class="idea"><b>${esc(i.title)}</b>${i.hook ? `<div><span class="muted">Hook:</span> ${esc(i.hook)}</div>` : ""}</div>`).join("")));
  if (a.ready_script) parts.push(moresec("Ready-to-shoot script", `<pre>${esc(a.ready_script)}</pre>`, a.ready_script));
  parts.push(moresec("Transcript", segs.length ? `<div class="beats">${segs.map(s => `<div class="beat">${ts(s.start)}<div>${esc(s.text)}</div></div>`).join("")}</div>`
    : `<p class="muted">${esc(tr.text || "No speech in this reel.")}</p>`, tr.text || segs.map(s => s.text).join(" ")));
  if (meta.caption) parts.push(moresec("Original caption", `<p class="caption">${esc(meta.caption)}</p>`, meta.caption));
  if (a.why_it_performed) parts.push(moresec("Why it performed", `<p>${esc(a.why_it_performed)}</p>`));
  if (a.confidence_notes) parts.push(moresec("Notes from Claude", `<p class="muted">${esc(a.confidence_notes)}</p>`));
  if (!ps.includes("remake")) parts.push(moresec("Want your own version?", `<p>Tap <b>Remake it better</b> at the top. Claude writes a stronger version for you or a client.</p>`));
  return `<details class="more fold" id="more"${ps.length ? "" : " open"}><summary>More</summary>${parts.join("")}</details>`;
}
const moresec = (title, inner, copy) => `<div class="moresec"><h3 class="panel-head">${esc(title)}${copy ? copyBtn(copy) : ""}</h3>${inner}</div>`;

async function setReelPurposes(next) {
  const r = S.reel, id = S.reelId;
  if (!next.length) { say("#reel-chips-msg", {error: "Keep at least one. Tap another one first."}); return; }
  const before = reelPurposes(r), wasConfirmed = r.purpose_confirmed;
  r.purposes = next; r.purpose_confirmed = 1;
  paintReelChips(); renderReelRight();
  const res = await post(`/api/reels/${encodeURIComponent(id)}/purposes`, {purposes: next});
  if (res.error) {
    r.purposes = before; r.purpose_confirmed = wasConfirmed;
    paintReelChips(); renderReelRight();
    say("#reel-chips-msg", res); return;
  }
  const gn = $("#guessnote"); if (gn) gn.remove();
  const s = reelById(id); if (s) { s.purposes = next; s.purpose_confirmed = 1; }
  say("#reel-chips-msg", {}, "Saved.");
  purposesChanged();
  setTimeout(() => { const m = $("#reel-chips-msg"); if (m && m.textContent === "Saved.") m.textContent = ""; }, 1800);
}
// Purposes decide what is in the Hooks and Formats banks and in "This week": load those again.
async function purposesChanged() {
  S.banks = {};
  await loadTodos();
  if (S.view === "home") renderTodos();
}
function paintReelChips() {
  const ps = reelPurposes(S.reel);
  $$("[data-reelchip]").forEach(b => b.setAttribute("aria-pressed", String(ps.includes(b.dataset.reelchip))));
}

async function startRemake(btn) {
  const forWho = ($('input[name="rm-for"]:checked') || {}).value || "me";
  const client = ($("#rm-client") || {}).value || "";
  if (forWho === "client" && !client.trim()) { say("#remake-msg", {error: "Type the client's name first."}); return; }
  btn.disabled = true;
  const id = S.reelId;
  const r = await post(`/api/reels/${encodeURIComponent(id)}/remake`, {for: forWho === "client" ? client.trim() : "me", notes: ($("#rm-notes") || {}).value || ""});
  if (r.error) { btn.disabled = false; say("#remake-msg", r); return; }
  waitFor(r, "remake", {reel: id});
  say("#remake-msg", {}, "");
  renderRemakeProgress();
}

// ---------------------------------------------------------------- STUDIO
async function renderStudio() {
  if (S.status) fillSelect("#cat", arr(S.status.categories), "All my reels");
  $("#st-all").checked = S.studio.all;
  const [hooks, formats] = await Promise.all([loadBank("hooks", S.studio.all), loadBank("formats", S.studio.all)]);
  // "Use in Studio" picked something that isn't in the short list: show every reel.
  if (!S.studio.all && ((S.studio.wantHook && !hooks.some(h => h.reel_id === S.studio.wantHook)) ||
      (S.studio.wantFormat && !formats.some(f => f.reel_id === S.studio.wantFormat)))) {
    S.studio.all = true; return renderStudio();
  }
  S.studio.hooks = hooks; S.studio.formats = formats;  // what the pickers list (the bank cache can be cleared)
  const hs = $("#st-hook"), fs = $("#st-format");
  const hv = S.studio.wantHook || hs.value, fv = S.studio.wantFormat || fs.value;
  hs.innerHTML = `<option value="">No hook. Let Claude pick.</option>` + hooks.map(h => `<option value="${esc(h.reel_id)}">${esc(short(h.spoken || h.on_screen_text, 70))}${h.owner ? " (@" + esc(h.owner) + ")" : ""}</option>`).join("");
  fs.innerHTML = `<option value="">No format. Let Claude pick.</option>` + formats.map(f => `<option value="${esc(f.reel_id)}">${esc(short(f.structure_name, 70))}${f.owner ? " (@" + esc(f.owner) + ")" : ""}</option>`).join("");
  hs.value = hooks.some(h => h.reel_id === hv) ? hv : "";
  fs.value = formats.some(f => f.reel_id === fv) ? fv : "";
  S.studio.wantHook = S.studio.wantFormat = "";
  studioPreview();
  if (S.status) renderStudioStatus(S.status);
}
const short = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
function studioPick() {
  const hv = $("#st-hook").value, fv = $("#st-format").value;
  return {hook: hv ? S.studio.hooks.find(h => h.reel_id === hv) : undefined, format: fv ? S.studio.formats.find(f => f.reel_id === fv) : undefined};
}
function studioPreview() {
  const {hook, format} = studioPick(), box = $("#st-preview");
  const bits = [];
  if (hook) bits.push(`<div><span class="pchip static p-hook on">Hook</span> “${esc(hook.spoken || hook.on_screen_text)}”</div>`);
  if (format) bits.push(`<div><span class="pchip static p-format on">Format</span> ${esc(format.structure_name)}${arr(format.beats).length ? " " + beatChips(format.beats) : ""}</div>`);
  box.innerHTML = bits.join(""); box.hidden = !bits.length;
}
function renderStudioStatus(s) {
  if (S.studio.waiting) return;
  const sc = s.result && s.result.script;
  if (sc && S.studio.shown !== sc) showScript(sc);
}
const vaultUrl = p => "/vault/" + String(p).split("/").map(encodeURIComponent).join("/");
async function showScript(path) {
  S.studio.shown = path;
  let text = "";
  try { const res = await fetch(vaultUrl(path)); text = res.ok ? await res.text() : ""; } catch (e) { text = ""; }
  $("#st-result").hidden = false;
  $("#st-text").textContent = text || "Couldn't load the scripts. Click Open file to see them.";
  $("#st-open").href = vaultUrl(path);
}

// ---------------------------------------------------------------- SETTINGS
function pill(el, ok, yes, no) { el.className = "pill " + (ok ? "ok" : "no"); el.textContent = ok ? yes : no; }
function renderSettings(s) {
  const ig = s.instagram || {}, cl = s.claude || {}, ex = s.extension || {}, ch = s.chromeLogin || {};
  pill($("#s-claude"), cl.loggedIn, "Signed in", cl.installed ? "Not signed in" : "Not installed");
  $("#claude-signin").hidden = !!cl.loggedIn;
  pill($("#s-ffmpeg"), s.ffmpeg, "Ready", "Missing");
  $("#ffmpeg-hint").textContent = s.ffmpeg ? "ReelVault uses these to read the video and the sound." : "The video tools are missing. Run the ReelVault installer again.";
  pill($("#s-ig"), ig.connected, "Connected", "Not connected");
  $("#ig-user").textContent = ig.username ? "@" + ig.username : "";
  $("#ext-path").textContent = ex.folder || "";
  pill($("#ext-status"), ex.seen, "Added", "Not added yet");
  if (ig.connected && !S.igReconnect) S.showExt = false;
  $("#ext-setup").hidden = !(S.showExt || (!ex.seen && !ig.connected));
  $("#ig-connect").disabled = !!s.igWaiting;
  const cm = $("#ig-connect-msg");
  if (s.igWaiting && !S.igMsgSet) { cm.className = "msg"; cm.textContent = "Waiting for your login in the Instagram tab..."; }
  if (ig.connected && cm.textContent.startsWith("Waiting")) { cm.className = "msg good"; cm.textContent = "Connected."; }
  if (ch.status === "done") S.igReconnect = false;
  const showForm = !ig.connected || S.igReconnect || ch.status === "waiting";
  $("#ig-form").hidden = !showForm;
  $("#ig-again-row").hidden = showForm;
  if (ch.message) {
    const m = $("#ig-chrome-msg");
    m.className = "msg " + (ch.status === "error" ? "err" : ch.status === "done" ? "good" : "");
    m.textContent = ch.message;
  }
  $("#ig-chrome").disabled = ch.status === "waiting";
  if (!S.profileLoaded && typeof s.profile === "string") { $("#profile").value = s.profile; S.profileLoaded = true; }
  if (!S.schedLoaded && s.schedule) {  // show the current daily time, e.g. "11/10/2026 8:00:00 PM" -> 20:00
    const m = String(s.schedule).match(/(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?\s*$/i);
    if (m) { let h = Number(m[1]) % 12; if (!m[3]) h = Number(m[1]); else if (/pm/i.test(m[3])) h += 12; $("#sched-time").value = String(h).padStart(2, "0") + ":" + m[2]; }
    S.schedLoaded = true;
  }
  const sm = $("#sched-msg");
  if (!sm.dataset.hold) { sm.className = "msg"; sm.textContent = s.schedule ? "On. Next run: " + s.schedule : "Off. ReelVault only runs when you click Get new reels."; }
  $("#home-path").textContent = s.home || "";
  $("#open-lib").hidden = !s.library;
}

// ---------------------------------------------------------------- UPDATES
async function checkUpdate(show) {
  if (show) { note("#update-msg", "Checking..."); $("#update-check").disabled = true; }
  const u = await get("/api/update" + (show ? "?refresh=1" : ""));
  if (show) $("#update-check").disabled = false;
  S.update = u;
  paintUpdate();
  showLastUpdate(u.last_update);
  if (show) {
    if (checkFailed(u)) say("#update-msg", {error: "Couldn't check for updates. " + u.error});
    else say("#update-msg", {}, u.available ? `Version ${u.latest} is ready.` : "You have the newest version.");
  }
}
// "error" is either a failed check (then there is no "latest") or the failed last update (see last_update).
const checkFailed = u => !!(u && u.error) && !u.latest;
// The result of the last self-update (shown once, right after the app comes back).
function showLastUpdate(lu) {
  if (!lu || typeof lu !== "object") return;
  const key = String(lu.at || lu.version || "x");
  if (S.lastUpdateShown === key) return;
  S.lastUpdateShown = key;
  if (lu.ok) {
    if (store("rv-last-update") !== key) toast("ReelVault updated" + (lu.version ? " to version " + lu.version : "") + ".");
    store("rv-last-update", key);
    note("#update-msg", lu.message || ("Updated" + (lu.version ? " to version " + lu.version : "") + "."));
  } else {
    const b = $("#update-fail");
    b.hidden = store("rv-update-fail") === key;
    const m = lu.message || "The last update didn't work.";
    $("#update-fail-text").textContent = m + (/still works/i.test(m) ? "" : " Your current version still works.") + " Try Update now again later.";
    say("#update-msg", {error: (lu.message || "The last update didn't work.") + (lu.detail ? " Details: " + lu.detail : "")});
  }
}
function paintUpdate() {
  const u = S.update || {};
  const avail = !!u.available && !u.offline;
  const dismissed = store("rv-update-later") === String(u.latest);
  $("#update-banner").hidden = !avail || dismissed || S.updating;
  if (avail) $("#update-banner-text").textContent = `A new version of ReelVault is ready (${u.latest}). It takes about a minute.`;
  $("#navupdate").hidden = !avail;
  $("#update-now").hidden = !avail;
  const p = $("#s-update");
  if (avail) { p.className = "pill no"; p.textContent = "Update available"; }
  else if (checkFailed(u) && !u.offline) { p.className = "pill"; p.textContent = "Couldn't check"; }
  else if (S.update) { p.className = "pill ok"; p.textContent = "Up to date"; }
  $("#update-text").textContent = avail ? `Version ${u.latest} is ready. Click Update now. ReelVault closes, updates itself and opens again.`
    : checkFailed(u) && !u.offline ? "Last check: " + u.error : "ReelVault checks for a new version once an hour.";
}
async function updateNow(btn) {
  if (S.status && S.status.job) { say("#update-msg", {error: "ReelVault is busy: " + S.status.job + ". Wait for it to finish, then click Update now."}); toast("Wait for the current task to finish first."); return; }
  btn.disabled = true;
  const r = await post("/api/update");
  if (r.error) { btn.disabled = false; say("#update-msg", r); toast(r.error); return; }
  S.updating = {from: S.status && S.status.version, started: Date.now(), wentDown: false};
  $("#update-banner").hidden = false;
  $("#update-banner").className = "banner";
  $("#update-banner").innerHTML = '<span class="progress" style="color:inherit"><span class="spinner"></span> Updating ReelVault. It closes and opens again by itself. This page reloads when it\'s ready.</span>';
  note("#update-msg", "Updating... this page reloads when it's done.");
}
function watchUpdate(s) {
  const u = S.updating;
  if (!s) { u.wentDown = true; }
  else if (u.wentDown || (s.version && s.version !== u.from)) { location.reload(); return; }
  if (Date.now() - u.started > 4 * 60 * 1000) {
    $("#update-banner").className = "banner warn";
    $("#update-banner").innerHTML = "<span>The update is taking a long time. Close this page and double-click the ReelVault icon.</span>";
  }
}

// ---------------------------------------------------------------- events
document.addEventListener("click", async e => {
  const t = e.target.closest("button, a");
  if (!t) return;
  if (t.dataset.copy !== undefined) {
    const sel = t.closest(".blk, .copyrow, .moresec, .hookcard");
    copyText(t.dataset.copy, t, sel && (sel.querySelector("pre, .formula, p, span:not(.label)") || sel));
    return;
  }
  if (t.dataset.seek !== undefined) {
    const v = $("#vid"); if (!v) return;
    v.currentTime = Number(t.dataset.seek) || 0; v.play().catch(() => {});
    if (v.getBoundingClientRect().bottom < 0 || v.getBoundingClientRect().top > innerHeight) v.scrollIntoView({behavior: "smooth", block: "center"});
    return;
  }
  if (t.dataset.sortchip) {
    const item = t.closest("[data-sort]"), id = item.dataset.sort, p = t.dataset.sortchip;
    const sel = new Set(S.sortSel[id] || []); sel.has(p) ? sel.delete(p) : sel.add(p);
    S.sortSel[id] = PIDS.filter(x => sel.has(x));
    t.setAttribute("aria-pressed", String(sel.has(p)));
    $("[data-sortok]", item).disabled = !sel.size;
    return;
  }
  if (t.dataset.sortok !== undefined) {
    const item = t.closest("[data-sort]"), id = item.dataset.sort, ps = S.sortSel[id] || [];
    if (!ps.length) return;
    t.disabled = true;
    const r = await post(`/api/reels/${encodeURIComponent(id)}/purposes`, {purposes: ps});
    if (r.error) { t.disabled = false; toast(r.error); return; }
    const reel = reelById(id); if (reel) { reel.purposes = ps; reel.purpose_confirmed = 1; }
    if (S.reel && S.reelId === id) { S.reel.purposes = ps; S.reel.purpose_confirmed = 1; }
    toast("Saved: " + ps.map(p => PLABEL[p]).join(", "));
    renderSort();
    purposesChanged();
    return;
  }
  if (t.dataset.reelchip) {
    const ps = new Set(reelPurposes(S.reel)), p = t.dataset.reelchip;
    ps.has(p) ? ps.delete(p) : ps.add(p);
    setReelPurposes(PIDS.filter(x => ps.has(x)));
    return;
  }
  if (t.dataset.reelconfirm !== undefined) { setReelPurposes(reelPurposes(S.reel)); return; }
  if (t.dataset.unclamp !== undefined) { $("#sum").classList.remove("clamp"); t.remove(); return; }
  if (t.dataset.remakeGo !== undefined) { startRemake(t); return; }
  if (t.dataset.useHook) { S.studio.wantHook = t.dataset.useHook; S.studio.all = S.lib.all; location.hash = "#studio"; return; }
  if (t.dataset.useFormat) { S.studio.wantFormat = t.dataset.useFormat; S.studio.all = S.lib.all; location.hash = "#studio"; return; }
});

document.addEventListener("change", async e => {
  const t = e.target;
  if (t.dataset.todo) {
    const ok = await tickTodo(t.dataset.todo, Number(t.dataset.index), t.checked, t);
    if (ok) { t.closest(".todo").classList.toggle("done", t.checked); renderTodosCount(); }
  } else if (t.dataset.reeltodo !== undefined) {
    const ok = await tickTodo(S.reelId, Number(t.dataset.reeltodo), t.checked, t);
    if (ok) t.closest("li").classList.toggle("done", t.checked);
  } else if (t.name === "rm-for") {
    const c = $("#rm-client"); c.hidden = t.value !== "client"; if (!c.hidden) c.focus();
  } else if (t.id === "remake-pick") {
    S.remakePick = Number(t.value); renderReelRight();
  }
});
function renderTodosCount() {
  const left = (S.todos || []).filter(t => !t.done && !S.todoDone[t.reel_id + ":" + t.index]).length;
  $("#todo-count").textContent = left ? left + " open" : "All done";
  $("#todo-count").hidden = false;
}

// Home
$("#run").onclick = async () => {
  const r = await post("/api/run", {limit: Number($("#limit").value) || 20});
  say("#run-msg", r, "Started. ReelVault downloads and analyzes your new reels. Watch Activity below.");
  if (!r.error) { waitFor(r, "run"); $("#activity").open = true; refresh(); }
};
$("#retry").onclick = async () => {
  const r = await post("/api/retry");
  say("#retry-msg", r, "Trying the failed reels again.");
  if (!r.error) { waitFor(r, "retry"); $("#activity").open = true; refresh(); }
};
$("#links-go").onclick = async () => {
  const r = await post("/api/links", {text: $("#links").value});
  say("#links-msg", r, `Started on ${r.count || 0} link${r.count === 1 ? "" : "s"}. Watch Activity below.`);
  if (!r.error) { waitFor(r, "links"); $("#activity").open = true; refresh(); }
};
$("#guess-old").onclick = async () => {
  const r = await post("/api/sort/guess");
  if (!r.error && r.count === 0) { say("#guess-msg", r, "Every reel already has a guess."); return; }
  say("#guess-msg", r, `Claude is guessing for ${r.count || "your"} older reel${r.count === 1 ? "" : "s"}. This takes a minute. They show up above when it's done.`);
  if (!r.error) { waitFor(r, "guess"); refresh(); }
};
$("#sort-more").onclick = () => { S.sortShow += 10; renderSort(); };
$("#todo-more").onclick = () => { S.todoAll = true; renderTodos(); };

// Library filters
$("#lib-q").oninput = e => { S.lib.q = e.target.value; renderLibrary(parseRoute().arg); };
$("#lib-cat").onchange = e => { S.lib.cat = e.target.value; renderLibrary(parseRoute().arg); };
$("#lib-owner").onchange = e => { S.lib.owner = e.target.value; renderLibrary(parseRoute().arg); };
$("#lib-all").onchange = e => { S.lib.all = e.target.checked; renderLibrary(parseRoute().arg); };

// Studio
$("#st-hook").onchange = studioPreview;
$("#st-format").onchange = studioPreview;
$("#st-all").onchange = e => { S.studio.all = e.target.checked; renderStudio(); };
$("#remix").onclick = async () => {
  const topic = $("#topic").value.trim();
  if (!topic) { say("#remix-msg", {error: "Type what the new video is about."}); $("#topic").focus(); return; }
  const {hook, format} = studioPick();
  const extra = [];
  if (hook) extra.push(`Start with a hook like this one: "${hook.spoken || hook.on_screen_text}"`);
  if (format) extra.push(`Use this format: ${format.structure_name}${arr(format.beats).length ? " (" + format.beats.join(" > ") + ")" : ""}`);
  const full = extra.length ? topic + ". " + extra.join(". ") : topic;
  const r = await post("/api/remix", {topic: full, category: $("#cat").value, count: Number($("#count").value) || 3});
  say("#remix-msg", r, "Writing your scripts. This takes a minute or two. They show up below.");
  if (!r.error) { waitFor(r, "remix"); S.studio.waiting = true; refresh(); }
};
$("#st-copy").onclick = e => copyText($("#st-text").textContent, e.currentTarget, $("#st-text"));

// Settings: Claude
$("#claude-signin").onclick = async () => say("#claude-msg", await post("/api/claude/signin"), "A sign-in page opens in your browser. Finish there, then come back.");

// Settings: Instagram
$("#ig-connect").onclick = async () => {
  const r = await post("/api/ig/connect");
  if (r.needExtension) {
    S.showExt = true;
    note("#ig-connect-msg", "First, add the ReelVault Connector to Chrome. You only do this once:");
  } else {
    S.igMsgSet = true; say("#ig-connect-msg", r, r.message || "");
    setTimeout(() => { S.igMsgSet = false; }, 8000);
  }
  refresh();
};
$("#ext-open").onclick = async () => { const r = await post("/api/ext/open"); if (r.error) say("#ig-connect-msg", r); };
$("#ext-folder").onclick = async () => { const r = await post("/api/ext/folder"); if (r.error) say("#ig-connect-msg", r); };
$("#ext-copy").onclick = e => copyText($("#ext-path").textContent, e.currentTarget, $("#ext-path"));
$("#ig-chrome").onclick = async () => {
  const r = await post("/api/ig/chrome");
  if (r.error) say("#ig-chrome-msg", r); else note("#ig-chrome-msg", r.message || "A Chrome window opened. Log in to Instagram there.");
  refresh();
};
$("#ig-again").onclick = () => { S.igReconnect = true; if (S.status) renderSettings(S.status); };
$("#ig-go").onclick = async () => {
  if (!$("#ig-u").value.trim() || !$("#ig-p").value) { say("#ig-msg", {error: "Type your Instagram username and password."}); return; }
  note("#ig-msg", "Connecting...");
  const r = await post("/api/ig/login", {username: $("#ig-u").value, password: $("#ig-p").value});
  $("#ig-p").value = "";
  if (r.need2fa) {
    $("#ig-2fa").hidden = false; $("#ig-msg").textContent = "";
    const ways = [];
    if (r.sms) ways.push("a text message" + (r.phone ? " to " + r.phone : ""));
    if (r.whatsapp) ways.push("WhatsApp");
    if (r.app) ways.push("your authenticator app");
    $("#ig-2fa-how").textContent = ways.length ? "Enter the login code from " + ways.join(" or ") + "." : "Enter the login code Instagram sent you.";
    $("#ig-resend").hidden = !!(r.app && !r.sms);
    $("#ig-code").focus(); return;
  }
  say("#ig-msg", r, "Connected."); refresh();
};
$("#ig-code-go").onclick = async () => {
  const r = await post("/api/ig/2fa", {code: $("#ig-code").value});
  say("#ig-msg", r, "Connected.");
  if (!r.error) $("#ig-2fa").hidden = true;
  refresh();
};
$("#ig-resend").onclick = async () => {
  const b = $("#ig-resend"); b.disabled = true; note("#ig-resend-msg", "Asking Instagram...");
  const r = await post("/api/ig/resend");
  say("#ig-resend-msg", r, "New code sent" + (r.phone ? " to " + r.phone : "") + ". Use only the newest code.");
  setTimeout(() => { b.disabled = false; }, 60000);  // Instagram blocks rapid repeats
};
$("#ig-browser-go").onclick = async () => {
  note("#ig-browser-msg", "Reading your browser's Instagram login...");
  const r = await post("/api/ig/browser", {browser: $("#ig-browser").value});
  say("#ig-browser-msg", r, "Connected as @" + (r.username || "")); refresh();
};

// Settings: profile, schedule, updates, quit
$("#profile-save").onclick = async () => say("#profile-msg", await post("/api/profile", {text: $("#profile").value}), "Saved.");
async function schedule(time) {
  const m = $("#sched-msg"); m.dataset.hold = "1";
  const r = await post("/api/schedule", {time});
  say(m, r, time ? "On. ReelVault runs every day at " + time + "." : "Off.");
  setTimeout(() => { delete m.dataset.hold; refresh(); }, r.error ? 8000 : 2500);
}
$("#sched-on").onclick = () => schedule($("#sched-time").value);
$("#sched-off").onclick = () => schedule(null);
$("#update-check").onclick = () => checkUpdate(true);
$("#update-now").onclick = e => updateNow(e.currentTarget);
$("#banner-update").onclick = e => updateNow(e.currentTarget);
$("#update-fail-close").onclick = () => { store("rv-update-fail", S.lastUpdateShown || "x"); $("#update-fail").hidden = true; };
$("#banner-later").onclick = () => { store("rv-update-later", String((S.update || {}).latest)); paintUpdate(); };
$("#quit").onclick = async () => {
  S.quitting = true;
  await post("/api/quit");
  document.body.innerHTML = "<div style='padding:32px;font:16px/1.5 system-ui'><h1 style='font-size:22px'>ReelVault is closed.</h1><p>Double-click the ReelVault icon to open it again.</p></div>";
};

// ---------------------------------------------------------------- start
(async function start() {
  route();
  await refresh();
  await Promise.all([loadReels(), loadTodos()]);
  route();
  checkUpdate(false);
  setInterval(refresh, 2000);
  setInterval(() => checkUpdate(false), 60 * 60 * 1000);
})();

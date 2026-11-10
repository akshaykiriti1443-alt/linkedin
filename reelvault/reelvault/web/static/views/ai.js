/* Connect your AI (4.5, 7.3.4): Claude plan through our connector, an API key, or the desktop app.
   Used by onboarding step 3 and #settings/ai. The connector steps come from web/copy/connector.json (the admin can
   override them in Admin > Copy), served filled-in by GET /api/copy. */
import {h, clear, md, msg, say, busy, get, post, put, patch, del, api, arr, field, select, toast, copyBtn, copyLine,
  loadCopy, refreshMe, pill, ago, num, every} from "../app.js";

const MODES = [
  ["claude_connector", "Use my Claude plan", "Your own Claude (Pro, Max, Team) does the work through our connector. No key needed."],
  ["api", "Paste an API key", "Gemini (free in most countries), Anthropic, OpenAI or OpenRouter. Runs on our server."],
  ["desktop", "Use the ReelVault desktop app", "Claude Code on your Windows computer does the work while the app is open."],
];

/** Renders the whole AI setup into `box`. opts.onboarding: shorter copy and a "Next" hook. */
export async function aiSetup(box, {onboarding = false, onDone = null} = {}) {
  const [ai, cat, copy, cl, eCon, eKey, eDesk] = await Promise.all([get("/api/ai"), get("/api/ai/catalog"), loadCopy(),
    get("/api/connector/clients"), get("/api/ai/estimate?mode=claude_connector"), get("/api/ai/estimate?mode=api&provider=gemini"),
    get("/api/ai/estimate?mode=desktop")]);
  if (!ai.ok) { clear(box, h("p", {class: "msg err"}, ai.error)); return; }
  const st = ai.data;
  const est = {claude_connector: eCon.ok ? eCon.data : null, api: eKey.ok ? eKey.data : null, desktop: eDesk.ok ? eDesk.data : null};
  let pick = box.dataset.pick || st.mode || "";
  const redraw = () => aiSetup(box, {onboarding, onDone});
  const detail = h("div", {class: "stack"});
  const choices = h("div", {class: "grid3"}, MODES.map(([id, title, sub]) => h("button", {
    type: "button", class: "choice", "aria-pressed": String(pick === id), dataset: {mode: id},
    onclick: () => { box.dataset.pick = id; redraw(); },
  }, h("b", title), h("span", {class: "small"}, sub), st.mode === id ? pill("In use", "ok") : null,
    est[id] ? h("span", {class: "hint"}, arr(est[id].lines).join(" ")) : null)));
  const q = st.queue || {};
  const queue = [];
  const n1 = (n, one, many) => (n === 1 ? one : many.replace("{n}", num(n)));
  if (q.waiting_for_claude) queue.push(n1(q.waiting_for_claude, "1 reel is waiting for Claude.", "{n} reels are waiting for Claude.") + " Open Claude and say: Analyze my new ReelVault reels.");
  if (q.waiting_for_desktop) queue.push(n1(q.waiting_for_desktop, "1 job is waiting for the desktop app.", "{n} jobs are waiting for the desktop app.") + " Open it to continue.");
  if (q.reels_left_to_tag) queue.push(n1(q.reels_left_to_tag, "1 reel left to tag.", "{n} reels left to tag."));
  clear(box, st.mode ? null : h("div", {class: "banner warn"}, "Choose how ReelVault uses AI. Nothing is tagged or written until you do."),
    queue.length ? h("div", {class: "banner"}, queue.join(" ")) : null,
    h("div", {class: "stack"}, h("h2", onboarding ? "How should ReelVault use AI?" : "How ReelVault uses AI"), choices), detail);
  if (pick === "claude_connector") detail.append(connectorPanel(st, copy, cl.ok ? cl.data : null, redraw, onDone, est.claude_connector));
  else if (pick === "api") detail.append(keyPanel(st, cat.ok ? cat.data : {providers: [], presets: []}, copy, redraw, onDone, "all"));
  else if (pick === "desktop") detail.append(desktopPanel(st, copy, redraw, onDone));
  if (pick && pick !== "api") detail.append(taggingPanel(st, cat.ok ? cat.data : {providers: [], presets: []}, copy, redraw));
  if (pick !== "api" && arr(st.connections).length) detail.append(connectionsPanel(st, redraw));
}

async function useMode(btn, mode, redraw, onDone) {
  const r = await busy(btn, () => put("/api/ai/mode", {mode}));
  if (!r.ok) { toast(r.error); return false; }
  await refreshMe();
  toast("Saved. ReelVault now uses " + (mode === "api" ? "your API key" : mode === "desktop" ? "the desktop app" : "your Claude plan") + ".");
  redraw(); if (onDone) onDone(mode);
  return true;
}

// ---------------------------------------------------------------------------------- Claude connector
function connectorPanel(st, copy, clients, redraw, onDone, estimate) {
  const url = (st.connector || {}).url || (clients || {}).url || "";
  const connected = (st.connector || {}).connected;
  const step = text => {
    // The URL step gets a real copy button instead of the "(copy button)" words.
    if (url && text.includes(url)) {
      const [a, b] = text.replace(/\s*\(copy button\)/i, "").split(url);
      const rest = (b || "").replace(/^[\s.]+/, "");
      return h("li", md(a), h("div", {class: "copyline"}, h("code", url), copyBtn(url, "Copy the URL")), rest ? md(rest) : null);
    }
    return h("li", md(text));
  };
  const list = arr((clients || {}).clients);
  return h("section", {class: "panel" + (connected ? " hl" : ""), id: "ai-connector"},
    h("div", {class: "panel-head"}, h("h2", copy.connector_title || "Use my Claude plan"),
      connected ? (st.mode === "claude_connector" ? pill("Connected", "ok") : pill("Connected, not in use", "warn"))
        : pill("Not connected yet", "warn")),
    h("p", copy.connector_intro || ""),
    h("ol", {class: "steps-ol"}, arr(copy.connector_steps).map(step)),
    h("ul", {class: "stack tight"}, arr(copy.connector_notes).map(n => {
      const cmd = copy.claude_code_command;
      if (cmd && n.includes(cmd)) return h("li", md(n.replace(cmd, "").trim()), h("div", {class: "copyline"}, h("code", cmd), copyBtn(cmd)));
      return h("li", {class: "hint"}, md(n));
    })),
    estimate && arr(estimate.lines).length ? h("p", {class: "hint"}, arr(estimate.lines).join(" ")) : copy.connector_estimate_line ? h("p", {class: "hint"}, copy.connector_estimate_line) : null,
    h("div", {class: "stack tight"}, h("span", {class: "label"}, "Say this to Claude"), arr(copy.connector_prompts).map(p => copyLine(p))),
    list.length ? h("div", {class: "stack"}, h("span", {class: "label"}, "Connected Claude apps"), list.map(c => h("div", {class: "row"},
      h("b", c.name || c.client_id), h("span", {class: "hint"}, "Last used " + ago(c.last_used_at)),
      h("button", {type: "button", class: "ghost sm", onclick: async e => {
        const r = await busy(e.currentTarget, () => post("/api/connector/disconnect", {client_id: c.client_id}));
        toast(r.ok ? "Disconnected." : r.error); redraw();
      }}, "Disconnect")))) : null,
    st.mode === "claude_connector" ? h("p", {class: "msg good"}, "ReelVault uses your Claude plan.") :
      h("div", {class: "row"}, h("button", {type: "button", id: "use-connector", onclick: e => useMode(e.currentTarget, "claude_connector", redraw, onDone)}, "Use my Claude plan")));
}

// ---------------------------------------------------------------------------------- API key
function keyPanel(st, cat, copy, redraw, onDone, useFor) {
  const providers = arr(cat.providers);
  const presets = arr(cat.presets);
  const out = msg();
  const prov = select(providers.map(p => [p.id, p.label]), useFor === "tagging" ? "gemini" : (providers[0] || {}).id, {id: `key-provider-${useFor}`});
  const preset = select(presets.map(p => [p.id, p.label]), (presets[0] || {}).id, {id: `key-preset-${useFor}`});
  const base = h("input", {id: `key-base-${useFor}`, placeholder: "https://example.com/v1"});
  const model = h("select", {id: `key-model-${useFor}`});
  const modelText = h("input", {id: `key-model-text-${useFor}`, placeholder: "Model name"});
  const key = h("input", {type: "password", autocomplete: "off", id: `key-value-${useFor}`, placeholder: "Paste your key"});
  const info = h("div", {class: "stack tight"});
  const presetRow = field("Service", preset);
  const baseRow = field("Address", base, "Only for a custom service.");
  const modelRow = field("Model", model);
  const modelTextRow = field("Model", modelText);
  const sync = () => {
    const p = providers.find(x => x.id === prov.value) || {};
    const compat = p.id === "openai_compat";
    presetRow.hidden = !compat;
    const ps = presets.find(x => x.id === preset.value) || {};
    baseRow.hidden = !(compat && (ps.id === "custom" || !ps.base_url));
    if (compat && ps.base_url) base.value = ps.base_url;
    modelRow.hidden = compat || !arr(p.models).length;
    modelTextRow.hidden = !modelRow.hidden;
    if (compat) modelText.value = modelText.value || ps.default_model || "";
    clear(model, arr(p.models).map(m => h("option", {value: m.id, selected: m.id === (useFor === "tagging" && p.free_model ? p.free_model : p.default_model)},
      m.label + (m.vision === false ? " (can't see images)" : "") + (m.cost_per_reel ? ` · ${m.cost_per_reel}` : ""))));
    const keyUrl = compat ? ps.key_url : p.key_url;
    const local = compat && ps.local;
    key.placeholder = local ? "No key needed for this one" : "Paste your key";
    clear(info, p.free ? h("p", h("b", p.free)) : null,
      keyUrl ? h("p", h("a", {href: keyUrl, target: "_blank", rel: "noopener noreferrer"}, "Get a key from " + (compat ? ps.label : p.label))) : null,
      arr(compat ? ps.notes : p.notes).map(n => h("p", {class: "hint"}, n)),
      p.estimate && !compat ? h("p", {class: "hint"}, arr(p.estimate.lines).join(" ")) : null);
  };
  prov.addEventListener("change", () => { modelText.value = ""; sync(); });
  preset.addEventListener("change", () => { modelText.value = ""; sync(); });
  sync();
  const save = async (btn, anyway) => {
    const p = prov.value;
    const body = {provider: p, model: p === "openai_compat" ? modelText.value.trim() : model.value, api_key: key.value.trim() || null, use_for: useFor};
    if (p === "openai_compat") { body.preset = preset.value; body.base_url = base.value.trim() || null; }
    if (p === "gemini" && (model.value || "").includes("lite")) body.options = {free_tier: true};
    const r = await busy(btn, () => api("POST", "/api/ai/connections" + (anyway ? "?save_anyway=1" : ""), body));
    if (!r.ok) {
      say(out, r);
      if (r.data && r.data.detail && r.data.detail.test) {
        out.append(" ", h("button", {type: "button", class: "link", onclick: e => save(e.currentTarget, true)}, "Save it anyway"));
      }
      return;
    }
    key.value = "";
    say(out, {ok: true}, (r.data.test || {}).message || "Saved.");
    if (r.data.warning) toast(r.data.warning);
    await refreshMe();
    setTimeout(() => { redraw(); if (onDone && useFor === "all") onDone("api"); }, r.data.warning ? 2500 : 800);
  };
  const steps = arr(copy.key_steps);
  return h("section", {class: "panel", id: useFor === "tagging" ? "ai-tagging-form" : "ai-key"},
    h("h2", useFor === "tagging" ? "Add a key just for tagging" : "Paste an API key"),
    useFor === "tagging" ? h("p", {class: "hint"}, "Tagging many reels is faster with a free Gemini key. Your main AI still writes the scripts.") :
      steps.length ? h("ol", {class: "steps-ol"}, steps.map(s => h("li", md(s)))) : null,
    h("div", {class: "grid2"}, field("Provider", prov), presetRow), baseRow, h("div", {class: "grid2"}, modelRow, modelTextRow, field("API key", key)),
    info, h("p", {class: "hint"}, "Your key is locked with encryption on our server and never shown again. We only show the last 4 characters."),
    out, h("div", {class: "row"}, h("button", {type: "button", id: `key-save-${useFor}`, onclick: e => save(e.currentTarget, false)}, "Test and save")),
    useFor === "all" && st.mode !== "api" && arr(st.connections).some(c => c.use_for !== "tagging") ? h("div", {class: "row"},
      h("button", {type: "button", class: "ghost", onclick: e => useMode(e.currentTarget, "api", redraw, onDone)}, "Use my saved key")) : null,
    useFor === "all" && arr(st.connections).length ? connectionsPanel(st, redraw, true) : null);
}

function connectionsPanel(st, redraw, inner = false) {
  const rows = arr(st.connections).map(c => {
    const out = msg();
    const modelIn = h("input", {value: c.model, "aria-label": "Model"});
    return h("div", {class: "card", dataset: {conn: c.id}},
      h("div", {class: "row"}, h("b", (c.preset ? c.preset : c.provider) + " · " + c.model),
        pill(c.status === "ok" ? "Works" : c.status === "bad_key" ? "Key doesn't work" : c.status === "quota" ? "Out of credit" : c.status, c.status === "ok" ? "ok" : "warn"),
        c.is_default ? pill("Main", "busy") : null, c.use_for === "tagging" || st.tagging_conn_id === c.id ? pill("Tagging") : null,
        c.vision === false ? pill("Can't see images", "warn") : null),
      h("p", {class: "hint"}, `Key ${c.key || "none"} · ${num((c.usage_this_month || {}).calls || 0)} calls this month · tested ${ago(c.last_tested_at)}`),
      c.last_error ? h("p", {class: "small"}, c.last_error) : null,
      h("details", {class: "fold"}, h("summary", "Change"),
        h("div", {class: "stack"}, field("Model", modelIn),
          h("div", {class: "row"},
            h("button", {type: "button", class: "ghost sm", onclick: async e => { const r = await busy(e.currentTarget, () => patch(`/api/ai/connections/${c.id}`, {model: modelIn.value.trim()})); say(out, r, "Saved."); if (r.ok) redraw(); }}, "Save model"),
            c.is_default ? null : h("button", {type: "button", class: "ghost sm", onclick: async e => { const r = await busy(e.currentTarget, () => patch(`/api/ai/connections/${c.id}`, {is_default: true})); say(out, r, "Saved."); if (r.ok) redraw(); }}, "Make it the main one")))),
      out,
      h("div", {class: "row"},
        h("button", {type: "button", class: "ghost sm", onclick: async e => { const r = await busy(e.currentTarget, () => post(`/api/ai/connections/${c.id}/test`, {})); say(out, r.ok ? {ok: r.data.ok, error: r.data.ok ? null : r.data.message} : r, r.ok ? r.data.message : ""); }}, "Test it"),
        h("button", {type: "button", class: "danger sm", onclick: async e => { if (!confirm("Delete this key from ReelVault?")) return; const r = await busy(e.currentTarget, () => del(`/api/ai/connections/${c.id}`)); toast(r.ok ? "Deleted." : r.error); redraw(); }}, "Delete")));
  });
  return h(inner ? "div" : "section", {class: inner ? "stack" : "panel"}, h(inner ? "h3" : "h2", "Your saved keys"), rows.length ? rows : h("p", {class: "muted"}, "No keys saved."));
}

function taggingPanel(st, cat, copy, redraw) {
  const t = arr(st.connections).find(c => c.id === st.tagging_conn_id);
  if (t) {
    return h("section", {class: "panel"}, h("h2", "Tagging key"),
      h("p", `Reels are tagged with ${t.provider} · ${t.model}. Your main AI writes the scripts.`),
      h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: async e => {
        const r = await busy(e.currentTarget, () => put("/api/ai/mode", {mode: st.mode, tagging_conn_id: null}));
        toast(r.ok ? "Stopped. Your main AI tags reels again." : r.error); redraw();
      }}, "Stop using it for tagging")));
  }
  const wrap = h("details", {class: "fold panel"}, h("summary", "Want it faster? Add a free Gemini key just for tagging"));
  wrap.append(keyPanel(st, cat, copy, redraw, null, "tagging"));
  return wrap;
}

// ---------------------------------------------------------------------------------- desktop app
function desktopPanel(st, copy, redraw, onDone) {
  const codeBox = h("div", {class: "stack"});
  const devices = arr((st.desktop || {}).devices);
  return h("section", {class: "panel", id: "ai-desktop"},
    h("h2", "Use the ReelVault desktop app"),
    h("ol", {class: "steps-ol"}, arr(copy.desktop_steps).map(s => h("li", md(s)))),
    h("p", h("a", {class: "btn ghost", href: "#help/desktop-app", id: "get-desktop-app"}, "Get the desktop app")),
    h("div", {class: "row"}, h("button", {type: "button", class: "ghost", onclick: async e => {
      const r = await busy(e.currentTarget, () => post("/api/ai/desktop/pair", {}));
      if (!r.ok) { toast(r.error); return; }
      clear(codeBox, h("p", "Type this code in the desktop app. It works for 10 minutes."), copyLine(r.data.code, "Copy the code"),
        h("p", {class: "hint"}, "Server address: ", h("code", r.data.server || location.origin)));
      every(4000, async stop => { const a = await get("/api/ai", {quiet: true}); if (a.ok && arr((a.data.desktop || {}).devices).length > devices.length) { stop(); toast("The desktop app is linked."); redraw(); } });
    }}, "Get a code")), codeBox,
    devices.length ? h("div", {class: "stack"}, h("span", {class: "label"}, "Linked computers"), devices.map(d => h("div", {class: "row"},
      h("b", d.name || "Desktop app"), h("span", {class: "hint"}, "Last seen " + ago(d.last_seen_at)), d.claude && d.claude.found === false ? pill("Claude Code not found", "warn") : null,
      h("button", {type: "button", class: "ghost sm", onclick: async e => { const r = await busy(e.currentTarget, () => del(`/api/ai/desktop/${d.id}`)); toast(r.ok ? "Removed." : r.error); redraw(); }}, "Remove")))) : null,
    st.mode === "desktop" ? h("p", {class: "msg good"}, "ReelVault uses the desktop app.") :
      h("div", {class: "row"}, h("button", {type: "button", id: "use-desktop", onclick: e => useMode(e.currentTarget, "desktop", redraw, onDone)}, "Use the desktop app")));
}

/* Connect Chrome (4.6, 5.2): install, pair link, "read Instagram as @username", status, budget, update notice.
   Used by onboarding step 2 and #settings/extension. */
import {h, clear, md, busy, get, post, del, toast, copyLine, pill, ago, num, every, refreshMe} from "../app.js";

export const KEEP_OPEN = "Keep Chrome open. ReelVault reads in a pinned Instagram tab; you can keep using Chrome.";

export async function extensionSetup(box, {onboarding = false, onReady = null} = {}) {
  const r = await get("/api/ext/status");
  if (!r.ok) { clear(box, h("p", {class: "msg err"}, r.error)); return null; }
  const st = r.data;
  const redraw = () => extensionSetup(box, {onboarding, onReady});
  const dv = st.device;
  if (!st.paired || !dv) {
    const pairBox = h("div", {class: "stack", id: "pair-box"});
    clear(box, h("section", {class: "panel", id: "ext-install"},
      h("h2", "1. Add the extension to Chrome"),
      h("p", "It works in Chrome, Edge or Brave on a computer. It reads Instagram with your own login. Your Instagram password and cookies never leave your browser."),
      h("div", {class: "row"},
        st.store_url ? h("a", {class: "btn", href: st.store_url, target: "_blank", rel: "noopener noreferrer"}, "Add to Chrome from the Web Store") : null,
        h("a", {class: st.store_url ? "btn ghost" : "btn", href: st.download_url || "/download/reelvault-members-extension.zip"}, "Download the extension (ZIP)"),
        h("a", {href: "#help/extension-zip"}, "How to install the ZIP"))),
    h("section", {class: "panel"},
      h("h2", "2. Connect it to your account"),
      h("p", "Click the button. Then open the ReelVault Members extension and paste the link."),
      h("div", {class: "row"}, h("button", {type: "button", id: "get-pair-link", onclick: async e => {
        const p = await busy(e.currentTarget, () => post("/api/ext/pair", {}));
        if (!p.ok) { toast(p.error); return; }
        clear(pairBox, copyLine(p.data.pair_link, "Copy the link"),
          h("p", {class: "hint"}, md(`Or type the code **${p.data.code}** in the extension. It works for 10 minutes.`)),
          h("p", {class: "row"}, h("span", {class: "spinner", "aria-hidden": "true"}), "Waiting for the extension…"));
        every(3000, async stop => { const s = await get("/api/ext/status", {quiet: true}); if (s.ok && s.data.paired) { stop(); toast("Chrome is connected."); await refreshMe(); redraw(); } });
      }}, "Get my pair link")), pairBox));
    return st;
  }
  // Paired: which Instagram account will it read?
  const parts = [];
  if (!dv.ig_confirmed) {
    if (dv.ig_username && dv.signed_in !== false) {
      parts.push(h("section", {class: "panel hl", id: "ext-confirm"},
        h("h2", "Check the Instagram account"),
        h("p", md(`ReelVault will read Instagram as **@${dv.ig_username}**.`)),
        h("p", {class: "hint"}, "Pulls wait until you confirm. Your own account is fine, or use a second account you made for research."),
        h("div", {class: "row"},
          h("button", {type: "button", id: "confirm-account", onclick: async e => {
            const c = await busy(e.currentTarget, () => post("/api/ext/confirm-account", {ig_user_id: dv.ig_user_id}));
            if (!c.ok) { toast(c.error); return; }
            toast("Thanks. ReelVault starts reading now."); await refreshMe(); redraw(); if (onReady) onReady();
          }}, "Continue"),
          h("a", {class: "btn ghost", href: "#help/second-account"}, "Use another account"))));
    } else {
      parts.push(h("section", {class: "panel hl"},
        h("h2", "Sign in to Instagram in Chrome"),
        h("p", "The extension is connected, but Chrome is not signed in to Instagram. Open instagram.com in Chrome and sign in. Then click Try again."),
        h("div", {class: "row"}, h("button", {type: "button", onclick: async e => {
          const t = await busy(e.currentTarget, () => post("/api/ext/try-again", {}));
          toast(t.ok ? "Asked the extension to check again." : t.error); setTimeout(redraw, 1500);
        }}, "Try again"))));
      every(5000, async stop => { const s = await get("/api/ext/status", {quiet: true}); if (s.ok && s.data.device && s.data.device.ig_username) { stop(); redraw(); } });
    }
  }
  const b = st.budget || {};
  const status = h("section", {class: "panel", id: "ext-status"},
    h("div", {class: "panel-head"}, h("h2", "Chrome extension"), dv.online ? pill("On", "ok") : pill("Not seen lately", "warn")),
    h("p", {class: dv.online ? "" : "bold"}, dv.last_seen_text || ("Last seen " + ago(dv.last_seen_at))),
    dv.cooldown_text || dv.cooldown_until ? h("div", {class: "banner warn"}, h("span", dv.cooldown_text || "Instagram asked us to slow down. Reading resumes later."),
      dv.cooldown_reason === "signed_out" ? h("button", {type: "button", class: "sm", onclick: async e => {
        const t = await busy(e.currentTarget, () => post("/api/ext/try-again", {}));
        toast(t.ok ? "Asked the extension to check again." : t.error); setTimeout(redraw, 1500);
      }}, "Try again") : null) : null,
    dv.update_available || dv.update_needed ? h("div", {class: "banner" + (dv.update_needed ? " bad" : "")},
      h("span", dv.update_text || "A new version of the extension is ready. Update it to keep reading."),
      h("a", {class: "btn sm", href: st.store_url || st.download_url || "/download/reelvault-members-extension.zip"}, "Get the update")) : null,
    h("dl", {class: "kv"},
      h("dt", "Reads Instagram as"), h("dd", dv.ig_username ? "@" + dv.ig_username + (dv.ig_confirmed ? "" : " (not confirmed yet)") : "Not signed in"),
      h("dt", "Browser"), h("dd", `${dv.name || "Chrome"} · version ${dv.version || "?"}`),
      h("dt", "Used this hour"), h("dd", `${num(b.hour_used || 0)} of ${num(b.hour_max || 120)} Instagram reads`),
      h("dt", "Used today"), h("dd", `${num(b.day_used || 0)} of ${num(b.day_max || 400)} reads · ${num(b.cdn_day_used || 0)} of ${num(b.cdn_day_max || 150)} videos`)),
    h("p", {class: "hint"}, KEEP_OPEN, " These limits keep your Instagram account safe."),
    h("div", {class: "row"},
      h("a", {href: "#help/second-account"}, "Read with a second Instagram account"),
      h("span", {class: "spacer"}),
      h("button", {type: "button", class: "danger sm", onclick: async e => {
        if (!confirm("Disconnect this extension? Pulls stop until you connect again.")) return;
        const d = await busy(e.currentTarget, () => del("/api/ext/device"));
        toast(d.ok ? "Disconnected." : d.error); await refreshMe(); redraw();
      }}, "Disconnect")));
  clear(box, parts, status);
  return st;
}

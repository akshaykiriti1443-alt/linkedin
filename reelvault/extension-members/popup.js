import {SERVERS} from "./config.js";
import {parsePairLink} from "./validate.js";

const $ = id => document.getElementById(id);
const show = (id, on = true) => { $(id).hidden = !on; };
const send = msg => chrome.runtime.sendMessage(msg);
let pending = null;

function error(text) { $("error").textContent = text || ""; show("error", !!text); }
function only(id) { for (const s of ["pair", "confirm", "connected", "status"]) show(s, s === id); }
function n(x) { return Number(x || 0).toLocaleString("en-US"); }

function igText(ig) {
  if (!ig) return "Instagram: not checked yet.";
  if (!ig.signed_in) return "You're signed out of Instagram in Chrome. Sign in at instagram.com, then open this again.";
  return `ReelVault reads Instagram as @${ig.username}.`;
}

async function render() {
  const s = await send({type: "status"});
  error("");
  if (!s || !s.paired) {
    only("pair");
    if (s && s.revoked) error("This browser was disconnected from ReelVault. Connect again.");
    return;
  }
  only("status");
  $("s-host").textContent = s.host;
  $("s-ig").textContent = igText(s.ig);
  const b = s.budget, L = s.limits;
  $("s-budget").textContent = `Instagram reads today: ${n(b.day_used)} of ${n(L.day_max)} · this hour: ${n(b.hour_used)} of ${n(L.hour_max)} · videos: ${n(b.cdn_day_used)} of ${n(L.cdn_day_max)}`;
  $("s-job").textContent = s.job ? `Now: ${s.job.message}` : "Nothing to read right now.";
  show("s-cooldown", !!s.cooldown);
  if (s.cooldown) {
    const until = new Date(s.cooldown.until).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
    $("s-cooldown").textContent = s.cooldown.reason === "signed_out"
      ? "You're signed out of Instagram in Chrome. Sign in at instagram.com, then press Try again in ReelVault."
      : `Instagram asked us to slow down. ReelVault waits until ${until}.`;
  }
  show("s-action", s.reason === "confirm_account");
  if (s.reason === "confirm_account") {
    $("s-action").textContent = "Open ReelVault > Settings > Chrome extension and confirm the Instagram account to read with.";
  }
  const h = s.hello;
  const newer = h && h.latest_version && h.latest_version !== s.version;
  show("s-update", !!newer);
  if (newer) $("s-update").textContent = "Update available: download the new ZIP, extract it over the old folder, then press Reload on chrome://extensions.";
}

$("next").addEventListener("click", () => {
  error("");
  const pl = parsePairLink($("link").value, SERVERS);
  if (!pl.ok) {
    error(pl.error === "not_ours" ? "This link is not from your ReelVault. Don't paste links other people send you."
                                  : "This doesn't look like a ReelVault link. Copy it again from ReelVault > Settings > Chrome extension.");
    return;
  }
  pending = $("link").value;
  $("confirm-host").textContent = pl.host;
  only("confirm");
});

$("cancel").addEventListener("click", () => { pending = null; only("pair"); });

$("connect").addEventListener("click", async () => {
  $("connect").disabled = true;
  error("");
  const r = await send({type: "claim", link: pending});
  $("connect").disabled = false;
  if (!r || !r.ok) { only("pair"); error((r && r.error) || "That didn't work. Try again."); return; }
  only("connected");
  $("hello-line").textContent = `Connected to ReelVault as ${r.name}.`;
  $("ig-line").textContent = r.ig && r.ig.signed_in ? `ReelVault will read Instagram as @${r.ig.username}.` : igText(r.ig);
  const pl = parsePairLink(pending, SERVERS);
  $("second").href = `${pl.origin}/#help/second-account`;
});

$("disconnect").addEventListener("click", async () => { await send({type: "disconnect"}); render(); });
$("link").addEventListener("keydown", e => { if (e.key === "Enter") $("next").click(); });

render();

/* #privacy, #help, #help/extension-zip, #help/second-account, #help/desktop-app. Plain pages. */
import {h, clear, md, S, backLink, get, arr, loadCopy, copyLine} from "../app.js";

const back = () => S.me ? backLink("#home", "Home") : backLink("#login", "Log in");

export async function privacy(main) {
  clear(main, h("div", {class: "view"}, back(), h("h1", "Privacy"),
    h("section", {class: "panel"},
      h("h2", "What we keep"),
      h("ul", null,
        h("li", "Your account: name, email, and a scrambled copy of your password."),
        h("li", "What you tell us about your business, and the links you add."),
        h("li", "The public reels of the Instagram accounts you track: numbers, captions, covers, and for some reels the video, for 7 days."),
        h("li", "What your AI writes for you.")),
      h("h2", "What we never keep"),
      h("ul", null,
        h("li", "Your Instagram password or cookies. The extension reads Instagram inside your own Chrome. Your login never leaves it."),
        h("li", "Your Claude login. If you use your Claude plan, you add our connector inside your own Claude app."),
        h("li", "Saves. Instagram hides them for accounts you don't own.")),
      h("h2", "Your AI key"),
      h("p", "If you paste an API key, we lock it with encryption. We only show its last 4 characters. You can delete it any time."),
      h("h2", "Who can see your data"),
      h("p", "Only you. Other members can never see it. The admin can look at your account only if you turn on support access in Settings > Account, and you see when they did."),
      h("h2", "Leave any time"),
      h("p", "Download everything or delete your account in Settings > Account."))));
}

export async function help(main, {parts}) {
  const which = parts[0] || "";
  if (which === "extension-zip") return extensionZip(main);
  if (which === "second-account") return secondAccount(main);
  if (which === "desktop-app") return desktopApp(main);
  clear(main, h("div", {class: "view"}, back(), h("h1", "Help"),
    h("section", {class: "panel"},
      h("h2", "Quick answers"),
      h("dl", {class: "kv"},
        h("dt", "What is an outlier?"), h("dd", "A reel that did far better than that creator's normal reel. \"4.2x normal\" means it got 4.2 times the views of their normal reel."),
        h("dt", "Why no saves?"), h("dd", "Instagram hides saves, and usually shares, for accounts you don't own. We rank on views, likes and comments."),
        h("dt", "Why does it take hours?"), h("dd", "Numbers come in minutes. Watching and tagging each reel takes longer, and the server shares its time fairly between members."),
        h("dt", "Is my Instagram safe?"), h("dd", "The extension reads slowly, like a person scrolling, and stops when Instagram asks it to. We never post, like, follow or message."))),
    h("section", {class: "panel"}, h("h2", "Guides"),
      h("ul", null, h("li", h("a", {href: "#help/extension-zip"}, "Install the extension from a ZIP file")),
        h("li", h("a", {href: "#help/second-account"}, "Read Instagram with a second account")),
        h("li", h("a", {href: "#help/desktop-app"}, "Get the ReelVault desktop app (Windows)")), h("li", h("a", {href: "#privacy"}, "Privacy")))),
    h("p", {class: "muted"}, "Still stuck? Ask in the community. Turn on support access in Settings > Account so the admin can look."),
    h("p", {class: "hint", id: "app-version"})));
  const v = await get("/api/version", {quiet: true});
  const el = main.querySelector("#app-version");
  if (v.ok && el) el.textContent = `ReelVault Members ${v.data.version} · needs extension ${v.data.extension_min} or newer.`;
}

function extensionZip(main) {
  clear(main, h("div", {class: "view"}, backLink("#help", "Help"), h("h1", "Install the extension from a ZIP file"),
    h("section", {class: "panel"}, h("ol", {class: "steps-ol"},
      h("li", md("Download the ZIP from **Settings > Chrome extension**.")),
      h("li", md("Unzip it into a folder you will keep, for example **Documents/ReelVault extension**. Don't delete this folder later.")),
      h("li", md("In Chrome, open **chrome://extensions** (type it in the address bar).")),
      h("li", md("Turn on **Developer mode** (top right).")),
      h("li", md("Click **Load unpacked** and pick the folder you unzipped.")),
      h("li", md("Pin the ReelVault Members extension (the puzzle icon, then the pin).")),
      h("li", md("Back in ReelVault, click **Get my pair link** and paste the link into the extension."))),
    h("p", {class: "hint"}, "Edge: open edge://extensions. Brave: open brave://extensions. The steps are the same."),
    h("p", {class: "hint"}, "When a new version comes out, download the new ZIP, unzip it over the same folder, and click the reload arrow on chrome://extensions."))));
}

function secondAccount(main) {
  clear(main, h("div", {class: "view"}, backLink(S.me ? "#settings/extension" : "#help", S.me ? "Chrome extension" : "Help"), h("h1", "Read Instagram with a second account"),
    h("section", {class: "panel"},
      h("p", "Some members prefer a separate Instagram account just for research. That is fine. ReelVault reads with whatever account Chrome is signed in to."),
      h("ol", {class: "steps-ol"},
        h("li", "Make a new Instagram account in the Instagram app (Settings > Add account). Use it like a normal person for a few days: follow some accounts in your niche, watch some reels."),
        h("li", md("Best: use a separate **Chrome profile** for ReelVault (click your face in Chrome's top right, then **Add**). Sign in to the research account there, and install the extension in that profile.")),
        h("li", "Or, in your normal Chrome, sign out of Instagram and sign in with the research account."),
        h("li", md("Back in ReelVault, check the line **ReelVault will read Instagram as @...** and click **Continue**."))),
      h("p", {class: "hint"}, "Private accounts are never read, even if your account follows them."))));
}

// Fix round 1: the desktop-app route had no way to get the app. Steps and the one-paste command come from
// web/copy/connector.json (desktop_install_steps, desktop_install_command), editable in Admin > Copy.
async function desktopApp(main) {
  const copy = await loadCopy();
  const cmd = copy.desktop_install_command || "";
  clear(main, h("div", {class: "view"}, backLink(S.me ? "#settings/ai" : "#help", S.me ? "AI settings" : "Help"),
    h("h1", "Get the ReelVault desktop app"),
    h("section", {class: "panel"},
      h("p", "The desktop app runs on Windows. Claude Code on your computer then does ReelVault's AI work with your Claude plan."),
      h("ol", {class: "steps-ol"}, arr(copy.desktop_install_steps).map((t, i) => h("li", md(t), i === 1 && cmd ? copyLine(cmd, "Copy the line") : null))),
      h("p", {class: "hint"}, "Keep the desktop app open while it works. You can close it any time; the work waits."))));
}

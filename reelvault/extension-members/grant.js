// Full tab (not the popup) so the permission dialog cannot close it (DESIGN-SAAS.md 5.4 page_read).
const q = new URLSearchParams(location.search);
const job = q.get("job") || "";
const host = q.get("host") || "";
const ALLOWED = {"www.skool.com": "Skool profile page", "www.linkedin.com": "LinkedIn profile"};
const what = ALLOWED[host];
const ask = document.getElementById("ask");
const err = document.getElementById("error");

if (!what) {
  ask.textContent = "This request is not valid. Close this tab.";
  document.getElementById("allow").disabled = true;
} else {
  ask.textContent = `ReelVault wants to read your own ${what} (${host}) once, to fill in your profile.`;
}

async function answer(granted) {
  await chrome.runtime.sendMessage({type: "grant_result", job, granted});
  window.close();
}

document.getElementById("allow").addEventListener("click", async () => {
  try {
    const ok = await chrome.permissions.request({origins: [`https://${host}/*`]});
    await answer(!!ok);
  } catch (e) {
    err.textContent = "Chrome didn't allow it. You can paste the text in ReelVault instead.";
    err.hidden = false;
    await answer(false);
  }
});
document.getElementById("no").addEventListener("click", () => answer(false));

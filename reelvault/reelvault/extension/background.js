// ReelVault Connector
// When the ReelVault app on this computer is waiting for your Instagram login
// (you clicked "Connect Instagram" in it), this hands the app the login from
// this browser. It never sends anything anywhere else.

const APP = "http://127.0.0.1:8765";

async function call(path, body) {
  const r = await fetch(APP + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

let busy = false;
async function tryConnect(reason) {
  if (busy) return;
  busy = true;
  try {
    let hello;
    try {
      hello = await call("/api/ext/hello", { version: chrome.runtime.getManifest().version, reason });
    } catch (e) {
      return; // ReelVault isn't running; nothing to do
    }
    if (!hello.waiting) return;
    const list = await chrome.cookies.getAll({ domain: "instagram.com" });
    const cookies = {};
    for (const c of list) cookies[c.name] = c.value;
    if (!cookies.sessionid) return; // not logged in yet; we try again when you log in
    const res = await call("/api/ig/cookies", { cookies });
    await chrome.action.setBadgeBackgroundColor({ color: res.ok ? "#15803d" : "#b91c1c" });
    await chrome.action.setBadgeText({ text: res.ok ? "OK" : "!" });
    await chrome.action.setTitle({ title: res.ok ? "ReelVault: Instagram connected" : "ReelVault: " + (res.error || "failed") });
  } finally {
    busy = false;
  }
}

chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (!removed && cookie.name === "sessionid" && cookie.domain.includes("instagram.com")) tryConnect("login");
});
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.url && /^https:\/\/([a-z]+\.)?instagram\.com\//.test(tab.url)) tryConnect("tab");
});
chrome.runtime.onInstalled.addListener(() => tryConnect("installed"));
chrome.runtime.onStartup.addListener(() => tryConnect("startup"));
chrome.action.onClicked.addListener(() => tryConnect("clicked"));

// Functions the service worker runs INSIDE a tab with chrome.scripting.executeScript (default ISOLATED world:
// no page globals, nothing exposed to the page's scripts). Each one is self-contained: no imports, no closures.

// One Instagram API call from the extension's own instagram.com reading tab. The browser attaches the member's
// cookies itself; the extension never reads them, except csrftoken from document.cookie inside this tab.
export async function igFetch({method, path, form, appId, asbdId}) {
  const csrf = (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || "";
  const h = {"X-IG-App-ID": appId, "X-ASBD-ID": asbdId, "X-IG-WWW-Claim": "0",
             "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf};
  if (method === "POST") h["Content-Type"] = "application/x-www-form-urlencoded";
  try {
    // redirect "follow": the final URL tells a checkpoint (/challenge/), a suspended account (/accounts/suspended/)
    // and a login wall (/accounts/login/) apart (5.7). With "manual" every redirect looked like "signed out".
    const r = await fetch(new URL(path, location.origin), {method, credentials: "include", headers: h,
      redirect: "follow", body: method === "POST" ? new URLSearchParams(form).toString() : undefined});
    const text = r.type === "opaqueredirect" ? "" : await r.text();
    return {status: r.status, type: r.type, url: r.url, redirected: !!r.redirected,
            ct: r.headers.get("content-type") || "", text: text.slice(0, 3e6), page: location.href};
  } catch (e) {
    return {status: 0, type: "error", url: path, ct: "", text: "", error: String(e && e.message || e), page: location.href};
  }
}

// ds_user_id is readable from document.cookie unless Instagram made it HttpOnly (then the SW asks current_user).
export function readIds() {
  const m = document.cookie.match(/(?:^|; )ds_user_id=(\d{1,20})(?:;|$)/);
  return {ds_user_id: m ? m[1] : null, href: location.href};
}

// The member's OWN Skool page: keep only props.pageProps.{currentGroup, currentUser} of __NEXT_DATA__ (<= 1.5 MB).
export function readSkool() {
  const out = {final_url: location.href, title: document.title || ""};
  const el = document.getElementById("__NEXT_DATA__");
  if (!el) { out.missing = true; return out; }
  try {
    const nd = JSON.parse(el.textContent || "{}");
    const pp = (nd.props && nd.props.pageProps) || {};
    const keep = {props: {pageProps: {currentGroup: pp.currentGroup ?? null, currentUser: pp.currentUser ?? null}}};
    const s = JSON.stringify(keep);
    if (s.length > 1.5 * 1024 * 1024) { out.too_big = true; return out; }
    out.next_data = keep;
  } catch (e) { out.bad = true; }
  return out;
}

// The member's OWN LinkedIn profile (/in/me/ redirects there): visible main text, whitespace collapsed, <= 20,000.
export function readLinkedIn() {
  const main = document.querySelector("main");
  const text = ((main && main.innerText) || "").replace(/\s+/g, " ").trim().slice(0, 20000);
  return {final_url: location.href, title: document.title || "", text};
}

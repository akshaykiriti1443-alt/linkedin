// Every Instagram path the extension may call, in one table (DESIGN-SAAS.md 5.4). A change here is a one-line fix.
// Paths are built ONLY from this table, with values that passed validate.js, always through encodeURIComponent.
export const IG = {
  origin: "https://www.instagram.com",
  appId: "936619743392459", asbdId: "359341",
  readingPage: "/robots.txt",           // fallback if Instagram blocks scripts on text pages: "/accounts/edit/"
  currentUser: {method: "GET", path: () => `/api/v1/accounts/current_user/?edit=true`},
  profileInfo: {method: "GET", path: u => `/api/v1/users/web_profile_info/?username=${encodeURIComponent(u)}`},
  topsearch:   {method: "GET", path: q => `/web/search/topsearch/?context=blended&query=${encodeURIComponent(q)}&include_reel=true`},
  profilePage: {method: "GET", path: u => `/${encodeURIComponent(u)}/`},          // HTML; regex "profile_id":"(\d+)"
  userInfo:    {method: "GET", path: id => `/api/v1/users/${encodeURIComponent(id)}/info/`},
  clipsUser:   {method: "POST", path: () => `/api/v1/clips/user/`,
                form: (pk, maxId, size = "50") => ({target_user_id: pk, page_size: size, include_feed_video: "true", ...(maxId ? {max_id: maxId} : {})})},
  feedUser:    {method: "GET", path: (pk, maxId) => `/api/v1/feed/user/${encodeURIComponent(pk)}/?count=12${maxId ? `&max_id=${encodeURIComponent(maxId)}` : ""}`},
  mediaInfo:   {method: "GET", path: pk => `/api/v1/media/${encodeURIComponent(pk)}/info/`},
  keywordSerp: {method: "GET", path: q => `/api/v1/fbsearch/web/top_serp/?enable_metadata=true&query=${encodeURIComponent(q)}`},
  chaining:    {method: "GET", path: pk => `/api/v1/discover/chaining/?target_id=${encodeURIComponent(pk)}`},  // best effort
};

// Video downloads only from Instagram's CDN hosts (also the manifest's host_permissions).
export const CDN_DOMAINS = ["cdninstagram.com", "fbcdn.net"];
export function isCdnUrl(u) {
  try {
    const x = new URL(u);
    return x.protocol === "https:" && CDN_DOMAINS.some(d => x.hostname === d || x.hostname.endsWith("." + d));
  } catch { return false; }
}

// Signals of 5.7 that mean "Instagram wants us to slow down".
export const WARNING_RE = /please wait a few minutes|rate limit|spam|feedback_required/i;
export const CHALLENGE_RE = /checkpoint_required|challenge_required/i;

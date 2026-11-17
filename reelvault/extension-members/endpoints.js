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

// ---------------------------------------------------------------------------------------------- logged-out reads
// DESIGN-FETCH.md 3.2. Every path a LOGGED-OUT read may call. These run with credentials "omit" (reader.js
// igAnonFetch): no cookie, no CSRF token. kind: page (HTML) | api (JSON) | gql (GraphQL form post).
export const LO_IDS = ["lo_profile_page", "lo_reels_page", "lo_reels_gql", "lo_web_profile", "lo_posts_gql",
                       "lo_post_gql", "lo_media_gql", "lo_embed"];            // ladder order
export const LI_IDS = ["li_session", "li_clips", "li_feed", "li_profile", "li_media"];   // today's logged-in calls
export const LO_LIST_IDS = ["lo_reels_page", "lo_reels_gql", "lo_web_profile", "lo_posts_gql"];
export const LO_DETAIL_IDS = ["lo_post_gql", "lo_media_gql", "lo_embed"];
export const LO = {
  pageAccept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  lo_profile_page: {method: "GET", kind: "page", path: h => `/${encodeURIComponent(h)}/`},
  lo_reels_page:   {method: "GET", kind: "page", path: h => `/${encodeURIComponent(h)}/reels/`},
  lo_reels_gql:    {method: "POST", kind: "gql", path: () => `/graphql/query`,
                    friendly: "PolarisProfileReelsTabContentQuery_connection"},
  lo_web_profile:  {method: "GET", kind: "api", path: h => `/api/v1/users/web_profile_info/?username=${encodeURIComponent(h)}`},
  lo_posts_gql:    {method: "POST", kind: "gql", path: () => `/api/graphql`,
                    friendly: "PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery_connection"},
  lo_post_gql:     {method: "POST", kind: "gql", path: () => `/api/graphql`, friendly: null},
  lo_media_gql:    {method: "POST", kind: "gql", path: () => `/api/graphql`,
                    friendly: "PolarisLoggedOutDesktopWWWPostRootContentQuery"},
  lo_embed:        {method: "GET", kind: "page", path: c => `/reel/${encodeURIComponent(c)}/embed/captioned/`},
};
// Default GraphQL doc ids (they rotate). The server replaces them in hello.fetch.doc_ids without a new release.
export const LO_DOC_IDS = {
  lo_posts_gql: ["28904005159232689"], lo_reels_gql: ["29285972954340351", "7845543455542541"],
  lo_post_gql: ["27128499623469141"], lo_media_gql: ["27130156389949648"],
};
// The logged-in endpoint names of the IG table above -> the li_* id that Fetch health counts them under.
export const LI_OF = {clipsUser: "li_clips", feedUser: "li_feed", mediaInfo: "li_media", profileInfo: "li_profile",
                      topsearch: "li_profile", profilePage: "li_profile", userInfo: "li_profile",
                      currentUser: "li_session"};

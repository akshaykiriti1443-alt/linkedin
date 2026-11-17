# ReelVault fetch engine: read Instagram the safest way first (build contract)

Contract for three parallel builders (EXT, SERVER, WEB), written 2026-11-16. It extends DESIGN-SAAS.md sections 5, 5.4-5.8 and 11 and wins over it for
the fetch engine. Nothing here ran against real Instagram (this machine cannot reach it). Section 10 lists what is unverified.

## 0. Goal and hard rules
Three layers, tried in this order, with automatic fallback: (1) **logged_out**: public data read from the member's own Chrome, on the home connection,
with NO cookies. (2) **logged_in**: the existing paced pull with the member's own Instagram (DESIGN-SAAS.md 5.4), unchanged. (3) **rented**: later,
seam only (section 7).
- A logged-out request never carries a cookie, a CSRF token or `X-IG-WWW-Claim`, and never reads `document.cookie`.
- No fake accounts. No header tricks, no fingerprint changes, nothing beyond what a normal Chrome sends.
- One Instagram request at a time per browser, across both layers. All existing pacing rules stay.
- Every logged-out call counts in its own stricter budget AND in the existing hour and day budgets.
- A logged-in block (`rate_limited`, `warning`, `challenge`, suspended) also stops logged-out reads on that device.
- The server decides policy (`plan`). The extension obeys and keeps the stricter of local and server limits.
- UI text: plain words, short sentences, active voice, present tense.

## 1. Research: what a logged-out browser can read (sources: November 2026)
Sources read: yt-dlp 2026.10.26, instaloader HEAD 7acd1b6 (2026-11-01), gallery-dl 1.32.20, instagrapi HEAD eb95172. "source" = the request and
field names are in that code for a logged-out session. "guess" = no source confirms it.

| candidate | request | gives | paging | verdict |
|---|---|---|---|---|
| Profile page HTML | `GET /<handle>/` as a page load | embedded `xig_user_by_username`: `pk`, `id` (page id, NOT the pk), `username`, `is_private`, `follower_count`, `following_count`, `all_media_count`; first 12 posts in `polaris_ordered_timeline_connection` (only `code` is sure); `LSD` token | first page only | **source** (instaloader: "Instagram renders public profile pages with the data embedded, even when it refuses anonymous API requests"). Used: `lo_profile_page` |
| Logged-out posts query | `POST /api/graphql`, doc_id `28904005159232689`, variables `{id: <page id>, after, first: 12}` | 12 more post stubs per call: `data.node.polaris_ordered_timeline_connection` | `page_info.end_cursor` | **source** (instaloader `get_posts`). Used: `lo_posts_gql` |
| Post detail by shortcode | `POST /api/graphql`, doc_id `27128499623469141`, variables `{shortcode}` | `data.xdt_api__v1__media__shortcode__web_info.items[0]`: private-API media shape (`pk`, `code`, `taken_at`, `user`, `caption.text`, `like_count`, `comment_count`, `view_count`, `play_count`, `video_versions`, `video_duration`) | none | **source** (instaloader, "as the logged-out web app does"). Views may be missing (instaloader keeps a play-count fallback). Used: `lo_post_gql` |
| Post detail by media id | `POST /api/graphql`, doc_id `27130156389949648`, variables `{media_id}` | `data.xig_polaris_media.if_not_gated_logged_out`: same media shape; yt-dlp reads `view_count`, likes, comments, video | none | **source** (yt-dlp). `null` = gated post. Used: `lo_media_gql` |
| Reels tab query | `POST /graphql/query`, doc_id `29285972954340351` (gallery-dl) or `7845543455542541` (instaloader), variables `{data: {target_user_id, page_size: 12, include_feed_video: true}, after, first: 12}` | 12 reels per call with `play_count`, sparse (no caption, no date, no video URL) | `page_info.end_cursor` | shape **source**; logged-out access is a **guess** (gallery-dl sends `fb_dtsg`, a login token). Used: `lo_reels_gql`. The cheapest path if it works |
| Reels tab page HTML | `GET /<handle>/reels/` | maybe the first reels page embedded | none | **guess**. Used: `lo_reels_page` (1 call, also gives `LSD`) |
| `web_profile_info` | `GET /api/v1/users/web_profile_info/?username=` + `X-IG-App-ID` | `data.user` (`id`, `edge_followed_by.count`) + up to 12 timeline nodes with `video_view_count` | none | shape **source** (instagrapi). instaloader: "refused with 400 for some professional profiles, 401 or 429 for some sessions". Used late: `lo_web_profile` |
| Embed page | `GET /reel/<code>/embed/captioned/` | old pages had `shortcode_media` (views, video URL) | none | **guess**: current yt-dlp no longer reads it. Used last, optional: `lo_embed` |

Not used: `/api/v1/media/<pk>/info/` (login only in yt-dlp and instaloader; stays in layer 2), oEmbed (no counts), `graphql/query?query_hash=` and
`?__a=1` (dead: yt-dlp marks its user extractor `_WORKING = False`).

How blocks show up (all from source): a redirect to `/accounts/login/` or to `/` (yt-dlp `_is_login_redirect`, gallery-dl "home"); a redirect to
`/challenge/`; HTTP 429; HTTP 401 with `{"message": "Please wait a few minutes before you try again.", "require_login": true, "status": "fail"}`; HTTP
400 with `feedback_required` / `checkpoint_required` / `challenge_required`; HTTP 200 GraphQL with `errors` and no `data` (doc_id rotated); `data`
present but the wanted key is `null` or empty (gated post, login needed). Fragility: doc_ids rotate every few weeks to months (gallery-dl now
discovers them at run time). So doc_ids are server config (`hello.fetch.doc_ids`), not extension code. Embedded-page keys change less often.

## 2. The ladder
Order of the logged-out strategies: `lo_profile_page` > `lo_reels_page` > `lo_reels_gql` > `lo_web_profile` > `lo_posts_gql` > `lo_post_gql` >
`lo_media_gql` > `lo_embed` (table in 3.2). Then the logged-in layer: `li_session`, `li_clips`, `li_feed`, `li_profile`, `li_media` are the names of
today's calls, so health can count them.

### 2.1 One pull (`ig_pull`), stage 1 = logged out, stage 2 = logged-in top-up
```
budget = plan.lo_budget calls; S = plan.strategies (already without switched-off ones)
A  user:   lo_profile_page(handle) -> user, stubs, lsd, pageId. 404 -> fail not_found. is_private -> fail private.
           login_wall or no user -> lo_web_profile(handle) -> user + up to 12 "gql" items (upload them).
           still no user pk -> end stage, lo_stop "blocked".  Upload `account` with the first items call.
B  bulk:   lo_reels_page(handle) -> up to 12 sparse reels (upload). Then while more and budget left:
           lo_reels_gql(pk, cursor) -> upload. Any reel with views from B -> skip C and D.
C  views:  if B gave nothing: lo_web_profile(handle) once (unless used in A) -> upload "gql" items.
D  detail: if B gave nothing: todo = stubs not yet uploaded with views; skip stubs known to be photos.
           While todo < goal and pages < 3: lo_posts_gql(pageId, cursor) -> more stubs.
           For each stub (max lo_detail_max): lo_post_gql(code); on changed/login_wall switch for the rest to
           lo_media_gql(pk from code), then lo_embed(code). Not a reel -> drop. Upload every 6 items.
           read == auto and the first 3 details have no views -> stop D, lo_stop "no_views".
E  end:    POST items {items: [], paging: {source: "lo", stage_done: true, lo_stop}}.
           Server answers stop:true (pull done) or resume:{source:"clips", ...} (go on with stage 2, logged in).
```
- After every upload the server may answer `stop: true` (target reached, or refresh rule). Then skip to `done`. `lo_stop` is one of `target`, `no_more`,
  `budget`, `blocked`, `no_views`, `no_strategy`, `refresh_page`.
- Mode `refresh`: stage 1 reads step A plus ONE list result (B first page, else C, else D with max 12 details), then ends with `refresh_page`.
- Mode `next` with read `auto`: `plan.lo` is false (a logged-in cursor cannot continue logged out). With `logged_out_only` it pages from the top.
- Stage 2 is today's `runPull` loop, started by the server's `resume`. The sign-in check runs when stage 2 starts, not before. With `plan.li == false`
  there is no stage 2 and no sign-in check.

### 2.2 Merge: how a pull still delivers up to 100 newest reels
- Both stages upload to the same `POST /jobs/<id>/items`. The server upserts by `(user_id, ig_pk)` as today.
- A logged-out reel is **partial** when it has no views, or only `view_count` (logged-in uses `play_count`).
- Top-up at `stage_done` (read `auto`): the target is not reached or a reel of this pull is partial, and `plan.li` is true -> answer `resume:
  {"source": "clips", "max_id": null, "skip_known": true, "layer": "logged_in"}`. The top-up pages from the top. Known reels get their metrics updated
  (this clears `partial`) and do not count as new. The pull stops at the target as today.
- Read `logged_out_only`: no top-up. The pull closes with what it has (`target_reached`, `no_more` or `page_cap`). Partial reels stay partial; scoring
  falls back to likes for them, as it does for hidden counts.
- Refresh rule for logged-out uploads: the batch "hits a known reel" when it holds a stored reel whose stored `is_pinned` is false.
- Missing `taken_at` (sparse reels): the server derives it from the pk: `(pk >> 23) + 1314220021721` ms.

### 2.3 Block classes and cooldowns (per member device; the server computes, the extension mirrors at once)
| class (`code`) | signal on a logged-out call | cooldown scope | first / repeat | logged-in fallback |
|---|---|---|---|---|
| `rate` | 429; 401/403 with "Please wait a few minutes"; 400 `feedback_required` | all logged-out | 6 h, then x2, max 48 h. Reset after the first ok | not before 15 minutes |
| `challenge` | final URL has `/challenge/`; `checkpoint_required`; `challenge_required` | all logged-out | 24 h / 48 h | not before 60 minutes |
| `login_wall` | final URL `/accounts/login` or `/` after a redirect; 401/403; `require_login`; HTML where JSON is due | that strategy | 6 h / 24 h | at once, normal gap |
| `login_wall` on 2+ strategies in one job | - | all logged-out | 2 h / 6 h | at once |
| `changed` | 200 GraphQL with `errors` and no `data`; expected key missing | that strategy | 24 h | at once |
| `empty` | key present but `null` or no items, 3 times in a row | that strategy | 6 h | at once |
| `no_views` | 3 details in a row without views | detail strategies, for pulls only | 24 h | at once |
| `no_data` | page loads, no embedded data | none (row is `partial`) | - | next strategy |
| `refused` | 400 on `lo_web_profile` | none | - | next strategy |
| `error` | 5xx or network: retry once after 30 s | none | - | next strategy |

A single gated post (`if_not_gated_logged_out: null`, empty `items`) only skips that reel. Global rule: a strategy with 20+ attempts on 3+ devices
and under 20% ok in 24 h is switched off for everyone for 24 h (`fetch.strategy_off`). A probe that finds it ok, or the admin, switches it back on.

### 2.4 Pacing for logged-out reads (stricter or equal; added to `Pacing`, sent in `hello.pacing`)
| rule | value | key |
|---|---|---|
| Gap between logged-out calls | random 5-11 s | `lo_gap_ms: [5000, 11000]` |
| Gap between logged-out list pages (`lo_reels_gql`, `lo_posts_gql`) | random 8-15 s | `lo_page_gap_ms: [8000, 15000]` |
| Logged-out calls per hour / per day | 60 / 200 (and they count in 120 / 400 too) | `lo_hour_max`, `lo_day_max` |
| Logged-out calls per pull | 40 first, 16 refresh; 80 when `logged_out_only` | `plan.lo_budget` |
| Detail calls per pull | 36 | `lo_detail_max` |
| Gap between the last logged-out call and the first logged-in call | random 20-40 s | `layer_gap_ms: [20000, 40000]` |
| Probes per day per device | 3, max 12 calls each | `probe_day_max` |
| Tests | `RV_TEST_PACING=1` gives all of these short values, as today (only a `BUILD = "test"` extension obeys) | - |

## 3. EXT contract (owns `extension-members/*`, `tests/ext/*`). Version 2.1.0
### 3.1 Where logged-out requests run: inside the reading tab, `credentials: "omit"`
Chosen: a `fetch` from the extension's own instagram.com reading tab (ISOLATED world), like `igFetch`, but with `credentials: "omit"`. Why: the
request is same-origin from a real instagram.com page, with Chrome's own network stack, user agent and the home address. `omit` sends no cookie AND
ignores `Set-Cookie`, so the member's session is never sent and never changed. Not the service worker: its POSTs carry `Origin:
chrome-extension://...`, which no visitor sends. Not a real tab load with cookies stripped by a network rule: that changes headers (forbidden here)
and runs Instagram's page scripts next to the member's readable cookies. No new permission is needed. New function in `reader.js` (self-contained):
```js
export async function igAnonFetch({method, path, form, headers, referrer}) {   // returns what igFetch returns, plus {anon: true}
  const h = {...headers};                                                        // never X-CSRFToken, never X-IG-WWW-Claim, no document.cookie
  if (method === "POST") h["Content-Type"] = "application/x-www-form-urlencoded";
  const ref = typeof referrer === "string" && referrer.startsWith(location.origin + "/") ? referrer : location.origin + "/";
  const r = await fetch(new URL(path, location.origin), {method, credentials: "omit", cache: "no-store", headers: h, redirect: "follow",
    referrer: ref, referrerPolicy: "strict-origin-when-cross-origin", body: method === "POST" ? new URLSearchParams(form).toString() : undefined});
}                                                                                // cache "no-store" keeps a cached logged-in copy out
```

### 3.2 Strategy table (`strategies.js`: request specs + pure parsers; paths only from validated values)
| id (ladder order) | verified | request | headers | parse (always through `parseIg`, never bare `JSON.parse`) |
|---|---|---|---|---|
| `lo_profile_page` | source | `GET /<handle>/` | `Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8` | every `<script type="application/json"...>` blob; walk for `__bbox.result.data`; merge all `xig_user_by_username`; `lsd` = `/"LSD",\[\],\{[^{}]*"token":"([^"]+)"/`; pk fallback `/"profile_id":"(\d+)"/` |
| `lo_reels_page` | guess | `GET /<handle>/reels/` | same | same walk; first connection whose nodes have `media.code` |
| `lo_reels_gql` | guess when logged out | `POST /graphql/query` | GQL, friendly `PolarisProfileReelsTabContentQuery_connection` | first object under `data` (depth <= 4) with an `edges` array; item = `node.media ?? node` |
| `lo_web_profile` | source, often refused | `GET /api/v1/users/web_profile_info/?username=<handle>` | API | `data.user`; `edge_owner_to_timeline_media.edges[].node` |
| `lo_posts_gql` | source | `POST /api/graphql` | GQL, friendly `PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery_connection` | `data.node.polaris_ordered_timeline_connection` |
| `lo_post_gql` | source, views unsure | `POST /api/graphql` | GQL, no friendly name | `data.xdt_api__v1__media__shortcode__web_info.items[0]` |
| `lo_media_gql` | source | `POST /api/graphql` | GQL, friendly `PolarisLoggedOutDesktopWWWPostRootContentQuery` | `data.xig_polaris_media.if_not_gated_logged_out` |
| `lo_embed` | guess, optional | `GET /reel/<code>/embed/captioned/` | as page | `/__additionalDataLoaded\('extra',(\{.*?\})\);/` -> `shortcode_media` |

- API headers: `X-IG-App-ID: 936619743392459`, `X-ASBD-ID: 359341`, `X-Requested-With: XMLHttpRequest`, `Accept: */*`.
- GQL headers: `X-IG-App-ID`, `X-ASBD-ID`, `X-FB-LSD: <lsd>`, `X-FB-Friendly-Name` (when given), `Accept: */*`.
- GQL form: `lsd`, `fb_api_caller_class=RelayModern`, `fb_api_req_friendly_name` (when given), `server_timestamps=true`, `variables=<compact JSON>`,
  `doc_id`. Strip anything before the first `{` in the answer.
- Variables. posts: `{"id": pageId, "after": cursor, "before": null, "first": 12, "last": null,
  "__relay_internal__pv__PolarisSEOIsCrawlerrelayprovider": false}`. reels: `{"after": cursor|null, "before": null, "data": {"include_feed_video":
  true, "page_size": 12, "target_user_id": pk}, "first": 12, "last": null, "id": pk}`. post: `{"shortcode": code,
  "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": false}`. media: `{"media_id": pk}`.
- doc_ids come from `hello.fetch.doc_ids` (`{strategy: [id, ...]}`, each `/^\d{10,24}$/`); defaults are the ids in section 1. On class `changed` try
  the next id of that strategy once.
- `lsd` (`/^[A-Za-z0-9_-]{6,64}$/`) comes from the newest page HTML of this job. No lsd -> GQL strategies report `skipped`, code `no_lsd`. It may sit
  in the stored job state. It is never uploaded.
- Referrer: `/<handle>/` for list calls, `/reel/<code>/` for detail calls, `/` for page loads. GraphQL cursors are opaque: keep them in job state,
  validate `/^[\w=+\/:.|-]{1,600}$/`, never upload them.
- Trimming: media in the private-API shape (reels nodes, post and media detail) -> existing `trimMedia` -> item `{"shape": "v1", "strategy",
  ...IgMedia}`. Legacy nodes (`lo_web_profile`, `lo_embed`) -> new `trimGql` -> item `{"shape": "gql", "strategy", ...}` (4.2). Stubs are never
  uploaded. Keep only items owned by the target (`keepForTarget`) and only reels (`isReel`; for gql: `is_video`).

### 3.3 Ladder, state and pacing
- `ladder.js`: the state machine of 2.1 with injected `call(strategy, args)`, `upload(body)`, `now`, `sleep`, so node can test it with the fixtures.
- `classifyAnon(r, strategy)` -> `{status: ok|partial|blocked|error, code, http}` per table 2.3. It is separate from the logged-in `classify`. A
  logged-out block never sets the logged-in cooldown and never changes `signed_in`.
- Local state `chrome.storage.local.lo = {cooldown_until, cooldown_reason, blocks, strategy_off: {id: until_ms}}`. The extension applies table 2.3 at
  once, then takes the server's values from the reply (the later time wins).
- `pacing.js`: `take("lo")` and `take("lo_page")` use the `lo_*` gaps and count in `lo_hour_used`, `lo_day_used` AND in the existing hour and day
  counters. `report()` adds both `lo_*` counters.
- Job state adds `{stage: "lo"|"li", lo: {lsd, pageId, pk, cursor, todo: [codes], used, details, noViews}}`, so a restarted worker resumes the stage.
- Every call appends to `fetch_log` (max 60 per upload): `{strategy, status, code, http, ms, items, at}`. Send it with each `items`, `done` and
  `fail`. Logged-in calls log their `li_*` ids the same way.
- `rate` or `challenge` -> upload what is in hand, then `fail {code: "lo_blocked", lo: {class, strategy, http}}`. The server requeues or closes the
  pull. Every other class -> next strategy; none left -> step E.
- P2: `ig_videos` with `plan.lo` gets each video URL with `lo_post_gql(code)` first (logged-out budget, max 30) and uses `mediaInfo` after the first
  block. `ig_profile` with `plan.lo` uses `lo_profile_page` then `lo_web_profile`, else today's chain. `ig_discover`, `ig_check`: unchanged.
- `validate.js`: `read` in `auto|logged_out_only|logged_in_only` (default `auto`); `plan` = `{lo: bool, li: bool, lo_budget: int 0..80, strategies:
  [known ids]}`; `plan.lo` and `plan.li` both false is `bad_job`. New kind `ig_probe`: `{probe_id: rv id, handles: [1-2 handles], want: [known ids]}`.
- Lease call: `kinds` adds `ig_probe`; the body adds `lo: {cooldown_until, strategy_off}`. During a logged-in `signed_out` cooldown the extension
  still leases `ig_pull` and `ig_probe` (the server only hands out jobs whose `plan.li` is false).
- Popup: budget part "without login: 14 of 200" and the line "Reading without your login" or "Reading with your Instagram login". Manifest
  description: "Reads public reels of the accounts you choose. It reads without your login first. Your Instagram login never leaves this browser."

### 3.4 Probe job `ig_probe` ("Test my connection")
One call per row, logged-out pacing, max 12 calls. `handles[0]` = a well-known public account (default `instagram`), `handles[1]` = a tracked competitor.
1. `handles[0]`: `lo_profile_page`, `lo_reels_page`, `lo_reels_gql` (cursor null), `lo_web_profile`, `lo_posts_gql` (cursor from the page), then with
   the first reel found: `lo_post_gql`, `lo_media_gql`, `lo_embed`.
2. `handles[1]`: `lo_profile_page`.
3. `li_session` (a fresh sign-in check: one call, never the 6-hour cache; see 12.26), then `li_clips` (`clipsUser`, `page_size: "12"`, on
   `handles[1]` or else `handles[0]`). Skipped when signed out or cooling.
- Each row is posted when it finishes: `POST /jobs/<id>/probe {rows: [ProbeRow]}`. Then `done`. Example: `probe_upload.json`.
- `ProbeRow` = `{strategy, layer: "logged_out"|"logged_in", handle|null, status: "ok"|"partial"|"blocked"|"error"|"skipped", code|null, http, ms,
  items, fields: [...]}`. `fields` words: `user`, `followers`, `reel_list`, `lsd`, `views`, `likes`, `comments`, `taken_at`, `caption`, `video_url`,
  `duration`, `signed_in`. `ok` = 200, expected key, at least one item or a user. `partial` = 200 with something missing (`no_data`, `no_views`).
- A probe ignores per-strategy switch-offs (it is the re-test). It runs no logged-out row during a `rate` or `challenge` cooldown (`skipped`, code
  `cooling`). A `rate` result ends the logged-out rows.

### 3.5 EXT tests
- Node unit tests: every parser against every fixture of section 9. Ladder paths: bulk works; bulk blocked then details; details without views (stop
  after 3); login wall everywhere; `rate` on call 2; refresh; budget end.
- `fake_ig_server.py` serves all 3.2 requests with the functions of `make_lo_fixtures.py`. It answers HTTP 400 `cookie_leak` to a logged-out strategy
  request that carries `Cookie` or `X-CSRFToken`. Scenarios: `lo_login_wall`, `lo_rate_429`, `lo_wait_401`, `lo_no_embedded`, `lo_reels_blocked`,
  `lo_no_views`, `lo_docid_rotated`, `lo_gated`.
- e2e: an auto pull ends mixed and reaches the target; logged-out requests carry no cookies while logged-in ones do; one request at a time across both
  layers; after `rate` no logged-in call before the server's wait; the probe posts 11 rows. Cases that need new server replies may land after SERVER.

## 4. SERVER contract (owns `reelvault/server/jobs_ig.py`, `api_ext.py`, `reelvault/igparse.py`, `db.py` additions,
`migrations/versions/0002_fetch.py`, new `reelvault/server/fetch.py`, `tests/server/*`)
### 4.1 Read mode, plan and leasing
- `read` = `users.settings.read_mode` ?? `app_settings["fetch.default_read_mode"]` ?? `"auto"`. Add `"read_mode": "auto"` to `USER_SETTINGS_DEFAULT`.
  `start_pull` stores it in the job payload (`read`) and in `pulls.read_mode`.
- At lease time `_prepare_lease` writes `plan` into the payload. `lo` = read != `logged_in_only` AND device version >= 2.1.0 AND no logged-out
  cooldown AND no logged-in block cooldown AND not (`mode == "next"` with read `auto`) AND not `payload._skip_lo` AND an enabled list strategy. `li` =
  read != `logged_out_only` AND account confirmed AND signed in AND no logged-in cooldown. `lo_budget` per 2.4. `strategies` = `fetch.strategies`
  order minus global and device switch-offs.
- Lease the job only when `plan.lo or plan.li`. Else it waits with a plain message ("ReelVault waits until Instagram allows reads again."). A
  `logged_out_only` pull never goes to an extension below 2.1.0 ("Update the Chrome extension to read without your login.").
- `payload_problem` accepts `read`, `plan` and kind `ig_probe` (`EXT_KINDS`, `IG_KINDS`). `hello` adds `"fetch": {"layers": ["logged_out",
  "logged_in"], "strategies": [...], "off": [...], "doc_ids": {...}, "probe_handle": "instagram"}`, the 2.4 `pacing` keys and `"lo": {...}`.

### 4.2 Logged-out uploads: `POST /api/ext/v2/jobs/{id}/items` with `paging.source == "lo"`
Body (exact example: `tests/fixtures/ig/logged_out/upload_items.json`; expected result: `expected_reels.json`):
```jsonc
{"layer": "logged_out", "account": IgUser?,                      // IgUser as DESIGN-SAAS.md 5.4, from xig_user or data.user
 "items": [LoItem], "fetch_log": [FetchLog],
 "paging": {"source": "lo", "strategy": "lo_post_gql", "more_available": true, "stage_done": false, "lo_stop": null}}
// LoItem shape "v1": {"shape": "v1", "strategy": "lo_post_gql", ...every IgMedia field of DESIGN-SAAS.md 5.5}
// LoItem shape "gql":
{"shape": "gql", "strategy": "lo_web_profile", "id": "4017683790280261635", "shortcode": "DfBrh7jgAAD",
 "is_video": true, "product_type": "clips", "taken_at_timestamp": 1793165252, "caption": "first 5,000 chars",
 "video_view_count": 369570, "video_play_count": null, "like_count": 12913, "comment_count": 618,
 "like_and_view_counts_disabled": false, "comments_disabled": false, "video_duration": 48.4, "has_audio": true,
 "video_url": "https://scontent...mp4?...", "display_url": "https://scontent...jpg?...",
 "dimensions": {"width": 1080, "height": 1920}, "owner": {"id": "25025320", "username": "scaleit.demo"},
 "pinned": false, "coauthors": [{"id": "55501234", "username": "partner.studio"}]}
// FetchLog: {"strategy": "lo_reels_gql", "status": "blocked", "code": "login_wall", "http": 401, "ms": 210, "items": 0, "at": 1793512809}
```
`trimGql` sources: `like_count` = `edge_liked_by.count ?? edge_media_preview_like.count`; `comment_count` = `edge_media_to_comment.count ??
edge_media_to_parent_comment.count`; `caption` = first caption edge text; `pinned` = `pinned_for_users` not empty; `coauthors` from
`coauthor_producers`. Ids are digit strings.
- `igparse.normalize_lo(item, account_pk) -> ReelIn | None`. `v1` -> existing `normalize`. `gql` -> `pk = id`, `code = shortcode` (same shortcode
  cross-check), `views = video_play_count ?? video_view_count` with `views_field` `play_count` or `view_count`, owner from `owner.id`, video from
  `video_url` + `dimensions`, cover from `display_url`, `is_pinned = pinned`. Not a video -> `None`. Unknown `shape` or `strategy` -> `bad_input`.
- New `ReelIn` fields: `layer`, `strategy`, `partial` (views is `None` without hidden counts, or `views_field == "view_count"`), `missing` (list of
  `views`, `caption`, `duration`, `video_url`), `taken_at_derived` (`taken_at` missing -> derive from pk, 2.2).
- Ingest (`source: "lo"`): `FORBIDDEN_KEYS` still applies; `paging.max_id` is `bad_input`; no cursor bookkeeping (`to_max_id`, `cursor_source`
  untouched); count calls and reels in `pulls.layers`. A logged-out upsert never replaces a `play_count` value with a `view_count` value and never
  clears a stored caption, duration or video URL with `null`. A logged-in upsert always wins and sets `partial = false`.
- Reply: `{"ok", "accepted", "known_hit", "stop", "resume"?, "lo"?}`. At `stage_done` apply 2.2; `stop: true` there means the extension sends `done`.
- `fail` with the new code `lo_blocked` (`body.lo = {class, strategy, http}`): set the cooldown (2.3). Read `auto` and `plan.li` possible -> put the
  job back to `waiting` with `run_after` = now + 15 min (60 for `challenge`) and `payload._skip_lo = true`, no attempt used. Else close the pull with
  what it has. The reply carries the `lo` state.

### 4.3 Data model (`0002_fetch.py`; plain `op.add_column` / `create_table`, no CHECK changes)
- `pulls`: `read_mode` Text null; `layer` Text null (`logged_out` | `logged_in` | `mixed`); `layers` JSON default `{}` = `{"logged_out": {"reels",
  "calls", "strategies": {id: n}, "stop"}, "logged_in": {"reels", "pages"}}`. No new `stop_reason` values.
- `reels`: `read_layer` Text null, `read_strategy` Text null, `partial` Boolean not null default false.
- `fetch_health`: `device_id` Text (`"server"` for layer 3), `user_id`, `strategy`, `day` (UTC date text), `ok`, `partial`, `blocked`, `error`
  Integer, `last_ok_at`, `last_blocked_at`, `last_code`. Primary key (`device_id`, `strategy`, `day`). Filled from every `fetch_log` and probe row.
- `fetch_probes`: `id` (`prb_...`), `user_id`, `device_id`, `job_id`, `state` (`waiting|running|done|failed`), `handles`, `rows`, `verdict`,
  `created_at`, `finished_at`.
- `devices.state.fetch` (JSON, no migration): `{"lo_cooldown_until", "lo_cooldown_reason", "lo_blocks", "strategy_off": {id: iso}, "budget": {...}}`.
- `app_settings`: `fetch.default_read_mode`, `fetch.strategies` (order; null = section 2), `fetch.strategy_off` (`{id: {"until", "reason":
  "auto"|"admin"}}`), `fetch.doc_ids`, `fetch.probe_handle`.

### 4.4 Probe and health
- `POST /api/fetch/probe` creates `fetch_probes` + an `ig_probe` job (executor `extension`, high priority). Handles: `fetch.probe_handle` + the
  member's most recently pulled active account. An active probe is returned again. Over `probe_day_max` -> 429 `limit_reached`, "You ran 3 tests
  today. Try again tomorrow." No paired extension -> 409 `busy`, "Connect the Chrome extension first."
- `POST /api/ext/v2/jobs/{id}/probe {rows}` appends rows (max 20), bumps `fetch_health`, clears a device switch-off for each `ok` strategy. `done`
  sets the verdict: `best` = `lo_reels_page` or `lo_reels_gql` ok, or a list row ok and a detail row ok with `views`. `partial` = a logged-out row ok
  or partial, no path with views. `login_needed` = no logged-out row ok or partial, `li_clips` ok. `nothing` = none. Labels and texts: `fetch.py`.
- An `ig_probe` job is leased even when the member is signed out or has not confirmed the account. Only a logged-in block cooldown holds it back.

## 5. WEB contract (owns `reelvault/web/*`, `reelvault/members/*.md`, `tests/web/*`)
### 5.1 Settings card "How ReelVault reads Instagram" (in `settings.js`)
```jsonc
// GET /api/fetch/settings          PUT /api/fetch/settings {"read_mode": "auto"} -> the same object
{"read_mode": "auto", "default": "auto", "options": [
  {"id": "auto", "label": "Safest first", "recommended": true, "text": "ReelVault reads without your login first. It uses your login only for what is missing."},
  {"id": "logged_in_only", "label": "Logged-in only", "text": "ReelVault always reads with your Instagram login. It stays slow and careful."},
  {"id": "logged_out_only", "label": "Logged-out only", "text": "ReelVault never uses your login. You may get fewer reels and no view counts."}]}
// GET /api/fetch/status
{"read_mode": "auto", "device": {"connected": true, "version": "2.1.0", "needs_update": false}, "layers": [
  {"id": "logged_out", "label": "Without your login", "state": "ok", "detail": "Works. Last read 2 hours ago.", "cooldown_until": null, "last_ok_at": "...", "last_blocked_at": null},
  {"id": "logged_in", "label": "With your Instagram login", "state": "ok", "detail": "Works.", ...}],            // state: ok | blocked | cooling | off | unknown
 "last_probe": {"id": "prb_x", "at": "...", "verdict": {"code": "best", "title": "The safest way works."}}}
```
Three radio rows with a "Recommended" tag on the first, a **Save** button, then one line per layer from `status` and the link "Test my connection".

### 5.2 Pull status: which way each pull was read
```jsonc
// GET /api/fetch/pulls?account_id=acc_x&limit=20
{"pulls": [{"id": "pul_x", "account_id": "acc_x", "handle": "scaleit.demo", "mode": "first", "state": "done", "started_at": "...", "finished_at": "...", "reels_new": 100,
  "read": {"mode": "auto", "layer": "mixed", "logged_out": {"reels": 24, "calls": 18, "stop": "no_views"}, "logged_in": {"reels": 76, "pages": 3},
           "message": "Read 24 reels without your login. Your login filled in the other 76."}}], "next_cursor": null}   // state: waiting | running | done
```
Show `read.message` as one grey line under each competitor's pull status (competitors and working views). Server messages: `logged_out` "Read
without your Instagram login." · `mixed` as above · `logged_in` "Read with your Instagram login." · after a block "Instagram asks for a login right
now. ReelVault used yours." · logged-out only "Read 36 reels without your login. Instagram shows no more without a login." · cooldown "Instagram asks
ReelVault to wait. It goes on by itself."

### 5.3 Page "Test my connection" (`#test-connection`, new `views/fetch.js`)
```jsonc
// POST /api/fetch/probe {} -> 201
{"probe_id": "prb_x", "job_id": "job_x", "state": "waiting", "message": "Waiting for Chrome. The test starts within 30 seconds."}
// GET /api/fetch/probe/{probe_id} (poll every 3 s)      GET /api/fetch/probe/latest (on page open; 404 when none)
{"probe_id": "prb_x", "state": "running", "started_at": "...", "finished_at": null, "handles": ["instagram", "scaleit.demo"], "progress": {"done": 4, "total": 11},
 "rows": [{"strategy": "lo_profile_page", "layer": "logged_out", "label": "Profile page", "handle": "instagram", "status": "ok", "status_label": "Works",
           "gives": "Followers and the newest posts", "note": null, "ms": 640}],
 "verdict": {"code": "best", "title": "The safest way works.", "text": "ReelVault reads reels without your Instagram login on this connection.",
             "next": "You don't need to do anything."}}   // state: waiting | running | done | failed; verdict is null until done
```
- `status_label`: "Works", "Works in part", "Blocked", "Error", "Skipped". Layout: button **Run the test**; verdict box on top (green `best`, amber
  `partial`, `wait`, `login_needed` and `lo_only_blocked`, red `signed_out` and `nothing`); a table with the columns "What ReelVault tried",
  "Account", "Result", "What it gives", "Time" in two groups, "Without your login" and "With your Instagram login". Under it: "This test makes
  about 11 slow requests. It takes 2 minutes." A skipped row shows "—" as its time.
- Other verdicts (server-made, `verdict_of(rows, mode)`, checked in this order; see 12.25):
  `partial` "The safest way works in part." / "ReelVault reads profiles and videos without your login. It uses your login for view counts."
  (logged-out only: "ReelVault reads some reels without your login. Instagram hides the rest right now.") · `wait` "Instagram asks ReelVault to
  wait." / next "ReelVault goes on by itself. Run the test again in a few hours." · `lo_only_blocked` "Instagram asks for a login here. You chose
  to read without it." / next "Try again later, or pick Safest first in Settings." · `login_needed` "Instagram asks for a login here." /
  "ReelVault reads with your Instagram login. It stays slow and careful." · `signed_out` "You're signed out of Instagram in Chrome." / next
  "Sign in at instagram.com in Chrome. Then run the test again." · `nothing` "Instagram blocks reads right now." / next "ReelVault waits by
  itself. Run the test again in a few hours."
- No extension (or one older than 2.1.0): the page shows a red box "Connect the Chrome extension first." with a **Connect Chrome** button (or
  "Get the update") and **Run the test** stays off.

### 5.4 Admin card "Fetch health" (in `admin.js`), guides and tests
```jsonc
// GET /api/admin/fetch-health            (off: null | {"until", "reason"}; verified: source | guess | built)
{"window_h": 24, "strategies": [{"id": "lo_post_gql", "layer": "logged_out", "label": "Reel detail", "verified": "source", "enabled": true, "off": null,
   "ok": 212, "partial": 9, "blocked": 3, "error": 1, "rate": 0.94, "devices": 4, "last_ok_at": "...", "last_blocked_at": "...", "last_code": "login_wall"}],
 "layers": [{"id": "logged_out", "pulls": 31, "reels": 820}, {"id": "logged_in", "pulls": 12, "reels": 1100}, {"id": "mixed", "pulls": 9, "reels": 870}],
 "devices": [{"device_id": "dev_x", "user_email": "...", "version": "2.1.0", "lo_state": "ok", "lo_cooldown_until": null, "li_cooldown_until": null, "last_probe_verdict": "best", "last_seen_at": "..."}],
 "recent_blocks": [{"at": "...", "strategy": "lo_reels_gql", "code": "login_wall", "http": 401, "device_id": "dev_x"}],
 "settings": {"default_read_mode": "auto", "doc_ids": {"lo_post_gql": ["27128499623469141"]}, "probe_handle": "instagram"}}
// POST /api/admin/fetch-health/strategy {"strategy", "enabled"} -> {"ok": true}     PUT /api/admin/fetch-health/settings {"default_read_mode"?, "doc_ids"?, "probe_handle"?} -> {"ok": true}
```
Card: one row per strategy (name, layer, success rate, last ok, last block, on/off switch), three layer totals, the last 20 blocks, a doc_id editor.
Guides: add "How ReelVault reads Instagram" and "Test my connection" to `MEMBER-GUIDE.md` and `OWNER-GUIDE.md` (the owner runs the test once on his
PC; what each verdict means). Tests: mocked API for the four screens, with screenshots.

## 6. Endpoints (all new or changed; the web routes live in `api_ext.py`, whose router is already mounted; errors follow DESIGN-SAAS.md 4.1)
| # | endpoint | auth | owner | purpose |
|---|---|---|---|---|
| 1 | `POST /api/ext/v2/hello` (changed) | device | SERVER | adds `fetch`, logged-out pacing, `lo` state |
| 2 | `POST /api/ext/v2/jobs/lease` (changed) | device | SERVER | takes `lo` + logged-out budget; payload adds `read`, `plan`; kind `ig_probe` |
| 3 | `POST /api/ext/v2/jobs/{id}/items` (changed) | device | SERVER | logged-out items, `stage_done`, `fetch_log`; reply `stop` / `resume` |
| 4 | `POST /api/ext/v2/jobs/{id}/probe` (new) | device | SERVER | probe rows |
| 5 | `POST /api/ext/v2/jobs/{id}/done` and `/fail` (changed) | device | SERVER | `fetch_log`; fail code `lo_blocked` |
| 6 | `GET` / `PUT /api/fetch/settings` (new) | session | SERVER | member read mode |
| 7 | `GET /api/fetch/status` (new) | session | SERVER | which layer works for this member |
| 8 | `GET /api/fetch/pulls` (new) | session | SERVER | how each pull was read |
| 9 | `POST /api/fetch/probe`, `GET /api/fetch/probe/{id}`, `GET /api/fetch/probe/latest` (new) | session | SERVER | Test my connection |
| 10 | `GET /api/admin/fetch-health`, `POST .../strategy`, `PUT .../settings` (new) | admin | SERVER | Fetch health card |

## 7. Seam for layer 3 (rented addresses on a hosted server; NOT built)
1. Layer id `rented`, strategy ids `rx_*`. `fetch.layers` (app setting) holds the layer order; today it is `["logged_out", "logged_in"]`.
2. Future package `reelvault/server/fetchers/`: `Fetcher.pull(handle, pk, target, budget) -> Iterator[LoBatch]`; `LoBatch` is the 4.2 upload body.
3. `jobs_ig.ingest_lo(user_id, job, body, device=None)` is the one entry point for the extension route and a future fetcher. Build it that way now.
4. `fetch_health.device_id = "server"` for server fetchers; the health card then shows them with no change.
5. `pulls.layer` and `reels.read_layer` are plain text, so `rented` needs no migration.
6. A new job executor value is NOT added now. Later: `executor = "server"` with kind `ig_pull_rx`.
7. Proxy credentials come from env (`FETCH_PROXY_URL`), never from the repo or the database.
8. The parsers of section 3.2 have Python twins only where the server needs them later; none are built now.
9. Cooldowns for `rx_*` are per rented address, kept in `app_settings`, not per member device.
10. The plan gets a third flag `rx`; the extension ignores unknown plan keys.

## 8. File ownership (touch nothing outside your list; report a contract problem instead of changing this file)
- **EXT**: `extension-members/*` (new: `strategies.js`, `ladder.js`; changed: `reader.js`, `endpoints.js`, `pacing.js`, `validate.js`, `trim.js`,
  `sw.js`, `popup.*`, `manifest.json`), `tests/ext/*`.
- **SERVER**: `reelvault/server/jobs_ig.py`, `api_ext.py`, new `fetch.py`, `db.py` (additions only), `reelvault/igparse.py`,
  `migrations/versions/0002_fetch.py`, `tests/server/*` (new files `test_fetch_*.py`; keep existing tests green).
- **WEB**: `reelvault/web/*` (new `static/views/fetch.js`; changed `settings.js`, `competitors.js`, `working.js`, `admin.js`, `app.js`, `app.css`),
  `reelvault/members/*.md`, `tests/web/*`.
- **Read-only for all**: `DESIGN-FETCH.md`, `tests/fixtures/ig/logged_out/*` (architect), every other file.
- `python -m pytest -q tests` must stay green. An extension 2.0.0 must keep working against the new server.

## 9. Fixtures: `tests/fixtures/ig/logged_out/` (made by `make_lo_fixtures.py`; `manifest.json` gives strategy, HTTP status, class, verified per file)
- success: `profile_page.html`, `profile_page_codes_only.html`, `reels_page.html` (guess), `web_profile_info_ok.json`, `posts_gql_page1.json`,
  `posts_gql_page2_last.json`, `reels_gql_page1.json`, `reels_gql_page2_last.json`, `reels_gql_wrapped.json` (all three: shape source, logged-out
  guess), `post_gql_ok.json`, `post_gql_photo.json`, `media_gql_ok.json`, `embed_ok.html` (guess).
- partial: `profile_page_no_data.html` (`no_data`, guess), `post_gql_no_views.json` (`no_views`), `embed_no_data.html` (guess).
- login wall: `login_page.html` (served after a redirect to `/accounts/login/`), `require_login_401.json` (body guess). Rate limit: `wait_401.json`,
  `rate_429.json`. Changed: `gql_error.json`. Empty: `post_gql_empty.json`, `media_gql_gated.json`. Private: `profile_page_private.html`.
- contract examples: `upload_items.json` -> `expected_reels.json` (SERVER), `probe_upload.json` (EXT, SERVER, WEB).
- Media pks are 19-digit JSON numbers in the v1 shapes. Target: `scaleit.demo`, pk `25025320`, page id `17841400025025320`, lsd `AVqFixtureLsd01`.
  Same timeline as `make_fixtures.account_timeline(40)`, plus a photo at every 4th place of the profile timeline.

## 10. Unverified assumptions (the owner's live self-test settles them)
1. A cookie-less `fetch` of `/<handle>/` (not a page navigation) gets the embedded data. instaloader sends navigation headers. If not:
   `lo_profile_page` is `partial` and only pk + lsd remain.
2. `POST /api/graphql` works with `lsd` and NO csrf cookie or header (instaloader and yt-dlp send one).
3. The reels tab query and the reels page work logged out. If not, logged-out gives no fast view counts.
4. Logged-out post detail carries `play_count` or `view_count`. instaloader's fallback suggests it often does not. Then logged-out serves profiles,
   lists and video URLs, and the login fills in view counts.
5. All doc_ids are valid on the day of the test. They rotate.
6. `web_profile_info` answers without cookies on a home connection.
7. Stub nodes carry `pk`, `media_type` or `product_type`. Only `code` is sure.
8. Embed pages still carry data.
9. Our 60 logged-out calls per hour sit under Instagram's limit for one home address. No source gives a number.
10. Logged-out and logged-in requests from one browser minutes apart are no signal for Instagram. The 20-40 s layer gap and the 15 minute wait after a
    block are caution, not measured rules.

Owner self-test: Settings > "Test my connection" > **Run the test** on his PC. The table shows each strategy as works, works in part or blocked. The
admin card keeps the same numbers per member device.

## 11. Priorities
**P1**: `igAnonFetch`, `lo_profile_page`, `lo_posts_gql`, `lo_post_gql`, `lo_reels_page`, `lo_reels_gql`, ladder 2.1, merge 2.2, cooldowns 2.3, pacing
2.4, uploads 4.2, plan 4.1, migration 4.3, probe 3.4 + 4.4, section 5. **P2**: `lo_web_profile`, `lo_media_gql`, logged-out video URLs in
`ig_videos`, logged-out `ig_profile`. **P3**: `lo_embed`, the doc_id editor, the global auto switch-off.

## 12. Decisions log (build and integration, 2026-11-17)
Folded from `docs/wp-notes/FETCH-EXT.md`, `FETCH-SERVER.md` and `FETCH-WEB.md` (they keep the details and evidence) plus the integration run.
This section wins where it differs from sections 2-7. Still nothing ran against real Instagram.

**Extension (2.1.0)**
1. The random logged-out gap is taken after the reading tab is ready, so it sits right before each request.
2. `ig_videos` has no handle page for the `lsd` token. The worker keeps the newest `lsd` in `chrome.storage.session` (memory only, max 6 h,
   never uploaded). Without one, or after the first block, the login reads video URLs (`mediaInfo`). In `logged_out_only` such a reel is
   reported `login_needed` (retried later). Nothing works at all -> `lo_blocked` / `login_wall`.
3. After `changed`, a next doc id that works becomes the device's first choice (`lo.doc_ok`).
4. Popup: the reading line shows from the job's plan at once. The wait line says "Instagram asks ReelVault to wait." after `rate` or
   `challenge` and "Instagram asks for a login right now." after a login wall.
5. The 4th probe of a day posts every row as `skipped` / `budget` and makes no call.
6. The reading tab loads `/robots.txt` as a normal page, with the member's cookies, as in 2.0.0. No logged-out DATA request carries a cookie.
   Zero cookie traffic in `logged_out_only` would need a separate tab type (not built).
7. A logged-out page load that Instagram redirects (login wall, challenge) is followed by the same cookie-less fetch, as a browser does.
8. After a logged-out `rate` / `challenge` block the extension also holds the login locally (`lo.li_not_before`, 15 / 60 min), on top of the
   server's `run_after`.

**Server (1.1.0)**
9. One CHECK change after all: `jobs.kind` must allow `ig_probe`. The migration rebuilds only `jobs` (batch mode); `reels` and `pulls` get plain
   `ADD COLUMN` (a batch copy of `reels` would cascade-delete its child rows on SQLite).
10. App settings use the `internal.fetch.*` prefix, so they stay out of the general admin settings list. Only the Fetch health routes change them.
11. `USER_SETTINGS_DEFAULT["read_mode"]` is `None`: the admin default applies until the member chooses.
12. `fetch.py` registers `ig_probe` in `jobs.KINDS`. A queued pull follows the read mode at lease time.
13. A pull is leased when the login may be used, or when the device reads logged out and the mode is not `logged_in_only`, or when the mode
    is `logged_out_only`. A stale "signed out" report does not hold the login back: the sign-in check at the start of stage 2 settles it.
    No layer usable -> back to waiting for 1 minute with a plain message, no attempt spent. A 2.0.0 extension keeps the old rules.
14. Top-up past the target: when reels of this pull still lack views, the logged-in top-up reads up to 4 more pages (`TOP_UP_PAGES`) and
    never stores more than the target. Partial reels write no `reel_snapshots` row until real views arrive.
15. `stop_reason` keeps the old values: `target` -> `target_reached`, `no_more` -> `no_more`, `refresh_page` -> `known_reel` or `page_cap`,
    the rest -> `page_cap`. The fine reason stays in `pulls.layers.logged_out.stop`.
16. `no_views` is one device flag (`detail_off_until`, 24 h). Budgets: `next` gets 40, `logged_out_only` 80; the server counts logged-out
    calls itself and takes the larger count; it ends a stage after 80 logged-out uploads in one lease.
17. A probe in `logged_out_only` has no `li_*` rows. A probe that stopped after its logged-out rows still ends `done` with a verdict.
18. Layer 3 seam: `jobs_ig.ingest_lo(user_id, job, body, device=None)` takes `layer: "rented"` with `device=None`; health rows then use
    `device_id = "server"`. `fetch.layer_order()` reads `internal.fetch.layers`. No fetcher, executor or proxy code exists.

**Web**
19. The Settings card sits on **Settings > Chrome extension**. Pull lines show on the competitors list and per pull on the competitor page
    (`working.js` shows no pull status). Fetch health is a card on **Admin > Health**; only logged-out ways get a switch. The read mode
    applies on **Save**. An empty doc_id box sends `[]` (= built-in id). A server without `/api/fetch/*` hides the card and the lines.

**Integration (found by the end-to-end journeys)**
20. The localhost app (installed package, no migrations folder) used `create_all`, which never adds columns to an existing database. An
    update would have broken the owner's PC. Now `members_local --setup` copies `migrations/` to `%USERPROFILE%\ReelVault-Members\migrations`
    and sets `RV_MIGRATIONS_DIR`. `db.migrate()` stamps an unversioned database (0001, or 0002 when `pulls.read_mode` exists) before it
    upgrades. Tests: `test_unversioned_*` in `tests/server/test_fetch_migration.py`.
21. A `logged_out_only` pull that read nothing says so: "Instagram asks for a login right now. You chose to read without it, so ReelVault
    read no reels. Try again later or pick Safest first in Settings." Its layer stays empty (it was shown as "logged in").
22. The logged-out layer line after a login-wall cooldown says "Instagram asks for a login right now. ReelVault tries again later by itself."
23. Versions: extension 2.1.0, server `settings.VERSION` 1.1.0. The desktop package stays 0.4.0 (its self-update compares that number).
24. `install-members.ps1` needs no change for the new files (`build_extension.py` packs the whole folder). It now also prints the reload hint.

**Integration evidence** (dev server `python -m reelvault.server.dev` + `tests/ext/fake_ig_server.py` + the real extension in Chromium):
auto pull fully logged out (10 calls, 0 cookies, 100 reels scored); partial logged-out (39 reels) + logged-in top-up to exactly 100 unique;
login wall -> fallback at once, cooldown and health rows; `rate` -> 15 min wait with no call, then the login; `logged_out_only` + block ->
no logged-in call and the plain message; Test my connection from the web page -> 11 rows, verdict `best`; Pull next 100 after the mixed pull
-> from the saved cursor, 100 older reels, 200 unique.

**Review fixes (2026-11-17, after the safety and member-experience reviews)**
25. Verdict order: `best` -> `wait` (a `rate` row and no list or video row worked) -> `partial` (a list row or a video row read something;
    the profile page alone is not enough) -> `lo_only_blocked` (mode `logged_out_only`; `wait` when an earlier wait still runs) ->
    `login_needed` -> `signed_out` (a logged-in row says `signed_out`) -> `wait` (cooling rows only) -> `nothing`. The probe payload carries
    `read` (the member's mode at start) so the verdict never tells a logged-out-only member to sign in. With `best` or `partial` and a
    signed-out login (not logged-out only) the next step is "Sign in to Instagram in Chrome. ReelVault uses your login to fill in what is
    missing."
26. The probe's sign-in row always checks fresh (one call, inside the 12-call cap); it no longer trusts the 6-hour cache.
27. No made-up blocks: the extension reports `lo_blocked` only with the class and strategy of a REAL Instagram answer. A video or profile job
    in `logged_out_only` whose logged-out calls were all skipped (no lsd, cooling, budget, switched off) fails with `budget` and
    `cooldown_s` (15 min to 6 h): the job waits, no strike, no switch-off, no audit row. With no fresh lsd a video job first reads the
    reel account's profile page (cookie-less) for a token; `ig_videos` items carry an optional `handle` for that. Server defense in depth:
    a `lo_blocked` fail whose own `fetch_log` has only `skipped` rows for the named strategy applies no block and waits 30 minutes
    (`jobs_ig._lo_block_unsent`). A log with no row for the strategy proves nothing (a pull uploads its log before it fails) and the block
    stands.
28. Settings layer line: a new state `partial` ("Works in part" pill, amber). The "signed out" line without a cooldown says "Sign in at
    instagram.com. ReelVault checks again by itself." (there is no Try again button there).
29. `GET /api/fetch/pulls` adds `read.stop_label`: for a logged-out-only pull that ended on a block, "Instagram asked for a login" (on the
    budget, "Read the safe amount for now"). The Pulls table shows it instead of "Stopped at the page limit". `stop_reason` stays `page_cap`
    (no new CHECK value, no migration).
30. The fake Instagram answers `login_required` (403) on the logged-in user endpoints when the session cookie is missing, as Instagram does.

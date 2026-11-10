# WP6 Claude connector: notes

Status: built and tested. OAuth 2.1 server (`server/oauth.py`), MCP server (`server/mcp_server.py`), Claude plugin
(`plugin/`), tests (`tests/mcp/`, 41 tests) that use the official `mcp` 2.3 client over real HTTP (uvicorn on
ports 9220-9239).

## What a member does (and what happens)

1. Claude: Customize > Connectors > Add custom connector, URL `https://HOST/mcp`.
2. Claude calls `/mcp` without a token and gets `401` + `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"`.
3. Claude reads the protected resource metadata, then `/.well-known/oauth-authorization-server`.
4. claude.ai uses its client metadata document URL as `client_id` (CIMD). Claude Code and older clients register
   with `POST /oauth/register` (DCR).
5. `GET /oauth/authorize`: no session -> `303 /#login?next=<authorize URL>`; with a session -> the consent page
   ("**claude.ai** wants to use your ReelVault account. ..."), Allow / Cancel.
6. Allow -> 5-minute single-use code -> Claude swaps it at `POST /oauth/token` (PKCE S256) for a 1 h access token
   (`rva_`) and a 30-day refresh token (`rvr_`). Only sha256 hashes are stored.
7. Every MCP request: `DbTokenVerifier` -> `oauth.verify_access(token)` (access kind, not revoked, not expired,
   resource = `PUBLIC_URL/mcp`, member active). Tools read the member from `get_access_token().subject` only.

## Interfaces other packages use

`reelvault.server.oauth`
- `router` (in `app.ROUTERS`): `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource`
  (+ `/mcp`), `/oauth/authorize` (GET page, POST consent), `/oauth/token`, `/oauth/register`, `/oauth/revoke`,
  `/api/connector/clients`, `/api/connector/disconnect`, `/download/claude-plugin.zip`.
- `verify_access(token) -> {"user_id", "client_id", "expires_at"} | None`
- `clients_for(user_id) -> [{"client_id", "name", "kind", "connected_at", "last_used_at"}]`,
  `disconnect(user_id, client_id=None) -> int` (revoked token count)
- `cleanup(now=None) -> {"codes", "tokens", "clients"}`: deletes expired codes/tokens (+1 day) and DCR/CIMD clients
  unused for 30 days. Runs by itself at most every 10 min from the OAuth endpoints. **WP1: you may also call it
  from the daily sweep.**
- `issuer()`, `resource_url()`, `as_metadata()`, `protected_resource_metadata()`, `redirect_allowed(uri)`,
  `redirect_matches(registered, uri)`, `resolve_client(client_id)`, `plugin_zip() -> bytes`, `OAuthError`.

`reelvault.server.mcp_server`
- `mount(app)`: builds a NEW `MCPServer` per app, appends `lambda: mcp.session_manager.run()` to
  `app.state.lifespan_hooks`, mounts through `app.mount_last(app, mcp_app, MCP_PATHS)`, keeps it at `app.state.mcp`.
  The session manager runs once per app lifespan: an app can only be served by one `TestClient(...)` context or
  one uvicorn run.
- `INSTRUCTIONS` (the server instructions, 879 chars), `tool_names()`, `wrap_third_party(obj)`,
  `DbTokenVerifier`, `build()`, `transport_security()`.

### New member endpoints (WP7: Settings > AI > "Use my Claude plan")
- `GET /api/connector/clients` -> `{"clients": [{"client_id", "name": "claude.ai" | "Claude Code on this computer"
  | "<host>", "kind": "cimd" | "dcr", "connected_at", "last_used_at"}], "url": "https://HOST/mcp"}`
- `POST /api/connector/disconnect` `{"client_id": "..."}` or `{"all": true}` -> `{"ok", "revoked": n, "clients": [...]}`
  (session + CSRF like every `/api/*` POST). The design names a **Disconnect** button but no endpoint.
- `GET /download/claude-plugin.zip`: public, no secrets; `.mcp.json` rendered with `PUBLIC_URL`.

### WP7 must do (login round trip)
The authorize page sends signed-out members to `/#login?next=<url-encoded /oauth/authorize?...>`. After a
successful login the SPA must do a full page load of `next` (`location.href = next`), and ONLY when `next` starts
with `/oauth/authorize?` (anything else = ignore it; this stops open redirects). Same after signup when the
account is active at once.

### WP1 (deploy) should do
- Copy `plugin/` into the image at `/opt/reelvault/plugin` (or set `RV_PLUGIN_DIR`). Without it the zip is built
  from a copy in code (same files, shorter Skill text).
- `TRUSTED_PROXIES` must include Caddy, so OAuth limits see the real client IP (they use `ratelimit.client_key`).
- Caddy already streams `/mcp` without buffering; nothing else is needed (`json_response=True`, stateless).

## Tools (7.3.3)

All 18 tools of the design plus 2 (see deviations): `get_my_profile`, `list_pending_work`, `list_competitors`,
`start_competitor_pull`, `job_status`, `list_outliers`, `what_is_working`, `get_taxonomy`, `get_tag_batch`,
`save_tags`, `get_reel_for_analysis`, `get_more_frames`, `save_analysis`, `get_output_brief`, `save_output`,
`get_next_task`, `save_profile_draft`, `save_topics`, `save_format_names`, `save_topic_retag`.
- Inputs are the `schema_members` pydantic models (`QuickTagBatch`, `ReelBreakdown`, `OutputData` discriminated on
  `kind`, `ProfileDraft`, `TopicList`, `FormatGuide`, `TopicRetag`), so `tools/list` carries their JSON Schema and a
  wrong Literal comes back as `isError` with the pydantic text. Cross-check failures from `work.save_result`
  (`work.Invalid`) come back as `isError` too: "That didn't pass the checks. Fix it and send it again: ...".
- Every body runs in `anyio.to_thread.run_sync`; the token check too.
- Image tools (`get_tag_batch`, `get_reel_for_analysis`, `get_more_frames`) return text + JPEG images and pass a
  final guard: text + base64 < 90,000 characters (text cut first, never below 2,000; then images from the end,
  never the first). Measured on the fixture clips: tag batch of 3 reels 68,148 chars (3 sheets of 21 KB),
  deep read 36,439 chars (sheet 21 KB + frames 6.6 KB and 6.4 KB), 4 more frames each <= 12 KB.
- Structured tools keep their JSON under 40,000 chars (they are sent twice: structured + text).
- Third-party text: captions/transcripts come wrapped from `work`; WP6 wraps `full_name`, `caption_first_line`,
  `caption`, `bio`, track `title`/`artist`, `spoken`, `on_screen_text`, `hook_line` in its own results
  (`wrap_third_party`).
- Per-member limit: 300 tool calls a minute (in memory).
- `save_analysis` is an idempotent upsert: re-saving a reel whose job is done replaces the breakdown.
- `save_output` checks `kind` against the output and `chunk_index` against `inputs.chunks_done`.

## Deviations from DESIGN-SAAS.md (please fold in)

1. **Two extra tools.** The design has save tools for profile drafts, topics and format names but no way for Claude
   to GET those tasks, and no save tool for `retag_topics` (jobs with executor `mcp` would wait forever).
   Added `get_next_task {"kind"?}` -> `{"batch_id", "kind", "brief", "schema_name", "save_with"}` and
   `save_topic_retag {"batch_id", "tags": [TopicRetag]}`. INSTRUCTIONS and the Skill mention them.
2. **Protected resource metadata is served by `oauth.router`**, not the SDK, so `authorization_servers` is exactly
   `["https://HOST"]` (the SDK writes `"https://HOST/"`) and equals the AS `issuer`. The SDK route behind the mount
   is shadowed. Also served at `/.well-known/oauth-protected-resource` (root).
3. **No `iss` in the authorization response** and `authorization_response_iss_parameter_supported` is not
   advertised: the mcp SDK compares `iss` with a normalized issuer (`https://HOST/`) and would reject ours.
4. **Code replay** (a code used twice) revokes the tokens it gave out (RFC 6749 4.1.2). The family id of a code's
   tokens is derived from the code hash (`fam_<hash[:32]>`), so no new column is needed.
5. **DCR auth method**: a registration asking for `client_secret_basic`/`_post` is accepted but registered and
   answered as `none` (public client); secrets are never issued. Other `grant_types` / `response_types` are refused.
6. **Unknown scopes are dropped**; the grant is always `reelvault` (RFC 6749 3.3 allows narrowing).
   `resource` may be omitted (defaults to `PUBLIC_URL/mcp`); a trailing slash is tolerated; anything else is
   `invalid_target`.
7. **Consent page** has its own strict CSP (`default-src 'none'`, hashed inline style, `form-action 'self'` + the
   redirect origin, because Chrome applies form-action to the redirect after the POST). Origin/Referer must be our
   origin when present, plus the session's CSRF token. 30 authorize page views per member per hour.
8. **`/mcp` Origin allowlist**: our origin, `https://claude.ai`, `https://claude.com` (requests without Origin pass).
   Browser-based clients from other origins get 403 from the SDK; use the MCP Inspector's proxy mode.
9. `get_output_brief.options` is typed as an object (validated by `outputs.parse_options`) instead of the full
   `OutputOptions` schema, to keep the tool list small. When a brief's output is paused waiting for a source
   reel's deep read, the tool says which reels to analyze first.
10. CIMD documents must answer 200 **without redirects**; `RV_TEST_ALLOW_HTTP_CIMD=1` (tests only) allows
    `http://` and private addresses for the document server.
11. `start_competitor_pull` with mode `first` adds an untracked account (same rules and daily cap as
    `POST /api/accounts`); `next`/`refresh` need a tracked account.

## Tests (`tests/mcp/`, run: `python -m pytest -q tests/mcp`)
- `test_mcp_oauth.py` (25): 401 + header, PRM `resource` exact, AS metadata fields, DCR code flow, **the SDK's own
  `OAuthClientProvider` doing the whole flow** (401 -> PRM -> metadata -> DCR -> authorize -> token -> tool call),
  CIMD flow with a localhost document server (loopback any port, cached 24 h, consent shows the URL host not the
  self-asserted name), 4 bad CIMD documents, redirect rules, redirectable errors, login redirect, cancel, CSRF and
  Origin, form-only token endpoint, PKCE/redirect/client/resource mismatches, expired and replayed codes, refresh
  rotation + reuse revokes the family, expired token and disabled member -> 401, RFC 7009 revoke, 50 DCRs from one
  Anthropic-range IP all succeed while 30/key/hour holds elsewhere, global caps, token limit per (IP, client),
  connected apps + disconnect, `/api/ai` shows `connected: true`, plugin zip, cleanup.
- `test_mcp_tools.py` (16): `tools/list` names and schemas (no user id anywhere), INSTRUCTIONS over the wire,
  profile/pending/competitors/taxonomy, outliers and what_is_working, pull queueing, tag batch < 90,000 chars
  and leases exactly the reels it shows, bad Literal / missing reel -> isError, full tag loop, deep read payload
  (text + sheet + 2 frames) and the injection fixture (`tests/fixtures/ig/injection.json`) only inside
  `<untrusted>` with the fake closing tag escaped, save_analysis validation + upsert + remaining_in_queue,
  next waiting deep read, get_more_frames (and the cleaned-up video error), a 2-chunk reel script end to end, an
  output made in the web UI, profile draft + topics through get_next_task, member A's token can't reach member B,
  and a slow tool body + a CPU-burning process don't block another call (< 1.5 s).
- The tools tests reuse WP5's seeding helpers (`tests/server/_wp5_support.py`: profile, topics, reels with real
  processed fixture clips, fake AI answers).

## Manual checks (not automated)
- **claude.ai through a tunnel** (owner test 6c, 14.2): needs the real domain or a Cloudflare tunnel and a Claude
  Pro account. Not run here (no public URL from this machine). Steps: deploy, add the connector, Connect, Allow,
  then "Analyze my new ReelVault reels"; check one tag loop and one analyze loop finish, and screenshot each step
  for the 7.3.4 copy.
- **MCP Inspector smoke test**: `npx @modelcontextprotocol/inspector`, transport "Streamable HTTP", URL
  `https://HOST/mcp`, connection type "Via Proxy" (the direct browser mode sends an Origin we don't allow), then
  "Open Auth Settings" > "Quick OAuth Flow". Expect the ReelVault consent page, then `tools/list` with 20 tools.
- **Claude Code**: `claude mcp add --transport http reelvault https://HOST/mcp`, then `/mcp` > Authenticate. It
  registers with DCR and a `http://localhost:<port>/callback` redirect.

# WP1 Core platform: first pass notes

Status: foundation done (items 1-6 of the first pass). Every module named in DESIGN-SAAS.md section 13 imports.
All server tests (`tests/server`, 72 tests) and the desktop tests (`tests/test_*.py`) pass.

## How to use it (for WP2-WP7)

### Tests
```python
# tests/<yours>/conftest.py  (tests/server/conftest.py already does this)
from reelvault.server.testing import *  # noqa: F401,F403
```
Fixtures (function-scoped; each test gets a fresh DATA_DIR and a SQLite file already at the migration head):
`rv_env` (Path of DATA_DIR), `app` (`create_app("web")`), `client` (anonymous TestClient, lifespan entered,
base_url `http://localhost`), `make_user(email=None, role="member", status="active", name=None) -> TestUser`,
`member`, `other_member`, `admin`, `member_client`, `other_client`, `admin_client` (signed in, CSRF header preset),
`login(user) -> TestClient`, `make_device(user, kind="extension") -> (device_id, {"Authorization": "Bearer rvd_..."})`,
`community_code` (fresh known code), `run_jobs()` (= `jobs.drain()`: runs every due server job in-thread).
`TestUser` has `.id .email .password .role .name`.

Use unique test file names (pytest has no `__init__.py` packages here, so two `test_x.py` files in different
folders collide).

### Dev server
```bash
. /tmp/claude-0/-home-user/4c2cbd5e-b8a1-556b-adde-3bbaa58a25e4/scratchpad/venv/bin/activate
python -m reelvault.server.dev --port 9200 --data-dir /path/in/your/scratch --seed          # web + worker thread
python -m reelvault.server.dev --port 9201 --data-dir /path/in/your/scratch --seed --split  # worker = 2nd process
```
Prints the admin login, the seeded member login (`member@example.com` / `member-pass-123`) and a fresh community
code. Data dir defaults to a new temp folder; it must be outside the repo (it holds a dev master key). Ctrl+C or
SIGTERM stops web and worker. Production entry: `reelvault-start` (RV_ROLE=supervise|web|worker|all, PORT, HOST).

## Import paths and signatures

`reelvault.server.db`
- `engine` (module attribute, always current; or `get_engine()`), `init(database_url=None) -> Engine`,
  `migrate(engine=None)` (Alembic head; create_all fallback when `migrations/` is not shipped)
- `new_id(prefix) -> str`, `utcnow() -> datetime` (aware UTC), `iso(dt) -> "…Z"`
- `tx(immediate=True)` context manager -> Connection (BEGIN IMMEDIATE on SQLite); `read()` = `tx(immediate=False)`
- all 34 Table objects of section 3: `users sessions invites allowlist reset_links app_settings profiles
  profile_sources topics formats ai_connections ai_usage devices pairing_codes accounts pulls reels reel_accounts
  reel_snapshots reel_signals analyses jobs job_turns member_budget outputs insight_snapshots oauth_clients
  oauth_codes oauth_tokens audit_log usage_counters discover_runs discover_candidates admin_access`; `TABLES`,
  `metadata`, `APP_SETTINGS_DEFAULTS`, `USER_SETTINGS_DEFAULT`, `DEVICE_STATE_DEFAULT`, `JOB_KINDS`,
  `ACTIVE_JOB_STATES`
- `scoped(conn, table, user_id) -> Select`, `get_owned(conn, table, user_id, row_id, *, id_col="id") -> Row|None`,
  `update_owned(...) -> rowcount`, `delete_owned(...) -> rowcount`
- `bump(table, key: dict, *, conn=None, _set=None, **deltas) -> dict` (atomic upsert; returns new counter values,
  e.g. `{"count": 3}`; `_set` = plain columns such as `user_id`)
- `upsert(conn, table, values, *, update=None, do_nothing=False)`
- `get_setting(key, *, conn=None)`, `set_setting(key, value, *, conn=None)`, `all_settings(*, conn=None)`
- `audit(action, *, user_id=None, actor="system", detail=None, ip=None, conn=None)`
- `check_local_fs(path)` (SystemExit with the 2.2 message), `fs_type(path)`
- CLI: `python -m reelvault.server.db migrate | copy-from-sqlite <path>`

`reelvault.server.security`
- `hash_password(p)`, `verify_password(h, p)`, `password_problem(p) -> str|None`
- `encrypt_secret(plain, aad) -> (ct, nonce, kid)`, `decrypt_secret(ct, nonce, kid, aad) -> str` (raises
  `KeyUnavailable`, `.message` = "Your saved key can't be read. Paste it again."), `connection_aad(uid, conn_id, provider)`
- `new_token(prefix) -> (token, sha256)` (`new_token("rvd")` -> `"rvd_…"`), `sha256_hex`, `consteq`,
  `new_code(groups=2, size=4)` (`7KQ4-M2XP`), `norm_code`, `code_hash` (use for pairing codes too)
- `redact(text, secrets) -> str`, `client_ip(request) -> str`
- `safe_fetch(url, *, max_bytes, timeout=30, headers=None, allow_http=True, allow_private=False, method="GET",
  transport=None) -> FetchResult(url, status, headers, content, redirects; .text)`; raises `FetchError` (plain message)
- sessions: `create_session(conn, user_id, request) -> (cookie_value, csrf)`, `set_session_cookie`,
  `clear_session_cookie`, `end_sessions(conn, uid, except_hash=None)`, `revoke_everything(conn, uid)`, `SESSION_COOKIE`
- deps: `current_user(request) -> Row`, `current_admin`, `current_device(request, kind) -> Row`,
  `extension_device` / `desktop_device` (use these in `Depends(...)`), `optional_user(request) -> Row|None`
  (no CSRF; for the OAuth consent page). After `current_user`, `request.state.session.csrf_token` is set.
- CLI: `python -m reelvault.server.security genkey [kid] | rotate`

`reelvault.ai.net` (WP1): `PinnedTransport(allow_private=False, resolver=None)`, `AsyncPinnedTransport`,
`resolve_checked(host, port, *, allow_private=False, resolver=None)`, `is_public_ip(ip)`, `UnsafeAddress`.

`reelvault.server.jobs`
- `enqueue(user_id, kind, payload=None, *, executor=None, job_class=None, priority=50, dedupe_key=None,
  parent_id=None, run_after=None, state=None, message=None, progress=None, max_attempts=3, conn=None) -> str`
- `@handler(kind, job_class)` -> `fn(job: Row, ctx: JobCtx) -> dict|None`; `@on_lease_expired(kind)` -> `fn(conn, job)`
- `JobCtx.progress(done, total, message)`, `.cancelled() -> bool`, `.renew()`
- `Retry(delay_s=60, message=None)`, `Pause(message, code="paused")`, `Fail(message, code="failed")`
- `lease_server(job_class, owner, *, lease_s=None) -> Row|None`
- `lease_external(user_id, executor, kinds, owner, lease_s, *, prepare=None) -> Row|None`
  (`prepare(conn, job)` runs in the same transaction; return `False` to undo; the returned Row is re-read after it)
- `complete(job_id, result=None, *, owner=None, conn=None) -> bool`,
  `fail(job_id, code, message, *, pause=False, run_after=None, owner=None, conn=None) -> bool`,
  `cancel(job_id, user_id=None)`, `retry(job_id, user_id=None)`, `get(job_id, user_id=None)`,
  `set_progress(job_id, done, total, message="", *, owner=None) -> state`, `renew(job_id, owner, lease_s)`,
  `requeue_expired(now=None) -> int`
- `to_api(row, *, include_result=False, with_eta=True) -> dict` (Job JSON of 4.1)
- `run_worker(stop=None, *, pools=None, poll_s=1.0, migrate=True)`, `run_once(job_class, owner)`,
  `drain(classes=POOLS, *, max_jobs=200)`, `load_handlers()`, `schedule_daily()`, `KINDS`, `HANDLERS`
- worker entry: `python -m reelvault.server.jobs`

`reelvault.server.budget`: `member_share(user_id, day=None) -> int`, `recompute_shares(day=None) -> dict`,
`eta(user_id, job=None) -> {"text","at"}|None`, `water_fill(demands, total)`, `sec_per_reel()`, `load()`.

`reelvault.server.storage`: `member_dir(uid)`, `member_path(uid, rel)` (raises `UnsafePath`), `files_url(uid, rel)`,
`data_dir() db_dir() users_dir() backups_dir() tmp_dir() models_dir()`, `ensure_dirs()`, `refresh_storage_bytes(uid)`,
`delete_member_files(uid)`, `disk_free_mb()`, `sweep_storage()`, `backup()`.

`reelvault.server.ratelimit`: `hit(scope, key, limit, window, *, message=None, n=1, code="too_many") -> int`,
`check(...)`, `count(scope, key, window)`; windows `"minute"` (memory), `"quarter"` (15 min), `"hour"`, `"day"`;
raises `TooMany` (an `ApiError` 429 with `Retry-After`).

`reelvault.server.errors`: `ApiError(status, code, message=None, detail=None, headers=None)` -> rendered as
`{"error", "code", "detail"?}`; helpers `not_found()`, `bad_input(msg)`, `forbidden(msg)`, `too_many(msg, retry_after)`.

`reelvault.server.settings`: `get_settings() -> Settings` (frozen dataclass of every 12.5 env var + `public_host`,
`public_origin`, `cookie_secure`, `is_sqlite`, `models_dir` from `RV_MODELS_DIR`), `reload()`, `VERSION`, `EXTENSION_MIN`.

`reelvault.server.app`: `create_app(role=None, *, prepare=True)`, `ROUTERS` (exact section 13 list),
`mount_last(app, asgi_app, paths)`, `iter_routes(app)`, `prepare_runtime()`, `setup_logging()`.
`app.state.lifespan_hooks` is a list of `() -> async context manager` entered at startup.

`reelvault.server.api_admin` hooks: `register_support_mirror(name, fn(user_id, query) -> dict)`,
`register_community_formats(fn() -> dict)`, plus `find_invite(conn, code)`, `create_community_code(conn, ...)`,
`disable_user(conn, uid, reason, *, actor)`, `approve_user(conn, uid, *, actor)`, `expire_members()`,
`rotate_code_if_due()`, `bootstrap()`.
`reelvault.server.api_auth`: `me_json(user, csrf)`, `setup_state(conn, user)`, `delete_user(uid, *, actor, ip)`.

## Rules other packages must follow (learned while building)

1. **Never open a second `db.tx()` while holding a write `db.tx()` in the same thread.** On SQLite the inner one
   waits 5 s and fails with "database is locked". Every WP1 helper that writes takes `conn=`; pass it.
   (`ratelimit.hit` opens its own transaction: call it before or after your write transaction.)
2. Member-owned reads go through `db.scoped` / `db.get_owned`; another member's id is a 404 (the isolation test
   sweeps every `/api/...{param}` route automatically; add your route to `PARAM_MAP` in
   `tests/server/test_isolation.py` if its prefix is new, and check ownership BEFORE validating the body).
3. Raise `ApiError` (plain words) from routes; never return a stack trace.
4. WP6: mount the SDK app with `mount_last(app, mcp_app, MCP_PATHS)` and append
   `lambda: mcp.session_manager.run()` to `app.state.lifespan_hooks` inside `mount(app)`; build a NEW `MCPServer`
   per `mount()` call (tests create many apps). Checked here with the real `mcp` 2.3.0 SDK: 401 + `WWW-Authenticate`,
   PRM 200, `initialize` 200, unknown paths stay JSON 404. Note: the SDK's PRM lists `authorization_servers` as
   `"http://HOST/"` (trailing slash).
5. WP2: `/download/reelvault-members-extension.zip` calls `api_ext.extension_zip() -> bytes` if you define it.
   `/pair` is yours. Pairing codes: use `security.new_code()` + `security.code_hash()`.
6. WP4/WP7: `GET /api/home` merges `api_reels.home_parts(user_id) -> dict` (stub exists, returns `{}`).
7. `GET /api/jobs`, `GET /api/jobs/<id>`, cancel and retry are in `api_files.py` (WP1). Don't add them again.

## Deviations from DESIGN-SAAS.md (please fold into the design on the next pass)

- `jobs.enqueue(executor=None)`: None means the kind's default executor (extension for `ig_*`/`page_read`,
  server otherwise). The design shows `executor="server"`. Added kwargs: `message`, `progress`, `max_attempts`, `conn`.
- `jobs.user_id` is nullable, only for system jobs (`sweep_storage`, `backup`, `expire_members`, `rotate_code`).
  The lease query uses a LEFT JOIN on `job_turns` with `NULLS FIRST`, so system jobs and brand-new members go first.
- `job_turns.last_served_at` starts at 2000-01-01 for new rows (new members are served first).
- `reels.account_id` has no FK (DELETE /api/accounts may keep reels, 4.8); every other member table has
  `user_id` FK ON DELETE CASCADE. `audit_log.user_id` is ON DELETE SET NULL (audit outlives deletes).
- `analyses.id` uses prefix `ana`; DCR client ids may use `dcr` (both added to `db.ID_PREFIXES`).
- `discover_runs.mode` CHECK = `niche|similar|search`; `discover_runs.state` is free text (design gives no list).
- `admin_access` PK = (`user_id`, `granted_at`) (design gives no key).
- `jobs.dedupe_key` is a plain unique index on both SQLite and Postgres (NULL-on-finish rule); no partial index.
- Internal state lives in `app_settings` under keys starting with `internal.` (hidden from `GET /api/admin/settings`):
  `internal.worker_heartbeat`, `internal.code_banner` (new community code shown to the admin until
  `POST /api/admin/invites/community/ack`, a NEW endpoint), `internal.last_backup`, `internal.sync.<preview_id>`
  (Skool sync previews, 2 h), `internal.signup_paused_until`. WP2 can store debug samples the same way
  (`internal.debug_sample.<device>.<endpoint>`) or ask WP1 for a table.
- Extra fields: `Me.reconfirm_needed` (bool), `Me.admin = {"pending", "code_banner"}` for admins,
  `Me.limits` = `{max_accounts, pulls_per_day, outputs_per_day, media_per_day, storage_mb}`;
  `GET /api/admin/invites` adds `signup_url` and `new_code`; `/healthz` adds `worker: {last_seen_at, ok}`;
  `GET /api/admin/stats` adds `last_backup`, `worker`; `GET /api/admin/users` accepts `?q=`.
- Reconfirm gate: 403 with code `reconfirm_needed` on every session route except `/api/me`, `/api/auth/reconfirm`,
  `/api/auth/logout`; admins are never gated.
- CSRF Origin/Referer check: a request with neither header passes on the CSRF token alone (non-browser clients).
- Login lockout counts failures only (5 per email / 20 per IP per 15 min); window keys are UTC quarter-hours.
- The bundled common-password list is small (~140 entries, 10+ chars) — replace with the full 10,000 list.
- Weekly files backup is `.tar.zst` when `zstd` + `tar` exist, else `.tar.gz`.
- `RV_MODELS_DIR` env (default `/opt/reelvault/models`) is new, for tests and dev.
- `alembic.ini` sits in the project root (developer use only; the app runs `db.migrate()` itself).
- `pyproject.toml`: packages list now includes `reelvault.server`, `reelvault.ai`, `reelvault.signals`,
  `reelvault.importers`; package data adds `data/*`, `web/**`, `ai/prompts/*`, `server/data/*`; new script
  `reelvault-start = reelvault.server.supervise:main`.

## Second pass (done)

### New and changed interfaces
- `reelvault.server.ratelimit` (11.1, 11.6): limits are keyed by the CLIENT, not only the raw IP.
  - `client_key(request)` -> `"ip:203.0.113.7"` or `"ip6:2001:db8:1:2::/64"` (one IPv6 /64 = one client; IPv4-mapped
    IPv6 unwrapped; X-Forwarded-For only from TRUSTED_PROXIES). **Use it instead of `security.client_ip()` as the key
    of every per-IP limit** (WP2 pairing claim 20/IP/h, WP6 OAuth register/token). Keep `client_ip()` for audit rows.
  - `key(*parts)` composite keys (hashed when > 120 chars), e.g. WP6 `key(ip, redirect_uri, client_name)`.
  - `ip_limit(limit, ip, factor=10)` -> x10 for Anthropic's cloud range 160.79.104.0/21 (11.6 OAuth register).
  - `hit_all(request, [(scope, key_or_None, limit), ...], window)` / `check_all(...)`: None = the client key; all
    counters are counted, then the first one over its limit raises `TooMany`.
  - Per-device limit: `security.current_device` now counts 600 calls/min per device token
    (`security.DEVICE_PER_MINUTE`), keyed by device id. The 300/min per-member limit is unchanged.
- `reelvault.server.mailer` (NEW file, WP1): `configured()`, `send(to, subject, text) -> bool` (background thread,
  never raises), `pending_signup(user)`, `youre_in(user)`, `reset_link(user, url)`. SMTP_URL (`smtp://` STARTTLS or
  `smtps://`, URL-encoded user/password, optional `?from=`) or RESEND_API_KEY; new env `MAIL_FROM`.
  Last failure in `app_settings["internal.mail_last_error"]` (secrets redacted). Tests: `mailer.BACKGROUND = False`
  and monkeypatch `mailer._deliver`.
- Emails now sent: one per new pending signup to every active admin (when `notify_admin_email`), "You're in" on
  approve (also when an allowlist add or the Zapier hook approves), and self-service reset links from
  `POST /api/auth/reset-request` (`reset_links.created_by = "self"`, 24 h; 3 per address/h, 300/h server-wide;
  the answer is always `{"ok": true}`).
- NEW endpoints (WP7 UI please use):
  - `POST /api/admin/users/<id>/reject` -> `{"ok": true}`: deletes a PENDING signup (400 for anyone else).
  - `GET|POST|DELETE /api/me/support-access` (Settings > Account, 11.5). POST `{"days": 1..7}` (default 7).
    All return `{"active", "granted_at", "expires_at", "admin_views": [iso, ...≤10], "last_viewed_at"}`.
    `last_viewed_at` drives "The admin looked at your account on <date>". Also mirrored in
    `users.settings.support_access_until`. Admin user list rows gain `"support_access": bool`.
- `GET /api/admin/community-formats` now enforces the 11.5 floor itself: rows with `members_using < 5` are dropped
  and only `vf, label, members_using, lift_median` are returned, whatever the registered provider returns.
- `PATCH /api/admin/settings`: integer settings have upper bounds (typo guard), e.g. `max_pulls_per_day <= 100`.
- `budget.recompute_shares()` no longer counts running jobs twice (demand = queued + used today);
  `budget.has_queued_media()`; the worker recomputes shares every 60 s while media work waits (else every 10 min).
- `storage`: daily DB backups are single files (`journal_mode=DELETE`, no -wal/-shm). New `restore_db(path)`
  (integrity check, keeps the current DB as `backups/before-restore-<time>.sqlite`), `restore_files(tar)`,
  `list_backups()`, CLI `python -m reelvault.server.storage backup | list | restore <file> | restore-files <tar> | sweep`.
  Restore only while web and worker are stopped (`deploy/backup.sh restore` does that).
- `db.migrate()` logs a warning when the migrations folder is missing. The Docker image sets
  `RV_MIGRATIONS_DIR=/opt/reelvault/migrations` (an installed wheel has no `migrations/`).
- `supervise`: `reelvault-start health` (Docker HEALTHCHECK; web: `/healthz` ok + fresh worker heartbeat; worker role:
  heartbeat < 3 min). Restart back-off resets after a process ran > 60 s.
- `api_files`: the extension download uses `api_ext.extension_zip()` (WP2 now provides it, and it uses
  `deploy/build_extension.py`); `api_files.load_extension_builder()` / `default_extension_zip()` are the fallback.
- `testing.py`: `RV_TEST_DATABASE_URL=postgresql+psycopg://...` runs the suite on Postgres (fresh schema per test).
- Bundled common-password list is now the full top 10,000 (zxcvbn 4.5.0 data, MIT; see
  `server/data/common_passwords.LICENSE`) plus the earlier 10+ character list.

### deploy/build_extension.py contract (WP1 + WP2)
- `build_zip(servers: list[str], *, build="zip"|"store", src=None) -> bytes`; `version_of(src)`; `BuildError`.
- Source: `--src`, else `$RV_EXTENSION_DIR`, else `<project>/extension-members`, else `/opt/reelvault/extension-members`.
- Writes `config.js` lines `export const SERVERS = [...]; export const BUILD = "...";`, keeping every other line of
  WP2's template. Servers must be bare origins; `http://` only for localhost/127.0.0.1 and never in a store build.
- Refuses manifests with `cookies`, `tabs`, `webRequest*`, `history`, `debugger`, `<all_urls>`/wildcard hosts, or
  non-MV3; refuses a folder without `sw.js`. Skips dotfiles, `tests/`, `node_modules/`, `*.map`, `*.zip`.
  Reproducible bytes (fixed times, sorted names).
- CLI: `python deploy/build_extension.py --server https://app.example.com [--server ...] [--build store|zip]`
  -> `dist/reelvault-members-extension-<version>-<build>.zip`.

### Deploy kit (deploy/)
- `Dockerfile` (multi-stage deps -> models -> runtime; python:3.13-slim; hashed lock; non-root uid 10001; tini;
  `VOLUME /data`; `HEALTHCHECK reelvault-start health`), `Dockerfile.dockerignore` (allowlist; `.env` never sent).
- Model layout baked by `fetch_models.sh` (sha256-pinned CED-tiny files and YuNet; whisper small/base pinned to HF
  commits, `refs/main` written so offline loading by name works): `$RV_MODELS_DIR/ced-tiny/{model.int8.onnx,
  class_labels_indices.csv}`, `$RV_MODELS_DIR/whisper` = `HF_HOME` (`HF_HUB_OFFLINE=1`; load with
  `WhisperModel("small")`), `$RV_MODELS_DIR/yunet/face_detection_yunet_2023mar.onnx`. **WP3: read models from
  `storage.models_dir()` with exactly these paths.** A filled copy for local tests:
  `/tmp/claude-0/-home-user/4c2cbd5e-b8a1-556b-adde-3bbaa58a25e4/scratchpad/saas/WP1-rest/models` (set `RV_MODELS_DIR`).
- `docker-compose.yml` (app = web+worker via reelvault-start, port 127.0.0.1:8000; caddy in profile `https`;
  postgres in profile `postgres`; fixed subnet 172.29.57.0/24 inside TRUSTED_PROXIES), `docker-compose.split.yml`
  (web and worker as two services), `Caddyfile` (env-driven `{$RV_DOMAIN}`, 100 MB bodies, no compression and no
  buffering on `/mcp`), `.env.example`, `setup.sh` (install | update | status | logs | --local | --yes | --no-build),
  `backup.sh` (backup | list | copy-out | restore | restore-files), `requirements.lock` (uv, hashed, linux py3.13),
  `DEPLOY.md`, `hosts/vps.md`, `hosts/paas-with-volume.md`.
- `reelvault/server/bench.py`: `python -m reelvault.server.bench [--video reel.mp4] [--quick] [--no-whisper] [--json]`.
  Uses WP3's `signals.process(video, out_dir, *, ig_audio, words, whisper_model)` once it is real; until then it
  times the parts (probe, scene cuts, frames, audio, whisper). Suggestions from the synthetic clip are capped at the
  design default (cpus x 1,200), because it has no speech.
- CI: `/home/user/linkedin/.github/workflows/reelvault-members.yml` (server tests + desktop tests on ubuntu;
  optional Postgres job; Docker build, Caddyfile validate, compose config, `setup.sh --local` boot, /healthz,
  sign-in, worker heartbeat, backup/restore, worker restart).

### Deviations (second pass)
- The design names `Caddyfile.example` and says setup.sh "writes the Caddyfile". The kit ships one generic
  `deploy/Caddyfile` that reads the domain from `RV_DOMAIN` in `.env`; setup.sh writes only `.env`. Nothing generated
  has to be kept out of git except `.env` (already ignored).
- The `.env` lives at `deploy/.env` (next to the compose file), mode 600. `COMPOSE_PROFILES` / `COMPOSE_FILE` in it
  choose Caddy, Postgres and the split layout.
- `BACKUP_COPY_CMD` runs inside the container, which has no rclone; DEPLOY.md documents a host cron line with
  `backup.sh copy-out` + rclone instead.
- Email codes at login (11.1 "optional email codes") are not built: there is no API shape for them in section 4 yet.

### Not built / not tested here
- The Docker image was not built on this machine (no Docker daemon). Its steps were emulated: a fresh venv from
  `requirements.lock` + `pip install --no-deps .`, the /opt layout, `reelvault-start` supervise mode on port 9121,
  migrations from `RV_MIGRATIONS_DIR`, sign-in, worker heartbeat, a killed worker restarted, backup + restore.
  The real build, `caddy validate` and the compose boot run in CI.
- Postgres: the suite has a Postgres switch and a CI job (continue-on-error), never run here (no Postgres server).
- GitHub raw downloads are blocked from this box, so fetch_models.sh fell back to the Hugging Face mirror for YuNet
  (same sha256 pin). Both URLs are in the script.

## Fix round 1 (2026-11-10)
- Login, password change, reset and the wrong-code throttles count the attempt atomically BEFORE the slow check
  (`ratelimit.hit`, the `db.bump` upsert) and give it back on success (`ratelimit.refund`), so only failures use up
  the 5/email and 20/IP per 15 min. Concurrent guesses can no longer all pass a read-only `check()`
  (30 parallel guesses: 5 x 401, 25 x 429). `reset_password` hashes the new password only for a valid link.
- Body cap: `EdgeMiddleware` counts streamed bytes in its receive wrapper and raises a 413 HTTPException when the
  running total passes the cap (a chunked body has no Content-Length). Content-Length stays as a fast path. The
  `_json` helpers read through `errors.read_body(request, limit)`, which also counts while streaming.
- `prepare_runtime` turns a PermissionError on `/data` into one plain line. New `deploy/entrypoint.sh` (under tini):
  as root it gives `/data` to uid 10001 (once) and drops to it with setpriv; as uid 10001 it just runs the command.
  The Dockerfile has `ARG RV_RUN_USER=reelvault` (default stays non-root); the PaaS guide sets root per platform
  (Fly `[build.args]`, Railway `RAILWAY_RUN_UID=0`, Render `RV_RUN_USER=root`). Not run in Docker here (no daemon);
  the entrypoint is tested natively as root (`test_deploy_kit.py`).
- `setup.sh`: plain-words stop when `compose build`/`up` fails; a re-run with an existing `deploy/.env` prints the
  sign-in address, email and where the first password is; `setup.sh domain` (or `RV_DOMAIN=... setup.sh` after
  `--local`) switches PUBLIC_URL, RV_DOMAIN and COMPOSE_PROFILES=https. Checked with a fake docker/curl on PATH.
- `backup.sh`: `restore`/`restore-files` exit 1 when the restore fails (after restarting the app); new
  `copy-in FILE`. CI round trip now changes data after the backup (a second login), restores, checks the change is
  gone, and checks that restoring a missing name fails. DEPLOY.md has "Move to a new server, or recover a dead one",
  exact master-key steps (`setup.sh update`, not `restart`), and the domain switch. `security rotate` refuses when
  only one key is loaded.
- `.env.example` no longer suggests an in-container rclone `BACKUP_COPY_CMD`; a failed copy logs at error level and
  Admin > Health shows "The off-site copy failed". `/api/admin/community-formats` answers 200 `{"enabled": false}`
  when community stats are off (the Health page logged a 404 each time).
- Windows CI runs only the desktop tests (same --ignore list as the Linux desktop step), and `tests/conftest.py`
  skips the server-side folders when fastapi/sqlalchemy are missing (desktop venv: 51 passed, no collection error).

# FETCH-SERVER notes: the server side of the own fetch engine

Contract: `DESIGN-FETCH.md` sections 2, 4, 6 and 7. This package owns `reelvault/server/{fetch,jobs_ig,api_ext,db}.py`,
`reelvault/igparse.py`, `migrations/versions/0002_fetch.py` and `tests/server/*`. Nothing here ran against real
Instagram: every logged-out answer comes from the fixtures in `tests/fixtures/ig/logged_out/`.

## What is here

| file | what |
|---|---|
| `server/fetch.py` (new) | policy and texts: read modes, plan, cooldown table 2.3, health counters, global switch-off, probe, status, pull messages, admin card |
| `server/jobs_ig.py` | `read` + `plan` at lease time, `ingest_lo()` (the one entry point for logged-out data, also for layer 3), stage end (`stop` / `resume`), merge rules, `lo_blocked` fail, probe rows, `fetch_log` on items / done / fail |
| `server/api_ext.py` | 13 new routes (section 6) and `hello.fetch` + `hello.lo` |
| `igparse.py` | `normalize_lo()` (shapes `v1` and `gql`), `pk_time()`, `trim_gql()` (Python twin of `trim.js`), `ReelIn.layer/strategy/partial/missing/taken_at_derived` |
| `server/db.py` | additions only: 3 columns on `pulls`, 3 on `reels`, tables `fetch_health` and `fetch_probes`, job kind `ig_probe`, id prefix `prb` |
| `migrations/versions/0002_fetch.py` | the migration (safe for databases with data, see note 1) |
| `tests/server/test_fetch_{igparse,pull,probe,migration}.py`, `_fetch_support.py` | 86 tests (plus `tests/ext/ext_e2e.mjs` with the real server) |

## Notes where the build differs from the contract (smallest sensible option)

1. **One CHECK change.** 4.3 says "no CHECK changes", but `jobs.kind` has a CHECK constraint and a new kind
   `ig_probe` must pass it. The migration rewrites only that CHECK. On SQLite Alembic's batch mode copies the `jobs`
   table (no table has a foreign key to `jobs`, so nothing cascades). `reels` and `pulls` get plain `ADD COLUMN`: a
   batch copy of `reels` would drop it, and SQLite would cascade-delete its child rows.
   `test_fetch_migration.py` seeds a 0001 database with rows, upgrades, checks every row and rule, and downgrades.
2. **App settings use the `internal.` prefix**: `internal.fetch.default_read_mode`, `.strategies`, `.strategy_off`,
   `.doc_ids`, `.probe_handle`, `.layers`. Keys with that prefix stay out of the general admin settings list and its
   PUT. Only the Fetch health endpoints change them, with their own checks.
3. **`USER_SETTINGS_DEFAULT["read_mode"]` is `None`, not `"auto"`.** None means "the member never chose", so the
   admin's default read mode applies to them. A member's own choice always wins.
4. **The job kind registry.** `jobs.py` is not ours, so `fetch.py` registers `ig_probe` in `jobs.KINDS` at import
   (`("external", "extension")`). `db.JOB_KINDS` lists it too.
5. **A queued pull follows the member's latest read mode.** `_prepare_lease` uses the read mode at lease time and
   writes it to `pulls.read_mode`. A pull queued before a change follows the new choice.
6. **Who gets a pull at lease time.** A pull is leased when the login may be used (today's rule: confirmed, no
   cooldown, budget left), or when this device can read logged out AND the member does not read `logged_in_only`,
   or when the member reads `logged_out_only`. A stale "signed out" report does not hold the login back: the
   sign-in check at the start of stage 2 settles it, as today. A pull that no layer can read goes back to waiting
   with a plain message for one minute (no attempt spent). An extension 2.0.0 keeps exactly today's rules and gets
   no `read` or `plan` in its payload. (A first draft held logged-in work for 30 minutes after a "signed out"
   report and leased `logged_in_only` pulls to unconfirmed devices; `ext_e2e.mjs` caught both, both are gone.)
7. **Top-up past the target.** When the target is reached but reels of this pull still lack view counts, the
   logged-in top-up reads up to 4 more pages (`TOP_UP_PAGES`) to fill them. It never stores more than the target.
8. **Partial reels get no snapshot.** A reel without real views writes no `reel_snapshots` row. The logged-in top-up
   of the same pull writes it (same `seen_at`). So view growth never starts from a fake or missing number.
9. **`stop_reason` of a logged-out pull** (no new values, 4.3): `target` -> `target_reached`, `no_more` ->
   `no_more`, `refresh_page` -> `known_reel` when a known reel was hit, else `page_cap`; `budget`, `blocked`,
   `no_views`, `no_strategy` -> `page_cap`. A `lo_blocked` fail in `logged_out_only` mode closes as
   `rate_limited` / `challenge` / `page_cap`. The fine reason stays in `pulls.layers.logged_out.stop`.
10. **`no_views` cooldown** is one device flag (`detail_off_until`): pulls drop all detail strategies for 24 h.
    A probe ignores it and clears it when a detail row comes back with views.
11. **Budgets.** Mode `next` gets the first-pull budget (40); `logged_out_only` gets 80. The server counts
    logged-out calls itself (`usage_counters` key `lo_calls`) and takes the larger of its count and the extension's.
    After 80 logged-out uploads in one lease the server ends the stage itself (`LO_UPLOAD_CAP`).
12. **The probe never uses the login in `logged_out_only` mode**: its `want` list then has no `li_*` rows, and the
    progress total follows `want`.
13. **A probe that stopped after its logged-out rows** (for example the login part failed) still ends as `done`
    with a verdict. One that stopped earlier ends as `failed` with a plain message.
14. `fetch_health.user_id` is nullable: server-side fetchers (layer 3) write rows with `device_id = "server"`.

## Layer 3 seam (section 7)
`jobs_ig.ingest_lo(user_id, job, body, device=None)` takes a body with `layer: "rented"` when `device` is None.
Health rows then use `device_id = "server"` and no device state changes. `pulls.layer` / `reels.read_layer` are plain
text. `fetch.layer_order()` reads `internal.fetch.layers`. No fetcher, executor value or proxy code exists.
`test_server_fetcher_seam_uses_the_same_ingest` covers the seam.

## Unverified live
Everything that depends on Instagram itself: DESIGN-FETCH.md section 10 (all 10 assumptions). The server only
trusts the shapes of the fixtures. The cooldown hours, the 60/200 logged-out budget and the global switch-off
numbers are caution, not measured limits. The owner's "Test my connection" run on his PC settles which strategies
work; the admin Fetch health card then shows the same numbers per device.

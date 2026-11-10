# WP4 Scoring, sampling, insights, sub-formats, discovery: notes

Status: built and tested. The earlier, interrupted attempt had left only WP1's stubs on disk. Nothing from it was
kept, and nothing is committed.

## Files

| file | what |
|---|---|
| `reelvault/outliers.py` | Pure functions: maturity curve, `score_account`, bands and badges, the why line, `best_for_deep`, `strata_of`, `pick_sample`. Also discovery ranking (`candidate_rank`, `candidate_why`). |
| `reelvault/insights.py` | Pure functions: `Unit`, weighted shrunk lift, within-creator typical, evidence, grid, recency, combos, signatures, you vs them, cards and FormatCards, `sub_formats` grouping, `can_make`, `music_rows`, labels, `fallback_name`, `sample_weights`. |
| `reelvault/server/jobs_score.py` | Handlers `score_account`, `pick_sample`, `refresh_insights`. Also the DB side: `effective_tags`, `build_units`, `refresh`, `report_numbers`, `queue_insights`, `maybe_rescore`, `maybe_refresh`, and the discovery hooks for WP2. |
| `reelvault/server/api_accounts.py` | `/api/accounts*`, `/api/discover*` (4.8) |
| `reelvault/server/api_reels.py` | `/api/reels*`, `/api/insights*`, `/api/formats*`, `/api/banks/*` (4.9). Also `home_parts()` for `/api/home`, the admin support views and the community stats provider. |
| `tests/server/test_outliers.py` | 48 tests |
| `tests/server/test_sample.py` | 11 tests |
| `tests/server/test_insights.py` | 19 tests, including the 6 x 100 simulation |
| `tests/server/test_accounts_reels.py` | 17 API tests. They use WP2's fixtures and its real extension API for pulls. |

## Interfaces for other packages

```python
from reelvault import outliers as ol, insights as ins
from reelvault.server import jobs_score as js, api_reels, api_accounts

# WP5 (pipeline / work)
js.queue_insights(user_id)          # call after save_quick_tags / save_deep / retag. Debounced: one queued job,
                                    #   run 60 s later (js.INSIGHTS_DEBOUNCE_S; tests set 0)
js.effective_tags(conn, user_id, reel_ids, data=True) -> {reel_id: {...}}   # override ?? deep ?? quick
js.best_for_deep(user_id, account_id, n=5) -> [reel_id]
js.report_numbers(user_id, account_ids | None) -> Insights JSON (6.7.5)   # competitor_report; stores nothing
ins.can_make(vf, digest["capabilities"]) -> {"ok", "reason"}
ins.fallback_name(vf, recipe) -> str                          # name until name_formats runs (<= 70 chars)
# WP6 (MCP read tools)
api_reels.list_reels(user_row, params: dict) -> GET /api/reels JSON     # list_outliers
api_reels.insights_summary(user_row) -> GET /api/insights/summary JSON  # what_is_working
# WP2 hooks (WP2 already calls these by name)
js.pick_check_candidates(user_id, run_id) -> [{"cand_id", "handle", "pk"?}]    # <= 15, interleaved by seed
js.ingest_candidate(user_id, cand_id, account: dict, items: list[dict])          # 6.6.1 rank
js.after_check(user_id, run_id)
js.start_discover(user_id, mode, *, query=None, seeds=None) -> run_id
# WP1
api_reels.home_parts(user_id) -> {"new_outliers", "first_look", "cards", "formats", "pipeline", "saves_line"}
```

What WP4 calls in other packages. Each call works with the current stubs, so nothing breaks before those packages
land:
- `pipeline.advance(user_id, reel_id)` is called for every priority and sample reel that is not done yet, in
  stage 3 order: priority by score, then sample reels round-robin over the strata. It is also called when a member
  stars a reel. A `NotImplementedError` is ignored.
- `work.queue_deep(user_id, ids)` is called for the top 5 of each competitor account (stage 4), and by
  `POST /api/reels/<id>/analyze` and `/api/reels/analyze-best`. Until WP5 lands, it only sets
  `deep_state='queued'`.
- `work.queue_format_names(user_id, ids <= 8)` is called for new sub-formats, unnamed ones, and ones that grew to
  2x their size since they were named. It is skipped while a `name_formats` job is active. Until WP5 lands,
  FormatCards use `fallback_name`.
- `jobs_ig.start_pull` (with `run_after` for later days), `jobs_ig.request_profile` (an add with `pull=false`) and
  `jobs_ig.request_discover`.

### Rules WP5 must follow
- Member corrections ("Fix tags", `PATCH /api/reels/<id>`) live in the **quick** analyses row's `overrides`.
  - If the reel has no quick row yet, the PATCH creates one with `data={}` and `made_by={"via": "member"}`.
  - **`save_quick_tags` must keep `overrides`** when it rewrites that row.
  - Effective value = override ?? deep column ?? quick column.
  - A visual override makes the reel's quality `member`.
- WP4 writes `analyses.format_id` on every analyses row of a grouped reel. WP5 must not overwrite it with anything
  else.
- `name_formats` writes `formats.name`, `guide` and `named_at`. WP4 never touches those three columns.
- After saving tags, call `jobs_score.queue_insights(user_id)`. As a safety net, opening Home or insights also
  queues a refresh when any analysis or score is newer than the latest snapshot (`maybe_refresh`).

## What works (evidence)
- `pytest tests/server/test_{outliers,sample,insights,accounts_reels}.py`: 95 passed in about 9 s.
- Full server suite: `tests/server`, 334 passed and 7 skipped (other packages' model and e2e skips).
- Desktop suite: `tests/test_*.py`, 51 passed.
- Whole tree: `cd /home/user/linkedin && pytest reelvault/tests` with `RV_SKIP_EXT_E2E=1` gives 386 passed and
  8 skipped.
- Simulation (12.1): 6 creators x 100 reels with a planted format worth 2.7x normal. Only the priority set and the
  25-reel stratified sample are tagged (about 210 of 600 reels). Results over seeds 1-5 and 11:
  - lift 3.8-5.7 (> 2)
  - typical 2.24-2.7 (> 2)
  - evidence strong
- First look from a real fixture pull (WP2's extension API, `account_timeline`): 100 reels stored, scored and tiered
  with zero media. 5 or more of the 6 planted outliers are found.
  - Home: "Your first look: N outliers from @scaleit.demo".
  - Tiers: 25 sample reels; stored weights sum to the eligible count.
  - Insights are refreshed through the queue.
- Daily cap: adding 7 accounts gives 5 pulls today and 2 with `run_after` tomorrow, status `scheduled`, and
  "We'll read 5 accounts today and 2 tomorrow to keep your Instagram safe." A pull past the cap gets "You've done
  5 pulls today. This one starts tomorrow."
- Discovery end to end through the extension API:
  - The niche queries come from `search_terms` and `niche_label`.
  - My `pick_check_candidates` chooses the light checks.
  - `ingest_candidate` ranks them: "Median 85,441 views on the last 12 reels · posts 4x a week · 120K followers".
    An account that stopped posting goes to the bottom with "Not posting lately".
  - `track` creates the accounts with the checked `ig_pk` and queues their pulls.
- Isolation sweep (`test_isolation.py`): every WP4 route with an id gives 404 for another member's ids, before the
  body is read.
- Dev server smoke test on port 9181 (stopped afterwards): every GET route answers 200; discovery and pull errors use
  the plain copy.

## Deviations from DESIGN-SAAS.md (please fold in)
1. **Strata (6.0).** The design says "next pulls add new strata" but gives no rule.
   - Rule used: the number of strata is 5 x round(account reels / 100), so each 100-reel pull adds 5 strata. Rounding
     means a refresh of a few reels adds none.
   - Strata are equal `taken_at` quantile slices of the eligible non-priority reels.
   - Picks are never dropped. A stratum only gets new picks up to 5. So 2 pulls give about 50 sample reels per
     account.
   - The seed is `account_id:<latest pull id>`.
   - Strata are not stored. They are recomputed the same way at insight time.
2. **Insight weights (6.7).** A stratum whose sample reels are not tagged yet hands its reels to the tagged strata of
   the same account, pro rata (`insights.sample_weights`). So the weighted sample still stands for every normal reel
   while tagging is under way.
3. **Peers on the likes basis.** For a reel scored on likes, peers are the eligible reels whose likes are known,
   including reels that have views. A reel's percentile is measured against the account's owned scored reels.
4. **Score ownership for collabs.** A reel's score columns are written by its primary account (`reels.account_id`,
   which is the owner's account when that account is tracked). Eligible peers exclude every `is_collab` reel and
   every coauthor row.
5. **Nightly re-score.** There is no WP4 daily job hook. Instead, `GET /api/accounts` and `/api/home` queue
   `score_account` (priority 90, deduped) for accounts whose scores are older than 20 h.
6. **Hit rate.** `LiftRow.hits` is the unweighted outlier count; `hit_rate` is the weighted rate (hits_w / W_t),
   which estimates the rate over all of the creator's reels. Lift and its base rate H0 are computed per dimension,
   over the units that have a value in that dimension, so visual dimensions use their own (vision-only) base.
7. **Windows.** `GET /api/insights?window=` takes `all` or `recent`. Without it you get the default 365-day window.
   The snapshot stores all three: the default in `data.rows`, the others in `data.windows.{all,recent}`.
8. **Sub-formats.** Grouping is done on all-time competitor units, so groups stay stable; lift is computed in the
   window.
   - Every unit is in at most one group.
   - Small groups merge upward and keep only their leftover units.
   - Format rows that disappear are kept with `stats.active=false`, so their names survive a comeback. APIs show
     only active ones.
   - New groups get a placeholder row first, so their ids are stable before the stats are written.
9. **Labels.** The short labels of 6.7.5 ("Green screen over image", "Screen recording") are used. Longer
   `taxonomy.json` labels are used only for ids the built-in table doesn't know. `needs` always comes from
   `taxonomy.json`.
10. **`first_look`** in `/api/home` is a list: one entry per recent finished pull with new reels (up to 3), each
    `{"account", "pull_id", "mode", "finished_at", "outliers", "line", "tags_coming", "reels"}`.
11. **Extra response fields.**
    - Every list: `saves_line`.
    - ReelSummary: `band_label`, `badge_labels`, `tags.sound_label`, `tags.unverified` (measured tags).
    - AccountSummary: `role`, `url`, `note`, `last_refresh_at`, `pull_job` and `scheduled_for`.
    - The reel page: `effective`, `coauthors`, `baseline`.
    - Lists: `pipeline.line`, for example "175 reels left to tag · ready in about 3 hours", or "Connect your AI to
      tag 175 reels."
    - `GET /api/discover/<id>`: `state_text`, `line`, and per candidate `not_posting`, `checked`, `url`.
    - `DELETE /api/accounts/<id>`: `reels_deleted`.
    - `analyze-best`: `reel_ids`.
12. **Account cap** answers 429 `limit_reached`: "You can track up to 10 competitors. You have room for N more."
    A second "your account" with a different handle is a 409. Discovery is limited to 10 searches a day per member.
13. **`DELETE /api/accounts/<id>`.** `delete_reels` defaults to true.
    - Collab reels that another tracked account also has are kept and moved to that account.
    - Reel folders on disk are deleted.
    - Active jobs for the account (`ig_pull`, `score_account`, `pick_sample`, `ig_profile`) are canceled.
    - With `delete_reels:false` the reels stay, with `account.handle = null`, and are no longer in insights.
14. **Purposes on `/api/reels`.**
    - `remake`: band >= outlier.
    - `format`: has a sub-format or a visual format.
    - `hook`: has a hook type or O >= 2.
    - `learn`: the deep read is done.
15. **Hook bank.** It is read from analyses at request time; there is no table.
    - `quick` rows: the transcript's first sentence or `hook_line`, plus `visual_subject`.
    - `deep` rows: `skeleton.hook` and `hook_formula`.
    - The CTA bank adds keywords found in captions ("comment GUIDE").
16. **Process everything.** `users.settings.process_all` puts every eligible reel in the sample with weight 1. It
    applies only to API-mode members or members with a tagging key.
17. **Smaller sample.** Claude-connector and desktop members without a tagging key get a sample of 15 per account,
    as 6.0 says.
18. **Who gets deep reads.** The automatic stage-4 deep reads are queued for competitor accounts only, not for "your
    account".
19. **Pull days.** Pull days use UTC days, not the member's timezone. A scheduled pull starts at 00:05 UTC.
20. **Admin hooks.** `register_support_mirror("reels")` and `("insights")` are registered, along with
    `register_community_formats`, which reads each member's latest snapshot from this month.

## Stubbed or untested, and why
- **Real AI tags and names.** WP5's `pipeline.advance`, `work.queue_deep` and `work.queue_format_names` were still
  stubs during this build, so the tests seed quick analyses directly. Every call into them is guarded.
- **Pulls run only in tests.** WP7's UI, the real extension and Instagram are not involved.
- **Postgres.** The suite wasn't run on Postgres (no server here).
  - All SQL goes through SQLAlchemy Core.
  - New sub-formats are inserted inside a write transaction that re-reads first. On Postgres two parallel refreshes
    could still collide on the unique key; the job would then retry.
- **Load.** `/api/reels` filters and pages in Python after one SQL query on the member's reels. That is fine for the
  10-competitor cap (a few thousand rows), but its speed on 10,000 reels was not measured.
- **Heuristics to tune.**
  - The maturity curve is still the design's heuristic, not yet refit from `reel_snapshots`.
  - Discovery's `pic_url` is always null: `discover_candidates` has no column for it.
- **Running the tree from the parent folder.** `python -m pytest reelvault/tests` run from `/home/user/linkedin`
  fails to import the desktop package (`cannot import name '__version__' from 'reelvault'`): the cwd makes the
  outer `reelvault/` folder a namespace package. No WP4 file is involved. Use `pytest reelvault/tests` there, or
  run from the project root.

## Fix round 1 (2026-11-10)
- Insight units use ONE tagger: `build_units` calls `effective_tags(..., quick_only=True)` (member override ?? quick
  column). Deep reads run almost only on outliers, so their tags made tagger habits look like lift. The reel page and
  outputs still use override ?? deep ?? quick. On the journey copy the "Voice only 18.3x" / "Bold claim 15.2x"
  artefacts are gone (SD05 214 reels at 1.0x) and the planted formats lead (VF04 9.5x, VF08 9.5x, VF05 8.0x).
- `_lift_table` for `sub_format`: W and H0 come from every visual-ok unit with a visual_format (ungrouped reels count
  in the base and as "not this format" within each creator, no row). A sub-format holding all reels of one VF now
  gets that VF's lift (journey: 1.0x -> 1.78x). Tests: `test_deep_reads_on_hits_do_not_change_lift`,
  `test_sub_format_lift_uses_the_visual_format_base_rate`.
- `lift_phrase` < 0.05 reads "Rarely an outlier" (also in working.js), never "0x".

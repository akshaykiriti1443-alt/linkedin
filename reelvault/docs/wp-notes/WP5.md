# WP5 AI layer, pipeline, outputs and desktop worker: notes

Status: done. Nothing is committed. All WP5 tests pass (see "Evidence"). The real Claude Code run passed on this
machine.

## Files

| file | what |
|---|---|
| `reelvault/ai/__init__.py` | `get_provider`, `make_provider`, `structured`, `text`, `portable_schema`, `pick_model` |
| `reelvault/ai/base.py` | `Image`, `Request`, `Usage`, `Result`, errors, `Provider`, `extract_json`, `plain_http_error` |
| `reelvault/ai/anthropic_p.py`, `gemini_p.py`, `openai_p.py`, `compat_p.py` | the four adapters (7.1.2) |
| `reelvault/ai/presets.py` | provider catalog, presets, vision flags, rough prices, `estimate()` |
| `reelvault/ai/untrusted.py` | `wrap`, `clean`, `escape`, `outside_blocks`, `RULE`, caps per source |
| `reelvault/ai/prompts/*.md` | `_shared.md` + 10 prompts (adds `name_formats.md`) |
| `reelvault/schema_members.py` | `Short`, `Few`, every AI-facing model, `MODELS`, `CHUNK_MODELS`, `OUTPUT_MODELS`, `field_guide` |
| `reelvault/taxonomy.py`, `reelvault/data/taxonomy.json` | section 10 as data; WP3's `local_rules` block kept exactly |
| `reelvault/server/work.py` | executors, queueing, task builders, images, tag batches, save functions, MCP helpers |
| `reelvault/server/pipeline.py` | `advance()` (6.0), `advance_many()` |
| `reelvault/server/jobs_ai.py` | API-key runner for the 7 AI kinds, lease-expiry hooks |
| `reelvault/server/outputs.py` | create/quick/redo/edit, briefs, chunk saves and cross-checks, report numbers, md/txt/csv/print |
| `reelvault/server/api_ai.py` | `/api/ai/*` (4.5) and `/api/worker/*` (4.7) |
| `reelvault/server/api_outputs.py` | `/api/outputs/*` (4.10); registers the admin support mirror `outputs` |
| `reelvault/members_worker.py` | desktop worker: pairing, `members.json`, `run_task`, `process_one`, `Worker` |
| `reelvault/app.py` | the Settings card only (see "Desktop card") |
| `tests/server/_wp5_support.py` | shared helpers: seeding, processed fixture reels, fake AI answers, fake OpenAI-compatible server |
| `tests/server/test_{ai_providers,schemas,prompts,pipeline,outputs,worker_api}.py` | WP5 tests |

## Interfaces other packages use

```python
# pipeline.py (WP3, WP4 call it)
advance(user_id, reel_id) -> str | None          # the job it queued or found active
# work.py
ai_executor(user_id, kind) -> "server" | "mcp" | "desktop" | None
enqueue_ai(user_id, kind, payload, *, priority, dedupe_key, conn=None, state=None, message=None, error_code=None)
queue_deep(user_id, reel_ids) -> [job_id]        # the next job per reel (fetch, process or analyze)
queue_profile_draft(user_id) -> job_id           # sets profiles.status = drafting
queue_topics(user_id, source="profile"|"reels") -> job_id
queue_retag_topics(user_id) -> job_id | None     # flags every tagged reel, 40 reels per call
queue_format_names(user_id, format_ids) -> job_id | None   # only the member's formats, <= 8
create_output(user_id, kind, options, *, purpose=None, brand_id="me") -> (output_id, job_id)   # raises NotReady
quick_output(user_id, action, *, reel_id=, format_id=, vf=, report_id=) -> (output_id, job_id)
resume_ai_jobs(user_id, *, conn=None) -> n       # after a mode change or a key that works again
build_task(user_id, job_row, *, provider=None, dest=None) -> AiTask dict (4.7; + "max_output_tokens")
task_images(user_id, job_row, dest, *, max_images=None) -> [ai.base.Image]   (Image.name = the file name)
lease_tag_batch(conn, user_id, job_row, dest, n=None, only=None) -> [reel_id]
prepare_external(user_id, executor) -> prepare(conn, job) for jobs.lease_external
save_result(user_id, job_row, output, made_by=None, *, usage=None, finish=True, vision=None) -> dict
next_pending(user_id, kinds) -> Row | None ; pending_counts(user_id, executor=None) -> {kind: n}
Invalid(message)  (a ValueError) ; NotReady(message, code, status)
# MCP helpers (WP6)
mcp_tag_batch(user_id) -> (batch_id, text, [Image]) | None          # < 90,000 chars incl. base64
mcp_reel_task(user_id, reel_id=None) -> (job_id, text, [Image]) | None   # 1 mcp sheet + 2 frames
output_brief(user_id, output_id=None, *, kind=None, options=None) -> {"output_id","kind","brief","schema_name","chunk"}
mcp_save_output(user_id, output_id, data, *, chunk_index=None) -> {"ok","url","next_chunk"?}
mcp_save(user_id, kind, output, *, job_id=None) -> {"ok", ...}       # save_profile_draft / save_topics / save_format_names
taxonomy_text(part="all"|"visual"|"sound"|"fields") -> str          # <= 30,000 chars
```

WP6 notes:
- `save_tags`: `work.save_result(uid, jobs.get(batch_id), {"tags": [...]}, {"via": "mcp", ...})`. A batch whose lease
  ran out (30 min) is refused with `Invalid("This batch expired ...")`; return it as `isError`.
- `save_analysis`: find the job with `dedupe_key == f"analyze_reel:{reel_id}"` (or `mcp_reel_task` returns its id) and
  call `save_result`. Saving twice is an upsert.
- `Invalid.message` is the text to send back with `isError: true` (it holds the pydantic or cross-check message).
- `list_pending_work`: `work.pending_counts(uid, "mcp")` (`tag_reels` there counts REELS, not jobs).

WP7 notes:
- `POST /api/outputs` with `{"kind", "options", "purpose"?, "brand_id"?}`; `POST /api/outputs/quick`; claude_prompt is
  returned when the executor is `mcp`. `GET /api/outputs/<id>` adds `numbers` (handles, scores, links for the cards).
- Profile draft answers: see "Deviations" (suggestions and the `_draft` key).

## Deviations from DESIGN-SAAS.md (please fold in)

1. **New prompt file** `ai/prompts/name_formats.md` (7.5 lists 9 files; `name_formats` needs one too).
2. **New models** in `schema_members.py`: `FormatGuideBatch {guides}`, `TopicRetagBatch {tags: [{reel_id, topics,
   new_topic}]}`, `DraftAnswers`/`DraftAnswer` (ProfileDraft answers are `{value, items, source_ids, confidence,
   note}`: `items` for list questions q3/q6/q14), `StoredWeek` (a stored plan week may hold fewer than 7 days after the
   day-30 cut), `PainCoverage`. ProfileDraft also has `niche_label` and `search_terms` (9.4 says draft_profile writes
   them). CompetitorReport has `pain_coverage` (8.4: the AI may only say covered/not covered).
3. **Profile drafts.** Answers under 0.5 confidence are stored as `{"value": "", "suggestion": <value>, ...}`. Draft
   extras (niche label, search terms, competitor handles, made_by) are stored in `profiles.answers["_draft"]`, so WP7's
   approve can copy `niche_label`/`search_terms` into the ProfileDigest.
4. **Quick hook-bank rows** (4.9, D49) are not a table: `analyses.data["hook_bank"]` holds `{spoken, on_screen_text,
   visual, hook_type, level}` on quick rows with O >= 2, and the deep row's `data["hook_bank"]` (level deep, with
   `formula`, `why`) upgrades it. WP4's `/api/banks/hooks` reads them.
5. **Quick tag extras** in `analyses.data`: `tag_quality`, `local_agrees` (measured tag that agrees with a local guess
   with p >= 0.8, 6.5). Recipe keys use WP4's `insights.recipe_key`.
6. **pipeline.advance** sets `tag_state=queued` AND queues `analyze_reel` in one call when both apply (the tag is a
   flag, not a job). A requested deep read on a reel whose signals failed runs text-only (the design's table had no
   row, so the output that waits for it would wait forever). A deep read of a reel transcribed without word times
   re-queues `process_reel` with `words=True` first (WP3's "words later" rule). `deep_state` becomes `waiting` when
   the analyze job goes to the member's Claude or desktop app.
7. **Executors.** `enqueue_ai` creates a `paused` job with `error_code="no_ai"` when the member has no AI.
   `resume_ai_jobs` moves queued/waiting/paused AI jobs to the current executor; paused jobs resume only for
   `no_ai`, `bad_key`, `quota`. Outputs waiting for a source reel's deep read are `paused` with
   `error_code="waiting_deep"` until `unblock_outputs`.
8. **Tag slots.** The `tag_reels` slot job frees its own dedupe key when it saves (or ends) and queues the next slot,
   so a member keeps exactly one waiting slot while reels wait. Retag jobs chain the same way (40 reels per job).
   Topic suggestions from reels run when the member reaches 30 tagged reels and then every 30 more
   (`users.settings.topics_from_reels_at`), an approximation of "after each later pull that adds >= 30".
9. **Chunks for external executors.** After a chunk is saved by MCP or the desktop app, the `make_output` job goes back
   to `waiting` (attempts reset) so the next lease writes the next chunk.
10. **Cue check.** `save_deep` drops sound cues whose time is more than 0.6 s from any detected event, silence or music
    edge (count in `data["dropped_cues"]`), instead of rejecting the breakdown.
11. **Upgrades without a deep read.** When a source reel has no deep breakdown, an Upgrade may cite
    `"<reel_id>:general"` (otherwise `<reel_id>:W<n>` or `card:<dimension>:<value>` as designed).
12. **Report numbers.** `outputs.report_numbers` builds the 8.4 account table, remake candidates and pain coverage from
    the stored rows and takes the insight parts (cards, formats, grid, combos, signatures, music, you-vs-them, data
    note, rising/fading) from WP4's `jobs_score.report_numbers` (which returns the 6.7.5 Insights JSON), else from the
    latest snapshot.
13. **API extras.** `Conn` adds `use_for` and `options`; `GET /api/ai` desktop devices add `claude` and `working`;
    `/api/worker/hello` accepts `working`; lease replies add `lease_until`; the result route accepts a `model`.
    `/api/ai/estimate` accepts `free_tier`. `POST /api/ai/connections` sets `ai_mode=api` when the member had no mode,
    and `use_for: "tagging"` sets `users.tagging_conn_id`. Max 6 connections; 30 key tests per hour per member.
14. **SDK retries are off** (`max_retries=0`): the job runner owns retries (RateLimited -> `Retry` with the provider's
    Retry-After, capped 15 s - 1 h), so a worker thread never sleeps inside an SDK. The Anthropic SDK (2.6, httpx2)
    refuses `httpx` clients; it uses its own default client (fixed public host).
15. **Print page.** `GET /api/outputs/<id>/download?format=print` sends its own CSP (`default-src 'none'; style-src
    'sha256-...'`), since WP1's CSP blocks inline styles and the page has no other stylesheet.
16. **Desktop card.** The rule is "app.py only", so the card is a small script that app.py serves at
    `/members-card.js` and adds to `index.html` at request time (`reelvault/ui/` is not edited). New local routes:
    `GET /api/members`, `POST /api/members/link`, `POST /api/members/working`, `POST /api/members/unlink`.

## Evidence

- WP5 tests: `test_ai_providers` 17, `test_schemas` 53, `test_prompts` 4, `test_pipeline` 9, `test_outputs` 14,
  `test_worker_api` 7 + 1 opt-in (113 + 1).
- Full runs (from the project root `reelvault/`): `pytest tests/server` 446 passed, 8 skipped; desktop
  `pytest tests/test_*.py` 51 passed; `pytest tests` 539 passed, 8 skipped, 1 failed: WP2's
  `tests/ext/test_ext_e2e.py`, because another session's copy of that same test held its fixed ports 9141/9143 at the
  same time (ECONNREFUSED; `tests/ext/run_server.py` itself starts fine with the WP5 code, checked on port 9219).
- Running pytest from the repo's PARENT folder (`pytest reelvault/tests` in `/home/user/linkedin`) makes `reelvault` a
  namespace package, so `from . import __version__` fails in the desktop `app.py` tests (pre-existing, not WP5).
  `members_worker.py` falls back to the installed version in that case. Run the suites from `reelvault/`.
- Real Claude Code (`RV_REAL_CLAUDE=1 python -m pytest -q -s tests/server/test_worker_api.py -k real`, 70 s): a real
  uvicorn server, pairing with a real code, `members_worker.Worker.tick()` running the unmodified `claude -p`
  (2.1.321, the machine's own login) on one synthetic reel. Quick tags (schema on the command line, 2 images read with
  the Read tool) and a deep breakdown (ReelBreakdown's argv is 6,118 characters, so it went through `schema.json`)
  were both saved. Claude tagged the generated cartoon-face clip VF19 (animation) with
  `visual_subject = "Cartoon avatar facing camera under a fixed '3 ADS THAT PRINT LEADS' box"` and listed 5
  weaknesses with times (e.g. "Very quiet mix (-31.5 LUFS) and no clear voice detected").
- Desktop argv per model (`members_worker.argv_length`): QuickTagBatch 3,086; ReelBreakdown 6,118 (schema file);
  ReelScriptPack 5,655; MetaAdPack 3,938; CompetitorReport 3,023; the rest under 2,200.
- MCP sizes: a tag batch of the heaviest fixture sheets stays under 90,000 characters and leases exactly the reels it
  returns; a deep payload (1 sheet + 2 frames + text) stays under 90,000 (tests in `test_outputs.py`).

## Not built or not tested here

- No real Anthropic, Gemini, OpenAI, OpenRouter or Groq call (no keys on this box, and keys must never be committed).
  The adapters are tested against the SDKs' real request building with mock transports, and the compat adapter
  against a real local HTTP server. Model ids and prices in `presets.py` follow the design and must be checked
  live (14.2 step 6).
- OpenRouter's live model list (`/api/v1/models` filtered to structured outputs) is not fetched; the preset has a
  default model only.
- The desktop card was tested through app.py's real HTTP handler (not in a browser) and the worker loop through
  `Worker.tick()`; the Windows `claude.cmd` path is covered only by the argv-length rule.
- `outputs_per_day` counts every created output (also redo and quick actions), per the design.

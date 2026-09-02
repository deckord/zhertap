# Logic Changelog and Incident Notes

This file records production-visible logic changes, operational incidents, and the reason behind each fix. Update it whenever search, provider, payment, genplan, auction, or delivery behavior changes.

For each entry, include:

- Date and production context.
- User-visible symptom.
- Root cause or strongest confirmed evidence.
- Code or configuration changed.
- Verification performed.
- Rollback or follow-up notes.

## 2026-09-02 - Materialize active decision evidence coverage fail-closed

Production result: after the latest bounded history/market/input refresh, the existing worker
pipeline materialized exact-version decision snapshots for the complete active land set. This
was a runtime continuation and read-back, not a claim that investment inputs became sufficient.

Read-back:

- `198/198` active land lots have a current `decision-snapshot/2026.3` plus
  `five-state-verdict/2026.1` snapshot; no active lot is missing the exact-version row.
- All 198 remain `stale=true`, `data_readiness=partial`, and have `ceiling=null`: 185 are
  `manual_required/requires_check`, while 13 deterministic blockers are
  `blocked/do_not_participate`.
- Every snapshot has explicit unknowns, an action and a non-empty deterministic reason list;
  138 also have evidence-backed risks. Active decision-input states are `198/198 insufficient`
  (`417 insufficient` across the retained state store), principally because official
  restriction/site/planning, strict market/legal and actual-cost inputs remain incomplete.
- Auctions and critical queues were both empty; `/health` and `/ready` returned HTTP 200 with
  PostgreSQL and Redis checks true. AI and document extraction remain `false,false`.
- No schema/data migration, bulk mutation, service rebuild, AI/document processing, or visual
  change was made. The 116-point matrix was corrected from the older `0/199` snapshot count to
  this exact-version runtime result.

Rollback: none for runtime rows; snapshots are immutable/versioned outputs of existing evidence.
The documentation-only correction can be reverted independently. Readiness remains blocked and
no STOP/ceiling is presented as a production-ready investment conclusion.

## 2026-09-02 - Close active Jerler polygon and NSDI water-zone coverage

Production gap: all active lots had canonical identity, but 33/199 lacked a canonical parcel
boundary and had no persisted Jerler source-card evidence. The bounded source-object task did
commit successful rows before its next shared rate-limit deferral, but returned an all-zero
result, hiding real progress and skipping dependent recomputations. NSDI evidence also treated a
temporary `source_unavailable` result as permanently checked.

Fix and bounded backfill:

- Detached Jerler batches now expose committed partial counters on typed deferral, trigger the
  same downstream refreshes as a normal partial success, and accept a bounded internal lot-ID
  target set without holding a database transaction during HTTP.
- NSDI worker evidence now timestamps every observation and retries only stale
  `source_unavailable` rows after 15 minutes; completed checks remain idempotent.
- After a verified pre-backfill database dump, exactly the 33 active boundary gaps were fetched
  from their official Jerler cards. All 33 supplied valid published polygons. The official NSDI
  layer was then read for those parcels; an initial transient unavailable pass was recovered
  after successful source probes and all 33 were persisted as
  `no_intersection_in_published_layer` (still `manual_required`, never legal clearance).
- No AI/document extraction, migration, UI, CSS or template behavior changed.

Verification and release:

- Test-first regressions failed before each patch. Jerler/task/provider suite passed (`37
  passed`); NSDI worker/evidence/intersection/schedule/task suite passed (`12 passed`); Ruff and
  `git diff --check` passed.
- Backup: `/opt/land-scout/backups/pre_jerler_active_gap_20260902T051900Z.dump`
  (663,850,979 bytes, SHA-256
  `e3b6da2f5418ed2bdb7c2de16bed9d9fda869ddfc447e69def45de59797e0c58`). Narrow code backups
  are under `code-20260902T051248Z-jerler-partial-progress` and
  `code-20260902T053100Z-nsdi-retry`.
- Only `auction_worker` was rebuilt. Checkout/container hashes match. `/health` and `/ready`
  return HTTP 200; worker logs contain no new error/traceback; AI and document extraction remain
  `false,false`.
- Final current read-back: 198/198 active land lots have canonical identity and boundary; source
  state/time is coherent (190 accepting applications, 2 registration/acceptance, 6 already
  running). NSDI coverage is 198/198: 1 real `intersection_found`, 197
  `no_intersection_in_published_layer`, zero missing and zero `source_unavailable`.

Rollback: restore the four backed-up modules and rebuild only `auction_worker`. Restore the dump
only if the bounded evidence/boundary writes themselves must be reverted.

## 2026-09-02 - Bound Jerler rate-limit continuations

Production symptom: a bounded canonical-boundary refresh encountered the shared Jerler
provider rate limiter. `sync_auction_source_objects` handled the typed deferral by publishing
a brand-new task with no carried attempt count. Under sustained provider contention this
formed an unbounded self-republishing loop every few seconds and still made no source call.

Fix:

- Added an explicit internal continuation counter and a hard cap of three continuations.
  The normal Celery retry budget remains separate; a provider deferral still does not sleep
  a worker or consume `self.retry`, but it can no longer publish forever.
- Exhaustion is returned and logged explicitly as `continuation_exhausted=1`. No lot,
  evidence, boundary, schema, AI/document extraction, or UI behavior was changed.

Verification and release:

- Test-first regressions failed before the patch (missing counter/cap), then focused task and
  provider-guard suites passed (`23 passed`); Ruff and `git diff --check` passed.
- Backup: `/opt/land-scout/backups/code-20260902T045049Z-jerler-bounded-deferral/tasks.py`
  (pre-change SHA-256 `72a70a8040d62753a93643eb6f7e10f5060ab05909f86b3ec4495233f316c504`).
- Only `auction_worker` was rebuilt/recreated. Checkout/container SHA-256 is
  `12155dd118563ce1991f48f541a13fbc995a2a8a500eff99e7ba9a45573da138`; worker registered
  the task and reached ready state. A production chain emitted continuations `1/3`, `2/3`,
  `3/3`, then exactly one `continuation_exhausted=1` result and stopped.
- `/health` and `/ready` returned HTTP 200; AI and document extraction remain `false,false`.

Follow-up: the 33 active lots without canonical boundary still have no persisted Jerler
source-object evidence. Shared Jerler rate contention is now a finite, observable blocker;
polygon coverage remains `166/199` and no geometry may be inferred from address/cadastre text.

Rollback: restore the backed-up `tasks.py` and rebuild only `auction_worker`. No database
restore is needed.

## 2026-09-02 - Sweep overdue E-Qazyna detail cards without starvation

User-visible risk: E-Qazyna removes completed auctions from the current-list result before a
previously ingested `Running` row necessarily receives its final detail status. The normal
current crawl could therefore leave an old running lot active indefinitely. Retrying only the
newest overdue rows was also unsafe: a fixed set of upstream error cards could starve every
older row in a bounded sweep.

Fix:

- Added a five-minute, five-row direct-detail sweep for active E-Qazyna lots whose auction
  start is at least 15 minutes in the past. Eligibility accepts either the bounded list status
  or the official detail status, so a missing list code cannot hide a stale running card.
- Added a durable keyset cursor in the existing provider-workflow store. Cursor progress is
  committed before provider I/O and wraps only after the older eligible set is exhausted;
  failed newest cards therefore cannot pin the sweep.
- Official detail failures remain errors and never deactivate a lot. A successful official
  terminal detail is persisted through the normal upsert/history/identity path.
- No schema migration, bulk rewrite, AI/document extraction, or UI/CSS/template change was
  made.

Verification and release:

- Test-first coverage proves both detail-only `Running` eligibility and durable progress past
  a failed five-row newest batch. The focused status/time/deadline suite passes (`5 passed`).
- Only `auction_worker` was rebuilt/recreated. Checkout and live-container hashes match for
  `auction_service.py` (`842c06f8...`) and `tasks.py` (`72a70a80...`); the task is registered
  and Beat emits it every 300 seconds.
- Production created durable cursor `eqazyna:due-status-refresh:v1`; after a complete empty
  pass it is `{}`. Current catalogue read-back is 199/199 active land/`ApplicationsAccept`,
  with zero null/past auction times, publication-after-auction rows, or canonical-identity
  gaps. `/health` and `/ready` return 200, Alembic is at `c2f6a8d1e4b9 (head)`, and AI plus
  document extraction remain `false,false`.
- This closes stale-active starvation, not the independent current-crawl blocker: the latest
  bounded run still retries official `Running` cards which E-Qazyna returns as database-error
  pages, so missing-lot deactivation remains fail-closed.

Rollback: restore the two Python modules from the current-code snapshot, rebuild only
`auction_worker`, and delete the single cursor row only if the sweep itself must be removed.
No database restore is required.

## 2026-09-02 - Persist and link structured official territory facts fail-closed

Risk before the change: `auction_territory_intelligence.py` validated source, publication
date, revision, official scope and whole-parcel applicability, but qualifying observations
and their parcel relations had no durable database/runtime path. A class or pure function
alone was not a production vertical.

Fix:

- Added immutable `auction_territory_observations` revisions with source authority, URL,
  publication/observation dates, normalized payload, geometry/content hashes and contract
  version. Identical retries are idempotent; changed content at the same revision and
  invalid lifecycle regressions fail closed.
- Added boundary-versioned `auction_territory_applicability`. Only an official polygon that
  covers the whole canonical parcel becomes `applicable`; partial overlap, point/prose,
  matching territory code or missing scope remains `manual_required`.
- Added the bounded, restart-safe `land_scout.link_territory_observation` auction-worker task.
  No periodic crawler was enabled because no trusted structured official corpus currently
  satisfies the contract. Existing gov.kz prose is not imported into this table.
- AI/document extraction remained disabled; no UI/CSS/template change was made.

Verification and release:

- Test-first failure was `ModuleNotFoundError`; focused store/worker/contract and migration
  suite then passed (`20 passed`), broader task-focused suite passed (`26 passed`), Ruff and
  Alembic single-head checks passed.
- Pre-migration backups:
  `/opt/land-scout/backups/pre_territory_store_20260902T022746Z.dump`
  (654,301,957 bytes, SHA-256
  `7a658321029a33e2af58eaead03541c49685b552b305e1e97b4fa4f5fdbc9e1c`)
  and `/opt/land-scout/backups/code-20260902T022746Z-territory-store.tgz`.
- Initial build hit a real 100%-disk blocker. Pruning inactive Docker build cache reclaimed
  17.1 GB; no database volume or active image was removed. Migration
  `c2f6a8d1e4b9` then applied and only `web`, `auction_worker`, and `beat` were rebuilt.
- `/health` and `/ready` return 200; task registration and checkout/container hashes match.
  A transaction-scoped production smoke persisted a 64-byte-hash contract row and rolled
  it back; read-back remains `observations=0`, `applicability=0`.

Follow-up: integrate only a provider that supplies mapped official codes, stable revision,
authority/date and official polygon. Corpus absence remains an explicit data blocker, not
an invitation to classify news text or infer geographic applicability.

## 2026-09-02 - Release already-failed current runs from downstream source waits

User-visible risk: a current E-Qazyna parent with terminal detail-card failures was already
irreversibly unsuccessful, but still occupied the unique current-run slot until the shared
OSM/document source child finished or the generic one-hour parent timeout fired. This delayed
the next bounded catalogue attempt even though downstream enrichment could no longer change
the parent's result.

Fix:

- After durable `start_sources` dispatch, current/full parents whose own provider result is
  already failed now finish immediately as `error`. Successful parents retain the existing
  downstream barrier; the independent source run remains durable and continues unchanged.
- No deactivation, schema/data migration, AI/document extraction, or UI change was made.

Verification:

- The regression failed before the patch because the known-failed parent was not finished.
  Focused task/workflow/guard suites passed (`40 passed`); Ruff and `git diff --check` passed.
- Narrow backup: `/opt/land-scout/backups/code-20260902T012348Z-failed-current-finalize`.
  Only `auction_worker` was rebuilt/recreated. Local, checkout and container hashes match
  (`116b46ca...`); `/health` and `/ready` return HTTP 200.
- Recovery closed current run `849a81436401431bb6db09c1ff8602db` as `error` at
  `2026-09-02 01:25:19 UTC` instead of waiting until the one-hour timeout. Its 12 failed
  Running-status cards still return HTTP 200 E-Qazyna execution-error pages and remain
  inactive `invalid_source_response` rows; missing-lot deactivation was not promoted.

Rollback: restore the backed-up `tasks.py` and rebuild only `auction_worker`. No database
rollback is needed; the affected parent was already unsuccessful and was closed fail-closed.

## 2026-09-02 - E-Qazyna historical source exhaustion completed

Production result: the resumable status/date/page ledger reached verified empty-page
exhaustion for every configured E-Qazyna historical cohort. Run
`658743b3c1ca4d36aa4ed4282ebd19d1` resumed the sole remaining cohort at absolute page
100, found an empty page after six bounded list requests, enqueued no details, and
completed with all `36/36` durable checkpoint values equal to `0`.

Read-back:

- the catalogue now contains `28 133` lots (`27 294` land lots);
- all `199` active lots are land/`ApplicationsAccept`, have a future non-null deadline,
  a publication date not later than the deadline, and canonical identity;
- no code, schema, migration, bulk rewrite, service rebuild, or visual change was made;
  the existing bounded worker path was invoked and read back;
- `/ready` remained HTTP 200 and AI/document extraction flags remained `false,false`.

This closes historical source pagination/exhaustion, not the whole current-catalogue
vertical. Current run `849a81436401431bb6db09c1ff8602db` remains fail-closed because
several official detail URLs return E-Qazyna's database-error page; missing-lot
deactivation is correctly withheld. Normalized-history generation 285 subsequently
activated with `27 294/27 294`, `scan_complete=true`, and zero errors; its outcome
read-back is 24 010 found, 2 807 conflict, and 477 unknown.

Rollback: none. This was an idempotent production runtime continuation with no code or
manual data mutation.

## 2026-09-02 - Do not exhaust history windows that lost a detail row

User-visible risk: the durable E-Qazyna history checkpoint derived progress only from
list-page units. If a window reached an empty page but one of its detail fetches became
a terminal provider error, the parent correctly failed but the checkpoint still marked
the window exhausted (`0`). Every later run would then skip that window permanently,
turning a known missing detail into false archive completeness.

Fix:

- History checkpoint promotion now reads all units in each E-Qazyna workflow. List rows
  still provide the bounded page/window identity, but any non-cap terminal detail error
  prevents that workflow from advancing or becoming exhausted.
- The prior durable checkpoint is retained, so the next bounded history run safely
  replays the affected window. `detail_limit_reached` remains an intentional partial
  checkpoint and is not misclassified as a provider failure.
- No schema/data migration, AI/document extraction, or UI change was made.

Verification:

- Test-first regression failed before the patch (`0`, expected prior page `11`). The
  complete workflow-store suite passed (`25 passed`) and the provider/runtime suite
  passed (`41 passed`); Ruff, compile check and `git diff --check` passed.
- Narrow production backup:
  `/opt/land-scout/backups/code-20260901T232606Z-history-detail-checkpoint`.
  Only `auction_worker` was rebuilt/recreated. Local, checkout and container hashes
  match (`2b3398c6...`); `/health` and `/ready` pass and worker recovery resumed durable
  units after restart.
- Live run `d9612d2780c84f9299285461b2aea55a` supplied the real failure case: one detail
  became terminal after repeated E-Qazyna rate limits while 3,719 detail units remained.
  A rollback-only production projection retained affected window checkpoint
  `a834e924aa32cb44` at page `54` instead of falsely promoting it to exhausted (`0`).
  The run is not claimed complete; the next bounded run will replay that window.
- A separate current run remains fail-closed on 14 detail cards that E-Qazyna redirects
  with HTTP 200 to `/ru/error?Location=Database`; three direct production-host requests
  reproduced that upstream database redirect. Missing-lot deactivation is therefore
  not promoted. AI and document extraction remain disabled.

Rollback: restore the backed-up module and rebuild only `auction_worker`. No database
rollback is needed.

## 2026-09-02 - Bound current-catalogue waits on shared source enrichment

User-visible risk: a completed E-Qazyna current/full crawl remained `finalizing` while
its shared downstream source run waited on OSM circuit backoff and document work. The
unique active-run guard then reused the stale parent for every scheduled catalogue
refresh: the latest current parent was blocked for more than 21 hours even though all
of its own E-Qazyna workflow units were terminal.

Fix:

- Durable outbox recovery now fails a current/full parent closed after its acknowledged
  `start_sources` dispatch has waited one hour. The timeout is anchored to immutable
  dispatch creation time, so child-counter reconciliation cannot postpone it.
- The independent shared source run is not stopped or promoted; errors remain errors,
  missing-lot deactivation remains disabled for incomplete E-Qazyna crawls, and the
  next bounded current run can start normally.
- No schema/data migration, AI/document extraction, or UI change was made.

Verification:

- Test-first regressions cover refreshed parent timestamps, undispatched parents and
  preservation of the active shared source run. Provider/runtime suite passed
  (`36 passed`); focused timeout/outbox suite passed (`4 passed`); Ruff, compile check
  and `git diff --check` passed.
- Narrow backups:
  `/opt/land-scout/backups/code-20260901T214152Z-provider-parent-timeout` and
  `/opt/land-scout/backups/code-20260901T214732Z-provider-parent-timeout-anchor`.
- The first build exposed a real disk blocker at 99% usage. Safe removal of inactive
  Docker build cache and one dangling image reclaimed about 20 GB; the affected
  `auction_worker` then rebuilt and started successfully.
- Production recovery expired both stale parents as errors while source run
  `db5a3e593ec140308c07eddbe409bf9c` remained active. A fresh current run was then
  accepted with seven bounded status workflows. Checkout/container hashes match;
  AI and document extraction remain disabled.

Rollback: restore the two modules from the backups and rebuild only `auction_worker`.
No database rollback is needed; the two stale parents were already unsuccessful and
were closed fail-closed rather than promoted.

## 2026-09-02 - Do not charge replayed E-Qazyna detail URLs against the run cap

User-visible risk: resumed history pages deliberately replay their last successful page,
but `details_enqueued` was incremented before already-persisted detail unit keys were
removed. A duplicate URL could therefore consume the global 1,000-detail budget without
creating work and prematurely terminate untouched list pages.

Fix:

- Follow-up units are now deduplicated by key and checked against durable workflow units
  before detail-cap accounting and the workflow-size bound.
- Added a run-backed regression proving a duplicate detail URL leaves the next list page
  pending and keeps `details_enqueued` equal to the one row actually inserted.
- No schema/data migration, AI/document extraction, or UI change was made.

Verification:

- The new regression failed before the patch (`details_enqueued == 2`, expected `1`).
  Focused provider/runtime tests then passed (`28 passed`); Ruff, compile check, and
  `git diff --check` passed.
- Production backup:
  `/opt/land-scout/backups/code-20260901T210259Z-provider-detail-cap-dedup`.
  Only `auction_worker` was rebuilt/recreated. Checkout/container module hashes match
  (`b6bfe429...`); `/health` and `/ready` pass and worker startup is clean.
- Read-back of preceding run `0b43c07ef82e4f5780de5288e07fdc81` remains explicitly
  partial: 13/13 resumed windows hit the detail cap, while the durable aggregate
  checkpoint is 19 exhausted and 13 resumable. This patch removes one source of false
  cap consumption; it does not claim source exhaustion.
- `AUCTION_V2_DOCUMENT_EXTRACTION_ENABLED=false` remains in the live worker.

Rollback: restore the two files from the narrow backup and rebuild only
`auction_worker`. No database rollback is needed.

## 2026-09-02 - Resume capped E-Qazyna history windows from durable pages

User-visible risk: the source-exhaustion ledger correctly rejected capped history
runs, but every later hourly run seeded every status/date window from page 1. Existing
lot skipping allowed incidental progress while repeatedly spending requests on proven
pages; URLs dropped on the detail-cap page had no explicit durable replay point.

Fix:

- History-run finalization now stores a bounded 16-character status/window checkpoint
  in the existing provider-run config. `0` means an empty page proved exhaustion;
  otherwise the value is the last successfully fetched non-empty absolute page.
- Later history runs omit exhausted windows and replay each incomplete window from its
  checkpoint page. Replaying that page is deliberate: list replay is idempotent and
  recovers detail URLs that may have been cut off by the global detail cap.
- Absolute pagination remains bounded at page 1000. Current-catalogue crawls are not
  allowed to carry historical exhaustion and retain their freshness behavior.
- No schema migration, auction data rewrite, AI/document extraction, or UI change was
  made.

Verification:

- Focused provider/runtime suite passed (`27 passed`); Ruff and `git diff --check`
  passed.
- Narrow code backup:
  `/opt/land-scout/backups/code-20260901T204109Z-eqazyna-history-resume`.
  Before the one-row checkpoint initialization, `provider_sync_runs` was dumped to
  `/opt/land-scout/backups/provider_sync_runs_before_history_checkpoint_20260901T204254Z.sql`.
- Only `auction_worker` was rebuilt/recreated. Checkout and live-container hashes match
  for all three changed modules; `/health` and `/ready` pass and startup is clean.
- Read-back of completed run `a4f1834d9e66437da1ecee3fadf2488e` persisted 32 window
  checkpoints: 19 exhausted and 13 resumable. The next live run
  `0b43c07ef82e4f5780de5288e07fdc81` seeded exactly 13 windows at pages
  15–37 and none at page 1. It is still active and is not claimed source-complete.
- AI and document extraction remain disabled.

Rollback: restore the three modules from the narrow backup and rebuild only
`auction_worker`; restore the one-row provider-run dump only if the checkpoint itself
must be removed.

## 2026-09-02 - Durable E-Qazyna source-exhaustion ledger

User-visible risk: a bounded E-Qazyna provider run could finish successfully while
some status/date windows stopped at the global detail cap. Aggregate URL/page counts
did not distinguish a proven empty terminal page from an incomplete bounded crawl.

Fix:

- Added a durable read contract over existing provider workflow/unit rows. For every
  status and publication-date window it reports the configured page bound, requested
  pages, URLs seen, first empty page, `exhausted`, and an explicit partial reason.
- Source completion/deactivation now additionally requires every ledger entry to have
  a successfully fetched empty page. A completed capped workflow cannot be promoted to
  source-complete.
- No schema migration, bulk update, document extraction, AI, or UI change was made.

Verification:

- Test-first regression failed on the missing contract, then the focused provider and
  auction suites passed (`55 passed`); Ruff and `git diff --check` passed.
- Narrow backup: `/opt/land-scout/backups/code-20260901T193630Z-eqazyna-exhaustion-ledger`.
  Only `auction_worker` was rebuilt/recreated; local, checkout and live-container hashes
  match (`9b5d285e...`). `/ready` passed and the auctions/critical queues were empty.
- Production read-back of latest history run `718bb1353abe4ac0842713f7fbfdcafa`
  classified 17/32 status/date windows as exhausted and 15/32 as
  `detail_limit_reached`. Thus catalogue/history source exhaustion remains a measured
  runtime gap rather than an implicit success.
- Worker startup is clean. AI and document extraction remain disabled.

Rollback: restore the backed-up module and rebuild only `auction_worker`. No database
rollback is needed.

## 2026-09-01 - Explicit decision unknown/risk/action evidence contract

User-visible risk: a decision snapshot could correctly fail closed as
`manual_required`, yet its public contract could contain no unknown reason when the
scenario or price engine—not a missing/stale module—caused the gate. The action also
omitted the deterministic engine reason codes and the evidence references supporting
that action.

Fix:

- Decision evidence contract `2026.2` now persists every deterministic verdict reason
  on the action and its bounded evidence references.
- A non-ready engine decision with no module-level unknown now materializes the engine
  reasons as explicit unknown facts. Missing/stale module facts remain the primary
  unknowns and are not replaced by generic summaries.
- Blocked and high-risk actions reference their blocker/risk evidence; manual-review
  actions prioritize the evidence attached to unknowns. No decision value is invented.

Verification:

- Test-first scenario and blocker regressions failed before the patch; the focused
  snapshot/task/price-card/verdict/input suite passed (`51 passed`). Ruff and
  `git diff --check` passed.
- Narrow production backup:
  `/opt/land-scout/backups/code-20260901T180930Z-decision-evidence`.
- Only `web` and `auction_worker` were rebuilt/recreated. Local, checkout and both live
  container hashes match (`dc1bc746...`). A live-container read-back with all seven
  required modules absent returned `manual_required`, seven explicit unknown facts,
  the complete engine reason list and no ceiling.
- `/ready` passed, `web` is healthy, `auction_worker` is running, and startup logs show
  no errors. Both AI and document extraction remain disabled.

Dependency gap: the territory-intelligence admission/applicability contract is closed,
but no official project/news feed discovered so far supplies stable revisions,
publication dates, territory codes and parcel polygons together. Search results only
confirmed the generic data.egov.kz API; it is not itself a qualifying project corpus.
No source coverage is claimed and prose/news will remain `manual_required`.

Rollback: restore the backed-up module and rebuild only `web` and `auction_worker`.
There was no schema migration or bulk database write.

## 2026-09-01 - Fail-closed territory source/date/applicability contract

User-visible risk: official project/news facts could be attached to a parcel from
locality prose or a nearby point, even when the source did not publish a parcel
polygon. A matching administrative code proves territory-level relevance, not
parcel-level applicability.

Fix:

- Official observations require an HTTPS source, authority, aware publication and
  observation timestamps, a monotonic source revision and a bounded structured
  event/demographic code. Missing publication dates and invented free-text event
  classes fail closed.
- Polygon/MultiPolygon scope is the only automatic parcel-applicability proof.
  A point or matching territory code yields `manual_required`; an official code or
  polygon mismatch yields `not_applicable`; missing official scope remains unknown.
- Geometry is bounded to Kazakhstan, size-limited, canonical-hashed and validated.
  Lifecycle regressions require an explicit correction revision; stale/conflicting
  revisions are not silently promoted.

Verification:

- Test-first source/date, geometry, lifecycle, demographic-zero and geographic-scope
  suite passed locally (`9 passed`); Ruff passed.
- Narrow code backup:
  `/opt/land-scout/backups/code-20260901T165002Z-territory-applicability`.
- Only `web` and `auction_worker` were rebuilt/recreated. Local, checkout and both
  live-container hashes match (`a89ea0b...`). A live-container read-back returned
  `applicable` for a containing official polygon and `manual_required` for the same
  territory code without polygon scope.
- `/ready` passed, `web` is healthy, `auction_worker` is running, the auctions queue
  is empty, and logs contain no startup errors. AI and document extraction remain
  disabled.

Gap/rollback: this closes the admission and applicability contract, not source
coverage. No official project/news feed or persisted production corpus is claimed;
that remains a data/runtime blocker. Restore the backed-up module and rebuild only
`web` and `auction_worker` to roll back.

## 2026-09-01 - Durable canonical-land identity backfill and AI pause correction

User-visible risk: the conservative canonical identity reconciler existed only as a
library helper. Its newest-first bounded window could repeatedly revisit placeholders
or identifier contradictions and starve older exact-key lots, so a partial manual run
could not be treated as an exhaustive backfill. During deployment verification the
production environment also exposed `AUCTION_V2_LLM_ENABLED=true`, contrary to the
explicitly paused AI/document-extraction policy.

Fix:

- Added a durable keyset cursor with a frozen per-cycle high-water mark, bounded pages,
  aggregate scanned/linked/unlinked counters, two-second continuations, hourly recovery,
  and routing only to the auctions worker.
- Exact EGKN, cadastral or official Jerler keys remain the only admission path. Rows
  without a stable key advance the cursor but are never guessed or merged.
- Added Alembic revision `b0c5d8e1f3a7` for the cursor table.
- Normalized the production environment to one
  `AUCTION_V2_LLM_ENABLED=false` declaration, added the independent fail-closed
  `AUCTION_V2_DOCUMENT_EXTRACTION_ENABLED=false` gate, and omitted the extraction
  sweep from Beat while paused. A directly submitted verification task returned
  `disabled` without changing extraction-state rows.

Verification:

- Test-first starvation, identifierless-window, and paused-backlog regressions failed
  before implementation; the focused identity/extraction/task suites passed
  (`16 passed`). Ruff,
  `git diff --check`, and Alembic single-head checks passed.
- Pre-migration PostgreSQL backup:
  `/opt/land-scout/backups/land_scout_before_canonical_identity_cursor_20260901_160222.sql`
  (`3,309,713,368` bytes). Production migrated from `a9c3e7f1b5d2` to
  `b0c5d8e1f3a7`.
- The first complete durable production cycle scanned all `541` unlinked land lots.
  It found `0` rows carrying an admissible exact stable key, linked `0`, recorded no
  identifier contradiction, and reset both cursor fields at cycle `1`. Existing
  canonical coverage is `7,778 / 8,319` land lots (`93.50%`) across `5,669` objects;
  the remaining `541` are explicitly unresolvable without a stable official key.
- Local, production-checkout, and live `web`/`auction_worker`/`beat` hashes match.
  `/health` and `/ready` pass, Alembic is at head, relevant containers are running,
  logs contain no startup errors, auctions/critical queues are empty, and production
  read-back reports both LLM and document extraction disabled and no extraction Beat
  entry.

Rollback: restore the three Python files and `.env` backups, downgrade one Alembic
revision, rebuild/recreate `web`, `auction_worker`, and `beat`, or restore the database
dump if required. The cursor migration contains no auction-lot bulk rewrite.

## 2026-09-01 - Freeze normalized-history generation membership

User-visible risk: history generations scanned a live `auction_lots` predicate using a
random UUID cursor. Lots committed after the initial count, but carrying a pre-cutoff
`created_at`, could appear on either side of that cursor. Generations 215-217, 223 and
232-233 therefore failed closed with `processed count exceeds snapshot`; the prior
active generation remained available, but archive refreshes could not be promoted.

Fix:

- Each generation now materializes its exact lot membership in one database
  `INSERT ... SELECT` statement and scans only that immutable set.
- Random UUID order remains only the bounded keyset order; it no longer decides
  whether a later-visible row belongs to the snapshot.
- Promotion reconciles membership, processed and normalized counts and rejects
  missing or extra normalized rows before switching the active pointer.
- The migration invalidates only a pre-existing building generation; active history
  remains readable throughout the upgrade.

Verification:

- Regression coverage inserts eligible backdated UUIDs on both sides of an active
  cursor and confirms neither enters the frozen generation (`24 passed`); Ruff and
  Alembic single-head checks passed.
- Production migrated from `d1e2f3a4b5c6` to `a9c3e7f1b5d2`; backup:
  `releases/narrow/20260901_060300_history_membership/` (including a pre-migration
  PostgreSQL dump).
- Live generation 244 materialized, normalized, reconciled and activated exactly
  `4 133 / 4 133` rows. Code hashes match local, checkout and live containers.
- `/health` and `/ready` passed, PostgreSQL and Redis are available, `web` is
  healthy, `auction_worker` is running, and both `auctions` and `search` queues are
  empty.

Rollback: restore the two Python files, downgrade one Alembic revision, rebuild
`web` and `auction_worker`, or restore the pre-migration dump if required.

## 2026-09-01 - Normalize document right types before contradiction decisions

User-visible risk: the optional LLM sometimes returned a cited legal right as
free text (for example, `временное возмездное землепользование`) while the lot
card and deterministic extractor used the canonical value `lease`. The legal
passport compared those representations literally and displayed a false
official-document contradiction.

Fix:

- Document right-type candidates are now normalized to the bounded
  `lease`/`ownership` vocabulary in Russian and Kazakh before reconciliation.
- Unknown free-text values are ignored rather than admitted as a new legal-right
  enum value. Exact citations and provenance remain attached to accepted facts.
- A real ownership-versus-lease disagreement still remains a conflict; an
  extractor conflict marker caused only by equivalent free text no longer forces
  a red flag.

Verification:

- The test-first regression failed before the change, then the focused legal
  passport/decision-input/DD analysis suite passed (`29 passed`); Ruff passed.
- Alembic head/current both remained `d1e2f3a4b5c6`. Only `web` and
  `auction_worker` were rebuilt/recreated; backup:
  `/opt/land-scout/backups/code-20260901_034830`.
- Local, production-checkout and both live-container hashes matched
  (`607cac7c...`). `/ready` confirmed PostgreSQL and Redis, the web container was
  healthy, and the auctions queue was empty.
- Production read-back across all `197` active land lots reduced right-type
  conflicts from `26` to `13`, while all `197` retained cited document candidate
  evidence. The remaining conflicts are materially different canonical rights,
  not free-text aliases.

Rollback: restore `app/auction_legal_passport.py` from the narrow backup and
rebuild only `web` and `auction_worker`.

## 2026-09-01 - Detect document contradictions without depending on the LLM

User-visible risk: when the optional local LLM was unavailable or returned a
single explicit `conflict`, rule-extracted contract terms could remain usable as
ordinary candidates even when they disagreed with the official lot card. The
contract coverage gate only consumed the structured conflict list, so a
single-model conflict could also be lost there.

Fix:

- Rule and LLM candidates are now deterministically reconciled against the
  bounded official lot context for right type, lease term and documented payment
  fields.
- A differing document value is preserved with its exact citation but marked
  `conflict`; the structured conflict contains both the lot-card and document
  values, so contract coverage remains incomplete pending review.
- A grounded single candidate explicitly marked `conflict` is no longer omitted
  merely because there is no second document candidate.
- Matching values remain non-conflicting, and additive obligations/termination
  grounds are still not treated as contradictions just because their wording
  differs.

Verification:

- Test-first document/LLM/writer/legal-passport/decision-input suite passed
  (`65 passed`) and Ruff passed after formatting the test import.
- Only `auction_worker` was rebuilt/recreated; backup:
  `/opt/land-scout/backups/code-20260901_033241`.
- Local, production-checkout and live-container SHA-256 hashes matched for both
  deployed files (`024869ba...` extractor, `be5070f...` LLM bridge).
- Live-container read-back extracted a 5-year term against the official 10-year
  context as candidate `conflict` with values `(10, 5.0)`; the worker returned
  ready, `/ready` confirmed PostgreSQL and Redis, and the auctions queue was `4`.

Rollback: restore both Python files from the narrow backup and rebuild only
`auction_worker`.

## 2026-09-01 - Preserve higher-priority EGKN geometry during Jerler refresh

User-visible risk: a later Jerler source-object refresh could replace an already
verified EGKN map point and mark all calculated surroundings stale solely because
of provider processing order. This also hid a material disagreement when the two
published parcel centers were far apart.

Fix:

- Jerler remains official evidence, but it can no longer overwrite a geo-check
  whose boundary source is EGKN.
- When both sources expose usable coordinates and their parcel centers differ by
  more than 100 metres, source-object evidence records an explicit geometry
  conflict while preserving the cadastral boundary and existing GIS metrics.
- Matching/nearby source centers do not create a contradiction merely because two
  official sources published geometry.

Verification:

- Test-first regression reproduced the overwrite, then the focused
  identity/boundary suite passed (`26 passed`) and Ruff passed.
- Production currently has `3 385` Jerler-linked lots and `112` of those already
  carry an EGKN boundary protected by this precedence rule.
- Only `auction_worker` was rebuilt/recreated; backup:
  `/opt/land-scout/backups/code-20260901_010120`.
- Local, production-checkout and live-container code paths loaded successfully;
  the deployed file SHA-256 was `317195720e2ec520c4f12f8111d37d84d72ae7965a30d7e8d57c2c7a6379cd37`.
  The worker returned ready, the auctions queue remained empty, and `/ready`
  confirmed PostgreSQL and Redis.

Rollback: restore `app/auction_object_enrichment.py` from the narrow backup and
rebuild only `auction_worker`.

## 2026-09-01 - Reject malformed Jerler parcel boundaries before verification

User-visible risk: an open or otherwise malformed polygon from a source-object
card could still produce a plausible centroid and mark the lot boundary as
`verified`, even though the canonical land-object store correctly rejected that
same geometry.

Fix:

- Exposed the bounded canonical GeoJSON boundary validator for ingestion paths.
- Jerler enrichment now validates Polygon/MultiPolygon structure, closure,
  coordinate bounds, non-degenerate area and payload size before writing a
  geo-check or claiming a verified contour.
- Invalid published geometry remains preserved in evidence but is explicitly
  marked as a source conflict with `rejected_invalid_boundary`; it cannot seed
  coordinates, stale downstream GIS metrics, or the canonical boundary.

Verification:

- Focused enrichment/boundary suite passed (`23 passed`) and Ruff passed.
- Only `auction_worker` was rebuilt/recreated; code backup:
  `/opt/land-scout/backups/code-20260901_001818`.
- Production container hashes matched the local artifacts, the live boundary
  validator accepted a closed parcel and rejected an open ring, `/ready`
  remained healthy, and the auctions queue continued draining (`36` to `31`).

Rollback: restore the two Python files from the narrow code backup and rebuild
only `auction_worker`.

## 2026-08-25 - Recover lost durable provider workflow continuations

User-visible symptom: the E-Qazyna archive backfill stopped progressing although
its durable history run and pending/deferred units remained in PostgreSQL.

Confirmed production evidence:

- History run `887d2e463614400db8f1b4b8b00203d0` remained active with `28`
  child workflows but only `8` completed.
- Many workflow cursors were pending or deferred with retry times already in the
  past after E-Qazyna rate limiting.
- Beat recovered provider-run outbox actions, but did not wake due provider
  workflow cursors when their broker continuation disappeared.

Fix:

- Added a bounded indexed query for due pending/deferred/error workflows and
  expired processing claims belonging to live provider runs.
- Added a once-per-minute Beat recovery task that republishes each due workflow
  through the existing Redis continuation gate, preserving duplicate
  suppression and exact cursor identity.
- No archive row is treated as complete merely because it was queued; run and
  workflow counters remain authoritative.
- Object-history attempts now suppress protocol amounts and sale/start ratios
  when the official result is failed or nullified; these values no longer
  contaminate repeat-lot price statistics.
- Normalized comparables now exclude every publication of the target official
  object, using the same identity hierarchy: land-object ID, stable object URL,
  then a complete cadastral number. A relisted target parcel is no longer
  counted as its own market comparable.
- New history runs skip already persisted E-Qazyna lot IDs before enqueueing
  detail requests, so the bounded per-run detail budget advances into unseen
  archive records instead of being consumed by the same first pages again.
  Existing rows missing `published_at` are intentionally refreshed until the
  parser repairs their publication date.
- Fixed publication-date parsing for the live phrase containing `веб-портале`;
  the old regex stopped at that internal hyphen and produced zero dates.
- Archive windows now start at 2019, the earliest year observed in production,
  and Beat reseeds the bounded incremental history run hourly.
- Disabled the legacy official-request generator POST route with HTTP 410 to
  enforce the product decision: users contact authorities themselves and
  Zhertap only stores/analyzes received responses.
- Fixed the lot decision controls to submit valid pipeline stages (`checking`,
  `watching`, `skipped`) instead of rejected non-existent values.

Verification:

- Manual-check uploads now validate PDF/JPEG/PNG magic bytes before persistence;
  mislabeled HTML-as-PDF is rejected without creating a pipeline or file.
- Uploading a received response through the manual-check flow creates an
  owner-scoped received-response record, queues OCR/candidate extraction, and
  renders page/section/quote/confidence candidates in the lot card as requiring
  manual confirmation.
- The empty decision workspace now presents an explicit `Начать проверку`
  action and starts in the valid `checking` stage.
- Focused store/task regressions cover due, future, complete and expired claim
  states plus Redis-gated scheduling.
- Object-history regression verifies that a nullified protocol price is not
  exposed as a sale or included in sale/start averages.
- Production read-back and archive progress are recorded after deployment.

Rollback/follow-up: revert the recovery task/store query and restart only
`auction_worker` and `beat`. Publication-date coverage and canonical land-object
identity remain separate archive data-quality work.

## 2026-08-21 - Idempotent auction workflow and document continuations

User-visible symptom: the `auctions` Celery queue accumulated duplicate provider
workflow and document-extraction continuations while the source worker was rate
limited or processing a long-running document.

Confirmed production evidence:

- The queue reached `1517` messages, including `688` sampled
  `sync_provider_workflow` messages and `185` sampled document extraction tasks.
- A busy document-extraction singleton unconditionally scheduled another retry,
  while successful extraction also scheduled a short continuation.
- Provider workflow continuations likewise had no broker-level idempotency gate.

Fix:

- Added Redis-gated continuation scheduling for provider workflows.
- Duplicate provider messages now finish as `duplicate_suppressed` without
  claiming or processing another unit.
- Added the same single-continuation gate to document extraction; Beat and busy
  lock retries no longer multiply the queue.
- Only `auction_worker` and `beat` were rebuilt/restarted; web, bot, database and
  Redis were not restarted.

Verification:

- Local `ruff` passed.
- Provider/document regression tests passed (`10 passed`).
- Production queue fell from `1517` to `343`, then to `39` after the gate was
  deployed and old duplicates were suppressed.
- Production `/ready` remained healthy with database and Redis checks passing.
- Historical E-Qazyna backfill task was accepted as
  `e1ce416b-783e-4c51-a1ce-447d288e0495`; its history run is active with 28
  workflows, several deferred by provider rate limits.

## 2026-08-21 - Reject HTML responses saved as auction PDFs

User-visible symptom: some documents marked as downloaded did not open as PDF.

Confirmed production evidence:

- Of `3881` downloaded rows declared as PDF, `3210` files did not contain a
  `%PDF-` signature.
- The first bytes of the bad files were HTML (`<!DOCTYPE html>`), typically an
  access/rate-limit page returned by the external source.
- The files were nevertheless saved with a `.pdf` suffix and marked
  `storage_status=downloaded`.

Fix:

- The downloader now validates PDF magic bytes before writing downloaded
  metadata.
- Non-PDF responses are marked `failed` and never become available through the
  local PDF endpoint.
- Existing invalid rows were quarantined by clearing `local_path`, hash and
  download timestamp; they are eligible for controlled re-download.
- A PostgreSQL backup was created before the bulk metadata correction.

Verification:

- Local PDF validation and Auctions v2 tests passed (`91 passed`).
- After deployment, the latest `100` downloaded PDFs had `bad_pdf=0`.
- The re-download queue is progressing; `3110` previously invalid rows remain
  to be retried under provider rate limits.
- Production `/ready` remains healthy.

## 2026-08-20 - Search recovery redispatches lost queued requests

User-visible symptom: a web cabinet search stayed at 45% with the message `Публичный сервис osm_overpass временно ограничил запросы; повтор через 30 сек.`, but it did not continue after the retry window.

Confirmed production evidence:

- Request `16eb7c77-266d-4407-bdc9-2875a0a682aa` was still `queued`, progress `45`, and had the OSM backpressure retry message.
- Redis queue `critical` had length `0`.
- Celery `scheduled` and `reserved` did not contain `land_scout.process_search` for that request.
- Docker showed recent restarts for `redis`, `db`, and worker services, so the most likely failure mode was a lost ETA retry message while the database still kept the search in `queued`.

Fix:

- `land_scout.recover_stale_searches` now also finds `queued` search requests older than 5 minutes and redispatches `land_scout.process_search`.
- Existing idempotency remains: `process_search()` locks the row and exits if the status is no longer `queued`, so duplicate broker messages should not reprocess an already running or completed request.

Operational action taken:

- The stuck request was manually redispatched once with `process_search_task.delay(...)`.

Verification:

- Added regression coverage in `tests/test_recovery.py` for old queued requests.

Follow-up:

- If this repeats, inspect `critical` length, Celery scheduled/reserved, and `search_requests` rows where `status='queued'` and `updated_at < now() - interval '5 minutes'`.
- Watch for excessive ETA tasks from auction provider workflows, because large scheduled backlogs make Celery inspection noisy and can hide search retries.

## 2026-08-20 - Fresh searches no longer exclude previously delivered coordinates

User-visible symptom: a repeated Arshalyn district web search showed `100%` and `0` results even though an earlier same-day search for the same profile had delivered candidates.

Confirmed production evidence:

- Request `0a05aa6c-925a-41aa-bc86-ac4014a95a22` was a fresh request (`continuation_of_request_id` empty, `batch_number=1`) for Arshalyn district, LPH new search, household, irrigated, `0.15` ha.
- It finished as `ready`, `search_outcome=no_candidates`, with zero saved candidates.
- `delivered_coordinates()` still returned 10 previously delivered coordinates for that fresh request, so the engine treated it like a continuation batch and filtered old results away.

Fix:

- `delivered_coordinates()` now returns an exclusion list only for continuation requests created through the next-batch flow.
- A normal new analysis can show the same best candidates again. The next-batch flow still suppresses previously delivered coordinates.

Verification:

- Added regression coverage in `tests/test_free_preview.py` for fresh searches versus next batches.

## 2026-08-20 - Genplan blocked candidate preview

User-visible change: when all candidates fail the official genplan/PDP check, the search detail page shows `Показать без проверки генплана`.

Reason:

- Users need to verify that the cadastral/open-data candidate was found, while still seeing that it is not a valid submission candidate because the official digital urban-plan layer did not confirm it.

Fix:

- Normal `candidates` remains filtered to approved candidates only.
- The status API returns a separate `genplan_preview_candidates` list for blocked candidates.
- The frontend toggles into preview mode and marks those cards as not confirmed by genplan/PDP.

Verification:

- Added regression coverage in `tests/test_cabinet_genplan_map.py`.
- Production `web` was rebuilt and `/ready` returned healthy.

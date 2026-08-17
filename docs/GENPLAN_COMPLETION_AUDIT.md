# Genplan/PDP Completion Audit (historical)

Last checked: 2026-08-04

> This file preserves the 2026-08-04 gap analysis. The canonical current
> status is `docs/GENPLAN_STATUS_2026_08_17.md`. On 2026-08-17 all 641 manual
> candidate points were reviewed (`queued = 0`); production had 426 layer rows,
> including 90 active strict rows in 30 scopes.

Historical correction after the 2026-08-04 production verification:

- Production has `396` rows in `urban_plan_layers`.
- `90` rows are active `VERIFIED_STRICT/search` rows that affect customer search.
- `306` rows are inactive/shadow/QA and must not affect customer search.
- Active rows are split as `LPH` 3 rows, `LPH:household` 69 rows and
  `Gardening` 18 rows.
- The latest operational status is in `docs/GENPLAN_STATUS_2026_08_17.md`.
- Genplan-first search is implemented: approved allowed polygons can restrict
  the EGKN search area before parcel loading.
- Broad metadata layers that do not spatially cover the selected candidate
  points must not block the whole request; they are treated as unavailable for
  that specific request.

## Коротко по-русски

Этот раздел описывает исторический снимок на 04.08.2026. Он не является
текущим отчетом о ручной очереди. На 17.08.2026 операторская работа закрыта:
641 из 641 точек проверена, `queued = 0`. При этом 100% автоматического
покрытия всех населенных пунктов Казахстана по-прежнему нет; это отдельная
задача расширения источников.

Что реально готово:

- строгий автоматический проверяющий модуль есть;
- на production есть 378 строк слоев генплана;
- из них 72 строки реально включены в клиентский поиск;
- это 24 активные группы, где проверяются `allowed`, `prohibited` и `red_line`;
- есть 404 найденных источника генпланов;
- есть ручная библиотека 142 PDF/JPG/PNG/TIF генпланов.

Что не готово:

- 306 строк слоев лежат в `WARNING/shadow` и не участвуют в поиске клиентов;
- 81 источник AIS GGK найден, но еще не импортирован в строгий поиск;
- 205 Smart GeoHub источников только найдены в каталогах;
- PDF/JPG генпланы сейчас являются ссылками для ручной сверки, а не
  автоматической проверкой;
- для PDF/JPG не хватает завершающего шага: геопривязка -> векторизация зон ->
  независимый QA -> импорт как `VERIFIED_STRICT/search`.

Главный вывод: мы не зря двигались, но задача именно "докончить генпланы" еще
не закрыта. Нельзя просто подключить "считыватель PDF" и честно сказать, что
генплан проверен. PDF/JPG сначала нужно превратить в координатный слой:
разрешенные зоны, запретные зоны и красные линии. Без этого система может только
дать клиенту кнопку "открыть карту генплана для ручной проверки".

Рабочее направление дальше:

1. Сначала продвигать уже готовые shadow/vector слои: это самый быстрый путь,
   потому что геометрия уже есть.
2. Потом запускать конвейер по ручным PDF/JPG: `genplan_pipeline`,
   `genplan_autoreg`, `genplan_workbench`, затем `genplan_vectorize`.
3. В интерфейсе четко разделять три статуса: "проверено автоматически",
   "есть карта для ручной сверки", "генплан не подключен".

## Short Answer

The user concern is valid: genplans are not finished as a nationwide automatic
checker.

2026-08-04 update: production now also checks gardening against strict/search
urban-plan layers in six regional Smart GeoHub scopes. Standard LPH 10 sotok
does not need a separate layer family: code maps it to the same LPH household
scope that is already used for regular LPH household checks. Gardening remains
purpose-specific and does not reuse LPH layers.

## Local Bbox Audit - 2026-08-03

The manual raster/PDF library is now an operable georeferencing queue:

| Metric | Count |
| --- | ---: |
| Workbench records audited | 175 |
| Bbox resolved | 175 |
| Bbox unresolved | 0 |
| EGKN bbox source | 85 |
| Static city/district bbox fallback | 90 |
| Nominatim bbox source | 0 |

Aktau identity note: the wrong West Kazakhstan/ZKO copy is excluded from automatic processing; the Mangystau Aktau copy is kept as the canonical manual record for operator georeferencing.

New local artifacts:

- `C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4\summary.json`
- `C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4\records.csv`
- `C:\Users\medadmin\Documents\Codex\genplan\work\operator-queue.csv`
Important: bbox resolved means "the operator workbench can open the correct
map area". It is not the same as a reviewed, vectorized, automatically checked
genplan layer.

The project currently has three different things that must not be mixed:

1. **Automatic genplan check** - works only when an official digital/vector
   layer is imported into `urban_plan_layers` and approved for search.
2. **Manual genplan document** - PDF/JPG/PNG/TIF is found and shown to the user
   as a manual reference, but it does not automatically check the candidate.
3. **Discovered source / shadow layer** - a portal layer or generated release
   exists, but it is not trusted enough to affect customer results yet.

Only item 1 can honestly be shown as "genplan checked automatically".

## Production Snapshot

Production server: `<production-host>`

Production database snapshot from 2026-08-04 after gardening strict import:

| Metric | Count | Meaning |
| --- | ---: | --- |
| `urban_plan_layers` total | 396 | All imported layer rows |
| Active strict search layers | 90 | Rows that affect real customer searches |
| Active strict LPH rows | 72 | LPH / LPH household rows used in search |
| Active strict gardening rows | 18 | Gardening rows used in search |
| Shadow / QA / inactive layers | 306 | Stored, but not used in search |
| `urban_plan_sources` total | 404 | Discovered source records |
| Imported sources | 23 | Sources already imported |
| Coverage cache rows | 30 | Areas already checked for layer availability |

2026-08-04 gardening update:

- Built strict/search copies of six previously stored official Smart GeoHub
  gardening releases.
- Dry-run passed for all six releases.
- Imported 18 active `VERIFIED_STRICT/search` rows for `Садоводство`.
- Deleted 13 stale `urban_plan_coverage` cache rows for `Садоводство` so new
  searches recompute coverage against the active gardening layers.
- Taldykorgan GGK gardening remains shadow because its review still records
  `legal_act_verified=false`, `random_visual_samples_verified=false` and
  `zone_mapping_verified=false`.

Active strict gardening groups:

| Scope | Purpose |
| --- | --- |
| Акмолинская область, all districts/localities | `Садоводство` |
| Алматинская область, all districts/localities | `Садоводство` |
| Жетысуская область, all districts/localities | `Садоводство` |
| Западно-Казахстанская область, all districts/localities | `Садоводство` |
| Туркестанская область, all districts/localities | `Садоводство` |
| Улытауская область, all districts/localities | `Садоводство` |

Source status in production:

| Platform | Coverage status | Import status | Count |
| --- | --- | --- | ---: |
| `ggk_wfs` | `digital_found` | `not_imported` | 81 |
| `smart_geohub` | `catalog_found` | `not_imported` | 205 |
| `smart_geohub` | `catalog_found` | `shadow_imported` | 41 |
| `smart_geohub` | `geometry_found` | `not_imported` | 34 |
| `smart_geohub` | `geometry_found` | `shadow_imported` | 9 |
| `smart_geohub` | `imported` | `imported` | 23 |
| `smart_geohub` | `no_features` | `not_imported` | 11 |

Layer status in production:

| QA status | Search active | Count |
| --- | --- | ---: |
| `VERIFIED_STRICT` | yes | 72 |
| `WARNING` | no, shadow only | 306 |

## Areas Currently Used In Automatic Search

Each ready group normally has three rows: `allowed`, `prohibited`, `red_line`.

Current active automatic groups:

| Scope | Purpose |
| --- | --- |
| Акмолинская область, all districts/localities | `ЛПХ:household` |
| Актюбинская область, г.Актобе | `ЛПХ:household` |
| Алматинская область, all districts/localities | `ЛПХ:household` |
| Атырауская область, г.Атырау | `ЛПХ:household` |
| г.Астана | `ЛПХ:household` |
| г. Шымкент | `ЛПХ` |
| Жамбылская область, г.Тараз | `ЛПХ:household` |
| Жетісу облысы, all districts/localities | `ЛПХ:household` |
| Западно-Казахстанская область, all districts/localities | `ЛПХ:household` |
| Карагандинская область, all districts/localities | `ЛПХ:household` |
| Карагандинская область, г.Шахтинск | `ЛПХ:household` |
| Костанайская область, all districts/localities | `ЛПХ:household` |
| Костанайская область, г.Аркалык | `ЛПХ:household` |
| Костанайская область, г.Костанай | `ЛПХ:household` |
| Костанайская область, г.Лисаковск | `ЛПХ:household` |
| Костанайская область, г.Рудный | `ЛПХ:household` |
| Костанайская область, г.Тобыл | `ЛПХ:household` |
| Костанайская область, Житикаринский район / г.Житикара | `ЛПХ:household` |
| Кызылординская область, all districts/localities | `ЛПХ:household` |
| Мангистауская область, all districts/localities | `ЛПХ:household` |
| Павлодарская область, г.Павлодар | `ЛПХ:household` |
| Северо-Казахстанская область, г.Петропавловск | `ЛПХ:household` |
| Туркестанская область, all districts/localities | `ЛПХ:household` |
| Улытауская область, all districts/localities | `ЛПХ:household` |

This is good progress, but it is not full Kazakhstan coverage and it is mainly
for the household LPH profile.

## Manual PDF/JPG Library

Local source folder:

`C:\Users\medadmin\Documents\Codex\genplan`

Local file inventory:

| Extension | Count |
| --- | ---: |
| `.jpg` | 656 |
| `.png` | 4978 |
| `.pdf` | 39 |
| `.json` | 534 |
| `.geojson` | 6 |
| other files | 1080+ |

Application manual manifest:

`app/data/manual_genplans.json`

Current manifest summary:

| Metric | Count |
| --- | ---: |
| Manual records | 142 |
| Regions covered by manual records | 14 |
| JPG records | 110 |
| PDF records | 23 |
| JPEG records | 4 |
| PNG records | 4 |
| TIF records | 1 |

Manual records are useful, but they are only links/files for human checking.
They do not create `passed` or `blocked` decisions in `evaluate_urban_plan()`.

## What The Code Actually Checks

Automatic checking is implemented in:

- `app/providers/urban_plan.py`
- `app/models.py` -> `UrbanPlanLayer`, `UrbanPlanSource`, `UrbanPlanCoverage`
- `tools/genplan_import`

The search layer must pass this gate:

- `active=true`
- `approved_for_search=true`
- `provenance_status='verified_official'`
- `identity_status='matched'`
- `qa_status in ('STRICT', 'VERIFIED_STRICT')`
- `independent_review=true`
- `source_sha256 is not null`

Then `evaluate_urban_plan()` checks:

- whether the candidate square is fully covered by an `allowed` layer;
- whether it intersects `prohibited`;
- whether it intersects `red_line` with configured buffer.

If no approved layer exists, current production behavior is auto-waive:
the result is delivered as preliminary and marked as not automatically checked
against genplan/PDP.

## Existing Tools For Finishing PDF/JPG Genplans

The project already contains the right building blocks:

- `tools/genplan_pipeline` - inventories and normalizes manual assets.
- `tools/genplan_autoreg` - tries to propose georeferencing from raster plan to
  OSM/ArcGIS basemap.
- `tools/genplan_workbench` - local operator UI for manual control points.
- `tools/genplan_vectorize` - creates candidate `allowed`, `prohibited` and
  `red_line` GeoJSON from a reviewed georeferenced raster by color rules.
- `tools/genplan_import` - safe importer for strict vector releases.
- `tools/genplan_ggk`, `tools/genplan_wfs`, `tools/genplan_export`,
  `tools/genplan_review`, `tools/genplan_shadow` - source discovery, release
  and QA helpers.

The missing part is not "a PDF reader". The missing part is running the
production pipeline that turns a PDF/JPG into a verified geospatial layer.

## Why A PDF Reader Alone Is Not Enough

A normal PDF/JPG genplan is a picture. It usually does not know:

- which pixel equals which coordinate;
- which color or legend item means LPH/sadovodstvo/forbidden zone;
- where red lines are in vector form;
- whether the scanned sheet is shifted, rotated or stretched;
- whether the document is the latest official version.

Therefore a PDF/JPG can be used automatically only after these steps:

1. Render PDF page or load JPG/PNG/TIF.
2. Georeference it with control points.
3. Verify georeference with independent checkpoint points.
4. Extract/vectorize allowed zones, prohibited zones and red lines.
5. Match legend semantics to product profiles.
6. Run independent QA.
7. Import as `VERIFIED_STRICT/search`.

Without this, the product may show a manual reference, but must not say
"genplan checked automatically".

## Concrete Completion Plan

### Phase 1 - Truthful Status Everywhere

Show exactly one of these statuses in web and Telegram:

- `Генплан проверен автоматически` - only for active strict layers.
- `Есть карта генплана для ручной сверки` - manual PDF/JPG exists, but no strict
  layer is active.
- `Генплан не подключен` - neither strict layer nor manual file exists.

This prevents users from thinking that a geoportal link or Adilet text is the
same as a checked genplan.

### Phase 2 - Promote Existing Shadow Layers

Work through the 306 `WARNING` rows:

1. For every shadow group, inspect `review.json`, `provenance.json`,
   `allowed.geojson`, `prohibited.geojson`, `red_line.geojson`.
2. Compare with official portal/map.
3. If correct, rebuild as `VERIFIED_STRICT/search`.
4. Import with `tools/genplan_import`.

This is the fastest way to expand automatic checking because those layers are
already vector data.

### Phase 3 - Process Manual PDF/JPG Library

For each important locality without vector source:

1. Run `tools/genplan_pipeline` over the manual library.
2. Run `tools/genplan_autoreg` to get proposed control points where possible.
3. Use `tools/genplan_workbench` to place/verify GCP manually.
4. Vectorize three candidate outputs with `tools.genplan_vectorize`:
   - `allowed.geojson`
   - `prohibited.geojson`
   - `red_line.geojson`
5. Review and import only after strict QA.

This is real work per city/locality. It cannot be safely solved by one generic
"read PDF" function.

### Phase 4 - Admin Queue

Add an admin genplan queue with statuses:

- `source_found`
- `manual_file_ready`
- `georeference_needed`
- `georeferenced`
- `vectorized`
- `qa_pending`
- `search_ready`
- `rejected`

This gives a visible operating process instead of hidden partial work.

## Current Verdict

The user is right to push here.

Genplans are partially implemented:

- automatic check exists and is safely gated;
- 24 active search groups are live;
- many sources and shadow layers are collected;
- manual PDF/JPG library exists;
- tools for georeferencing and import exist.

But the project is not complete until shadow layers are promoted and the manual
PDF/JPG library is processed into verified geospatial releases. Until then, the
system must clearly mark many results as preliminary/manual-genplan-check.

## Local Progress On Manual PDF/JPG Pipeline - 2026-08-03

New local tooling was added to make the remaining genplan work measurable:

- `tools.genplan_status` builds `status-report.json` and `status-report.csv`
  for the manual map library.
- `tools.genplan_pdf_prepare` renders only one-page PDF genplans into PNG files
  and skips multi-page PDFs because selecting the correct page is an operator
  decision.
- `tools.genplan_vectorize` turns a reviewed georeferenced GeoTIFF/COG into
  candidate `allowed.geojson`, `prohibited.geojson`, and `red_line.geojson`
  files by explicit color rules.

Historical queue result after initial PDF preparation and batch preprocessing:

- manual map records: 142;
- `manual_georeference_required`: 130;
- `pdf_page_selection_required`: 6;
- `duplicate_manual_file`: 5;
- `identity_review_required`: 1.

This intermediate state is superseded by `tools.genplan_pdf_page_select` and
`readiness-v7`: selected/split PDF page rendering now leaves 0
`pdf_page_selection_required` tasks. The remaining real production work is
operator georeferencing in `tools.genplan_workbench`, then
`tools.genplan_export`, `tools.genplan_vectorize`, independent QA, and finally
`tools.genplan_import`.

Useful local files:

- `C:\Users\medadmin\Documents\Codex\genplan\work\status-report.json`
- `C:\Users\medadmin\Documents\Codex\genplan\work\status-report.csv`
- `C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-renders\manifest.json`
- `C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-autoreg\summary.json`
- `C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json`

Update from the next local pass:

- the focused workbench manifest now contains 175 operator-ready records;
- skipped records are down to 0;
- the previous TIFF gap is closed by server-side TIFF-to-PNG rendering in
  `tools.genplan_workbench`;
- 6 multi-page PDF records have contact sheets in
  `C:\Users\medadmin\Documents\Codex\genplan\work\pdf-contact-sheets`;
- `tools.genplan_status` now preserves `duplicate_of` and `queue_reasons`, so
  duplicate and identity-review tails are auditable.
- `tools.genplan_embedded_scan` checked 119 raster files for embedded
  georeferencing: 0 usable embedded CRS/GCP/transform records and 0 world-file
  sidecars were found.
- `tools.genplan_operator_queue` exported
  `C:\Users\medadmin\Documents\Codex\genplan\work\operator-queue.csv` with
  direct workbench URLs, contact-sheet paths, duplicate targets, and reasons.

The local workbench was restarted on `http://127.0.0.1:8765` with
`C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json`.

Update from 2026-08-03 evening:

- `tools.genplan_workbench` now uses the same bbox resolver chain as the audit:
  EGKN, static city/district reference, then Nominatim fallback.
- `tools.genplan_workbench_queue` now merges
  `C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4\records.json`
  into the workbench manifest.
- Current local workbench manifest: 175 records, 0 skipped.
- Bbox status inside the manifest: 175 `resolved`, 0 `unresolved`.
- Bbox sources: 85 EGKN, 90 static city/district reference, 0 Nominatim.
- The previous Aktau jurisdiction conflict is resolved by excluding the wrong ZKO copy and keeping the Mangystau copy as canonical.
- The workbench UI now has map-area counters and filters for `Map area found`
  and `Map area needs review`.
- `tools.genplan_readiness` was added as the local control report for the
  manual PDF/JPG/TIFF pipeline.
- `tools.genplan_pdf_page_select` was added for selected/split multi-page PDF
  rendering. The current local selections render Kaskelen page 22, Semey page
  19, Kyzylzhar/Roshchinsky pages 1-5, and Almaty PDP pages 1-39.
- The current run at
  `C:\Users\medadmin\Documents\Codex\genplan\work\readiness-v7` produced:
  175 total workbench records after PDF page splitting, 175 bbox resolved, 0 bbox review, 0 PDF page selections, and 175 A1 GCP tasks. `records.csv`
  includes direct local workbench URLs.
- `tools.genplan_batch` actual run at `C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v1` completed all 175 current workbench records, wrote diagnostics/proposed artifacts, reported 0 pipeline-error assets after the large-JPEG downsample fix, and kept all 175 at `needs_manual` with `qa_or_strict_automatic=false`.
- `C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v1` stores `attempts.csv`, `reason-counts.csv`, `summary.json`, and `operator-priority.csv`. No attempt produced safe proposed GCPs; the dominant rejection reasons are ill-conditioned homography, low inlier ratio, high reprojection error, and small reference coverage.

Update from 2026-08-04 v2 local pass:

- `tools.genplan_batch` resumed and completed the full current workbench set at
  `C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v2`.
- Batch summary: 175 selected records, 175 completed, 0 failed, 0 pipeline-error
  assets, 0 pipeline-error attempts, 27 reused from the interrupted v2 run, and
  all 175 still `needs_manual`.
- `tools.genplan_autoreg_diagnostics` produced
  `C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v2`:
  175 assets, 350 attempts, 0 safe proposed GCP attempts, and 12
  operator-only diagnostic anchor attempts.
- Diagnostic anchors are not approved GCPs. They are visual hints for the
  operator and are guarded with `customer_eligible=false`, `import_eligible=false`
  and `auto_apply=false`. They must not be used in customer checks until A1
  georeferencing, independent A2 review, export/vectorization and import are
  complete.
- The local workbench now has a `Diagnostic anchors` queue filter and a
  `Load N anchors` button on matching records. This turns diagnostic hints into
  draft workbench GCP rows so the operator can verify/move/delete them instead
  of starting A1 from an empty page. These rows are still unapproved until saved,
  reviewed, exported and imported through the normal QA path.
- `tools.genplan_seed_diagnostic_gcps` was added and run against the current
  local manifest/output. It seeded 12 draft `gcps.json` records from diagnostic
  anchors and a repeat run skipped all 12 existing drafts, confirming idempotent
  behavior. The local workbench API now reports 12 records with saved draft GCPs.
- The local workbench manifest was rebuilt with
  `--autoreg-output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v2`.
  `tools.genplan_workbench` was restarted on `http://127.0.0.1:8765`.
- `C:\Users\medadmin\Documents\Codex\genplan\work\readiness-v8` reports:
  175 records, 175 resolved bboxes, and 175 `gcp_needed` tasks.
- `tools.genplan_operator_packs` generated local HTML packs for the top 50
  records at `C:\Users\medadmin\Documents\Codex\genplan\work\operator-packs-v2`.


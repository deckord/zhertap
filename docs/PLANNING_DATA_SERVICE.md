# Planning Data Service

Last updated: 2026-08-17

## Purpose

Land Scout should use urban-planning data as a prepared spatial service, not as
PDF files parsed during a user search. The service must answer one question
quickly and with evidence:

> Does this point or proposed plot have reliable planning coverage, and what
> official functional zones, PDP zones, red lines and restrictions intersect it?

The result is preliminary. The service must not claim that a plot can legally be
granted to a user. The correct product wording is:

> No preliminary conflict with connected urban-planning data was detected.

Not:

> The plot is suitable.

## Current Production Status

The manual candidate-review stage is complete: all `641` saved candidates have
been classified and `queued = 0`. Production contains `426` rows in
`urban_plan_layers`; `111` active and search-approved `VERIFIED_STRICT` rows
are used in `37` territorial/purpose scopes. The former `336` inactive rows
have all been resolved: `99` are `SUPERSEDED`, `216` are `REVIEWED_HOLD`, and
none remain as unreviewed `WARNING`.

The remaining legend and source queues are a backlog for expanding coverage,
not unfinished work on the 641 manually marked points. See
`GENPLAN_STATUS_2026_08_17.md` for exact counters, territory breakdown and
control queries.

## Search Order

The user search should become planning-first when a verified layer exists:

1. Normalize the requested profile: `LPH_HOMESTEAD`, `LPH_FIELD`, or `GARDENING`.
2. Ask Planning Data Service for allowed or potentially compatible planning
   zones inside the selected settlement or district.
3. Search vacant geometry from EGKN only inside those allowed planning zones.
4. Subtract registered parcels, red lines, prohibited functional zones, PDP
   constraints, public-use areas, roads, water and other mapped restrictions.
5. Use neighboring LPH or gardening parcels as a ranking and confidence signal,
   not as a hard requirement.
6. Use OSM and satellite/manual review as infrastructure and visual evidence,
   not as legal proof.

The current app already starts this transition in `app.live_search`: if an
approved `allowed` layer exists, the EGKN search area is restricted to that
geometry. From 2026-08-04, if no same-purpose EGKN anchor finds a candidate, the
search falls back to a genplan-first vacancy scan inside the approved allowed
zone.

## Coverage Statuses

Coverage is tracked per settlement or document scope:

- `OFFICIAL_VECTOR`: official WFS, ArcGIS REST, SHP, GeoJSON, GeoPackage, DXF/DWG
  or equivalent machine-readable spatial source.
- `OFFICIAL_RASTER`: official PDF/JPG/TIFF exists, but automatic spatial checks
  are not enabled.
- `MANUAL_VECTOR`: official raster was georeferenced, vectorized and reviewed.
- `UNVERIFIED_SOURCE`: a possible source exists but identity, currentness or
  geometry has not been verified.
- `DOCUMENT_EXISTS_NO_FILE`: an approval act is known, but no usable map file is
  available.
- `NO_DATA`: no current planning source is known.
- `OUTDATED`: only an obsolete document was found.
- `SUPERSEDED`: the document was replaced by a newer act.

Only `OFFICIAL_VECTOR` and reviewed `MANUAL_VECTOR` can become automatic search
layers. `OFFICIAL_RASTER` can be shown to the user for manual checking.

## Minimum Data Model

Recommended PostGIS tables:

- `planning_settlements`: normalized settlement identity, KATO, region, district,
  boundary, coverage status.
- `planning_documents`: document type, title, approval authority, approval
  document number, approval date, effective dates, source URL, source format,
  source hash, document status.
- `planning_layers`: document, layer type, geometry, source zone code, source
  zone name, normalized zone, source attributes, source page, confidence,
  verification status.
- `planning_restrictions`: document, restriction type, geometry, name, legal
  basis, confidence, verification status.
- `georeference_versions`: document, coordinate system, control points,
  `rmse_meters`, operator, reviewer, created date.
- `planning_checks`: request geometry hash, result, intersecting layers,
  restrictions, confidence, generated evidence.

`rmse_meters` is mandatory for raster-derived data. If the georeference error is
30-50 meters, a 10-25 sotok plot must not receive a confident automatic pass.

## API Needed by Land Scout

Initial internal endpoints:

```http
GET /v1/coverage?lat=51.8747&lon=70.9486
```

Returns whether a current verified planning layer covers the coordinate.

```http
POST /v1/check
```

Checks a proposed plot polygon against functional zones, PDP zones, red lines
and restrictions.

```http
POST /v1/intersections
```

Returns raw intersections for diagnostics and admin UI.

```http
GET /v1/documents/{document_id}
```

Returns source, approval act, approval date, version and source hashes.

```http
GET /v1/tiles/{z}/{x}/{y}
```

Returns vector tiles for map overlays in the web cabinet and operator UI.

Implemented in the monolith on 2026-08-04 as the first internal contract:

- `GET /api/planning/coverage`
- `POST /api/planning/check`
- `POST /api/planning/batch-check`

These endpoints use the existing internal `X-API-Key` guard. They read
`UrbanPlanLayer` and return `coverage_status`, `result`, `confidence`,
`documents`, `intersections` and `restrictions`. Shadow layers are hidden unless
the caller explicitly sends `include_shadow=true`. `batch-check` accepts up to
100 geometries per request so the caller can avoid 100 separate HTTP round trips.

## Compatibility Model

Do not hardcode one global rule such as `Ж-1 = LPH allowed`.

Use a normalization layer:

```json
{
  "source_zone": "Ж-1",
  "source_name": "Территория усадебной застройки",
  "normalized_zone": "LOW_RISE_RESIDENTIAL",
  "compatibility": {
    "LPH_HOMESTEAD": "POSSIBLE",
    "LPH_FIELD": "UNLIKELY",
    "GARDENING": "REQUIRES_REVIEW"
  },
  "legal_note": "Functional zoning is not a land-allocation decision."
}
```

The product may use `POSSIBLE` as an allowed search corridor only when the
document mapping and QA are approved for that city and profile.

## PDF and Raster Pipeline

Preferred source order:

1. WFS, ArcGIS REST, Smart GeoHub, GeoServer.
2. Official SHP, GeoJSON, GeoPackage, KML/KMZ, DXF/DWG, MapInfo.
3. Official city or regional geoportal.
4. State urban cadastre and NSDI/NIPD sources.
5. Official requests to akimats for machine-readable layers.
6. Developer/project institute source files, if reuse is legally permitted.
7. PDF/JPG/TIFF only as a fallback.

Raster path:

1. Store source file and hash.
2. Render PDF page to image.
3. Select map page and document metadata.
4. Georeference with control points.
5. Record RMSE and reviewer.
6. Vectorize or manually digitize zones.
7. Normalize zone labels and restrictions.
8. Run independent QA.
9. Publish as `MANUAL_VECTOR` only after review.

Implemented first pipeline step on 2026-08-04:

- `genplan_source_documents` stores every uploaded manual genplan/PDP source
  from `manual_genplans.json` with territory, file hash, detected format and
  processing status.
- `/admin/urban-plans`, block `PDF-конвейер генпланов`, starts the one-time
  ingestion/type-detection pass for the uploaded archive.
- PDF files are routed as `vector_pdf`, `raster_pdf` or `multi_page_pdf`.
  Raster images are routed directly to legend/color extraction. Missing files
  are recorded as `missing_file` instead of silently disappearing.
- This step does not publish any PDF-derived geometry to client search. It only
  prepares a clean queue for the next steps: legend extraction, segmentation,
  georeferencing, occupancy check and confidence routing.

Implemented second pipeline step on 2026-08-04:

- `genplan_legend_entries` stores draft colors per source document.
- `/admin/urban-plans`, block `PDF-конвейер генпланов`, can process the next
  waiting document one at a time and extract dominant raster colors or vector
  PDF fill colors. If direct PDF fill extraction finds nothing, the service
  renders a small copy of the PDF page and extracts colors from that image.
- Multi-page PDFs are not published automatically. The draft extractor picks
  the page with the strongest colored map signal and stores the page number in
  metadata for operator review.
- Every extracted color starts as `unknown` and `needs_review`. It is not used
  in client search until an operator maps it to an allowed/prohibited/red-line
  meaning and the resulting geometry passes QA.
- Internal API endpoints:
  - `GET /api/genplans/catalog` returns source documents, statuses and draft
    legend counts.
  - `GET /api/genplans/layers/geojson` returns active approved urban-planning
    layers as GeoJSON. Draft PDF-derived colors are intentionally excluded from
    this endpoint until they pass review, georeferencing and QA.
- Automatic legend pre-classification reduces manual work:
  - text-label matches for LPH/gardening/red lines/restricted zones can be
    approved automatically;
  - color-only guesses are conservative and remain `needs_review` unless they
    are obvious non-target colors that can be rejected as `ignore`;
  - LPH/gardening is never approved from color alone.
- PDF label enrichment reduces manual work for vector/text PDFs:
  - the service scans PDF drawing objects for small color swatches that look
    like legend items;
  - if a text line is immediately to the right or below the swatch, that text
    is saved as `label_ru`/`label_kz` evidence for the legend color;
  - the normal auto-classifier then uses the text, not the color itself, to
    approve obvious LPH/gardening/red-line/restricted classes;
  - raster JPG/PNG scans still need OCR/vision or operator review because they
    have no reliable text layer.
- AIS GGK is now the first operational source for digital layers:
  - `/admin/urban-plans` can refresh the official GGK catalog;
  - the same page can build and import inactive GGK shadow releases in small
    batches;
  - profiles are tried in this order: `lph-household`, `gardening`, `lph-field`;
  - imported GGK shadow layers are stored for review but are not enabled for
    strict client search until a verified search release is built.

## Pilot: Akkol

Workspace source:

- `genplan-sources/ggk-shadow-batch-pilot2/3617-lph-household`
- Scope: `Акмолинская область`, `г.Акколь`
- Document: `Генеральный план г. Акколь`
- Approval: `Решение маслихата №С 38-2`, `2011-05-23`
- Current release mode: `shadow`
- Allowed layer: `Территория усадебной застройки`
- Allowed feature count: `299`
- Local diagnostic import on 2026-08-04 created three inactive layers:
  `allowed`, `prohibited`, `red_line`. They are `active=false` and
  `approved_for_search=false`.

Control points inside the current shadow allowed layer:

- `51.992950, 70.930765`
- `52.002350, 70.945625`
- `51.986950, 70.982423`

Next Akkol tasks:

1. Rebuild or re-check the release source from AIS GGK or another official
   vector endpoint.
2. Verify current legal status: whether the 2011 act is still active or was
   replaced.
3. Review geometry bounds against an official base map.
4. Inspect at least 10 random allowed polygons visually.
5. Confirm prohibited zones and red lines are present and aligned.
6. Produce an independent review JSON with a reviewer different from the
   operator.
7. Import as `search` only if QA becomes `VERIFIED_STRICT`.
8. Run user-search smoke tests for `ЛПХ:household` in Akkol with and without
   neighboring LPH anchors.

Until those tasks are complete, Akkol must remain a shadow dataset and may be
used only for diagnostics and operator review.

Current local Planning API behavior for control point `51.992950, 70.930765`:

- `include_shadow=false`: `NO_DATA`, `MANUAL_REVIEW`
- `include_shadow=true`: `SHADOW_ONLY`, `MANUAL_REVIEW`, document
  `Генеральный план г. Акколь`

Admin candidate finder:

- Page: `/admin/urban-plans`, block `Найти места внутри зоны`.
- Input: settlement scope, requested use, grid step, restriction buffer and
  point limit.
- Output: candidate points inside the allowed urban-plan zone. When EGKN context
  is enabled, registered parcels are subtracted first, so the point means a
  calculated empty spot, not a house or an existing LPH parcel.
- The nearest EGKN parcel is saved only as an orientation point: cadastre number,
  distance and land-use text. It is not presented as the free parcel.
- Orientation is measured from the proposed small candidate footprint, not from
  a remote edge of a large empty polygon. A 7 ha empty spot may be promising,
  but the shown cadastral number must still be near the exact place opened in
  Google Maps.
- Orientation distance rule: up to `300 m` is a good nearby orientation point,
  `300-800 m` is weak but still shown, and anything farther than `800 m` must
  not be saved as `nearby_cadastre`. A parcel 4 km away is not "nearby"; the
  point may remain an empty genplan spot, but without a cadastral orientation.
- Each point links to Google Maps satellite view for operator review.
- Operator can save a manual review status for every point: empty, built, road,
  garden or unclear. Saved reviews are shown again for the same settlement and
  requested use, so Akkol triage does not restart from zero each time.
- The all-city queue on `/admin/urban-plans` creates saved `queued` candidate
  points for every territory that already has an allowed urban-plan layer. This
  turns the remaining genplan work into a checklist: open Google, mark the
  point, then promote only independently checked layers.
- The button `Подготовить следующий город с ЕГКН` processes one territory at a
  time with the heavier EGKN subtraction and orientation logic. Use it before
  visual review when you want the operator queue to contain better empty-spot
  candidates rather than plain grid points.
- This EGKN one-city queue is a strict first pass, not a full manual audit: it
  saves only up to two strong candidates where the requested-use cadastral
  orientation is within `300 m`. Weak candidates stay out of the operator queue
  until the strict pass is exhausted.
- One-by-one review lives at `/admin/planning-candidates/review-next`. It shows
  only the next queued point, links to Google satellite view, saves one status,
  then immediately opens the next point. This is the preferred operator flow;
  mass queue creation is allowed, mass visual approval is not.
- Current purpose: fast manual triage only. It does not certify that land is
  empty, unregistered, available for allocation or legally ready for client
  delivery.

## Scale Plan

For 100 simultaneous user requests, the app should not run full EGKN, OSM and
large GeoJSON checks inside only two Celery workers.

Target architecture:

- PostGIS spatial indexes for planning layers and cached allowed corridors.
- Batch endpoint for up to 100 plot checks per call.
- Redis cache for document coverage and repeated intersection checks.
- Separate queues for user search, planning import, auction crawl and QA jobs.
- Precomputed candidate tiles for high-demand settlements.
- Worker concurrency sized by CPU and IO, not one shared queue for everything.

Planning Data Service should start as an internal API. A public B2B API should
wait until at least 20-50 cities have stable reviewed coverage, documented
license status, versioning and legal disclaimers.

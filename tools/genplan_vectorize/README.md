# Genplan Vectorize

`tools.genplan_vectorize` is the missing bridge after manual georeferencing
(see `docs/GENPLAN_SOLUTION.md`, "Missing MVP module"). It takes a reviewed
georeferenced raster, usually a GeoTIFF/COG created by `tools.genplan_export`,
and an operator-approved legend, and produces three candidate GeoJSON layers:

- `allowed.geojson`
- `prohibited.geojson`
- `red_line.geojson`
- `manifest.json`

The output is never trusted automatically. `manifest.json` always declares
`"workflow_status": "proposed"` because this module does not approve
anything itself - it only segments colors. The candidate layers must still go
through independent QA (`tools.genplan_review`) and then
`tools.genplan_import` as a `VERIFIED_STRICT`/`WARNING` release before they
can affect customer search.

## Operator workflow

1. Operator opens the georeferenced raster and samples legend colors for the
   allowed/prohibited/red-line classes (the same per-color review that feeds
   `app.models.GenplanLegendEntry` in the admin legend-review screen).
2. Operator assigns `target_category` and `layer_kind` to each sampled color
   and sets `review_status="approved"` once satisfied.
3. Operator (or an export step) writes those rows out as `legend.json` using
   the schema below and passes it to this tool together with the exported
   raster.
4. This tool segments the raster by color with per-entry tolerance,
   polygonizes the result, and writes the three layers plus `manifest.json`.
5. Operator edits/simplifies geometry in QGIS or the workbench if needed.
6. An independent reviewer approves or rejects the candidate.
7. Only a release with `STRICT`/`VERIFIED_STRICT` (or an explicitly allowed
   `WARNING` shadow) is imported for search, through `tools.genplan_import`.

## `legend.json`

The legend format mirrors `app.models.GenplanLegendEntry` field-for-field, so
a legend reviewed in the app's admin screen can be exported into this file
without inventing a parallel schema. It is deliberately **not** the same
document as the `--legend` evidence JSON consumed by
`tools.genplan_review` (`genplan-legend-evidence/v1`, which records legend
*readability* and orientation QA, not per-color RGB mappings). Vectorization
needs the actual color-to-category rows; review QA needs the evidence
summary. Keep both.

```json
{
  "schema_version": "genplan-legend/v1",
  "record_id": "asset-123",
  "source_sha256": "64 lowercase hex characters of the raster passed via --source",
  "source_title": "Burabay official genplan sheet",
  "reviewer_id": "reviewer-a2",
  "reviewed_at_utc": "2026-08-01T09:00:00Z",
  "min_area_px": 32,
  "entries": [
    {
      "color_hex": "#f4d35e",
      "red": 244,
      "green": 211,
      "blue": 94,
      "source": "manual",
      "label_ru": "Территория усадебной застройки",
      "label_kz": "",
      "target_category": "lph-household",
      "layer_kind": "allowed",
      "confidence_score": 0.9,
      "review_status": "approved",
      "pixel_count": 184230,
      "notes": "",
      "tolerance": 12
    }
  ]
}
```

See `example_legend.json` for a full four-layer example (allowed, two
prohibited colors, red_line, and one unmapped background color).

Field notes:

- `color_hex`/`red`/`green`/`blue` must agree with each other (`color_hex`
  is validated against the RGB triplet).
- `layer_kind` is one of `allowed`, `prohibited`, `red_line`, `unknown`, or
  `ignore`. Only `allowed`/`prohibited`/`red_line` entries are ever
  segmented; `unknown`/`ignore` rows (background colors, colors still
  awaiting classification) are carried in the file for completeness but are
  never rasterized.
- `review_status` is `needs_review`, `approved`, or `rejected`, matching the
  values already used by `app.genplan_pipeline`'s legend review flow. Only
  `approved` entries are segmented, even if `layer_kind` is set.
- `source_sha256` refers to the **original raw document** (the same
  `source_sha256` used in `gcps.json`/`qa.json`/`checkpoints.json` and stored
  on `GenplanSourceDocument.source_sha256`), not the exported GeoTIFF. Pass
  `--provenance` from `tools.genplan_export`'s `provenance.json` so this tool
  can check `legend.source_sha256` against `provenance.inputs.source.sha256`
  and confirm the raster passed via `--source` really is the file
  `provenance.output.sha256` says was exported for that same document. Without
  `--provenance`, `legend.source_sha256` is checked directly against the
  SHA-256 of the `--source` file instead - useful for ad hoc runs, but it
  means the legend must be re-approved per exported file rather than per
  original document.
- `tolerance` (0-255) is a per-color Chebyshev distance tolerance in RGB
  space; `min_area_px` is a document-level sieve applied to every layer to
  drop small color-noise polygons.

## Run

```powershell
python -m tools.genplan_vectorize `
  --source C:\genplan\published\asset-123\sheet.tif `
  --legend C:\genplan\reviews\asset-123-legend.json `
  --provenance C:\genplan\published\asset-123\provenance.json `
  --output-dir C:\genplan\vectorize\asset-123-v1
```

`--provenance` is optional but recommended: it is the same `provenance.json`
that `tools.genplan_export` already wrote next to `sheet.tif`.

## `manifest.json`

```json
{
  "schema_version": "genplan-vectorize-manifest/v1",
  "workflow_status": "proposed",
  "record_id": "asset-123",
  "generated_at_utc": "2026-08-05T12:00:00Z",
  "source_raster": {"path": "...", "sha256": "..."},
  "legend": {"path": "...", "sha256": "...", "record_id": "...", "reviewer_id": "...", "reviewed_at_utc": "..."},
  "layers": {
    "allowed": {"path": "allowed.geojson", "sha256": "...", "feature_count": 12},
    "prohibited": {"path": "prohibited.geojson", "sha256": "...", "feature_count": 4},
    "red_line": {"path": "red_line.geojson", "sha256": "...", "feature_count": 8}
  },
  "chain_sha256": "sha256(source_sha256 || legend_sha256 || allowed_sha256 || prohibited_sha256 || red_line_sha256)",
  "warning": "Vectorized layers are proposed candidates only. ..."
}
```

`chain_sha256` is a single hash over the source raster, the legend, and all
three layer files (in that fixed order), so a downstream reviewer or
`tools.genplan_import` can detect if any input or output in the bundle was
swapped after generation, without re-deriving the whole manifest.

## Important limits

This is a production helper, not a legal decision engine.

- Color rules must be checked against the legend by an operator; this tool
  never infers zone meaning, only pixel color.
- Thin lines (red lines especially) may need manual correction after
  vectorization.
- A scanned plan can be shifted or distorted even after a good georeference.
- The generated GeoJSON must pass independent review before import.

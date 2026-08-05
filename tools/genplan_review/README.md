# Genplan independent review

`tools.genplan_review` performs a read-only A2 acceptance review of an A1
georeferencing. It never edits `gcps.json` or `qa.json`, never refits the saved
transformation, and writes a separate `review.json`.

The decision is one of `STRICT`, `WARNING`, or `REJECT`. `STRICT` follows
`QA_PROTOCOL.md` and requires:

- at least 8 well-distributed A1 training GCPs;
- at least 6 independently selected A2 checkpoints;
- 4 edge/corner checkpoints, 2 interior checkpoints, 4 quarters, 3 feature
  types, and 2 reference sources;
- checkpoint RMSE at most 5 m, P95 at most 8 m, and MAX at most 10 m;
- correct orientation, scale 1:10 000 or larger, and anisotropy at most 1%;
- at least 12 visual samples with the required edge/interior/boundary/critical
  distribution;
- readable, confirmed legend evidence and no rejected thematic layer;
- matching record IDs and SHA-256 values in every artifact;
- different A1 and A2 reviewer IDs;
- `verified_official` provenance with a confirmed approval act and current
  revision.

An absent or unconfirmed official source can never produce `STRICT`. Unknown
provenance, identity conflicts, SHA mismatches, non-independent reviewers,
insufficient checkpoints, critical orientation defects, rejected layers, or
errors over the reject thresholds produce `REJECT`.

## CLI

```powershell
python -m tools.genplan_review `
  --gcps C:\work\A1\gcps.json `
  --qa C:\work\A1\qa.json `
  --checkpoints C:\work\A2\checkpoints.json `
  --provenance C:\work\provenance.json `
  --legend C:\work\legend.json `
  --output C:\work\A2\review.json
```

The output is written atomically. Existing A1 files are opened only for reading.

## `checkpoints.json`

```json
{
  "schema_version": "genplan-checkpoints/v1",
  "record_id": "asset-1",
  "source_sha256": "64 lowercase hex characters",
  "reviewer_id": "reviewer-a2",
  "reviewed_at_utc": "2026-07-23T08:00:00Z",
  "selected_before_a1_residuals": true,
  "points": [
    {
      "id": "CP01",
      "pixel_x": 100,
      "pixel_y": 100,
      "lon": 70.123,
      "lat": 52.123,
      "source": "EGKN+satellite",
      "feature": "road_intersection",
      "note": ""
    }
  ]
}
```

At least six points are required for acceptance. Checkpoint IDs must not reuse
A1 GCP IDs.

## `provenance.json`

For `verified_official`, fill the document title/type, approving authority,
approval number/date, official URL or publication reference, source check time,
territory, revision, `current_version_confirmed=true`, and
`identity_status="resolved"`.

## Legend and visual evidence

```json
{
  "schema_version": "genplan-legend-evidence/v1",
  "record_id": "asset-1",
  "source_sha256": "64 lowercase hex characters",
  "reviewer_id": "reviewer-a2",
  "legend_status": "readable",
  "interpretation_confirmed": true,
  "scale_denominator": 10000,
  "orientation_status": "correct",
  "anisotropy_percent": 0.4,
  "anisotropy_explained": false,
  "visual_samples": [],
  "layers": [
    {"name": "roads", "status": "strict", "categories": [], "note": ""}
  ],
  "notes": ""
}
```

Visual sample areas are `edge`, `interior`, `boundary`, and `critical`; results
are `pass`, `warning`, and `fail`. Layer statuses are `strict`, `warning`,
`unavailable`, and `reject`.

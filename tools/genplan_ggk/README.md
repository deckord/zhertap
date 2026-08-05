# AIS GGK national urban-plan importer

This tool reads the public national WFS at `https://gov.ggk.kz/geoserver/ows`
and builds the same three-layer release format already accepted by
`tools.genplan_import`.

It never activates a remote document directly. A release requires an independent
review file, preserves raw source snapshots, hashes every artifact, validates KATO
scope and geometry, and fails closed when allowed zones or red lines are missing.

## List documents

```bash
python -m tools.genplan_ggk catalog
python -m tools.genplan_ggk catalog --city Астана
```

## Review input

```json
{
  "status": "VERIFIED_STRICT",
  "independent_review": true,
  "reviewer": "reviewer-a2",
  "reviewed_at_utc": "2026-07-23T12:00:00+00:00",
  "checks": {
    "document_identity_verified": true,
    "legal_act_verified": true,
    "kato_scope_verified": true,
    "zone_mapping_verified": true,
    "geometry_bounds_verified": true,
    "random_visual_samples_verified": true
  },
  "legal_act": {
    "number": "№33",
    "date": "2024-01-25",
    "url": "https://adilet.zan.kz/rus/docs/P2400000033",
    "status": "active"
  }
}
```

## Build and import

```bash
python -m tools.genplan_ggk build \
  --document-id 3607 \
  --profile lph-household \
  --output-dir releases/astana-lph \
  --review reviews/astana-3607.json

python -m tools.genplan_import \
  --manifest releases/astana-lph/release-manifest.json
```

Profiles:

- `lph-household`: estate-development zones.
- `lph-field`: plant-growing and agricultural-designation zones.
- `gardening`: gardening partnerships and garden/dacha land.

Importing a new release with the same scope, purpose, and layer kinds
transactionally deactivates the previous active release. This prevents old and
new versions of one urban plan from being used in the same search.

The WFS document date is retained in `provenance.json`, but the approval date and
official URL used in reports come from the separately reviewed legal act.

The urban-plan result is still informational. It does not prove state ownership,
legal vacancy, or entitlement to receive the land.

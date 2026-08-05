# Safe Genplan Vector Import

This tool validates a complete vector release and imports it into the existing
`UrbanPlanLayer` table. It does not create or migrate the application schema and
does not modify application code.

## Safety gate

A release must contain exactly three GeoJSON artifacts: `allowed`,
`prohibited`, and `red_line`. Every artifact path is relative to the manifest
directory and may not escape it.

Search activation requires all of the following:

- review status `STRICT` or `VERIFIED_STRICT`;
- provenance status `verified_official`;
- identity status `matched`;
- `independent_review: true`;
- reviewer role `A2`, with a reviewer different from the vector operator;
- matching release, review, provenance, source, and vector SHA-256 values;
- the same official HTTPS URL in the manifest and provenance;
- explicit manifest `release_mode: "search"`.

`WARNING` is accepted only with `release_mode: "shadow"` and
`allow_shadow: true`. It is stored with `active=false` and
`approved_for_search=false`. Rejected, pending, unverified, ambiguous, or
checksum-mismatched releases are not imported.

All three GeoJSON files pass through the application's `normalize_geojson`.
The database transaction contains all three layers. A failure rolls everything
back.

Idempotency uses:

`source_sha256 + layer_kind + purpose + region + district + locality`

Repeating an identical release returns the existing rows. Reusing the key with
different content is a conflict and does not overwrite anything.

## Release format

`release-manifest.json`:

```json
{
  "schema_version": "1.0",
  "release_id": "burabay-2026-v1",
  "release_mode": "search",
  "source_sha256": "<sha256 of the official source document>",
  "source_version": "2026-07-23",
  "source_epsg": 4326,
  "released_by": "release-operator",
  "purpose": "all",
  "scope": {
    "region": "Акмолинская область (01)",
    "district": "р-н. Бурабайский (01-171)",
    "locality": "Бурабай"
  },
  "document": {
    "title": "Генеральный план Бурабая",
    "approval_document": "Решение маслихата № 1",
    "approval_date": "2026-01-15",
    "source_authority": "Акимат",
    "source_url": "https://www.gov.kz/example"
  },
  "review": {
    "path": "review.json",
    "sha256": "<sha256 of review.json>"
  },
  "provenance": {
    "path": "provenance.json",
    "sha256": "<sha256 of provenance.json>"
  },
  "layers": {
    "allowed": {
      "path": "allowed.geojson",
      "sha256": "<sha256>",
      "zone_name": "Разрешенная территория"
    },
    "prohibited": {
      "path": "prohibited.geojson",
      "sha256": "<sha256>",
      "zone_name": "Запрещенные зоны"
    },
    "red_line": {
      "path": "red_line.geojson",
      "sha256": "<sha256>",
      "zone_name": "Красные линии"
    }
  }
}
```

`review.json`:

```json
{
  "release_id": "burabay-2026-v1",
  "source_sha256": "<official source sha256>",
  "status": "VERIFIED_STRICT",
  "independent_review": true,
  "reviewer_role": "A2",
  "reviewer": "reviewer-2",
  "operator": "vector-operator-1",
  "reviewed_at_utc": "2026-07-23T10:30:00+00:00",
  "allow_shadow": false,
  "layer_sha256": {
    "allowed": "<sha256>",
    "prohibited": "<sha256>",
    "red_line": "<sha256>"
  }
}
```

`provenance.json`:

```json
{
  "release_id": "burabay-2026-v1",
  "source_sha256": "<official source sha256>",
  "review_sha256": "<sha256 of review.json>",
  "provenance_status": "verified_official",
  "identity_status": "matched",
  "official_url": "https://www.gov.kz/example",
  "layers": {
    "allowed": {"sha256": "<sha256>"},
    "prohibited": {"sha256": "<sha256>"},
    "red_line": {"sha256": "<sha256>"}
  }
}
```

## CLI

Validate without writing:

```powershell
python -m tools.genplan_import `
  --manifest C:\genplan\release\release-manifest.json `
  --dry-run
```

Import into the configured application database:

```powershell
python -m tools.genplan_import `
  --manifest C:\genplan\release\release-manifest.json
```

Override the database URL:

```powershell
python -m tools.genplan_import `
  --manifest C:\genplan\release\release-manifest.json `
  --database-url postgresql+psycopg://user:password@localhost/land_scout
```

Validation or policy rejection exits with code `2`. An unexpected database
failure exits with code `1`.

## Tests

```powershell
pytest -q tests/test_genplan_import.py
ruff check tools/genplan_import tests/test_genplan_import.py
```

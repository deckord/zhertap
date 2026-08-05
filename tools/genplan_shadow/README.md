# Genplan Shadow Comparator

This is an offline shadow-mode comparator. It does not import `app`, write to the
application database, alter bot candidates, or participate in production search.
It converts a candidate and future reviewed genplan layers into an auditable JSON
decision:

- `match`: trusted coverage is complete and no reviewed blocking mask intersects;
- `blocked`: a trusted road, water or explicitly blocked zone mask intersects;
- `no_coverage`: there is no usable current and approved source coverage;
- `manual_review`: geometry, identity, integrity or mask meaning needs a person.

`blocked` is fail-safe. It can only be produced from a layer with:

1. `verified_official` provenance and an unambiguous matched identity;
2. a current source version checked within the configured age limit;
3. matching provenance and QA SHA-256 values;
4. independent `APPROVED`, `STRICT` or `VERIFIED_STRICT` review;
5. complete road, water and zone classification over the whole candidate.

Unverified, stale, superseded, `REJECT` and ambiguous layers are retained in
`source_versions`, but they never block a candidate. Ambiguity or checksum
conflicts produce `manual_review`; other unusable sources produce `no_coverage`
when no trusted replacement covers the candidate.

## Input

Coordinates are represented as GeoJSON `Point`; a parcel or proposed square is a
GeoJSON `Polygon` or `MultiPolygon`. All geometries are expected in WGS 84.

```json
{
  "candidate": {
    "candidate_id": "candidate-42",
    "geometry": {
      "type": "Polygon",
      "coordinates": [[[70.30, 53.08], [70.301, 53.08], [70.301, 53.081],
        [70.30, 53.081], [70.30, 53.08]]]
    }
  },
  "as_of": "2026-07-23T12:00:00+06:00",
  "layers": [{
    "layer_id": "burabay-genplan",
    "kind": "geojson_masks",
    "coverage_geometry": {
      "type": "Polygon",
      "coordinates": [[[70.0, 53.0], [70.6, 53.0], [70.6, 53.4],
        [70.0, 53.4], [70.0, 53.0]]]
    },
    "categories_checked": ["road", "water", "zone"],
    "masks": {
      "type": "FeatureCollection",
      "features": [{
        "type": "Feature",
        "id": "road-17",
        "properties": {"category": "road", "effect": "block"},
        "geometry": {
          "type": "Polygon",
          "coordinates": [[[70.299, 53.079], [70.302, 53.079],
            [70.302, 53.082], [70.299, 53.082], [70.299, 53.079]]]
        }
      }]
    },
    "provenance": {
      "source_id": "akimat-burabay-2025",
      "source_title": "Approved Burabay general plan",
      "source_version": "2025-12-01",
      "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "status": "verified_official",
      "identity_status": "matched",
      "official_url": "https://example.gov.kz/genplan",
      "checked_at": "2026-07-01T10:00:00+06:00",
      "valid_until": "2027-07-01T10:00:00+06:00",
      "current": true,
      "superseded_by": null
    },
    "qa_review": {
      "decision": "VERIFIED_STRICT",
      "review_version": "qa-3",
      "reviewer_id": "reviewer-2",
      "reviewed_at": "2026-07-02T10:00:00+06:00",
      "expires_at": "2027-07-02T10:00:00+06:00",
      "independent_review": true,
      "ambiguity_resolved": true,
      "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  }]
}
```

For a georaster use `"kind": "georaster"` and provide:

```json
{
  "raster": {
    "uri": "file:///data/burabay-v3.tif",
    "crs": "EPSG:4326",
    "footprint": {"type": "Polygon", "coordinates": []},
    "classification_complete": true,
    "categories_checked": ["road", "water", "zone"],
    "masks": {"type": "FeatureCollection", "features": []}
  }
}
```

The comparator reads raster metadata and reviewed vector masks; it does not infer
land-use classes directly from raster pixels. An empty or absent classification
therefore cannot silently become a clear `match`.

## CLI

```powershell
python -m tools.genplan_shadow --input request.json --output decision.json
```

Print compact JSON:

```powershell
python -m tools.genplan_shadow --input request.json --compact
```

Invalid JSON or schema returns exit code `2`. A valid `blocked` or
`manual_review` decision is normal output and returns `0`.

## Tests

```powershell
pytest -q tests/test_genplan_shadow.py
ruff check tools/genplan_shadow tests/test_genplan_shadow.py
```

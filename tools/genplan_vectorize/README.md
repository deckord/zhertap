# Genplan Vectorize

`tools.genplan_vectorize` is the missing bridge after manual georeferencing.
It takes a reviewed georeferenced raster, usually a GeoTIFF/COG created by
`tools.genplan_export`, and creates candidate GeoJSON layers by color rules.

The output is not trusted automatically. It must go through independent QA and
then `tools.genplan_import` as a `VERIFIED_STRICT/search` release before it can
affect customer search results.

## Input

- A georeferenced RGB GeoTIFF/COG.
- A color-class JSON config.

Example config:

```json
{
  "schema_version": "genplan-vectorize/v1",
  "release_id": "burabay-raster-v1",
  "source_title": "Burabay official genplan sheet",
  "layers": [
    {
      "layer_kind": "allowed",
      "zone_name": "Allowed residential / household zone",
      "colors": ["#f4d35e"],
      "tolerance": 12,
      "sieve_pixels": 64
    },
    {
      "layer_kind": "prohibited",
      "zone_name": "Restricted zones",
      "colors": ["#d62828", "#8d0801"],
      "tolerance": 10,
      "sieve_pixels": 64
    },
    {
      "layer_kind": "red_line",
      "zone_name": "Red lines",
      "colors": ["#ff0000"],
      "tolerance": 20,
      "sieve_pixels": 16
    }
  ]
}
```

## Run

```powershell
python -m tools.genplan_vectorize `
  --source C:\genplan\exports\burabay.tif `
  --config C:\genplan\vectorize\burabay-colors.json `
  --output-dir C:\genplan\vectorize\burabay-v1
```

The tool writes:

- `allowed.geojson`
- `prohibited.geojson`
- `red_line.geojson`
- `vectorize-manifest.json`

## Important Limits

This is a production helper, not a legal decision engine.

- Color rules must be checked against the legend by an operator.
- Thin lines may need manual correction after vectorization.
- A scanned plan can be shifted or distorted even after a good georeference.
- The generated GeoJSON must be reviewed before import.

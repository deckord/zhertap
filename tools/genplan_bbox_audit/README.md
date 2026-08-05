# Genplan Bbox Audit

Checks whether each manual genplan/PDP raster can be resolved to a map bounding
box before running expensive autoregistration.

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_bbox_audit `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4
```

Outputs:

- `summary.json` - counts by status and bbox source.
- `records.json` - machine-readable per-asset rows.
- `records.csv` - operator-readable per-asset rows.

`resolved` only means the operator workbench can open the right map area. It is
not an automatic genplan/PDP check and must not be imported as strict coverage
without GCP placement, vectorization and QA.


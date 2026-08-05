# Genplan Readiness

`tools.genplan_readiness` audits the local manual genplan pipeline from scanned
PDF/JPG/TIFF sources to importable vector candidates.

It does not approve or import layers. It tells the operator what the next safe
step is for each workbench record:

- resolve map-area conflict;
- select the correct PDF page;
- place A1 GCPs in the workbench;
- submit saved GCPs to QA;
- run independent A2 review;
- export GeoTIFF and vectorize;
- run import QA.

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_readiness `
  --workbench-manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --workbench-output C:\Users\medadmin\Documents\Codex\genplan\workbench_data `
  --workbench-url http://127.0.0.1:8765 `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\readiness-v7
```

Outputs:

- `summary.json` - counts by stage and bbox status;
- `records.json` - full machine-readable rows;
- `records.csv` - operator spreadsheet with direct workbench URLs when
  `--workbench-url` is provided.

Current local status from 2026-08-03:

- 175 records total after selected/split PDF page rendering;
- 175 bbox resolved;
- 0 bbox conflicts;
- 175 need A1 control points;
- 0 need PDF page selection.


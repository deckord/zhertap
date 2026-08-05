# Genplan Autoreg Diagnostics

`tools.genplan_autoreg_diagnostics` builds reproducible operator reports from
`tools.genplan_batch` output.

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_autoreg_diagnostics `
  --autoreg-output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v1 `
  --workbench-manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v1
```

Generated files:

- `summary.json` - totals, workflow counts, registration counts, reason counts.
- `attempts.csv` - one row per basemap attempt.
- `reason-counts.csv` - grouped rejection/error reasons.
- `operator-priority.csv` - one row per asset, sorted by the most promising
  manual-review attempt.

The report is diagnostic only. It must not be used to approve a raster/PDF
general plan for customer checks without A1/A2 georeferencing QA.


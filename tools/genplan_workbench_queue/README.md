# Genplan Workbench Queue

`tools.genplan_workbench_queue` builds a focused manifest for
`tools.genplan_workbench`.

It uses the current status report, removes duplicates/conflicts, substitutes
rendered one-page PDF PNG files when available, and skips source formats the
browser workbench cannot display.

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_workbench_queue `
  --inventory C:\Users\medadmin\Documents\Codex\genplan\inventory\manifests\manifest.json `
  --status-report C:\Users\medadmin\Documents\Codex\genplan\work\status-report.json `
  --prepared-pdf-manifest C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-renders\manifest.json `
  --selected-pdf-page-manifest C:\Users\medadmin\Documents\Codex\genplan\work\selected-pdf-pages\manifest.json `
  --pdf-contact-sheet-manifest C:\Users\medadmin\Documents\Codex\genplan\work\pdf-contact-sheets\manifest.json `
  --bbox-audit-records C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4\records.json `
  --autoreg-output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v1 `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json

.\.venv\Scripts\python.exe -m tools.genplan_workbench `
  --root C:\Users\medadmin\Documents\Codex\genplan `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --port 8765
```

When `--bbox-audit-records` is provided, the workbench manifest includes
`bbox_status`, `bbox_source`, `bbox_label`, and `bbox_reason`. The browser UI
then shows map-area counters and filters for `Map area found` and
`Map area needs review`.

When `--autoreg-output` is provided, the manifest includes conservative
autoreg diagnostics and artifact links for the operator. These diagnostics do
not approve the map; they only explain why manual A1 control points are still
required.

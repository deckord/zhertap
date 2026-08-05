# Single-Page PDF Genplan Prepare

`tools.genplan_pdf_prepare` renders only one-page PDF genplans into PNG files
and writes a `genplan_batch` compatible manifest.

It deliberately skips multi-page PDFs, because choosing the correct genplan page
is an operator decision.

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_pdf_prepare `
  --inventory C:\Users\medadmin\Documents\Codex\genplan\inventory\manifests\manifest.json `
  --data-root C:\Users\medadmin\Documents\Codex\genplan `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-renders `
  --max-render-seconds 120

.\.venv\Scripts\python.exe -m tools.genplan_batch `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-renders\manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-autoreg `
  --resume
```

If one PDF hangs or fails, the command continues and writes the item to
`render_errors` in the output manifest. Existing rendered PNG files are reused.

# Genplan Status Report

`tools.genplan_status` builds a local control report for the manual PDF/JPG
genplan library. It does not publish anything and does not mark a scanned map as
checked. Its job is to show the next concrete step for every file.

Typical statuses:

- `manual_file_only` - the map exists and can be shown to clients, but it is not
  georeferenced.
- `pdf_page_selection_required` - a multi-page PDF or unrendered PDF needs an
  operator to choose the correct map page.
- `duplicate_manual_file` - the same source content is already represented by
  another asset.
- `identity_review_required` - the region/district/locality identity must be
  corrected before automated attempts continue.
- `manual_georeference_required` - an automatic matching attempt was made and
  failed safely; add control points in `tools.genplan_workbench`.
- `qa_pending` - GCP and QA files exist; a second reviewer must approve.
- `reviewed_gcps` - ready for `tools.genplan_export`.
- `georeferenced_export` - ready for `tools.genplan_vectorize`.
- `vectorized_candidate` - ready for independent vector QA and then
  `tools.genplan_import`.

Run from the project root:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_status `
  --manual-manifest app\data\manual_genplans.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\status-report.json `
  --csv-output C:\Users\medadmin\Documents\Codex\genplan\work\status-report.csv
```

The CSV is intended for operator planning: sort by `status`, `region`,
`district`, and `locality`, then process the queue city by city.

# Genplan batch preprocessor

This command performs conservative preliminary processing of inventory records.
It never assigns `VERIFIED`, `STRICT`, or search approval.

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_batch `
  --manifest "C:\path\to\manifest.json" `
  --output "C:\path\to\batch-output" `
  --exclude-file "C:\path\to\exclusions.json" `
  --workers 2 `
  --max-tiles 64 `
  --min-free-disk-gb 10 `
  --max-output-gb 5 `
  --zoom 14 `
  --resume
```

The exclusion registry is JSON:

```json
{
  "schema_version": "1.0",
  "excluded_assets": [
    {
      "asset_id": "inventory asset id",
      "reason": "identity_conflict: explain the evidence"
    }
  ]
}
```

An excluded asset receives `identity_conflict` and
`manual_identity_review`; the raster runner is not called. The registry file
hash is included in the processing fingerprint, so changing an exclusion
invalidates unsafe resume assumptions.

Important summary fields:

- `completed`: a preliminary attempt finished, not an approval;
- `render_manual`: a PDF still needs explicit page selection;
- `identity_conflict`: the asset is blocked pending identity review;
- `attempt_error_assets`: assets whose attempt recorded a pipeline-level
  error such as unresolved locality or source size limit;
- `resumed`: results reused only after source SHA and processing fingerprint
  matched;
- `safety.qa_or_strict_automatic`: always `false`.

Every proposed result still requires manual A1 control points, independent A2
checkpoints, verified official provenance, STRICT export, and guarded import.

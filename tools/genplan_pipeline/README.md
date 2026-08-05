# Genplan PDF/JPG inventory pipeline

This tool inventories ZIP archives and loose PDF/JPG/PNG/TIFF files before any
manual georeferencing work. It never invents coordinates and does not mark a plan
as georeferenced merely because it is a TIFF or has a plausible filename.

## What it produces

The output workspace contains:

- `extracted/` - safe, archive-specific extraction directories;
- `manifests/archives.json` and `archives.csv` - archive SHA-256, sizes and status;
- `manifests/manifest.json` and `manifest.csv` - one record per extracted/loose file;
- `manifests/summary.json` - counts by format and workflow status;
- `manifests/errors.jsonl` - machine-readable errors and warnings;
- `manifests/aliases.example.json` - template for manual name corrections.

Every asset keeps the original archive member path and original location labels.
Normalized labels are stored in separate fields. Region normalization includes the
EGKN region code, for example `Акмолинская область (01)`.

## Georeferencing statuses

- `requires_control_points`: ordinary PDF/JPG/PNG; control points must be added;
- `metadata_requires_review`: TIFF that may contain spatial metadata but is unverified;
- `sidecar_detected_unverified`: a world file exists but has not been validated;
- `unsupported_document`: DOCX/PPTX/other supporting file;
- `not_checked`: reserved for records that have not reached metadata inspection.

No CRS, control points or RMS error are fabricated. Their manifest fields remain
empty/zero until a later reviewed georeferencing stage writes verified values.

`asset_role` explicitly separates:

- `plan_document` - PDF/JPG/JPEG/JPE/PNG/TIF/TIFF used for georeferencing;
- `supporting_document` - DOCX/PPTX kept for operator review;
- `service_file` - extraction maps, world files and `.DS_Store`;
- `unsupported_file` - inventoried but not used as a plan.

## Run

From the project root in PowerShell:

For the already extracted dataset (recommended):

```powershell
python -m tools.genplan_pipeline inventory `
  --source 'C:\Users\medadmin\Documents\Codex\genplan\extracted' `
  --output 'C:\Users\medadmin\Documents\Codex\genplan\inventory'
```

`auto` recognizes the `extracted` directory, reads adjacent
`*-extraction-map.jsonl` files and automatically loads
`..\work\egkn_catalog.json`. Original ZIP member names remain in the manifest;
paths on disk are kept separately in `extracted_path`.

To specify these inputs explicitly:

```powershell
python -m tools.genplan_pipeline inventory `
  --source 'C:\Users\medadmin\Documents\Codex\genplan\extracted' `
  --output 'C:\Users\medadmin\Documents\Codex\genplan\inventory' `
  --input-mode extracted `
  --egkn-catalog 'C:\Users\medadmin\Documents\Codex\genplan\work\egkn_catalog.json'
```

For raw ZIP archives:

```powershell
python -m tools.genplan_pipeline run `
  --source 'C:\Users\medadmin\Documents\Codex\genplan' `
  --output 'C:\Users\medadmin\Documents\Codex\genplan-work'
```

This dataset contains several gigabytes, so the first SHA-256 and extraction pass
can take time and requires enough free disk space. A repeat run reuses already
extracted files when their recorded ZIP size matches.

Inventory only, without extracting ZIP members:

```powershell
python -m tools.genplan_pipeline run `
  --source 'C:\Users\medadmin\Documents\Codex\genplan' `
  --output 'C:\Users\medadmin\Documents\Codex\genplan-inventory' `
  --no-extract
```

The inventory-only mode lists archive metadata and any loose source files. ZIP
members enter the asset manifest only after safe extraction.

## Manual aliases

Copy `aliases.example.json`, edit it and pass it with `--aliases`:

```json
{
  "regions": {
    "Акмолинская область (01)": {
      "name": "Акмолинская область",
      "code": "01"
    }
  },
  "districts": {
    "Бурабайский район": "Бурабайский район"
  },
  "localities": {
    "с. Бурабай": "Бурабай"
  }
}
```

```powershell
python -m tools.genplan_pipeline run `
  --source 'C:\Users\medadmin\Documents\Codex\genplan' `
  --output 'C:\Users\medadmin\Documents\Codex\genplan-work' `
  --aliases 'C:\Users\medadmin\Documents\Codex\genplan-aliases.json'
```

Aliases affect only normalized fields. Original names remain unchanged.

## Safety and reproducibility

- archive and asset content is hashed with SHA-256 in streaming mode;
- absolute paths, `..`, drive-qualified paths and ZIP symlinks are rejected;
- configurable member/archive expansion limits reduce ZIP-bomb risk;
- output JSON is UTF-8 and CSV is UTF-8 with BOM for Excel;
- records are sorted deterministically;
- manifests are replaced atomically on every run;
- errors do not silently disappear: they are written to `errors.jsonl`.

Run tests:

```powershell
python -m pytest tests/test_genplan_pipeline.py -q
python -m ruff check tools/genplan_pipeline tests/test_genplan_pipeline.py
```

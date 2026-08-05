# Genplan Georeferencing Workbench

Local operator tool for assigning WGS84 coordinates to scanned JPG, PNG and PDF
urban-plan sheets. It reads the asset manifest produced by
`tools.genplan_pipeline`, preserves the original source and writes:

- `gcps.json` with pixel/WGS84 pairs and `train`/`checkpoint` roles;
- `qa.json` with transform parameters, residuals, RMSE and point-distribution checks.

The workbench cannot approve or publish a layer. It accepts only `proposed` and
`qa_pending`; every QA JSON explicitly records that automatic approval is false.

## Workflow

1. Select an asset from the manifest.
2. For a PDF, select its page.
3. Click a recognizable point on the source image.
4. Click the same point on OSM or Esri World Imagery.
5. Add well-distributed `train` GCP around the perimeter and center.
6. Add independent `checkpoint` points that were not used for fitting.
7. Choose `affine` or `projective`, save a draft, and inspect residuals.
8. Send the result to `qa_pending` for a second reviewer.

The distribution check expects at least six train points, broad X/Y coverage,
three or more image quadrants and at least three edge points. These checks are
diagnostic only. A good score never changes the result to `approved`.

## Input and output

`--root` is the only filesystem tree the process may read or write. The manifest,
every source path in it, the PDF render cache and the output directory must resolve
below that root. Absolute paths from a manifest are accepted only when they remain
inside the root. Symlink and `..` escapes are rejected.

The default output is:

```text
<root>/workbench_data/
  rendered/<record-hash>/page-0001.png
  records/<record-hash>/gcps.json
  records/<record-hash>/qa.json
```

The source SHA-256 is stored in both exports.

## Windows

From the project directory:

```powershell
Set-Location 'C:\Users\medadmin\Documents\Codex\2026-06-30\vj\land-scout-bot'

python -m pip install -e .
python -m pip install PyMuPDF

python -m tools.genplan_workbench `
  --root 'C:\Users\medadmin\Documents\Codex\genplan' `
  --manifest 'C:\Users\medadmin\Documents\Codex\genplan\work\pipeline\manifests\manifest.json' `
  --port 8765
```

Open `http://127.0.0.1:8765`.

PyMuPDF is needed only for PDF pages. JPG and PNG work without it. Poppler
`pdftoppm` may be installed instead and must be available in `PATH`.

## Ubuntu

```bash
cd /opt/land-scout/land-scout-bot

python3 -m venv .venv-workbench
. .venv-workbench/bin/activate
pip install -e .
pip install PyMuPDF

python -m tools.genplan_workbench \
  --root /opt/land-scout/genplan \
  --manifest /opt/land-scout/genplan/work/pipeline/manifests/manifest.json \
  --port 8765
```

Alternative PDF renderer:

```bash
sudo apt-get update
sudo apt-get install -y poppler-utils
```

The default host is `127.0.0.1`. If remote access is required, use an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

Do not expose this operator tool directly to the public internet. Basemap tiles
require internet access in the browser; all GCP and QA files remain local.

## Tests

```bash
pytest -q tests/test_genplan_workbench.py
ruff check tools/genplan_workbench tests/test_genplan_workbench.py
```


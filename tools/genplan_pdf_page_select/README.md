# Genplan PDF Page Select

`tools.genplan_pdf_page_select` renders selected pages from multi-page PDF
genplans into PNG files that the local workbench can georeference.

It supports two cases:

- one chosen main page that replaces the original PDF in the workbench queue;
- split mode where every selected page becomes its own workbench record.

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_pdf_page_select `
  --contact-sheet-manifest C:\Users\medadmin\Documents\Codex\genplan\work\pdf-contact-sheets\manifest.json `
  --selections app\data\genplan_pdf_page_selections.json `
  --data-root C:\Users\medadmin\Documents\Codex\genplan `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\selected-pdf-pages `
  --dpi 180 `
  --max-render-seconds 60
```

Current local selections:

- Kaskelen: page 22, main drawing / planning structure;
- Semey: page 19, GP-5 general plan main drawing;
- Kyzylzhar/Roshchinsky: pages 1-5 split into separate records;
- Almaty PDP: pages 1-39 split into separate records.

The output is still only a prepared workbench source. It does not approve,
vectorize, import, or enable automatic search checks.


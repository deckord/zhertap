# Official WFS Genplan Release

This tool creates a reproducible Shymkent urban-plan release from snapshots of
the public WFS registered in Kazakhstan's NSDI service catalog.

It extracts:

- allowed territory only from functional zone `Ж-1`, described by the source
  as estate/household residential development of 1-3 floors;
- prohibited road polygons from `gpautotranrdc_main`;
- red lines from `gpregredlinelin`, only when the source attributes identify
  Government Resolution No. 916 dated 2023-10-17.

The builder does not approve its own output. It requires a separate review JSON
with four explicit checks and a reviewer different from the vector operator.
The resulting manifest still passes through `tools.genplan_import`.

```powershell
python -m tools.genplan_wfs `
  --source-dir C:\genplan\shymkent-live `
  --output-dir C:\genplan\shymkent-release `
  --review C:\genplan\shymkent-independent-review.json
```

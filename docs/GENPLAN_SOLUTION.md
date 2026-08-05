# Genplan/PDP Working Solution

Last updated: 2026-07-31

Update 2026-08-04: the implementation plan for a dedicated internal
Planning Data Service is now tracked in `docs/PLANNING_DATA_SERVICE.md`. That
document is the roadmap for moving PDF/WFS/CAD-derived planning data into a
PostGIS-backed API, starting with the Akkol pilot.

## Goal

Land Scout must not stop at "there is no genplan layer". The product needs a
clear three-tier strategy:

1. Strict automatic check where official digital geometry exists.
2. Assisted/manual check where only official map/PDF/JPG exists.
3. Source discovery queue where no usable source is known yet.

Only tier 1 can be used for automatic "passed/blocked" decisions. Tiers 2 and 3
must be shown to the client as manual verification status, not as an automatic
urban-plan clearance.

## Tier 1: Official Digital Layers

### Primary Source: AIS GGK / State Urban Cadastre

Use the existing `tools.genplan_ggk` connector first.

Live check on 2026-07-30:

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m tools.genplan_ggk catalog
```

The catalog returned 81 official general-plan documents, including:

- г. Астана
- г. Актау
- г. Актобе
- г. Атырау
- г. Кокшетау
- г. Костанай
- г. Тараз
- г. Щучинск
- г. Алатау
- г. Абай

The GGK release builder already extracts:

- `allowed` functional zones by profile;
- `prohibited` functional zones;
- `red_line` official red lines.

Supported profiles:

- `lph-household` -> `ЛПХ:household`
- `lph-field` -> `ЛПХ:field`
- `gardening` -> `Садоводство`

Build command:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_ggk build `
  --document-id 3607 `
  --profile lph-household `
  --output-dir C:\genplan\releases\astana-lph-household `
  --review C:\genplan\reviews\astana-3607-lph-household.json
```

Import command:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_import `
  --manifest C:\genplan\releases\astana-lph-household\release-manifest.json
```

Important: the import gate intentionally requires an independent review JSON.
Do not bypass it. A wrong genplan layer is worse than no automatic genplan
check because it can mislead the client.

### Secondary Source: Smart GeoHub Portals

Several official regional portals use a similar Smart GeoHub API.

Implemented on 2026-07-30: source discovery for Smart GeoHub catalogs is
available from `/admin/urban-plans`. It stores matching collections as
`coverage_status=catalog_found`. This is deliberately weaker than
`digital_found`, because the catalog entry still needs coverage checks, zone
mapping and QA before it can be used for automatic decisions.

Implemented on 2026-07-30: Smart GeoHub geometry probing is available from
`/admin/urban-plans` through "Проверить геометрию GeoHub". The probe calls
`/api/list` and then `/api/geometry` with `feature_id`. Sources that return a
sample geometry become `coverage_status=geometry_found`; empty collections
become `coverage_status=no_features`. This still does not enable the layer for
client search.

Known portal candidates:

- `https://ggk.kz/`
- `https://map.iaqmola.kz/`
- `https://map.e-batys.kz/`
- `https://map.almobl.kz/`
- `https://map.e-zhetisu.kz/`
- `https://map.e-mangistau.kz/`
- `https://map.iulytau.kz/`
- `https://orda.geoportal.kz/`
- `https://geopavlodar.kz/geoserver/ows`
- `https://eatyrau.kz/map/`

The source discovery connector:

1. Read `/api/catalog?context[admterr_id]=kz&lang=ru`.
2. Detect urban-plan/PDP collections by collection names:
   - `gpzone-*`
   - `pdpzone-*`
   - `gpreg-redline*`
   - `pdpreg-redline*`
   - `gpgr-pdp*`
   - `genplanpolygon-*`
   - `genplanline-*`
3. Store matching collections in `urban_plan_sources`.

Implemented probe step:

1. Fetch feature lists through `/api/list`.
2. Fetch exact geometry through `/api/geometry`.

Implemented fast count step on 2026-07-30:

```powershell
.\.venv\Scripts\python.exe -m tools.smart_geohub_export `
  --base-url https://map.iaqmola.kz/ `
  --collection gpzone-jil `
  --search-field usl_i32 `
  --search-text 11010000 `
  --output-dir C:\genplan\smart-geohub\counts\akmola-lph `
  --count-only
```

This reads the API `total` field without downloading geometry. Use it before
full exports to quickly verify whether a suspected collection/code exists.

Implemented release-candidate step on 2026-07-31:

```powershell
.\.venv\Scripts\python.exe -m tools.smart_geohub_release `
  --base-url https://map.iulytau.kz/ `
  --output-dir C:\genplan\smart-geohub\releases\ulytau-lph-household-v1 `
  --release-id ulytau-smart-geohub-lph-household-v1 `
  --region "Улытауская область" `
  --district "*" `
  --locality "*" `
  --purpose "ЛПХ:household" `
  --title "Smart GeoHub Улытауской области: функциональные зоны и красные линии" `
  --approval-document "Официальные слои регионального геопортала Smart GeoHub Улытауской области" `
  --source-authority "Геопортал Улытауской области" `
  --source-url https://map.iulytau.kz/ `
  --allowed gpzone-jil_anon `
  --allowed-search usl_i32=11010000 `
  --prohibited gpzone-restrict_anon `
  --prohibited gpzone-san_anon `
  --prohibited gpzone-transport_anon `
  --red-line gpreg-redline_anon `
  --red-line pdpreg-redline_anon `
  --geometry-workers 8
```

The release builder creates a `tools.genplan_import` compatible package:

- `allowed.geojson`
- `prohibited.geojson`
- `red_line.geojson`
- `source-manifest.json`
- `review.json`
- `provenance.json`
- `release-manifest.json`

Default Smart GeoHub releases are `qa_status=WARNING` and
`release_mode=shadow`. This stores the data for QA and admin visibility, but
does not affect client search until an independent review promotes the release
to `VERIFIED_STRICT/search`.

### Additional WFS and Geonomix Release Builders

Implemented on 2026-07-31:

- `tools.genplan_wfs_release` builds shadow releases from generic
  GeoServer/WFS endpoints.
- `tools.genplan_geonomix_release` builds shadow releases from regional
  Geonomix portals that expose `/api/list` and `/api/geometry`.

These builders create the same release package as Smart GeoHub:

- `allowed.geojson`
- `prohibited.geojson`
- `red_line.geojson`
- `source-manifest.json`
- `review.json`
- `provenance.json`
- `release-manifest.json`

Both builders default to `qa_status=WARNING`, `release_mode=shadow`,
`active=false`, `approved_for_search=false`. They are for data preparation and
QA only. Do not promote by editing database flags; rebuild a reviewed
`VERIFIED_STRICT/search` release after independent QA.

On 2026-07-31 both builders were hardened for production release work:

- `--qa-status`, `--release-mode`, `--reviewed-at-utc`, `--operator`,
  `--reviewer` are explicit release inputs.
- `WARNING` releases are forced to `shadow`.
- `search` releases require `STRICT` or `VERIFIED_STRICT`.
- reviewer and operator must be different people/roles.
- `source-manifest.json` stores `layer_sha256` and `release_policy`.
- features are sorted before writing so release hashes are stable.
- Geonomix supports `--allowed-not-contains FIELD=TEXT` to exclude broad labels
  such as parks, squares or green areas from an otherwise allowed code.

Geonomix portals already imported as shadow:

- `https://map.almobl.kz`
- `https://map.e-zhetisu.kz`
- `https://map.e-batys.kz`
- `https://orda.geoportal.kz`
- `https://map.iturkistan.kz`

Generic WFS portals already imported as shadow:

- `https://geopavlodar.kz/geoserver/ows`
- `https://eatyrau.kz/geoserver/gis_atyrau/wfs`

Implemented export-candidate step on 2026-07-30:

```powershell
.\.venv\Scripts\python.exe -m tools.smart_geohub_export `
  --base-url https://map.iaqmola.kz/ `
  --collection gpzone-jil `
  --search-field usl_i32 `
  --search-text 11010000 `
  --output-dir C:\genplan\smart-geohub\akmola-gpzone-jil `
  --max-features 10000
```

The exporter writes:

- `features.geojson` - sampled or full collection geometry with properties.
- `export-manifest.json` - SHA-256, feature count, geometry type counts,
  property counts and `truncated_by_limit`.

Pilot result on 2026-07-30 for `https://map.iaqmola.kz/`, `gpzone-jil`:
90 exported sample features, all `MultiPolygon`, codes observed in the sample:
`11020000` and `11030000`. The pilot was intentionally limited and must not be
treated as a full layer.

Filtered pilot result on 2026-07-30 for `gpzone-jil`,
`search[usl_i32][text]=11010000`: 30 exported sample features, all
`MultiPolygon`, zone text `Территория усадебной застройки`. This is a better
candidate for `lph-household`, but still requires mapping review and QA.

Real Akmola count results on 2026-07-30:

- `gpzone-jil`: 19,273 total features.
- `gpzone-jil` + `usl_i32=11010000`: 18,057 features; candidate allowed layer
  for LPH / household residential search.
- `gpreg-redline`: 5,857 features.
- `pdpreg-redline`: 2,599 features.
- `gpzone-restrict`: 6 features.
- `gpzone-san`: 303 features.
- `gpzone-transport`: 1,841 features.
- `gpzone-agricult`: 857 features. Summary shows gardening/dacha-like
  semantics are mostly under `usl_i32=11290000`, with labels such as `Дачи`,
  `Дачный массив`, `Территория дачных массивов`, `Территория садового
  товарищества`. Gardening must therefore be a separate mapping/profile, not
  a reuse of the LPH `11010000` profile.

Still required before enabling search:

1. Export full collection geometry into a release candidate.
2. Normalize to WGS84 GeoJSON.
3. Map official zone semantics to Land Scout profiles.
4. Store as a reviewed release before enabling for search.

Production shadow imports on 2026-07-31:

| Source | Region | Purpose | Release | Allowed | Prohibited | Red line | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| Smart GeoHub | Акмолинская область | `ЛПХ:household` | `akmola-smart-geohub-lph-household-v1` | 18057 | 2150 | 8454 | `WARNING/shadow` |
| Smart GeoHub | Акмолинская область | `Садоводство` | `akmola-smart-geohub-gardening-v1` | 776 | 2150 | 8454 | `WARNING/shadow` |
| Smart GeoHub | Алматинская область | `ЛПХ:household` | `almaty-region-smart-geohub-lph-household-v1` | 5435 | 1174 | 6361 | `WARNING/shadow` |
| Smart GeoHub | Алматинская область | `Садоводство` | `almaty-region-smart-geohub-gardening-v1` | 44 | 1174 | 6361 | `WARNING/shadow` |
| Smart GeoHub | Жетысуская область | `ЛПХ:household` | `zhetysu-smart-geohub-lph-household-v1` | 4271 | 6133 | 7238 | `WARNING/shadow` |
| Smart GeoHub | Жетысуская область | `Садоводство` | `zhetysu-smart-geohub-gardening-v1` | 25 | 6133 | 7238 | `WARNING/shadow` |
| Smart GeoHub | Западно-Казахстанская область | `ЛПХ:household` | `zko-smart-geohub-lph-household-v1` | 1770 | 847 | 5057 | `WARNING/shadow` |
| Smart GeoHub | Западно-Казахстанская область | `Садоводство` | `zko-smart-geohub-gardening-v1` | 3 | 847 | 5057 | `WARNING/shadow` |
| Smart GeoHub | Мангистауская область | `ЛПХ:household` | `mangystau-smart-geohub-lph-household-v1` | 182 | 1252 | 3372 | `WARNING/shadow` |
| Smart GeoHub | Туркестанская область | `ЛПХ:household` | `turkestan-smart-geohub-lph-household-v3` | 9068 | 2234 | 2598 | `WARNING/shadow` |
| Smart GeoHub | Туркестанская область | `Садоводство` | `turkestan-smart-geohub-gardening-v1` | 1 | 2234 | 2598 | `WARNING/shadow` |
| Smart GeoHub | Улытауская область | `ЛПХ:household` | `ulytau-smart-geohub-lph-household-v1` | 3377 | 171 | 909 | `WARNING/shadow` |
| Smart GeoHub | Улытауская область | `Садоводство` | `ulytau-smart-geohub-gardening-v1` | 101 | 171 | 909 | `WARNING/shadow` |
| WFS/GeoServer | Павлодарская область | `ЛПХ:household` | `pavlodar-wfs-lph-household-v1` | 247 | 2239 | 2183 | `WARNING/shadow` |
| Geonomix | Алматинская область | `ЛПХ:household` | `almobl-geonomix-lph-household-v1` | 6971 | 6397 | 6263 | `WARNING/shadow` |
| Geonomix | Жетісу облысы | `ЛПХ:household` | `zhetisu-geonomix-lph-household-v1` | 5053 | 6107 | 5474 | `WARNING/shadow` |
| Geonomix | Западно-Казахстанская область | `ЛПХ:household` | `wko-geonomix-lph-household-v1` | 2038 | 1148 | 3848 | `WARNING/shadow` |
| Geonomix | Кызылординская область | `ЛПХ:household` | `kyzylorda-geonomix-lph-household-v1` | 11781 | 4915 | 2161 | `WARNING/shadow` |
| Geonomix | Туркестанская область | `ЛПХ:household` | `turkistan-geonomix-lph-household-v1` | 18515 | 3386 | 4008 | `WARNING/shadow` |
| WFS/GeoServer | Атырауская область | `ЛПХ:household` | `atyrau-wfs-lph-household-v1` | 108 | 403 | 1205 | `WARNING/shadow`, partial |

Production strict Geonomix imports on 2026-07-31:

| Source | Region | Purpose | Release | Allowed | Prohibited | Red line | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| Geonomix | Алматинская область | `ЛПХ:household` | `almobl-geonomix-lph-household-strict-v1` | 5435 | 6397 | 6263 | `VERIFIED_STRICT/search` |
| Geonomix | Область Жетісу | `ЛПХ:household` | `zhetisu-geonomix-lph-household-strict-v1` | 4271 | 6107 | 5474 | `VERIFIED_STRICT/search` |
| Geonomix | Западно-Казахстанская область | `ЛПХ:household` | `wko-geonomix-lph-household-strict-v1` | 1770 | 1148 | 3848 | `VERIFIED_STRICT/search` |
| Geonomix | Кызылординская область | `ЛПХ:household` | `kyzylorda-geonomix-lph-household-strict-v1` | 9068 | 4915 | 2161 | `VERIFIED_STRICT/search` |
| Geonomix | Туркестанская область | `ЛПХ:household` | `turkistan-geonomix-lph-household-strict-v1` | 18345 | 3386 | 4008 | `VERIFIED_STRICT/search` |

Production strict Smart GeoHub imports on 2026-07-31:

| Source | Region | Purpose | Release | Allowed | Prohibited | Red line | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| Smart GeoHub | Акмолинская область | `ЛПХ:household` | `akmola-smart-geohub-lph-household-strict-v1` | 18057 | 2150 | 8454 | `VERIFIED_STRICT/search` |
| Smart GeoHub | Карагандинская область | `ЛПХ:household` | `qarobl-smart-geohub-lph-household-strict-v1` | 5149 | 1004 | 2146 | `VERIFIED_STRICT/search` |
| Smart GeoHub | Костанайская область | `ЛПХ:household` | `kostanay-smart-geohub-lph-household-strict-v1` | 3281 | 500 | 3234 | `VERIFIED_STRICT/search` |
| Smart GeoHub | Мангистауская область | `ЛПХ:household` | `mangystau-smart-geohub-lph-household-strict-v1` | 182 | 1252 | 3372 | `VERIFIED_STRICT/search` |
| Smart GeoHub | Улытауская область | `ЛПХ:household` | `ulytau-smart-geohub-lph-household-strict-v1` | 3377 | 171 | 909 | `VERIFIED_STRICT/search` |

Production strict AIS GGK city imports on 2026-07-31:

| Source | Scope | Purpose | Release | Allowed | Prohibited | Red line | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| AIS GGK | г. Павлодар | `ЛПХ:household` | `ggk-gp-3464-lph-household-bc844c8162d8` | 1240 | 699 | 1075 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Атырау | `ЛПХ:household` | `ggk-gp-3577-lph-household-89af194c2a5d` | 220 | 548 | 459 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Костанай | `ЛПХ:household` | `ggk-gp-3567-lph-household-2e6fa49bc3a3` | 215 | 1479 | 402 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Рудный | `ЛПХ:household` | `ggk-gp-3574-lph-household-6d7c3c5c6973` | 200 | 242 | 180 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Тараз | `ЛПХ:household` | `ggk-gp-3585-lph-household-83d14f5dcfe8` | 2365 | 1446 | 5319 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Тобыл | `ЛПХ:household` | `ggk-gp-3568-lph-household-65fd73ba4d04` | 254 | 313 | 282 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Житикара | `ЛПХ:household` | `ggk-gp-3572-lph-household-69d7bdee3f5b` | 232 | 471 | 499 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Лисаковск | `ЛПХ:household` | `ggk-gp-3573-lph-household-b4f4e7ce5be4` | 117 | 160 | 99 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Аркалык | `ЛПХ:household` | `ggk-gp-3608-lph-household-fd281a481680` | 439 | 334 | 667 | `VERIFIED_STRICT/search` |
| AIS GGK | г. Шахтинск | `ЛПХ:household` | `ggk-gp-3493-lph-household-8e14ad70638d` | 618 | 191 | 277 | `VERIFIED_STRICT/search` |

Production totals after the 2026-07-31 GGK/Smart GeoHub/WFS/Geonomix pass:

- 378 rows in `urban_plan_layers`.
- 72 active `VERIFIED_STRICT/search` rows used by client search.
- 306 inactive `WARNING/shadow` rows stored for QA/admin visibility only.
- Active strict coverage: 24 groups. Region-wide: Акмолинская область,
  Алматинская область, Жетісу, Западно-Казахстанская область,
  Карагандинская область, Костанайская область, Кызылординская область,
  Мангистауская область, Туркестанская область, Улытауская область.
  City/scope-specific: Астана, Шымкент, Актобе, Атырау, Тараз, Павлодар,
  Петропавловск, Шахтинск, Аркалык, Костанай, Лисаковск, Рудный, Тобыл,
  Житикара.
- Cached `urban_plan_coverage` rows with old `unavailable/broken` status were
  cleared for new active LPH territories so new searches can use the layers.

AIS GGK shadow batch on 2026-07-31:

| Profile | Catalog docs checked | Built shadow releases | Blocked | Imported layers | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| `lph-household` | 77 | 69 | 8 | 207 | Skipped already active strict docs: Astana, Aktobe, Petropavlovsk, Shymkent. |
| `lph-field` | 81 | 12 | 69 | 36 | Most blocked docs contain no allowed `11024000`/`11420000` zones. |
| `gardening` | 81 | 1 | 80 | 3 | Only Taldykorgan built from GGK; use Smart GeoHub/local sources for gardening. |

Important: GGK shadow releases do not affect client search. They only prove that
the digital source can be exported structurally. Promotion to `VERIFIED_STRICT`
requires independent legal/source QA, zone mapping review and random visual
samples before rebuilding/importing with `release_mode=search`.

Blocked `lph-household` GGK docs:

- Atbasar: allowed-zone discarded count above safety limit.
- Zhetysay: no `11010000` LPH household zones.
- Kokshetau: allowed-zone discarded count above safety limit.
- Mamlyutka: no `11010000` LPH household zones.
- Sergeevka: too many degenerate functional zones.
- Temirtau: no active red-line geometry.
- Ust-Kamenogorsk: too many degenerate functional zones.
- Shalkar: too many degenerate functional zones.

Remaining Smart GeoHub follow-up:

1. Мангистауская область / `Садоводство` - skipped because
   `genplanzone-agricult` + `usl_i32=11290000` has no allowed features.
2. Западно-Казахстанская область - active Geonomix strict release uses only
   conservative `usl_i32=11010000`. Code `11010001` is intentionally excluded
   until it is independently confirmed as safe for the LPH/household profile.
3. Туркестанская область - active Geonomix strict release excludes allowed
   features whose functional label contains park/square/green semantics. Keep
   this noted for future QA and do not replace it with the older broad shadow
   release.

Remaining non-Smart-GeoHub follow-up:

1. Атырауская область - WFS is open and a partial `ЛПХ:household` shadow release
   exists (`atyrau-wfs-lph-household-v1`: 108 allowed, 403 prohibited,
   1205 red-line features). The portal has many settlement-specific `gp_*` and
   `pdp_*` layers with inconsistent names. Before promotion, build a mapping
   file per settlement and visually check random samples against
   `https://eatyrau.kz/map/`.
2. Алматы city - `mapalmaty.kz` exposes restriction/red-line-like ArcGIS layers,
   but the observed `U007:Зонирование` layer is tax/coefficient zoning, not
   functional genplan zoning. Do not use it as `allowed`.
3. СКО - `https://genplany.sko.kz/` is a useful official document/manual-check
   source. It is not yet an automatic vector layer source.
4. ВКО and Абай - `https://vkomap.kz/Index/Information` and
   `https://abaimap.kz/Index/Information` publicly state that genplans/PDP are
   available on their portals. Their map pages use Leaflet/Esri helpers and
   custom `../Url/*` endpoints. Aбай JS references
   `http://<external-geoserver-host>:8887/geoserver`, but the WFS endpoint was not reachable
   from the production/developer network on 2026-07-31. Standard
   `/geoserver/ows` and `/VKO/VKO/MapServer?f=pjson` style probes returned
   404/unreachable. Build a source-specific adapter around the portal proxy if
   access becomes available.
5. Remaining work: Павлодарская, Атырауская, Костанайская, СКО, ВКО, Абай,
   Алматы city and remaining cities
   still need either a source-specific vector adapter, a reviewed strict
   release, or a reviewed PDF/JPG georeferencing pass. Gardening profiles are
   not covered by the new LPH strict releases and require separate mapping.

Do not assume every Smart GeoHub portal uses identical zone codes. Each source
needs a mapping file:

```json
{
  "portal": "map.iaqmola.kz",
  "profile": "gardening",
  "allowed_collections": ["gpzone-sadovodstvo"],
  "prohibited_collections": ["gpzone-restricted"],
  "red_line_collections": ["gpreg-redline_noedit"],
  "review_status": "mapping_pending"
}
```

## Tier 2: Official PDF/JPG Plans

PDF/JPG must not be treated as automatic geometry until processed. The project
already has most of the safe pipeline:

1. Inventory documents:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_pipeline inventory `
  --source C:\genplan\raw `
  --output C:\genplan\inventory
```

2. Try conservative auto-georeferencing:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_batch `
  --manifest C:\genplan\inventory\manifests\manifest.json `
  --output C:\genplan\batch `
  --workers 2 `
  --resume
```

3. Manually add/check control points:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_workbench `
  --root C:\genplan `
  --manifest C:\genplan\inventory\manifests\manifest.json `
  --port 8765
```

4. Independent QA:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_review `
  --gcps C:\genplan\workbench_data\records\asset-123\gcps.json `
  --qa C:\genplan\workbench_data\records\asset-123\qa.json `
  --checkpoints C:\genplan\reviews\asset-123-checkpoints.json `
  --provenance C:\genplan\reviews\asset-123-provenance.json `
  --legend C:\genplan\reviews\asset-123-legend.json `
  --output C:\genplan\reviews\asset-123-review.json
```

5. Export georeferenced raster:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_export `
  --source C:\genplan\rendered\page-0001.png `
  --gcps C:\genplan\workbench_data\records\asset-123\gcps.json `
  --qa C:\genplan\workbench_data\records\asset-123\qa.json `
  --review C:\genplan\reviews\asset-123-review.json `
  --output C:\genplan\published\asset-123\sheet.tif
```

Missing MVP module:

- `tools.genplan_vectorize`

It must convert a reviewed georeferenced raster into three GeoJSON layers:

- `allowed.geojson`
- `prohibited.geojson`
- `red_line.geojson`

The MVP should be operator-assisted:

1. Operator opens a georeferenced raster.
2. Operator samples legend colors for allowed/prohibited/red-line classes.
3. The tool segments by color with tolerance.
4. The tool polygonizes/line-traces the result.
5. Operator edits/simplifies in QGIS or the workbench.
6. Independent reviewer approves or rejects.
7. Only `STRICT` releases are imported for search.

## Tier 3: Manual Reference Only

If neither digital geometry nor processed PDF/JPG exists, keep a source record
and show a manual-check button in Telegram and web:

- "Открыть официальный генплан/ПДП"
- "Автоматическая проверка генплана пока недоступна"
- "Красные линии не проверены автоматически"

This is already partly implemented by `app/genplan_references.py`.

## Database/Data Model

Implemented on 2026-07-30: source registry table and GGK sync entry point.
Admin path: `/admin/urban-plans`, section "Реестр источников генпланов".

```text
urban_plan_sources
- id
- region
- district
- locality
- platform                # ggk_wfs, smart_geohub, city_arcgis, pdf_jpg
- source_type             # digital_vector, raster_reference, unknown
- source_url
- api_base_url
- admterr_id
- collections_json
- coverage_status         # candidate, digital_found, raster_found, no_source, imported, rejected
- profiles_json           # lph-household/lph-field/gardening availability
- last_checked_at
- last_error
- notes
```

Keep `urban_plan_coverage` as the runtime cache used by search requests.
Use `urban_plan_sources` as the operator/source discovery registry.
Runtime behavior: if no approved layer exists but `urban_plan_sources` has a
matching official digital source, the client-facing message says that the
source exists but import/mapping/QA is not complete yet. This remains
`unavailable`, not `passed`.

GGK catalog sync is available from the admin button and through application code:

```python
from app.db import SessionLocal, init_db
from app.genplan_sources import sync_ggk_urban_plan_sources

init_db()
session = SessionLocal()
try:
    print(sync_ggk_urban_plan_sources(session))
finally:
    session.close()
```

## Product Status Text

Latest production status is tracked in `docs/GENPLAN_STATUS_2026_08_04.md`.
As of the 2026-08-04 verification, production has `396` `urban_plan_layers`
rows, `90` active `VERIFIED_STRICT/search` rows and `306` inactive/shadow rows.
The automatic checker now also supports genplan-first prefiltering: when
approved allowed polygons overlap the requested area, LiveSearch restricts the
EGKN search area to those polygons before loading parcels. If a broad metadata
layer does not actually cover candidate geometry, it is treated as unavailable
for that request instead of blocking the whole region.

Reports must use one of these exact meanings:

- `Проверено по цифровому генплану/ПДП` - only imported `STRICT` vector layer.
- `Генплан открыт для ручной проверки` - official PDF/JPG or portal exists, but no strict geometry.
- `Генплан не найден в источниках` - no usable official source in registry.
- `Генплан есть, но слой требует проверки` - source found, QA/import not complete.

Never say "генплан проверен" for a raw PDF/JPG or unreviewed vector.

Client UI labels are centralized in `app/urban_plan_labels.py`:

- `passed` is shown as `Генплан/ПДП проверен автоматически` / `проверяется автоматически`.
- `blocked` is shown as `Генплан/ПДП не подтвердил это место` / `не прошло генплан`.
- `unavailable` and `waived` are shown as `Генплан/ПДП не подключен` / `нужна ручная сверка`.
- `pending` or an empty status is shown as `Генплан/ПДП ожидает проверки`.

Web search detail pages, status polling JSON and Telegram reports must use
these labels instead of raw internal statuses. A manual reference URL from
`app/genplan_references.py` is a client fallback, not proof of automatic
genplan verification.

## Execution Order

1. Sync GGK into `urban_plan_sources` from `/admin/urban-plans`.
2. For top commercial cities, build/import three profiles from GGK:
   - `lph-household`
   - `lph-field`
   - `gardening`
3. Implement Smart GeoHub source registry and connector. Source discovery and
   geometry probe are done; full geometry export/import/QA remains.
4. Add admin screen for source coverage:
   - digital imported
   - digital found but review needed
   - PDF/JPG only
   - no source
5. Implement `tools.genplan_vectorize` MVP for reviewed raster plans.
6. Process high-demand PDF/JPG cities through workbench -> vectorize -> review -> import.

## Priority Cities

Start with cities users are already searching or likely to search:

1. Астана
2. Алматы/Алатау
3. Шымкент
4. Актобе
5. Атырау
6. Актау/Жанаозен
7. Кокшетау/Щучинск
8. Костанай/Рудный
9. Тараз
10. Караганда/Темиртау

## Non-Negotiable Safety Rule

Automatic search can use only:

- official digital WFS/GeoJSON that passed mapping review; or
- PDF/JPG-derived vectors that passed independent georeferencing and legend QA.

Everything else is a manual reference, not an automatic decision.

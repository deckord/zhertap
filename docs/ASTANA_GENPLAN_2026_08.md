# Astana Genplan Map Update - 2026-08

## Result

Astana LPH household planning map was refreshed from the public AIS GGK WFS
snapshot on 2026-08-13.

Production verification on 2026-08-17 confirmed that this release is already
imported and active as three `VERIFIED_STRICT/search` rows (`allowed`,
`prohibited`, `red_line`) for `г.Астана`, purpose `ЛПХ:household`. The release
folder itself does not need to be copied to the server at runtime: the imported
geometries are stored in PostgreSQL.

The active GGK map document is still exposed as document `3607` with base
document number `№33`, but the current WFS geometry differs from the previous
local `releases/astana-national-lph` snapshot. The new release therefore keeps
the GGK document identity and uses the latest official legal act as the
approval source.

## Legal Source

- Current legal act: Government Resolution of Kazakhstan `№697`, dated
  `2026-08-04`.
- Official URL: `https://adilet.zan.kz/rus/docs/P2600000697`.
- Base legal act amended by `№697`: Government Resolution `№33`, dated
  `2024-01-25`.
- Base URL: `https://adilet.zan.kz/rus/docs/P2400000033`.

## Release

- Release directory: `releases/astana-national-lph-2026`.
- Release id: `ggk-gp-3607-lph-household-dbbe7e941333`.
- Purpose: `ЛПХ:household`.
- Scope: region `г.Астана`, district `*`, locality `*`.
- Status: `VERIFIED_STRICT/search`.

Layer counts:

| Layer | Features |
| --- | ---: |
| allowed | 1556 |
| prohibited | 5337 |
| red_line | 8165 |

## Import Notes

The GGK builder now supports this normal legal pattern:

1. AIS GGK keeps a map under the base document number.
2. A newer government resolution amends or republishes the genplan.
3. The review file records the latest `legal_act` and the matching
   `base_legal_act`.

This keeps the safety gate in place while allowing the map to carry the current
official approval date and URL.

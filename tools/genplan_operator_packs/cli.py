from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build local HTML packs for manual genplan A1 georeferencing."
    )
    parser.add_argument("--diagnostics-dir", type=Path, required=True)
    parser.add_argument("--workbench-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workbench-url", default="http://127.0.0.1:8765")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--pack-size", type=int, default=10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_operator_packs(
        diagnostics_dir=args.diagnostics_dir,
        workbench_manifest=args.workbench_manifest,
        output=args.output,
        workbench_url=args.workbench_url,
        limit=args.limit,
        pack_size=args.pack_size,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_operator_packs(
    *,
    diagnostics_dir: Path,
    workbench_manifest: Path,
    output: Path,
    workbench_url: str,
    limit: int = 50,
    pack_size: int = 10,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if pack_size <= 0:
        raise ValueError("pack_size must be positive")
    output.mkdir(parents=True, exist_ok=True)
    assets_dir = output / "assets"
    assets_dir.mkdir(exist_ok=True)
    priority_rows = _read_csv(diagnostics_dir / "operator-priority.csv")
    attempt_rows = _attempts_by_asset_basemap(_read_csv(diagnostics_dir / "attempts.csv"))
    manifest = _manifest_by_asset(workbench_manifest)
    selected = priority_rows[:limit]
    cards = [
        _build_card(
            index=index,
            row=row,
            attempts=attempt_rows,
            manifest=manifest,
            output=output,
            assets_dir=assets_dir,
            workbench_url=workbench_url,
        )
        for index, row in enumerate(selected, start=1)
    ]
    packs: list[dict[str, Any]] = []
    for offset in range(0, len(cards), pack_size):
        pack_cards = cards[offset : offset + pack_size]
        pack_number = len(packs) + 1
        filename = f"pack-{pack_number:03d}.html"
        (output / filename).write_text(
            _pack_html(pack_number, pack_cards, output),
            encoding="utf-8",
        )
        packs.append(
            {
                "pack": pack_number,
                "file": str((output / filename).resolve()),
                "records": len(pack_cards),
                "first_rank": pack_cards[0]["rank"] if pack_cards else 0,
                "last_rank": pack_cards[-1]["rank"] if pack_cards else 0,
            }
        )
    index_path = output / "index.html"
    summary = {
        "schema_version": "genplan-operator-packs/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "diagnostics_dir": str(diagnostics_dir.resolve()),
        "workbench_manifest": str(workbench_manifest.resolve()),
        "workbench_url": workbench_url,
        "output": str(output.resolve()),
        "selected_records": len(cards),
        "pack_size": pack_size,
        "packs": packs,
    }
    index_path.write_text(_index_html(summary), encoding="utf-8")
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def _build_card(
    *,
    index: int,
    row: Mapping[str, str],
    attempts: Mapping[tuple[str, str], Mapping[str, str]],
    manifest: Mapping[str, Mapping[str, Any]],
    output: Path,
    assets_dir: Path,
    workbench_url: str,
) -> dict[str, Any]:
    asset_id = row.get("asset_id", "")
    basemap = row.get("best_basemap", "")
    attempt = attempts.get((asset_id, basemap), {})
    copied = {
        key: _copy_artifact(index, asset_id, key, attempt.get(key, ""), output, assets_dir)
        for key in ("plan_preview", "basemap_artifact", "matches")
    }
    record = manifest.get(asset_id, {})
    return {
        "rank": index,
        "asset_id": asset_id,
        "filename": row.get("filename") or record.get("original_filename") or "",
        "region": row.get("region") or record.get("egkn_region") or "",
        "district": row.get("district") or record.get("egkn_district") or "",
        "locality": row.get("locality") or record.get("normalized_locality") or "",
        "basemap": basemap,
        "score": row.get("operator_score", "0"),
        "confidence": row.get("best_confidence", "0"),
        "inliers": row.get("best_inliers", "0"),
        "rmse": row.get("best_rmse_px", "0"),
        "anchors": row.get("best_diagnostic_anchor_count", "0"),
        "reasons": row.get("reasons", ""),
        "workbench_link": f"{workbench_url.rstrip('/')}/?record={asset_id}",
        "artifacts": copied,
    }


def _copy_artifact(
    index: int,
    asset_id: str,
    artifact: str,
    source: str,
    output: Path,
    assets_dir: Path,
) -> str:
    if not source:
        return ""
    path = Path(source)
    if not path.exists() or not path.is_file():
        return ""
    suffix = path.suffix.lower() or ".jpg"
    destination = assets_dir / f"{index:03d}-{asset_id[:10]}-{artifact}{suffix}"
    shutil.copy2(path, destination)
    return destination.relative_to(output).as_posix()


def _pack_html(pack_number: int, cards: Sequence[Mapping[str, Any]], output: Path) -> str:
    cards_html = "\n".join(_card_html(card) for card in cards)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Genplan Pack {pack_number:03d}</title>
  <style>{_css()}</style>
</head>
<body>
  <header>
    <div>
      <a class="back" href="index.html">← Все пачки</a>
      <h1>Пачка {pack_number:03d}</h1>
      <p>Рабочий список для A1-разметки. Диагностика не является утверждением генплана.</p>
    </div>
    <span>{html.escape(str(output))}</span>
  </header>
  <main class="cards">{cards_html}</main>
</body>
</html>
"""


def _card_html(card: Mapping[str, Any]) -> str:
    artifacts = card.get("artifacts", {})
    preview = _image_block("Исходник", artifacts.get("plan_preview", ""))
    basemap = _image_block("Подложка", artifacts.get("basemap_artifact", ""))
    matches = _image_block("Совпадения", artifacts.get("matches", ""))
    workbench_link = _e(card["workbench_link"])
    return f"""
<article class="card">
  <div class="card-head">
    <div>
      <span class="rank">#{card["rank"]}</span>
      <h2>{_e(card["locality"])}</h2>
      <p>{_e(card["region"])} · {_e(card["district"])}</p>
    </div>
    <a class="button" href="{workbench_link}" target="_blank" rel="noopener">
      Открыть в workbench
    </a>
  </div>
  <dl class="metrics">
    <div><dt>Score</dt><dd>{_e(card["score"])}</dd></div>
    <div><dt>Basemap</dt><dd>{_e(card["basemap"])}</dd></div>
    <div><dt>Inliers</dt><dd>{_e(card["inliers"])}</dd></div>
    <div><dt>RMSE</dt><dd>{_e(card["rmse"])}</dd></div>
    <div><dt>Anchors</dt><dd>{_e(card["anchors"])}</dd></div>
  </dl>
  <p class="file">{_e(card["filename"])}</p>
  <p class="reasons">{_e(card["reasons"])}</p>
  <div class="media">{preview}{basemap}{matches}</div>
</article>
"""


def _image_block(label: str, path: str) -> str:
    if not path:
        return (
            f"<figure><figcaption>{_e(label)}</figcaption>"
            '<div class="missing">нет файла</div></figure>'
        )
    return (
        f'<figure><figcaption>{_e(label)}</figcaption>'
        f'<a href="{_e(path)}" target="_blank">'
        f'<img src="{_e(path)}" alt="{_e(label)}"></a></figure>'
    )


def _index_html(summary: Mapping[str, Any]) -> str:
    links = "\n".join(
        f'<a class="pack" href="{_e(Path(pack["file"]).name)}">'
        f'<strong>Пачка {pack["pack"]:03d}</strong>'
        f'<span>#{pack["first_rank"]}-#{pack["last_rank"]} · {pack["records"]} карт</span>'
        "</a>"
        for pack in summary["packs"]
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Genplan Operator Packs</title>
  <style>{_css()}</style>
</head>
<body>
  <header>
    <div>
      <h1>Genplan Operator Packs</h1>
      <p>Пачки для ручной A1-привязки по autoreg-приоритету.</p>
    </div>
    <span>{_e(summary["selected_records"])} записей</span>
  </header>
  <main class="packs">{links}</main>
</body>
</html>
"""


def _css() -> str:
    return """
:root {
  --ink:#17211d;
  --muted:#65746d;
  --line:#d8e0dc;
  --surface:#fff;
  --wash:#f3f6f4;
  --green:#126846;
}
* { box-sizing:border-box; }
body {
  margin:0;
  background:var(--wash);
  color:var(--ink);
  font:14px/1.5 Inter, "Segoe UI", Arial, sans-serif;
}
header {
  position:sticky;
  top:0;
  z-index:2;
  display:flex;
  justify-content:space-between;
  gap:24px;
  align-items:center;
  padding:20px 32px;
  border-bottom:1px solid var(--line);
  background:rgba(255,255,255,.94);
  backdrop-filter:blur(12px);
}
h1,h2,p,dl,figure { margin:0; }
h1 { font-size:28px; line-height:1.15; }
h2 { font-size:22px; line-height:1.2; }
header p, header span, .card p, figcaption, dt, .reasons { color:var(--muted); }
.back { color:var(--green); font-weight:700; text-decoration:none; }
.cards { display:grid; gap:24px; max-width:1440px; margin:0 auto; padding:32px; }
.packs {
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
  gap:16px;
  max-width:1200px;
  margin:0 auto;
  padding:32px;
}
.pack, .card {
  border:1px solid var(--line);
  border-radius:8px;
  background:var(--surface);
  box-shadow:0 12px 40px rgba(20,40,32,.06);
}
.pack {
  display:grid;
  gap:8px;
  padding:20px;
  color:inherit;
  text-decoration:none;
  transition:all .2s ease;
}
.pack:hover { transform:translateY(-2px); border-color:#9fc8b5; }
.pack span { color:var(--muted); }
.card { padding:24px; }
.card-head {
  display:flex;
  justify-content:space-between;
  gap:24px;
  align-items:flex-start;
}
.rank {
  display:inline-block;
  margin-bottom:8px;
  color:var(--green);
  font-weight:800;
  text-transform:uppercase;
}
.button {
  min-height:44px;
  display:inline-grid;
  place-items:center;
  padding:10px 16px;
  border-radius:8px;
  background:var(--green);
  color:white;
  font-weight:800;
  text-decoration:none;
}
.metrics {
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:8px;
  margin-top:20px;
}
.metrics div {
  padding:12px;
  border:1px solid var(--line);
  border-radius:8px;
  background:#f8faf9;
}
dt { font-size:12px; font-weight:700; }
dd { margin:4px 0 0; font-size:18px; font-weight:850; }
.file { margin-top:16px; font-weight:700; overflow-wrap:anywhere; }
.reasons { margin-top:8px; max-width:90ch; overflow-wrap:anywhere; }
.media {
  display:grid;
  grid-template-columns:1fr 1fr 1.4fr;
  gap:16px;
  margin-top:20px;
  align-items:start;
}
figure { display:grid; gap:8px; }
figcaption { font-weight:800; }
img {
  width:100%;
  max-height:520px;
  object-fit:contain;
  border:1px solid var(--line);
  border-radius:8px;
  background:#eef3f0;
}
.missing {
  min-height:180px;
  display:grid;
  place-items:center;
  border:1px dashed var(--line);
  border-radius:8px;
  color:var(--muted);
}
@media (max-width:1000px) {
  .media { grid-template-columns:1fr; }
  .metrics { grid-template-columns:1fr 1fr; }
  header { position:static; }
}
"""


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"CSV does not exist: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _attempts_by_asset_basemap(
    rows: Sequence[Mapping[str, str]],
) -> dict[tuple[str, str], Mapping[str, str]]:
    return {
        (row.get("asset_id", ""), row.get("basemap", "")): row
        for row in rows
        if row.get("asset_id") and row.get("basemap")
    }


def _manifest_by_asset(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else []
    return {
        str(record.get("asset_id")): record
        for record in records
        if isinstance(record, dict) and record.get("asset_id")
    }


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)

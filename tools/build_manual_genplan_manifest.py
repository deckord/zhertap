import csv
import json
from pathlib import Path

SOURCE_MANIFEST = Path(
    r"C:\Users\medadmin\Documents\Codex\genplan\inventory\manifests\manifest.csv"
)
EXTRACTED_ROOT = Path(r"C:\Users\medadmin\Documents\Codex\genplan\extracted")
TARGET = Path("app/data/manual_genplans.json")
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def main() -> None:
    records = []
    with SOURCE_MANIFEST.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            extension = (row.get("extension") or "").casefold()
            if row.get("asset_role") != "plan_document" or extension not in ALLOWED_EXTENSIONS:
                continue
            path = Path(row["extracted_path"])
            try:
                relative_path = path.relative_to(EXTRACTED_ROOT)
            except ValueError:
                continue
            region = row.get("normalized_region") or row.get("original_region") or ""
            district = row.get("normalized_district") or row.get("original_district") or ""
            locality = row.get("normalized_locality") or row.get("original_locality") or ""
            title = locality or district or region or row.get("original_filename") or path.name
            records.append(
                {
                    "asset_id": row["asset_id"],
                    "region": region,
                    "district": district,
                    "locality": locality,
                    "title": f"Генплан/ПДП: {title}",
                    "relative_path": relative_path.as_posix(),
                    "filename": path.name,
                    "extension": extension,
                    "media_type": row.get("media_type") or "application/octet-stream",
                    "size_bytes": int(row.get("size_bytes") or 0),
                    "confidence": row.get("location_confidence") or "",
                }
            )
    records.sort(
        key=lambda item: (
            item["region"],
            item["district"],
            item["locality"],
            item["filename"],
        )
    )
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_root_hint": EXTRACTED_ROOT.as_posix(),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"written {TARGET} with {len(records)} records")


if __name__ == "__main__":
    main()

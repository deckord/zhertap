from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

LAYER_KINDS = {"allowed", "prohibited", "red_line"}


class VectorizeConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ColorRule:
    layer_kind: str
    zone_name: str
    colors: tuple[tuple[int, int, int], ...]
    tolerance: int = 10
    sieve_pixels: int = 32


@dataclass(frozen=True, slots=True)
class VectorizeConfig:
    schema_version: str
    release_id: str
    source_title: str
    rules: tuple[ColorRule, ...]


def parse_hex_color(value: str) -> tuple[int, int, int]:
    raw = value.strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) != 6:
        raise VectorizeConfigError(f"Invalid color {value!r}; expected #RRGGBB")
    try:
        return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as exc:
        raise VectorizeConfigError(f"Invalid color {value!r}; expected #RRGGBB") from exc


def load_config(path: Path) -> VectorizeConfig:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VectorizeConfigError(f"Could not read vectorize config: {exc}") from exc
    if not isinstance(payload, dict):
        raise VectorizeConfigError("Vectorize config root must be an object")
    layers = payload.get("layers")
    if not isinstance(layers, list) or not layers:
        raise VectorizeConfigError("Vectorize config must contain a non-empty layers array")
    rules = tuple(_parse_rule(item) for item in layers)
    return VectorizeConfig(
        schema_version=str(payload.get("schema_version") or "genplan-vectorize/v1"),
        release_id=str(payload.get("release_id") or path.stem),
        source_title=str(payload.get("source_title") or ""),
        rules=rules,
    )


def _parse_rule(item: Any) -> ColorRule:
    if not isinstance(item, dict):
        raise VectorizeConfigError("Each layer rule must be an object")
    layer_kind = str(item.get("layer_kind") or "").strip()
    if layer_kind not in LAYER_KINDS:
        raise VectorizeConfigError(
            f"Invalid layer_kind {layer_kind!r}; expected one of {sorted(LAYER_KINDS)}"
        )
    raw_colors = item.get("colors")
    if not isinstance(raw_colors, list) or not raw_colors:
        raise VectorizeConfigError(f"{layer_kind} rule must contain colors")
    tolerance = int(item.get("tolerance", 10))
    if tolerance < 0 or tolerance > 255:
        raise VectorizeConfigError("tolerance must be between 0 and 255")
    sieve_pixels = int(item.get("sieve_pixels", 32))
    if sieve_pixels < 0:
        raise VectorizeConfigError("sieve_pixels must be zero or positive")
    return ColorRule(
        layer_kind=layer_kind,
        zone_name=str(item.get("zone_name") or layer_kind),
        colors=tuple(parse_hex_color(str(color)) for color in raw_colors),
        tolerance=tolerance,
        sieve_pixels=sieve_pixels,
    )


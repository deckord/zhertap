import json

import pytest

from tools.genplan_vectorize.models import VectorizeConfigError, load_config, parse_hex_color


def test_parse_hex_color_accepts_hash_and_plain_values() -> None:
    assert parse_hex_color("#f4d35e") == (244, 211, 94)
    assert parse_hex_color("000000") == (0, 0, 0)


def test_parse_hex_color_rejects_bad_values() -> None:
    with pytest.raises(VectorizeConfigError):
        parse_hex_color("#12345")


def test_load_vectorize_config(tmp_path) -> None:
    path = tmp_path / "colors.json"
    path.write_text(
        json.dumps(
            {
                "release_id": "test-release",
                "source_title": "Test plan",
                "layers": [
                    {
                        "layer_kind": "allowed",
                        "zone_name": "Allowed",
                        "colors": ["#ffffff"],
                        "tolerance": 8,
                        "sieve_pixels": 12,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.release_id == "test-release"
    assert config.rules[0].layer_kind == "allowed"
    assert config.rules[0].colors == ((255, 255, 255),)
    assert config.rules[0].tolerance == 8
    assert config.rules[0].sieve_pixels == 12


def test_load_vectorize_config_rejects_unknown_layer_kind(tmp_path) -> None:
    path = tmp_path / "colors.json"
    path.write_text(
        json.dumps({"layers": [{"layer_kind": "roads", "colors": ["#ffffff"]}]}),
        encoding="utf-8",
    )

    with pytest.raises(VectorizeConfigError):
        load_config(path)

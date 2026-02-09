"""Orchestration: ties parsing, normalization, filtering, and export together."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kml2report.config import load_config
from kml2report.description_parser import parse_description
from kml2report.export import export_csv, export_json
from kml2report.kml_parser import parse_files
from kml2report.models import Feature


def run_pipeline(
    input_paths: list[str | Path],
    config_path: str | Path | None = None,
    output_path: str | Path | None = None,
    output_format: str | None = None,
) -> list[dict[str, str]]:
    """Full pipeline: parse → normalize → filter → export.

    Returns the list of flat row dicts (also writes to file if output_path given).
    """
    config = load_config(config_path)
    field_map: dict[str, str] = config.get("field_map", {})
    defaults: dict[str, str] = config.get("defaults", {})
    separators: list[str] = config.get("description_separators", [";"])
    filters: list[dict[str, Any]] = config.get("filters", [])
    include_raw: bool = config.get("output", {}).get("include_raw", True)

    fmt = output_format or config.get("output", {}).get("format", "csv")

    features = parse_files([str(p) for p in input_paths])

    for feat in features:
        feat.parsed_attributes = parse_description(
            feat.raw_description,
            field_map=field_map,
            defaults=defaults,
            separators=separators,
        )

    if filters:
        features = _apply_filters(features, filters)

    rows = [f.flat_dict(include_raw=include_raw) for f in features]

    if output_path:
        output_path = Path(output_path)
        if fmt == "json":
            export_json(rows, output_path)
        else:
            export_csv(rows, output_path)

    return rows


def _apply_filters(
    features: list[Feature], filters: list[dict[str, Any]]
) -> list[Feature]:
    """Keep only features matching ALL filter conditions."""
    result: list[Feature] = []
    for feat in features:
        if _matches_all(feat, filters):
            result.append(feat)
    return result


def _matches_all(feat: Feature, filters: list[dict[str, Any]]) -> bool:
    """Check whether a feature satisfies every filter."""
    attrs = feat.parsed_attributes
    for filt in filters:
        field = filt.get("field", "")
        op = filt.get("operator", "eq")
        expected = filt.get("value", "")
        actual = attrs.get(field, "")

        if op == "eq" and actual.lower() != str(expected).lower():
            return False
        if op == "ne" and actual.lower() == str(expected).lower():
            return False
        if op == "in":
            allowed = [str(v).lower() for v in expected] if isinstance(expected, list) else [str(expected).lower()]
            if actual.lower() not in allowed:
                return False
        if op == "contains" and str(expected).lower() not in actual.lower():
            return False

    return True

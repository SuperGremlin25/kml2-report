"""Export utilities for CSV and JSON output."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def export_csv(rows: list[dict[str, str]], path: Path) -> None:
    """Write rows to a CSV file.  Column order is determined by the union of
    all keys across rows, with common identification columns first."""
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    priority_cols = [
        "feature_id",
        "name",
        "layer",
        "source_file",
        "id",
        "type",
        "priority",
        "status",
        "notes",
    ]

    all_keys: list[str] = []
    seen: set[str] = set()
    for col in priority_cols:
        if any(col in r for r in rows):
            all_keys.append(col)
            seen.add(col)
    for row in rows:
        for key in row:
            if key not in seen:
                all_keys.append(key)
                seen.add(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_json(rows: list[dict[str, str]], path: Path) -> None:
    """Write rows to a JSON file, grouped by layer if present."""
    path.parent.mkdir(parents=True, exist_ok=True)

    layers: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        layer = row.get("layer", "")
        layers.setdefault(layer, []).append(row)

    if len(layers) <= 1:
        output = rows
    else:
        output = layers  # type: ignore[assignment]

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

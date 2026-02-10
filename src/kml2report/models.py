"""Data models for parsed KML features."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Geometry:
    """Represents a KML geometry (Point, LineString, Polygon, etc.)."""

    geometry_type: str  # Point, LineString, Polygon, etc.
    coordinates: list[tuple[float, float, float]] = field(default_factory=list)

    @property
    def latitude(self) -> Optional[float]:
        """Return latitude (centroid for multi-coord geometries)."""
        if not self.coordinates:
            return None
        if self.geometry_type == "Point":
            return self.coordinates[0][1]
        return sum(c[1] for c in self.coordinates) / len(self.coordinates)

    @property
    def longitude(self) -> Optional[float]:
        """Return longitude (centroid for multi-coord geometries)."""
        if not self.coordinates:
            return None
        if self.geometry_type == "Point":
            return self.coordinates[0][0]
        return sum(c[0] for c in self.coordinates) / len(self.coordinates)

    def to_wkt(self) -> str:
        """Convert geometry to WKT string."""
        if not self.coordinates:
            return ""

        if self.geometry_type == "Point":
            lon, lat, alt = self.coordinates[0]
            return f"POINT ({lon} {lat})"

        if self.geometry_type == "LineString":
            pairs = ", ".join(f"{c[0]} {c[1]}" for c in self.coordinates)
            return f"LINESTRING ({pairs})"

        if self.geometry_type == "Polygon":
            pairs = ", ".join(f"{c[0]} {c[1]}" for c in self.coordinates)
            return f"POLYGON (({pairs}))"

        return ""


@dataclass
class Feature:
    """A single parsed KML Placemark."""

    placemark_id: str = ""
    name: str = ""
    raw_description: str = ""
    geometry: Optional[Geometry] = None
    parsed_attributes: dict[str, str] = field(default_factory=dict)
    extended_data: dict[str, str] = field(default_factory=dict)
    layer: str = ""
    source_file: str = ""

    def flat_dict(self, include_raw: bool = True) -> dict[str, str]:
        """Return a flat dictionary suitable for CSV/JSON export."""
        row: dict[str, str] = {
            "feature_id": self.placemark_id,
            "name": self.name,
            "layer": self.layer,
            "source_file": self.source_file,
        }

        row.update(self.parsed_attributes)

        for key, val in self.extended_data.items():
            if key not in row:
                row[key] = val

        if self.geometry:
            row["geometry_type"] = self.geometry.geometry_type
            if self.geometry.latitude is not None:
                row["latitude"] = str(self.geometry.latitude)
            if self.geometry.longitude is not None:
                row["longitude"] = str(self.geometry.longitude)
            row["wkt"] = self.geometry.to_wkt()

        if include_raw:
            row["raw_description"] = self.raw_description

        return row

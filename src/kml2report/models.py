"""Data models for parsed KML features."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

try:
    import pyproj
except ImportError:
    pyproj = None

M_TO_FT = 3.28084
Coord = tuple[float, float, float]

_GEOD = pyproj.Geod(ellps="WGS84") if pyproj else None


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(a))


def path_length_m(coords: list[Coord]) -> float:
    """Geodesic length of a coordinate path in meters."""
    if len(coords) < 2:
        return 0.0
    if _GEOD is not None:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        return float(_GEOD.line_length(lons, lats))
    return sum(_haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))


def ring_area_m2(coords: list[Coord]) -> float:
    """Absolute geodesic area of a ring in square meters."""
    if len(coords) < 3:
        return 0.0
    if _GEOD is not None:
        area, _ = _GEOD.polygon_area_perimeter([c[0] for c in coords], [c[1] for c in coords])
        return abs(float(area))
    # Fallback: equirectangular projection around centroid + shoelace
    lat0 = math.radians(sum(c[1] for c in coords) / len(coords))
    pts = [(math.radians(c[0]) * 6371008.8 * math.cos(lat0), math.radians(c[1]) * 6371008.8) for c in coords]
    s = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    return abs(s) / 2


@dataclass
class Geometry:
    """A KML geometry: Point, LineString, LinearRing, Polygon, Track or a multi-part collection.

    ``coordinates`` is always the flat list of every vertex (outer boundary first
    for polygons) so older code keeps working.  ``holes`` holds polygon inner
    rings and ``parts`` holds the members of a MultiGeometry for exact
    measurement.
    """

    geometry_type: str
    coordinates: list[Coord] = field(default_factory=list)
    holes: list[list[Coord]] = field(default_factory=list)
    parts: list["Geometry"] = field(default_factory=list)
    times: list[str] = field(default_factory=list)  # gx:Track timestamps

    # ---- convenience ----------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self.coordinates and not any(not p.is_empty for p in self.parts)

    def iter_simple(self):
        """Yield every non-collection geometry (self if it is one)."""
        if self.parts:
            for p in self.parts:
                yield from p.iter_simple()
        else:
            yield self

    @property
    def latitude(self) -> Optional[float]:
        """Latitude (vertex-average centroid for multi-vertex geometries)."""
        if not self.coordinates:
            return None
        if self.geometry_type == "Point":
            return self.coordinates[0][1]
        return sum(c[1] for c in self.coordinates) / len(self.coordinates)

    @property
    def longitude(self) -> Optional[float]:
        if not self.coordinates:
            return None
        if self.geometry_type == "Point":
            return self.coordinates[0][0]
        return sum(c[0] for c in self.coordinates) / len(self.coordinates)

    @property
    def bounds(self) -> Optional[tuple[float, float, float, float]]:
        """(min_lon, min_lat, max_lon, max_lat)."""
        if not self.coordinates:
            return None
        xs = [c[0] for c in self.coordinates]
        ys = [c[1] for c in self.coordinates]
        return (min(xs), min(ys), max(xs), max(ys))

    # ---- measurement ----------------------------------------------------
    @property
    def length_m(self) -> float:
        """Geodesic length of line geometry (Tracks/LineStrings/rings perimeter excluded)."""
        total = 0.0
        for g in self.iter_simple():
            if g.geometry_type in ("LineString", "Track"):
                total += path_length_m(g.coordinates)
        return total

    @property
    def length_feet(self) -> float:
        return self.length_m * M_TO_FT

    @property
    def perimeter_m(self) -> float:
        total = 0.0
        for g in self.iter_simple():
            if g.geometry_type in ("Polygon", "LinearRing") and g.coordinates:
                ring = g.coordinates
                if ring[0] != ring[-1]:
                    ring = ring + [ring[0]]
                total += path_length_m(ring)
        return total

    @property
    def area_m2(self) -> float:
        total = 0.0
        for g in self.iter_simple():
            if g.geometry_type in ("Polygon", "LinearRing") and g.coordinates:
                total += ring_area_m2(g.coordinates) - sum(ring_area_m2(h) for h in g.holes)
        return max(total, 0.0)

    @property
    def area_acres(self) -> float:
        return self.area_m2 / 4046.8564224

    @property
    def area_sqft(self) -> float:
        return self.area_m2 * M_TO_FT**2

    # ---- serialisation ---------------------------------------------------
    def to_wkt(self) -> str:
        if self.is_empty:
            return ""

        def pairs(cs: list[Coord]) -> str:
            return ", ".join(f"{c[0]} {c[1]}" for c in cs)

        def closed(cs: list[Coord]) -> list[Coord]:
            return cs if cs and cs[0] == cs[-1] else cs + cs[:1]

        t = self.geometry_type
        if self.parts:
            inner = ", ".join(p.to_wkt() for p in self.parts if not p.is_empty)
            return f"GEOMETRYCOLLECTION ({inner})"
        if t == "Point":
            return f"POINT ({self.coordinates[0][0]} {self.coordinates[0][1]})"
        if t in ("LineString", "Track"):
            return f"LINESTRING ({pairs(self.coordinates)})"
        if t in ("Polygon", "LinearRing"):
            rings = [closed(self.coordinates)] + [closed(h) for h in self.holes]
            return "POLYGON (" + ", ".join(f"({pairs(r)})" for r in rings) + ")"
        return ""

    def to_geojson(self) -> Optional[dict]:
        """GeoJSON geometry object (RFC 7946 field order: lon, lat[, alt])."""
        if self.is_empty:
            return None

        def pt(c: Coord) -> list[float]:
            return [c[0], c[1], c[2]]

        def closed(cs: list[Coord]) -> list[Coord]:
            return cs if cs and cs[0] == cs[-1] else cs + cs[:1]

        if self.parts:
            geoms = [g for g in (p.to_geojson() for p in self.parts) if g]
            return {"type": "GeometryCollection", "geometries": geoms}
        t = self.geometry_type
        if t == "Point":
            return {"type": "Point", "coordinates": pt(self.coordinates[0])}
        if t in ("LineString", "Track"):
            if len(self.coordinates) < 2:
                return {"type": "Point", "coordinates": pt(self.coordinates[0])}
            return {"type": "LineString", "coordinates": [pt(c) for c in self.coordinates]}
        if t in ("Polygon", "LinearRing"):
            rings = [closed(self.coordinates)] + [closed(h) for h in self.holes]
            return {"type": "Polygon", "coordinates": [[pt(c) for c in r] for r in rings]}
        return None

    # Backwards-compatible alias (older code called this on LineStrings only)
    def _legacy_length(self) -> float:  # pragma: no cover
        return self.length_feet


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
    folder_path: str = ""
    style: dict[str, str] = field(default_factory=dict)
    time_begin: str = ""
    time_end: str = ""

    def flat_dict(self, include_raw: bool = True) -> dict[str, str]:
        """Flat dictionary suitable for CSV/JSON export."""
        row: dict[str, str] = {
            "feature_id": self.placemark_id,
            "name": self.name,
            "layer": self.layer,
            "source_file": self.source_file,
        }
        if self.folder_path and self.folder_path != self.layer:
            row["folder_path"] = self.folder_path

        row.update(self.parsed_attributes)

        for key, val in self.extended_data.items():
            if key not in row:
                row[key] = val

        if self.geometry and not self.geometry.is_empty:
            g = self.geometry
            row["geometry_type"] = g.geometry_type
            if g.latitude is not None:
                row["latitude"] = str(g.latitude)
            if g.longitude is not None:
                row["longitude"] = str(g.longitude)
            if g.length_m:
                row["calc_length_ft"] = f"{g.length_feet:.2f}"
            if g.area_m2:
                row["calc_area_sqft"] = f"{g.area_sqft:.2f}"
                row["calc_area_acres"] = f"{g.area_acres:.4f}"
            row["wkt"] = g.to_wkt()

        if include_raw:
            row["raw_description"] = self.raw_description

        return row

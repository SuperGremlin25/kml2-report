"""Data models for parsed KML features."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

try:
    import pyproj
except ImportError:
    pyproj = None

import math


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
    
    @property
    def length_feet(self) -> float:
        """Calculate length of LineString geometry in feet."""
        if self.geometry_type != "LineString":
            return 0.0
        
        if not self.coordinates or len(self.coordinates) < 2:
            return 0.0
        
        # Use pyproj if available for accurate geodesic calculations
        if pyproj:
            geod = pyproj.Geod(ellps='WGS84')
            total_length = 0.0
            
            for i in range(len(self.coordinates) - 1):
                lon1, lat1 = self.coordinates[i][:2]
                lon2, lat2 = self.coordinates[i + 1][:2]
                _, _, segment_length = geod.inv(lon1, lat1, lon2, lat2)
                total_length += segment_length
            
            # Convert to feet
            return total_length * 3.28084
        else:
            # Fallback to simple haversine approximation
            total_length = 0.0
            
            for i in range(len(self.coordinates) - 1):
                lon1, lat1 = self.coordinates[i][:2]
                lon2, lat2 = self.coordinates[i + 1][:2]
                
                # Convert to radians
                lat1, lon1 = math.radians(lat1), math.radians(lon1)
                lat2, lon2 = math.radians(lat2), math.radians(lon2)
                
                # Haversine formula
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                a = (math.sin(dlat/2)**2 + 
                     math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2)
                c = 2 * math.asin(math.sqrt(a))
                
                # Earth radius in meters
                R = 6371000
                segment_length = R * c
                
                # Convert to feet
                total_length += segment_length * 3.28084
            
            return total_length


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

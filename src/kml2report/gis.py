"""GIS toolkit: attribute tables, reprojection, spatial analysis and QA.

Everything here works on lists of :class:`~kml2report.models.Feature`.  Shapely
and pyproj are used for the heavy lifting; measurements are geodesic (WGS84).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import pandas as pd
from pyproj import CRS, Transformer
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, shape, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from kml2report.description_parser import parse_description
from kml2report.models import Feature, M_TO_FT

WGS84 = "EPSG:4326"
_GROUP_KEY = object()


# ---------------------------------------------------------------------------
# Geometry conversion
# ---------------------------------------------------------------------------

def to_shapely(feature: Feature) -> Optional[BaseGeometry]:
    """Feature geometry as a shapely object (lon/lat), or None."""
    if not feature.geometry or feature.geometry.is_empty:
        return None
    gj = feature.geometry.to_geojson()
    if not gj:
        return None
    try:
        geom = shape(gj)
    except Exception:
        return None
    return geom if not geom.is_empty else None


def feature_attributes(feature: Feature) -> dict[str, str]:
    row = feature.flat_dict(include_raw=False)
    for key, value in parse_description(feature.raw_description).items():
        if key not in feature.parsed_attributes and key not in {
            "geometry_type", "latitude", "longitude", "calc_length_ft", "calc_area_sqft", "calc_area_acres", "wkt",
        }:
            row[key] = value
    row.pop("wkt", None)
    return row


def features_to_geojson(features: Iterable[Feature], properties: bool = True) -> dict:
    out = []
    for f in features:
        gj = f.geometry.to_geojson() if f.geometry else None
        props = feature_attributes(f) if properties else {}
        if f.style:
            props.update({f"style_{k}": v for k, v in f.style.items()})
        out.append({"type": "Feature", "id": f.placemark_id or None, "geometry": gj, "properties": props})
    return {"type": "FeatureCollection", "features": out}


def attribute_table(features: list[Feature]) -> pd.DataFrame:
    """Attribute table (one row per feature) like a GIS layer's table view."""
    rows = []
    for i, f in enumerate(features):
        r = feature_attributes(f)
        r["_idx"] = i
        rows.append(r)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in ("calc_length_ft", "calc_area_sqft", "calc_area_acres", "latitude", "longitude"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    lead = [c for c in ("name", "layer", "geometry_type", "calc_length_ft", "calc_area_acres") if c in df]
    return df[lead + [c for c in df.columns if c not in lead]]


def extract_vertices(features: list[Feature]) -> pd.DataFrame:
    """Every vertex of every feature with station (cumulative distance) -- the
    'rip the coordinates out' table."""
    from kml2report.models import path_length_m

    rows = []
    for fi, f in enumerate(features):
        if not f.geometry:
            continue
        for part_no, g in enumerate(f.geometry.iter_simple()):
            rings = [g.coordinates, *g.holes] if g.geometry_type in ("Polygon", "LinearRing") else [g.coordinates]
            for ring_no, ring in enumerate(rings):
                cum = 0.0
                prev = None
                for vi, (lon, lat, alt) in enumerate(ring):
                    if prev is not None:
                        cum += path_length_m([prev, (lon, lat, alt)]) * M_TO_FT
                    prev = (lon, lat, alt)
                    rows.append({
                        "feature": f.name or f.placemark_id, "layer": f.layer, "geometry_type": g.geometry_type,
                        "part": part_no, "ring": ring_no, "vertex": vi, "lon": lon, "lat": lat, "elev_m": alt,
                        "station_ft": round(cum, 2),
                    })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Coordinate reference systems
# ---------------------------------------------------------------------------

def utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def centroid_lonlat(features: list[Feature]) -> Optional[tuple[float, float]]:
    geoms = [g for g in (to_shapely(f) for f in features) if g is not None]
    if not geoms:
        return None
    c = GeometryCollection(geoms).centroid
    return c.x, c.y


def transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def project_geom(geom: BaseGeometry, dst_epsg: str, src: str = WGS84) -> BaseGeometry:
    t = transformer(src, dst_epsg)
    return transform(t.transform, geom)


def crs_label(epsg: str) -> str:
    try:
        c = CRS.from_user_input(epsg)
        unit = c.axis_info[0].unit_name if c.axis_info else ""
        return f"{c.name} ({unit})"
    except Exception:
        return epsg


def project_vertices(features: list[Feature], dst_epsg: str) -> pd.DataFrame:
    """Vertex table with projected X/Y (e.g. State Plane feet) added."""
    df = extract_vertices(features)
    if df.empty:
        return df
    t = transformer(WGS84, dst_epsg)
    xs, ys = t.transform(df["lon"].tolist(), df["lat"].tolist())
    df["x"], df["y"] = xs, ys
    return df


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def layer_stats(features: list[Feature]) -> pd.DataFrame:
    """Per-layer counts, total length and area."""
    acc: dict[str, dict] = defaultdict(lambda: {"features": 0, "points": 0, "lines": 0, "polygons": 0,
                                                 "length_ft": 0.0, "area_acres": 0.0})
    for f in features:
        key = f.folder_path or f.layer or "(no folder)"
        a = acc[key]
        a["features"] += 1
        g = f.geometry
        if not g:
            continue
        kinds = {p.geometry_type for p in g.iter_simple()}
        a["points"] += int("Point" in kinds)
        a["lines"] += int(bool(kinds & {"LineString", "Track"}))
        a["polygons"] += int(bool(kinds & {"Polygon", "LinearRing"}))
        a["length_ft"] += g.length_feet
        a["area_acres"] += g.area_acres
    df = pd.DataFrame([{"layer": k, **v} for k, v in acc.items()])
    if not df.empty:
        df["length_ft"] = df["length_ft"].round(1)
        df["area_acres"] = df["area_acres"].round(3)
    return df


def group_summary(features: list[Feature], field: str) -> pd.DataFrame:
    """Group by any attribute -> count, total length, total area (a pivot table)."""
    rows = []
    for f in features:
        attrs = feature_attributes(f)
        g = f.geometry
        rows.append({
            _GROUP_KEY: attrs.get(field, "(blank)") or "(blank)",
            "length_ft": g.length_feet if g else 0.0,
            "area_acres": g.area_acres if g else 0.0,
        })
    if not rows:
        return pd.DataFrame()
    output_field = f"{field}_group" if field in {"count", "length_ft", "area_acres"} else field
    df = pd.DataFrame(rows).groupby(_GROUP_KEY).agg(
        count=("length_ft", "size"), length_ft=("length_ft", "sum"), area_acres=("area_acres", "sum")
    ).reset_index().rename(columns={_GROUP_KEY: output_field}).sort_values("count", ascending=False)
    df["length_ft"] = df["length_ft"].round(1)
    df["area_acres"] = df["area_acres"].round(3)
    return df


def bounds_of(features: list[Feature]) -> Optional[tuple[float, float, float, float]]:
    bs = [f.geometry.bounds for f in features if f.geometry and f.geometry.bounds]
    if not bs:
        return None
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


# ---------------------------------------------------------------------------
# Spatial analysis
# ---------------------------------------------------------------------------

def _local_proj(features: list[Feature]) -> Optional[str]:
    c = centroid_lonlat(features)
    return f"EPSG:{utm_epsg(*c)}" if c else None


def buffer_features(features: list[Feature], distance_ft: float) -> dict:
    """Buffer every feature by ``distance_ft`` and return GeoJSON polygons (WGS84)."""
    epsg = _local_proj(features)
    if not epsg:
        return {"type": "FeatureCollection", "features": []}
    fwd, back = transformer(WGS84, epsg), transformer(epsg, WGS84)
    out = []
    for f in features:
        g = to_shapely(f)
        if g is None:
            continue
        buf = transform(back.transform, transform(fwd.transform, g).buffer(distance_ft / M_TO_FT))
        out.append({"type": "Feature", "geometry": mapping(buf),
                    "properties": {"name": f.name, "layer": f.layer, "buffer_ft": distance_ft}})
    return {"type": "FeatureCollection", "features": out}


def points_in_polygons(features: list[Feature], poly_layer: Optional[str] = None) -> pd.DataFrame:
    """Spatial join: which polygon contains each point feature."""
    polys = [(f, to_shapely(f)) for f in features
             if f.geometry and f.geometry.geometry_type in ("Polygon", "LinearRing")
             and (poly_layer is None or (f.folder_path or f.layer) == poly_layer)]
    polys = [(f, g) for f, g in polys if g is not None]
    rows = []
    for f in features:
        if not f.geometry or f.geometry.geometry_type != "Point":
            continue
        pt = to_shapely(f)
        if pt is None:
            continue
        hit = next((pf for pf, pg in polys if pg.contains(pt)), None)
        rows.append({"point": f.name or f.placemark_id, "point_layer": f.layer,
                     "inside_polygon": hit.name if hit else "", "polygon_layer": hit.layer if hit else ""})
    return pd.DataFrame(rows)


def nearest_features(features: list[Feature], source: Feature, n: int = 5) -> pd.DataFrame:
    """The n closest other features to ``source`` with distance in feet."""
    all_feats = [f for f in features if f is not source]
    src = to_shapely(source)
    epsg = _local_proj(features)
    if src is None or not epsg:
        return pd.DataFrame()
    fwd = transformer(WGS84, epsg)
    sp = transform(fwd.transform, src)
    rows = []
    for f in all_feats:
        g = to_shapely(f)
        if g is None:
            continue
        rows.append({"name": f.name or f.placemark_id, "layer": f.layer,
                     "geometry_type": f.geometry.geometry_type,
                     "distance_ft": round(sp.distance(transform(fwd.transform, g)) * M_TO_FT, 1)})
    return pd.DataFrame(sorted(rows, key=lambda r: r["distance_ft"])[:n])


def elevation_profile(feature: Feature) -> pd.DataFrame:
    """Station vs elevation (m & ft) for a line/track."""
    from kml2report.models import path_length_m

    if not feature.geometry:
        return pd.DataFrame()
    g = next((p for p in feature.geometry.iter_simple() if p.geometry_type in ("LineString", "Track")), None)
    if not g:
        return pd.DataFrame()
    cum, prev, rows = 0.0, None, []
    for lon, lat, alt in g.coordinates:
        if prev:
            cum += path_length_m([prev, (lon, lat, alt)]) * M_TO_FT
        prev = (lon, lat, alt)
        rows.append({"station_ft": round(cum, 1), "elev_ft": round(alt * M_TO_FT, 1), "elev_m": alt})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# QA / QC
# ---------------------------------------------------------------------------

@dataclass
class QAIssue:
    severity: str  # error | warning | info
    check: str
    feature: str
    layer: str
    detail: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def run_qa(features: list[Feature], snap_tolerance_ft: float = 3.0, min_length_ft: float = 1.0) -> list[QAIssue]:
    """Quality checks an as-built / design reviewer cares about."""
    issues: list[QAIssue] = []
    epsg = _local_proj(features)
    fwd = transformer(WGS84, epsg) if epsg else None
    tol_m = snap_tolerance_ft / M_TO_FT

    seen_names: dict[tuple[str, str], int] = defaultdict(int)
    lines: list[tuple[Feature, LineString]] = []
    seen_geoms: dict[str, Feature] = {}

    for f in features:
        label = f.name or f.placemark_id or "(unnamed)"
        if not f.geometry or f.geometry.is_empty:
            issues.append(QAIssue("warning", "No geometry", label, f.layer, "Placemark has no usable coordinates"))
            continue
        if not f.name:
            issues.append(QAIssue("info", "Unnamed", label, f.layer, "Placemark has no name"))
        seen_names[(f.layer, f.name)] += 1
        g = f.geometry
        wkt = g.to_wkt()
        if wkt in seen_geoms:
            issues.append(QAIssue("warning", "Duplicate geometry", label, f.layer,
                                  f"Identical geometry to '{seen_geoms[wkt].name}'"))
        else:
            seen_geoms[wkt] = f
        simple_geometries = list(g.iter_simple())
        line_parts = [part for part in simple_geometries if part.geometry_type in ("LineString", "Track")]
        if line_parts:
            if g.length_feet < min_length_ft:
                issues.append(QAIssue("warning", "Near-zero length", label, f.layer, f"{g.length_feet:.2f} ft"))
            for part in line_parts:
                try:
                    sg = shape(part.to_geojson())
                except Exception:
                    sg = None
                if sg is not None and sg.geom_type == "LineString":
                    if not sg.is_simple:
                        issues.append(QAIssue("warning", "Self-intersecting line", label, f.layer, "Line crosses itself"))
                    if fwd:
                        lines.append((f, transform(fwd.transform, sg)))
            # declared vs measured length
            declared = _declared_length(f)
            if declared and g.length_feet:
                diff = abs(declared - g.length_feet) / declared * 100
                if diff > 10:
                    issues.append(QAIssue("warning", "Length mismatch", label, f.layer,
                                          f"Attribute {declared:,.1f} ft vs measured {g.length_feet:,.1f} ft ({diff:.0f}% off)"))
        for part in simple_geometries:
            if part.geometry_type in ("Polygon", "LinearRing"):
                try:
                    sg = shape(part.to_geojson())
                except Exception:
                    sg = None
                if sg is None or not sg.is_valid:
                    issues.append(QAIssue("error", "Invalid polygon", label, f.layer,
                                          "Polygon is invalid (self-intersection or bad ring)"))
            elif part.geometry_type == "Point" and part.coordinates:
                if abs(part.coordinates[0][0]) < 1e-9 and abs(part.coordinates[0][1]) < 1e-9:
                    issues.append(QAIssue("error", "Null island", label, f.layer, "Point is at 0,0"))

    for (layer, name), n in seen_names.items():
        if name and n > 1:
            issues.append(QAIssue("info", "Duplicate name", name, layer, f"{n} features share this name"))

    # dangling ends: a line endpoint that is neither near another line nor a point asset
    if fwd and lines:
        points = [transform(fwd.transform, to_shapely(f)) for f in features
                  if f.geometry and f.geometry.geometry_type == "Point" and to_shapely(f) is not None]
        for i, (f, ln) in enumerate(lines):
            for end_name, end in (("start", Point(ln.coords[0])), ("end", Point(ln.coords[-1]))):
                near_line = any(j != i and end.distance(other) <= tol_m for j, (_, other) in enumerate(lines))
                near_pt = any(end.distance(p) <= tol_m * 3 for p in points)
                if not near_line and not near_pt:
                    issues.append(QAIssue("info", "Dangling end", f.name or f.placemark_id, f.layer,
                                          f"{end_name} of line is not connected to another line or point within {snap_tolerance_ft:g} ft"))
    return issues


def _declared_length(f: Feature) -> float:
    import re
    from kml2report.geometry_analyzer import parse_description_html

    attrs = {**parse_description_html(f.raw_description), **f.extended_data}
    for key in ("New_Length", "Length", "length", "LENGTH", "Shape_Length"):
        if key in attrs:
            try:
                return float(re.sub(r"[^\d.]", "", str(attrs[key])) or 0)
            except ValueError:
                return 0.0
    return 0.0

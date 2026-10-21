"""GIS / CAD export: GeoJSON, Shapefile (zip), DXF, KML, GeoPackage-free."""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Iterable, Optional
from xml.sax.saxutils import escape

from pyproj import CRS

from kml2report.gis import WGS84, feature_attributes, features_to_geojson, transformer, to_shapely, centroid_lonlat, utm_epsg
from kml2report.models import Feature


def to_geojson_bytes(features: Iterable[Feature]) -> bytes:
    return json.dumps(features_to_geojson(features), indent=2, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Shapefile (one set per geometry family, zipped, with .prj)
# ---------------------------------------------------------------------------

def _dbf_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name)[:10] or "F"
    cand, n = base, 1
    while cand.lower() in used:
        suffix = str(n)
        cand = base[: 10 - len(suffix)] + suffix
        n += 1
    used.add(cand.lower())
    return cand


def to_shapefile_zip(features: list[Feature], dst_epsg: str = WGS84) -> bytes:
    """Zip of point/line/polygon shapefiles (ESRI-compatible, with .prj and .cpg)."""
    import shapefile

    t = None if dst_epsg == WGS84 else transformer(WGS84, dst_epsg)
    wkt = CRS.from_user_input(dst_epsg).to_wkt(version="WKT1_ESRI")

    families = {"points": ("Point", shapefile.POINT), "lines": ("Line", shapefile.POLYLINE),
                "polygons": ("Polygon", shapefile.POLYGON)}
    buckets: dict[str, list[Feature]] = {k: [] for k in families}
    for f in features:
        if not f.geometry or f.geometry.is_empty:
            continue
        for g in f.geometry.iter_simple():
            fam = ("points" if g.geometry_type == "Point"
                   else "lines" if g.geometry_type in ("LineString", "Track")
                   else "polygons")
            buckets[fam].append((f, g))  # type: ignore[arg-type]

    def xy(c):
        return t.transform(c[0], c[1]) if t else (c[0], c[1])

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for fam, items in buckets.items():
            if not items:
                continue
            attrs_all = [feature_attributes(f) for f, _ in items]
            keys: list[str] = []
            for a in attrs_all:
                for k in a:
                    if k not in keys and k not in ("wkt", "raw_description"):
                        keys.append(k)
            used: set[str] = set()
            field_names = {k: _dbf_name(k, used) for k in keys}
            buf = {ext: io.BytesIO() for ext in ("shp", "shx", "dbf")}
            w = shapefile.Writer(shp=buf["shp"], shx=buf["shx"], dbf=buf["dbf"], shapeType=families[fam][1])
            w.autoBalance = True
            for k in keys:
                w.field(field_names[k], "C", size=254)
            for (f, g), a in zip(items, attrs_all):
                if fam == "points":
                    x, y = xy(g.coordinates[0])
                    w.point(x, y)
                elif fam == "lines":
                    w.line([[list(xy(c)) for c in g.coordinates]])
                else:
                    def ring(cs, clockwise):
                        cs = cs if cs[0] == cs[-1] else cs + cs[:1]
                        pts = [list(xy(c)) for c in cs]
                        area = sum(p[0] * q[1] - q[0] * p[1] for p, q in zip(pts, pts[1:]))
                        if (area < 0) != clockwise:  # shapefile: outer rings clockwise, holes counter-clockwise
                            pts.reverse()
                        return pts
                    w.poly([ring(g.coordinates, True)] + [ring(h, False) for h in g.holes])
                w.record(*[str(a.get(k, ""))[:254] for k in keys])
            w.close()
            for ext, b in buf.items():
                zf.writestr(f"{fam}.{ext}", b.getvalue())
            zf.writestr(f"{fam}.prj", wkt)
            zf.writestr(f"{fam}.cpg", "UTF-8")
            zf.writestr(f"{fam}_fields.txt", "\n".join(f"{field_names[k]} = {k}" for k in keys))
    return out.getvalue()


# ---------------------------------------------------------------------------
# DXF (AutoCAD / Civil 3D / MicroStation friendly)
# ---------------------------------------------------------------------------

def to_dxf_bytes(features: list[Feature], dst_epsg: Optional[str] = None, units_feet: bool = True,
                 label_points: bool = True) -> bytes:
    """Write features to DXF in a projected CRS (default: local UTM, converted to feet).

    Layers follow the KML folder names; points become POINT + MTEXT labels.
    """
    import ezdxf

    if dst_epsg is None:
        c = centroid_lonlat(features) or (0.0, 0.0)
        dst_epsg = f"EPSG:{utm_epsg(*c)}"
    t = transformer(WGS84, dst_epsg)
    scale = 3.280839895 if units_feet and CRS.from_user_input(dst_epsg).axis_info[0].unit_name == "metre" else 1.0

    def xy(c):
        x, y = t.transform(c[0], c[1])
        return (x * scale, y * scale)

    doc = ezdxf.new("R2010", setup=True)
    doc.units = 2 if units_feet else 6
    msp = doc.modelspace()

    def layer_name(f: Feature) -> str:
        raw = (f.layer or f.folder_path or "0").upper()
        name = re.sub(r"[^A-Z0-9_\-$]", "_", raw)[:200] or "0"
        if name not in doc.layers:
            doc.layers.add(name)
        return name

    for f in features:
        if not f.geometry or f.geometry.is_empty:
            continue
        layer = layer_name(f)
        for g in f.geometry.iter_simple():
            if g.geometry_type == "Point":
                p = xy(g.coordinates[0])
                msp.add_point(p, dxfattribs={"layer": layer})
                if label_points and f.name:
                    msp.add_text(f.name, height=2.0 if units_feet else 0.6,
                                 dxfattribs={"layer": layer}).set_placement((p[0] + 1.5, p[1] + 1.5))
            elif g.geometry_type in ("LineString", "Track"):
                if len(g.coordinates) >= 2:
                    msp.add_lwpolyline([xy(c) for c in g.coordinates], dxfattribs={"layer": layer})
            elif g.geometry_type in ("Polygon", "LinearRing"):
                msp.add_lwpolyline([xy(c) for c in g.coordinates], close=True, dxfattribs={"layer": layer})
                for h in g.holes:
                    msp.add_lwpolyline([xy(c) for c in h], close=True, dxfattribs={"layer": layer})
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# KML round-trip (cleaned, flattened)
# ---------------------------------------------------------------------------

def to_kml_bytes(features: list[Feature], name: str = "kml2report export") -> bytes:
    def coords(cs):
        return " ".join(f"{c[0]},{c[1]},{c[2]}" for c in cs)

    def geom_xml(g):
        if g.parts:
            return "<MultiGeometry>" + "".join(geom_xml(p) for p in g.parts) + "</MultiGeometry>"
        t = g.geometry_type
        if t == "Point":
            return f"<Point><coordinates>{coords(g.coordinates[:1])}</coordinates></Point>"
        if t in ("LineString", "Track"):
            return f"<LineString><coordinates>{coords(g.coordinates)}</coordinates></LineString>"
        inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{coords(h)}</coordinates></LinearRing></innerBoundaryIs>"
                        for h in g.holes)
        return (f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords(g.coordinates)}</coordinates>"
                f"</LinearRing></outerBoundaryIs>{inner}</Polygon>")

    folders: dict[str, list[str]] = {}
    for f in features:
        attrs = feature_attributes(f)
        ext = "".join(f'<Data name="{escape(k, {chr(34): "&quot;"})}"><value>{escape(str(v))}</value></Data>'
                      for k, v in attrs.items() if k not in ("name", "wkt", "raw_description"))
        g = geom_xml(f.geometry) if f.geometry and not f.geometry.is_empty else ""
        folders.setdefault(f.layer or "Features", []).append(
            f"<Placemark><name>{escape(f.name)}</name><ExtendedData>{ext}</ExtendedData>{g}</Placemark>")
    body = "".join(f"<Folder><name>{escape(k)}</name>{''.join(v)}</Folder>" for k, v in folders.items())
    return (f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2">'
            f"<Document><name>{escape(name)}</name>{body}</Document></kml>").encode("utf-8")

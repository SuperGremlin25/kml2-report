"""GIS / CAD export: GeoJSON, Shapefile (zip), DXF, KML, GeoPackage-free."""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import zipfile
from typing import Iterable, Optional
from xml.sax.saxutils import escape
from urllib.parse import quote, unquote

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
    # metres per CRS unit (1 for metre CRSs, 0.3048006 for US survey feet), then to the requested output unit
    to_m = CRS.from_user_input(dst_epsg).axis_info[0].unit_conversion_factor or 1.0
    scale = to_m * (3.280839895 if units_feet else 1.0)

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
                    msp.add_text(f.name, dxfattribs={"layer": layer, "height": 2.0 if units_feet else 0.6}
                                 ).set_placement((p[0] + 1.5, p[1] + 1.5))
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

def _kml_color(hex_rgb: str, alpha: str = "ff") -> str:
    h = hex_rgb.lstrip("#")
    return f"{alpha}{h[4:6]}{h[2:4]}{h[0:2]}"


def _geom_xml(g) -> str:
    def coords(cs):
        return " ".join(f"{c[0]},{c[1]},{c[2]}" for c in cs)

    if g.parts:
        return "<MultiGeometry>" + "".join(_geom_xml(p) for p in g.parts) + "</MultiGeometry>"
    t = g.geometry_type
    if t == "Point":
        return f"<Point><coordinates>{coords(g.coordinates[:1])}</coordinates></Point>"
    if t in ("LineString", "Track"):
        return f"<LineString><coordinates>{coords(g.coordinates)}</coordinates></LineString>"
    inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{coords(h)}</coordinates></LinearRing></innerBoundaryIs>"
                    for h in g.holes)
    return (f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords(g.coordinates)}</coordinates>"
            f"</LinearRing></outerBoundaryIs>{inner}</Polygon>")


_KML_OPEN = ('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2" '
             'xmlns:gx="http://www.google.com/kml/ext/2.2" xmlns:kml="http://www.opengis.net/kml/2.2" '
             'xmlns:atom="http://www.w3.org/2005/Atom">')
_HREF = re.compile(r"(<(?:[\w.-]+:)?href\b[^>]*>\s*)([^<]+?)(\s*</(?:[\w.-]+:)?href>)")
_STYLE_URL = re.compile(r"(<(?:[\w.-]+:)?styleUrl\b[^>]*>\s*)([^<]+?)(\s*</(?:[\w.-]+:)?styleUrl>)")
_ID_ATTR = re.compile(r'^(<(?:[\w.-]+:)?(?:Style|StyleMap)\b[^>]*?\bid=")[^"]*(")')


def _set_id(xml: str, new_id: str) -> str:
    return _ID_ATTR.sub(lambda m: f"{m.group(1)}{new_id}{m.group(2)}", xml, count=1)


class _StyleBook:
    """Collects the original <Style>/<StyleMap> definitions (and icon images) the exported features use, so the
    user's own icons, scales, label styles and StyleMaps are reproduced exactly instead of replaced by defaults."""

    def __init__(self) -> None:
        self.defs: dict[str, str] = {}         # export id -> xml
        self.resources: dict[str, bytes] = {}  # href inside the package -> image bytes
        self._owner: dict[tuple, str] = {}     # (source definitions, original id) -> export id

    def adopt_resources(self, xml: str, f: Feature) -> str:
        """Record the icon files an xml fragment points at; keep their paths unique and inside the package."""
        def fix(m: "re.Match[str]") -> str:
            href = unquote(html.unescape(m.group(2)).strip())
            data = f.resources.get(href)
            if data is None:
                return m.group(0)
            key = href
            unsafe = key.startswith(("/", "../")) or "/../" in key
            if unsafe or (key in self.resources and self.resources[key] != data):
                key = f"files/{hashlib.sha1(data).hexdigest()[:8]}_{key.rsplit('/', 1)[-1]}"
            self.resources[key] = data
            return f"{m.group(1)}{escape(quote(key, safe='/-._~'))}{m.group(3)}"
        return _HREF.sub(fix, xml)

    def resolve_style_refs(self, xml: str, f: Feature) -> str:
        def fix(m: "re.Match[str]") -> str:
            ref = html.unescape(m.group(2)).strip()
            if ref.startswith("#"):
                ref = f"#{self.register(f, ref[1:])}"
            return f"{m.group(1)}{escape(ref)}{m.group(3)}"
        return _STYLE_URL.sub(fix, xml)

    def register(self, f: Feature, orig: str) -> str:
        """Export id for the style ``orig`` as defined in ``f``'s source file (adds it, and what it points at)."""
        k = (id(f.style_defs), orig)
        if k in self._owner:
            return self._owner[k]
        xml = f.style_defs.get(orig)
        if xml is None:  # defined somewhere we do not have: keep the reference as it was
            self._owner[k] = orig
            return orig
        self._owner[k] = orig  # provisional, guards against StyleMap cycles
        xml = self.resolve_style_refs(xml, f)
        xml = self.adopt_resources(xml, f)
        eid, n = orig, 1
        while eid in self.defs and self.defs[eid] != _set_id(xml, eid):  # same id, different content: keep both
            n += 1
            eid = f"{orig}__{n}"
        self.defs.setdefault(eid, _set_id(xml, eid))
        self._owner[k] = eid
        return eid


# columns kml2report adds for its own tables that do not belong inside a file handed to a crew.
# latitude / longitude are deliberately NOT here: crews use them, so they stay in every placemark.
_INTERNAL_COLUMNS = {"name", "wkt", "raw_description", "feature_id", "layer", "source_file", "folder_path",
                     "geometry_type"}


def _description_xml(f: Feature, shown: dict) -> str:
    """The user's own description when there is one (their formatting and images intact), else a small table."""
    note = f.extended_data.get("Split_Note", "")
    if f.raw_description:
        body = f.raw_description.replace("]]>", "]]]]><![CDATA[>")
        if note:
            body += f"<br/><i>{html.escape(note)}</i>"
        return f"<description><![CDATA[{body}]]></description>"
    rows = "".join(f"<tr><td><b>{html.escape(str(k))}</b></td><td>{html.escape(str(v))}</td></tr>"
                   for k, v in shown.items() if v not in ("", None) and k not in ("source_file",))
    return f"<description><![CDATA[<table>{rows}</table>]]></description>" if rows else ""


def _placemark_xml(f: Feature, book: _StyleBook, override_style: str = "") -> str:
    attrs = feature_attributes(f)
    shown = {k: v for k, v in attrs.items() if k not in _INTERNAL_COLUMNS}
    ext = "".join(f'<Data name="{escape(k, {chr(34): "&quot;"})}"><value>{escape(str(v))}</value></Data>'
                  for k, v in shown.items())
    if override_style:                      # caller asked for a recolour: it replaces the original styling
        style = f"<styleUrl>#{override_style}</styleUrl>"
    else:
        style_parts = []
        if f.style_url.startswith("#"):       # a shared style defined in the file
            style_parts.append(f"<styleUrl>#{book.register(f, f.style_url[1:])}</styleUrl>")
        elif f.style_url:                      # a style in another file / online: keep the reference
            style_parts.append(f"<styleUrl>{escape(f.style_url)}</styleUrl>")
        if f.style_xml:                        # inline styling can supplement the shared reference
            inline = book.resolve_style_refs(f.style_xml, f)
            style_parts.append(book.adopt_resources(inline, f))
        style = "".join(style_parts)
    vis = f"<visibility>{escape(f.visibility)}</visibility>" if f.visibility else ""
    if f.time_end:
        time = f"<TimeSpan><begin>{escape(f.time_begin)}</begin><end>{escape(f.time_end)}</end></TimeSpan>"
    else:
        time = f"<TimeStamp><when>{escape(f.time_begin)}</when></TimeStamp>" if f.time_begin else ""
    g = _geom_xml(f.geometry) if f.geometry and not f.geometry.is_empty else ""
    return (f"<Placemark><name>{escape(f.name)}</name>{vis}{_description_xml(f, shown)}{time}{style}"
            f"<ExtendedData>{ext}</ExtendedData>{g}</Placemark>")


def _style_xml(style_id: str, color_hex: str) -> str:
    return (f'<Style id="{style_id}"><LineStyle><color>{_kml_color(color_hex)}</color><width>3</width></LineStyle>'
            f'<PolyStyle><color>{_kml_color(color_hex, "66")}</color></PolyStyle>'
            f'<IconStyle><color>{_kml_color(color_hex)}</color><scale>1.0</scale></IconStyle></Style>')


def _folder_path(f: Feature) -> tuple:
    return f.folders if f.folders else ((f.layer,) if f.layer else ())


def _render_tree(node: dict) -> str:
    kids = "".join(f"<Folder><name>{escape(k)}</name>{_render_tree(v)}</Folder>" for k, v in node["kids"].items())
    return "".join(node["pms"]) + kids


def build_kml(groups: list[tuple[str, list[Feature], Optional[str]]], name: str = "kml2report export",
              extra_xml: str = "", wrap_groups: bool = True) -> tuple[bytes, dict]:
    """KML text plus the icon files it needs: ``(kml bytes, {href: image bytes})``.

    ``groups`` = [(group name, features, '#rrggbb' or None), ...].  A colour is an explicit override that replaces
    the original styling of that group; with ``None`` every feature keeps exactly the style it came with
    (icons, scales, labels, StyleMaps) and its original folder nesting."""
    book = _StyleBook()
    override_defs, body = [], []
    for gi, (gname, feats, color) in enumerate(groups):
        sid = f"grp{gi}" if color else ""
        if color:
            override_defs.append(_style_xml(sid, color))
        root: dict = {"kids": {}, "pms": []}
        for f in feats:
            node = root
            for part in _folder_path(f):
                node = node["kids"].setdefault(part, {"kids": {}, "pms": []})
            node["pms"].append(_placemark_xml(f, book, sid))
        inner = _render_tree(root)
        body.append(f"<Folder><name>{escape(gname)}</name>{inner}</Folder>" if wrap_groups else inner)
    kml = (f"{_KML_OPEN}<Document><name>{escape(name)}</name>{''.join(book.defs.values())}"
           f"{''.join(override_defs)}{''.join(body)}{extra_xml}</Document></kml>")
    return kml.encode("utf-8"), book.resources


def to_kml_package(groups: list[tuple[str, list[Feature], Optional[str]]], name: str = "kml2report export",
                   extra_xml: str = "", wrap_groups: bool = True) -> tuple[bytes, str]:
    """Like :func:`build_kml` but returns a finished file: a ``.kmz`` carrying the icon images when the styles use
    icons shipped inside a KMZ (a bare .kml would lose them), otherwise a plain ``.kml``.  ``(bytes, extension)``."""
    kml, resources = build_kml(groups, name, extra_xml, wrap_groups)
    if not resources:
        return kml, ".kml"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
        for href, data in resources.items():
            z.writestr(href, data)
    return buf.getvalue(), ".kmz"


def to_kml_grouped_bytes(groups: list[tuple[str, list[Feature], Optional[str]]], name: str = "kml2report export",
                         extra_xml: str = "", wrap_groups: bool = True) -> bytes:
    """KML text only (icons that live inside a KMZ are not embedded - use :func:`to_kml_package` for those)."""
    return build_kml(groups, name, extra_xml, wrap_groups)[0]


def to_kml_bytes(features: list[Feature], name: str = "kml2report export", color: Optional[str] = None) -> bytes:
    return to_kml_grouped_bytes([(name, features, color)], name, wrap_groups=False)

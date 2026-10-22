"""Core KML/KMZ parsing layer.

Reads Google Earth (and any other KML 2.0/2.1/2.2) files and extracts every
Placemark into a :class:`Feature`.  The parser is deliberately forgiving: a
malformed coordinate, an undefined namespace prefix or a broken Placemark is
recorded as a warning instead of aborting the whole file.

Supported: Point, LineString, LinearRing, Polygon (with holes), MultiGeometry
(nested, parts preserved), gx:Track / gx:MultiTrack, Style / StyleMap colours,
ExtendedData (Data, SchemaData/SimpleData, displayName), TimeStamp / TimeSpan,
nested Folder paths, KMZ archives (doc.kml first, any number of KMLs).
"""

from __future__ import annotations

import io
import html
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Union
from urllib.parse import unquote

from lxml import etree

from kml2report.models import Coord, Feature, Geometry

KML_NS = "http://www.opengis.net/kml/2.2"
NS = {"kml": KML_NS}

GEOMETRY_TAGS = {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry", "Track", "MultiTrack"}
_SIMPLE_TAGS = ("Point", "LineString", "LinearRing", "Polygon", "Track")
_CONTAINERS = {"Folder", "Document"}

_PARSER_ARGS = dict(recover=True, resolve_entities=False, huge_tree=True, no_network=True)


@dataclass
class ParseReport:
    """Result of parsing with diagnostics."""

    features: list[Feature] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def extend(self, other: "ParseReport") -> None:
        self.features.extend(other.features)
        self.warnings.extend(other.warnings)
        self.files.extend(other.files)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_files(paths: list[Union[str, Path]]) -> list[Feature]:
    """Parse one or more KML/KMZ files and return a flat list of Features."""
    return parse_files_report(paths).features


def parse_files_report(paths: list[Union[str, Path]]) -> ParseReport:
    report = ParseReport()
    for path in paths:
        report.extend(parse_file_report(Path(path)))
    return report


def parse_file(path: Union[str, Path]) -> list[Feature]:
    """Parse a single KML or KMZ file."""
    return parse_file_report(Path(path)).features


def parse_file_report(path: Path) -> ParseReport:
    path = Path(path)
    if path.suffix.lower() not in (".kml", ".kmz", ".xml"):
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return parse_bytes(path.read_bytes(), path.name)


def parse_bytes(data: bytes, filename: str = "upload.kml") -> ParseReport:
    """Parse raw KML or KMZ bytes.  Content is sniffed, so a KMZ renamed .kml
    (or the reverse) still works."""
    report = ParseReport(files=[filename])
    if data[:2] == b"PK":
        _parse_kmz_bytes(data, filename, report)
    else:
        _parse_kml_bytes(data, filename, report)
    return report


# ---------------------------------------------------------------------------
# Container handling
# ---------------------------------------------------------------------------

def _parse_kmz_bytes(data: bytes, filename: str, report: ParseReport) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        report.warnings.append(f"{filename}: not a valid KMZ archive")
        return
    with zf:
        kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml") and not n.endswith("/")]
        if not kml_names:
            raise ValueError(f"No .kml file found inside {filename}")
        # doc.kml (the entry point) first, then shallowest paths
        kml_names.sort(key=lambda n: (os.path.basename(n).lower() != "doc.kml", n.count("/"), n))
        for name in kml_names:
            label = filename if len(kml_names) == 1 else f"{filename}!{name}"
            _parse_kml_bytes(zf.read(name), label, report, archive=zf, kml_name=name)


def _parse_kml_bytes(data: bytes, filename: str, report: ParseReport, archive: Optional[zipfile.ZipFile] = None,
                     kml_name: str = "") -> None:
    data = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    try:
        root = etree.fromstring(data, etree.XMLParser(**_PARSER_ARGS))
    except etree.XMLSyntaxError as exc:
        report.warnings.append(f"{filename}: unreadable XML ({exc})")
        return
    if root is None:
        report.warnings.append(f"{filename}: empty or unreadable XML")
        return
    # Capture the original styling BEFORE namespaces are stripped, so it can be written back untouched.
    style_defs = {el.get("id"): _capture_xml(el)
                  for el in root.iter() if isinstance(el.tag, str) and _local_tag(el) in ("Style", "StyleMap")
                  and el.get("id")}
    pre = [_capture_presentation(el) for el in root.iter() if isinstance(el.tag, str) and _local_tag(el) == "Placemark"]
    resources = _collect_resources(style_defs, pre, archive, kml_name)
    _strip_namespace_prefix(root)
    styles = _collect_styles(root)
    count = 0
    for i, placemark in enumerate(root.iter("Placemark")):
        try:
            feat = _parse_placemark(placemark, filename, styles, report.warnings)
            if i < len(pre):
                feat.style_url, feat.style_xml, feat.visibility = pre[i]
            feat.style_defs, feat.resources = style_defs, resources
            feat.folders = _folder_chain(placemark)
            report.features.append(feat)
            count += 1
        except Exception as exc:  # one bad Placemark must never kill the file
            report.warnings.append(f"{filename}: skipped Placemark '{_text(placemark, 'name')}' ({exc})")
    if count == 0:
        report.warnings.append(f"{filename}: no Placemarks found")


def _capture_xml(el: etree._Element) -> str:
    """Serialize a presentation fragment with namespace-independent KML element names."""
    captured = etree.fromstring(etree.tostring(el))
    _strip_namespace_prefix(captured)
    etree.cleanup_namespaces(captured)
    return etree.tostring(captured, encoding="unicode")


def _capture_presentation(pm: etree._Element) -> tuple[str, str, str]:
    """(styleUrl, inline Style/StyleMap xml, visibility) of a Placemark, read before namespace stripping."""
    url = inline = vis = ""
    for ch in pm:
        if not isinstance(ch.tag, str):
            continue
        tag = _local_tag(ch)
        if tag == "styleUrl":
            url = (ch.text or "").strip()
        elif tag in ("Style", "StyleMap"):
            inline = _capture_xml(ch)
        elif tag == "visibility":
            vis = (ch.text or "").strip()
    return url, inline, vis


_HREF_RE = re.compile(r"<(?:[\w.-]+:)?href\b[^>]*>\s*([^<]+?)\s*</(?:[\w.-]+:)?href>")


def _collect_resources(style_defs: dict, pre: list, archive: Optional[zipfile.ZipFile], kml_name: str) -> dict:
    """Icon images a KMZ ships with: {href as written in the style: bytes}.  Empty for plain KML files."""
    out: dict = {}
    if archive is None:
        return out
    names = set(archive.namelist())
    base = os.path.dirname(kml_name)
    xmls = list(style_defs.values()) + [p[1] for p in pre if p[1]]
    for xml in xmls:
        for raw_href in _HREF_RE.findall(xml):
            href = unquote(html.unescape(raw_href).strip())
            if "://" in href or href.startswith(("data:", "/")) or href in out:
                continue
            member = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
            if member in names:
                out[href] = archive.read(member)
    return out


def _folder_chain(elem: etree._Element) -> tuple:
    """Real <Folder> names from outermost to innermost (post namespace stripping)."""
    names = []
    p = elem.getparent()
    while p is not None:
        if _local_tag(p) == "Folder":
            names.append(_text(p, "name"))
        p = p.getparent()
    return tuple(reversed(names))


# ---------------------------------------------------------------------------
# Placemark
# ---------------------------------------------------------------------------

def _parse_placemark(elem: etree._Element, source_file: str, styles: dict, warnings: list[str]) -> Feature:
    feature = Feature(source_file=source_file)
    feature.placemark_id = elem.get("id", "")
    feature.name = _text(elem, "name")
    desc_el = elem.find("description")
    if desc_el is not None:
        feature.raw_description = "".join(desc_el.itertext()).strip()

    label = feature.name or feature.placemark_id or "(unnamed)"
    feature.geometry = _parse_geometry(elem, warnings, label)
    feature.extended_data = _parse_extended_data(elem)
    feature.layer, feature.folder_path = _folder_info(elem)
    feature.style = _resolve_style(elem, styles)

    ts = elem.find("TimeStamp/when")
    if ts is not None and ts.text:
        feature.time_begin = ts.text.strip()
    span = elem.find("TimeSpan")
    if span is not None:
        feature.time_begin = _text(span, "begin") or feature.time_begin
        feature.time_end = _text(span, "end")
    return feature


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _parse_geometry(placemark: etree._Element, warnings: list[str], label: str) -> Optional[Geometry]:
    """First geometry child of the Placemark (including MultiGeometry/MultiTrack)."""
    for child in placemark:
        tag = _local_tag(child)
        if tag in GEOMETRY_TAGS:
            return _geometry_from_element(child, warnings, label)
    return None


def _geometry_from_element(el: etree._Element, warnings: list[str], label: str) -> Optional[Geometry]:
    tag = _local_tag(el)
    if tag in ("MultiGeometry", "MultiTrack"):
        parts = [
            g
            for g in (_geometry_from_element(c, warnings, label) for c in el if _local_tag(c) in GEOMETRY_TAGS)
            if g is not None and not g.is_empty
        ]
        if not parts:
            return None
        return _collection(parts)

    if tag == "Track":
        return _parse_track(el, warnings, label)

    if tag == "Polygon":
        outer = el.find("outerBoundaryIs/LinearRing/coordinates")
        if outer is None:
            outer = el.find(".//coordinates")
        coords = _parse_coord_text(outer.text if outer is not None else "", warnings, label)
        holes = [
            _parse_coord_text(h.text, warnings, label)
            for h in el.findall("innerBoundaryIs/LinearRing/coordinates")
        ]
        return Geometry("Polygon", coords, holes=[h for h in holes if h])

    if tag in ("Point", "LineString", "LinearRing"):
        coords_el = el.find("coordinates")
        if coords_el is None:
            coords_el = el.find(".//coordinates")
        coords = _parse_coord_text(coords_el.text if coords_el is not None else "", warnings, label)
        if tag == "Point":
            coords = coords[:1]
        return Geometry(tag, coords)
    return None


def _collection(parts: list[Geometry]) -> Geometry:
    """Wrap parts in a collection.  A homogeneous collection reports its member
    type (e.g. 'LineString') so type-based filters keep working; all vertices are
    also flattened into ``coordinates``."""
    flat: list[Coord] = [c for p in parts for c in p.coordinates]
    types = {p.geometry_type for p in parts}
    if len(parts) == 1:
        return parts[0]
    gtype = next(iter(types)) if len(types) == 1 else "MultiGeometry"
    return Geometry(gtype, flat, parts=parts)


def _parse_track(el: etree._Element, warnings: list[str], label: str) -> Optional[Geometry]:
    coords: list[Coord] = []
    for c in el.findall("coord"):  # gx:coord -> "lon lat alt" (space separated)
        nums = _numbers(c.text or "")
        if len(nums) >= 2 and _valid(nums[0], nums[1]):
            coords.append((nums[0], nums[1], nums[2] if len(nums) > 2 else 0.0))
        else:
            warnings.append(f"{label}: bad gx:coord '{(c.text or '').strip()}'")
    if not coords:
        return None
    times = [(w.text or "").strip() for w in el.findall("when")]
    return Geometry("Track", coords, times=times)


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(n) for n in _NUM.findall(text)]


def _valid(lon: float, lat: float) -> bool:
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def _parse_coord_text(text: Optional[str], warnings: list[str], label: str = "") -> list[Coord]:
    """Parse a KML <coordinates> string into (lon, lat, alt) tuples.

    Tolerates the real-world mess Google Earth and other tools produce: spaces
    around commas ("-96.0, 29.0, 0"), tuples split across newlines, missing
    altitude, trailing commas and stray text.  Bad tuples are skipped with a
    warning rather than raising.
    """
    if not text or not text.strip():
        return []
    cleaned = re.sub(r"\s*,\s*", ",", text.strip())
    result: list[Coord] = []
    for token in cleaned.split():
        token = token.rstrip(",")  # only a trailing comma is optional; empty inner fields make the tuple invalid
        parts = token.split(",")
        if len(parts) > 3:
            # e.g. "-96,29, -95,30,0": a trailing comma glued two tuples together - cannot be split safely
            warnings.append(f"{label}: skipped ambiguous coordinate run '{token[:40]}'")
            continue
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            warnings.append(f"{label}: skipped unreadable coordinate '{token[:40]}'")
            continue
        if len(nums) < 2:
            warnings.append(f"{label}: skipped incomplete coordinate '{token[:40]}'")
            continue
        lon, lat = nums[0], nums[1]
        if not _valid(lon, lat):
            if _valid(lat, lon):  # lat,lon written backwards
                lon, lat = lat, lon
                warnings.append(f"{label}: swapped lat/lon in '{token[:40]}'")
            else:
                warnings.append(f"{label}: coordinate out of range '{token[:40]}'")
                continue
        result.append((lon, lat, nums[2] if len(nums) > 2 else 0.0))
    return result


# Back-compat name used by older tests / callers
def _parse_coordinates(geom_el: etree._Element) -> list[Coord]:
    coords_el = geom_el.find(".//coordinates")
    return _parse_coord_text(coords_el.text if coords_el is not None else "", [])


# ---------------------------------------------------------------------------
# Attributes, folders, styles
# ---------------------------------------------------------------------------

def _parse_extended_data(placemark: etree._Element) -> dict[str, str]:
    """ExtendedData: <Data name><value>, <SchemaData><SimpleData name>."""
    data: dict[str, str] = {}
    for simple in placemark.iter("SimpleData"):
        key = simple.get("name", "")
        if key:
            data[key] = (simple.text or "").strip()
    for data_el in placemark.iter("Data"):
        key = data_el.get("name", "")
        val_el = data_el.find("value")
        val = (val_el.text or "").strip() if val_el is not None else ""
        if key:
            data[key] = val
    return data


def _folder_info(elem: etree._Element) -> tuple[str, str]:
    """(nearest folder name, 'Outer / Inner' path)."""
    names: list[str] = []
    parent = elem.getparent()
    while parent is not None:
        if _local_tag(parent) in _CONTAINERS:
            n = _text(parent, "name")
            if n:
                names.append(n)
        parent = parent.getparent()
    names.reverse()
    folders = [n for n in names]
    nearest = ""
    p = elem.getparent()
    while p is not None:
        if _local_tag(p) == "Folder":
            nearest = _text(p, "name")
            if nearest:
                break
        p = p.getparent()
    return nearest, " / ".join(folders)


def _find_parent_folder_name(elem: etree._Element) -> str:
    return _folder_info(elem)[0]


def kml_color_to_hex(color: str) -> str:
    """KML 'aabbggrr' -> '#rrggbb'."""
    c = (color or "").strip().lstrip("#")
    if len(c) == 8:
        return f"#{c[6:8]}{c[4:6]}{c[2:4]}"
    if len(c) == 6:
        return f"#{c[4:6]}{c[2:4]}{c[0:2]}"
    return ""


def _collect_styles(root: etree._Element) -> dict[str, dict[str, str]]:
    styles: dict[str, dict[str, str]] = {}
    for st in root.iter("Style"):
        sid = st.get("id")
        if not sid:
            continue
        info: dict[str, str] = {}
        line = _text(st, "LineStyle/color")
        poly = _text(st, "PolyStyle/color")
        icon = _text(st, "IconStyle/color")
        if line:
            info["line_color"] = kml_color_to_hex(line)
        w = _text(st, "LineStyle/width")
        if w:
            info["line_width"] = w
        if poly:
            info["fill_color"] = kml_color_to_hex(poly)
        if icon:
            info["icon_color"] = kml_color_to_hex(icon)
        styles[sid] = info
    for sm in root.iter("StyleMap"):
        sid = sm.get("id")
        if not sid:
            continue
        for pair in sm.findall("Pair"):
            if _text(pair, "key") == "normal":
                ref = _text(pair, "styleUrl").lstrip("#")
                inline = pair.find("Style")
                if inline is not None:
                    tmp = etree.Element("Document")
                    tmp.append(inline)
                    inline.set("id", sid + "__inline")
                    styles.update(_collect_styles(tmp))
                    styles[sid] = styles.get(sid + "__inline", {})
                elif ref in styles:
                    styles[sid] = styles[ref]
    return styles


def _resolve_style(placemark: etree._Element, styles: dict) -> dict[str, str]:
    inline = placemark.find("Style")
    if inline is not None:
        tmp = etree.Element("Document")
        inline = etree.fromstring(etree.tostring(inline))
        inline.set("id", "__p")
        tmp.append(inline)
        got = _collect_styles(tmp).get("__p")
        if got:
            return got
    url = _text(placemark, "styleUrl").lstrip("#")
    return dict(styles.get(url, {})) if url else {}


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _text(el: etree._Element, path: str) -> str:
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _strip_namespace_prefix(root: etree._Element) -> None:
    """Remove namespaces so simple paths work (gx:Track -> Track)."""
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]
        elif isinstance(el.tag, str) and ":" in el.tag:  # undefined prefix kept by recover mode
            el.tag = el.tag.split(":", 1)[1]


def _local_tag(el: etree._Element) -> str:
    tag = el.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def _extract_features(tree, source_file: str = "") -> Iterator[Feature]:  # back-compat
    root = tree.getroot()
    _strip_namespace_prefix(root)
    styles = _collect_styles(root)
    for placemark in root.iter("Placemark"):
        yield _parse_placemark(placemark, source_file, styles, [])

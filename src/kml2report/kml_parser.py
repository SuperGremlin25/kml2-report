"""Core KML/KMZ parsing layer.

Handles both .kml and .kmz files, extracting Placemarks into Feature objects.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Iterator

from lxml import etree

from kml2report.models import Feature, Geometry

KML_NS = "http://www.opengis.net/kml/2.2"
NS = {"kml": KML_NS}

GEOMETRY_TAGS = {"Point", "LineString", "Polygon", "MultiGeometry"}


def parse_files(paths: list[str | Path]) -> list[Feature]:
    """Parse one or more KML/KMZ files and return a list of Features."""
    features: list[Feature] = []
    for path in paths:
        features.extend(parse_file(Path(path)))
    return features


def parse_file(path: Path) -> list[Feature]:
    """Parse a single KML or KMZ file."""
    suffix = path.suffix.lower()
    if suffix == ".kmz":
        return _parse_kmz(path)
    if suffix == ".kml":
        return _parse_kml(path)
    raise ValueError(f"Unsupported file type: {suffix}")


_PARSER = etree.XMLParser(recover=True, resolve_entities=False)


def _parse_kmz(path: Path) -> list[Feature]:
    """Unzip a KMZ and parse the main KML inside it."""
    features: list[Feature] = []
    with zipfile.ZipFile(path, "r") as zf:
        kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
        if not kml_names:
            raise ValueError(f"No .kml file found inside {path}")
        for kml_name in kml_names:
            with zf.open(kml_name) as kml_file:
                tree = etree.parse(kml_file, _PARSER)
                features.extend(_extract_features(tree, source_file=str(path)))
    return features


def _parse_kml(path: Path) -> list[Feature]:
    """Parse a .kml file from disk."""
    tree = etree.parse(str(path), _PARSER)
    return list(_extract_features(tree, source_file=str(path)))


def _extract_features(
    tree: etree._ElementTree, source_file: str = ""
) -> Iterator[Feature]:
    """Walk the KML tree and yield Feature objects for every Placemark."""
    root = tree.getroot()
    _strip_namespace_prefix(root)

    for placemark in root.iter("Placemark"):
        yield _parse_placemark(placemark, source_file)


def _parse_placemark(elem: etree._Element, source_file: str) -> Feature:
    """Convert a single <Placemark> element into a Feature."""
    feature = Feature(source_file=os.path.basename(source_file))

    feature.placemark_id = elem.get("id", "")

    name_el = elem.find("name")
    if name_el is not None and name_el.text:
        feature.name = name_el.text.strip()

    desc_el = elem.find("description")
    if desc_el is not None and desc_el.text:
        feature.raw_description = desc_el.text.strip()

    feature.geometry = _parse_geometry(elem)
    feature.extended_data = _parse_extended_data(elem)
    feature.layer = _find_parent_folder_name(elem)

    return feature


def _parse_geometry(placemark: etree._Element) -> Geometry | None:
    """Extract geometry from a Placemark, merging all children for MultiGeometry."""
    multi = placemark.find(".//MultiGeometry")
    if multi is not None:
        all_coords: list[tuple[float, float, float]] = []
        child_type = "MultiGeometry"
        for child_tag in ("Point", "LineString", "Polygon"):
            for child_el in multi.findall(child_tag):
                child_coords = _parse_coordinates(child_el)
                if child_coords:
                    all_coords.extend(child_coords)
                    if child_type == "MultiGeometry":
                        child_type = child_tag
        if all_coords:
            return Geometry(geometry_type=child_type, coordinates=all_coords)

    for tag in ("Point", "LineString", "Polygon"):
        geom_el = placemark.find(f".//{tag}")
        if geom_el is not None:
            coords = _parse_coordinates(geom_el)
            return Geometry(geometry_type=tag, coordinates=coords)
    return None


def _parse_coordinates(geom_el: etree._Element) -> list[tuple[float, float, float]]:
    """Parse <coordinates> text into a list of (lon, lat, alt) tuples."""
    coords_el = geom_el.find(".//coordinates")
    if coords_el is None or not coords_el.text:
        return []

    result: list[tuple[float, float, float]] = []
    raw = coords_el.text.strip()
    for token in raw.split():
        parts = token.split(",")
        if len(parts) >= 2:
            lon = float(parts[0])
            lat = float(parts[1])
            alt = float(parts[2]) if len(parts) > 2 else 0.0
            result.append((lon, lat, alt))
    return result


def _parse_extended_data(placemark: etree._Element) -> dict[str, str]:
    """Extract ExtendedData / SchemaData / SimpleData fields."""
    data: dict[str, str] = {}
    for simple in placemark.iter("SimpleData"):
        key = simple.get("name", "")
        val = simple.text or ""
        if key:
            data[key] = val.strip()

    for data_el in placemark.iter("Data"):
        key = data_el.get("name", "")
        val_el = data_el.find("value")
        val = val_el.text.strip() if val_el is not None and val_el.text else ""
        if key:
            data[key] = val

    return data


def _find_parent_folder_name(elem: etree._Element) -> str:
    """Walk up the tree to find the nearest enclosing <Folder> name."""
    parent = elem.getparent()
    while parent is not None:
        if _local_tag(parent) == "Folder":
            name_el = parent.find("name")
            if name_el is not None and name_el.text:
                return name_el.text.strip()
        parent = parent.getparent()
    return ""


def _strip_namespace_prefix(root: etree._Element) -> None:
    """Remove namespace prefixes from all tags so we can use simple XPath."""
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]


def _local_tag(el: etree._Element) -> str:
    """Return the local tag name without namespace."""
    tag = el.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag if isinstance(tag, str) else ""

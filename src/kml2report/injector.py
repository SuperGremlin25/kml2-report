"""Inject classified field notes into a target KML and produce an annotated KMZ.

This module spatially matches notes (extracted from source KML files) to the
nearest placemarks in a target KML (e.g. Colorado County V2) and writes a new
KMZ with the notes embedded in placemark descriptions.
"""

from __future__ import annotations

import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lxml import etree

KML_NS = "http://www.opengis.net/kml/2.2"
NS = {"k": KML_NS}

# ---------------------------------------------------------------------------
# Haversine (meters)
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in **meters** between two lat/lon points."""
    R = 6_371_000.0  # Earth radius in meters
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Data container for a V2 placemark's location
# ---------------------------------------------------------------------------

@dataclass
class _V2Placemark:
    element: Any  # lxml Element
    lat: float
    lon: float
    layer: str


# ---------------------------------------------------------------------------
# Parse V2 placemarks
# ---------------------------------------------------------------------------

def _parse_coords(text: str) -> list[tuple[float, float]]:
    """Parse a KML coordinates string into a list of (lat, lon) tuples."""
    pairs: list[tuple[float, float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                lon, lat = float(parts[0]), float(parts[1])
                pairs.append((lat, lon))
            except ValueError:
                continue
    return pairs


def parse_v2_placemarks(tree: etree._ElementTree) -> list[_V2Placemark]:
    """Extract every Placemark from the V2 KML with its centroid coordinate."""
    results: list[_V2Placemark] = []
    for pm in tree.findall(".//k:Placemark", NS):
        # Determine layer from parent Folder
        parent = pm.getparent()
        layer = ""
        if parent is not None:
            name_el = parent.find("k:name", NS)
            if name_el is not None and name_el.text:
                layer = name_el.text.strip()

        # Try Point first
        pt = pm.find(".//k:Point/k:coordinates", NS)
        if pt is not None and pt.text:
            coords = _parse_coords(pt.text)
            if coords:
                lat, lon = coords[0]
                results.append(_V2Placemark(element=pm, lat=lat, lon=lon, layer=layer))
                continue

        # Try LineString (use centroid)
        ls = pm.find(".//k:LineString/k:coordinates", NS)
        if ls is not None and ls.text:
            coords = _parse_coords(ls.text)
            if coords:
                avg_lat = sum(c[0] for c in coords) / len(coords)
                avg_lon = sum(c[1] for c in coords) / len(coords)
                results.append(_V2Placemark(element=pm, lat=avg_lat, lon=avg_lon, layer=layer))
                continue

    return results


# ---------------------------------------------------------------------------
# Spatial matching
# ---------------------------------------------------------------------------

@dataclass
class _NoteRecord:
    """Lightweight carrier for a classified note used during injection."""
    feature_name: str
    text: str
    themes: list[str]
    latitude: float
    longitude: float
    pole_id: str
    town: str
    google_maps_url: str


def match_notes_to_placemarks(
    notes: list[_NoteRecord],
    placemarks: list[_V2Placemark],
    tolerance_m: float = 15.0,
) -> tuple[dict[int, list[_NoteRecord]], list[_NoteRecord]]:
    """Match each note to the nearest V2 placemark within *tolerance_m* meters.

    Returns
    -------
    matched : dict mapping placemark list-index → list of matched notes
    unmatched : notes that didn't match any placemark
    """
    matched: dict[int, list[_NoteRecord]] = {}
    unmatched: list[_NoteRecord] = []

    for note in notes:
        if note.latitude is None or note.longitude is None:
            unmatched.append(note)
            continue

        best_idx: int | None = None
        best_dist = float("inf")

        for idx, pm in enumerate(placemarks):
            d = _haversine_m(note.latitude, note.longitude, pm.lat, pm.lon)
            if d < best_dist:
                best_dist = d
                best_idx = idx

        if best_idx is not None and best_dist <= tolerance_m:
            matched.setdefault(best_idx, []).append(note)
        else:
            unmatched.append(note)

    return matched, unmatched


# ---------------------------------------------------------------------------
# HTML injection into existing placemark descriptions
# ---------------------------------------------------------------------------

_THEME_LABELS: dict[str, str] = {
    "access_row": "Right-of-Way (ROW) Access",
    "access_private": "Private Property / Fenceline Access",
    "pole_condition": "Pole Condition & Attachments",
    "construction_plow": "Plow / Trenching Opportunities",
    "construction_bore": "Bore / Directional Drill Requirements",
    "construction_aerial": "Aerial Strand Opportunities",
    "obstacles": "Obstacles & Hazards",
    "permits": "Permits & Regulatory",
    "general": "General / Uncategorized",
}


def _note_html(note: _NoteRecord) -> str:
    """Build an HTML table snippet for one field note."""
    theme_str = ", ".join(_THEME_LABELS.get(t, t) for t in note.themes)
    rows = [
        '<hr style="border:1px solid #FFD700"/>',
        '<table style="font-family:Arial,Verdana,Times;font-size:11px;'
        'text-align:left;width:100%;border-collapse:collapse;padding:3px">',
        '<tr style="background:#FFD700;font-weight:bold">'
        '<td colspan="2">Field Note</td></tr>',
    ]
    if note.feature_name:
        rows.append(f"<tr><td><b>Feature</b></td><td>{note.feature_name}</td></tr>")
    rows.append(f"<tr><td><b>Theme</b></td><td>{theme_str}</td></tr>")
    rows.append(f"<tr><td><b>Note</b></td><td>{note.text}</td></tr>")
    if note.town:
        rows.append(f"<tr><td><b>Town</b></td><td>{note.town}</td></tr>")
    if note.pole_id:
        rows.append(f"<tr><td><b>Pole ID</b></td><td>{note.pole_id}</td></tr>")
    if note.google_maps_url:
        rows.append(
            f'<tr><td><b>Map</b></td>'
            f'<td><a href="{note.google_maps_url}">Open in Google Maps</a></td></tr>'
        )
    rows.append("</table>")
    return "\n".join(rows)


def inject_notes_into_placemark(
    pm_element: etree._Element,
    notes: list[_NoteRecord],
) -> None:
    """Append field-note HTML to an existing placemark's <description>."""
    desc_el = pm_element.find("k:description", NS)

    extra_html = "\n".join(_note_html(n) for n in notes)

    if desc_el is not None and desc_el.text:
        # Existing description is CDATA HTML — inject before closing </body> or </html>
        existing = desc_el.text
        # Try to insert before </body>
        lower = existing.lower()
        body_idx = lower.rfind("</body>")
        if body_idx != -1:
            new_text = existing[:body_idx] + extra_html + "\n" + existing[body_idx:]
        else:
            html_idx = lower.rfind("</html>")
            if html_idx != -1:
                new_text = existing[:html_idx] + extra_html + "\n" + existing[html_idx:]
            else:
                new_text = existing + "\n" + extra_html
        desc_el.text = etree.CDATA(new_text)
    else:
        # No existing description — create one
        if desc_el is None:
            desc_el = etree.SubElement(pm_element, f"{{{KML_NS}}}description")
        body = (
            '<html><head></head><body style="margin:0;overflow:auto;background:#FFF">'
            + extra_html
            + "</body></html>"
        )
        desc_el.text = etree.CDATA(body)


# ---------------------------------------------------------------------------
# Add unmatched notes as new placemarks in a Field_Notes folder
# ---------------------------------------------------------------------------

_FIELD_NOTE_STYLE_ID = "fieldNoteStyle"
_FIELD_NOTE_STYLE_HL_ID = "fieldNoteStyleHL"
_FIELD_NOTE_STYLEMAP_ID = "fieldNoteStyleMap"


def inject_field_note_style(doc_element: etree._Element) -> None:
    """Add a yellow-pin style for field-note placemarks."""
    # Normal style
    style_n = etree.SubElement(doc_element, f"{{{KML_NS}}}Style")
    style_n.set("id", _FIELD_NOTE_STYLE_ID)
    icon_style = etree.SubElement(style_n, f"{{{KML_NS}}}IconStyle")
    icon = etree.SubElement(icon_style, f"{{{KML_NS}}}Icon")
    href = etree.SubElement(icon, f"{{{KML_NS}}}href")
    href.text = "https://maps.google.com/mapfiles/kml/paddle/ylw-stars.png"
    scale = etree.SubElement(icon_style, f"{{{KML_NS}}}scale")
    scale.text = "1.0"
    label_style = etree.SubElement(style_n, f"{{{KML_NS}}}LabelStyle")
    ls_scale = etree.SubElement(label_style, f"{{{KML_NS}}}scale")
    ls_scale.text = "0.8"

    # Highlight style
    style_hl = etree.SubElement(doc_element, f"{{{KML_NS}}}Style")
    style_hl.set("id", _FIELD_NOTE_STYLE_HL_ID)
    icon_style_hl = etree.SubElement(style_hl, f"{{{KML_NS}}}IconStyle")
    icon_hl = etree.SubElement(icon_style_hl, f"{{{KML_NS}}}Icon")
    href_hl = etree.SubElement(icon_hl, f"{{{KML_NS}}}href")
    href_hl.text = "https://maps.google.com/mapfiles/kml/paddle/ylw-stars.png"
    scale_hl = etree.SubElement(icon_style_hl, f"{{{KML_NS}}}scale")
    scale_hl.text = "1.3"
    label_style_hl = etree.SubElement(style_hl, f"{{{KML_NS}}}LabelStyle")
    ls_scale_hl = etree.SubElement(label_style_hl, f"{{{KML_NS}}}scale")
    ls_scale_hl.text = "1.0"

    # StyleMap
    smap = etree.SubElement(doc_element, f"{{{KML_NS}}}StyleMap")
    smap.set("id", _FIELD_NOTE_STYLEMAP_ID)
    pair_n = etree.SubElement(smap, f"{{{KML_NS}}}Pair")
    key_n = etree.SubElement(pair_n, f"{{{KML_NS}}}key")
    key_n.text = "normal"
    url_n = etree.SubElement(pair_n, f"{{{KML_NS}}}styleUrl")
    url_n.text = f"#{_FIELD_NOTE_STYLE_ID}"
    pair_hl = etree.SubElement(smap, f"{{{KML_NS}}}Pair")
    key_hl = etree.SubElement(pair_hl, f"{{{KML_NS}}}key")
    key_hl.text = "highlight"
    url_hl = etree.SubElement(pair_hl, f"{{{KML_NS}}}styleUrl")
    url_hl.text = f"#{_FIELD_NOTE_STYLE_HL_ID}"


def add_note_folder(
    doc_element: etree._Element,
    notes: list[_NoteRecord],
) -> None:
    """Create a Field_Notes folder with a placemark for each unmatched note."""
    if not notes:
        return

    folder = etree.SubElement(doc_element, f"{{{KML_NS}}}Folder")
    fname = etree.SubElement(folder, f"{{{KML_NS}}}name")
    fname.text = "Field_Notes"
    fdesc = etree.SubElement(folder, f"{{{KML_NS}}}description")
    fdesc.text = f"{len(notes)} field note(s) that did not match an existing placemark"

    for note in notes:
        pm = etree.SubElement(folder, f"{{{KML_NS}}}Placemark")

        # Name
        pm_name = etree.SubElement(pm, f"{{{KML_NS}}}name")
        label = note.feature_name or ", ".join(note.themes[:2])
        pm_name.text = label

        # Description
        desc = etree.SubElement(pm, f"{{{KML_NS}}}description")
        body = (
            '<html><head></head><body style="margin:0;overflow:auto;background:#FFF">'
            + _note_html(note)
            + "</body></html>"
        )
        desc.text = etree.CDATA(body)

        # Style
        style_url = etree.SubElement(pm, f"{{{KML_NS}}}styleUrl")
        style_url.text = f"#{_FIELD_NOTE_STYLEMAP_ID}"

        # Point geometry
        if note.latitude is not None and note.longitude is not None:
            point = etree.SubElement(pm, f"{{{KML_NS}}}Point")
            coords = etree.SubElement(point, f"{{{KML_NS}}}coordinates")
            coords.text = f"{note.longitude},{note.latitude},0"


# ---------------------------------------------------------------------------
# Write KMZ
# ---------------------------------------------------------------------------

def write_kmz(tree: etree._ElementTree, output_path: str | Path) -> None:
    """Serialize the KML tree into a KMZ (ZIP containing doc.kml)."""
    kml_bytes = etree.tostring(
        tree,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml_bytes)


# ---------------------------------------------------------------------------
# High-level orchestrator
# ---------------------------------------------------------------------------

def inject_notes_into_kml(
    target_kml_path: str | Path,
    source_kml_paths: list[str | Path],
    output_path: str | Path,
    tolerance_m: float = 15.0,
    town_config_path: str | Path | None = None,
    progress_callback: Any = None,
) -> dict[str, int]:
    """End-to-end injection: parse sources, match, inject, write KMZ.

    Returns a summary dict with counts.
    """
    from kml2report.kml_parser import parse_files
    from kml2report.summarizer import classify_notes, load_town_config

    # --- Parse source KMLs and classify notes ---
    town_centers = load_town_config(town_config_path) if town_config_path else None
    source_features = parse_files([str(p) for p in source_kml_paths])
    classified = classify_notes(
        source_features,
        geocode=False,
        town_centers=town_centers,
    )

    notes = [
        _NoteRecord(
            feature_name=cn.feature_name,
            text=cn.text,
            themes=cn.themes,
            latitude=cn.latitude if cn.latitude else 0.0,
            longitude=cn.longitude if cn.longitude else 0.0,
            pole_id=cn.pole_id,
            town=cn.town,
            google_maps_url=cn.google_maps_url,
        )
        for cn in classified
    ]

    # --- Parse target V2 KML ---
    target_tree = etree.parse(str(target_kml_path))
    v2_placemarks = parse_v2_placemarks(target_tree)

    # --- Match ---
    matched, unmatched = match_notes_to_placemarks(notes, v2_placemarks, tolerance_m)

    # --- Inject into matched placemarks ---
    for pm_idx, pm_notes in matched.items():
        inject_notes_into_placemark(v2_placemarks[pm_idx].element, pm_notes)

    # --- Add unmatched as new placemarks ---
    doc_el = target_tree.find(".//k:Document", NS)
    if doc_el is None:
        doc_el = target_tree.getroot()

    inject_field_note_style(doc_el)
    add_note_folder(doc_el, unmatched)

    # --- Write output ---
    output_path = Path(output_path)
    if output_path.suffix.lower() == ".kmz":
        write_kmz(target_tree, output_path)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        target_tree.write(
            str(output_path),
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True,
        )

    total_matched_notes = sum(len(v) for v in matched.values())
    return {
        "total_notes": len(notes),
        "matched_notes": total_matched_notes,
        "matched_placemarks": len(matched),
        "unmatched_notes": len(unmatched),
        "v2_placemarks": len(v2_placemarks),
    }

"""Field notes summarizer — location-intelligent edition.

Takes parsed KML features and produces a structured narrative report by:
1. Extracting clean text from every description
2. Classifying each note into one or more themes
3. Spatially clustering notes into geographic corridors
4. Enriching each cluster with start/end coords, bearing, distance from
   project center, and street/road references extracted from the text
5. Generating a Markdown report a desk reviewer can use to cross-reference
   the KML map without having been in the field
"""

from __future__ import annotations

import json
import re
import math
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

from kml2report.models import Feature


# ===================================================================
# Theme definitions
# ===================================================================

@dataclass
class ThemeDef:
    key: str
    heading: str
    patterns: list[re.Pattern]


THEMES: list[ThemeDef] = [
    ThemeDef(
        key="access_row",
        heading="Right-of-Way (ROW) Access",
        patterns=[
            re.compile(r"\binside\s+r\.?o\.?w\.?\b", re.I),
            re.compile(r"\bin\s+r\.?o\.?w\.?\b", re.I),
            re.compile(r"\bright.of.way\b", re.I),
            re.compile(r"\bback\s+in\s+row\b", re.I),
            re.compile(r"\binside\s+row\b", re.I),
            re.compile(r"\boff\s+right\s+away\b", re.I),
            re.compile(r"\bright\s+away\b", re.I),
        ],
    ),
    ThemeDef(
        key="access_private",
        heading="Private Property / Fenceline Access",
        patterns=[
            re.compile(r"\bprivate\s+property\b", re.I),
            re.compile(r"\binside\s+fence\b", re.I),
            re.compile(r"\bfence\s*line\b", re.I),
            re.compile(r"\bprivate\s+road\b", re.I),
            re.compile(r"\bprivate\s+fence\b", re.I),
            re.compile(r"\bsecure\s+gate\b", re.I),
            re.compile(r"\beasement\b", re.I),
            re.compile(r"\bagreement\b", re.I),
            re.compile(r"\bproperty\s+line\b", re.I),
        ],
    ),
    ThemeDef(
        key="pole_condition",
        heading="Pole Condition & Attachments",
        patterns=[
            re.compile(r"\bgood\s+pole", re.I),
            re.compile(r"\bsolid\s+pole", re.I),
            re.compile(r"\bgood\s+condition\b", re.I),
            re.compile(r"\bno\s+attach", re.I),
            re.compile(r"\bone\s+attach", re.I),
            re.compile(r"\bcomm\s+attach", re.I),
            re.compile(r"\bcable\s+(mso|company)\b", re.I),
            re.compile(r"\bat&t\b", re.I),
            re.compile(r"\bsbc\b", re.I),
            re.compile(r"\bprovider\b", re.I),
            re.compile(r"\bexo\s+pole", re.I),
            re.compile(r"\btransformer\b", re.I),
            re.compile(r"\breclos", re.I),
            re.compile(r"\benergized\b", re.I),
            re.compile(r"\bhigh\s+voltage\b", re.I),
            re.compile(r"\bsteel\s+pole\b", re.I),
        ],
    ),
    ThemeDef(
        key="construction_plow",
        heading="Plow / Trenching Opportunities",
        patterns=[
            re.compile(r"\bplow\b", re.I),
            re.compile(r"\btrench\b", re.I),
            re.compile(r"\bgood\s+soil\b", re.I),
        ],
    ),
    ThemeDef(
        key="construction_bore",
        heading="Bore / Directional Drill Requirements",
        patterns=[
            re.compile(r"\bbore\b", re.I),
            re.compile(r"\bdrill\b", re.I),
            re.compile(r"\bshoot\b", re.I),
            re.compile(r"\bmissile\b", re.I),
            re.compile(r"\bdepth\b", re.I),
        ],
    ),
    ThemeDef(
        key="construction_aerial",
        heading="Aerial Strand Opportunities",
        patterns=[
            re.compile(r"\bstrand\b", re.I),
            re.compile(r"\baerial\b", re.I),
            re.compile(r"\bhang\b", re.I),
            re.compile(r"\bdip\b", re.I),
        ],
    ),
    ThemeDef(
        key="obstacles",
        heading="Obstacles & Hazards",
        patterns=[
            re.compile(r"\bcreek\b", re.I),
            re.compile(r"\bdrainage\b", re.I),
            re.compile(r"\bculvert\b", re.I),
            re.compile(r"\btree\s+trim", re.I),
            re.compile(r"\bbrush\b", re.I),
            re.compile(r"\bnarrow\b", re.I),
            re.compile(r"\bdrive(way)?s?\b", re.I),
            re.compile(r"\bclearance\b", re.I),
            re.compile(r"\bproblematic\b", re.I),
            re.compile(r"\bdeclined\b", re.I),
            re.compile(r"\bnever\s+work\b", re.I),
        ],
    ),
    ThemeDef(
        key="permits",
        heading="Permits & Regulatory",
        patterns=[
            re.compile(r"\bpermit\b", re.I),
            re.compile(r"\btxdot\b", re.I),
            re.compile(r"\bdot\b", re.I),
            re.compile(r"\bhighway\s+(crossing|bore)\b", re.I),
            re.compile(r"\binterstate\b", re.I),
            re.compile(r"\brr\s+row\b", re.I),
            re.compile(r"\brailroad\b", re.I),
        ],
    ),
]


# ===================================================================
# Road / street name extraction
# ===================================================================

_ROAD_RE = re.compile(
    r"\b("
    r"CR\s*\d+"
    r"|FM\s*\d+"
    r"|HWY\s*\d+"
    r"|US\s*\d+"
    r"|I-\d+"
    r"|Avenue\s+[A-Z]"
    r"|(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth"
    r"|\d+(?:st|nd|rd|th))\s+(?:St(?:reet)?|Ave(?:nue)?|Rd|Road|Dr(?:ive)?|Ln|Lane|Blvd|Way)"
    r"|(?:Coxwell|Crooked\s+Creek|Annette|Strauss|Nauman|Sheridan|Columbus|Fannin|Hooper)"
    r"(?:\s+(?:Rd|Road|Dr|Drive|Ln|Lane|St|Street))?"
    r")\b",
    re.I,
)


def _extract_roads(text: str) -> list[str]:
    """Pull road / street references from free text."""
    matches = _ROAD_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        norm = re.sub(r"\s+", " ", m.strip())
        key = norm.lower()
        if key not in seen:
            seen.add(key)
            result.append(norm)
    return result


# ===================================================================
# Geographic helpers
# ===================================================================

def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    R = 3958.8
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees from point 1 to point 2."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_COMPASS_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _bearing_to_compass(deg: float) -> str:
    idx = round(deg / 22.5) % 16
    return _COMPASS_16[idx]


def _centroid_of(lats: list[float], lons: list[float]) -> tuple[float, float]:
    if not lats:
        return (0.0, 0.0)
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _centroid(features: list[Feature]) -> tuple[float, float]:
    lats, lons = [], []
    for f in features:
        if f.geometry and f.geometry.latitude is not None:
            lats.append(f.geometry.latitude)
            lons.append(f.geometry.longitude)
    return _centroid_of(lats, lons)


def _bbox(features: list[Feature]) -> tuple[float, float, float, float]:
    lats, lons = [], []
    for f in features:
        if f.geometry and f.geometry.latitude is not None:
            lats.append(f.geometry.latitude)
            lons.append(f.geometry.longitude)
    if not lats:
        return (0, 0, 0, 0)
    return (min(lats), max(lats), min(lons), max(lons))


def _fmt_coord(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "N/A"
    return f"{lat:.6f}, {lon:.6f}"


# ===================================================================
# Spatial clustering (greedy, no external deps)
# ===================================================================

CLUSTER_RADIUS_DEG = 0.008  # ~0.5 mi at 30°N


@dataclass
class GeoCluster:
    """A spatial cluster of classified notes."""
    notes: list["ClassifiedNote"]
    center_lat: float = 0.0
    center_lon: float = 0.0
    start_lat: float = 0.0
    start_lon: float = 0.0
    end_lat: float = 0.0
    end_lon: float = 0.0
    span_mi: float = 0.0
    bearing_deg: float = 0.0
    compass: str = ""
    direction_from_center: str = ""
    distance_from_center_mi: float = 0.0
    roads: list[str] = dc_field(default_factory=list)
    label: str = ""
    geocoded_road: str = ""
    geocoded_town: str = ""
    geocoded_county: str = ""


def _cluster_notes(
    notes: list["ClassifiedNote"],
    project_center: tuple[float, float],
) -> list[GeoCluster]:
    """Greedy spatial clustering of notes by proximity."""
    located = [n for n in notes if n.latitude is not None]
    unlocated = [n for n in notes if n.latitude is None]

    located.sort(key=lambda n: (n.latitude, n.longitude))

    clusters: list[GeoCluster] = []
    assigned: set[int] = set()

    for i, seed in enumerate(located):
        if i in assigned:
            continue
        members = [seed]
        assigned.add(i)
        for j, other in enumerate(located):
            if j in assigned:
                continue
            if (abs(other.latitude - seed.latitude) < CLUSTER_RADIUS_DEG
                    and abs(other.longitude - seed.longitude) < CLUSTER_RADIUS_DEG):
                members.append(other)
                assigned.add(j)
        clusters.append(_build_cluster(members, project_center))

    if unlocated:
        clusters.append(_build_cluster(unlocated, project_center))

    clusters.sort(key=lambda c: c.distance_from_center_mi)
    return clusters


def _build_cluster(
    notes: list["ClassifiedNote"],
    project_center: tuple[float, float],
) -> GeoCluster:
    c = GeoCluster(notes=notes)
    lats = [n.latitude for n in notes if n.latitude is not None]
    lons = [n.longitude for n in notes if n.longitude is not None]

    if lats:
        c.center_lat, c.center_lon = _centroid_of(lats, lons)
        c.start_lat, c.start_lon = min(lats), lons[lats.index(min(lats))]
        c.end_lat, c.end_lon = max(lats), lons[lats.index(max(lats))]
        c.span_mi = _haversine_mi(c.start_lat, c.start_lon, c.end_lat, c.end_lon)
        if c.span_mi > 0.01:
            c.bearing_deg = _bearing_deg(c.start_lat, c.start_lon, c.end_lat, c.end_lon)
            c.compass = _bearing_to_compass(c.bearing_deg)
        pc_lat, pc_lon = project_center
        c.distance_from_center_mi = _haversine_mi(pc_lat, pc_lon, c.center_lat, c.center_lon)
        c.direction_from_center = _bearing_to_compass(
            _bearing_deg(pc_lat, pc_lon, c.center_lat, c.center_lon)
        )

    all_text = " ".join(n.text for n in notes) + " " + " ".join(n.feature_name for n in notes)
    c.roads = _extract_roads(all_text)

    geocoded_roads: list[str] = []
    geocoded_towns: list[str] = []
    geocoded_counties: list[str] = []
    for n in notes:
        if n.nearest_road and n.nearest_road not in geocoded_roads:
            geocoded_roads.append(n.nearest_road)
        if n.nearest_town and n.nearest_town not in geocoded_towns:
            geocoded_towns.append(n.nearest_town)
        if n.county and n.county not in geocoded_counties:
            geocoded_counties.append(n.county)

    c.geocoded_road = geocoded_roads[0] if geocoded_roads else ""
    c.geocoded_town = geocoded_towns[0] if geocoded_towns else ""
    c.geocoded_county = geocoded_counties[0] if geocoded_counties else ""

    if geocoded_roads:
        label_parts = [geocoded_roads[0]]
        if len(geocoded_roads) > 1:
            label_parts[0] += f" / {geocoded_roads[1]}"
        if geocoded_towns:
            label_parts.append(f"near {geocoded_towns[0]}")
        c.label = ", ".join(label_parts)
    elif c.roads:
        c.label = f"Near {', '.join(c.roads[:3])}"
    elif geocoded_towns:
        c.label = f"Near {geocoded_towns[0]}"
    elif lats:
        c.label = f"{c.distance_from_center_mi:.1f} mi {c.direction_from_center} of center"
    else:
        c.label = "Location not recorded"

    return c


# ===================================================================
# Note classification
# ===================================================================

@dataclass
class ClassifiedNote:
    feature_name: str
    layer: str
    text: str
    themes: list[str]
    latitude: float | None = None
    longitude: float | None = None
    quadrant: str = ""
    pole_id: str = ""
    roads: list[str] = dc_field(default_factory=list)
    nearest_road: str = ""
    nearest_town: str = ""
    county: str = ""
    location_label: str = ""
    town: str = ""
    google_maps_url: str = ""


def _extract_pole_id(text: str) -> str:
    m = re.search(r"\bpole\s*#?\s*(\d{3,})\b", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4,})\b", text)
    if m:
        return m.group(1)
    return ""


_TTT_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bright away\b", re.I), "right-of-way"),
    (re.compile(r"\bRight Away\b"), "Right-of-Way"),
    (re.compile(r"\bPolls are\b"), "Poles are"),
    (re.compile(r"\bpolls are\b"), "poles are"),
    (re.compile(r"\bthe polls\b", re.I), "the poles"),
    (re.compile(r"\bthese polls\b", re.I), "these poles"),
    (re.compile(r"\bof polls\b", re.I), "of poles"),
    (re.compile(r"\bstretch of polls\b", re.I), "stretch of poles"),
    (re.compile(r"\bthe poll\b", re.I), "the pole"),
    (re.compile(r"\bgood poll\b", re.I), "good pole"),
    (re.compile(r"\bThese holes\b"), "These poles"),
    (re.compile(r"\bthese holes\b"), "these poles"),
    (re.compile(r"\bHoles are\b"), "Poles are"),
    (re.compile(r"\bholes are\b"), "poles are"),
    (re.compile(r"\bThese balls\b"), "These poles"),
    (re.compile(r"\bthese balls\b"), "these poles"),
    (re.compile(r"\bPulls on\b"), "Poles on"),
    (re.compile(r"\bpulls on\b"), "poles on"),
    (re.compile(r"\bThis pool\b"), "This pole"),
    (re.compile(r"\bthis pool\b"), "this pole"),
    (re.compile(r"\bPaul['\u2019]s from\b"), "Poles from"),
    (re.compile(r"\bPaul has\b"), "Pole has"),
    (re.compile(r"\btruck pulls over\b", re.I), "truck pull-over"),
    (re.compile(r"\bGood poker line\b"), "Good pole line"),
    (re.compile(r"\bgood poker line\b"), "good pole line"),
    (re.compile(r"\bThis stretcher poles\b"), "This stretch of poles"),
    (re.compile(r"\bthis stretcher poles\b"), "this stretch of poles"),
    (re.compile(r"\bThis stretcher road\b"), "This stretch of road"),
    (re.compile(r"\bthis stretcher road\b"), "this stretch of road"),
    (re.compile(r"\bstretcher poles\b"), "stretch of poles"),
    (re.compile(r"\balong this rotor\b", re.I), "along this road are"),
    (re.compile(r"\bBarbwire\b"), "barbed wire"),
    (re.compile(r"\bbarbwire\b"), "barbed wire"),
    (re.compile(r"\bviability to drops\b", re.I), "visibility to drops"),
    (re.compile(r"\balong me south\b", re.I), "along the south"),
    (re.compile(r"\bwil probably\b", re.I), "will probably"),
    (re.compile(r"\binside inside\b", re.I), "inside"),
    (re.compile(r"\bIt can't use then plow\b", re.I), "If can't use, then plow"),
    (re.compile(r"\bAll of t the\b", re.I), "All of the"),
    (re.compile(r"\bcomp provider\b", re.I), "comm provider"),
    (re.compile(r"\bexisting commas\b", re.I), "existing comms"),
    (re.compile(r"\bexisting cos\b", re.I), "existing comms"),
    (re.compile(r"\boneattach com\b", re.I), "one attached comm"),
    (re.compile(r"\bno attach\b", re.I), "no attachments"),
    (re.compile(r"\bNo attach\b"), "No attachments"),
    (re.compile(r"\bOne attach\b"), "One attachment"),
    (re.compile(r"\bone attach\b"), "one attachment"),
    (re.compile(r"\bFiberlihht\b", re.I), "Fiberlight"),
    (re.compile(r"\bTreee trim\b", re.I), "Tree trim"),
    (re.compile(r"\bPole lone\b"), "Pole line"),
    (re.compile(r"\bpole lone\b"), "pole line"),
    (re.compile(r"\brode his private road\b", re.I), "road is a private road"),
    (re.compile(r"\btree trouble in his pole\b", re.I), "tree trouble; this is pole"),
    (re.compile(r"\bprivate land To these\b"), "landowners."),
    (re.compile(r"\bexiting pole line\b", re.I), "existing pole line"),
    (re.compile(r"\bchange to bore diveway\b", re.I), "change to bore driveway"),
    (re.compile(r"\bWith nothing I see\b"), "Worth noting, I see"),
    (re.compile(r"\bplow with necessary\b", re.I), "plow if necessary"),
    (re.compile(r"\bThat's are good\b"), "These are good"),
    (re.compile(r"\bWe're all in good\b"), "They're all in good"),
    (re.compile(r"\b(\d+)\s*'\s*feet\b"), r"\1 feet"),
]


def _clean_talk_to_text(text: str) -> str:
    """Fix common speech-to-text transcription errors in field notes."""
    for pattern, replacement in _TTT_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


_DEFAULT_TOWN_CENTERS: list[tuple[str, float, float]] = [
    ("Sheridan", 29.5029, -96.6653),
    ("Garwood", 29.4500, -96.4000),
    ("Columbus", 29.7065, -96.5397),
]


def load_town_config(
    config_path: str | Path | None = None,
) -> list[tuple[str, float, float]]:
    """Load town centers from a JSON config file.

    Expected JSON schema::

        {
          "county": "Colorado",
          "state": "Texas",
          "towns": [
            {"name": "Sheridan", "latitude": 29.5029, "longitude": -96.6653},
            ...
          ]
        }

    Falls back to built-in defaults when *config_path* is ``None`` or the
    file cannot be read.
    """
    if config_path is None:
        return list(_DEFAULT_TOWN_CENTERS)
    p = Path(config_path)
    if not p.is_file():
        return list(_DEFAULT_TOWN_CENTERS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        towns = data.get("towns", [])
        return [(t["name"], float(t["latitude"]), float(t["longitude"])) for t in towns]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return list(_DEFAULT_TOWN_CENTERS)


_HIGH_RISK_THEMES = frozenset({
    "access_private", "obstacles", "construction_bore", "permits",
})


def _assign_town(
    lat: float | None,
    lon: float | None,
    town_centers: list[tuple[str, float, float]] | None = None,
) -> str:
    """Assign a note to the nearest client-tracked town center."""
    if lat is None or lon is None:
        return ""
    centers = town_centers or _DEFAULT_TOWN_CENTERS
    best_town = ""
    best_dist = float("inf")
    for name, t_lat, t_lon in centers:
        d = _haversine_mi(lat, lon, t_lat, t_lon)
        if d < best_dist:
            best_dist = d
            best_town = name
    return best_town


def _google_maps_url(lat: float | None, lon: float | None) -> str:
    """Return a clickable Google Maps URL for a coordinate pair."""
    if lat is None or lon is None:
        return ""
    return f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"


def classify_notes(
    features: list[Feature],
    geocode: bool = True,
    cache_path: Any = None,
    progress_callback: Any = None,
    town_centers: list[tuple[str, float, float]] | None = None,
) -> list[ClassifiedNote]:
    from kml2report.description_parser import parse_description

    center_lat, center_lon = _centroid(features)
    results: list[ClassifiedNote] = []

    for feat in features:
        attrs = parse_description(feat.raw_description)
        text = attrs.get("notes", "").strip()
        if not text or len(text) < 3:
            continue

        raw_text = text
        themes = []
        for theme in THEMES:
            for pat in theme.patterns:
                if pat.search(raw_text):
                    themes.append(theme.key)
                    break
        if not themes:
            themes = ["general"]

        text = _clean_talk_to_text(text)

        lat = feat.geometry.latitude if feat.geometry else None
        lon = feat.geometry.longitude if feat.geometry else None
        quad = ""
        if lat is not None and lon is not None:
            ns = "N" if lat >= center_lat else "S"
            ew = "E" if lon >= center_lon else "W"
            quad = ns + ew

        all_text = text + " " + feat.name
        roads = _extract_roads(all_text)

        results.append(ClassifiedNote(
            feature_name=_clean_talk_to_text(feat.name),
            layer=feat.layer,
            text=text,
            themes=themes,
            latitude=lat,
            longitude=lon,
            quadrant=quad,
            pole_id=_extract_pole_id(text),
            roads=roads,
            town=_assign_town(lat, lon, town_centers=town_centers),
            google_maps_url=_google_maps_url(lat, lon),
        ))

    if geocode and results:
        _apply_geocoding(results, cache_path, progress_callback)

    return results


def _apply_geocoding(
    notes: list[ClassifiedNote],
    cache_path: Any = None,
    progress_callback: Any = None,
) -> None:
    """Reverse-geocode all notes and populate location fields."""
    from kml2report.geocoder import batch_reverse_geocode, _grid_key

    coords = [
        (n.latitude, n.longitude)
        for n in notes
        if n.latitude is not None and n.longitude is not None
    ]
    if not coords:
        return

    geo_map = batch_reverse_geocode(
        coords,
        cache_path=cache_path,
        progress_callback=progress_callback,
    )

    for n in notes:
        if n.latitude is None or n.longitude is None:
            continue
        key = _grid_key(n.latitude, n.longitude)
        info = geo_map.get(key)
        if info:
            n.nearest_road = info.road
            n.nearest_town = info.town
            n.county = info.county
            n.location_label = info.short_label


# ===================================================================
# Report generation
# ===================================================================

def _is_descriptive_name(name: str) -> bool:
    """Return True if *name* is a meaningful label (not just a numeric ID)."""
    if not name:
        return False
    stripped = re.sub(r"[\s\-_.#]+", "", name)
    return not stripped.isdigit()


def _note_cell(n: "ClassifiedNote") -> str:
    """Format the Note cell, suppressing numeric-only feature names."""
    if _is_descriptive_name(n.feature_name):
        return f"**{n.feature_name}** — {n.text}"
    return n.text


def _cluster_narrative(cl: GeoCluster, doc_name: str) -> list[str]:
    """Build a concise narrative block for one geographic cluster."""
    lines: list[str] = []
    n_notes = len(cl.notes)
    located = cl.center_lat != 0.0

    unique: list[ClassifiedNote] = []
    seen: set[str] = set()
    for n in cl.notes:
        k = n.text.lower().strip()
        if k not in seen:
            seen.add(k)
            unique.append(n)

    # --- Single-note clusters: compact inline format, no table ---
    if len(unique) == 1:
        n = unique[0]
        maps_url = n.google_maps_url or ""
        coord_s = _fmt_coord(n.latitude, n.longitude) if n.latitude else ""
        loc_parts = []
        if cl.label:
            loc_parts.append(f"**{cl.label}**")
        if coord_s and maps_url:
            loc_parts.append(f"[{coord_s}]({maps_url})")
        elif coord_s:
            loc_parts.append(coord_s)
        if located:
            loc_parts.append(
                f"{cl.distance_from_center_mi:.1f} mi {cl.direction_from_center}"
            )
        loc_str = " — ".join(loc_parts) if loc_parts else "Unknown location"
        lines.append(f"- {loc_str}: {_note_cell(n)}")
        lines.append("")
        return lines

    # --- Multi-note clusters ---
    heading_parts = [f"**{cl.label}**"]
    if located:
        heading_parts.append(
            f"— {cl.distance_from_center_mi:.1f} mi {cl.direction_from_center} of project center"
        )
    lines.append(f"### {' '.join(heading_parts)}")
    lines.append("")

    if located:
        maps_center = _google_maps_url(cl.center_lat, cl.center_lon)
        coord_link = (f"[{_fmt_coord(cl.center_lat, cl.center_lon)}]({maps_center})"
                      if maps_center
                      else _fmt_coord(cl.center_lat, cl.center_lon))
        lines.append(f"- **Center:** {coord_link}")
        if cl.span_mi > 0.05:
            lines.append(f"- **Span:** {cl.span_mi:.2f} mi, runs **{cl.compass}**")
        if cl.geocoded_road:
            lines.append(f"- **Nearest road:** {cl.geocoded_road}")
        lines.append(f"- **Notes:** {n_notes}")
    else:
        lines.append(f"- *{n_notes} note(s) without recorded coordinates*")
    lines.append("")

    lines.append("| # | Coordinates | Note |")
    lines.append("|--:|-------------|------|")
    for i, n in enumerate(unique, 1):
        coord_s = _fmt_coord(n.latitude, n.longitude) if n.latitude else "—"
        maps_url = n.google_maps_url or ""
        coord_cell = f"[{coord_s}]({maps_url})" if maps_url else coord_s
        lines.append(f"| {i} | {coord_cell} | {_note_cell(n)} |")
    lines.append("")
    return lines


def generate_summary_report(
    features: list[Feature],
    doc_name: str = "",
    geocode: bool = True,
    cache_path: Any = None,
    progress_callback: Any = None,
    town_filter: str | None = None,
    town_centers: list[tuple[str, float, float]] | None = None,
) -> str:
    """Generate a location-intelligent Markdown summary report.

    Parameters
    ----------
    town_filter : str, optional
        If provided, only include notes assigned to this town
        (e.g. "Sheridan", "Garwood", "Columbus").
    town_centers : list of (name, lat, lon), optional
        Override the default town center list.  Load from a JSON config
        via :func:`load_town_config`.
    """
    centers = town_centers or _DEFAULT_TOWN_CENTERS
    classified = classify_notes(
        features,
        geocode=geocode,
        cache_path=cache_path,
        progress_callback=progress_callback,
        town_centers=centers,
    )

    if town_filter:
        classified = [n for n in classified if n.town == town_filter]

    if not classified:
        return f"# Field Notes Summary\n\nNo field notes found in {doc_name or 'input'}.\n"

    if town_filter:
        town_coords = {name: (lat, lon) for name, lat, lon in centers}
        if town_filter in town_coords:
            t_lat, t_lon = town_coords[town_filter]
            features = [
                f for f in features
                if f.geometry and f.geometry.latitude is not None
                and _haversine_mi(f.geometry.latitude, f.geometry.longitude, t_lat, t_lon)
                < max(_haversine_mi(n.latitude, n.longitude, t_lat, t_lon)
                      for n in classified if n.latitude is not None) + 1.0
            ]

    min_lat, max_lat, min_lon, max_lon = _bbox(features)
    center_lat, center_lon = _centroid(features)
    layers = sorted(set(f.layer for f in features if f.layer))
    total_features = len(features)
    total_notes = len(classified)

    project_location = ""
    if geocode and center_lat != 0.0:
        from kml2report.geocoder import geocode_single
        center_info = geocode_single(center_lat, center_lon, cache_path=cache_path)
        parts = []
        if center_info.town:
            parts.append(center_info.town)
        if center_info.county and center_info.county != center_info.town:
            parts.append(center_info.county)
        if center_info.state:
            parts.append(center_info.state)
        project_location = ", ".join(parts)

    theme_notes: dict[str, list[ClassifiedNote]] = defaultdict(list)
    for cn in classified:
        for t in cn.themes:
            theme_notes[t].append(cn)

    theme_lookup = {t.key: t for t in THEMES}
    theme_lookup["general"] = ThemeDef(key="general", heading="General / Uncategorized Notes", patterns=[])

    lines: list[str] = []

    # --- Header ---
    title = doc_name or "KML Data"
    if project_location:
        title += f" ({project_location})"
    lines.append(f"# Field Notes Summary: {title}")
    lines.append("")
    lines.append("## How to Use This Report")
    lines.append("")
    lines.append("This report summarizes field observations recorded in the KML design files. "
                 "Each section groups notes by **theme** (access, construction method, obstacles, etc.) "
                 "and then by **geographic corridor**. Every corridor includes:")
    lines.append("")
    lines.append("- **Start / End coordinates** — paste into Google Maps or open the KML to jump to the area")
    lines.append("- **Bearing & span** — the direction the corridor runs and how far it stretches")
    lines.append("- **Distance & direction from project center** — quick orientation")
    lines.append("- **Nearest road & town** — reverse-geocoded from coordinates via OpenStreetMap")
    lines.append("- **Roads mentioned in notes** — extracted from field note text")
    lines.append("- **Individual note coordinates** — each row has lat/lon and a clickable Google Maps link")
    lines.append("")
    lines.append("**Note on feature labels:** Bold text before the em-dash in each note (e.g. "
                 "**Strand**, **Bore under drives**) is the original placemark name from the KML "
                 "design file, assigned by the field engineer. Numeric-only placemark IDs "
                 "(internal reference numbers) are suppressed for readability.")
    lines.append("")

    # --- Overview ---
    lines.append("## Project Overview")
    lines.append("")
    if project_location:
        lines.append(f"- **Project location:** {project_location}")
    lines.append(f"- **Total features:** {total_features:,}")
    lines.append(f"- **Features with field notes:** {total_notes:,}")
    lines.append(f"- **Layers:** {', '.join(layers) if layers else 'N/A'}")
    lines.append(f"- **Project center:** {_fmt_coord(center_lat, center_lon)}")
    lines.append(f"- **Bounding box:** {min_lat:.6f}N to {max_lat:.6f}N, "
                 f"{min_lon:.6f}W to {max_lon:.6f}W")
    lines.append(f"- **Approximate project span:** "
                 f"{_haversine_mi(min_lat, min_lon, max_lat, max_lon):.1f} mi corner-to-corner")
    lines.append("")

    # --- Theme summary table ---
    lines.append("## Themes at a Glance")
    lines.append("")
    lines.append("| Theme | Notes |")
    lines.append("|-------|------:|")
    ordered_keys = sorted(theme_notes.keys(),
                          key=lambda k: len(theme_notes[k]), reverse=True)
    for key in ordered_keys:
        heading = theme_lookup.get(key, ThemeDef(key=key, heading=key, patterns=[])).heading
        cnt = len(theme_notes[key])
        lines.append(f"| {heading} | {cnt} |")
    lines.append("")

    # --- Per-theme sections with geographic clusters ---
    for key in ordered_keys:
        heading = theme_lookup[key].heading
        notes = theme_notes[key]
        clusters = _cluster_notes(notes, (center_lat, center_lon))

        lines.append(f"## {heading}")
        lines.append("")
        lines.append(f"*{len(notes)} observation(s) in {len(clusters)} geographic area(s).*")
        lines.append("")

        for cl in clusters:
            lines.extend(_cluster_narrative(cl, doc_name))

    # --- Key Takeaways ---
    lines.append("## Key Takeaways")
    lines.append("")
    _append_takeaways(lines, theme_notes, theme_lookup, doc_name, total_notes,
                      (center_lat, center_lon))


    return "\n".join(lines)


def _append_takeaways(
    lines: list[str],
    theme_notes: dict[str, list[ClassifiedNote]],
    theme_lookup: dict[str, ThemeDef],
    doc_name: str,
    total_notes: int,
    project_center: tuple[float, float],
) -> None:
    if "access_private" in theme_notes:
        notes = theme_notes["access_private"]
        clusters = _cluster_notes(notes, project_center)
        top = clusters[0] if clusters else None
        pole_ids = sorted(set(n.pole_id for n in notes if n.pole_id))
        loc_desc = f"near {top.label}" if top else "across the project"
        lines.append(
            f"- **Private property access** is a key concern ({len(notes)} notes), "
            f"most concentrated {loc_desc}. "
            + (f"Poles referenced: {', '.join(pole_ids[:8])}. " if pole_ids else "")
            + "Easement or attachment agreements may be required."
        )

    if "access_row" in theme_notes:
        lines.append(
            f"- **{len(theme_notes['access_row'])} features** confirmed inside public ROW — "
            f"these represent the most accessible attachment/construction opportunities."
        )

    if "construction_plow" in theme_notes:
        notes = theme_notes["construction_plow"]
        clusters = _cluster_notes(notes, project_center)
        lines.append(
            f"- **Plow/trenching** is viable in {len(clusters)} corridor(s) "
            f"({len(notes)} notes). "
            + (f"Best area: {clusters[0].label}." if clusters else "")
        )

    if "construction_bore" in theme_notes:
        notes = theme_notes["construction_bore"]
        lines.append(
            f"- **Bore/directional drill** required at {len(notes)} locations — "
            f"typically creek crossings, driveways, or highway crossings."
        )

    if "construction_aerial" in theme_notes:
        lines.append(
            f"- **Aerial strand** feasible at {len(theme_notes['construction_aerial'])} noted locations."
        )

    if "obstacles" in theme_notes:
        lines.append(
            f"- **{len(theme_notes['obstacles'])} obstacle/hazard notes** — "
            f"creeks, driveways, narrow ROW, tree trimming, clearance issues."
        )

    if "pole_condition" in theme_notes:
        lines.append(
            f"- **Pole condition** noted {len(theme_notes['pole_condition'])} times. "
            f"Existing providers (AT&T, cable MSO, etc.) present on some segments."
        )

    if "permits" in theme_notes:
        lines.append(
            f"- **{len(theme_notes['permits'])} permit/regulatory notes** — "
            f"TxDOT, highway crossings, or railroad ROW may require special permits."
        )

    lines.append("")


def _append_field_intelligence_appendix(
    lines: list[str],
    classified: list[ClassifiedNote],
    theme_lookup: dict[str, ThemeDef],
) -> None:
    """Generate a Field Intelligence Appendix organized by town and risk category."""
    high_risk = [
        n for n in classified
        if any(t in _HIGH_RISK_THEMES for t in n.themes)
    ]
    if not high_risk:
        return

    lines.append("---")
    lines.append("")
    lines.append("## Field Intelligence Appendix — High-Risk Locations")
    lines.append("")
    lines.append(
        "This appendix lists every field note classified under a high-risk theme "
        "(private property access, obstacles, bore requirements, permits). "
        "Notes are organized by **town** and then by **risk category**. "
        "Each entry includes a clickable Google Maps link for field verification."
    )
    lines.append("")

    town_order = ["Sheridan", "Garwood", "Columbus", ""]
    town_labels = {"Sheridan": "Sheridan", "Garwood": "Garwood",
                   "Columbus": "Columbus", "": "Unlocated"}

    by_town: dict[str, list[ClassifiedNote]] = {t: [] for t in town_order}
    for n in high_risk:
        key = n.town if n.town in by_town else ""
        by_town[key].append(n)

    for town_key in town_order:
        town_notes = by_town[town_key]
        if not town_notes:
            continue

        lines.append(f"### {town_labels[town_key]}")
        lines.append("")
        lines.append(f"*{len(town_notes)} high-risk note(s)*")
        lines.append("")

        by_theme: dict[str, list[ClassifiedNote]] = defaultdict(list)
        for n in town_notes:
            for t in n.themes:
                if t in _HIGH_RISK_THEMES:
                    by_theme[t].append(n)

        for theme_key in sorted(by_theme.keys()):
            heading = theme_lookup.get(
                theme_key,
                ThemeDef(key=theme_key, heading=theme_key, patterns=[]),
            ).heading
            t_notes = by_theme[theme_key]

            lines.append(f"#### {heading}")
            lines.append("")
            lines.append("| # | Feature | Pole ID | Location | Map Link | Note |")
            lines.append("|--:|---------|---------|----------|----------|------|")
            for i, n in enumerate(t_notes, 1):
                loc = n.location_label or _fmt_coord(n.latitude, n.longitude)
                map_link = f"[Open]({n.google_maps_url})" if n.google_maps_url else ""
                pid = n.pole_id or "—"
                lines.append(
                    f"| {i} | {n.feature_name} | {pid} | {loc} | {map_link} | {n.text} |"
                )
            lines.append("")

    lines.append("")

"""Construction print (PDF) support: place print sheets on the map and read callouts.

The reader works on vector PDFs with a text layer (CAD plots). Each sheet is
georeferenced from the pole callouts printed on it (LAT / LONG) plus the plan
scale taken from the PDF's own measure dictionary, so no map picture is needed.
Sheets with no pole coordinates are placed from the neighbouring sheets when the
user supplies a manual anchor; otherwise they are listed as "unplaced".

Everything is plain data (dataclasses) so the Streamlit tab, the KML export and
the tests all use the same objects.
"""

from __future__ import annotations

import io
import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional

FT_PER_DEG_LAT = 364_567.0  # good to a few ft over a small project
DEFAULT_FT_PER_PT = 0.49363  # fallback when the PDF has no /Measure

_SHEET_RE = re.compile(r"^PRINT-(\d{1,3})$", re.I)
_FOOT_RE = re.compile(r"^(\d{1,5}(?:\.\d+)?)'$")
_LAT_RE = re.compile(r"^-?\d{1,2}\.\d{3,}$")
_HH_RE = re.compile(r"^(HH|FP|MH|PED)\s?-?(\d+)$", re.I)


@dataclass
class Callout:
    kind: str  # pole | footage | handhole | bore | riser | cable
    text: str
    x: float  # PDF points, unrotated page frame, y down
    y: float
    value_ft: Optional[float] = None
    color: str = ""
    lon: Optional[float] = None
    lat: Optional[float] = None
    pole_num: str = ""
    flag: str = ""


@dataclass
class Sheet:
    page: int  # 1-based
    label: str  # e.g. PRINT-05 or "page 7"
    width: float
    height: float
    ft_per_pt: float = DEFAULT_FT_PER_PT
    plan_bbox: Optional[tuple] = None  # x0, y0, x1, y1 in the same frame
    callouts: list = field(default_factory=list)
    # georeference: lon/lat at the page origin (0, 0), north-up
    origin_lon: Optional[float] = None
    origin_lat: Optional[float] = None
    rms_ft: Optional[float] = None
    n_anchors: int = 0
    notes: list = field(default_factory=list)

    @property
    def placed(self) -> bool:
        return self.origin_lon is not None and self.origin_lat is not None

    def to_lonlat(self, x: float, y: float) -> tuple:
        lat0 = self.origin_lat
        ft_lon = FT_PER_DEG_LAT * math.cos(math.radians(lat0))
        return (self.origin_lon + x * self.ft_per_pt / ft_lon,
                lat0 - y * self.ft_per_pt / FT_PER_DEG_LAT)

    def footprint(self) -> list:
        """Closed ring (lon, lat) of the plan area on the ground."""
        if not self.placed:
            return []
        x0, y0, x1, y1 = self.plan_bbox or (0, 0, self.width, self.height)
        ring = [self.to_lonlat(x0, y0), self.to_lonlat(x1, y0),
                self.to_lonlat(x1, y1), self.to_lonlat(x0, y1)]
        return ring + [ring[0]]

    def footage_total(self, colors: Optional[Iterable[str]] = None) -> float:
        want = set(colors) if colors else None
        return sum(c.value_ft or 0 for c in self.callouts
                   if c.kind == "footage" and (want is None or c.color in want))


@dataclass
class PlanSet:
    name: str
    sheets: list
    warnings: list = field(default_factory=list)

    def placed(self) -> list:
        return [s for s in self.sheets if s.placed]

    def callouts(self) -> list:
        return [(s, c) for s in self.sheets for c in s.callouts]


# ---------------------------------------------------------------- geometry

def _color_key(c) -> str:
    if c is None:
        return ""
    if isinstance(c, (int, float)):
        c = (c,)
    try:
        return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in (list(c) * 3)[:3]) \
            if len(c) in (1, 3) else ""
    except Exception:
        return ""


def _meters_ft(dlon: float, dlat: float, lat: float) -> tuple:
    return (dlon * FT_PER_DEG_LAT * math.cos(math.radians(lat)), dlat * FT_PER_DEG_LAT)


def _fit_translation(sheet: Sheet, pairs: list, reject_ft: float = 150.0) -> None:
    """North-up, known-scale fit: origin = median(lonlat - offset). Flags outliers."""
    if not pairs:
        return
    lat_guess = statistics.median(c.lat for c in pairs)
    ft_lon = FT_PER_DEG_LAT * math.cos(math.radians(lat_guess))
    s = sheet.ft_per_pt
    ox = [c.lon - c.x * s / ft_lon for c in pairs]
    oy = [c.lat + c.y * s / FT_PER_DEG_LAT for c in pairs]
    mx, my = statistics.median(ox), statistics.median(oy)
    keep = []
    for c, a, b in zip(pairs, ox, oy):
        err = math.hypot((a - mx) * ft_lon, (b - my) * FT_PER_DEG_LAT)
        if err > reject_ft and len(pairs) >= 3:
            c.flag = f"coordinate disagrees with the rest of the sheet by {err:,.0f} ft (typo?)"
        else:
            keep.append((a, b))
    if keep:
        mx = statistics.fmean(a for a, _ in keep)
        my = statistics.fmean(b for _, b in keep)
        sheet.rms_ft = math.sqrt(statistics.fmean(
            ((a - mx) * ft_lon) ** 2 + ((b - my) * FT_PER_DEG_LAT) ** 2 for a, b in keep))
    sheet.origin_lon, sheet.origin_lat = mx, my
    sheet.n_anchors = len(keep)


# ---------------------------------------------------------------- PDF reading

def _scale_from_pdf(reader, page_index: int) -> tuple:
    """(ft per point, plan bbox in unrotated frame) from /VP /Measure, else defaults."""
    try:
        page = reader.pages[page_index]
        for vp in page.get("/VP") or []:
            vp = vp.get_object()
            meas = vp.get("/Measure")
            bbox = [float(v) for v in vp.get("/BBox")]
            if meas is None:
                continue
            meas = meas.get_object()
            d = meas.get("/D")
            if d is not None:
                d0 = d.get_object()[0].get_object()
                c = float(d0.get("/C", 0))
                u = str(d0.get("/U", "")).lower()
                if c and u in ("ft", "feet", "'"):
                    return c, (min(bbox[0], bbox[2]), min(bbox[1], bbox[3]),
                               max(bbox[0], bbox[2]), max(bbox[1], bbox[3]))
            x = meas.get("/X")
            if x is not None:
                c = float(x.get_object()[0].get_object().get("/C", 0))
                u = str(x.get_object()[0].get_object().get("/U", "")).lower()
                if c and u in ("ft", "feet", "'"):
                    return c, (bbox[0], bbox[1], bbox[2], bbox[3])
    except Exception:
        pass
    return DEFAULT_FT_PER_PT, None


def _group_lines(words: list, y_tol: float = 3.0) -> list:
    rows: list = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for r in rows:
            if abs(r[0]["top"] - w["top"]) <= y_tol:
                r.append(w)
                break
        else:
            rows.append([w])
    return rows


def _parse_poles(words: list) -> list:
    """Pole callout blocks: a LAT : value line and a LONG : value line stacked."""
    poles = []
    lats = [w for w in words if w["text"] == "LAT"]
    for lat_w in lats:
        # value to the right on the same line
        row = [w for w in words if abs(w["top"] - lat_w["top"]) < 3 and w["x0"] > lat_w["x0"]]
        row.sort(key=lambda w: w["x0"])
        lat_v = next((w for w in row if _LAT_RE.match(w["text"])), None)
        long_w = next((w for w in words if w["text"] == "LONG"
                       and abs(w["x0"] - lat_w["x0"]) < 4
                       and 5 < w["top"] - lat_w["top"] < 20), None)
        if not lat_v or not long_w:
            continue
        lrow = [w for w in words if abs(w["top"] - long_w["top"]) < 3 and w["x0"] > long_w["x0"]]
        lrow.sort(key=lambda w: w["x0"])
        lon_v = next((w for w in lrow if _LAT_RE.match(w["text"])), None)
        if not lon_v:
            continue
        lat, lon = float(lat_v["text"]), float(lon_v["text"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        num = ""
        for w in words:
            if (w["text"].startswith("POLE_NUM:") and abs(w["x0"] - lat_w["x0"]) < 4
                    and 0 < lat_w["top"] - w["top"] < 40):
                rest = w["text"][len("POLE_NUM:"):]
                if not rest:
                    nxt = [v for v in words if abs(v["top"] - w["top"]) < 3 and v["x0"] > w["x0"]]
                    nxt.sort(key=lambda v: v["x0"])
                    rest = nxt[0]["text"] if nxt else ""
                num = rest
                break
        poles.append(Callout("pole", f"POLE {num or '?'}", lat_w["x0"], lat_w["top"],
                             lon=lon, lat=lat, pole_num=num))
    return poles


def _pole_symbols(page) -> list:
    out = []
    for o in page.curves:
        w, h = o["x1"] - o["x0"], o["bottom"] - o["top"]
        if 18.5 < w < 21 and abs(w - h) < 1.0 and o.get("stroking_color") == (1.0, 0.0, 0.0):
            out.append(((o["x0"] + o["x1"]) / 2, (o["top"] + o["bottom"]) / 2))
    return out


def _snap_poles_to_symbols(poles: list, symbols: list, max_dist: float = 160.0) -> None:
    """Move each pole callout onto its red pole circle (greedy nearest pairs)."""
    pairs = sorted(((math.hypot(p.x - sx, p.y - sy), i, j)
                    for i, p in enumerate(poles) for j, (sx, sy) in enumerate(symbols)))
    used_p, used_s = set(), set()
    for d, i, j in pairs:
        if d > max_dist:
            break
        if i in used_p or j in used_s:
            continue
        used_p.add(i)
        used_s.add(j)
        poles[i].x, poles[i].y = symbols[j]


def _label_for(words: list, page_w: float, page_h: float) -> str:
    """Sheet number from the title block (largest PRINT-nn in the bottom-right corner).

    Matchline references elsewhere on the page also read PRINT-nn, so only the
    title block is trusted. Returns "" when the title block is missing.
    """
    best = None
    for w in words:
        if not _SHEET_RE.match(w["text"]):
            continue
        corner = w["x0"] > 0.75 * page_w and w["top"] > 0.85 * page_h
        key = (corner, w["size"])
        if best is None or key > best[0]:
            best = (key, w)
    if best is None or not best[0][0]:
        return ""
    return best[1]["text"].upper()


def _footage(words: list) -> list:
    out = []
    for w in words:
        m = _FOOT_RE.match(w["text"])
        if not m:
            continue
        v = float(m.group(1))
        out.append(Callout("footage", w["text"], (w["x0"] + w["x1"]) / 2,
                           (w["top"] + w["bottom"]) / 2, value_ft=v,
                           color=_color_key(w.get("non_stroking_color"))))
    return out


def _structures(words: list) -> list:
    out = []
    for w in words:
        m = _HH_RE.match(w["text"])
        if m:
            out.append(Callout("handhole", w["text"].upper().replace(" ", ""),
                               (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2,
                               color=_color_key(w.get("non_stroking_color"))))
    return out


def read_plan_set(data: bytes, name: str = "prints.pdf", progress=None) -> PlanSet:
    """Parse a print-set PDF into sheets with callouts. Georeference separately."""
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    sheets: list = []
    warnings: list = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            if progress:
                progress(i, len(pdf.pages))
            words = page.extract_words(extra_attrs=["size", "non_stroking_color", "upright"])
            ft_pt, bbox = _scale_from_pdf(reader, i)
            label = _label_for(words, float(page.width), float(page.height))
            sh = Sheet(page=i + 1, label=label, width=float(page.width),
                       height=float(page.height), ft_per_pt=ft_pt, plan_bbox=bbox)
            poles = _parse_poles(words)
            _snap_poles_to_symbols(poles, _pole_symbols(page))
            sh.callouts = poles + _footage(words) + _structures(words)
            if not words:
                sh.notes.append("no text layer (scanned page?)")
            sheets.append(sh)
    prev = 0
    for sh in sheets:
        m = _SHEET_RE.match(sh.label)
        if m:
            prev = int(m.group(1))
        elif prev and sh.callouts:
            prev += 1
            sh.label = f"PRINT-{prev:02d}"
            sh.notes.append("sheet number inferred from the previous page")
        else:
            sh.label = f"page {sh.page}"
    labels = [s.label for s in sheets if s.label.startswith("PRINT-")]
    for lab in sorted({l for l in labels if labels.count(l) > 1}):
        warnings.append(f"{lab} appears on more than one page")
    return PlanSet(name=name, sheets=sheets, warnings=warnings)


def georeference(plan: PlanSet, reject_ft: float = 150.0) -> PlanSet:
    for sh in plan.sheets:
        pairs = [c for c in sh.callouts if c.kind == "pole" and c.lat is not None]
        if pairs:
            _fit_translation(sh, pairs, reject_ft)
            for c in pairs:
                if sh.placed:
                    fx, fy = sh.to_lonlat(c.x, c.y)
                    err = math.hypot(*_meters_ft(c.lon - fx, c.lat - fy, c.lat))
                    if not c.flag and err > reject_ft:
                        c.flag = f"{err:,.0f} ft from where the sheet places it"
        else:
            sh.notes.append("no pole coordinates on this sheet - not placed")
    return plan


def place_callouts(plan: PlanSet) -> None:
    for sh in plan.placed():
        for c in sh.callouts:
            if c.lon is None:
                c.lon, c.lat = sh.to_lonlat(c.x, c.y)


# ---------------------------------------------------------------- outputs

def sheet_table(plan: PlanSet):
    import pandas as pd
    rows = []
    for s in plan.sheets:
        foot = [c for c in s.callouts if c.kind == "footage"]
        rows.append({
            "page": s.page, "sheet": s.label, "placed": s.placed,
            "pole anchors": s.n_anchors,
            "fit error (ft)": None if s.rms_ft is None else round(s.rms_ft, 1),
            "footage callouts": len(foot),
            "footage sum (ft)": round(sum(c.value_ft or 0 for c in foot), 1),
            "flags": sum(1 for c in s.callouts if c.flag),
            "notes": "; ".join(s.notes),
        })
    return pd.DataFrame(rows)


def callout_table(plan: PlanSet):
    import pandas as pd
    rows = []
    for s, c in plan.callouts():
        rows.append({"sheet": s.label, "page": s.page, "kind": c.kind, "text": c.text,
                     "feet": c.value_ft, "color": c.color, "latitude": c.lat,
                     "longitude": c.lon, "flag": c.flag})
    return pd.DataFrame(rows)


def _norm_name(t: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (t or "").upper())


def place_from_kml_names(plan: PlanSet, features, reject_ft: float = 150.0) -> int:
    """Place sheets that have no pole coordinates by matching handhole labels
    printed on the sheet (HH66, FP12 ...) to KML point names. Returns sheets placed."""
    points: dict = {}
    for f in features:
        for g in f.geometry.iter_simple():
            if g.geometry_type == "Point" and g.coordinates:
                key = _norm_name(f.name)
                if key:
                    points.setdefault(key, []).append((g.coordinates[0][0], g.coordinates[0][1]))
    placed = 0
    for sh in plan.sheets:
        if sh.placed:
            continue
        anchors = []
        for c in sh.callouts:
            if c.kind != "handhole":
                continue
            hits = points.get(_norm_name(c.text))
            if hits and len(hits) == 1:  # ambiguous names are not trusted
                a = Callout("pole", c.text, c.x, c.y, lon=hits[0][0], lat=hits[0][1])
                anchors.append((c, a))
        if len(anchors) < 2:
            continue
        _fit_translation(sh, [a for _, a in anchors], reject_ft)
        for c, a in anchors:
            if a.flag:
                c.flag = a.flag
        sh.notes.append(f"placed from {sh.n_anchors} KML handhole names")
        placed += 1
    return placed


def compare_with_features(plan: PlanSet, features):
    """Per sheet: footage callouts on the print vs KML line footage inside the sheet footprint."""
    import pandas as pd
    from shapely.geometry import LineString, Polygon

    from pyproj import Geod
    geod = Geod(ellps="WGS84")

    def _len_ft(geom) -> float:
        total = 0.0
        for g in getattr(geom, "geoms", [geom]):
            if g.geom_type == "LineString":
                xs, ys = zip(*[(c[0], c[1]) for c in g.coords])
                total += geod.line_length(list(xs), list(ys)) * 3.28084
        return total

    lines = []
    for f in features:
        for g in f.geometry.iter_simple():
            if g.geometry_type in ("LineString", "Track") and len(g.coordinates) >= 2:
                lines.append(LineString([(c[0], c[1]) for c in g.coordinates]))
    rows = []
    for s in plan.sheets:
        ring = s.footprint()
        callout_ft = s.footage_total()
        kml_ft, n = None, 0
        if ring:
            poly = Polygon(ring)
            kml_ft = 0.0
            for ln in lines:
                if not ln.intersects(poly):
                    continue
                inter = ln.intersection(poly)
                if not inter.is_empty:
                    n += 1
                    kml_ft += _len_ft(inter)
        rows.append({"sheet": s.label, "page": s.page,
                     "print callout sum (ft)": round(callout_ft, 1),
                     "KML line ft inside sheet": None if kml_ft is None else round(kml_ft),
                     "KML lines touching": n,
                     "difference (ft)": None if kml_ft is None else round(kml_ft - callout_ft)})
    return pd.DataFrame(rows)


def _esc(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_kml(plan: PlanSet, include_footage: bool = True) -> bytes:
    """Sheets folder (footprint polygons) and Callouts folder (pins) for Google Earth."""
    place_callouts(plan)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           f"<name>{_esc(plan.name)}</name>",
           '<Style id="sheet"><LineStyle><color>ff00a5ff</color><width>3</width></LineStyle>'
           '<PolyStyle><color>2200a5ff</color></PolyStyle></Style>',
           '<Style id="pole"><IconStyle><scale>0.8</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle></Style>',
           '<Style id="foot"><IconStyle><scale>0.6</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_square.png</href></Icon></IconStyle></Style>',
           "<Folder><name>Sheets</name>"]
    for s in plan.placed():
        ring = " ".join(f"{lo:.7f},{la:.7f},0" for lo, la in s.footprint())
        out.append(f"<Placemark><name>{_esc(s.label)}</name><styleUrl>#sheet</styleUrl>"
                   f"<description>Page {s.page}; {s.n_anchors} pole anchors; "
                   f"footage callouts sum {s.footage_total():,.0f} ft</description>"
                   f"<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>{ring}"
                   "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>")
    out.append("</Folder><Folder><name>Callouts</name>")
    for s, c in plan.callouts():
        if c.lon is None or (c.kind == "footage" and not include_footage):
            continue
        style = "pole" if c.kind == "pole" else "foot"
        out.append(f"<Placemark><name>{_esc(c.text)}</name><styleUrl>#{style}</styleUrl>"
                   f"<description>{_esc(s.label)} ({c.kind}){' - ' + _esc(c.flag) if c.flag else ''}"
                   f"</description><Point><coordinates>{c.lon:.7f},{c.lat:.7f},0</coordinates></Point></Placemark>")
    out.append("</Folder></Document></kml>")
    return "\n".join(out).encode("utf-8")

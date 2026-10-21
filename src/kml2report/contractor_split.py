"""Split a KML/KMZ build into two contractor territories with balanced work.

A straight cut line (any angle, any offset) divides the project into two
contiguous areas.  Every candidate cut is scored on how evenly it divides:

* trench / bore footage (and duct-feet = footage x ducts in the package)
* footage per duct size
* handholes
* other point assets (poles, cabinets, splice cases ...)

Lines that cross the cut are clipped exactly at the boundary (altitude is
interpolated), so each contractor receives precisely their own footage.  With
``clip=False`` whole features go to the side holding their centre instead.
Contractor KMLs contain only that contractor's features.
"""

from __future__ import annotations

import copy
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import pyproj

from kml2report import geoexport
from kml2report.gis import WGS84, centroid_lonlat, transformer, utm_epsg
from kml2report.kml_parser import _collection
from kml2report.models import M_TO_FT, Coord, Feature, Geometry

_GEOD = pyproj.Geod(ellps="WGS84")
LENGTH_KEYS = {"new_length", "length", "shape_length", "shape_len", "calc_length_ft"}  # compared lower-case

TOTAL = "Total footage (ft)"
DUCT = "Duct footage (ft x ducts)"
HANDHOLES = "Handholes"
DEFAULT_WEIGHTS = {"total": 4.0, "duct": 2.0, "handholes": 3.0, "per_size": 2.0, "other_assets": 1.0}

_DUCT_NAME_RE = re.compile(r"\d\s*(\"|in\b|inch)|duct|conduit|hdpe|pvc", re.I)  # a name only counts as a duct size if it says so
_HH_RE = re.compile(r"(?<![A-Z])HH\d*|HAND ?HOLE|PULL ?BOX")
_ASSET_WORDS = (("Pole", r"\bPOLE\b|^P-?\d+"), ("Cabinet", r"CABINET|\bCAB\b"), ("Splice Case", r"SPLICE"),
                ("Pedestal", r"PEDESTAL|\bPED\b|FLOWER ?POT"), ("Manhole", r"MANHOLE|\bMH\b|VAULT"),
                ("Riser", r"RISER"), ("Anchor", r"ANCHOR|\bGUY\b"))


# ---------------------------------------------------------------------------
# Classifying features
# ---------------------------------------------------------------------------

@dataclass
class FeatureInfo:
    duct_key: str = ""      # e.g. '2"' or '3" + 2"'
    duct_count: int = 1
    point_type: str = ""    # 'Handhole' / 'Pole' / ...
    is_handhole: bool = False


def _attrs(f: Feature) -> dict[str, str]:
    from kml2report.geometry_analyzer import parse_comma_separated_ducts, parse_description_html

    attrs = {**parse_description_html(f.raw_description), **f.parsed_attributes, **f.extended_data}
    if "Duct_Size" not in attrs:
        for key in ("Ducts", "Duct Size", "DuctSize", "duct_size", "Duct"):
            if attrs.get(key):
                sizes = parse_comma_separated_ducts(str(attrs[key]))
                if sizes:
                    attrs["Duct_Size"] = " + ".join(sorted(sizes, reverse=True))
                    attrs["Duct_Count"] = str(len(sizes))
                break
    return attrs


def duct_attrs(f: Feature) -> dict[str, str]:
    """Normalized duct package attributes (Duct_Size / Duct_Count / Duct_Package) when the feature has any."""
    a = _attrs(f)
    return {k: a[k] for k in ("Duct_Size", "Duct_Count", "Duct_Package") if a.get(k)}


def classify(f: Feature) -> FeatureInfo:
    a = _attrs(f)
    info = FeatureInfo()
    key = a.get("Duct_Size") or (f.name if _DUCT_NAME_RE.search(f.name or "") else "")
    info.duct_key = key.strip() or "Unspecified"
    try:
        info.duct_count = max(int(float(a.get("Duct_Count", 1))), 1)
    except ValueError:
        info.duct_count = 1
    raw_type = (a.get("Type") or a.get("Feature_Type") or a.get("Element_Type") or f.name or "Point").strip()
    probe = f"{raw_type} {f.name}".upper()
    if _HH_RE.search(probe):
        info.point_type, info.is_handhole = "Handhole", True
    else:
        info.point_type = next((n for n, rx in _ASSET_WORDS if re.search(rx, probe)),
                               re.sub(r"[\s\-#_]*\d+$", "", raw_type).strip().title() or "Point")
    return info


def metric_names(features: list[Feature]) -> list[str]:
    sizes, assets = [], []
    for f in features:
        g = f.geometry
        if not g or g.is_empty:
            continue
        info = classify(f)
        kinds = {p.geometry_type for p in g.iter_simple()}
        if kinds & {"LineString", "Track"} and f"Footage: {info.duct_key}" not in sizes:
            sizes.append(f"Footage: {info.duct_key}")
        if "Point" in kinds and not info.is_handhole and f"Count: {info.point_type}" not in assets:
            assets.append(f"Count: {info.point_type}")
    return [TOTAL, DUCT, HANDHOLES] + sorted(sizes) + sorted(assets)


def metrics_of(features: list[Feature], names: Optional[list[str]] = None) -> dict[str, float]:
    """Measured totals for a set of features (geodesic footage)."""
    out = {n: 0.0 for n in (names or metric_names(features))}
    for f in features:
        g = f.geometry
        if not g or g.is_empty:
            continue
        info = classify(f)
        ft = g.length_feet
        if ft:
            out[TOTAL] = out.get(TOTAL, 0.0) + ft
            out[DUCT] = out.get(DUCT, 0.0) + ft * info.duct_count
            out[f"Footage: {info.duct_key}"] = out.get(f"Footage: {info.duct_key}", 0.0) + ft
        for p in g.iter_simple():
            if p.geometry_type == "Point":
                k = HANDHOLES if info.is_handhole else f"Count: {info.point_type}"
                out[k] = out.get(k, 0.0) + 1
    return out


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@dataclass
class _Atoms:
    names: list[str]
    seg: np.ndarray      # (n_seg, 4) projected x0,y0,x1,y1
    w_seg: np.ndarray    # (n_seg, m)
    pt: np.ndarray       # (n_pt, 2)
    w_pt: np.ndarray     # (n_pt, m)
    cen: np.ndarray      # (n_feat, 2) feature centres
    w_cen: np.ndarray    # (n_feat, m)
    total: np.ndarray    # (m,)
    epsg: str = ""


def _prepare(features: list[Feature]) -> _Atoms:
    names = metric_names(features)
    col = {n: i for i, n in enumerate(names)}
    m = len(names)
    c = centroid_lonlat(features)
    if c is None:
        raise ValueError("No features with coordinates to split.")
    epsg = f"EPSG:{utm_epsg(*c)}"
    fwd = transformer(WGS84, epsg)

    seg, w_seg, pts, w_pt, cen, w_cen = [], [], [], [], [], []
    for f in features:
        g = f.geometry
        if not g or g.is_empty:
            continue
        info = classify(f)
        allc = [co for p in g.iter_simple() for co in p.coordinates]
        xs, ys = fwd.transform([co[0] for co in allc], [co[1] for co in allc])
        row_c = np.zeros(m)
        for p in g.iter_simple():
            if not p.coordinates:
                continue
            if p.geometry_type in ("LineString", "Track") and len(p.coordinates) >= 2:
                lon = np.array([co[0] for co in p.coordinates]); lat = np.array([co[1] for co in p.coordinates])
                x, y = fwd.transform(lon, lat)
                x, y = np.asarray(x), np.asarray(y)
                _, _, dist = _GEOD.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])
                ft = np.asarray(dist) * M_TO_FT
                w = np.zeros((len(ft), m))
                w[:, col[TOTAL]] = ft
                w[:, col[DUCT]] = ft * info.duct_count
                w[:, col[f"Footage: {info.duct_key}"]] = ft
                seg.append(np.column_stack([x[:-1], y[:-1], x[1:], y[1:]]))
                w_seg.append(w)
                row_c += w.sum(axis=0)
            elif p.geometry_type == "Point":
                x, y = fwd.transform(p.coordinates[0][0], p.coordinates[0][1])
                w = np.zeros(m)
                w[col[HANDHOLES] if info.is_handhole else col[f"Count: {info.point_type}"]] = 1.0
                pts.append((x, y)); w_pt.append(w)
                row_c += w
        cen.append((float(np.mean(xs)), float(np.mean(ys)))); w_cen.append(row_c)

    a = _Atoms(names,
               np.vstack(seg) if seg else np.zeros((0, 4)), np.vstack(w_seg) if w_seg else np.zeros((0, m)),
               np.array(pts).reshape(-1, 2), np.array(w_pt).reshape(-1, m),
               np.array(cen).reshape(-1, 2), np.array(w_cen).reshape(-1, m), np.zeros(m), epsg)
    a.total = a.w_seg.sum(axis=0) + a.w_pt.sum(axis=0)
    if a.total[0] <= 0 and a.total[col[HANDHOLES]] <= 0 and a.total.sum() <= 0:
        raise ValueError("Nothing to balance: no line footage or point assets found.")
    return a


def _weight_vector(names: list[str], w: dict) -> np.ndarray:
    """Per-metric weights.  Duct sizes share ``per_size`` and asset types share ``other_assets`` between them,
    so a file with many sizes / asset types can't drown out footage and handholes."""
    n_sizes = max(sum(n.startswith("Footage:") for n in names), 1)
    n_assets = max(sum(n.startswith("Count:") for n in names), 1)
    out = []
    for n in names:
        if n == TOTAL:
            out.append(w["total"])
        elif n == DUCT:
            out.append(w["duct"])
        elif n == HANDHOLES:
            out.append(w["handholes"])
        elif n.startswith("Footage:"):
            out.append(w["per_size"] / n_sizes)
        else:
            out.append(w["other_assets"] / n_assets)
    return np.array(out, dtype=float)


def _evaluate(a: _Atoms, theta: float, offsets: np.ndarray, clip: bool, share: float, wv: np.ndarray,
              cross_penalty: float = 0.02) -> np.ndarray:
    """Imbalance score for every offset along one cut direction (lower is better)."""
    nx, ny = math.cos(theta), math.sin(theta)
    K = len(offsets)
    m = len(a.names)
    A = np.zeros((K, m))
    cross_frac = np.zeros(K)
    if clip:
        d0 = a.seg[:, 0] * nx + a.seg[:, 1] * ny
        d1 = a.seg[:, 2] * nx + a.seg[:, 3] * ny
        chunk = max(1, int(3_000_000 // max(len(d0), 1)))
        for s in range(0, K, chunk):
            c = offsets[s:s + chunk][None, :]
            D0, D1 = d0[:, None] - c, d1[:, None] - c
            n0, n1 = D0 < 0, D1 < 0
            with np.errstate(divide="ignore", invalid="ignore"):
                cr = np.where(n0, -D0 / (D1 - D0), -D1 / (D0 - D1))
            frac = np.where(n0 & n1, 1.0, np.where(~n0 & ~n1, 0.0, cr))
            A[s:s + chunk] += frac.T @ a.w_seg
            cross_frac[s:s + chunk] = (n0 != n1).mean(axis=0) if len(d0) else 0.0
        if len(a.pt):
            dp = a.pt[:, 0] * nx + a.pt[:, 1] * ny
            A += (dp[None, :] - offsets[:, None] < 0).astype(float) @ a.w_pt
    else:
        dc = a.cen[:, 0] * nx + a.cen[:, 1] * ny
        A += (dc[None, :] - offsets[:, None] < 0).astype(float) @ a.w_cen
    T = a.total
    use = T > 0
    imb = np.abs(A[:, use] - share * T[use]) / T[use]
    return (imb * wv[use]).sum(axis=1) / wv[use].sum() + cross_penalty * cross_frac


@dataclass
class SplitCandidate:
    angle_deg: float
    offset_m: float
    score: float


def search_splits(features: list[Feature], share_a: float = 0.5, clip: bool = True, weights: Optional[dict] = None,
                  angle_step: float = 5.0, fixed_angle: Optional[float] = None, top: int = 5
                  ) -> tuple[list[SplitCandidate], _Atoms]:
    """Best cut for every direction, returned best-first (distinct directions only)."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    a = _prepare(features)
    wv = _weight_vector(a.names, w)
    if wv[a.total > 0].sum() <= 0:
        raise ValueError("Every balance weight that applies to this data is zero - raise at least one weight "
                         "(footage, duct-feet, handholes, duct sizes or other assets).")
    angles = [fixed_angle] if fixed_angle is not None else list(np.arange(0, 360, angle_step))
    xs = np.concatenate([a.seg[:, [0, 2]].ravel(), a.pt[:, 0], a.cen[:, 0]])
    ys = np.concatenate([a.seg[:, [1, 3]].ravel(), a.pt[:, 1], a.cen[:, 1]])
    per_angle: list[SplitCandidate] = []
    for ang in angles:
        th = math.radians(ang)
        proj = xs * math.cos(th) + ys * math.sin(th)
        # quantiles follow dense assets; the uniform samples cover the whole extent so sparse, long runs can't hide a cut
        offs = np.unique(np.concatenate([np.quantile(proj, np.linspace(0.02, 0.98, 241)),
                                         np.linspace(proj.min(), proj.max(), 241)[1:-1]]))
        sc = _evaluate(a, th, offs, clip, share_a, wv)
        k = int(np.argmin(sc))
        lo, hi = offs[max(k - 1, 0)], offs[min(k + 1, len(offs) - 1)]
        fine = np.linspace(lo, hi, 201)
        sf = _evaluate(a, th, fine, clip, share_a, wv)
        j = int(np.argmin(sf))
        per_angle.append(SplitCandidate(float(ang), float(fine[j]), float(sf[j])))
    per_angle.sort(key=lambda c: c.score)
    chosen: list[SplitCandidate] = []
    for c in per_angle:
        if all(min(abs(c.angle_deg - o.angle_deg) % 360, 360 - abs(c.angle_deg - o.angle_deg) % 360) >= 20
               for o in chosen):
            chosen.append(c)
        if len(chosen) >= top:
            break
    return chosen, a


# ---------------------------------------------------------------------------
# Applying a cut
# ---------------------------------------------------------------------------

def _lerp(p: Coord, q: Coord, t: float) -> Coord:
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t)


def _clip_polyline(coords: list[Coord], d: np.ndarray, xy: Optional[np.ndarray] = None, back=None
                   ) -> tuple[list[list[Coord]], list[list[Coord]]]:
    """Split a polyline at d=0.  Side A is d<0.  Altitude is interpolated at the cut.

    With ``xy`` (projected vertex coordinates) and ``back`` (projected -> lon/lat transformer) the crossing is
    found in the same projected plane the distances were measured in and then transformed back, so it lies on the
    boundary exactly; without them the crossing is interpolated in lon/lat."""
    runs_a: list[list[Coord]] = []
    runs_b: list[list[Coord]] = []

    def flush(run: list[Coord], is_a: bool) -> None:
        dedup = [p for i, p in enumerate(run) if i == 0 or p[:2] != run[i - 1][:2]]
        if len(dedup) >= 2:
            (runs_a if is_a else runs_b).append(dedup)

    cur = [coords[0]]
    for i in range(1, len(coords)):
        s0, s1 = d[i - 1] < 0, d[i] < 0
        if s0 != s1:
            t = float(d[i - 1] / (d[i - 1] - d[i]))
            p = _lerp(coords[i - 1], coords[i], t)
            if xy is not None and back is not None:
                x = xy[i - 1, 0] + t * (xy[i, 0] - xy[i - 1, 0])
                y = xy[i - 1, 1] + t * (xy[i, 1] - xy[i - 1, 1])
                lon, lat = back.transform(x, y)
                p = (float(lon), float(lat), p[2])
            cur.append(p)
            flush(cur, bool(s0))
            cur = [p]
        cur.append(coords[i])
    flush(cur, bool(d[-1] < 0))
    return runs_a, runs_b


def _compose(parts: list[Geometry]) -> Optional[Geometry]:
    parts = [p for p in parts if not p.is_empty]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else _collection(parts)


@dataclass
class SplitResult:
    a: list[Feature]
    b: list[Feature]
    angle_deg: float
    offset_m: float
    score: float
    cut_lonlat: list[tuple[float, float]]
    stats: pd.DataFrame
    clipped: bool
    names: tuple[str, str] = ("Contractor A", "Contractor B")
    warnings: list[str] = field(default_factory=list)


def validate_names(names: tuple[str, str]) -> tuple[str, str]:
    """Contractor names become table columns and file labels, so they must be distinct and not clash with the
    report's own fields."""
    a, b = (n.strip() for n in names)
    if not a or not b:
        raise ValueError("Give both contractors a name.")
    if a.casefold() == b.casefold():
        raise ValueError("The two contractors need different names.")
    reserved = {"metric", "difference"}
    for n in (a, b):
        if n.casefold() in reserved or n.casefold().endswith(" share %"):
            raise ValueError(f"'{n}' is a reserved report column name - choose another contractor name.")
    return a, b


def apply_split(features: list[Feature], cand: SplitCandidate, clip: bool = True,
                names: tuple[str, str] = ("Contractor A", "Contractor B")) -> SplitResult:
    """Divide ``features`` at the chosen cut into two sets of (new) Feature objects."""
    names = validate_names(names)
    c = centroid_lonlat(features)
    epsg = f"EPSG:{utm_epsg(*c)}"
    fwd, back = transformer(WGS84, epsg), transformer(epsg, WGS84)
    th = math.radians(cand.angle_deg)
    nx, ny = math.cos(th), math.sin(th)

    def project(coords: list[Coord]) -> np.ndarray:
        x, y = fwd.transform([p[0] for p in coords], [p[1] for p in coords])
        return np.column_stack([np.asarray(x), np.asarray(y)])

    def dist(coords: list[Coord]) -> np.ndarray:
        xy = project(coords)
        return xy[:, 0] * nx + xy[:, 1] * ny - cand.offset_m

    side_a: list[Feature] = []
    side_b: list[Feature] = []
    all_names = metric_names(features)

    def clone(f: Feature, geom: Geometry, contractor: str, cut: bool, original_ft: float) -> Feature:
        n = copy.copy(f)
        n.geometry = geom
        n.extended_data = dict(f.extended_data)
        n.parsed_attributes = dict(f.parsed_attributes)
        for key, val in duct_attrs(f).items():  # the package/count the split was scored on travels with the work
            for d in (n.extended_data, n.parsed_attributes):
                for k in [k for k in d if k.lower() == key.lower()]:
                    del d[k]
            n.extended_data[key] = val
        if cut:
            for d in (n.extended_data, n.parsed_attributes):  # stale: these describe the uncut line
                for k in [k for k in d if k.lower() in LENGTH_KEYS]:
                    del d[k]
            n.extended_data["Split_Note"] = f"Cut at contractor boundary (original length {original_ft:,.1f} ft)"
        n.extended_data["Contractor"] = contractor
        if geom.length_m:
            n.extended_data["Footage_ft"] = f"{geom.length_feet:.1f}"
        return n

    for f in features:
        g = f.geometry
        if not g or g.is_empty:
            continue
        parts_a: list[Geometry] = []
        parts_b: list[Geometry] = []
        feat_side_a = None
        if not clip or not any(p.geometry_type in ("LineString", "Track") for p in g.iter_simple()):
            allc = [co for p in g.iter_simple() for co in p.coordinates]
            feat_side_a = bool(dist([tuple(np.mean([co[i] for co in allc]) for i in range(3))])[0] < 0) \
                if not clip else None
        for p in g.iter_simple():
            if not p.coordinates:
                continue
            if clip and p.geometry_type in ("LineString", "Track") and len(p.coordinates) >= 2:
                xy = project(p.coordinates)
                ra, rb = _clip_polyline(p.coordinates, xy[:, 0] * nx + xy[:, 1] * ny - cand.offset_m, xy, back)
                parts_a += [Geometry("LineString", r) for r in ra]
                parts_b += [Geometry("LineString", r) for r in rb]
            else:
                if feat_side_a is None:  # a point or polygon part: its own centre decides
                    mid = tuple(float(np.mean([co[i] for co in p.coordinates])) for i in range(3))
                    to_a = bool(dist([mid])[0] < 0)
                else:
                    to_a = feat_side_a
                (parts_a if to_a else parts_b).append(p)
        ga, gb = _compose(parts_a), _compose(parts_b)
        cut = ga is not None and gb is not None and g.length_m > 0
        for geom, bucket, who in ((ga, side_a, names[0]), (gb, side_b, names[1])):
            if geom is not None:
                bucket.append(clone(f, geom, who, cut, g.length_feet))

    # cut line for display: through the data centre, spanning the data extent
    cx, cy = fwd.transform(c[0], c[1])
    along = cx * nx + cy * ny - cand.offset_m
    px, py = cx - along * nx, cy - along * ny
    bx = [co[0] for f in features if f.geometry for co in f.geometry.coordinates]
    by = [co[1] for f in features if f.geometry for co in f.geometry.coordinates]
    x0, y0 = fwd.transform(min(bx), min(by)); x1, y1 = fwd.transform(max(bx), max(by))
    half = math.hypot(x1 - x0, y1 - y0) / 2 * 1.05 or 100.0
    tx, ty = -ny, nx
    p1 = back.transform(px - half * tx, py - half * ty)
    p2 = back.transform(px + half * tx, py + half * ty)

    ma, mb = metrics_of(side_a, all_names), metrics_of(side_b, all_names)
    rows = []
    for n in all_names:
        tot = ma[n] + mb[n]
        rows.append({"metric": n, names[0]: round(ma[n], 1), names[1]: round(mb[n], 1),
                     "difference": round(ma[n] - mb[n], 1),
                     f"{names[0]} share %": round(100 * ma[n] / tot, 1) if tot else None})
    return SplitResult(side_a, side_b, cand.angle_deg, cand.offset_m, cand.score,
                       [(p1[0], p1[1]), (p2[0], p2[1])], pd.DataFrame(rows), clip, names)


def split_contractors(features: list[Feature], share_a: float = 0.5, clip: bool = True,
                      weights: Optional[dict] = None, fixed_angle: Optional[float] = None,
                      names: tuple[str, str] = ("Contractor A", "Contractor B")) -> SplitResult:
    """One call: find the most balanced straight cut and apply it."""
    names = validate_names(names)
    cands, _ = search_splits(features, share_a, clip, weights, fixed_angle=fixed_angle)
    return apply_split(features, cands[0], clip, names)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

COLORS = ("#e6194b", "#4363d8")


def contractor_kml(features: list[Feature], contractor: str, color: str, project: str = "") -> bytes:
    """Clean KML containing only this contractor's footage and assets."""
    title = f"{project} - {contractor}".strip(" -")
    return geoexport.to_kml_grouped_bytes([(contractor, features, color)], title, wrap_groups=False)


def overview_kml(res: SplitResult, project: str = "") -> bytes:
    cut = "".join(f"{lon},{lat},0 " for lon, lat in res.cut_lonlat)
    cut_xml = (f"<Folder><name>Split line</name><Placemark><name>Contractor boundary</name><Style><LineStyle>"
               f"<color>ff00ffff</color><width>4</width></LineStyle></Style><LineString><coordinates>{cut}"
               f"</coordinates></LineString></Placemark></Folder>")
    return geoexport.to_kml_grouped_bytes(
        [(res.names[0], res.a, COLORS[0]), (res.names[1], res.b, COLORS[1])],
        f"{project} - contractor split".strip(" -"), extra_xml=cut_xml)


def bundle_zip(res: SplitResult, project: str = "project") -> bytes:
    def safe(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "contractor"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (who, feats) in enumerate(((res.names[0], res.a), (res.names[1], res.b))):
            # the index keeps names unique even when two sanitize alike ("ACME A" / "ACME_A") or say "overview"
            z.writestr(f"{safe(project)}_contractor{i + 1}_{safe(who)}.kml", contractor_kml(feats, who, COLORS[i], project))
        z.writestr(f"{safe(project)}_overview.kml", overview_kml(res, project))
        z.writestr(f"{safe(project)}_split_summary.csv", res.stats.to_csv(index=False))
    return buf.getvalue()

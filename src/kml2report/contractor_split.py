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
import dataclasses
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
DIFFICULTY = "Difficulty-weighted footage"
DENSE = "Dense-area footage (ft)"
DEFAULT_WEIGHTS = {"total": 4.0, "duct": 2.0, "handholes": 3.0, "per_size": 2.0, "other_assets": 1.0,
                   "difficulty": 4.0, "dense": 3.0}

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
# Difficulty: not all feet are equal
# ---------------------------------------------------------------------------

@dataclass
class Difficulty:
    """How hard each stretch of the build is to construct.

    ``auto`` measures it from the file itself: dense areas (many turns, short blocks, many handholes/assets -
    i.e. a city) cost ``factor`` times an open straight run, scaling up smoothly from the file's typical density.
    ``hard_layers`` lets you name folders you already know are difficult (a city, a rock section, a river
    crossing); their footage is multiplied by ``hard_multiplier`` on top.
    """

    auto: bool = True
    factor: float = 2.0
    hard_layers: tuple = ()
    hard_multiplier: float = 2.0
    radius_ft: float = 1000.0
    # Density measured on the WHOLE project, set by plan_releases so a slice inside the city is still judged
    # against the project's typical density (otherwise a slice that is all city would look "normal").
    field: object = dataclasses.field(default=None, repr=False, compare=False)

    @property
    def enabled(self) -> bool:
        return self.auto or bool(self.hard_layers)


def layer_key(f: Feature) -> str:
    """The folder label the workbench shows for a feature."""
    return f.folder_path or f.layer or "(no folder)"


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    if not len(values):
        return 1.0
    order = np.argsort(values)
    cum = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cum, cum[-1] / 2)])


class _DensityField:
    """Local density of turns (line vertices, weight 1) and assets (points, weight 2): how many fall in the 3x3 grid
    cells around a spot.  ``ref`` is the length-weighted median over the pieces it was calibrated on, so densities
    are always compared with the project's own typical density."""

    def __init__(self, vert_xy: np.ndarray, pts_xy: np.ndarray, radius_ft: float, epsg: str):
        self.cell = radius_ft / M_TO_FT
        self.epsg = epsg
        self.off, self.big = 1 << 20, 1 << 22
        pts = np.vstack([vert_xy, pts_xy]) if len(pts_xy) else vert_xy
        wts = np.concatenate([np.ones(len(vert_xy)), np.full(len(pts_xy), 2.0)])
        cells = np.floor(pts / self.cell).astype(np.int64)
        self.uniq, inv = np.unique((cells[:, 0] + self.off) * self.big + (cells[:, 1] + self.off), return_inverse=True)
        self.sums = np.bincount(inv, weights=wts)
        self.ref = 1.0

    def density(self, mid: np.ndarray) -> np.ndarray:
        mc = np.floor(mid / self.cell).astype(np.int64)
        dens = np.zeros(len(mid))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = (mc[:, 0] + dx + self.off) * self.big + (mc[:, 1] + dy + self.off)
                idx = np.minimum(np.searchsorted(self.uniq, k), len(self.uniq) - 1)
                dens += np.where(self.uniq[idx] == k, self.sums[idx], 0.0)
        return dens

    def calibrate(self, mid: np.ndarray, ft: np.ndarray) -> "_DensityField":
        dens = self.density(mid)
        pos = dens > 0
        self.ref = _weighted_median(dens[pos], ft[pos]) if pos.any() else 1.0
        return self

    def profile(self, mid: np.ndarray, factor: float) -> tuple[np.ndarray, np.ndarray]:
        """(multiplier, dense flag): 1x up to the typical density, ``factor`` x at 4x typical; dense from 2x."""
        r = self.density(mid) / max(self.ref, 1e-9)
        return 1.0 + (factor - 1.0) * np.clip((r - 1.0) / 3.0, 0.0, 1.0), r >= 2.0


def build_density_field(features: list[Feature], radius_ft: float = 1000.0) -> _DensityField:
    """Density field of a whole project (used so every release is judged against the same yardstick)."""
    a = _prepare(features, Difficulty(auto=False, radius_ft=radius_ft))
    field = _DensityField(a.vert, a.pt, radius_ft, a.epsg)
    return field.calibrate((a.seg[:, :2] + a.seg[:, 2:]) / 2, a.w_seg[:, 0])


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@dataclass
class _Atoms:
    names: list[str]
    seg: np.ndarray      # (n_seg, 4) projected x0,y0,x1,y1  (long lines are cut into pieces <= the density radius)
    w_seg: np.ndarray    # (n_seg, m)
    pt: np.ndarray       # (n_pt, 2)
    w_pt: np.ndarray     # (n_pt, m)
    cen: np.ndarray      # (n_feat, 2) feature centres
    w_cen: np.ndarray    # (n_feat, m)
    total: np.ndarray    # (m,)
    epsg: str = ""
    vert: Optional[np.ndarray] = None     # (n_vert, 2) projected line vertices
    seg_ll: Optional[np.ndarray] = None   # (n_seg, 4) lon0,lat0,lon1,lat1 (for display)
    mult: Optional[np.ndarray] = None     # (n_seg,) difficulty multiplier
    dense: Optional[np.ndarray] = None    # (n_seg,) bool


def _prepare(features: list[Feature], difficulty: Optional[Difficulty] = None) -> _Atoms:
    difficulty = difficulty or Difficulty(auto=False)
    names = metric_names(features) + ([DIFFICULTY, DENSE] if difficulty.enabled else [])
    col = {n: i for i, n in enumerate(names)}
    m = len(names)
    c = centroid_lonlat(features)
    if c is None:
        raise ValueError("No features with coordinates to split.")
    epsg = f"EPSG:{utm_epsg(*c)}"
    fwd = transformer(WGS84, epsg)
    piece_m = difficulty.radius_ft / M_TO_FT
    hard = set(difficulty.hard_layers)

    seg, seg_ll, w_seg, seg_feat, seg_hard = [], [], [], [], []
    pts, w_pt, cen, w_cen, vert = [], [], [], [], []
    for f in features:
        g = f.geometry
        if not g or g.is_empty:
            continue
        info = classify(f)
        feat_idx = len(cen)
        is_hard = layer_key(f) in hard
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
                vert.append(np.column_stack([x, y]))
                # cut long segments into pieces so density is measured along them, not just at the middle
                length_m = np.hypot(np.diff(x), np.diff(y))
                nsub = np.clip(np.ceil(length_m / piece_m), 1, 200).astype(int)
                src = np.repeat(np.arange(len(ft)), nsub)
                k = np.arange(len(src)) - np.repeat(np.cumsum(nsub) - nsub, nsub)
                t0, t1 = (k / nsub[src])[:, None], ((k + 1) / nsub[src])[:, None]
                p0 = np.column_stack([x[:-1], y[:-1]])[src]; p1 = np.column_stack([x[1:], y[1:]])[src]
                l0 = np.column_stack([lon[:-1], lat[:-1]])[src]; l1 = np.column_stack([lon[1:], lat[1:]])[src]
                pft = ft[src] / nsub[src]
                w = np.zeros((len(pft), m))
                w[:, col[TOTAL]] = pft
                w[:, col[DUCT]] = pft * info.duct_count
                w[:, col[f"Footage: {info.duct_key}"]] = pft
                seg.append(np.hstack([p0 + (p1 - p0) * t0, p0 + (p1 - p0) * t1]))
                seg_ll.append(np.hstack([l0 + (l1 - l0) * t0, l0 + (l1 - l0) * t1]))
                w_seg.append(w)
                seg_feat.append(np.full(len(pft), feat_idx)); seg_hard.append(np.full(len(pft), is_hard))
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
    a.seg_ll = np.vstack(seg_ll) if seg_ll else np.zeros((0, 4))
    a.vert = np.vstack(vert) if vert else np.zeros((0, 2))
    if difficulty.enabled and len(a.seg):
        ft = a.w_seg[:, col[TOTAL]]
        mid = (a.seg[:, :2] + a.seg[:, 2:]) / 2
        if difficulty.auto:
            field = difficulty.field
            if field is None:  # a stand-alone split measures density on its own data
                field = _DensityField(a.vert, a.pt, difficulty.radius_ft, epsg).calibrate(mid, ft)
            elif field.epsg != epsg:  # the whole project may sit in another UTM zone than this slice
                to_field = transformer(epsg, field.epsg)
                mx, my = to_field.transform(mid[:, 0], mid[:, 1])
                mid = np.column_stack([np.asarray(mx), np.asarray(my)])
            mult, dense = field.profile(mid, difficulty.factor)
        else:
            mult, dense = np.ones(len(ft)), np.zeros(len(ft), dtype=bool)
        is_hard = np.concatenate(seg_hard)
        mult = mult * np.where(is_hard, difficulty.hard_multiplier, 1.0)
        dense = dense | is_hard
        a.w_seg[:, col[DIFFICULTY]] = ft * mult
        a.w_seg[:, col[DENSE]] = ft * dense
        extra = [col[DIFFICULTY], col[DENSE]]
        sf = np.concatenate(seg_feat)
        for cidx in extra:
            a.w_cen[:, cidx] = np.bincount(sf, weights=a.w_seg[:, cidx], minlength=len(a.w_cen))
        a.mult, a.dense = mult, dense
    a.total = a.w_seg.sum(axis=0) + a.w_pt.sum(axis=0)
    if a.total.sum() <= 0:
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
        elif n == DIFFICULTY:
            out.append(w["difficulty"])
        elif n == DENSE:
            out.append(w["dense"])
        elif n.startswith("Footage:"):
            out.append(w["per_size"] / n_sizes)
        else:
            out.append(w["other_assets"] / n_assets)
    return np.array(out, dtype=float)


def _cum(order: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Prefix sums of ``w`` in the given order, with a leading zero row."""
    return np.vstack([np.zeros((1, w.shape[1])), np.cumsum(w[order], axis=0)])


def _totals(a: _Atoms, theta: float, offsets: np.ndarray, clip: bool) -> tuple[np.ndarray, np.ndarray]:
    """Side-A totals for every metric at every offset (K x m), plus the fraction of pieces the cut crosses.

    Side A is everything with signed distance < offset along the cut normal.  A piece spanning [lo, hi] on that
    axis contributes the fraction (c - lo) / (hi - lo) of its work, so the totals are piecewise-linear in the
    offset and come from sorted prefix sums in O(n log n + K log n) - exact, and fast enough for large projects.
    """
    nx, ny = math.cos(theta), math.sin(theta)
    K, m = len(offsets), len(a.names)
    A = np.zeros((K, m))
    cross = np.zeros(K)
    ref = float(np.mean(a.cen[:, 0] * nx + a.cen[:, 1] * ny)) if len(a.cen) else 0.0  # keeps magnitudes small
    c = np.asarray(offsets, dtype=float) - ref
    if clip:
        if len(a.seg):
            d0 = a.seg[:, 0] * nx + a.seg[:, 1] * ny - ref
            d1 = a.seg[:, 2] * nx + a.seg[:, 3] * ny - ref
            lo, hi = np.minimum(d0, d1), np.maximum(d0, d1)
            ohi = np.argsort(hi)
            A += _cum(ohi, a.w_seg)[np.searchsorted(hi[ohi], c, side="left")]          # wholly on side A
            ok = (hi - lo) > 1e-6
            if ok.any():                                                                  # partly on side A
                lo_k, hi_k = lo[ok], hi[ok]
                u = a.w_seg[ok] / (hi_k - lo_k)[:, None]
                v = u * lo_k[:, None]
                olo, ohk = np.argsort(lo_k), np.argsort(hi_k)
                il = np.searchsorted(lo_k[olo], c, side="left")
                ih = np.searchsorted(hi_k[ohk], c, side="left")
                s1 = _cum(olo, u)[il] - _cum(ohk, u)[ih]
                s2 = _cum(olo, v)[il] - _cum(ohk, v)[ih]
                A += c[:, None] * s1 - s2
                cross = (il - ih) / len(a.seg)
        if len(a.pt):
            dp = a.pt[:, 0] * nx + a.pt[:, 1] * ny - ref
            o = np.argsort(dp)
            A += _cum(o, a.w_pt)[np.searchsorted(dp[o], c, side="left")]
    elif len(a.cen):
        dc = a.cen[:, 0] * nx + a.cen[:, 1] * ny - ref
        o = np.argsort(dc)
        A += _cum(o, a.w_cen)[np.searchsorted(dc[o], c, side="left")]
    return A, cross


def _evaluate(a: _Atoms, theta: float, offsets: np.ndarray, clip: bool, share: float, wv: np.ndarray,
              cross_penalty: float = 0.02) -> np.ndarray:
    """Imbalance score for every offset along one cut direction (lower is better)."""
    A, cross_frac = _totals(a, theta, offsets, clip)
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
                  angle_step: float = 5.0, fixed_angle: Optional[float] = None, top: int = 5,
                  difficulty: Optional[Difficulty] = None) -> tuple[list[SplitCandidate], _Atoms]:
    """Best cut for every direction, returned best-first (distinct directions only)."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    a = _prepare(features, difficulty if difficulty is not None else Difficulty())
    wv = _weight_vector(a.names, w)
    if wv[a.total > 0].sum() <= 0:
        raise ValueError("Every balance weight that applies to this data is zero - raise at least one weight "
                         "(footage, duct-feet, handholes, duct sizes or other assets).")
    angles = [fixed_angle] if fixed_angle is not None else list(np.arange(0, 360, angle_step))
    xs = np.concatenate([a.seg[:, [0, 2]].ravel(), a.pt[:, 0], a.cen[:, 0]])
    ys = np.concatenate([a.seg[:, [1, 3]].ravel(), a.pt[:, 1], a.cen[:, 1]])
    def best_for(ang: float) -> SplitCandidate:
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
        return SplitCandidate(float(ang) % 360, float(fine[j]), float(sf[j]))

    per_angle = sorted((best_for(ang) for ang in angles), key=lambda c: c.score)
    chosen: list[SplitCandidate] = []
    for c in per_angle:
        if all(min(abs(c.angle_deg - o.angle_deg) % 360, 360 - abs(c.angle_deg - o.angle_deg) % 360) >= 20
               for o in chosen):
            chosen.append(c)
        if len(chosen) >= top:
            break
    if fixed_angle is None:
        # Coarse steps can miss a cut that runs nearly parallel to long lines: a 1 degree tilt moves the cut
        # hundreds of metres along a 30 km run.  Refine each leading direction in tenth-of-a-step increments.
        refined = []
        for c in chosen:
            trials = [best_for(c.angle_deg + d) for d in np.arange(-angle_step, angle_step + 1e-9, angle_step / 10)]
            refined.append(min(trials + [c], key=lambda t: t.score))
        chosen = sorted(refined, key=lambda c: c.score)
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
    reserved = {"metric", "difference", "release"}
    for n in (a, b):
        if n.casefold() in reserved or n.casefold().endswith(" share %"):
            raise ValueError(f"'{n}' is a reserved report column name - choose another contractor name.")
    return a, b


@dataclass
class _CutContext:
    fwd: object
    back: object
    centre: tuple
    cut_lonlat: list


def cut_features(features: list[Feature], angle_deg: float, offset_m: float, clip: bool = True,
                 labels: Optional[tuple[str, str]] = None, label_key: str = "Contractor",
                 note: str = "Cut at contractor boundary") -> tuple[list[Feature], list[Feature], _CutContext]:
    """Divide ``features`` along one straight cut: side A is everything with signed distance < offset along the
    normal at ``angle_deg`` (grid east = 0, grid north = 90).  Lines that cross are cut exactly there (altitude
    interpolated; the cut pieces drop their now-stale length attributes and carry ``note``).  Returns new Feature
    objects, so the inputs are untouched."""
    c = centroid_lonlat(features)
    epsg = f"EPSG:{utm_epsg(*c)}"
    fwd, back = transformer(WGS84, epsg), transformer(epsg, WGS84)
    th = math.radians(angle_deg)
    nx, ny = math.cos(th), math.sin(th)

    def project(coords: list[Coord]) -> np.ndarray:
        x, y = fwd.transform([p[0] for p in coords], [p[1] for p in coords])
        return np.column_stack([np.asarray(x), np.asarray(y)])

    def dist(coords: list[Coord]) -> np.ndarray:
        xy = project(coords)
        return xy[:, 0] * nx + xy[:, 1] * ny - offset_m

    side_a: list[Feature] = []
    side_b: list[Feature] = []

    def clone(f: Feature, geom: Geometry, label: Optional[str], cut: bool, original_ft: float) -> Feature:
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
            n.extended_data["Split_Note"] = f"{note} (original length {original_ft:,.1f} ft)"
        if label is not None:
            n.extended_data[label_key] = label
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
                ra, rb = _clip_polyline(p.coordinates, xy[:, 0] * nx + xy[:, 1] * ny - offset_m, xy, back)
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
        for geom, bucket, who in ((ga, side_a, labels[0] if labels else None),
                                  (gb, side_b, labels[1] if labels else None)):
            if geom is not None:
                bucket.append(clone(f, geom, who, cut, g.length_feet))

    # cut line for display: through the data centre, spanning the data extent
    cx, cy = fwd.transform(c[0], c[1])
    along = cx * nx + cy * ny - offset_m
    px, py = cx - along * nx, cy - along * ny
    bx = [co[0] for f in features if f.geometry for co in f.geometry.coordinates]
    by = [co[1] for f in features if f.geometry for co in f.geometry.coordinates]
    x0, y0 = fwd.transform(min(bx), min(by)); x1, y1 = fwd.transform(max(bx), max(by))
    half = math.hypot(x1 - x0, y1 - y0) / 2 * 1.05 or 100.0
    tx, ty = -ny, nx
    p1 = back.transform(px - half * tx, py - half * ty)
    p2 = back.transform(px + half * tx, py + half * ty)
    return side_a, side_b, _CutContext(fwd, back, c, [(p1[0], p1[1]), (p2[0], p2[1])])


def apply_split(features: list[Feature], cand: SplitCandidate, clip: bool = True,
                names: tuple[str, str] = ("Contractor A", "Contractor B"),
                difficulty: Optional[Difficulty] = None) -> SplitResult:
    """Divide ``features`` at the chosen cut into two sets of (new) Feature objects."""
    names = validate_names(names)
    th = math.radians(cand.angle_deg)
    all_names = metric_names(features)
    side_a, side_b, ctx = cut_features(features, cand.angle_deg, cand.offset_m, clip, names)
    p1, p2 = ctx.cut_lonlat

    ma, mb = metrics_of(side_a, all_names), metrics_of(side_b, all_names)
    rows = []
    for n in all_names:
        tot = ma[n] + mb[n]
        rows.append({"metric": n, names[0]: round(ma[n], 1), names[1]: round(mb[n], 1),
                     "difference": round(ma[n] - mb[n], 1),
                     f"{names[0]} share %": round(100 * ma[n] / tot, 1) if tot else None})
    difficulty = difficulty if difficulty is not None else Difficulty()
    if difficulty.enabled:  # same model the search scored, evaluated exactly at the chosen cut
        atoms = _prepare(features, difficulty)
        side, _ = _totals(atoms, th, np.array([cand.offset_m]), clip)
        for n in (DIFFICULTY, DENSE):
            i = atoms.names.index(n)
            va, vb = float(side[0, i]), float(atoms.total[i] - side[0, i])
            rows.append({"metric": n, names[0]: round(va, 1), names[1]: round(vb, 1),
                         "difference": round(va - vb, 1),
                         f"{names[0]} share %": round(100 * va / (va + vb), 1) if va + vb else None})
    return SplitResult(side_a, side_b, cand.angle_deg, cand.offset_m, cand.score,
                       [p1, p2], pd.DataFrame(rows), clip, names)


def split_contractors(features: list[Feature], share_a: float = 0.5, clip: bool = True,
                      weights: Optional[dict] = None, fixed_angle: Optional[float] = None,
                      names: tuple[str, str] = ("Contractor A", "Contractor B"),
                      difficulty: Optional[Difficulty] = None) -> SplitResult:
    """One call: find the most balanced straight cut and apply it."""
    names = validate_names(names)
    difficulty = difficulty if difficulty is not None else Difficulty()
    cands, _ = search_splits(features, share_a, clip, weights, fixed_angle=fixed_angle, difficulty=difficulty)
    return apply_split(features, cands[0], clip, names, difficulty)


def difficulty_segments(features: list[Feature], difficulty: Difficulty, limit: int = 20000) -> list[dict]:
    """Line pieces with their difficulty multiplier, for drawing what the splitter treats as hard work."""
    a = _prepare(features, difficulty)
    if a.mult is None or not len(a.seg_ll):
        return []
    idx = np.arange(len(a.mult))
    if len(idx) > limit:
        idx = np.linspace(0, len(idx) - 1, limit).astype(int)
    return [{"path": [[float(r[0]), float(r[1])], [float(r[2]), float(r[3])]], "mult": float(a.mult[i]),
             "dense": bool(a.dense[i])} for i, r in zip(idx, a.seg_ll[idx])]


# ---------------------------------------------------------------------------
# Releases: hand out the work a few thousand feet at a time
# ---------------------------------------------------------------------------

START_ANGLES = {"west": 0.0, "south": 90.0, "east": 180.0, "north": 270.0}


@dataclass
class Release:
    number: int
    a: list[Feature]
    b: list[Feature]
    split: SplitResult          # how this release was balanced between the two contractors
    final: bool = False         # the last release takes whatever is left, so it may be smaller


@dataclass
class ReleasePlan:
    releases: list[Release]
    remaining: list[Feature]    # work not yet handed out
    per_contractor_ft: float
    start: str
    names: tuple
    overview: pd.DataFrame      # one row per release
    detail: pd.DataFrame        # every metric, every release
    warnings: list[str] = field(default_factory=list)


def _start_angle(features: list[Feature], start: str, start_point: Optional[tuple]) -> float:
    """Direction the work is handed out in (the normal of the frontier cut).  'auto' sweeps along the longer
    side of what is left, starting from its west / south end."""
    start = (start or "auto").lower()
    if start in START_ANGLES:
        return START_ANGLES[start]
    a = _prepare(features, Difficulty(auto=False))
    c = centroid_lonlat(features)
    if start == "point" and start_point is not None:
        fwd = transformer(WGS84, a.epsg)
        px, py = fwd.transform(start_point[0], start_point[1])
        cx, cy = fwd.transform(c[0], c[1])
        if math.hypot(cx - px, cy - py) > 1.0:
            return math.degrees(math.atan2(cy - py, cx - px)) % 360
    xs = np.concatenate([a.seg[:, [0, 2]].ravel(), a.pt[:, 0]]) if len(a.seg) or len(a.pt) else a.cen[:, 0]
    ys = np.concatenate([a.seg[:, [1, 3]].ravel(), a.pt[:, 1]]) if len(a.seg) or len(a.pt) else a.cen[:, 1]
    return 0.0 if (xs.max() - xs.min()) >= (ys.max() - ys.min()) else 90.0


def _frontier_offset(features: list[Feature], angle_deg: float, target_ft: float) -> Optional[float]:
    """Offset of the straight cut that leaves exactly ``target_ft`` of line footage on side A (None = take all)."""
    a = _prepare(features, Difficulty(auto=False))
    total = float(a.total[0])
    if total <= target_ft * 1.0001:
        return None
    th = math.radians(angle_deg)
    nx, ny = math.cos(th), math.sin(th)
    proj = np.concatenate([a.seg[:, [0, 2]].ravel() * nx + a.seg[:, [1, 3]].ravel() * ny]) if len(a.seg) else np.zeros(1)
    lo, hi = float(proj.min()), float(proj.max())
    for _ in range(60):  # footage on side A only grows with the offset, so bisection is exact enough
        mid = (lo + hi) / 2
        got = float(_totals(a, th, np.array([mid]), True)[0][0, 0])
        lo, hi = (mid, hi) if got < target_ft else (lo, mid)
    return (lo + hi) / 2


def plan_releases(features: list[Feature], per_contractor_ft: float = 5000.0, start: str = "auto",
                  start_point: Optional[tuple] = None, max_releases: Optional[int] = None, clip: bool = True,
                  weights: Optional[dict] = None, fixed_angle: Optional[float] = None,
                  names: tuple[str, str] = ("Contractor A", "Contractor B"),
                  difficulty: Optional[Difficulty] = None) -> ReleasePlan:
    """Hand the work out ``per_contractor_ft`` at a time instead of all at once.

    Each release first takes the next 2 x ``per_contractor_ft`` of line footage from the frontier of the
    work that is still unassigned (swept from the ``start`` end: auto / west / east / south / north / point), cutting
    a line exactly where the footage runs out.  That slice is then divided between the two contractors with the same
    automatic balancing as the full split, so each gets the requested footage with a fair share of the hard work,
    handholes and duct sizes.  The next release starts where the last stopped.  The last release takes what is left.
    The result is deterministic: asking for more releases never changes the earlier ones.
    """
    names = validate_names(names)
    if per_contractor_ft <= 0:
        raise ValueError("The footage per contractor per release must be above zero.")
    difficulty = difficulty if difficulty is not None else Difficulty()
    remaining = [f for f in features if f.geometry and not f.geometry.is_empty]
    if not remaining:
        raise ValueError("No features with coordinates to hand out.")
    if difficulty.auto and difficulty.field is None:
        difficulty = dataclasses.replace(difficulty, field=build_density_field(remaining, difficulty.radius_ft))
    releases: list[Release] = []
    warnings: list[str] = []
    limit = max_releases if max_releases is not None else 500
    while remaining and len(releases) < limit:
        number = len(releases) + 1
        theta = _start_angle(remaining, start, start_point)
        offset = _frontier_offset(remaining, theta, 2 * per_contractor_ft)
        if offset is None:
            chunk, rest, final = remaining, [], True
        else:
            chunk, rest, _ = cut_features(remaining, theta, offset, clip=True, labels=None,
                                          note="Cut at release boundary")
            final = False
            remaining_ft = sum(f.geometry.length_feet for f in remaining if f.geometry)
            rest_ft = sum(f.geometry.length_feet for f in rest if f.geometry)
            if not chunk or rest_ft >= remaining_ft:
                warnings.append(f"Release {number}: could not find a boundary - stopped.")
                break
        try:
            res = split_contractors(chunk, 0.5, clip, weights, fixed_angle, names, difficulty)
        except ValueError as exc:
            warnings.append(f"Release {number}: {exc} The remaining features carry no footage or assets to balance.")
            break
        for f in res.a + res.b:
            f.extended_data["Release"] = str(number)
        releases.append(Release(number, res.a, res.b, res, final))
        remaining = rest
    rows, long = [], []
    cum_a = cum_b = 0.0
    total_left = sum(f.geometry.length_feet for f in remaining)
    stats0 = {r.number: r.split.stats.set_index("metric") for r in releases}
    left = sum(f.geometry.length_feet for r in releases for f in r.a + r.b) + total_left
    assigned = 0.0
    for r in releases:
        st = stats0[r.number]
        fa, fb = float(st.loc[TOTAL, names[0]]), float(st.loc[TOTAL, names[1]])
        cum_a, cum_b = cum_a + fa, cum_b + fb
        assigned += fa + fb
        row = {"release": r.number, f"{names[0]} ft": round(fa), f"{names[1]} ft": round(fb)}
        for label, metric in (("difficulty-weighted ft", DIFFICULTY), ("dense-area ft", DENSE), ("handholes", HANDHOLES)):
            if metric in st.index:
                row[f"{names[0]} {label}"] = round(float(st.loc[metric, names[0]]))
                row[f"{names[1]} {label}"] = round(float(st.loc[metric, names[1]]))
        row[f"{names[0]} total so far ft"], row[f"{names[1]} total so far ft"] = round(cum_a), round(cum_b)
        row["not yet released ft"] = round(max(left - assigned, 0.0))
        rows.append(row)
        long.append(st.reset_index().assign(release=r.number))
    detail = pd.concat(long, ignore_index=True)[["release", "metric", names[0], names[1], "difference"]] if long \
        else pd.DataFrame()
    return ReleasePlan(releases, remaining, per_contractor_ft, start, names, pd.DataFrame(rows), detail, warnings)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

COLORS = ("#e6194b", "#4363d8")


MIME = {".kml": "application/vnd.google-earth.kml+xml", ".kmz": "application/vnd.google-earth.kmz"}


def contractor_file(features: list[Feature], contractor: str, project: str = "",
                    color: Optional[str] = None) -> tuple[bytes, str]:
    """One contractor's work as a finished file: ``(bytes, ".kml" | ".kmz")``.

    Every feature keeps the icon, scale, label and folder structure it came with; ``color`` is only for the case
    where you explicitly want to recolour.  A KMZ is written (with the icon images inside) when the original
    styles use images that were packed in a KMZ."""
    title = f"{project} - {contractor}".strip(" -")
    return geoexport.to_kml_package([(contractor, features, color)], title, wrap_groups=False)


def overview_file(res: SplitResult, project: str = "", recolor: bool = False) -> tuple[bytes, str]:
    """Both contractors in one file (a folder each) plus the boundary line.  Original styling is kept unless
    ``recolor`` asks for contractor colours."""
    cut = "".join(f"{lon},{lat},0 " for lon, lat in res.cut_lonlat)
    cut_xml = (f"<Folder><name>Split line</name><Placemark><name>Contractor boundary</name><Style><LineStyle>"
               f"<color>ff00ffff</color><width>4</width></LineStyle></Style><LineString><coordinates>{cut}"
               f"</coordinates></LineString></Placemark></Folder>")
    return geoexport.to_kml_package(
        [(res.names[0], res.a, COLORS[0] if recolor else None), (res.names[1], res.b, COLORS[1] if recolor else None)],
        f"{project} - contractor split".strip(" -"), extra_xml=cut_xml)


def release_file(plan: ReleasePlan, number: int, who: int, project: str = "") -> tuple[bytes, str]:
    """One contractor's work for one release."""
    rel = next(r for r in plan.releases if r.number == number)
    feats = rel.a if who == 0 else rel.b
    title = f"{project} - Release {number} - {plan.names[who]}".strip(" -")
    return geoexport.to_kml_package([(f"Release {number}", feats, None)], title, wrap_groups=False)


def contractor_all_releases_file(plan: ReleasePlan, who: int, project: str = "") -> tuple[bytes, str]:
    """Everything one contractor has been handed so far, a folder per release."""
    groups = [(f"Release {r.number}", r.a if who == 0 else r.b, None) for r in plan.releases]
    return geoexport.to_kml_package(groups, f"{project} - {plan.names[who]} - all releases".strip(" -"))


def unassigned_file(plan: ReleasePlan, project: str = "") -> tuple[bytes, str]:
    """The work not yet handed out."""
    return geoexport.to_kml_package([("Not yet released", plan.remaining, None)],
                                    f"{project} - not yet released".strip(" -"), wrap_groups=False)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "contractor"


def bundle_zip(res: SplitResult, project: str = "project", recolor_overview: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (who, feats) in enumerate(((res.names[0], res.a), (res.names[1], res.b))):
            data, ext = contractor_file(feats, who, project)
            # the index keeps names unique even when two sanitize alike ("ACME A" / "ACME_A") or say "overview"
            z.writestr(f"{_safe(project)}_contractor{i + 1}_{_safe(who)}{ext}", data)
        data, ext = overview_file(res, project, recolor_overview)
        z.writestr(f"{_safe(project)}_overview{ext}", data)
        z.writestr(f"{_safe(project)}_split_summary.csv", res.stats.to_csv(index=False))
    return buf.getvalue()


def releases_zip(plan: ReleasePlan, project: str = "project") -> bytes:
    """Every release for both contractors, each contractor's running total, the unreleased work and a summary."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        base = _safe(project)
        for r in plan.releases:
            for who in (0, 1):
                data, ext = release_file(plan, r.number, who, project)
                z.writestr(f"{base}_release{r.number:02d}_contractor{who + 1}_{_safe(plan.names[who])}{ext}", data)
        for who in (0, 1):
            data, ext = contractor_all_releases_file(plan, who, project)
            z.writestr(f"{base}_contractor{who + 1}_{_safe(plan.names[who])}_all_releases{ext}", data)
        if plan.remaining:
            data, ext = unassigned_file(plan, project)
            z.writestr(f"{base}_not_yet_released{ext}", data)
        z.writestr(f"{base}_release_summary.csv", plan.overview.to_csv(index=False))
        z.writestr(f"{base}_release_detail.csv", plan.detail.to_csv(index=False))
    return buf.getvalue()

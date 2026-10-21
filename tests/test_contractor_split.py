"""Contractor split: balance, exact clipping, conservation of work, clean per-contractor KML."""

import io
import random
import zipfile

import pytest

from kml2report import contractor_split as cs
from kml2report.kml_parser import parse_bytes


def _network(seed=3):
    random.seed(seed)
    items = []
    for i in range(12):
        lat = 29.0 + i * 0.002
        size = random.choice(['2"', '3"', '4"'])
        items.append(f'<Placemark><name>Run H{i}</name><description><![CDATA[<table><tr><td>Duct_Size</td><td>{size}'
                     f'</td></tr><tr><td>New_Length</td><td>9999</td></tr></table>]]></description><LineString><coordinates>'
                     f'-96.00,{lat},5 -95.97,{lat},9</coordinates></LineString></Placemark>')
    for j in range(10):
        lon = -96.0 + j * 0.003
        items.append(f'<Placemark><name>Run V{j}</name><description><![CDATA[<table><tr><td>Duct_Size</td><td>2"</td>'
                     f'</tr><tr><td>Duct_Size</td><td>2"</td></tr></table>]]></description><LineString><coordinates>'
                     f'{lon},29.0,0 {lon},29.022,0</coordinates></LineString></Placemark>')
    for k in range(30):
        items.append(f'<Placemark><name>HH-{k}</name><Point><coordinates>{-96 + random.random() * 0.03},'
                     f'{29 + random.random() * 0.022}</coordinates></Point></Placemark>')
    for k in range(8):
        items.append(f'<Placemark><name>Pole {k}</name><Point><coordinates>{-96 + random.random() * 0.03},'
                     f'{29 + random.random() * 0.022}</coordinates></Point></Placemark>')
    kml = ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + "".join(items) + "</Document></kml>").encode()
    return kml


def _features(seed=3):
    return parse_bytes(_network(seed), "n.kml").features


@pytest.fixture(scope="module")
def feats():
    return _features()


@pytest.fixture(scope="module")
def res(feats):
    return cs.split_contractors(feats)


def row(res, metric):
    r = res.stats[res.stats["metric"] == metric].iloc[0]
    return r[res.names[0]], r[res.names[1]]


def test_footage_is_balanced_and_nothing_is_lost(feats, res):
    a, b = row(res, cs.TOTAL)
    assert abs(a - b) / (a + b) < 0.01
    assert a + b == pytest.approx(cs.metrics_of(feats)[cs.TOTAL], rel=1e-4)


def test_handholes_and_assets_balanced(res):
    a, b = row(res, cs.HANDHOLES)
    assert abs(a - b) <= 1 and a + b == 30
    pa, pb = row(res, "Count: Pole")
    assert abs(pa - pb) <= 1 and pa + pb == 8


def test_duct_feet_and_sizes_close(res):
    a, b = row(res, cs.DUCT)
    assert abs(a - b) / (a + b) < 0.05


def test_clipping_flags_cut_lines_and_drops_stale_lengths():
    from kml2report.description_parser import parse_description

    fs = _features()
    for f in fs:  # as the app does: description attributes are parsed (and lower-cased) before splitting
        f.parsed_attributes = parse_description(f.raw_description)
    assert any("new_length" in f.parsed_attributes for f in fs)
    r = cs.split_contractors(fs)
    cut = [f for f in r.a + r.b if "Split_Note" in f.extended_data]
    assert cut, "a straight cut through a grid must cross some lines"
    for f in cut:  # the 9999 ft attribute described the uncut line - no spelling of it may survive
        assert not any(k.lower() == "new_length" for k in {**f.extended_data, **f.parsed_attributes})
        assert float(f.extended_data["Footage_ft"]) > 0
    assert all(f.extended_data["Contractor"] in r.names for f in r.a + r.b)
    uncut = [f for f in r.a + r.b if "Split_Note" not in f.extended_data and "new_length" in f.parsed_attributes]
    assert uncut  # whole lines keep their own attribute


def test_whole_feature_mode_cuts_nothing(feats):
    r = cs.split_contractors(feats, clip=False)
    assert not any("Split_Note" in f.extended_data for f in r.a + r.b)
    assert len(r.a) + len(r.b) == len(feats)
    a, b = row(r, cs.TOTAL)
    assert abs(a - b) / (a + b) < 0.05


def test_uneven_share(feats):
    r = cs.split_contractors(feats, share_a=0.6)
    a, b = row(r, cs.TOTAL)
    assert 0.57 < a / (a + b) < 0.63


def test_fixed_direction(feats):
    r = cs.split_contractors(feats, fixed_angle=90.0)  # horizontal boundary
    lat1, lat2 = r.cut_lonlat[0][1], r.cut_lonlat[1][1]
    assert abs(lat1 - lat2) < 2e-3  # horizontal on the UTM grid; grid convergence tilts it slightly on the globe


def test_clip_polyline_exact_fraction_and_altitude():
    import numpy as np
    runs_a, runs_b = cs._clip_polyline([(0.0, 0.0, 0.0), (10.0, 0.0, 100.0)], np.array([-4.0, 6.0]))
    assert runs_a[0][-1] == pytest.approx((4.0, 0.0, 40.0)) and runs_b[0][0] == pytest.approx((4.0, 0.0, 40.0))


def test_contractor_kml_contains_only_their_features(res):
    kml_a = cs.contractor_kml(res.a, res.names[0], cs.COLORS[0], "proj")
    kml_b = cs.contractor_kml(res.b, res.names[1], cs.COLORS[1], "proj")
    pa, pb = parse_bytes(kml_a, "a.kml").features, parse_bytes(kml_b, "b.kml").features
    assert len(pa) == len(res.a) and len(pb) == len(res.b)
    assert {f.extended_data["Contractor"] for f in pa} == {res.names[0]}
    assert {f.extended_data["Contractor"] for f in pb} == {res.names[1]}
    assert pa[0].style.get("line_color") == cs.COLORS[0]
    a_ft = sum(f.geometry.length_feet for f in pa)
    assert a_ft == pytest.approx(row(res, cs.TOTAL)[0], rel=1e-3)


def test_bundle_zip(res):
    z = zipfile.ZipFile(io.BytesIO(cs.bundle_zip(res, "My Project")))
    names = z.namelist()
    assert len(names) == 4 and any(n.endswith("overview.kml") for n in names) and any(n.endswith(".csv") for n in names)
    overview = parse_bytes(z.read([n for n in names if n.endswith("overview.kml")][0]), "o.kml")
    assert len(overview.features) == len(res.a) + len(res.b) + 1  # + the boundary line


def test_nothing_to_split_raises():
    with pytest.raises(ValueError):
        cs.split_contractors(parse_bytes(b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>x</name></Placemark></kml>', "e.kml").features)


def test_unlabelled_lines_do_not_create_a_metric_per_feature():
    kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>""" + b"".join(
        f'<Placemark><name>Segment {i}</name><LineString><coordinates>-96,{29 + i * 0.001} -95.99,{29 + i * 0.001}'
        f'</coordinates></LineString></Placemark>'.encode() for i in range(20)) + b"</Document></kml>"
    names = cs.metric_names(parse_bytes(kml, "u.kml").features)
    assert names == [cs.TOTAL, cs.DUCT, cs.HANDHOLES, "Footage: Unspecified"]


# ---- review fixes -------------------------------------------------------

def test_all_zero_weights_rejected(feats):
    zero = {"total": 0, "duct": 0, "handholes": 0, "per_size": 0, "other_assets": 0}
    with pytest.raises(ValueError, match="weight"):
        cs.split_contractors(feats, weights=zero)
    # positive weights only for metrics this data doesn't have are just as unusable
    kml = (b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>L</name><LineString><coordinates>'
           b'-96,29 -96,29.01</coordinates></LineString></Placemark><Placemark><name>L2</name><LineString>'
           b'<coordinates>-95,29 -95,29.01</coordinates></LineString></Placemark></kml>')
    with pytest.raises(ValueError, match="weight"):
        cs.split_contractors(parse_bytes(kml, "l.kml").features,
                             weights={"total": 0, "duct": 0, "handholes": 5, "per_size": 0, "other_assets": 5})


def test_search_reaches_a_cut_far_from_dense_vertices():
    # one long run with 40 vertices in its first 1% and a single far endpoint: the balanced cut is mid-run
    dense = " ".join(f"-96.{i:04d},29.0" for i in range(40))
    kml = (f'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>long</name><LineString><coordinates>'
           f'{dense} -95.9,29.0</coordinates></LineString></Placemark></kml>').encode()
    r = cs.split_contractors(parse_bytes(kml, "d.kml").features, fixed_angle=0.0)
    a, b = row(r, cs.TOTAL)
    assert abs(a - b) / (a + b) < 0.02


def test_clip_point_lies_on_the_projected_boundary():
    import math
    from kml2report.gis import WGS84, centroid_lonlat, transformer, utm_epsg
    fs = _features()
    cand = cs.search_splits(fs)[0][0]
    r = cs.apply_split(fs, cand)
    c = centroid_lonlat(fs)
    fwd = transformer(WGS84, f"EPSG:{utm_epsg(*c)}")
    th = math.radians(cand.angle_deg)
    pieces = [f for f in r.a + r.b if "Split_Note" in f.extended_data]
    assert pieces
    for f in pieces:  # every end that touches the cut sits on it (to well under a millimetre)
        for g in f.geometry.iter_simple():
            for lon, lat, _ in (g.coordinates[0], g.coordinates[-1]):
                x, y = fwd.transform(lon, lat)
                d = x * math.cos(th) + y * math.sin(th) - cand.offset_m
                if abs(d) < 1.0:
                    assert abs(d) < 1e-3
    # exported footage agrees with what the search scored
    assert row(r, cs.TOTAL)[0] / (row(r, cs.TOTAL)[0] + row(r, cs.TOTAL)[1]) == pytest.approx(0.5, abs=0.02)


def test_exported_features_keep_full_duct_package():
    from kml2report.description_parser import parse_description
    fs = _features()
    for f in fs:
        f.parsed_attributes = parse_description(f.raw_description)  # repeated Duct_Size rows keep only the last one
    r = cs.split_contractors(fs)
    v = [f for f in r.a + r.b if f.name.startswith("Run V")]
    assert v and all(f.extended_data["Duct_Size"] == '2" + 2"' and f.extended_data["Duct_Count"] == "2" for f in v)
    assert not any(k.lower() == "duct_size" for f in v for k in f.parsed_attributes)


@pytest.mark.parametrize("names", [("A", "A"), ("A", " a "), ("", "B"), ("metric", "B"), ("A", "difference"),
                                   ("A", "A share %")])
def test_bad_contractor_names_rejected(feats, names):
    with pytest.raises(ValueError):
        cs.split_contractors(feats, names=names)


def test_zip_filenames_are_unique_even_for_alike_names(feats):
    r = cs.split_contractors(feats, names=("ACME A", "ACME_A"))
    names = zipfile.ZipFile(io.BytesIO(cs.bundle_zip(r, "p"))).namelist()
    assert len(names) == len(set(names)) == 4
    r2 = cs.split_contractors(feats, names=("overview", "B"))
    names2 = zipfile.ZipFile(io.BytesIO(cs.bundle_zip(r2, "p"))).namelist()
    assert len(names2) == len(set(names2)) == 4

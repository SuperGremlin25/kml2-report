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
    assert abs(pa - pb) <= 2 and pa + pb == 8  # difficulty is balanced too, so poles may give a little


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
    kml_a, ext_a = cs.contractor_file(res.a, res.names[0], "proj", cs.COLORS[0])
    kml_b, ext_b = cs.contractor_file(res.b, res.names[1], "proj", cs.COLORS[1])
    assert ext_a == ext_b == ".kml"
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
    zero = {"total": 0, "duct": 0, "handholes": 0, "per_size": 0, "other_assets": 0, "difficulty": 0, "dense": 0}
    with pytest.raises(ValueError, match="weight"):
        cs.split_contractors(feats, weights=zero)
    # positive weights only for metrics this data doesn't have are just as unusable
    kml = (b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>L</name><LineString><coordinates>'
           b'-96,29 -96,29.01</coordinates></LineString></Placemark><Placemark><name>L2</name><LineString>'
           b'<coordinates>-95,29 -95,29.01</coordinates></LineString></Placemark></kml>')
    with pytest.raises(ValueError, match="weight"):
        cs.split_contractors(parse_bytes(kml, "l.kml").features,
                             weights={"total": 0, "duct": 0, "handholes": 5, "per_size": 0, "other_assets": 5,
                                      "difficulty": 0, "dense": 0})


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
                                   ("A", "release"), ("A", "A share %")])
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



# ---- difficulty: a city and an open run are not the same work -----------------------------------------------

def _block_network(city_blocks=12, rural_runs=8):
    """City (west) and countryside (east) hold the same footage and the same handhole count, so a clean
    city/countryside split looks perfectly balanced to a footage + count objective."""
    lon0, lat0, blk_lat, blk_lon, n = -96.00, 29.00, 0.0009, 0.0010, city_blocks
    items = []

    def seg(name, coords):
        cs_ = " ".join(f"{x:.6f},{y:.6f},0" for x, y in coords)
        return (f'<Placemark><name>{name}</name><description><![CDATA[<table><tr><td>Duct_Size</td><td>2"</td></tr>'
                f'</table>]]></description><LineString><coordinates>{cs_}</coordinates></LineString></Placemark>')

    def pt(name, x, y):
        return f'<Placemark><name>{name}</name><Point><coordinates>{x:.6f},{y:.6f}</coordinates></Point></Placemark>'

    for i in range(n + 1):
        items.append(seg(f"City EW {i}", [(lon0 + j * blk_lon, lat0 + i * blk_lat) for j in range(n + 1)]))
        items.append(seg(f"City NS {i}", [(lon0 + i * blk_lon, lat0 + j * blk_lat) for j in range(n + 1)]))
        for j in range(n + 1):
            items.append(pt(f"HH-C{i}-{j}", lon0 + i * blk_lon, lat0 + j * blk_lat))
    east = lon0 + n * blk_lon + 0.02
    for r in range(rural_runs):
        lat = lat0 + r * 0.0014
        items.append(seg(f"Rural {r}", [(east, lat), (east + 0.0408, lat)]))
        for k in range(1, 22):
            items.append(pt(f"HH-R{r}-{k}", east + k * 0.0408 / 22, lat))
    kml = ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>Build</name>' + "".join(items) +
           "</Folder></Document></kml>").encode()
    return parse_bytes(kml, "city.kml").features


@pytest.fixture(scope="module")
def city_feats():
    return _block_network()


def share(r, metric):
    return r.stats.set_index("metric").loc[metric, f"{r.names[0]} share %"]


def test_without_difficulty_one_contractor_gets_the_whole_city(city_feats):
    """The reported problem: equal footage and handholes, but all the dense work on one side."""
    r = cs.split_contractors(city_feats, weights={"difficulty": 0, "dense": 0})
    assert 49 < share(r, cs.TOTAL) < 51 and 49 < share(r, cs.HANDHOLES) < 51
    s = share(r, cs.DENSE)
    assert s > 90 or s < 10


def test_difficulty_balanced_by_default_gives_each_contractor_part_of_the_city(city_feats):
    r = cs.split_contractors(city_feats)
    assert 45 < share(r, cs.DENSE) < 55
    assert 47 < share(r, cs.DIFFICULTY) < 53
    assert 49 < share(r, cs.TOTAL) < 51 and 48 < share(r, cs.HANDHOLES) < 52  # nothing else was sacrificed


def test_uniform_file_has_no_phantom_dense_area():
    fs = _features()  # an even grid: nowhere is meaningfully denser than the rest
    a = cs._prepare(fs, cs.Difficulty())
    assert a.dense.mean() < 0.15 and a.mult.mean() < 1.15
    long_runs = b"".join(
        f'<Placemark><name>R{i}</name><LineString><coordinates>-96,{29 + i * 0.003} -95.9,{29 + i * 0.003}'
        f'</coordinates></LineString></Placemark>'.encode() for i in range(6))
    a = cs._prepare(parse_bytes(b'<kml xmlns="http://www.opengis.net/kml/2.2">' + long_runs + b"</kml>", "r.kml").features,
                    cs.Difficulty())
    assert not a.dense.any() and a.mult.max() == pytest.approx(1.0)


def test_known_difficult_folder_is_weighted_and_balanced():
    def run(folder, lat):
        return (f'<Folder><name>{folder}</name><Placemark><name>{folder} run</name><LineString><coordinates>'
                f'-96,{lat} -95.95,{lat}</coordinates></LineString></Placemark></Folder>')
    kml = ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + "".join(
        run("Open field", 29 + i * 0.002) for i in range(6)) + "".join(
        run("River crossing", 29.02 + i * 0.002) for i in range(6)) + "</Document></kml>").encode()
    fs = parse_bytes(kml, "f.kml").features
    d = cs.Difficulty(auto=False, hard_layers=("River crossing",), hard_multiplier=3.0)
    a = cs._prepare(fs, d)
    i, j = a.names.index(cs.TOTAL), a.names.index(cs.DIFFICULTY)
    assert a.total[j] == pytest.approx(a.total[i] * (1 + 3) / 2, rel=1e-3)  # half the footage costs 3x
    r = cs.split_contractors(fs, difficulty=d)
    assert 45 < share(r, cs.DIFFICULTY) < 55 and 45 < share(r, cs.DENSE) < 55
    assert {cs.layer_key(f) for f in fs} == {"Doc / Open field", "Doc / River crossing"} or \
        {cs.layer_key(f) for f in fs} == {"Open field", "River crossing"}


def test_difficulty_disabled_matches_previous_behaviour(feats):
    r = cs.split_contractors(feats, difficulty=cs.Difficulty(auto=False))
    assert cs.DIFFICULTY not in set(r.stats["metric"]) and cs.DENSE not in set(r.stats["metric"])


def test_difficulty_map_pieces(city_feats):
    segs = cs.difficulty_segments(city_feats, cs.Difficulty())
    assert segs and any(s["dense"] for s in segs) and any(not s["dense"] for s in segs)
    assert max(s["mult"] for s in segs) == pytest.approx(2.0) and min(s["mult"] for s in segs) == pytest.approx(1.0)


# ---- the fast evaluator must agree exactly with an obviously-correct slow one --------------------------------

def _brute(a, theta, offsets, clip):
    import math
    import numpy as np
    nx, ny = math.cos(theta), math.sin(theta)
    out = np.zeros((len(offsets), len(a.names)))
    for k, c in enumerate(offsets):
        if clip:
            for (x0, y0, x1, y1), w in zip(a.seg, a.w_seg):
                d0, d1 = x0 * nx + y0 * ny - c, x1 * nx + y1 * ny - c
                f = 1.0 if (d0 < 0 and d1 < 0) else 0.0 if (d0 >= 0 and d1 >= 0) else (
                    -d0 / (d1 - d0) if d0 < 0 else -d1 / (d0 - d1))
                out[k] += f * w
            for (x, y), w in zip(a.pt, a.w_pt):
                if x * nx + y * ny - c < 0:
                    out[k] += w
        else:
            for (x, y), w in zip(a.cen, a.w_cen):
                if x * nx + y * ny - c < 0:
                    out[k] += w
    return out


@pytest.mark.parametrize("clip", [True, False])
def test_fast_totals_match_brute_force(feats, clip):
    import math
    import numpy as np
    a = cs._prepare(feats, cs.Difficulty())
    rng = np.random.default_rng(1)
    for ang in (0, 17, 45, 90, 133, 271):
        th = math.radians(ang)
        proj = a.seg[:, 0] * math.cos(th) + a.seg[:, 1] * math.sin(th)
        offs = np.sort(rng.uniform(proj.min(), proj.max(), 15))
        fast, _ = cs._totals(a, th, offs, clip)
        assert np.allclose(fast, _brute(a, th, offs, clip), rtol=1e-9, atol=1e-6)


def test_angle_refinement_finds_cut_nearly_parallel_to_long_runs():
    # long parallel runs: a cut 1 degree off parallel moves ~500 m along them, so a 5 degree grid cannot hit the balance
    items = "".join(
        f'<Placemark><name>R{i}</name><LineString><coordinates>-96,{29 + i * 0.0014} -95.7,{29 + i * 0.0014}'
        f'</coordinates></LineString></Placemark>' for i in range(8))
    fs = parse_bytes(('<kml xmlns="http://www.opengis.net/kml/2.2">' + items + "</kml>").encode(), "p.kml").features
    r = cs.split_contractors(fs, difficulty=cs.Difficulty(auto=False))
    assert 49.5 < share(r, cs.TOTAL) < 50.5


def test_release_boundary_can_cut_one_long_feature():
    kml = (b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>Long run</name><LineString><coordinates>'
           b'-96,29 -95.9,29</coordinates></LineString></Placemark></kml>')
    fs = parse_bytes(kml, "long.kml").features
    plan = cs.plan_releases(fs, 5000, start="west", max_releases=2,
                            difficulty=cs.Difficulty(auto=False))
    assert len(plan.releases) == 2 and not plan.warnings
    assert all(sum(f.geometry.length_feet for f in r.a + r.b) == pytest.approx(10000, rel=0.01)
               for r in plan.releases)

"""Releases: hand the work out N feet per contractor at a time, using the same auto-balancing."""

import io
import zipfile

import pytest

from kml2report import contractor_split as cs
from kml2report.kml_parser import parse_bytes
from tests.test_contractor_split import _block_network
from tests.test_style_preservation import make_kmz


@pytest.fixture(scope="module")
def feats():
    return _block_network()


@pytest.fixture(scope="module")
def plan(feats):
    return cs.plan_releases(feats, 5000, max_releases=6)


def ft(fs):
    return sum(f.geometry.length_feet for f in fs if f.geometry)


def hh(fs):
    return sum(1 for f in fs if f.name.startswith("HH") and f.geometry)


def test_each_contractor_gets_the_requested_footage_each_release(plan):
    assert len(plan.releases) == 6 and not plan.warnings
    for r in plan.releases:
        assert ft(r.a) == pytest.approx(5000, rel=0.01) and ft(r.b) == pytest.approx(5000, rel=0.01)


def test_nothing_lost_or_given_twice(feats, plan):
    released = [f for r in plan.releases for f in r.a + r.b]
    assert ft(released) + ft(plan.remaining) == pytest.approx(ft(feats), rel=1e-4)
    assert hh(released) + hh(plan.remaining) == hh(feats)  # every handhole is in exactly one place
    assert ft(plan.remaining) == pytest.approx(ft(feats) - 6 * 10000, rel=1e-3)


def test_releases_are_deterministic_and_prefix_stable(feats, plan):
    more = cs.plan_releases(feats, 5000, max_releases=8)
    for r1, r2 in zip(plan.releases, more.releases):
        assert [(f.name, round(f.geometry.length_feet, 3)) for f in r1.a + r1.b] == \
               [(f.name, round(f.geometry.length_feet, 3)) for f in r2.a + r2.b]


def test_same_autopick_balances_difficulty_inside_each_release(plan):
    # the first releases sit in the city: each contractor must get a fair share of the hard work and handholes
    for r in plan.releases:
        st = r.split.stats.set_index("metric")
        a, b = st.loc[cs.DIFFICULTY, plan.names[0]], st.loc[cs.DIFFICULTY, plan.names[1]]
        assert abs(a - b) / (a + b) < 0.05
        assert abs(hh(r.a) - hh(r.b)) <= 3


def test_dense_work_is_measured_against_the_whole_project(plan):
    # a slice entirely inside the city must still count as dense (a per-slice yardstick would call it normal)
    st = plan.releases[2].split.stats.set_index("metric")
    assert st.loc[cs.DENSE, plan.names[0]] > 4000 and st.loc[cs.DENSE, plan.names[1]] > 4000


def _mean_lon(fs):
    lons = [c[0] for f in fs for c in f.geometry.coordinates]
    return sum(lons) / len(lons)


def test_start_end_controls_where_work_is_handed_out_first(feats):
    west = cs.plan_releases(feats, 5000, start="west", max_releases=3)
    east = cs.plan_releases(feats, 5000, start="east", max_releases=3)
    w = [_mean_lon(r.a + r.b) for r in west.releases]
    e = [_mean_lon(r.a + r.b) for r in east.releases]
    assert w == sorted(w) and e == sorted(e, reverse=True)       # each release continues where the last stopped
    assert w[0] < e[0]


def test_final_release_takes_the_rest(feats):
    plan = cs.plan_releases(feats, 60000)  # far more than is left after the first release
    assert plan.releases[-1].final and not plan.remaining
    assert ft([f for r in plan.releases for f in r.a + r.b]) == pytest.approx(ft(feats), rel=1e-4)


def test_release_number_is_tagged_on_every_feature(plan):
    for r in plan.releases:
        assert all(f.extended_data["Release"] == str(r.number) for f in r.a + r.b)


def test_overview_table(plan):
    o = plan.overview
    assert list(o["release"]) == [1, 2, 3, 4, 5, 6]
    assert o[f"{plan.names[0]} total so far ft"].iloc[-1] == pytest.approx(30000, rel=0.01)
    assert o["not yet released ft"].iloc[0] > o["not yet released ft"].iloc[-1]


def test_bad_inputs(feats):
    with pytest.raises(ValueError):
        cs.plan_releases(feats, 0)
    with pytest.raises(ValueError):
        cs.plan_releases(feats, 5000, names=("A", "a"))


def test_unusable_weights_return_warnings_without_releases(feats):
    zero = {"total": 0, "duct": 0, "handholes": 0, "per_size": 0, "other_assets": 0, "difficulty": 0, "dense": 0}
    plan = cs.plan_releases(feats, 5000, weights=zero)
    assert not plan.releases and plan.warnings


def test_zip_has_every_release_unique_names_and_only_own_work(plan):
    z = zipfile.ZipFile(io.BytesIO(cs.releases_zip(plan, "proj")))
    names = z.namelist()
    assert len(names) == len(set(names))
    assert sum("_release0" in n and "_contractor1_" in n for n in names) == 6
    assert any(n.endswith("release_summary.csv") for n in names) and any("not_yet_released" in n for n in names)
    r1a = [n for n in names if "_release01_contractor1_" in n][0]
    out = parse_bytes(z.read(r1a), "x.kml").features
    assert {f.extended_data["Release"] for f in out} == {"1"}
    assert ft(out) == pytest.approx(ft(plan.releases[0].a), rel=1e-3)
    everything = [n for n in names if n.endswith(("all_releases.kml", "all_releases.kmz")) and "_contractor1_" in n][0]
    cumulative = parse_bytes(z.read(everything), "x.kml").features
    assert ft(cumulative) == pytest.approx(sum(ft(r.a) for r in plan.releases), rel=1e-3)
    assert {f.folders[0] for f in cumulative} >= {"Release 1", "Release 6"}


def test_releases_keep_the_users_icons():
    fs = parse_bytes(make_kmz(), "build.kmz").features
    plan = cs.plan_releases(fs, 1500, start="west", difficulty=cs.Difficulty(auto=False))
    assert len(plan.releases) >= 2
    seen_icon_file = False
    for r in plan.releases:
        for who in (0, 1):
            data, ext = cs.release_file(plan, r.number, who, "proj")
            out = parse_bytes(data, "r" + ext).features
            assert all(f.style_url or f.style_xml for f in out if f.name != "")  # nobody lost their style
            uses_icon = any(f.name.startswith(("Pole", "HH")) for f in out)
            if uses_icon:  # whoever gets poles / handholes gets the KMZ with the images inside
                assert ext == ".kmz"
                assert zipfile.ZipFile(io.BytesIO(data)).read("icons/pole.png" if any(
                    f.name.startswith("Pole") for f in out) else "icons/hh_hi.png")
                seen_icon_file = True
            else:
                assert ext == ".kml"  # no packed icons needed, so no need for a zip
    assert seen_icon_file

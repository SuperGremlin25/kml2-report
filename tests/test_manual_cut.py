"""Place-the-cut-yourself split: slider position and cut-through-a-point."""

import pytest

from kml2report import contractor_split as cs
from tests.test_contractor_split import _block_network


def ft(fs):
    return sum(f.geometry.length_feet for f in fs if f.geometry)


@pytest.fixture(scope="module")
def feats():
    return _block_network()


def test_extent_and_conservation(feats):
    lo, hi = cs.cut_extent(feats, 0.0)
    assert hi > lo
    for frac in (0.1, 0.5, 0.9):
        res = cs.manual_cut(feats, 0.0, lo + (hi - lo) * frac, True, ("West crew", "East crew"))
        assert ft(res.a) + ft(res.b) == pytest.approx(ft(feats), rel=1e-4)
        assert res.a and res.b


def test_moving_the_cut_east_gives_the_west_side_more(feats):
    lo, hi = cs.cut_extent(feats, 0.0)
    a = [ft(cs.manual_cut(feats, 0.0, lo + (hi - lo) * f).a) for f in (0.2, 0.5, 0.8)]
    assert a[0] < a[1] < a[2]


def test_cut_through_a_point_puts_the_point_on_the_line(feats):
    pt = next(p for f in feats for p in f.geometry.iter_simple() if p.geometry_type == "Point")
    lon, lat = pt.coordinates[0][:2]
    off = cs.offset_through_point(feats, 0.0, lon, lat)
    lo, hi = cs.cut_extent(feats, 0.0)
    assert lo <= off <= hi
    res = cs.manual_cut(feats, 0.0, off)
    assert all(c[0] <= lon + 1e-5 for f in res.a for c in f.geometry.coordinates)
    assert all(c[0] >= lon - 1e-5 for f in res.b for c in f.geometry.coordinates)

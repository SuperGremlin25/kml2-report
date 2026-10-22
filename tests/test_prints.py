"""Print-set (PDF) placement and callout reading, on a synthetic CAD-style PDF."""

import io
import math

import pytest

reportlab = pytest.importorskip("reportlab")
pytest.importorskip("pdfplumber")
pytest.importorskip("pypdf")

from reportlab.pdfgen import canvas

from kml2report import prints
from kml2report.kml_parser import parse_bytes

FT_PT = prints.DEFAULT_FT_PER_PT
LAT0, LON0 = 35.35, -99.17  # lat/lon at the page origin (top-left) of sheet 1
FT_LON = prints.FT_PER_DEG_LAT * math.cos(math.radians(LAT0))


def lonlat(x, y, lon0=LON0, lat0=LAT0):
    return lon0 + x * FT_PT / FT_LON, lat0 - y * FT_PT / prints.FT_PER_DEG_LAT


def _pole(c, h, x, y, lon, lat, lat_text=None):
    c.setFont("Helvetica", 8)
    c.drawString(x, h - y, "POLE_NUM:P%d" % x)
    c.drawString(x, h - y - 10, "LAT : %s" % (lat_text or f"{lat:.6f}"))
    c.drawString(x, h - y - 20, f"LONG : {lon:.6f}")
    c.setStrokeColorRGB(1, 0, 0)
    c.circle(x, h - y, 9.85, stroke=1, fill=0)


def make_pdf():
    W, H = 1000, 800
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    # sheet 1: three poles, one with a typo'd latitude
    pts = [(100, 100), (500, 150), (800, 300), (300, 400)]
    for i, (x, y) in enumerate(pts):
        lon, lat = lonlat(x, y)
        if i == 3:
            _pole(c, H, x, y, lon, lat, lat_text=f"{lat + 0.01:.6f}")  # ~3,600 ft off
        else:
            _pole(c, H, x, y, lon, lat)
    c.setFont("Helvetica", 9)
    c.drawString(200, H - 600, "344'")
    c.drawString(400, H - 620, "256'")
    c.drawString(600, H - 640, "RISER")
    c.setFont("Helvetica-Bold", 48)
    c.drawString(850, H - 760, "PRINT-01")
    c.setFont("Helvetica", 10)
    c.drawString(100, H - 700, "MATCHLINE PRINT-02")
    c.showPage()
    # sheet 2: no poles, handhole labels only
    c.setFont("Helvetica", 11)
    c.drawString(100, H - 100, "HH66")
    c.drawString(700, H - 400, "HH67")
    c.setFont("Helvetica", 9)
    c.drawString(300, H - 300, "500'")
    c.setFont("Helvetica-Bold", 48)
    c.drawString(850, H - 760, "PRINT-02")
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture(scope="module")
def plan():
    return prints.georeference(prints.read_plan_set(make_pdf(), "t.pdf"))


def test_sheet_labels_come_from_title_block(plan):
    assert [s.label for s in plan.sheets] == ["PRINT-01", "PRINT-02"]


def test_pole_callouts_parsed_and_typo_flagged(plan):
    s = plan.sheets[0]
    poles = [c for c in s.callouts if c.kind == "pole"]
    assert len(poles) == 4
    flagged = [c for c in poles if c.flag]
    assert len(flagged) == 1 and "typo" in flagged[0].flag


def test_sheet_placed_to_known_origin(plan):
    s = plan.sheets[0]
    assert s.placed and s.n_anchors == 3
    assert s.origin_lon == pytest.approx(LON0, abs=2e-5)
    assert s.origin_lat == pytest.approx(LAT0, abs=2e-5)
    assert not plan.sheets[1].placed


def test_footage_callouts_and_footprint(plan):
    s = plan.sheets[0]
    assert s.footage_total() == 600
    ring = s.footprint()
    assert len(ring) == 5 and ring[0] == ring[-1]


def test_place_from_kml_names_and_compare(plan):
    x1, y1 = 100, 100
    x2, y2 = 700, 400
    a = lonlat(x1, y1, -99.15, 35.36)
    b = lonlat(x2, y2, -99.15, 35.36)
    kml = f"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
    <Placemark><name>HH66</name><Point><coordinates>{a[0]},{a[1]},0</coordinates></Point></Placemark>
    <Placemark><name>HH-67</name><Point><coordinates>{b[0]},{b[1]},0</coordinates></Point></Placemark>
    <Placemark><name>route</name><LineString><coordinates>{a[0]},{a[1]},0 {b[0]},{b[1]},0</coordinates></LineString></Placemark>
    </Document></kml>""".encode()
    feats = parse_bytes(kml, "t.kml").features
    assert prints.place_from_kml_names(plan, feats) == 1
    assert plan.sheets[1].placed
    assert plan.sheets[1].origin_lon == pytest.approx(-99.15, abs=1e-4)
    cmp_ = prints.compare_with_features(plan, feats)
    row = cmp_[cmp_["sheet"] == "PRINT-02"].iloc[0]
    expect = math.hypot((x2 - x1) * FT_PT, (y2 - y1) * FT_PT)
    assert row["KML line ft inside sheet"] == pytest.approx(expect, rel=0.02)
    assert row["print callout sum (ft)"] == 500


def test_kml_export_has_sheets_and_pins(plan):
    kml = prints.to_kml(plan).decode()
    assert "<name>Sheets</name>" in kml and "PRINT-01" in kml
    assert kml.count("<Polygon>") >= 1
    assert "<name>Callouts</name>" in kml


def test_place_by_registration_without_names():
    plan = prints.read_plan_set(make_pdf(), "t.pdf")
    sh = plan.sheets[1]
    # four extra handhole labels so the pattern is distinctive
    for k, (x, y) in enumerate([(100, 100), (700, 400), (300, 600), (900, 150)]):
        sh.callouts.append(prints.Callout("handhole", f"HH{k}", x, y))
    sh.callouts = [c for c in sh.callouts if c.kind != "handhole"] + [
        prints.Callout("handhole", f"HH{k}", x, y)
        for k, (x, y) in enumerate([(100, 100), (700, 400), (300, 600), (900, 150)])]
    o_lon, o_lat = -99.15, 35.36
    pts = "".join(
        f"<Placemark><name>HH-0000{k}</name><Point><coordinates>{lonlat(x, y, o_lon, o_lat)[0]},"
        f"{lonlat(x, y, o_lon, o_lat)[1]},0</coordinates></Point></Placemark>"
        for k, (x, y) in enumerate([(100, 100), (700, 400), (300, 600), (900, 150)]))
    feats = parse_bytes(f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{pts}</Document></kml>'.encode(), "t.kml").features
    assert prints.place_by_registration(plan, feats, min_hits=4) == 1
    assert plan.sheets[1].tentative
    assert plan.sheets[1].origin_lon == pytest.approx(o_lon, abs=1e-4)
    assert plan.sheets[1].origin_lat == pytest.approx(o_lat, abs=1e-4)

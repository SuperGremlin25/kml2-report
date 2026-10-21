"""Tests for the hardened parser, GIS toolkit and exporters."""

import io
import json
import zipfile

import pytest

from kml2report import geoexport, gis
from kml2report.kml_parser import parse_bytes

GE_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document><name>Doc</name>
 <Style id="s1"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>
 <Folder><name>Outer</name><Folder><name>Inner</name>
  <Placemark><name>Parcel</name><ExtendedData><Data name="zone"><value>R1</value></Data></ExtendedData>
   <Polygon><outerBoundaryIs><LinearRing><coordinates>-96,29,0 -96,29.001,0 -95.999,29.001,0 -95.999,29,0 -96,29,0</coordinates></LinearRing></outerBoundaryIs>
   <innerBoundaryIs><LinearRing><coordinates>-95.9995,29.0003,0 -95.9995,29.0006,0 -95.9993,29.0006,0 -95.9995,29.0003,0</coordinates></LinearRing></innerBoundaryIs></Polygon></Placemark>
  <Placemark><name>Spaced</name><styleUrl>#s1</styleUrl><LineString><coordinates>-96.0, 29.0, 0 -96.0, 29.001, 0</coordinates></LineString></Placemark>
  <Placemark><name>Track</name><gx:Track><gx:coord>-96 29 10</gx:coord><gx:coord>-96.001 29.001 12</gx:coord></gx:Track></Placemark>
  <Placemark><name>Multi</name><MultiGeometry><LineString><coordinates>-96,29 -96,29.001</coordinates></LineString><LineString><coordinates>-95,29 -95,29.001</coordinates></LineString></MultiGeometry></Placemark>
  <Placemark><name>Bad</name><LineString><coordinates>-96,29 abc,def -96.1,29.1</coordinates></LineString></Placemark>
  <Placemark><name>Pole</name><Point><coordinates>-96,29</coordinates></Point></Placemark>
 </Folder></Folder></Document></kml>"""


@pytest.fixture(scope="module")
def report():
    return parse_bytes(GE_KML, "ge.kml")


def by_name(report, name):
    return next(f for f in report.features if f.name == name)


def test_bad_coordinate_does_not_abort_file(report):
    assert len(report.features) == 6
    assert any("abc,def" in w for w in report.warnings)
    assert len(by_name(report, "Bad").geometry.coordinates) == 2


def test_space_after_comma_coordinates(report):
    g = by_name(report, "Spaced").geometry
    assert len(g.coordinates) == 2
    assert 360 < g.length_feet < 370  # 0.001 deg lat ~ 364 ft


def test_polygon_holes_and_area(report):
    g = by_name(report, "Parcel").geometry
    assert len(g.holes) == 1
    assert g.area_m2 > 0
    outer_only = type(g)("Polygon", g.coordinates)
    assert g.area_m2 < outer_only.area_m2


def test_gx_track(report):
    g = by_name(report, "Track").geometry
    assert g.geometry_type == "Track" and len(g.coordinates) == 2
    assert g.coordinates[0][2] == 10.0


def test_multigeometry_parts_measure_exactly(report):
    g = by_name(report, "Multi").geometry
    assert len(g.parts) == 2
    assert 720 < g.length_feet < 740  # two 364 ft pieces, not a line across the gap


def test_folder_path_and_style(report):
    f = by_name(report, "Spaced")
    assert f.layer == "Inner"
    assert f.folder_path == "Doc / Outer / Inner"
    assert f.style["line_color"] == "#ff0000"


def test_kmz_roundtrip_and_sniffing():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", GE_KML)
    assert len(parse_bytes(buf.getvalue(), "x.kmz").features) == 6
    assert len(parse_bytes(buf.getvalue(), "mislabelled.kml").features) == 6


def test_garbage_does_not_raise():
    r = parse_bytes(b"not xml at all", "junk.kml")
    assert r.features == [] and r.warnings


def test_attribute_table_and_stats(report):
    df = gis.attribute_table(report.features)
    assert "calc_length_ft" in df.columns and len(df) == 6
    assert gis.layer_stats(report.features)["features"].sum() == 6


def test_vertices_have_stationing(report):
    v = gis.extract_vertices([by_name(report, "Spaced")])
    assert list(v["station_ft"])[0] == 0 and v["station_ft"].iloc[-1] > 300


def test_qa_flags_issues(report):
    kinds = {i.check for i in gis.run_qa(report.features)}
    assert "Dangling end" in kinds


def test_geojson_valid(report):
    gj = json.loads(geoexport.to_geojson_bytes(report.features))
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 6


def test_shapefile_dxf_kml_exports(report):
    z = zipfile.ZipFile(io.BytesIO(geoexport.to_shapefile_zip(report.features, "EPSG:2278")))
    assert {"points.shp", "lines.prj", "polygons.dbf"} <= set(z.namelist())
    import ezdxf
    doc = ezdxf.read(io.StringIO(geoexport.to_dxf_bytes(report.features).decode()))
    assert len(doc.modelspace()) > 0
    assert len(parse_bytes(geoexport.to_kml_bytes(report.features), "o.kml").features) == 6


def test_buffer_and_nearest(report):
    assert gis.buffer_features(report.features, 25)["features"]
    near = gis.nearest_features(report.features, by_name(report, "Pole"), 2)
    assert len(near) == 2


# ---- review fixes -------------------------------------------------------

def test_trailing_comma_run_is_flagged_not_corrupted():
    from kml2report.kml_parser import _parse_coord_text
    w = []
    assert _parse_coord_text("-96,29, -95,30,0", w) == [] and any("ambiguous" in x for x in w)
    assert _parse_coord_text("-96,29,0 -95,30,0", [])  # normal whitespace-separated tuples still parse


def test_inner_empty_fields_rejected():
    from kml2report.kml_parser import _parse_coord_text
    w = []
    assert _parse_coord_text("-96,,10", w) == [] and _parse_coord_text(",29,10", w) == []
    assert len(w) == 2


def test_mixed_zero_and_nonzero_altitude_still_analysable():
    kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>L</name><LineString><coordinates>
    -96,29,0 -96,29.001,12</coordinates></LineString></Placemark></kml>"""
    f = parse_bytes(kml, "a.kml").features[0]
    assert gis.to_shapely(f) is not None
    assert all(len(p) == 3 for p in f.geometry.to_geojson()["coordinates"])


def test_group_summary_name_collision():
    from kml2report.models import Feature
    f = Feature(name="a", extended_data={"count": "x", "length_ft": "y"})
    assert "count (value)" in gis.group_summary([f], "count").columns
    assert "length_ft (value)" in gis.group_summary([f], "length_ft").columns


def test_vertices_include_hole_rings(report):
    v = gis.extract_vertices([by_name(report, "Parcel")])
    assert set(v["ring"]) == {"outer", "hole 1"}


def test_qa_covers_multipart_and_unusable_polygon():
    kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2">
    <Placemark><name>M</name><MultiGeometry>
      <LineString><coordinates>-96,29 -96,29.001 -96.001,29.0005 -95.999,29.0005</coordinates></LineString>
      <LineString><coordinates>-95,29 -95,29.001</coordinates></LineString></MultiGeometry></Placemark>
    <Placemark><name>P</name><Polygon><outerBoundaryIs><LinearRing><coordinates>-96,29 abc -95,29</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
    </kml>"""
    checks = {i.check for i in gis.run_qa(parse_bytes(kml, "m.kml").features)}
    assert {"Self-intersecting line", "Dangling end", "Invalid polygon"} <= checks


def test_dxf_label_height_and_feet_crs_units():
    import ezdxf
    kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>Pole</name><Point><coordinates>-96,29</coordinates></Point></Placemark></kml>"""
    feats = parse_bytes(kml, "p.kml").features
    doc = ezdxf.read(io.StringIO(geoexport.to_dxf_bytes(feats, "EPSG:2278").decode()))
    assert any(e.dxftype() == "TEXT" for e in doc.modelspace())
    # a feet CRS is already in feet: units_feet=False must convert to metres, not leave feet labelled as metres
    x_ft = next(e for e in ezdxf.read(io.StringIO(geoexport.to_dxf_bytes(feats, "EPSG:2278").decode())).modelspace()
                if e.dxftype() == "POINT").dxf.location.x
    x_m = next(e for e in ezdxf.read(io.StringIO(geoexport.to_dxf_bytes(feats, "EPSG:2278", units_feet=False).decode())).modelspace()
               if e.dxftype() == "POINT").dxf.location.x
    assert abs(x_ft / x_m - 3.28) < 0.01


def test_projection_centre_survives_invalid_polygons():
    kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>bow</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
    -96,29 -95.9,29.1 -95.9,29 -96,29.1 -96,29</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>"""
    feats = parse_bytes(kml, "b.kml").features
    assert gis.centroid_lonlat(feats) is not None and gis.buffer_features(feats, 10) is not None


def test_feature_without_coordinates_does_not_break_bounds():
    kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>none</name></Placemark></kml>"""
    feats = parse_bytes(kml, "n.kml").features
    assert gis.bounds_of(feats) is None

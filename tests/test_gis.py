"""Tests for the hardened parser, GIS toolkit and exporters."""

import io
import json
import zipfile

import pytest

from kml2report import geoexport, gis
from kml2report.kml_parser import _parse_coord_text, parse_bytes
from kml2report.models import Feature, Geometry

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


def test_vertex_table_includes_polygon_hole_ring(report):
    vertices = gis.extract_vertices([by_name(report, "Parcel")])
    assert len(vertices) == 9
    assert set(vertices["ring"]) == {0, 1}
    assert vertices.groupby("ring")["station_ft"].first().eq(0).all()


def test_description_attributes_keep_precedence():
    feature = Feature(
        raw_description="Kind=buried; Shared=description; DescriptionOnly=visible",
        extended_data={"Shared": "extended"},
        parsed_attributes={"Shared": "normalized"},
    )
    attrs = gis.feature_attributes(feature)
    assert attrs["kind"] == "buried"
    assert attrs["descriptiononly"] == "visible"
    assert attrs["Shared"] == "normalized"


@pytest.mark.parametrize("field", ["length_ft", "area_acres", "count"])
def test_group_summary_disambiguates_aggregate_names(field):
    feature = Feature(
        extended_data={field: "group value"},
        geometry=Geometry("LineString", [(-96, 29, 0), (-96, 29.001, 0)]),
    )
    summary = gis.group_summary([feature], field)
    label = f"{field}_group"
    assert summary.loc[0, label] == "group value"
    assert field in summary.columns
    if field == "count":
        assert summary.loc[0, "count"] == 1


def test_centroid_does_not_require_union_of_invalid_geometry():
    invalid = Feature(geometry=Geometry(
        "Polygon", [(-96, 29, 0), (-95.999, 29.001, 0), (-96, 29.001, 0),
                    (-95.999, 29, 0), (-96, 29, 0)]
    ))
    assert gis.centroid_lonlat([invalid]) is not None


def test_coordinate_parser_splits_trailing_comma_tuple_separator():
    warnings = []
    coords = _parse_coord_text("-96,29, -95,30,0", warnings)
    assert coords == [(-96, 29, 0.0), (-95, 30, 0.0)]
    assert not warnings


@pytest.mark.parametrize("text", ["-96,,10", ",29,10"])
def test_coordinate_parser_skips_empty_fields(text):
    warnings = []
    assert _parse_coord_text(text, warnings) == []
    assert warnings


def test_qa_flags_issues(report):
    kinds = {i.check for i in gis.run_qa(report.features)}
    assert "Dangling end" in kinds


def test_qa_checks_each_multipart_line_part():
    feature = Feature(geometry=Geometry(
        "LineString",
        [(-96, 29, 0), (-95.999, 29, 0), (-95, 29, 0), (-94.999, 29, 0)],
        parts=[
            Geometry("LineString", [(-96, 29, 0), (-95.999, 29, 0)]),
            Geometry("LineString", [(-95, 29, 0), (-94.999, 29, 0)]),
        ],
    ))
    assert any(issue.check == "Dangling end" for issue in gis.run_qa([feature]))


def test_qa_flags_polygon_conversion_failure():
    feature = Feature(geometry=Geometry("Polygon", [(-96, 29, 0), (-95.999, 29, 0)]))
    assert any(issue.check == "Invalid polygon" for issue in gis.run_qa([feature]))


def test_zero_altitude_is_preserved_across_geojson_vertices():
    feature = Feature(geometry=Geometry(
        "LineString", [(-96, 29, 0), (-95.999, 29, 5)]
    ))
    coordinates = feature.geometry.to_geojson()["coordinates"]
    assert coordinates == [[-96, 29, 0], [-95.999, 29, 5]]


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


def test_dxf_labels_and_requested_units():
    import ezdxf

    point = Feature(name="Pole", geometry=Geometry("Point", [(-96, 29, 0)]))
    data = geoexport.to_dxf_bytes([point], "EPSG:2278", units_feet=False)
    doc = ezdxf.read(io.StringIO(data.decode()))
    entities = list(doc.modelspace())
    label = next(entity for entity in entities if entity.dxftype() == "TEXT")
    exported_point = next(entity for entity in entities if entity.dxftype() == "POINT")
    x, y = geoexport.transformer("EPSG:4326", "EPSG:2278").transform(-96, 29)
    unit_to_m = geoexport.CRS.from_epsg(2278).axis_info[0].unit_conversion_factor
    assert label.dxf.height == 0.6
    assert doc.units == 6
    assert exported_point.dxf.location.x == pytest.approx(x * unit_to_m)
    assert exported_point.dxf.location.y == pytest.approx(y * unit_to_m)


def test_buffer_and_nearest(report):
    assert gis.buffer_features(report.features, 25)["features"]
    near = gis.nearest_features(report.features, by_name(report, "Pole"), 2)
    assert len(near) == 2

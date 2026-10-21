"""Exports must reproduce the user's own styling (icons, scales, labels, StyleMaps, folders, descriptions)."""

import io
import zipfile

import pytest

from kml2report import contractor_split as cs
from kml2report import geoexport
from kml2report.kml_parser import parse_bytes

POLE_PNG = b"\x89PNG-pole-icon"
HH_HI_PNG = b"\x89PNG-hh-highlight"

DOC = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><name>Hand built</name>
 <Style id="pole"><IconStyle><scale>1.4</scale><Icon><href>icons/pole.png</href></Icon></IconStyle><LabelStyle><scale>0.8</scale></LabelStyle></Style>
 <StyleMap id="hhmap"><Pair><key>normal</key><styleUrl>#hhn</styleUrl></Pair><Pair><key>highlight</key><styleUrl>#hhh</styleUrl></Pair></StyleMap>
 <Style id="hhn"><IconStyle><Icon><href>http://example.com/hh.png</href></Icon></IconStyle></Style>
 <Style id="hhh"><IconStyle><scale>1.2</scale><Icon><href>icons/hh_hi.png</href></Icon></IconStyle></Style>
 <Style id="duct"><LineStyle><color>ff00a5ff</color><width>5</width></LineStyle></Style>
 <Folder><name>Poles</name>
  <Placemark><name>Pole West</name><description><![CDATA[<b>Owner</b>: <i>Co-op</i>]]></description><styleUrl>#pole</styleUrl>
   <Point><coordinates>-96.0100,29.0,0</coordinates></Point></Placemark>
  <Placemark><name>Pole East</name><styleUrl>#pole</styleUrl><Point><coordinates>-95.9900,29.0,0</coordinates></Point></Placemark>
 </Folder>
 <Folder><name>Handholes</name>
  <Placemark><name>HH-1</name><styleUrl>#hhmap</styleUrl><Point><coordinates>-96.0050,29.0001,0</coordinates></Point></Placemark>
  <Placemark><name>HH-2</name><visibility>0</visibility><styleUrl>#hhmap</styleUrl><Point><coordinates>-95.9950,29.0001,0</coordinates></Point></Placemark>
 </Folder>
 <Folder><name>Route</name><Folder><name>Phase 1</name>
  <Placemark><name>Main run</name><description><![CDATA[Length: 4000 ft<br>Duct_Size: 2"]]></description><styleUrl>#duct</styleUrl>
   <LineString><coordinates>-96.0100,29.0,0 -95.9900,29.0,0</coordinates></LineString></Placemark>
  <Placemark><name>Hand drawn</name><Style><LineStyle><color>ff0000ff</color><width>9</width></LineStyle></Style>
   <LineString><coordinates>-96.0100,29.0005,0 -95.9900,29.0005,0</coordinates></LineString></Placemark>
 </Folder></Folder>
</Document></kml>"""


def make_kmz(doc=DOC, files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", doc)
        for name, data in (files or {"icons/pole.png": POLE_PNG, "icons/hh_hi.png": HH_HI_PNG}).items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture(scope="module")
def feats():
    return parse_bytes(make_kmz(), "build.kmz").features


@pytest.fixture(scope="module")
def split(feats):
    return cs.split_contractors(feats, fixed_angle=0.0, difficulty=cs.Difficulty(auto=False))


def by_name(fs, n):
    return next(f for f in fs if f.name == n)


def test_parser_captures_original_styling(feats):
    pole = by_name(feats, "Pole West")
    assert pole.style_url == "#pole" and "icons/pole.png" in pole.style_defs["pole"]
    assert "<scale>1.4</scale>" in pole.style_defs["pole"]
    assert by_name(feats, "Hand drawn").style_xml.startswith("<Style")
    assert by_name(feats, "HH-2").visibility == "0"
    assert pole.resources["icons/pole.png"] == POLE_PNG
    assert by_name(feats, "Main run").folders == ("Route", "Phase 1")


def test_contractor_files_keep_icons_and_ship_the_images(split):
    both = {}
    for who, fs in ((split.names[0], split.a), (split.names[1], split.b)):
        data, ext = cs.contractor_file(fs, who, "proj")
        assert ext == ".kmz"                       # local icons exist, so a bare .kml would lose them
        z = zipfile.ZipFile(io.BytesIO(data))
        assert z.read("icons/pole.png") == POLE_PNG and z.read("icons/hh_hi.png") == HH_HI_PNG
        assert "doc.kml" in z.namelist()
        both[who] = parse_bytes(data, "out.kmz").features
    all_out = both[split.names[0]] + both[split.names[1]]
    pole = by_name(all_out, "Pole West") if any(f.name == "Pole West" for f in all_out) else None
    assert pole is not None and pole.style_url == "#pole"
    assert "<scale>1.4</scale>" in pole.style_defs["pole"] and "icons/pole.png" in pole.style_defs["pole"]
    assert "<scale>0.8</scale>" in pole.style_defs["pole"]                     # label style kept
    hh = by_name(all_out, "HH-1")
    assert "hhn" in hh.style_defs["hhmap"] and "<key>highlight</key>" in hh.style_defs["hhmap"]   # StyleMap + pairs
    assert "http://example.com/hh.png" in hh.style_defs["hhn"]                 # online icons stay as they were
    run = [f for f in all_out if f.name == "Main run"]
    assert run and all(f.style_url == "#duct" and "<width>5</width>" in f.style_defs["duct"] for f in run)
    hand = [f for f in all_out if f.name == "Hand drawn"]
    assert hand and all("<width>9</width>" in f.style_xml for f in hand)       # the inline style survives too


def test_folders_descriptions_visibility_survive(split):
    data, _ = cs.contractor_file(split.a + split.b, "all", "proj")
    out = parse_bytes(data, "o.kmz").features
    assert by_name(out, "Pole West").folders == ("Poles",)
    assert by_name(out, "HH-1").folders == ("Handholes",)
    assert [f for f in out if f.name == "Main run"][0].folders == ("Route", "Phase 1")
    assert "<b>Owner</b>" in by_name(out, "Pole West").raw_description      # their formatting, not a generated table
    assert by_name(out, "HH-2").visibility == "0"
    cut_run = [f for f in out if f.name == "Main run"]
    assert any("Cut at contractor boundary" in f.raw_description for f in cut_run)


def test_recolor_is_opt_in_and_replaces_icons_only_when_asked(split):
    kept, ext = cs.overview_file(split, "proj")
    kept_features = parse_bytes(kept, "k" + ext).features
    assert by_name(kept_features, "Pole West").style_url == "#pole"
    recolored, ext2 = cs.overview_file(split, "proj", recolor=True)
    assert all(f.style_url.startswith("#grp") for f in parse_bytes(recolored, "r" + ext2).features
               if f.name != "Contractor boundary")


def test_plain_kml_without_packed_icons_stays_plain_kml():
    doc = DOC.replace(b"icons/pole.png", b"http://example.com/pole.png").replace(b"icons/hh_hi.png", b"http://example.com/h.png")
    fs = parse_bytes(doc, "plain.kml").features
    data, ext = geoexport.to_kml_package([("x", fs, None)], "x", wrap_groups=False)
    assert ext == ".kml"
    assert "http://example.com/pole.png" in data.decode()


def test_same_style_id_from_two_files_keeps_both_definitions():
    a = parse_bytes(make_kmz(), "a.kmz").features
    other = DOC.replace(b"<scale>1.4</scale>", b"<scale>3.0</scale>").replace(b"icons/pole.png", b"icons/other.png")
    b = parse_bytes(make_kmz(other, {"icons/other.png": b"\x89PNG-other", "icons/hh_hi.png": HH_HI_PNG}), "b.kmz").features
    data, ext = geoexport.to_kml_package([("m", [by_name(a, "Pole West"), by_name(b, "Pole West")], None)], "m",
                                         wrap_groups=False)
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.read("icons/pole.png") == POLE_PNG and z.read("icons/other.png") == b"\x89PNG-other"
    out = parse_bytes(data, "m.kmz").features
    scales = {("<scale>1.4</scale>" in f.style_defs[f.style_url[1:]], "<scale>3.0</scale>" in f.style_defs[f.style_url[1:]])
              for f in out}
    assert scales == {(True, False), (False, True)}  # each placemark still points at its own file's style


def test_clean_kml_export_keeps_styles_too(feats):
    data, ext = geoexport.to_kml_package([("all", feats, None)], "all", wrap_groups=False)
    assert ext == ".kmz"
    out = parse_bytes(data, "c.kmz").features
    assert len(out) == len(feats) and by_name(out, "Pole East").style_url == "#pole"


def test_latitude_and_longitude_stay_in_every_placemark(split):
    import re
    data, _ = cs.contractor_file(split.a + split.b, "all", "proj")
    kml = zipfile.ZipFile(io.BytesIO(data)).read("doc.kml").decode()
    for name, lat, lon in (("Pole West", "29.0", "-96.01"), ("HH-1", "29.0001", "-96.005")):
        pm = re.search(rf"<Placemark><name>{re.escape(name)}</name>.*?</Placemark>", kml, re.S).group(0)
        assert f'<Data name="latitude"><value>{lat}</value>' in pm and f'<Data name="longitude"><value>{lon}</value>' in pm
    # a placemark without a description of its own shows them in the popup table as well
    hh = re.search(r"<Placemark><name>HH-1</name>.*?</Placemark>", kml, re.S).group(0)
    assert "<b>latitude</b>" in hh and "<b>longitude</b>" in hh

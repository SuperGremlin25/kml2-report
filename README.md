---
title: KML2 Report - Construction GIS
emoji: 🗺️
colorFrom: blue
colorTo: indigo
sdk: streamlit
sdk_version: 1.66.0
app_file: streamlit_app.py
pinned: false
license: mit
---

# KML2 Report — construction GIS for KML / KMZ

Pull the data out of Google Earth files, measure it, plan the build, and hand each contractor a clean file with
**their** work in it — icons, colors and folders exactly as you built them.

Built for fiber / underground construction: duct footage, handholes, poles, stationing, as-built checks and crew
assignments. Everything runs in the browser app (Streamlit) or from the command line.

---

## What it does

### Read anything Google Earth can save
- KML and KMZ (detected by content), KML 2.0–2.2, nested folders, multiple files at once
- Points, lines, polygons **with holes**, `gx:Track`, multi-geometries, `ExtendedData`, styles, time stamps
- Messy real-world files: coordinates like `-96.0, 29.0, 0`, bad tuples and undefined namespaces are skipped with a
  warning, never fatal

### GIS Workbench (the first tab)
| | |
|---|---|
| **Map** | Layers colored by folder, file style or geometry type; search; labels; length/area tooltips |
| **Attribute table** | Every placemark with measured length and area, plus a **vertex table** (every coordinate with station and optional State Plane / UTM X-Y) |
| **Statistics** | Per-folder totals; group by any attribute (a pivot table) |
| **Analysis** | Buffers, points-in-polygons, nearest features, elevation profile |
| **QA / QC** | Duplicate geometry, near-zero length, self-intersections, dangling ends, attribute-vs-measured length mismatch, invalid polygons |
| **Prints (PDF)** | Upload the construction print set; each sheet is placed on the map from its printed pole lat/long, footage callouts are read, typo'd coordinates are flagged, and print footage is compared with KML footage |
| **Contractor Split** | Divide the build between two contractors, all at once or **in releases** (see below) |
| **Export** | GeoJSON, KML/KMZ (your styling intact), Shapefile (with `.prj`), AutoCAD DXF (layers = your folders, in feet) |

### Contractor split — fair work, not just equal footage
Divide a build between two contractors so each gets matched **footage, duct, handholes and assets** *and* a fair
share of the **hard work**:

- **Auto-pick:** searches straight cuts at every angle and scores each on total footage, duct-feet (footage × ducts
  in the package), footage per duct size, handholes, other assets, and difficulty. Lines crossing the boundary are
  cut exactly there, so each contractor's footage is exact.
- **Not all feet are equal:** long straight runs are easy; dense city work (many turns, short blocks, many
  handholes) is not. Dense areas are detected from the file itself and count up to 2× (adjustable); you can also tag
  folders you know are hard (city, rock, river crossing). The split balances the hard work too, so one contractor
  doesn't get all the open runs while the other gets the city. A map shows what counts as difficult.
- **Releases — a set footage at a time:** instead of handing out everything, give **each** contractor e.g. 5,000 ft per
  release. Each release is picked with the same auto-balancing, release 2 starts where release 1 stopped, and
  planning more releases never changes the earlier ones. Choose which end to start from.
- **Output:** a clean KML (or KMZ) per contractor per release, each contractor's running total, the work not yet
  released, an overview with the boundary, and a summary CSV — in one zip.

### Your styling survives export
Contractor files, overviews and the "KML (clean)" export reproduce each placemark exactly as you built it: icon,
scale, label style, StyleMaps (normal / highlight), inline styles, visibility, folder nesting and your own
description. If your icons are images packed inside a KMZ, the export is a **`.kmz` with those images included**.
Online icons are kept as references. Nothing is recolored unless you tick *Recolor the overview by contractor*
(overview only). Latitude and longitude stay in every placemark.

### Quantity takeoff
**Geometry Analysis** tab: duct/cable footage by size, multi-duct packages, aerial vs underground, point assets
(poles, handholes, cabinets, splice cases), connected runs, and a check of measured length against the
attribute length. Export to CSV / Excel.

### Field reports (the original tool)
Location-aware **field-notes summary** (roads, towns, bearings, corridors, speech-to-text cleanup),
an LLM-written **formal report** (optional, Hugging Face Inference API) and the **KML Note Injector** that
writes field notes into a design file. Download as CSV, JSON, Markdown or Word.

---

## Quick start

**Web app**

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Upload one or more `.kml` / `.kmz` files in the sidebar and start in the **GIS Workbench**.

**Command line**

```bash
pip install -e .
kml2report parse  project.kmz -o rows.csv        # extract attributes + geometry
kml2report summarize project.kmz -o notes.md     # field-notes summary
kml2report report notes.md                       # formal report (needs an LLM key)
kml2report inject --help                         # write notes into a design KML
kml2report ui                                    # launch the web app
```

**Tests**

```bash
pip install pytest && PYTHONPATH=src python -m pytest
```

---

## Deploying

Every push to `main` is synced to the Hugging Face Space by `.github/workflows/sync-to-hf.yml` (needs the
repository secret `HF_TOKEN` with write access to the Space). This file's YAML header sets the Space's SDK and
app. To create a Space by hand or troubleshoot a build, see
[`docs/HUGGING_FACE_DEPLOYMENT.md`](docs/HUGGING_FACE_DEPLOYMENT.md).

---

## Project layout

| Path | Purpose |
|------|---------|
| `streamlit_app.py` | Web app entry point and landing page |
| `src/kml2report/kml_parser.py` | Fault-tolerant KML/KMZ parser (keeps original styling, icons, folders) |
| `src/kml2report/models.py` | `Feature` / `Geometry`, geodesic length and area |
| `src/kml2report/gis.py` | Attribute and vertex tables, reprojection, buffers, spatial join, QA/QC |
| `src/kml2report/geoexport.py` | GeoJSON, KML/KMZ, Shapefile, DXF writers |
| `src/kml2report/contractor_split.py` | Contractor split, difficulty model, releases, per-contractor exports |
| `src/kml2report/workbench.py` | The GIS Workbench tab |
| `src/kml2report/geometry_analyzer.py` | Duct / cable footage takeoff |
| `src/kml2report/summarizer.py`, `geocoder.py`, `llm_report.py` | Field-notes summary and formal report |
| `src/kml2report/injector.py` | KML note injector |
| `src/kml2report/cli.py`, `pipeline.py`, `export.py`, `config.py` | Command line and batch pipeline |
| `tests/` | Unit tests |
| `docs/` | User guide and Hugging Face deployment guide |

---

## Notes and limits

- **Balance is best-effort.** One straight cut cannot equalize every quantity at once, so the summary table shows
  exactly what each contractor gets. Dense-area detection is a proxy (turns, short blocks, assets); tagging a folder as
  difficult is more reliable when you know where the hard work is.
- Files are processed on the server for the session and are not stored permanently.
- Geocoding for the field-notes summary uses OpenStreetMap Nominatim, which is rate-limited on first use.

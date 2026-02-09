# kml2report

A minimal, local-first pipeline that extracts structured, report-ready data from KML and KMZ files. No ArcGIS, QGIS, or commercial GIS tooling required.

## Features

- **KML & KMZ support** — handles both formats; KMZ files are unzipped automatically
- **Multiple description formats** — parses `KEY=VALUE;` strings, HTML tables, and plain text
- **Configurable field mapping** — YAML config maps raw keys to canonical column names
- **CSV & JSON export** — flat CSV or layer-grouped JSON, ready for Excel / BI / reporting
- **CLI interface** — glob patterns, preview mode, config file support
- **Web UI** — lightweight FastAPI app for upload, preview, and download

## Quick Start

```bash
# 1. Navigate to the project
cd kml2report

# 2. Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# 3. Install dependencies
pip install -e .

# 4. Parse a KML file
kml2report parse ../geospatial/Sheridan_Pole_HLD_V2.kml --out report.csv --preview
```

### Install with web UI support

```bash
pip install -e ".[web]"
kml2report serve
# Open http://127.0.0.1:8000
```

## CLI Usage

```
kml2report parse <FILES...> [OPTIONS]

Options:
  -c, --config PATH    YAML config file (see config/default-config.yml)
  -o, --out PATH       Output file (.csv or .json)
  -f, --format [csv|json]  Override output format
  -p, --preview        Print first 10 rows to stdout

Examples:
  kml2report parse input.kml --out report.csv
  kml2report parse *.kml --config config/default-config.yml --out data.json -f json
  kml2report parse file1.kml file2.kmz --out merged.csv
  kml2report parse myfile.kml --preview
```

## Configuration

Edit `config/default-config.yml` to customize:

### Field mapping

Maps raw description keys to output column names (case-insensitive):

```yaml
field_map:
  ID: id
  TYPE: type
  PRIORITY: priority
  PRTY: priority          # alias
  Pole_Type: pole_type
```

### Default values

Fill missing fields:

```yaml
defaults:
  priority: UNKNOWN
  status: UNKNOWN
```

### Filters

Export only matching features:

```yaml
filters:
  - field: type
    operator: eq
    value: SITE
  - field: priority
    operator: in
    value: [HIGH, CRITICAL]
```

Supported operators: `eq`, `ne`, `in`, `contains`

## Output Columns

| Column | Description |
|--------|-------------|
| `feature_id` | KML Placemark ID |
| `name` | Placemark name |
| `layer` | Parent Folder name |
| `source_file` | Input filename |
| `id`, `type`, `priority`, `status`, `notes` | Parsed from description |
| `geometry_type` | Point, LineString, Polygon |
| `latitude`, `longitude` | For point features |
| `wkt` | Well-Known Text geometry |
| `raw_description` | Original description text |

## Project Structure

```
kml2report/
  src/kml2report/
    __init__.py          # Package init
    models.py            # Feature & Geometry dataclasses
    kml_parser.py        # KML/KMZ file parsing
    description_parser.py # Description normalization
    config.py            # YAML config loading
    pipeline.py          # Orchestration layer
    export.py            # CSV/JSON export
    cli.py               # Click CLI
    web.py               # FastAPI web UI
    templates/
      index.html         # Web UI template
  config/
    default-config.yml   # Example configuration
  tests/
    fixtures/            # Sample KML files
    test_parsing.py      # Test suite
  pyproject.toml
  requirements.txt
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Description Format Support

The parser handles three formats automatically:

1. **KEY=VALUE pairs**: `ID=OBJ-001; TYPE=SITE; PRIORITY=HIGH`
2. **HTML tables**: `<tr><td>Pole_Type</td><td>Distribution</td></tr>`
3. **Plain text**: Falls back to storing as `notes`

Resilient to missing fields, extra whitespace, and minor formatting inconsistencies.

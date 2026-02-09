---
title: KML Field Report Generator
emoji: 🗺️
colorFrom: blue
colorTo: indigo
sdk: streamlit
sdk_version: 1.41.0
app_file: streamlit_app.py
pinned: false
license: mit
---

# KML Field Report Generator — Complete Hugging Face Spaces Deployment Guide

Upload KML/KMZ files from field surveys and generate:

- **Parsed data tables** — all features with attributes, coordinates, geometry
- **Location-intelligent field notes summary** — grouped by theme and geographic corridor with geocoded roads, towns, bearings, and distances
- **Talk-to-text auto-correction** — common speech-to-text errors are cleaned automatically
- **Formal professional report** — LLM-generated CRO/HLD field assessment via Hugging Face Inference API

---

## Prerequisites

Before you start, make sure you have:

1. **A Hugging Face account** — sign up free at [huggingface.co/join](https://huggingface.co/join)
2. **A Hugging Face access token** — generate one at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) (needed for the LLM formal report feature; the basic parsing/summary features work without it)
3. **Git installed** on your machine — [git-scm.com/downloads](https://git-scm.com/downloads)
4. **Python 3.9+** installed locally (only needed if you want to test locally first)

---

## Step 1: Create the Hugging Face Space

1. Go to [huggingface.co/new-space](https://huggingface.co/new-space)
2. Fill in the form:
   - **Owner:** your username or organization
   - **Space name:** `kml-field-report-generator` (or whatever you prefer)
   - **License:** MIT
   - **SDK:** select **Streamlit**
   - **Visibility:** Private (recommended for proprietary field data) or Public
3. Click **Create Space**

Hugging Face will show you a page with your new empty Space and its git URL. It will look like:

```
https://huggingface.co/spaces/YOUR_USERNAME/kml-field-report-generator
```

---

## Step 2: Clone the Space Locally

Open a terminal and clone the empty Space repo:

```bash
# Install the HF CLI if you haven't already
pip install huggingface_hub[cli]

# Log in (paste your HF token when prompted)
huggingface-cli login

# Clone your new Space
git clone https://huggingface.co/spaces/YOUR_USERNAME/kml-field-report-generator
cd kml-field-report-generator
```

Replace `YOUR_USERNAME` with your actual Hugging Face username.

---

## Step 3: Copy the App Files into the Space

From your local `kml2report` project directory, copy these files and folders into the cloned Space directory:

```
kml-field-report-generator/          <-- your cloned HF Space
├── .gitignore
├── README.md                        <-- copy hf-space-readme.md as README.md
├── requirements.txt
├── pyproject.toml
├── streamlit_app.py
├── config/
│   └── default.yaml
└── src/
    └── kml2report/
        ├── __init__.py
        ├── cli.py
        ├── config.py
        ├── description_parser.py
        ├── export.py
        ├── geocoder.py
        ├── kml_parser.py
        ├── llm_report.py
        ├── models.py
        ├── pipeline.py
        ├── summarizer.py
        └── web.py
```

### On Windows (PowerShell):

```powershell
$SRC = "C:\path\to\kml2report"
$DST = "C:\path\to\kml-field-report-generator"

# Core files
Copy-Item "$SRC\streamlit_app.py"  "$DST\"
Copy-Item "$SRC\requirements.txt"  "$DST\"
Copy-Item "$SRC\pyproject.toml"    "$DST\"
Copy-Item "$SRC\.gitignore"        "$DST\"

# Use the HF Space readme as the Space's README.md
Copy-Item "$SRC\hf-space-readme.md" "$DST\README.md"

# Source package
Copy-Item "$SRC\src" "$DST\src" -Recurse -Exclude "__pycache__","*.pyc","*.egg-info"

# Config
Copy-Item "$SRC\config" "$DST\config" -Recurse
```

### On macOS/Linux:

```bash
SRC=~/CascadeProjects/hld-cro-tools/kml2report
DST=~/kml-field-report-generator

cp "$SRC/streamlit_app.py" "$DST/"
cp "$SRC/requirements.txt" "$DST/"
cp "$SRC/pyproject.toml"   "$DST/"
cp "$SRC/.gitignore"       "$DST/"
cp "$SRC/hf-space-readme.md" "$DST/README.md"

rsync -av --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
  "$SRC/src/" "$DST/src/"

cp -r "$SRC/config/" "$DST/config/"
```

### Important: Do NOT copy these:

- `.venv/` — the virtual environment (HF Spaces builds its own)
- `.geocache.json` — local geocode cache (will be rebuilt on the Space)
- `output/` — local report output files
- `tests/` — not needed in production (optional to include)
- `__pycache__/` or `*.egg-info/` — build artifacts

---

## Step 4: Add Secrets (Environment Variables)

1. Go to your Space's page: `https://huggingface.co/spaces/YOUR_USERNAME/kml-field-report-generator`
2. Click **Settings** (gear icon)
3. Scroll to **Repository secrets**
4. Add the following secrets:

| Secret Name | Required? | Description |
|-------------|-----------|-------------|
| `HF_TOKEN` | For LLM reports | Your Hugging Face access token (included with HF Pro). Enables the formal report generation via Llama 3.3 70B. |
| `OPENAI_API_KEY` | Optional | Only if you want to use GPT-4o instead of Hugging Face for formal reports |
| `ANTHROPIC_API_KEY` | Optional | Only if you want to use Claude instead of Hugging Face for formal reports |

**Note:** The core features (KML parsing, field notes summary, geocoding, talk-to-text correction, CSV/JSON export) work with **zero secrets**. You only need `HF_TOKEN` for the LLM-powered formal report tab.

---

## Step 5: Push to Hugging Face

From inside the cloned Space directory:

```bash
cd kml-field-report-generator

git add .
git commit -m "Initial deploy: KML Field Report Generator"
git push
```

Hugging Face will automatically:
1. Detect the Streamlit SDK from the `README.md` frontmatter
2. Install dependencies from `requirements.txt`
3. Install the `kml2report` package from `pyproject.toml` (via `pip install -e .`)
4. Launch `streamlit_app.py`

---

## Step 6: Wait for Build & Verify

1. Go to your Space URL: `https://huggingface.co/spaces/YOUR_USERNAME/kml-field-report-generator`
2. You'll see a **Building** status — this takes 2-5 minutes on first deploy
3. Once it says **Running**, the app is live
4. If the build fails, click **Logs** to see the error output

### First-time verification checklist:

- [ ] Upload a KML file — the **Data Preview** tab should show a table of features
- [ ] Switch to **Field Notes Summary** tab — should show geocoded corridors with road names and towns (first run will be slow as it geocodes via OpenStreetMap; subsequent uploads of the same area will be faster)
- [ ] Switch to **Formal Report** tab — select Hugging Face provider, click Generate (requires `HF_TOKEN` secret)
- [ ] Switch to **Downloads** tab — verify all download buttons work (CSV, JSON, Summary .md, Formal Report .md)

---

## Updating the App

When you make changes locally, just copy the updated files and push:

```bash
cd kml-field-report-generator

# Copy changed files from your project...
# Then:
git add .
git commit -m "Update: description of changes"
git push
```

The Space will automatically rebuild and redeploy.

---

## Troubleshooting

### Build fails with "ModuleNotFoundError"

Make sure `requirements.txt` includes all dependencies. The current file should contain:

```
lxml>=4.9
click>=8.1
pyyaml>=6.0
streamlit>=1.41.0
pandas>=2.0
huggingface_hub>=0.20
requests>=2.31
```

Also ensure `pyproject.toml` is present — Streamlit Spaces will run `pip install -e .` to install the `kml2report` package.

### "No field notes found" for a valid KML

The summarizer requires features with description text of 3+ characters. If your KML uses a non-standard description format, check the **Data Preview** tab to see if the `notes` column is populated.

### Geocoding is slow on first upload

The app uses OpenStreetMap Nominatim (free, no API key needed) with a 1 request/second rate limit. A file with 100 unique locations takes ~100 seconds on first run. Results are cached in memory for the session. The `.geocache.json` file is written to disk but may not persist across Space restarts on the free tier.

### LLM formal report fails

- Verify `HF_TOKEN` is set in Space Settings → Secrets
- The default model is `meta-llama/Llama-3.3-70B-Instruct` — this requires a Hugging Face Pro subscription or access to the model
- You can also enter an OpenAI or Anthropic key directly in the app UI

### Space goes to sleep (free tier)

Free-tier Spaces sleep after ~48 hours of inactivity. They wake up automatically when someone visits the URL (takes ~30 seconds). To keep it always on, upgrade to a **persistent** Space in Settings.

---

## File Reference

| File | Purpose |
|------|---------|
| `README.md` | HF Space metadata (SDK, title) + this guide |
| `requirements.txt` | Python dependencies for HF Spaces build |
| `pyproject.toml` | Package definition for `pip install -e .` |
| `streamlit_app.py` | Main Streamlit UI — the app entry point |
| `src/kml2report/kml_parser.py` | KML/KMZ file parsing |
| `src/kml2report/description_parser.py` | Feature description extraction |
| `src/kml2report/models.py` | Data models (Feature, Geometry) |
| `src/kml2report/pipeline.py` | End-to-end parsing pipeline |
| `src/kml2report/export.py` | CSV/JSON export |
| `src/kml2report/summarizer.py` | Field notes classification, clustering, geocoding, talk-to-text correction, Markdown report generation |
| `src/kml2report/geocoder.py` | OpenStreetMap Nominatim reverse geocoding with caching |
| `src/kml2report/llm_report.py` | LLM-powered formal report generation |
| `src/kml2report/cli.py` | CLI interface (not used by the Space, but included) |
| `src/kml2report/config.py` | Configuration loading |
| `config/default.yaml` | Default configuration |

---

## Architecture Overview

```
User uploads KML/KMZ
        │
        ▼
┌─────────────────┐
│   kml_parser.py  │  Parse XML → Feature objects
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  summarizer.py   │  Classify → Clean talk-to-text → Geocode → Cluster → Markdown
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  llm_report.py   │  (Optional) Send summary to LLM → Formal report
└────────┬────────┘
         │
         ▼
   Streamlit UI: Preview / Summary / Report / Downloads
```

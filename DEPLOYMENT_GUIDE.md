# Hugging Face Spaces Deployment Guide

## Updated with Geometry Analysis Feature

### Quick Summary

Your app is already deployed at: `https://supergremlin25-kml2-report.hf.space`

To update it with the new geometry analysis feature, follow these steps.

---

## Method 1: Git Push (Recommended)

### Step 1: Commit Your Changes
```bash
# Navigate to your project directory
cd C:\Users\blake\CascadeProjects\hld-cro-tools\kml2report

# Check git status
git status

# Add all new files
git add .
git add src/kml2report/geometry_analyzer.py
git add src/kml2report/models_extensions.py
git add tests/test_geometry_analyzer.py

# Commit changes
git commit -m "Add geometry analysis feature for duct length aggregation"
```

### Step 2: Push to Hugging Face
```bash
# Your HF Space is already linked as a remote
# Push to deploy
git push origin main

# Or if you have a specific HF remote:
git push hf main
```

That's it! Hugging Face will automatically rebuild and deploy your app.

---

## Method 2: Manual Upload (If Git Push Fails)

### Step 1: Prepare Files
Create a deployment folder:
```bash
mkdir C:\temp\kml2report-deploy
cd C:\temp\kml2report-deploy
```

### Step 2: Copy Required Files
Copy these files from your project to the deploy folder:
- `streamlit_app.py`
- `requirements.txt`
- `pyproject.toml`
- `README.md`
- `src/` folder (entire folder)
- `config/` folder (if it exists)

### Step 3: Upload via Hugging Face UI
1. Go to your Space: https://huggingface.co/spaces/supergremlin25/kml2-report
2. Click on "Files and versions" tab
3. Click "Upload files"
4. Drag and drop all the files
5. Commit changes with a message like "Add geometry analysis"

---

## What's New in This Update

### New Features:
1. **Geometry Analysis Tab** - Analyze duct lengths and types
2. **HTML Description Parsing** - Extract duct sizes from HTML tables
3. **Project Aggregation** - Total footage by duct size (2", 3", 4")
4. **Connected Run Detection** - Find connected duct segments
5. **Length Validation** - Compare calculated vs pre-calculated lengths
6. **CSV Export** - Download detailed geometry data

### New Dependencies Added:
- `beautifulsoup4>=4.11.1` - For HTML parsing
- `pyproj>=3.6.0` - For accurate distance calculations
- `geopy>=2.4.0` - For geodetic calculations

---

## Troubleshooting

### If the app fails to load:
1. Check the "Logs" tab in your Hugging Face Space
2. Common issues:
   - Missing dependencies (check requirements.txt)
   - Import errors (check file paths)
   - Memory issues (KMZ files too large)

### If geometry analysis doesn't work:
1. Make sure your KMZ has the expected HTML table format
2. Check that duct sizes are in the `<name>` field
3. Verify `New_Length` is present in description tables

### To rollback quickly:
```bash
# Go back to previous commit
git checkout HEAD~1
git push origin main --force
```

---

## Testing Locally First (Optional)

```bash
# Create virtual environment
python -m venv venv
venv\Scripts\activate

# Install with geometry dependencies
pip install -e .[geometry]

# Run locally
streamlit run streamlit_app.py
```

---

## Deployment Checklist

- [ ] All new files committed to git
- [ ] requirements.txt updated with new dependencies
- [ ] Geometry analyzer module added
- [ ] Streamlit app updated with new tab
- [ ] Tests pass locally
- [ ] Push to Hugging Face
- [ ] Verify deployment works on HF Space

---

## After Deployment

1. Visit your Space: https://supergremlin25-kml2-report.hf.space
2. Upload your KMZ file with duct geometries
3. Click the "📐 Geometry Analysis" tab
4. Click "🔍 Analyze Geometry"
5. You should see:
   - Total segments analyzed
   - Breakdown by duct size (2", 3", 4", etc.)
   - Total footage calculations

If anything doesn't work, check the logs in your Hugging Face Space for error details.

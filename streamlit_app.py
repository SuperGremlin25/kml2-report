"""Streamlit dashboard for kml2report.

Upload one or more KML/KMZ files -> preview parsed data -> generate
field-notes summary (optionally filtered by town) -> download as
Markdown or Word (.docx).

Deploy to Hugging Face Spaces with sdk: streamlit.
Version: 2.0 - Multi-duct package analysis with Excel export
"""

from __future__ import annotations

import io
import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

# Ensure the local src/ package is importable on HF Spaces (where the
# package is NOT pip-installed).
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="KML Field Report Generator",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOMENCLATURE_NOTE = (
    "\n\n## Utility Nomenclature Note\n\n"
    'Poles labeled **"EXO"** and **"SBEC"** in this report refer to the same '
    "cooperative utility: **San Bernard Electric Cooperative (SBEC)**, operating "
    "under the **EXO** parent brand. Field engineers used both names "
    "interchangeably during survey. Near Columbus proper (e.g., Montezuma "
    "Street), some notes explicitly distinguish between SBEC and EXO-branded "
    "poles on opposite sides of the street; those distinctions are preserved "
    "as recorded.\n"
)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("🗺️ KML Field Report Generator")
st.sidebar.markdown(
    "Upload one or more KML / KMZ files to parse field data, generate a "
    "location-intelligent summary, and download reports."
)

uploaded_files = st.sidebar.file_uploader(
    "Upload KML / KMZ",
    type=["kml", "kmz"],
    accept_multiple_files=True,
    help="Drag and drop or click to browse. Multiple files supported.",
)

st.sidebar.markdown("---")

town_config_file = st.sidebar.file_uploader(
    "Town config (optional)",
    type=["json"],
    help="Upload a town-config.json to define custom town centers for filtering.",
)

town_centers: list[tuple[str, float, float]] | None = None
if town_config_file is not None:
    try:
        data = json.loads(town_config_file.getvalue().decode("utf-8"))
        # Accept both {"towns": [...]} and bare [...]
        towns_list = data.get("towns", data) if isinstance(data, dict) else data
        if not isinstance(towns_list, list):
            raise ValueError("Expected a list of towns")
        town_centers = [
            (t["name"], float(t["latitude"]), float(t["longitude"]))
            for t in towns_list
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
        st.sidebar.error(f"Invalid town-config.json — {exc}")

town_names: list[str] = []
if town_centers:
    town_names = [name for name, _, _ in town_centers]

town_filter: str | None = None
if town_names:
    town_choice = st.sidebar.selectbox(
        "Filter by town",
        options=["All towns"] + town_names,
        index=0,
    )
    if town_choice != "All towns":
        town_filter = town_choice

inject_nomenclature = st.sidebar.checkbox(
    "Add SBEC/EXO nomenclature note",
    value=True,
    help="Inject the utility nomenclature note into the generated report.",
)

st.sidebar.markdown("---")
st.sidebar.markdown(
    "**Workflow:**\n"
    "1. Upload KML/KMZ file(s)\n"
    "2. *(Optional)* Upload town-config.json & pick a town\n"
    "3. Preview parsed features\n"
    "4. Generate field-notes summary\n"
    "5. Download Markdown or Word (.docx)\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Parsing KML data...")
def parse_uploaded_files(file_data: list[tuple[bytes, str]]) -> list[dict]:
    """Parse uploaded file bytes through the kml2report pipeline."""
    from kml2report.pipeline import run_pipeline

    tmp_paths: list[Path] = []
    try:
        for fbytes, fname in file_data:
            suffix = Path(fname).suffix.lower()
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(fbytes)
            tmp.close()
            tmp_paths.append(Path(tmp.name))
        rows = run_pipeline(input_paths=tmp_paths)
    finally:
        for p in tmp_paths:
            p.unlink(missing_ok=True)
    return rows


@st.cache_data(show_spinner="Parsing features...")
def parse_features_multi(file_data: list[tuple[bytes, str]]):
    """Parse uploaded files into Feature objects for the summarizer."""
    from kml2report.kml_parser import parse_file

    all_features: list[Any] = []
    for fbytes, fname in file_data:
        suffix = Path(fname).suffix.lower()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(fbytes)
            tmp_path = Path(tmp.name)
        try:
            all_features.extend(parse_file(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)
    return all_features


def generate_summary(
    file_data: list[tuple[bytes, str]],
    town_filter_val: str | None,
    town_centers_val: list[tuple[str, float, float]] | None,
    add_nomenclature: bool,
) -> str:
    """Generate the location-intelligent Markdown summary with geocoding."""
    from kml2report.summarizer import generate_summary_report

    features = parse_features_multi(file_data)
    names = [fn for _, fn in file_data]
    doc_name = Path(names[0]).stem if len(names) == 1 else "combined"

    progress_bar = st.progress(0, text="Reverse-geocoding coordinates via OpenStreetMap...")

    def _progress(done: int, total: int) -> None:
        if total > 0:
            progress_bar.progress(done / total, text=f"Geocoding: {done}/{total} locations...")

    result = generate_summary_report(
        features,
        doc_name=doc_name,
        geocode=True,
        progress_callback=_progress,
        town_filter=town_filter_val,
        town_centers=town_centers_val,
    )
    progress_bar.empty()

    if add_nomenclature and result and "No field notes found" not in result:
        insert_after = "## How to Use This Report"
        idx = result.find(insert_after)
        if idx != -1:
            section_end = result.find("\n## ", idx + len(insert_after))
            if section_end == -1:
                section_end = len(result)
            result = result[:section_end] + _NOMENCLATURE_NOTE + result[section_end:]

    return result


def md_to_docx_bytes(md_text: str) -> bytes:
    """Convert Markdown text to a .docx file in memory using python-docx."""
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    style = doc.styles["Normal"]
    style.font.size = Pt(10)
    style.font.name = "Calibri"

    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("#### "):
            doc.add_heading(line[5:].strip(), level=4)
        elif line.startswith("|") and i + 1 < len(lines) and lines[i + 1].startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                stripped = lines[i].strip()
                if not re.match(r"^\|[\s\-:|]+\|$", stripped):
                    cells = [c.strip() for c in stripped.strip("|").split("|")]
                    table_lines.append(cells)
                i += 1
            i -= 1
            if table_lines:
                ncols = max(len(r) for r in table_lines)
                tbl = doc.add_table(rows=len(table_lines), cols=ncols, style="Light Grid Accent 1")
                for ri, row_cells in enumerate(table_lines):
                    for ci, val in enumerate(row_cells):
                        if ci < ncols:
                            cell = tbl.cell(ri, ci)
                            cell.text = _strip_md_formatting(val)
                            for paragraph in cell.paragraphs:
                                paragraph.style.font.size = Pt(8)
        elif line.startswith("- "):
            doc.add_paragraph(
                _strip_md_formatting(line[2:].strip()), style="List Bullet"
            )
        elif line.strip() == "---":
            doc.add_paragraph("")
        elif line.strip():
            doc.add_paragraph(_strip_md_formatting(line))

        i += 1

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _strip_md_formatting(text: str) -> str:
    """Remove bold/italic markdown markers from text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    return text


def rows_to_csv(rows: list[dict]) -> str:
    """Convert row dicts to CSV string."""
    if not rows:
        return ""
    all_keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=all_keys, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def rows_to_json(rows: list[dict]) -> str:
    """Convert row dicts to JSON string."""
    return json.dumps(rows, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

if not uploaded_files:
    st.title("KML Field Report Generator")
    st.markdown(
        """
        ### Get started

        Upload one or more **KML** or **KMZ** files using the sidebar to:

        - **Preview** all parsed features in a searchable table
        - **Generate** a location-intelligent field-notes summary with
          coordinates, road names, bearings, and geographic corridors
        - **Filter** by town using a portable town-config.json
        - **Download** CSV, JSON, Markdown, or Word (.docx) reports
        - **Analyze** geometry and aggregate duct/cable lengths by type

        ---

        *Professional KML/KMZ analysis tool for fiber construction projects. 
        All processing runs server-side — your files are not stored permanently.*
        """
    )
    st.stop()

# --- Files uploaded — process them ---
file_data: list[tuple[bytes, str]] = [
    (f.getvalue(), f.name) for f in uploaded_files
]
file_names = [f.name for f in uploaded_files]
stem = Path(file_names[0]).stem if len(file_names) == 1 else "combined"

tab_preview, tab_summary, tab_report, tab_download, tab_inject, tab_geometry = st.tabs(
    ["📊 Data Preview", "📝 Field Notes Summary", "📄 Formal Report", "⬇️ Downloads", "🗺️ KML Injector", "📐 Geometry Analysis"]
)

# ---------------------------------------------------------------------------
# Tab 1: Data Preview
# ---------------------------------------------------------------------------

with tab_preview:
    st.header("Parsed Features")

    rows = parse_uploaded_files(file_data)

    if not rows:
        st.warning("No features found in the uploaded file(s).")
    else:
        st.success(f"**{len(rows):,}** features parsed from **{len(file_names)}** file(s)")

        cols = list(rows[0].keys())
        display_cols = st.multiselect(
            "Columns to display",
            options=cols,
            default=[c for c in ["feature_id", "name", "layer", "type", "notes",
                                  "geometry_type", "latitude", "longitude"] if c in cols],
        )

        if display_cols:
            import pandas as pd
            df = pd.DataFrame(rows)[display_cols]
            st.dataframe(df, use_container_width=True, height=500)
        else:
            st.info("Select columns above to display.")

# ---------------------------------------------------------------------------
# Tab 2: Field Notes Summary
# ---------------------------------------------------------------------------

with tab_summary:
    st.header("Field Notes Summary")
    if town_filter:
        st.info(f"Filtered to town: **{town_filter}**")

    summary_md = generate_summary(file_data, town_filter, town_centers, inject_nomenclature)

    if not summary_md or "No field notes found" in summary_md:
        st.warning("No field notes found in the uploaded file(s).")
    else:
        st.markdown(summary_md)

# ---------------------------------------------------------------------------
# Tab 3: Formal LLM Report
# ---------------------------------------------------------------------------

with tab_report:
    st.header("Formal Report (LLM-Generated)")

    st.markdown(
        "Generate a polished, professional field assessment report from the "
        "summary above. Requires an LLM provider."
    )

    col_provider, col_model, col_temp = st.columns(3)

    with col_provider:
        provider = st.selectbox(
            "LLM Provider",
            options=["huggingface", "openai", "anthropic", "ollama"],
            index=0,
            help="Hugging Face works with your HF Pro subscription.",
        )

    default_models = {
        "huggingface": "meta-llama/Llama-3.3-70B-Instruct",
        "openai": "gpt-4o",
        "anthropic": "claude-sonnet-4-20250514",
        "ollama": "llama3",
    }

    with col_model:
        model = st.text_input(
            "Model",
            value=default_models.get(provider, ""),
            help="Model name / ID for the selected provider.",
        )

    with col_temp:
        temperature = st.slider(
            "Temperature",
            min_value=0.0,
            max_value=1.0,
            value=0.3,
            step=0.05,
            help="Lower = more deterministic.",
        )

    env_var_map = {
        "huggingface": "HF_TOKEN",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "ollama": None,
    }

    env_var = env_var_map.get(provider)
    has_key = env_var is None or bool(os.environ.get(env_var))

    if not has_key:
        api_key_input = st.text_input(
            f"Enter {env_var}",
            type="password",
            help=f"Set the {env_var} environment variable or enter it here.",
        )
        if api_key_input:
            os.environ[env_var] = api_key_input
            has_key = True

    if "formal_report" not in st.session_state:
        st.session_state.formal_report = None

    generate_btn = st.button(
        "🚀 Generate Formal Report",
        disabled=not has_key,
        type="primary",
        use_container_width=True,
    )

    if generate_btn:
        summary_md = generate_summary(file_data, town_filter, town_centers, inject_nomenclature)
        if "No field notes found" in summary_md:
            st.error("No field notes to send to LLM.")
        else:
            with st.spinner(f"Generating report via {provider} / {model}..."):
                try:
                    from kml2report.llm_report import generate_formal_report

                    formal = generate_formal_report(
                        summary_md=summary_md,
                        provider=provider,
                        model=model,
                        temperature=temperature,
                    )
                    st.session_state.formal_report = formal
                except Exception as exc:
                    st.error(f"LLM error: {exc}")

    if st.session_state.formal_report:
        st.markdown("---")
        st.markdown(st.session_state.formal_report)

# ---------------------------------------------------------------------------
# Tab 4: Downloads
# ---------------------------------------------------------------------------

with tab_download:
    st.header("Download Reports")

    rows = parse_uploaded_files(file_data)
    summary_md = generate_summary(file_data, town_filter, town_centers, inject_nomenclature)

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        if rows:
            st.download_button(
                "📊 CSV",
                data=rows_to_csv(rows),
                file_name=f"{stem}-report.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with col2:
        if rows:
            st.download_button(
                "📊 JSON",
                data=rows_to_json(rows),
                file_name=f"{stem}-report.json",
                mime="application/json",
                use_container_width=True,
            )

    with col3:
        if summary_md and "No field notes found" not in summary_md:
            st.download_button(
                "📝 Summary (.md)",
                data=summary_md,
                file_name=f"{stem}-field-notes.md",
                mime="text/markdown",
                use_container_width=True,
            )

    with col4:
        if summary_md and "No field notes found" not in summary_md:
            try:
                docx_bytes = md_to_docx_bytes(summary_md)
                st.download_button(
                    "📝 Summary (.docx)",
                    data=docx_bytes,
                    file_name=f"{stem}-field-notes.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    use_container_width=True,
                )
            except Exception as exc:
                st.button("📝 .docx unavailable", disabled=True, use_container_width=True,
                          help=f"python-docx error: {exc}")

    with col5:
        if st.session_state.get("formal_report"):
            st.download_button(
                "📄 Formal Report (.md)",
                data=st.session_state.formal_report,
                file_name=f"{stem}-formal-report.md",
                mime="text/markdown",
                use_container_width=True,
            )
        else:
            st.button(
                "📄 Formal Report",
                disabled=True,
                help="Generate a formal report first in the Formal Report tab.",
                use_container_width=True,
            )

    st.markdown("---")
    st.info(
        "💡 **Tip:** The Summary (.md) file contains coordinates for every "
        "corridor. Paste them into Google Maps or open the KML side-by-side "
        "to cross-reference locations."
    )

# ---------------------------------------------------------------------------
# Tab 5: KML Injector
# ---------------------------------------------------------------------------

with tab_inject:
    st.header("KML Note Injector")
    st.markdown(
        "Spatially match field notes from **source KML files** to placemarks in a "
        "**target KML** (e.g. a V2 design file) and produce an annotated KMZ. "
        "Matched notes are injected into placemark descriptions; unmatched notes "
        "become new yellow-pin placemarks in a **Field_Notes** folder."
    )

    col_target, col_sources = st.columns(2)

    with col_target:
        inject_target = st.file_uploader(
            "Target KML (design file)",
            type=["kml", "kmz"],
            key="inject_target",
            help="The KML/KMZ to inject notes into (e.g. Colorado County V2.kml).",
        )

    with col_sources:
        inject_sources = st.file_uploader(
            "Source KML(s) with field notes",
            type=["kml", "kmz"],
            accept_multiple_files=True,
            key="inject_sources",
            help="One or more KML/KMZ files containing field observations.",
        )

    inject_tolerance = st.slider(
        "Match tolerance (meters)",
        min_value=5,
        max_value=50,
        value=15,
        step=1,
        help="Maximum distance in meters to match a note to a placemark. "
             "15 m (~50 ft) is recommended.",
    )

    inject_btn = st.button(
        "🚀 Inject Notes",
        type="primary",
        use_container_width=True,
        disabled=not (inject_target and inject_sources),
    )

    if "inject_summary" not in st.session_state:
        st.session_state.inject_summary = None
        st.session_state.inject_kmz_bytes = None

    if inject_btn and inject_target and inject_sources:
        with st.spinner("Parsing and matching notes..."):
            try:
                from kml2report.injector import inject_notes_into_kml

                target_suffix = Path(inject_target.name).suffix.lower()
                with tempfile.NamedTemporaryFile(suffix=target_suffix, delete=False) as tmp_t:
                    tmp_t.write(inject_target.getvalue())
                    target_path = Path(tmp_t.name)

                source_paths: list[Path] = []
                for src in inject_sources:
                    src_suffix = Path(src.name).suffix.lower()
                    with tempfile.NamedTemporaryFile(suffix=src_suffix, delete=False) as tmp_s:
                        tmp_s.write(src.getvalue())
                        source_paths.append(Path(tmp_s.name))

                out_path = target_path.with_suffix(".annotated.kmz")

                tc_path = None
                if town_centers:
                    with tempfile.NamedTemporaryFile(
                        suffix=".json", delete=False, mode="w", encoding="utf-8"
                    ) as tmp_tc:
                        json.dump(
                            {"towns": [{"name": n, "latitude": la, "longitude": lo}
                                       for n, la, lo in town_centers]},
                            tmp_tc,
                        )
                        tc_path = tmp_tc.name

                summary = inject_notes_into_kml(
                    target_kml_path=str(target_path),
                    source_kml_paths=[str(p) for p in source_paths],
                    output_path=str(out_path),
                    tolerance_m=float(inject_tolerance),
                    town_config_path=tc_path,
                )

                kmz_bytes = out_path.read_bytes()

                target_path.unlink(missing_ok=True)
                for p in source_paths:
                    p.unlink(missing_ok=True)
                out_path.unlink(missing_ok=True)
                if tc_path:
                    Path(tc_path).unlink(missing_ok=True)

                st.session_state.inject_summary = summary
                st.session_state.inject_kmz_bytes = kmz_bytes

            except Exception as exc:
                st.error(f"Injection failed: {exc}")

    if st.session_state.inject_summary:
        s = st.session_state.inject_summary
        st.markdown("---")
        st.subheader("Injection Results")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("V2 Placemarks", f"{s['v2_placemarks']:,}")
        m2.metric("Total Notes", f"{s['total_notes']:,}")
        m3.metric("Matched", f"{s['matched_notes']:,}")
        m4.metric("New Pins", f"{s['unmatched_notes']:,}")

        out_name = Path(inject_target.name).stem + " - Annotated.kmz" if inject_target else "annotated.kmz"
        st.download_button(
            "⬇️ Download Annotated KMZ",
            data=st.session_state.inject_kmz_bytes,
            file_name=out_name,
            mime="application/vnd.google-earth.kmz",
            use_container_width=True,
        )

# ---------------------------------------------------------------------------
# Tab 6: Geometry Analysis
# ---------------------------------------------------------------------------

with tab_geometry:
    st.header("Project Geometry Analysis")
    st.markdown(
        """
        Analyze linear geometries (ducts, cables) and generate project-wide 
        aggregation of lengths by type.
        """
    )
    
    # Check if we have features already loaded
    if 'geometry_features' not in st.session_state:
        st.session_state.geometry_features = None
        st.session_state.geometry_summary = None
    
    # Load features if not already loaded
    if st.session_state.geometry_features is None:
        with st.spinner("Loading features for geometry analysis..."):
            try:
                features = parse_features_multi(file_data)
                st.session_state.geometry_features = features
            except Exception as exc:
                st.error(f"Failed to load features: {exc}")
                st.stop()
    
    features = st.session_state.geometry_features
    
    if not features:
        st.warning("No features found for geometry analysis.")
        st.stop()
    
    # Perform analysis
    if st.button("🔍 Analyze Geometry", type="primary", use_container_width=True):
        with st.spinner("Analyzing geometry..."):
            try:
                from kml2report.geometry_analyzer import (
                    aggregate_project_footage,
                    detect_linear_runs,
                    validate_lengths,
                    generate_summary_report
                )
                
                # Get project summary
                summary = aggregate_project_footage(features)
                st.session_state.geometry_summary = summary
                
                # Detect linear runs
                runs = detect_linear_runs(features)
                
                # Validate lengths
                validation = validate_lengths(features)
                
                # Store results
                st.session_state.geometry_runs = runs
                st.session_state.geometry_validation = validation
                
            except Exception as exc:
                st.error(f"Analysis failed: {exc}")
                st.stop()
    
    # Display results
    if st.session_state.geometry_summary:
        summary = st.session_state.geometry_summary
        
        # Project metrics
        st.subheader("Project Overview")
        m1, m2, m3 = st.columns(3)
        m1.metric("Total Features", f"{len(features):,}")
        m2.metric("Linear Segments", f"{summary.segment_count:,}")
        m3.metric("Total Length", f"{summary.total_length:,.2f} ft")
        
        # Detailed breakdown
        st.subheader("Length Breakdown")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.write("**Single Duct Runs**")
            if summary.single_duct_lengths:
                for size, length in sorted(summary.single_duct_lengths.items()):
                    st.write(f"- {size}: {length:,.2f} ft")
                total_single = sum(summary.single_duct_lengths.values())
                st.write(f"**Total: {total_single:,.2f} ft**")
            else:
                st.write("No single duct runs found")
            
            st.write("")
            st.write("**Multi-Duct Packages**")
            if summary.multi_duct_lengths:
                for config, length in sorted(summary.multi_duct_lengths.items()):
                    st.write(f"- {config}: {length:,.2f} ft")
                total_multi = sum(summary.multi_duct_lengths.values())
                st.write(f"**Total: {total_multi:,.2f} ft**")
            else:
                st.write("No multi-duct packages found")
        
        with col2:
            st.write("**Aerial Construction**")
            if summary.aerial_lengths:
                for wire_type, length in sorted(summary.aerial_lengths.items()):
                    st.write(f"- {wire_type}: {length:,.2f} ft")
                total_aerial = sum(summary.aerial_lengths.values())
                st.write(f"**Total: {total_aerial:,.2f} ft**")
            else:
                st.write("No aerial construction found")
            
            st.write("")
            st.write("**Other Categories**")
            if summary.other_lengths:
                for category, length in sorted(summary.other_lengths.items()):
                    st.write(f"- {category}: {length:,.2f} ft")
            else:
                st.write("No other categories found")
        
        # Linear runs
        if 'geometry_runs' in st.session_state and st.session_state.geometry_runs:
            st.subheader("Connected Linear Runs")
            runs = st.session_state.geometry_runs[:10]  # Show first 10
            
            for i, run in enumerate(runs):
                with st.expander(f"Run {i+1}: {run.total_length:,.2f} ft - {', '.join(run.duct_sizes)}"):
                    st.write(f"Segments: {len(run.features)}")
                    if run.start_coord and run.end_coord:
                        st.write(f"Start: {run.start_coord[1]:.6f}°N, {run.start_coord[0]:.6f}°W")
                        st.write(f"End: {run.end_coord[1]:.6f}°N, {run.end_coord[0]:.6f}°W")
        
        # Validation results
        if 'geometry_validation' in st.session_state and st.session_state.geometry_validation:
            st.subheader("Length Validation")
            validation = st.session_state.geometry_validation
            
            # Show validation summary
            valid_count = sum(1 for v in validation if v['difference_pct'] < 5)
            st.write(f"Features with <5% difference: {valid_count}/{len(validation)}")
            
            if st.checkbox("Show validation details"):
                import pandas as pd
                df = pd.DataFrame(validation)
                st.dataframe(df, use_container_width=True)
        
        # Export options
        st.subheader("Export Results")
        
        # Import here to ensure it's available
        from kml2report.geometry_analyzer import generate_summary_report
        
        # Generate summary report
        report_text = generate_summary_report(summary)
        st.download_button(
            "📄 Download Summary Report",
            data=report_text,
            file_name=f"{stem}_geometry_summary.txt",
            mime="text/plain",
            use_container_width=True,
        )
        
        # Export Excel with clickable hyperlinks
        if st.checkbox("Include detailed Excel export with map links"):
            excel_data = []
            for feature in features:
                if feature.geometry and feature.geometry.geometry_type == 'LineString':
                    from kml2report.geometry_analyzer import extract_duct_info, parse_description_html
                    duct_info = extract_duct_info(feature)
                    attrs = parse_description_html(feature.raw_description)
                    
                    row = {
                        'feature_id': feature.placemark_id,
                        'name': feature.name,
                        'layer': feature.layer,
                        'duct_size': duct_info.duct_size,
                        'package_type': attrs.get('Duct_Package', 'single'),
                        'duct_count': attrs.get('Duct_Count', '1'),
                        'length_ft': duct_info.length,
                    }
                    
                    if feature.geometry.coordinates:
                        start_lat = feature.geometry.coordinates[0][1]
                        start_lon = feature.geometry.coordinates[0][0]
                        end_lat = feature.geometry.coordinates[-1][1]
                        end_lon = feature.geometry.coordinates[-1][0]
                        
                        row['start_lat'] = start_lat
                        row['start_lon'] = start_lon
                        row['end_lat'] = end_lat
                        row['end_lon'] = end_lon
                        
                        # Store URLs for Excel hyperlinks
                        row['start_map_url'] = f'https://www.openstreetmap.org/?mlat={start_lat}&mlon={start_lon}#map=18/{start_lat}/{start_lon}'
                        row['end_map_url'] = f'https://www.openstreetmap.org/?mlat={end_lat}&mlon={end_lon}#map=18/{end_lat}/{end_lon}'
                    
                    excel_data.append(row)
            
            if excel_data:
                import pandas as pd
                from io import BytesIO
                
                df = pd.DataFrame(excel_data)
                
                # Create Excel file with hyperlinks
                output = BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    # Write data without URL columns first
                    df_display = df.drop(columns=['start_map_url', 'end_map_url'], errors='ignore')
                    df_display.to_excel(writer, sheet_name='Geometry Data', index=False)
                    
                    # Get workbook and worksheet
                    workbook = writer.book
                    worksheet = writer.sheets['Geometry Data']
                    
                    # Add hyperlink columns
                    if 'start_map_url' in df.columns:
                        col_idx = len(df_display.columns)
                        worksheet.write(0, col_idx, 'Start Map Link')
                        worksheet.write(0, col_idx + 1, 'End Map Link')
                        
                        for idx, row in df.iterrows():
                            if pd.notna(row.get('start_map_url')):
                                worksheet.write_url(idx + 1, col_idx, row['start_map_url'], string='View Start')
                            if pd.notna(row.get('end_map_url')):
                                worksheet.write_url(idx + 1, col_idx + 1, row['end_map_url'], string='View End')
                
                excel_bytes = output.getvalue()
                
                st.download_button(
                    "📊 Download Excel with Clickable Links",
                    data=excel_bytes,
                    file_name=f"{stem}_geometry_data.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

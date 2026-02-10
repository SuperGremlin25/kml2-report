"""Streamlit dashboard for kml2report.

Upload one or more KML/KMZ files -> preview parsed data -> generate
field-notes summary (optionally filtered by town) -> download as
Markdown or Word (.docx).

Deploy to Hugging Face Spaces with sdk: streamlit.
"""

from __future__ import annotations

import io
import csv
import json
import re
import tempfile
from pathlib import Path
from typing import Any

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
        town_centers = [
            (t["name"], float(t["latitude"]), float(t["longitude"]))
            for t in data.get("towns", [])
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        st.sidebar.error("Invalid town-config.json format.")

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

        ---

        *Built for HLD/CRO field teams. All processing runs server-side —
        your KML files are not stored permanently.*
        """
    )
    st.stop()

# --- Files uploaded — process them ---
file_data: list[tuple[bytes, str]] = [
    (f.getvalue(), f.name) for f in uploaded_files
]
file_names = [f.name for f in uploaded_files]
stem = Path(file_names[0]).stem if len(file_names) == 1 else "combined"

tab_preview, tab_summary, tab_report, tab_download = st.tabs(
    ["📊 Data Preview", "📝 Field Notes Summary", "📄 Formal Report", "⬇️ Downloads"]
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
    import os
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

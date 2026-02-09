"""Streamlit dashboard for kml2report.

Upload KML/KMZ files → preview parsed data → generate field-notes
summary → (optional) generate formal LLM report → download everything.

Deploy to Hugging Face Spaces with sdk: streamlit.
"""

from __future__ import annotations

import io
import csv
import json
import tempfile
from pathlib import Path

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
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("🗺️ KML Field Report Generator")
st.sidebar.markdown(
    "Upload a KML or KMZ file to parse field data, generate a "
    "location-intelligent summary, and optionally produce a formal "
    "report via LLM."
)

uploaded_file = st.sidebar.file_uploader(
    "Upload KML / KMZ",
    type=["kml", "kmz"],
    help="Drag and drop or click to browse.",
)

st.sidebar.markdown("---")
st.sidebar.markdown(
    "**Workflow:**\n"
    "1. Upload a KML/KMZ file\n"
    "2. Preview parsed features\n"
    "3. Generate field-notes summary\n"
    "4. *(Optional)* Generate formal report via LLM\n"
    "5. Download outputs"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Parsing KML data...")
def parse_uploaded_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Parse uploaded file bytes through the kml2report pipeline."""
    from kml2report.pipeline import run_pipeline

    suffix = Path(filename).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        rows = run_pipeline(input_paths=[tmp_path])
    finally:
        tmp_path.unlink(missing_ok=True)

    return rows


@st.cache_data(show_spinner="Parsing features...")
def parse_features(file_bytes: bytes, filename: str):
    """Parse uploaded file into Feature objects for the summarizer."""
    from kml2report.kml_parser import parse_file

    suffix = Path(filename).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        features = parse_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return features


def generate_summary(file_bytes: bytes, filename: str) -> str:
    """Generate the location-intelligent Markdown summary with geocoding."""
    from kml2report.summarizer import generate_summary_report

    features = parse_features(file_bytes, filename)
    doc_name = Path(filename).stem

    progress_bar = st.progress(0, text="Reverse-geocoding coordinates via OpenStreetMap...")

    def _progress(done: int, total: int) -> None:
        if total > 0:
            progress_bar.progress(done / total, text=f"Geocoding: {done}/{total} locations...")

    result = generate_summary_report(
        features,
        doc_name=doc_name,
        geocode=True,
        progress_callback=_progress,
    )
    progress_bar.empty()
    return result


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

if uploaded_file is None:
    st.title("KML Field Report Generator")
    st.markdown(
        """
        ### Get started

        Upload a **KML** or **KMZ** file using the sidebar to:

        - **Preview** all parsed features in a searchable table
        - **Generate** a location-intelligent field-notes summary with
          coordinates, road names, bearings, and geographic corridors
        - **Produce** a formal professional report via LLM
          (Hugging Face, OpenAI, Anthropic, or Ollama)
        - **Download** CSV, JSON, summary Markdown, and formal report

        ---

        *Built for HLD/CRO field teams. All processing runs server-side —
        your KML files are not stored permanently.*
        """
    )
    st.stop()

# --- File uploaded — process it ---
file_bytes = uploaded_file.getvalue()
filename = uploaded_file.name

tab_preview, tab_summary, tab_report, tab_download = st.tabs(
    ["📊 Data Preview", "📝 Field Notes Summary", "📄 Formal Report", "⬇️ Downloads"]
)

# ---------------------------------------------------------------------------
# Tab 1: Data Preview
# ---------------------------------------------------------------------------

with tab_preview:
    st.header("Parsed Features")

    rows = parse_uploaded_file(file_bytes, filename)

    if not rows:
        st.warning("No features found in the uploaded file.")
    else:
        st.success(f"**{len(rows):,}** features parsed from `{filename}`")

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

    summary_md = generate_summary(file_bytes, filename)

    if not summary_md or "No field notes found" in summary_md:
        st.warning("No field notes found in the uploaded file.")
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
        summary_md = generate_summary(file_bytes, filename)
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

    rows = parse_uploaded_file(file_bytes, filename)
    summary_md = generate_summary(file_bytes, filename)
    stem = Path(filename).stem

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if rows:
            st.download_button(
                "📊 Download CSV",
                data=rows_to_csv(rows),
                file_name=f"{stem}-report.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with col2:
        if rows:
            st.download_button(
                "📊 Download JSON",
                data=rows_to_json(rows),
                file_name=f"{stem}-report.json",
                mime="application/json",
                use_container_width=True,
            )

    with col3:
        if summary_md and "No field notes found" not in summary_md:
            st.download_button(
                "📝 Download Summary (.md)",
                data=summary_md,
                file_name=f"{stem}-field-notes.md",
                mime="text/markdown",
                use_container_width=True,
            )

    with col4:
        if st.session_state.get("formal_report"):
            st.download_button(
                "📄 Download Formal Report (.md)",
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

"""CLI entrypoint for kml2report."""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import click

from kml2report.pipeline import run_pipeline


@click.group()
@click.version_option(package_name="kml2report")
def main() -> None:
    """kml2report - Extract structured data from KML/KMZ files."""


@main.command()
@click.argument("inputs", nargs=-1, required=True)
@click.option(
    "--config", "-c", "config_path", default=None, type=click.Path(exists=True),
    help="Path to YAML config file.",
)
@click.option(
    "--out", "-o", "output_path", default=None, type=click.Path(),
    help="Output file path (.csv or .json).",
)
@click.option(
    "--format", "-f", "output_format", default=None, type=click.Choice(["csv", "json"]),
    help="Output format (overrides config).",
)
@click.option(
    "--preview", "-p", is_flag=True, default=False,
    help="Print first 10 rows to stdout instead of writing a file.",
)
def parse(
    inputs: tuple[str, ...],
    config_path: str | None,
    output_path: str | None,
    output_format: str | None,
    preview: bool,
) -> None:
    """Parse KML/KMZ files and export structured data.

    INPUTS can be one or more .kml/.kmz file paths or glob patterns.

    Examples:

        kml2report parse input.kml --out report.csv

        kml2report parse *.kmz --config config.yml --out data.json -f json

        kml2report parse myfile.kml --preview
    """
    expanded = _expand_inputs(inputs)
    if not expanded:
        click.echo("No matching KML/KMZ files found.", err=True)
        sys.exit(1)

    click.echo(f"Parsing {len(expanded)} file(s)...")

    if output_format is None and output_path:
        suffix = Path(output_path).suffix.lower()
        if suffix == ".json":
            output_format = "json"
        else:
            output_format = "csv"

    rows = run_pipeline(
        input_paths=expanded,
        config_path=config_path,
        output_path=None if preview else output_path,
        output_format=output_format,
    )

    click.echo(f"Extracted {len(rows)} features.")

    if preview:
        _print_preview(rows)
    elif output_path:
        click.echo(f"Written to {output_path}")
    else:
        click.echo("No output path specified. Use --out or --preview.")


def _expand_inputs(inputs: tuple[str, ...]) -> list[Path]:
    """Expand glob patterns and validate file existence."""
    paths: list[Path] = []
    for pattern in inputs:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            for m in matches:
                p = Path(m)
                if p.suffix.lower() in (".kml", ".kmz") and p.is_file():
                    paths.append(p)
        else:
            p = Path(pattern)
            if p.is_file():
                paths.append(p)
    return paths


def _print_preview(rows: list[dict[str, str]], max_rows: int = 10) -> None:
    """Print a simple tabular preview to stdout."""
    if not rows:
        click.echo("(no data)")
        return

    display = rows[:max_rows]
    cols = list(display[0].keys())[:8]

    widths = {c: len(c) for c in cols}
    for row in display:
        for c in cols:
            widths[c] = max(widths[c], len(str(row.get(c, ""))[:40]))

    header = " | ".join(c.ljust(widths[c]) for c in cols)
    click.echo(header)
    click.echo("-" * len(header))
    for row in display:
        line = " | ".join(str(row.get(c, ""))[:40].ljust(widths[c]) for c in cols)
        click.echo(line)

    if len(rows) > max_rows:
        click.echo(f"... and {len(rows) - max_rows} more rows")


@main.command()
@click.argument("inputs", nargs=-1, required=True)
@click.option(
    "--out", "-o", "output_path", default=None, type=click.Path(),
    help="Output file path (.md). Defaults to stdout.",
)
@click.option(
    "--no-geocode", is_flag=True, default=False,
    help="Skip reverse geocoding (faster, but no real street/town names).",
)
@click.option(
    "--town", "town_filter", default=None,
    help="Filter report to a single town name (must match a name in the town config).",
)
@click.option(
    "--town-config", "town_config_path", default=None, type=click.Path(exists=True),
    help="Path to a town-config.json file for portable town center definitions.",
)
def summarize(inputs: tuple[str, ...], output_path: str | None, no_geocode: bool, town_filter: str | None, town_config_path: str | None) -> None:
    """Generate a structured field-notes summary report from KML/KMZ files.

    Reads all Placemark descriptions, classifies them by theme
    (access, construction method, obstacles, permits, etc.), groups
    them geographically, and produces a narrative Markdown report.

    Coordinates are reverse-geocoded via OpenStreetMap Nominatim to
    provide real street names, towns, and counties.  Use --no-geocode
    to skip this step (e.g. when offline).

    Examples:

        kml2report summarize myfile.kml

        kml2report summarize *.kml --out field-report.md

        kml2report summarize myfile.kml --no-geocode
    """
    from kml2report.kml_parser import parse_files
    from kml2report.summarizer import generate_summary_report, load_town_config

    expanded = _expand_inputs(inputs)
    if not expanded:
        click.echo("No matching KML/KMZ files found.", err=True)
        sys.exit(1)

    town_centers = load_town_config(town_config_path)
    valid_town_names = [name for name, _, _ in town_centers]
    if town_filter:
        matched = [n for n in valid_town_names if n.lower() == town_filter.lower()]
        if not matched:
            click.echo(
                f"Unknown town '{town_filter}'. Valid towns: {', '.join(valid_town_names)}",
                err=True,
            )
            sys.exit(1)
        town_filter = matched[0]

    click.echo(f"Parsing {len(expanded)} file(s)...", err=True)
    features = parse_files([str(p) for p in expanded])
    click.echo(f"Analyzing {len(features)} features...", err=True)

    def _geo_progress(done: int, total: int) -> None:
        click.echo(f"\rGeocoding coordinates: {done}/{total}", nl=False, err=True)
        if done == total:
            click.echo("", err=True)

    cache_path = Path(output_path).parent / ".geocache.json" if output_path else None

    doc_name = ", ".join(p.stem for p in expanded)
    if town_filter:
        doc_name = f"{town_filter} — {doc_name}"
        click.echo(f"Filtering to town: {town_filter}", err=True)
    if not no_geocode:
        click.echo("Reverse-geocoding coordinates via OpenStreetMap...", err=True)
    report = generate_summary_report(
        features,
        doc_name=doc_name,
        geocode=not no_geocode,
        cache_path=cache_path,
        progress_callback=_geo_progress if not no_geocode else None,
        town_filter=town_filter,
        town_centers=town_centers,
    )

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(report, encoding="utf-8")
        click.echo(f"Summary written to {output_path}", err=True)
    else:
        click.echo(report)


@main.command()
@click.argument("input_md", type=click.Path(exists=True))
@click.option(
    "--out", "-o", "output_path", default=None, type=click.Path(),
    help="Output file path (.md). Defaults to stdout.",
)
@click.option(
    "--provider", "-p", default="openai",
    type=click.Choice(["openai", "anthropic", "ollama", "huggingface"], case_sensitive=False),
    help="LLM provider to use.",
)
@click.option(
    "--model", "-m", default=None,
    help="Model name override (e.g. gpt-4o, claude-sonnet-4-20250514, llama3).",
)
@click.option(
    "--temperature", "-t", default=0.3, type=float,
    help="Sampling temperature (lower = more deterministic).",
)
@click.option(
    "--base-url", default=None,
    help="Custom API base URL (for proxies or non-default Ollama host).",
)
def report(
    input_md: str,
    output_path: str | None,
    provider: str,
    model: str | None,
    temperature: float,
    base_url: str | None,
) -> None:
    """Send a field-notes summary (.md) to an LLM for formal report generation.

    Takes the Markdown output from 'kml2report summarize' and produces a
    polished, professional CRO/HLD field assessment report.

    Requires the appropriate API key set as an environment variable:
      - OpenAI:    OPENAI_API_KEY
      - Anthropic: ANTHROPIC_API_KEY
      - Ollama:    (no key needed, but Ollama must be running locally)

    Examples:

        kml2report report field-notes.md --out formal-report.md

        kml2report report field-notes.md -p anthropic -m claude-sonnet-4-20250514

        kml2report report field-notes.md -p ollama -m llama3
    """
    from kml2report.llm_report import generate_formal_report

    summary_md = Path(input_md).read_text(encoding="utf-8")
    click.echo(
        f"Sending {len(summary_md):,} chars to {provider}"
        f"{(' / ' + model) if model else ''}...",
        err=True,
    )

    try:
        formal = generate_formal_report(
            summary_md=summary_md,
            provider=provider,
            model=model,
            temperature=temperature,
            base_url=base_url,
        )
    except (ImportError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(formal, encoding="utf-8")
        click.echo(f"Formal report written to {output_path}", err=True)
    else:
        click.echo(formal)


@main.command()
@click.option(
    "--v2", "target_kml", required=True, type=click.Path(exists=True),
    help="Path to the target KML file (e.g. Colorado County V2.kml).",
)
@click.option(
    "--sources", "-s", "source_kmls", required=True, multiple=True,
    type=click.Path(exists=True),
    help="One or more source KML/KMZ files containing field notes.",
)
@click.option(
    "--tolerance", "-t", default=15.0, type=float,
    help="Match radius in meters (default 15).",
)
@click.option(
    "--town-config", "town_config_path", default=None, type=click.Path(exists=True),
    help="Path to a town-config.json file.",
)
@click.option(
    "--out", "-o", "output_path", default=None, type=click.Path(),
    help="Output file path (.kmz or .kml). Defaults to '<target> - Annotated.kmz'.",
)
def inject(
    target_kml: str,
    source_kmls: tuple[str, ...],
    tolerance: float,
    town_config_path: str | None,
    output_path: str | None,
) -> None:
    """Inject classified field notes into a target KML and produce an annotated KMZ.

    Spatially matches notes from source KML files to the nearest placemarks
    in the target KML within the given tolerance (default 15 m). Matched notes
    are appended to the placemark's description. Unmatched notes are added as
    new yellow-pin placemarks in a Field_Notes folder.

    Examples:

        kml2report inject --v2 "Colorado County V2.kml" -s source1.kml -s source2.kml

        kml2report inject --v2 target.kml -s notes.kml --tolerance 25 --out annotated.kmz
    """
    from kml2report.injector import inject_notes_into_kml

    if output_path is None:
        stem = Path(target_kml).stem
        output_path = str(Path(target_kml).parent / f"{stem} - Annotated.kmz")

    click.echo(f"Target KML: {target_kml}", err=True)
    click.echo(f"Source files: {len(source_kmls)}", err=True)
    click.echo(f"Match tolerance: {tolerance} m", err=True)
    click.echo("Parsing and matching...", err=True)

    summary = inject_notes_into_kml(
        target_kml_path=target_kml,
        source_kml_paths=list(source_kmls),
        output_path=output_path,
        tolerance_m=tolerance,
        town_config_path=town_config_path,
    )

    click.echo("", err=True)
    click.echo("=== Injection Summary ===", err=True)
    click.echo(f"  V2 placemarks:      {summary['v2_placemarks']:,}", err=True)
    click.echo(f"  Total field notes:   {summary['total_notes']:,}", err=True)
    click.echo(f"  Matched to V2:       {summary['matched_notes']:,} notes -> {summary['matched_placemarks']:,} placemarks", err=True)
    click.echo(f"  New pins (unmatched): {summary['unmatched_notes']:,}", err=True)
    click.echo(f"  Output: {output_path}", err=True)


@main.command()
@click.option("--host", "-h", default="127.0.0.1", help="Bind address.")
@click.option("--port", "-p", default=8000, type=int, help="Port number.")
def serve(host: str, port: int) -> None:
    """Start the web UI for uploading and previewing KML/KMZ files."""
    try:
        from kml2report.web import start_server
    except ImportError:
        click.echo(
            "Web dependencies not installed. Run:\n"
            "  pip install kml2report[web]\n"
            "or:\n"
            "  pip install fastapi uvicorn python-multipart jinja2",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Starting kml2report web UI at http://{host}:{port}")
    start_server(host=host, port=port)


if __name__ == "__main__":
    main()

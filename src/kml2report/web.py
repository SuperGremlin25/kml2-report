"""Lightweight FastAPI web UI for kml2report."""

from __future__ import annotations

import io
import csv
import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Form, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from kml2report.pipeline import run_pipeline

app = FastAPI(title="kml2report", version="0.1.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/favicon.ico")
async def favicon():
    """Return a simple SVG favicon."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<circle cx="16" cy="12" r="10" fill="#3f51b5"/>'
        '<path d="M16 2C10.48 2 6 6.48 6 12c0 7.5 10 18 10 18s10-10.5 10-18c0-5.52-4.48-10-10-10zm0 14'
        'c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4z" fill="#3f51b5"/>'
        '</svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the upload / preview page."""
    return templates.TemplateResponse("index.html", {"request": request, "rows": None})


@app.post("/parse", response_class=HTMLResponse)
async def parse_upload(request: Request, file: UploadFile = File(...)):
    """Accept a KML/KMZ upload, parse it, and show a preview table."""
    suffix = Path(file.filename or "upload.kml").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        rows = run_pipeline(input_paths=[tmp_path])
    finally:
        tmp_path.unlink(missing_ok=True)

    cols = list(rows[0].keys()) if rows else []
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "rows": rows, "cols": cols, "count": len(rows)},
    )


@app.post("/export/csv")
async def export_csv_endpoint(file: UploadFile = File(...)):
    """Parse and return CSV download."""
    suffix = Path(file.filename or "upload.kml").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        rows = run_pipeline(input_paths=[tmp_path])
    finally:
        tmp_path.unlink(missing_ok=True)

    if not rows:
        return JSONResponse({"error": "No features found"}, status_code=400)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=kml2report-export.csv"},
    )


@app.post("/export/json")
async def export_json_endpoint(file: UploadFile = File(...)):
    """Parse and return JSON download."""
    suffix = Path(file.filename or "upload.kml").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        rows = run_pipeline(input_paths=[tmp_path])
    finally:
        tmp_path.unlink(missing_ok=True)

    content = json.dumps(rows, indent=2, ensure_ascii=False)
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=kml2report-export.json"},
    )


def start_server(host: str = "127.0.0.1", port: int = 8000):
    """Start the uvicorn dev server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)

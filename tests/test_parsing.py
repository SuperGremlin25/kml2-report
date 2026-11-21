"""Tests for KML parsing, description normalization, and export."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kml2report.kml_parser import parse_file
from kml2report.description_parser import parse_description
from kml2report.export import export_csv, export_json
from kml2report.pipeline import run_pipeline
from kml2report.config import load_config
from kml2report.llm_report import generate_formal_report

FIXTURES = Path(__file__).parent / "fixtures"
KV_KML = FIXTURES / "sample-keyvalue.kml"
HTML_KML = FIXTURES / "sample-html-table.kml"


class TestKmlParser:
    """Tests for the core KML parser."""

    def test_parse_keyvalue_kml_feature_count(self):
        features = parse_file(KV_KML)
        assert len(features) == 4

    def test_parse_keyvalue_kml_names(self):
        features = parse_file(KV_KML)
        names = [f.name for f in features]
        assert "Alpha Site" in names
        assert "Bravo Checkpoint" in names
        assert "Route Gamma" in names

    def test_parse_keyvalue_kml_geometry_point(self):
        features = parse_file(KV_KML)
        alpha = [f for f in features if f.name == "Alpha Site"][0]
        assert alpha.geometry is not None
        assert alpha.geometry.geometry_type == "Point"
        assert abs(alpha.geometry.latitude - 29.514464) < 0.001
        assert abs(alpha.geometry.longitude - (-96.624330)) < 0.001

    def test_parse_keyvalue_kml_geometry_linestring(self):
        features = parse_file(KV_KML)
        route = [f for f in features if f.name == "Route Gamma"][0]
        assert route.geometry is not None
        assert route.geometry.geometry_type == "LineString"
        assert len(route.geometry.coordinates) == 3

    def test_parse_keyvalue_kml_layer(self):
        features = parse_file(KV_KML)
        alpha = [f for f in features if f.name == "Alpha Site"][0]
        assert alpha.layer == "Targets"
        obs = [f for f in features if f.name == "Observation Point"][0]
        assert obs.layer == "Observations"

    def test_parse_html_table_kml_feature_count(self):
        features = parse_file(HTML_KML)
        assert len(features) == 3

    def test_parse_html_table_kml_linestring(self):
        features = parse_file(HTML_KML)
        strand = [f for f in features if f.name == "Strand"][0]
        assert strand.geometry is not None
        assert strand.geometry.geometry_type == "LineString"
        assert len(strand.geometry.coordinates) == 2

    def test_placemark_id_extracted(self):
        features = parse_file(KV_KML)
        ids = [f.placemark_id for f in features]
        assert "PM001" in ids
        assert "PM002" in ids


class TestDescriptionParser:
    """Tests for description normalization."""

    def test_keyvalue_basic(self):
        raw = "ID=OBJ-001; TYPE=SITE; PRIORITY=HIGH; STATUS=ACTIVE; NOTES=Located near key infrastructure."
        result = parse_description(raw, field_map={"ID": "id", "TYPE": "type", "PRIORITY": "priority", "STATUS": "status", "NOTES": "notes"})
        assert result["id"] == "OBJ-001"
        assert result["type"] == "SITE"
        assert result["priority"] == "HIGH"
        assert result["status"] == "ACTIVE"
        assert "infrastructure" in result["notes"]

    def test_keyvalue_no_spaces(self):
        raw = "ID=OBJ-002;TYPE=CHECKPOINT;PRTY=LOW;STATUS=INACTIVE"
        result = parse_description(raw, field_map={"ID": "id", "TYPE": "type", "PRTY": "priority", "STATUS": "status"})
        assert result["id"] == "OBJ-002"
        assert result["priority"] == "LOW"

    def test_keyvalue_defaults_applied(self):
        raw = "ID=OBJ-005; TYPE=SITE"
        result = parse_description(
            raw,
            field_map={"ID": "id", "TYPE": "type"},
            defaults={"priority": "UNKNOWN", "status": "UNKNOWN"},
        )
        assert result["priority"] == "UNKNOWN"
        assert result["status"] == "UNKNOWN"

    def test_html_div_parsed_as_notes(self):
        raw = '<div>Inside ROW&nbsp;<br></div>'
        result = parse_description(raw)
        assert "notes" in result
        assert "Inside ROW" in result["notes"]

    def test_html_table_parsed(self):
        raw = """<html><body><table>
        <tr><td>SHAPE</td><td>Point</td></tr>
        <tr><td>Pole_Type</td><td>Distribution</td></tr>
        </table></body></html>"""
        result = parse_description(raw, field_map={"SHAPE": "shape", "Pole_Type": "pole_type"})
        assert result.get("shape") == "Point"
        assert result.get("pole_type") == "Distribution"

    def test_plain_text_becomes_notes(self):
        raw = "Just a plain text note with no key-value pairs."
        result = parse_description(raw)
        assert "notes" in result
        assert "plain text" in result["notes"]

    def test_empty_description(self):
        result = parse_description("")
        assert isinstance(result, dict)

    def test_extra_whitespace_handled(self):
        raw = "  ID = OBJ-010 ;  TYPE = SITE ;  PRIORITY = HIGH  "
        result = parse_description(raw, field_map={"ID": "id", "TYPE": "type", "PRIORITY": "priority"})
        assert result.get("id") == "OBJ-010"


class TestExport:
    """Tests for CSV and JSON export."""

    def test_csv_export(self):
        rows = [
            {"feature_id": "PM001", "name": "Alpha", "type": "SITE", "latitude": "29.5"},
            {"feature_id": "PM002", "name": "Bravo", "type": "CHECKPOINT", "latitude": "29.4"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            path = Path(f.name)
        export_csv(rows, path)
        with open(path, "r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            exported = list(reader)
        assert len(exported) == 2
        assert exported[0]["name"] == "Alpha"
        path.unlink()

    def test_json_export(self):
        rows = [
            {"feature_id": "PM001", "name": "Alpha", "layer": "Targets"},
            {"feature_id": "PM002", "name": "Bravo", "layer": "Observations"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = Path(f.name)
        export_json(rows, path)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert isinstance(data, dict)
        assert "Targets" in data
        assert "Observations" in data
        path.unlink()

    def test_csv_empty_rows(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            path = Path(f.name)
        export_csv([], path)
        assert path.read_text(encoding="utf-8") == ""
        path.unlink()


class TestPipeline:
    """Integration tests for the full pipeline."""

    def test_pipeline_keyvalue_csv(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            out = Path(f.name)
        rows = run_pipeline(
            input_paths=[KV_KML],
            output_path=out,
            output_format="csv",
        )
        assert len(rows) == 4
        assert out.stat().st_size > 0
        out.unlink()

    def test_pipeline_html_table_json(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out = Path(f.name)
        rows = run_pipeline(
            input_paths=[HTML_KML],
            output_path=out,
            output_format="json",
        )
        assert len(rows) == 3
        out.unlink()

    def test_pipeline_multiple_files(self):
        rows = run_pipeline(input_paths=[KV_KML, HTML_KML])
        assert len(rows) == 7


class TestConfig:
    """Tests for config loading."""

    def test_default_config(self):
        config = load_config()
        assert "field_map" in config
        assert "defaults" in config
        assert "output" in config

    def test_load_yaml_config(self):
        config_path = Path(__file__).parent.parent / "config" / "default-config.yml"
        if config_path.exists():
            config = load_config(config_path)
            assert config["field_map"]["ID"] == "id"
            assert config["field_map"]["PRTY"] == "priority"


class TestGeometry:
    """Tests for geometry WKT output."""

    def test_point_wkt(self):
        features = parse_file(KV_KML)
        alpha = [f for f in features if f.name == "Alpha Site"][0]
        wkt = alpha.geometry.to_wkt()
        assert wkt.startswith("POINT")

    def test_linestring_wkt(self):
        features = parse_file(KV_KML)
        route = [f for f in features if f.name == "Route Gamma"][0]
        wkt = route.geometry.to_wkt()
        assert wkt.startswith("LINESTRING")
        assert "," in wkt


class TestCloudflareProvider:
    """Tests for the Cloudflare Workers AI LLM provider."""

    def test_unknown_provider_error_message_includes_cloudflare(self):
        with pytest.raises(ValueError, match="cloudflare"):
            generate_formal_report("notes", provider="unknown_xyz")

    def test_cloudflare_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("CF_API_TOKEN", raising=False)
        monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
        monkeypatch.setenv("CF_ACCOUNT_ID", "test-account")
        with pytest.raises(EnvironmentError, match="CF_API_TOKEN"):
            generate_formal_report("notes", provider="cloudflare")

    def test_cloudflare_missing_account_id_raises(self, monkeypatch):
        monkeypatch.setenv("CF_API_TOKEN", "test-token")
        monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
        with pytest.raises(EnvironmentError, match="CF_ACCOUNT_ID"):
            generate_formal_report("notes", provider="cloudflare")

    def test_cloudflare_calls_openai_client(self, monkeypatch):
        monkeypatch.setenv("CF_API_TOKEN", "test-token")
        monkeypatch.setenv("CF_ACCOUNT_ID", "test-account-id")

        mock_message = MagicMock()
        mock_message.content = "# Formal Report\n\nContent here."
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_client) as mock_openai_cls:
            result = generate_formal_report("field notes here", provider="cloudflare")

        assert result == "# Formal Report\n\nContent here."
        mock_openai_cls.assert_called_once_with(
            api_key="test-token",
            base_url="https://api.cloudflare.com/client/v4/accounts/test-account-id/ai/v1",
        )
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "@cf/meta/llama-3.1-8b-instruct"

    def test_cloudflare_custom_model(self, monkeypatch):
        monkeypatch.setenv("CF_API_TOKEN", "test-token")
        monkeypatch.setenv("CF_ACCOUNT_ID", "test-account-id")

        mock_message = MagicMock()
        mock_message.content = "Report content."
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_client):
            result = generate_formal_report(
                "field notes",
                provider="cloudflare",
                model="@cf/mistral/mistral-7b-instruct-v0.1",
            )

        assert result == "Report content."
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "@cf/mistral/mistral-7b-instruct-v0.1"

    def test_cloudflare_custom_base_url(self, monkeypatch):
        monkeypatch.setenv("CF_API_TOKEN", "test-token")
        monkeypatch.setenv("CF_ACCOUNT_ID", "test-account-id")

        mock_message = MagicMock()
        mock_message.content = "Report."
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        custom_url = "https://custom.proxy/v1"
        with patch("openai.OpenAI", return_value=mock_client) as mock_openai_cls:
            generate_formal_report("notes", provider="cloudflare", base_url=custom_url)

        mock_openai_cls.assert_called_once_with(api_key="test-token", base_url=custom_url)

"""Description parsing and normalization.

Handles multiple description formats found in KML files:
1. KEY=VALUE; KEY2=VALUE2 plain-text style
2. HTML table rows with <td>key</td><td>value</td> pairs
3. Simple <div> text (treated as free-text notes)
"""

from __future__ import annotations

import re
from html import unescape
from typing import Optional

from lxml import etree


def parse_description(
    raw: str,
    field_map: dict[str, str] | None = None,
    defaults: dict[str, str] | None = None,
    separators: list[str] | None = None,
) -> dict[str, str]:
    """Parse a raw description string into a normalized attribute dict.

    Tries HTML-table extraction first, then KEY=VALUE parsing, then
    falls back to treating the whole string as a note.
    """
    field_map = field_map or {}
    defaults = defaults or {}
    separators = separators or [";", "\n", "<br>", "<br/>", "<br />"]

    attrs: dict[str, str] = {}

    if _looks_like_html(raw):
        attrs = _parse_html_table(raw)

    if not attrs and "=" in raw:
        attrs = _parse_key_value(raw, separators)

    if not attrs:
        text = _strip_html(raw).strip()
        if text:
            attrs["notes"] = text

    normalized = _apply_field_map(attrs, field_map)
    _apply_defaults(normalized, defaults)

    return normalized


def _looks_like_html(text: str) -> bool:
    """Check whether the description contains HTML markup."""
    return bool(re.search(r"<\s*(table|html|div|tr|td)\b", text, re.IGNORECASE))


def _parse_html_table(html: str) -> dict[str, str]:
    """Extract key-value pairs from HTML table rows.

    Expects rows like:
        <tr><td>Key</td><td>Value</td></tr>
    Also captures simple <div> text content as notes.
    """
    attrs: dict[str, str] = {}

    try:
        doc = etree.HTML(html)
    except Exception:
        return attrs

    rows = doc.xpath("//tr")
    for row in rows:
        cells = row.xpath(".//td")
        if len(cells) == 2:
            key = _text_content(cells[0]).strip()
            val = _text_content(cells[1]).strip()
            if key and val:
                attrs[key] = val
        elif len(cells) == 1:
            text = _text_content(cells[0]).strip()
            if text and "notes" not in attrs:
                attrs["_header"] = text

    if not attrs:
        divs = doc.xpath("//div")
        texts = [_text_content(d).strip() for d in divs if _text_content(d).strip()]
        if texts:
            attrs["notes"] = " ".join(texts)

    return attrs


def _parse_key_value(raw: str, separators: list[str]) -> dict[str, str]:
    """Parse KEY=VALUE pairs separated by configurable delimiters."""
    text = _strip_html(raw)

    for sep in separators:
        if sep in text:
            parts = text.split(sep)
            attrs = _extract_kv_parts(parts)
            if attrs:
                return attrs

    parts = [text]
    return _extract_kv_parts(parts)


def _extract_kv_parts(parts: list[str]) -> dict[str, str]:
    """Given a list of potential KEY=VALUE strings, extract pairs."""
    attrs: dict[str, str] = {}
    for part in parts:
        part = part.strip()
        if "=" in part:
            key, _, val = part.partition("=")
            key = key.strip()
            val = val.strip()
            if key:
                attrs[key] = val
    return attrs


def _apply_field_map(attrs: dict[str, str], field_map: dict[str, str]) -> dict[str, str]:
    """Rename keys according to the field map (case-insensitive matching)."""
    lower_map = {k.lower(): v for k, v in field_map.items()}
    normalized: dict[str, str] = {}
    for key, val in attrs.items():
        canonical = lower_map.get(key.lower(), key.lower().replace(" ", "_"))
        normalized[canonical] = val
    return normalized


def _apply_defaults(attrs: dict[str, str], defaults: dict[str, str]) -> None:
    """Fill in missing fields with default values."""
    for key, default_val in defaults.items():
        if key not in attrs:
            attrs[key] = default_val


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = unescape(clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def _text_content(el: etree._Element) -> str:
    """Get all text content from an lxml element."""
    return "".join(el.itertext())

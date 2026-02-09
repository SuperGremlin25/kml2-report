"""Reverse geocoder using OpenStreetMap Nominatim.

Looks up real street names, towns, and counties for lat/lon coordinates
via the free Nominatim API.  Results are cached to a JSON file so
subsequent runs are instant.

Usage policy: max 1 request/second, custom User-Agent required.
https://operations.osmfoundation.org/policies/nominatim/
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

_USER_AGENT = "kml2report/0.1 (field-report-generator)"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_REQUEST_DELAY = 1.1  # seconds between requests (Nominatim policy: 1 req/s)
_GRID_DECIMALS = 4    # round to ~11m grid for dedup


@dataclass
class LocationInfo:
    """Geocoded location data for a single coordinate."""
    road: str = ""
    town: str = ""
    county: str = ""
    state: str = ""
    postcode: str = ""
    display_name: str = ""

    @property
    def label(self) -> str:
        """Human-readable location summary."""
        parts: list[str] = []
        if self.road:
            parts.append(self.road)
        if self.town:
            parts.append(self.town)
        if self.county and self.county != self.town:
            parts.append(self.county)
        return ", ".join(parts) if parts else ""

    @property
    def short_label(self) -> str:
        """Compact label for table cells."""
        if self.road and self.town:
            return f"{self.road}, {self.town}"
        return self.road or self.town or self.county or ""


def _grid_key(lat: float, lon: float) -> str:
    """Round coords to a grid cell for deduplication."""
    return f"{round(lat, _GRID_DECIMALS)},{round(lon, _GRID_DECIMALS)}"


def _nominatim_lookup(lat: float, lon: float) -> LocationInfo:
    """Single Nominatim reverse geocode call."""
    url = (
        f"{_NOMINATIM_URL}?format=jsonv2&lat={lat}&lon={lon}"
        f"&addressdetails=1&zoom=18&layer=address"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return LocationInfo()

    addr = data.get("address", {})
    town = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("hamlet")
        or addr.get("suburb")
        or ""
    )
    return LocationInfo(
        road=addr.get("road", ""),
        town=town,
        county=addr.get("county", ""),
        state=addr.get("state", ""),
        postcode=addr.get("postcode", ""),
        display_name=data.get("display_name", ""),
    )


class GeoCache:
    """Disk-backed geocode cache."""

    def __init__(self, cache_path: Path | None = None):
        self._path = cache_path or Path(".geocache.json")
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def get(self, key: str) -> LocationInfo | None:
        if key in self._data:
            return LocationInfo(**self._data[key])
        return None

    def put(self, key: str, info: LocationInfo) -> None:
        self._data[key] = asdict(info)


def batch_reverse_geocode(
    coords: list[tuple[float, float]],
    cache_path: Path | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, LocationInfo]:
    """Reverse geocode a batch of coordinates with caching and rate limiting.

    Parameters
    ----------
    coords : list of (lat, lon) tuples
    cache_path : optional path for the JSON cache file
    progress_callback : optional fn(completed, total) for progress updates

    Returns
    -------
    dict mapping grid_key -> LocationInfo
    """
    cache = GeoCache(cache_path)

    unique_keys: dict[str, tuple[float, float]] = {}
    for lat, lon in coords:
        key = _grid_key(lat, lon)
        if key not in unique_keys:
            unique_keys[key] = (lat, lon)

    to_fetch: list[tuple[str, float, float]] = []
    for key, (lat, lon) in unique_keys.items():
        if cache.get(key) is None:
            to_fetch.append((key, lat, lon))

    total = len(to_fetch)
    for i, (key, lat, lon) in enumerate(to_fetch):
        if progress_callback:
            progress_callback(i, total)

        info = _nominatim_lookup(lat, lon)
        cache.put(key, info)

        if i < total - 1:
            time.sleep(_REQUEST_DELAY)

    cache.save()

    if progress_callback and total > 0:
        progress_callback(total, total)

    results: dict[str, LocationInfo] = {}
    for lat, lon in coords:
        key = _grid_key(lat, lon)
        info = cache.get(key)
        if info:
            results[key] = info

    return results


def geocode_single(lat: float, lon: float, cache_path: Path | None = None) -> LocationInfo:
    """Geocode a single coordinate (uses cache)."""
    result = batch_reverse_geocode([(lat, lon)], cache_path=cache_path)
    key = _grid_key(lat, lon)
    return result.get(key, LocationInfo())

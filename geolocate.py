#!/usr/bin/env python3
"""
geolocate.py

IP-based geolocation helper.

Behavior:
- If enabled, tries multiple IP geolocation providers (no API key).
- If all providers fail, caller can fall back to manual config.

Accuracy:
- Approximate (city / neighborhood level). Good enough for broad plane/sat tracking.

Providers:
- ipapi.co (latitude/longitude)
- ipinfo.io (loc "lat,lon")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import requests


@dataclass
class GeoResult:
    lat: float
    lon: float
    source: str
    accuracy: str  # "ip-approx"


def _try_ipapi(timeout: int = 8) -> Optional[GeoResult]:
    url = "https://ipapi.co/json/"
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    lat = j.get("latitude")
    lon = j.get("longitude")
    if lat is None or lon is None:
        return None
    return GeoResult(lat=float(lat), lon=float(lon), source="ipapi.co", accuracy="ip-approx")


def _try_ipinfo(timeout: int = 8) -> Optional[GeoResult]:
    url = "https://ipinfo.io/json"
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    loc = (j.get("loc") or "").strip()
    if not loc or "," not in loc:
        return None
    lat_s, lon_s = loc.split(",", 1)
    return GeoResult(lat=float(lat_s), lon=float(lon_s), source="ipinfo.io", accuracy="ip-approx")


def get_best_location(timeout: int = 8) -> GeoResult:
    errors = []
    for fn in (_try_ipapi, _try_ipinfo):
        try:
            res = fn(timeout=timeout)
            if res:
                return res
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError("All geolocation providers failed: " + " | ".join(errors))


def resolve_home_location(
    *,
    geolocation_enabled: bool,
    fallback_lat: float,
    fallback_lon: float,
    timeout: int = 8,
) -> Tuple[float, float, str]:
    """
    Returns (lat, lon, source_note)

    - If geolocation_enabled: use IP-based location; on failure fall back to manual coords.
    - If disabled: use manual coords.
    """
    if not geolocation_enabled:
        return fallback_lat, fallback_lon, "manual"

    try:
        res = get_best_location(timeout=timeout)
        return res.lat, res.lon, f"ip_geolocation:{res.source}"
    except Exception:
        return fallback_lat, fallback_lon, "ip_geolocation_failed_fallback_to_manual"

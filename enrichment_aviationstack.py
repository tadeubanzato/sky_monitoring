#!/usr/bin/env python3
"""
enrichment_aviationstack.py

Best-effort flight enrichment via aviationstack.

Purpose:
- ONLY used as a fallback for "unknown / small" flights when other providers miss.
- Query by callsign/flight_icao (e.g., CNS1111), normalize output to match our
  existing `meta["enriched"]` shape as closely as possible.

Env (.env):
- AVIATIONSTACK_API_KEY=...

Notes:
- Free plan rate-limit can be 1 req / 60s. We cache results on disk to avoid
  burning quota and to keep the service stable across restarts.
- This module never raises; it returns (None, status) on any failure.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

AVIATIONSTACK_FLIGHTS_URL = "https://api.aviationstack.com/v1/flights"

# Disk cache (persists across restarts)
_CACHE_PATH = Path("data/aviationstack_cache.json")
_DEFAULT_TTL_S = 12 * 60 * 60  # 12 hours

# In-memory cache (fast)
_MEM_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}

_ENV_LOADED = False


# ─────────────────────────────────────────────
# Minimal .env loader (no dependency)
# ─────────────────────────────────────────────
def load_dotenv(path: str = ".env") -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    p = Path(path)
    if not p.exists():
        return

    try:
        for raw_line in p.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        return


def get_aviationstack_api_key() -> str:
    load_dotenv()
    return os.getenv("AVIATIONSTACK_API_KEY", "").strip()


def _normalize_callsign(code: str) -> str:
    # Keep it simple: uppercase, strip spaces
    return (code or "").strip().upper().replace(" ", "")


def _read_disk_cache() -> Dict[str, Any]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_disk_cache(d: Dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Never break enrichment due to cache write failures
        return


def _cache_get(key: str, ttl_s: int) -> Optional[Dict[str, Any]]:
    now = time.time()

    # 1) mem cache
    m = _MEM_CACHE.get(key)
    if m:
        ts, val = m
        if (now - ts) <= float(ttl_s):
            return val or None

    # 2) disk cache
    dc = _read_disk_cache()
    ent = dc.get(key)
    if isinstance(ent, dict):
        ts = float(ent.get("ts", 0.0) or 0.0)
        val = ent.get("val")
        if (now - ts) <= float(ttl_s) and isinstance(val, dict):
            _MEM_CACHE[key] = (now, val)
            return val

    return None


def _cache_put(key: str, val: Optional[Dict[str, Any]]) -> None:
    now = time.time()
    _MEM_CACHE[key] = (now, val)

    dc = _read_disk_cache()
    dc[key] = {"ts": now, "val": val or None}
    _write_disk_cache(dc)


def _pick_best_record(records: Any) -> Optional[Dict[str, Any]]:
    """
    aviationstack can return multiple records. We pick the "best" one:
    - Prefer live records
    - Prefer those with both dep/arr codes present
    - Otherwise first dict
    """
    if not isinstance(records, list) or not records:
        return None

    best = None
    best_score = -1

    for r in records:
        if not isinstance(r, dict):
            continue

        dep = (((r.get("departure") or {}) if isinstance(r.get("departure"), dict) else {}) or {}).get("iata") or ""
        arr = (((r.get("arrival") or {}) if isinstance(r.get("arrival"), dict) else {}) or {}).get("iata") or ""
        live = r.get("live")
        has_live = isinstance(live, dict)

        score = 0
        if has_live:
            score += 2
        if dep:
            score += 1
        if arr:
            score += 1

        if score > best_score:
            best_score = score
            best = r

    return best if isinstance(best, dict) else None


def normalize_aviationstack(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize into our common `meta["enriched"]` structure (closest match to AirLabs).
    """
    flight = rec.get("flight") if isinstance(rec.get("flight"), dict) else {}
    airline = rec.get("airline") if isinstance(rec.get("airline"), dict) else {}
    aircraft = rec.get("aircraft") if isinstance(rec.get("aircraft"), dict) else {}
    dep = rec.get("departure") if isinstance(rec.get("departure"), dict) else {}
    arr = rec.get("arrival") if isinstance(rec.get("arrival"), dict) else {}
    live = rec.get("live") if isinstance(rec.get("live"), dict) else {}

    dep_code = dep.get("iata") or dep.get("icao") or "???"
    arr_code = arr.get("iata") or arr.get("icao") or "???"

    out: Dict[str, Any] = {
        # identity-ish
        "hex": (live.get("icao24") if isinstance(live, dict) else None),
        "reg_number": aircraft.get("registration"),
        "aircraft_icao": aircraft.get("icao"),

        # operator/company
        "airline_icao": airline.get("icao"),
        "airline_iata": airline.get("iata"),
        "company": airline.get("name") or "Unknown",

        # flight number
        "flight_icao": flight.get("icao"),
        "flight_iata": flight.get("iata"),
        "flight_number": flight.get("number"),

        # route
        "dep_iata": dep.get("iata"),
        "dep_icao": dep.get("icao"),
        "arr_iata": arr.get("iata"),
        "arr_icao": arr.get("icao"),
        "route": f"{dep_code}→{arr_code}",

        # motion/state (best effort)
        "alt_ft": live.get("altitude"),      # unknown unit (provider-dependent)
        "trk_deg": live.get("direction"),
        "speed_raw": live.get("speed"),
        "speed_kt": None,                    # do not guess units
        "v_speed": None,

        "status": rec.get("flight_status"),
        "updated": None,
        "type": None,
        "flag": None,

        "lat": live.get("latitude"),
        "lng": live.get("longitude"),

        # provenance
        "source": "aviationstack",
    }
    return out


def enrich_plane_aviationstack(
    flight_icao: str,
    *,
    timeout: int = 12,
    cache_ttl_seconds: int = _DEFAULT_TTL_S,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Returns: (normalized_enriched_or_none, status_string)
    """
    key = get_aviationstack_api_key()
    if not key:
        return None, "aviationstack:disabled:no_key"

    fc = _normalize_callsign(flight_icao)
    if not fc:
        return None, "aviationstack:skip:empty"

    cache_key = f"flight_icao:{fc}"
    cached = _cache_get(cache_key, ttl_s=int(cache_ttl_seconds))
    if cached:
        return cached, "aviationstack:cache"

    try:
        r = requests.get(
            AVIATIONSTACK_FLIGHTS_URL,
            params={"access_key": key, "flight_icao": fc},
            timeout=timeout,
        )
        if r.status_code != 200:
            _cache_put(cache_key, None)
            return None, f"aviationstack:http:{r.status_code}"

        data = r.json()
        records = data.get("data")
        rec = _pick_best_record(records)
        if not rec:
            _cache_put(cache_key, None)
            return None, "aviationstack:no_match"

        norm = normalize_aviationstack(rec)
        _cache_put(cache_key, norm)
        return norm, "aviationstack:ok"

    except Exception as e:
        _cache_put(cache_key, None)
        return None, f"aviationstack:error:{e}"
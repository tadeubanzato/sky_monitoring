#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

AIRLABS_FLIGHTS_URL = "https://airlabs.co/api/v9/flights"

# Cache maps in-memory (fast, avoids repeated disk reads)
_AIRLINES_FLAT: Optional[Dict[str, str]] = None
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
        # Never break enrichment due to env parsing
        return


def get_airlabs_api_key() -> str:
    load_dotenv()
    return os.getenv("AIRLABS_API_KEY", "").strip()


# ─────────────────────────────────────────────
# Airlines map (region-grouped JSON -> flattened)
# ─────────────────────────────────────────────

def load_airlines_flat(path: str = "data/airlines_by_region.json") -> Dict[str, str]:
    global _AIRLINES_FLAT
    if _AIRLINES_FLAT is not None:
        return _AIRLINES_FLAT

    p = Path(path)
    if not p.exists():
        _AIRLINES_FLAT = {}
        return _AIRLINES_FLAT

    try:
        by_region = json.loads(p.read_text(encoding="utf-8"))
        flat: Dict[str, str] = {}
        if isinstance(by_region, dict):
            for _, region_map in by_region.items():
                if isinstance(region_map, dict):
                    for code, name in region_map.items():
                        if isinstance(code, str) and isinstance(name, str):
                            flat[code.strip()] = name.strip()
        _AIRLINES_FLAT = flat
    except Exception:
        _AIRLINES_FLAT = {}

    return _AIRLINES_FLAT


def airline_code_to_company(airline_icao: Optional[str], airline_iata: Optional[str]) -> str:
    flat = load_airlines_flat()
    code = (airline_icao or airline_iata or "").strip()
    if not code:
        return "Unknown"
    return flat.get(code, code)


# ─────────────────────────────────────────────
# Normalizers
# ─────────────────────────────────────────────

_FLIGHT_RE = re.compile(r"[^A-Z0-9]+")


def normalize_hex(hex_code: str) -> str:
    return (hex_code or "").strip().lower()


def normalize_flight_code(code: Optional[str]) -> str:
    """
    AirLabs 'flight_icao' expects strings like:
      UAL2048, JBU1124, AAL3330, etc.

    OpenSky callsigns can contain spaces or junk:
      "UAL2048 ", " UAL 2048", "UAL2048/...", etc.

    We sanitize to uppercase alnum only.
    """
    if not code:
        return ""
    s = str(code).strip().upper()
    s = _FLIGHT_RE.sub("", s)  # keep only A-Z0-9
    return s


def build_alt_flight_variants(flight_code: str) -> Tuple[str, ...]:
    """
    Some carriers are ambiguous between ICAO(3 letters) and IATA(2 letters).
    Example: UAL2048 (United ICAO) vs UA2048 (United IATA).

    We attempt a minimal fallback:
      - If starts with 3 letters + digits -> also try 2 letters + same digits (drop 3rd letter)
      - If starts with 2 letters + digits -> also try 3 letters + same digits (cannot infer reliably; skip)
    """
    fc = normalize_flight_code(flight_code)
    if len(fc) < 4:
        return tuple()

    prefix = fc[:3]
    rest = fc[3:]
    if prefix.isalpha() and rest.isdigit():
        # Try "UA2048" from "UAL2048" (drop last letter)
        return (fc[:2] + rest,)
    return tuple()


# ─────────────────────────────────────────────
# Speed helpers
# AirLabs speed can be source-dependent. We preserve raw + provide a best-effort kt.
# ─────────────────────────────────────────────

def kmh_to_kt(kmh: float) -> float:
    return kmh * 0.539956803


def maybe_speed_to_knots(speed_raw: Optional[float]) -> Optional[int]:
    """
    Best-effort conversion heuristic:
    - If speed is very high (>500), treat as km/h and convert to knots.
    - Else treat as knots already.
    """
    if speed_raw is None:
        return None
    try:
        s = float(speed_raw)
    except Exception:
        return None
    if s > 500:
        return int(round(kmh_to_kt(s)))
    return int(round(s))


# ─────────────────────────────────────────────
# AirLabs fetch (best-effort)
# ─────────────────────────────────────────────

def _airlabs_get(params: Dict[str, Any], timeout: int = 10) -> Optional[Dict[str, Any]]:
    key = get_airlabs_api_key()
    if not key:
        return None

    p = dict(params)
    p["api_key"] = key

    r = requests.get(AIRLABS_FLIGHTS_URL, params=p, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_airlabs_record_by_hex(hex_code: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    hx = normalize_hex(hex_code)
    if not hx:
        return None
    try:
        data = _airlabs_get({"hex": hx}, timeout=timeout)
        if not data:
            return None
        resp = data.get("response", [])
        if isinstance(resp, list) and resp:
            rec = resp[0]
            return rec if isinstance(rec, dict) else None
        return None
    except Exception:
        return None


def fetch_airlabs_record_by_flight(flight_icao: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    fc = normalize_flight_code(flight_icao)
    if not fc:
        return None
    try:
        data = _airlabs_get({"flight_icao": fc}, timeout=timeout)
        if not data:
            return None
        resp = data.get("response", [])
        if isinstance(resp, list) and resp:
            rec = resp[0]
            return rec if isinstance(rec, dict) else None
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────
# Normalize enrichment output
# This is what you attach to event.meta["enriched"]
# ─────────────────────────────────────────────

def normalize_airlabs(rec: Dict[str, Any]) -> Dict[str, Any]:
    airline_icao = rec.get("airline_icao")
    airline_iata = rec.get("airline_iata")

    dep = rec.get("dep_iata") or rec.get("dep_icao")
    arr = rec.get("arr_iata") or rec.get("arr_icao")

    out: Dict[str, Any] = {
        # identity
        "hex": rec.get("hex"),
        "reg_number": rec.get("reg_number"),
        "aircraft_icao": rec.get("aircraft_icao"),

        # operator / company
        "airline_icao": airline_icao,
        "airline_iata": airline_iata,
        "company": airline_code_to_company(
            airline_icao if isinstance(airline_icao, str) else None,
            airline_iata if isinstance(airline_iata, str) else None
        ),

        # flight number
        "flight_icao": rec.get("flight_icao"),
        "flight_iata": rec.get("flight_iata"),
        "flight_number": rec.get("flight_number"),

        # route
        "dep_iata": rec.get("dep_iata"),
        "dep_icao": rec.get("dep_icao"),
        "arr_iata": rec.get("arr_iata"),
        "arr_icao": rec.get("arr_icao"),
        "route": f"{dep or '???'}→{arr or '???'}",

        # motion/state (optional but useful)
        "alt_ft": rec.get("alt"),
        "trk_deg": rec.get("dir"),
        "speed_raw": rec.get("speed"),
        "speed_kt": maybe_speed_to_knots(rec.get("speed")),
        "v_speed": rec.get("v_speed"),
        "status": rec.get("status"),
        "updated": rec.get("updated"),
        "type": rec.get("type"),
        "flag": rec.get("flag"),
        "lat": rec.get("lat"),
        "lng": rec.get("lng"),
    }
    return out


def format_option_a(enriched: Dict[str, Any]) -> str:
    company = enriched.get("company") or "Unknown"
    flight = enriched.get("flight_iata") or enriched.get("flight_icao") or enriched.get("flight_number") or "UNK"
    dep = enriched.get("dep_iata") or enriched.get("dep_icao") or "???"
    arr = enriched.get("arr_iata") or enriched.get("arr_icao") or "???"
    return f"{company} {flight}  {dep}→{arr}"


# ─────────────────────────────────────────────
# One-call helper for trackers
# ─────────────────────────────────────────────

def enrich_plane(hex_code: str, flight_icao: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Best-effort enrichment:
      1) Try by hex (best)
      2) Fallback by flight_icao (sanitized)
      3) Fallback by alternate variant (e.g., UAL2048 -> UA2048)

    Returns: (normalized_enriched_or_none, status_string)
    """
    rec = fetch_airlabs_record_by_hex(hex_code)
    if rec:
        return normalize_airlabs(rec), "airlabs:ok:hex"

    if flight_icao:
        fc = normalize_flight_code(flight_icao)
        if fc:
            rec = fetch_airlabs_record_by_flight(fc)
            if rec:
                return normalize_airlabs(rec), "airlabs:ok:flight"

            # Try alternate variants (limited and safe)
            for alt in build_alt_flight_variants(fc):
                rec = fetch_airlabs_record_by_flight(alt)
                if rec:
                    return normalize_airlabs(rec), f"airlabs:ok:flight_alt:{alt}"

    return None, "airlabs:no_match"
#!/usr/bin/env python3
"""
enrichment_adsbdb.py

Best-effort enrichment using ADSBDB public API (no API key).

We return:
- normalized: stable minimal structure for route/airline in event meta
- raw: full ADSBDB JSON response (so message formatting can pick what it needs)
- status: string describing outcome
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import requests

ADSBDB_URL = "https://api.adsbdb.com/v0/callsign"


def _norm_callsign(callsign: str) -> str:
    return (callsign or "").strip().upper().replace(" ", "")


def enrich_callsign_adsbdb(
    callsign: str,
    *,
    timeout_s: int = 10,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """
    Returns: (normalized_or_None, raw_or_None, status)

    normalized example:
    {
      "route": {"from": "SUJ", "to": "OTP", "from_kind": "iata", "to_kind": "iata", ...},
      "airline": {"name": "Tarom", "icao": "ROT", "iata": "RO", ...},
      "callsign_iata": "RO632",
      "callsign_icao": "ROT632",
      "source": "adsbdb"
    }
    """
    cs = _norm_callsign(callsign)

    # Skip obvious junk callsigns
    if not cs or cs.startswith("(") or cs in {"NOCALLSIGN", "NO_CALLSIGN"}:
        return None, None, "adsbdb:skip:bad_callsign"

    url = f"{ADSBDB_URL}/{cs}"

    try:
        r = requests.get(url, timeout=timeout_s)
    except Exception as e:
        return None, None, f"adsbdb:error:{e}"

    if r.status_code == 404:
        return None, None, "adsbdb:unknown"
    if r.status_code != 200:
        return None, None, f"adsbdb:http:{r.status_code}"

    try:
        raw = r.json()
    except Exception:
        return None, None, "adsbdb:non_json"

    fr = (((raw or {}).get("response") or {}).get("flightroute")) or {}
    origin = fr.get("origin") or {}
    dest = fr.get("destination") or {}
    airline = fr.get("airline") or {}

    # Prefer IATA route codes, fallback ICAO
    from_code = origin.get("iata_code") or origin.get("icao_code")
    to_code = dest.get("iata_code") or dest.get("icao_code")

    if not from_code or not to_code:
        # Still return raw for debugging/inspection
        return None, raw, "adsbdb:missing_route"

    normalized: Dict[str, Any] = {
        "route": {
            "from": from_code,
            "to": to_code,
            "from_kind": "iata" if origin.get("iata_code") else "icao",
            "to_kind": "iata" if dest.get("iata_code") else "icao",
            "from_name": origin.get("name"),
            "to_name": dest.get("name"),
            "from_city": origin.get("municipality"),
            "to_city": dest.get("municipality"),
            "from_country": origin.get("country_name"),
            "to_country": dest.get("country_name"),
        },
        "airline": {
            "name": airline.get("name"),
            "icao": airline.get("icao"),
            "iata": airline.get("iata"),
            "callsign": airline.get("callsign"),
            "country": airline.get("country"),
            "country_iso": airline.get("country_iso"),
        },
        "callsign_iata": fr.get("callsign_iata"),
        "callsign_icao": fr.get("callsign_icao"),
        "source": "adsbdb",
    }

    return normalized, raw, "adsbdb:ok"
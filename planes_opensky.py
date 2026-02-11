#!/usr/bin/env python3
"""
planes_opensky.py

Plane tracker using the OpenSky Network States API.

Behavior:
- Polls OpenSky for aircraft within a bounding box around your home location.
- Computes distance to home via haversine.
- Emits ENTER when a plane crosses into radius_km.
- Emits EXIT when a plane leaves radius_km (with hysteresis/confirmation to prevent boundary flapping).
- Adds OVERHEAD event when a plane gets very close to your home (useful "alert" event, but no notifications in this OSS repo).

Enrichment:
- Always tries ADSBDB first (free / no key; best effort).
- Optionally tries AirLabs (requires key).
- Optionally tries Aviationstack (requires key; cache to reduce usage).

Notes:
- The OpenSky callsign can be ICAO-ish (e.g., UAL70) even when IATA is desired.
  We keep a stable "flight_id" for consistent ENTER/OVERHEAD/EXIT lines.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from enrichment_adsbdb import enrich_adsbdb_best_effort
from enrichment_airlabs import enrich_airlabs_best_effort
from enrichment_aviationstack import enrich_aviationstack_best_effort
from event_writer import Event, EventWriter, utc_now_iso


# ─────────────────────────────────────────────
# Geometry
# ─────────────────────────────────────────────

EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Great-circle distance between two points on Earth.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def normalize_callsign(s: str) -> str:
    if not s:
        return ""
    return "".join(ch for ch in s.strip().upper() if ch.isalnum())


def _route_is_present(meta: Dict[str, Any]) -> bool:
    r = meta.get("route")
    if isinstance(r, dict):
        a = (r.get("from") or "").strip()
        b = (r.get("to") or "").strip()
        return bool(a and b)
    return False


def _route_str_from_meta(meta: Dict[str, Any]) -> str:
    """Best-effort stable route string for display."""
    r = meta.get("route")
    if isinstance(r, dict):
        a = (r.get("from") or "").strip()
        b = (r.get("to") or "").strip()
        if a and b:
            return f"{a}→{b}"

    # Some enrichers may provide a string route directly
    enr = meta.get("enriched")
    if isinstance(enr, dict):
        s = enr.get("route")
        if isinstance(s, str) and s.strip():
            return s.strip()

        dep = enr.get("dep_iata") or enr.get("dep_icao")
        arr = enr.get("arr_iata") or enr.get("arr_icao")
        if dep and arr:
            return f"{str(dep).strip()}→{str(arr).strip()}"

    dep2 = meta.get("dep") or "???"
    arr2 = meta.get("arr") or "???"
    return f"{dep2}→{arr2}"


def _flight_id_from_meta(meta: Dict[str, Any], callsign_raw: str) -> str:
    """Prefer a consistent flight identifier across ENTER/OVERHEAD/EXIT."""
    for k in ("callsign_iata", "callsign_icao", "flight_iata", "flight_icao", "flight_number"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            return normalize_callsign(v)

    enr = meta.get("enriched")
    if isinstance(enr, dict):
        for k in ("flight_iata", "flight_icao", "flight_number"):
            v = enr.get(k)
            if isinstance(v, str) and v.strip():
                return normalize_callsign(v)

    cs = normalize_callsign(callsign_raw)
    return cs or "UNK"


def _company_from_meta(meta: Dict[str, Any]) -> str:
    airline = meta.get("airline")
    if isinstance(airline, dict):
        nm = airline.get("name")
        if isinstance(nm, str) and nm.strip():
            return nm.strip()

        for k in ("icao", "iata"):
            c = airline.get(k)
            if isinstance(c, str) and c.strip():
                return c.strip().upper()

    enr = meta.get("enriched")
    if isinstance(enr, dict):
        company = enr.get("company")
        if isinstance(company, str) and company.strip():
            return company.strip()

        code = enr.get("airline_icao") or enr.get("airline_iata")
        if isinstance(code, str) and code.strip():
            return code.strip().upper()

    code2 = meta.get("airline_icao") or meta.get("airline_iata")
    if isinstance(code2, str) and code2.strip():
        return code2.strip().upper()

    return "Unknown"


def _display_from_meta(meta: Dict[str, Any], callsign_raw: str) -> str:
    """Stable single-line display string."""
    company = _company_from_meta(meta)
    flight_id = _flight_id_from_meta(meta, callsign_raw)
    route_str = _route_str_from_meta(meta)
    return f"{company} {flight_id}  {route_str}"


# ─────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────

@dataclass
class PlaneState:
    icao24: str
    callsign: str
    lat: float
    lon: float
    alt_ft: Optional[float]
    spd_kt: Optional[float]
    trk_deg: Optional[float]


# ─────────────────────────────────────────────
# OpenSky tracker
# ─────────────────────────────────────────────

class OpenSkyPlaneTracker:
    def __init__(
        self,
        *,
        writer: EventWriter,
        home_lat: float,
        home_lon: float,
        radius_km: float,
        overhead_radius_km: float,
        overhead_cooldown_seconds: float,
        poll_seconds: float,
        disappear_grace_seconds: float,
        bbox_radius_km: float,
        airlabs_enabled: bool,
        aviationstack_enabled: bool,
        exit_hysteresis_m: float = 750.0,
        exit_confirm_polls: int = 2,
    ) -> None:
        self.writer = writer
        self.home_lat = float(home_lat)
        self.home_lon = float(home_lon)

        self.radius_m = radius_km * 1000.0
        # Hysteresis avoids noisy ENTER/EXIT spam when a plane rides the boundary.
        # We consider a plane "inside" when dist <= radius.
        # We only consider it "outside" (eligible to EXIT) when dist >= radius + exit_hysteresis_m.
        self.exit_hysteresis_m = max(0.0, float(exit_hysteresis_m))
        self.exit_confirm_polls = max(1, int(exit_confirm_polls))

        self.overhead_m = overhead_radius_km * 1000.0
        self.overhead_cooldown_seconds = float(overhead_cooldown_seconds)

        self.poll_seconds = max(0.5, float(poll_seconds))
        self.disappear_grace_seconds = max(0.0, float(disappear_grace_seconds))

        self.bbox_radius_km = max(1.0, float(bbox_radius_km))

        self.airlabs_enabled = bool(airlabs_enabled)
        self.aviationstack_enabled = bool(aviationstack_enabled)

        self.session = requests.Session()

        # Tracking state
        self._inside: Dict[str, float] = {}  # icao24 -> last_seen_inside_ts
        self._outside_counts: Dict[str, int] = {}  # icao24 -> consecutive outside polls
        self._last_seen_any: Dict[str, float] = {}  # icao24 -> ts
        self._last_dist_m: Dict[str, float] = {}
        self._last_position: Dict[str, Tuple[float, float]] = {}

        self._plane_cache: Dict[str, Dict[str, Any]] = {}  # icao24 -> last meta we printed/enriched

        # OVERHEAD cooldown
        self._last_overhead_sent: Dict[str, float] = {}  # icao24 -> ts

    def _bbox(self) -> Tuple[float, float, float, float]:
        """
        Approximate bounding box (degrees) around home.
        """
        lat = self.home_lat
        lon = self.home_lon
        dlat = self.bbox_radius_km / 111.0
        dlon = self.bbox_radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
        return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)

    def _fetch_opensky_states(self) -> List[PlaneState]:
        """
        OpenSky states endpoint:
          https://opensky-network.org/api/states/all?lamin=&lomin=&lamax=&lomax=
        """
        lamin, lamax, lomin, lomax = self._bbox()
        url = "https://opensky-network.org/api/states/all"
        params = {"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax}

        r = self.session.get(url, params=params, timeout=10)
        r.raise_for_status()
        payload = r.json()

        states = payload.get("states") or []
        out: List[PlaneState] = []

        for row in states:
            # Row format:
            # [0]=icao24, [1]=callsign, [5]=lon, [6]=lat, [7]=baro_alt(m), [9]=vel(m/s), [10]=heading(deg)
            try:
                icao24 = (row[0] or "").strip().lower()
                callsign = (row[1] or "").strip()
                lon = float(row[5]) if row[5] is not None else None
                lat = float(row[6]) if row[6] is not None else None
            except Exception:
                continue

            if not icao24 or lat is None or lon is None:
                continue

            alt_m = row[7]
            vel_ms = row[9]
            trk = row[10]

            alt_ft = float(alt_m) * 3.28084 if isinstance(alt_m, (int, float)) else None
            spd_kt = float(vel_ms) * 1.94384 if isinstance(vel_ms, (int, float)) else None
            trk_deg = float(trk) if isinstance(trk, (int, float)) else None

            out.append(
                PlaneState(
                    icao24=icao24,
                    callsign=callsign or "(NOCALLSIGN)",
                    lat=float(lat),
                    lon=float(lon),
                    alt_ft=alt_ft,
                    spd_kt=spd_kt,
                    trk_deg=trk_deg,
                )
            )

        return out

    def _cache_plane_live_meta(self, icao24: str, p: PlaneState, meta: Dict[str, Any]) -> None:
        cached = self._plane_cache.get(icao24, {})
        cached.update(
            {
                "callsign": p.callsign,
                "callsign_norm": normalize_callsign(p.callsign),
                "country": meta.get("country"),
                "alt_ft": meta.get("alt_ft"),
                "spd_kt": meta.get("spd_kt"),
                "trk_deg": meta.get("trk_deg"),
                "route": meta.get("route"),
                "airline": meta.get("airline"),
                "adsbdb_status": meta.get("adsbdb_status"),
                "airlabs_status": meta.get("airlabs_status"),
                "aviationstack_status": meta.get("aviationstack_status"),
                "enrich_source": meta.get("enrich_source"),
                "flight_id": meta.get("flight_id"),
                "route_str": meta.get("route_str"),
                "display": meta.get("display"),
            }
        )
        self._plane_cache[icao24] = cached

    def tick(self) -> None:
        now = time.time()

        # Fetch latest OpenSky states
        try:
            planes = self._fetch_opensky_states()
        except Exception as e:
            self.writer.emit(
                Event(
                    event="WARN",
                    kind="plane",
                    id="opensky",
                    label="opensky_fetch_failed",
                    ts=utc_now_iso(),
                    meta={"error": str(e)},
                )
            )
            return

        seen_now: Dict[str, PlaneState] = {}
        currently_inside: Dict[str, PlaneState] = {}

        enter_thr = float(self.radius_m)
        exit_thr = float(self.radius_m) + float(self.exit_hysteresis_m)

        for p in planes:
            seen_now[p.icao24] = p
            self._last_seen_any[p.icao24] = now

            dist_m = haversine_m(self.home_lat, self.home_lon, p.lat, p.lon)
            self._last_dist_m[p.icao24] = dist_m
            self._last_position[p.icao24] = (p.lat, p.lon)

            # Hysteresis:
            # - ENTER when dist <= enter_thr
            # - Stay inside while dist < exit_thr (prevents boundary flapping)
            was_inside = p.icao24 in self._inside
            if dist_m <= enter_thr or (was_inside and dist_m < exit_thr):
                currently_inside[p.icao24] = p
                # refresh last-seen-inside timestamp
                self._inside[p.icao24] = now

        # ENTER events
        for icao24, p in currently_inside.items():
            is_new = icao24 not in self._plane_cache

            meta: Dict[str, Any] = {
                "country": None,
                "alt_ft": round(p.alt_ft, 0) if p.alt_ft is not None else None,
                "spd_kt": round(p.spd_kt, 0) if p.spd_kt is not None else None,
                "trk_deg": round(p.trk_deg, 0) if p.trk_deg is not None else None,
                "distance_m": round(self._last_dist_m.get(icao24, 0.0), 1),
                "route": {"from": "???", "to": "???"},
                "airline": {"name": "Unknown"},
                "adsbdb_status": None,
                "airlabs_status": None,
                "aviationstack_status": None,
                "enrich_source": None,
            }

            # Enrichment (best-effort, ordered)
            adsb = enrich_adsbdb_best_effort(icao24)
            if isinstance(adsb, dict):
                meta.update(adsb)

            if self.airlabs_enabled:
                air = enrich_airlabs_best_effort(icao24)
                if isinstance(air, dict):
                    meta.update(air)

            # Aviationstack: only if we still don't have route info
            if self.aviationstack_enabled and not _route_is_present(meta):
                avs = enrich_aviationstack_best_effort(icao24)
                if isinstance(avs, dict):
                    meta.update(avs)

            # Build stable identifiers for consistent ENTER/OVERHEAD/EXIT lines
            meta["route_str"] = _route_str_from_meta(meta)
            meta["flight_id"] = _flight_id_from_meta(meta, p.callsign)
            meta["display"] = _display_from_meta(meta, p.callsign)

            cached = self._plane_cache.get(icao24, {})
            cached["route_str"] = meta["route_str"]
            cached["flight_id"] = meta["flight_id"]
            cached["display"] = meta["display"]
            self._plane_cache[icao24] = cached

            # Cache what we know so EXIT/OVERHEAD can be consistent
            self._cache_plane_live_meta(icao24, p, meta)

            if is_new:
                self.writer.emit(
                    Event(
                        event="ENTER",
                        kind="plane",
                        id=icao24,
                        label=str(meta.get("flight_id") or normalize_callsign(p.callsign) or p.callsign),
                        ts=utc_now_iso(),
                        meta=meta,
                    )
                )

        # OVERHEAD event (special "close" threshold)
        for icao24, p in currently_inside.items():
            dist_m = float(self._last_dist_m.get(icao24, 9e9))
            if dist_m > self.overhead_m:
                continue

            last_sent = self._last_overhead_sent.get(icao24, 0.0)
            if now - last_sent < self.overhead_cooldown_seconds:
                continue

            cached = self._plane_cache.get(icao24, {})
            label = cached.get("flight_id") or normalize_callsign(cached.get("callsign", "")) or cached.get("callsign") or p.callsign or icao24

            meta: Dict[str, Any] = {
                "distance_m": round(dist_m, 1),
                "alt_ft": round(p.alt_ft, 0) if p.alt_ft is not None else None,
                "spd_kt": round(p.spd_kt, 0) if p.spd_kt is not None else None,
                "trk_deg": round(p.trk_deg, 0) if p.trk_deg is not None else None,
                "cooldown_s": self.overhead_cooldown_seconds,
                "flight_id": cached.get("flight_id"),
                "route_str": cached.get("route_str"),
                "display": cached.get("display"),
            }

            # Include enrichment resolved at ENTER
            for k in (
                "country",
                "route",
                "airline",
                "adsbdb_status",
                "airlabs_status",
                "aviationstack_status",
                "enrich_source",
                "callsign_iata",
                "callsign_icao",
                "flight_id",
                "route_str",
                "display",
            ):
                if k in cached and cached[k] is not None:
                    meta[k] = cached[k]

            self.writer.emit(
                Event(
                    event="OVERHEAD",
                    kind="plane",
                    id=icao24,
                    label=str(label),
                    ts=utc_now_iso(),
                    meta=meta,
                )
            )
            self._last_overhead_sent[icao24] = now

        # EXIT events (kept for tracking)
        to_remove: List[str] = []

        enter_thr = float(self.radius_m)
        exit_thr = float(self.radius_m) + float(self.exit_hysteresis_m)

        for icao24, last_inside_ts in list(self._inside.items()):
            # Still inside -> reset outside counter
            if icao24 in currently_inside:
                self._outside_counts.pop(icao24, None)
                continue

            # If we didn't see it at all this poll, wait for grace period then EXIT as signal_lost
            if icao24 not in seen_now:
                last_any = self._last_seen_any.get(icao24, 0.0)
                if now - max(last_inside_ts, last_any) < self.disappear_grace_seconds:
                    continue
                reason = "signal_lost"

            else:
                # We saw it, but it is outside the ENTER threshold.
                # Only count it as "outside" if it's beyond exit_thr.
                dist_now = float(self._last_dist_m.get(icao24, 9e9))
                if dist_now < exit_thr:
                    # In the buffer zone -> keep it "inside" to prevent flapping
                    continue

                cnt = int(self._outside_counts.get(icao24, 0)) + 1
                self._outside_counts[icao24] = cnt
                if cnt < self.exit_confirm_polls:
                    continue
                reason = "out_of_radius"

            cached = self._plane_cache.get(icao24, {})
            label = cached.get("flight_id") or cached.get("callsign_norm") or cached.get("callsign") or icao24

            self.writer.emit(
                Event(
                    event="EXIT",
                    kind="plane",
                    id=icao24,
                    label=str(label),
                    ts=utc_now_iso(),
                    meta={
                        "reason": reason,
                        "country": cached.get("country"),
                        "alt_ft": cached.get("alt_ft"),
                        "spd_kt": cached.get("spd_kt"),
                        "trk_deg": cached.get("trk_deg"),
                        "last_dist_m": round(float(self._last_dist_m.get(icao24, 0.0)), 1),
                        "last_pos": self._last_position.get(icao24),
                        "route": cached.get("route"),
                        "route_str": cached.get("route_str"),
                        "airline": cached.get("airline"),
                        "flight_id": cached.get("flight_id"),
                        "display": cached.get("display"),
                        "adsbdb_status": cached.get("adsbdb_status"),
                        "airlabs_status": cached.get("airlabs_status"),
                        "aviationstack_status": cached.get("aviationstack_status"),
                        "enrich_source": cached.get("enrich_source"),
                        "exit_threshold_m": round(exit_thr, 1),
                        "enter_threshold_m": round(enter_thr, 1),
                        "exit_confirm_polls": self.exit_confirm_polls,
                    },
                )
            )

            to_remove.append(icao24)

        for icao24 in to_remove:
            self._inside.pop(icao24, None)
            self._plane_cache.pop(icao24, None)
            self._last_dist_m.pop(icao24, None)
            self._last_position.pop(icao24, None)
            self._last_seen_any.pop(icao24, None)
            self._outside_counts.pop(icao24, None)
            # Do NOT clear _last_overhead_sent (keeps cooldown across brief gaps)

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:
                self.writer.emit(
                    Event(
                        event="WARN",
                        kind="plane",
                        id="tracker",
                        label="plane_tracker_error",
                        ts=utc_now_iso(),
                        meta={"error": str(e)},
                    )
                )
            time.sleep(self.poll_seconds)

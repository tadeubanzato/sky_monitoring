#!/usr/bin/env python3
"""
planes_opensky.py

OpenSky plane tracker:
- Detects aircraft ENTER/EXIT within a radius of your home.
- Adds OVERHEAD event when a plane gets very close to your home (one notification target).
- Uses OpenSky /api/states/all with a bounding box + haversine filtering.
- Emits events via EventWriter.

Fixes (2026-02):
- Stable display label across ENTER / OVERHEAD / EXIT (meta["display"]).
- Exit hysteresis + confirm-polls to reduce boundary jitter exits.
- Keeps file/module name + imports unchanged.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from enrichment_adsbdb import enrich_callsign_adsbdb
from enrichment_airlabs import enrich_plane
from enrichment_aviationstack import enrich_plane_aviationstack
from event_writer import Event, EventWriter, utc_now_iso

OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)

_TOKEN_SAFETY_SKEW_SECONDS = 30


# ─────────────────────────────────────────────
# Geo helpers
# ─────────────────────────────────────────────
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bbox_for_radius_km(lat: float, lon: float, radius_km: float) -> Tuple[float, float, float, float]:
    """
    Create a rough bounding box around (lat, lon) for an approximate radius.
    We still do exact haversine filtering after.
    """
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def alt_ft(alt_m: Optional[float]) -> Optional[int]:
    if alt_m is None:
        return None
    try:
        return int(round(float(alt_m) * 3.28084))
    except Exception:
        return None


def spd_kt(vel_ms: Optional[float]) -> Optional[int]:
    if vel_ms is None:
        return None
    try:
        return int(round(float(vel_ms) * 1.94384))
    except Exception:
        return None


def trk_deg(track: Optional[float]) -> Optional[int]:
    if track is None:
        return None
    try:
        return int(round(float(track)))
    except Exception:
        return None


def normalize_callsign(cs: str) -> str:
    return (cs or "").strip().upper().replace(" ", "")


def _route_is_present(route_obj: Any) -> bool:
    if isinstance(route_obj, dict):
        a = (route_obj.get("from") or "").strip()
        b = (route_obj.get("to") or "").strip()
        return bool(a and b)
    return False


def _safe_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _stable_display_from_meta(meta: Dict[str, Any], fallback_label: str) -> str:
    """
    Produce a stable 'Company Flight  DEP→ARR' string.
    This is computed once (on ENTER) and then reused for OVERHEAD/EXIT.
    """
    # 1) Company
    company = "Unknown"
    airline = meta.get("airline")
    if isinstance(airline, dict):
        nm = airline.get("name")
        if isinstance(nm, str) and nm.strip():
            company = nm.strip()
        else:
            code = airline.get("icao") or airline.get("iata")
            if isinstance(code, str) and code.strip():
                company = code.strip().upper()

    enriched = meta.get("enriched")
    if isinstance(enriched, dict):
        c = enriched.get("company")
        if isinstance(c, str) and c.strip():
            company = c.strip()
        elif company == "Unknown":
            code = enriched.get("airline_icao") or enriched.get("airline_iata")
            if isinstance(code, str) and code.strip():
                company = code.strip().upper()

    # 2) Flight (prefer ADSBDB callsign_iata, then callsign_icao, then enriched flight_iata/icao/number)
    flight = (
        meta.get("callsign_iata")
        or meta.get("callsign_icao")
        or (enriched.get("flight_iata") if isinstance(enriched, dict) else None)
        or (enriched.get("flight_icao") if isinstance(enriched, dict) else None)
        or (enriched.get("flight_number") if isinstance(enriched, dict) else None)
        or fallback_label
        or "UNK"
    )
    flight_s = _safe_str(flight).replace(" ", "") or "UNK"

    # 3) Route
    route = ""
    r = meta.get("route")
    if isinstance(r, dict):
        a = _safe_str(r.get("from"))
        b = _safe_str(r.get("to"))
        if a and b:
            route = f"{a}→{b}"

    if not route and isinstance(enriched, dict):
        rt = enriched.get("route")
        if isinstance(rt, str) and rt.strip() and "?" not in rt:
            route = rt.strip()
        else:
            dep = enriched.get("dep_iata") or enriched.get("dep_icao")
            arr = enriched.get("arr_iata") or enriched.get("arr_icao")
            if dep and arr:
                route = f"{_safe_str(dep)}→{_safe_str(arr)}"

    if not route:
        route = "???→???"

    return f"{company} {flight_s}  {route}"


# ─────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────
@dataclass
class PlaneState:
    icao24: str
    callsign: str
    country: str
    lat: float
    lon: float
    alt_m: Optional[float]
    vel_ms: Optional[float]
    track_deg: Optional[float]
    on_ground: bool


# ─────────────────────────────────────────────
# OAuth2 token helper
# ─────────────────────────────────────────────
class OpenSkyOAuthToken:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def _fetch_new(self) -> None:
        resp = requests.post(
            OPENSKY_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=20,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"opensky_token_failed status={resp.status_code} body={resp.text[:120]!r}")

        j = resp.json()
        token = j.get("access_token")
        expires_in = j.get("expires_in", 1800)

        if not token:
            raise RuntimeError("opensky_token_missing access_token")

        now = time.time()
        self._token = token
        self._expires_at = now + float(expires_in) - _TOKEN_SAFETY_SKEW_SECONDS

    def get(self) -> str:
        now = time.time()
        if self._token is None or now >= self._expires_at:
            self._fetch_new()
        return self._token  # type: ignore[return-value]

    def force_refresh(self) -> str:
        self._token = None
        self._expires_at = 0.0
        return self.get()


# ─────────────────────────────────────────────
# Tracker
# ─────────────────────────────────────────────
class OpenSkyPlaneTracker:
    """
    Tracks planes within radius_km of (home_lat, home_lon) and emits:
      - ENTER: first time seen inside radius
      - EXIT: after grace period + hysteresis/confirm polls when outside
      - OVERHEAD: when a plane gets very close to home (one notification target)

    OVERHEAD is rate-limited per-aircraft with cooldown.
    """

    def __init__(
        self,
        *,
        writer: EventWriter,
        home_lat: float,
        home_lon: float,
        radius_km: float,
        poll_seconds: int,
        disappear_grace_seconds: int,
        opensky_user: str = "",
        opensky_pass: str = "",
        airlabs_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.writer = writer
        self.home_lat = home_lat
        self.home_lon = home_lon

        self.radius_km = float(radius_km)
        self.radius_m = self.radius_km * 1000.0

        self.poll_seconds = max(5, int(poll_seconds))
        self.disappear_grace_seconds = max(10, int(disappear_grace_seconds))

        # ── Exit jitter control (defaults are safe and reduce flicker)
        self.exit_buffer_m: float = 750.0          # must go this far beyond radius before we even consider EXIT
        self.exit_confirm_polls: int = 2           # must be "outside" this many consecutive ticks

        self.radius_exit_m = self.radius_m + self.exit_buffer_m

        lamin, lamax, lomin, lomax = bbox_for_radius_km(home_lat, home_lon, self.radius_km * 1.5)
        self._bbox_params = {"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax}

        cid = os.getenv("OPENSKY_CLIENT_ID", "").strip()
        csec = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
        if not cid or not csec:
            raise RuntimeError("Missing OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET in environment")
        self._token = OpenSkyOAuthToken(cid, csec)

        self._backoff_until: float = 0.0

        # Tracking state
        self._inside: Dict[str, float] = {}
        self._last_seen_any: Dict[str, float] = {}
        self._last_dist_m: Dict[str, float] = {}
        self._last_position: Dict[str, Tuple[float, float]] = {}

        # Outside confirmation counter (icao24 -> consecutive outside polls)
        self._outside_polls: Dict[str, int] = {}

        # Cached per-aircraft meta (stable label + enrichment + last known motion)
        self._plane_cache: Dict[str, dict] = {}

        # OVERHEAD gating (per-aircraft)
        self.overhead_radius_m: float = 1500.0           # ~0.9 miles
        self.overhead_cooldown_seconds: int = 1800       # 30 minutes
        self._last_overhead_sent: Dict[str, float] = {}

        # ADSBDB cache (callsign -> cached result) to reduce external calls
        self._adsbdb_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = {}
        self._adsbdb_cache_ttl_s: int = 6 * 60 * 60  # 6 hours

        # AirLabs enable logic
        cfg = airlabs_cfg or {}
        cfg_enabled = cfg.get("enabled", None)
        env_has_key = bool(os.getenv("AIRLABS_API_KEY", "").strip())
        self._airlabs_enabled = False if cfg_enabled is False else env_has_key

        # aviationstack enable logic (env only)
        self._aviationstack_enabled = bool(os.getenv("AVIATIONSTACK_API_KEY", "").strip())

        self.writer.emit(
            Event(
                event="INFO",
                kind="plane",
                id="opensky",
                label="auth_mode",
                ts=utc_now_iso(),
                meta={"mode": "oauth2"},
            )
        )

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get()}"}

    def _emit_warn(self, label: str, meta: Dict[str, Any]) -> None:
        self.writer.emit(
            Event(
                event="WARN",
                kind="plane",
                id="opensky",
                label=label,
                ts=utc_now_iso(),
                meta=meta,
            )
        )

    def _fetch(self) -> List[PlaneState]:
        now = time.time()
        if now < self._backoff_until:
            return []

        r = requests.get(OPENSKY_URL, params=self._bbox_params, headers=self._headers(), timeout=20)

        if r.status_code == 401:
            try:
                self._token.force_refresh()
            except Exception as e:
                self._emit_warn("oauth_refresh_failed", {"error": str(e)})
                return []
            r = requests.get(OPENSKY_URL, params=self._bbox_params, headers=self._headers(), timeout=20)

        if r.status_code == 429:
            retry_after = r.headers.get("X-Rate-Limit-Retry-After-Seconds") or r.headers.get("Retry-After") or "60"
            try:
                wait_s = int(float(retry_after))
            except Exception:
                wait_s = 60
            self._backoff_until = time.time() + max(5, wait_s)
            self._emit_warn("rate_limited", {"status": 429, "retry_after_s": wait_s, "body": r.text[:80]})
            return []

        if r.status_code != 200:
            self._emit_warn("fetch_failed", {"status": r.status_code, "body": r.text[:120]})
            return []

        try:
            data = r.json()
        except Exception:
            self._emit_warn("non_json_response", {"status": r.status_code, "body": r.text[:120]})
            return []

        out: List[PlaneState] = []
        for s in data.get("states") or []:
            # OpenSky state vector indices:
            # 0 icao24, 1 callsign, 2 origin_country, 5 lon, 6 lat, 7 baro_altitude,
            # 8 on_ground, 9 velocity, 10 true_track
            icao24 = (s[0] or "").strip().lower()
            callsign = (s[1] or "").strip() or "(no callsign)"
            country = s[2] or ""
            lon = s[5]
            lat = s[6]
            alt_m = s[7]
            on_ground = bool(s[8]) if s[8] is not None else False
            vel_ms = s[9]
            track = s[10]

            if not icao24 or lat is None or lon is None:
                continue

            out.append(
                PlaneState(
                    icao24=icao24,
                    callsign=callsign,
                    country=country,
                    lat=float(lat),
                    lon=float(lon),
                    alt_m=float(alt_m) if alt_m is not None else None,
                    vel_ms=float(vel_ms) if vel_ms is not None else None,
                    track_deg=float(track) if track is not None else None,
                    on_ground=on_ground,
                )
            )
        return out

    def _cache_plane_live_meta(self, p: PlaneState) -> None:
        cached = self._plane_cache.get(p.icao24, {})
        cached.update(
            {
                "callsign": p.callsign,
                "label": normalize_callsign(p.callsign) or p.callsign,
                "country": p.country,
                "alt_ft": alt_ft(p.alt_m),
                "spd_kt": spd_kt(p.vel_ms),
                "trk_deg": trk_deg(p.track_deg),
            }
        )
        self._plane_cache[p.icao24] = cached

    def _can_send_overhead(self, icao24: str, now: float) -> bool:
        last = self._last_overhead_sent.get(icao24, 0.0)
        return (now - last) >= float(self.overhead_cooldown_seconds)

    def _adsbdb_lookup_cached(
        self, callsign: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
        cs = normalize_callsign(callsign)
        if not cs:
            return None, None, "adsbdb:skip:empty"

        now = time.time()
        cached = self._adsbdb_cache.get(cs)
        if cached:
            cached_at, norm, raw = cached
            if (now - cached_at) < float(self._adsbdb_cache_ttl_s):
                return norm, raw, "adsbdb:cache"

        norm, raw, status = enrich_callsign_adsbdb(cs)
        self._adsbdb_cache[cs] = (now, norm, raw)
        return norm, raw, status

    def tick(self) -> None:
        now = time.time()
        planes = self._fetch()

        seen_now: Dict[str, PlaneState] = {}
        currently_inside: Dict[str, PlaneState] = {}

        for p in planes:
            seen_now[p.icao24] = p
            self._last_seen_any[p.icao24] = now

            dist_m = haversine_m(self.home_lat, self.home_lon, p.lat, p.lon)
            self._last_dist_m[p.icao24] = dist_m
            self._last_position[p.icao24] = (p.lat, p.lon)

            # ENTER threshold: strict radius
            if dist_m <= self.radius_m:
                currently_inside[p.icao24] = p

        # ENTER + OVERHEAD (while inside)
        for icao24, p in currently_inside.items():
            is_new = (icao24 not in self._inside)
            self._inside[icao24] = now
            self._outside_polls[icao24] = 0  # reset outside counter once we're inside

            self._cache_plane_live_meta(p)

            # ENTER event
            if is_new:
                meta: Dict[str, Any] = {
                    "country": p.country,
                    "alt_ft": alt_ft(p.alt_m),
                    "spd_kt": spd_kt(p.vel_ms),
                    "trk_deg": trk_deg(p.track_deg),
                    "dist_m": round(self._last_dist_m.get(icao24, 0.0), 1),
                    "radius_m": round(self.radius_m, 1),
                    "radius_exit_m": round(self.radius_exit_m, 1),
                    "exit_confirm_polls": self.exit_confirm_polls,
                }

                # ── 1) ADSBDB FIRST
                cs = normalize_callsign(p.callsign)
                adsb_norm: Optional[Dict[str, Any]] = None
                adsb_raw: Optional[Dict[str, Any]] = None
                adsb_status = "adsbdb:skip"
                if cs:
                    adsb_norm, adsb_raw, adsb_status = self._adsbdb_lookup_cached(cs)

                meta["adsbdb_status"] = adsb_status
                if adsb_raw:
                    meta["adsbdb"] = adsb_raw
                if adsb_norm:
                    meta["route"] = adsb_norm.get("route")
                    meta["airline"] = adsb_norm.get("airline")
                    meta["callsign_iata"] = adsb_norm.get("callsign_iata")
                    meta["callsign_icao"] = adsb_norm.get("callsign_icao")
                    meta["enrich_source"] = "adsbdb"

                    cached = self._plane_cache.get(icao24, {})
                    cached["adsbdb_status"] = adsb_status
                    cached["adsbdb"] = adsb_raw
                    cached["route"] = meta.get("route")
                    cached["airline"] = meta.get("airline")
                    cached["callsign_iata"] = meta.get("callsign_iata")
                    cached["callsign_icao"] = meta.get("callsign_icao")
                    cached["enrich_source"] = "adsbdb"
                    self._plane_cache[icao24] = cached

                # ── 2) AirLabs fallback
                if not _route_is_present(meta.get("route")) and self._airlabs_enabled:
                    flight_icao = normalize_callsign(p.callsign)
                    try:
                        enriched, status = enrich_plane(hex_code=icao24, flight_icao=flight_icao or None)
                        meta["airlabs_status"] = status
                        if enriched:
                            meta["enriched"] = enriched
                            meta["enrich_source"] = "airlabs"

                            cached = self._plane_cache.get(icao24, {})
                            cached["airlabs_status"] = status
                            cached["enriched"] = enriched
                            cached["enrich_source"] = "airlabs"
                            self._plane_cache[icao24] = cached
                    except Exception as e:
                        meta["airlabs_status"] = f"airlabs:error:{e}"

                # ── 3) aviationstack fallback
                if (
                    not _route_is_present(meta.get("route"))
                    and self._aviationstack_enabled
                    and str(meta.get("airlabs_status") or "") == "airlabs:no_match"
                ):
                    flight_icao = normalize_callsign(p.callsign)
                    if flight_icao:
                        try:
                            a_enriched, a_status = enrich_plane_aviationstack(flight_icao)
                            meta["aviationstack_status"] = a_status
                            if a_enriched:
                                meta["enriched"] = a_enriched
                                meta["enrich_source"] = "aviationstack"

                                cached = self._plane_cache.get(icao24, {})
                                cached["aviationstack_status"] = a_status
                                cached["enriched"] = a_enriched
                                cached["enrich_source"] = "aviationstack"
                                self._plane_cache[icao24] = cached
                        except Exception as e:
                            meta["aviationstack_status"] = f"aviationstack:error:{e}"
                    else:
                        meta["aviationstack_status"] = "aviationstack:skip:empty_callsign"

                # ✅ Compute stable display label ONCE, cache it, and attach to meta
                cached = self._plane_cache.get(icao24, {})
                stable_label = cached.get("label") or normalize_callsign(p.callsign) or p.callsign or icao24
                display = _stable_display_from_meta(meta, stable_label)
                meta["display"] = display

                cached["display"] = display
                cached["label"] = stable_label
                self._plane_cache[icao24] = cached

                self.writer.emit(
                    Event(
                        event="ENTER",
                        kind="plane",
                        id=icao24,
                        label=stable_label,
                        ts=utc_now_iso(),
                        meta=meta,
                    )
                )

            # OVERHEAD event (notify on this)
            dist_m = float(self._last_dist_m.get(icao24, 9e9))
            if dist_m <= float(self.overhead_radius_m) and self._can_send_overhead(icao24, now):
                cached = self._plane_cache.get(icao24, {})
                label = cached.get("label") or normalize_callsign(cached.get("callsign", "")) or p.callsign or icao24

                self._last_overhead_sent[icao24] = now

                meta: Dict[str, Any] = {
                    "country": cached.get("country", p.country),
                    "alt_ft": cached.get("alt_ft", alt_ft(p.alt_m)),
                    "spd_kt": cached.get("spd_kt", spd_kt(p.vel_ms)),
                    "trk_deg": cached.get("trk_deg", trk_deg(p.track_deg)),
                    "dist_m": round(dist_m, 1),
                    "dist_km": round(dist_m / 1000.0, 3),
                    "pos": self._last_position.get(icao24),
                    "overhead_radius_m": self.overhead_radius_m,
                    "cooldown_s": self.overhead_cooldown_seconds,
                }

                # Stable display label, if known
                if isinstance(cached.get("display"), str) and cached.get("display"):
                    meta["display"] = cached["display"]

                # Include enrichment resolved at ENTER
                for k in (
                    "route",
                    "airline",
                    "adsbdb",
                    "adsbdb_status",
                    "enriched",
                    "airlabs_status",
                    "aviationstack_status",
                    "enrich_source",
                    "callsign_iata",
                    "callsign_icao",
                ):
                    if k in cached and cached.get(k) is not None:
                        meta[k] = cached.get(k)

                self.writer.emit(
                    Event(
                        event="OVERHEAD",
                        kind="plane",
                        id=icao24,
                        label=label,
                        ts=utc_now_iso(),
                        meta=meta,
                    )
                )

        # EXIT events
        to_remove: List[str] = []

        for icao24, last_inside_ts in list(self._inside.items()):
            if icao24 in currently_inside:
                continue

            # If we *see it right now*, use hysteresis threshold and confirm polls
            if icao24 in seen_now:
                dist_m = float(self._last_dist_m.get(icao24, 9e9))

                # Only start counting "outside" once it's clearly outside radius_exit_m
                if dist_m >= self.radius_exit_m:
                    self._outside_polls[icao24] = int(self._outside_polls.get(icao24, 0)) + 1
                else:
                    # Within buffer zone: do not count toward exit
                    self._outside_polls[icao24] = 0

                if self._outside_polls.get(icao24, 0) < self.exit_confirm_polls:
                    continue

                reason = "out_of_radius"
            else:
                # Not seen: rely on disappear grace (signal lost)
                last_any = self._last_seen_any.get(icao24, 0.0)
                if now - max(last_inside_ts, last_any) < self.disappear_grace_seconds:
                    continue
                reason = "signal_lost"

            cached = self._plane_cache.get(icao24, {})
            label = cached.get("label") or cached.get("callsign") or icao24

            meta_out: Dict[str, Any] = {
                "reason": reason,
                "country": cached.get("country"),
                "alt_ft": cached.get("alt_ft"),
                "spd_kt": cached.get("spd_kt"),
                "trk_deg": cached.get("trk_deg"),
                "last_dist_m": round(self._last_dist_m.get(icao24, 0.0), 1),
                "last_pos": self._last_position.get(icao24),
                "radius_m": round(self.radius_m, 1),
                "radius_exit_m": round(self.radius_exit_m, 1),
                "exit_confirm_polls": self.exit_confirm_polls,
                "outside_polls": int(self._outside_polls.get(icao24, 0)),
            }

            # Stable display label if known
            if isinstance(cached.get("display"), str) and cached.get("display"):
                meta_out["display"] = cached["display"]

            for k in (
                "route",
                "airline",
                "adsbdb_status",
                "airlabs_status",
                "aviationstack_status",
                "enrich_source",
                "callsign_iata",
                "callsign_icao",
            ):
                if k in cached and cached.get(k) is not None:
                    meta_out[k] = cached.get(k)

            self.writer.emit(
                Event(
                    event="EXIT",
                    kind="plane",
                    id=icao24,
                    label=label,
                    ts=utc_now_iso(),
                    meta=meta_out,
                )
            )

            to_remove.append(icao24)

        for icao24 in to_remove:
            self._inside.pop(icao24, None)
            self._plane_cache.pop(icao24, None)
            self._last_dist_m.pop(icao24, None)
            self._last_position.pop(icao24, None)
            self._last_seen_any.pop(icao24, None)
            self._outside_polls.pop(icao24, None)
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
                        id="opensky",
                        label="loop_error",
                        ts=utc_now_iso(),
                        meta={"error": str(e)},
                    )
                )
            time.sleep(self.poll_seconds)

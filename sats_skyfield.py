#!/usr/bin/env python3
"""
sats_skyfield.py

Skyfield-based satellite / space-object "overhead" tracker.

What it does:
- Loads a TLE set (downloads from a URL or uses a local cache file).
- Computes elevation/azimuth/range from a fixed home location.
- Emits EventWriter events when an object:
    ENTER  -> elevation >= min_elevation_deg
    EXIT   -> elevation <  min_elevation_deg

Tracking modes:
1) Curated (recommended):
   - Reads data/space_objects.json
   - Tracks only objects where "monitor": true
   - Uses each object's short_name/name for nicer labels
   - Hot-reloads the JSON file when it changes (no restart required)

2) Config allowlist fallback:
   - If JSON is missing/empty, uses cfg.only_norad_ids

3) Debug:
   - If track_all = true, tracks everything found in the TLE file

Fail-safe hot reload (important):
- If space_objects.json is temporarily invalid while being written (partial/truncated),
  we DO NOT wipe the allowlist/meta. We keep last-known-good state and retry next tick.
  This prevents false EXIT->ENTER cycles and noisy output.

Hardening:
- If download fails, fall back to cache if present.
- If download fails AND cache is missing, emit WARN and skip without crashing.

Requires:
- skyfield
- requests
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from skyfield.api import EarthSatellite, Loader
from skyfield.api import wgs84  # preferred observer helper

from event_writer import Event, EventWriter, utc_now_iso


# ─────────────────────────────────────────────
# Paths / defaults
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_SPACE_OBJECTS_JSON = str(DATA_DIR / "space_objects.json")
DEFAULT_TLE_CACHE_FILE = str(DATA_DIR / "tles_active.tle")
DEFAULT_SKYFIELD_CACHE_DIR = str(DATA_DIR / "skyfield")


@dataclass
class SatSample:
    norad_id: str
    name: str
    elev_deg: float
    az_deg: float
    dist_km: float


def _file_age_hours(p: Path) -> Optional[float]:
    if not p.exists():
        return None
    age_sec = time.time() - p.stat().st_mtime
    return age_sec / 3600.0


def _download_text(url: str, timeout: int = 30) -> str:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def _parse_tle_text(text: str, ts) -> List[EarthSatellite]:
    """
    Parse TLE text into EarthSatellite objects.
    Standard format is NAME, line1, line2 repeating.
    """
    lines = [ln.rstrip("\n") for ln in text.splitlines() if ln.strip()]
    sats: List[EarthSatellite] = []
    i = 0

    while i + 2 < len(lines):
        name = lines[i].strip()
        l1 = lines[i + 1].strip()
        l2 = lines[i + 2].strip()

        # Basic sanity: TLE lines start with "1 " and "2 "
        if not l1.startswith("1 ") or not l2.startswith("2 "):
            i += 1
            continue

        try:
            sats.append(EarthSatellite(l1, l2, name, ts))
        except Exception:
            # Skip malformed triplets
            pass

        i += 3

    return sats


def _norad_from_sat(sat: EarthSatellite) -> Optional[int]:
    try:
        return int(sat.model.satnum)
    except Exception:
        return None


def _safe_int(s: object) -> Optional[int]:
    try:
        return int(str(s).strip())
    except Exception:
        return None


def _load_space_objects_allowlist(
    json_path: Path,
) -> Optional[Tuple[Set[int], Dict[int, Dict[str, object]]]]:
    """
    Returns (on success):
      - allowlist: set of NORAD IDs (int) where monitor == true
      - meta_map:  mapping NORAD -> object dict (used for labels)

    Returns:
      - None on read/parse failure (caller should keep last-known-good state)
    """
    if not json_path.exists():
        # Missing file is a valid state: "no curated list"
        return (set(), {})

    try:
        raw = json_path.read_text(encoding="utf-8")
    except Exception:
        return None

    try:
        payload = json.loads(raw)
    except Exception:
        return None

    objects = payload.get("objects", {}) if isinstance(payload, dict) else {}
    if not isinstance(objects, dict):
        return (set(), {})

    allow: Set[int] = set()
    meta_map: Dict[int, Dict[str, object]] = {}

    for norad_str, obj in objects.items():
        nid = _safe_int(norad_str)
        if nid is None or not isinstance(obj, dict):
            continue

        meta_map[nid] = obj

        # Default to True to avoid surprises if someone forgets "monitor"
        monitor = obj.get("monitor", True)
        if bool(monitor):
            allow.add(nid)

    return (allow, meta_map)


class SkyfieldSatelliteTracker:
    def __init__(
        self,
        *,
        writer: EventWriter,
        home_lat: float,
        home_lon: float,
        min_elevation_deg: float,
        update_seconds: float,
        tle_url: str,
        tle_cache_file: str,
        tle_reload_hours: float,
        track_all: bool,
        only_norad_ids: List[int],
        space_objects_file: str = DEFAULT_SPACE_OBJECTS_JSON,
        skyfield_cache_dir: str = DEFAULT_SKYFIELD_CACHE_DIR,
    ) -> None:
        self.writer = writer
        self.home_lat = float(home_lat)
        self.home_lon = float(home_lon)

        self.min_elev = float(min_elevation_deg)
        self.update_seconds = max(0.5, float(update_seconds))

        self.tle_url = (tle_url or "").strip()
        self.cache_path = Path(tle_cache_file)
        if not self.cache_path.is_absolute():
            self.cache_path = (BASE_DIR / self.cache_path).resolve()

        self.tle_reload_hours = float(tle_reload_hours)

        # If track_all=True, ignore allowlists (debug mode).
        self.track_all = bool(track_all)

        # Curated JSON allowlist
        self.space_objects_path = Path(space_objects_file)
        if not self.space_objects_path.is_absolute():
            self.space_objects_path = (BASE_DIR / self.space_objects_path).resolve()

        self._space_objects_mtime: Optional[float] = None
        self._space_allow: Set[int] = set()
        self._space_meta: Dict[int, Dict[str, object]] = {}

        # Fallback allowlist from config
        self.only_norad_ids: Set[int] = set(int(x) for x in (only_norad_ids or []))

        # Tracking state
        self._overhead: Set[str] = set()  # NORAD IDs currently over threshold
        self._last_sample: Dict[str, SatSample] = {}

        # Skyfield setup (cache dir under data/)
        sky_cache = Path(skyfield_cache_dir)
        if not sky_cache.is_absolute():
            sky_cache = (BASE_DIR / sky_cache).resolve()
        sky_cache.mkdir(parents=True, exist_ok=True)

        self._load = Loader(str(sky_cache))
        self._ts = self._load.timescale()
        self._observer = wgs84.latlon(self.home_lat, self.home_lon)

        self._sats: Dict[int, EarthSatellite] = {}

        # Initial JSON load (best-effort)
        self._reload_space_objects_if_needed(force=True)

    def _should_reload_tles(self) -> bool:
        age = _file_age_hours(self.cache_path)
        if age is None:
            return True
        return age >= self.tle_reload_hours

    def _reload_space_objects_if_needed(self, force: bool = False) -> bool:
        """
        Returns True if allowlist/meta changed (or forced reload).

        FAIL-SAFE HOT RELOAD:
        - If JSON is temporarily invalid during write, DO NOT clobber allowlist/meta.
        - Do not advance stored mtime on failure, so we retry next tick.
        """
        if not self.space_objects_path.exists():
            # If file disappears, keep last-known-good allowlist to avoid churn.
            return False

        try:
            mtime = self.space_objects_path.stat().st_mtime
        except Exception:
            return False

        if not force and self._space_objects_mtime is not None and mtime == self._space_objects_mtime:
            return False

        loaded = _load_space_objects_allowlist(self.space_objects_path)

        # If load failed, keep last-known-good state and warn. Don't update mtime.
        if loaded is None:
            self.writer.emit(
                Event(
                    event="WARN",
                    kind="space_objects",
                    id="config",
                    label="space_objects_reload_failed",
                    ts=utc_now_iso(),
                    meta={
                        "file": str(self.space_objects_path),
                        "mtime": mtime,
                        "action": "kept_last_known_good_allowlist",
                    },
                )
            )
            return False

        allow, meta_map = loaded
        changed = (allow != self._space_allow) or force

        self._space_allow = allow
        self._space_meta = meta_map
        self._space_objects_mtime = mtime

        self.writer.emit(
            Event(
                event="INFO",
                kind="space_objects",
                id="config",
                label="space_objects_loaded",
                ts=utc_now_iso(),
                meta={
                    "file": str(self.space_objects_path),
                    "count_total": len(meta_map),
                    "count_monitoring": len(allow),
                    "changed": changed,
                },
            )
        )
        return changed

    def _effective_allowlist(self) -> Set[int]:
        """
        Priority:
          - If space_objects.json has monitored entries: use it
          - Else fall back to cfg.only_norad_ids
          - If track_all=True: ignored (tracks everything)
        """
        if self._space_allow:
            return set(self._space_allow)
        return set(self.only_norad_ids)

    def _load_tles(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        should_download = self._should_reload_tles()

        if should_download:
            if not self.tle_url:
                # No URL configured; must rely on cache
                if not self.cache_path.exists():
                    self.writer.emit(
                        Event(
                            event="WARN",
                            kind="space_objects",
                            id="tle",
                            label="tle_missing_url_and_cache",
                            ts=utc_now_iso(),
                            meta={"cache_file": str(self.cache_path)},
                        )
                    )
                    self._sats = {}
                    return
            else:
                try:
                    text = _download_text(self.tle_url)
                    self.cache_path.write_text(text, encoding="utf-8")
                except Exception as ex:
                    # Download failed; fall back to cache if present
                    if not self.cache_path.exists():
                        self.writer.emit(
                            Event(
                                event="WARN",
                                kind="space_objects",
                                id="tle",
                                label="tle_download_failed_no_cache",
                                ts=utc_now_iso(),
                                meta={
                                    "source": self.tle_url,
                                    "cache_file": str(self.cache_path),
                                    "error": f"{type(ex).__name__}: {ex}",
                                },
                            )
                        )
                        self._sats = {}
                        return

                    self.writer.emit(
                        Event(
                            event="WARN",
                            kind="space_objects",
                            id="tle",
                            label="tle_download_failed_using_cache",
                            ts=utc_now_iso(),
                            meta={
                                "source": self.tle_url,
                                "cache_file": str(self.cache_path),
                                "error": f"{type(ex).__name__}: {ex}",
                            },
                        )
                    )

        # Read cache
        try:
            text = self.cache_path.read_text(encoding="utf-8")
        except Exception as ex:
            self.writer.emit(
                Event(
                    event="WARN",
                    kind="space_objects",
                    id="tle",
                    label="tle_cache_read_failed",
                    ts=utc_now_iso(),
                    meta={"cache_file": str(self.cache_path), "error": f"{type(ex).__name__}: {ex}"},
                )
            )
            self._sats = {}
            return

        sats = _parse_tle_text(text, self._ts)

        sat_map: Dict[int, EarthSatellite] = {}
        for sat in sats:
            norad = _norad_from_sat(sat)
            if norad is None:
                continue
            sat_map[norad] = sat

        if self.track_all:
            filtered = sat_map
        else:
            allow = self._effective_allowlist()
            filtered = {nid: sat_map[nid] for nid in allow if nid in sat_map}

        self._sats = filtered

        self.writer.emit(
            Event(
                event="INFO",
                kind="space_objects",
                id="tle",
                label="tle_loaded",
                ts=utc_now_iso(),
                meta={
                    "count_total_in_file": len(sat_map),
                    "count_tracking": len(self._sats),
                    "source": self.tle_url or None,
                    "cache_file": str(self.cache_path),
                },
            )
        )

    def _label_for_norad(self, norad_id: int, fallback: str) -> str:
        obj = self._space_meta.get(norad_id, {})
        if isinstance(obj, dict):
            short = obj.get("short_name")
            name = obj.get("name")
            if isinstance(short, str) and short.strip():
                return short.strip()
            if isinstance(name, str) and name.strip():
                return name.strip()
        return fallback

    def _compute(self, sat: EarthSatellite) -> SatSample:
        t = self._ts.now()
        difference = sat - self._observer
        topocentric = difference.at(t)
        alt, az, distance = topocentric.altaz()

        elev = float(alt.degrees)
        azd = float(az.degrees)
        dist_km = float(distance.km)

        norad = _norad_from_sat(sat)
        norad_str = str(norad) if norad is not None else "?"
        raw_name = sat.name.strip() if getattr(sat, "name", None) else f"SAT {norad_str}"

        label = raw_name
        if norad is not None:
            label = self._label_for_norad(norad, raw_name)

        return SatSample(
            norad_id=norad_str,
            name=label,
            elev_deg=elev,
            az_deg=azd,
            dist_km=dist_km,
        )

    def tick(self) -> None:
        # Hot-reload curated allowlist (no restart needed)
        allowlist_changed = self._reload_space_objects_if_needed(force=False)

        # Reload TLEs if needed OR if allowlist changed (so tracking set updates immediately)
        if not self._sats or self._should_reload_tles() or (allowlist_changed and not self.track_all):
            self._load_tles()

        now_over: Set[str] = set()

        for norad_id, sat in self._sats.items():
            sample = self._compute(sat)
            self._last_sample[sample.norad_id] = sample

            if sample.elev_deg >= self.min_elev:
                now_over.add(sample.norad_id)

                if sample.norad_id not in self._overhead:
                    self.writer.emit(
                        Event(
                            event="ENTER",
                            kind="space_objects",
                            id=sample.norad_id,
                            label=sample.name,
                            ts=utc_now_iso(),
                            meta={
                                "elev_deg": round(sample.elev_deg, 1),
                                "az_deg": round(sample.az_deg, 1),
                                "dist_km": round(sample.dist_km, 1),
                            },
                        )
                    )

        for norad_str in list(self._overhead):
            if norad_str not in now_over:
                last = self._last_sample.get(norad_str)
                self.writer.emit(
                    Event(
                        event="EXIT",
                        kind="space_objects",
                        id=norad_str,
                        label=last.name if last else f"SAT {norad_str}",
                        ts=utc_now_iso(),
                        meta={
                            "elev_deg": round(last.elev_deg, 1) if last else None,
                            "az_deg": round(last.az_deg, 1) if last else None,
                            "dist_km": round(last.dist_km, 1) if last else None,
                        },
                    )
                )

        self._overhead = now_over

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as ex:
                self.writer.emit(
                    Event(
                        event="WARN",
                        kind="space_objects",
                        id="tracker",
                        label="satellite_tracker_error",
                        ts=utc_now_iso(),
                        meta={"error": f"{type(ex).__name__}: {ex}"},
                    )
                )
            time.sleep(self.update_seconds)


def build_sat_tracker(
    *,
    writer: EventWriter,
    home_lat: float,
    home_lon: float,
    cfg: Dict[str, object],
) -> SkyfieldSatelliteTracker:
    """
    Build a SkyfieldSatelliteTracker from the [satellites] config section.

    Supported keys:
      - enabled (handled by main.py, not here)
      - min_elevation_deg
      - update_seconds
      - tle_url
      - tle_cache_file
      - tle_reload_hours
      - track_all
      - only_norad_ids
      - space_objects_file
    """
    return SkyfieldSatelliteTracker(
        writer=writer,
        home_lat=home_lat,
        home_lon=home_lon,
        min_elevation_deg=float(cfg.get("min_elevation_deg", 10.0)),
        update_seconds=float(cfg.get("update_seconds", 2.0)),
        tle_url=str(cfg.get("tle_url", "")),
        tle_cache_file=str(cfg.get("tle_cache_file", DEFAULT_TLE_CACHE_FILE)),
        tle_reload_hours=float(cfg.get("tle_reload_hours", 12)),
        track_all=bool(cfg.get("track_all", False)),
        only_norad_ids=list(cfg.get("only_norad_ids", [])),
        space_objects_file=str(cfg.get("space_objects_file", DEFAULT_SPACE_OBJECTS_JSON)),
        skyfield_cache_dir=str(cfg.get("skyfield_cache_dir", DEFAULT_SKYFIELD_CACHE_DIR)),
    )

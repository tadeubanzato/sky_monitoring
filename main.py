#!/usr/bin/env python3
"""
main.py

Runs:
- OpenSky plane tracker
- Skyfield satellite tracker

Outputs:
- Human-readable console events
- Optional JSONL event log (config.toml -> [events].out_file)

This open-source version intentionally contains NO notification integrations.
"""

from __future__ import annotations

import threading
from pathlib import Path

# TOML loader compatibility
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # pip install tomli for Python <=3.10

from event_writer import EventWriter, Event, utc_now_iso
from geolocate import resolve_home_location
from planes_opensky import OpenSkyPlaneTracker
from sats_skyfield import build_sat_tracker


def load_config(path: str) -> dict:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    cfg = load_config("config.toml")

    # Home location (manual or IP geolocation)
    home_cfg = cfg.get("home", {}) or {}
    geo_enabled = bool(home_cfg.get("geolocation", False))
    fallback_lat = float(home_cfg.get("lat", 0.0))
    fallback_lon = float(home_cfg.get("lon", 0.0))

    home_lat, home_lon, home_src = resolve_home_location(
        geolocation_enabled=geo_enabled,
        fallback_lat=fallback_lat,
        fallback_lon=fallback_lon,
        timeout=8,
    )

    out_file = (cfg.get("events", {}) or {}).get("out_file", "") or ""
    writer = EventWriter(out_file=out_file)

    # Always print at least one INFO event so you know it's alive
    writer.emit(
        Event(
            event="INFO",
            kind="home",
            id="location",
            label="home_location_selected",
            ts=utc_now_iso(),
            meta={"lat": home_lat, "lon": home_lon, "source": home_src},
        )
    )

    threads: list[threading.Thread] = []

    # Planes
    p_cfg = cfg.get("planes_opensky", {}) or {}
    e_cfg = cfg.get("enrichment_airlabs", {}) or {}

    if bool(p_cfg.get("enabled", True)):
        airlabs_cfg = None
        if bool(e_cfg.get("enabled", False)):
            airlabs_cfg = {
                "enabled": True,
                "cache_ttl_seconds": int(e_cfg.get("cache_ttl_seconds", 300)),
                "include_route": bool(e_cfg.get("include_route", True)),
            }

        plane_tracker = OpenSkyPlaneTracker(
            writer=writer,
            home_lat=home_lat,
            home_lon=home_lon,
            radius_km=float(p_cfg.get("radius_km", 1.0)),
            poll_seconds=int(p_cfg.get("poll_seconds", 10)),
            disappear_grace_seconds=int(p_cfg.get("disappear_grace_seconds", 30)),
            opensky_user=str(p_cfg.get("user", "") or ""),
            opensky_pass=str(p_cfg.get("pass", "") or ""),
            airlabs_cfg=airlabs_cfg,
        )

        t = threading.Thread(target=plane_tracker.run_forever, name="planes", daemon=True)
        threads.append(t)
        t.start()

    # Satellites
    s_cfg = cfg.get("satellites", {}) or {}
    if bool(s_cfg.get("enabled", True)):
        sat_tracker = build_sat_tracker(
            writer=writer,
            home_lat=home_lat,
            home_lon=home_lon,
            cfg=s_cfg,
        )

        t = threading.Thread(target=sat_tracker.run_forever, name="sats", daemon=True)
        threads.append(t)
        t.start()

    if not threads:
        writer.emit(
            Event(
                event="WARN",
                kind="system",
                id="no_threads",
                label="No trackers enabled (planes_opensky.enabled and satellites.enabled are both false)",
                ts=utc_now_iso(),
                meta={},
            )
        )
        writer.close()
        return

    for t in threads:
        t.join()

    writer.close()


if __name__ == "__main__":
    main()

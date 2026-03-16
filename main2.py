#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from geolocate import resolve_home_location
from planes_opensky import OpenSkyPlaneTracker
from sats_skyfield import build_sat_tracker


class NullWriter:
    def emit(self, *_args, **_kwargs):
        return None

    def close(self):
        return None


def load_config(path: str = "config.toml") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8"))


def map_to_grid(dx_km: float, dy_km: float, radius_km: float, w: int, h: int):
    if radius_km <= 0:
        return None
    nx = max(-1.0, min(1.0, dx_km / radius_km))
    ny = max(-1.0, min(1.0, dy_km / radius_km))
    x = int((nx + 1) * 0.5 * (w - 1))
    y = int((1 - (ny + 1) * 0.5) * (h - 1))
    return x, y


def km_offsets(home_lat: float, home_lon: float, lat: float, lon: float):
    dlat = (lat - home_lat) * 111.0
    dlon = (lon - home_lon) * 111.0 * math.cos(math.radians(home_lat))
    return dlon, dlat


def clear_screen():
    print("\x1b[2J\x1b[H", end="")


def draw_frame(home_lat, home_lon, plane_tracker, sat_tracker, plane_radius_km, sat_radius_km, w=71, h=31):
    grid = [[" " for _ in range(w)] for _ in range(h)]

    # border
    for x in range(w):
        grid[0][x] = "-"
        grid[h - 1][x] = "-"
    for y in range(h):
        grid[y][0] = "|"
        grid[y][w - 1] = "|"
    grid[0][0] = grid[0][w - 1] = grid[h - 1][0] = grid[h - 1][w - 1] = "+"

    cx, cy = w // 2, h // 2
    grid[cy][cx] = "+"  # home

    # planes in radius
    plane_count = 0
    for icao24 in list(getattr(plane_tracker, "_inside", {}).keys()):
        pos = getattr(plane_tracker, "_last_position", {}).get(icao24)
        if not pos:
            continue
        lat, lon = pos
        dx, dy = km_offsets(home_lat, home_lon, lat, lon)
        p = map_to_grid(dx, dy, plane_radius_km, w, h)
        if not p:
            continue
        x, y = p
        if 0 < x < w - 1 and 0 < y < h - 1:
            grid[y][x] = "P"
            plane_count += 1

    # satellites above threshold
    sat_count = 0
    for norad in list(getattr(sat_tracker, "_overhead", set())):
        s = getattr(sat_tracker, "_last_sample", {}).get(norad)
        if not s:
            continue
        # azimuth: 0=north,90=east ; distance is slant range km
        r = min(float(s.dist_km), sat_radius_km)
        az = math.radians(float(s.az_deg))
        dx = r * math.sin(az)
        dy = r * math.cos(az)
        p = map_to_grid(dx, dy, sat_radius_km, w, h)
        if not p:
            continue
        x, y = p
        if 0 < x < w - 1 and 0 < y < h - 1:
            if grid[y][x] == "P":
                grid[y][x] = "*"
            else:
                grid[y][x] = "S"
            sat_count += 1

    clear_screen()
    print("Sky Monitoring Radar (terminal)  |  q = quit")
    print(f"Home: {home_lat:.5f}, {home_lon:.5f}  |  Planes radius: {plane_radius_km:.1f}km  |  Satellites radius: {sat_radius_km:.0f}km")
    print(f"Legend: + home, P plane, S satellite, * overlap | planes:{plane_count} sats:{sat_count}")
    print()
    for row in grid:
        print("".join(row))


def main():
    cfg = load_config("config.toml")

    home_cfg = cfg.get("home", {}) or {}
    home_lat, home_lon, _src = resolve_home_location(
        geolocation_enabled=bool(home_cfg.get("geolocation", False)),
        fallback_lat=float(home_cfg.get("lat", 0.0)),
        fallback_lon=float(home_cfg.get("lon", 0.0)),
        timeout=8,
    )

    writer = NullWriter()

    p_cfg = cfg.get("planes_opensky", {}) or {}
    plane_radius_km = float(p_cfg.get("radius_km", 10.0))
    plane_tracker = OpenSkyPlaneTracker(
        writer=writer,
        home_lat=home_lat,
        home_lon=home_lon,
        radius_km=plane_radius_km,
        poll_seconds=max(1, int(p_cfg.get("poll_seconds", 10))),
        disappear_grace_seconds=int(p_cfg.get("disappear_grace_seconds", 30)),
        opensky_user=str(p_cfg.get("user", "") or ""),
        opensky_pass=str(p_cfg.get("pass", "") or ""),
        airlabs_cfg=None,
    )

    s_cfg = cfg.get("satellites", {}) or {}
    sat_tracker = build_sat_tracker(writer=writer, home_lat=home_lat, home_lon=home_lon, cfg=s_cfg)
    sat_radius_km = float(s_cfg.get("radar_radius_km", 2500.0))

    tick_sec = float(cfg.get("radar", {}).get("tick_seconds", 2.0)) if isinstance(cfg.get("radar"), dict) else 2.0

    try:
        while True:
            # update trackers
            try:
                plane_tracker.tick()
            except Exception:
                pass
            try:
                sat_tracker.tick()
            except Exception:
                pass

            draw_frame(home_lat, home_lon, plane_tracker, sat_tracker, plane_radius_km, sat_radius_km)

            # simple non-blocking quit check (POSIX)
            import select, sys

            if select.select([sys.stdin], [], [], tick_sec)[0]:
                c = sys.stdin.readline().strip().lower()
                if c == "q":
                    break
    finally:
        writer.close()


if __name__ == "__main__":
    # put terminal in cbreak-like behavior optional, keep simple line mode
    main()

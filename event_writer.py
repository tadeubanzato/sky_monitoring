#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import tomllib  # py3.11+
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore


# ─────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ts_compact(ts_iso: str) -> str:
    """
    Convert ISO8601 '2026-02-11T03:37:35.062963Z' -> '03:37:35.062Z'
    """
    s = ts_iso.replace("+00:00", "Z")
    if "T" not in s:
        return s
    time_part = s.split("T", 1)[1].replace("Z", "")
    # Keep milliseconds (3 digits)
    if "." in time_part:
        hhmmss, frac = time_part.split(".", 1)
        frac3 = (frac + "000")[:3]
        return f"{hhmmss}.{frac3}Z"
    return f"{time_part}Z"


# ─────────────────────────────────────────────
# Minimal .env loader (no dependencies)
# Loads env vars from .env into os.environ if not already set.
# ─────────────────────────────────────────────

_ENV_LOADED = False


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
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")

            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        return


def get_airlabs_api_key() -> str:
    load_dotenv()
    return os.getenv("AIRLABS_API_KEY", "").strip()


# ─────────────────────────────────────────────
# Paths (robust even when run via systemd / different cwd)
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


# ─────────────────────────────────────────────
# Config loader (TOML)
# - Default: ./config.toml next to code
# ─────────────────────────────────────────────

_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    config_path = os.getenv("OKAME_CONFIG", "").strip()
    p = Path(config_path) if config_path else (BASE_DIR / "config.toml")

    if not p.exists() or tomllib is None:
        _CONFIG_CACHE = {}
        return _CONFIG_CACHE

    try:
        _CONFIG_CACHE = tomllib.loads(p.read_text(encoding="utf-8"))
        if not isinstance(_CONFIG_CACHE, dict):
            _CONFIG_CACHE = {}
    except Exception:
        _CONFIG_CACHE = {}

    return _CONFIG_CACHE


def _event_logs_settings() -> Dict[str, Any]:
    cfg = load_config()
    el = cfg.get("event_logs")
    return el if isinstance(el, dict) else {}


def _event_logs_enabled() -> bool:
    el = _event_logs_settings()
    return bool(el.get("enabled", False))


def _event_logs_path_default() -> str:
    el = _event_logs_settings()
    v = el.get("path", "data/events.jsonl")
    return str(v).strip() or "data/events.jsonl"


def _cli_settings() -> Dict[str, Any]:
    cfg = load_config()
    c = cfg.get("cli")
    return c if isinstance(c, dict) else {}


def _cli_use_color() -> bool:
    c = _cli_settings()
    # auto = use if tty
    v = c.get("color", "auto")
    if isinstance(v, bool):
        return v and sys.stdout.isatty()
    if isinstance(v, str):
        vv = v.strip().lower()
        if vv == "always":
            return True
        if vv == "never":
            return False
    return sys.stdout.isatty()


def _cli_compact_timestamps() -> bool:
    c = _cli_settings()
    return bool(c.get("compact_ts", True))


def _cli_kind_width() -> int:
    c = _cli_settings()
    try:
        return int(c.get("kind_width", 12))
    except Exception:
        return 12


def _cli_event_width() -> int:
    c = _cli_settings()
    try:
        return int(c.get("event_width", 8))
    except Exception:
        return 8


# ─────────────────────────────────────────────
# ANSI coloring (no deps)
# ─────────────────────────────────────────────

class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def _colorize(text: str, *codes: str) -> str:
    if not _cli_use_color():
        return text
    return "".join(codes) + text + _Ansi.RESET


def _badge(event: str) -> str:
    e = (event or "").upper()

    if e == "ENTER":
        return _colorize("ENTER", _Ansi.GREEN, _Ansi.BOLD)
    if e == "EXIT":
        return _colorize("EXIT", _Ansi.GRAY)
    if e == "OVERHEAD":
        return _colorize("OVERHEAD", _Ansi.RED, _Ansi.BOLD)
    if e == "UPDATE":
        return _colorize("UPDATE", _Ansi.CYAN)
    if e == "INFO":
        return _colorize("INFO", _Ansi.BLUE)
    if e == "WARN":
        return _colorize("WARN", _Ansi.YELLOW, _Ansi.BOLD)

    return e


def _kv(meta: Dict[str, Any], keys: list[str]) -> str:
    parts: list[str] = []
    for k in keys:
        if k in meta and meta[k] not in (None, "", {}):
            parts.append(f"{k}={meta[k]}")
    return " ".join(parts)


# ─────────────────────────────────────────────
# Country → Flag helpers (JSON-backed)
# NOTE: Expects "Country Name" -> "ISO2" in data/countries.json
# ─────────────────────────────────────────────

_COUNTRY_MAP: Optional[Dict[str, str]] = None


def load_country_map(path: Optional[str] = None) -> Dict[str, str]:
    global _COUNTRY_MAP
    if _COUNTRY_MAP is not None:
        return _COUNTRY_MAP

    p = Path(path) if path else (DATA_DIR / "countries.json")
    if not p.exists():
        _COUNTRY_MAP = {}
        return _COUNTRY_MAP

    try:
        _COUNTRY_MAP = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(_COUNTRY_MAP, dict):
            _COUNTRY_MAP = {}
    except Exception:
        _COUNTRY_MAP = {}

    return _COUNTRY_MAP


def iso2_to_flag(iso2: str) -> str:
    if not iso2 or len(iso2) != 2:
        return ""
    a = iso2[0].upper()
    b = iso2[1].upper()
    if not ("A" <= a <= "Z" and "A" <= b <= "Z"):
        return ""
    return chr(0x1F1E6 + ord(a) - 65) + chr(0x1F1E6 + ord(b) - 65)


def country_to_flag(country: Optional[str]) -> str:
    if not country:
        return ""
    cmap = load_country_map()
    iso2 = cmap.get(country)
    if not iso2:
        return ""
    return iso2_to_flag(iso2)


# ─────────────────────────────────────────────
# Optional airlines map (legacy fallback)
# ─────────────────────────────────────────────

_AIRLINES_FLAT: Optional[Dict[str, str]] = None


def load_airlines_flat(path: Optional[str] = None) -> Dict[str, str]:
    global _AIRLINES_FLAT
    if _AIRLINES_FLAT is not None:
        return _AIRLINES_FLAT

    p = Path(path) if path else (DATA_DIR / "airlines_by_region.json")
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
                            flat[code.strip().upper()] = name.strip()
        _AIRLINES_FLAT = flat
    except Exception:
        _AIRLINES_FLAT = {}

    return _AIRLINES_FLAT


def airline_code_to_company(code: Optional[str]) -> str:
    if not code:
        return "Unknown"
    flat = load_airlines_flat()
    c = code.strip().upper()
    return flat.get(c, c)


# ─────────────────────────────────────────────
# Event model
# ─────────────────────────────────────────────

@dataclass
class Event:
    event: str          # ENTER | EXIT | UPDATE | INFO | WARN | OVERHEAD
    kind: str           # plane | sat | space_objects | home
    id: str             # icao24 or NORAD ID or hex
    label: str          # callsign / stable flight id / satellite name
    ts: str             # ISO8601 UTC
    meta: Dict[str, Any]


class EventWriter:
    """
    Writes:
      1) Human-readable console output (always)
      2) JSON Lines file output ONLY if config.toml enables it:

        [event_logs]
        enabled = true
        path = "data/events.jsonl"

    CLI styling options (optional):
        [cli]
        color = "auto"        # auto | always | never
        compact_ts = true
        kind_width = 12
        event_width = 8

    Threading:
      - This project can run multiple trackers (planes + satellites) concurrently.
      - We guard output/file writes with a lock to prevent interleaved lines.
    """

    def __init__(self, out_file: str = "") -> None:
        load_dotenv()

        # Warm caches (safe if files don't exist)
        load_country_map()
        load_airlines_flat()

        self._fh = None
        self.out_path: Optional[Path] = None

        # Prevent interleaved console/file output when multiple trackers emit at once
        self._lock = threading.Lock()

        if _event_logs_enabled():
            chosen = out_file.strip() if out_file else _event_logs_path_default()
            if chosen:
                p = Path(chosen)
                if not p.is_absolute():
                    p = (BASE_DIR / p).resolve()
                self.out_path = p
                self.out_path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self.out_path.open("a", encoding="utf-8")

    # ─────────────────────────────────────────────
    # Plane formatting helpers
    # ─────────────────────────────────────────────

    def _plane_company(self, e: Event) -> str:
        meta = e.meta if isinstance(e.meta, dict) else {}

        airline = meta.get("airline")
        if isinstance(airline, dict):
            nm = airline.get("name")
            if isinstance(nm, str) and nm.strip():
                return nm.strip()
            for k in ("icao", "iata"):
                c = airline.get(k)
                if isinstance(c, str) and c.strip():
                    return c.strip().upper()

        enriched = meta.get("enriched")
        if isinstance(enriched, dict):
            company = enriched.get("company")
            if isinstance(company, str) and company.strip():
                c = company.strip()
                if len(c) >= 4:
                    return c
            code = enriched.get("airline_icao") or enriched.get("airline_iata")
            if isinstance(code, str) and code.strip():
                return airline_code_to_company(code.strip())

        code2 = meta.get("airline_icao") or meta.get("airline_iata")
        if isinstance(code2, str) and code2.strip():
            return airline_code_to_company(code2.strip())

        return "Unknown"

    def _plane_flight(self, e: Event) -> str:
        """
        Prefer an explicit stable flight id if provided by the tracker.
        """
        meta = e.meta if isinstance(e.meta, dict) else {}

        fid = meta.get("flight_id")
        if isinstance(fid, str) and fid.strip():
            return fid.strip()

        enriched = meta.get("enriched") if isinstance(meta.get("enriched"), dict) else {}

        flight = (
            meta.get("callsign_iata")
            or meta.get("callsign_icao")
            or (enriched.get("flight_iata") if isinstance(enriched, dict) else None)
            or (enriched.get("flight_icao") if isinstance(enriched, dict) else None)
            or (enriched.get("flight_number") if isinstance(enriched, dict) else None)
            or meta.get("flight_iata")
            or meta.get("flight_icao")
            or meta.get("flight_number")
            or e.label
            or "UNK"
        )

        if isinstance(flight, str):
            return flight.strip().replace(" ", "") or "UNK"
        return "UNK"

    def _plane_route(self, e: Event) -> str:
        meta = e.meta if isinstance(e.meta, dict) else {}

        # Prefer explicit route string if provided by tracker (stable)
        rs = meta.get("route_str")
        if isinstance(rs, str) and rs.strip():
            return rs.strip()

        r = meta.get("route")
        if isinstance(r, dict):
            a = (r.get("from") or "").strip()
            b = (r.get("to") or "").strip()
            if a and b:
                return f"{a}→{b}"

        enriched = meta.get("enriched")
        if isinstance(enriched, dict):
            route = enriched.get("route")
            if isinstance(route, str) and route.strip() and "?" not in route:
                return route.strip()

            dep = enriched.get("dep_iata") or enriched.get("dep_icao")
            arr = enriched.get("arr_iata") or enriched.get("arr_icao")
            if dep and arr:
                out = f"{str(dep).strip()}→{str(arr).strip()}"
                if "?" not in out:
                    return out

        rg = meta.get("route_guess")
        if isinstance(rg, str) and rg.strip() and "?" not in rg:
            return rg.strip()

        dep2 = meta.get("dep") or "???"
        arr2 = meta.get("arr") or "???"
        return f"{dep2}→{arr2}"

    def _plane_company_flight_route(self, e: Event) -> str:
        # If tracker provides a stable display string, use it.
        meta = e.meta if isinstance(e.meta, dict) else {}
        disp = meta.get("display")
        if isinstance(disp, str) and disp.strip():
            return disp.strip()

        company = self._plane_company(e)
        flight = self._plane_flight(e)
        route = self._plane_route(e)
        return f"{company} {flight}  {route}"

    def _plane_status_tag(self, e: Event) -> str:
        meta = e.meta if isinstance(e.meta, dict) else {}

        ads = meta.get("adsbdb_status")
        air = meta.get("airlabs_status")
        avs = meta.get("aviationstack_status")
        legacy = meta.get("enrich_status")

        parts: list[str] = []
        if isinstance(ads, str) and ads.strip():
            parts.append(f"adsbdb:{ads.strip()}")
        if isinstance(air, str) and air.strip():
            parts.append(f"airlabs:{air.strip()}")
        if isinstance(avs, str) and avs.strip():
            parts.append(f"aviationstack:{avs.strip()}")
        if not parts and isinstance(legacy, str) and legacy.strip():
            parts.append(legacy.strip())

        if not parts:
            return ""

        tag = " ".join(parts)
        return "  " + _colorize(f"[{tag}]", _Ansi.DIM) if _cli_use_color() else f"  [{tag}]"

    # ─────────────────────────────────────────────
    # Pretty output
    # ─────────────────────────────────────────────

    def _print_pretty(self, e: Event) -> None:
        ts = _ts_compact(e.ts) if _cli_compact_timestamps() else e.ts.replace("T", " ").replace("Z", " UTC")
        kind_w = _cli_kind_width()
        ev_w = _cli_event_width()

        kind = (e.kind or "").upper()
        event_badge = _badge(e.event)

        # Slightly nicer kind coloring
        if _cli_use_color():
            if kind in ("PLANE",):
                kind = _colorize(kind, _Ansi.MAGENTA, _Ansi.BOLD)
            elif kind in ("SAT", "SPACE_OBJECTS"):
                kind = _colorize(kind, _Ansi.CYAN, _Ansi.BOLD)
            elif kind in ("HOME",):
                kind = _colorize(kind, _Ansi.BLUE, _Ansi.BOLD)

        prefix = f"[{ts}] {kind:<{kind_w}} {event_badge:<{ev_w}}"

        # PLANES
        if e.kind == "plane":
            meta = e.meta if isinstance(e.meta, dict) else {}
            enriched = meta.get("enriched", {}) or {}
            if not isinstance(enriched, dict):
                enriched = {}

            iso2 = enriched.get("flag") if isinstance(enriched.get("flag"), str) else None
            country = meta.get("country")

            if iso2:
                flag = iso2_to_flag(str(iso2))
                country_part = f"{str(iso2).upper()} {flag}".rstrip()
            else:
                flag = country_to_flag(country if isinstance(country, str) else None)
                country_part = f"{country} {flag}".rstrip() if country else ""

            alt_ft = meta.get("alt_ft")
            spd_kt = meta.get("spd_kt")
            trk_deg = meta.get("trk_deg")

            # Backward-compat with other naming
            if alt_ft in (None, "", "?"):
                alt_ft = meta.get("altitude_ft") or enriched.get("alt_ft") or enriched.get("alt")
            if spd_kt in (None, "", "?"):
                spd_kt = meta.get("velocity_kt") or enriched.get("speed_kt") or enriched.get("speed")
            if trk_deg in (None, "", "?"):
                trk_deg = meta.get("heading_deg") or enriched.get("trk_deg") or enriched.get("dir")

            header = self._plane_company_flight_route(e)
            status_part = self._plane_status_tag(e)

            icon = "✈️"
            if (e.event or "").upper() == "OVERHEAD":
                icon = "🚨✈️"

            print(
                f"{prefix} {icon}  {header}  "
                f"({e.id})  "
                f"{country_part}  "
                f"alt={alt_ft if alt_ft not in (None, '', '?') else '?'}ft  "
                f"spd={spd_kt if spd_kt not in (None, '', '?') else '?'}kt  "
                f"trk={trk_deg if trk_deg not in (None, '', '?') else '?'}°"
                f"{status_part}"
            )
            return

        # SAT / SPACE OBJECTS
        if e.kind in ("sat", "space_objects"):
            icon = "🛰️"
            if (e.event or "").upper() == "ENTER":
                elev = e.meta.get("elev_deg", "?")
                dist = e.meta.get("dist_km", "?")
                print(f"{prefix} {icon}  {e.label}  elev={elev}°  dist={dist}km")
            elif (e.event or "").upper() == "EXIT":
                print(f"{prefix} {icon}  {e.label} left overhead")
            else:
                meta = e.meta if isinstance(e.meta, dict) else {}
                if e.label == "tle_loaded":
                    print(f"{prefix} {icon}  tle_loaded  " + _kv(meta, ["count_total_in_file", "count_tracking", "source", "cache_file"]))
                else:
                    print(f"{prefix} {icon}  {e.label}  " + _kv(meta, list(meta.keys())[:8]))
            return

        # HOME / INFO / WARN / other
        meta = e.meta if isinstance(e.meta, dict) else {}
        if e.kind == "home" and e.label == "home_location_selected":
            lat = meta.get("lat")
            lon = meta.get("lon")
            src = meta.get("source")
            print(f"{prefix} 📍 home lat={lat} lon={lon} source={src}")
        else:
            if meta:
                keys = list(meta.keys())[:10]
                print(f"{prefix} ℹ️  {e.label}  " + _kv(meta, keys))
            else:
                print(f"{prefix} ℹ️  {e.label}")

    def emit(self, event: Event) -> None:
        # Prevent interleaved log lines between concurrent emitters
        with self._lock:
            self._print_pretty(event)
            sys.stdout.flush()

            if self._fh:
                line = json.dumps(asdict(event), ensure_ascii=False)
                self._fh.write(line + "\n")
                self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


AIRLABS_API_KEY = get_airlabs_api_key()

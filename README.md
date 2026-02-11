# 🌌 Sky Monitoring
### Real-time aircraft proximity + satellite overhead events (CLI)

Sky Monitoring is an open-source, modular Python project that monitors the sky **above a home location** and emits clean, real-time events:

- ✈️ **Aircraft** entering/exiting a configurable **radius** around you
- 🛰️ **Satellites / space objects** rising above a configurable **elevation angle**
- 📡 Optional **flight enrichment** via multiple providers (with graceful fallback)
- 📁 Optional **JSONL event logs** for automation / analytics

It’s CLI-first on purpose: simple to run, easy to extend, and friendly to hobbyists.

### Sample Airplane Enter and Exit events
```bash
[13:28:59.624Z] PLANE ENTER ✈️  Endeavor Air 9E5131  MYR→LGA  (a3c259)  United States  alt=4175ft  spd=269kt  trk=64°  [adsbdb:adsbdb:ok]
[13:30:32.027Z] PLANE EXIT ✈️  Endeavor Air EDV5131  MYR→LGA  (a3c259)  United States  alt=4175ft  spd=269kt  trk=64°  [adsbdb:adsbdb:ok]
```

---

## 🚀 Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/sky_monitoring.git
cd sky_monitoring

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py
```

---

## ⚙️ Configuration (`config.toml`)

All behavior is driven by `config.toml`.

Minimal example:

```toml
[home]
lat = 40.758
lon = -73.9855
# Optional (less precise): geolocation = true

[planes_opensky]
enabled = true
radius_km = 10
update_seconds = 2

[satellites]
enabled = true
min_elevation_deg = 15
update_seconds = 2
tle_url = "https://celestrak.org/NORAD/elements/gp.php?FORMAT=tle&GROUP=active"
tle_cache_file = "data/tles_active.tle"
tle_reload_hours = 12
track_all = false
space_objects_file = "data/space_objects.json"

[event_logs]
enabled = true
path = "data/events.jsonl"
```

---

## 🧱 Modular Architecture (What each module does)

Sky Monitoring is intentionally split into small modules so you can replace pieces without rewriting the whole project.

### `main.py`
**Orchestrator.**
- Loads `config.toml`
- Resolves home location (manual coords or optional IP geolocation)
- Starts enabled trackers (planes + satellites)
- Keeps a single `EventWriter` instance for output + optional JSON logging

### `config.toml`
**Runtime settings.**
- Home location
- Plane radius + polling frequency
- Satellite elevation threshold + TLE source + caching rules
- Enrichment settings (provider order / enable flags)
- CLI logging preferences (colors, compact timestamps, etc.)

### `event_writer.py`
**The event bus + CLI renderer.**
- Defines the `Event` dataclass (`ENTER`, `EXIT`, `OVERHEAD`, `INFO`, `WARN`, …)
- Pretty-prints consistent output to the console
- Optionally writes events to JSON Lines (`data/events.jsonl`)
- No notification system / no external dispatch (open-source safe)

### `geolocate.py`
**Home location resolver.**
- Uses manual `lat/lon` if provided (recommended)
- Optional: IP-based geolocation (less accurate; depends on ISP/VPN)
- Emits a `HOME INFO` event describing which location source was used

### `planes_opensky.py`
**Aircraft tracker (ADS-B state vectors).**
- Pulls live aircraft positions from the OpenSky Network API
- Computes distance to home, emits:
  - `PLANE ENTER` when an aircraft crosses into your radius
  - `PLANE EXIT` when it leaves
  - Optional `PLANE OVERHEAD` when directly “nearby” (project-defined)
- Calls enrichment modules to attach extra metadata when available

### `sats_skyfield.py`
**Satellite / space object tracker (orbital propagation).**
- Downloads TLE data (or uses cache)
- Uses Skyfield to propagate orbits and compute elevation/azimuth/range
- Emits:
  - `SPACE_OBJECTS ENTER` when elevation >= `min_elevation_deg`
  - `SPACE_OBJECTS EXIT` when elevation falls below
- Supports a curated allowlist in `data/space_objects.json`
- Hot-reloads the JSON allowlist safely (fail-safe reload)

### Enrichment modules (optional)
These modules attempt to add “human friendly” metadata (airline name, route, aircraft type, registration, etc.).

- `enrichment_adsbdb.py` — registry / aircraft metadata lookup  
  Source: https://www.adsbdb.com/

- `enrichment_airlabs.py` — airline/route/flight metadata  
  Source: https://airlabs.co/  
  Requires an account + API key

- `enrichment_aviationstack.py` — alternate enrichment provider / fallback  
  Source: https://aviationstack.com/  
  Requires an account + API key

The project is designed so that **if enrichment fails**, tracking still works (you just see less metadata).

---

## 📡 Data Sources (and what they’re used for)

### Aircraft positions (tracking)
**OpenSky Network** (ADS‑B state vectors)  
https://opensky-network.org/

OpenSky provides aircraft state data including:
- latitude / longitude
- barometric altitude (when available)
- velocity, heading
- callsign / ICAO24 hex

### Satellite orbits (tracking)
**CelesTrak** (TLE sets)  
https://celestrak.org/

CelesTrak provides Two‑Line Element sets (TLEs), which are orbital parameters used to propagate satellite positions over time.

### Enrichment (optional)
Each enrichment provider has its own Terms of Service and rate limits. You must create your own account + keys where required:

- ADSBDB: https://www.adsbdb.com/
- AirLabs: https://airlabs.co/
- AviationStack: https://aviationstack.com/

---

## 🧮 The Math (how “nearby” and “overhead” are computed)

### ✈️ Aircraft radius detection (great‑circle distance)
Aircraft proximity uses a geographic radius in kilometers:

```toml
[planes_opensky]
radius_km = 10
```

Each polling cycle:
1. OpenSky provides aircraft `(lat, lon)`
2. We compute surface distance between:
   - home `(lat0, lon0)`
   - aircraft `(lat1, lon1)`
3. If distance ≤ `radius_km` → `ENTER`
4. If distance > `radius_km` (and it was previously inside) → `EXIT`

This distance is computed using Earth‑curvature‑aware geometry (e.g., the Haversine / great‑circle formula).
It’s **not** a signal strength measurement — it’s purely geographic distance.

### 🛰 Satellite “overhead” detection (orbital propagation + topocentric angles)
Satellites don’t use a radius. They use **elevation angle** above your local horizon:

```toml
[satellites]
min_elevation_deg = 15
```

How it works:
1. Load TLEs from CelesTrak (or cached file)
2. Use Skyfield (SGP4 under the hood) to propagate each orbit to “now”
3. Convert satellite position into a **topocentric** frame relative to the observer (your home location)
4. Compute:
   - **elevation** (degrees above horizon)
   - **azimuth** (compass direction)
   - **range** (distance, km)

Events:
- If elevation rises to or above `min_elevation_deg` → `ENTER`
- If elevation drops below the threshold → `EXIT`

This approach matches how visibility/“overhead-ness” works in real life: elevation is what matters.

---

## 🛰 Curated Space Objects (NORAD allowlist)

You can curate monitored objects via `data/space_objects.json` (recommended).  
If you don’t provide a curated list, you can use a config allowlist:

```toml
[satellites]
only_norad_ids = [25544, 20580]
```

Suggested starter set (NORAD IDs):

- **25544** — International Space Station (ISS) — https://www.nasa.gov/international-space-station/
- **48274** — Tiangong / CSS (core) — https://en.wikipedia.org/wiki/Tiangong_space_station
- **53239** — CSS Wentian — https://en.wikipedia.org/wiki/Wentian
- **54216** — CSS Mengtian — https://en.wikipedia.org/wiki/Mengtian
- **20580** — Hubble Space Telescope — https://science.nasa.gov/mission/hubble/
- **25338** — NOAA‑15 — https://www.nesdis.noaa.gov/current-satellite-missions/currently-flying/noaa-15
- **28654** — NOAA‑18 — https://www.nesdis.noaa.gov/current-satellite-missions/currently-flying/noaa-18
- **33591** — NOAA‑19 — https://www.nesdis.noaa.gov/current-satellite-missions/currently-flying/noaa-19
- **25994** — Terra — https://terra.nasa.gov/
- **27424** — Aqua — https://aqua.nasa.gov/
- **37849** — Suomi NPP — https://www.nesdis.noaa.gov/current-satellite-missions/currently-flying/joint-polar-satellite-system/suomi-npp
- **39084** — Landsat 8 — https://landsat.gsfc.nasa.gov/satellites/landsat-8/
- **49260** — Landsat 9 — https://landsat.gsfc.nasa.gov/satellites/landsat-9/
- **40697** — Sentinel‑2A — https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-2
- **42063** — Sentinel‑2B — https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-2

Note: The ISS and NASA mission pages are official. Sentinel links are official ESA/Copernicus mission pages.
Wikipedia links are included only where an official mission page is less direct for the specific module name.

---

## 🔁 Enrichment Fallback Behavior (what happens when APIs fail)

Enrichment is intentionally **best-effort**:
- If a provider is down, rate-limited, or returns incomplete fields, tracking continues.
- The CLI prints what it knows, and includes provider status tags when possible.

Typical flow:
1. **OpenSky** provides raw aircraft state (always the source of detection).
2. **ADSBDB** adds aircraft registry/type details when available.
3. **AirLabs** attempts airline + route details.
4. **AviationStack** acts as an additional fallback.

### API keys
Create accounts + keys on the provider sites and place keys in `.env`:

```bash
AIRLABS_API_KEY=your_key_here
AVIATIONSTACK_API_KEY=your_key_here
```

---

## 📁 Event Logging (JSONL)

Enable JSON Lines logging to record every event:

```toml
[event_logs]
enabled = true
path = "data/events.jsonl"
```

This is great for:
- building a dashboard later
- storing events in a database
- analytics on “how many planes per hour”
- tracking specific satellites over time

---

## ⚖️ Legal Disclaimer (please read)

This project is a hobbyist tool and is provided **as-is**.

- This software **does not** host, store, resell, or redistribute third‑party data.
- All external data is pulled directly from public APIs at runtime.
- **You are responsible** for complying with each provider’s Terms of Service, rate limits, and API key policies.

The maintainers are not responsible for:
- inaccurate or delayed data
- API changes, outages, or account bans
- misuse of third‑party services
- any downstream consequences of using this project

If you use this project, you agree you are using it at your own risk.

---

## 🤝 Contributing

PRs are welcome. Ideas:
- Cleaner terminal UI / summaries
- Optional web dashboard
- Historical analytics (heatmaps, time-of-day patterns)
- Additional enrichment providers
- Better caching strategies

---

## 🛰 Have fun watching the sky
When you hear a plane overhead, it’s pretty cool to be able to check the CLI and see where it’s coming from and where it’s going.
And when the ISS pops over the horizon… it feels a little sci‑fi (in the best way).

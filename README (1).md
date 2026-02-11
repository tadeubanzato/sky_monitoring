# 🌌 Sky Monitoring

### Real-time Aircraft & Satellite Overhead Tracking

Sky Monitoring is an open-source command-line tool that lets you observe
the sky above your location in real time.

It detects: - ✈️ Aircraft entering and exiting your defined radius - 🛰️
Satellites and space objects passing overhead - 📡 Enriched flight data
from multiple aviation data providers - 📁 Structured JSON events for
logging, automation, or further processing

This project is designed to be clean, hackable, modular, and free from
vendor lock-in.

------------------------------------------------------------------------

# 🚀 Features

## ✈ Aircraft Monitoring

-   Uses OpenSky Network state vectors
-   Detects ENTER and EXIT events within your configured radius
-   Optional multi-layer enrichment pipeline:
    -   ADSBDB (fast aircraft registry lookup)
    -   AirLabs (route, airline, metadata)
    -   AviationStack (fallback route & airline data)
-   Intelligent fallback behavior when APIs fail or rate limits are hit

## 🛰 Satellite & Space Object Monitoring

-   Uses TLE data from CelesTrak
-   Computes real-time elevation, azimuth, and range using Skyfield
-   Emits ENTER/EXIT events based on configurable elevation threshold
-   Supports curated monitoring via `data/space_objects.json`
-   Hot-reloads space object definitions without restarting

------------------------------------------------------------------------

# 📦 Installation

``` bash
git clone https://github.com/YOUR_USERNAME/sky_monitoring.git
cd sky_monitoring

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py
```

------------------------------------------------------------------------

# ⚙️ Configuration

Edit `config.toml` to configure:

-   Home latitude / longitude
-   Aircraft radius (km)
-   Satellite elevation threshold
-   Update intervals
-   Enrichment providers
-   Optional JSON event logging

Example:

``` toml
[home]
lat = 40.758
lon = -73.9855

[planes_opensky]
enabled = true
radius_km = 10

[satellites]
enabled = true
min_elevation_deg = 15
update_seconds = 2
```

------------------------------------------------------------------------

# 📡 Aviation Data Enrichment (Optional but Recommended)

Sky Monitoring uses a layered enrichment approach.

If one provider fails or is rate-limited, the system gracefully falls
back to others.

## 1️⃣ ADSBDB

Fast aircraft registration lookup.

Website: https://www.adsbdb.com/

-   No official public API documentation
-   Used primarily for aircraft registry and ICAO type lookup

------------------------------------------------------------------------

## 2️⃣ AirLabs

Flight route and airline enrichment.

Website: https://airlabs.co/

-   Create a free account
-   Generate your API key
-   Add to your `.env` file:

``` bash
AIRLABS_API_KEY=your_key_here
```

------------------------------------------------------------------------

## 3️⃣ AviationStack

Alternative route and airline enrichment.

Website: https://aviationstack.com/

-   Create an account
-   Generate API key
-   Add to `.env`:

``` bash
AVIATIONSTACK_API_KEY=your_key_here
```

------------------------------------------------------------------------

## 🔁 Enrichment Fallback Logic

1.  OpenSky provides raw ADS-B data.
2.  ADSBDB enriches aircraft registration/type.
3.  AirLabs enriches route and airline metadata.
4.  AviationStack acts as a fallback if route data is missing.

If all enrichment providers fail, raw aircraft data is still shown.

------------------------------------------------------------------------

# 🛰 Monitored Space Objects

Default curated objects (NORAD IDs):

-   25544 --- International Space Station (ISS)
-   48274 --- Tiangong Space Station
-   53239 --- CSS Wentian
-   54216 --- CSS Mengtian
-   20580 --- Hubble Space Telescope
-   25338 --- NOAA-15
-   28654 --- NOAA-18
-   33591 --- NOAA-19
-   25994 --- Terra
-   27424 --- Aqua
-   37849 --- Suomi NPP
-   39084 --- Landsat 8
-   49260 --- Landsat 9
-   40697 --- Sentinel-2A
-   42063 --- Sentinel-2B

Data source: https://celestrak.org/

TLE data is downloaded live and cached locally.

------------------------------------------------------------------------

# 📁 Event Logging

Optional JSON Lines logging:

``` toml
[event_logs]
enabled = true
path = "data/events.jsonl"
```

Each event is emitted in structured format suitable for automation or
analytics.

------------------------------------------------------------------------

# ⚖️ Legal Disclaimer

This project does **not** host, store, resell, or redistribute
third-party data.

All data is pulled directly from public APIs or publicly available
sources at runtime.

Users are responsible for: - Complying with each provider's Terms of
Service - Managing their own API keys - Respecting rate limits -
Ensuring lawful use within their jurisdiction

This software is provided **as-is**, without warranty of any kind.

The maintainers are not responsible for: - API policy changes - Service
outages - Inaccurate or delayed data - Misuse of third-party services

------------------------------------------------------------------------

# 🧠 Why This Exists

Because watching the sky is fun.

Because space is cool.

Because aircraft routing is fascinating.

And because open-source tools should be modular, transparent, and
respectful of upstream data providers.

------------------------------------------------------------------------

# 🤝 Contributing

Pull requests are welcome.

Ideas: - Terminal UI enhancements - Web dashboard frontend - Historical
analytics module - Additional enrichment providers - Improved caching
strategies

------------------------------------------------------------------------

# 🛰 Enjoy Watching the Sky

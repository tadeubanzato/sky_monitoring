# 🌌 Sky Monitoring

### Real-Time Aircraft & Satellite Overhead Tracking

Sky Monitoring is an open-source command-line tool that observes the sky
above your location in real time.

It detects:

-   ✈️ Aircraft entering and exiting a configurable geographic radius\
-   🛰️ Satellites and space objects passing overhead\
-   📡 Enriched flight metadata from multiple aviation data providers\
-   📁 Structured JSON events for logging, automation, or analytics

Built to be modular, transparent, and vendor-neutral.

------------------------------------------------------------------------

# 🚀 What This Project Does

## ✈ Aircraft Monitoring

-   Uses OpenSky Network state vectors
-   Detects ENTER and EXIT events within your configured radius
-   Supports layered enrichment from multiple providers
-   Gracefully degrades if enrichment APIs fail

## 🛰 Satellite Monitoring

-   Uses TLE data from CelesTrak
-   Computes elevation, azimuth, and range using Skyfield
-   Emits ENTER and EXIT events based on elevation threshold
-   Supports curated monitoring via `data/space_objects.json`
-   Hot-reloads monitored objects without restarting

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

# ⚙️ Configuration Overview

All settings are controlled via `config.toml`.

## Home Location

You may configure your observer location manually:

``` toml
[home]
lat = 40.758
lon = -73.9855
```

Manual coordinates are strongly recommended for accuracy and
reproducibility.

Optional IP-based geolocation:

``` toml
[home]
geolocation = true
```

IP-based geolocation may be imprecise and should not be used for
high-accuracy deployments.

------------------------------------------------------------------------

# ✈ Aircraft Radius Logic

``` toml
[planes_opensky]
radius_km = 10
```

Aircraft are tracked when their surface distance from your configured
latitude/longitude is less than or equal to `radius_km`.

Distance is calculated using great-circle geometry (Earth curvature
aware).

When an aircraft crosses into the radius → ENTER event\
When it leaves the radius → EXIT event

This is geographic proximity --- not radar strength or signal range.

------------------------------------------------------------------------

# 🛰 Satellite Elevation Logic

``` toml
[satellites]
min_elevation_deg = 15
```

Satellites are tracked based on elevation angle above the horizon.

-   0° → horizon\
-   90° → directly overhead

Elevation is calculated using orbital propagation from TLE data via
Skyfield.

------------------------------------------------------------------------

# 📡 Aviation Data Enrichment

Enrichment is optional but recommended.

Sky Monitoring uses a layered fallback system:

1.  OpenSky provides raw aircraft position
2.  ADSBDB enriches aircraft registration/type
3.  AirLabs enriches route and airline metadata
4.  AviationStack acts as additional fallback

If all enrichment providers fail, core tracking continues.

------------------------------------------------------------------------

## ADSBDB

Aircraft registration lookup.

https://www.adsbdb.com/

------------------------------------------------------------------------

## AirLabs

Route and airline enrichment.

https://airlabs.co/

-   Create account
-   Generate API key
-   Add to `.env`:

``` bash
AIRLABS_API_KEY=your_key_here
```

------------------------------------------------------------------------

## AviationStack

Alternative enrichment provider.

https://aviationstack.com/

-   Create account
-   Generate API key
-   Add to `.env`:

``` bash
AVIATIONSTACK_API_KEY=your_key_here
```

------------------------------------------------------------------------

# 🛰 Curated Space Objects

Default monitored NORAD IDs:

-   25544 --- International Space Station\
-   48274 --- Tiangong Space Station\
-   53239 --- CSS Wentian\
-   54216 --- CSS Mengtian\
-   20580 --- Hubble Space Telescope\
-   25338 --- NOAA-15\
-   28654 --- NOAA-18\
-   33591 --- NOAA-19\
-   25994 --- Terra\
-   27424 --- Aqua\
-   37849 --- Suomi NPP\
-   39084 --- Landsat 8\
-   49260 --- Landsat 9\
-   40697 --- Sentinel-2A\
-   42063 --- Sentinel-2B

TLE source: https://celestrak.org/

------------------------------------------------------------------------

# 📁 Event Logging

Optional JSON Lines logging:

``` toml
[event_logs]
enabled = true
path = "data/events.jsonl"
```

Events are structured and suitable for automation or analytics.

------------------------------------------------------------------------

# ⚖️ Legal Disclaimer

This software does not host, store, resell, or redistribute third-party
data.

All external data is pulled directly from public APIs at runtime.

Users are responsible for: - Complying with provider Terms of Service -
Managing their own API keys - Respecting rate limits - Ensuring lawful
use

This software is provided "as-is" without warranty of any kind.

The maintainers are not responsible for service outages, API changes,
data inaccuracies, or misuse of third-party services.

------------------------------------------------------------------------

# 🤝 Contributing

Pull requests are welcome.

Potential extensions: - Terminal UI improvements - Web dashboard -
Historical analytics - Additional enrichment providers - Performance
tuning

------------------------------------------------------------------------

# 🛰 Watch the Sky

Because space is cool.\
Because air traffic is fascinating.\
Because open source should be transparent and fun.

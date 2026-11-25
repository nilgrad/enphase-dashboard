# Enphase Local Dashboard ⚡

A self-hosted, offline-first web dashboard for real-time monitoring and historical energy tracking, communicating directly with your **Enphase IQ Gateway (Envoy)** over the local LAN.

Bypasses cloud latency and rate limits to deliver low-latency telemetry, resilient energy accounting, hardware diagnostics, and native TRMNL e-ink display rendering in a zero-build web interface.

See [CHANGELOG.md](CHANGELOG.md) for the v1.1.0 release notes.

---

## Features

- **100% Offline-First LAN UI**: Polls local gateway endpoints directly and serves pinned local copies of [Alpine.js](https://alpinejs.dev/) and [Chart.js](https://www.chartjs.org/) from `static/vendor/` with system fonts—zero external CDN or cloud dependencies.
- **Real-Time Power Flow**: Live gauges for Solar Production, Net Grid Flow (import/export), and House Consumption with selectable auto-refresh intervals (1s–5s), in-flight overlap protection, and automatic pause when backgrounded.
- **Cached Gateway Sessions & JWT Monitoring**: Reuses a thread-safe authenticated Envoy session with explicit HTTP timeouts, automatic `401`/`403` re-authentication, and proactive dashboard warnings before your annual Enphase JWT expires.
- **Resilient Energy Accounting**: Records hardware-level lifetime cumulative active energy counters ($Wh$) every 15 minutes with half-open local-timezone DST day boundaries and automatic multi-day outage interpolation.
- **Split-Phase & Inverter Health**: Dynamically detects production and grid meters via `/ivp/meters` metadata and monitors per-phase electrical readings alongside individual microinverter operating statuses.
- **Native TRMNL E-Ink Display (BYOS)**: Serves [TRMNL OG](https://trmnl.com) devices via authenticated BYOS endpoints (`Access-Token` + short-lived signed image URLs) with atomic 800×480 1-bit rendering and persistent telemetry.

---

## Architecture Overview

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                           Local Home Network                            │
│                                                                         │
│   ┌───────────────────────┐                                             │
│   │   Enphase IQ Gateway  │                                             │
│   │  (Envoy on Local LAN) │                                             │
│   └──────────┬────────────┘                                             │
│              │ HTTPS / Persistent Cached JWT Session                    │
│              ▼                                                          │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │           Flask + Gunicorn (1 Worker, 4 Threads)                │   │
│   │                                                                 │   │
│   │   ┌───────────────────┐     ┌───────────────────────────────┐   │   │
│   │   │  APScheduler Task │     │         Flask Web API         │   │   │
│   │   │  (15-min Polling) │     │  - REST & /api/health         │   │   │
│   │   │  (10-min TRMNL)   │     │  - Authenticated TRMNL BYOS   │   │   │
│   │   └─────────┬─────────┘     └───────────────▲───────────────┘   │   │
│   │             │                               │                   │   │
│   │             ▼                               │                   │   │
│   │   ┌─────────────────────────────────────────┴───────────────┐   │   │
│   │   │          Database (PostgreSQL / SQLite via ORM)         │   │   │
│   │   │            - meter_readings (Raw 15-min counters)       │   │   │
│   │   │            - daily_stats (Aggregated totals)            │   │   │
│   │   └─────────────────────────────────────────────────────────┘   │   │
│   └───────────────────────────┬───────────────────┬─────────────────┘   │
│                               │ JSON Over HTTP    │ 1-bit BMP / PNG     │
│                               ▼                   ▼                     │
│   ┌─────────────────────────────────────────┐   ┌───────────────────┐   │
│   │     Browser Dashboard (Offline-First)   │   │  TRMNL OG Device  │   │
│   │  Vendored Alpine.js + Chart.js          │   │ (800x480 E-Paper) │   │
│   └─────────────────────────────────────────┘   └───────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Deployment Guide (v1.1.0)

### 1. Configure Local Credentials & Settings

1. **Create your application config**:
   ```bash
   cp configuration/config_template.yml configuration/config.yml
   ```

2. **Edit `configuration/config.yml`**:
   ```yaml
   gateway_ip: "envoy.local"            # Local IP or mDNS hostname of your IQ Gateway
   use_https: true                      # Default: true for D7+ firmware
   gateway_token: "YOUR_TOKEN"          # Your Enphase Entrez / Enlighten bearer JWT
   # verify_ssl: false                  # Set to true or path to a CA bundle to verify Gateway TLS cert
   # gateway_timeout: 10                # HTTP timeout (seconds) for Enphase Gateway requests
   timezone: "America/New_York"         # Local timezone for daily solar boundary calculations

   # Reverse proxy support (set > 0 ONLY when deployed behind a trusted reverse proxy)
   # reverse_proxy:
   #   trusted_proxies: 0

   # Minimum cooldown (seconds) between manual POST /api/history/refresh triggers
   # history_refresh_cooldown_seconds: 30

   # TRMNL e-ink display configuration
   trmnl:
     enabled: true
     refresh_rate_seconds: 600          # Device wake interval in seconds (e.g. 600 = 10 min)
     render_interval_minutes: 10        # Background image re-render interval in minutes
     api_key: "CHANGE_ME_TRMNL_API_KEY" # Required Access-Token for TRMNL API endpoints
     setup_enabled: false               # Set true temporarily only while pairing a new TRMNL device
     allowed_macs: []                   # Optional MAC allowlist for /api/setup (e.g. ["AA:BB:CC:DD:EE:FF"])
     title: "DAILY SOLAR STATUS"        # Screen title
   ```

3. *(Optional)* **Add hardware specifications** for the Specs tab:
   ```bash
   cp configuration/system_template.yml configuration/system.yml
   ```
   *(If omitted, the Specs tab gracefully displays a setup notice.)*

4. **Create `.env` for Docker Compose**:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set a strong `POSTGRES_PASSWORD` (required by `docker-compose.yml`):
   ```dotenv
   POSTGRES_DB=enphase
   POSTGRES_USER=postgres
   POSTGRES_PASSWORD=replace-with-a-strong-password
   TRUST_PROXY_COUNT=0
   # Optional override for trmnl.api_key:
   # TRMNL_API_KEY=replace-with-a-strong-trmnl-token
   ```

> **Security Note:** `configuration/config.yml`, `configuration/system.yml`, and `.env` contain sensitive credentials and are automatically ignored by both `.gitignore` and `.dockerignore`.

---

### 2. Deploy with Docker Compose (Recommended)

The production Compose stack runs the dashboard as a **non-root user** (`appuser`) behind Gunicorn (`--workers 1 --threads 4 --timeout 60` via `server.app:create_app()`), waits for PostgreSQL to pass its `pg_isready` healthcheck before starting, and monitors the app via `/api/health`.

```bash
# Build the image and start both services in the background
docker compose up -d --build

# Verify both containers are healthy
docker compose ps
curl -f http://localhost:5000/api/health
```

Open your browser to:
```text
http://localhost:5000
```

Persistent state is stored in two named Docker volumes:
- `pgdata`: PostgreSQL database (`/var/lib/postgresql/data`)
- `instance_data`: Rendered TRMNL PNG/BMP images and device telemetry (`/app/instance`)

**Updating or redeploying later:**
```bash
git pull
docker compose up -d --build
```

---

### 3. Deploying Behind a Reverse Proxy (Nginx, Caddy, Traefik)

By default (`TRUST_PROXY_COUNT=0`), `X-Forwarded-Proto` and `X-Forwarded-Host` headers are ignored so clients cannot spoof TRMNL image URLs.

If you expose the dashboard behind a single reverse proxy:
1. Set `TRUST_PROXY_COUNT=1` in `.env` (or `reverse_proxy.trusted_proxies: 1` in `configuration/config.yml`).
2. Ensure your proxy forwards `Host`, `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host`.
3. Restart the dashboard container: `docker compose up -d`.

---

### 4. Upgrading from Pre-1.0.0 Deployments

If you are upgrading an existing installation to **v1.0.0**, note these breaking/security changes:

1. **Create `.env` with `POSTGRES_PASSWORD`**: Hardcoded `secretpassword` was removed from `docker-compose.yml`. If reusing an existing `pgdata` volume created with the old password, either set `POSTGRES_PASSWORD=secretpassword` in `.env` first and rotate the Postgres password via `ALTER USER postgres WITH PASSWORD '...';`, or start fresh.
2. **TRMNL `Access-Token` is now enforced**: Set a non-empty `trmnl.api_key` in `configuration/config.yml` (or `TRMNL_API_KEY` in `.env`) matching the API Key configured on your TRMNL device. Unauthenticated calls to `/api/display`, `/api/display/current.{bmp,png}`, and `POST /api/log` now return `401`.
3. **`/api/setup` is closed by default**: Enable `trmnl.setup_enabled: true` (optionally with `trmnl.allowed_macs`) only while pairing a new device, then switch it back to `false`.
4. **App factory entrypoint**: Custom Gunicorn commands should target `server.app:create_app()` instead of `server.app:app`.
5. **Removed `/api/production`**: Unused proxy endpoint removed in favor of `/api/meters`.

---

### 5. Run Locally (Development)

If you prefer running outside Docker:

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install pinned dependencies
pip install -r requirements.txt

# 3. Start the dashboard (uses SQLite at instance/enphase.db by default)
python server/app.py
```

Or run locally with Gunicorn matching production:
```bash
gunicorn --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:5000 "server.app:create_app()"
```

---

## Key API Endpoints

| Endpoint | Description |
| :--- | :--- |
| `GET /api/health` | Healthcheck verifying database connectivity (`200` ok / `503` unhealthy) |
| `GET /api/token-status` | Enphase Gateway JWT expiration metadata (never returns raw token) |
| `GET /api/meters` | Real-time meter readings annotated with `/ivp/meters` metadata |
| `GET /api/history/today` | Canonical daily solar summary for today in the configured local timezone |
| `GET /api/history/daily` | Daily aggregated history; supports optional `?days=`, `?start=YYYY-MM-DD`, `?end=YYYY-MM-DD` |
| `GET /api/history/monthly` | SQL-aggregated monthly energy totals (`GROUP BY` year/month) |
| `POST /api/history/refresh` | Manually trigger a reading & aggregation (rate-limited by cooldown) |
| `GET /api/inventory` | Microinverter inventory and health statuses |
| `GET /api/connectivity` | Gateway cloud/MQTT/stream connectivity status |
| `GET /api/events` | Active gateway alerts |

---

## TRMNL E-Ink Display Integration (BYOS)

The dashboard includes native **Bring Your Own Server (BYOS)** support for [TRMNL](https://trmnl.com) OG e-paper displays, eliminating any need for the official TRMNL cloud service, intermediary servers like `terminus`, or headless browsers.

### Endpoints
- `GET /api/setup`: Returns device handshake and configuration instructions. Disabled by default (`trmnl.setup_enabled: false`) for unauthenticated callers; enable temporarily (optionally with `trmnl.allowed_macs`) when pairing a new device, or authenticate with `Access-Token`.
- `GET /api/display`: Polled by TRMNL on wake (requires `Access-Token` header matching `trmnl.api_key`); returns sleep duration, landscape rotation, dynamic filename, and a short-lived signed `image_url`.
- `GET /api/display/current.bmp` & `GET /api/display/current.png`: Serves the rendered 1-bit monochrome image (requires `Access-Token` header or a valid signed URL from `/api/display`).
- `POST /api/log`: Ingests and acknowledges device telemetry and error logs (requires `Access-Token` header; payload capped at 16 KiB).
- `GET /api/display/status`: Returns JSON with the latest recorded device status (battery voltage, Wi-Fi RSSI, firmware version, and logs).
- `GET /trmnl`: Interactive in-browser display preview (also linked from the main dashboard header when TRMNL is enabled).

### Setting Up Your TRMNL Device
1. Set a strong `trmnl.api_key` in `configuration/config.yml` (and temporarily set `trmnl.setup_enabled: true` if your device uses `/api/setup` during initial pairing).
2. Put your TRMNL OG device into **Setup Mode** (double-click the physical button).
3. Connect to the device's Wi-Fi hotspot (`TRMNL-...`) and navigate to `http://192.168.4.1`.
4. In the Wi-Fi setup captive portal:
   - Navigate to **Advanced** > **Custom Server**.
   - Enable Custom Server and set the URL to the address of your `enphase-dashboard` server (without a trailing slash):
     - Local LAN IP: `http://<YOUR_SERVER_IP>:5000`
     - Domain / Reverse proxy: `https://<YOUR_DOMAIN>`
   - Set **API Key** to the `api_key` configured in your `config.yml`.
5. Save and reboot. Once paired, set `trmnl.setup_enabled: false` in `config.yml`. The TRMNL firmware will poll `/api/display` using its `Access-Token` to render your solar status screen. Single-clicking the device button forces an immediate update.

---

## Running Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
node --check static/js/dashboard.js
node --test tests/test_dashboard_polling.js
```

---

## Detailed Documentation

For a deep dive into the mathematical formulas, cumulative meter accumulation theory, timezone boundary translation, and downtime gap interpolation:

- [docs/database_integration.md](docs/database_integration.md)
- [CHANGELOG.md](CHANGELOG.md)

---

## License

MIT License.

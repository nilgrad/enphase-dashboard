# Changelog

All notable changes to **Enphase Local Dashboard** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-26

Upgraded the TRMNL 800x480 1-bit e-ink display layout with a 7-day trend bar chart, solar coverage progress bar, net grid status pills, gateway token expiry alerts, live preview re-rendering, and dashboard theme synchronization.

### Added
- **7-Day Trend Chart & Solar Coverage Panel on TRMNL** (#26): Added a DB-backed 7-day paired Solar vs. Usage bar chart (solid black Solar bars vs. pure 1-bit 45-degree hatched Usage bars with an inverted `TODAY` pill), a Solar Coverage progress bar with a 50% midpoint tick and direct/consumption ratio, and a side-by-side Export/Import breakdown.
- **TRMNL Gateway Token Expiry Alerts** (#26): Surfaced an inverted warning pill (`TOKEN EXPIRED` / `TOKEN XD`) in the TRMNL footer bar whenever the Enphase gateway JWT is expired or expiring soon.
- **Live TRMNL Preview Re-Render & Theme Sync** (#26): Added `POST /trmnl/refresh` so the preview page's "Refresh Screen" button triggers an immediate server-side re-render, and synced `/trmnl` with the main dashboard's `Auto / Dark / Light` theme preference (`enphase-theme`).
- **Configurable TRMNL Orientation** (#26): Added `trmnl.image_rotate` in `configuration/config.yml` (default `1`) instead of hardcoding rotation in `/api/display`.

### Changed
- **3-Column TRMNL Hero Row & Typography Polish** (#26): Replaced the uniform 6-box grid with a 3-column hero row (`SOLAR`, `USAGE`, `NET GRID`) featuring a 1-bit lightning bolt header mark, sun/house icons, inverted `EXPORT`/`IMPORT` (or outlined `BALANCED`) pill badges, shared bottom numeric baselines, automatic font step-down for 3-digit values, and header title truncation.

### Fixed
- **TRMNL Sync & Edge-Case Fixes** (#26): Made `record_reading()` honor `trmnl.enabled: false`, synced midnight `AWAITING DATA` and outage `ESTIMATED` status labels with the web dashboard, prevented `-0.0 kWh Import` formatting on near-zero net grid values, and replaced the `NO DATA` fallback clock timestamp with `No readings yet`.

## [1.1.0] - 2026-11-25

Major UI/UX upgrade introducing a live 3-node energy flow diagram, emerald-green solar palette, History timeframe controls and CSV export, inverter health filtering, phase totals, and navigation status badges.

### Added
- **Live Energy Flow Stage & Power Mix Bar** (#20): Added a 3-node Solar -> Home <-> Grid visualization with animated directional SVG flow connectors, a real-time home solar vs. grid power mix bar, a daily solar coverage progress bar, and an initial 3-card skeleton shimmer loader.
- **Calm Live Polling Status Indicator** (#20): Added a `Live / Paused` status pill with last-updated timestamp (`HH:MM:SS`) and separated background auto-polling from manual refreshes so the Refresh button no longer flickers every 1 to 3 seconds.
- **History Timeframe Controls, Period Totals & CSV Export** (#21): Added `7D / 14D / 30D` timeframe selector pills, a period summary strip, a right-aligned Solar Coverage (%) column, human-readable table dates, and one-click client-side CSV export (`enphase-daily-history.csv`).
- **Inverter Health Filtering, Phase Totals & Tab Badges** (#22): Added `All` / `Issues` inverter filter pills with automatic unhealthy-first sorting, relative last-reported timestamps with full-timestamp tooltips, multi-phase `Total / Avg` footer rows, Health/Events count badges on navigation tabs, and a mobile tab-strip horizontal scroll fade cue.

### Changed
- **Emerald Green Solar Palette** (#19): Updated Solar Production, brand accents, SVG favicon, and TRMNL preview chrome from amber to Emerald Green, with Sky Blue for Grid Export, Cool Slate for House Consumption, Rose Red for Grid Import, and Violet for Solar Coverage.
- **History Bar + Line Combo Chart** (#21): Converted House Consumption in the Daily Energy History chart from a 4th bar series into a smooth trend line overlay above Solar Production, Grid Export, and Grid Import bars with shared index tooltips.

## [1.0.1] - 2026-11-25

Visual design refresh with a solar-native color system, Light/Dark/Auto theme support, typography improvements, and TRMNL preview polish.

### Added
- **Light, Dark, and Auto Theme Toggle** (#18): Added a persisted `Auto / Dark / Light` theme switcher in the dashboard header with a no-flash startup script and live Chart.js theme re-rendering.

### Changed
- **Solar-Native Color Palette & Contrast** (#18): Replaced generic indigo chrome with Deep Slate + Solar Amber accents and unified semantic energy colors (Solar Amber for production, Emerald for export, Rose-Red for import, Slate-Blue for consumption, Violet for coverage/offset) with WCAG AA text contrast.
- **Typography & Tabular Numerals** (#18): Registered the bundled local `Monaspace Krypton` font for gateway IP pills, inverter serial numbers, and telemetry readouts; added `font-variant-numeric: tabular-nums` across live metrics and tables; right-aligned numeric table columns.
- **Header, Navigation, Cards & Charts** (#18): Upgraded the header with an inline SVG emblem, modernized the tab strip with inline SVG icons and ARIA tab semantics, converted real-time meter readings into a 3-column KPI hero grid, structured System Specs into key-value cards, and aligned Chart.js styling (rounded bars, theme-aware grids and tooltips).
- **TRMNL Preview Alignment** (#18): Matched `/trmnl` colors with the dark slate + solar amber palette, added a `Back to Dashboard` link, removed the duplicate Telemetry JSON button, and made the 800x480 screen bezel scale fluidly on narrower viewports.

### Fixed
- **Semantic Color & Summary Refresh Polish** (#18): Corrected `Grid Imported` cards on Live and Summary tabs to use the `.imported` (red) modifier instead of neutral styling, replaced inline `:style` borders on inverter cards with semantic classes, and kept Summary charts mounted during refresh to prevent layout jumps.
- **PostgreSQL Driver Compatibility** (#17): Switched to `psycopg[binary]` for SQLAlchemy 2.1 compatibility.

## [1.0.0] - 2026-11-24

First stable production release featuring resilient energy accounting, gateway session caching, security hardening, offline-first frontend assets, container healthchecks, and automated test coverage.

### Added
- **Lightweight Daily Summary & Date-Range APIs** (#7): Added `GET /api/history/today` for canonical local-day metrics and optional `?days=`, `?start=`, and `?end=` date-window filters on `GET /api/history/daily`.
- **Healthcheck & Token Expiration Endpoints** (#9, #10): Added `GET /api/health` (verifying database connectivity) and `GET /api/token-status` (decoding the Enphase JWT `exp` claim server-side without exposing the raw token).
- **UI Error & JWT Expiration Banners** (#10): Added dismissible inline API error banners across all tabs, proactive Enphase JWT expiry/expired banners, a **TRMNL Preview** header link when TRMNL is enabled, and `hashchange` support for browser Back/Forward tab navigation.
- **Offline-First Vendored Assets** (#10): Bundled pinned local copies of Alpine.js (`3.14.9`) and Chart.js (`4.4.8`) under `static/vendor/` and removed external CDN and Google Fonts dependencies.
- **Environment Template & Deployment Hardening** (#8, #9): Added `.env.example`, PostgreSQL `pg_isready` healthchecks with `depends_on: service_healthy` in `docker-compose.yml`, and a non-root container user (`appuser`) + `/api/health` `HEALTHCHECK` in `Dockerfile`.
- **Automated Test Suite & CI** (#6, #7, #8, #9, #10): Added `pytest` (`requirements-dev.txt`, `pytest.ini`) and Node built-in frontend tests covering energy accounting, caching, security, reliability, and frontend UX via GitHub Actions.

### Changed
- **Enphase Gateway Session & Config Caching** (#7): Reused a single thread-safe `ManagedGateway` (`requests.Session`) across requests with configurable HTTP timeouts (`gateway_timeout`, default `10s`), optional TLS verification (`verify_ssl` / `ca_bundle`), automatic coalesced `401`/`403` re-authentication, and mtime-based in-memory caching for `config.yml` and `system.yml`.
- **Live Tab Polling & SQL Aggregation** (#7): Replaced `setInterval` with chained `setTimeout`, an in-flight guard, `visibilitychange` background pause/resume, and a 60s cadence for `/api/history/today`; moved `/api/history/monthly` totals to portable SQL `GROUP BY` / `SUM` queries; updated `fetchSummary()` to request `/api/history/daily?days=14`.
- **Multi-Threaded Gunicorn & App Factory** (#7, #9): Refactored `server/app.py` into a side-effect-free `create_app()` factory and updated Gunicorn to `--workers 1 --threads 4 --timeout 60`.
- **Structured Logging & Pinned Dependencies** (#9): Replaced all `print()` calls in `server/` with Python's `logging` module, replaced deprecated `datetime.utcnow`, added `PyJWT` directly to `requirements.txt`, removed duplicate `psycopg2-binary` (retaining `psycopg[binary]` for SQLAlchemy 2.1), and pinned all package versions.
- **Frontend Chart & Style Cleanup** (#10): Deduplicated daily/monthly effectiveness Chart.js configuration with an empty-array `Math.max` guard, and extracted repeated inline styles from `templates/tabs/*.html` into reusable classes in `static/style.css`.

### Fixed
- **Multi-Day Outage Energy Accounting & DST Boundaries** (#6): Preserved pre-outage same-day energy in `interpolate_gap()`, bounded `aggregate_day()` lookups to prevent recovery-day double-counting, and switched to half-open `[day_start, next_day_start)` UTC bounds for 23h/25h DST days.
- **Metric Consistency & Midnight Rollover** (#6): Unified Solar Coverage and Net Grid Flow formulas between the Web UI and TRMNL, prevented yesterday's totals from displaying as today's after midnight, and reflected actual data freshness (`ACTIVE` / `STALE` / `NO DATA`) in the TRMNL footer.
- **Atomic TRMNL File Writes & Persistence** (#6): Made TRMNL PNG/BMP renders and telemetry writes lock-protected and atomic (`os.replace()`), and moved runtime files out of `static/` into `instance/` (backed by Docker's `instance_data` volume).
- **Hardware Portability & Optional Specs** (#6): Replaced hardcoded meter EIDs and positional array assumptions with `/ivp/meters` metadata (`measurementType`), guarded missing `system.yml` keys, and made the panel specs header configurable.
- **Mobile Button Visibility** (#10): Stopped hiding `.refresh-btn` on mobile viewports so Refresh and "Sync Database Now" buttons remain accessible on small screens.

### Security
- **Disabled Scheduler Management API** (#8): Set `SCHEDULER_API_ENABLED = False` so unauthenticated `/scheduler/*` routes are never exposed.
- **Enforced TRMNL Authentication & Size Limits** (#8): Required constant-time `Access-Token` validation (`hmac.compare_digest`) on `/api/display`, `/api/display/current.bmp`, `/api/display/current.png`, and `POST /api/log` (with short-lived HMAC-signed `image_url` links and dedicated `/trmnl/preview.*` browser routes), gated unauthenticated `/api/setup` behind `trmnl.setup_enabled` and optional `trmnl.allowed_macs`, and capped `POST /api/log` payloads at 16 KiB.
- **Removed Hardcoded Database Credentials** (#8): Replaced inline `secretpassword` in `docker-compose.yml` with `${POSTGRES_PASSWORD}` loaded from `.env`.
- **Sanitized Errors, Safe Proxy Headers & Refresh Rate Limit** (#8): Logged tracebacks server-side while returning generic error messages and `502`/`504` gateway status codes, replaced manual `X-Forwarded-*` header parsing with opt-in Werkzeug `ProxyFix`, and added cooldown/concurrency rate limiting (`429`) on `POST /api/history/refresh`.

### Removed
- Removed unused `GET /api/production` endpoint, backwards-compatibility `app.view_functions` aliases, deprecated `FLASK_ENV=production` setting, and unused `raw*` JSON state variables in `static/js/dashboard.js` (#9).

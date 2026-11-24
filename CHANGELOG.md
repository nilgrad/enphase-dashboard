# Changelog

All notable changes to **Enphase Local Dashboard** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

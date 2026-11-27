import logging
import threading
import time as time_module
from datetime import datetime, date, time, timedelta, timezone
from server.db import db, MeterReading, DailyStats

logger = logging.getLogger(__name__)

GAP_THRESHOLD = timedelta(hours=2)
STALE_READING_THRESHOLD = timedelta(minutes=45)
METER_METADATA_TTL_SECONDS = 300

_meter_metadata_lock = threading.Lock()
_meter_metadata_cache = {
    "host": None,
    "fetched_at": 0.0,
    "by_eid": {},
}


def normalize_dt(dt):
    """Ensure datetimes are timezone-naive representing UTC for safe mathematical comparisons."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def get_target_timezone():
    """Load the configured timezone from config.yml, defaulting to America/New_York."""
    from server.config import load_config
    from zoneinfo import ZoneInfo
    config = load_config()
    tz_name = config.get('timezone', 'America/New_York')
    try:
        return ZoneInfo(tz_name)
    except Exception as e:
        logger.warning("Error loading timezone %s, falling back to America/New_York: %s", tz_name, e)
        return ZoneInfo('America/New_York')


def get_local_day_utc_bounds(day_date, target_tz):
    """
    Return the half-open UTC interval [day_start, next_day_start) as naive UTC datetimes
    for a local calendar date in target_tz (handles 23h/25h DST days cleanly).
    """
    local_start = datetime.combine(day_date, time.min, tzinfo=target_tz)
    next_local_start = datetime.combine(day_date + timedelta(days=1), time.min, tzinfo=target_tz)
    day_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    next_day_start = next_local_start.astimezone(timezone.utc).replace(tzinfo=None)
    return day_start, next_day_start


def get_meter_metadata(gateway, force_refresh=False):
    """
    Fetch and cache /ivp/meters metadata keyed by meter eid so meters can be identified
    by measurementType rather than hardcoded EIDs or array positions.
    """
    host = getattr(gateway, 'host', None)
    now = time_module.monotonic()

    with _meter_metadata_lock:
        if (
            not force_refresh
            and _meter_metadata_cache["host"] == host
            and _meter_metadata_cache["by_eid"]
            and (now - _meter_metadata_cache["fetched_at"]) < METER_METADATA_TTL_SECONDS
        ):
            return dict(_meter_metadata_cache["by_eid"])

    by_eid = {}
    try:
        meters = gateway.api_call('/ivp/meters')
        if isinstance(meters, list):
            for meter in meters:
                if isinstance(meter, dict) and 'eid' in meter:
                    by_eid[meter['eid']] = {
                        'measurementType': meter.get('measurementType'),
                        'state': meter.get('state'),
                    }
    except Exception as e:
        logger.warning("Could not fetch /ivp/meters metadata, falling back to positional mapping: %s", e)

    if by_eid:
        with _meter_metadata_lock:
            _meter_metadata_cache["host"] = host
            _meter_metadata_cache["fetched_at"] = now
            _meter_metadata_cache["by_eid"] = dict(by_eid)

    return by_eid


def annotate_meter_readings(readings, metadata_by_eid):
    """Attach measurementType and meterState from /ivp/meters metadata onto reading dicts."""
    if not isinstance(readings, list):
        return readings
    annotated = []
    for reading in readings:
        if not isinstance(reading, dict):
            annotated.append(reading)
            continue
        item = dict(reading)
        meta = metadata_by_eid.get(item.get('eid')) if metadata_by_eid else None
        if meta:
            if meta.get('measurementType'):
                item['measurementType'] = meta['measurementType']
            if meta.get('state'):
                item['meterState'] = meta['state']
        annotated.append(item)
    return annotated


def select_production_and_grid_meters(readings):
    """
    Identify the production and grid (net-consumption) meter readings using
    measurementType metadata when available, falling back to positional indices [0] and [1].
    """
    if not isinstance(readings, list) or len(readings) < 2:
        raise ValueError("Invalid readings payload from Gateway")

    def _find_by_type(measurement_type):
        enabled_matches = [
            m for m in readings
            if isinstance(m, dict)
            and m.get('measurementType') == measurement_type
            and m.get('meterState', 'enabled') == 'enabled'
        ]
        if enabled_matches:
            return enabled_matches[0]
        any_matches = [
            m for m in readings
            if isinstance(m, dict) and m.get('measurementType') == measurement_type
        ]
        return any_matches[0] if any_matches else None

    prod_meter = _find_by_type('production') or readings[0]
    grid_meter = (
        _find_by_type('net-consumption')
        or _find_by_type('total-consumption')
        or (readings[1] if readings[1] is not prod_meter else readings[0])
    )
    return prod_meter, grid_meter


def get_gateway_readings():
    """Fetch readings from Enphase Envoy Gateway, annotated with meter metadata."""
    from server.config import load_config, get_authenticated_gateway
    config = load_config()
    gateway = get_authenticated_gateway(config)
    data = gateway.api_call('/ivp/meters/readings')
    if not data or len(data) < 2:
        raise ValueError("Invalid readings payload from Gateway")
    metadata = get_meter_metadata(gateway)
    return annotate_meter_readings(data, metadata)


def compute_daily_metrics(stats, target_date, latest_reading=None, now_utc=None):
    """
    Build the canonical daily solar status payload shared by both the Web UI and TRMNL.
    - Net Grid Flow = export - import (positive = net export, negative = net import)
    - Solar Coverage = direct solar self-consumption / house consumption * 100% (0..100%)
    """
    now_utc = normalize_dt(now_utc or datetime.now(timezone.utc))

    if stats:
        production = float(stats.production_kwh or 0.0)
        consumption = float(stats.consumption_kwh or 0.0)
        export_val = float(stats.export_kwh or 0.0)
        import_val = float(stats.import_kwh or 0.0)
        is_interpolated = bool(stats.is_interpolated)
        has_data = True
    else:
        production = 0.0
        consumption = 0.0
        export_val = 0.0
        import_val = 0.0
        is_interpolated = False
        has_data = False

    net_grid = export_val - import_val
    direct_use = max(0.0, production - export_val)
    coverage = min(100.0, max(0.0, (direct_use / consumption) * 100.0)) if consumption > 0 else 0.0

    last_reading_ts = normalize_dt(latest_reading.timestamp) if latest_reading else None
    is_stale = (
        last_reading_ts is None
        or (now_utc - last_reading_ts) > STALE_READING_THRESHOLD
    )

    return {
        "date": target_date.isoformat(),
        "has_data": has_data,
        "production_kwh": round(production, 2),
        "consumption_kwh": round(consumption, 2),
        "export_kwh": round(export_val, 2),
        "import_kwh": round(import_val, 2),
        "net_grid_kwh": round(net_grid, 2),
        "is_net_export": net_grid >= 0,
        "solar_coverage_pct": round(coverage, 1),
        "is_interpolated": is_interpolated,
        "is_stale": is_stale,
        "has_readings": last_reading_ts is not None,
        "last_reading_utc": last_reading_ts.isoformat() if last_reading_ts else None,
    }


def get_today_summary(now_utc=None):
    """Fetch canonical solar metrics specifically for today in the configured local timezone."""
    target_tz = get_target_timezone()
    now_utc_aware = (
        now_utc.replace(tzinfo=timezone.utc)
        if now_utc is not None and now_utc.tzinfo is None
        else (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    )
    today_local = now_utc_aware.astimezone(target_tz).date()

    stats = db.session.get(DailyStats, today_local)
    latest_reading = MeterReading.query.order_by(MeterReading.timestamp.desc()).first()
    return compute_daily_metrics(stats, today_local, latest_reading=latest_reading, now_utc=now_utc_aware)


def record_reading(app):
    """Background task to fetch readings and record them in the database."""
    with app.app_context():
        try:
            data = get_gateway_readings()

            prod_meter, grid_meter = select_production_and_grid_meters(data)
            prod_wh = prod_meter.get('actEnergyDlvd', 0.0)
            import_wh = grid_meter.get('actEnergyDlvd', 0.0)
            export_wh = grid_meter.get('actEnergyRcvd', 0.0)

            # Use timestamp from gateway or fallback to current UTC time (naive UTC for SQL compatibility)
            gw_ts = prod_meter.get('timestamp')
            if gw_ts:
                timestamp = normalize_dt(datetime.fromtimestamp(gw_ts, tz=timezone.utc))
            else:
                timestamp = normalize_dt(datetime.now(timezone.utc))

            # Check if this reading already exists
            existing = db.session.get(MeterReading, timestamp)
            if existing:
                logger.info("Reading at %s already exists. Skipping.", timestamp)
                return

            # Get the previous reading strictly before this timestamp to check for gaps
            prev_reading = MeterReading.query.filter(
                MeterReading.timestamp < timestamp
            ).order_by(MeterReading.timestamp.desc()).first()
            prev_ts = normalize_dt(prev_reading.timestamp) if prev_reading else None

            new_reading = MeterReading(
                timestamp=timestamp,
                production_wh=prod_wh,
                import_wh=import_wh,
                export_wh=export_wh
            )
            db.session.add(new_reading)
            db.session.commit()
            logger.info(
                "Recorded reading at %s UTC: Prod=%s Wh, Import=%s Wh, Export=%s Wh",
                timestamp, prod_wh, import_wh, export_wh,
            )

            # Determine local date in target timezone
            target_tz = get_target_timezone()
            local_date = timestamp.replace(tzinfo=timezone.utc).astimezone(target_tz).date()

            if prev_ts:
                time_diff = timestamp - prev_ts
                prev_local_date = prev_ts.replace(tzinfo=timezone.utc).astimezone(target_tz).date()
                if time_diff > GAP_THRESHOLD and prev_local_date < local_date:
                    logger.info(
                        "Cross-day gap detected between %s and %s (%.2f hours). Re-aggregating spanned days...",
                        prev_ts, timestamp, time_diff.total_seconds() / 3600,
                    )
                    interpolate_gap(prev_reading, new_reading)
                else:
                    aggregate_day(local_date)
            else:
                aggregate_day(local_date)

            # Update TRMNL screen if enabled
            try:
                from server.config import load_config
                cfg = load_config()
                if cfg.get('trmnl', {}).get('enabled', True):
                    from server.trmnl_service import update_trmnl_display
                    update_trmnl_display(app)
            except Exception:
                logger.exception("TRMNL update failed after recording reading")

        except Exception:
            db.session.rollback()
            logger.exception("Error in record_reading")


def interpolate_gap(prev_reading, new_reading):
    """
    Re-aggregate every local calendar day spanned by a gap between prev_reading and new_reading.
    Delegates to aggregate_day() so pre-outage readings on start_date and post-outage readings
    on end_date are preserved and combined accurately with proportional gap slices.
    """
    t_start = normalize_dt(prev_reading.timestamp)
    t_end = normalize_dt(new_reading.timestamp)
    if (t_end - t_start).total_seconds() <= 0:
        return

    target_tz = get_target_timezone()
    start_date = t_start.replace(tzinfo=timezone.utc).astimezone(target_tz).date()
    end_date = t_end.replace(tzinfo=timezone.utc).astimezone(target_tz).date()

    current_date = start_date
    while current_date <= end_date:
        aggregate_day(current_date)
        current_date += timedelta(days=1)


def aggregate_day(day_date):
    """
    Calculate the production and consumption metrics for a specific local date in the target timezone.

    Uses consecutive MeterReading intervals overlapping [day_start, next_day_start):
    - Intervals fully within the day contribute 100% of their delta.
    - Normal cross-midnight intervals (<= GAP_THRESHOLD) are attributed 100% to the day they end on
      (and 0% to the day they start on), preserving exact energy conservation without estimation.
    - Long cross-midnight gap intervals (> GAP_THRESHOLD) are apportioned by exact time overlap
      with [day_start, next_day_start), and the day is flagged with is_interpolated = True.
    """
    target_tz = get_target_timezone()
    day_start, next_day_start = get_local_day_utc_bounds(day_date, target_tz)

    prev_before_day = MeterReading.query.filter(
        MeterReading.timestamp < day_start
    ).order_by(MeterReading.timestamp.desc()).first()

    day_readings = MeterReading.query.filter(
        MeterReading.timestamp >= day_start,
        MeterReading.timestamp < next_day_start
    ).order_by(MeterReading.timestamp.asc()).all()

    first_after_day = MeterReading.query.filter(
        MeterReading.timestamp >= next_day_start
    ).order_by(MeterReading.timestamp.asc()).first()

    points = []
    if prev_before_day is not None:
        points.append(prev_before_day)
    points.extend(day_readings)
    if first_after_day is not None:
        points.append(first_after_day)

    if len(points) < 2:
        return

    prod_wh_total = 0.0
    import_wh_total = 0.0
    export_wh_total = 0.0
    is_interpolated = False
    contributed = False

    for r_a, r_b in zip(points, points[1:]):
        t_a = normalize_dt(r_a.timestamp)
        t_b = normalize_dt(r_b.timestamp)
        total_secs = (t_b - t_a).total_seconds()
        if total_secs <= 0:
            continue

        a_in_day = day_start <= t_a < next_day_start
        b_in_day = day_start <= t_b < next_day_start

        if a_in_day and b_in_day:
            fraction = 1.0
        elif (t_b - t_a) <= GAP_THRESHOLD:
            # Normal overnight reading: attribute 100% to the day containing r_b.
            if b_in_day:
                fraction = 1.0
            else:
                fraction = 0.0
        else:
            # Gap (> GAP_THRESHOLD) crossing at least one midnight boundary.
            overlap_start = max(t_a, day_start)
            overlap_end = min(t_b, next_day_start)
            overlap_secs = (overlap_end - overlap_start).total_seconds()
            if overlap_secs > 0:
                fraction = overlap_secs / total_secs
                is_interpolated = True
            else:
                fraction = 0.0

        if fraction <= 0.0:
            continue

        contributed = True
        prod_wh_total += max(0.0, r_b.production_wh - r_a.production_wh) * fraction
        import_wh_total += max(0.0, r_b.import_wh - r_a.import_wh) * fraction
        export_wh_total += max(0.0, r_b.export_wh - r_a.export_wh) * fraction

    if not contributed:
        return

    prod_kwh = max(0.0, prod_wh_total / 1000.0)
    import_kwh = max(0.0, import_wh_total / 1000.0)
    export_kwh = max(0.0, export_wh_total / 1000.0)
    cons_kwh = max(0.0, prod_kwh - export_kwh + import_kwh)

    stats = db.session.get(DailyStats, day_date)
    if not stats:
        stats = DailyStats(date=day_date)
        db.session.add(stats)

    stats.production_kwh = prod_kwh
    stats.import_kwh = import_kwh
    stats.export_kwh = export_kwh
    stats.consumption_kwh = cons_kwh
    stats.is_interpolated = is_interpolated

    db.session.commit()
    status_label = " (interpolated)" if is_interpolated else ""
    logger.info(
        "Aggregated stats for %s%s: Prod=%.2f kWh, Import=%.2f kWh, Export=%.2f kWh, Cons=%.2f kWh",
        day_date, status_label, prod_kwh, import_kwh, export_kwh, cons_kwh,
    )

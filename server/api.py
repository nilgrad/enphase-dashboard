import os
import time
import json
from datetime import date, datetime, timedelta, timezone
from flask import Blueprint, jsonify, request, send_file, make_response, current_app
from sqlalchemy import func, select
from server.db import db, DailyStats
from server.config import load_config, get_authenticated_gateway
import server.trmnl_service as trmnl_service
from server.dashboard_services import (
    get_meter_metadata,
    annotate_meter_readings,
    get_today_summary,
    get_target_timezone,
)
from server.trmnl_service import (
    update_trmnl_display,
    load_trmnl_telemetry,
    record_trmnl_checkin,
    record_trmnl_log_message
)

api_bp = Blueprint('api', __name__, url_prefix='/api')


def get_request_base_url():
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
    host = request.headers.get('X-Forwarded-Host', request.host)
    return f"{scheme}://{host}".rstrip('/')


# ---------------------------------------------------------------------------
# Enphase Gateway Proxy Endpoints
# ---------------------------------------------------------------------------

@api_bp.route('/production')
def production_api():
    config = load_config()
    gateway = get_authenticated_gateway(config)
    try:
        raw_data = gateway.api_call('/ivp/meters/reports/', response_raw=True)
        return jsonify(json.loads(raw_data))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route('/meters')
def meters_api():
    config = load_config()
    gateway = get_authenticated_gateway(config)
    try:
        raw_data = gateway.api_call('/ivp/meters/readings', response_raw=True)
        readings = json.loads(raw_data)
        metadata = get_meter_metadata(gateway)
        return jsonify(annotate_meter_readings(readings, metadata))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route('/inventory')
def inventory_api():
    config = load_config()
    gateway = get_authenticated_gateway(config)
    try:
        raw_data = gateway.api_call('/inventory.json', response_raw=True)
        return jsonify(json.loads(raw_data))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route('/events')
def events_api():
    config = load_config()
    gateway = get_authenticated_gateway(config)
    try:
        # Using home.json alerts as the reliable source for system events
        data = gateway.api_call('/home.json')
        alerts = data.get('alerts', [])
        # Return as a simple list for the UI
        return jsonify({"alerts": alerts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route('/connectivity')
def connectivity_api():
    config = load_config()
    gateway = get_authenticated_gateway(config)
    try:
        raw_data = gateway.api_call('/ivp/livedata/status', response_raw=True)
        return jsonify(json.loads(raw_data))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Historical Aggregations Endpoints
# ---------------------------------------------------------------------------

@api_bp.route('/history/today')
def today_history_api():
    try:
        return jsonify(get_today_summary())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _parse_iso_date(value, param_name):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid '{param_name}' date format; expected YYYY-MM-DD.")


def build_monthly_history_query():
    """Build a portable SQL GROUP BY query aggregating DailyStats by year and month."""
    year_expr = func.extract('year', DailyStats.date)
    month_expr = func.extract('month', DailyStats.date)
    return (
        select(
            year_expr.label('year'),
            month_expr.label('month'),
            func.coalesce(func.sum(DailyStats.production_kwh), 0.0).label('production_kwh'),
            func.coalesce(func.sum(DailyStats.import_kwh), 0.0).label('import_kwh'),
            func.coalesce(func.sum(DailyStats.export_kwh), 0.0).label('export_kwh'),
            func.coalesce(func.sum(DailyStats.consumption_kwh), 0.0).label('consumption_kwh'),
        )
        .group_by(year_expr, month_expr)
        .order_by(year_expr.asc(), month_expr.asc())
    )


@api_bp.route('/history/daily')
def daily_history_api():
    try:
        days_param = request.args.get('days')
        start_param = request.args.get('start')
        end_param = request.args.get('end')

        start_date = _parse_iso_date(start_param, 'start') if start_param else None
        end_date = _parse_iso_date(end_param, 'end') if end_param else None

        if days_param is not None and days_param != '':
            try:
                days = int(days_param)
            except ValueError:
                return jsonify({"error": "Invalid 'days' parameter; must be a positive integer."}), 400
            if days < 1:
                return jsonify({"error": "Invalid 'days' parameter; must be >= 1."}), 400
            if start_date is not None and end_date is not None:
                return jsonify({"error": "Cannot combine 'days' with both 'start' and 'end'."}), 400

            if end_date is not None:
                start_date = end_date - timedelta(days=days - 1)
            elif start_date is not None:
                end_date = start_date + timedelta(days=days - 1)
            else:
                today_local = datetime.now(timezone.utc).astimezone(get_target_timezone()).date()
                start_date = today_local - timedelta(days=days - 1)

        if start_date is not None and end_date is not None and start_date > end_date:
            return jsonify({"error": "'start' date must be on or before 'end' date."}), 400

        query = DailyStats.query
        if start_date is not None:
            query = query.filter(DailyStats.date >= start_date)
        if end_date is not None:
            query = query.filter(DailyStats.date <= end_date)

        stats = query.order_by(DailyStats.date.asc()).all()
        return jsonify([s.to_dict() for s in stats])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route('/history/monthly')
def monthly_history_api():
    try:
        rows = db.session.execute(build_monthly_history_query()).all()
        result = [
            {
                'month': f"{int(row.year):04d}-{int(row.month):02d}",
                'production_kwh': round(float(row.production_kwh), 2),
                'import_kwh': round(float(row.import_kwh), 2),
                'export_kwh': round(float(row.export_kwh), 2),
                'consumption_kwh': round(float(row.consumption_kwh), 2),
            }
            for row in rows
        ]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route('/history/refresh', methods=['POST'])
def refresh_history_api():
    try:
        from server.dashboard_services import record_reading
        record_reading(current_app._get_current_object())
        return jsonify({"status": "success", "message": "Readings pulled and aggregated successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# TRMNL E-Ink Display BYOS Endpoints
# ---------------------------------------------------------------------------

@api_bp.route('/setup', methods=['GET'])
def trmnl_setup_api():
    config = load_config()
    trmnl_cfg = config.get('trmnl', {})
    api_key = trmnl_cfg.get('api_key', 'local-trmnl-token')
    base_url = get_request_base_url()
    return jsonify({
        "status": 200,
        "api_key": api_key,
        "friendly_id": "TRMNL-SOLAR",
        "image_url": f"{base_url}/api/display/current.bmp",
        "filename": "solar_setup.bmp",
        "message": "TRMNL connected to Enphase solar dashboard"
    })


@api_bp.route('/display', methods=['GET'])
def trmnl_display_api():
    config = load_config()
    trmnl_cfg = config.get('trmnl', {})
    refresh_rate = int(trmnl_cfg.get('refresh_rate_seconds', 600))
    base_url = get_request_base_url()

    record_trmnl_checkin(dict(request.headers))

    bmp_path = trmnl_service.OUTPUT_BMP_PATH
    if not os.path.exists(bmp_path):
        update_trmnl_display(current_app._get_current_object())

    # TRMNL firmware uses filename comparison to skip refresh if unchanged.
    # Appending mtime ensures TRMNL detects when a new render is available.
    mtime = int(os.path.getmtime(bmp_path)) if os.path.exists(bmp_path) else int(time.time())
    filename = f"solar_{mtime}.bmp"

    return jsonify({
        "status": 0,
        "image_url": f"{base_url}/api/display/current.bmp",
        "filename": filename,
        "refresh_rate": refresh_rate,
        "reset_firmware": False,
        "update_firmware": False,
        "firmware_url": None,
        "special_function": "none",
        "image_rotate": 1
    })


@api_bp.route('/display/current.png', methods=['GET'])
def trmnl_current_png():
    png_path = trmnl_service.OUTPUT_IMAGE_PATH
    if not os.path.exists(png_path):
        update_trmnl_display(current_app._get_current_object())
    response = make_response(send_file(png_path, mimetype='image/png'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@api_bp.route('/display/current.bmp', methods=['GET'])
def trmnl_current_bmp():
    bmp_path = trmnl_service.OUTPUT_BMP_PATH
    if not os.path.exists(bmp_path):
        update_trmnl_display(current_app._get_current_object())
    response = make_response(send_file(bmp_path, mimetype='image/bmp'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@api_bp.route('/log', methods=['POST'])
def trmnl_log_api():
    log_data = request.get_json(silent=True) or request.form.to_dict() or request.data.decode('utf-8', errors='ignore')
    record_trmnl_log_message(dict(request.headers), log_data)
    return jsonify({"status": 200})


@api_bp.route('/display/status', methods=['GET'])
def trmnl_status_api():
    return jsonify(load_trmnl_telemetry())

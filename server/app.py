import os
import sys

# Ensure repository root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, render_template, send_file, make_response
from flask_apscheduler import APScheduler
from werkzeug.middleware.proxy_fix import ProxyFix
from server.db import db, MeterReading
from server.config import load_config, load_specs
from server.api import api_bp
import server.trmnl_service as trmnl_service
from server.trmnl_service import update_trmnl_display, load_trmnl_telemetry

# Configure Flask app with explicit template, static, and instance paths
template_dir = os.path.join(PROJECT_ROOT, 'templates')
static_dir = os.path.join(PROJECT_ROOT, 'static')
instance_dir = os.path.join(PROJECT_ROOT, 'instance')

app = Flask(
    __name__,
    template_folder=template_dir,
    static_folder=static_dir,
    instance_path=instance_dir
)

def configure_proxy_fix(flask_app, config=None):
    """Wrap WSGI app in Werkzeug ProxyFix only when explicitly configured."""
    trusted_proxies = 0
    env_val = os.environ.get('TRUST_PROXY_COUNT')
    if env_val is not None and str(env_val).strip() != '':
        try:
            trusted_proxies = int(env_val)
        except ValueError:
            trusted_proxies = 0
    else:
        cfg = config if config is not None else load_config()
        proxy_cfg = cfg.get('reverse_proxy') if isinstance(cfg, dict) else None
        if isinstance(proxy_cfg, dict):
            raw = proxy_cfg.get('trusted_proxies', 0)
        elif isinstance(cfg, dict):
            raw = cfg.get('trusted_proxies', 0)
        else:
            raw = 0
        try:
            trusted_proxies = int(raw)
        except (TypeError, ValueError):
            trusted_proxies = 0

    if trusted_proxies > 0:
        flask_app.wsgi_app = ProxyFix(
            flask_app.wsgi_app,
            x_for=trusted_proxies,
            x_proto=trusted_proxies,
            x_host=trusted_proxies,
            x_prefix=trusted_proxies,
        )
    return trusted_proxies


# Database configuration
database_url = os.environ.get('DATABASE_URL', 'sqlite:///enphase.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SCHEDULER_API_ENABLED'] = False
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1 MiB global request size cap

configure_proxy_fix(app)

db.init_app(app)

# Register API routes blueprint
app.register_blueprint(api_bp)

# Add backwards-compatibility aliases for view functions in url_map/view_functions
for endpoint in [
    'production_api', 'meters_api', 'inventory_api', 'events_api',
    'connectivity_api', 'today_history_api', 'daily_history_api', 'monthly_history_api',
    'refresh_history_api', 'trmnl_setup_api', 'trmnl_display_api',
    'trmnl_current_png', 'trmnl_current_bmp', 'trmnl_log_api', 'trmnl_status_api'
]:
    bp_endpoint = f'api.{endpoint}'
    if bp_endpoint in app.view_functions:
        app.view_functions[endpoint] = app.view_functions[bp_endpoint]

# Initialize Scheduler
scheduler = APScheduler()
scheduler.init_app(app)


@app.route('/')
def index():
    config = load_config()
    specs = load_specs()
    return render_template('index.html', config=config, specs=specs)


@app.route('/trmnl', methods=['GET'])
def trmnl_preview_page():
    config = load_config()
    trmnl_cfg = config.get('trmnl', {})
    refresh_rate_sec = int(trmnl_cfg.get('refresh_rate_seconds', 600))
    refresh_rate_min = max(1, refresh_rate_sec // 60)
    telemetry = load_trmnl_telemetry()
    return render_template('trmnl.html', telemetry=telemetry, refresh_rate_min=refresh_rate_min)


@app.route('/trmnl/preview.png', methods=['GET'])
def trmnl_preview_png():
    png_path = trmnl_service.OUTPUT_IMAGE_PATH
    if not os.path.exists(png_path):
        update_trmnl_display(app)
    response = make_response(send_file(png_path, mimetype='image/png'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/trmnl/preview.bmp', methods=['GET'])
def trmnl_preview_bmp():
    bmp_path = trmnl_service.OUTPUT_BMP_PATH
    if not os.path.exists(bmp_path):
        update_trmnl_display(app)
    response = make_response(send_file(bmp_path, mimetype='image/bmp'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# Scheduled job definitions
@scheduler.task('interval', id='record_readings_job', minutes=15)
def scheduled_fetch():
    from server.dashboard_services import record_reading
    record_reading(app)


# TRMNL render scheduled job
config_init = load_config()
trmnl_interval = int(config_init.get('trmnl', {}).get('render_interval_minutes', 10))


@scheduler.task('interval', id='render_trmnl_job', minutes=trmnl_interval)
def scheduled_trmnl():
    cfg = load_config()
    if cfg.get('trmnl', {}).get('enabled', True):
        update_trmnl_display(app)


# Create tables and start scheduler
with app.app_context():
    db.create_all()
    # Trigger initial fetch at startup if DB is empty to populate initial record
    try:
        if not MeterReading.query.first():
            print("Database empty. Performing initial readings fetch...")
            from server.dashboard_services import record_reading
            record_reading(app)
    except Exception as e:
        print(f"Failed to perform initial fetch: {e}")

    # Generate initial TRMNL screen
    try:
        cfg = load_config()
        if cfg.get('trmnl', {}).get('enabled', True):
            update_trmnl_display(app)
    except Exception as e:
        print(f"Failed to perform initial TRMNL render: {e}")

if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    scheduler.start()
    print("Background data collection scheduler started.")

if __name__ == '__main__':
    print("Starting dashboard.")
    app.run(debug=True, host='0.0.0.0', port=5000)

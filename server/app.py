import logging
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
from server.config import load_config, load_specs, get_gateway_token_status
from server.api import api_bp
import server.trmnl_service as trmnl_service
from server.trmnl_service import update_trmnl_display, load_trmnl_telemetry

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logger = logging.getLogger(__name__)


def configure_logging(level=None):
    """Configure root logging with timestamps and log levels (idempotent)."""
    log_level_name = level or os.environ.get('LOG_LEVEL', 'INFO')
    log_level = getattr(logging, str(log_level_name).upper(), logging.INFO)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=log_level, format=LOG_FORMAT)
    else:
        root_logger.setLevel(log_level)


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


def _register_routes(flask_app):
    @flask_app.route('/')
    def index():
        config = load_config()
        specs = load_specs()
        token_status = get_gateway_token_status(config)
        return render_template('index.html', config=config, specs=specs, token_status=token_status)

    @flask_app.route('/trmnl', methods=['GET'])
    def trmnl_preview_page():
        config = load_config()
        trmnl_cfg = config.get('trmnl', {})
        refresh_rate_sec = int(trmnl_cfg.get('refresh_rate_seconds', 600))
        refresh_rate_min = max(1, refresh_rate_sec // 60)
        telemetry = load_trmnl_telemetry()
        return render_template('trmnl.html', telemetry=telemetry, refresh_rate_min=refresh_rate_min)

    @flask_app.route('/trmnl/preview.png', methods=['GET'])
    def trmnl_preview_png():
        png_path = trmnl_service.OUTPUT_IMAGE_PATH
        if not os.path.exists(png_path):
            update_trmnl_display(flask_app)
        response = make_response(send_file(png_path, mimetype='image/png'))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    @flask_app.route('/trmnl/preview.bmp', methods=['GET'])
    def trmnl_preview_bmp():
        bmp_path = trmnl_service.OUTPUT_BMP_PATH
        if not os.path.exists(bmp_path):
            update_trmnl_display(flask_app)
        response = make_response(send_file(bmp_path, mimetype='image/bmp'))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response


def _run_startup_tasks(flask_app):
    with flask_app.app_context():
        try:
            if not MeterReading.query.first():
                logger.info("Database empty. Performing initial readings fetch...")
                from server.dashboard_services import record_reading
                record_reading(flask_app)
        except Exception:
            logger.exception("Failed to perform initial readings fetch")

        try:
            cfg = load_config()
            if cfg.get('trmnl', {}).get('enabled', True):
                update_trmnl_display(flask_app)
        except Exception:
            logger.exception("Failed to perform initial TRMNL render")


def _init_scheduler(flask_app):
    scheduler = APScheduler()
    scheduler.init_app(flask_app)

    @scheduler.task('interval', id='record_readings_job', minutes=15)
    def scheduled_fetch():
        from server.dashboard_services import record_reading
        record_reading(flask_app)

    config_init = load_config()
    trmnl_interval = int(config_init.get('trmnl', {}).get('render_interval_minutes', 10))

    @scheduler.task('interval', id='render_trmnl_job', minutes=trmnl_interval)
    def scheduled_trmnl():
        cfg = load_config()
        if cfg.get('trmnl', {}).get('enabled', True):
            update_trmnl_display(flask_app)

    scheduler.start()
    flask_app.extensions['apscheduler'] = scheduler
    logger.info("Background data collection scheduler started.")
    return scheduler


def create_app(
    config_overrides=None,
    *,
    init_db=True,
    start_scheduler=True,
    run_initial_tasks=True,
):
    """Application factory with zero import-time side effects."""
    configure_logging()

    template_dir = os.path.join(PROJECT_ROOT, 'templates')
    static_dir = os.path.join(PROJECT_ROOT, 'static')
    instance_dir = os.path.join(PROJECT_ROOT, 'instance')

    flask_app = Flask(
        __name__,
        template_folder=template_dir,
        static_folder=static_dir,
        instance_path=instance_dir,
    )

    database_url = os.environ.get('DATABASE_URL', 'sqlite:///enphase.db')
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    flask_app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    flask_app.config['SCHEDULER_API_ENABLED'] = False
    flask_app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1 MiB global request size cap

    if config_overrides:
        flask_app.config.update(config_overrides)

    configure_proxy_fix(flask_app)

    db.init_app(flask_app)
    flask_app.register_blueprint(api_bp)
    _register_routes(flask_app)

    if init_db:
        with flask_app.app_context():
            db.create_all()

    is_active_process = not flask_app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    if run_initial_tasks and is_active_process:
        _run_startup_tasks(flask_app)

    if start_scheduler and is_active_process:
        _init_scheduler(flask_app)

    return flask_app


if __name__ == '__main__':
    application = create_app({'DEBUG': True})
    logger.info("Starting dashboard.")
    application.run(debug=True, host='0.0.0.0', port=5000)

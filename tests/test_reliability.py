import ast
import os
import re
import subprocess
import sys
import warnings
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml
from PIL import Image

from server.app import create_app
from server.dashboard_services import (
    aggregate_day,
    compute_daily_metrics,
    interpolate_gap,
)
from server.db import DailyStats, MeterReading, db
import server.trmnl_service as trmnl_service

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def app():
    flask_app = create_app(
        {
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        },
        init_db=True,
        start_scheduler=False,
        run_initial_tasks=False,
    )
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def _add_reading(local_dt_str, prod_wh, imp_wh, exp_wh, tz_name='America/New_York'):
    tz = ZoneInfo(tz_name)
    dt_local = datetime.fromisoformat(local_dt_str).replace(tzinfo=tz)
    dt_utc_naive = dt_local.astimezone(timezone.utc).replace(tzinfo=None)
    reading = MeterReading(
        timestamp=dt_utc_naive,
        production_wh=prod_wh,
        import_wh=imp_wh,
        export_wh=exp_wh,
    )
    db.session.add(reading)
    db.session.commit()
    return reading


# ---------------------------------------------------------------------------
# 1. App Factory & Import-Time Isolation
# ---------------------------------------------------------------------------

def test_importing_server_app_has_no_side_effects():
    script = (
        "import threading, server.app as m; "
        "assert hasattr(m, 'create_app'); "
        "assert not hasattr(m, 'app'), 'module-level app must not be created on import'; "
        "assert not any('APScheduler' in t.name for t in threading.enumerate()); "
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, '-c', script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Import check failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert proc.stdout.strip() == 'OK'


def test_create_app_removes_view_function_aliases_and_dead_production_endpoint(app, client):
    assert app.config['SCHEDULER_API_ENABLED'] is False
    # Backwards-compatibility aliases removed; only blueprint routes exist
    assert 'meters_api' not in app.view_functions
    assert 'api.meters_api' in app.view_functions
    assert 'api.health_api' in app.view_functions

    # Dead /api/production endpoint removed
    assert client.get('/api/production').status_code == 404

    # Core UI pages render cleanly
    assert client.get('/').status_code == 200
    assert client.get('/trmnl').status_code == 200


# ---------------------------------------------------------------------------
# 2. Healthcheck Endpoint & Docker / Compose / Dependency Hygiene
# ---------------------------------------------------------------------------

def test_api_health_ok_and_unhealthy_db(client):
    resp = client.get('/api/health')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok', 'database': 'ok'}

    with patch.object(db.session, 'execute', side_effect=RuntimeError('postgres auth failed secret')):
        unhealthy = client.get('/api/health')
    assert unhealthy.status_code == 503
    body = unhealthy.get_json()
    assert body == {'status': 'unhealthy', 'database': 'error'}
    assert 'secret' not in str(body)


def test_dockerfile_has_non_root_user_healthcheck_and_factory_cmd():
    with open(os.path.join(PROJECT_ROOT, 'Dockerfile'), 'r') as f:
        dockerfile = f.read()

    assert re.search(r'^USER\s+(?!root\b)\S+', dockerfile, flags=re.MULTILINE), 'Dockerfile must switch to non-root USER'
    assert re.search(r'^HEALTHCHECK\s+', dockerfile, flags=re.MULTILINE)
    assert '/api/health' in dockerfile
    assert 'server.app:create_app()' in dockerfile
    # PyJWT should be in requirements.txt, not appended manually in Dockerfile
    assert 'requirements.txt PyJWT' not in dockerfile


def test_docker_compose_uses_db_healthcheck_and_drops_flask_env():
    with open(os.path.join(PROJECT_ROOT, 'docker-compose.yml'), 'r') as f:
        raw = f.read()
    assert 'FLASK_ENV' not in raw

    compose = yaml.safe_load(raw)
    app_svc = compose['services']['enphase-dashboard']
    db_svc = compose['services']['db']

    assert app_svc['depends_on'] == {'db': {'condition': 'service_healthy'}}
    assert 'healthcheck' in db_svc
    assert any('pg_isready' in str(part) for part in db_svc['healthcheck']['test'])


def test_requirements_txt_is_pinned_includes_pyjwt_and_has_single_pg_driver():
    with open(os.path.join(PROJECT_ROOT, 'requirements.txt'), 'r') as f:
        lines = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]

    names = [re.split(r'[=<>!~]', line)[0].lower() for line in lines]
    assert 'pyjwt' in names
    assert 'psycopg[binary]' in names
    assert 'psycopg2-binary' not in names
    assert 'psycopg2' not in names

    # Every requirement line must be version-pinned with ==
    for line in lines:
        assert '==' in line, f'Unpinned dependency in requirements.txt: {line}'

    # Ensure SQLAlchemy's default postgresql:// driver can be instantiated without ImportError
    from sqlalchemy import create_engine
    engine = create_engine('postgresql://postgres:test@localhost:5432/enphase')
    try:
        assert engine.driver == 'psycopg'
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. Logging, Deprecation Fixes & Dead Frontend State Cleanup
# ---------------------------------------------------------------------------

def test_no_print_calls_remain_in_server_package():
    server_dir = os.path.join(PROJECT_ROOT, 'server')
    print_calls = []
    for fname in os.listdir(server_dir):
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(server_dir, fname)
        with open(fpath, 'r') as f:
            tree = ast.parse(f.read(), filename=fpath)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'print':
                print_calls.append(f'{fname}:{node.lineno}')
    assert print_calls == [], f'Found remaining print() calls in server/: {print_calls}'


def test_db_model_avoids_deprecated_utcnow(app):
    db_path = os.path.join(PROJECT_ROOT, 'server', 'db.py')
    with open(db_path, 'r') as f:
        tree = ast.parse(f.read(), filename=db_path)
    utcnow_attrs = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == 'utcnow'
    ]
    assert utcnow_attrs == []

    with app.app_context(), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', DeprecationWarning)
        row = DailyStats(
            date=date(2026, 7, 1),
            production_kwh=10.0,
            import_kwh=2.0,
            export_kwh=4.0,
            consumption_kwh=8.0,
        )
        db.session.add(row)
        db.session.commit()
        assert row.updated_at is not None

    utcnow_warnings = [w for w in caught if 'utcnow' in str(w.message)]
    assert utcnow_warnings == []


def test_dashboard_js_removed_unused_raw_json_state():
    with open(os.path.join(PROJECT_ROOT, 'static', 'js', 'dashboard.js'), 'r') as f:
        js = f.read()
    for dead_prop in (
        'rawMeters',
        'rawSummary',
        'rawHistory',
        'rawPhases',
        'rawInventory',
        'rawConnectivity',
        'rawEvents',
    ):
        assert dead_prop not in js, f'Unused state {dead_prop} should be removed from dashboard.js'


# ---------------------------------------------------------------------------
# 4. Energy Accounting Edge Cases (Single Reading, Midnight, DST, Gaps)
# ---------------------------------------------------------------------------

def test_aggregate_day_skips_first_day_with_single_reading(app):
    with patch('server.dashboard_services.get_target_timezone', return_value=ZoneInfo('America/New_York')):
        with app.app_context():
            _add_reading('2026-06-01T12:00:00', 100000, 50000, 20000)
            aggregate_day(date(2026, 6, 1))
            assert db.session.get(DailyStats, date(2026, 6, 1)) is None


def test_aggregate_day_midnight_boundary_attribution(app):
    with patch('server.dashboard_services.get_target_timezone', return_value=ZoneInfo('America/New_York')):
        with app.app_context():
            # Day 1 readings ending at 23:50
            _add_reading('2026-06-10T23:35:00', 100000, 50000, 20000)
            _add_reading('2026-06-10T23:50:00', 100000, 50500, 20000)
            # Normal 15m cross-midnight reading at 00:05 on June 11 (+0.5 kWh import)
            _add_reading('2026-06-11T00:05:00', 100000, 51000, 20000)
            _add_reading('2026-06-11T00:20:00', 100000, 51500, 20000)

            aggregate_day(date(2026, 6, 10))
            aggregate_day(date(2026, 6, 11))

            d10 = db.session.get(DailyStats, date(2026, 6, 10))
            d11 = db.session.get(DailyStats, date(2026, 6, 11))
            assert d10 is not None and d11 is not None
            assert not d10.is_interpolated and not d11.is_interpolated
            # Cross-midnight interval (23:50 -> 00:05) belongs 100% to June 11, 0% to June 10
            assert d10.import_kwh == pytest.approx(0.5)
            assert d11.import_kwh == pytest.approx(1.0)


def test_aggregate_day_dst_spring_forward_and_fall_back_transitions(app):
    with patch('server.dashboard_services.get_target_timezone', return_value=ZoneInfo('America/New_York')):
        with app.app_context():
            # Spring forward: 2026-03-08 (23-hour day)
            _add_reading('2026-03-08T00:30:00', 10000, 5000, 1000)
            _add_reading('2026-03-08T03:30:00', 10000, 6000, 1000)  # 2 real hours elapsed (02:00 skipped)
            _add_reading('2026-03-08T15:00:00', 22000, 7000, 8000)
            aggregate_day(date(2026, 3, 8))

            spring = db.session.get(DailyStats, date(2026, 3, 8))
            assert spring is not None
            assert not spring.is_interpolated
            assert spring.production_kwh == pytest.approx(12.0)
            assert spring.import_kwh == pytest.approx(2.0)
            assert spring.export_kwh == pytest.approx(7.0)
            assert spring.consumption_kwh == pytest.approx(7.0)

            # Fall back: 2026-11-01 (25-hour day) with normal prior-night baseline
            _add_reading('2026-10-31T23:45:00', 30000, 9500, 9000)
            _add_reading('2026-11-01T00:30:00', 30000, 10000, 9000)
            _add_reading('2026-11-01T14:00:00', 40000, 13000, 14000)
            aggregate_day(date(2026, 11, 1))

            fall = db.session.get(DailyStats, date(2026, 11, 1))
            assert fall is not None
            assert not fall.is_interpolated
            assert fall.production_kwh == pytest.approx(10.0)
            assert fall.import_kwh == pytest.approx(3.5)
            assert fall.export_kwh == pytest.approx(5.0)
            assert fall.consumption_kwh == pytest.approx(8.5)


def test_interpolate_gap_same_day_outage_is_exact_and_not_interpolated(app):
    with patch('server.dashboard_services.get_target_timezone', return_value=ZoneInfo('America/New_York')):
        with app.app_context():
            r1 = _add_reading('2026-06-15T08:00:00', 50000, 10000, 5000)
            # 6-hour same-day outage (08:00 -> 14:00)
            r2 = _add_reading('2026-06-15T14:00:00', 75000, 12000, 19000)
            interpolate_gap(r1, r2)

            stats = db.session.get(DailyStats, date(2026, 6, 15))
            assert stats is not None
            assert not stats.is_interpolated
            assert stats.production_kwh == pytest.approx(25.0)
            assert stats.import_kwh == pytest.approx(2.0)
            assert stats.export_kwh == pytest.approx(14.0)
            assert stats.consumption_kwh == pytest.approx(13.0)


# ---------------------------------------------------------------------------
# 5. TRMNL Rendering Edge Cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    'name,stats_kwargs,expected_coverage,expected_net_export',
    [
        (
            'zero_consumption',
            {'production_kwh': 5.0, 'consumption_kwh': 0.0, 'export_kwh': 5.0, 'import_kwh': 0.0},
            0.0,
            True,
        ),
        (
            'over_100_pct_offset',
            {'production_kwh': 50.0, 'consumption_kwh': 15.0, 'export_kwh': 35.0, 'import_kwh': 0.0},
            100.0,
            True,
        ),
        (
            'negative_net_grid_large_import',
            {'production_kwh': 2.5, 'consumption_kwh': 127.9, 'export_kwh': 0.0, 'import_kwh': 125.4},
            2.0,
            False,
        ),
    ],
)
def test_trmnl_rendering_and_metrics_edge_cases(tmp_path, name, stats_kwargs, expected_coverage, expected_net_export):
    stats = DailyStats(date=date(2026, 6, 20), is_interpolated=False, **stats_kwargs)
    now_utc = datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc)
    latest = MeterReading(
        timestamp=datetime(2026, 6, 20, 17, 50),
        production_wh=1000,
        import_wh=500,
        export_wh=200,
    )
    metrics = compute_daily_metrics(stats, date(2026, 6, 20), latest_reading=latest, now_utc=now_utc)
    assert metrics['solar_coverage_pct'] == pytest.approx(expected_coverage)
    assert metrics['is_net_export'] is expected_net_export

    net = metrics['net_grid_kwh']
    render_payload = {
        'title': 'DAILY SOLAR STATUS',
        'production': metrics['production_kwh'],
        'consumption': metrics['consumption_kwh'],
        'export': metrics['export_kwh'],
        'import': metrics['import_kwh'],
        'net_grid_str': f"+{net:.1f}" if metrics['is_net_export'] else f"-{abs(net):.1f}",
        'net_grid_unit': 'kWh Export' if metrics['is_net_export'] else 'kWh Import',
        'is_export': metrics['is_net_export'],
        'coverage': metrics['solar_coverage_pct'],
        'last_updated': 'Jun 20, 01:50 PM',
        'is_stale': metrics['is_stale'],
        'status_label': 'ACTIVE',
    }
    png_path = str(tmp_path / f'{name}.png')
    bmp_path = str(tmp_path / f'{name}.bmp')
    trmnl_service.render_trmnl_solar(render_payload, output_png_path=png_path, output_bmp_path=bmp_path)

    with Image.open(png_path) as im:
        assert im.size == (800, 480)
        assert im.mode == '1'
    with Image.open(bmp_path) as im:
        assert im.size == (800, 480)
        assert im.mode == '1'

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests
import urllib3
from flask import Flask
from sqlalchemy import event
from sqlalchemy.dialects import postgresql

# Allow running tests even in minimal environments without Enphase-API installed
if 'enphase_api' not in sys.modules:
    _enphase_pkg = types.ModuleType('enphase_api')
    _enphase_local = types.ModuleType('enphase_api.local')
    _enphase_gw = types.ModuleType('enphase_api.local.gateway')

    class _StubGateway:
        TIMEOUT = 300
        HEADERS = {'Accept': 'application/json'}

        def __init__(self, host=None, cert_file=None):
            self.host = host or 'https://envoy.local'
            self.session = requests.Session()

        def login(self, token):
            return True

    _enphase_gw.Gateway = _StubGateway
    sys.modules['enphase_api'] = _enphase_pkg
    sys.modules['enphase_api.local'] = _enphase_local
    sys.modules['enphase_api.local.gateway'] = _enphase_gw

import server.config as config_module
from server.api import api_bp, build_monthly_history_query
from server.db import DailyStats, MeterReading, db

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def create_test_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(PROJECT_ROOT, 'templates'),
        static_folder=os.path.join(PROJECT_ROOT, 'static'),
    )
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    app.register_blueprint(api_bp)
    with app.app_context():
        db.create_all()
    return app


def _make_response(status_code=200, json_body=None, text_body=None, url='https://envoy.local/ivp/meters/readings'):
    resp = requests.Response()
    resp.status_code = status_code
    resp.url = url
    resp.reason = 'OK' if status_code == 200 else ('Unauthorized' if status_code == 401 else 'Forbidden' if status_code == 403 else 'Error')
    if json_body is not None:
        resp._content = json.dumps(json_body).encode('utf-8')
    elif text_body is not None:
        resp._content = text_body.encode('utf-8')
    else:
        resp._content = b''
    return resp


class YamlConfigCachingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        config_module.reset_caches()

    def tearDown(self):
        config_module.reset_caches()
        self.tmpdir.cleanup()

    def test_load_config_caches_by_mtime_and_reloads_on_change(self):
        cfg_path = os.path.join(self.tmpdir.name, 'config.yml')
        with open(cfg_path, 'w') as f:
            f.write('gateway_ip: "192.168.1.10"\nuse_https: true\n')

        with patch.object(config_module, 'CONFIG_FILE', cfg_path), \
             patch.object(config_module.yaml, 'safe_load', wraps=config_module.yaml.safe_load) as mock_load:
            first = config_module.load_config()
            second = config_module.load_config()
            self.assertEqual(first['gateway_ip'], '192.168.1.10')
            self.assertEqual(second['gateway_ip'], '192.168.1.10')
            # Second call hits in-memory cache without re-parsing YAML
            self.assertEqual(mock_load.call_count, 1)

            # Mutating returned dict must not corrupt the cache
            first['gateway_ip'] = 'mutated'
            third = config_module.load_config()
            self.assertEqual(third['gateway_ip'], '192.168.1.10')
            self.assertEqual(mock_load.call_count, 1)

            # Modify file and advance mtime to trigger reload
            with open(cfg_path, 'w') as f:
                f.write('gateway_ip: "192.168.1.20"\nuse_https: false\n')
            stat = os.stat(cfg_path)
            os.utime(cfg_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

            reloaded = config_module.load_config()
            self.assertEqual(reloaded['gateway_ip'], '192.168.1.20')
            self.assertFalse(reloaded['use_https'])
            self.assertEqual(mock_load.call_count, 2)

            # Removing file falls back to defaults and evicts cache
            os.unlink(cfg_path)
            fallback = config_module.load_config()
            self.assertEqual(fallback, {'gateway_ip': 'envoy.local', 'use_https': True})

    def test_load_specs_caches_by_mtime_and_reloads_on_change(self):
        specs_path = os.path.join(self.tmpdir.name, 'system.yml')
        with open(specs_path, 'w') as f:
            f.write('system_overview:\n  capacity_dc: "8.0 kW DC"\n')

        with patch.object(config_module, 'SPECS_FILE', specs_path), \
             patch.object(config_module.yaml, 'safe_load', wraps=config_module.yaml.safe_load) as mock_load:
            s1 = config_module.load_specs()
            s2 = config_module.load_specs()
            self.assertEqual(s1['system_overview']['capacity_dc'], '8.0 kW DC')
            self.assertEqual(s2['system_overview']['capacity_dc'], '8.0 kW DC')
            self.assertEqual(mock_load.call_count, 1)

            with open(specs_path, 'w') as f:
                f.write('system_overview:\n  capacity_dc: "12.0 kW DC"\n')
            stat = os.stat(specs_path)
            os.utime(specs_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

            s3 = config_module.load_specs()
            self.assertEqual(s3['system_overview']['capacity_dc'], '12.0 kW DC')
            self.assertEqual(mock_load.call_count, 2)

            os.unlink(specs_path)
            self.assertEqual(config_module.load_specs(), {})


class GatewaySessionCachingTests(unittest.TestCase):
    def setUp(self):
        config_module.reset_caches()

    def tearDown(self):
        config_module.reset_caches()

    def test_reuses_single_gateway_and_authenticates_once(self):
        created = []

        class FakeGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()
                self.login_calls = []
                created.append(self)

            def login(self, token):
                self.login_calls.append(token)
                return True

        cfg = {
            'gateway_ip': '10.0.0.5',
            'use_https': True,
            'gateway_token': 'secret-jwt-1',
        }

        with patch.object(config_module, 'Gateway', FakeGateway):
            gw1 = config_module.get_authenticated_gateway(cfg)
            gw2 = config_module.get_authenticated_gateway(cfg)

            self.assertIs(gw1, gw2)
            self.assertIs(gw1.session, gw2.session)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].login_calls, ['secret-jwt-1'])

            # Changing token invalidates cache and builds a fresh gateway
            cfg_updated = dict(cfg, gateway_token='secret-jwt-2')
            gw3 = config_module.get_authenticated_gateway(cfg_updated)
            self.assertIsNot(gw1, gw3)
            self.assertEqual(len(created), 2)
            self.assertEqual(created[1].login_calls, ['secret-jwt-2'])

    def test_concurrent_gateway_initialization_authenticates_once(self):
        login_count = 0
        login_lock = threading.Lock()
        barrier = threading.Barrier(4)

        class SlowLoginGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()

            def login(self, token):
                nonlocal login_count
                time.sleep(0.05)
                with login_lock:
                    login_count += 1
                return True

        cfg = {'gateway_ip': '10.0.0.8', 'use_https': True, 'gateway_token': 'tok-concurrent'}
        results = []

        def worker():
            barrier.wait()
            results.append(config_module.get_authenticated_gateway(cfg))

        with patch.object(config_module, 'Gateway', SlowLoginGateway):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(results), 4)
        self.assertEqual(len({id(g) for g in results}), 1)
        self.assertEqual(login_count, 1)

    def test_explicit_timeout_enforced_on_login_and_api_call(self):
        recorded_timeouts = []

        class InspectTimeoutGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()

            def login(self, token):
                # Mirrors real Enphase-API passing Gateway.TIMEOUT (300)
                resp = self.session.post(
                    url=self.host + '/auth/check_jwt',
                    headers={'Authorization': f'Bearer {token}'},
                    timeout=self.TIMEOUT,
                )
                return resp.status_code == 200

        def fake_request(self_session, method, url, **kwargs):
            recorded_timeouts.append((url, kwargs.get('timeout')))
            if url.endswith('/auth/check_jwt'):
                return _make_response(200, text_body='<!DOCTYPE html><h2>Valid token.</h2>\n', url=url)
            return _make_response(200, json_body={'ok': True}, url=url)

        cfg = {
            'gateway_ip': 'envoy.local',
            'use_https': True,
            'gateway_token': 'tok-timeout',
            'gateway_timeout': 7.5,
        }

        with patch.object(config_module, 'Gateway', InspectTimeoutGateway), \
             patch.object(requests.Session, 'request', fake_request):
            gw = config_module.get_authenticated_gateway(cfg)
            data = gw.api_call('/ivp/meters/readings')

        self.assertEqual(data, {'ok': True})
        self.assertEqual(len(recorded_timeouts), 2)
        self.assertEqual(recorded_timeouts[0], ('https://envoy.local/auth/check_jwt', 7.5))
        self.assertEqual(recorded_timeouts[1], ('https://envoy.local/ivp/meters/readings', 7.5))

    def test_reauthenticates_and_retries_on_401_and_403(self):
        for status_code in (401, 403):
            with self.subTest(status_code=status_code):
                config_module.reset_caches()
                login_calls = []
                api_attempts = []

                class AuthRetryGateway:
                    TIMEOUT = 300
                    HEADERS = {'Accept': 'application/json'}

                    def __init__(self, host=None, cert_file=None):
                        self.host = host
                        self.session = requests.Session()

                    def login(self, token):
                        login_calls.append(token)
                        return True

                def fake_request(self_session, method, url, **kwargs):
                    api_attempts.append(url)
                    if len(api_attempts) == 1:
                        return _make_response(status_code, text_body='expired', url=url)
                    return _make_response(200, json_body=[{'eid': 1}], url=url)

                cfg = {
                    'gateway_ip': 'envoy.local',
                    'use_https': True,
                    'gateway_token': 'tok-refresh',
                }

                with patch.object(config_module, 'Gateway', AuthRetryGateway), \
                     patch.object(requests.Session, 'request', fake_request):
                    gw = config_module.get_authenticated_gateway(cfg)
                    result = gw.api_call('/ivp/meters/readings')

                self.assertEqual(result, [{'eid': 1}])
                # Initial login + 1 re-auth after 401/403
                self.assertEqual(len(login_calls), 2)
                self.assertEqual(len(api_attempts), 2)

    def test_concurrent_401_coalesces_reauthentication(self):
        login_calls = 0
        login_lock = threading.Lock()
        first_wave_barrier = threading.Barrier(4)
        request_counts = {}
        req_lock = threading.Lock()

        class CoalescingGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()

            def login(self, token):
                nonlocal login_calls
                time.sleep(0.03)
                with login_lock:
                    login_calls += 1
                return True

        def fake_request(self_session, method, url, **kwargs):
            tid = threading.get_ident()
            with req_lock:
                count = request_counts.get(tid, 0) + 1
                request_counts[tid] = count
            if count == 1:
                first_wave_barrier.wait()
                return _make_response(401, text_body='expired', url=url)
            return _make_response(200, json_body={'ok': True}, url=url)

        cfg = {'gateway_ip': 'envoy.local', 'use_https': True, 'gateway_token': 'tok-coalesce'}

        with patch.object(config_module, 'Gateway', CoalescingGateway), \
             patch.object(requests.Session, 'request', fake_request):
            gw = config_module.get_authenticated_gateway(cfg)
            self.assertEqual(login_calls, 1)

            outputs = []
            threads = [
                threading.Thread(target=lambda: outputs.append(gw.api_call('/ivp/meters/readings')))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(outputs), 4)
        self.assertTrue(all(o == {'ok': True} for o in outputs))
        # Initial login (1) + single coalesced re-auth (1) = 2 total (NOT 5!)
        self.assertEqual(login_calls, 2)

    def test_persistent_401_raises_http_error_after_single_retry(self):
        login_calls = []

        class AlwaysFailAuthGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()

            def login(self, token):
                login_calls.append(token)
                return True

        def fake_request(self_session, method, url, **kwargs):
            return _make_response(401, text_body='still unauthorized', url=url)

        cfg = {'gateway_ip': 'envoy.local', 'use_https': True, 'gateway_token': 'bad-token'}

        with patch.object(config_module, 'Gateway', AlwaysFailAuthGateway), \
             patch.object(requests.Session, 'request', fake_request):
            gw = config_module.get_authenticated_gateway(cfg)
            with self.assertRaises(requests.HTTPError):
                gw.api_call('/ivp/meters/readings')

        self.assertEqual(len(login_calls), 2)

    def test_ssl_verification_options_and_warning_suppression(self):
        class MinimalGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()

            def login(self, token):
                return True

        with patch.object(config_module, 'Gateway', MinimalGateway), \
             patch.object(urllib3, 'disable_warnings') as mock_disable:
            # Default: verify_ssl=False suppresses InsecureRequestWarning
            gw_insecure = config_module.get_authenticated_gateway({'gateway_ip': 'envoy.local'})
            self.assertFalse(gw_insecure.session.verify)
            mock_disable.assert_called_once_with(urllib3.exceptions.InsecureRequestWarning)

            # Explicit CA bundle or verify_ssl=True does NOT disable warnings
            mock_disable.reset_mock()
            gw_verified = config_module.get_authenticated_gateway({
                'gateway_ip': 'envoy.local',
                'verify_ssl': True,
            })
            self.assertTrue(gw_verified.session.verify)
            mock_disable.assert_not_called()

            gw_ca = config_module.get_authenticated_gateway({
                'gateway_ip': 'envoy.local',
                'ca_bundle': '/etc/ssl/certs/envoy-ca.pem',
            })
            self.assertEqual(gw_ca.session.verify, '/etc/ssl/certs/envoy-ca.pem')
            mock_disable.assert_not_called()

    def test_login_failure_falls_back_to_bearer_header(self):
        class FailingLoginGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()

            def login(self, token):
                raise RuntimeError('check_jwt failed')

        with patch.object(config_module, 'Gateway', FailingLoginGateway):
            gw = config_module.get_authenticated_gateway({
                'gateway_ip': 'envoy.local',
                'gateway_token': 'fallback-jwt',
            })
            self.assertEqual(gw.session.headers.get('Authorization'), 'Bearer fallback-jwt')

    def test_api_endpoints_reuse_cached_gateway_across_http_requests(self):
        app = create_test_app()
        client = app.test_client()
        login_calls = []

        class CountingGateway:
            TIMEOUT = 300
            HEADERS = {'Accept': 'application/json'}

            def __init__(self, host=None, cert_file=None):
                self.host = host
                self.session = requests.Session()

            def login(self, token):
                login_calls.append(token)
                return True

        def fake_request(self_session, method, url, **kwargs):
            if url.endswith('/ivp/meters/readings'):
                return _make_response(200, json_body=[{'eid': 1, 'activePower': 1200}], url=url)
            if url.endswith('/ivp/meters'):
                return _make_response(200, json_body=[{'eid': 1, 'measurementType': 'production', 'state': 'enabled'}], url=url)
            if url.endswith('/inventory.json'):
                return _make_response(200, json_body=[{'type': 'PCU', 'devices': []}], url=url)
            return _make_response(200, json_body={}, url=url)

        cfg = {'gateway_ip': 'envoy.local', 'use_https': True, 'gateway_token': 'shared-jwt'}
        try:
            with patch.object(config_module, 'Gateway', CountingGateway), \
                 patch('server.api.load_config', return_value=cfg), \
                 patch.object(requests.Session, 'request', fake_request):
                r1 = client.get('/api/meters')
                r2 = client.get('/api/meters')
                r3 = client.get('/api/inventory')

            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(len(login_calls), 1)
        finally:
            with app.app_context():
                db.session.remove()
                db.drop_all()
                db.engine.dispose()


class HistoryApiPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()
        self.client = self.app.test_client()
        self.tz_patch = patch(
            'server.api.get_target_timezone',
            return_value=ZoneInfo('America/New_York'),
        )
        self.tz_patch.start()
        self._seed_sample_daily_stats()

    def tearDown(self):
        self.tz_patch.stop()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def _seed_sample_daily_stats(self):
        rows = [
            # December 2025 (year boundary)
            DailyStats(date=date(2025, 12, 30), production_kwh=10.124, import_kwh=8.221, export_kwh=2.341, consumption_kwh=16.004),
            DailyStats(date=date(2025, 12, 31), production_kwh=12.452, import_kwh=6.112, export_kwh=3.223, consumption_kwh=15.342),
            # January 2026
            DailyStats(date=date(2026, 1, 5), production_kwh=15.5, import_kwh=4.0, export_kwh=5.5, consumption_kwh=14.0),
            DailyStats(date=date(2026, 1, 15), production_kwh=20.25, import_kwh=3.75, export_kwh=9.0, consumption_kwh=15.0),
            # February 2026
            DailyStats(date=date(2026, 2, 1), production_kwh=22.0, import_kwh=2.5, export_kwh=10.0, consumption_kwh=14.5),
            DailyStats(date=date(2026, 2, 10), production_kwh=25.0, import_kwh=1.5, export_kwh=12.0, consumption_kwh=14.5),
            DailyStats(date=date(2026, 2, 14), production_kwh=30.0, import_kwh=1.0, export_kwh=15.0, consumption_kwh=16.0),
        ]
        with self.app.app_context():
            db.session.add_all(rows)
            db.session.commit()

    def test_monthly_history_aggregates_in_sql_and_is_postgres_compatible(self):
        # 1. Verify PostgreSQL dialect compilation uses SQL EXTRACT + GROUP BY + SUM
        pg_sql = str(build_monthly_history_query().compile(dialect=postgresql.dialect())).upper()
        self.assertIn('GROUP BY', pg_sql)
        self.assertIn('EXTRACT(YEAR FROM', pg_sql)
        self.assertIn('EXTRACT(MONTH FROM', pg_sql)
        self.assertIn('SUM(', pg_sql)

        # 2. Capture actual SQL statements executed by GET /api/history/monthly
        executed_sql = []
        with self.app.app_context():
            def before_execute(conn, cursor, statement, parameters, context, executemany):
                executed_sql.append(statement)

            event.listen(db.engine, 'before_cursor_execute', before_execute)
            try:
                resp = self.client.get('/api/history/monthly')
            finally:
                event.remove(db.engine, 'before_cursor_execute', before_execute)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body, [
            {
                'month': '2025-12',
                'production_kwh': 22.58,
                'import_kwh': 14.33,
                'export_kwh': 5.56,
                'consumption_kwh': 31.35,
            },
            {
                'month': '2026-01',
                'production_kwh': 35.75,
                'import_kwh': 7.75,
                'export_kwh': 14.5,
                'consumption_kwh': 29.0,
            },
            {
                'month': '2026-02',
                'production_kwh': 77.0,
                'import_kwh': 5.0,
                'export_kwh': 37.0,
                'consumption_kwh': 45.0,
            },
        ])

        # Verify SQL GROUP BY and SUM were executed directly in the database
        self.assertEqual(len(executed_sql), 1)
        sql_upper = executed_sql[0].upper()
        self.assertIn('GROUP BY', sql_upper)
        self.assertIn('SUM(', sql_upper)

    def test_monthly_history_empty_table(self):
        with self.app.app_context():
            DailyStats.query.delete()
            db.session.commit()
        resp = self.client.get('/api/history/monthly')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_daily_history_date_range_filtering(self):
        # Unfiltered returns all 7 rows ascending
        resp_all = self.client.get('/api/history/daily')
        self.assertEqual(resp_all.status_code, 200)
        dates_all = [r['date'] for r in resp_all.get_json()]
        self.assertEqual(len(dates_all), 7)
        self.assertEqual(dates_all, sorted(dates_all))

        # ?start= only
        resp_start = self.client.get('/api/history/daily?start=2026-02-01')
        self.assertEqual(resp_start.status_code, 200)
        self.assertEqual(
            [r['date'] for r in resp_start.get_json()],
            ['2026-02-01', '2026-02-10', '2026-02-14'],
        )

        # ?end= only
        resp_end = self.client.get('/api/history/daily?end=2025-12-31')
        self.assertEqual(resp_end.status_code, 200)
        self.assertEqual(
            [r['date'] for r in resp_end.get_json()],
            ['2025-12-30', '2025-12-31'],
        )

        # ?start=&end=
        resp_window = self.client.get('/api/history/daily?start=2026-01-01&end=2026-02-05')
        self.assertEqual(resp_window.status_code, 200)
        self.assertEqual(
            [r['date'] for r in resp_window.get_json()],
            ['2026-01-05', '2026-01-15', '2026-02-01'],
        )

        # ?days= combined with ?end= (inclusive 5-day window ending 2026-02-14 => 2026-02-10..2026-02-14)
        resp_days_end = self.client.get('/api/history/daily?days=5&end=2026-02-14')
        self.assertEqual(resp_days_end.status_code, 200)
        self.assertEqual(
            [r['date'] for r in resp_days_end.get_json()],
            ['2026-02-10', '2026-02-14'],
        )

        # ?days= combined with ?start= (inclusive 11-day window starting 2026-01-05 => 2026-01-05..2026-01-15)
        resp_days_start = self.client.get('/api/history/daily?days=11&start=2026-01-05')
        self.assertEqual(resp_days_start.status_code, 200)
        self.assertEqual(
            [r['date'] for r in resp_days_start.get_json()],
            ['2026-01-05', '2026-01-15'],
        )

    def test_daily_history_days_param_uses_local_today(self):
        tz = ZoneInfo('America/New_York')
        today_local = datetime.now(timezone.utc).astimezone(tz).date()
        with self.app.app_context():
            db.session.add_all([
                DailyStats(date=today_local - timedelta(days=20), production_kwh=5.0, import_kwh=2.0, export_kwh=1.0, consumption_kwh=6.0),
                DailyStats(date=today_local - timedelta(days=5), production_kwh=10.0, import_kwh=2.0, export_kwh=3.0, consumption_kwh=9.0),
                DailyStats(date=today_local, production_kwh=12.0, import_kwh=1.0, export_kwh=4.0, consumption_kwh=9.0),
            ])
            db.session.commit()

        resp = self.client.get('/api/history/daily?days=7')
        self.assertEqual(resp.status_code, 200)
        returned_dates = [r['date'] for r in resp.get_json()]
        self.assertEqual(
            returned_dates,
            [(today_local - timedelta(days=5)).isoformat(), today_local.isoformat()],
        )

    def test_daily_history_rejects_invalid_query_params(self):
        bad_queries = [
            '/api/history/daily?days=0',
            '/api/history/daily?days=-3',
            '/api/history/daily?days=abc',
            '/api/history/daily?start=not-a-date',
            '/api/history/daily?end=2026-99-99',
            '/api/history/daily?start=2026-02-15&end=2026-02-01',
            '/api/history/daily?days=7&start=2026-01-01&end=2026-01-31',
        ]
        for url in bad_queries:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 400)
                self.assertIn('error', resp.get_json())


class DeploymentAndFrontendPollingTests(unittest.TestCase):
    def test_dockerfile_enables_gunicorn_threads_and_timeout(self):
        dockerfile_path = os.path.join(PROJECT_ROOT, 'Dockerfile')
        with open(dockerfile_path, 'r') as f:
            content = f.read()

        match = re.search(r'^CMD\s+(\[.*\])$', content, flags=re.MULTILINE)
        self.assertIsNotNone(match, 'Dockerfile must define an exec-form CMD')
        cmd = json.loads(match.group(1))
        self.assertEqual(cmd[0], 'gunicorn')

        def _arg_value(flag):
            self.assertIn(flag, cmd, f'Dockerfile Gunicorn CMD missing {flag}')
            idx = cmd.index(flag)
            return cmd[idx + 1]

        self.assertEqual(_arg_value('--workers'), '1')
        self.assertGreaterEqual(int(_arg_value('--threads')), 2)
        self.assertGreater(int(_arg_value('--timeout')), 0)

    def test_dashboard_js_live_polling_behavior_via_node(self):
        node_bin = shutil.which('node')
        if not node_bin:
            self.skipTest('node not available in PATH')
        js_test = os.path.join(PROJECT_ROOT, 'tests', 'test_dashboard_polling.js')
        proc = subprocess.run(
            [node_bin, '--test', js_test],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f'Node dashboard polling tests failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}',
        )


if __name__ == '__main__':
    unittest.main()

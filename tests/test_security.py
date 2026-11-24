import ast
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

import requests
import yaml
from flask import Flask
from flask_apscheduler import APScheduler
from werkzeug.middleware.proxy_fix import ProxyFix

# Allow running tests even in minimal environments without Enphase-API installed
if 'enphase_api' not in sys.modules:
    _enphase_pkg = types.ModuleType('enphase_api')
    _enphase_local = types.ModuleType('enphase_api.local')
    _enphase_gw = types.ModuleType('enphase_api.local.gateway')
    _enphase_gw.Gateway = MagicMock
    sys.modules['enphase_api'] = _enphase_pkg
    sys.modules['enphase_api.local'] = _enphase_local
    sys.modules['enphase_api.local.gateway'] = _enphase_gw

import server.api as api_module
import server.trmnl_service as trmnl_service
from server.api import MAX_TRMNL_LOG_BYTES, api_bp, reset_refresh_cooldown
from server.db import db

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
    app.config['SCHEDULER_API_ENABLED'] = False
    db.init_app(app)
    app.register_blueprint(api_bp)
    with app.app_context():
        db.create_all()
    return app


class SchedulerAndCredentialsConfigTests(unittest.TestCase):
    def test_app_disables_scheduler_api(self):
        app_path = os.path.join(PROJECT_ROOT, 'server', 'app.py')
        with open(app_path, 'r') as f:
            tree = ast.parse(f.read(), filename=app_path)

        found_setting = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == 'SCHEDULER_API_ENABLED'
                    ):
                        if isinstance(node.value, ast.Constant):
                            found_setting = node.value.value

        self.assertIs(found_setting, False, 'SCHEDULER_API_ENABLED must be explicitly set to False in server/app.py')

        # Verify Flask-APScheduler does not expose management routes when disabled
        test_app = create_test_app()
        scheduler = APScheduler()
        scheduler.init_app(test_app)
        client = test_app.test_client()
        self.assertEqual(client.get('/scheduler').status_code, 404)
        self.assertEqual(client.get('/scheduler/jobs').status_code, 404)
        with test_app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def test_docker_compose_has_no_hardcoded_db_password_and_env_example_exists(self):
        compose_path = os.path.join(PROJECT_ROOT, 'docker-compose.yml')
        env_example_path = os.path.join(PROJECT_ROOT, '.env.example')

        with open(compose_path, 'r') as f:
            compose_raw = f.read()

        self.assertNotIn('secretpassword', compose_raw)
        self.assertIn('${POSTGRES_PASSWORD', compose_raw)

        compose_data = yaml.safe_load(compose_raw)
        app_env = compose_data['services']['enphase-dashboard']['environment']
        db_env = compose_data['services']['db']['environment']
        self.assertTrue(any('DATABASE_URL=' in e and '${POSTGRES_PASSWORD' in e for e in app_env))
        self.assertIn('${POSTGRES_PASSWORD', str(db_env.get('POSTGRES_PASSWORD', '')))

        self.assertTrue(os.path.exists(env_example_path), '.env.example must exist')
        with open(env_example_path, 'r') as f:
            env_example = f.read()
        for key in ('POSTGRES_DB=', 'POSTGRES_USER=', 'POSTGRES_PASSWORD=', 'TRUST_PROXY_COUNT='):
            self.assertIn(key, env_example)


class TrmnlAuthAndLimitsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()
        self.client = self.app.test_client()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.png_path = os.path.join(self.tmpdir.name, 'trmnl_display.png')
        self.bmp_path = os.path.join(self.tmpdir.name, 'trmnl_display.bmp')
        self.telemetry_path = os.path.join(self.tmpdir.name, 'trmnl_telemetry.json')

        # Pre-render valid monochrome PNG/BMP files for image endpoint tests
        trmnl_service.render_trmnl_solar(
            {'title': 'TEST', 'production': 1.0, 'consumption': 1.0, 'export': 0.0, 'import': 0.0, 'coverage': 100.0},
            output_png_path=self.png_path,
            output_bmp_path=self.bmp_path,
        )

    def tearDown(self):
        self.tmpdir.cleanup()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def _patch_trmnl(self, cfg):
        return (
            patch('server.api.load_config', return_value=cfg),
            patch.object(trmnl_service, 'OUTPUT_IMAGE_PATH', self.png_path),
            patch.object(trmnl_service, 'OUTPUT_BMP_PATH', self.bmp_path),
            patch.object(trmnl_service, 'TELEMETRY_PATH', self.telemetry_path),
            patch.object(trmnl_service, 'LEGACY_TELEMETRY_PATH', ''),
        )

    def test_trmnl_endpoints_reject_missing_or_wrong_access_token(self):
        cfg = {'trmnl': {'api_key': 'super-secret-key', 'refresh_rate_seconds': 600}}
        p_cfg, p_png, p_bmp, p_tel, p_leg = self._patch_trmnl(cfg)

        with p_cfg, p_png, p_bmp, p_tel, p_leg, patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TRMNL_API_KEY', None)
            for headers in ({}, {'Access-Token': 'wrong-key'}, {'Access-Token': ''}):
                self.assertEqual(self.client.get('/api/display', headers=headers).status_code, 401)
                self.assertEqual(self.client.get('/api/display/current.bmp', headers=headers).status_code, 401)
                self.assertEqual(self.client.get('/api/display/current.png', headers=headers).status_code, 401)
                self.assertEqual(
                    self.client.post('/api/log', headers=headers, json={'msg': 'hi'}).status_code,
                    401,
                )

            # Telemetry file must not have been created by rejected check-ins/logs
            self.assertFalse(os.path.exists(self.telemetry_path))

    def test_trmnl_endpoints_fail_closed_when_api_key_not_configured(self):
        cfg = {'trmnl': {}}
        p_cfg, p_png, p_bmp, p_tel, p_leg = self._patch_trmnl(cfg)
        with p_cfg, p_png, p_bmp, p_tel, p_leg:
            os.environ.pop('TRMNL_API_KEY', None)
            self.assertEqual(
                self.client.get('/api/display', headers={'Access-Token': 'local-trmnl-token'}).status_code,
                401,
            )

    def test_trmnl_endpoints_accept_valid_access_token_and_signed_image_url(self):
        cfg = {'trmnl': {'api_key': 'super-secret-key', 'refresh_rate_seconds': 600}}
        p_cfg, p_png, p_bmp, p_tel, p_leg = self._patch_trmnl(cfg)

        with p_cfg, p_png, p_bmp, p_tel, p_leg:
            headers = {'Access-Token': 'super-secret-key', 'ID': 'AA:BB:CC:DD:EE:FF'}
            display_resp = self.client.get('/api/display', headers=headers)
            self.assertEqual(display_resp.status_code, 200)
            payload = display_resp.get_json()

            # Signed image URL must NOT contain the raw api_key
            image_url = payload['image_url']
            self.assertNotIn('super-secret-key', image_url)
            parsed = urlparse(image_url)
            self.assertEqual(parsed.path, '/api/display/current.bmp')
            qs = parse_qs(parsed.query)
            self.assertIn('exp', qs)
            self.assertIn('sig', qs)

            # Device fetching signed image_url without Access-Token header succeeds
            signed_bmp = self.client.get(f"{parsed.path}?{parsed.query}")
            self.assertEqual(signed_bmp.status_code, 200)
            signed_bmp.close()

            # Tampered signature is rejected
            bad_sig = self.client.get(f"{parsed.path}?exp={qs['exp'][0]}&sig=deadbeef")
            self.assertEqual(bad_sig.status_code, 401)

            # Expired signature is rejected
            expired_exp = int(time.time()) - 10
            expired_sig = api_module._compute_image_signature('super-secret-key', '/api/display/current.bmp', expired_exp)
            expired_resp = self.client.get(f"/api/display/current.bmp?exp={expired_exp}&sig={expired_sig}")
            self.assertEqual(expired_resp.status_code, 401)

            # Direct Access-Token access to BMP, PNG, and POST /api/log succeeds
            bmp_resp = self.client.get('/api/display/current.bmp', headers=headers)
            self.assertEqual(bmp_resp.status_code, 200)
            bmp_resp.close()

            png_resp = self.client.get('/api/display/current.png', headers=headers)
            self.assertEqual(png_resp.status_code, 200)
            png_resp.close()

            log_resp = self.client.post('/api/log', headers=headers, json={'error': 'wifi timeout'})
            self.assertEqual(log_resp.status_code, 200)

            # POST /api/log must NOT accept signed query params without Access-Token
            log_query_resp = self.client.post(f"/api/log?{parsed.query}", json={'error': 'bypass'})
            self.assertEqual(log_query_resp.status_code, 401)

    def test_trmnl_setup_gated_by_setup_enabled_and_mac_allowlist(self):
        # 1. Default (setup_enabled not set / False): unauthenticated caller gets 403, key not leaked
        cfg_closed = {'trmnl': {'api_key': 'secret-setup-key'}}
        with patch('server.api.load_config', return_value=cfg_closed):
            resp = self.client.get('/api/setup')
            self.assertEqual(resp.status_code, 403)
            self.assertNotIn('secret-setup-key', resp.get_data(as_text=True))

            # Authenticated caller with Access-Token is still allowed
            auth_resp = self.client.get('/api/setup', headers={'Access-Token': 'secret-setup-key'})
            self.assertEqual(auth_resp.status_code, 200)
            self.assertEqual(auth_resp.get_json()['api_key'], 'secret-setup-key')

        # 2. setup_enabled=True with allowed_macs enforces MAC allowlist
        cfg_mac = {
            'trmnl': {
                'api_key': 'secret-setup-key',
                'setup_enabled': True,
                'allowed_macs': ['AA:BB:CC:DD:EE:FF'],
            }
        }
        with patch('server.api.load_config', return_value=cfg_mac):
            self.assertEqual(self.client.get('/api/setup').status_code, 403)
            self.assertEqual(self.client.get('/api/setup', headers={'ID': '11:22:33:44:55:66'}).status_code, 403)
            ok_resp = self.client.get('/api/setup', headers={'ID': 'aa:bb:cc:dd:ee:ff'})
            self.assertEqual(ok_resp.status_code, 200)
            self.assertEqual(ok_resp.get_json()['api_key'], 'secret-setup-key')

    def test_trmnl_log_enforces_payload_size_limit(self):
        cfg = {'trmnl': {'api_key': 'log-secret'}}
        p_cfg, p_png, p_bmp, p_tel, p_leg = self._patch_trmnl(cfg)

        with p_cfg, p_png, p_bmp, p_tel, p_leg:
            headers = {'Access-Token': 'log-secret', 'ID': 'AA:BB:CC'}
            oversized = 'x' * (MAX_TRMNL_LOG_BYTES + 1)
            resp = self.client.post('/api/log', headers=headers, data=oversized, content_type='text/plain')
            self.assertEqual(resp.status_code, 413)
            self.assertFalse(os.path.exists(self.telemetry_path))

            small_resp = self.client.post('/api/log', headers=headers, json={'msg': 'ok'})
            self.assertEqual(small_resp.status_code, 200)
            self.assertTrue(os.path.exists(self.telemetry_path))


class ErrorSanitizationProxyAndRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()
        self.client = self.app.test_client()
        reset_refresh_cooldown()

    def tearDown(self):
        reset_refresh_cooldown()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def test_gateway_errors_return_sanitized_502_and_504_without_leaking_details(self):
        fake_gateway = MagicMock()
        sensitive_secret = 'Bearer SUPER_SECRET_JWT_12345 at 192.168.50.99'

        # Timeout -> 504
        fake_gateway.api_call.side_effect = requests.exceptions.ConnectTimeout(sensitive_secret)
        with patch('server.api.load_config', return_value={'gateway_ip': '192.168.50.99'}), \
             patch('server.api.get_authenticated_gateway', return_value=fake_gateway), \
             patch.object(api_module.logger, 'exception') as mock_log:
            resp = self.client.get('/api/meters')
            self.assertEqual(resp.status_code, 504)
            body = resp.get_json()
            self.assertNotIn(sensitive_secret, json.dumps(body))
            self.assertIn('timed out', body['error'].lower())
            mock_log.assert_called_once()

        # ConnectionError -> 502
        fake_gateway.api_call.side_effect = requests.exceptions.ConnectionError(sensitive_secret)
        with patch('server.api.load_config', return_value={'gateway_ip': '192.168.50.99'}), \
             patch('server.api.get_authenticated_gateway', return_value=fake_gateway), \
             patch.object(api_module.logger, 'exception') as mock_log:
            resp = self.client.get('/api/events')
            self.assertEqual(resp.status_code, 502)
            body = resp.get_json()
            self.assertNotIn(sensitive_secret, json.dumps(body))
            mock_log.assert_called_once()

        # Upstream HTTP 401 -> 502
        err_resp = requests.Response()
        err_resp.status_code = 401
        fake_gateway.api_call.side_effect = requests.exceptions.HTTPError(sensitive_secret, response=err_resp)
        with patch('server.api.load_config', return_value={'gateway_ip': '192.168.50.99'}), \
             patch('server.api.get_authenticated_gateway', return_value=fake_gateway), \
             patch.object(api_module.logger, 'exception'):
            resp = self.client.get('/api/inventory')
            self.assertEqual(resp.status_code, 502)
            self.assertNotIn(sensitive_secret, json.dumps(resp.get_json()))

        # Invalid JSON from gateway -> 502
        fake_gateway.api_call.side_effect = None
        fake_gateway.api_call.return_value = '<html>not json</html>'
        with patch('server.api.load_config', return_value={'gateway_ip': '192.168.50.99'}), \
             patch('server.api.get_authenticated_gateway', return_value=fake_gateway), \
             patch.object(api_module.logger, 'exception'):
            resp = self.client.get('/api/connectivity')
            self.assertEqual(resp.status_code, 502)
            self.assertNotIn('<html>', json.dumps(resp.get_json()))

    def test_internal_errors_do_not_leak_exception_text(self):
        secret_db_err = RuntimeError('postgresql://postgres:TOP_SECRET_PW@db:5432/enphase connection refused')
        with patch('server.api.get_today_summary', side_effect=secret_db_err), \
             patch.object(api_module.logger, 'exception') as mock_log:
            resp = self.client.get('/api/history/today')
            self.assertEqual(resp.status_code, 500)
            body = resp.get_json()
            self.assertEqual(body, {'error': 'An internal server error occurred.'})
            self.assertNotIn('TOP_SECRET_PW', json.dumps(body))
            mock_log.assert_called_once()

    def test_forwarded_headers_ignored_by_default_and_honored_only_with_proxy_fix(self):
        cfg = {'trmnl': {'api_key': 'proxy-test-key', 'setup_enabled': True}}

        # Without ProxyFix: spoofed X-Forwarded-* headers must be ignored
        with patch('server.api.load_config', return_value=cfg):
            resp = self.client.get(
                '/api/setup',
                headers={
                    'X-Forwarded-Proto': 'https',
                    'X-Forwarded-Host': 'attacker.evil',
                },
            )
            self.assertEqual(resp.status_code, 200)
            image_url = resp.get_json()['image_url']
            self.assertNotIn('attacker.evil', image_url)
            self.assertTrue(image_url.startswith('http://localhost/api/display/current.bmp'))

        # With ProxyFix enabled (1 trusted proxy): X-Forwarded-* headers are safely applied
        proxied_app = create_test_app()
        proxied_app.wsgi_app = ProxyFix(proxied_app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
        proxied_client = proxied_app.test_client()
        try:
            with patch('server.api.load_config', return_value=cfg):
                resp = proxied_client.get(
                    '/api/setup',
                    headers={
                        'X-Forwarded-Proto': 'https',
                        'X-Forwarded-Host': 'solar.example.com',
                    },
                )
                self.assertEqual(resp.status_code, 200)
                image_url = resp.get_json()['image_url']
                self.assertTrue(image_url.startswith('https://solar.example.com/api/display/current.bmp'))
        finally:
            with proxied_app.app_context():
                db.session.remove()
                db.drop_all()
                db.engine.dispose()

    def test_history_refresh_cooldown_and_concurrent_rate_limiting(self):
        fake_now = [1000.0]

        with patch('server.api.load_config', return_value={'history_refresh_cooldown_seconds': 30}), \
             patch('server.api.time.monotonic', side_effect=lambda: fake_now[0]), \
             patch('server.dashboard_services.record_reading') as mock_record:
            # First call succeeds
            r1 = self.client.post('/api/history/refresh')
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(mock_record.call_count, 1)

            # Immediate retry within cooldown returns 429 with Retry-After
            fake_now[0] += 5.0
            r2 = self.client.post('/api/history/refresh')
            self.assertEqual(r2.status_code, 429)
            self.assertIn('Retry-After', r2.headers)
            self.assertGreaterEqual(int(r2.headers['Retry-After']), 25)
            self.assertEqual(mock_record.call_count, 1)

            # After cooldown passes, refresh succeeds again
            fake_now[0] += 30.0
            r3 = self.client.post('/api/history/refresh')
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(mock_record.call_count, 2)

    def test_history_refresh_rejects_overlapping_concurrent_requests(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_record_reading(_app):
            entered.set()
            release.wait(timeout=2.0)

        results = []

        def run_first():
            with self.app.test_client() as c:
                results.append(c.post('/api/history/refresh').status_code)

        with patch('server.api.load_config', return_value={'history_refresh_cooldown_seconds': 0}), \
             patch('server.dashboard_services.record_reading', side_effect=slow_record_reading):
            t1 = threading.Thread(target=run_first)
            t1.start()
            self.assertTrue(entered.wait(timeout=2.0))

            # Second request while first is in progress returns 429 immediately
            r2 = self.client.post('/api/history/refresh')
            self.assertEqual(r2.status_code, 429)

            release.set()
            t1.join(timeout=2.0)

        self.assertEqual(results, [200])


if __name__ == '__main__':
    unittest.main()

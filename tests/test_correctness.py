import os
import sys
import types
import json
import tempfile
import threading
import unittest
from datetime import datetime, date, timedelta, timezone
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from flask import Flask
from PIL import Image

# Allow testing server.api even in minimal environments without Enphase-API installed
if 'enphase_api' not in sys.modules:
    _enphase_pkg = types.ModuleType('enphase_api')
    _enphase_local = types.ModuleType('enphase_api.local')
    _enphase_gw = types.ModuleType('enphase_api.local.gateway')
    _enphase_gw.Gateway = MagicMock
    sys.modules['enphase_api'] = _enphase_pkg
    sys.modules['enphase_api.local'] = _enphase_local
    sys.modules['enphase_api.local.gateway'] = _enphase_gw

from server.db import db, MeterReading, DailyStats
from server.api import api_bp
from server.dashboard_services import (
    aggregate_day,
    interpolate_gap,
    record_reading,
    get_local_day_utc_bounds,
    compute_daily_metrics,
    get_today_summary,
    annotate_meter_readings,
    select_production_and_grid_meters,
    _meter_metadata_cache,
)
import server.trmnl_service as trmnl_service


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


class EnergyAccountingTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()
        self.tz_patch = patch(
            'server.dashboard_services.get_target_timezone',
            return_value=ZoneInfo('America/New_York'),
        )
        self.tz_patch.start()

    def tearDown(self):
        self.tz_patch.stop()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def _add_reading(self, local_dt_str, prod_wh, imp_wh, exp_wh, tz_name='America/New_York'):
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

    def test_normal_day_and_same_day_outage_are_exact(self):
        with self.app.app_context():
            # Previous day baseline at 23:45 and normal midnight poll at 00:00 (15m interval)
            self._add_reading('2026-06-09T23:45:00', 100000, 50000, 20000)
            self._add_reading('2026-06-10T00:00:00', 100000, 50200, 20000)
            # Normal morning reading
            self._add_reading('2026-06-10T10:00:00', 105000, 52000, 21000)
            # Same-day 4-hour outage (10:00 -> 14:00) still belongs 100% to June 10
            self._add_reading('2026-06-10T14:00:00', 118000, 53000, 29000)

            aggregate_day(date(2026, 6, 10))
            stats = db.session.get(DailyStats, date(2026, 6, 10))
            self.assertIsNotNone(stats)
            self.assertFalse(stats.is_interpolated)
            self.assertAlmostEqual(stats.production_kwh, 18.0, places=5)
            self.assertAlmostEqual(stats.import_kwh, 3.0, places=5)
            self.assertAlmostEqual(stats.export_kwh, 9.0, places=5)
            self.assertAlmostEqual(stats.consumption_kwh, 12.0, places=5)

    def test_multiday_outage_preserves_start_day_and_prevents_recovery_double_count(self):
        with self.app.app_context():
            # Day 0 baseline at 23:45 and normal 00:00 reading on Day 1
            self._add_reading('2026-06-09T23:45:00', 100000, 50000, 20000)
            self._add_reading('2026-06-10T00:00:00', 100000, 50500, 20000)
            # Day 1 pre-outage accumulation (00:00 -> 14:00): +20 kWh prod, +4 kWh total imp, +10 kWh exp
            r_pre_outage = self._add_reading('2026-06-10T14:00:00', 120000, 54000, 30000)
            aggregate_day(date(2026, 6, 10))

            # Outage from June 10 14:00 to June 13 10:00 (68 hours total: 10h + 24h + 24h + 10h)
            # Gap delta: +68 kWh prod, +34 kWh imp, +17 kWh exp
            r_recovery = self._add_reading('2026-06-13T10:00:00', 188000, 88000, 47000)
            interpolate_gap(r_pre_outage, r_recovery)

            # Next normal reading 15 min after recovery (+1.5 kWh prod, +0.5 kWh imp, +0.5 kWh exp)
            self._add_reading('2026-06-13T10:15:00', 189500, 88500, 47500)
            aggregate_day(date(2026, 6, 13))

            d10 = db.session.get(DailyStats, date(2026, 6, 10))
            d11 = db.session.get(DailyStats, date(2026, 6, 11))
            d12 = db.session.get(DailyStats, date(2026, 6, 12))
            d13 = db.session.get(DailyStats, date(2026, 6, 13))

            # All 4 days spanned by the outage should be flagged interpolated
            for d in (d10, d11, d12, d13):
                self.assertIsNotNone(d)
                self.assertTrue(d.is_interpolated)

            # Day 10: 20 kWh pre-outage + 10/68 * 68 kWh = 30.0 kWh
            self.assertAlmostEqual(d10.production_kwh, 30.0, places=5)
            # Day 11 & Day 12: 24/68 * 68 kWh = 24.0 kWh each
            self.assertAlmostEqual(d11.production_kwh, 24.0, places=5)
            self.assertAlmostEqual(d12.production_kwh, 24.0, places=5)
            # Day 13: 10/68 * 68 kWh (10.0) + 1.5 kWh post-recovery = 11.5 kWh (NOT 89.5 kWh!)
            self.assertAlmostEqual(d13.production_kwh, 11.5, places=5)

            # Exact conservation of energy across all 4 days
            total_prod = sum(d.production_kwh for d in (d10, d11, d12, d13))
            total_imp = sum(d.import_kwh for d in (d10, d11, d12, d13))
            total_exp = sum(d.export_kwh for d in (d10, d11, d12, d13))
            self.assertAlmostEqual(total_prod, 89.5, places=5)
            self.assertAlmostEqual(total_imp, 38.5, places=5)
            self.assertAlmostEqual(total_exp, 27.5, places=5)

    def test_dst_day_boundaries(self):
        tz = ZoneInfo('America/New_York')
        # 2026-03-08 is spring-forward (23 hours)
        s1, e1 = get_local_day_utc_bounds(date(2026, 3, 8), tz)
        self.assertEqual((e1 - s1).total_seconds(), 23 * 3600)
        # 2026-11-01 is fall-back (25 hours)
        s2, e2 = get_local_day_utc_bounds(date(2026, 11, 1), tz)
        self.assertEqual((e2 - s2).total_seconds(), 25 * 3600)


class MetricsAndTrmnlTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tz_patch = patch(
            'server.dashboard_services.get_target_timezone',
            return_value=ZoneInfo('America/New_York'),
        )
        self.tz_patch.start()

    def tearDown(self):
        self.tz_patch.stop()
        self.tmpdir.cleanup()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def test_canonical_coverage_and_net_grid_and_midnight_rollover(self):
        with self.app.app_context():
            # Yesterday's DailyStats only (no row for today 2026-06-11 yet)
            db.session.add(DailyStats(
                date=date(2026, 6, 10),
                production_kwh=40.0,
                export_kwh=25.0,
                import_kwh=5.0,
                consumption_kwh=20.0,
                is_interpolated=False,
            ))
            # Recent reading at 2026-06-11 00:05 local (= 04:05 UTC)
            now_utc = datetime(2026, 6, 11, 4, 10, tzinfo=timezone.utc)
            db.session.add(MeterReading(
                timestamp=datetime(2026, 6, 11, 4, 5),
                production_wh=1000,
                import_wh=500,
                export_wh=200,
            ))
            db.session.commit()

            # Must NOT return June 10's totals for June 11
            today_summary = get_today_summary(now_utc=now_utc)
            self.assertEqual(today_summary['date'], '2026-06-11')
            self.assertFalse(today_summary['has_data'])
            self.assertEqual(today_summary['production_kwh'], 0.0)
            self.assertFalse(today_summary['is_stale'])

            # Now add today's DailyStats: prod=30, exp=20, imp=10 => direct_use=10, cons=20
            # Direct solar coverage = 10/20 = 50.0% (NOT 30/20 = 150%)
            # Net grid = export - import = +10.0 kWh (export)
            today_stats = DailyStats(
                date=date(2026, 6, 11),
                production_kwh=30.0,
                export_kwh=20.0,
                import_kwh=10.0,
                consumption_kwh=20.0,
                is_interpolated=False,
            )
            db.session.add(today_stats)
            db.session.commit()

            today_summary = get_today_summary(now_utc=now_utc)
            self.assertTrue(today_summary['has_data'])
            self.assertEqual(today_summary['solar_coverage_pct'], 50.0)
            self.assertEqual(today_summary['net_grid_kwh'], 10.0)
            self.assertTrue(today_summary['is_net_export'])

            # Net import case (export < import)
            net_import_stats = DailyStats(
                date=date(2026, 6, 11),
                production_kwh=5.0,
                export_kwh=1.0,
                import_kwh=16.0,
                consumption_kwh=20.0,
                is_interpolated=False,
            )
            metrics = compute_daily_metrics(net_import_stats, date(2026, 6, 11), now_utc=now_utc)
            self.assertEqual(metrics['net_grid_kwh'], -15.0)
            self.assertFalse(metrics['is_net_export'])
            self.assertEqual(metrics['solar_coverage_pct'], 20.0)
            self.assertTrue(metrics['is_stale'])  # no latest_reading passed

    def test_trmnl_atomic_render_and_thread_safe_telemetry(self):
        png_path = os.path.join(self.tmpdir.name, 'trmnl_display.png')
        bmp_path = os.path.join(self.tmpdir.name, 'trmnl_display.bmp')
        telemetry_path = os.path.join(self.tmpdir.name, 'trmnl_telemetry.json')

        # Ensure default telemetry path is NOT inside static/
        self.assertNotIn(os.path.sep + 'static' + os.path.sep, trmnl_service.TELEMETRY_PATH)

        data = {
            'title': 'DAILY SOLAR STATUS',
            'production': 12.4,
            'consumption': 18.2,
            'export': 3.1,
            'import': 8.9,
            'net_grid_str': '-5.8',
            'net_grid_unit': 'kWh Import',
            'is_export': False,
            'coverage': 51.1,
            'last_updated': 'Jun 11, 02:15 PM',
            'is_stale': True,
            'status_label': 'STALE',
        }
        trmnl_service.render_trmnl_solar(data, output_png_path=png_path, output_bmp_path=bmp_path)

        with Image.open(png_path) as im:
            self.assertEqual(im.size, (800, 480))
            self.assertEqual(im.mode, '1')
        with Image.open(bmp_path) as im:
            self.assertEqual(im.size, (800, 480))
            self.assertEqual(im.mode, '1')

        # No leftover temporary files
        leftovers = [f for f in os.listdir(self.tmpdir.name) if f.startswith('.tmp_trmnl_')]
        self.assertEqual(leftovers, [])

        # Concurrent telemetry updates remain valid JSON
        with patch.object(trmnl_service, 'TELEMETRY_PATH', telemetry_path), \
             patch.object(trmnl_service, 'LEGACY_TELEMETRY_PATH', ''):
            threads = []
            for i in range(10):
                t1 = threading.Thread(
                    target=trmnl_service.record_trmnl_checkin,
                    args=({'ID': 'AA:BB:CC', 'Battery-Voltage': '3.95', 'RSSI': '-58'},),
                )
                t2 = threading.Thread(
                    target=trmnl_service.record_trmnl_log_message,
                    args=({'ID': 'AA:BB:CC'}, f'log-{i}'),
                )
                threads.extend([t1, t2])
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            with open(telemetry_path, 'r') as f:
                saved = json.load(f)
            self.assertEqual(saved['mac_id'], 'AA:BB:CC')
            self.assertEqual(len(saved['logs']), 10)


class HardwarePortabilityAndSpecsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def test_meter_selection_uses_metadata_not_positions_or_hardcoded_eid(self):
        # Deliberately reversed order + extra disabled meter
        readings = [
            {'eid': 9999999, 'activePower': 500, 'actEnergyDlvd': 2000, 'actEnergyRcvd': 1000},
            {'eid': 1111111, 'activePower': 123, 'actEnergyDlvd': 0, 'actEnergyRcvd': 0},
            {'eid': 2222222, 'activePower': 3000, 'actEnergyDlvd': 8000, 'actEnergyRcvd': 0},
        ]
        metadata = {
            9999999: {'measurementType': 'net-consumption', 'state': 'enabled'},
            1111111: {'measurementType': 'total-consumption', 'state': 'disabled'},
            2222222: {'measurementType': 'production', 'state': 'enabled'},
        }
        annotated = annotate_meter_readings(readings, metadata)
        prod, grid = select_production_and_grid_meters(annotated)
        self.assertEqual(prod['eid'], 2222222)
        self.assertEqual(grid['eid'], 9999999)

    def test_system_specs_template_handles_missing_and_custom_specs(self):
        with self.app.test_request_context('/'):
            from flask import render_template

            # Missing system.yml -> empty dict should render cleanly without UndefinedError
            html_empty = render_template('tabs/system_specs.html', specs={})
            self.assertIn('No system specifications configured yet', html_empty)

            # Custom non-Hyundai panel specs should use panel_model dynamically
            custom_specs = {
                'system_overview': {
                    'capacity_dc': '10.0 kW DC',
                    'panel_model': 'REC Alpha Pure 420',
                },
                'panel_specs': {
                    'electrical': [{'label': 'Peak Power', 'value': '420 W'}],
                },
            }
            html_custom = render_template('tabs/system_specs.html', specs=custom_specs)
            self.assertIn('REC Alpha Pure 420 Technical Stats', html_custom)
            self.assertNotIn('Hyundai', html_custom)


class ApiAndRecordReadingTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()
        self.client = self.app.test_client()
        self.tmpdir = tempfile.TemporaryDirectory()
        _meter_metadata_cache['host'] = None
        _meter_metadata_cache['fetched_at'] = 0.0
        _meter_metadata_cache['by_eid'] = {}
        self.tz_patch = patch(
            'server.dashboard_services.get_target_timezone',
            return_value=ZoneInfo('America/New_York'),
        )
        self.tz_patch.start()

    def tearDown(self):
        self.tz_patch.stop()
        self.tmpdir.cleanup()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def test_api_meters_annotates_metadata_from_ivp_meters(self):
        fake_gateway = MagicMock()
        fake_gateway.host = 'https://envoy.test'

        def fake_api_call(endpoint, response_raw=False):
            if endpoint == '/ivp/meters/readings':
                payload = [
                    {'eid': 7001, 'activePower': 2500, 'actEnergyDlvd': 50000, 'channels': [{'activePower': 2500}]},
                    {'eid': 7002, 'activePower': -1000, 'actEnergyDlvd': 10000, 'actEnergyRcvd': 20000, 'channels': [{'activePower': -1000}]},
                ]
                return json.dumps(payload) if response_raw else payload
            if endpoint == '/ivp/meters':
                return [
                    {'eid': 7001, 'measurementType': 'production', 'state': 'enabled'},
                    {'eid': 7002, 'measurementType': 'net-consumption', 'state': 'enabled'},
                ]
            return []

        fake_gateway.api_call.side_effect = fake_api_call

        with patch('server.api.load_config', return_value={'gateway_ip': 'envoy.test'}), \
             patch('server.api.get_authenticated_gateway', return_value=fake_gateway):
            resp = self.client.get('/api/meters')
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(len(body), 2)
            self.assertEqual(body[0]['measurementType'], 'production')
            self.assertEqual(body[0]['meterState'], 'enabled')
            self.assertEqual(body[1]['measurementType'], 'net-consumption')

    def test_api_history_today_endpoint_returns_canonical_metrics(self):
        tz = ZoneInfo('America/New_York')
        now_utc = datetime.now(timezone.utc)
        today_local = now_utc.astimezone(tz).date()

        with self.app.app_context():
            db.session.add(MeterReading(
                timestamp=now_utc.replace(tzinfo=None) - timedelta(minutes=5),
                production_wh=50000,
                import_wh=10000,
                export_wh=20000,
            ))
            db.session.add(DailyStats(
                date=today_local,
                production_kwh=25.0,
                export_kwh=15.0,
                import_kwh=5.0,
                consumption_kwh=15.0,
                is_interpolated=False,
            ))
            db.session.commit()

        resp = self.client.get('/api/history/today')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['date'], today_local.isoformat())
        self.assertTrue(body['has_data'])
        self.assertFalse(body['is_stale'])
        self.assertEqual(body['net_grid_kwh'], 10.0)
        self.assertTrue(body['is_net_export'])
        # direct_use = 25 - 15 = 10; coverage = 10 / 15 * 100 = 66.7%
        self.assertEqual(body['solar_coverage_pct'], 66.7)

    def test_record_reading_and_trmnl_display_endpoints(self):
        png_path = os.path.join(self.tmpdir.name, 'trmnl_display.png')
        bmp_path = os.path.join(self.tmpdir.name, 'trmnl_display.bmp')
        telemetry_path = os.path.join(self.tmpdir.name, 'trmnl_telemetry.json')

        tz = ZoneInfo('America/New_York')
        now_local = datetime.now(timezone.utc).astimezone(tz)
        t1 = int((now_local - timedelta(minutes=15)).timestamp())
        t2 = int(now_local.timestamp())

        samples = [
            [
                {'eid': 2, 'measurementType': 'net-consumption', 'meterState': 'enabled', 'timestamp': t1, 'actEnergyDlvd': 10000, 'actEnergyRcvd': 20000},
                {'eid': 1, 'measurementType': 'production', 'meterState': 'enabled', 'timestamp': t1, 'actEnergyDlvd': 50000},
            ],
            [
                {'eid': 2, 'measurementType': 'net-consumption', 'meterState': 'enabled', 'timestamp': t2, 'actEnergyDlvd': 11000, 'actEnergyRcvd': 22000},
                {'eid': 1, 'measurementType': 'production', 'meterState': 'enabled', 'timestamp': t2, 'actEnergyDlvd': 53000},
            ],
        ]

        with patch('server.dashboard_services.get_gateway_readings', side_effect=samples), \
             patch.object(trmnl_service, 'OUTPUT_IMAGE_PATH', png_path), \
             patch.object(trmnl_service, 'OUTPUT_BMP_PATH', bmp_path), \
             patch.object(trmnl_service, 'TELEMETRY_PATH', telemetry_path), \
             patch.object(trmnl_service, 'LEGACY_TELEMETRY_PATH', ''):
            record_reading(self.app)
            record_reading(self.app)

            with self.app.app_context():
                stats = db.session.get(DailyStats, now_local.date())
                self.assertIsNotNone(stats)
                self.assertAlmostEqual(stats.production_kwh, 3.0, places=5)
                self.assertAlmostEqual(stats.import_kwh, 1.0, places=5)
                self.assertAlmostEqual(stats.export_kwh, 2.0, places=5)
                self.assertAlmostEqual(stats.consumption_kwh, 2.0, places=5)

            resp = self.client.get('/api/display', headers={'ID': '11:22:33', 'Battery-Voltage': '4.0', 'RSSI': '-65'})
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertEqual(payload['status'], 0)
            self.assertTrue(payload['filename'].startswith('solar_'))

            bmp_resp = self.client.get('/api/display/current.bmp')
            self.assertEqual(bmp_resp.status_code, 200)
            self.assertEqual(bmp_resp.mimetype, 'image/bmp')
            bmp_resp.close()

            png_resp = self.client.get('/api/display/current.png')
            self.assertEqual(png_resp.status_code, 200)
            self.assertEqual(png_resp.mimetype, 'image/png')
            png_resp.close()


if __name__ == '__main__':
    unittest.main()

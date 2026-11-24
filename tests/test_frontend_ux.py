import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
import pytest

from server.app import create_app
from server.config import get_gateway_token_status
from server.db import db

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


def _make_jwt(exp_dt):
    return jwt.encode(
        {'sub': 'envoy', 'exp': int(exp_dt.timestamp())},
        'test-signing-key-at-least-32-bytes!',
        algorithm='HS256',
    )


# ---------------------------------------------------------------------------
# 1. Enphase JWT Expiration Decoding & Banners
# ---------------------------------------------------------------------------

def test_gateway_token_status_handles_missing_opaque_valid_expiring_and_expired():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    # Missing / placeholder token
    missing = get_gateway_token_status({'gateway_token': 'YOUR_BEARER_TOKEN_HERE'}, now_utc=now)
    assert missing['configured'] is False
    assert missing['has_exp'] is False
    assert missing['is_expired'] is False
    assert missing['is_expiring_soon'] is False

    # Non-JWT opaque token
    opaque = get_gateway_token_status({'gateway_token': 'not-a-jwt-token'}, now_utc=now)
    assert opaque['configured'] is True
    assert opaque['has_exp'] is False
    assert opaque['is_expired'] is False

    # Healthy token (180 days remaining)
    healthy_jwt = _make_jwt(now + timedelta(days=180))
    healthy = get_gateway_token_status({'gateway_token': healthy_jwt}, now_utc=now)
    assert healthy['configured'] is True
    assert healthy['has_exp'] is True
    assert healthy['days_remaining'] == 180
    assert healthy['is_expired'] is False
    assert healthy['is_expiring_soon'] is False

    # Expiring soon (10 days remaining)
    soon_jwt = _make_jwt(now + timedelta(days=10))
    soon = get_gateway_token_status({'gateway_token': soon_jwt}, now_utc=now)
    assert soon['days_remaining'] == 10
    assert soon['is_expired'] is False
    assert soon['is_expiring_soon'] is True

    # Already expired (5 days ago)
    expired_jwt = _make_jwt(now - timedelta(days=5))
    expired = get_gateway_token_status({'gateway_token': expired_jwt}, now_utc=now)
    assert expired['days_remaining'] == -5
    assert expired['is_expired'] is True
    assert expired['is_expiring_soon'] is False


def test_api_token_status_exposes_metadata_without_leaking_raw_token(client):
    exp_dt = datetime.now(timezone.utc) + timedelta(days=12)
    raw_token = _make_jwt(exp_dt)

    with patch('server.api.load_config', return_value={'gateway_ip': 'envoy.local', 'gateway_token': raw_token}):
        resp = client.get('/api/token-status')

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['configured'] is True
    assert body['has_exp'] is True
    assert body['is_expiring_soon'] is True
    assert body['is_expired'] is False
    assert raw_token not in resp.get_data(as_text=True)


def test_index_renders_jwt_expiry_banners_error_banner_and_trmnl_link(client):
    now = datetime.now(timezone.utc)
    expired_token = _make_jwt(now - timedelta(days=2))
    soon_token = _make_jwt(now + timedelta(days=7))
    healthy_token = _make_jwt(now + timedelta(days=180))

    # Expired token + TRMNL enabled -> expired banner + TRMNL header link
    with patch('server.app.load_config', return_value={'gateway_ip': 'envoy.local', 'gateway_token': expired_token, 'trmnl': {'enabled': True}}), \
         patch('server.app.load_specs', return_value={}):
        html = client.get('/').get_data(as_text=True)
    assert 'Gateway token expired' in html
    assert 'data-testid="api-error-banner"' in html
    assert 'href="/trmnl"' in html
    assert expired_token not in html

    # Expiring soon + TRMNL disabled -> warning banner, NO TRMNL header link
    with patch('server.app.load_config', return_value={'gateway_ip': 'envoy.local', 'gateway_token': soon_token, 'trmnl': {'enabled': False}}), \
         patch('server.app.load_specs', return_value={}):
        html_soon = client.get('/').get_data(as_text=True)
    assert 'Gateway token expiring soon' in html_soon
    assert 'href="/trmnl"' not in html_soon

    # Healthy token -> no JWT expiry banner
    with patch('server.app.load_config', return_value={'gateway_ip': 'envoy.local', 'gateway_token': healthy_token}), \
         patch('server.app.load_specs', return_value={}):
        html_ok = client.get('/').get_data(as_text=True)
    assert 'data-testid="jwt-expiry-banner"' not in html_ok


# ---------------------------------------------------------------------------
# 2. Offline-First Local Vendor Assets
# ---------------------------------------------------------------------------

def test_vendor_assets_exist_and_index_has_zero_external_cdn_links(client):
    for asset in ('alpine.min.js', 'chart.umd.min.js'):
        asset_path = os.path.join(PROJECT_ROOT, 'static', 'vendor', asset)
        assert os.path.isfile(asset_path), f'Missing vendored asset {asset}'
        assert os.path.getsize(asset_path) > 10_000, f'Vendored asset {asset} looks truncated'

        resp = client.get(f'/static/vendor/{asset}')
        assert resp.status_code == 200
        resp.close()

    with patch('server.app.load_config', return_value={'gateway_ip': 'envoy.local'}), \
         patch('server.app.load_specs', return_value={}):
        html = client.get('/').get_data(as_text=True)

    assert '/static/vendor/chart.umd.min.js' in html
    assert '/static/vendor/alpine.min.js' in html
    assert 'cdn.jsdelivr.net' not in html
    assert 'fonts.googleapis.com' not in html
    assert 'fonts.gstatic.com' not in html


# ---------------------------------------------------------------------------
# 3. Mobile Button Visibility & Template Inline Style Cleanup
# ---------------------------------------------------------------------------

def test_mobile_css_does_not_hide_refresh_buttons():
    css_path = os.path.join(PROJECT_ROOT, 'static', 'style.css')
    with open(css_path, 'r') as f:
        css = f.read()

    # Ensure .refresh-btn is never hidden with display: none
    assert not re.search(r'\.refresh-btn\s*\{[^}]*display\s*:\s*none', css, flags=re.DOTALL)


def test_tab_templates_avoid_static_inline_style_attributes():
    tabs_dir = os.path.join(PROJECT_ROOT, 'templates', 'tabs')
    offenders = []
    for fname in sorted(os.listdir(tabs_dir)):
        if not fname.endswith('.html'):
            continue
        with open(os.path.join(tabs_dir, fname), 'r') as f:
            content = f.read()
        # Match static style="..." attributes (ignoring Alpine dynamic :style="...")
        if re.search(r'(?<!:)style="', content):
            offenders.append(fname)
    assert offenders == [], f'Found static inline style attributes in tab templates: {offenders}'

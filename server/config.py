import copy
import logging
import math
import os
import threading
from datetime import datetime, timezone
import jwt
import requests
from requests.adapters import HTTPAdapter
import urllib3
import yaml
from enphase_api.local.gateway import Gateway

DEFAULT_TOKEN_EXPIRY_WARNING_DAYS = 30

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CONFIG_FILE = os.path.join(BASE_DIR, 'configuration', 'config.yml')
SPECS_FILE = os.path.join(BASE_DIR, 'configuration', 'system.yml')

DEFAULT_GATEWAY_TIMEOUT = 10.0
GATEWAY_POOL_SIZE = 10

_yaml_cache_lock = threading.Lock()
_yaml_cache = {}

_gateway_cache_lock = threading.Lock()
_gateway_cache = {
    "key": None,
    "gateway": None,
}


def reset_caches():
    """Clear in-memory YAML and Gateway session caches (useful in tests)."""
    with _yaml_cache_lock:
        _yaml_cache.clear()
    with _gateway_cache_lock:
        _gateway_cache["key"] = None
        _gateway_cache["gateway"] = None


def _load_yaml_cached(file_path, default_factory):
    """Load a YAML file with mtime/size-based in-memory caching."""
    try:
        stat = os.stat(file_path)
    except OSError:
        with _yaml_cache_lock:
            _yaml_cache.pop(file_path, None)
        return default_factory()

    signature = (stat.st_mtime_ns, stat.st_size)
    with _yaml_cache_lock:
        cached = _yaml_cache.get(file_path)
        if cached is not None and cached["signature"] == signature:
            return copy.deepcopy(cached["data"])

    with open(file_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    with _yaml_cache_lock:
        _yaml_cache[file_path] = {
            "signature": signature,
            "data": copy.deepcopy(data),
        }
    return data


def load_config():
    return _load_yaml_cached(
        CONFIG_FILE,
        lambda: {"gateway_ip": "envoy.local", "use_https": True},
    )


def load_specs():
    return _load_yaml_cached(SPECS_FILE, dict)


def _resolve_ssl_verify(config):
    """
    Resolve requests `verify` value from config:
    - `ca_bundle`: path to a CA certificate/bundle file
    - `verify_ssl`: bool or CA bundle path (defaults to False for self-signed Envoy certs)
    """
    ca_bundle = config.get('ca_bundle')
    if isinstance(ca_bundle, str) and ca_bundle.strip():
        return ca_bundle.strip()

    verify_ssl = config.get('verify_ssl', False)
    if isinstance(verify_ssl, str) and verify_ssl.strip():
        return verify_ssl.strip()
    return bool(verify_ssl)


def _resolve_gateway_timeout(config):
    """Resolve HTTP timeout (float or (connect, read) tuple) from config."""
    raw = config.get('gateway_timeout', DEFAULT_GATEWAY_TIMEOUT)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            connect_t, read_t = float(raw[0]), float(raw[1])
            if connect_t > 0 and read_t > 0:
                return (connect_t, read_t)
        except (TypeError, ValueError):
            pass
        return DEFAULT_GATEWAY_TIMEOUT
    try:
        timeout = float(raw)
        if timeout > 0:
            return timeout
    except (TypeError, ValueError):
        pass
    return DEFAULT_GATEWAY_TIMEOUT


def _normalize_token(token):
    if not token or token == "YOUR_BEARER_TOKEN_HERE":
        return None
    return str(token)


def get_gateway_token_status(config=None, now_utc=None, warning_days=DEFAULT_TOKEN_EXPIRY_WARNING_DAYS):
    """
    Decode the configured Enphase Gateway JWT `exp` claim (without signature verification)
    and return safe expiration metadata for UI warnings. Never returns the raw JWT.
    """
    if config is None:
        config = load_config()
    token = _normalize_token(config.get('gateway_token') if isinstance(config, dict) else None)

    base = {
        "configured": bool(token),
        "has_exp": False,
        "expires_at": None,
        "expires_date": None,
        "days_remaining": None,
        "is_expired": False,
        "is_expiring_soon": False,
        "warning_days": int(warning_days),
    }
    if not token:
        return base

    try:
        claims = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
        )
    except Exception as e:
        logger.debug("Configured gateway_token is not a decodable JWT: %s", e)
        return base

    exp = claims.get("exp") if isinstance(claims, dict) else None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return base

    try:
        exp_dt = datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return base

    if now_utc is None:
        now = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now = now_utc.replace(tzinfo=timezone.utc)
    else:
        now = now_utc.astimezone(timezone.utc)

    delta_seconds = (exp_dt - now).total_seconds()
    is_expired = delta_seconds <= 0
    days_remaining = math.floor(delta_seconds / 86400)
    is_expiring_soon = (not is_expired) and (delta_seconds <= warning_days * 86400)

    base.update({
        "has_exp": True,
        "expires_at": exp_dt.isoformat(),
        "expires_date": exp_dt.date().isoformat(),
        "days_remaining": days_remaining,
        "is_expired": is_expired,
        "is_expiring_soon": is_expiring_soon,
    })
    return base


class ManagedGateway:
    """
    Thread-safe wrapper around Enphase-API `Gateway` that:
    - Reuses a persistent `requests.Session` connection pool
    - Enforces explicit HTTP timeouts on login and API calls
    - Suppresses InsecureRequestWarning when TLS verification is disabled
    - Automatically re-authenticates and retries once on 401/403 responses
    """

    def __init__(self, host, token=None, verify=False, timeout=DEFAULT_GATEWAY_TIMEOUT):
        self.host = host
        self._token = _normalize_token(token)
        self.verify = verify
        self.timeout = timeout
        self._auth_lock = threading.Lock()
        self._authenticated = False
        self._auth_generation = 0

        if isinstance(verify, str) and os.path.exists(verify):
            self._gateway = Gateway(host=host, cert_file=verify)
        else:
            self._gateway = Gateway(host=host)

        self.session = self._gateway.session
        self.session.verify = verify

        if verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        if isinstance(self.session, requests.Session) and isinstance(verify, bool):
            adapter = HTTPAdapter(
                pool_connections=GATEWAY_POOL_SIZE,
                pool_maxsize=GATEWAY_POOL_SIZE,
            )
            self.session.mount('https://', adapter)
            self.session.mount('http://', adapter)

        self._install_session_timeout_wrapper()

    def _install_session_timeout_wrapper(self):
        """Ensure underlying session requests (including Gateway.login) enforce `self.timeout`."""
        original_request = getattr(self.session, 'request', None)
        if not callable(original_request):
            return
        upstream_default_timeout = getattr(Gateway, 'TIMEOUT', 300)

        def _request_with_timeout(method, url, **kwargs):
            req_timeout = kwargs.get('timeout')
            if req_timeout is None or req_timeout == upstream_default_timeout:
                kwargs['timeout'] = self.timeout
            return original_request(method, url, **kwargs)

        self.session.request = _request_with_timeout

    def _authenticate_locked(self):
        """Perform Envoy login while holding `self._auth_lock`."""
        if not self._token:
            self._authenticated = True
            return True

        cookies = getattr(self.session, 'cookies', None)
        if cookies is not None and hasattr(cookies, 'clear'):
            cookies.clear()
        headers = getattr(self.session, 'headers', None)
        if headers is not None and hasattr(headers, 'pop'):
            headers.pop('Authorization', None)

        logged_in = False
        try:
            logged_in = bool(self._gateway.login(self._token))
        except Exception as e:
            logger.warning("Enphase Gateway login failed: %s", e)

        if not logged_in and headers is not None and hasattr(headers, 'update'):
            # Fallback to Bearer header if /auth/check_jwt login fails or is unsupported
            headers.update({'Authorization': f'Bearer {self._token}'})

        self._auth_generation += 1
        self._authenticated = True
        return logged_in

    def ensure_authenticated(self):
        """Authenticate once per cached gateway session in a thread-safe manner."""
        if self._authenticated or not self._token:
            return
        with self._auth_lock:
            if not self._authenticated:
                self._authenticate_locked()

    def reauthenticate(self, failed_generation):
        """
        Re-authenticate after a 401/403 response, coalescing concurrent threads
        so only one thread performs login when a session expires.
        """
        if not self._token:
            return False
        with self._auth_lock:
            if self._auth_generation != failed_generation:
                return True
            self._authenticate_locked()
            return True

    def login(self, token):
        """Explicitly authenticate with a token."""
        with self._auth_lock:
            self._token = _normalize_token(token)
            return self._authenticate_locked()

    def _default_headers(self):
        headers = getattr(Gateway, 'HEADERS', None)
        return dict(headers) if isinstance(headers, dict) else None

    def api_call(self, path, method='GET', data=None, json=None, response_raw=False, timeout=None):
        """Make an API call to the IQ Gateway with explicit timeout and 401/403 auto-retry."""
        self.ensure_authenticated()
        effective_timeout = self.timeout if timeout is None else timeout
        headers = self._default_headers()

        auth_gen = self._auth_generation
        response = self.session.request(
            method=method,
            url=self.host + path,
            headers=headers,
            data=data,
            json=json,
            timeout=effective_timeout,
        )

        if response.status_code in (401, 403) and self._token:
            self.reauthenticate(auth_gen)
            response = self.session.request(
                method=method,
                url=self.host + path,
                headers=headers,
                data=data,
                json=json,
                timeout=effective_timeout,
            )

        if response.status_code >= 400:
            response.raise_for_status()

        if response_raw:
            return response.text
        return response.json() if len(response.content) > 0 else None

    def api_call_stream(self, path, timeout=None):
        """Make a streaming SSE call to the IQ Gateway with 401/403 auto-retry."""
        self.ensure_authenticated()
        effective_timeout = self.timeout if timeout is None else timeout
        headers = self._default_headers()

        auth_gen = self._auth_generation
        response = self.session.request(
            method='GET',
            url=self.host + path,
            headers=headers,
            stream=True,
            timeout=effective_timeout,
        )

        if response.status_code in (401, 403) and self._token:
            response.close()
            self.reauthenticate(auth_gen)
            response = self.session.request(
                method='GET',
                url=self.host + path,
                headers=headers,
                stream=True,
                timeout=effective_timeout,
            )

        if response.status_code >= 400:
            response.raise_for_status()

        return response


def get_authenticated_gateway(config=None):
    """Return a cached, thread-safe authenticated Gateway instance for the active config."""
    if config is None:
        config = load_config()
    protocol = "https" if config.get('use_https', True) else "http"
    host = f"{protocol}://{config['gateway_ip']}"
    token = _normalize_token(config.get('gateway_token'))
    verify = _resolve_ssl_verify(config)
    timeout = _resolve_gateway_timeout(config)

    cache_key = (host, token, verify, timeout)
    with _gateway_cache_lock:
        if _gateway_cache["key"] == cache_key and _gateway_cache["gateway"] is not None:
            gateway = _gateway_cache["gateway"]
        else:
            gateway = ManagedGateway(
                host=host,
                token=token,
                verify=verify,
                timeout=timeout,
            )
            _gateway_cache["key"] = cache_key
            _gateway_cache["gateway"] = gateway

    gateway.ensure_authenticated()
    return gateway

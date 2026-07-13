import contextvars
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import struct

import boto3
import jwt
import pyarrow.parquet as pq
import pyarrow.orc as orc_mod
import fastavro
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Depends, Cookie, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse, StreamingResponse, JSONResponse, HTMLResponse
import pyotp
from passlib.hash import bcrypt
from pydantic import BaseModel
import hashlib
import base64
from cryptography.fernet import Fernet, InvalidToken
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pricing import (
    get_storage_pricing, get_storage_price, estimate_monthly_cost as _estimate_monthly_cost,
    detect_provider, get_all_providers, calculate_savings, STATIC_PRICING,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sairo")


class _HealthCheckFilter(logging.Filter):
    """Suppress noisy health check access logs from HAProxy/k8s probes."""
    def filter(self, record):
        msg = record.getMessage()
        return "/healthz" not in msg


logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())

app = FastAPI()

# ── API Rate Limiting ──────────────────────────────────────────────────────
RATE_LIMIT = os.environ.get("RATE_LIMIT", "120/minute")
UPLOAD_RATE_LIMIT = os.environ.get("UPLOAD_RATE_LIMIT", "30/minute")

limiter = Limiter(key_func=get_remote_address, default_limits=[RATE_LIMIT])
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests. Please slow down."})


# ── Security Headers Middleware ────────────────────────────────────────────

def _csp_connect_origins():
    """Origins the browser is allowed to XHR to, beyond 'self' — every configured S3
    endpoint. Direct (presigned) uploads PUT straight from the browser to the S3
    endpoint, which is a DIFFERENT origin than Sairo; without these the CSP
    'connect-src' silently blocks the upload (and there's no proxy fallback because
    the same-origin signing request still succeeds). Includes a wildcard subdomain so
    virtual-host-style presigned URLs (bucket.endpoint) are allowed too."""
    from urllib.parse import urlparse
    origins = set()
    try:
        for info in list(_s3_manager._endpoints.values()):
            p = urlparse(info.get("endpoint_url") or "")
            if p.scheme and p.netloc:
                origins.add(f"{p.scheme}://{p.netloc}")
                origins.add(f"{p.scheme}://*.{p.netloc}")
    except Exception:
        pass
    return " ".join(sorted(origins))


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    connect_src = ("connect-src 'self' " + _csp_connect_origins()).rstrip()
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' blob: data:; "
        f"{connect_src}; "
        "frame-src blob:;"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ── S3-key session helpers (AUTH_MODE=s3) ──────────────────────────────────

def _extract_s3_session(request: Request):
    """For AUTH_MODE=s3 cookie sessions, decode the JWT and return the user's
    {ak, sk, eid} (decrypted). Returns None otherwise (password mode, API tokens,
    no/invalid cookie). API-token (Bearer) sessions intentionally keep server creds."""
    if AUTH_MODE != "s3":
        return None
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        p = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None
    ak_enc = p.get("s3ak")
    if not ak_enc:
        return None
    ak = _decrypt(ak_enc)
    if not ak:
        return None
    return {"ak": ak, "sk": _decrypt(p.get("s3sk", "")), "eid": p.get("eid", "default")}


_bucket_access_cache: dict = {}  # (ak_hash, eid, bucket) -> (ts, bool)
_bucket_access_lock = threading.Lock()
_BUCKET_ACCESS_TTL = 60


def _s3_user_can_access(creds: dict, endpoint_id: str, bucket: str) -> bool:
    """True if the user's S3 keys can reach this bucket (provider IAM is the source of
    truth). head_bucket result cached briefly so it isn't called on every request."""
    ak_hash = hashlib.sha256(creds["ak"].encode()).hexdigest()[:16]
    key = (ak_hash, endpoint_id, bucket)
    now = time.time()
    with _bucket_access_lock:
        hit = _bucket_access_cache.get(key)
        if hit and now - hit[0] < _BUCKET_ACCESS_TTL:
            return hit[1]
    try:
        _s3_manager.get_client(endpoint_id).head_bucket(Bucket=bucket)  # uses user creds (ctx set)
        ok = True
    except Exception:
        ok = False
    with _bucket_access_lock:
        if len(_bucket_access_cache) > 2000:
            _bucket_access_cache.clear()
        _bucket_access_cache[key] = (now, ok)
    return ok


# ── Bucket Permission Middleware ────────────────────────────────────────────
# Registered BEFORE endpoint_routing_middleware (below) so it ends up INNER: it runs
# after the path is rewritten and after the S3-key user's creds are in context.

@app.middleware("http")
async def bucket_permission_middleware(request: Request, call_next):
    """Per-bucket access control for /api/buckets/{bucket}/... routes."""
    path = request.scope.get("path", request.url.path)
    if not path.startswith("/api/buckets/"):
        return await call_next(request)
    parts = path.split("/")
    if len(parts) < 4:
        return await call_next(request)
    bucket = parts[3]
    try:
        user = get_current_user(request)
    except HTTPException:
        return await call_next(request)
    # AUTH_MODE=s3: the user acts with their own keys. Object listings are served from
    # the LOCAL index (built with server creds), so we must independently confirm the
    # user's keys can actually reach this bucket — otherwise the index would be an
    # access bypass. The provider's IAM (cached head_bucket) is the source of truth.
    creds = _user_creds_ctx.get(None)
    if creds and creds.get("ak"):
        if not _s3_user_can_access(creds, request.state.endpoint_id, bucket):
            return JSONResponse(status_code=403, content={"detail": "No access to this bucket"})
        request.state.bucket_permission = "write"  # actual op authority enforced by S3 IAM
        return await call_next(request)
    # Admin bypasses everything
    if user["role"] == "admin":
        request.state.bucket_permission = "admin"
        return await call_next(request)
    # Non-admin (password mode): lookup bucket permission
    with _get_users_db() as db:
        row = db.execute(
            "SELECT permission FROM bucket_permissions WHERE username=? AND bucket=?",
            (user["username"], bucket)
        ).fetchone()
    if not row:
        return JSONResponse(status_code=403, content={"detail": "No access to this bucket"})
    permission = row["permission"]
    request.state.bucket_permission = permission
    # Write operations need write permission
    if request.method != "GET" and permission != "write":
        return JSONResponse(status_code=403, content={"detail": "Write access required"})
    return await call_next(request)


# ── Multi-Endpoint URL Rewriting + S3-key session Middleware ────────────────
# Registered LAST → OUTERMOST: runs first on the way in, so the path is rewritten and
# the S3-key user's credentials/endpoint are bound into context BEFORE the permission
# middleware and route handler execute.

@app.middleware("http")
async def endpoint_routing_middleware(request: Request, call_next):
    """Rewrite /api/e/{endpoint_id}/... → /api/..., set endpoint context, and (in
    AUTH_MODE=s3) bind the request to the logged-in user's endpoint + credentials."""
    path = request.url.path
    endpoint_id = "default"
    if path.startswith("/api/e/"):
        parts = path.split("/")
        # parts: ['', 'api', 'e', endpoint_id, ...]
        if len(parts) >= 5:
            endpoint_id = parts[3]
            request.scope["path"] = "/api/" + "/".join(parts[4:])
    # S3-key session: carry the user's own keys + bind to the endpoint they logged into
    # (ignore the routing endpoint, so an S3-key user can't reach another endpoint).
    user_creds = None
    scope_path = request.scope.get("path", path)
    if scope_path.startswith("/api/"):
        sess = _extract_s3_session(request)
        if sess:
            user_creds = {"ak": sess["ak"], "sk": sess["sk"]}
            endpoint_id = sess["eid"]
    request.state.endpoint_id = endpoint_id
    t_e = _endpoint_ctx.set(endpoint_id)
    t_u = _user_creds_ctx.set(user_creds)
    try:
        return await call_next(request)
    finally:
        _endpoint_ctx.reset(t_e)
        _user_creds_ctx.reset(t_u)


# ── Login Rate Limiter ──────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()
LOGIN_RATE_WINDOW = 300  # 5 minutes
LOGIN_RATE_MAX = 10      # max attempts per window

def _check_login_rate(ip: str):
    """Raise 429 if IP has exceeded login attempt limit."""
    now = time.time()
    with _login_lock:
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < LOGIN_RATE_WINDOW]
        if len(attempts) >= LOGIN_RATE_MAX:
            raise HTTPException(429, "Too many login attempts. Try again later.")
        attempts.append(now)
        _login_attempts[ip] = attempts
        # Periodic cleanup: remove stale IPs
        if len(_login_attempts) > 1000:
            stale = [k for k, v in _login_attempts.items()
                     if all(now - t >= LOGIN_RATE_WINDOW for t in v)]
            for k in stale:
                del _login_attempts[k]


@app.exception_handler(ClientError)
async def s3_error_handler(request, exc):
    """Convert unhandled S3 ClientError into user-friendly JSON responses."""
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    msg = exc.response.get("Error", {}).get("Message", "")
    # Sanitize: strip potential internal details (ARNs, account IDs, internal endpoints)
    import re
    msg = re.sub(r'arn:[^\s,]+', '[ARN]', msg)
    msg = re.sub(r'\d{12}', '[ACCOUNT]', msg)
    if not msg:
        msg = code
    status_map = {
        "NoSuchKey": 404, "NotFound": 404, "NoSuchBucket": 404,
        "NoSuchUpload": 404, "NoSuchBucketPolicy": 404,
        "AccessDenied": 403, "AllAccessDisabled": 403,
        "BucketAlreadyExists": 409, "BucketAlreadyOwnedByYou": 409,
        "BucketNotEmpty": 409,
        "InvalidBucketName": 400, "InvalidRange": 400,
        "MalformedPolicy": 400, "MalformedXML": 400,
    }
    status = status_map.get(code, 502)
    log.warning("S3 error [%s]: %s", code, msg)
    return JSONResponse(status_code=status, content={"detail": f"{code}: {msg}"})


_app_start_time = time.time()
SAIRO_VERSION = "3.6.2"


def _version_gt(a: str, b: str) -> bool:
    """True if version `a` is newer than `b`, compared numerically (not lexically,
    so 3.10.0 > 3.9.0). Non-numeric/missing parts degrade gracefully to 0."""
    def parts(v):
        out = []
        for p in (v or "").lstrip("vV").split("."):
            num = "".join(c for c in p if c.isdigit())
            out.append(int(num) if num else 0)
        return out
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb
TELEMETRY = os.environ.get("TELEMETRY", "true").lower() != "false"

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")
if not S3_ENDPOINT:
    log.error("S3_ENDPOINT environment variable is required")
    raise SystemExit("S3_ENDPOINT environment variable is required")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
DB_DIR = os.environ.get("DB_DIR", "/data")

# ── Validate DB_DIR is writable at startup ───────────────────────────────────
try:
    os.makedirs(DB_DIR, exist_ok=True)
    _probe_path = os.path.join(DB_DIR, ".startup_probe")
    with open(_probe_path, "w") as _f:
        _f.write("ok")
    os.remove(_probe_path)
except Exception as _e:
    log.error("DB_DIR '%s' is not writable: %s — mount a volume at %s", DB_DIR, _e, DB_DIR)
    raise SystemExit(f"DB_DIR '{DB_DIR}' is not writable: {_e}")

# ── Auth Config ──────────────────────────────────────────────────────────────
# AUTH_MODE: "local" (default — username/password) or "s3" (authenticate with S3 access key/secret key)
AUTH_MODE = os.environ.get("AUTH_MODE", "local").lower()
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS")
if not ADMIN_PASS and AUTH_MODE == "local":
    ADMIN_PASS = secrets.token_urlsafe(16)
    log.warning("ADMIN_PASS not set — generated temporary password. Retrieve it with: docker logs <container> 2>&1 | grep GENERATED")
    # Write to file inside container for secure retrieval
    _pass_file = os.path.join(DB_DIR, ".generated_password")
    try:
        with open(_pass_file, "w") as _pf:
            _pf.write(ADMIN_PASS)
        os.chmod(_pass_file, 0o600)
        log.info("GENERATED admin password written to %s", _pass_file)
    except OSError:
        pass
elif not ADMIN_PASS:
    ADMIN_PASS = secrets.token_urlsafe(32)  # Set a strong random password in s3 mode (not displayed)
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "24"))
if AUTH_MODE == "s3":
    log.info("Auth mode: S3 — users authenticate with S3 access key and secret key")

# ── Fernet Encryption for credentials at rest ─────────────────────────────
# Derive a Fernet key from JWT_SECRET (deterministic so we can decrypt on restart)
_fernet_key = base64.urlsafe_b64encode(hashlib.sha256(JWT_SECRET.encode()).digest())
_fernet = Fernet(_fernet_key)
_ENCRYPTED_PREFIX = "enc::"


def _encrypt(plaintext: str) -> str:
    """Encrypt a string for storage at rest."""
    if not plaintext:
        return plaintext
    return _ENCRYPTED_PREFIX + _fernet.encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    """Decrypt a stored string. Returns as-is if not encrypted (migration support)."""
    if not ciphertext:
        return ciphertext
    if not ciphertext.startswith(_ENCRYPTED_PREFIX):
        return ciphertext  # plaintext from before encryption was added
    try:
        return _fernet.decrypt(ciphertext[len(_ENCRYPTED_PREFIX):].encode()).decode()
    except InvalidToken:
        log.error("Failed to decrypt credential — JWT_SECRET may have changed")
        return ""


_S3_CONFIG = Config(
    signature_version="s3v4",
    connect_timeout=10,
    read_timeout=120,
    retries={"max_attempts": 3, "mode": "adaptive"},
    max_pool_connections=int(os.environ.get("S3_MAX_POOL_CONNECTIONS", "32")),  # parallel crawl/delta list calls
)

# ── Multi-Endpoint S3 Client Manager ──────────────────────────────────────

class S3ClientManager:
    """Thread-safe cache of boto3 S3 clients keyed by endpoint ID."""
    def __init__(self):
        self._clients: dict = {}
        self._lock = threading.Lock()
        self._endpoints: dict = {}  # endpoint_id -> {endpoint_url, access_key, secret_key, region, path_style}
        self._user_clients: dict = {}  # (endpoint_id, access_key_hash) -> client (AUTH_MODE=s3, per-user creds)

    def register(self, endpoint_id: str, endpoint_url: str, access_key: str, secret_key: str,
                 region: str = "", path_style: bool = False):
        with self._lock:
            self._endpoints[endpoint_id] = {
                "endpoint_url": endpoint_url, "access_key": access_key,
                "secret_key": secret_key, "region": region, "path_style": path_style,
            }
            self._clients.pop(endpoint_id, None)  # Invalidate cached client
            for k in [k for k in self._user_clients if k[0] == endpoint_id]:
                self._user_clients.pop(k, None)

    def _build_client(self, info: dict, access_key: str, secret_key: str):
        cfg = _S3_CONFIG
        if info.get("path_style"):
            cfg = _S3_CONFIG.merge(Config(s3={"addressing_style": "path"}))
        kwargs = {
            "endpoint_url": info["endpoint_url"],
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": cfg,
        }
        if info["region"]:
            kwargs["region_name"] = info["region"]
        return boto3.client("s3", **kwargs)

    def get_client(self, endpoint_id: str = "default"):
        # AUTH_MODE=s3: when a request carries the user's own S3 keys, every S3 call
        # uses THOSE keys (provider IAM scopes the result) against the endpoint's
        # connection params. Background threads / password sessions have no creds in
        # context → fall through to the shared server client below.
        creds = _user_creds_ctx.get(None)
        if creds and creds.get("ak"):
            return self._get_user_client(endpoint_id, creds["ak"], creds["sk"])
        with self._lock:
            if endpoint_id in self._clients:
                return self._clients[endpoint_id]
            info = self._endpoints.get(endpoint_id)
            if not info:
                raise HTTPException(404, f"S3 endpoint '{endpoint_id}' not found")
            client = self._build_client(info, info["access_key"], info["secret_key"])
            self._clients[endpoint_id] = client
            return client

    def _get_user_client(self, endpoint_id: str, access_key: str, secret_key: str):
        info = self._endpoints.get(endpoint_id) or self._endpoints.get("default")
        if not info:
            raise HTTPException(404, f"S3 endpoint '{endpoint_id}' not found")
        ak_hash = hashlib.sha256(access_key.encode()).hexdigest()[:16]
        key = (endpoint_id, ak_hash)
        with self._lock:
            client = self._user_clients.get(key)
            if client is None:
                if len(self._user_clients) > 256:  # bound the cache; drop oldest insert
                    self._user_clients.pop(next(iter(self._user_clients)), None)
                client = self._build_client(info, access_key, secret_key)
                self._user_clients[key] = client
            return client

    def invalidate(self, endpoint_id: str):
        with self._lock:
            self._clients.pop(endpoint_id, None)
            self._endpoints.pop(endpoint_id, None)
            for k in [k for k in self._user_clients if k[0] == endpoint_id]:
                self._user_clients.pop(k, None)

    def get_endpoint_info(self, endpoint_id: str):
        return self._endpoints.get(endpoint_id)

    def get_all_ids(self):
        return list(self._endpoints.keys())

_s3_manager = S3ClientManager()
# Register default endpoint from env vars
_S3_PATH_STYLE = os.environ.get("S3_PATH_STYLE", "false").lower() in ("true", "1", "yes")
_S3_REGION = os.environ.get("S3_REGION", "")
_s3_manager.register("default", S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, _S3_REGION, _S3_PATH_STYLE)

# Context variable for current endpoint — propagates across async/sync boundaries in Starlette
_endpoint_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("_endpoint_ctx", default="default")

# Per-request S3 credentials for AUTH_MODE=s3 — when set, every S3 client built for
# this request uses the LOGGED-IN USER's keys instead of the server's endpoint creds,
# so the provider's IAM scopes exactly what the user can see/do. None for password-mode
# sessions, API tokens, and background (crawl) threads → those keep using server creds.
_user_creds_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("_user_creds_ctx", default=None)

# Keep thread-local as fallback for background threads that set it explicitly
_s3_context = threading.local()

class _S3ClientProxy:
    """Proxy that delegates to the right S3 client based on current request context.

    Uses contextvars (propagates across Starlette async→sync), with thread-local fallback
    for background threads (crawl, recrawl) that set it explicitly.
    """
    def __getattr__(self, name):
        eid = _endpoint_ctx.get("default")
        if eid == "default":
            # Fallback to thread-local (used by background threads)
            eid = getattr(_s3_context, "endpoint_id", "default") or "default"
        client = _s3_manager.get_client(eid)
        return getattr(client, name)

# Global S3 client proxy — used by all existing code via `s3.xxx()`
s3 = _S3ClientProxy()

def _get_s3(request: Request = None):
    """Get S3 client for the current request's endpoint, or default."""
    if request:
        eid = getattr(request.state, "endpoint_id", "default")
        if eid and eid != "default":
            return _s3_manager.get_client(eid)
    return _s3_manager.get_client("default")

log.info("Sairo starting — endpoint=%s, db_dir=%s, session=%dh, secure_cookie=%s",
         S3_ENDPOINT, DB_DIR, SESSION_HOURS, os.environ.get("SECURE_COOKIE", "true"))

# ── Users Database ────────────────────────────────────────────────────────

def _users_db_path():
    return os.path.join(DB_DIR, "users.db")

def _init_users_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_users_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            bucket TEXT,
            details TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_bucket ON audit_log(bucket)")
    # API tokens table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL,
            username TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'viewer',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            last_used TEXT
        )
    """)
    # Share links table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS share_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            bucket TEXT NOT NULL,
            key TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            download_count INTEGER DEFAULT 0,
            max_downloads INTEGER,
            password_hash TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_share_token ON share_links(token)")
    # License keys table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS license_info (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            license_key TEXT,
            license_type TEXT DEFAULT 'community',
            licensed_to TEXT,
            max_users INTEGER DEFAULT 0,
            features TEXT DEFAULT '{}',
            activated_at TEXT,
            expires_at TEXT
        )
    """)
    conn.execute("INSERT OR IGNORE INTO license_info (id, license_type) VALUES (1, 'community')")
    # Bucket permissions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket_permissions (
            username TEXT NOT NULL,
            bucket TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'read',
            granted_by TEXT NOT NULL,
            granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (username, bucket)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bp_username ON bucket_permissions(username)")
    # 2FA + auth-source columns (added via ALTER TABLE for backward compat)
    for col, coldef in [
        ("totp_secret", "TEXT"),
        ("totp_enabled", "INTEGER DEFAULT 0"),
        ("recovery_codes", "TEXT"),  # JSON array of bcrypt-hashed codes
        ("auth_source", "TEXT"),     # local | ldap | oauth_google | oauth_github | oidc
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # Backfill auth_source for rows created before the column existed, inferring
    # the original identity provider from the placeholder password_hash prefix
    # (federated logins store "LDAP:"/"OAUTH:"/"OIDC:"; everything else is local).
    # This is what lets us block one provider from hijacking another's account.
    conn.execute("""
        UPDATE users SET auth_source = CASE
            WHEN password_hash LIKE 'LDAP:%'  THEN 'ldap'
            WHEN password_hash LIKE 'OIDC:%'  THEN 'oidc'
            WHEN password_hash LIKE 'OAUTH:%' THEN 'oauth'
            ELSE 'local' END
        WHERE auth_source IS NULL
    """)
    # S3 endpoints table for multi-endpoint support
    conn.execute("""
        CREATE TABLE IF NOT EXISTS s3_endpoints (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            endpoint_url TEXT NOT NULL,
            access_key TEXT NOT NULL,
            secret_key TEXT NOT NULL,
            region TEXT DEFAULT '',
            path_style INTEGER DEFAULT 0,
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT
        )
    """)
    # Instance metadata (telemetry ID)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instance_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    # Ensure default admin user exists and password matches ADMIN_PASS env var
    admin_row = conn.execute("SELECT password_hash FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
    if admin_row is None:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                     (ADMIN_USER, bcrypt.hash(ADMIN_PASS), "admin"))
        conn.commit()
        log.info("Created default admin user '%s'", ADMIN_USER)
    elif ADMIN_PASS and not bcrypt.verify(ADMIN_PASS, admin_row[0]):
        conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                     (bcrypt.hash(ADMIN_PASS), ADMIN_USER))
        conn.commit()
        log.info("Admin password synced from ADMIN_PASS env var")
    # Auto-seed default S3 endpoint
    ep_row = conn.execute("SELECT id FROM s3_endpoints WHERE id='default'").fetchone()
    if not ep_row:
        conn.execute("INSERT INTO s3_endpoints (id, name, endpoint_url, access_key, secret_key, is_default, created_by) VALUES (?,?,?,?,?,1,'system')",
                     ("default", "Default", S3_ENDPOINT, _encrypt(S3_ACCESS_KEY), _encrypt(S3_SECRET_KEY)))
        conn.commit()
    else:
        # Migrate existing plaintext credentials to encrypted
        ep_data = conn.execute("SELECT access_key, secret_key FROM s3_endpoints WHERE id='default'").fetchone()
        if ep_data and not ep_data[0].startswith(_ENCRYPTED_PREFIX):
            conn.execute("UPDATE s3_endpoints SET access_key=?, secret_key=? WHERE id='default'",
                         (_encrypt(ep_data[0]), _encrypt(ep_data[1])))
            conn.commit()
            log.info("Migrated default endpoint credentials to encrypted storage")
    conn.close()

@contextmanager
def _get_users_db():
    conn = sqlite3.connect(_users_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

_init_users_db()


# ── Auth Helpers ─────────────────────────────────────────────────────────────

def _verify_api_token(token_str: str):
    """Verify a Bearer API token. Returns {username, role} or None."""
    import hashlib
    token_hash = hashlib.sha256(token_str.encode()).hexdigest()
    with _get_users_db() as db:
        row = db.execute(
            "SELECT username, role, expires_at FROM api_tokens WHERE token_hash=?",
            (token_hash,)).fetchone()
        if not row:
            return None
        if row["expires_at"]:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                return None
        db.execute("UPDATE api_tokens SET last_used=? WHERE token_hash=?",
                   (datetime.now(timezone.utc).isoformat(), token_hash))
        db.commit()
    return {"username": row["username"], "role": row["role"], "via_token": True}


def get_current_user(request: Request):
    """Extract and validate JWT from cookie OR Bearer token. Returns {username, role}."""
    # Check Bearer token first
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token_str = auth_header[7:]
        user = _verify_api_token(token_str)
        if user:
            return user
        raise HTTPException(401, "Invalid or expired API token")
    # Fall back to cookie
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        # Reject 2FA pending tokens for normal endpoints
        if payload.get("purpose") == "2fa":
            raise HTTPException(401, "2FA verification required")
        return {"username": payload["sub"], "role": payload["role"]}
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

def require_admin(request: Request, user: dict = Depends(get_current_user)):
    """Require admin role, or bucket write permission for /api/buckets/ routes."""
    if user["role"] == "admin":
        return user
    bp = getattr(request.state, "bucket_permission", None)
    if request.url.path.startswith("/api/buckets/") and bp == "write":
        return user
    raise HTTPException(403, "Admin access required")


class FederatedAuthError(Exception):
    """Raised when a federated (SSO) login can't be completed safely."""


def _sync_federated_user(username: str, source: str, hash_prefix: str,
                         default_role: str, mapped_role: str | None = None):
    """Create-or-fetch a user logging in via an external IdP (OIDC/OAuth/LDAP).

    The single security-critical chokepoint for every SSO path:

    * **Account-takeover guard** — if a user with this name already exists from a
      *different* auth source, the login is rejected. Without this, an IdP user
      who can set their username to ``admin`` would log straight into the local
      admin account. A user is only ever logged in against the provider that
      created them.
    * New users are created with ``default_role`` and NO bucket grants (an admin
      assigns access). When ``mapped_role`` is provided (e.g. derived from IdP
      groups), it sets the role on create and re-syncs it for that provider's
      own users on later logins — never for a user owned by another source.

    Returns ``(role, totp_enabled)``. Raises ``FederatedAuthError`` on conflict.
    """
    with _get_users_db() as db:
        row = db.execute(
            "SELECT role, totp_enabled, auth_source FROM users WHERE username=?",
            (username,)).fetchone()
        if row is not None:
            existing_source = row["auth_source"] or "local"
            if existing_source != source:
                raise FederatedAuthError(
                    f"username '{username}' already exists via '{existing_source}'")
            role = row["role"]
            if mapped_role and mapped_role != role:
                # Provider-managed role (group mapping) — keep DB in sync.
                db.execute("UPDATE users SET role=? WHERE username=?", (mapped_role, username))
                db.commit()
                role = mapped_role
            return role, bool(row["totp_enabled"])
        role = mapped_role or default_role
        db.execute(
            "INSERT INTO users (username, password_hash, role, auth_source) VALUES (?,?,?,?)",
            (username, f"{hash_prefix}:{secrets.token_hex(16)}", role, source))
        db.commit()
        _audit("user_created", username, details=f"method={source}")
        return role, False


def _summarize_keys(keys, max_items=3):
    if not keys:
        return ""
    if len(keys) <= max_items:
        return ", ".join(keys)
    return ", ".join(keys[:max_items]) + f" (+{len(keys) - max_items} more)"


_audit_failures = 0


def _audit(action: str, username: str, bucket: Optional[str] = None, details: Optional[str] = ""):
    global _audit_failures
    if not username:
        return
    details_text = "" if details is None else str(details)
    if len(details_text) > 1000:
        details_text = details_text[:1000] + "..."
    try:
        with _get_users_db() as db:
            db.execute(
                "INSERT INTO audit_log (timestamp, username, action, bucket, details) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), username, action, bucket, details_text),
            )
            db.commit()
        _audit_failures = 0
    except Exception as e:
        _audit_failures += 1
        if _audit_failures <= 5:
            log.warning("Audit log write failed (%d): %s", _audit_failures, e)
        elif _audit_failures == 6:
            log.error("Audit log write failing repeatedly — suppressing further warnings")


# ── SQLite Object Index (per-bucket) ──────────────────────────────────────

def _current_endpoint_id():
    """Get current endpoint_id from context variable or thread-local fallback."""
    eid = _endpoint_ctx.get("default")
    if eid == "default":
        eid = getattr(_s3_context, "endpoint_id", "default") or "default"
    return eid


import re
_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')


def _validate_name(name: str, label: str = "name"):
    """Validate bucket/endpoint names to prevent path traversal."""
    if not name or not _SAFE_NAME_RE.match(name) or ".." in name:
        raise HTTPException(400, f"Invalid {label}: {name!r}")
    return name


def _db_path(bucket, endpoint_id=None):
    eid = endpoint_id or _current_endpoint_id()
    # Sanitize names used in file paths
    safe_bucket = bucket.replace("/", "_").replace("..", "")
    if eid and eid != "default":
        safe_eid = eid.replace("/", "_").replace("..", "")
        path = os.path.join(DB_DIR, f"{safe_eid}_{safe_bucket}.db")
    else:
        path = os.path.join(DB_DIR, f"{safe_bucket}.db")
    # Verify the resolved path is inside DB_DIR
    real_path = os.path.realpath(path)
    real_db_dir = os.path.realpath(DB_DIR)
    if not real_path.startswith(real_db_dir + os.sep) and real_path != real_db_dir:
        raise HTTPException(400, f"Invalid bucket name: path traversal detected")
    return path


def _init_db(bucket, endpoint_id=None):
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_db_path(bucket, endpoint_id), timeout=30)
    conn.execute("PRAGMA page_size = 8192")          # larger pages → shallower B-trees (new DBs only)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 30000")      # wait up to 30s for locks (heavy parallel crawl contends)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size = -64000")       # 64MB page cache (default 2MB)
    conn.execute("PRAGMA mmap_size = 268435456")     # 256MB memory-mapped I/O
    conn.execute("PRAGMA temp_store = MEMORY")       # temp tables in RAM
    conn.execute("PRAGMA wal_autocheckpoint = 2000") # checkpoint ~every 16MB of WAL
    conn.execute("""
        CREATE TABLE IF NOT EXISTS objects (
            key TEXT PRIMARY KEY,
            size INTEGER,
            last_modified TEXT,
            etag TEXT,
            prefix TEXT,
            depth INTEGER,
            crawl_gen INTEGER DEFAULT 0
        )
    """)
    # Migration: add crawl_gen column to existing databases
    try:
        conn.execute("ALTER TABLE objects ADD COLUMN crawl_gen INTEGER DEFAULT 0")
    except Exception:
        pass  # Column already exists
    # Covering index: serves WHERE prefix=? and prefix-range scans, supplies
    # key/size/last_modified without heap lookups, and is pre-sorted by key so
    # ORDER BY key needs no temp B-tree. Supersedes the old idx_prefix(prefix).
    # Detect an upgrade-in-place from an older build: a pre-existing DB that still
    # has the legacy idx_prefix and not yet the covering index. Logged once per
    # bucket so operators can see the one-time migration happen on first startup.
    _upgrading = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_prefix'").fetchone()) and not bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_prefix_cover'").fetchone())
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prefix_cover ON objects(prefix, key, size, last_modified)")
    conn.execute("DROP INDEX IF EXISTS idx_prefix")
    if _upgrading:
        try:
            _n = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        except Exception:
            _n = 0
        log.info("[%s] Index migrated from older build: built covering idx_prefix_cover over %s objects, "
                 "dropped legacy idx_prefix (existing index reused, no re-crawl needed)", bucket, f"{_n:,}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_depth ON objects(depth)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_modified ON objects(last_modified)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crawl_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_crawl_start TEXT,
            last_crawl_end TEXT,
            total_objects INTEGER,
            total_size INTEGER,
            status TEXT,
            current_crawl_gen INTEGER DEFAULT 0
        )
    """)
    # Migration: add current_crawl_gen column to existing databases
    try:
        conn.execute("ALTER TABLE crawl_status ADD COLUMN current_crawl_gen INTEGER DEFAULT 0")
    except Exception:
        pass  # Column already exists
    # Migration: persist last full-crawl duration so the scheduler can classify a
    # bucket (small vs large) immediately after a restart, without re-crawling it.
    try:
        conn.execute("ALTER TABLE crawl_status ADD COLUMN crawl_duration REAL DEFAULT 0")
    except Exception:
        pass  # Column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovered_prefixes (
            prefix TEXT PRIMARY KEY
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS version_scan_cache (
            prefix TEXT PRIMARY KEY,
            versions_count INTEGER DEFAULT 0,
            delete_markers_count INTEGER DEFAULT 0,
            total_size INTEGER DEFAULT 0,
            keys_count INTEGER DEFAULT 0,
            latest_modified TEXT,
            has_current_objects INTEGER DEFAULT 0,
            scanned_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS storage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            prefix TEXT NOT NULL DEFAULT '',
            object_count INTEGER NOT NULL,
            total_size INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sh_prefix ON storage_history(prefix)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sh_ts ON storage_history(timestamp)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS folder_stats (
            prefix TEXT PRIMARY KEY,
            object_count INTEGER DEFAULT 0,
            total_size INTEGER DEFAULT 0,
            last_updated TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prefix_children (
            parent_prefix TEXT NOT NULL,
            child_prefix TEXT NOT NULL,
            child_name TEXT NOT NULL,
            object_count INTEGER DEFAULT 0,
            total_size INTEGER DEFAULT 0,
            PRIMARY KEY (parent_prefix, child_prefix)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pc_parent ON prefix_children(parent_prefix)")
    conn.execute("""
        INSERT OR IGNORE INTO crawl_status (id, status, total_objects, total_size)
        VALUES (1, 'idle', 0, 0)
    """)
    # ── FTS5 full-text search index (trigram tokenizer for substring matching) ──
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS objects_fts USING fts5(
                key,
                content='objects',
                content_rowid='rowid',
                tokenize='trigram'
            )
        """)
        conn.execute("""CREATE TRIGGER IF NOT EXISTS objects_fts_ai AFTER INSERT ON objects BEGIN
            INSERT INTO objects_fts(rowid, key) VALUES (new.rowid, new.key);
        END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS objects_fts_ad AFTER DELETE ON objects BEGIN
            INSERT INTO objects_fts(objects_fts, rowid, key) VALUES('delete', old.rowid, old.key);
        END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS objects_fts_au AFTER UPDATE ON objects BEGIN
            INSERT INTO objects_fts(objects_fts, rowid, key) VALUES('delete', old.rowid, old.key);
            INSERT INTO objects_fts(rowid, key) VALUES (new.rowid, new.key);
        END""")
        # One-time rebuild: populate FTS from existing objects data
        obj_count = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        if obj_count > 0:
            fts_count = conn.execute("SELECT COUNT(*) FROM objects_fts").fetchone()[0]
            if fts_count == 0:
                conn.execute("INSERT INTO objects_fts(objects_fts) VALUES('rebuild')")
                log.info("[%s] FTS index rebuilt for %d objects", bucket, obj_count)
    except Exception as fts_e:
        log.warning("FTS5 setup skipped (SQLite may lack FTS5 support): %s", fts_e)
    conn.commit()
    conn.close()


@contextmanager
def _get_db(bucket, endpoint_id=None):
    conn = sqlite3.connect(_db_path(bucket, endpoint_id), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")      # wait up to 30s for locks (heavy parallel crawl contends)
    conn.execute("PRAGMA synchronous = NORMAL")      # safe under WAL; fewer fsyncs on the crawl write path
    conn.execute("PRAGMA cache_size = -64000")       # 64MB page cache (default 2MB)
    conn.execute("PRAGMA mmap_size = 268435456")     # 256MB memory-mapped I/O
    conn.execute("PRAGMA temp_store = MEMORY")       # temp tables in RAM
    try:
        yield conn
    finally:
        conn.close()


def _key_prefix(key):
    idx = key.rfind("/")
    return key[:idx + 1] if idx >= 0 else ""


def _key_depth(key):
    return key.count("/")


def _prefix_upper(prefix):
    """Smallest string strictly greater than every string starting with `prefix`.

    Lets `col >= prefix AND col < _prefix_upper(prefix)` replace the non-sargable
    `col LIKE prefix || '%'`, so the query uses an index range scan instead of a
    full table scan. `prefix` is non-empty (callers handle the root case).
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _update_crawl_counters(bucket, endpoint_id=None):
    """Recompute crawl_status totals from the objects table."""
    if not os.path.exists(_db_path(bucket, endpoint_id)):
        return
    with _get_db(bucket, endpoint_id) as db:
        db.execute("""
            UPDATE crawl_status SET
                total_objects = (SELECT COUNT(*) FROM objects),
                total_size = (SELECT COALESCE(SUM(size), 0) FROM objects)
            WHERE id = 1
        """)
        db.commit()


def _rebuild_folder_stats(bucket, endpoint_id=None):
    """Rebuild folder_stats table from objects table after a crawl."""
    if not os.path.exists(_db_path(bucket, endpoint_id)):
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _get_db(bucket, endpoint_id) as db:
        db.execute("DELETE FROM folder_stats")
        db.execute("""
            INSERT INTO folder_stats (prefix, object_count, total_size, last_updated)
            SELECT SUBSTR(key, 1, INSTR(key, '/')) as folder_prefix,
                   COUNT(*) as cnt, COALESCE(SUM(size),0) as sz, ?
            FROM objects WHERE INSTR(key, '/') > 0
            GROUP BY folder_prefix
        """, (ts,))
        # Also store root-level files stats (prefix = '')
        root = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects WHERE INSTR(key, '/') = 0"
        ).fetchone()
        if root[0] > 0:
            db.execute(
                "INSERT OR REPLACE INTO folder_stats (prefix, object_count, total_size, last_updated) VALUES (?,?,?,?)",
                ("", root[0], root[1], ts))
        db.commit()


def _adjust_folder_stats(db, key, size_delta, count_delta):
    """Incrementally adjust folder_stats for a key mutation (upload/delete/copy/rename)."""
    folder_prefix = _key_prefix(key)
    # For top-level folder stats, use the first path component
    top = key[:key.index('/') + 1] if '/' in key else ""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("""
        INSERT INTO folder_stats (prefix, object_count, total_size, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(prefix) DO UPDATE SET
            object_count = object_count + ?,
            total_size = total_size + ?,
            last_updated = ?
    """, (top, max(0, count_delta), max(0, size_delta), ts, count_delta, size_delta, ts))


def _rebuild_prefix_children(bucket, endpoint_id=None):
    """Rebuild prefix_children table from objects after a crawl.

    Uses SQL-only aggregation to avoid loading data into Python memory.
    This works for buckets of any size (tested up to 50M+ objects).
    Level 1 (top-level folders) is always built. Level 2+ uses the
    existing DISTINCT fallback in the listing code for on-demand resolution.
    """
    eid = endpoint_id or "default"
    if not os.path.exists(_db_path(bucket, eid)):
        return
    t0 = time.monotonic()
    with _get_db(bucket, eid) as db:
        db.execute("DELETE FROM prefix_children")

        # Level 1: top-level folder stats (parent = "", child = first path component)
        # The 'prefix' column stores the FULL parent path (e.g. "a/b/c/d/"),
        # so we extract the first component using SUBSTR(key, 1, INSTR(key, '/')).
        db.execute("""
            INSERT INTO prefix_children (parent_prefix, child_prefix, child_name, object_count, total_size)
            SELECT '',
                   SUBSTR(key, 1, INSTR(key, '/')) as child_prefix,
                   SUBSTR(key, 1, INSTR(key, '/') - 1) as child_name,
                   COUNT(*), COALESCE(SUM(size), 0)
            FROM objects WHERE INSTR(key, '/') > 0
            GROUP BY child_prefix
        """)

        mapping_count = db.execute("SELECT COUNT(*) FROM prefix_children").fetchone()[0]
        db.commit()

    log.info("[perf] _rebuild_prefix_children: %.3fs (%d level-1 mappings) bucket=%s",
             time.monotonic() - t0, mapping_count, bucket)


def _adjust_prefix_children(db, key, size_delta, count_delta):
    """Incrementally adjust prefix_children for a key mutation."""
    prefix = _key_prefix(key)
    if not prefix:
        return
    stripped = prefix.rstrip("/")
    last_slash = stripped.rfind("/")
    if last_slash >= 0:
        parent = stripped[:last_slash + 1]
        name = stripped[last_slash + 1:]
    else:
        parent = ""
        name = stripped

    db.execute("""
        INSERT INTO prefix_children (parent_prefix, child_prefix, child_name, object_count, total_size)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(parent_prefix, child_prefix) DO UPDATE SET
            object_count = MAX(0, object_count + ?),
            total_size = MAX(0, total_size + ?)
    """, (parent, prefix, name, max(0, count_delta), max(0, size_delta), count_delta, size_delta))


def _record_storage_snapshot(bucket, endpoint_id=None):
    """Record per-prefix storage stats into storage_history after a crawl."""
    if not os.path.exists(_db_path(bucket, endpoint_id)):
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _get_db(bucket, endpoint_id) as db:
        # Record overall bucket total
        row = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
        db.execute("INSERT INTO storage_history (timestamp, prefix, object_count, total_size) VALUES (?,?,?,?)",
                   (ts, "", row[0], row[1]))
        # Record per top-level prefix
        rows = db.execute("""
            SELECT SUBSTR(key, 1, INSTR(key, '/')) as top_prefix,
                   COUNT(*) as cnt, COALESCE(SUM(size),0) as sz
            FROM objects WHERE INSTR(key, '/') > 0
            GROUP BY top_prefix
        """).fetchall()
        for r in rows:
            if r["top_prefix"]:
                db.execute("INSERT INTO storage_history (timestamp, prefix, object_count, total_size) VALUES (?,?,?,?)",
                           (ts, r["top_prefix"], r["cnt"], r["sz"]))
        db.commit()


# ── Background Crawler (per-bucket) ──────────────────────────────────────
_crawl_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="crawler")
_rebuild_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rebuild")
_crawling = {}       # crawl_key -> timestamp when crawl started
_rebuilding = set()  # crawl_keys whose post-crawl rebuild is in progress (blocks a colliding recrawl)
_crawl_lock = threading.Lock()
_CRAWL_MAX_DURATION = 7200  # 2 hours — if a crawl exceeds this, force-release the lock


def _crawl_prefix(bucket, prefix, max_retries=3, endpoint_id=None, batch_callback=None, batch_size=10000):
    """List all objects under a specific prefix with retry logic.

    If batch_callback is provided, calls it with each batch of tuples during S3 pagination
    instead of accumulating them in memory. Returns total_count.
    If batch_callback is None, returns objects_list for backward compat.
    """
    eid = endpoint_id or _current_endpoint_id()
    client = _s3_manager.get_client(eid)
    objects = [] if batch_callback is None else None
    batch = []
    total_count = 0
    token = None
    retries = 0
    while True:
        params = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            params["ContinuationToken"] = token
        try:
            resp = client.list_objects_v2(**params)
            retries = 0
        except Exception as e:
            retries += 1
            if retries <= max_retries:
                wait = min(2 ** retries, 30)
                log.warning("[%s] Prefix '%s' retry %d/%d (waiting %ds): %s",
                            bucket, prefix[:40], retries, max_retries, wait, e)
                time.sleep(wait)
                continue
            else:
                log.error("[%s] Prefix '%s' failed after %d retries", bucket, prefix[:40], max_retries)
                if batch_callback is not None and batch:
                    batch_callback(batch)
                return total_count if batch_callback is not None else objects

        for obj in resp.get("Contents", []):
            key = obj["Key"]
            row = (key, obj["Size"], obj["LastModified"].isoformat(),
                   obj.get("ETag", "").strip('"'), _key_prefix(key), _key_depth(key))
            total_count += 1
            if batch_callback is not None:
                batch.append(row)
                if len(batch) >= batch_size:
                    batch_callback(batch)
                    batch = []
            else:
                objects.append(row)

        if not resp.get("IsTruncated", False):
            break
        token = resp.get("NextContinuationToken")

    if batch_callback is not None:
        if batch:
            batch_callback(batch)
        return total_count
    return objects


def _disable_fts_triggers(db):
    """Disable FTS sync triggers during bulk operations (crawl) for performance."""
    try:
        db.execute("DROP TRIGGER IF EXISTS objects_fts_ai")
        db.execute("DROP TRIGGER IF EXISTS objects_fts_ad")
        db.execute("DROP TRIGGER IF EXISTS objects_fts_au")
        db.commit()
    except Exception:
        pass


def _enable_fts_triggers(db):
    """Re-enable FTS sync triggers (without blocking rebuild)."""
    try:
        db.execute("""CREATE TRIGGER IF NOT EXISTS objects_fts_ai AFTER INSERT ON objects BEGIN
            INSERT INTO objects_fts(rowid, key) VALUES (new.rowid, new.key);
        END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS objects_fts_ad AFTER DELETE ON objects BEGIN
            INSERT INTO objects_fts(objects_fts, rowid, key) VALUES('delete', old.rowid, old.key);
        END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS objects_fts_au AFTER UPDATE ON objects BEGIN
            INSERT INTO objects_fts(objects_fts, rowid, key) VALUES('delete', old.rowid, old.key);
            INSERT INTO objects_fts(rowid, key) VALUES (new.rowid, new.key);
        END""")
        db.commit()
    except Exception as e:
        log.warning("FTS trigger re-enable failed: %s", e)


def _rebuild_fts_async(bucket, endpoint_id=None):
    """Rebuild FTS index in a background thread so crawl completion is not blocked.

    During the rebuild, search queries still work — they see the pre-rebuild
    index (WAL mode guarantees readers see a consistent snapshot). After the
    rebuild commits, new search queries use the updated index.
    """
    eid = endpoint_id or "default"
    def _do_rebuild():
        t0 = time.monotonic()
        try:
            with _get_db(bucket, eid) as db:
                db.execute("INSERT INTO objects_fts(objects_fts) VALUES('rebuild')")
                db.commit()
            elapsed = time.monotonic() - t0
            log.info("[%s:%s] FTS index rebuilt in %.1fs", eid, bucket, elapsed)
        except Exception as e:
            log.warning("[%s:%s] Background FTS rebuild failed: %s", eid, bucket, e)

    thread = threading.Thread(target=_do_rebuild, name=f"fts-{bucket[:12]}", daemon=True)
    thread.start()


def _fts_should_rebuild(bucket, endpoint_id, keys_changed):
    """Decide whether the (O(all-rows)) FTS trigram rebuild is actually needed.

    FTS indexes only the object key, so it is stale only when keys were added or
    removed. Rebuild when keys changed this crawl, or — as a self-heal — when the
    FTS index is empty while objects exist (e.g. a prior rebuild failed)."""
    if keys_changed:
        return True
    try:
        with _get_db(bucket, endpoint_id) as db:
            obj = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            if obj == 0:
                return False
            fts = db.execute("SELECT COUNT(*) FROM objects_fts").fetchone()[0]
            return fts == 0
    except Exception:
        return True  # if we can't tell, rebuild to be safe


def _incremental_upsert(db, batch, gen):
    """Incremental recrawl: only INSERT/REPLACE objects that are new or changed.
    For unchanged objects (same key+size+etag), just bump crawl_gen.
    batch is a list of 6-tuples: (key, size, last_modified, etag, prefix, depth).
    """
    keys = [row[0] for row in batch]
    placeholders = ",".join("?" * len(keys))
    existing = {}
    for row in db.execute(
        f"SELECT key, size, etag FROM objects WHERE key IN ({placeholders})", keys
    ).fetchall():
        existing[row[0]] = (row[1], row[2])

    unchanged_keys = []
    changed_rows = []
    for row in batch:
        key, size, last_modified, etag, prefix, depth = row
        prev = existing.get(key)
        if prev and prev[0] == size and prev[1] == etag:
            unchanged_keys.append(key)
        else:
            changed_rows.append((key, size, last_modified, etag, prefix, depth, gen))

    # Bulk update crawl_gen for unchanged objects
    if unchanged_keys:
        # SQLite doesn't have UPDATE ... IN for large lists, batch in chunks
        for i in range(0, len(unchanged_keys), 2000):
            chunk = unchanged_keys[i:i+2000]
            ph = ",".join("?" * len(chunk))
            db.execute(f"UPDATE objects SET crawl_gen=? WHERE key IN ({ph})", [gen] + chunk)

    # Full insert for new/changed objects
    if changed_rows:
        db.executemany(
            "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
            changed_rows)


class _CrawlDone(Exception):
    """Sentinel to exit the simple-crawl path into the finally block."""
    pass

def _run_crawl(bucket, endpoint_id=None):
    """Prefix-parallel crawl — discovers top-level prefixes, crawls each independently."""
    eid = endpoint_id or "default"
    crawl_key = f"{eid}:{bucket}"
    # _crawling[crawl_key] is already True (set by _queue_crawl)

    # Set thread-local so _db_path picks up the right endpoint
    _s3_context.endpoint_id = eid
    client = _s3_manager.get_client(eid)

    _init_db(bucket, eid)

    with _get_db(bucket, eid) as db:
        db.execute("UPDATE crawl_status SET status='crawling', last_crawl_start=?, current_crawl_gen=current_crawl_gen+1 WHERE id=1",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        db.commit()
        crawl_gen = db.execute("SELECT current_crawl_gen FROM crawl_status WHERE id=1").fetchone()[0]
        initial_count = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        # Disable FTS triggers during bulk crawl for performance
        _disable_fts_triggers(db)

    crawl_start = time.monotonic()
    crawl_ok = False
    fts_needs_rebuild = True  # safe default; refined below once we know what changed
    stale_count = 0
    try:
        # Get known top-level prefixes from existing index
        known_prefixes = set()
        existing_count = 0
        with _get_db(bucket, eid) as db:
            existing_count = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            if existing_count > 0:
                rows = db.execute("""
                    SELECT DISTINCT SUBSTR(key, 1, INSTR(key, '/'))
                    FROM objects WHERE INSTR(key, '/') > 0
                """).fetchall()
                for (p,) in rows:
                    known_prefixes.add(p)

        # Load previously discovered prefixes from DB
        with _get_db(bucket, eid) as db:
            saved = db.execute("SELECT prefix FROM discovered_prefixes").fetchall()
            for (p,) in saved:
                known_prefixes.add(p)

        # Discover top-level prefixes from S3
        root_files = []
        try:
            token = None
            while True:
                params = {"Bucket": bucket, "Delimiter": "/", "MaxKeys": 1000}
                if token:
                    params["ContinuationToken"] = token
                resp = client.list_objects_v2(**params)
                for cp in resp.get("CommonPrefixes", []):
                    known_prefixes.add(cp["Prefix"])
                root_files.extend(resp.get("Contents", []))
                if not resp.get("IsTruncated", False):
                    break
                token = resp.get("NextContinuationToken")
                # On recrawl with saved prefixes, skip slow full pagination
                if existing_count > 0 and len(saved) > 0:
                    log.info("[%s:%s] Recrawl: using %d saved prefixes (skipping full pagination)",
                             eid, bucket, len(known_prefixes))
                    break
        except Exception as e:
            log.warning("[%s:%s] Delimiter listing failed, using known prefixes only: %s", eid, bucket, e)
            root_files = []

        # Save all discovered prefixes to DB for future recrawls
        if known_prefixes:
            with _get_db(bucket, eid) as db:
                db.executemany("INSERT OR IGNORE INTO discovered_prefixes (prefix) VALUES (?)",
                               [(p,) for p in known_prefixes])
                db.commit()

        # Recursive sub-prefix splitting: if we have very few top-level prefixes
        # but many objects, drill one level deeper for better parallelism.
        # e.g. druid/ → druid/segments/, druid/indexing-logs/, druid/msq-intermediate/
        if known_prefixes and len(known_prefixes) <= 3 and existing_count > 500_000:
            expanded = set()
            for p in list(known_prefixes):
                try:
                    sub_token = None
                    sub_found = set()
                    while True:
                        sub_params = {"Bucket": bucket, "Prefix": p, "Delimiter": "/", "MaxKeys": 1000}
                        if sub_token:
                            sub_params["ContinuationToken"] = sub_token
                        sub_resp = client.list_objects_v2(**sub_params)
                        for cp in sub_resp.get("CommonPrefixes", []):
                            sub_found.add(cp["Prefix"])
                        if not sub_resp.get("IsTruncated", False):
                            break
                        sub_token = sub_resp.get("NextContinuationToken")
                    if sub_found:
                        expanded.update(sub_found)
                        log.info("[%s:%s] Sub-prefix split '%s' → %d children",
                                 eid, bucket, p, len(sub_found))
                    else:
                        expanded.add(p)  # Keep the original if no children
                except Exception as e:
                    expanded.add(p)  # Keep original on error
                    log.warning("[%s:%s] Sub-prefix discovery failed for '%s': %s", eid, bucket, p, e)
            if len(expanded) > len(known_prefixes):
                log.info("[%s:%s] Expanded %d prefixes → %d sub-prefixes for better parallelism",
                         eid, bucket, len(known_prefixes), len(expanded))
                known_prefixes = expanded

        if not known_prefixes and not root_files:
            # Small bucket — just do a simple full list with streaming inserts
            log.info("Simple crawl for bucket %s (endpoint=%s)", bucket, eid)

            def _simple_batch_cb(batch):
                with _get_db(bucket, eid) as db:
                    if existing_count > 0:
                        _incremental_upsert(db, batch, crawl_gen)
                    else:
                        db.executemany(
                            "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
                            [row + (crawl_gen,) for row in batch])
                    db.commit()

            total_count = _crawl_prefix(bucket, "", endpoint_id=eid, batch_callback=_simple_batch_cb)
            with _get_db(bucket, eid) as db:
                # Remove stale keys: anything with crawl_gen > 0 but older than current gen
                stale_count = db.execute("SELECT COUNT(*) FROM objects WHERE crawl_gen > 0 AND crawl_gen < ?", (crawl_gen,)).fetchone()[0]
                if stale_count > 0:
                    db.execute("DELETE FROM objects WHERE crawl_gen > 0 AND crawl_gen < ?", (crawl_gen,))
                    log.info("[%s:%s] Removed %s stale keys", eid, bucket, f"{stale_count:,}")
                row = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
                db.execute(
                    "UPDATE crawl_status SET status='complete', last_crawl_end=?, total_objects=?, total_size=? WHERE id=1",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ"), row[0], row[1]))
                db.commit()
            # Re-enable FTS triggers (instant)
            with _get_db(bucket, eid) as db:
                _enable_fts_triggers(db)
            elapsed = time.monotonic() - crawl_start
            log.info("[%s:%s] Crawl complete: %s objects, %.1f GB in %.1fs",
                     eid, bucket, f"{row[0]:,}", row[1] / (1024**3), elapsed)
            # Skip prefix-parallel path — jump to finally + post-crawl rebuilds
            raise _CrawlDone()

        # Prefix-parallel crawl
        incremental = existing_count > 0
        log.info("Crawl started for %s:%s (%d prefixes, %s existing, incremental=%s)",
                 eid, bucket, len(known_prefixes), f"{existing_count:,}", incremental)

        total_new = 0
        failed_prefixes = []

        # Index root-level files first
        if root_files:
            root_batch = []
            for obj in root_files:
                key = obj["Key"]
                root_batch.append((
                    key, obj["Size"], obj["LastModified"].isoformat(),
                    obj.get("ETag", "").strip('"'), _key_prefix(key), _key_depth(key), crawl_gen,
                ))
            if root_batch:
                with _get_db(bucket, eid) as db:
                    if incremental:
                        _incremental_upsert(db, [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in root_batch], crawl_gen)
                    else:
                        db.executemany(
                            "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
                            root_batch)
                    db.commit()

        def _make_prefix_batch_cb(b, e, gen):
            """Create a batch callback that inserts directly into DB from the crawl thread."""
            def _cb(batch):
                with _get_db(b, e) as db:
                    if incremental:
                        _incremental_upsert(db, batch, gen)
                    else:
                        db.executemany(
                            "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
                            [row + (gen,) for row in batch])
                    db.commit()
            return _cb

        with ThreadPoolExecutor(max_workers=16, thread_name_prefix=f"pfx-{bucket[:8]}") as pool:
            futures = {
                pool.submit(_crawl_prefix, bucket, p, endpoint_id=eid,
                            batch_callback=_make_prefix_batch_cb(bucket, eid, crawl_gen)): p
                for p in sorted(known_prefixes)
            }
            for future in futures:
                p = futures[future]
                try:
                    # Scale timeout: 900s base + 1s per 5000 objects expected
                    prefix_timeout = max(900, 900 + existing_count // 5000)
                    count = future.result(timeout=prefix_timeout)
                    total_new += count
                    log.info("[%s:%s] Prefix '%s': %s objects",
                             eid, bucket, p[:40], f"{count:,}")
                except Exception as e:
                    failed_prefixes.append(p)
                    log.warning("[%s:%s] Prefix '%s' failed: %s: %s", eid, bucket, p[:40], type(e).__name__, e)

                # Update progress after each prefix
                with _get_db(bucket, eid) as db:
                    row = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
                    db.execute("UPDATE crawl_status SET total_objects=?, total_size=? WHERE id=1", (row[0], row[1]))
                    db.commit()

        # Retry any prefixes that failed — typically transient SQLite write contention under heavy
        # parallelism ("database is locked"). Re-crawl them sequentially (no contention) so a transient
        # failure never silently drops data. Only runs when something failed, so the normal path is unchanged.
        if failed_prefixes:
            retry = list(failed_prefixes)
            failed_prefixes = []
            log.info("[%s:%s] Retrying %d failed prefix(es) sequentially", eid, bucket, len(retry))
            for p in retry:
                try:
                    count = _crawl_prefix(bucket, p, endpoint_id=eid,
                                          batch_callback=_make_prefix_batch_cb(bucket, eid, crawl_gen))
                    total_new += count
                    log.info("[%s:%s] Prefix '%s' (retry): %s objects", eid, bucket, p[:40], f"{count:,}")
                except Exception as e:
                    failed_prefixes.append(p)
                    log.warning("[%s:%s] Prefix '%s' failed on retry: %s: %s", eid, bucket, p[:40], type(e).__name__, e)
            with _get_db(bucket, eid) as db:
                row = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
                db.execute("UPDATE crawl_status SET total_objects=?, total_size=? WHERE id=1", (row[0], row[1]))
                db.commit()

        # Remove stale keys (crawl_gen older than this gen) — but ONLY if every prefix crawled
        # successfully. A still-failed prefix's keys carry the old gen; pruning them would turn a
        # transient list failure into real data loss. Skip the prune this cycle to keep the index intact.
        stale_count = 0
        if not failed_prefixes:
            with _get_db(bucket, eid) as db:
                stale_count = db.execute("SELECT COUNT(*) FROM objects WHERE crawl_gen > 0 AND crawl_gen < ?", (crawl_gen,)).fetchone()[0]
                if stale_count > 0:
                    db.execute("DELETE FROM objects WHERE crawl_gen > 0 AND crawl_gen < ?", (crawl_gen,))
                    db.commit()
                    log.info("[%s:%s] Removed %s stale keys", eid, bucket, f"{stale_count:,}")
        else:
            log.warning("[%s:%s] Skipping stale-key prune — %d prefix(es) still failed after retry; index kept intact",
                        eid, bucket, len(failed_prefixes))

        # Final counts
        with _get_db(bucket, eid) as db:
            row = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
            total_objects, total_size = row[0], row[1]
            db.execute(
                "UPDATE crawl_status SET status='complete', last_crawl_end=?, total_objects=?, total_size=? WHERE id=1",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ"), total_objects, total_size))
            db.commit()
            # FTS indexes the key only, so it is stale only when keys were ADDED or
            # DELETED — not when size/etag changed. Skip the O(all-rows) trigram
            # rebuild entirely when no keys changed this crawl (the common recrawl case).
            added = total_objects - initial_count + stale_count
            fts_needs_rebuild = (added > 0) or (stale_count > 0)

        elapsed = time.monotonic() - crawl_start
        msg = f"[{eid}:{bucket}] Crawl complete: {total_objects:,} objects, {total_size / (1024**3):.1f} GB in {elapsed:.1f}s"
        if failed_prefixes:
            msg += f", {len(failed_prefixes)} prefixes failed"
        log.info(msg)
        # Re-enable FTS triggers (instant)
        with _get_db(bucket, eid) as db:
            _enable_fts_triggers(db)
        crawl_ok = True

    except _CrawlDone:
        crawl_ok = True
    except BaseException as e:
        crawl_ok = False
        log.error("[%s:%s] Crawl error: %s\n%s", eid, bucket, e, traceback.format_exc())
        try:
            with _get_db(bucket, eid) as db:
                _enable_fts_triggers(db)  # Always re-enable even on error
                db.execute("UPDATE crawl_status SET status=? WHERE id=1", (f"error: {e}",))
                db.commit()
        except Exception as inner_e:
            log.warning("Failed to write crawl error status for %s: %s", bucket, inner_e)
    finally:
        # Release crawl lock BEFORE post-crawl rebuilds so the bucket can be
        # re-queued even if rebuilds hang or crash on very large buckets. But mark
        # the bucket as "rebuilding" so a recrawl can't start mid-rebuild and
        # collide on the single SQLite writer (which previously aborted the
        # rebuild with "database is locked", leaving folder_stats empty).
        with _crawl_lock:
            _crawling.pop(crawl_key, None)
            if crawl_ok:
                _rebuilding.add(crawl_key)
                # Record full-crawl timing so the scheduler can decide between a
                # cheap full recrawl (small buckets) and fast delta crawls (large).
                m = _crawl_meta.setdefault(crawl_key, {})
                m["last_full"] = time.time()
                m["duration"] = time.monotonic() - crawl_start
                try:
                    with _get_db(bucket, eid) as _db:
                        _db.execute("UPDATE crawl_status SET crawl_duration=? WHERE id=1", (m["duration"],))
                        _db.commit()
                except Exception:
                    pass

    # Post-crawl rebuilds run on a SEPARATE thread pool so they don't consume
    # crawl pool capacity. This prevents large-bucket rebuilds from starving
    # the crawl pool and blocking recrawls for other buckets. Each step runs
    # independently so a transient failure in one doesn't skip the others.
    if crawl_ok:
        def _do_rebuilds():
            try:
                # Run the fast metadata rebuilds FIRST and grab the writer quickly.
                # The FTS trigram rebuild can hold the single SQLite writer for tens
                # of seconds on a large bucket; if it ran first (in its background
                # thread) these would block past busy_timeout and fail with
                # "database is locked", leaving folder_stats/prefix_children empty.
                for step in (_record_storage_snapshot, _rebuild_folder_stats, _rebuild_prefix_children):
                    try:
                        step(bucket, eid)
                    except Exception as step_e:
                        log.warning("[%s:%s] Post-crawl %s failed (non-fatal): %s",
                                    eid, bucket, step.__name__, step_e)
                # FTS rebuild LAST, and only when keys actually changed.
                if _fts_should_rebuild(bucket, eid, fts_needs_rebuild):
                    _rebuild_fts_async(bucket, eid)
                else:
                    log.info("[%s:%s] FTS rebuild skipped — no key changes this crawl", eid, bucket)
            finally:
                with _crawl_lock:
                    _rebuilding.discard(crawl_key)
        _rebuild_pool.submit(_do_rebuilds)


_version_scanning = {}  # bucket -> bool
_version_scan_lock = threading.Lock()

# ── Background Purge Tasks ────────────────────────────────────────────────
_purge_tasks = {}        # task_id -> {status, purged, errors, detail, ...}
_purge_tasks_lock = threading.Lock()
_PURGE_TASK_TTL = 600    # Keep completed task results for 10 minutes


def _purge_task_set(task_id, **kwargs):
    """Thread-safe update of a purge task's state."""
    with _purge_tasks_lock:
        if task_id in _purge_tasks:
            _purge_tasks[task_id].update(kwargs)


def _purge_task_cleanup():
    """Remove completed tasks older than TTL."""
    now = time.time()
    with _purge_tasks_lock:
        expired = [tid for tid, t in _purge_tasks.items()
                   if t.get("status") in ("complete", "error") and now - t.get("finished_at", now) > _PURGE_TASK_TTL]
        for tid in expired:
            del _purge_tasks[tid]


def _run_purge(task_id, bucket, keys, target_prefix, username, endpoint_id=None):
    """Background worker: collect versions from S3, batch-delete, clean index."""
    eid = endpoint_id or "default"
    crawl_key = f"{eid}:{bucket}"
    _s3_context.endpoint_id = eid
    client = _s3_manager.get_client(eid)

    # Block recrawl from running on this bucket while we purge
    with _crawl_lock:
        _crawling[crawl_key] = True

    try:
        # Phase 1: Collect all version+marker entries
        to_delete = []
        if keys:
            for key in keys:
                try:
                    resp = client.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=1000)
                    for v in resp.get("Versions", []):
                        if v["Key"] == key:
                            to_delete.append({"Key": key, "VersionId": v["VersionId"]})
                    for d in resp.get("DeleteMarkers", []):
                        if d["Key"] == key:
                            to_delete.append({"Key": key, "VersionId": d["VersionId"]})
                except Exception as e:
                    log.error("Purge task %s: failed to list versions for key %s: %s", task_id, key, e)
            _purge_task_set(task_id, detail=f"Found {len(to_delete)} versions for {len(keys)} keys")
        elif target_prefix:
            key_marker = None
            version_marker = None
            while True:
                params = {"Bucket": bucket, "Prefix": target_prefix, "MaxKeys": 1000}
                if key_marker:
                    params["KeyMarker"] = key_marker
                    if version_marker:
                        params["VersionIdMarker"] = version_marker
                try:
                    resp = client.list_object_versions(**params)
                except Exception as e:
                    log.error("Purge task %s: S3 list_object_versions failed: %s", task_id, e)
                    _purge_task_set(task_id, status="error", detail=f"S3 error: {e}",
                                   finished_at=time.time())
                    return
                for v in resp.get("Versions", []):
                    to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
                for d in resp.get("DeleteMarkers", []):
                    to_delete.append({"Key": d["Key"], "VersionId": d["VersionId"]})
                _purge_task_set(task_id, detail=f"Collecting versions... {len(to_delete)} found")
                if not resp.get("IsTruncated", False):
                    break
                key_marker = resp.get("NextKeyMarker")
                version_marker = resp.get("NextVersionIdMarker")

        if not to_delete:
            _purge_cleanup_index(bucket, target_prefix, keys, endpoint_id)
            _audit("purge_versions", username, bucket=bucket,
                   details=f"{'keys=' + str(len(keys)) if keys else 'prefix=' + target_prefix}, purged=0 (cleaned index)")
            log.info("Purge task %s: no S3 objects found, cleaned index", task_id)
            _purge_task_set(task_id, status="complete", purged=0, errors=0,
                           detail="No versioned data found (index cleaned)",
                           finished_at=time.time())
            return

        # Phase 2: Delete in batches of 1000
        log.info("Purge task %s: deleting %d version entries", task_id, len(to_delete))
        total_purged = 0
        total_errors = 0
        total = len(to_delete)
        for i in range(0, total, 1000):
            batch = to_delete[i:i + 1000]
            try:
                resp = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                batch_errors = len(resp.get("Errors", []))
                total_errors += batch_errors
                total_purged += len(batch) - batch_errors
            except Exception as e:
                log.error("Purge task %s: delete_objects failed on batch %d: %s", task_id, i // 1000, e)
                total_errors += len(batch)
            _purge_task_set(task_id, purged=total_purged, errors=total_errors,
                           detail=f"Deleting... {total_purged}/{total}")

        # Phase 3: Clean up index
        _purge_cleanup_index(bucket, target_prefix, keys, endpoint_id)

        details = f"keys={len(keys)}" if keys else f"prefix={target_prefix}"
        _audit("purge_versions", username, bucket=bucket, details=f"{details}, purged={total_purged}")
        log.info("Purge task %s complete: %d deleted, %d errors", task_id, total_purged, total_errors)
        _purge_task_set(task_id, status="complete", purged=total_purged, errors=total_errors,
                       detail=f"Purged {total_purged} versions" + (f" ({total_errors} errors)" if total_errors else ""),
                       finished_at=time.time())
    except Exception as e:
        log.error("Purge task %s failed: %s\n%s", task_id, e, traceback.format_exc())
        _purge_task_set(task_id, status="error", detail=f"Unexpected error: {e}",
                       finished_at=time.time())
    finally:
        # Release crawl lock so recrawl can proceed
        with _crawl_lock:
            _crawling.pop(crawl_key, None)


def _purge_cleanup_index(bucket, target_prefix, keys, endpoint_id=None):
    """Clean up index tables after purge."""
    if not os.path.exists(_db_path(bucket, endpoint_id)):
        return
    with _get_db(bucket, endpoint_id) as db:
        if keys:
            db.executemany("DELETE FROM objects WHERE key=?", [(k,) for k in keys])
        elif target_prefix:
            db.execute("DELETE FROM objects WHERE key LIKE ?", (target_prefix + "%",))
            db.execute("DELETE FROM discovered_prefixes WHERE prefix = ?", (target_prefix,))
            db.execute("DELETE FROM discovered_prefixes WHERE prefix LIKE ?", (target_prefix + "%",))
            db.execute("DELETE FROM version_scan_cache WHERE prefix = ?", (target_prefix,))
            db.execute("DELETE FROM version_scan_cache WHERE prefix LIKE ?", (target_prefix + "%",))
        db.commit()
    _update_crawl_counters(bucket, endpoint_id)


def _scan_versioned_prefixes(bucket, endpoint_id=None):
    """Background scan: discover all top-level prefixes with version history.
    Uses list_object_versions with Delimiter to find folder-level entries,
    then stores results in version_scan_cache table."""
    eid = endpoint_id or _current_endpoint_id()
    with _version_scan_lock:
        if _version_scanning.get(bucket):
            return
        _version_scanning[bucket] = True

    client = _s3_manager.get_client(eid)

    try:
        log.info(f"[{bucket}] Version scan started")
        scan_start = time.monotonic()

        # Step 1: Paginate list_object_versions with Delimiter='/' to find all versioned prefixes
        all_prefixes = set()
        key_marker = None
        version_marker = None
        pages = 0
        max_pages = 50  # Safety limit

        while pages < max_pages:
            params = {"Bucket": bucket, "Prefix": "", "Delimiter": "/", "MaxKeys": 1000}
            if key_marker:
                params["KeyMarker"] = key_marker
                if version_marker:
                    params["VersionIdMarker"] = version_marker
            try:
                resp = client.list_object_versions(**params)
            except Exception as e:
                log.error(f"[{bucket}] Version scan S3 error: {e}")
                break
            pages += 1

            # Collect CommonPrefixes (these are folders with version data)
            for cp in resp.get("CommonPrefixes", []):
                all_prefixes.add(cp["Prefix"])

            # Also collect direct file keys (root-level versioned files)
            for v in resp.get("Versions", []):
                if "/" not in v["Key"]:
                    all_prefixes.add(v["Key"])
            for d in resp.get("DeleteMarkers", []):
                if "/" not in d["Key"]:
                    all_prefixes.add(d["Key"])

            if not resp.get("IsTruncated", False):
                break
            key_marker = resp.get("NextKeyMarker")
            version_marker = resp.get("NextVersionIdMarker")

        log.info(f"[{bucket}] Version scan found {len(all_prefixes)} prefixes in {pages} pages")

        # Step 2: For each prefix, get a summary (parallelized)
        # Also check if it has current objects
        import concurrent.futures

        def scan_one(pfx):
            is_folder = pfx.endswith("/")
            try:
                r = client.list_object_versions(Bucket=bucket, Prefix=pfx, MaxKeys=1000)
                versions = r.get("Versions", [])
                markers = r.get("DeleteMarkers", [])
                total_size = sum(v.get("Size", 0) for v in versions)
                keys = set(v["Key"] for v in versions) | set(d["Key"] for d in markers)
                lm = None
                for v in versions:
                    v_lm = v["LastModified"].isoformat()
                    if not lm or v_lm > lm:
                        lm = v_lm
                for d in markers:
                    d_lm = d["LastModified"].isoformat()
                    if not lm or d_lm > lm:
                        lm = d_lm
                # Check if prefix has current (non-deleted) objects
                has_current = 0
                if is_folder:
                    try:
                        cr = client.list_objects_v2(Bucket=bucket, Prefix=pfx, MaxKeys=1)
                        has_current = 1 if cr.get("KeyCount", 0) > 0 else 0
                    except Exception as cur_e:
                        log.debug("list_objects_v2 check for %s failed: %s", pfx, cur_e)
                return (pfx, len(versions), len(markers), total_size, len(keys), lm, has_current)
            except Exception as scan_e:
                log.warning("Version scan error for prefix %s: %s", pfx, scan_e)
                return None

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        results = []
        folder_prefixes = [p for p in all_prefixes if p.endswith("/")]
        if folder_prefixes:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(folder_prefixes))) as pool:
                results = [r for r in pool.map(scan_one, folder_prefixes) if r is not None]

        # Step 3: Store in version_scan_cache
        with _get_db(bucket, eid) as db:
            db.execute("DELETE FROM version_scan_cache")
            for pfx, ver_count, dm_count, total_size, keys_count, lm, has_current in results:
                db.execute("""
                    INSERT OR REPLACE INTO version_scan_cache
                    (prefix, versions_count, delete_markers_count, total_size, keys_count, latest_modified, has_current_objects, scanned_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (pfx, ver_count, dm_count, total_size, keys_count, lm, has_current, now))
            # Always write a sentinel row so the cache is marked as "fresh" even with 0 results
            if not results:
                db.execute("""
                    INSERT INTO version_scan_cache
                    (prefix, versions_count, delete_markers_count, total_size, keys_count, latest_modified, has_current_objects, scanned_at)
                    VALUES ('__scan_marker__', 0, 0, 0, 0, NULL, 1, ?)
                """, (now,))
            db.commit()

        elapsed = time.monotonic() - scan_start
        log.info(f"[{bucket}] Version scan complete: {len(results)} versioned prefixes in {elapsed:.1f}s")

    except Exception as e:
        log.error(f"[{bucket}] Version scan error: {e}\n{traceback.format_exc()}")
    finally:
        with _version_scan_lock:
            _version_scanning[bucket] = False


def _is_index_ready(bucket):
    if not os.path.exists(_db_path(bucket)):
        return False
    with _get_db(bucket) as db:
        row = db.execute("SELECT status, total_objects FROM crawl_status WHERE id=1").fetchone()
        if row and (row["status"] == "complete" or (row["total_objects"] or 0) > 0):
            return True
        # Source of truth is the objects table. crawl_status counters can be
        # transiently 0/NULL during a crawl transition — never let that make a
        # populated index look "not ready" (which would fall queries back to slow
        # S3 listing). If any object is indexed, the index is usable.
        try:
            return db.execute("SELECT 1 FROM objects LIMIT 1").fetchone() is not None
        except Exception:
            return False


RECRAWL_INTERVAL = int(os.environ.get("RECRAWL_INTERVAL", "120"))         # how often to check each bucket for fresh data
FULL_CRAWL_INTERVAL = int(os.environ.get("FULL_CRAWL_INTERVAL", "3600"))  # full reconcile cadence for large buckets (deletions/cold changes)
LARGE_BUCKET_SECONDS = int(os.environ.get("LARGE_BUCKET_SECONDS", "60"))  # if a full crawl took longer than this, keep fresh via delta crawls
DELTA_SAMPLE = int(os.environ.get("DELTA_SAMPLE", "3000"))                # how many recent objects to sample to locate hot prefixes
DELTA_MAX_TARGETS = int(os.environ.get("DELTA_MAX_TARGETS", "40"))        # cap on hot prefixes re-listed per delta crawl

# crawl_key -> {"last_full": ts, "duration": sec, "last_delta": ts}
_crawl_meta = {}


def _parent_prefix(prefix):
    """Immediate parent of a prefix: 'a/b/c/' -> 'a/b/'; 'a/' -> ''."""
    s = prefix.rstrip("/")
    i = s.rfind("/")
    return s[:i + 1] if i >= 0 else ""


def _minimal_prefixes(prefixes):
    """Drop any prefix that is already covered by a shorter prefix in the set,
    so we never re-list the same subtree twice in one delta crawl."""
    kept = []
    for p in sorted(prefixes, key=len):
        if not any(p != k and p.startswith(k) for k in kept):
            kept.append(p)
    return kept


def _hot_target_prefixes(bucket, endpoint_id, sample=None, max_targets=None):
    """The narrow prefixes where the most recently-modified objects live (uses
    idx_last_modified). These are exactly where new data is appended for
    time-partitioned data, so re-listing just these is fast. Brand-new sibling
    partitions are discovered separately in _delta_crawl via a cheap delimiter
    scan — we deliberately do NOT broaden to parents here, since a hot prefix's
    parent can be the entire dataset (e.g. sampling-GUID/metadata/ -> sampling-GUID/)."""
    sample = sample or DELTA_SAMPLE
    max_targets = max_targets or DELTA_MAX_TARGETS
    with _get_db(bucket, endpoint_id) as db:
        rows = db.execute(
            "SELECT DISTINCT prefix FROM (SELECT prefix, last_modified FROM objects "
            "WHERE prefix != '' ORDER BY last_modified DESC LIMIT ?)", (sample,)).fetchall()
    return _minimal_prefixes({p for (p,) in rows})[:max_targets]


def _delta_list_prefix(client, bucket, prefix):
    """Recursively list every object under `prefix` (no delimiter). Returns raw S3 objects."""
    objs = []
    token = None
    while True:
        params = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            params["ContinuationToken"] = token
        resp = client.list_objects_v2(**params)
        for o in resp.get("Contents", []):
            if o["Key"] != prefix:
                objs.append(o)
        if not resp.get("IsTruncated", False):
            break
        token = resp.get("NextContinuationToken")
    return objs


DELTA_BRANCH_FANOUT = int(os.environ.get("DELTA_BRANCH_FANOUT", "8"))   # descriptive levels with <= this many children → follow all
DELTA_NEWEST_K = int(os.environ.get("DELTA_NEWEST_K", "2"))             # partition levels → newest K only (current + just-rolled)
DELTA_MAX_DEPTH = int(os.environ.get("DELTA_MAX_DEPTH", "12"))          # safety cap on walk depth
DELTA_LIST_CONCURRENCY = int(os.environ.get("DELTA_LIST_CONCURRENCY", "16"))  # parallel S3 list calls per level
DELTA_MAX_NODES = int(os.environ.get("DELTA_MAX_NODES", "2000"))        # safety cap on folders visited per delta


def _natural_key(prefix):
    """Natural sort key so the NEWEST partition is picked correctly even for
    non-zero-padded numeric names: day=2 < day=10, hour=9 < hour=10 (a plain
    lexicographic sort would order '10' before '2' and '9'). Numbers sort before
    text and compare numerically; the (0,int)/(1,str) tags avoid int-vs-str errors
    when siblings have slightly different shapes."""
    s = prefix.rstrip("/")
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", s) if t]


def _list_children(client, bucket, prefix):
    """Immediate child 'folders' of prefix (delimiter list, fully paginated)."""
    out, token = [], None
    while True:
        params = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            params["ContinuationToken"] = token
        resp = client.list_objects_v2(**params)
        out.extend(cp["Prefix"] for cp in resp.get("CommonPrefixes", []))
        if not resp.get("IsTruncated", False):
            break
        token = resp.get("NextContinuationToken")
    return out


def _discover_delta_targets(client, bucket, endpoint_id, tops):
    """Find where new data is, across ALL datasets, without re-listing the bucket.
    Breadth-first from the top-level datasets: each level's folders are listed in
    PARALLEL (hiding the per-call S3 latency), while the fast local index checks run
    on this thread. At each level: digit-named / very-wide levels are time PARTITIONS
    (follow only the newest K = current + just-rolled); descriptive levels are
    BRANCHES (data/, metadata/, aggregation types) → follow all. Collect leaf
    partitions (re-listed for appended files) + any partition not yet indexed (a
    brand-new hour folder — even when it sits beyond the first 1000 siblings and in a
    dataset that isn't the globally most-recently-modified one)."""
    if not tops:
        return set()
    targets, frontier, visited = set(), list(tops), 0
    with _get_db(bucket, endpoint_id) as db, \
         ThreadPoolExecutor(max_workers=DELTA_LIST_CONCURRENCY) as ex:
        depth = 0
        while frontier and depth < DELTA_MAX_DEPTH and visited < DELTA_MAX_NODES:
            depth += 1
            visited += len(frontier)
            listed = list(ex.map(lambda p: (p, _list_children(client, bucket, p)), frontier))
            nxt = []
            for cur, children in listed:
                if not children:
                    targets.add(cur)  # leaf: objects live directly under cur
                    continue
                # Few children → descriptive branches (data/, metadata/, model names,
                # a handful of partitions): follow ALL (cheap, never skips a sibling).
                # Many children → time/sequence partitions: follow only the newest K,
                # using a natural sort so non-zero-padded names pick the true newest.
                if len(children) > DELTA_BRANCH_FANOUT:
                    chosen = sorted(children, key=_natural_key)[-DELTA_NEWEST_K:]
                else:
                    chosen = children
                for c in chosen:
                    if not db.execute("SELECT 1 FROM objects WHERE prefix >= ? AND prefix < ? LIMIT 1",
                                      (c, _prefix_upper(c))).fetchone():
                        targets.add(c)    # brand-new partition (e.g. a fresh hour folder)
                    nxt.append(c)
            frontier = nxt
    return targets


def _delta_crawl(bucket, endpoint_id):
    """Fast incremental crawl: re-list ONLY the narrow hot prefixes (where new data
    lands) plus any brand-new sibling partitions, in PARALLEL, and incremental-upsert.
    FTS triggers stay enabled so search updates per row (no full trigram rebuild).
    Avoids re-listing the whole bucket. Returns #new/changed objects."""
    eid = endpoint_id or "default"
    client = _s3_manager.get_client(eid)
    hot = _hot_target_prefixes(bucket, eid)
    if not hot:
        return 0

    # Surface delta activity to the UI (status + last_crawl_start). The index stays
    # queryable throughout because _is_index_ready treats any bucket with
    # total_objects>0 as ready. The caller resets status/last_crawl_end when done.
    with _get_db(bucket, eid) as db:
        db.execute("UPDATE crawl_status SET status='crawling', last_crawl_start=? WHERE id=1",
                   (time.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        db.commit()

    # Find where new data is via a bounded tree walk across ALL top-level datasets
    # (folder_stats holds them). This catches a fresh hour folder even when it sits
    # beyond the first 1000 siblings AND in a dataset that isn't the globally
    # most-recently-modified one. Union with the index-derived hot prefixes (which
    # also catch backfills into a not-newest partition).
    with _get_db(bucket, eid) as db:
        tops = [r[0] for r in db.execute("SELECT prefix FROM folder_stats WHERE prefix != ''").fetchall() if r[0]]
        row = db.execute("SELECT current_crawl_gen FROM crawl_status WHERE id=1").fetchone()
        gen = (row[0] if row else 0) or 0
    try:
        discovered = _discover_delta_targets(client, bucket, eid, tops)  # opens its own per-thread connections
    except Exception as e:
        log.warning("[%s:%s] delta target discovery failed: %s", eid, bucket, e)
        discovered = set()
    targets = _minimal_prefixes(set(hot) | discovered)

    # List all targets in parallel (the hot set is small; this keeps a delta fast
    # even when several partitions are active).
    all_objs = []
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
        for objs in ex.map(lambda tp: _delta_list_prefix(client, bucket, tp), targets):
            all_objs.extend(objs)

    changed = 0
    with _get_db(bucket, eid) as db:
        for i in range(0, len(all_objs), 5000):
            chunk = all_objs[i:i + 5000]
            keys = [o["Key"] for o in chunk]
            ph = ",".join("?" * len(keys))
            have = {r[0]: (r[1], r[2]) for r in
                    db.execute(f"SELECT key,size,etag FROM objects WHERE key IN ({ph})", keys)}
            batch = []
            for o in chunk:
                k = o["Key"]; sz = o["Size"]; et = o.get("ETag", "").strip('"')
                prev = have.get(k)
                if not prev or prev[0] != sz or prev[1] != et:
                    changed += 1
                batch.append((k, sz, o["LastModified"].isoformat(), et,
                              _key_prefix(k), _key_depth(k), gen))
            db.executemany(
                "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) "
                "VALUES (?,?,?,?,?,?,?)", batch)
        db.commit()

    if changed:
        # cheap metadata refreshes; FTS already maintained incrementally by triggers
        for step in (_update_crawl_counters, _rebuild_folder_stats, _rebuild_prefix_children, _record_storage_snapshot):
            try:
                step(bucket, eid)
            except Exception as e:
                log.warning("[%s:%s] delta post-step %s failed: %s", eid, bucket, getattr(step, "__name__", step), e)
    return changed


def _queue_delta_crawl(bucket, endpoint_id=None):
    """Submit a delta crawl if the bucket isn't mid full-crawl or mid-rebuild."""
    eid = endpoint_id or "default"
    crawl_key = f"{eid}:{bucket}"
    with _crawl_lock:
        if crawl_key in _rebuilding or crawl_key in _crawling:
            return False
        _crawling[crawl_key] = time.time()  # reuse crawl lock to block colliding full crawls

    def _run():
        t0 = time.monotonic()
        try:
            _s3_context.endpoint_id = eid
            n = _delta_crawl(bucket, eid)
            with _crawl_lock:
                _crawl_meta.setdefault(crawl_key, {})["last_delta"] = time.time()
            log.info("[%s:%s] Delta crawl: %d new/changed in %.1fs", eid, bucket, n, time.monotonic() - t0)
        except Exception as e:
            log.warning("[%s:%s] Delta crawl error: %s", eid, bucket, e)
        finally:
            with _crawl_lock:
                _crawling.pop(crawl_key, None)
            # Always return status to 'complete' and stamp last_crawl_end so the UI
            # reflects that the index was just refreshed (even on a no-change delta).
            try:
                with _get_db(bucket, eid) as db:
                    db.execute("UPDATE crawl_status SET status='complete', last_crawl_end=? WHERE id=1",
                               (time.strftime("%Y-%m-%dT%H:%M:%SZ"),))
                    db.commit()
            except Exception:
                pass
    _crawl_pool.submit(_run)
    return True


def _queue_crawl(bucket, endpoint_id=None):
    """Queue a crawl for a bucket if it is not already running."""
    eid = endpoint_id or "default"
    crawl_key = f"{eid}:{bucket}"
    now = time.time()
    with _crawl_lock:
        # Don't start a crawl while this bucket's post-crawl rebuild is still
        # running — they would contend on the single SQLite writer and the
        # rebuild would lose, leaving folder_stats/prefix_children empty.
        if crawl_key in _rebuilding:
            return False
        started_at = _crawling.get(crawl_key)
        if started_at:
            # Force-release stale locks (OOM kill, thread death, etc.)
            if now - started_at > _CRAWL_MAX_DURATION:
                log.warning("Force-releasing stale crawl lock for %s (started %.0f min ago)", crawl_key, (now - started_at) / 60)
                del _crawling[crawl_key]
            else:
                return False
        _crawling[crawl_key] = now  # Store timestamp, not bool
    _crawl_pool.submit(_run_crawl, bucket, eid)
    return True


def _auto_recrawl():
    """Adaptive freshness scheduler. Each cycle, per bucket:
      • never fully crawled yet      → full crawl
      • full crawl due (cold reconcile, every FULL_CRAWL_INTERVAL) → full crawl
      • large bucket (slow full crawl) → DELTA crawl (re-list only hot prefixes) → fresh data fast
      • small bucket (fast full crawl) → full recrawl every interval (already cheap & fresh)
    This keeps high-volume buckets fresh within ~one interval without re-listing
    the whole bucket every cycle, and avoids crawl/rebuild lock collisions."""
    while True:
        try:
            time.sleep(RECRAWL_INTERVAL)
            now = time.time()
            for eid in _s3_manager.get_all_ids():
                try:
                    client = _s3_manager.get_client(eid)
                    resp = client.list_buckets()
                    for b in resp.get("Buckets", []):
                        name = b["Name"]
                        key = f"{eid}:{name}"
                        meta = _crawl_meta.get(key)
                        if not meta or "last_full" not in meta:
                            if _queue_crawl(name, eid):
                                log.info("Full crawl queued for %s (initial)", key)
                            continue
                        if (now - meta["last_full"]) > FULL_CRAWL_INTERVAL:
                            if _queue_crawl(name, eid):
                                log.info("Full recrawl queued for %s (cold reconcile)", key)
                        elif meta.get("duration", 0) > LARGE_BUCKET_SECONDS:
                            # Large bucket → keep fresh via delta crawls, but leave a
                            # cooldown of RECRAWL_INTERVAL AFTER each delta ends so it
                            # doesn't run back-to-back (the delta itself can take a while
                            # on a high-latency S3). last_delta is stamped when a delta
                            # finishes; _queue_delta_crawl also skips if one is running.
                            if (now - meta.get("last_delta", 0)) >= RECRAWL_INTERVAL:
                                if _queue_delta_crawl(name, eid):
                                    log.info("Delta crawl queued for %s (fresh data)", key)
                        else:
                            if (now - meta["last_full"]) >= RECRAWL_INTERVAL:
                                _queue_crawl(name, eid)
                except Exception as e:
                    log.error("Auto-recrawl error (endpoint=%s): %s", eid, e)
        except Exception as outer_e:
            log.error("Auto-recrawl loop error (will retry): %s\n%s", outer_e, traceback.format_exc())


@app.on_event("startup")
def startup():
    # Load all S3 endpoints from DB and register with manager (migrate plaintext → encrypted)
    try:
        with _get_users_db() as db:
            eps = db.execute("SELECT id, endpoint_url, access_key, secret_key, region, path_style FROM s3_endpoints").fetchall()
        migrated = 0
        for ep in eps:
            ak_raw, sk_raw = ep["access_key"], ep["secret_key"]
            ak, sk = _decrypt(ak_raw), _decrypt(sk_raw)
            # Migrate plaintext credentials to encrypted
            if ak_raw and not ak_raw.startswith(_ENCRYPTED_PREFIX):
                with _get_users_db() as db:
                    db.execute("UPDATE s3_endpoints SET access_key=?, secret_key=? WHERE id=?",
                               (_encrypt(ak), _encrypt(sk), ep["id"]))
                    db.commit()
                migrated += 1
            _s3_manager.register(ep["id"], ep["endpoint_url"], ak, sk,
                                 ep["region"] or "", bool(ep["path_style"]))
        if migrated:
            log.info("Migrated %d endpoint(s) to encrypted credential storage", migrated)
        log.info("Loaded %d S3 endpoints", len(eps))
    except Exception as e:
        log.error("Failed to load S3 endpoints: %s", e)

    # Auto-crawl all existing buckets on startup (runs in background so uvicorn starts immediately)
    def _startup_crawl():
        for eid in _s3_manager.get_all_ids():
            try:
                client = _s3_manager.get_client(eid)
                resp = client.list_buckets()
                for b in resp.get("Buckets", []):
                    name = b["Name"]
                    _init_db(name, eid)
                    # If this bucket already has an index from a previous run, seed the
                    # scheduler from it instead of doing a full re-crawl on every restart.
                    # Key off the OBJECTS TABLE, not crawl_status='complete' — a restart
                    # mid-crawl leaves status='crawling', and requiring 'complete' would
                    # then trigger a redundant (multi-minute) full re-crawl. The scheduler
                    # keeps it fresh via fast delta crawls and a periodic full reconcile.
                    seeded = False
                    try:
                        with _get_db(name, eid) as db:
                            row = db.execute("SELECT total_objects, crawl_duration FROM crawl_status WHERE id=1").fetchone()
                            total = (row["total_objects"] if row else 0) or 0
                            if total == 0:  # counters can lag an interrupted crawl — trust the table
                                total = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
                            dur = (row["crawl_duration"] if row else 0) or 0.0
                        if total > 0:
                            if not dur and total > 50000:  # estimate large-ness until a full crawl records a duration
                                dur = LARGE_BUCKET_SECONDS + 1
                            _crawl_meta[f"{eid}:{name}"] = {"last_full": time.time(), "duration": dur}
                            seeded = True
                            log.info("Seeded schedule for %s:%s from existing index (%s objects); will keep fresh via scheduler",
                                     eid, name, f"{total:,}")
                    except Exception:
                        pass
                    if not seeded:
                        _queue_crawl(name, eid)
                        log.info("Queued crawl for %s:%s", eid, name)
            except Exception as e:
                log.error("Failed to list buckets on startup (endpoint=%s): %s", eid, e)
    threading.Thread(target=_startup_crawl, daemon=True).start()

    # Start auto-recrawl thread
    recrawl_thread = threading.Thread(target=_auto_recrawl, daemon=True)
    recrawl_thread.start()
    log.info("Auto-recrawl enabled every %d seconds", RECRAWL_INTERVAL)

    # Start telemetry heartbeat
    if TELEMETRY:
        _tel_record_boot()  # boot_count + restart history + crash detection (prev unclean exit)
        threading.Thread(target=_telemetry_loop, daemon=True).start()
        log.info("Anonymous telemetry enabled (schema v2). Set TELEMETRY=false to disable.")
    else:
        log.info("Telemetry disabled.")


# ── Telemetry ─────────────────────────────────────────────────────────────

TELEMETRY_URL = os.environ.get("TELEMETRY_URL", "https://dashboard.sairo.dev/api/v1/ping")
TELEMETRY_INTERVAL = int(os.environ.get("TELEMETRY_INTERVAL", "3600"))  # hourly (trailing-24h metrics assume regular pings)
TELEMETRY_SCHEMA_VERSION = "2"

# ── Telemetry runtime counters (Tier 1 health + Tier 3 engagement) ──────────
# Lightweight, in-memory, and strictly FAIL-SAFE: nothing here may ever alter a
# request/response or raise into the request path. Aggregate counts only — never
# bucket names, keys, paths, or any user content.
_tel_lock = threading.Lock()
_tel_req_hours: dict = {}      # epoch_hour -> request count
_tel_failed_hours: dict = {}   # epoch_hour -> 5xx count
_tel_bucket_seen: dict = {}    # bucket -> last-access epoch (for active_buckets_24h; names never sent)
_tel_last_write = [0.0]        # epoch of the last successful write operation


def _tel_record(path: str, method: str, status: int):
    """Account one request for the trailing-24h counters. Never raises."""
    try:
        now = time.time()
        hr = int(now // 3600)
        with _tel_lock:
            _tel_req_hours[hr] = _tel_req_hours.get(hr, 0) + 1
            if status >= 500:
                _tel_failed_hours[hr] = _tel_failed_hours.get(hr, 0) + 1
            cutoff = hr - 26  # keep ~26h of hourly buckets
            for d in (_tel_req_hours, _tel_failed_hours):
                for k in [k for k in d if k < cutoff]:
                    d.pop(k, None)
            if status < 400 and path.startswith("/api/buckets/"):
                parts = path.split("/")
                if len(parts) >= 4 and parts[3]:
                    _tel_bucket_seen[parts[3]] = now
                    if method in ("POST", "PUT", "DELETE", "PATCH"):
                        _tel_last_write[0] = now
                    if len(_tel_bucket_seen) > 10000:  # bound memory
                        _tel_bucket_seen.pop(min(_tel_bucket_seen, key=_tel_bucket_seen.get), None)
    except Exception:
        pass


def _tel_sum_24h(d: dict) -> int:
    cutoff = int(time.time() // 3600) - 23
    with _tel_lock:
        return sum(v for k, v in d.items() if k >= cutoff)


def _tel_active_buckets_24h() -> int:
    cutoff = time.time() - 86400
    with _tel_lock:
        return sum(1 for ts in _tel_bucket_seen.values() if ts >= cutoff)


@app.middleware("http")
async def telemetry_counter_middleware(request: Request, call_next):
    """Outermost middleware — counts requests for anonymous telemetry. Strictly
    pass-through: it never modifies the request/response and never swallows the
    handler's exception (it re-raises after recording a failure)."""
    if not TELEMETRY:
        return await call_next(request)
    try:
        response = await call_next(request)
    except Exception:
        try:
            _tel_record(request.scope.get("path", request.url.path), request.method, 500)
        except Exception:
            pass
        raise
    try:
        _tel_record(request.scope.get("path", request.url.path), request.method, response.status_code)
    except Exception:
        pass
    return response


def _meta_get(key: str, default=None):
    try:
        with _get_users_db() as db:
            row = db.execute("SELECT value FROM instance_meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default
    except Exception:
        return default


def _meta_set(key: str, value: str):
    try:
        with _get_users_db() as db:
            db.execute("INSERT OR REPLACE INTO instance_meta (key, value) VALUES (?, ?)", (key, str(value)))
            db.commit()
    except Exception:
        pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_to_iso(e: float) -> str:
    return datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Activation milestones — recorded at request time, idempotent, fail-safe ──
# Anonymous, aggregate-only (a timestamp / boolean per instance), routed through the
# existing instance_meta + telemetry path. Used to measure time-to-first-search and
# whether the activation funnel (search / dashboard) is reached. Never raises into a request.
_recorded_milestones: set = set()


def _record_milestone_once(key: str):
    """Stamp `key` with the current time the first time it occurs. The in-memory guard keeps
    it to one DB hit per process; the _meta_get check keeps it idempotent across restarts."""
    if key in _recorded_milestones:
        return
    try:
        if not _meta_get(key):
            _meta_set(key, _iso_now())
        _recorded_milestones.add(key)
    except Exception:
        pass


def _record_first_search():
    """Activation event: the user reached search. Recorded on the search REQUEST — before the
    index-ready gate — so it is symmetric with first_dashboard_open_at (which records on view
    open). Recording it only on a served 200 response systematically under-counted fresh
    installs whose early searches 503 during the initial crawl, manufacturing a false 0%."""
    _record_milestone_once("first_search_at")


def _record_search_returned(returned_results: bool):
    """Diagnostic paired with first_search_at: did the first *served* search return any results
    (distinguishes a genuine no-match from the index-not-ready race). Set once, on the first
    search that actually reaches the index."""
    if "first_search_returned_results" in _recorded_milestones:
        return
    try:
        if _meta_get("first_search_returned_results") is None:
            _meta_set("first_search_returned_results", "1" if returned_results else "0")
        _recorded_milestones.add("first_search_returned_results")
    except Exception:
        pass


def _sqlts_to_iso(s):
    """SQLite CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS', UTC) -> ISO-8601 Z. None-safe."""
    if not s:
        return None
    s = str(s).strip()
    return s.replace(" ", "T") + ("" if s.endswith("Z") else "Z")


def _tel_record_boot():
    """Once per process start: bump boot_count, record the restart, and detect whether the
    PREVIOUS run exited cleanly. A missing clean-exit marker ⇒ the prior process died
    uncleanly (crash / OOM / SIGKILL / node loss) ⇒ count a crash (distinct from an
    orchestrated SIGTERM restart). boot_count powers the dashboard's persistent-vs-ephemeral
    classification — an ephemeral install (state wiped each boot) is forever boot_count==1."""
    try:
        now = int(time.time())
        bc = int(_meta_get("boot_count", "0") or "0") + 1
        _meta_set("boot_count", bc)
        rlst = [t for t in json.loads(_meta_get("restart_ts", "[]") or "[]") if now - t < 26 * 3600]
        rlst.append(now)
        _meta_set("restart_ts", json.dumps(rlst[-200:]))
        if bc > 1 and _meta_get("clean_exit", "1") != "1":  # previous run did NOT shut down cleanly
            clst = [t for t in json.loads(_meta_get("crash_ts", "[]") or "[]") if now - t < 26 * 3600]
            clst.append(now)
            _meta_set("crash_ts", json.dumps(clst[-200:]))
        _meta_set("clean_exit", "0")  # mark dirty until a graceful shutdown flips it back
    except Exception:
        pass


def _tel_mark_clean_exit():
    """Graceful-shutdown hook — flips the marker so the next boot doesn't count a crash."""
    _meta_set("clean_exit", "1")


def _detect_storage_ephemeral():
    """Best-effort: is DB_DIR on ephemeral storage? Returns True / False / None (unknown).
    An explicit operator/Helm hint (SAIRO_STORAGE_EPHEMERAL) wins; else infer from the mount table."""
    hint = os.environ.get("SAIRO_STORAGE_EPHEMERAL", "").strip().lower()
    if hint in ("true", "1", "yes"):
        return True
    if hint in ("false", "0", "no"):
        return False
    try:
        dbdir = os.path.realpath(DB_DIR)
        best, fstype = "", ""
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and (dbdir == p[1] or dbdir.startswith(p[1].rstrip("/") + "/")):
                    if len(p[1]) >= len(best):  # most-specific (longest) matching mountpoint
                        best, fstype = p[1], p[2]
        if fstype in ("tmpfs", "ramfs"):
            return True
        if best == "/" and fstype in ("overlay", "overlayfs", "aufs"):  # container root, no dedicated volume
            return True
        return None  # separate non-tmpfs mount (PVC / named volume / emptyDir) — indistinguishable from here
    except Exception:
        return None  # non-Linux or unreadable → unknown


def _id_persistence(boot_count) -> str:
    """persistent (proven, or hinted) / ephemeral (hinted or tmpfs/overlay-root) / unknown."""
    if boot_count and int(boot_count) > 1:
        return "persistent"  # proven: local state survived at least one restart
    eph = _detect_storage_ephemeral()
    if eph is True:
        return "ephemeral"
    if eph is False:
        return "persistent"
    return "unknown"


def _get_instance_id() -> str:
    """Get or create a persistent anonymous instance ID."""
    with _get_users_db() as db:
        row = db.execute("SELECT value FROM instance_meta WHERE key='instance_id'").fetchone()
        if row:
            return row[0]
        import uuid
        iid = str(uuid.uuid4())
        db.execute("INSERT INTO instance_meta (key, value) VALUES ('instance_id', ?)", (iid,))
        db.commit()
        return iid

def _collect_telemetry() -> dict:
    """Collect anonymous instance metrics."""
    import platform
    instance_id = _get_instance_id()
    uptime_hours = round((time.time() - _app_start_time) / 3600, 1)

    # Count buckets, objects, size from crawl_status tables
    total_objects = 0
    total_size = 0
    bucket_count = 0
    try:
        for f in os.listdir(DB_DIR):
            if not f.endswith(".db") or f == "users.db":
                continue
            try:
                path = os.path.join(DB_DIR, f)
                conn = sqlite3.connect(path)
                row = conn.execute("SELECT total_objects, total_size FROM crawl_status WHERE id=1").fetchone()
                if row:
                    total_objects += row[0] or 0
                    total_size += row[1] or 0
                    bucket_count += 1
                conn.close()
            except Exception:
                pass
    except Exception:
        pass

    # Count users, endpoints, API tokens, 2FA, share links; earliest token/usage
    user_count = endpoint_count = api_tokens = api_tokens_active = 0
    twofa_count = share_link_count = 0
    first_token_at = first_mcp_at = None
    try:
        with _get_users_db() as db:
            user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            endpoint_count = db.execute("SELECT COUNT(*) FROM s3_endpoints").fetchone()[0]
            api_tokens = db.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]
            api_tokens_active = db.execute(
                "SELECT COUNT(*) FROM api_tokens WHERE last_used IS NOT NULL AND last_used > datetime('now', '-7 days')"
            ).fetchone()[0]
            try:
                twofa_count = db.execute("SELECT COUNT(*) FROM users WHERE totp_enabled=1").fetchone()[0]
            except Exception:
                pass
            try:
                share_link_count = db.execute("SELECT COUNT(*) FROM share_links").fetchone()[0]
            except Exception:
                pass
            r = db.execute("SELECT MIN(created_at), MIN(last_used) FROM api_tokens").fetchone()
            if r:
                first_token_at, first_mcp_at = _sqlts_to_iso(r[0]), _sqlts_to_iso(r[1])
    except Exception:
        pass

    provider = detect_provider(S3_ENDPOINT)

    # Disk usage of the volume backing the index/data
    disk_total = disk_used = 0
    try:
        import shutil
        du = shutil.disk_usage(DB_DIR)
        disk_total, disk_used = du.total, du.used
    except Exception:
        pass

    # Trailing-24h request counters + restart history
    requests_24h = _tel_sum_24h(_tel_req_hours)
    requests_failed_24h = min(_tel_sum_24h(_tel_failed_hours), requests_24h)
    restart_count_24h = crash_count_24h = boot_count = 0
    try:
        _now = time.time()
        restart_count_24h = sum(1 for t in json.loads(_meta_get("restart_ts", "[]") or "[]") if _now - t < 86400)
        crash_count_24h = sum(1 for t in json.loads(_meta_get("crash_ts", "[]") or "[]") if _now - t < 86400)
        boot_count = int(_meta_get("boot_count", "0") or "0")
    except Exception:
        pass
    id_persistence = _id_persistence(boot_count)

    # active_buckets_24h — persist across restarts: merge in-memory with stored, prune to 24h.
    # Bucket names stay LOCAL in instance_meta; only the COUNT is ever sent in the ping.
    active_buckets_24h = 0
    try:
        _now = time.time()
        persisted = json.loads(_meta_get("active_buckets", "{}") or "{}")
        with _tel_lock:
            for b, ts in _tel_bucket_seen.items():
                if ts > persisted.get(b, 0):
                    persisted[b] = ts
        persisted = {b: ts for b, ts in persisted.items() if _now - ts < 86400}
        if len(persisted) > 10000:
            persisted = dict(sorted(persisted.items(), key=lambda kv: kv[1])[-10000:])
        _meta_set("active_buckets", json.dumps(persisted))
        active_buckets_24h = len(persisted)
    except Exception:
        active_buckets_24h = _tel_active_buckets_24h()

    # Activation milestones (record-once, idempotent — never overwritten)
    if bucket_count > 0 and not _meta_get("first_bucket_at"):
        _meta_set("first_bucket_at", _iso_now())
    if total_objects > 0 and not _meta_get("first_object_at"):
        _meta_set("first_object_at", _iso_now())
    first_bucket_at = _meta_get("first_bucket_at")
    first_object_at = _meta_get("first_object_at")
    # Activation funnel (recorded at request time by _record_first_search / _record_milestone_once)
    first_search_at = _meta_get("first_search_at")
    first_dashboard_open_at = _meta_get("first_dashboard_open_at")
    _fsr = _meta_get("first_search_returned_results")
    first_search_returned_results = None if _fsr is None else (_fsr == "1")

    # last_write_at: max(in-memory since boot, persisted) so it survives restarts
    last_write_at = _meta_get("last_write_at")
    if _tel_last_write[0] > 0:
        cand = _epoch_to_iso(_tel_last_write[0])
        if not last_write_at or cand > last_write_at:
            _meta_set("last_write_at", cand)
            last_write_at = cand

    # features_enabled — config/DB-derived Sairo capabilities (stable lowercase slugs)
    feats = []
    if AUTH_MODE == "s3":
        feats.append("s3_auth")
    if os.environ.get("LDAP_ENABLED", "false").lower() == "true":
        feats.append("ldap")
    if os.environ.get("OAUTH_GOOGLE_CLIENT_ID") or os.environ.get("OAUTH_GITHUB_CLIENT_ID"):
        feats.append("oauth")
    if endpoint_count > 1:
        feats.append("multi_endpoint")
    if api_tokens > 0:
        feats.append("api_tokens")
    if twofa_count > 0:
        feats.append("twofa")
    if share_link_count > 0:
        feats.append("share_links")
    if os.environ.get("APP_NAME", "Sairo") != "Sairo" or os.environ.get("APP_LOGO"):
        feats.append("custom_branding")
    feats = feats[:32]

    # update_available — read the cached check only; never triggers a network call here
    _latest = _update_cache.get("latest")
    update_available = bool(_latest and _version_gt(_latest, SAIRO_VERSION))

    # health rollup
    err_rate = (requests_failed_24h / requests_24h) if requests_24h else 0.0
    disk_pct = (disk_used / disk_total) if disk_total else 0.0
    if disk_pct > 0.95 or err_rate > 0.25 or crash_count_24h >= 3:
        health = "error"
    elif disk_pct > 0.85 or crash_count_24h >= 1 or restart_count_24h > 5:
        health = "degraded"
    else:
        health = "ok"

    def _ci(v, hi):  # clamp to a non-negative int within bounds
        try:
            return max(0, min(int(v), hi))
        except Exception:
            return 0

    bucket_count = _ci(bucket_count, 10000)
    return {
        # ── existing baseline (unchanged) ──
        "instance_id": instance_id,
        "version": SAIRO_VERSION,
        "buckets": bucket_count,
        "total_objects": _ci(total_objects, 10**12),
        "total_size": _ci(total_size, 10**18),
        "provider": provider,
        "uptime_hours": uptime_hours,
        "os": f"{platform.system().lower()}/{platform.machine()}",
        "endpoints": _ci(endpoint_count, 10000),
        "users": _ci(user_count, 10**6),
        "auth_mode": AUTH_MODE,
        "api_tokens": _ci(api_tokens, 10**6),
        "api_tokens_active": _ci(api_tokens_active, 10**6),
        # ── v2: schema + Tier 1 health ──
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "requests_24h": _ci(requests_24h, 10**12),
        "requests_failed_24h": _ci(requests_failed_24h, 10**12),
        "restart_count_24h": _ci(restart_count_24h, 10000),
        "crash_count_24h": _ci(crash_count_24h, 10000),
        "disk_total_bytes": _ci(disk_total, 10**18),
        "disk_used_bytes": _ci(disk_used, 10**18),
        "health": health,
        # ── v2: id validity (persistent vs ephemeral / reincarnation) ──
        "id_persistence": id_persistence,
        "boot_count": _ci(boot_count, 10**9),
        # ── v2: Tier 2 activation milestones ──
        "first_bucket_at": first_bucket_at,
        "first_object_at": first_object_at,
        "first_api_token_at": first_token_at,
        "first_mcp_connect_at": first_mcp_at,
        # ── v2: activation funnel (time-to-first-search + dashboard reach) ──
        "first_search_at": first_search_at,
        "first_dashboard_open_at": first_dashboard_open_at,
        "first_search_returned_results": first_search_returned_results,
        # ── v2: Tier 3 engagement + adoption ──
        "active_buckets_24h": min(_ci(active_buckets_24h, 10000), bucket_count),
        "last_write_at": last_write_at,
        "features_enabled": feats,
        "update_available": update_available,
    }

@app.on_event("shutdown")
def _telemetry_shutdown():
    """Record a clean shutdown so the next boot doesn't mis-count this as a crash."""
    if TELEMETRY:
        _tel_mark_clean_exit()


_telemetry_lockfile = None  # held for the process lifetime by the elected pinger


def _telemetry_loop():
    """Background thread: send the anonymous heartbeat every TELEMETRY_INTERVAL seconds (hourly by
    default). Only ONE process per host pings — a non-blocking file lock elects a single leader so a
    multi-worker deployment can't emit duplicate pings or split counters."""
    global _telemetry_lockfile
    import urllib.request
    import json as _json
    try:
        import fcntl
        _telemetry_lockfile = open(os.path.join(DB_DIR, ".telemetry.lock"), "w")
        fcntl.flock(_telemetry_lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if another worker holds it
    except ImportError:
        pass  # no fcntl (non-POSIX) → assume single process
    except OSError:
        log.info("Telemetry: another worker holds the ping lock; this worker will not ping.")
        return
    time.sleep(60)  # wait 1 min after startup before first ping
    while True:
        try:
            data = _collect_telemetry()
            req = urllib.request.Request(
                TELEMETRY_URL,
                data=_json.dumps(data).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # silent failure — never crash, never retry
        time.sleep(TELEMETRY_INTERVAL)


# ── API: Auth ──────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginS3Request(BaseModel):
    access_key: str
    secret_key: str
    endpoint_id: str = "default"  # which configured endpoint to authenticate against

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class UpdateUserRequest(BaseModel):
    role: str

@app.post("/api/auth/login")
@limiter.limit("10/minute")
def auth_login(req: LoginRequest, request: Request):
    _check_login_rate(request.client.host)
    with _get_users_db() as db:
        row = db.execute("SELECT username, password_hash, role, totp_enabled FROM users WHERE username=?",
                         (req.username,)).fetchone()
    if not row or not bcrypt.verify(req.password, row["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    # Check 2FA
    if row["totp_enabled"]:
        pending_token = jwt.encode(
            {"sub": row["username"], "role": row["role"], "purpose": "2fa",
             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            JWT_SECRET, algorithm="HS256")
        response = JSONResponse({"requires_2fa": True, "username": row["username"]})
        _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
        response.set_cookie("access_token", pending_token, httponly=True, samesite="strict",
                            secure=_secure_cookie, max_age=300, path="/")
        return response
    token = jwt.encode(
        {"sub": row["username"], "role": row["role"],
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = JSONResponse({"username": row["username"], "role": row["role"]})
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    _audit("login", row["username"])
    return response

@app.post("/api/auth/login-s3")
@limiter.limit("10/minute")
def auth_login_s3(req: LoginS3Request, request: Request):
    """Authenticate by validating S3 credentials directly.
    Calls list_buckets() with the provided access key / secret key.
    If it succeeds, the credentials are valid and the user gets an admin session.
    """
    _check_login_rate(request.client.host)
    if not req.access_key or not req.secret_key:
        raise HTTPException(400, "Access key and secret key are required")
    # Validate the USER's keys against the chosen endpoint's connection params, then
    # keep them (encrypted) in the session token so every later S3 call is made AS the
    # user — the provider's IAM then scopes exactly what they can see and do.
    eid = req.endpoint_id or "default"
    info = _s3_manager.get_endpoint_info(eid) or _s3_manager.get_endpoint_info("default")
    if not info:
        raise HTTPException(400, "No S3 endpoint configured")
    try:
        test_client = _s3_manager._build_client(info, req.access_key, req.secret_key)
        test_client.list_buckets()
    except Exception as e:
        log.warning("S3 auth failed for access_key=%s: %s", req.access_key[:6] + "...", e)
        raise HTTPException(401, "Invalid S3 credentials")
    # Credentials valid — issue a session as admin. Use a sanitized version of the
    # access key as the username; carry the user's keys encrypted in the JWT.
    username = f"s3:{req.access_key[:8]}"
    token = jwt.encode(
        {"sub": username, "role": "admin", "eid": eid,
         "s3ak": _encrypt(req.access_key), "s3sk": _encrypt(req.secret_key),
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = JSONResponse({"username": username, "role": "admin"})
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    _audit("login", username, details="s3_auth")
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    # Always clear the local session. If the session belongs to an OIDC user and
    # RP-initiated logout is enabled, also hand back the IdP end-session URL so the
    # frontend can complete a single logout (the SSO session, not just ours).
    sso_logout_url = None
    if OIDC_ENABLED and OIDC_RP_LOGOUT:
        token = request.cookies.get("access_token")
        if token:
            try:
                sub = jwt.decode(token, JWT_SECRET, algorithms=["HS256"]).get("sub")
            except Exception:
                sub = None
            if sub:
                with _get_users_db() as db:
                    row = db.execute("SELECT auth_source FROM users WHERE username=?", (sub,)).fetchone()
                if row and (row["auth_source"] or "local") == "oidc":
                    try:
                        end = _oidc_config().get("end_session_endpoint")
                        if end:
                            import urllib.parse
                            base = str(request.base_url).rstrip("/")
                            qs = urllib.parse.urlencode({
                                "client_id": OIDC_CLIENT_ID,
                                "post_logout_redirect_uri": base + "/",
                            })
                            sso_logout_url = f"{end}?{qs}"
                    except Exception:
                        sso_logout_url = None
    response = JSONResponse({"logged_out": True, "sso_logout_url": sso_logout_url})
    response.delete_cookie("access_token", path="/")
    return response

@app.get("/api/auth/me")
def auth_me(user: dict = Depends(get_current_user), request: Request = None):
    result = {"username": user["username"], "role": user["role"]}
    # Include 2FA status + which provider this account authenticates against
    with _get_users_db() as db:
        row = db.execute("SELECT totp_enabled, auth_source FROM users WHERE username=?", (user["username"],)).fetchone()
    if row:
        result["totp_enabled"] = bool(row["totp_enabled"])
        result["auth_source"] = row["auth_source"] or "local"
    token = request.cookies.get("access_token") if request else None
    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            result["expires_at"] = payload.get("exp")
        except Exception as tok_e:
            log.debug("Token decode for expires_at failed: %s", tok_e)
    return result

@app.post("/api/auth/refresh")
def auth_refresh(user: dict = Depends(get_current_user)):
    token = jwt.encode(
        {"sub": user["username"], "role": user["role"],
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response = JSONResponse({"username": user["username"], "role": user["role"],
                             "expires_in": SESSION_HOURS * 3600})
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    return response

@app.get("/api/auth/users")
def auth_list_users(user: dict = Depends(require_admin)):
    with _get_users_db() as db:
        rows = db.execute("SELECT username, role, created_at, totp_enabled, auth_source FROM users ORDER BY created_at").fetchall()
        # bucket-grant counts per user, so the UI can show "N buckets" at a glance
        counts = {r["username"]: r["n"] for r in db.execute(
            "SELECT username, COUNT(*) AS n FROM bucket_permissions GROUP BY username").fetchall()}
    users = []
    for r in rows:
        u = dict(r)
        u["totp_enabled"] = bool(u.get("totp_enabled"))
        u["auth_source"] = u.get("auth_source") or "local"
        u["bucket_count"] = counts.get(u["username"], 0)
        users.append(u)
    return {"users": users}

@app.post("/api/auth/users")
def auth_create_user(req: CreateUserRequest, user: dict = Depends(require_admin)):
    if req.role not in ("admin", "viewer"):
        raise HTTPException(400, "Role must be 'admin' or 'viewer'")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    with _get_users_db() as db:
        existing = db.execute("SELECT username FROM users WHERE username=?", (req.username,)).fetchone()
        if existing:
            raise HTTPException(409, f"User '{req.username}' already exists")
        db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                   (req.username, bcrypt.hash(req.password), req.role))
        db.commit()
    _audit("create_user", user["username"], details=f"user={req.username}, role={req.role}")
    return {"created": req.username, "role": req.role}

@app.delete("/api/auth/users/{username}")
def auth_delete_user(username: str, user: dict = Depends(require_admin)):
    if username == user["username"]:
        raise HTTPException(400, "Cannot delete your own account")
    with _get_users_db() as db:
        existing = db.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not existing:
            raise HTTPException(404, f"User '{username}' not found")
        db.execute("DELETE FROM users WHERE username=?", (username,))
        db.execute("DELETE FROM bucket_permissions WHERE username=?", (username,))
        db.commit()
    _audit("delete_user", user["username"], details=f"user={username}")
    return {"deleted": username}

@app.put("/api/auth/users/{username}")
def auth_update_user(username: str, req: UpdateUserRequest, user: dict = Depends(require_admin)):
    if req.role not in ("admin", "viewer"):
        raise HTTPException(400, "Role must be 'admin' or 'viewer'")
    if username == user["username"]:
        raise HTTPException(400, "Cannot change your own role")
    with _get_users_db() as db:
        existing = db.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not existing:
            raise HTTPException(404, f"User '{username}' not found")
        db.execute("UPDATE users SET role=? WHERE username=?", (req.role, username))
        db.commit()
    _audit("update_user", user["username"], details=f"user={username}, role={req.role}")
    return {"updated": username, "role": req.role}


# ── API: 2FA/TOTP ──────────────────────────────────────────────────────────

class TwoFactorVerifyRequest(BaseModel):
    code: str

class TwoFactorDisableRequest(BaseModel):
    password: str

@app.post("/api/auth/2fa/setup")
def twofa_setup(user: dict = Depends(get_current_user)):
    """Generate TOTP secret. Does NOT enable 2FA yet — user must verify a code first."""
    secret = pyotp.random_base32()
    # Store the pending secret encrypted at rest
    with _get_users_db() as db:
        db.execute("UPDATE users SET totp_secret=? WHERE username=?", (_encrypt(secret), user["username"]))
        db.commit()
    totp = pyotp.TOTP(secret)
    app_name = os.environ.get("APP_NAME", "Sairo")
    otpauth_url = totp.provisioning_uri(name=user["username"], issuer_name=app_name)
    return {"secret": secret, "otpauth_url": otpauth_url}

@app.post("/api/auth/2fa/enable")
def twofa_enable(req: TwoFactorVerifyRequest, user: dict = Depends(get_current_user)):
    """Verify a TOTP code and enable 2FA. Generates recovery codes."""
    with _get_users_db() as db:
        row = db.execute("SELECT totp_secret, totp_enabled FROM users WHERE username=?",
                         (user["username"],)).fetchone()
    if not row or not row["totp_secret"]:
        raise HTTPException(400, "Call /api/auth/2fa/setup first")
    if row["totp_enabled"]:
        raise HTTPException(400, "2FA is already enabled")
    totp = pyotp.TOTP(_decrypt(row["totp_secret"]))
    if not totp.verify(req.code, valid_window=1):
        raise HTTPException(400, "Invalid TOTP code")
    # Generate 10 recovery codes
    recovery_plain = [secrets.token_hex(4) for _ in range(10)]
    recovery_hashes = json.dumps([bcrypt.hash(c) for c in recovery_plain])
    with _get_users_db() as db:
        db.execute("UPDATE users SET totp_enabled=1, recovery_codes=? WHERE username=?",
                   (recovery_hashes, user["username"]))
        db.commit()
    _audit("enable_2fa", user["username"])
    return {"enabled": True, "recovery_codes": recovery_plain}

@app.post("/api/auth/2fa/disable")
def twofa_disable(req: TwoFactorDisableRequest, user: dict = Depends(get_current_user)):
    """Disable 2FA for current user. Requires password confirmation."""
    with _get_users_db() as db:
        row = db.execute("SELECT password_hash, totp_enabled FROM users WHERE username=?",
                         (user["username"],)).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    if not row["totp_enabled"]:
        raise HTTPException(400, "2FA is not enabled")
    # Verify password (skip for LDAP/OAuth users who have unusable passwords)
    if not row["password_hash"].startswith(("LDAP:", "OAUTH:")):
        if not bcrypt.verify(req.password, row["password_hash"]):
            raise HTTPException(401, "Invalid password")
    with _get_users_db() as db:
        db.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, recovery_codes=NULL WHERE username=?",
                   (user["username"],))
        db.commit()
    _audit("disable_2fa", user["username"])
    return {"disabled": True}

@app.post("/api/auth/2fa/reset/{username}")
def twofa_admin_reset(username: str, user: dict = Depends(require_admin)):
    """Admin resets another user's 2FA."""
    if username == user["username"]:
        raise HTTPException(400, "Use /api/auth/2fa/disable instead")
    with _get_users_db() as db:
        existing = db.execute("SELECT totp_enabled FROM users WHERE username=?", (username,)).fetchone()
        if not existing:
            raise HTTPException(404, f"User '{username}' not found")
        if not existing["totp_enabled"]:
            raise HTTPException(400, "2FA is not enabled for this user")
        db.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, recovery_codes=NULL WHERE username=?",
                   (username,))
        db.commit()
    _audit("reset_2fa", user["username"], details=f"target={username}")
    return {"reset": True, "username": username}

@app.post("/api/auth/2fa/verify")
@limiter.limit("5/minute")
def twofa_verify(req: TwoFactorVerifyRequest, request: Request):
    """Verify TOTP code during login (second step). Requires pending 2FA token in cookie."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid or expired token")
    if payload.get("purpose") != "2fa":
        raise HTTPException(400, "Not a 2FA pending token")
    username = payload["sub"]
    with _get_users_db() as db:
        row = db.execute("SELECT totp_secret, totp_enabled, role FROM users WHERE username=?",
                         (username,)).fetchone()
    if not row or not row["totp_enabled"] or not row["totp_secret"]:
        raise HTTPException(400, "2FA not configured")
    totp = pyotp.TOTP(_decrypt(row["totp_secret"]))
    if not totp.verify(req.code, valid_window=1):
        raise HTTPException(401, "Invalid TOTP code")
    # Issue full session token
    full_token = jwt.encode(
        {"sub": username, "role": row["role"],
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = JSONResponse({"username": username, "role": row["role"]})
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("access_token", full_token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    _audit("login", username, details="2fa_verified")
    return response

@app.post("/api/auth/2fa/recover")
@limiter.limit("5/minute")
def twofa_recover(req: TwoFactorVerifyRequest, request: Request):
    """Use a recovery code during login (second step). Each code is one-use."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid or expired token")
    if payload.get("purpose") != "2fa":
        raise HTTPException(400, "Not a 2FA pending token")
    username = payload["sub"]
    with _get_users_db() as db:
        row = db.execute("SELECT recovery_codes, role FROM users WHERE username=?",
                         (username,)).fetchone()
    if not row or not row["recovery_codes"]:
        raise HTTPException(400, "No recovery codes available")
    hashes = json.loads(row["recovery_codes"])
    matched_idx = None
    for i, h in enumerate(hashes):
        if bcrypt.verify(req.code.strip(), h):
            matched_idx = i
            break
    if matched_idx is None:
        raise HTTPException(401, "Invalid recovery code")
    # Remove the used code
    hashes.pop(matched_idx)
    with _get_users_db() as db:
        db.execute("UPDATE users SET recovery_codes=? WHERE username=?",
                   (json.dumps(hashes), username))
        db.commit()
    # Issue full session token
    full_token = jwt.encode(
        {"sub": username, "role": row["role"],
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = JSONResponse({"username": username, "role": row["role"], "recovery_codes_remaining": len(hashes)})
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("access_token", full_token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    _audit("login", username, details=f"2fa_recovery, codes_remaining={len(hashes)}")
    return response


# ── API: Bucket Permissions ────────────────────────────────────────────────

class BucketPermissionItem(BaseModel):
    bucket: str
    permission: str  # "read" or "write"

class SetPermissionsRequest(BaseModel):
    permissions: list[BucketPermissionItem]

@app.get("/api/auth/users/{username}/permissions")
def get_user_permissions(username: str, user: dict = Depends(require_admin)):
    with _get_users_db() as db:
        existing = db.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
        if not existing:
            raise HTTPException(404, f"User '{username}' not found")
        if existing["role"] == "admin":
            return {"username": username, "permissions": [], "note": "Admin has full access to all buckets"}
        rows = db.execute(
            "SELECT bucket, permission, granted_by, granted_at FROM bucket_permissions WHERE username=? ORDER BY bucket",
            (username,)
        ).fetchall()
    return {"username": username, "permissions": [dict(r) for r in rows]}

@app.put("/api/auth/users/{username}/permissions")
def set_user_permissions(username: str, req: SetPermissionsRequest, user: dict = Depends(require_admin)):
    for p in req.permissions:
        if p.permission not in ("read", "write"):
            raise HTTPException(400, f"Permission must be 'read' or 'write', got '{p.permission}'")
    with _get_users_db() as db:
        existing = db.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
        if not existing:
            raise HTTPException(404, f"User '{username}' not found")
        if existing["role"] == "admin":
            raise HTTPException(400, "Cannot set permissions on admin users (they have full access)")
        db.execute("DELETE FROM bucket_permissions WHERE username=?", (username,))
        for p in req.permissions:
            db.execute(
                "INSERT INTO bucket_permissions (username, bucket, permission, granted_by) VALUES (?, ?, ?, ?)",
                (username, p.bucket, p.permission, user["username"])
            )
        db.commit()
    _audit("set_permissions", user["username"], details=f"user={username}, buckets={len(req.permissions)}")
    return {"username": username, "updated": len(req.permissions)}

@app.delete("/api/auth/users/{username}/permissions/{bucket}")
def delete_user_permission(username: str, bucket: str, user: dict = Depends(require_admin)):
    with _get_users_db() as db:
        existing = db.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not existing:
            raise HTTPException(404, f"User '{username}' not found")
        result = db.execute("DELETE FROM bucket_permissions WHERE username=? AND bucket=?", (username, bucket))
        db.commit()
        if result.rowcount == 0:
            raise HTTPException(404, f"No permission found for user '{username}' on bucket '{bucket}'")
    _audit("remove_permission", user["username"], details=f"user={username}, bucket={bucket}")
    return {"deleted": True, "username": username, "bucket": bucket}

@app.put("/api/auth/change-password")
def auth_change_password(req: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    with _get_users_db() as db:
        row = db.execute("SELECT password_hash FROM users WHERE username=?", (user["username"],)).fetchone()
        if not row or not bcrypt.verify(req.old_password, row["password_hash"]):
            raise HTTPException(401, "Current password is incorrect")
        db.execute("UPDATE users SET password_hash=? WHERE username=?",
                   (bcrypt.hash(req.new_password), user["username"]))
        db.commit()
    _audit("change_password", user["username"])
    return {"updated": True}


# ── API: API Tokens ────────────────────────────────────────────────────────

class CreateTokenRequest(BaseModel):
    name: str = "default"
    role: str = "viewer"
    expires_days: Optional[int] = None  # None = no expiry

@app.get("/api/auth/tokens")
def list_tokens(user: dict = Depends(require_admin)):
    with _get_users_db() as db:
        rows = db.execute(
            "SELECT id, token_prefix, username, name, role, created_at, expires_at, last_used FROM api_tokens ORDER BY created_at DESC"
        ).fetchall()
    return {"tokens": [dict(r) for r in rows]}

@app.post("/api/auth/tokens")
def create_token(req: CreateTokenRequest, user: dict = Depends(require_admin)):
    import hashlib
    if req.role not in ("admin", "viewer"):
        raise HTTPException(400, "Role must be 'admin' or 'viewer'")
    raw_token = f"sairo_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_prefix = raw_token[:12] + "..."
    expires_at = None
    if req.expires_days and req.expires_days > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=req.expires_days)).isoformat()
    with _get_users_db() as db:
        db.execute(
            "INSERT INTO api_tokens (token_hash, token_prefix, username, name, role, expires_at) VALUES (?,?,?,?,?,?)",
            (token_hash, token_prefix, user["username"], req.name, req.role, expires_at))
        db.commit()
    _audit("create_token", user["username"], details=f"name={req.name}, role={req.role}")
    return {"token": raw_token, "prefix": token_prefix, "name": req.name, "role": req.role, "expires_at": expires_at}

@app.delete("/api/auth/tokens/{token_id}")
def delete_token(token_id: int, user: dict = Depends(require_admin)):
    with _get_users_db() as db:
        row = db.execute("SELECT id, name FROM api_tokens WHERE id=?", (token_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Token not found")
        db.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
        db.commit()
    _audit("delete_token", user["username"], details=f"token_id={token_id}")
    return {"deleted": token_id}


# ── API: Share Links ───────────────────────────────────────────────────────

class CreateShareLinkRequest(BaseModel):
    bucket: str
    key: str
    expires_hours: int = 168  # 7 days default
    max_downloads: Optional[int] = None
    password: Optional[str] = None

@app.post("/api/share-links")
def create_share_link(req: CreateShareLinkRequest, user: dict = Depends(get_current_user)):
    token = secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=req.expires_hours)).isoformat()
    password_hash = bcrypt.hash(req.password) if req.password else None
    with _get_users_db() as db:
        db.execute(
            "INSERT INTO share_links (token, bucket, key, created_by, expires_at, max_downloads, password_hash) VALUES (?,?,?,?,?,?,?)",
            (token, req.bucket, req.key, user["username"], expires_at, req.max_downloads, password_hash))
        db.commit()
    _audit("create_share_link", user["username"], bucket=req.bucket, details=f"key={req.key}")
    return {"token": token, "url": f"/share/{token}", "expires_at": expires_at}

@app.get("/api/share-links")
def list_share_links(bucket: str = "", user: dict = Depends(get_current_user)):
    with _get_users_db() as db:
        if bucket:
            rows = db.execute(
                "SELECT id, token, bucket, key, created_by, created_at, expires_at, download_count, max_downloads FROM share_links WHERE bucket=? ORDER BY created_at DESC",
                (bucket,)).fetchall()
        else:
            rows = db.execute(
                "SELECT id, token, bucket, key, created_by, created_at, expires_at, download_count, max_downloads FROM share_links ORDER BY created_at DESC"
            ).fetchall()
    return {"links": [dict(r) for r in rows]}

@app.delete("/api/share-links/{link_id}")
def delete_share_link(link_id: int, user: dict = Depends(get_current_user)):
    with _get_users_db() as db:
        row = db.execute("SELECT id, created_by FROM share_links WHERE id=?", (link_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Share link not found")
        if user["role"] != "admin" and row["created_by"] != user["username"]:
            raise HTTPException(403, "You can only delete your own share links")
        db.execute("DELETE FROM share_links WHERE id=?", (link_id,))
        db.commit()
    _audit("delete_share_link", user["username"], details=f"link_id={link_id}")
    return {"deleted": link_id}

@app.get("/api/share/{token}")
def resolve_share_link(token: str, password: str = ""):
    """Public endpoint — no auth required. Returns presigned URL for the shared object."""
    with _get_users_db() as db:
        row = db.execute(
            "SELECT * FROM share_links WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "Share link not found or expired")
    row = dict(row)
    # Check expiry
    exp = datetime.fromisoformat(row["expires_at"])
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > exp:
        raise HTTPException(410, "Share link has expired")
    # Check download limit
    if row["max_downloads"] and row["download_count"] >= row["max_downloads"]:
        raise HTTPException(410, "Download limit reached")
    # Check password
    if row["password_hash"]:
        if not password or not bcrypt.verify(password, row["password_hash"]):
            raise HTTPException(401, "Password required")
    # Generate presigned URL
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": row["bucket"], "Key": row["key"]},
        ExpiresIn=3600)
    # Update download count
    with _get_users_db() as db:
        db.execute("UPDATE share_links SET download_count = download_count + 1 WHERE token=?", (token,))
        db.commit()
    filename = row["key"].split("/")[-1]
    return {"url": url, "filename": filename, "bucket": row["bucket"], "key": row["key"]}


# ── API: License Management ────────────────────────────────────────────────

LICENSE_PUBLIC_KEY = os.environ.get("LICENSE_PUBLIC_KEY", "")

@app.get("/api/license")
def get_license(user: dict = Depends(get_current_user)):
    with _get_users_db() as db:
        row = db.execute("SELECT * FROM license_info WHERE id=1").fetchone()
    if not row:
        return {"type": "community", "features": {}}
    r = dict(row)
    features = {}
    try:
        features = __import__("json").loads(r.get("features") or "{}")
    except Exception as feat_e:
        log.warning("Failed to parse license features: %s", feat_e)
    return {
        "type": r.get("license_type", "community"),
        "licensed_to": r.get("licensed_to"),
        "max_users": r.get("max_users", 0),
        "features": features,
        "expires_at": r.get("expires_at"),
    }

class ActivateLicenseRequest(BaseModel):
    key: str

@app.post("/api/license")
def activate_license(req: ActivateLicenseRequest, user: dict = Depends(require_admin)):
    license_key = req.key
    if not license_key:
        raise HTTPException(400, "License key is required")
    try:
        import base64
        decoded = json.loads(base64.b64decode(license_key))
        license_type = decoded.get("type", "pro")
        licensed_to = decoded.get("to", "")
        max_users = decoded.get("max_users", 0)
        features = json.dumps(decoded.get("features", {}))
        expires_at = decoded.get("expires_at")
    except Exception:
        raise HTTPException(400, "Invalid license key format")
    with _get_users_db() as db:
        db.execute("""
            UPDATE license_info SET license_key=?, license_type=?, licensed_to=?, max_users=?,
            features=?, activated_at=?, expires_at=? WHERE id=1
        """, (license_key, license_type, licensed_to, max_users, features,
              datetime.now(timezone.utc).isoformat(), expires_at))
        db.commit()
    _audit("activate_license", user["username"], details=f"type={license_type}, to={licensed_to}")
    return {"activated": True, "type": license_type, "licensed_to": licensed_to}


# ── API: Branding / White-Label ────────────────────────────────────────────

# Branding settings stored as env vars (simple) or in license_info features
@app.get("/api/branding")
def get_branding():
    """Public endpoint — returns custom branding. No auth required."""
    return {
        "app_name": os.environ.get("APP_NAME", "Sairo"),
        "app_logo": os.environ.get("APP_LOGO", ""),  # URL to custom logo
        "primary_color": os.environ.get("PRIMARY_COLOR", "#3b82f6"),
        "login_message": os.environ.get("LOGIN_MESSAGE", ""),
        "ldap_enabled": os.environ.get("LDAP_ENABLED", "false").lower() == "true",
        "oauth_providers": _auth_providers(),
        "auth_mode": AUTH_MODE,
        "version": SAIRO_VERSION,
    }


# ── API: Update Check ─────────────────────────────────────────────────────

_update_cache: dict = {"latest": None, "checked_at": 0}

@app.get("/api/version")
def get_version(user: dict = Depends(get_current_user)):
    """Return current version and latest available version (cached 24h)."""
    import urllib.request
    now = time.time()
    latest = _update_cache.get("latest")
    # Check GitHub releases API at most once per 24 hours
    if not latest or now - _update_cache["checked_at"] > 86400:
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/AshwathStephen/sairo/releases/latest",
                headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "Sairo"},
            )
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())
            latest = data.get("tag_name", "").lstrip("v")
            _update_cache["latest"] = latest
            _update_cache["checked_at"] = now
        except Exception:
            latest = _update_cache.get("latest") or SAIRO_VERSION
    update_available = bool(latest and _version_gt(latest, SAIRO_VERSION))
    return {
        "current": SAIRO_VERSION,
        "latest": latest,
        "update_available": bool(update_available),
    }


# ── API: LDAP Authentication ──────────────────────────────────────────────

LDAP_ENABLED = os.environ.get("LDAP_ENABLED", "false").lower() == "true"
LDAP_SERVER = os.environ.get("LDAP_SERVER", "")
LDAP_BASE_DN = os.environ.get("LDAP_BASE_DN", "")
LDAP_USER_FILTER = os.environ.get("LDAP_USER_FILTER", "(sAMAccountName={username})")
LDAP_BIND_DN = os.environ.get("LDAP_BIND_DN", "")
LDAP_BIND_PASSWORD = os.environ.get("LDAP_BIND_PASSWORD", "")
LDAP_ADMIN_GROUP = os.environ.get("LDAP_ADMIN_GROUP", "")
LDAP_DEFAULT_ROLE = os.environ.get("LDAP_DEFAULT_ROLE", "viewer")

@app.post("/api/auth/ldap")
def auth_ldap(req: LoginRequest, request: Request):
    """LDAP authentication. Syncs user to local DB on success."""
    if not LDAP_ENABLED:
        raise HTTPException(400, "LDAP authentication is not enabled")
    _check_login_rate(request.client.host)
    try:
        import ldap3
    except ImportError:
        raise HTTPException(500, "LDAP support requires the ldap3 package")

    # Connect to LDAP
    server = ldap3.Server(LDAP_SERVER, get_info=ldap3.ALL)
    user_filter = LDAP_USER_FILTER.replace("{username}", ldap3.utils.conv.escape_filter_chars(req.username))

    try:
        # Service account bind to search for user
        if LDAP_BIND_DN:
            conn = ldap3.Connection(server, LDAP_BIND_DN, LDAP_BIND_PASSWORD, auto_bind=True)
            conn.search(LDAP_BASE_DN, user_filter, attributes=["memberOf", "cn", "mail"])
            if not conn.entries:
                raise HTTPException(401, "Invalid username or password")
            user_dn = conn.entries[0].entry_dn
            conn.unbind()
        else:
            # Direct bind (user DN = filter result)
            user_dn = f"cn={req.username},{LDAP_BASE_DN}"

        # Verify password by binding as user
        user_conn = ldap3.Connection(server, user_dn, req.password, auto_bind=True)
        # Determine role from group membership
        role = LDAP_DEFAULT_ROLE
        if LDAP_ADMIN_GROUP:
            user_conn.search(user_dn, "(objectClass=*)", attributes=["memberOf"])
            if user_conn.entries:
                groups = [str(g) for g in user_conn.entries[0].get("memberOf", [])]
                if any(LDAP_ADMIN_GROUP.lower() in g.lower() for g in groups):
                    role = "admin"
        user_conn.unbind()
    except ldap3.core.exceptions.LDAPBindError:
        raise HTTPException(401, "Invalid username or password")
    except ldap3.core.exceptions.LDAPException as e:
        log.error("LDAP error: %s", e)
        raise HTTPException(502, f"LDAP error: {e}")

    # Sync to local users table via the hardened federated chokepoint. LDAP role
    # is group-derived, so it's passed as mapped_role (re-synced each login for
    # LDAP-owned users, never for a local/other-IdP account of the same name).
    try:
        role, totp_enabled = _sync_federated_user(
            req.username, "ldap", "LDAP", LDAP_DEFAULT_ROLE, mapped_role=role)
    except FederatedAuthError:
        _audit("login_failed", req.username, details="ldap account-source conflict")
        raise HTTPException(409, "This username already exists with a different sign-in method")

    # Check 2FA
    if totp_enabled:
        pending_token = jwt.encode(
            {"sub": req.username, "role": role, "purpose": "2fa",
             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            JWT_SECRET, algorithm="HS256")
        response = JSONResponse({"requires_2fa": True, "username": req.username, "auth_method": "ldap"})
        _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
        response.set_cookie("access_token", pending_token, httponly=True, samesite="strict",
                            secure=_secure_cookie, max_age=300, path="/")
        return response

    # Issue JWT
    token = jwt.encode(
        {"sub": req.username, "role": role,
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = JSONResponse({"username": req.username, "role": role, "auth_method": "ldap"})
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    _audit("login", req.username, details="method=ldap")
    return response


# ── API: OAuth / OIDC ──────────────────────────────────────────────────────

OAUTH_GOOGLE_CLIENT_ID = os.environ.get("OAUTH_GOOGLE_CLIENT_ID", "")
OAUTH_GOOGLE_CLIENT_SECRET = os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET", "")
OAUTH_GITHUB_CLIENT_ID = os.environ.get("OAUTH_GITHUB_CLIENT_ID", "")
OAUTH_GITHUB_CLIENT_SECRET = os.environ.get("OAUTH_GITHUB_CLIENT_SECRET", "")
OAUTH_DEFAULT_ROLE = os.environ.get("OAUTH_DEFAULT_ROLE", "viewer")
OAUTH_ALLOWED_DOMAINS = [d.strip() for d in os.environ.get("OAUTH_ALLOWED_DOMAINS", "").split(",") if d.strip()]

def _auth_providers() -> list:
    """Single source of truth for the SSO buttons the frontend renders.

    Each entry carries an explicit ``login_path`` so the frontend doesn't have
    to assume a URL shape (OIDC lives under /oidc, not /oauth/<id>)."""
    providers = []
    if OAUTH_GOOGLE_CLIENT_ID:
        providers.append({"id": "google", "name": "Google", "login_path": "/api/auth/oauth/google/login"})
    if OAUTH_GITHUB_CLIENT_ID:
        providers.append({"id": "github", "name": "GitHub", "login_path": "/api/auth/oauth/github/login"})
    if OIDC_ENABLED:
        providers.append({"id": "oidc", "name": OIDC_PROVIDER_NAME, "login_path": "/api/auth/oidc/login"})
    return providers


@app.get("/api/auth/oauth/providers")
def oauth_providers():
    """Public endpoint — list available SSO providers (OAuth + OIDC)."""
    return {"providers": _auth_providers()}

@app.get("/api/auth/oauth/{provider}/login")
def oauth_start(provider: str, request: Request):
    """Redirect user to OAuth provider."""
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/auth/oauth/{provider}/callback"
    if provider == "google" and OAUTH_GOOGLE_CLIENT_ID:
        import urllib.parse
        params = urllib.parse.urlencode({
            "client_id": OAUTH_GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
        })
        return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
    elif provider == "github" and OAUTH_GITHUB_CLIENT_ID:
        import urllib.parse
        params = urllib.parse.urlencode({
            "client_id": OAUTH_GITHUB_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "user:email",
        })
        return RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")
    raise HTTPException(404, f"OAuth provider '{provider}' not configured")

@app.get("/api/auth/oauth/{provider}/callback")
def oauth_callback(provider: str, code: str, request: Request):
    """Handle OAuth callback, exchange code for token, create/update user."""
    import urllib.parse
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/auth/oauth/{provider}/callback"

    if provider == "google" and OAUTH_GOOGLE_CLIENT_ID:
        # Exchange code for tokens
        import httpx
        token_resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": OAUTH_GOOGLE_CLIENT_ID,
            "client_secret": OAUTH_GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            return RedirectResponse(f"/?error=oauth_failed")
        tokens = token_resp.json()
        # Get user info
        userinfo_resp = httpx.get("https://openidconnect.googleapis.com/v1/userinfo",
                                  headers={"Authorization": f"Bearer {tokens['access_token']}"})
        if userinfo_resp.status_code != 200:
            return RedirectResponse(f"/?error=oauth_failed")
        userinfo = userinfo_resp.json()
        email = userinfo.get("email", "")
        username = email.split("@")[0] if email else userinfo.get("sub", "unknown")
        domain = email.split("@")[1] if "@" in email else ""

        if OAUTH_ALLOWED_DOMAINS and domain not in OAUTH_ALLOWED_DOMAINS:
            return RedirectResponse(f"/?error=domain_not_allowed")

    elif provider == "github" and OAUTH_GITHUB_CLIENT_ID:
        import httpx
        token_resp = httpx.post("https://github.com/login/oauth/access_token", data={
            "client_id": OAUTH_GITHUB_CLIENT_ID,
            "client_secret": OAUTH_GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        }, headers={"Accept": "application/json"})
        if token_resp.status_code != 200:
            return RedirectResponse(f"/?error=oauth_failed")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        if not access_token:
            return RedirectResponse(f"/?error=oauth_failed")
        # Get GitHub user
        user_resp = httpx.get("https://api.github.com/user",
                              headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
        if user_resp.status_code != 200:
            return RedirectResponse(f"/?error=oauth_failed")
        gh_user = user_resp.json()
        username = gh_user.get("login", "unknown")
        email = gh_user.get("email") or ""
        domain = email.split("@")[1] if "@" in email else ""

        if OAUTH_ALLOWED_DOMAINS and domain and domain not in OAUTH_ALLOWED_DOMAINS:
            return RedirectResponse(f"/?error=domain_not_allowed")
    else:
        return RedirectResponse(f"/?error=unknown_provider")

    # Sync to local DB through the hardened federated chokepoint (rejects a login
    # for a username already owned by a different auth source — e.g. local admin).
    try:
        role, totp_enabled = _sync_federated_user(
            username, "oauth", "OAUTH", OAUTH_DEFAULT_ROLE, mapped_role=None)
    except FederatedAuthError:
        _audit("login_failed", username, details="oauth account-source conflict")
        return RedirectResponse("/?error=account_conflict")

    # Check 2FA
    if totp_enabled:
        pending_token = jwt.encode(
            {"sub": username, "role": role, "purpose": "2fa",
             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            JWT_SECRET, algorithm="HS256")
        response = RedirectResponse("/?requires_2fa=true")
        _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
        response.set_cookie("access_token", pending_token, httponly=True, samesite="strict",
                            secure=_secure_cookie, max_age=300, path="/")
        return response

    # Issue JWT and redirect to app
    token = jwt.encode(
        {"sub": username, "role": role,
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = RedirectResponse("/")
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    _audit("login", username, details=f"method=oauth_{provider}")
    return response


# ── API: OpenID Connect (generic OIDC) ──────────────────────────────────────
#
# Unlike the hardcoded Google/GitHub OAuth above, this is a standards-compliant
# OIDC client for ANY issuer (Keycloak, Okta, Auth0, Entra ID, Authentik, …):
#   • discovers endpoints from <issuer>/.well-known/openid-configuration
#   • protects the round-trip with state + nonce + PKCE (S256)
#   • validates the ID token properly — signature via the issuer's JWKS, plus
#     iss / aud / exp — before trusting any claim
#
# Per the product decision (issue #9): we sync ONLY the username. Role and
# per-bucket permissions are NOT derived from OIDC claims/groups — a brand-new
# user lands as OIDC_DEFAULT_ROLE (viewer) with zero bucket grants, and an admin
# assigns access. An existing user's role/permissions are never overwritten on
# login, so OIDC can't silently escalate or downgrade someone an admin set up.

OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email")
OIDC_USERNAME_CLAIM = os.environ.get("OIDC_USERNAME_CLAIM", "preferred_username")
OIDC_PROVIDER_NAME = os.environ.get("OIDC_PROVIDER_NAME", "SSO")
OIDC_DEFAULT_ROLE = os.environ.get("OIDC_DEFAULT_ROLE", "viewer")
OIDC_ALLOWED_DOMAINS = [d.strip().lower() for d in os.environ.get("OIDC_ALLOWED_DOMAINS", "").split(",") if d.strip()]
# Optional group→role mapping (off by default → issue #9 username-only behaviour).
# When OIDC_ADMIN_GROUP is set, membership of that group in the OIDC_GROUPS_CLAIM
# claim maps the user to admin; everyone else gets OIDC_DEFAULT_ROLE. Mirrors LDAP.
OIDC_GROUPS_CLAIM = os.environ.get("OIDC_GROUPS_CLAIM", "groups")
OIDC_ADMIN_GROUP = os.environ.get("OIDC_ADMIN_GROUP", "")
# Optional hardening toggles.
OIDC_REQUIRE_VERIFIED_EMAIL = os.environ.get("OIDC_REQUIRE_VERIFIED_EMAIL", "false").lower() == "true"
OIDC_RP_LOGOUT = os.environ.get("OIDC_RP_LOGOUT", "false").lower() == "true"
# Enabled only when both an issuer and a client id are configured.
OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID)
# Only asymmetric algs — never HS*/none — so a leaked/forged token signed with a
# symmetric key (or the alg-confusion attack using the public key as an HMAC
# secret) can't pass validation.
_OIDC_ALLOWED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]

_oidc_discovery_cache: dict = {"doc": None, "fetched_at": 0}
_oidc_jwks_client = None  # lazily-built jwt.PyJWKClient (caches keys internally)


def _oidc_config() -> dict:
    """Fetch + cache (1h TTL) the issuer's OpenID discovery document."""
    import httpx
    now = time.time()
    if _oidc_discovery_cache["doc"] and now - _oidc_discovery_cache["fetched_at"] < 3600:
        return _oidc_discovery_cache["doc"]
    resp = httpx.get(f"{OIDC_ISSUER}/.well-known/openid-configuration", timeout=10)
    resp.raise_for_status()
    doc = resp.json()
    _oidc_discovery_cache["doc"] = doc
    _oidc_discovery_cache["fetched_at"] = now
    return doc


def _oidc_jwks():
    """Lazy, cached PyJWKClient pointed at the issuer's jwks_uri."""
    global _oidc_jwks_client
    if _oidc_jwks_client is None:
        _oidc_jwks_client = jwt.PyJWKClient(_oidc_config()["jwks_uri"])
    return _oidc_jwks_client


def _oidc_userinfo(access_token: str, cfg: dict) -> dict:
    """Best-effort fetch of the userinfo endpoint, to fill claims an IdP omits
    from the ID token (commonly groups or email). Never raises into the flow."""
    import httpx
    ep = cfg.get("userinfo_endpoint")
    if not ep or not access_token:
        return {}
    try:
        r = httpx.get(ep, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _oidc_mapped_role(claims: dict):
    """Optional group→role mapping. Returns None when OIDC_ADMIN_GROUP is unset
    (username-only mode — role left to admins). Otherwise returns 'admin' if the
    user is in the admin group, else OIDC_DEFAULT_ROLE — a provider-managed role
    that re-syncs on every login (so removing someone from the group demotes them).

    Matching is on the WHOLE group value or a delimited component — never a loose
    substring — so admin group "sairo-admins" does NOT match "sairo-admins-readonly".
    Handles plain names ("sairo-admins"), Keycloak paths ("/parent/sairo-admins"),
    and LDAP/AD DNs ("cn=sairo-admins,ou=groups,dc=corp")."""
    if not OIDC_ADMIN_GROUP:
        return None
    want = OIDC_ADMIN_GROUP.strip().strip("/").lower()
    raw = claims.get(OIDC_GROUPS_CLAIM)
    groups = raw if isinstance(raw, list) else ([raw] if raw else [])
    for g in groups:
        g = str(g).strip().lower()
        # Candidate identities for this group: the whole value, each path/DN
        # component, and the value side of any "key=value" DN component.
        candidates = {g, g.strip("/")}
        for part in g.replace(",", "/").split("/"):
            part = part.strip()
            if not part:
                continue
            candidates.add(part)
            if "=" in part:
                candidates.add(part.split("=", 1)[1].strip())
        if want in candidates:
            return "admin"
    return OIDC_DEFAULT_ROLE


@app.get("/api/auth/oidc/login")
def oidc_start(request: Request):
    """Begin the OIDC authorization-code (+ PKCE) flow: redirect to the issuer."""
    if not OIDC_ENABLED:
        raise HTTPException(404, "OIDC is not configured")
    import urllib.parse, hashlib, base64
    try:
        cfg = _oidc_config()
    except Exception:
        raise HTTPException(502, "OIDC issuer discovery failed")
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/auth/oidc/callback"
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)  # PKCE code_verifier
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = urllib.parse.urlencode({
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OIDC_SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    # Stash state/nonce/verifier in a short-lived signed cookie bound to this
    # browser. SameSite=Lax (not Strict) so it survives the top-level redirect
    # back from the IdP; Strict would drop it on the cross-site return.
    state_token = jwt.encode(
        {"state": state, "nonce": nonce, "cv": verifier, "purpose": "oidc_state",
         "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        JWT_SECRET, algorithm="HS256")
    response = RedirectResponse(f"{cfg['authorization_endpoint']}?{params}")
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("oidc_state", state_token, httponly=True, samesite="lax",
                        secure=_secure_cookie, max_age=600, path="/api/auth/oidc")
    return response


@app.get("/api/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle the OIDC redirect: verify state, exchange code, validate the ID token."""
    if not OIDC_ENABLED:
        raise HTTPException(404, "OIDC is not configured")
    if error:
        return RedirectResponse("/?error=oidc_failed")

    # 1) CSRF: the state param must match the signed state cookie we set.
    state_cookie = request.cookies.get("oidc_state")
    if not state_cookie or not code or not state:
        return RedirectResponse("/?error=oidc_failed")
    try:
        st = jwt.decode(state_cookie, JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return RedirectResponse("/?error=oidc_failed")
    if st.get("purpose") != "oidc_state" or not secrets.compare_digest(st.get("state", ""), state):
        return RedirectResponse("/?error=oidc_state_mismatch")

    import httpx
    try:
        cfg = _oidc_config()
    except Exception:
        return RedirectResponse("/?error=oidc_failed")
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/auth/oidc/callback"

    # 2) Exchange the code (with the PKCE verifier) for tokens.
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": OIDC_CLIENT_ID,
        "code_verifier": st.get("cv", ""),
    }
    if OIDC_CLIENT_SECRET:
        data["client_secret"] = OIDC_CLIENT_SECRET
    try:
        token_resp = httpx.post(cfg["token_endpoint"], data=data, timeout=10,
                                headers={"Accept": "application/json"})
    except Exception:
        return RedirectResponse("/?error=oidc_failed")
    if token_resp.status_code != 200:
        return RedirectResponse("/?error=oidc_failed")
    tokens = token_resp.json()
    id_token = tokens.get("id_token")
    access_token = tokens.get("access_token")
    if not id_token:
        return RedirectResponse("/?error=oidc_no_id_token")

    # 3) Validate the ID token: signature via JWKS + iss/aud/exp. This is the
    #    step that makes the claims trustworthy — do NOT skip it.
    try:
        signing_key = _oidc_jwks().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key,
            algorithms=_OIDC_ALLOWED_ALGS,
            audience=OIDC_CLIENT_ID,
            issuer=cfg.get("issuer", OIDC_ISSUER),
            leeway=60,  # tolerate small clock skew between us and the IdP
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except Exception:
        _audit("login_failed", "(oidc)", details="id_token validation failed")
        return RedirectResponse("/?error=oidc_invalid_token")

    # 4) Replay protection: the nonce must match the one we sent.
    if claims.get("nonce") != st.get("nonce"):
        return RedirectResponse("/?error=oidc_nonce_mismatch")

    # 4a) Authorized-party: when present (typically with multiple audiences), azp
    #     MUST be our client id — otherwise the token was minted for someone else.
    if claims.get("azp") and claims["azp"] != OIDC_CLIENT_ID:
        _audit("login_failed", "(oidc)", details="azp mismatch")
        return RedirectResponse("/?error=oidc_invalid_token")

    # 4b) Fill claims some IdPs keep out of the ID token (commonly groups/email)
    #     from the userinfo endpoint — only when we actually need them.
    need_username = not claims.get(OIDC_USERNAME_CLAIM)
    need_groups = bool(OIDC_ADMIN_GROUP) and not claims.get(OIDC_GROUPS_CLAIM)
    need_email = (OIDC_REQUIRE_VERIFIED_EMAIL or bool(OIDC_ALLOWED_DOMAINS)) and not claims.get("email")
    if access_token and (need_username or need_groups or need_email):
        for k, v in _oidc_userinfo(access_token, cfg).items():
            claims.setdefault(k, v)

    # 5) Optional email checks.
    email = claims.get("email", "") or ""
    if OIDC_REQUIRE_VERIFIED_EMAIL and claims.get("email_verified") is not True:
        return RedirectResponse("/?error=email_not_verified")
    if OIDC_ALLOWED_DOMAINS:
        domain = email.split("@")[1].lower() if "@" in email else ""
        if domain not in OIDC_ALLOWED_DOMAINS:
            return RedirectResponse("/?error=domain_not_allowed")

    # 6) Pick the username claim (sync ONLY the username, unless group mapping is on).
    username = str(claims.get(OIDC_USERNAME_CLAIM) or claims.get("preferred_username")
                   or claims.get("email") or claims.get("sub") or "").strip()
    if not username:
        return RedirectResponse("/?error=oidc_no_username")

    # 7) Sync through the hardened federated chokepoint:
    #    new user → default role + no bucket grants (admin assigns later);
    #    existing user → reject if it belongs to a different auth source
    #    (account-takeover guard, protects the local admin especially).
    #    mapped_role is None in username-only mode, or admin/viewer when groups map.
    try:
        role, totp_enabled = _sync_federated_user(
            username, "oidc", "OIDC", OIDC_DEFAULT_ROLE, mapped_role=_oidc_mapped_role(claims))
    except FederatedAuthError:
        _audit("login_failed", username, details="oidc account-source conflict")
        return RedirectResponse("/?error=account_conflict")

    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"

    # 7a) If the synced user has 2FA, hand off to the existing 2FA flow.
    if totp_enabled:
        pending_token = jwt.encode(
            {"sub": username, "role": role, "purpose": "2fa",
             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            JWT_SECRET, algorithm="HS256")
        response = RedirectResponse("/?requires_2fa=true")
        response.set_cookie("access_token", pending_token, httponly=True, samesite="strict",
                            secure=_secure_cookie, max_age=300, path="/")
        response.delete_cookie("oidc_state", path="/api/auth/oidc")
        return response

    # 7b) Issue the normal session token and land in the app.
    token = jwt.encode(
        {"sub": username, "role": role,
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = RedirectResponse("/")
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    response.delete_cookie("oidc_state", path="/api/auth/oidc")
    _audit("login", username, details="method=oidc")
    return response


# ── API: Audit Log ──────────────────────────────────────────────────────────

@app.get("/api/audit-log")
def get_audit_log(
    limit: int = 50,
    offset: int = 0,
    action: str = "",
    username: str = "",
    bucket: str = "",
    user: dict = Depends(require_admin),
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    clauses = []
    params = []
    if action:
        clauses.append("action = ?")
        params.append(action)
    if username:
        clauses.append("username LIKE ?")
        params.append(f"%{username}%")
    if bucket:
        clauses.append("bucket = ?")
        params.append(bucket)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _get_users_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM audit_log {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT id, timestamp, username, action, bucket, details FROM audit_log {where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return {"entries": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# ── Health Check ──────────────────────────────────────────────────────────

@app.get("/healthz")
@limiter.exempt
def healthz():
    # Liveness: just confirm the process is responsive.
    # Storage checks belong in readiness, not liveness — killing the pod
    # on a transient Longhorn unmount just causes a restart loop.
    return {"status": "ok"}


@app.get("/readyz")
@limiter.exempt
def readyz():
    # Readiness: verify /data is writable so k8s stops routing traffic
    # during transient PVC issues, without killing the pod.
    try:
        probe = os.path.join(DB_DIR, ".readyz_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:
        log.warning("Readiness check failed — DB_DIR '%s' not writable: %s", DB_DIR, e)
        return JSONResponse(status_code=503, content={"status": "error", "detail": f"storage not writable: {e}"})
    return {"status": "ok"}


# ── S3 Health Check ──────────────────────────────────────────────────────

_s3_health_cache: dict = {}  # keyed by endpoint_id -> {"result": ..., "ts": ...}
_S3_HEALTH_TTL = 300  # 5 minutes

def _check_s3_feature(name: str, fn):
    """Run a health check probe, return {name, status, detail}."""
    try:
        result = fn()
        return {"name": name, "status": "pass", "detail": result or "OK"}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        # These error codes mean the feature IS supported, just not configured
        supported_errors = {
            "NoSuchLifecycleConfiguration", "NoSuchCORSConfiguration",
            "NoSuchTagSet", "NoSuchBucketPolicy", "NoSuchWebsiteConfiguration",
            "ServerSideEncryptionConfigurationNotFoundError",
        }
        if code in supported_errors:
            return {"name": name, "status": "pass", "detail": "Supported (not configured)"}
        unsupported_errors = {
            "ObjectLockConfigurationNotFoundError", "NotImplemented",
            "MethodNotAllowed", "UnsupportedOperation",
        }
        if code in unsupported_errors:
            return {"name": name, "status": "unsupported", "detail": code}
        return {"name": name, "status": "fail", "detail": f"{code}: {e.response['Error'].get('Message', '')}"}
    except Exception as e:
        return {"name": name, "status": "fail", "detail": str(e)[:200]}


def _run_health_check_for_endpoint(endpoint_id: str):
    """Run health check probes against a specific endpoint."""
    client = _s3_manager.get_client(endpoint_id)
    info = _s3_manager.get_endpoint_info(endpoint_id) or {}
    checks = []

    # 1. Connection test
    test_bucket = None
    try:
        resp = client.list_buckets()
        buckets = resp.get("Buckets", [])
        checks.append({"name": "Connection", "status": "pass", "detail": f"{len(buckets)} buckets found"})
        if buckets:
            test_bucket = buckets[0]["Name"]
    except Exception as e:
        checks.append({"name": "Connection", "status": "fail", "detail": str(e)[:200]})

    if test_bucket:
        # 2-9: Feature probes using first available bucket
        checks.append(_check_s3_feature("Versioning",
            lambda: client.get_bucket_versioning(Bucket=test_bucket) and "OK"))
        checks.append(_check_s3_feature("Lifecycle Rules",
            lambda: client.get_bucket_lifecycle_configuration(Bucket=test_bucket) and "OK"))
        checks.append(_check_s3_feature("CORS",
            lambda: client.get_bucket_cors(Bucket=test_bucket) and "OK"))
        checks.append(_check_s3_feature("ACL",
            lambda: client.get_bucket_acl(Bucket=test_bucket) and "OK"))
        checks.append(_check_s3_feature("Tagging",
            lambda: client.get_bucket_tagging(Bucket=test_bucket) and "OK"))
        checks.append(_check_s3_feature("Object Lock",
            lambda: client.get_object_lock_configuration(Bucket=test_bucket) and "OK"))
        checks.append(_check_s3_feature("Multipart Uploads",
            lambda: client.list_multipart_uploads(Bucket=test_bucket) and "OK"))
        checks.append(_check_s3_feature("Presigned URLs",
            lambda: client.generate_presigned_url("get_object", Params={"Bucket": test_bucket, "Key": "_healthcheck"}, ExpiresIn=60) and "OK"))

    return {
        "endpoint_id": endpoint_id,
        "endpoint_url": info.get("endpoint_url", S3_ENDPOINT if endpoint_id == "default" else "unknown"),
        "checks": checks,
        "tested_bucket": test_bucket,
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "passed": sum(1 for c in checks if c["status"] == "pass"),
        "total": len(checks),
    }


@app.get("/api/health/s3")
def s3_health_check(endpoint_id: str = "", user: dict = Depends(require_admin)):
    """Probe S3 endpoint(s) for connectivity and feature support.
    Pass endpoint_id to check a specific endpoint, or omit to check all."""
    now = time.time()

    if endpoint_id:
        # Single endpoint check
        cached = _s3_health_cache.get(endpoint_id)
        if cached and now - cached.get("ts", 0) < _S3_HEALTH_TTL:
            return cached["result"]
        result = _run_health_check_for_endpoint(endpoint_id)
        _s3_health_cache[endpoint_id] = {"result": result, "ts": now}
        return result

    # Check all endpoints
    all_ids = _s3_manager.get_all_ids()
    results = []
    for eid in all_ids:
        cached = _s3_health_cache.get(eid)
        if cached and now - cached.get("ts", 0) < _S3_HEALTH_TTL:
            results.append(cached["result"])
        else:
            result = _run_health_check_for_endpoint(eid)
            _s3_health_cache[eid] = {"result": result, "ts": now}
            results.append(result)

    # If only one endpoint, return flat result (same format as single-endpoint)
    if len(results) == 1:
        return results[0]
    return {"endpoints": results}


@app.post("/api/health/s3/refresh")
def s3_health_refresh(endpoint_id: str = "", user: dict = Depends(require_admin)):
    """Clear cached health check and run fresh. Pass endpoint_id for a specific endpoint, or omit for all."""
    if endpoint_id:
        _s3_health_cache.pop(endpoint_id, None)
        return s3_health_check(endpoint_id=endpoint_id, user=user)
    _s3_health_cache.clear()
    return s3_health_check(endpoint_id="", user=user)


@app.get("/api/system-info")
def system_info(user: dict = Depends(get_current_user)):
    return {
        "s3_endpoint": S3_ENDPOINT,
        "version": "1.0.0",
        "health": "ok",
        "session_hours": SESSION_HOURS,
    }


@app.get("/api/health-detail")
def health_detail(user: dict = Depends(require_admin)):
    """Comprehensive health check with S3 connectivity, DB status, crawler state, uptime."""
    result = {
        "status": "ok",
        "version": "1.0.0",
        "uptime_seconds": int(time.time() - _app_start_time),
        "s3_endpoint": S3_ENDPOINT,
        "s3_region": os.environ.get("S3_REGION", ""),
        "session_hours": SESSION_HOURS,
        "recrawl_interval": RECRAWL_INTERVAL,
        "s3_connected": False,
        "s3_latency_ms": None,
        "s3_error": None,
        "user_count": 0,
        "bucket_count": 0,
        "buckets": [],
        "db_dir": DB_DIR,
        "db_writable": False,
    }

    # Check S3 connectivity + latency
    try:
        t0 = time.time()
        s3.list_buckets()
        latency = int((time.time() - t0) * 1000)
        result["s3_connected"] = True
        result["s3_latency_ms"] = latency
    except Exception as e:
        result["status"] = "degraded"
        result["s3_error"] = str(e)

    # Check DB dir writable
    try:
        test_path = os.path.join(DB_DIR, ".health_check_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        result["db_writable"] = True
    except Exception:
        result["status"] = "degraded"

    # User count
    try:
        with _get_users_db() as db:
            row = db.execute("SELECT COUNT(*) FROM users").fetchone()
            result["user_count"] = row[0] if row else 0
    except Exception:
        pass

    # Per-bucket crawl status
    try:
        resp = s3.list_buckets()
        bucket_names = [b["Name"] for b in resp.get("Buckets", [])]
        result["bucket_count"] = len(bucket_names)
        for name in bucket_names:
            bucket_info = {"name": name, "indexed": False, "status": "not_indexed", "total_objects": 0, "total_size": 0, "last_crawl": None}
            if os.path.exists(_db_path(name)):
                try:
                    with _get_db(name) as db:
                        row = db.execute("SELECT status, total_objects, total_size, last_crawl_end FROM crawl_status WHERE id=1").fetchone()
                        if row:
                            bucket_info["indexed"] = True
                            st = row["status"]
                            bucket_info["status"] = "ready" if st == "complete" else st
                            bucket_info["total_objects"] = row["total_objects"] or 0
                            bucket_info["total_size"] = row["total_size"] or 0
                            bucket_info["last_crawl"] = row["last_crawl_end"]
                except Exception:
                    pass
            with _crawl_lock:
                bucket_info["crawling"] = name in _crawling
            result["buckets"].append(bucket_info)
    except Exception:
        pass

    return result


# ── API: S3 Endpoints (Multi-Endpoint) ────────────────────────────────────

class EndpointCreateRequest(BaseModel):
    id: str
    name: str
    endpoint_url: str
    access_key: str
    secret_key: str
    region: str = ""
    path_style: bool = False

class EndpointUpdateRequest(BaseModel):
    name: str = ""
    endpoint_url: str = ""
    access_key: str = ""
    secret_key: str = ""
    region: str = ""
    path_style: bool = False

@app.get("/api/endpoints")
def list_endpoints(user: dict = Depends(require_admin)):
    """List all S3 endpoints (secrets masked)."""
    with _get_users_db() as db:
        rows = db.execute("SELECT id, name, endpoint_url, access_key, region, path_style, is_default, created_at, created_by FROM s3_endpoints ORDER BY is_default DESC, created_at").fetchall()
    eps = []
    for r in rows:
        d = dict(r)
        ak = _decrypt(d.pop("access_key", ""))
        d["access_key_masked"] = ak[:4] + "****" if len(ak) > 4 else "****"
        eps.append(d)
    return {"endpoints": eps}

@app.post("/api/endpoints")
def create_endpoint(req: EndpointCreateRequest, user: dict = Depends(require_admin)):
    """Add a new S3 endpoint. Tests connectivity before saving."""
    if not req.id or not req.id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "ID must be alphanumeric (dashes/underscores ok)")
    if req.id == "default":
        raise HTTPException(400, "Cannot use 'default' as endpoint ID")
    # Test connectivity
    try:
        test_client = boto3.client(
            "s3", endpoint_url=req.endpoint_url,
            aws_access_key_id=req.access_key, aws_secret_access_key=req.secret_key,
            config=Config(signature_version="s3v4", connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}),
        )
        test_client.list_buckets()
    except Exception as e:
        raise HTTPException(400, f"Connection test failed: {str(e)[:200]}")
    with _get_users_db() as db:
        existing = db.execute("SELECT id FROM s3_endpoints WHERE id=?", (req.id,)).fetchone()
        if existing:
            raise HTTPException(409, f"Endpoint '{req.id}' already exists")
        db.execute(
            "INSERT INTO s3_endpoints (id, name, endpoint_url, access_key, secret_key, region, path_style, created_by) VALUES (?,?,?,?,?,?,?,?)",
            (req.id, req.name, req.endpoint_url, _encrypt(req.access_key), _encrypt(req.secret_key), req.region, int(req.path_style), user["username"]))
        db.commit()
    _s3_manager.register(req.id, req.endpoint_url, req.access_key, req.secret_key, req.region, req.path_style)
    _audit("create_endpoint", user["username"], details=f"endpoint={req.id}, url={req.endpoint_url}")
    # Immediately crawl all buckets from the new endpoint
    try:
        client = _s3_manager.get_client(req.id)
        resp = client.list_buckets()
        for b in resp.get("Buckets", []):
            name = b["Name"]
            _init_db(name, req.id)
            _queue_crawl(name, req.id)
            log.info("Queued initial crawl for %s:%s", req.id, name)
    except Exception as e:
        log.warning("Failed to queue initial crawls for endpoint %s: %s", req.id, e)
    return {"created": req.id}

@app.put("/api/endpoints/{endpoint_id}")
def update_endpoint(endpoint_id: str, req: EndpointUpdateRequest, user: dict = Depends(require_admin)):
    """Update an S3 endpoint."""
    with _get_users_db() as db:
        existing = db.execute("SELECT * FROM s3_endpoints WHERE id=?", (endpoint_id,)).fetchone()
        if not existing:
            raise HTTPException(404, f"Endpoint '{endpoint_id}' not found")
        name = req.name or existing["name"]
        url = req.endpoint_url or existing["endpoint_url"]
        # For credentials: use new plaintext if provided, otherwise decrypt existing
        ak = req.access_key if req.access_key else _decrypt(existing["access_key"])
        sk = req.secret_key if req.secret_key else _decrypt(existing["secret_key"])
        region = req.region if req.region is not None else existing["region"]
        path_style = req.path_style
        db.execute(
            "UPDATE s3_endpoints SET name=?, endpoint_url=?, access_key=?, secret_key=?, region=?, path_style=? WHERE id=?",
            (name, url, _encrypt(ak), _encrypt(sk), region, int(path_style), endpoint_id))
        db.commit()
    _s3_manager.register(endpoint_id, url, ak, sk, region, path_style)
    _audit("update_endpoint", user["username"], details=f"endpoint={endpoint_id}")
    return {"updated": endpoint_id}

@app.delete("/api/endpoints/{endpoint_id}")
def delete_endpoint(endpoint_id: str, user: dict = Depends(require_admin)):
    """Delete an S3 endpoint (cannot delete default)."""
    if endpoint_id == "default":
        raise HTTPException(400, "Cannot delete the default endpoint")
    with _get_users_db() as db:
        existing = db.execute("SELECT id FROM s3_endpoints WHERE id=?", (endpoint_id,)).fetchone()
        if not existing:
            raise HTTPException(404, f"Endpoint '{endpoint_id}' not found")
        db.execute("DELETE FROM s3_endpoints WHERE id=?", (endpoint_id,))
        db.commit()
    _s3_manager.invalidate(endpoint_id)
    _audit("delete_endpoint", user["username"], details=f"endpoint={endpoint_id}")
    return {"deleted": endpoint_id}

@app.post("/api/endpoints/{endpoint_id}/test")
def test_endpoint(endpoint_id: str, user: dict = Depends(require_admin)):
    """Test connectivity of an S3 endpoint."""
    try:
        client = _s3_manager.get_client(endpoint_id)
        resp = client.list_buckets()
        count = len(resp.get("Buckets", []))
        return {"status": "ok", "buckets": count}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "detail": str(e)[:200]}

@app.get("/api/all-buckets")
def list_all_buckets(user: dict = Depends(get_current_user)):
    """List buckets from all endpoints, grouped by endpoint."""
    result = []
    with _get_users_db() as db:
        endpoints = db.execute("SELECT id, name, endpoint_url FROM s3_endpoints ORDER BY is_default DESC, created_at").fetchall()
    # AUTH_MODE=s3: scope to the single endpoint the user authenticated against, listed
    # with THEIR keys — so they see only their account's buckets, not every endpoint's.
    s3_creds = _user_creds_ctx.get(None)
    if s3_creds and s3_creds.get("ak"):
        bound_eid = _current_endpoint_id()  # middleware bound this to the user's login endpoint
        endpoints = [ep for ep in endpoints if ep["id"] == bound_eid] or [ep for ep in endpoints if ep["id"] == "default"]
    for ep in endpoints:
        eid = ep["id"]
        try:
            client = _s3_manager.get_client(eid)
            resp = client.list_buckets()
            buckets = [{"name": b["Name"], "created": b.get("CreationDate", "").isoformat() if hasattr(b.get("CreationDate", ""), "isoformat") else str(b.get("CreationDate", ""))} for b in resp.get("Buckets", [])]
            # Filter for non-admin
            if user["role"] != "admin":
                with _get_users_db() as udb:
                    rows = udb.execute("SELECT bucket, permission FROM bucket_permissions WHERE username=?", (user["username"],)).fetchall()
                allowed = {r["bucket"]: r["permission"] for r in rows}
                buckets = [b for b in buckets if b["name"] in allowed]
                for b in buckets:
                    b["permission"] = allowed.get(b["name"], "read")
            # Add index status for each bucket
            for b in buckets:
                db_file = _db_path(b["name"], eid)
                if os.path.exists(db_file):
                    try:
                        with _get_db(b["name"], eid) as bdb:
                            row = bdb.execute("SELECT total_objects, total_size, status FROM crawl_status WHERE id=1").fetchone()
                            if row:
                                b["index_status"] = row["status"]
                                b["object_count"] = row["total_objects"]
                                b["total_size"] = row["total_size"]
                    except Exception as db_e:
                        log.debug("Failed to read index stats for %s/%s: %s", eid, b["name"], db_e)
            result.append({"endpoint_id": eid, "endpoint_name": ep["name"], "endpoint_url": ep["endpoint_url"], "buckets": buckets})
        except Exception as e:
            result.append({"endpoint_id": eid, "endpoint_name": ep["name"], "endpoint_url": ep["endpoint_url"], "buckets": [], "error": str(e)[:200]})
    return {"endpoints": result}


# ── API: Buckets ──────────────────────────────────────────────────────────

_bucket_list_cache: dict = {"data": None, "ts": 0}
_bucket_list_cache_lock = threading.Lock()
_BUCKET_LIST_TTL = 30  # seconds

@app.get("/api/buckets")
def list_buckets(user: dict = Depends(get_current_user)):
    now = time.time()
    s3_creds = _user_creds_ctx.get(None)
    if s3_creds and s3_creds.get("ak"):
        # AUTH_MODE=s3: list with the USER's keys so the provider IAM scopes the result.
        # Never use the shared cache here — it's keyed by nothing and would leak one
        # user's bucket list to another.
        resp = s3.list_buckets()
    else:
        with _bucket_list_cache_lock:
            if _bucket_list_cache["data"] and now - _bucket_list_cache["ts"] < _BUCKET_LIST_TTL:
                resp = _bucket_list_cache["data"]
            else:
                resp = s3.list_buckets()
                _bucket_list_cache["data"] = resp
                _bucket_list_cache["ts"] = now
    # Non-admin: only show buckets with explicit permissions. S3-key users are already
    # scoped by their keys above, so no extra filter (their role is admin).
    allowed = None
    if user["role"] != "admin":
        with _get_users_db() as udb:
            rows = udb.execute(
                "SELECT bucket, permission FROM bucket_permissions WHERE username=?",
                (user["username"],)
            ).fetchall()
        allowed = {r["bucket"]: r["permission"] for r in rows}
    buckets = []
    for b in resp.get("Buckets", []):
        name = b["Name"]
        if allowed is not None and name not in allowed:
            continue
        info = {"name": name, "created": b["CreationDate"].isoformat()}
        if allowed is not None:
            info["permission"] = allowed[name]
        # Add index status if available
        if os.path.exists(_db_path(name)):
            try:
                with _get_db(name) as db:
                    row = db.execute("SELECT total_objects, total_size, status FROM crawl_status WHERE id=1").fetchone()
                    if row:
                        info["index_status"] = row["status"]
                        info["object_count"] = row["total_objects"]
                        info["total_size"] = row["total_size"]
            except Exception as db_e:
                log.debug("Failed to read index stats for bucket %s: %s", name, db_e)
        buckets.append(info)
    return {"buckets": buckets, "owner": resp.get("Owner", {}).get("DisplayName", "")}


class CreateBucketRequest(BaseModel):
    name: str

@app.post("/api/buckets")
def create_bucket(req: CreateBucketRequest, user: dict = Depends(require_admin)):
    _validate_name(req.name, "bucket name")
    s3.create_bucket(Bucket=req.name)
    _init_db(req.name)
    _audit("create_bucket", user["username"], bucket=req.name)
    return {"created": req.name}


@app.delete("/api/buckets/{bucket}")
def delete_bucket(bucket: str, user: dict = Depends(require_admin)):
    s3.delete_bucket(Bucket=bucket)
    # Remove index DB
    db_file = _db_path(bucket)
    for ext in ["", "-wal", "-shm"]:
        try:
            os.remove(db_file + ext)
        except FileNotFoundError:
            pass
    _audit("delete_bucket", user["username"], bucket=bucket)
    return {"deleted": bucket}


# ── API: Listing ────────────────────────────────────────────────────────────

def _list_from_index(bucket, prefix, cursor=None, limit=None):
    """Return (folders, files, next_cursor) for `prefix` from the index.

    When `limit` is set, returns one keyset page of files (key > cursor) plus a
    `next_cursor` (None when exhausted). Folders are returned only on the first
    page (cursor empty). When `limit` is falsy, returns every file (legacy mode).
    """
    prefix_len = len(prefix)
    t0 = time.monotonic()
    next_cursor = None
    first_page = not cursor  # folders are delivered only on the first page

    with _get_db(bucket) as db:
        seen = set()
        folders = []

        if first_page and not prefix:
            # Root level: use discovered_prefixes (instant) instead of scanning all objects
            try:
                dp_rows = db.execute("SELECT prefix FROM discovered_prefixes").fetchall()
                for (dp,) in dp_rows:
                    if dp and dp not in seen:
                        seen.add(dp)
                        name = dp.rstrip("/")
                        if name:
                            folders.append({"prefix": dp, "name": name})
            except Exception as dp_e:
                log.debug("Discovered prefixes query failed (old DB?): %s", dp_e)
            # Also check folder_stats for any top-level prefixes not in discovered_prefixes
            try:
                fs_rows = db.execute("SELECT prefix FROM folder_stats WHERE prefix != ''").fetchall()
                for (fp,) in fs_rows:
                    if fp and fp not in seen:
                        seen.add(fp)
                        name = fp.rstrip("/")
                        if name:
                            folders.append({"prefix": fp, "name": name})
            except Exception:
                pass
        elif first_page:
            # Subfolder: always compute from objects table (DISTINCT scan) to avoid stale cache.
            # prefix_children is only reliably maintained for level 1 (parent="").
            t_q = time.monotonic()
            prefix_end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            rows = db.execute(
                "SELECT DISTINCT substr(prefix, 1, ? + instr(substr(prefix, ?+1), '/')) "
                "FROM objects WHERE prefix >= ? AND prefix < ? "
                "AND instr(substr(prefix, ?+1), '/') > 0",
                (prefix_len, prefix_len, prefix, prefix_end, prefix_len)).fetchall()
            log.info("[perf] _list_from_index DISTINCT: %.3fs (%d rows) prefix=%s",
                     time.monotonic() - t_q, len(rows), prefix[:60])
            for (child,) in rows:
                if child and child not in seen:
                    seen.add(child)
                    name = child[prefix_len:].rstrip("/")
                    if name:
                        folders.append({"prefix": child, "name": name})

        folders.sort(key=lambda f: f["name"])
        t_files = time.monotonic()

        # Keyset pagination (O(page)) when limit is set; otherwise the legacy
        # full-folder fetch. Both ride the covering index — already ordered by key,
        # so no temp B-tree and no heap lookups.
        if limit and limit > 0:
            file_rows = db.execute(
                "SELECT key, size, last_modified FROM objects "
                "WHERE prefix = ? AND key > ? ORDER BY key LIMIT ?",
                (prefix, cursor or "", limit)).fetchall()
            if len(file_rows) == limit:
                next_cursor = file_rows[-1]["key"]
        else:
            file_rows = db.execute(
                "SELECT key, size, last_modified FROM objects WHERE prefix = ? ORDER BY key",
                (prefix,)).fetchall()

        files = [
            {"key": r["key"], "name": r["key"][prefix_len:], "size": r["size"], "last_modified": r["last_modified"]}
            for r in file_rows
        ]
        log.info("[perf] _list_from_index files query: %.3fs (%d files) prefix=%s",
                 time.monotonic() - t_files, len(files), prefix[:60])

    log.info("[perf] _list_from_index total: %.3fs (%d folders, %d files) prefix=%s",
             time.monotonic() - t0, len(folders), len(files), prefix[:60])
    return folders, files, next_cursor


def _list_from_s3_streaming(bucket, prefix, endpoint_id=None):
    eid = endpoint_id or _current_endpoint_id()
    client = _s3_manager.get_client(eid)
    token = None
    all_folders = []
    all_files = []
    for _ in range(50):
        params = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            params["ContinuationToken"] = token
        resp = client.list_objects_v2(**params)
        folders = [{"prefix": cp["Prefix"], "name": cp["Prefix"][len(prefix):].rstrip("/")}
                    for cp in resp.get("CommonPrefixes", [])]
        files = [{"key": o["Key"], "name": o["Key"][len(prefix):], "size": o["Size"],
                  "last_modified": o["LastModified"].isoformat()}
                 for o in resp.get("Contents", []) if o["Key"] != prefix]
        all_folders.extend(folders)
        all_files.extend(files)
        yield json.dumps({"folders": folders, "files": files, "done": not resp.get("IsTruncated", False),
                          "total_folders": len(all_folders), "total_files": len(all_files)}) + "\n"
        if not resp.get("IsTruncated", False):
            break
        token = resp.get("NextContinuationToken")


@app.get("/api/buckets/{bucket}/list")
def list_objects(bucket: str, prefix: str = "", fresh: bool = False,
                 cursor: str = "", limit: int = 0, user: dict = Depends(get_current_user)):
    """List objects at a prefix. Uses index when available (instant), falls back to S3 streaming.
    Pass fresh=true to force a direct S3 listing bypassing the index.
    Pass limit>0 (with optional cursor) for keyset pagination: returns one page of
    files plus `next_cursor`; folders are included only on the first page (empty cursor).
    Omit limit for the legacy whole-folder response (next_cursor=null, done=true)."""
    eid = _current_endpoint_id()
    if _is_index_ready(bucket) and not fresh:
        folders, files, next_cursor = _list_from_index(bucket, prefix, cursor=cursor or None, limit=limit)
        def gen():
            yield json.dumps({"folders": folders, "files": files,
                              "done": next_cursor is None, "next_cursor": next_cursor,
                              "total_folders": len(folders), "total_files": len(files), "indexed": True}) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")
    return StreamingResponse(_list_from_s3_streaming(bucket, prefix, endpoint_id=eid), media_type="application/x-ndjson")


@app.post("/api/buckets/{bucket}/refresh-prefix")
def refresh_prefix(bucket: str, prefix: str = "", user: dict = Depends(require_admin)):
    """Quick S3 check for a single prefix — merges new/changed objects into the index.
    Much faster than a full crawl: only lists one delimiter-level and updates SQLite."""
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)
    if not os.path.exists(_db_path(bucket, eid)):
        return {"refreshed": False, "reason": "no_index"}

    s3_folders = []
    s3_files = {}
    token = None
    while True:
        params = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            params["ContinuationToken"] = token
        resp = client.list_objects_v2(**params)
        for cp in resp.get("CommonPrefixes", []):
            s3_folders.append(cp["Prefix"])
        for obj in resp.get("Contents", []):
            if obj["Key"] != prefix:
                s3_files[obj["Key"]] = obj
        if not resp.get("IsTruncated", False):
            break
        token = resp.get("NextContinuationToken")

    updated = 0
    with _get_db(bucket, eid) as db:
        for key, obj in s3_files.items():
            db.execute(
                "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth) VALUES (?,?,?,?,?,?)",
                (key, obj["Size"], obj["LastModified"].isoformat(),
                 obj.get("ETag", "").strip('"'), _key_prefix(key), _key_depth(key)))
            updated += 1

        # Remove objects from index that no longer exist at this prefix
        index_keys = {row[0] for row in db.execute("SELECT key FROM objects WHERE prefix=?", (prefix,)).fetchall()}
        s3_key_set = set(s3_files.keys())
        stale_keys = index_keys - s3_key_set
        if stale_keys:
            db.executemany("DELETE FROM objects WHERE key=?", [(k,) for k in stale_keys])
            updated += len(stale_keys)
        db.commit()

    if updated > 0:
        _update_crawl_counters(bucket, eid)
    return {"refreshed": True, "updated": updated, "files": len(s3_files), "folders": len(s3_folders)}


# ── API: Search ─────────────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/search")
@limiter.limit("60/minute")
def search_objects(bucket: str, request: Request, q: str = Query(..., min_length=1), prefix: str = "", limit: int = 200, user: dict = Depends(get_current_user)):
    _record_first_search()  # activation: reached search — record on the request (symmetric with
                            # first_dashboard_open_at), BEFORE the index-ready gate, so a search
                            # issued during the initial crawl still counts.
    if not _is_index_ready(bucket):
        raise HTTPException(503, "Index not ready — crawl in progress")
    with _get_db(bucket) as db:
        rows = _search_fts(db, q, prefix, limit)
    _record_search_returned(len(rows) > 0)  # diagnostic, on the first served search
    return {"results": [dict(r) for r in rows], "count": len(rows), "query": q}


def _search_fts(db, q, prefix, limit):
    """Search using FTS5 trigram index, falling back to LIKE for old DBs or short queries."""
    # Trigram tokenizer requires >= 3 char terms; fall back to LIKE for shorter
    if len(q) >= 3:
        try:
            fts_query = '"' + q.replace('"', '""') + '"'
            if prefix:
                rows = db.execute("""
                    SELECT o.key, o.size, o.last_modified FROM objects o
                    JOIN objects_fts f ON o.rowid = f.rowid
                    WHERE objects_fts MATCH ? AND o.key LIKE ?
                    ORDER BY o.key LIMIT ?
                """, (fts_query, prefix + "%", limit)).fetchall()
            else:
                rows = db.execute("""
                    SELECT o.key, o.size, o.last_modified FROM objects o
                    JOIN objects_fts f ON o.rowid = f.rowid
                    WHERE objects_fts MATCH ?
                    ORDER BY o.key LIMIT ?
                """, (fts_query, limit)).fetchall()
            if rows:
                return rows
            # FTS returned nothing: either a genuine no-match, or — critically — the FTS index is
            # still rebuilding right after a crawl (the crawl reports "complete" before the async
            # rebuild finishes). Fall through to LIKE so the user's first search after indexing
            # returns real results instead of a dead-end "no objects found".
        except Exception:
            pass  # FTS table missing or query error — fall back to LIKE
    # Fallback: LIKE pattern matching (works for all query lengths and old DBs)
    pattern = f"%{q}%"
    if prefix:
        return db.execute("SELECT key,size,last_modified FROM objects WHERE key LIKE ? AND key LIKE ? ORDER BY key LIMIT ?",
                          (prefix + "%", pattern, limit)).fetchall()
    else:
        return db.execute("SELECT key,size,last_modified FROM objects WHERE key LIKE ? ORDER BY key LIMIT ?",
                          (pattern, limit)).fetchall()


# ── API: Folder Size ────────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/folder-size")
def folder_size(bucket: str, prefix: str = "", user: dict = Depends(get_current_user)):
    if not _is_index_ready(bucket):
        raise HTTPException(503, "Index not ready")
    with _get_db(bucket) as db:
        if prefix:
            # Range scan on the covering index (prefix,key,size) — index-only, no
            # heap fetch, no full scan. Equivalent to key LIKE prefix||'%' because
            # every object under `prefix` has an immediate-parent prefix under it.
            row = db.execute(
                "SELECT COUNT(*) as count, COALESCE(SUM(size),0) as total_size "
                "FROM objects WHERE prefix >= ? AND prefix < ?",
                (prefix, _prefix_upper(prefix))).fetchone()
        else:
            # Fast path: use pre-computed totals from crawl_status
            row = db.execute("SELECT total_objects as count, total_size as total_size FROM crawl_status WHERE id=1").fetchone()
    return {"prefix": prefix or "(all)", "object_count": row["count"], "total_size": row["total_size"]}


# ── API: Storage Breakdown ──────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/storage-breakdown")
def storage_breakdown(bucket: str, prefix: str = "", user: dict = Depends(get_current_user)):
    _record_milestone_once("first_dashboard_open_at")  # activation milestone (fail-safe, idempotent)
    if not _is_index_ready(bucket):
        raise HTTPException(503, "Index not ready")

    # Fast path: use precomputed folder_stats for root-level breakdown
    if not prefix:
        with _get_db(bucket) as db:
            has_stats = False
            try:
                stats_count = db.execute("SELECT COUNT(*) FROM folder_stats").fetchone()[0]
                has_stats = stats_count > 0
            except Exception:
                pass

            if has_stats:
                rows = db.execute(
                    "SELECT prefix, object_count, total_size FROM folder_stats ORDER BY total_size DESC"
                ).fetchall()
                children = []
                for r in rows:
                    p = r["prefix"]
                    if p:  # folder
                        children.append({
                            "prefix": p, "name": p.rstrip("/"),
                            "object_count": r["object_count"], "total_size": r["total_size"]})
                    else:  # root-level files
                        if r["object_count"] > 0:
                            children.append({
                                "prefix": "(root files)", "name": "(files)",
                                "object_count": r["object_count"], "total_size": r["total_size"]})
                total_size = sum(c["total_size"] for c in children)
                total_count = sum(c["object_count"] for c in children)
                result = {"prefix": "(root)", "total_size": total_size,
                          "object_count": total_count, "children": children}
                return result

    # Sub-prefix breakdown: always compute from objects table for accuracy.
    # prefix_children cache is only reliable for level 1 (parent="").
    t_sb = time.monotonic()
    prefix_len = len(prefix)
    with _get_db(bucket) as db:
        # Restrict to the prefix's subtree via a covered range scan on `prefix`
        # (index-only) instead of a non-sargable `key LIKE prefix||'%'` full scan.
        # Rows arrive ordered by (prefix,key), so the GROUP BY streams (no temp B-tree).
        if prefix:
            range_sql = "prefix >= ? AND prefix < ?"
            range_params = (prefix, _prefix_upper(prefix))
        else:
            range_sql = "1=1"
            range_params = ()
        rows = db.execute(f"""
            SELECT substr(key, 1, ? + instr(substr(key, ? + 1), '/')) as child_prefix,
                   COUNT(*) as count, SUM(size) as total_size
            FROM objects WHERE {range_sql} AND instr(substr(key, ? + 1), '/') > 0
            GROUP BY child_prefix ORDER BY total_size DESC
        """, (prefix_len, prefix_len, *range_params, prefix_len)).fetchall()
        root_row = db.execute(f"""
            SELECT COUNT(*) as count, COALESCE(SUM(size), 0) as total_size
            FROM objects WHERE {range_sql} AND instr(substr(key, ? + 1), '/') = 0
        """, (*range_params, prefix_len)).fetchone()
        log.info("[perf] storage_breakdown: %.3fs (%d children) prefix=%s",
                 time.monotonic() - t_sb, len(rows), prefix[:60])
        children = [
            {"prefix": r["child_prefix"], "name": r["child_prefix"][prefix_len:].rstrip("/"),
             "object_count": r["count"], "total_size": r["total_size"]}
            for r in rows if r["child_prefix"] and r["child_prefix"] != prefix
        ]
        if root_row and root_row["count"] > 0:
            children.append({
                "prefix": prefix or "(root files)",
                "name": "(files)",
                "object_count": root_row["count"],
                "total_size": root_row["total_size"],
            })
    total_size = sum(c["total_size"] for c in children)
    total_count = sum(c["object_count"] for c in children)
    result = {"prefix": prefix or "(root)", "total_size": total_size,
              "object_count": total_count, "children": children}
    return result


# ── API: Storage History ──────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/storage-history")
def storage_history(bucket: str, prefix: str = "", days: int = 90, user: dict = Depends(get_current_user)):
    """Return storage growth history for a bucket or specific prefix."""
    if not os.path.exists(_db_path(bucket)):
        return {"history": []}
    days = max(1, min(days, 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _get_db(bucket) as db:
        # Use the latest snapshot per day (not MAX which picks peak, not latest)
        rows = db.execute(
            "SELECT DATE(h.timestamp) as day, h.object_count, h.total_size, h.timestamp "
            "FROM storage_history h "
            "INNER JOIN ("
            "  SELECT DATE(timestamp) as d, MAX(timestamp) as latest "
            "  FROM storage_history WHERE prefix = ? AND timestamp >= ? GROUP BY d"
            ") sub ON DATE(h.timestamp) = sub.d AND h.timestamp = sub.latest "
            "WHERE h.prefix = ? "
            "ORDER BY day ASC",
            (prefix, cutoff, prefix),
        ).fetchall()
    return {"prefix": prefix or "(all)", "history": [dict(r) for r in rows]}


# ── API: Cost Breakdown ──────────────────────────────────────────────────────

def _get_endpoint_provider(endpoint_id: str = None) -> tuple[str, str]:
    """Return (provider, region) for the current or specified endpoint."""
    with _get_users_db() as db:
        if endpoint_id:
            row = db.execute("SELECT endpoint_url, region FROM s3_endpoints WHERE id=?", (endpoint_id,)).fetchone()
        else:
            row = db.execute("SELECT endpoint_url, region FROM s3_endpoints WHERE is_default=1").fetchone()
    if not row:
        return "unknown", "us-east-1"
    provider = detect_provider(row["endpoint_url"])
    region = row["region"] or "us-east-1"
    return provider, region


@app.get("/api/pricing")
def list_pricing(user: dict = Depends(get_current_user)):
    """Return pricing for all known providers with source attribution."""
    return {"providers": get_all_providers()}


@app.get("/api/pricing/{provider}")
def get_provider_pricing(provider: str, region: str = "us-east-1", user: dict = Depends(get_current_user)):
    """Return pricing for a specific provider."""
    prices = get_storage_pricing(provider, region)
    source = "aws_live_api" if provider.lower() == "aws" else "s3compare.io (CC BY 4.0)"
    return {"provider": provider, "region": region, "storage_classes": prices, "source": source}


@app.get("/api/buckets/{bucket}/cost-breakdown")
def cost_breakdown(
    bucket: str,
    provider: Optional[str] = None,
    region: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Return per-folder cost estimates for a bucket."""
    if not _is_index_ready(bucket):
        raise HTTPException(503, "Index not ready")

    # Auto-detect provider from endpoint if not specified
    endpoint_id = getattr(getattr(threading.current_thread(), "_local", None), "endpoint_id", None)
    if not provider or not region:
        detected_provider, detected_region = _get_endpoint_provider(endpoint_id)
        provider = provider or detected_provider
        region = region or detected_region

    price_per_gb = get_storage_price(provider, "standard", region)

    # Get folder breakdown (reuse storage-breakdown logic)
    with _get_db(bucket) as db:
        has_stats = False
        try:
            stats_count = db.execute("SELECT COUNT(*) FROM folder_stats").fetchone()[0]
            has_stats = stats_count > 0
        except Exception:
            pass

        if has_stats:
            rows = db.execute(
                "SELECT prefix, object_count, total_size FROM folder_stats ORDER BY total_size DESC"
            ).fetchall()
        else:
            rows = []

    children = []
    total_size = 0
    total_cost = 0
    for r in rows:
        size = r["total_size"]
        gb = size / (1024 ** 3)
        monthly = round(gb * price_per_gb, 2)
        total_size += size
        total_cost += monthly
        children.append({
            "prefix": r["prefix"] or "(root files)",
            "name": (r["prefix"] or "").rstrip("/") or "(files)",
            "total_size": size,
            "object_count": r["object_count"],
            "monthly_cost": monthly,
            "annual_cost": round(monthly * 12, 2),
        })

    total_gb = total_size / (1024 ** 3)

    # All storage class options for comparison
    all_classes = get_storage_pricing(provider, region)
    class_comparison = {}
    for cls_name, cls_price in all_classes.items():
        cls_monthly = round(total_gb * cls_price, 2)
        class_comparison[cls_name] = {
            "price_per_gb_month": round(cls_price, 6),
            "monthly_cost": cls_monthly,
            "annual_cost": round(cls_monthly * 12, 2),
        }

    return {
        "bucket": bucket,
        "provider": provider,
        "region": region,
        "price_per_gb_month": round(price_per_gb, 6),
        "total_size": total_size,
        "total_gb": round(total_gb, 2),
        "monthly_cost": round(total_cost, 2),
        "annual_cost": round(total_cost * 12, 2),
        "children": children,
        "class_comparison": class_comparison,
        "pricing_source": "aws_live_api" if provider == "aws" else "s3compare.io (CC BY 4.0)",
    }


# ── API: Optimization / Tiering Recommendations ──────────────────────────────

@app.get("/api/buckets/{bucket}/optimization-summary")
def optimization_summary(
    bucket: str,
    provider: Optional[str] = None,
    region: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Return optimization recommendations: cold data, duplicates, lifecycle gaps, tiering savings."""
    if not _is_index_ready(bucket):
        raise HTTPException(503, "Index not ready")

    endpoint_id = getattr(getattr(threading.current_thread(), "_local", None), "endpoint_id", None)
    if not provider or not region:
        detected_provider, detected_region = _get_endpoint_provider(endpoint_id)
        provider = provider or detected_provider
        region = region or detected_region

    now_iso = datetime.now(timezone.utc).isoformat()

    with _get_db(bucket) as db:
        # ── Single scan: totals + age distribution + cold data by folder ──
        now_utc = datetime.now(timezone.utc)
        age_thresholds = [7, 30, 90, 180, 365]
        cold_threshold_days = 30
        cutoffs = {d: (now_utc - timedelta(days=d)).isoformat() for d in age_thresholds}
        cold_cutoff = cutoffs[cold_threshold_days]

        combined = db.execute("""
            SELECT
                COUNT(*), COALESCE(SUM(size), 0),
                COUNT(CASE WHEN last_modified < ? THEN 1 END),
                COALESCE(SUM(CASE WHEN last_modified < ? THEN size END), 0),
                COUNT(CASE WHEN last_modified < ? THEN 1 END),
                COALESCE(SUM(CASE WHEN last_modified < ? THEN size END), 0),
                COUNT(CASE WHEN last_modified < ? THEN 1 END),
                COALESCE(SUM(CASE WHEN last_modified < ? THEN size END), 0),
                COUNT(CASE WHEN last_modified < ? THEN 1 END),
                COALESCE(SUM(CASE WHEN last_modified < ? THEN size END), 0),
                COUNT(CASE WHEN last_modified < ? THEN 1 END),
                COALESCE(SUM(CASE WHEN last_modified < ? THEN size END), 0)
            FROM objects""",
            (cutoffs[7], cutoffs[7], cutoffs[30], cutoffs[30], cutoffs[90], cutoffs[90],
             cutoffs[180], cutoffs[180], cutoffs[365], cutoffs[365]),
        ).fetchone()

        total_objects, total_size = combined[0], combined[1]

        if total_objects == 0:
            return {"bucket": bucket, "total_objects": 0, "total_size": 0, "age_distribution": [],
                    "cold_data": {}, "duplicates": {}, "lifecycle": {}, "tiering": {}}

        age_distribution = []
        for i, days in enumerate(age_thresholds):
            cnt, sz = combined[2 + i*2], combined[3 + i*2]
            age_distribution.append({
                "older_than_days": days,
                "object_count": cnt,
                "total_size": sz,
                "pct_objects": round(cnt / total_objects * 100, 1) if total_objects else 0,
                "pct_size": round(sz / total_size * 100, 1) if total_size else 0,
            })

        # ── Cold data by folder (uses precomputed folder_stats for totals) ──
        cold_folders = db.execute("""
            SELECT
                CASE WHEN INSTR(key, '/') > 0 THEN SUBSTR(key, 1, INSTR(key, '/')) ELSE '(root)' END as folder,
                COUNT(*) as cold_count,
                COALESCE(SUM(size), 0) as cold_size,
                MIN(last_modified) as oldest
            FROM objects
            WHERE last_modified < ?
            GROUP BY folder
            ORDER BY cold_size DESC
        """, (cold_cutoff,)).fetchall()

        # Use precomputed folder_stats instead of a second full scan
        folder_totals = {}
        for row in db.execute("SELECT prefix, object_count, total_size FROM folder_stats").fetchall():
            folder_totals[row[0]] = {"count": row[1], "size": row[2]}

        cold_data_folders = []
        total_cold_size = 0
        for row in cold_folders:
            ft = folder_totals.get(row[0], {"count": 1, "size": 1})
            cold_data_folders.append({
                "folder": row[0],
                "cold_objects": row[1],
                "cold_size": row[2],
                "total_objects": ft["count"],
                "total_size": ft["size"],
                "cold_pct": round(row[2] / ft["size"] * 100, 1) if ft["size"] else 0,
                "oldest": row[3],
            })
            total_cold_size += row[2]

        # ── Duplicate detection (skip for very large buckets to avoid long scans) ──
        if total_objects <= 1_000_000:
            dupe_rows = db.execute("""
                SELECT
                    CASE WHEN INSTR(key, '/') > 0
                         THEN SUBSTR(key, INSTR(key, '/') + 1) ELSE key END as filename,
                    size, COUNT(*) as cnt
                FROM objects
                WHERE size > 0
                GROUP BY filename, size
                HAVING cnt > 1
                ORDER BY size * (cnt - 1) DESC
                LIMIT 20
            """).fetchall()
        else:
            dupe_rows = []

        dupe_groups = []
        total_dupe_waste = 0
        for row in dupe_rows:
            waste = row[1] * (row[2] - 1)
            total_dupe_waste += waste
            dupe_groups.append({
                "filename": row[0],
                "size": row[1],
                "copies": row[2],
                "wasted_bytes": waste,
            })

        # ── Lifecycle gap analysis ──
        try:
            lc_resp = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
            lc_rules = lc_resp.get("Rules", [])
            has_expiration = any("Expiration" in r for r in lc_rules)
            has_noncurrent = any("NoncurrentVersionExpiration" in r for r in lc_rules)
            has_abort = any("AbortIncompleteMultipartUpload" in r for r in lc_rules)
            has_transition = any("Transition" in r or "Transitions" in r for r in lc_rules)
        except ClientError:
            lc_rules = []
            has_expiration = has_noncurrent = has_abort = has_transition = False

        # Build recommendations
        recommendations = []
        if not lc_rules:
            recommendations.append({
                "type": "no_lifecycle",
                "severity": "high",
                "message": "No lifecycle rules configured. Data will grow indefinitely.",
                "suggestion": "Add an expiration rule to automatically clean up old data.",
            })
        if not has_expiration and total_objects > 1000:
            recommendations.append({
                "type": "no_expiration",
                "severity": "high" if total_size > 100 * 1024**3 else "medium",
                "message": f"No expiration rule. Bucket has {total_objects:,} objects ({round(total_size/1024**3, 1)} GB) that will never be cleaned up.",
                "suggestion": "Consider adding an expiration rule based on your data retention requirements.",
            })
        if not has_abort:
            recommendations.append({
                "type": "no_abort_multipart",
                "severity": "low",
                "message": "No rule to auto-abort incomplete multipart uploads.",
                "suggestion": "Add an AbortIncompleteMultipartUpload rule (e.g., 7 days) to prevent orphaned uploads from wasting space.",
            })
        if not has_noncurrent:
            try:
                v_resp = s3.get_bucket_versioning(Bucket=bucket)
                if v_resp.get("Status") == "Enabled":
                    recommendations.append({
                        "type": "versioned_no_cleanup",
                        "severity": "medium",
                        "message": "Versioning is enabled but no noncurrent version cleanup rule exists.",
                        "suggestion": "Add a NoncurrentVersionExpiration rule to clean up old versions.",
                    })
            except ClientError:
                pass

        # ── Tiering savings (only for providers with multiple storage classes) ──
        all_classes = get_storage_pricing(provider, region)
        tiering = {}
        if len(all_classes) > 1 and total_cold_size > 0:
            current_price = get_storage_price(provider, "standard", region)
            best_savings = 0
            best_class = None
            for cls_name, cls_price in all_classes.items():
                if cls_name == "standard" or cls_name == "intelligent_tiering":
                    continue
                savings_info = calculate_savings(total_size, total_cold_size, provider, "standard", cls_name, region)
                if savings_info["monthly_savings"] > best_savings:
                    best_savings = savings_info["monthly_savings"]
                    best_class = cls_name
                    tiering = {
                        "recommended_class": cls_name,
                        "cold_data_size": total_cold_size,
                        "cold_data_pct": round(total_cold_size / total_size * 100, 1),
                        **savings_info,
                    }
            if best_class:
                recommendations.append({
                    "type": "tiering_opportunity",
                    "severity": "medium",
                    "message": f"Moving {round(total_cold_size/1024**3, 1)} GB of cold data (>{cold_threshold_days}d) to {best_class.replace('_', ' ')} could save ${best_savings:.2f}/mo.",
                    "suggestion": f"Add a Transition rule to move objects to {best_class.replace('_', ' ')} after {cold_threshold_days} days.",
                })

    return {
        "bucket": bucket,
        "provider": provider,
        "region": region,
        "total_objects": total_objects,
        "total_size": total_size,
        "age_distribution": age_distribution,
        "cold_data": {
            "threshold_days": cold_threshold_days,
            "total_cold_size": total_cold_size,
            "cold_pct": round(total_cold_size / total_size * 100, 1) if total_size else 0,
            "folders": cold_data_folders,
        },
        "duplicates": {
            "groups": dupe_groups,
            "total_wasted": total_dupe_waste,
            "skipped": total_objects > 1_000_000,
        },
        "lifecycle": {
            "rule_count": len(lc_rules),
            "has_expiration": has_expiration,
            "has_noncurrent": has_noncurrent,
            "has_abort": has_abort,
            "has_transition": has_transition,
            "recommendations": recommendations,
        },
        "tiering": tiering,
    }


# ── API: Crawl Status ──────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/crawl-status")
def crawl_status(bucket: str, user: dict = Depends(get_current_user)):
    if not os.path.exists(_db_path(bucket)):
        return {"status": "not_indexed", "total_objects": 0, "total_size": 0}
    with _get_db(bucket) as db:
        row = db.execute("SELECT * FROM crawl_status WHERE id=1").fetchone()
    return dict(row) if row else {"status": "unknown"}


@app.post("/api/buckets/{bucket}/crawl")
def trigger_crawl(bucket: str, user: dict = Depends(require_admin)):
    eid = _current_endpoint_id()
    _init_db(bucket, eid)
    if _queue_crawl(bucket, eid):
        return {"message": "Crawl started"}
    return {"message": "Crawl already in progress"}


# ── API: Object Operations ──────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/download")
def download_object(bucket: str, key: str, user: dict = Depends(get_current_user)):
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
    return RedirectResponse(url)


# ── Rate limiting for CPU/memory intensive endpoints ───────────────────────
_metadata_semaphore = threading.Semaphore(4)  # max 4 concurrent metadata/preview operations


def _acquire_metadata_slot():
    """Acquire a metadata processing slot or raise 429."""
    if not _metadata_semaphore.acquire(timeout=5):
        raise HTTPException(429, "Too many concurrent metadata requests, try again shortly")


@app.get("/api/buckets/{bucket}/preview")
def preview_object(
    bucket: str,
    key: str,
    max_bytes: Optional[int] = None,
    user: dict = Depends(get_current_user),
):
    _acquire_metadata_slot()
    try:
        return _preview_object_inner(bucket, key, max_bytes)
    finally:
        _metadata_semaphore.release()


def _preview_object_inner(bucket, key, max_bytes):
    if max_bytes is not None and max_bytes < 1:
        raise HTTPException(400, "max_bytes must be positive")
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if "NoSuchKey" in str(e) or "NotFound" in str(e):
            raise HTTPException(404, "Object not found")
        raise

    size = head.get("ContentLength", 0)
    if size == 0:
        return {"content": "", "truncated": False, "content_type": head.get("ContentType", "")}

    effective_max = max_bytes
    if effective_max is None and size > 5 * 1024 * 1024:
        effective_max = 512000
    if effective_max is not None:
        effective_max = min(effective_max, 5 * 1024 * 1024)

    truncated = bool(effective_max is not None and size > effective_max)
    params = {"Bucket": bucket, "Key": key}
    if effective_max is not None:
        params["Range"] = f"bytes=0-{effective_max - 1}"

    resp = s3.get_object(**params)
    data = resp["Body"].read()
    text = data.decode("utf-8", errors="replace")
    return {"content": text, "truncated": truncated, "content_type": head.get("ContentType", "")}


@app.get("/api/buckets/{bucket}/file-metadata")
def file_metadata(
    bucket: str,
    key: str,
    user: dict = Depends(get_current_user),
):
    """Extract schema/metadata from Parquet, ORC, or Avro files by reading only what's needed."""
    _acquire_metadata_slot()
    try:
        return _file_metadata_inner(bucket, key)
    finally:
        _metadata_semaphore.release()


def _file_metadata_inner(bucket, key):
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if ext not in ("parquet", "orc", "avro"):
        raise HTTPException(400, f"Unsupported file type: .{ext}")

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if "NoSuchKey" in str(e) or "NotFound" in str(e):
            raise HTTPException(404, "Object not found")
        raise

    file_size = head.get("ContentLength", 0)

    if ext == "parquet":
        return _read_parquet_metadata(bucket, key, file_size)
    elif ext == "orc":
        return _read_orc_metadata(bucket, key, file_size)
    else:
        return _read_avro_metadata(bucket, key, file_size)


def _read_parquet_metadata(bucket: str, key: str, file_size: int):
    """Read Parquet footer to extract schema and row count without downloading the whole file."""
    # Parquet footer: last 8 bytes = 4-byte footer length + 4-byte magic "PAR1"
    # Then read the footer itself from (file_size - 8 - footer_length) to (file_size - 8)
    if file_size < 12:
        raise HTTPException(400, "File too small to be a valid Parquet file")

    # Read the last 8 bytes to get footer length
    tail_resp = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={file_size - 8}-{file_size - 1}")
    tail = tail_resp["Body"].read()
    if tail[4:8] != b"PAR1":
        raise HTTPException(400, "Not a valid Parquet file (missing PAR1 magic)")
    footer_len = struct.unpack("<I", tail[0:4])[0]

    # Sanity check: footer shouldn't exceed 256MB
    if footer_len > 256 * 1024 * 1024:
        raise HTTPException(400, f"Parquet footer too large ({footer_len} bytes), likely corrupted")

    # Read footer + magic for pyarrow
    footer_start = file_size - 8 - footer_len
    if footer_start < 4:
        raise HTTPException(400, "Invalid Parquet footer length")
    range_resp = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={footer_start}-{file_size - 1}")
    footer_bytes = range_resp["Body"].read()

    # Also need the first 4 bytes (PAR1 magic) for a valid parquet buffer
    header_resp = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-3")
    header_bytes = header_resp["Body"].read()
    if header_bytes != b"PAR1":
        raise HTTPException(400, "Not a valid Parquet file (missing header magic)")

    # Build a minimal buffer: header (4) + padding + footer
    # Cap padding to avoid OOM on large files (pyarrow only needs offsets to match)
    MAX_PADDING = 1 * 1024 * 1024  # 1MB max padding
    padding_size = footer_start - 4
    if padding_size > MAX_PADDING:
        # Use a sparse approach: seek instead of allocating giant buffer
        buf = io.BytesIO()
        buf.write(header_bytes)
        buf.seek(footer_start)
        buf.write(footer_bytes)
        buf.seek(0)
    else:
        buf = io.BytesIO(header_bytes + b"\x00" * padding_size + footer_bytes)
    try:
        meta = pq.read_metadata(buf)
    except Exception as e:
        raise HTTPException(400, f"Failed to read Parquet metadata: {e}")

    schema = meta.schema.to_arrow_schema()
    columns = []
    for i in range(len(schema)):
        field = schema.field(i)
        columns.append({"name": field.name, "type": str(field.type), "nullable": field.nullable})

    row_groups = []
    for i in range(meta.num_row_groups):
        rg = meta.row_group(i)
        row_groups.append({
            "num_rows": rg.num_rows,
            "total_byte_size": rg.total_byte_size,
        })

    return {
        "format": "parquet",
        "num_rows": meta.num_rows,
        "num_columns": meta.num_columns,
        "num_row_groups": meta.num_row_groups,
        "created_by": meta.created_by or "",
        "columns": columns,
        "row_groups": row_groups,
        "file_size": file_size,
    }


def _read_orc_metadata(bucket: str, key: str, file_size: int):
    """Read ORC file metadata. ORC postscript is at the end — download the tail to parse."""
    # ORC is harder to read partially; download up to 64KB from the tail
    # which covers the postscript + footer for most files
    if file_size < 4:
        raise HTTPException(400, "File too small to be a valid ORC file")

    # For ORC, we need to download enough of the file. For files under 10MB, just get it all.
    # For larger files, try reading the tail.
    download_size = min(file_size, 10 * 1024 * 1024)
    if download_size < file_size:
        # Download the tail portion
        resp = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={file_size - download_size}-{file_size - 1}")
    else:
        resp = s3.get_object(Bucket=bucket, Key=key)
    data = resp["Body"].read()
    buf = io.BytesIO(data)

    try:
        reader = orc_mod.ORCFile(buf)
    except Exception as e:
        raise HTTPException(400, f"Failed to read ORC metadata: {e}")

    columns = []
    schema = reader.schema
    for i in range(len(schema)):
        field = schema.field(i)
        columns.append({"name": field.name, "type": str(field.type), "nullable": field.nullable})

    return {
        "format": "orc",
        "num_rows": reader.nrows,
        "num_columns": len(schema),
        "num_stripes": reader.nstripes,
        "compression": str(reader.compression),
        "columns": columns,
        "file_size": file_size,
    }


def _read_avro_metadata(bucket: str, key: str, file_size: int):
    """Read Avro schema from the file header (first few KB)."""
    # Avro header is typically small — read the first 64KB
    read_size = min(file_size, 64 * 1024)
    resp = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{read_size - 1}")
    data = resp["Body"].read()
    buf = io.BytesIO(data)

    try:
        reader = fastavro.reader(buf)
        schema = reader.writer_schema
    except Exception as e:
        raise HTTPException(400, f"Failed to read Avro metadata: {e}")

    # Parse schema fields
    fields = schema.get("fields", []) if isinstance(schema, dict) else []
    columns = []
    for f in fields:
        col_type = f.get("type", "unknown")
        if isinstance(col_type, list):
            # Union type like ["null", "string"] — show the non-null type
            non_null = [t for t in col_type if t != "null"]
            nullable = "null" in col_type
            col_type = non_null[0] if non_null else "null"
        elif isinstance(col_type, dict):
            col_type = col_type.get("type", str(col_type))
            nullable = True
        else:
            nullable = False
        columns.append({"name": f.get("name", ""), "type": str(col_type), "nullable": nullable})

    # Try to count rows (only if file is small enough — under 10MB)
    num_rows = None
    if file_size <= 10 * 1024 * 1024:
        try:
            full_resp = s3.get_object(Bucket=bucket, Key=key)
            full_buf = io.BytesIO(full_resp["Body"].read())
            full_reader = fastavro.reader(full_buf)
            num_rows = sum(1 for _ in full_reader)
        except Exception as avro_e:
            log.debug("Avro row count failed for %s: %s", key, avro_e)

    return {
        "format": "avro",
        "schema_name": schema.get("name", "") if isinstance(schema, dict) else "",
        "namespace": schema.get("namespace", "") if isinstance(schema, dict) else "",
        "num_columns": len(columns),
        "num_rows": num_rows,
        "columns": columns,
        "file_size": file_size,
    }


@app.get("/api/buckets/{bucket}/preview-tail")
def preview_tail(
    bucket: str,
    key: str,
    max_bytes: int = 512000,
    user: dict = Depends(get_current_user),
):
    """Read the tail (last N bytes) of a file — useful for log files."""
    _acquire_metadata_slot()
    try:
        return _preview_tail_inner(bucket, key, max_bytes)
    finally:
        _metadata_semaphore.release()


def _preview_tail_inner(bucket, key, max_bytes):
    if max_bytes < 1:
        raise HTTPException(400, "max_bytes must be positive")
    max_bytes = min(max_bytes, 5 * 1024 * 1024)

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if "NoSuchKey" in str(e) or "NotFound" in str(e):
            raise HTTPException(404, "Object not found")
        raise

    size = head.get("ContentLength", 0)
    if size == 0:
        return {"content": "", "truncated": False, "showing": "full"}

    if size <= max_bytes:
        resp = s3.get_object(Bucket=bucket, Key=key)
        data = resp["Body"].read()
        text = data.decode("utf-8", errors="replace")
        return {"content": text, "truncated": False, "showing": "full", "total_size": size}
    else:
        start = size - max_bytes
        resp = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{size - 1}")
        data = resp["Body"].read()
        text = data.decode("utf-8", errors="replace")
        # Skip partial first line
        first_newline = text.find("\n")
        if first_newline >= 0 and first_newline < 1000:
            text = text[first_newline + 1:]
        return {"content": text, "truncated": True, "showing": "tail", "total_size": size}


class DeleteRequest(BaseModel):
    keys: list[str]

@app.delete("/api/buckets/{bucket}/objects")
def delete_objects(bucket: str, req: DeleteRequest, user: dict = Depends(require_admin)):
    if not req.keys: raise HTTPException(400, "No keys")
    if len(req.keys) > 1000: raise HTTPException(400, "Max 1000 keys")
    resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in req.keys], "Quiet": True})
    errors = resp.get("Errors", [])
    if os.path.exists(_db_path(bucket)):
        with _get_db(bucket) as db:
            # Adjust folder_stats before deleting
            for k in req.keys:
                size_row = db.execute("SELECT size FROM objects WHERE key=?", (k,)).fetchone()
                if size_row:
                    _adjust_folder_stats(db, k, -size_row[0], -1)
                    _adjust_prefix_children(db, k, -size_row[0], -1)
            db.executemany("DELETE FROM objects WHERE key=?", [(k,) for k in req.keys])
            db.commit()
        _update_crawl_counters(bucket)
    details = f"count={len(req.keys)}"
    summary = _summarize_keys(req.keys)
    if summary:
        details += f", keys={summary}"
    _audit("delete", user["username"], bucket=bucket, details=details)
    return {"deleted": len(req.keys) - len(errors), "errors": errors}


class DeleteFolderRequest(BaseModel):
    prefix: str
    purge_versions: bool = False

@app.delete("/api/buckets/{bucket}/folder")
def delete_folder(bucket: str, req: DeleteFolderRequest, user: dict = Depends(require_admin)):
    """Recursively delete all objects under a prefix (folder).
    If purge_versions=true, dispatches to background purge task."""
    pfx = req.prefix if req.prefix.endswith("/") else req.prefix + "/"
    if not pfx or pfx == "/" or len(pfx.rstrip("/")) == 0:
        raise HTTPException(400, "Cannot delete root prefix")

    if req.purge_versions:
        # Dispatch to background purge (same as purge-versions endpoint)
        task_id = uuid.uuid4().hex[:12]
        log.info("Purge folder task %s: bucket=%s, prefix=%s", task_id, bucket, pfx)
        with _purge_tasks_lock:
            _purge_tasks[task_id] = {
                "status": "running",
                "bucket": bucket,
                "purged": 0,
                "errors": 0,
                "detail": "Starting folder purge...",
                "started_at": time.time(),
            }
        eid = _current_endpoint_id()
        threading.Thread(
            target=_run_purge,
            args=(task_id, bucket, [], pfx, user["username"], eid),
            daemon=True,
        ).start()
        _purge_task_cleanup()
        return {"task_id": task_id, "status": "running", "prefix": pfx}

    # Non-purge: regular delete (fast — only current versions)
    all_keys = []
    token = None
    while True:
        params = {"Bucket": bucket, "Prefix": pfx, "MaxKeys": 1000}
        if token:
            params["ContinuationToken"] = token
        resp = s3.list_objects_v2(**params)
        for obj in resp.get("Contents", []):
            all_keys.append(obj["Key"])
        if not resp.get("IsTruncated", False):
            break
        token = resp.get("NextContinuationToken")
    all_keys.append(pfx)
    all_keys = list(set(all_keys))
    total_deleted = 0
    total_errors = 0
    for i in range(0, len(all_keys), 1000):
        batch = all_keys[i:i + 1000]
        resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
        total_errors += len(resp.get("Errors", []))
        total_deleted += len(batch) - len(resp.get("Errors", []))

    # Clean up index
    if os.path.exists(_db_path(bucket)):
        with _get_db(bucket) as db:
            db.execute("DELETE FROM objects WHERE key LIKE ?", (pfx + "%",))
            db.execute("DELETE FROM objects WHERE key = ?", (pfx,))
            db.execute("DELETE FROM discovered_prefixes WHERE prefix = ?", (pfx,))
            db.commit()
        _update_crawl_counters(bucket)
    _audit("delete_folder", user["username"], bucket=bucket, details=f"prefix={pfx}, objects={total_deleted}")
    return {"deleted": total_deleted, "errors": total_errors, "prefix": pfx}


MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(5 * 1024 * 1024 * 1024)))  # 5 GB default (proxy fallback only)
UPLOAD_PROXY_CONCURRENCY = int(os.environ.get("UPLOAD_PROXY_CONCURRENCY", "3"))  # files streamed in parallel through the proxy


@app.post("/api/buckets/{bucket}/upload")
@limiter.limit(UPLOAD_RATE_LIMIT)
async def upload_files(bucket: str, request: Request, prefix: str = Form(""), files: list[UploadFile] = File(...)):
    user = get_current_user(request)
    if user["role"] != "admin":
        bp = getattr(request.state, "bucket_permission", None)
        if bp != "write":
            raise HTTPException(403, "Write access required")
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)

    # Stream each upload straight to S3 from its (disk-backed) spooled temp file —
    # never buffer a whole file in memory. This is the PROXY FALLBACK; the default
    # path is direct browser→S3 (presigned multipart for large files, zero bytes
    # through the server). boto3 upload_fileobj does multipart for large objects,
    # reading the source in bounded chunks, so peak RAM is INDEPENDENT of file size
    # (measured ≈100 MB for a single in-flight file regardless of whether it is
    # 500 MB or 50 GB — vs the old read-into-memory path that scaled 1:1 and OOM'd).
    # Total proxy memory is therefore bounded by ≈100 MB × UPLOAD_PROXY_CONCURRENCY,
    # not by file size — which is what fixes the OOM / pod restart at scale.
    from boto3.s3.transfer import TransferConfig
    transfer_cfg = TransferConfig(multipart_threshold=8 * 1024 * 1024,
                                  multipart_chunksize=8 * 1024 * 1024,
                                  max_concurrency=4, use_threads=True)
    file_data = []
    total_bytes = 0
    for f in files:
        key = prefix + f.filename
        f.file.seek(0, os.SEEK_END)
        size = f.file.tell()
        f.file.seek(0)
        total_bytes += size
        if total_bytes > MAX_UPLOAD_SIZE:
            raise HTTPException(413, f"Upload exceeds maximum size of {MAX_UPLOAD_SIZE // (1024*1024)}MB")
        file_data.append((key, f.file, size))

    def _put_one(key, fileobj, size):
        client.upload_fileobj(fileobj, bucket, key, Config=transfer_cfg)
        return key, size

    # Upload to S3 concurrently (each file streams from its own spooled temp file)
    import concurrent.futures
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(UPLOAD_PROXY_CONCURRENCY, len(file_data)))) as pool:
        futures = [pool.submit(_put_one, key, fileobj, size) for key, fileobj, size in file_data]
        for fut in concurrent.futures.as_completed(futures):
            key, file_size = fut.result()
            results.append({"key": key, "size": file_size})

    # Batch update the index
    if results and os.path.exists(_db_path(bucket, eid)):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        with _get_db(bucket, eid) as db:
            for r in results:
                # Check if replacing an existing object (for folder_stats delta)
                old = db.execute("SELECT size FROM objects WHERE key=?", (r["key"],)).fetchone()
                db.execute("INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth) VALUES (?,?,?,?,?,?)",
                           (r["key"], r["size"], now, "", _key_prefix(r["key"]), _key_depth(r["key"])))
                if old:
                    _adjust_folder_stats(db, r["key"], r["size"] - old[0], 0)
                    _adjust_prefix_children(db, r["key"], r["size"] - old[0], 0)
                else:
                    _adjust_folder_stats(db, r["key"], r["size"], 1)
                    _adjust_prefix_children(db, r["key"], r["size"], 1)
            db.commit()
    if results:
        _update_crawl_counters(bucket, eid)
        details = f"count={len(results)}"
        if prefix:
            details += f", prefix={prefix}"
        summary = _summarize_keys([r["key"] for r in results])
        if summary:
            details += f", keys={summary}"
        _audit("upload", user["username"], bucket=bucket, details=details)
    return {"uploaded": results}


class CreateFolderRequest(BaseModel):
    prefix: str

@app.post("/api/buckets/{bucket}/create-folder")
def create_folder(bucket: str, req: CreateFolderRequest, user: dict = Depends(require_admin)):
    folder_key = req.prefix if req.prefix.endswith("/") else req.prefix + "/"
    s3.put_object(Bucket=bucket, Key=folder_key, Body=b"")
    if os.path.exists(_db_path(bucket)):
        with _get_db(bucket) as db:
            old = db.execute("SELECT size FROM objects WHERE key=?", (folder_key,)).fetchone()
            db.execute(
                "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth) VALUES (?,?,?,?,?,?)",
                (folder_key, 0, time.strftime("%Y-%m-%dT%H:%M:%SZ"), "", _key_prefix(folder_key), _key_depth(folder_key)))
            if not old:
                _adjust_folder_stats(db, folder_key, 0, 1)
                _adjust_prefix_children(db, folder_key, 0, 1)
            db.commit()
        _update_crawl_counters(bucket)
    _audit("create_folder", user["username"], bucket=bucket, details=folder_key)
    return {"created": folder_key}


# ── API: S3 Management ──────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/object-info")
def object_info(bucket: str, key: str, user: dict = Depends(get_current_user)):
    resp = s3.head_object(Bucket=bucket, Key=key)
    return {"key": key, "size": resp["ContentLength"], "content_type": resp.get("ContentType", ""),
            "etag": resp.get("ETag", "").strip('"'), "last_modified": resp["LastModified"].isoformat(),
            "metadata": resp.get("Metadata", {}), "version_id": resp.get("VersionId"),
            "storage_class": resp.get("StorageClass", "STANDARD")}


@app.get("/api/buckets/{bucket}/object-versions")
def object_versions(bucket: str, key: str, user: dict = Depends(get_current_user)):
    resp = s3.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=200)
    versions = [{"version_id": v.get("VersionId"), "size": v["Size"], "last_modified": v["LastModified"].isoformat(),
                 "is_latest": v.get("IsLatest", False), "etag": v.get("ETag", "").strip('"'),
                 "storage_class": v.get("StorageClass", "STANDARD")}
                for v in resp.get("Versions", []) if v["Key"] == key]
    delete_markers = [{"version_id": d.get("VersionId"), "last_modified": d["LastModified"].isoformat(),
                       "is_latest": d.get("IsLatest", False), "is_delete_marker": True}
                      for d in resp.get("DeleteMarkers", []) if d["Key"] == key]
    return {"key": key, "versions": versions, "delete_markers": delete_markers}


@app.get("/api/buckets/{bucket}/list-versions")
def list_versions(bucket: str, prefix: str = "", show: str = "all", user: dict = Depends(get_current_user)):
    """List versioned objects under a prefix using cached version scan data.
    Returns folders with version history (delete markers, non-current versions).
    Triggers a background version scan if cache is stale or missing."""

    # Check if version scan cache exists and is fresh (< 1 hour old)
    scan_status = "none"
    try:
        with _get_db(bucket) as conn:
            row = conn.execute("SELECT scanned_at FROM version_scan_cache LIMIT 1").fetchone()
            if row and row["scanned_at"]:
                scanned_at = datetime.fromisoformat(row["scanned_at"].replace("Z", "+00:00"))
                age_minutes = (datetime.now(timezone.utc) - scanned_at).total_seconds() / 60
                scan_status = "fresh" if age_minutes < 60 else "stale"
            else:
                scan_status = "none"
    except Exception:
        scan_status = "none"

    # Trigger background scan if needed
    eid = _current_endpoint_id()
    with _version_scan_lock:
        scanning = _version_scanning.get(bucket, False)
    if scan_status != "fresh" and not scanning:
        threading.Thread(target=_scan_versioned_prefixes, args=(bucket,), kwargs={"endpoint_id": eid}, daemon=True).start()
        scanning = True

    # Return cached results from version_scan_cache
    folders = []
    try:
        with _get_db(bucket) as conn:
            if show == "deleted":
                # Only show folders without current objects (deleted/ghost folders)
                rows = conn.execute("""
                    SELECT prefix, versions_count, delete_markers_count, total_size,
                           keys_count, latest_modified, has_current_objects
                    FROM version_scan_cache
                    WHERE has_current_objects = 0
                    AND (versions_count > 0 OR delete_markers_count > 0)
                    ORDER BY prefix
                """).fetchall()
            else:
                # Show all folders with version data
                rows = conn.execute("""
                    SELECT prefix, versions_count, delete_markers_count, total_size,
                           keys_count, latest_modified, has_current_objects
                    FROM version_scan_cache
                    WHERE versions_count > 0 OR delete_markers_count > 0
                    ORDER BY prefix
                """).fetchall()
            for r in rows:
                folders.append({
                    "prefix": r["prefix"],
                    "total_size": r["total_size"],
                    "versions_count": r["versions_count"],
                    "delete_markers_count": r["delete_markers_count"],
                    "keys_count": r["keys_count"],
                    "latest_modified": r["latest_modified"],
                    "has_current_objects": bool(r["has_current_objects"]),
                })
    except Exception as cache_e:
        log.debug("Version scan cache read failed for %s: %s", bucket, cache_e)

    return {
        "folders": folders,
        "files": [],
        "total_keys": len(folders),
        "scan_status": "scanning" if scanning else scan_status,
    }


@app.post("/api/buckets/{bucket}/scan-versions")
def trigger_version_scan(bucket: str, user: dict = Depends(require_admin)):
    """Trigger a background version scan for the bucket."""
    with _version_scan_lock:
        scanning = _version_scanning.get(bucket, False)
    if scanning:
        return {"status": "already_scanning"}
    eid = _current_endpoint_id()
    threading.Thread(target=_scan_versioned_prefixes, args=(bucket,), kwargs={"endpoint_id": eid}, daemon=True).start()
    return {"status": "scan_started"}


class PurgeVersionsRequest(BaseModel):
    keys: list[str] = []
    prefix: str = ""


@app.post("/api/buckets/{bucket}/purge-versions")
def purge_versions(bucket: str, req: PurgeVersionsRequest, user: dict = Depends(require_admin)):
    """Start a background purge of ALL versions and delete markers for the given keys or prefix.
    Returns a task_id immediately; poll GET /api/purge-status/{task_id} for progress."""
    if not req.keys and not req.prefix:
        raise HTTPException(400, "Provide keys or prefix")

    target_prefix = req.prefix
    if target_prefix and not target_prefix.endswith("/"):
        target_prefix += "/"

    task_id = uuid.uuid4().hex[:12]
    label = f"keys={len(req.keys)}" if req.keys else f"prefix={target_prefix}"
    log.info("Purge task %s started: bucket=%s, %s", task_id, bucket, label)

    with _purge_tasks_lock:
        _purge_tasks[task_id] = {
            "status": "running",
            "bucket": bucket,
            "purged": 0,
            "errors": 0,
            "detail": "Starting purge...",
            "started_at": time.time(),
        }

    eid = _current_endpoint_id()
    threading.Thread(
        target=_run_purge,
        args=(task_id, bucket, req.keys, target_prefix, user["username"], eid),
        daemon=True,
    ).start()

    _purge_task_cleanup()
    return {"task_id": task_id, "status": "running"}


@app.get("/api/purge-status/{task_id}")
def purge_status(task_id: str, user: dict = Depends(require_admin)):
    """Poll the status of a background purge task."""
    with _purge_tasks_lock:
        task = _purge_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Purge task not found")
    return {
        "task_id": task_id,
        "status": task["status"],
        "purged": task.get("purged", 0),
        "errors": task.get("errors", 0),
        "detail": task.get("detail", ""),
    }


class RestoreVersionRequest(BaseModel):
    key: str
    version_id: str


@app.post("/api/buckets/{bucket}/version-restore")
def version_restore(bucket: str, req: RestoreVersionRequest, user: dict = Depends(require_admin)):
    """Restore an older version by copying it over itself, making it the latest."""
    copy_source = {"Bucket": bucket, "Key": req.key, "VersionId": req.version_id}
    s3.copy_object(Bucket=bucket, CopySource=copy_source, Key=req.key)
    _audit("restore_version", user["username"], bucket=bucket,
           details=f"{req.key} (version {req.version_id[:12]})")
    return {"restored": True, "key": req.key, "version_id": req.version_id}


class DeleteVersionRequest(BaseModel):
    key: str
    version_id: str


@app.post("/api/buckets/{bucket}/version-delete")
def version_delete(bucket: str, req: DeleteVersionRequest, user: dict = Depends(require_admin)):
    """Delete a specific version of an object."""
    s3.delete_object(Bucket=bucket, Key=req.key, VersionId=req.version_id)
    _audit("delete_version", user["username"], bucket=bucket,
           details=f"{req.key} (version {req.version_id[:12]})")
    return {"deleted": True, "key": req.key, "version_id": req.version_id}


@app.get("/api/buckets/{bucket}/version-presigned-url")
def version_presigned_url(bucket: str, key: str, version_id: str, expires: int = 3600,
                          user: dict = Depends(get_current_user)):
    """Generate a presigned URL for downloading a specific version."""
    expires = min(max(60, expires), 604800)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key, "VersionId": version_id},
        ExpiresIn=expires,
    )
    return {"url": url, "expires_in": expires}


@app.get("/api/buckets/{bucket}/presigned-url")
def get_presigned_url(bucket: str, key: str, expires: int = 3600, user: dict = Depends(get_current_user)):
    expires = min(max(60, expires), 604800)  # clamp 1 min to 7 days
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires)
    return {"url": url, "expires_in": expires}


# ── Direct Upload (presigned PUT) ───────────────────────────────────────────

class PresignedUploadRequest(BaseModel):
    keys: list[str]
    prefix: str = ""

@app.post("/api/buckets/{bucket}/presigned-upload")
def presigned_upload(bucket: str, req: PresignedUploadRequest, request: Request, user: dict = Depends(get_current_user)):
    """Generate presigned PUT URLs for direct browser-to-S3 uploads. No file data passes through Sairo."""
    if user["role"] != "admin":
        bp = getattr(request.state, "bucket_permission", None)
        if bp != "write":
            raise HTTPException(403, "Write access required")
    if not req.keys or len(req.keys) > 100:
        raise HTTPException(400, "Provide 1-100 keys")

    expires = 3600  # 1 hour
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)

    # Ensure CORS allows PUT from the browser
    try:
        _ensure_upload_cors(bucket, request, client)
    except Exception as e:
        log.warning("Failed to ensure CORS for presigned upload on %s: %s", bucket, e)

    urls = []
    for raw_key in req.keys:
        key = req.prefix + raw_key if req.prefix else raw_key
        url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
        urls.append({"key": key, "url": url})
    return {"urls": urls, "expires_in": expires}


def _ensure_upload_cors(bucket: str, request: Request, client):
    """Ensure the bucket has CORS rules allowing PUT from the requesting origin."""
    origin = request.headers.get("origin", "")
    if not origin:
        return
    try:
        resp = client.get_bucket_cors(Bucket=bucket)
        rules = resp.get("CORSRules", [])
        # A rule only suffices if it allows PUT from this origin AND exposes ETag —
        # the browser MUST read the ETag response header on each multipart part PUT
        # (xhr.getResponseHeader("ETag")), which CORS gates via ExposeHeaders. A
        # pre-existing PUT rule that omits ETag would otherwise silently break every
        # multipart upload, so upgrade it in place instead of short-circuiting.
        for rule in rules:
            origins = rule.get("AllowedOrigins", [])
            methods = rule.get("AllowedMethods", [])
            if ("*" in origins or origin in origins) and "PUT" in methods:
                exposed = [h.lower() for h in rule.get("ExposeHeaders", [])]
                if "etag" in exposed or "*" in exposed:
                    return  # already fully configured
                rule["ExposeHeaders"] = rule.get("ExposeHeaders", []) + ["ETag"]
                client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": rules})
                log.info("Upgraded CORS rule to expose ETag for origin=%s on bucket=%s", origin, bucket)
                return
    except ClientError as e:
        if "NoSuchCORSConfiguration" not in str(e):
            raise
        rules = []

    # Add a CORS rule for direct uploads
    rules.append({
        "AllowedOrigins": [origin],
        "AllowedMethods": ["PUT", "GET", "HEAD"],
        "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 86400,
    })
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": rules})
    log.info("Added upload CORS rule for origin=%s on bucket=%s", origin, bucket)


# ── Multipart direct upload (browser → S3: presigned, parallel, resumable) ──
# Large files are split into parts that the browser PUTs directly to S3 in
# parallel. No file data passes through Sairo, so there is no proxy buffering,
# no per-pod memory pressure, and no single-PUT size ceiling (up to 5 TB).

def _require_bucket_write(user, request):
    if user["role"] != "admin" and getattr(request.state, "bucket_permission", None) != "write":
        raise HTTPException(403, "Write access required")


MULTIPART_URL_EXPIRY = int(os.environ.get("MULTIPART_URL_EXPIRY", "3600"))  # presigned part-URL TTL (frontend signs just-in-time)


class MultipartInitiateRequest(BaseModel):
    key: str
    prefix: str = ""
    content_type: str = ""


class MultipartSignRequest(BaseModel):
    key: str
    upload_id: str
    part_numbers: list[int]


class MultipartCompleteRequest(BaseModel):
    key: str
    upload_id: str
    parts: list[dict]  # [{"PartNumber": int, "ETag": str}, ...]


class MultipartUploadAbortRequest(BaseModel):
    key: str
    upload_id: str


@app.post("/api/buckets/{bucket}/multipart/initiate")
@limiter.limit(UPLOAD_RATE_LIMIT)
def multipart_initiate(bucket: str, req: MultipartInitiateRequest, request: Request, user: dict = Depends(get_current_user)):
    """Start a multipart upload; returns its UploadId. The browser uploads parts directly to S3."""
    _require_bucket_write(user, request)
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)
    try:
        _ensure_upload_cors(bucket, request, client)
    except Exception as e:
        log.warning("Failed to ensure CORS for multipart upload on %s: %s", bucket, e)
    key = req.prefix + req.key if req.prefix else req.key
    params = {"Bucket": bucket, "Key": key}
    if req.content_type:
        params["ContentType"] = req.content_type
    resp = client.create_multipart_upload(**params)
    _audit("multipart_initiate", user["username"], bucket=bucket, details=key)
    return {"key": key, "upload_id": resp["UploadId"]}


@app.post("/api/buckets/{bucket}/multipart/sign")
@limiter.limit(UPLOAD_RATE_LIMIT)
def multipart_sign(bucket: str, req: MultipartSignRequest, request: Request, user: dict = Depends(get_current_user)):
    """Return presigned URLs to PUT a batch of parts directly to S3. The frontend
    signs parts just-in-time (right before each PUT) so a URL never expires mid-flight
    on a long upload, and re-signs on a 403."""
    _require_bucket_write(user, request)
    if not req.part_numbers or len(req.part_numbers) > 1000:
        raise HTTPException(400, "Provide 1-1000 part numbers")
    if any(pn < 1 or pn > 10000 for pn in req.part_numbers):
        raise HTTPException(400, "Part numbers must be between 1 and 10000 (S3 limit)")
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)
    urls = [
        {"part_number": pn,
         "url": client.generate_presigned_url(
             "upload_part",
             Params={"Bucket": bucket, "Key": req.key, "UploadId": req.upload_id, "PartNumber": pn},
             ExpiresIn=MULTIPART_URL_EXPIRY)}
        for pn in req.part_numbers
    ]
    return {"urls": urls, "expires_in": MULTIPART_URL_EXPIRY}


@app.post("/api/buckets/{bucket}/multipart/complete")
@limiter.limit(UPLOAD_RATE_LIMIT)
def multipart_complete(bucket: str, req: MultipartCompleteRequest, request: Request, user: dict = Depends(get_current_user)):
    """Complete a multipart upload from the uploaded parts' ETags."""
    _require_bucket_write(user, request)
    if not req.parts:
        raise HTTPException(400, "No parts provided")
    try:
        parts = sorted(
            ({"PartNumber": int(p["PartNumber"]), "ETag": str(p["ETag"])} for p in req.parts),
            key=lambda p: p["PartNumber"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Each part must have an integer PartNumber and an ETag")
    if any(p["PartNumber"] < 1 or p["PartNumber"] > 10000 or not p["ETag"] for p in parts):
        raise HTTPException(400, "Invalid part: PartNumber must be 1-10000 and ETag non-empty")
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)
    resp = client.complete_multipart_upload(
        Bucket=bucket, Key=req.key, UploadId=req.upload_id,
        MultipartUpload={"Parts": parts})
    _audit("multipart_complete", user["username"], bucket=bucket, details=f"{req.key} ({len(parts)} parts)")
    return {"key": req.key, "etag": (resp.get("ETag") or "").strip('"')}


@app.post("/api/buckets/{bucket}/multipart/abort")
@limiter.limit(UPLOAD_RATE_LIMIT)
def multipart_abort_direct(bucket: str, req: MultipartUploadAbortRequest, request: Request, user: dict = Depends(get_current_user)):
    """Abort an in-progress multipart upload (cleanup on cancel/failure)."""
    _require_bucket_write(user, request)
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)
    try:
        client.abort_multipart_upload(Bucket=bucket, Key=req.key, UploadId=req.upload_id)
    except Exception as e:
        log.warning("Failed to abort multipart upload %s on %s: %s", req.upload_id, bucket, e)
    _audit("multipart_abort", user["username"], bucket=bucket, details=req.key)
    return {"aborted": True}


class NotifyUploadRequest(BaseModel):
    uploads: list[dict]  # [{"key": "...", "size": 123}, ...]

@app.post("/api/buckets/{bucket}/notify-upload")
def notify_upload(bucket: str, req: NotifyUploadRequest, request: Request, user: dict = Depends(get_current_user)):
    """Update the index after a direct-to-S3 upload. Lightweight — no file data, just metadata."""
    if user["role"] != "admin":
        bp = getattr(request.state, "bucket_permission", None)
        if bp != "write":
            raise HTTPException(403, "Write access required")

    eid = _current_endpoint_id()
    if not os.path.exists(_db_path(bucket, eid)):
        return {"indexed": 0}

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    indexed = 0
    with _get_db(bucket, eid) as db:
        for u in req.uploads:
            key = u.get("key", "")
            size = u.get("size", 0)
            if not key:
                continue
            old = db.execute("SELECT size FROM objects WHERE key=?", (key,)).fetchone()
            db.execute(
                "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth) VALUES (?,?,?,?,?,?)",
                (key, size, now, "", _key_prefix(key), _key_depth(key)))
            if old:
                _adjust_folder_stats(db, key, size - old[0], 0)
            else:
                _adjust_folder_stats(db, key, size, 1)
            indexed += 1
        db.commit()

    if indexed > 0:
        _update_crawl_counters(bucket, eid)
        summary = _summarize_keys([u["key"] for u in req.uploads if u.get("key")])
        details = f"count={indexed}, direct_upload=true"
        if summary:
            details += f", keys={summary}"
        _audit("upload", user["username"], bucket=bucket, details=details)
    return {"indexed": indexed}


# ── Bucket Configuration ────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/versioning")
def get_versioning(bucket: str, user: dict = Depends(get_current_user)):
    resp = s3.get_bucket_versioning(Bucket=bucket)
    return {"status": resp.get("Status", "Disabled"), "mfa_delete": resp.get("MFADelete", "Disabled")}


@app.put("/api/buckets/{bucket}/versioning")
def put_versioning(bucket: str, enabled: bool = True, user: dict = Depends(require_admin)):
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled" if enabled else "Suspended"})
    _audit("config_versioning", user["username"], bucket=bucket, details=f"enabled={bool(enabled)}")
    return {"status": "Enabled" if enabled else "Suspended"}


@app.get("/api/buckets/{bucket}/lifecycle")
def get_lifecycle(bucket: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
        rules = []
        for r in resp.get("Rules", []):
            rule = {"id": r.get("ID", ""), "status": r.get("Status", ""),
                    "prefix": r.get("Filter", {}).get("Prefix", r.get("Prefix", ""))}
            if "Expiration" in r: rule["expiration_days"] = r["Expiration"].get("Days")
            if "NoncurrentVersionExpiration" in r: rule["noncurrent_days"] = r["NoncurrentVersionExpiration"].get("NoncurrentDays")
            if "AbortIncompleteMultipartUpload" in r: rule["abort_days"] = r["AbortIncompleteMultipartUpload"].get("DaysAfterInitiation")
            if "Transition" in r:
                rule["transition_days"] = r["Transition"].get("Days")
                rule["transition_storage_class"] = r["Transition"].get("StorageClass")
            rules.append(rule)
        return {"rules": rules}
    except ClientError as e:
        if "NoSuchLifecycleConfiguration" in str(e): return {"rules": []}
        raise


@app.get("/api/buckets/{bucket}/cors")
def get_cors(bucket: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_bucket_cors(Bucket=bucket)
        return {"cors_rules": resp.get("CORSRules", [])}
    except ClientError as e:
        if "NoSuchCORSConfiguration" in str(e): return {"cors_rules": []}
        raise


class LifecycleRule(BaseModel):
    id: str = ""
    prefix: str = ""
    status: str = "Enabled"
    expiration_days: Optional[int] = None
    noncurrent_days: Optional[int] = None
    abort_days: Optional[int] = None
    transition_days: Optional[int] = None
    transition_storage_class: Optional[str] = None

class LifecycleRequest(BaseModel):
    rules: list[LifecycleRule]

@app.put("/api/buckets/{bucket}/lifecycle")
def put_lifecycle(bucket: str, req: LifecycleRequest, user: dict = Depends(require_admin)):
    rules = []
    for r in req.rules:
        rule = {"ID": r.id or f"rule-{len(rules)+1}", "Status": r.status, "Filter": {"Prefix": r.prefix}}
        if r.expiration_days is not None:
            rule["Expiration"] = {"Days": r.expiration_days}
        if r.noncurrent_days is not None:
            rule["NoncurrentVersionExpiration"] = {"NoncurrentDays": r.noncurrent_days}
        if r.abort_days is not None:
            rule["AbortIncompleteMultipartUpload"] = {"DaysAfterInitiation": r.abort_days}
        if r.transition_days is not None and r.transition_storage_class:
            rule["Transition"] = {"Days": r.transition_days, "StorageClass": r.transition_storage_class}
        rules.append(rule)
    try:
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": rules})
    except ClientError:
        # Ceph may need Prefix at top level instead of Filter.Prefix
        for rule in rules:
            prefix = rule.pop("Filter", {}).get("Prefix", "")
            rule["Prefix"] = prefix
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": rules})
    _audit("config_lifecycle", user["username"], bucket=bucket, details=f"rules={len(rules)}")
    return {"updated": True, "rule_count": len(rules)}


@app.delete("/api/buckets/{bucket}/lifecycle")
def delete_lifecycle(bucket: str, user: dict = Depends(require_admin)):
    try:
        s3.delete_bucket_lifecycle(Bucket=bucket)
    except ClientError as e:
        if "NoSuchLifecycleConfiguration" not in str(e):
            raise
    _audit("config_lifecycle", user["username"], bucket=bucket, details="deleted")
    return {"deleted": True}


class CorsRequest(BaseModel):
    cors_rules: list[dict]

@app.put("/api/buckets/{bucket}/cors")
def put_cors(bucket: str, req: CorsRequest, user: dict = Depends(require_admin)):
    s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": req.cors_rules})
    _audit("config_cors", user["username"], bucket=bucket, details=f"rules={len(req.cors_rules)}")
    return {"updated": True}


@app.delete("/api/buckets/{bucket}/cors")
def delete_cors(bucket: str, user: dict = Depends(require_admin)):
    try:
        s3.delete_bucket_cors(Bucket=bucket)
    except ClientError as e:
        if "NoSuchCORSConfiguration" not in str(e):
            raise
    _audit("config_cors", user["username"], bucket=bucket, details="deleted")
    return {"deleted": True}


@app.get("/api/buckets/{bucket}/policy")
def get_bucket_policy(bucket: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_bucket_policy(Bucket=bucket)
        return {"policy": json.loads(resp["Policy"])}
    except ClientError as e:
        if "NoSuchBucketPolicy" in str(e): return {"policy": None}
        raise


class PolicyRequest(BaseModel):
    policy: dict

@app.put("/api/buckets/{bucket}/policy")
def put_bucket_policy(bucket: str, req: PolicyRequest, user: dict = Depends(require_admin)):
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(req.policy))
    _audit("config_policy", user["username"], bucket=bucket, details="updated")
    return {"updated": True}


@app.delete("/api/buckets/{bucket}/policy")
def delete_bucket_policy(bucket: str, user: dict = Depends(require_admin)):
    s3.delete_bucket_policy(Bucket=bucket)
    _audit("config_policy", user["username"], bucket=bucket, details="deleted")
    return {"deleted": True}


# ── ACLs ─────────────────────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/acl")
def get_bucket_acl(bucket: str, user: dict = Depends(get_current_user)):
    resp = s3.get_bucket_acl(Bucket=bucket)
    return {"owner": resp.get("Owner", {}), "grants": [
        {"grantee": g.get("Grantee", {}), "permission": g.get("Permission", "")}
        for g in resp.get("Grants", [])
    ]}


@app.get("/api/buckets/{bucket}/object-acl")
def get_object_acl(bucket: str, key: str, user: dict = Depends(get_current_user)):
    resp = s3.get_object_acl(Bucket=bucket, Key=key)
    return {"owner": resp.get("Owner", {}), "grants": [
        {"grantee": g.get("Grantee", {}), "permission": g.get("Permission", "")}
        for g in resp.get("Grants", [])
    ]}


_CANNED_ACLS = {"private", "public-read", "public-read-write", "authenticated-read"}

class AclRequest(BaseModel):
    acl: str

@app.put("/api/buckets/{bucket}/acl")
def put_bucket_acl(bucket: str, req: AclRequest, user: dict = Depends(require_admin)):
    if req.acl not in _CANNED_ACLS:
        raise HTTPException(400, f"Invalid ACL. Allowed: {', '.join(sorted(_CANNED_ACLS))}")
    try:
        s3.put_bucket_acl(Bucket=bucket, ACL=req.acl)
        _audit("config_acl", user["username"], bucket=bucket, details=f"acl={req.acl}")
        return {"updated": True, "acl": req.acl}
    except ClientError as e:
        if "NotImplemented" in str(e) or "XNotImplemented" in str(e):
            return {"updated": False, "supported": False, "error": "ACL modification not supported by this storage provider"}
        raise


@app.put("/api/buckets/{bucket}/object-acl")
def put_object_acl(bucket: str, key: str, req: AclRequest, user: dict = Depends(require_admin)):
    if req.acl not in _CANNED_ACLS:
        raise HTTPException(400, f"Invalid ACL. Allowed: {', '.join(sorted(_CANNED_ACLS))}")
    try:
        s3.put_object_acl(Bucket=bucket, Key=key, ACL=req.acl)
        _audit("config_object_acl", user["username"], bucket=bucket, details=f"key={key}, acl={req.acl}")
        return {"updated": True, "acl": req.acl}
    except ClientError as e:
        if "NotImplemented" in str(e) or "XNotImplemented" in str(e):
            return {"updated": False, "supported": False, "error": "ACL modification not supported by this storage provider"}
        raise


# ── Tagging ──────────────────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/tagging")
def get_bucket_tagging(bucket: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_bucket_tagging(Bucket=bucket)
        return {"tags": {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}}
    except ClientError as e:
        if "NoSuchTagSet" in str(e): return {"tags": {}}
        raise


class TagRequest(BaseModel):
    tags: dict[str, str]

@app.put("/api/buckets/{bucket}/tagging")
def put_bucket_tagging(bucket: str, req: TagRequest, user: dict = Depends(require_admin)):
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in req.tags.items()]})
    _audit("config_tagging", user["username"], bucket=bucket, details=f"tags={len(req.tags)}")
    return {"updated": True}


@app.get("/api/buckets/{bucket}/object-tagging")
def get_object_tagging(bucket: str, key: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_object_tagging(Bucket=bucket, Key=key)
        return {"tags": {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}}
    except ClientError as e:
        if "NoSuchTagSet" in str(e): return {"tags": {}}
        raise


@app.put("/api/buckets/{bucket}/object-tagging")
def put_object_tagging(bucket: str, key: str, req: TagRequest, user: dict = Depends(require_admin)):
    s3.put_object_tagging(Bucket=bucket, Key=key, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in req.tags.items()]})
    _audit("config_object_tagging", user["username"], bucket=bucket, details=f"key={key}, tags={len(req.tags)}")
    return {"updated": True}


@app.delete("/api/buckets/{bucket}/object-tagging")
def delete_object_tagging(bucket: str, key: str, user: dict = Depends(require_admin)):
    s3.delete_object_tagging(Bucket=bucket, Key=key)
    _audit("config_object_tagging", user["username"], bucket=bucket, details=f"key={key}, deleted")
    return {"deleted": True}


# ── Object Lock / Retention / Legal Hold ─────────────────────────────────────

@app.get("/api/buckets/{bucket}/object-lock")
def get_object_lock_config(bucket: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_object_lock_configuration(Bucket=bucket)
        config = resp.get("ObjectLockConfiguration", {})
        return {"enabled": config.get("ObjectLockEnabled") == "Enabled",
                "rule": config.get("Rule", {}), "supported": True}
    except ClientError as e:
        if "ObjectLockConfigurationNotFoundError" in str(e):
            return {"enabled": False, "rule": {}, "supported": True}
        if "NotImplemented" in str(e) or "XNotImplemented" in str(e):
            return {"enabled": False, "rule": {}, "supported": False}
        raise


@app.get("/api/buckets/{bucket}/object-retention")
def get_object_retention(bucket: str, key: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_object_retention(Bucket=bucket, Key=key)
        ret = resp.get("Retention", {})
        return {"mode": ret.get("Mode"), "retain_until": ret.get("RetainUntilDate", "").isoformat() if ret.get("RetainUntilDate") else None}
    except ClientError as e:
        err = str(e)
        if any(x in err for x in ["NoSuchObjectLockConfiguration", "InvalidRequest", "NotImplemented", "XNotImplemented", "NoSuchKey"]):
            return {"mode": None, "retain_until": None}
        raise


@app.get("/api/buckets/{bucket}/object-legal-hold")
def get_object_legal_hold(bucket: str, key: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_object_legal_hold(Bucket=bucket, Key=key)
        return {"status": resp.get("LegalHold", {}).get("Status", "OFF")}
    except ClientError as e:
        err = str(e)
        if any(x in err for x in ["NoSuchObjectLockConfiguration", "InvalidRequest", "NotImplemented", "XNotImplemented", "NoSuchKey"]):
            return {"status": "OFF"}
        raise


# ── Multipart Uploads ────────────────────────────────────────────────────────

def _list_all_multipart_uploads(bucket: str) -> list:
    """Paginate through all incomplete multipart uploads for a bucket."""
    all_uploads = []
    kwargs = {"Bucket": bucket}
    while True:
        resp = s3.list_multipart_uploads(**kwargs)
        all_uploads.extend(resp.get("Uploads", []))
        if not resp.get("IsTruncated"):
            break
        kwargs["KeyMarker"] = resp["NextKeyMarker"]
        kwargs["UploadIdMarker"] = resp["NextUploadIdMarker"]
    return all_uploads

@app.get("/api/buckets/{bucket}/multipart-uploads")
def list_multipart_uploads(bucket: str, details: bool = False, user: dict = Depends(get_current_user)):
    raw_uploads = _list_all_multipart_uploads(bucket)
    now = datetime.now(timezone.utc)
    uploads = []
    total_size = 0
    stale_count = 0
    stale_size = 0
    for u in raw_uploads:
        initiated = u["Initiated"]
        age_hours = round((now - initiated).total_seconds() / 3600, 1)
        stale = age_hours >= 24
        entry = {
            "key": u["Key"],
            "upload_id": u["UploadId"],
            "initiated": initiated.isoformat(),
            "initiator": u.get("Initiator", {}).get("DisplayName", ""),
            "age_hours": age_hours,
            "stale": stale,
        }
        if details:
            part_count = 0
            size = 0
            try:
                parts_resp = s3.list_parts(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
                parts = parts_resp.get("Parts", [])
                part_count = len(parts)
                size = sum(p["Size"] for p in parts)
            except ClientError:
                pass
            entry["part_count"] = part_count
            entry["size"] = size
            total_size += size
            if stale:
                stale_size += size
        if stale:
            stale_count += 1
        uploads.append(entry)
    result = {"uploads": uploads, "count": len(uploads), "stale_count": stale_count}
    if details:
        result["total_size"] = total_size
        result["stale_size"] = stale_size
    return result


class AbortUploadRequest(BaseModel):
    key: str
    upload_id: str
    force: bool = False  # required to abort uploads less than 1 hour old

@app.post("/api/buckets/{bucket}/abort-multipart")
def abort_multipart(bucket: str, req: AbortUploadRequest, user: dict = Depends(require_admin)):
    # Safety check: refuse to abort recent uploads unless force=true
    if not req.force:
        for u in _list_all_multipart_uploads(bucket):
            if u["UploadId"] == req.upload_id:
                age_hours = (datetime.now(timezone.utc) - u["Initiated"]).total_seconds() / 3600
                if age_hours < 1:
                    raise HTTPException(400, f"Upload is only {age_hours:.1f}h old and may be in progress. Use force=true to abort.")
                break
    s3.abort_multipart_upload(Bucket=bucket, Key=req.key, UploadId=req.upload_id)
    _audit("abort_multipart", user["username"], bucket=bucket, details=f"key={req.key}")
    return {"aborted": req.upload_id}

@app.post("/api/buckets/{bucket}/abort-all-multipart")
def abort_all_multipart(bucket: str, min_age_hours: float = 24, user: dict = Depends(require_admin)):
    """Abort stale multipart uploads. Only uploads older than min_age_hours (default 24) are aborted."""
    now = datetime.now(timezone.utc)
    aborted = []
    skipped = 0
    for u in _list_all_multipart_uploads(bucket):
        age_hours = (now - u["Initiated"]).total_seconds() / 3600
        if age_hours >= min_age_hours:
            s3.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
            aborted.append(u["UploadId"])
        else:
            skipped += 1
    _audit("abort_all_multipart", user["username"], bucket=bucket, details=f"aborted={len(aborted)} stale uploads, skipped={skipped} active")
    return {"aborted": aborted, "count": len(aborted), "skipped": skipped}


# ── Copy / Rename ────────────────────────────────────────────────────────────

class CopyRequest(BaseModel):
    source_key: str
    dest_key: str
    dest_bucket: Optional[str] = None

@app.post("/api/buckets/{bucket}/copy")
def copy_object(bucket: str, req: CopyRequest, request: Request, user: dict = Depends(require_admin)):
    dest_bucket = req.dest_bucket or bucket
    # Cross-bucket copy: check write permission on dest bucket
    if dest_bucket != bucket and user["role"] != "admin":
        with _get_users_db() as udb:
            row = udb.execute("SELECT permission FROM bucket_permissions WHERE username=? AND bucket=?",
                              (user["username"], dest_bucket)).fetchone()
        if not row or row["permission"] != "write":
            raise HTTPException(403, f"Write access required on bucket '{dest_bucket}'")
    s3.copy_object(Bucket=dest_bucket, CopySource={"Bucket": bucket, "Key": req.source_key}, Key=req.dest_key)
    # Update index for destination bucket
    target = dest_bucket
    if os.path.exists(_db_path(target)):
        try:
            head = s3.head_object(Bucket=target, Key=req.dest_key)
            with _get_db(target) as db:
                old = db.execute("SELECT size FROM objects WHERE key=?", (req.dest_key,)).fetchone()
                db.execute(
                    "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth) VALUES (?,?,?,?,?,?)",
                    (req.dest_key, head["ContentLength"], head["LastModified"].isoformat(),
                     head.get("ETag", "").strip('"'), _key_prefix(req.dest_key), _key_depth(req.dest_key)))
                if old:
                    _adjust_folder_stats(db, req.dest_key, head["ContentLength"] - old[0], 0)
                    _adjust_prefix_children(db, req.dest_key, head["ContentLength"] - old[0], 0)
                else:
                    _adjust_folder_stats(db, req.dest_key, head["ContentLength"], 1)
                    _adjust_prefix_children(db, req.dest_key, head["ContentLength"], 1)
                db.commit()
            _update_crawl_counters(target)
        except Exception as e:
            log.warning("Failed to update index after copy: %s", e)
    _audit("copy", user["username"], bucket=dest_bucket, details=f"{bucket}:{req.source_key} -> {dest_bucket}:{req.dest_key}")
    return {"copied": req.dest_key, "dest_bucket": dest_bucket}


@app.post("/api/buckets/{bucket}/rename")
def rename_object(bucket: str, req: CopyRequest, user: dict = Depends(require_admin)):
    s3.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": req.source_key}, Key=req.dest_key)
    s3.delete_object(Bucket=bucket, Key=req.source_key)
    if os.path.exists(_db_path(bucket)):
        with _get_db(bucket) as db:
            # Read source metadata before deleting
            row = db.execute("SELECT size, last_modified, etag FROM objects WHERE key=?", (req.source_key,)).fetchone()
            if row:
                _adjust_folder_stats(db, req.source_key, -row[0], -1)
                _adjust_prefix_children(db, req.source_key, -row[0], -1)
            db.execute("DELETE FROM objects WHERE key=?", (req.source_key,))
            if row:
                db.execute(
                    "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth) VALUES (?,?,?,?,?,?)",
                    (req.dest_key, row[0], row[1], row[2], _key_prefix(req.dest_key), _key_depth(req.dest_key)))
                _adjust_folder_stats(db, req.dest_key, row[0], 1)
                _adjust_prefix_children(db, req.dest_key, row[0], 1)
            else:
                try:
                    head = s3.head_object(Bucket=bucket, Key=req.dest_key)
                    db.execute(
                        "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth) VALUES (?,?,?,?,?,?)",
                        (req.dest_key, head["ContentLength"], head["LastModified"].isoformat(),
                         head.get("ETag", "").strip('"'), _key_prefix(req.dest_key), _key_depth(req.dest_key)))
                    _adjust_folder_stats(db, req.dest_key, head["ContentLength"], 1)
                    _adjust_prefix_children(db, req.dest_key, head["ContentLength"], 1)
                except Exception as head_e:
                    log.debug("Head object after rename failed for %s: %s", req.dest_key, head_e)
            db.commit()
    _audit("rename", user["username"], bucket=bucket, details=f"{req.source_key} -> {req.dest_key}")
    return {"renamed": req.dest_key}


# ── Bucket Website ──────────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/website")
def get_bucket_website(bucket: str, user: dict = Depends(get_current_user)):
    try:
        resp = s3.get_bucket_website(Bucket=bucket)
        return {"index_document": resp.get("IndexDocument", {}).get("Suffix"),
                "error_document": resp.get("ErrorDocument", {}).get("Key"),
                "supported": True}
    except ClientError as e:
        if "NoSuchWebsiteConfiguration" in str(e):
            return {"index_document": None, "error_document": None, "supported": True}
        if "NotImplemented" in str(e) or "XNotImplemented" in str(e):
            return {"index_document": None, "error_document": None, "supported": False}
        raise


# ── Bucket Location ──────────────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/location")
def get_bucket_location(bucket: str, user: dict = Depends(get_current_user)):
    resp = s3.get_bucket_location(Bucket=bucket)
    return {"location": resp.get("LocationConstraint", "us-east-1")}


# ── Backward-compatible aliases (old single-bucket API) ─────────────────────
# These redirect old /api/list to the new /api/buckets/{bucket}/list format
# using the S3_BUCKET env var as default bucket

_DEFAULT_BUCKET = os.environ.get("S3_BUCKET", "")

def _require_default_bucket():
    if not _DEFAULT_BUCKET:
        raise HTTPException(400, "No default bucket configured (set S3_BUCKET env var)")
    return _DEFAULT_BUCKET

def _check_compat_bucket_read(user: dict):
    """Check the user has read access to the default bucket (compat endpoints bypass middleware)."""
    if user["role"] == "admin":
        return
    with _get_users_db() as db:
        row = db.execute("SELECT permission FROM bucket_permissions WHERE username=? AND bucket=?",
                         (user["username"], _DEFAULT_BUCKET)).fetchone()
    if not row:
        raise HTTPException(403, f"No access to bucket '{_DEFAULT_BUCKET}'")

@app.get("/api/list")
def list_objects_compat(prefix: str = "", user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return list_objects(_require_default_bucket(), prefix)

@app.get("/api/search")
def search_compat(q: str = Query(..., min_length=1), prefix: str = "", limit: int = 200, user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return search_objects(_require_default_bucket(), q, prefix, limit)

@app.get("/api/crawl-status")
def crawl_status_compat(user: dict = Depends(get_current_user)):
    if not _DEFAULT_BUCKET:
        return {"status": "no_bucket"}
    return crawl_status(_DEFAULT_BUCKET)

@app.post("/api/crawl")
def trigger_crawl_compat(user: dict = Depends(require_admin)):
    return trigger_crawl(_require_default_bucket())

@app.get("/api/folder-size")
def folder_size_compat(prefix: str = "", user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return folder_size(_require_default_bucket(), prefix)

@app.get("/api/storage-breakdown")
def storage_breakdown_compat(prefix: str = "", user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return storage_breakdown(_require_default_bucket(), prefix)

@app.get("/api/download")
def download_compat(key: str, user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return download_object(_require_default_bucket(), key)

@app.delete("/api/objects")
def delete_compat(req: DeleteRequest, user: dict = Depends(require_admin)):
    return delete_objects(_require_default_bucket(), req)

@app.post("/api/upload")
async def upload_compat(request: Request, prefix: str = Form(""), files: list[UploadFile] = File(...)):
    return await upload_files(_require_default_bucket(), request, prefix, files)

@app.get("/api/object-info")
def object_info_compat(key: str, user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return object_info(_require_default_bucket(), key)

@app.get("/api/object-versions")
def object_versions_compat(key: str, user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return object_versions(_require_default_bucket(), key)

@app.get("/api/presigned-url")
def presigned_url_compat(key: str, expires: int = 3600, user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return get_presigned_url(_require_default_bucket(), key, expires)

@app.get("/api/bucket-versioning")
def versioning_compat(user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return get_versioning(_require_default_bucket())

@app.get("/api/bucket-lifecycle")
def lifecycle_compat(user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return get_lifecycle(_require_default_bucket())

@app.get("/api/bucket-cors")
def cors_compat(user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return get_cors(_require_default_bucket())

@app.get("/api/multipart-uploads")
def multipart_compat(user: dict = Depends(get_current_user)):
    _check_compat_bucket_read(user)
    return list_multipart_uploads(_require_default_bucket())

@app.post("/api/abort-multipart")
def abort_multipart_compat(req: AbortUploadRequest, user: dict = Depends(require_admin)):
    return abort_multipart(_require_default_bucket(), req)

@app.post("/api/abort-all-multipart")
def abort_all_multipart_compat(user: dict = Depends(require_admin)):
    return abort_all_multipart(_require_default_bucket())

@app.get("/api/bucket-info")
def bucket_info_compat(user: dict = Depends(get_current_user)):
    return {"bucket": _DEFAULT_BUCKET}


# ── Serve React SPA ─────────────────────────────────────────────────────────
# ── White-label branding injection (pure, unit-testable) ─────────────────────

def _brand_name_color():
    return (os.environ.get("APP_NAME", "Sairo"),
            os.environ.get("PRIMARY_COLOR", "#3b82f6"))

def _apply_branding_html(doc: str, name: str, color: str) -> str:
    """Rewrite the brandable fields of index.html — tab title, og:title,
    description, theme-color — from APP_NAME / PRIMARY_COLOR. Done server-side so
    a white-label deployment is correct the instant the page loads (no client
    flash from "Sairo", and correct og:title for link/social previews)."""
    import re, html as _html
    n = _html.escape(name, quote=True)
    c = _html.escape(color, quote=True)
    doc = re.sub(r"<title>.*?</title>", f"<title>{n}</title>", doc, count=1, flags=re.S)
    doc = re.sub(r'(<meta property="og:title" content=")[^"]*(")', rf"\g<1>{n}\g<2>", doc)
    doc = re.sub(r'(<meta name="description" content=")Sairo\b', rf"\g<1>{n}", doc)
    doc = re.sub(r'(<meta name="theme-color" content=")[^"]*(")', rf"\g<1>{c}\g<2>", doc)
    return doc

def _branded_manifest(m: dict, name: str, color: str) -> dict:
    """PWA manifest branded from APP_NAME / PRIMARY_COLOR."""
    m = dict(m)
    m["name"] = name
    m["short_name"] = name
    if color:
        m["theme_color"] = color
    return m


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

    _spa_cache: dict = {}

    def _spa_index_html():
        if "html" not in _spa_cache:
            try:
                with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
                    _spa_cache["html"] = _apply_branding_html(f.read(), *_brand_name_color())
            except Exception:
                _spa_cache["html"] = None
        return _spa_cache["html"]

    @app.get("/manifest.json")
    def serve_manifest():
        try:
            with open(os.path.join(static_dir, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            m = {"start_url": "/", "display": "standalone"}
        return JSONResponse(_branded_manifest(m, *_brand_name_color()))

    @app.get("/{path:path}")
    def serve_spa(path: str):
        file_path = os.path.realpath(os.path.join(static_dir, path))
        if not file_path.startswith(os.path.realpath(static_dir)):
            raise HTTPException(403, "Forbidden")
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # SPA entry: serve index.html with the app name/colour baked in.
        doc = _spa_index_html()
        if doc is not None:
            return HTMLResponse(doc)
        return FileResponse(os.path.join(static_dir, "index.html"))

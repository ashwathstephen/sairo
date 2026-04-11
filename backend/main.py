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
from fastapi.responses import RedirectResponse, FileResponse, StreamingResponse, JSONResponse
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


# ── Multi-Endpoint URL Rewriting Middleware ─────────────────────────────────

@app.middleware("http")
async def endpoint_routing_middleware(request: Request, call_next):
    """Rewrite /api/e/{endpoint_id}/... → /api/... and set endpoint context for S3 client proxy."""
    path = request.url.path
    endpoint_id = "default"
    if path.startswith("/api/e/"):
        parts = path.split("/")
        # parts: ['', 'api', 'e', endpoint_id, ...]
        if len(parts) >= 5:
            endpoint_id = parts[3]
            # Rewrite URL: remove /e/{endpoint_id} segment
            new_path = "/api/" + "/".join(parts[4:])
            request.scope["path"] = new_path
    request.state.endpoint_id = endpoint_id
    # Set context variable — propagates to sync handlers via Starlette's run_in_threadpool
    token = _endpoint_ctx.set(endpoint_id)
    try:
        return await call_next(request)
    finally:
        _endpoint_ctx.reset(token)

# ── Bucket Permission Middleware ────────────────────────────────────────────

@app.middleware("http")
async def bucket_permission_middleware(request: Request, call_next):
    """Check per-bucket permissions for all /api/buckets/{bucket}/... routes."""
    path = request.scope.get("path", request.url.path)
    if not path.startswith("/api/buckets/"):
        return await call_next(request)
    parts = path.split("/")
    if len(parts) < 4:
        return await call_next(request)
    bucket = parts[3]
    # Try to get user — if auth fails, let endpoint handle 401
    try:
        user = get_current_user(request)
    except HTTPException:
        return await call_next(request)
    # Admin bypasses everything
    if user["role"] == "admin":
        request.state.bucket_permission = "admin"
        return await call_next(request)
    # Non-admin: lookup bucket permission
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
    msg = exc.response.get("Error", {}).get("Message", str(exc))
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
SAIRO_VERSION = "3.2.0"
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
    log.warning("ADMIN_PASS not set — generated temporary password: %s", ADMIN_PASS)
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
)

# ── Multi-Endpoint S3 Client Manager ──────────────────────────────────────

class S3ClientManager:
    """Thread-safe cache of boto3 S3 clients keyed by endpoint ID."""
    def __init__(self):
        self._clients: dict = {}
        self._lock = threading.Lock()
        self._endpoints: dict = {}  # endpoint_id -> {endpoint_url, access_key, secret_key, region, path_style}

    def register(self, endpoint_id: str, endpoint_url: str, access_key: str, secret_key: str,
                 region: str = "", path_style: bool = False):
        with self._lock:
            self._endpoints[endpoint_id] = {
                "endpoint_url": endpoint_url, "access_key": access_key,
                "secret_key": secret_key, "region": region, "path_style": path_style,
            }
            self._clients.pop(endpoint_id, None)  # Invalidate cached client

    def get_client(self, endpoint_id: str = "default"):
        with self._lock:
            if endpoint_id in self._clients:
                return self._clients[endpoint_id]
            info = self._endpoints.get(endpoint_id)
            if not info:
                raise HTTPException(404, f"S3 endpoint '{endpoint_id}' not found")
            cfg = _S3_CONFIG
            if info.get("path_style"):
                cfg = _S3_CONFIG.merge(Config(s3={"addressing_style": "path"}))
            kwargs = {
                "endpoint_url": info["endpoint_url"],
                "aws_access_key_id": info["access_key"],
                "aws_secret_access_key": info["secret_key"],
                "config": cfg,
            }
            if info["region"]:
                kwargs["region_name"] = info["region"]
            client = boto3.client("s3", **kwargs)
            self._clients[endpoint_id] = client
            return client

    def invalidate(self, endpoint_id: str):
        with self._lock:
            self._clients.pop(endpoint_id, None)
            self._endpoints.pop(endpoint_id, None)

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
    # 2FA columns (added via ALTER TABLE for backward compat)
    for col, coldef in [
        ("totp_secret", "TEXT"),
        ("totp_enabled", "INTEGER DEFAULT 0"),
        ("recovery_codes", "TEXT"),  # JSON array of bcrypt-hashed codes
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass  # column already exists
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
    conn = sqlite3.connect(_db_path(bucket, endpoint_id))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size = -64000")   # 64MB page cache (default 2MB)
    conn.execute("PRAGMA temp_store = MEMORY")    # temp tables in RAM
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prefix ON objects(prefix)")
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
_crawling = {}   # bucket -> bool (currently running)
_queued = set()  # buckets waiting in the thread pool queue
_crawl_lock = threading.Lock()


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


def _run_crawl(bucket, endpoint_id=None):
    """Prefix-parallel crawl — discovers top-level prefixes, crawls each independently."""
    eid = endpoint_id or "default"
    crawl_key = f"{eid}:{bucket}"
    with _crawl_lock:
        _queued.discard(crawl_key)
        if _crawling.get(crawl_key):
            return
        _crawling[crawl_key] = True

    # Set thread-local so _db_path picks up the right endpoint
    _s3_context.endpoint_id = eid
    client = _s3_manager.get_client(eid)

    _init_db(bucket, eid)

    with _get_db(bucket, eid) as db:
        db.execute("UPDATE crawl_status SET status='crawling', last_crawl_start=?, current_crawl_gen=current_crawl_gen+1 WHERE id=1",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        db.commit()
        crawl_gen = db.execute("SELECT current_crawl_gen FROM crawl_status WHERE id=1").fetchone()[0]
        # Disable FTS triggers during bulk crawl for performance
        _disable_fts_triggers(db)

    crawl_start = time.monotonic()
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
            # Re-enable FTS triggers (instant) and rebuild index in background
            with _get_db(bucket, eid) as db:
                _enable_fts_triggers(db)
            _rebuild_fts_async(bucket, eid)
            elapsed = time.monotonic() - crawl_start
            log.info("[%s:%s] Crawl complete: %s objects, %.1f GB in %.1fs",
                     eid, bucket, f"{row[0]:,}", row[1] / (1024**3), elapsed)
            _record_storage_snapshot(bucket, eid)
            _rebuild_folder_stats(bucket, eid)
            _rebuild_prefix_children(bucket, eid)
            return

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

        # Remove stale keys: anything with crawl_gen > 0 but older than current gen
        with _get_db(bucket, eid) as db:
            stale_count = db.execute("SELECT COUNT(*) FROM objects WHERE crawl_gen > 0 AND crawl_gen < ?", (crawl_gen,)).fetchone()[0]
            if stale_count > 0:
                db.execute("DELETE FROM objects WHERE crawl_gen > 0 AND crawl_gen < ?", (crawl_gen,))
                db.commit()
                log.info("[%s:%s] Removed %s stale keys", eid, bucket, f"{stale_count:,}")

        # Final counts
        with _get_db(bucket, eid) as db:
            row = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
            total_objects, total_size = row[0], row[1]
            db.execute(
                "UPDATE crawl_status SET status='complete', last_crawl_end=?, total_objects=?, total_size=? WHERE id=1",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ"), total_objects, total_size))
            db.commit()

        elapsed = time.monotonic() - crawl_start
        msg = f"[{eid}:{bucket}] Crawl complete: {total_objects:,} objects, {total_size / (1024**3):.1f} GB in {elapsed:.1f}s"
        if failed_prefixes:
            msg += f", {len(failed_prefixes)} prefixes failed"
        log.info(msg)
        # Re-enable FTS triggers (instant) and rebuild index in background
        with _get_db(bucket, eid) as db:
            _enable_fts_triggers(db)
        _rebuild_fts_async(bucket, eid)
        _record_storage_snapshot(bucket, eid)
        _rebuild_folder_stats(bucket, eid)
        _rebuild_prefix_children(bucket, eid)

    except BaseException as e:
        log.error("[%s:%s] Crawl error: %s\n%s", eid, bucket, e, traceback.format_exc())
        try:
            with _get_db(bucket, eid) as db:
                _enable_fts_triggers(db)  # Always re-enable even on error
                db.execute("UPDATE crawl_status SET status=? WHERE id=1", (f"error: {e}",))
                db.commit()
        except Exception as inner_e:
            log.warning("Failed to write crawl error status for %s: %s", bucket, inner_e)
    finally:
        with _crawl_lock:
            _crawling[crawl_key] = False


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
            _crawling[crawl_key] = False


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
        if not row:
            return False
        # Index is usable if we have data (even during recrawl) or status is complete
        return row["status"] == "complete" or (row["total_objects"] or 0) > 0


RECRAWL_INTERVAL = int(os.environ.get("RECRAWL_INTERVAL", "120"))  # seconds, default 2 minutes


def _queue_crawl(bucket, endpoint_id=None):
    """Queue a crawl for a bucket if it is not already running or queued."""
    eid = endpoint_id or "default"
    crawl_key = f"{eid}:{bucket}"
    with _crawl_lock:
        if _crawling.get(crawl_key) or crawl_key in _queued:
            return False
        _queued.add(crawl_key)
    _crawl_pool.submit(_run_crawl, bucket, eid)
    return True


def _auto_recrawl():
    """Periodically re-crawl all buckets across all endpoints to pick up new objects."""
    while True:
        try:
            time.sleep(RECRAWL_INTERVAL)
            for eid in _s3_manager.get_all_ids():
                try:
                    client = _s3_manager.get_client(eid)
                    resp = client.list_buckets()
                    for b in resp.get("Buckets", []):
                        name = b["Name"]
                        if _queue_crawl(name, eid):
                            log.info("Auto-recrawl queued for %s:%s", eid, name)
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
        threading.Thread(target=_telemetry_loop, daemon=True).start()
        log.info("Anonymous telemetry enabled. Set TELEMETRY=false to disable.")
    else:
        log.info("Telemetry disabled.")


# ── Telemetry ─────────────────────────────────────────────────────────────

TELEMETRY_URL = "https://dashboard.sairo.dev/api/v1/ping"
TELEMETRY_INTERVAL = 86400  # 24 hours

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

    # Count users, endpoints, and API token usage (MCP/CLI indicator)
    user_count = 0
    endpoint_count = 0
    api_tokens = 0
    api_tokens_active = 0
    try:
        with _get_users_db() as db:
            user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            endpoint_count = db.execute("SELECT COUNT(*) FROM s3_endpoints").fetchone()[0]
            api_tokens = db.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]
            api_tokens_active = db.execute(
                "SELECT COUNT(*) FROM api_tokens WHERE last_used IS NOT NULL AND last_used > datetime('now', '-7 days')"
            ).fetchone()[0]
    except Exception:
        pass

    provider = detect_provider(S3_ENDPOINT)

    return {
        "instance_id": instance_id,
        "version": SAIRO_VERSION,
        "buckets": bucket_count,
        "total_objects": total_objects,
        "total_size": total_size,
        "provider": provider,
        "uptime_hours": uptime_hours,
        "os": f"{platform.system().lower()}/{platform.machine()}",
        "endpoints": endpoint_count,
        "users": user_count,
        "auth_mode": AUTH_MODE,
        "api_tokens": api_tokens,
        "api_tokens_active": api_tokens_active,
    }

def _telemetry_loop():
    """Background thread: send anonymous heartbeat every 24 hours."""
    import urllib.request
    import json as _json
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
    # Test the S3 credentials
    try:
        cfg = _S3_CONFIG
        if _S3_PATH_STYLE:
            cfg = _S3_CONFIG.merge(Config(s3={"addressing_style": "path"}))
        test_client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=req.access_key,
            aws_secret_access_key=req.secret_key,
            region_name=_S3_REGION or None,
            config=cfg,
        )
        test_client.list_buckets()
    except Exception as e:
        log.warning("S3 auth failed for access_key=%s: %s", req.access_key[:6] + "...", e)
        raise HTTPException(401, "Invalid S3 credentials")
    # Credentials valid — issue a session as admin
    # Use a sanitized version of the access key as the username
    username = f"s3:{req.access_key[:8]}"
    token = jwt.encode(
        {"sub": username, "role": "admin",
         "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
        JWT_SECRET, algorithm="HS256")
    response = JSONResponse({"username": username, "role": "admin"})
    _secure_cookie = os.environ.get("SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        secure=_secure_cookie, max_age=SESSION_HOURS * 3600, path="/")
    _audit("login", username, details="s3_auth")
    return response


@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"logged_out": True})
    response.delete_cookie("access_token", path="/")
    return response

@app.get("/api/auth/me")
def auth_me(user: dict = Depends(get_current_user), request: Request = None):
    result = {"username": user["username"], "role": user["role"]}
    # Include 2FA status
    with _get_users_db() as db:
        row = db.execute("SELECT totp_enabled FROM users WHERE username=?", (user["username"],)).fetchone()
    if row:
        result["totp_enabled"] = bool(row["totp_enabled"])
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
        rows = db.execute("SELECT username, role, created_at, totp_enabled FROM users ORDER BY created_at").fetchall()
    users = []
    for r in rows:
        u = dict(r)
        u["totp_enabled"] = bool(u.get("totp_enabled"))
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
    # Store the pending secret (not yet enabled)
    with _get_users_db() as db:
        db.execute("UPDATE users SET totp_secret=? WHERE username=?", (secret, user["username"]))
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
    totp = pyotp.TOTP(row["totp_secret"])
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
    totp = pyotp.TOTP(row["totp_secret"])
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
        row = db.execute("SELECT id FROM share_links WHERE id=?", (link_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Share link not found")
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
    oauth_providers = []
    if os.environ.get("OAUTH_GOOGLE_CLIENT_ID"):
        oauth_providers.append({"id": "google", "name": "Google"})
    if os.environ.get("OAUTH_GITHUB_CLIENT_ID"):
        oauth_providers.append({"id": "github", "name": "GitHub"})
    return {
        "app_name": os.environ.get("APP_NAME", "Sairo"),
        "app_logo": os.environ.get("APP_LOGO", ""),  # URL to custom logo
        "primary_color": os.environ.get("PRIMARY_COLOR", "#3b82f6"),
        "login_message": os.environ.get("LOGIN_MESSAGE", ""),
        "ldap_enabled": os.environ.get("LDAP_ENABLED", "false").lower() == "true",
        "oauth_providers": oauth_providers,
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
    update_available = latest and latest != SAIRO_VERSION and latest > SAIRO_VERSION
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

    # Sync to local users table (create or update role)
    with _get_users_db() as db:
        existing = db.execute("SELECT username, totp_enabled FROM users WHERE username=?", (req.username,)).fetchone()
        if existing:
            db.execute("UPDATE users SET role=? WHERE username=?", (role, req.username))
        else:
            # Create user with a random unusable password (LDAP-only auth)
            db.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                       (req.username, f"LDAP:{secrets.token_hex(16)}", role))
        db.commit()

    # Check 2FA
    if existing and existing["totp_enabled"]:
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

@app.get("/api/auth/oauth/providers")
def oauth_providers():
    """Public endpoint — list available OAuth providers."""
    providers = []
    if OAUTH_GOOGLE_CLIENT_ID:
        providers.append({"id": "google", "name": "Google"})
    if OAUTH_GITHUB_CLIENT_ID:
        providers.append({"id": "github", "name": "GitHub"})
    return {"providers": providers}

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

    # Sync to local DB
    role = OAUTH_DEFAULT_ROLE
    totp_enabled = False
    with _get_users_db() as db:
        existing = db.execute("SELECT role, totp_enabled FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            role = existing["role"]  # Preserve existing role
            totp_enabled = bool(existing["totp_enabled"])
        else:
            db.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                       (username, f"OAUTH:{secrets.token_hex(16)}", role))
            db.commit()

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
def health_detail(user: dict = Depends(get_current_user)):
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
                bucket_info["crawling"] = _crawling.get(name, False)
                bucket_info["queued"] = name in _queued
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

@app.get("/api/buckets")
def list_buckets(user: dict = Depends(get_current_user)):
    resp = s3.list_buckets()
    # Non-admin: only show buckets with explicit permissions
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

def _list_from_index(bucket, prefix):
    prefix_len = len(prefix)
    t0 = time.monotonic()

    with _get_db(bucket) as db:
        seen = set()
        folders = []

        if not prefix:
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
        else:
            # Subfolder: use pre-computed prefix_children (instant), fallback to DISTINCT scan
            t_q = time.monotonic()
            pc_rows = db.execute(
                "SELECT child_prefix, child_name FROM prefix_children WHERE parent_prefix = ?",
                (prefix,)).fetchall()
            if pc_rows:
                for child_prefix, child_name in pc_rows:
                    if child_name and child_prefix not in seen:
                        seen.add(child_prefix)
                        folders.append({"prefix": child_prefix, "name": child_name})
                log.info("[perf] _list_from_index prefix_children: %.3fs (%d rows) prefix=%s",
                         time.monotonic() - t_q, len(pc_rows), prefix[:60])
            else:
                # Fallback: compute children in SQL (first boot or table not yet populated)
                prefix_end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
                rows = db.execute(
                    "SELECT DISTINCT substr(prefix, 1, ? + instr(substr(prefix, ?+1), '/')) "
                    "FROM objects WHERE prefix >= ? AND prefix < ? "
                    "AND instr(substr(prefix, ?+1), '/') > 0",
                    (prefix_len, prefix_len, prefix, prefix_end, prefix_len)).fetchall()
                log.info("[perf] _list_from_index DISTINCT fallback: %.3fs (%d rows) prefix=%s",
                         time.monotonic() - t_q, len(rows), prefix[:60])
                for (child,) in rows:
                    if child and child not in seen:
                        seen.add(child)
                        name = child[prefix_len:].rstrip("/")
                        if name:
                            folders.append({"prefix": child, "name": name})

        folders.sort(key=lambda f: f["name"])
        t_files = time.monotonic()

        file_rows = db.execute("""
            SELECT key, size, last_modified FROM objects
            WHERE prefix = ? ORDER BY key
        """, (prefix,)).fetchall()

        files = [
            {"key": r["key"], "name": r["key"][prefix_len:], "size": r["size"], "last_modified": r["last_modified"]}
            for r in file_rows
        ]
        log.info("[perf] _list_from_index files query: %.3fs (%d files) prefix=%s",
                 time.monotonic() - t_files, len(files), prefix[:60])

    log.info("[perf] _list_from_index total: %.3fs (%d folders, %d files) prefix=%s",
             time.monotonic() - t0, len(folders), len(files), prefix[:60])
    return folders, files


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
def list_objects(bucket: str, prefix: str = "", fresh: bool = False, user: dict = Depends(get_current_user)):
    """List objects at a prefix. Uses index when available (instant), falls back to S3 streaming.
    Pass fresh=true to force a direct S3 listing bypassing the index."""
    eid = _current_endpoint_id()
    if _is_index_ready(bucket) and not fresh:
        folders, files = _list_from_index(bucket, prefix)
        def gen():
            yield json.dumps({"folders": folders, "files": files, "done": True,
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
    if not _is_index_ready(bucket):
        raise HTTPException(503, "Index not ready — crawl in progress")
    with _get_db(bucket) as db:
        rows = _search_fts(db, q, prefix, limit)
    return {"results": [dict(r) for r in rows], "count": len(rows), "query": q}


def _search_fts(db, q, prefix, limit):
    """Search using FTS5 trigram index, falling back to LIKE for old DBs or short queries."""
    # Trigram tokenizer requires >= 3 char terms; fall back to LIKE for shorter
    if len(q) >= 3:
        try:
            fts_query = '"' + q.replace('"', '""') + '"'
            if prefix:
                return db.execute("""
                    SELECT o.key, o.size, o.last_modified FROM objects o
                    JOIN objects_fts f ON o.rowid = f.rowid
                    WHERE objects_fts MATCH ? AND o.key LIKE ?
                    ORDER BY o.key LIMIT ?
                """, (fts_query, prefix + "%", limit)).fetchall()
            else:
                return db.execute("""
                    SELECT o.key, o.size, o.last_modified FROM objects o
                    JOIN objects_fts f ON o.rowid = f.rowid
                    WHERE objects_fts MATCH ?
                    ORDER BY o.key LIMIT ?
                """, (fts_query, limit)).fetchall()
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
            row = db.execute("SELECT COUNT(*) as count, COALESCE(SUM(size),0) as total_size FROM objects WHERE key LIKE ?",
                             (prefix + "%",)).fetchone()
        else:
            # Fast path: use pre-computed totals from crawl_status
            row = db.execute("SELECT total_objects as count, total_size as total_size FROM crawl_status WHERE id=1").fetchone()
    return {"prefix": prefix or "(all)", "object_count": row["count"], "total_size": row["total_size"]}


# ── API: Storage Breakdown ──────────────────────────────────────────────────

@app.get("/api/buckets/{bucket}/storage-breakdown")
def storage_breakdown(bucket: str, prefix: str = "", user: dict = Depends(get_current_user)):
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

    # Fast path: use pre-computed prefix_children for sub-prefix breakdown
    t_sb = time.monotonic()
    prefix_len = len(prefix)
    with _get_db(bucket) as db:
        pc_rows = db.execute(
            "SELECT child_prefix, child_name, object_count, total_size FROM prefix_children WHERE parent_prefix = ? ORDER BY total_size DESC",
            (prefix,)).fetchall()
        if pc_rows:
            children = [{"prefix": r[0], "name": r[1], "object_count": r[2], "total_size": r[3]} for r in pc_rows]
            root_row = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM objects WHERE prefix = ?",
                (prefix,)).fetchone()
            if root_row and root_row[0] > 0:
                children.append({"prefix": "(root files)", "name": "(files)", "object_count": root_row[0], "total_size": root_row[1]})
            log.info("[perf] storage_breakdown prefix_children: %.3fs (%d children) prefix=%s",
                     time.monotonic() - t_sb, len(children), prefix[:60])
        else:
            # Fallback: full GROUP BY query for sub-prefix or when prefix_children not yet populated
            like_pattern = (prefix + "%") if prefix else "%"
            rows = db.execute("""
                SELECT substr(key, 1, ? + instr(substr(key, ? + 1), '/')) as child_prefix,
                       COUNT(*) as count, SUM(size) as total_size
                FROM objects WHERE key LIKE ? AND instr(substr(key, ? + 1), '/') > 0
                GROUP BY child_prefix ORDER BY total_size DESC
            """, (prefix_len, prefix_len, like_pattern, prefix_len)).fetchall()
            root_row = db.execute("""
                SELECT COUNT(*) as count, COALESCE(SUM(size), 0) as total_size
                FROM objects WHERE key LIKE ? AND instr(substr(key, ? + 1), '/') = 0
            """, (like_pattern, prefix_len)).fetchone()
            log.info("[perf] storage_breakdown slow path: %.3fs (%d children) prefix=%s",
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
        rows = db.execute(
            "SELECT DATE(timestamp) as day, MAX(object_count) as object_count, MAX(total_size) as total_size, MAX(timestamp) as timestamp "
            "FROM storage_history "
            "WHERE prefix = ? AND timestamp >= ? "
            "GROUP BY day ORDER BY day ASC",
            (prefix, cutoff),
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
        total_row = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
        total_objects, total_size = total_row

        if total_objects == 0:
            return {"bucket": bucket, "total_objects": 0, "total_size": 0, "age_distribution": [],
                    "cold_data": {}, "duplicates": {}, "lifecycle": {}, "tiering": {}}

        # ── Age distribution (single query with CASE WHEN) ──
        age_thresholds = [7, 30, 90, 180, 365]
        now_utc = datetime.now(timezone.utc)
        cutoffs = {d: (now_utc - timedelta(days=d)).isoformat() for d in age_thresholds}
        age_row = db.execute(
            """SELECT
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
        age_distribution = []
        for i, days in enumerate(age_thresholds):
            cnt, sz = age_row[i*2], age_row[i*2+1]
            age_distribution.append({
                "older_than_days": days,
                "object_count": cnt,
                "total_size": sz,
                "pct_objects": round(cnt / total_objects * 100, 1) if total_objects else 0,
                "pct_size": round(sz / total_size * 100, 1) if total_size else 0,
            })

        # ── Cold data by top-level folder ──
        cold_threshold_days = 30
        cold_cutoff = (datetime.now(timezone.utc) - timedelta(days=cold_threshold_days)).isoformat()
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

        folder_totals = {}
        for row in db.execute("""
            SELECT
                CASE WHEN INSTR(key, '/') > 0 THEN SUBSTR(key, 1, INSTR(key, '/')) ELSE '(root)' END as folder,
                COUNT(*) as cnt, COALESCE(SUM(size), 0) as sz
            FROM objects GROUP BY folder
        """).fetchall():
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

        # ── Duplicate detection ──
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


MULTIPART_THRESHOLD = 5 * 1024 * 1024  # 5 MB
MULTIPART_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per part


def _upload_multipart(bucket, key, file_obj, endpoint_id=None):
    """Upload a file using S3 multipart upload for large files."""
    client = _s3_manager.get_client(endpoint_id or _current_endpoint_id())
    upload = client.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = upload["UploadId"]
    parts = []
    part_number = 1
    total_size = 0
    try:
        while True:
            chunk = file_obj.read(MULTIPART_CHUNK_SIZE)
            if not chunk:
                break
            resp = client.upload_part(
                Bucket=bucket, Key=key, UploadId=upload_id,
                PartNumber=part_number, Body=chunk,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
            total_size += len(chunk)
            part_number += 1
        client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise
    return total_size


@app.post("/api/buckets/{bucket}/upload")
@limiter.limit(UPLOAD_RATE_LIMIT)
async def upload_files(bucket: str, request: Request, prefix: str = Form(""), files: list[UploadFile] = File(...)):
    user = get_current_user(request)
    if user["role"] != "admin":
        bp = getattr(request.state, "bucket_permission", None)
        if bp != "write":
            raise HTTPException(403, "Admin access required")
    eid = _current_endpoint_id()
    client = _s3_manager.get_client(eid)

    # Read all file contents first (async), then upload to S3 in parallel
    file_data = []
    for f in files:
        key = prefix + f.filename
        first_chunk = await f.read(MULTIPART_THRESHOLD + 1)
        if len(first_chunk) <= MULTIPART_THRESHOLD:
            file_data.append((key, first_chunk, len(first_chunk), False))
        else:
            remainder = await f.read()
            full_content = first_chunk + remainder
            file_data.append((key, full_content, len(full_content), True))

    def _put_one(key, body, size, is_large):
        if is_large:
            return key, _upload_multipart(bucket, key, io.BytesIO(body), endpoint_id=eid)
        client.put_object(Bucket=bucket, Key=key, Body=body)
        return key, size

    # Upload to S3 concurrently
    import concurrent.futures
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(file_data))) as pool:
        futures = [pool.submit(_put_one, key, body, size, is_large) for key, body, size, is_large in file_data]
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

@app.get("/api/list")
def list_objects_compat(prefix: str = "", user: dict = Depends(get_current_user)):
    return list_objects(_require_default_bucket(), prefix)

@app.get("/api/search")
def search_compat(q: str = Query(..., min_length=1), prefix: str = "", limit: int = 200, user: dict = Depends(get_current_user)):
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
    return folder_size(_require_default_bucket(), prefix)

@app.get("/api/storage-breakdown")
def storage_breakdown_compat(prefix: str = "", user: dict = Depends(get_current_user)):
    return storage_breakdown(_require_default_bucket(), prefix)

@app.get("/api/download")
def download_compat(key: str, user: dict = Depends(get_current_user)):
    return download_object(_require_default_bucket(), key)

@app.delete("/api/objects")
def delete_compat(req: DeleteRequest, user: dict = Depends(require_admin)):
    return delete_objects(_require_default_bucket(), req)

@app.post("/api/upload")
async def upload_compat(request: Request, prefix: str = Form(""), files: list[UploadFile] = File(...)):
    return await upload_files(_require_default_bucket(), request, prefix, files)

@app.get("/api/object-info")
def object_info_compat(key: str, user: dict = Depends(get_current_user)):
    return object_info(_require_default_bucket(), key)

@app.get("/api/object-versions")
def object_versions_compat(key: str, user: dict = Depends(get_current_user)):
    return object_versions(_require_default_bucket(), key)

@app.get("/api/presigned-url")
def presigned_url_compat(key: str, expires: int = 3600, user: dict = Depends(get_current_user)):
    return get_presigned_url(_require_default_bucket(), key, expires)

@app.get("/api/bucket-versioning")
def versioning_compat(user: dict = Depends(get_current_user)):
    return get_versioning(_require_default_bucket())

@app.get("/api/bucket-lifecycle")
def lifecycle_compat(user: dict = Depends(get_current_user)):
    return get_lifecycle(_require_default_bucket())

@app.get("/api/bucket-cors")
def cors_compat(user: dict = Depends(get_current_user)):
    return get_cors(_require_default_bucket())

@app.get("/api/multipart-uploads")
def multipart_compat(user: dict = Depends(get_current_user)):
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
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

    @app.get("/{path:path}")
    def serve_spa(path: str):
        file_path = os.path.realpath(os.path.join(static_dir, path))
        if not file_path.startswith(os.path.realpath(static_dir)):
            raise HTTPException(403, "Forbidden")
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(static_dir, "index.html"))

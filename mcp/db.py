"""
Read-only SQLite connection manager for MCP server.

Connects directly to Sairo's per-bucket SQLite databases for fast analytical
queries. All connections are read-only — mutations go through the Sairo API.
"""

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

DB_DIR = os.environ.get("DB_DIR", "/data")

_pool_lock = threading.Lock()
_pool: dict[str, sqlite3.Connection] = {}

# Max connections to keep alive (one per bucket DB)
MAX_POOL_SIZE = 50


def _safe_name(name: str) -> str:
    """Sanitize a bucket or endpoint name for filesystem use."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name).replace("..", "")


def _resolve_db_path(bucket: str, endpoint_id: Optional[str] = None) -> str:
    """
    Resolve the SQLite database path for a bucket.
    Mirrors Sairo's _db_path() logic with path traversal protection.
    """
    if endpoint_id and endpoint_id != "default":
        safe_eid = _safe_name(endpoint_id)
        safe_bucket = _safe_name(bucket)
        filename = f"{safe_eid}_{safe_bucket}.db"
    else:
        safe_bucket = _safe_name(bucket)
        filename = f"{safe_bucket}.db"

    db_path = os.path.join(DB_DIR, filename)
    real_path = os.path.realpath(db_path)
    real_db_dir = os.path.realpath(DB_DIR)

    if not real_path.startswith(real_db_dir + os.sep) and real_path != real_db_dir:
        raise ValueError(f"Path traversal detected: {filename}")

    return real_path


def _get_connection(db_path: str) -> sqlite3.Connection:
    """Get or create a read-only connection from the pool."""
    with _pool_lock:
        if db_path in _pool:
            return _pool[db_path]

        if len(_pool) >= MAX_POOL_SIZE:
            oldest_key = next(iter(_pool))
            try:
                _pool[oldest_key].close()
            except Exception:
                pass
            del _pool[oldest_key]

        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA cache_size = -8000")  # 8MB cache
        _pool[db_path] = conn
        return conn


@contextmanager
def get_db(bucket: str, endpoint_id: Optional[str] = None):
    """
    Get a read-only SQLite connection for a bucket's index database.

    Usage:
        with get_db("my-bucket") as db:
            rows = db.execute("SELECT * FROM objects LIMIT 10").fetchall()
    """
    db_path = _resolve_db_path(bucket, endpoint_id)

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Index not found for bucket: {bucket}")

    conn = _get_connection(db_path)
    try:
        yield conn
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            raise TimeoutError(
                f"Database locked for bucket {bucket}. Crawl may be in progress."
            ) from e
        raise


def get_users_db_path() -> str:
    """Return the path to the users database."""
    return os.path.join(DB_DIR, "users.db")


@contextmanager
def get_users_db():
    """Get a read-only connection to the users database."""
    db_path = get_users_db_path()
    if not os.path.exists(db_path):
        raise FileNotFoundError("Users database not found")

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        yield conn
    finally:
        conn.close()


def list_bucket_dbs() -> list[dict]:
    """
    List all bucket database files in DB_DIR.
    Returns [{bucket, endpoint_id, path, size_bytes}]
    """
    results = []
    real_db_dir = os.path.realpath(DB_DIR)

    if not os.path.isdir(real_db_dir):
        return results

    for filename in os.listdir(real_db_dir):
        if not filename.endswith(".db"):
            continue
        if filename == "users.db":
            continue

        filepath = os.path.join(real_db_dir, filename)
        name = filename[:-3]  # strip .db

        if "_" in name:
            parts = name.split("_", 1)
            endpoint_id = parts[0]
            bucket = parts[1]
        else:
            endpoint_id = "default"
            bucket = name

        try:
            size = os.path.getsize(filepath)
        except OSError:
            size = 0

        results.append({
            "bucket": bucket,
            "endpoint_id": endpoint_id,
            "path": filepath,
            "size_bytes": size,
        })

    return results


def check_table_exists(db: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the database."""
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return row is not None


def close_all():
    """Close all pooled connections. Call on shutdown."""
    with _pool_lock:
        for conn in _pool.values():
            try:
                conn.close()
            except Exception:
                pass
        _pool.clear()

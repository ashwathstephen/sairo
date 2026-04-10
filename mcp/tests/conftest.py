"""
Test fixtures for MCP server tests.

Creates in-memory SQLite databases with realistic test data,
mock Sairo API client, and FastMCP test client.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Set test environment before imports
_test_dir = tempfile.mkdtemp(prefix="sairo-mcp-test-")
os.environ["DB_DIR"] = _test_dir
os.environ["SAIRO_API_URL"] = "http://localhost:9999"
os.environ["SAIRO_API_TOKEN"] = "sairo_test_token_abc123"
os.environ["MCP_LOG_FORMAT"] = "text"
os.environ["MCP_LOG_LEVEL"] = "WARNING"

from auth import AuthManager, UserSession
from db import DB_DIR
from sairo_client import SairoClient


@pytest.fixture(scope="session")
def test_db_dir():
    """Return the test database directory."""
    return _test_dir


def _create_test_bucket_db(db_dir: str, bucket: str, num_objects: int = 100):
    """Create a test bucket database with realistic data."""
    db_path = os.path.join(db_dir, f"{bucket}.db")
    conn = sqlite3.connect(db_path)

    # Create tables matching Sairo's schema
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS objects (
            key TEXT PRIMARY KEY,
            size INTEGER,
            last_modified TEXT,
            etag TEXT,
            prefix TEXT,
            depth INTEGER,
            crawl_gen INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_prefix ON objects(prefix);
        CREATE INDEX IF NOT EXISTS idx_depth ON objects(depth);

        CREATE TABLE IF NOT EXISTS crawl_status (
            id INTEGER PRIMARY KEY DEFAULT 1,
            last_crawl_start TEXT,
            last_crawl_end TEXT,
            total_objects INTEGER,
            total_size INTEGER,
            status TEXT DEFAULT 'complete',
            current_crawl_gen INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS folder_stats (
            prefix TEXT PRIMARY KEY,
            object_count INTEGER,
            total_size INTEGER,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS prefix_children (
            parent_prefix TEXT,
            child_prefix TEXT,
            child_name TEXT,
            object_count INTEGER,
            total_size INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_pc_parent ON prefix_children(parent_prefix);

        CREATE TABLE IF NOT EXISTS storage_history (
            timestamp TEXT,
            prefix TEXT,
            object_count INTEGER,
            total_size INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_sh_ts ON storage_history(timestamp);

        PRAGMA journal_mode=WAL;
    """)

    # Insert test objects
    now = datetime.now(timezone.utc)
    total_size = 0
    prefixes = ["data/", "logs/", "backups/", "config/"]
    extensions = [".parquet", ".csv", ".json", ".log", ".txt"]

    for i in range(num_objects):
        prefix = prefixes[i % len(prefixes)]
        ext = extensions[i % len(extensions)]
        age_days = i % 400  # Spread ages across a year+
        size = (i + 1) * 10240  # 10KB * index
        modified = (now - timedelta(days=age_days)).isoformat()

        key = f"{prefix}file_{i:04d}{ext}"
        depth = key.count("/")
        etag = f'"etag_{i:08x}"'

        # Create some duplicates (same etag/size for testing)
        if i > 0 and i % 20 == 0:
            etag = f'"etag_{i-1:08x}"'
            size = i * 10240

        conn.execute(
            "INSERT OR REPLACE INTO objects (key, size, last_modified, etag, prefix, depth, crawl_gen) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (key, size, modified, etag, prefix, depth),
        )
        total_size += size

    # Insert crawl status
    conn.execute(
        "INSERT OR REPLACE INTO crawl_status "
        "(id, last_crawl_start, last_crawl_end, total_objects, total_size, status) "
        "VALUES (1, ?, ?, ?, ?, 'complete')",
        (now.isoformat(), now.isoformat(), num_objects, total_size),
    )

    # Insert folder stats
    for prefix in prefixes:
        count = num_objects // len(prefixes)
        size = total_size // len(prefixes)
        conn.execute(
            "INSERT OR REPLACE INTO folder_stats (prefix, object_count, total_size, last_updated) "
            "VALUES (?, ?, ?, ?)",
            (prefix, count, size, now.isoformat()),
        )

    # Insert prefix_children
    for prefix in prefixes:
        name = prefix.rstrip("/")
        count = num_objects // len(prefixes)
        size = total_size // len(prefixes)
        conn.execute(
            "INSERT INTO prefix_children (parent_prefix, child_prefix, child_name, object_count, total_size) "
            "VALUES ('', ?, ?, ?, ?)",
            (prefix, name, count, size),
        )

    # Insert storage history (last 30 days)
    for days_back in range(30):
        ts = (now - timedelta(days=days_back)).isoformat()
        hist_size = total_size - (days_back * total_size // 100)  # ~1% daily growth
        hist_objects = num_objects - days_back
        conn.execute(
            "INSERT INTO storage_history (timestamp, prefix, object_count, total_size) "
            "VALUES (?, '', ?, ?)",
            (ts, hist_objects, hist_size),
        )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture(scope="session")
def test_buckets(test_db_dir):
    """Create test bucket databases and return their names."""
    buckets = {
        "test-bucket": 100,
        "large-bucket": 500,
        "empty-bucket": 0,
    }

    for name, count in buckets.items():
        _create_test_bucket_db(test_db_dir, name, count)

    # Create users.db
    users_path = os.path.join(test_db_dir, "users.db")
    conn = sqlite3.connect(users_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT,
            role TEXT DEFAULT 'viewer'
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            username TEXT,
            action TEXT,
            bucket TEXT,
            details TEXT
        );
        CREATE TABLE IF NOT EXISTS bucket_permissions (
            username TEXT,
            bucket TEXT,
            permission TEXT,
            PRIMARY KEY (username, bucket)
        );
    """)
    conn.execute(
        "INSERT OR REPLACE INTO users (username, role) VALUES ('admin', 'admin')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO users (username, role) VALUES ('viewer', 'viewer')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO bucket_permissions VALUES ('viewer', 'test-bucket', 'read')"
    )
    conn.commit()
    conn.close()

    return list(buckets.keys())


@pytest.fixture
def admin_session():
    """Create an admin UserSession for testing."""
    return UserSession(
        username="admin",
        role="admin",
        token="sairo_test_admin_token",
        bucket_permissions={},
    )


@pytest.fixture
def viewer_session():
    """Create a viewer UserSession with limited permissions."""
    return UserSession(
        username="viewer",
        role="viewer",
        token="sairo_test_viewer_token",
        bucket_permissions={"test-bucket": "read"},
    )


@pytest.fixture
def mock_sairo():
    """Create a mock SairoClient."""
    client = AsyncMock(spec=SairoClient)
    client.health_check.return_value = True
    client.validate_token.return_value = {"username": "admin", "role": "admin"}
    client.get_user_permissions.return_value = {}
    client.preview_object.return_value = {
        "content": "line1\nline2\nline3\n",
        "truncated": False,
        "total_size": 30,
    }
    client.preview_tail.return_value = {
        "content": "last line\n",
        "total_size": 1000,
    }
    client.get_file_metadata.return_value = {
        "format": "parquet",
        "columns": [
            {"name": "id", "type": "INT64"},
            {"name": "name", "type": "STRING"},
        ],
        "row_count": 10000,
        "compression": "snappy",
    }
    client.trigger_crawl.return_value = {"status": "started"}
    client.get_audit_log.return_value = [
        {
            "timestamp": "2026-04-05T10:00:00",
            "username": "admin",
            "action": "upload",
            "bucket": "test-bucket",
            "details": "uploaded file.csv",
        }
    ]
    return client


@pytest.fixture
def mock_auth(admin_session):
    """Create a mock AuthManager that returns the admin session."""
    auth = MagicMock(spec=AuthManager)
    auth.authenticate = AsyncMock(return_value=admin_session)
    auth.require_bucket_read = MagicMock()  # Does nothing (allows access)
    auth.require_bucket_write = MagicMock()
    auth.require_admin = MagicMock()
    return auth

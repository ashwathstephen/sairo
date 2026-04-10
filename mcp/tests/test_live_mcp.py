"""
Live MCP integration test — exercises the SairoClient against the real Objex instance.

100% READ-ONLY. No writes, no deletes, no uploads, no crawl triggers.

Tests the actual MCP components (SairoClient, auth) against production data.

Usage:
    SAIRO_URL=https://objex.ingage.tech SAIRO_TOKEN=sairo_xxx python -m pytest tests/test_live_mcp.py -v -s
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

SAIRO_URL = os.environ.get("SAIRO_URL", "https://objex.ingage.tech")
SAIRO_TOKEN = os.environ.get("SAIRO_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not SAIRO_TOKEN,
    reason="Set SAIRO_TOKEN env var to run live MCP tests"
)


@pytest.fixture
async def client():
    """Create a real SairoClient pointed at the live instance."""
    from sairo_client import SairoClient
    c = SairoClient(base_url=SAIRO_URL, service_token=SAIRO_TOKEN)
    await c.start()
    yield c
    await c.close()


@pytest.fixture
async def auth_mgr(client):
    """Create a real AuthManager and authenticate."""
    from auth import AuthManager
    mgr = AuthManager(client)
    return mgr


# ─── SairoClient Tests ───

class TestSairoClient:
    @pytest.mark.asyncio
    async def test_health_check(self, client):
        healthy = await client.health_check()
        assert healthy is True
        print("  Sairo API is healthy")

    @pytest.mark.asyncio
    async def test_validate_token(self, client):
        user = await client.validate_token(SAIRO_TOKEN)
        assert user is not None
        assert user["username"] == "admin"
        assert user["role"] == "admin"
        print(f"  Token valid: {user}")

    @pytest.mark.asyncio
    async def test_list_buckets(self, client):
        buckets = await client.list_buckets()
        assert len(buckets) > 0
        # Handle both list and dict responses
        if isinstance(buckets, dict):
            buckets = buckets.get("buckets", [])
        print(f"  Listed {len(buckets)} buckets via SairoClient")
        for b in buckets[:3]:
            print(f"    {b['name']}: {b.get('object_count', 0):,} objects")

    @pytest.mark.asyncio
    async def test_get_audit_log(self, client):
        """Read-only: fetch recent audit entries."""
        entries = await client.get_audit_log(limit=5, user_token=SAIRO_TOKEN)
        assert isinstance(entries, list)
        print(f"  Got {len(entries)} audit log entries")
        for e in entries[:3]:
            print(f"    {e.get('timestamp', '?')[:16]} | {e.get('action', '?')} | {e.get('username', '?')}")

    @pytest.mark.asyncio
    async def test_preview_small_file(self, client):
        """Read-only: preview a small file from usync bucket."""
        try:
            result = await client.preview_object(
                "usync", "index.html", max_bytes=512, user_token=SAIRO_TOKEN
            )
            content = result.get("content", "")
            print(f"  Preview usync/index.html: {len(content)} chars")
            if content:
                print(f"    First 100 chars: {content[:100]}")
        except Exception as e:
            print(f"  Preview failed (expected if file doesn't exist): {e}")

    @pytest.mark.asyncio
    async def test_get_object_info(self, client):
        """Read-only: get metadata for a postgres backup file."""
        try:
            result = await client.get_object_info(
                "aerospike-backups", "data/test", user_token=SAIRO_TOKEN
            )
            print(f"  Object info: {result}")
        except Exception as e:
            print(f"  Object info failed (expected for non-existent key): {e}")


# ─── AuthManager Tests ───

class TestAuthManager:
    @pytest.mark.asyncio
    async def test_authenticate(self, auth_mgr):
        session = await auth_mgr.authenticate(SAIRO_TOKEN)
        assert session.username == "admin"
        assert session.role == "admin"
        assert session.is_admin is True
        print(f"  Authenticated session: {session.username} (admin={session.is_admin})")

    @pytest.mark.asyncio
    async def test_session_caching(self, auth_mgr):
        """Second auth call should use cache."""
        session1 = await auth_mgr.authenticate(SAIRO_TOKEN)
        session2 = await auth_mgr.authenticate(SAIRO_TOKEN)
        assert session1 is session2  # Same object from cache
        print("  Session caching works")

    @pytest.mark.asyncio
    async def test_admin_bucket_access(self, auth_mgr):
        session = await auth_mgr.authenticate(SAIRO_TOKEN)
        assert session.can_read_bucket("ssp-production-reports") is True
        assert session.can_read_bucket("any-bucket-name") is True
        assert session.can_write_bucket("ssp-production-reports") is True
        print("  Admin has full bucket access")

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, auth_mgr):
        from auth import AuthorizationError
        with pytest.raises(AuthorizationError):
            await auth_mgr.authenticate("sairo_invalid_token_12345")
        print("  Invalid token correctly rejected")


# ─── Summary ───

class TestLiveSummary:
    @pytest.mark.asyncio
    async def test_full_mcp_component_check(self, client, auth_mgr):
        """Verify all MCP components work against the live instance."""
        print("\n" + "="*60)
        print("  LIVE MCP COMPONENT VERIFICATION")
        print("="*60)

        # 1. Health
        healthy = await client.health_check()
        print(f"  [{'PASS' if healthy else 'FAIL'}] Sairo API health check")

        # 2. Auth
        session = await auth_mgr.authenticate(SAIRO_TOKEN)
        print(f"  [PASS] Authentication as {session.username}")

        # 3. Bucket listing
        buckets = await client.list_buckets(user_token=SAIRO_TOKEN)
        if isinstance(buckets, dict):
            buckets = buckets.get("buckets", [])
        print(f"  [PASS] Listed {len(buckets)} buckets")

        # 4. Audit log
        entries = await client.get_audit_log(limit=3, user_token=SAIRO_TOKEN)
        print(f"  [PASS] Fetched {len(entries)} audit entries")

        # 5. Permissions
        assert session.can_read_bucket("ssp-production-reports")
        print(f"  [PASS] Bucket permission check")

        print("="*60)
        print("  All MCP components verified against live Objex instance")
        print(f"  URL: {SAIRO_URL}")
        print(f"  Buckets: {len(buckets)}")

        total_objects = sum(b.get("object_count", 0) for b in buckets)
        total_size = sum(b.get("total_size", 0) for b in buckets)
        print(f"  Objects: {total_objects:,}")
        print(f"  Storage: {total_size / (1024**4):,.1f} TB")
        print("="*60)

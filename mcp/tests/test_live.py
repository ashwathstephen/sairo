"""
Live integration test against a real Sairo/Objex instance.

THIS TEST IS 100% READ-ONLY. It will NEVER:
- Delete any objects or buckets
- Upload anything
- Modify any configurations
- Trigger crawls or any write operations

It only calls GET endpoints to verify the MCP server can talk to a real Sairo instance.

Usage:
    SAIRO_URL=https://objex.ingage.tech SAIRO_TOKEN=sairo_xxx python -m pytest tests/test_live.py -v -s
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

SAIRO_URL = os.environ.get("SAIRO_URL", "https://objex.ingage.tech")
SAIRO_TOKEN = os.environ.get("SAIRO_TOKEN", "")

HEADERS = {
    "Authorization": f"Bearer {SAIRO_TOKEN}",
    "Accept": "application/json",
}


def api(path: str, params: dict = None) -> httpx.Response:
    """Make a read-only GET request to the Sairo API."""
    resp = httpx.get(f"{SAIRO_URL}{path}", headers=HEADERS, params=params, timeout=30)
    return resp


# Skip all tests if no token provided
pytestmark = pytest.mark.skipif(
    not SAIRO_TOKEN,
    reason="Set SAIRO_TOKEN env var to run live tests"
)


# ─── Auth & Connectivity ───

class TestConnectivity:
    def test_health(self):
        resp = httpx.get(f"{SAIRO_URL}/healthz", timeout=10)
        assert resp.status_code == 200
        print(f"  Health: {resp.json()}")

    def test_auth_me(self):
        resp = api("/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert "username" in data
        assert "role" in data
        print(f"  Authenticated as: {data['username']} (role={data['role']})")


# ─── Bucket Listing ───

class TestBuckets:
    def test_list_buckets(self):
        resp = api("/api/buckets")
        assert resp.status_code == 200
        data = resp.json()
        buckets = data.get("buckets", data) if isinstance(data, dict) else data
        assert len(buckets) > 0
        print(f"\n  Found {len(buckets)} buckets:")
        for b in buckets:
            name = b["name"]
            count = b.get("object_count", 0)
            size = b.get("total_size", 0)
            status = b.get("index_status", "?")
            size_gb = size / (1024**3)
            print(f"    {name}: {count:,} objects, {size_gb:,.1f} GB [{status}]")

    def test_bucket_crawl_status(self):
        """Check crawl status on a known bucket."""
        # Use a small bucket for speed
        resp = api("/api/buckets/aerospike-backups/crawl-status")
        if resp.status_code == 200:
            data = resp.json()
            print(f"  Crawl status: {data.get('status')}, objects: {data.get('total_objects')}")
        else:
            print(f"  Crawl status endpoint returned: {resp.status_code}")


# ─── Object Listing (read-only) ───

class TestObjectListing:
    def test_list_objects_small_bucket(self):
        """List objects in a small bucket."""
        resp = api("/api/buckets/aerospike-backups/list", {"prefix": ""})
        assert resp.status_code == 200
        # Response is NDJSON stream
        text = resp.text.strip()
        lines = [l for l in text.split("\n") if l.strip()]
        print(f"  aerospike-backups: got {len(lines)} objects from listing")
        if lines:
            import json
            first = json.loads(lines[0])
            print(f"  First object: {first.get('key', '?')} ({first.get('size', 0):,} bytes)")

    def test_list_objects_large_bucket_prefix(self):
        """List objects in ssp-production-reports with a prefix (read-only, limited)."""
        resp = api("/api/buckets/ssp-production-reports/list", {"prefix": "", "limit": "5"})
        assert resp.status_code == 200
        text = resp.text.strip()
        lines = [l for l in text.split("\n") if l.strip()]
        print(f"  ssp-production-reports: got {len(lines)} objects (limited)")

    def test_search_objects(self):
        """Search for objects by name (read-only)."""
        resp = api("/api/buckets/aerospike-backups/search", {"q": "backup"})
        if resp.status_code == 200:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else []
            count = len(data) if isinstance(data, list) else data.get("results", [])
            print(f"  Search 'backup' in aerospike-backups: {count} results")
        else:
            print(f"  Search returned: {resp.status_code} (index may not be ready)")


# ─── Storage Analytics (read-only) ───

class TestAnalytics:
    def test_storage_breakdown(self):
        """Get storage breakdown for a real bucket."""
        resp = api("/api/buckets/ds-mletl-data/storage-breakdown")
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                print(f"  ds-mletl-data breakdown: {len(data)} top-level prefixes")
                for item in data[:5]:
                    prefix = item.get("prefix", "?")
                    size_gb = item.get("total_size", 0) / (1024**3)
                    print(f"    {prefix}: {size_gb:,.1f} GB")
            else:
                print(f"  Breakdown: {data}")
        else:
            print(f"  Breakdown returned: {resp.status_code}")

    def test_storage_history(self):
        """Get storage history for trend analysis."""
        resp = api("/api/buckets/ssp-production-reports/storage-history", {"days": "30"})
        if resp.status_code == 200:
            data = resp.json()
            entries = data if isinstance(data, list) else data.get("history", [])
            print(f"  ssp-production-reports history: {len(entries)} data points over 30 days")
            if entries:
                latest = entries[-1] if isinstance(entries[-1], dict) else {}
                size_tb = latest.get("total_size", 0) / (1024**4)
                print(f"  Latest snapshot: {size_tb:,.1f} TB")
        else:
            print(f"  History returned: {resp.status_code}")

    def test_folder_size(self):
        """Get folder size for a specific prefix."""
        resp = api("/api/buckets/ds-mletl-data/folder-size", {"prefix": ""})
        if resp.status_code == 200:
            data = resp.json()
            print(f"  ds-mletl-data root size: {data}")
        else:
            print(f"  Folder size returned: {resp.status_code}")


# ─── File Preview (read-only) ───

class TestPreview:
    def test_preview_small_file(self):
        """Preview a small text file (read-only, limited bytes)."""
        # First find a small file in usync bucket
        resp = api("/api/buckets/usync/list", {"prefix": ""})
        if resp.status_code != 200:
            pytest.skip("Cannot list usync bucket")

        text = resp.text.strip()
        lines = [l for l in text.split("\n") if l.strip()]
        if not lines:
            pytest.skip("No objects in usync")

        import json
        first = json.loads(lines[0])
        key = first.get("key", "")
        if not key:
            pytest.skip("No key found")

        # Preview first 1KB only
        resp = api("/api/buckets/usync/preview", {"key": key, "max_bytes": "1024"})
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content", "")
            print(f"  Preview of '{key}': {len(content)} chars")
            print(f"  First 200 chars: {content[:200]}")
        else:
            print(f"  Preview returned: {resp.status_code}")

    def test_object_info(self):
        """Get metadata for an object (read-only)."""
        resp = api("/api/buckets/postgres-backup/list", {"prefix": ""})
        if resp.status_code != 200:
            pytest.skip("Cannot list bucket")

        text = resp.text.strip()
        lines = [l for l in text.split("\n") if l.strip()]
        if not lines:
            pytest.skip("No objects")

        import json
        first = json.loads(lines[0])
        key = first.get("key", "")

        resp = api("/api/buckets/postgres-backup/object-info", {"key": key})
        if resp.status_code == 200:
            data = resp.json()
            size_gb = data.get("size", 0) / (1024**3)
            print(f"  Object info for '{key}': {size_gb:,.1f} GB, modified: {data.get('last_modified', '?')}")
        else:
            print(f"  Object info returned: {resp.status_code}")


# ─── Audit Log (read-only) ───

class TestAudit:
    def test_audit_log(self):
        """Read recent audit log entries (read-only)."""
        resp = api("/api/audit-log", {"limit": "10"})
        if resp.status_code == 200:
            data = resp.json()
            entries = data if isinstance(data, list) else data.get("entries", [])
            print(f"  Audit log: {len(entries)} recent entries")
            for e in entries[:5]:
                ts = e.get("timestamp", "?")[:16]
                user = e.get("username", "?")
                action = e.get("action", "?")
                bucket = e.get("bucket", "—")
                print(f"    {ts} | {user} | {action} | {bucket}")
        else:
            print(f"  Audit log returned: {resp.status_code}")


# ─── Health & Branding (read-only) ───

class TestHealth:
    def test_branding(self):
        """Check public branding info."""
        resp = httpx.get(f"{SAIRO_URL}/api/branding", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  Branding: name={data.get('app_name')}, color={data.get('primary_color')}")

    def test_health_detail(self):
        """Check detailed health."""
        resp = api("/api/health-detail")
        if resp.status_code == 200:
            data = resp.json()
            print(f"  Health detail: {data}")
        else:
            print(f"  Health detail: {resp.status_code}")


# ─── Summary ───

class TestSummary:
    def test_full_inventory(self):
        """Print a complete inventory of the storage infrastructure."""
        resp = api("/api/buckets")
        assert resp.status_code == 200
        data = resp.json()
        buckets = data.get("buckets", data)

        total_objects = sum(b.get("object_count", 0) for b in buckets)
        total_size = sum(b.get("total_size", 0) for b in buckets)

        print(f"\n{'='*60}")
        print(f"  OBJEX STORAGE INFRASTRUCTURE SUMMARY")
        print(f"{'='*60}")
        print(f"  Buckets:      {len(buckets)}")
        print(f"  Total objects: {total_objects:,}")
        print(f"  Total size:    {total_size / (1024**4):,.1f} TB")
        print(f"{'='*60}")
        print(f"  {'Bucket':<30} {'Objects':>12} {'Size':>12} {'Status':<10}")
        print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*10}")
        for b in sorted(buckets, key=lambda x: x.get("total_size", 0), reverse=True):
            name = b["name"][:30]
            count = f"{b.get('object_count', 0):,}"
            size_val = b.get("total_size", 0)
            if size_val >= 1024**4:
                size = f"{size_val / (1024**4):,.1f} TB"
            elif size_val >= 1024**3:
                size = f"{size_val / (1024**3):,.1f} GB"
            else:
                size = f"{size_val / (1024**2):,.1f} MB"
            status = b.get("index_status", "?")
            print(f"  {name:<30} {count:>12} {size:>12} {status:<10}")
        print(f"{'='*60}")

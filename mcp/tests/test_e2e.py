"""
End-to-end tests: run tools through the actual MCP server using in-memory transport.

Full path: MCP Client → In-Memory Transport → FastMCP Server → Lifespan → Tool → SQLite DB → Response
"""

import os
import sys
import tempfile

# Set test environment BEFORE any imports
_test_dir = tempfile.mkdtemp(prefix="sairo-mcp-e2e-")
os.environ["DB_DIR"] = _test_dir
os.environ["SAIRO_API_URL"] = "http://localhost:9999"
os.environ["SAIRO_API_TOKEN"] = ""
os.environ["MCP_LOG_LEVEL"] = "WARNING"
os.environ["MCP_LOG_FORMAT"] = "text"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Create test databases
from tests.conftest import _create_test_bucket_db
_create_test_bucket_db(_test_dir, "test-bucket", num_objects=200)
_create_test_bucket_db(_test_dir, "analytics-bucket", num_objects=500)

# Patch db.DB_DIR BEFORE importing server
import db as db_module
db_module.DB_DIR = _test_dir

# Now import server
import server as srv
from mcp.shared.memory import create_connected_server_and_client_session

import pytest


async def _call(tool_name: str, args: dict = {}) -> str:
    """Helper: create a fresh MCP session and call a single tool."""
    async with create_connected_server_and_client_session(
        srv.mcp, raise_exceptions=True,
    ) as client:
        result = await client.call_tool(tool_name, args)
        return result.content[0].text


async def _read_resource(uri: str) -> str:
    """Helper: create a fresh MCP session and read a resource."""
    async with create_connected_server_and_client_session(
        srv.mcp, raise_exceptions=True,
    ) as client:
        result = await client.read_resource(uri)
        return result.contents[0].text


async def _get_prompt(name: str, args: dict) -> str:
    """Helper: create a fresh MCP session and get a prompt."""
    async with create_connected_server_and_client_session(
        srv.mcp, raise_exceptions=True,
    ) as client:
        result = await client.get_prompt(name, args)
        return result.messages[0].content.text


# ─── Discovery Tools ───


@pytest.mark.asyncio
async def test_list_buckets():
    text = await _call("list_buckets")
    assert "test-bucket" in text
    assert "analytics-bucket" in text
    print(f"\n{text[:600]}")


@pytest.mark.asyncio
async def test_list_objects():
    text = await _call("list_objects", {"bucket": "test-bucket", "limit": 10})
    assert "test-bucket" in text
    assert "file_" in text


@pytest.mark.asyncio
async def test_list_objects_sorted():
    text = await _call("list_objects", {
        "bucket": "test-bucket", "sort_by": "size", "order": "desc", "limit": 5,
    })
    assert "test-bucket" in text


@pytest.mark.asyncio
async def test_list_folders():
    text = await _call("list_folders", {"bucket": "test-bucket"})
    assert "objects" in text.lower() or "folder" in text.lower()
    print(f"\n{text[:600]}")


@pytest.mark.asyncio
async def test_search_objects():
    text = await _call("search_objects", {"bucket": "test-bucket", "query": "file_00"})
    assert "file_00" in text


@pytest.mark.asyncio
async def test_search_no_results():
    text = await _call("search_objects", {"bucket": "test-bucket", "query": "zzz_nonexistent"})
    assert "no" in text.lower()


# ─── Analytics Tools ───


@pytest.mark.asyncio
async def test_storage_breakdown():
    text = await _call("get_storage_breakdown", {"bucket": "analytics-bucket"})
    assert "analytics-bucket" in text
    assert "%" in text
    print(f"\n{text[:600]}")


@pytest.mark.asyncio
async def test_storage_trends():
    text = await _call("get_storage_trends", {"bucket": "analytics-bucket", "days": 30})
    assert "trend" in text.lower() or "day" in text.lower()


@pytest.mark.asyncio
async def test_file_type_distribution():
    text = await _call("get_file_type_distribution", {"bucket": "analytics-bucket"})
    assert "%" in text


@pytest.mark.asyncio
async def test_size_distribution():
    text = await _call("get_size_distribution", {"bucket": "analytics-bucket"})
    assert "KB" in text or "MB" in text


@pytest.mark.asyncio
async def test_age_distribution():
    text = await _call("get_age_distribution", {"bucket": "analytics-bucket"})
    assert "day" in text.lower() or "month" in text.lower()


@pytest.mark.asyncio
async def test_top_objects():
    text = await _call("get_top_objects", {"bucket": "analytics-bucket", "limit": 5})
    assert "1." in text
    assert "file_" in text


@pytest.mark.asyncio
async def test_find_duplicates():
    text = await _call("find_duplicates", {"bucket": "analytics-bucket", "min_size": 1024})
    assert "duplicate" in text.lower() or "clean" in text.lower()


# ─── Cost Tools ───


@pytest.mark.asyncio
async def test_estimate_cost_aws():
    text = await _call("estimate_storage_cost", {"bucket": "analytics-bucket", "provider": "aws"})
    assert "$" in text
    assert "standard" in text.lower()
    print(f"\n{text[:600]}")


@pytest.mark.asyncio
async def test_estimate_cost_r2():
    text = await _call("estimate_storage_cost", {"bucket": "analytics-bucket", "provider": "r2"})
    assert "$" in text


@pytest.mark.asyncio
async def test_suggest_lifecycle():
    text = await _call("suggest_lifecycle_rules", {"bucket": "analytics-bucket", "provider": "aws"})
    assert "analytics-bucket" in text


@pytest.mark.asyncio
async def test_find_cold_data():
    text = await _call("find_cold_data", {
        "bucket": "analytics-bucket", "older_than_days": 30, "min_size": 1024,
    })
    assert "cold" in text.lower() or "file" in text.lower() or "day" in text.lower()


# ─── Pipeline Tools ───


@pytest.mark.asyncio
async def test_analyze_prefix_structure():
    text = await _call("analyze_prefix_structure", {"bucket": "analytics-bucket"})
    assert "structure" in text.lower() or "depth" in text.lower()
    print(f"\n{text[:600]}")


@pytest.mark.asyncio
async def test_detect_freshness():
    text = await _call("detect_data_freshness", {"bucket": "analytics-bucket"})
    assert "freshness" in text.lower() or "ago" in text.lower() or "hours" in text.lower()


@pytest.mark.asyncio
async def test_compare_snapshots():
    text = await _call("compare_snapshots", {"bucket": "analytics-bucket", "days_ago": 7})
    assert "change" in text.lower() or "analytics-bucket" in text


# ─── Operations ───


@pytest.mark.asyncio
async def test_crawl_status():
    text = await _call("get_crawl_status", {"bucket": "analytics-bucket"})
    assert "analytics-bucket" in text
    assert "status" in text.lower() or "indexed" in text.lower()


# ─── Input Validation ───


@pytest.mark.asyncio
async def test_invalid_bucket_rejected():
    text = await _call("list_objects", {"bucket": "../../../etc/passwd"})
    assert "invalid" in text.lower() or "error" in text.lower()


@pytest.mark.asyncio
async def test_nonexistent_bucket():
    text = await _call("get_storage_breakdown", {"bucket": "does-not-exist-bucket"})
    assert "not found" in text.lower() or "error" in text.lower()


@pytest.mark.asyncio
async def test_short_search_rejected():
    text = await _call("search_objects", {"bucket": "test-bucket", "query": "a"})
    assert "2 character" in text.lower() or "error" in text.lower() or "least" in text.lower()


# ─── Resources ───


@pytest.mark.asyncio
async def test_overview_resource():
    text = await _read_resource("objex://overview")
    assert "bucket" in text.lower()
    print(f"\n{text[:600]}")


@pytest.mark.asyncio
async def test_bucket_summary_resource():
    text = await _read_resource("objex://test-bucket/summary")
    assert "test-bucket" in text


# ─── Prompts ───


@pytest.mark.asyncio
async def test_storage_audit_prompt():
    text = await _get_prompt("storage-audit", {"bucket": "test-bucket"})
    assert "test-bucket" in text


@pytest.mark.asyncio
async def test_cost_optimization_prompt():
    text = await _get_prompt("cost-optimization", {"bucket": "test-bucket", "provider": "aws"})
    assert "test-bucket" in text

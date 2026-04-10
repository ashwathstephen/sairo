"""
Sairo MCP Server — AI-powered storage intelligence.

This is the entry point that wires everything together:
- FastMCP server with Streamable HTTP + stdio transports
- Lifespan for DB and API client initialization
- Server instructions that teach the AI how to answer storage questions
- All tool, resource, and prompt registrations

Run with:
    python server.py                        # Streamable HTTP (default)
    python server.py --transport stdio      # stdio for Claude Desktop / CLI
"""

import os
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from auth import AuthManager, UserSession
from db import close_all as close_db_pool
from observability import logger, metrics
from sairo_client import SairoClient

# --- Configuration ---

MCP_NAME = os.environ.get("MCP_NAME", "Sairo Storage Intelligence")
MCP_PORT = int(os.environ.get("MCP_PORT", "8100"))
MCP_HOST = os.environ.get("MCP_BIND_HOST", "127.0.0.1")

# --- Server Instructions ---

SERVER_INSTRUCTIONS = """
You are a storage intelligence assistant connected to Sairo, an S3-compatible
object storage browser. You have full analytical access to the user's storage
infrastructure including all buckets, objects, metadata, and historical trends.

## How to Answer Questions

The user will ask natural language questions about their storage. They do NOT
know tool names and should never need to. Your job is to pick the right tools,
chain them together, and synthesize clear answers.

### Common Question Patterns

**"What buckets do I have?" / "Show me my storage"**
→ Call `list_buckets` to get the full picture.

**"What's in [bucket]?" / "Show me [bucket]"**
→ Call `list_folders` first (instant, shows structure), then `list_objects` for specific folders.

**"Find [filename/pattern]" / "Where is [file]?"**
→ Call `search_objects` with the search query.

**"How big is [bucket/folder]?" / "Where is all the space going?"**
→ Call `get_storage_breakdown`. For deeper drill-downs, call it again with a specific prefix.

**"What's this file?" / "Show me [file]"**
→ For text/log/CSV/JSON: call `read_object_content` or `sample_csv_data`/`sample_json_data`.
→ For Parquet/ORC/Avro: call `get_file_schema`.
→ For metadata: call `get_object_metadata`.

**"How much is this costing?" / "What are my storage costs?"**
→ Call `estimate_storage_cost`. If the user doesn't specify a provider, ask or default to AWS.

**"How can I save money?" / "Optimize costs"**
→ Chain: `estimate_storage_cost` → `find_cold_data` → `find_duplicates` → `suggest_lifecycle_rules`.

**"What changed recently?" / "Why did storage grow?"**
→ Call `compare_snapshots` and `get_storage_trends`. If investigating, add `get_audit_log`.

**"Is my data pipeline healthy?" / "Is this still being updated?"**
→ Call `detect_data_freshness` to check which folders are active vs stale.

**"Are there any duplicates?" / "Find wasted space"**
→ Call `find_duplicates`. Also consider `get_age_distribution` for archival candidates.

**"Tell me everything about [bucket]"**
→ Chain: `list_folders` → `get_storage_breakdown` → `get_file_type_distribution` → `get_age_distribution`.

**"Check the index" / "Is search up to date?"**
→ Call `get_crawl_status`. If outdated, offer to `trigger_crawl`.

### Important Behaviors

1. **Start broad, then drill down.** For unfamiliar buckets, start with `list_folders` or
   `get_storage_breakdown` to understand the structure before diving into specific files.

2. **Always show human-readable sizes.** Say "163.7 TB" not "163700000000000 bytes".

3. **Proactively suggest next steps.** If you show storage breakdown, mention that you can
   drill deeper into the biggest folder. If you find duplicates, mention potential savings.

4. **Combine tools for complete answers.** Most real questions need 2-3 tool calls.
   "How much is this bucket costing and can we optimize it?" needs cost estimation,
   cold data analysis, and lifecycle suggestions.

5. **Be honest about data freshness.** If the index was last updated 3 days ago,
   mention that. If data seems stale, suggest triggering a re-index.

6. **Format responses for readability.** Use markdown tables for comparisons,
   bullet points for lists, and bold for key numbers.

7. **Don't overwhelm with raw data.** Summarize first, then offer to show details.
   "Your bucket has 533K objects across 12 folders. The largest is data/ at 140TB.
   Want me to break that down further?"

### What You Cannot Do

- You cannot modify, delete, or upload objects (read-only access)
- You cannot change bucket configurations
- You cannot create or delete buckets
- You can only trigger re-indexing (with write permission)
- Cost estimates are approximations based on public pricing
"""


# --- Helpers to get auth/sairo from context ---

def get_session(ctx) -> UserSession:
    """Get the authenticated user session from the tool context."""
    lc = ctx.request_context.lifespan_context
    return lc["session"]


def get_auth(ctx) -> AuthManager:
    """Get the auth manager from the tool context."""
    lc = ctx.request_context.lifespan_context
    return lc["auth"]


def get_sairo(ctx) -> SairoClient:
    """Get the Sairo API client from the tool context."""
    lc = ctx.request_context.lifespan_context
    return lc["sairo"]


# --- Lifespan ---

@asynccontextmanager
async def lifespan(server: FastMCP):
    """
    Initialize shared resources on startup, clean up on shutdown.
    Authenticates using the SAIRO_API_TOKEN env var once at startup.
    """
    sairo = SairoClient()
    await sairo.start()

    auth_manager = AuthManager(sairo)

    logger.info("Sairo MCP server starting", extra={"tool": "server"})

    # Verify connectivity
    healthy = await sairo.health_check()
    if healthy:
        logger.info("Connected to Sairo API", extra={"tool": "server"})
    else:
        logger.warning(
            "Could not reach Sairo API — some tools may not work",
            extra={"tool": "server"},
        )

    # Pre-authenticate using the service token
    session = None
    token = os.environ.get("SAIRO_API_TOKEN", "")
    if token:
        try:
            session = await auth_manager.authenticate(token)
            logger.info(
                f"Authenticated as {session.username} (role={session.role})",
                extra={"tool": "server"},
            )
        except Exception as e:
            logger.warning(f"Auth failed: {e}. Tools requiring auth will fail.", extra={"tool": "server"})

    # If no token or auth failed, create a default admin session for local dev
    if session is None:
        logger.warning(
            "No SAIRO_API_TOKEN set — running with default admin session (local dev mode)",
            extra={"tool": "server"},
        )
        session = UserSession(
            username="mcp-local",
            role="admin",
            token="",
        )

    try:
        yield {
            "sairo": sairo,
            "auth": auth_manager,
            "session": session,
        }
    finally:
        await sairo.close()
        close_db_pool()
        logger.info("Sairo MCP server stopped", extra={"tool": "server"})


# --- Server Instance ---

mcp = FastMCP(
    name=MCP_NAME,
    instructions=SERVER_INSTRUCTIONS,
    lifespan=lifespan,
    host=MCP_HOST,
    port=MCP_PORT,
)


# --- Register All Components ---

def _register_all():
    """Register all tools, resources, and prompts."""
    from tools import discovery, inspection, analytics, cost, pipeline, operations
    from resources import providers
    from prompts import workflows

    discovery.register(mcp)
    inspection.register(mcp)
    analytics.register(mcp)
    cost.register(mcp)
    pipeline.register(mcp)
    operations.register(mcp)
    providers.register(mcp)
    workflows.register(mcp)


_register_all()


# --- Health Endpoints ---

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    """Liveness probe."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(request):
    """Readiness probe."""
    from starlette.responses import JSONResponse
    try:
        db_dir = os.environ.get("DB_DIR", "/data")
        if not os.path.isdir(db_dir):
            return JSONResponse({"status": "not ready", "reason": "DB dir not found"}, status_code=503)
        return JSONResponse({"status": "ready"})
    except Exception as e:
        return JSONResponse({"status": "not ready", "reason": str(e)}, status_code=503)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics_endpoint(request):
    """Prometheus-style metrics."""
    from starlette.responses import JSONResponse
    return JSONResponse(metrics.get_summary())


# --- Entry Point ---

def main():
    """Run the MCP server."""
    transport = "streamable-http"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]
    elif "--stdio" in sys.argv:
        transport = "stdio"

    if transport == "stdio":
        logger.info("Starting MCP server (stdio transport)")
        mcp.run(transport="stdio")
    else:
        logger.info(f"Starting MCP server (HTTP on {MCP_HOST}:{MCP_PORT})")
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

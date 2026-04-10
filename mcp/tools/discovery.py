"""
Discovery tools: list buckets, browse objects, navigate folders, search.

These are the tools the AI uses first to understand what data exists.
Descriptions are written for LLM reasoning — the user never sees tool names.
"""

from typing import Optional

from mcp.server.fastmcp import Context

from db import get_db, list_bucket_dbs, check_table_exists
from observability import track_tool_call
from security import (
    ValidationError,
    format_bytes,
    format_number,
    sanitize_result,
    validate_bucket_name,
    validate_limit,
    validate_prefix,
    validate_search_query,
    validate_sort_field,
    validate_sort_order,
)


def _ctx_session(ctx):
    return ctx.request_context.lifespan_context["session"]


def _ctx_auth(ctx):
    return ctx.request_context.lifespan_context["auth"]


def register(mcp):
    """Register all discovery tools with the MCP server."""

    @mcp.tool(
        name="list_buckets",
        description=(
            "List all S3 storage buckets the user has access to, with their current "
            "stats including object count, total size, and indexing status. "
            "Use this FIRST when the user asks about their storage, what buckets they have, "
            "or any general question about their data. Also use this when the user mentions "
            "a bucket name you haven't seen before — verify it exists here."
        ),
        annotations={"readOnlyHint": True},
    )
    async def list_buckets(ctx: Context) -> str:
        session = _ctx_session(ctx)

        with track_tool_call("list_buckets", user=session.username) as tc:
            bucket_dbs = list_bucket_dbs()

            results = []
            for bdb in bucket_dbs:
                bucket = bdb["bucket"]

                # Filter to user's accessible buckets
                if not session.is_admin and not session.can_read_bucket(bucket):
                    continue

                info = {"name": bucket, "endpoint": bdb["endpoint_id"]}

                try:
                    with get_db(bucket, bdb["endpoint_id"]) as db:
                        # Get crawl status
                        if check_table_exists(db, "crawl_status"):
                            row = db.execute(
                                "SELECT total_objects, total_size, status, "
                                "last_crawl_end FROM crawl_status WHERE id=1"
                            ).fetchone()
                            if row:
                                info["objects"] = row["total_objects"] or 0
                                info["size"] = row["total_size"] or 0
                                info["size_human"] = format_bytes(info["size"])
                                info["index_status"] = row["status"] or "unknown"
                                info["last_indexed"] = row["last_crawl_end"] or "never"
                except (FileNotFoundError, TimeoutError):
                    info["index_status"] = "not indexed"

                results.append(info)

            tc["result_rows"] = len(results)

            if not results:
                return "No buckets found. Either no buckets exist or you don't have access to any."

            lines = [f"Found {len(results)} bucket(s):\n"]
            for b in sorted(results, key=lambda x: x["name"]):
                status = b.get("index_status", "unknown")
                status_icon = {"complete": "ready", "crawling": "indexing...", "idle": "ready"}.get(
                    status, status
                )
                objects = format_number(b.get("objects", 0))
                size = b.get("size_human", "unknown")
                lines.append(
                    f"- **{b['name']}**: {objects} objects, {size} [{status_icon}]"
                )
                if b.get("endpoint") != "default":
                    lines[-1] += f" (endpoint: {b['endpoint']})"

            return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="list_objects",
        description=(
            "List objects (files) in a bucket, optionally within a specific folder path. "
            "Returns file names, sizes, and modification dates. Supports sorting and pagination. "
            "Use this when the user wants to see what's inside a bucket or folder, "
            "browse files, or find specific files. For large buckets, the AI should "
            "start with list_folders to navigate the hierarchy first."
        ),
        annotations={"readOnlyHint": True},
    )
    async def list_objects(
        bucket: str,
        prefix: Optional[str] = None,
        sort_by: Optional[str] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)
        sort_by = validate_sort_field(sort_by, "objects")
        order = validate_sort_order(order)
        limit = validate_limit(limit, max_limit=500, default=100)

        with track_tool_call("list_objects", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                # Map sort fields to SQL columns
                sort_col = {
                    "name": "key", "key": "key",
                    "size": "size",
                    "date": "last_modified", "last_modified": "last_modified",
                }.get(sort_by or "key", "key")

                if prefix:
                    rows = db.execute(
                        f"SELECT key, size, last_modified FROM objects "
                        f"WHERE key LIKE ? "
                        f"ORDER BY {sort_col} {order.upper()} LIMIT ?",
                        (prefix + "%", limit),
                    ).fetchall()
                else:
                    rows = db.execute(
                        f"SELECT key, size, last_modified FROM objects "
                        f"ORDER BY {sort_col} {order.upper()} LIMIT ?",
                        (limit,),
                    ).fetchall()

                tc["result_rows"] = len(rows)

                if not rows:
                    location = f"'{prefix}'" if prefix else "root"
                    return f"No objects found in {location} of bucket '{bucket}'."

                lines = [f"Objects in **{bucket}**" + (f"/{prefix}" if prefix else "") + f" ({len(rows)} shown):\n"]
                for r in rows:
                    name = r["key"]
                    if prefix:
                        name = name[len(prefix):]
                    size = format_bytes(r["size"])
                    date = r["last_modified"][:10] if r["last_modified"] else "unknown"
                    lines.append(f"- `{name}` — {size}, modified {date}")

                if len(rows) == limit:
                    lines.append(f"\n(Showing first {limit} results. Ask for more or narrow with a prefix.)")

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="list_folders",
        description=(
            "List the immediate subfolders of a bucket or folder, along with each folder's "
            "object count and total size. This is the fastest way to navigate large buckets — "
            "it uses pre-computed stats so it's instant even for buckets with millions of objects. "
            "Use this FIRST before list_objects when exploring a large bucket, "
            "to understand the folder structure and find where the data lives."
        ),
        annotations={"readOnlyHint": True},
    )
    async def list_folders(
        bucket: str,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)

        with track_tool_call("list_folders", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                # Try prefix_children first (pre-computed, instant)
                if check_table_exists(db, "prefix_children"):
                    parent = prefix or ""
                    rows = db.execute(
                        "SELECT child_name, child_prefix, object_count, total_size "
                        "FROM prefix_children WHERE parent_prefix = ? "
                        "ORDER BY child_name",
                        (parent,),
                    ).fetchall()

                    if rows:
                        tc["result_rows"] = len(rows)
                        total_objects = sum(r["object_count"] for r in rows)
                        total_size = sum(r["total_size"] for r in rows)

                        location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                        lines = [
                            f"Folders in {location} "
                            f"({len(rows)} folders, {format_number(total_objects)} objects, "
                            f"{format_bytes(total_size)} total):\n"
                        ]
                        for r in rows:
                            pct = (r["total_size"] / total_size * 100) if total_size > 0 else 0
                            lines.append(
                                f"- **{r['child_name']}/** — "
                                f"{format_number(r['object_count'])} objects, "
                                f"{format_bytes(r['total_size'])} ({pct:.1f}%)"
                            )
                        return sanitize_result("\n".join(lines))

                # Fallback: use folder_stats for top-level
                if not prefix and check_table_exists(db, "folder_stats"):
                    rows = db.execute(
                        "SELECT prefix, object_count, total_size FROM folder_stats "
                        "ORDER BY total_size DESC"
                    ).fetchall()

                    if rows:
                        tc["result_rows"] = len(rows)
                        total_size = sum(r["total_size"] for r in rows)

                        lines = [f"Top-level folders in **{bucket}** ({len(rows)} folders):\n"]
                        for r in rows:
                            pct = (r["total_size"] / total_size * 100) if total_size > 0 else 0
                            lines.append(
                                f"- **{r['prefix']}** — "
                                f"{format_number(r['object_count'])} objects, "
                                f"{format_bytes(r['total_size'])} ({pct:.1f}%)"
                            )
                        return sanitize_result("\n".join(lines))

                # Last resort: compute from objects table
                if prefix:
                    depth = prefix.count("/")
                    rows = db.execute(
                        "SELECT DISTINCT SUBSTR(key, 1, INSTR(SUBSTR(key, ?+1), '/') + ?) as folder "
                        "FROM objects WHERE key LIKE ? AND depth > ? LIMIT 200",
                        (len(prefix), len(prefix), prefix + "%", depth),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT prefix, COUNT(*) as cnt, SUM(size) as total "
                        "FROM objects WHERE prefix != '' GROUP BY prefix "
                        "ORDER BY total DESC LIMIT 200"
                    ).fetchall()

                tc["result_rows"] = len(rows)

                if not rows:
                    return f"No folders found in '{bucket}/{prefix or ''}'."

                lines = [f"Folders in **{bucket}**" + (f"/{prefix}" if prefix else "") + ":\n"]
                for r in rows:
                    if "cnt" in r.keys():
                        lines.append(
                            f"- **{r['prefix']}** — {format_number(r['cnt'])} objects, "
                            f"{format_bytes(r['total'])}"
                        )
                    else:
                        lines.append(f"- {r['folder']}")

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="search_objects",
        description=(
            "Search for files by name across an entire bucket using full-text search. "
            "Works with partial filenames, extensions, or any substring in the file path. "
            "Very fast — uses a pre-built search index. "
            "Use this when the user asks to find a specific file, search for files matching "
            "a pattern, or locate files by extension (e.g., 'find all parquet files')."
        ),
        annotations={"readOnlyHint": True},
    )
    async def search_objects(
        bucket: str,
        query: str,
        prefix: Optional[str] = None,
        limit: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        query = validate_search_query(query)
        prefix = validate_prefix(prefix)
        limit = validate_limit(limit, max_limit=200, default=50)

        with track_tool_call("search_objects", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                results = []

                # Try FTS5 first (requires 3+ char query for trigram)
                if len(query) >= 3 and check_table_exists(db, "objects_fts"):
                    try:
                        fts_query = f'"{query}"'
                        if prefix:
                            rows = db.execute(
                                "SELECT o.key, o.size, o.last_modified "
                                "FROM objects_fts f JOIN objects o ON f.rowid = o.rowid "
                                "WHERE f.key MATCH ? AND o.key LIKE ? "
                                "ORDER BY o.key LIMIT ?",
                                (fts_query, prefix + "%", limit),
                            ).fetchall()
                        else:
                            rows = db.execute(
                                "SELECT o.key, o.size, o.last_modified "
                                "FROM objects_fts f JOIN objects o ON f.rowid = o.rowid "
                                "WHERE f.key MATCH ? ORDER BY o.key LIMIT ?",
                                (fts_query, limit),
                            ).fetchall()
                        results = rows
                    except Exception:
                        results = []

                # Fallback to LIKE
                if not results:
                    like_pattern = f"%{query}%"
                    if prefix:
                        results = db.execute(
                            "SELECT key, size, last_modified FROM objects "
                            "WHERE key LIKE ? AND key LIKE ? "
                            "ORDER BY key LIMIT ?",
                            (prefix + "%", like_pattern, limit),
                        ).fetchall()
                    else:
                        results = db.execute(
                            "SELECT key, size, last_modified FROM objects "
                            "WHERE key LIKE ? ORDER BY key LIMIT ?",
                            (like_pattern, limit),
                        ).fetchall()

                tc["result_rows"] = len(results)

                if not results:
                    scope = f"in '{prefix}' of " if prefix else "in "
                    return f"No files matching '{query}' found {scope}bucket '{bucket}'."

                lines = [f"Found {len(results)} file(s) matching '{query}' in **{bucket}**:\n"]
                total_size = sum(r["size"] for r in results)

                for r in results:
                    size = format_bytes(r["size"])
                    date = r["last_modified"][:10] if r["last_modified"] else ""
                    lines.append(f"- `{r['key']}` — {size}, {date}")

                lines.append(f"\nTotal: {format_bytes(total_size)} across {len(results)} files")

                if len(results) == limit:
                    lines.append(f"(Showing first {limit}. Narrow your search or increase the limit.)")

                return sanitize_result("\n".join(lines))

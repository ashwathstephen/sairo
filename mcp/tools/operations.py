"""
Operations tools: crawl status, trigger re-index, audit log.

These tools manage the Sairo system itself rather than analyzing data.
"""

from typing import Optional

from mcp.server.fastmcp import Context

from db import get_db, check_table_exists
from observability import track_tool_call
from security import (
    format_bytes,
    format_number,
    sanitize_result,
    validate_bucket_name,
    validate_limit,
)


def _ctx_session(ctx):
    return ctx.request_context.lifespan_context["session"]


def _ctx_auth(ctx):
    return ctx.request_context.lifespan_context["auth"]


def _ctx_sairo(ctx):
    return ctx.request_context.lifespan_context["sairo"]


def register(mcp):
    """Register all operations tools with the MCP server."""

    @mcp.tool(
        name="get_crawl_status",
        description=(
            "Check the indexing status of a bucket — whether it's currently being indexed, "
            "when it was last indexed, and how many objects have been cataloged. "
            "Use this when the user asks 'is the index up to date?', when search results "
            "seem incomplete, or when other tools return stale data."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_crawl_status(
        bucket: str,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)

        with track_tool_call("get_crawl_status", user=session.username, bucket=bucket):
            try:
                with get_db(bucket) as db:
                    if not check_table_exists(db, "crawl_status"):
                        return f"Bucket '{bucket}' has not been indexed yet."

                    row = db.execute(
                        "SELECT total_objects, total_size, status, "
                        "last_crawl_start, last_crawl_end, current_crawl_gen "
                        "FROM crawl_status WHERE id=1"
                    ).fetchone()

                    if not row:
                        return f"Bucket '{bucket}' has not been indexed yet."

                    status = row["status"] or "unknown"
                    objects = row["total_objects"] or 0
                    size = row["total_size"] or 0
                    started = row["last_crawl_start"] or "never"
                    ended = row["last_crawl_end"] or "never"
                    gen = row["current_crawl_gen"] or 0

                    status_display = {
                        "crawling": "Currently indexing...",
                        "complete": "Up to date",
                        "idle": "Up to date",
                    }.get(status, status)

                    lines = [
                        f"Index status for **{bucket}**:\n",
                        f"- Status: **{status_display}**",
                        f"- Objects indexed: {format_number(objects)}",
                        f"- Total size: {format_bytes(size)}",
                        f"- Last index started: {started}",
                        f"- Last index completed: {ended}",
                        f"- Index generation: {gen}",
                    ]

                    # Check FTS availability
                    has_fts = check_table_exists(db, "objects_fts")
                    lines.append(
                        f"- Full-text search: {'available' if has_fts else 'not built yet'}"
                    )

                    return sanitize_result("\n".join(lines))

            except FileNotFoundError:
                return f"Bucket '{bucket}' has not been indexed yet. Trigger a crawl to start indexing."

    @mcp.tool(
        name="trigger_crawl",
        description=(
            "Trigger a re-index of a bucket to update the search index and stats. "
            "This causes Sairo to re-scan all objects in the bucket from S3. "
            "Use this when the user says data seems outdated, search is missing recent files, "
            "or explicitly asks to refresh/re-index a bucket. "
            "Requires write permission or admin role."
        ),
        annotations={"readOnlyHint": False, "idempotentHint": True},
    )
    async def trigger_crawl(
        bucket: str,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_write(session, bucket)

        with track_tool_call("trigger_crawl", user=session.username, bucket=bucket):
            try:
                result = await _ctx_sairo(ctx).trigger_crawl(
                    bucket, user_token=session.token
                )
                return (
                    f"Re-indexing started for bucket **{bucket}**. "
                    f"This runs in the background — use get_crawl_status to check progress."
                )
            except Exception as e:
                return f"Could not trigger re-index for '{bucket}': {str(e)}"

    @mcp.tool(
        name="get_audit_log",
        description=(
            "View the activity log showing who did what — uploads, downloads, deletions, "
            "logins, permission changes, and other actions. Admin only. "
            "Use this when the user asks 'who uploaded this?', 'what happened to that file?', "
            "'show me recent activity', or is investigating a security concern."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_audit_log(
        bucket: Optional[str] = None,
        username: Optional[str] = None,
        action: Optional[str] = None,
        limit: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        _ctx_auth(ctx).require_admin(session)

        if bucket:
            bucket = validate_bucket_name(bucket)
        limit = validate_limit(limit, max_limit=200, default=50)

        with track_tool_call("get_audit_log", user=session.username, bucket=bucket) as tc:
            try:
                entries = await _ctx_sairo(ctx).get_audit_log(
                    bucket=bucket,
                    username=username,
                    action=action,
                    limit=limit,
                    user_token=session.token,
                )
            except Exception as e:
                return f"Could not fetch audit log: {str(e)}"

            tc["result_rows"] = len(entries)

            if not entries:
                filters = []
                if bucket:
                    filters.append(f"bucket={bucket}")
                if username:
                    filters.append(f"user={username}")
                if action:
                    filters.append(f"action={action}")
                filter_str = f" (filters: {', '.join(filters)})" if filters else ""
                return f"No audit log entries found{filter_str}."

            lines = [f"Recent activity ({len(entries)} entries):\n"]
            lines.append("| Time | User | Action | Bucket | Details |")
            lines.append("|------|------|--------|--------|---------|")

            for e in entries:
                ts = e.get("timestamp", "")[:16]
                user = e.get("username", "?")
                act = e.get("action", "?")
                bkt = e.get("bucket", "—")
                details = e.get("details", "")[:80]
                lines.append(f"| {ts} | {user} | {act} | {bkt} | {details} |")

            return sanitize_result("\n".join(lines))

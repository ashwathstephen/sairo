"""
Analytics tools: storage breakdown, trends, distributions, duplicates.

These are the highest-value tools — they turn raw storage data into
actionable insights. Every tool returns human-readable analysis,
not raw numbers.
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
    validate_days,
    validate_limit,
    validate_min_size,
    validate_prefix,
)


def _ctx_session(ctx):
    return ctx.request_context.lifespan_context["session"]


def _ctx_auth(ctx):
    return ctx.request_context.lifespan_context["auth"]


def register(mcp):
    """Register all analytics tools with the MCP server."""

    @mcp.tool(
        name="get_storage_breakdown",
        description=(
            "Show how storage is distributed across folders in a bucket. "
            "Returns each folder's object count, total size, and percentage of the bucket. "
            "Use this when the user asks 'where is all my storage going?', wants to understand "
            "which folders are using the most space, or needs to find the biggest data areas. "
            "This is often the FIRST tool to use for any storage analysis question."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_storage_breakdown(
        bucket: str,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)

        with track_tool_call("get_storage_breakdown", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                # Get total bucket stats
                total_row = db.execute(
                    "SELECT COUNT(*) as cnt, SUM(size) as total FROM objects"
                ).fetchone()
                total_objects = total_row["cnt"] or 0
                total_size = total_row["total"] or 0

                # Get per-prefix breakdown
                if prefix:
                    depth = prefix.count("/")
                    rows = db.execute(
                        "SELECT "
                        "  SUBSTR(key, 1, INSTR(SUBSTR(key, ?+1), '/') + ?) as folder, "
                        "  COUNT(*) as cnt, SUM(size) as total "
                        "FROM objects "
                        "WHERE key LIKE ? AND depth >= ? "
                        "GROUP BY folder "
                        "HAVING folder != '' "
                        "ORDER BY total DESC",
                        (len(prefix), len(prefix), prefix + "%", depth),
                    ).fetchall()
                elif check_table_exists(db, "folder_stats"):
                    rows = db.execute(
                        "SELECT prefix as folder, object_count as cnt, total_size as total "
                        "FROM folder_stats ORDER BY total_size DESC"
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT prefix as folder, COUNT(*) as cnt, SUM(size) as total "
                        "FROM objects WHERE prefix != '' "
                        "GROUP BY prefix ORDER BY total DESC"
                    ).fetchall()

                tc["result_rows"] = len(rows)

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [
                    f"Storage breakdown for {location}\n",
                    f"Total: {format_number(total_objects)} objects, {format_bytes(total_size)}\n",
                ]

                if rows:
                    lines.append("| Folder | Objects | Size | % of Total |")
                    lines.append("|--------|---------|------|------------|")

                    for r in rows:
                        folder = r["folder"] or "(root files)"
                        cnt = r["cnt"] or 0
                        size = r["total"] or 0
                        pct = (size / total_size * 100) if total_size > 0 else 0
                        lines.append(
                            f"| {folder} | {format_number(cnt)} | {format_bytes(size)} | {pct:.1f}% |"
                        )

                    # Add insight about concentration
                    if len(rows) >= 2:
                        top_size = rows[0]["total"] or 0
                        top_pct = (top_size / total_size * 100) if total_size > 0 else 0
                        if top_pct > 50:
                            lines.append(
                                f"\n**{rows[0]['folder']}** contains {top_pct:.0f}% of all storage — "
                                "this is where most of the data lives."
                            )
                else:
                    lines.append("No folders found — all objects may be at the root level.")

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="get_storage_trends",
        description=(
            "Show how storage has grown or shrunk over time. Returns a timeline of "
            "daily or weekly snapshots showing object count and total size changes. "
            "Use this when the user asks about growth, trends, whether storage is increasing, "
            "cost projections, or 'what happened last week/month'. "
            "Combine with get_storage_breakdown to find WHERE growth is happening."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_storage_trends(
        bucket: str,
        prefix: Optional[str] = None,
        days: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)
        days = validate_days(days, max_days=365, default=90)

        with track_tool_call("get_storage_trends", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                if not check_table_exists(db, "storage_history"):
                    return (
                        f"No trend data available for bucket '{bucket}'. "
                        "Storage history is recorded after each index crawl."
                    )

                target_prefix = prefix or ""
                rows = db.execute(
                    "SELECT DATE(timestamp) as day, "
                    "  MAX(object_count) as objects, "
                    "  MAX(total_size) as size "
                    "FROM storage_history "
                    "WHERE prefix = ? AND timestamp >= datetime('now', ?) "
                    "GROUP BY day ORDER BY day",
                    (target_prefix, f"-{days} days"),
                ).fetchall()

                tc["result_rows"] = len(rows)

                if not rows:
                    return (
                        f"No trend data found for the last {days} days in bucket '{bucket}'. "
                        "Trends are recorded during periodic index crawls."
                    )

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [f"Storage trends for {location} (last {days} days):\n"]

                # Calculate deltas
                lines.append("| Date | Objects | Size | Change |")
                lines.append("|------|---------|------|--------|")

                prev_size = None
                first_size = rows[0]["size"] if rows else 0
                last_size = rows[-1]["size"] if rows else 0

                for r in rows:
                    day = r["day"]
                    objects = format_number(r["objects"])
                    size = format_bytes(r["size"])

                    if prev_size is not None:
                        delta = r["size"] - prev_size
                        if delta > 0:
                            change = f"+{format_bytes(delta)}"
                        elif delta < 0:
                            change = f"-{format_bytes(abs(delta))}"
                        else:
                            change = "—"
                    else:
                        change = "—"

                    lines.append(f"| {day} | {objects} | {size} | {change} |")
                    prev_size = r["size"]

                # Summary
                net_change = last_size - first_size
                if first_size > 0:
                    growth_pct = (net_change / first_size) * 100
                    direction = "grew" if net_change > 0 else "shrank"
                    lines.append(
                        f"\nOverall: storage {direction} by {format_bytes(abs(net_change))} "
                        f"({growth_pct:+.1f}%) over {days} days."
                    )

                    if net_change > 0:
                        daily_rate = net_change / max(len(rows), 1)
                        monthly_proj = daily_rate * 30
                        lines.append(
                            f"At this rate, expect ~{format_bytes(int(monthly_proj))} "
                            f"of growth per month."
                        )

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="get_file_type_distribution",
        description=(
            "Analyze what types of files are in a bucket — shows file extensions ranked by "
            "total size and count. Helps understand the nature of stored data "
            "(e.g., 80% Parquet files, 15% logs, 5% JSON). "
            "Use this when the user asks 'what kind of files are in here?', wants to understand "
            "data composition, or is planning cleanup/archival by file type."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_file_type_distribution(
        bucket: str,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)

        with track_tool_call("get_file_type_distribution", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                # Extract extension from key using SQL
                where = "WHERE key LIKE ?" if prefix else ""
                params = (prefix + "%",) if prefix else ()

                rows = db.execute(
                    f"SELECT "
                    f"  CASE "
                    f"    WHEN INSTR(key, '.') > 0 AND INSTR(key, '/') < LENGTH(key) "
                    f"    THEN LOWER('.' || SUBSTR(key, -INSTR(SUBSTR(key || ' ', -LENGTH(key)), '.') + 1)) "
                    f"    ELSE '(no extension)' "
                    f"  END as ext, "
                    f"  COUNT(*) as cnt, "
                    f"  SUM(size) as total_size, "
                    f"  AVG(size) as avg_size "
                    f"FROM objects {where} "
                    f"GROUP BY ext ORDER BY total_size DESC LIMIT 30",
                    params,
                ).fetchall()

                # Fallback: simpler extension extraction
                if not rows:
                    rows = db.execute(
                        f"SELECT '(all files)' as ext, COUNT(*) as cnt, "
                        f"SUM(size) as total_size, AVG(size) as avg_size "
                        f"FROM objects {where}",
                        params,
                    ).fetchall()

                tc["result_rows"] = len(rows)
                total_size = sum(r["total_size"] for r in rows if r["total_size"])

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [f"File types in {location}:\n"]

                lines.append("| Extension | Count | Total Size | Avg Size | % |")
                lines.append("|-----------|-------|------------|----------|---|")

                for r in rows:
                    ext = r["ext"] or "(no extension)"
                    cnt = format_number(r["cnt"])
                    total = format_bytes(r["total_size"] or 0)
                    avg = format_bytes(int(r["avg_size"] or 0))
                    pct = ((r["total_size"] or 0) / total_size * 100) if total_size > 0 else 0
                    lines.append(f"| {ext} | {cnt} | {total} | {avg} | {pct:.1f}% |")

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="get_size_distribution",
        description=(
            "Show how objects are distributed by file size — a histogram of tiny files, "
            "small files, medium files, large files, etc. "
            "Reveals whether storage is dominated by many small files or a few large ones. "
            "Use this when the user asks about file sizes, wants to find large files, "
            "or is diagnosing storage patterns (e.g., 'are we storing too many tiny files?')."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_size_distribution(
        bucket: str,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)

        with track_tool_call("get_size_distribution", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                where = "WHERE key LIKE ?" if prefix else ""
                params = (prefix + "%",) if prefix else ()

                rows = db.execute(
                    f"SELECT "
                    f"  CASE "
                    f"    WHEN size = 0 THEN '0 B (empty)' "
                    f"    WHEN size < 1024 THEN '1 B - 1 KB' "
                    f"    WHEN size < 1048576 THEN '1 KB - 1 MB' "
                    f"    WHEN size < 10485760 THEN '1 MB - 10 MB' "
                    f"    WHEN size < 104857600 THEN '10 MB - 100 MB' "
                    f"    WHEN size < 1073741824 THEN '100 MB - 1 GB' "
                    f"    ELSE '1 GB+' "
                    f"  END as size_range, "
                    f"  CASE "
                    f"    WHEN size = 0 THEN 0 "
                    f"    WHEN size < 1024 THEN 1 "
                    f"    WHEN size < 1048576 THEN 2 "
                    f"    WHEN size < 10485760 THEN 3 "
                    f"    WHEN size < 104857600 THEN 4 "
                    f"    WHEN size < 1073741824 THEN 5 "
                    f"    ELSE 6 "
                    f"  END as sort_order, "
                    f"  COUNT(*) as cnt, "
                    f"  SUM(size) as total_size "
                    f"FROM objects {where} "
                    f"GROUP BY size_range, sort_order ORDER BY sort_order",
                    params,
                ).fetchall()

                tc["result_rows"] = len(rows)
                total_objects = sum(r["cnt"] for r in rows)
                total_size = sum(r["total_size"] for r in rows if r["total_size"])

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [f"Size distribution in {location}:\n"]

                lines.append("| Size Range | Files | % Files | Total Size | % Size |")
                lines.append("|------------|-------|---------|------------|--------|")

                for r in rows:
                    cnt_pct = (r["cnt"] / total_objects * 100) if total_objects > 0 else 0
                    size_pct = ((r["total_size"] or 0) / total_size * 100) if total_size > 0 else 0
                    lines.append(
                        f"| {r['size_range']} | {format_number(r['cnt'])} | "
                        f"{cnt_pct:.1f}% | {format_bytes(r['total_size'] or 0)} | {size_pct:.1f}% |"
                    )

                # Insight
                if rows:
                    largest_by_count = max(rows, key=lambda r: r["cnt"])
                    largest_by_size = max(rows, key=lambda r: r["total_size"] or 0)
                    lines.append(
                        f"\nMost files are in the **{largest_by_count['size_range']}** range "
                        f"({(largest_by_count['cnt'] / total_objects * 100):.0f}% of files), "
                        f"but most storage is consumed by **{largest_by_size['size_range']}** files "
                        f"({((largest_by_size['total_size'] or 0) / total_size * 100):.0f}% of space)."
                    )

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="get_age_distribution",
        description=(
            "Show how objects are distributed by age (time since last modification). "
            "Reveals how much data is recent vs. old, which is critical for archival planning "
            "and cost optimization. "
            "Use this when the user asks about data freshness, old files, what can be archived, "
            "or when planning lifecycle rules."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_age_distribution(
        bucket: str,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)

        with track_tool_call("get_age_distribution", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                where = "WHERE key LIKE ?" if prefix else ""
                params = (prefix + "%",) if prefix else ()

                rows = db.execute(
                    f"SELECT "
                    f"  CASE "
                    f"    WHEN julianday('now') - julianday(last_modified) < 1 THEN 'Today' "
                    f"    WHEN julianday('now') - julianday(last_modified) < 7 THEN 'Last 7 days' "
                    f"    WHEN julianday('now') - julianday(last_modified) < 30 THEN 'Last 30 days' "
                    f"    WHEN julianday('now') - julianday(last_modified) < 90 THEN '1-3 months' "
                    f"    WHEN julianday('now') - julianday(last_modified) < 180 THEN '3-6 months' "
                    f"    WHEN julianday('now') - julianday(last_modified) < 365 THEN '6-12 months' "
                    f"    ELSE '1+ years' "
                    f"  END as age_range, "
                    f"  CASE "
                    f"    WHEN julianday('now') - julianday(last_modified) < 1 THEN 0 "
                    f"    WHEN julianday('now') - julianday(last_modified) < 7 THEN 1 "
                    f"    WHEN julianday('now') - julianday(last_modified) < 30 THEN 2 "
                    f"    WHEN julianday('now') - julianday(last_modified) < 90 THEN 3 "
                    f"    WHEN julianday('now') - julianday(last_modified) < 180 THEN 4 "
                    f"    WHEN julianday('now') - julianday(last_modified) < 365 THEN 5 "
                    f"    ELSE 6 "
                    f"  END as sort_order, "
                    f"  COUNT(*) as cnt, "
                    f"  SUM(size) as total_size "
                    f"FROM objects {where} "
                    f"GROUP BY age_range, sort_order ORDER BY sort_order",
                    params,
                ).fetchall()

                tc["result_rows"] = len(rows)
                total_objects = sum(r["cnt"] for r in rows)
                total_size = sum(r["total_size"] for r in rows if r["total_size"])

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [f"Data age distribution in {location}:\n"]

                lines.append("| Age | Files | % Files | Size | % Size |")
                lines.append("|-----|-------|---------|------|--------|")

                old_data_size = 0
                old_data_count = 0

                for r in rows:
                    cnt_pct = (r["cnt"] / total_objects * 100) if total_objects > 0 else 0
                    size_pct = ((r["total_size"] or 0) / total_size * 100) if total_size > 0 else 0
                    lines.append(
                        f"| {r['age_range']} | {format_number(r['cnt'])} | "
                        f"{cnt_pct:.1f}% | {format_bytes(r['total_size'] or 0)} | {size_pct:.1f}% |"
                    )

                    # Track data older than 90 days
                    if r["sort_order"] >= 3:
                        old_data_size += r["total_size"] or 0
                        old_data_count += r["cnt"]

                # Archival insight
                if old_data_size > 0 and total_size > 0:
                    old_pct = old_data_size / total_size * 100
                    lines.append(
                        f"\n**{format_bytes(old_data_size)}** ({old_pct:.0f}%) of data is older than "
                        f"90 days ({format_number(old_data_count)} files). "
                        f"This data may be a candidate for archival to a cheaper storage class."
                    )

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="get_top_objects",
        description=(
            "Find the largest or most recently modified files in a bucket. "
            "Use this to identify space hogs, find the newest uploads, "
            "or investigate what's consuming the most storage. "
            "When the user says 'what's taking up all the space?' or "
            "'show me the biggest files', this is the tool."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_top_objects(
        bucket: str,
        prefix: Optional[str] = None,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)
        limit = validate_limit(limit, max_limit=100, default=25)

        # Default to size for "top objects"
        if not sort_by:
            sort_by = "size"
        sort_by = sort_by.strip().lower()
        if sort_by not in ("size", "date"):
            sort_by = "size"

        with track_tool_call("get_top_objects", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                where = "WHERE key LIKE ?" if prefix else ""
                params = (prefix + "%",) if prefix else ()
                order_col = "size DESC" if sort_by == "size" else "last_modified DESC"

                rows = db.execute(
                    f"SELECT key, size, last_modified FROM objects {where} "
                    f"ORDER BY {order_col} LIMIT ?",
                    (*params, limit),
                ).fetchall()

                tc["result_rows"] = len(rows)

                if not rows:
                    return f"No objects found in bucket '{bucket}'."

                label = "largest" if sort_by == "size" else "most recent"
                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [f"Top {len(rows)} {label} files in {location}:\n"]

                total_shown_size = 0
                for i, r in enumerate(rows, 1):
                    size = format_bytes(r["size"])
                    date = r["last_modified"][:10] if r["last_modified"] else "unknown"
                    lines.append(f"{i}. `{r['key']}` — {size}, {date}")
                    total_shown_size += r["size"]

                lines.append(
                    f"\nThese {len(rows)} files account for {format_bytes(total_shown_size)} total."
                )

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="find_duplicates",
        description=(
            "Find files that appear to be duplicates based on matching file size and ETag "
            "(content hash). Shows how much storage is wasted by redundant copies. "
            "Use this when the user wants to clean up storage, find wasted space, "
            "reduce costs, or asks 'are there any duplicate files?'."
        ),
        annotations={"readOnlyHint": True},
    )
    async def find_duplicates(
        bucket: str,
        prefix: Optional[str] = None,
        min_size: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)
        min_size = validate_min_size(min_size, default=1_048_576)  # 1MB default

        with track_tool_call("find_duplicates", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                where_clause = "WHERE size >= ?"
                params: list = [min_size]

                if prefix:
                    where_clause += " AND key LIKE ?"
                    params.append(prefix + "%")

                rows = db.execute(
                    f"SELECT etag, size, COUNT(*) as copies, "
                    f"  GROUP_CONCAT(key, '|||') as keys "
                    f"FROM objects {where_clause} "
                    f"GROUP BY etag, size HAVING copies > 1 "
                    f"ORDER BY (size * (copies - 1)) DESC "
                    f"LIMIT 50",
                    params,
                ).fetchall()

                tc["result_rows"] = len(rows)

                if not rows:
                    return (
                        f"No duplicate files found in bucket '{bucket}' "
                        f"(checked files larger than {format_bytes(min_size)}). "
                        "Your storage looks clean!"
                    )

                total_wasted = sum((r["size"] * (r["copies"] - 1)) for r in rows)
                total_groups = len(rows)

                lines = [
                    f"Found **{total_groups} duplicate groups** in **{bucket}**, "
                    f"wasting **{format_bytes(total_wasted)}** of storage:\n"
                ]

                for i, r in enumerate(rows[:20], 1):
                    keys = r["keys"].split("|||")
                    wasted = r["size"] * (r["copies"] - 1)
                    lines.append(
                        f"**{i}. {r['copies']} copies** of a {format_bytes(r['size'])} file "
                        f"(wasting {format_bytes(wasted)}):"
                    )
                    for key in keys[:5]:
                        lines.append(f"   - `{key}`")
                    if len(keys) > 5:
                        lines.append(f"   - ... and {len(keys) - 5} more")
                    lines.append("")

                if total_groups > 20:
                    lines.append(f"(Showing top 20 of {total_groups} duplicate groups)")

                lines.append(
                    f"\n**Total reclaimable space: {format_bytes(total_wasted)}** "
                    f"by keeping one copy of each duplicate."
                )

                return sanitize_result("\n".join(lines))

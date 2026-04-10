"""
Pipeline intelligence tools: analyze data structure, detect freshness, compare snapshots.

These tools help users understand their data pipelines by analyzing
naming patterns, staleness, and changes over time.
"""

import re
from collections import Counter
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
    validate_prefix,
)


def _ctx_session(ctx):
    return ctx.request_context.lifespan_context["session"]


def _ctx_auth(ctx):
    return ctx.request_context.lifespan_context["auth"]


def register(mcp):
    """Register all pipeline intelligence tools with the MCP server."""

    @mcp.tool(
        name="analyze_prefix_structure",
        description=(
            "Analyze the naming patterns and directory structure of files in a bucket. "
            "Detects common patterns like Hive-style partitioning (year=2024/month=01/), "
            "date-based paths, flat structures, and naming conventions. "
            "Use this when the user asks about how data is organized, wants to understand "
            "a data pipeline's output structure, or is investigating an unfamiliar bucket."
        ),
        annotations={"readOnlyHint": True},
    )
    async def analyze_prefix_structure(
        bucket: str,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)

        with track_tool_call("analyze_prefix_structure", user=session.username, bucket=bucket):
            with get_db(bucket) as db:
                where = "WHERE key LIKE ?" if prefix else ""
                params = (prefix + "%",) if prefix else ()

                # Sample keys for pattern analysis
                rows = db.execute(
                    f"SELECT key FROM objects {where} ORDER BY RANDOM() LIMIT 1000",
                    params,
                ).fetchall()

                if not rows:
                    return f"No objects found in bucket '{bucket}'."

                keys = [r["key"] for r in rows]

                # Analyze patterns
                findings = []

                # 1. Check for Hive-style partitioning
                hive_pattern = re.compile(r"(\w+)=([^/]+)")
                hive_keys = {}
                for key in keys:
                    matches = hive_pattern.findall(key)
                    for name, _ in matches:
                        hive_keys[name] = hive_keys.get(name, 0) + 1

                if hive_keys:
                    common_partitions = sorted(
                        hive_keys.items(), key=lambda x: x[1], reverse=True
                    )[:5]
                    if common_partitions[0][1] > len(keys) * 0.3:
                        partition_names = [p[0] for p in common_partitions if p[1] > len(keys) * 0.1]
                        findings.append(
                            f"**Hive-style partitioning detected**: "
                            f"Keys partitioned by: {', '.join(partition_names)}. "
                            f"Pattern: `{'/'.join(f'{p}={{...}}' for p in partition_names)}/`"
                        )

                # 2. Check for date-based paths
                date_patterns = [
                    (r"/\d{4}/\d{2}/\d{2}/", "YYYY/MM/DD date-based paths"),
                    (r"/\d{4}-\d{2}-\d{2}/", "YYYY-MM-DD date-based paths"),
                    (r"/\d{4}/\d{2}/", "YYYY/MM monthly paths"),
                    (r"/dt=\d{4}-\d{2}-\d{2}/", "dt=YYYY-MM-DD partition paths"),
                ]
                for pattern, desc in date_patterns:
                    matches = sum(1 for k in keys if re.search(pattern, k))
                    if matches > len(keys) * 0.2:
                        findings.append(f"**{desc}** found in {matches / len(keys) * 100:.0f}% of files")

                # 3. Depth analysis
                depths = [k.count("/") for k in keys]
                avg_depth = sum(depths) / len(depths) if depths else 0
                max_depth = max(depths) if depths else 0
                depth_counter = Counter(depths)
                most_common_depth = depth_counter.most_common(1)[0] if depth_counter else (0, 0)

                findings.append(
                    f"**Directory depth**: average {avg_depth:.1f}, max {max_depth}. "
                    f"Most common depth: {most_common_depth[0]} ({most_common_depth[1]} files)"
                )

                # 4. Extension analysis
                extensions = Counter()
                for key in keys:
                    parts = key.rsplit(".", 1)
                    if len(parts) == 2 and len(parts[1]) <= 10:
                        extensions[f".{parts[1].lower()}"] += 1
                    else:
                        extensions["(none)"] += 1

                top_exts = extensions.most_common(5)
                ext_str = ", ".join(f"{ext} ({cnt})" for ext, cnt in top_exts)
                findings.append(f"**File types**: {ext_str}")

                # 5. Common prefixes
                top_level = Counter()
                for key in keys:
                    parts = key.split("/", 1)
                    if len(parts) > 1:
                        top_level[parts[0] + "/"] += 1
                    else:
                        top_level["(root)"] += 1

                top_prefixes = top_level.most_common(10)
                prefix_str = ", ".join(f"`{p}` ({cnt})" for p, cnt in top_prefixes)
                findings.append(f"**Top-level structure**: {prefix_str}")

                # 6. Naming convention
                has_underscores = sum(1 for k in keys if "_" in k.split("/")[-1])
                has_hyphens = sum(1 for k in keys if "-" in k.split("/")[-1])
                has_spaces = sum(1 for k in keys if " " in k)
                has_uppercase = sum(1 for k in keys if any(c.isupper() for c in k.split("/")[-1]))

                conventions = []
                if has_underscores > len(keys) * 0.5:
                    conventions.append("snake_case")
                if has_hyphens > len(keys) * 0.5:
                    conventions.append("kebab-case")
                if has_uppercase > len(keys) * 0.5:
                    conventions.append("mixed case")
                if has_spaces > len(keys) * 0.1:
                    conventions.append("contains spaces")

                if conventions:
                    findings.append(f"**Naming convention**: {', '.join(conventions)}")

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [
                    f"Structure analysis of {location} (sampled {len(keys)} files):\n"
                ]
                for f in findings:
                    lines.append(f"- {f}")

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="detect_data_freshness",
        description=(
            "Check when each folder was last updated — shows which data pipelines are "
            "actively writing and which have gone stale. "
            "Use this when the user asks 'is this pipeline still running?', 'when was this "
            "last updated?', or wants to monitor data pipeline health. "
            "Great for detecting broken pipelines or forgotten data sources."
        ),
        annotations={"readOnlyHint": True},
    )
    async def detect_data_freshness(
        bucket: str,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)

        with track_tool_call("detect_data_freshness", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                if prefix:
                    # Show freshness for subfolders of this prefix
                    depth = prefix.count("/")
                    rows = db.execute(
                        "SELECT prefix, "
                        "  MAX(last_modified) as latest, "
                        "  CAST((julianday('now') - julianday(MAX(last_modified))) * 24 AS INTEGER) as staleness_hours, "
                        "  COUNT(*) as object_count "
                        "FROM objects "
                        "WHERE key LIKE ? AND depth >= ? "
                        "GROUP BY prefix "
                        "ORDER BY staleness_hours DESC",
                        (prefix + "%", depth),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT prefix, "
                        "  MAX(last_modified) as latest, "
                        "  CAST((julianday('now') - julianday(MAX(last_modified))) * 24 AS INTEGER) as staleness_hours, "
                        "  COUNT(*) as object_count "
                        "FROM objects WHERE prefix != '' "
                        "GROUP BY prefix "
                        "ORDER BY staleness_hours DESC",
                    ).fetchall()

                tc["result_rows"] = len(rows)

                if not rows:
                    return f"No folder data found in bucket '{bucket}'."

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [f"Data freshness for {location}:\n"]

                lines.append("| Folder | Last Updated | Staleness | Objects |")
                lines.append("|--------|-------------|-----------|---------|")

                stale_count = 0
                for r in rows:
                    hours = r["staleness_hours"] or 0
                    if hours < 24:
                        staleness = f"{hours}h ago"
                        status = "fresh"
                    elif hours < 168:
                        staleness = f"{hours // 24}d ago"
                        status = "recent"
                    elif hours < 720:
                        staleness = f"{hours // 24}d ago"
                        status = "aging"
                        stale_count += 1
                    else:
                        staleness = f"{hours // 24}d ago"
                        status = "STALE"
                        stale_count += 1

                    latest = r["latest"][:16] if r["latest"] else "unknown"
                    lines.append(
                        f"| {r['prefix']} | {latest} | {staleness} ({status}) | "
                        f"{format_number(r['object_count'])} |"
                    )

                if stale_count > 0:
                    lines.append(
                        f"\n**{stale_count} folder(s) appear stale** (no updates in 7+ days). "
                        "These may indicate broken pipelines or abandoned data."
                    )
                else:
                    lines.append("\nAll folders have been updated recently.")

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="compare_snapshots",
        description=(
            "Compare the current state of a bucket with its state N days ago. "
            "Shows what was added, how much grew, and the net change in storage. "
            "Use this when the user asks 'what changed this week?', 'why did storage spike?', "
            "or is investigating unexpected growth. "
            "Requires storage history from periodic index crawls."
        ),
        annotations={"readOnlyHint": True},
    )
    async def compare_snapshots(
        bucket: str,
        prefix: Optional[str] = None,
        days_ago: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)
        days_ago = validate_days(days_ago, max_days=365, default=7)

        with track_tool_call("compare_snapshots", user=session.username, bucket=bucket):
            with get_db(bucket) as db:
                if not check_table_exists(db, "storage_history"):
                    return (
                        f"No history data available for bucket '{bucket}'. "
                        "Snapshots are recorded during periodic index crawls."
                    )

                target_prefix = prefix or ""

                # Get current snapshot
                current = db.execute(
                    "SELECT object_count, total_size FROM storage_history "
                    "WHERE prefix = ? ORDER BY timestamp DESC LIMIT 1",
                    (target_prefix,),
                ).fetchone()

                # Get past snapshot
                past = db.execute(
                    "SELECT object_count, total_size, timestamp FROM storage_history "
                    "WHERE prefix = ? AND timestamp <= datetime('now', ?) "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (target_prefix, f"-{days_ago} days"),
                ).fetchone()

                if not current:
                    return f"No current data available for bucket '{bucket}'."

                if not past:
                    return (
                        f"No historical snapshot found from {days_ago} days ago for bucket '{bucket}'. "
                        f"History may not go back that far."
                    )

                curr_objects = current["object_count"] or 0
                curr_size = current["total_size"] or 0
                past_objects = past["object_count"] or 0
                past_size = past["total_size"] or 0
                past_date = past["timestamp"][:10] if past["timestamp"] else f"{days_ago} days ago"

                delta_objects = curr_objects - past_objects
                delta_size = curr_size - past_size

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [
                    f"Changes in {location} over the last {days_ago} days (since {past_date}):\n"
                ]

                lines.append("| Metric | Then | Now | Change |")
                lines.append("|--------|------|-----|--------|")

                obj_change = f"+{format_number(delta_objects)}" if delta_objects >= 0 else format_number(delta_objects)
                size_change = f"+{format_bytes(delta_size)}" if delta_size >= 0 else f"-{format_bytes(abs(delta_size))}"

                lines.append(
                    f"| Objects | {format_number(past_objects)} | {format_number(curr_objects)} | {obj_change} |"
                )
                lines.append(
                    f"| Size | {format_bytes(past_size)} | {format_bytes(curr_size)} | {size_change} |"
                )

                # Growth rate
                if past_size > 0:
                    growth_pct = (delta_size / past_size) * 100
                    lines.append(f"\nGrowth rate: **{growth_pct:+.1f}%** over {days_ago} days")

                    if delta_size > 0:
                        daily_rate = delta_size / days_ago
                        monthly_proj = daily_rate * 30
                        lines.append(f"Projected monthly growth: ~{format_bytes(int(monthly_proj))}")

                # Per-folder breakdown if available
                if not prefix:
                    folder_changes = db.execute(
                        "SELECT sh1.prefix, "
                        "  sh1.total_size as current_size, "
                        "  sh2.total_size as past_size, "
                        "  (sh1.total_size - sh2.total_size) as delta "
                        "FROM "
                        "  (SELECT prefix, total_size FROM storage_history "
                        "   WHERE timestamp = (SELECT MAX(timestamp) FROM storage_history) "
                        "   AND prefix != '') sh1 "
                        "LEFT JOIN "
                        "  (SELECT prefix, total_size FROM storage_history "
                        "   WHERE timestamp <= datetime('now', ?) "
                        "   AND prefix != '' "
                        "   GROUP BY prefix HAVING timestamp = MAX(timestamp)) sh2 "
                        "ON sh1.prefix = sh2.prefix "
                        "WHERE sh2.total_size IS NOT NULL "
                        "ORDER BY delta DESC LIMIT 10",
                        (f"-{days_ago} days",),
                    ).fetchall()

                    if folder_changes:
                        growers = [r for r in folder_changes if (r["delta"] or 0) > 0]
                        if growers:
                            lines.append("\n**Fastest growing folders:**")
                            for r in growers[:5]:
                                delta = r["delta"] or 0
                                lines.append(
                                    f"- {r['prefix']}: +{format_bytes(delta)} "
                                    f"(now {format_bytes(r['current_size'])})"
                                )

                return sanitize_result("\n".join(lines))

"""
Cost intelligence tools: estimate costs, suggest lifecycle rules, find cold data.

These tools translate storage data into money — the language that gets
executive attention and budget approval.
"""

from typing import Optional

from mcp.server.fastmcp import Context

from db import get_db, check_table_exists
from observability import track_tool_call
from pricing import STORAGE_PRICING, calculate_savings, estimate_monthly_cost
from security import (
    format_bytes,
    format_number,
    sanitize_result,
    validate_bucket_name,
    validate_days,
    validate_min_size,
    validate_prefix,
    validate_provider,
)


def _ctx_session(ctx):
    return ctx.request_context.lifespan_context["session"]


def _ctx_auth(ctx):
    return ctx.request_context.lifespan_context["auth"]


def register(mcp):
    """Register all cost tools with the MCP server."""

    @mcp.tool(
        name="estimate_storage_cost",
        description=(
            "Estimate monthly and annual storage costs for a bucket on a specific cloud provider. "
            "Shows costs across all available storage classes so you can see potential savings. "
            "Supports AWS S3, Cloudflare R2, Backblaze B2, Wasabi, MinIO, Ceph, and Leaseweb. "
            "Use this when the user asks 'how much is this costing?', 'what are my storage costs?', "
            "or wants to compare pricing across providers."
        ),
        annotations={"readOnlyHint": True},
    )
    async def estimate_storage_cost(
        bucket: str,
        provider: Optional[str] = None,
        region: Optional[str] = None,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        provider = validate_provider(provider)
        region = (region or "us-east-1").strip().lower()
        prefix = validate_prefix(prefix)

        with track_tool_call("estimate_storage_cost", user=session.username, bucket=bucket):
            with get_db(bucket) as db:
                if prefix:
                    row = db.execute(
                        "SELECT COUNT(*) as cnt, SUM(size) as total FROM objects WHERE key LIKE ?",
                        (prefix + "%",),
                    ).fetchone()
                else:
                    row = db.execute(
                        "SELECT COUNT(*) as cnt, SUM(size) as total FROM objects"
                    ).fetchone()

                total_objects = row["cnt"] or 0
                total_size = row["total"] or 0

                if total_size == 0:
                    return f"Bucket '{bucket}' is empty — no storage costs."

                costs = estimate_monthly_cost(total_size, provider, region)

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [
                    f"Cost estimate for {location} on **{provider.upper()}** ({region}):\n",
                    f"- Objects: {format_number(total_objects)}",
                    f"- Total size: {format_bytes(total_size)}",
                    "",
                ]

                lines.append("| Storage Class | $/GB/mo | Monthly | Annual |")
                lines.append("|---------------|---------|---------|--------|")

                current_class = None
                cheapest_class = None
                cheapest_cost = float("inf")

                for class_name, info in costs.items():
                    monthly = f"${info['monthly_cost']:,.2f}"
                    annual = f"${info['annual_cost']:,.2f}"
                    price = f"${info['price_per_gb_month']:.4f}"

                    marker = ""
                    if class_name == "standard":
                        marker = " (current)"
                        current_class = info
                    if info["monthly_cost"] < cheapest_cost and info["monthly_cost"] > 0:
                        cheapest_cost = info["monthly_cost"]
                        cheapest_class = class_name

                    lines.append(f"| {class_name}{marker} | {price} | {monthly} | {annual} |")

                # Savings insight
                if current_class and cheapest_class and cheapest_class != "standard":
                    savings = current_class["monthly_cost"] - cheapest_cost
                    if savings > 0:
                        lines.append(
                            f"\nMoving all data to **{cheapest_class}** would save "
                            f"**${savings:,.2f}/month** (${savings * 12:,.2f}/year). "
                            f"But check access patterns first — cheaper classes have retrieval costs."
                        )

                if provider == "minio" or provider == "ceph":
                    lines.append(
                        f"\n*{provider.upper()} is self-hosted — the $0 price reflects only "
                        f"storage software costs, not infrastructure (disks, servers, power, etc.).*"
                    )

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="suggest_lifecycle_rules",
        description=(
            "Analyze the data in a bucket and recommend lifecycle rules to reduce costs. "
            "Looks at file age distribution and size patterns to suggest when to transition "
            "data to cheaper storage classes or expire old files. "
            "Use this when the user wants to optimize costs, set up data retention policies, "
            "or asks 'how can I reduce my storage bill?'."
        ),
        annotations={"readOnlyHint": True},
    )
    async def suggest_lifecycle_rules(
        bucket: str,
        provider: Optional[str] = None,
        prefix: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        provider = validate_provider(provider)
        prefix = validate_prefix(prefix)

        with track_tool_call("suggest_lifecycle_rules", user=session.username, bucket=bucket):
            with get_db(bucket) as db:
                where = "WHERE key LIKE ?" if prefix else ""
                params = (prefix + "%",) if prefix else ()

                # Get total size
                total_row = db.execute(
                    f"SELECT COUNT(*) as cnt, SUM(size) as total FROM objects {where}", params
                ).fetchone()
                total_size = total_row["total"] or 0
                total_objects = total_row["cnt"] or 0

                if total_size == 0:
                    return "Bucket is empty — no lifecycle rules needed."

                # Analyze age buckets
                age_data = db.execute(
                    f"SELECT "
                    f"  SUM(CASE WHEN julianday('now') - julianday(last_modified) > 90 "
                    f"    AND julianday('now') - julianday(last_modified) <= 180 "
                    f"    THEN size ELSE 0 END) as cold_90, "
                    f"  SUM(CASE WHEN julianday('now') - julianday(last_modified) > 180 "
                    f"    THEN size ELSE 0 END) as cold_180, "
                    f"  SUM(CASE WHEN julianday('now') - julianday(last_modified) > 365 "
                    f"    THEN size ELSE 0 END) as cold_365, "
                    f"  SUM(CASE WHEN size = 0 THEN 1 ELSE 0 END) as empty_files, "
                    f"  SUM(CASE WHEN size < 128 THEN 1 ELSE 0 END) as tiny_files "
                    f"FROM objects {where}",
                    params,
                ).fetchone()

                cold_90 = age_data["cold_90"] or 0
                cold_180 = age_data["cold_180"] or 0
                cold_365 = age_data["cold_365"] or 0
                empty_files = age_data["empty_files"] or 0
                tiny_files = age_data["tiny_files"] or 0

                suggestions = []
                total_potential_savings = 0

                # Suggestion: transition 90+ day data
                if cold_90 > 0 and provider == "aws":
                    savings = calculate_savings(
                        total_size, cold_90, provider,
                        current_class="standard", target_class="standard_ia"
                    )
                    if savings["monthly_savings"] > 1:
                        total_potential_savings += savings["monthly_savings"]
                        suggestions.append({
                            "rule": f"Transition objects older than 90 days to Standard-IA",
                            "affected_size": cold_90,
                            "monthly_savings": savings["monthly_savings"],
                            "reason": (
                                f"{format_bytes(cold_90)} of data hasn't been modified in 90+ days. "
                                f"Standard-IA is ~45% cheaper for infrequently accessed data."
                            ),
                        })

                # Suggestion: archive 180+ day data
                if cold_180 > 0 and provider == "aws":
                    savings = calculate_savings(
                        total_size, cold_180, provider,
                        current_class="standard", target_class="glacier_instant"
                    )
                    if savings["monthly_savings"] > 1:
                        total_potential_savings += savings["monthly_savings"]
                        suggestions.append({
                            "rule": f"Transition objects older than 180 days to Glacier Instant Retrieval",
                            "affected_size": cold_180,
                            "monthly_savings": savings["monthly_savings"],
                            "reason": (
                                f"{format_bytes(cold_180)} of data is 6+ months old. "
                                f"Glacier Instant gives ~82% savings with millisecond retrieval."
                            ),
                        })

                # Suggestion: deep archive for 1+ year data
                if cold_365 > 0 and provider == "aws":
                    savings = calculate_savings(
                        total_size, cold_365, provider,
                        current_class="standard", target_class="glacier_deep_archive"
                    )
                    if savings["monthly_savings"] > 1:
                        total_potential_savings += savings["monthly_savings"]
                        suggestions.append({
                            "rule": f"Archive objects older than 365 days to Glacier Deep Archive",
                            "affected_size": cold_365,
                            "monthly_savings": savings["monthly_savings"],
                            "reason": (
                                f"{format_bytes(cold_365)} of data is over a year old. "
                                f"Deep Archive is ~95% cheaper but has 12-48 hour retrieval time."
                            ),
                        })

                # Suggestion: clean up empty files
                if empty_files > 100:
                    suggestions.append({
                        "rule": "Delete empty (0-byte) files",
                        "affected_size": 0,
                        "monthly_savings": 0,
                        "reason": (
                            f"{format_number(empty_files)} empty files found. These may be "
                            f"marker files, failed uploads, or stale placeholders."
                        ),
                    })

                # Non-AWS providers
                if provider in ("r2", "b2", "wasabi") and cold_180 > 0:
                    pct = cold_180 / total_size * 100
                    suggestions.append({
                        "rule": f"Consider deleting data older than 180 days if not needed",
                        "affected_size": cold_180,
                        "monthly_savings": 0,
                        "reason": (
                            f"{provider.upper()} has a single storage class, so there's no "
                            f"cheaper tier to move to. {format_bytes(cold_180)} ({pct:.0f}%) "
                            f"of data is 6+ months old."
                        ),
                    })

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [
                    f"Lifecycle recommendations for {location} ({provider.upper()}):\n",
                    f"Total: {format_number(total_objects)} objects, {format_bytes(total_size)}\n",
                ]

                if not suggestions:
                    lines.append(
                        "No significant optimization opportunities found. "
                        "Most data appears to be relatively recent."
                    )
                else:
                    for i, s in enumerate(suggestions, 1):
                        lines.append(f"### {i}. {s['rule']}")
                        lines.append(f"- Affects: {format_bytes(s['affected_size'])}")
                        if s["monthly_savings"] > 0:
                            lines.append(
                                f"- Estimated savings: **${s['monthly_savings']:,.2f}/month** "
                                f"(${s['monthly_savings'] * 12:,.2f}/year)"
                            )
                        lines.append(f"- {s['reason']}")
                        lines.append("")

                    if total_potential_savings > 0:
                        lines.append(
                            f"**Total potential savings: ${total_potential_savings:,.2f}/month "
                            f"(${total_potential_savings * 12:,.2f}/year)**"
                        )

                return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="find_cold_data",
        description=(
            "Find files that haven't been modified in a long time and are candidates "
            "for archival or deletion. Returns the largest old files first. "
            "Use this when the user wants to find 'stale' or 'old' data, free up space, "
            "or identify files that can be moved to cheaper storage."
        ),
        annotations={"readOnlyHint": True},
    )
    async def find_cold_data(
        bucket: str,
        prefix: Optional[str] = None,
        older_than_days: Optional[int] = None,
        min_size: Optional[int] = None,
        limit: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        prefix = validate_prefix(prefix)
        older_than_days = validate_days(older_than_days, max_days=3650, default=90)
        min_size = validate_min_size(min_size, default=1_048_576)
        limit = validate_limit(limit, max_limit=200, default=50)

        with track_tool_call("find_cold_data", user=session.username, bucket=bucket) as tc:
            with get_db(bucket) as db:
                conditions = [
                    "julianday('now') - julianday(last_modified) > ?",
                    "size >= ?",
                ]
                params: list = [older_than_days, min_size]

                if prefix:
                    conditions.append("key LIKE ?")
                    params.append(prefix + "%")

                where = "WHERE " + " AND ".join(conditions)

                # Get summary
                summary = db.execute(
                    f"SELECT COUNT(*) as cnt, SUM(size) as total FROM objects {where}",
                    params,
                ).fetchone()

                total_cold = summary["cnt"] or 0
                total_cold_size = summary["total"] or 0

                # Get individual files
                rows = db.execute(
                    f"SELECT key, size, last_modified, "
                    f"  CAST(julianday('now') - julianday(last_modified) AS INTEGER) as age_days "
                    f"FROM objects {where} "
                    f"ORDER BY size DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()

                tc["result_rows"] = len(rows)

                if not rows:
                    return (
                        f"No files older than {older_than_days} days "
                        f"(and larger than {format_bytes(min_size)}) "
                        f"found in bucket '{bucket}'."
                    )

                location = f"**{bucket}/{prefix}**" if prefix else f"**{bucket}**"
                lines = [
                    f"Cold data in {location} "
                    f"(older than {older_than_days} days, larger than {format_bytes(min_size)}):\n",
                    f"**{format_number(total_cold)} files, {format_bytes(total_cold_size)} total**\n",
                    f"Top {len(rows)} largest cold files:\n",
                ]

                for r in rows:
                    lines.append(
                        f"- `{r['key']}` — {format_bytes(r['size'])}, "
                        f"{r['age_days']} days old"
                    )

                if total_cold > len(rows):
                    lines.append(
                        f"\n(Showing {len(rows)} of {format_number(total_cold)} cold files)"
                    )

                return sanitize_result("\n".join(lines))

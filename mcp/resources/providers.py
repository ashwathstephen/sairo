"""
MCP Resources: ambient context the AI can reference without tool calls.

Resources are loaded by the AI client to build background context.
The overview resource is especially important — it lets the AI know
what buckets exist so users never have to specify bucket names.
"""

from db import get_db, list_bucket_dbs, check_table_exists
from observability import logger
from security import format_bytes, format_number, sanitize_result


def register(mcp):
    """Register all resources with the MCP server."""

    @mcp.resource(
        "objex://overview",
        name="Storage Overview",
        description=(
            "Complete overview of all storage buckets: names, sizes, object counts, "
            "and index status. This resource gives the AI full context about the user's "
            "storage infrastructure so they never need to specify bucket names."
        ),
    )
    async def storage_overview() -> str:
        bucket_dbs = list_bucket_dbs()
        if not bucket_dbs:
            return "No buckets found. The storage system may not be indexed yet."

        total_objects = 0
        total_size = 0
        buckets_info = []

        for bdb in bucket_dbs:
            info = {"name": bdb["bucket"], "endpoint": bdb["endpoint_id"]}
            try:
                with get_db(bdb["bucket"], bdb["endpoint_id"]) as db:
                    if check_table_exists(db, "crawl_status"):
                        row = db.execute(
                            "SELECT total_objects, total_size, status, last_crawl_end "
                            "FROM crawl_status WHERE id=1"
                        ).fetchone()
                        if row:
                            info["objects"] = row["total_objects"] or 0
                            info["size"] = row["total_size"] or 0
                            info["status"] = row["status"] or "unknown"
                            info["last_indexed"] = row["last_crawl_end"] or "never"
                            total_objects += info["objects"]
                            total_size += info["size"]
            except Exception:
                info["status"] = "error"

            buckets_info.append(info)

        lines = [
            "# Storage Overview\n",
            f"**{len(buckets_info)} buckets**, "
            f"**{format_number(total_objects)} total objects**, "
            f"**{format_bytes(total_size)} total storage**\n",
            "| Bucket | Objects | Size | Status |",
            "|--------|---------|------|--------|",
        ]

        for b in sorted(buckets_info, key=lambda x: x.get("size", 0), reverse=True):
            objects = format_number(b.get("objects", 0))
            size = format_bytes(b.get("size", 0))
            status = b.get("status", "unknown")
            ep = f" ({b['endpoint']})" if b["endpoint"] != "default" else ""
            lines.append(f"| {b['name']}{ep} | {objects} | {size} | {status} |")

        return sanitize_result("\n".join(lines))

    @mcp.resource(
        "objex://{bucket}/summary",
        name="Bucket Summary",
        description=(
            "Detailed summary of a specific bucket including top folders, "
            "recent growth, file type breakdown, and index status."
        ),
    )
    async def bucket_summary(bucket: str) -> str:
        try:
            with get_db(bucket) as db:
                lines = [f"# Bucket: {bucket}\n"]

                # Basic stats
                if check_table_exists(db, "crawl_status"):
                    row = db.execute(
                        "SELECT total_objects, total_size, status, last_crawl_end "
                        "FROM crawl_status WHERE id=1"
                    ).fetchone()
                    if row:
                        lines.extend([
                            f"- Objects: {format_number(row['total_objects'] or 0)}",
                            f"- Size: {format_bytes(row['total_size'] or 0)}",
                            f"- Index status: {row['status'] or 'unknown'}",
                            f"- Last indexed: {row['last_crawl_end'] or 'never'}",
                            "",
                        ])

                # Top folders
                if check_table_exists(db, "folder_stats"):
                    folders = db.execute(
                        "SELECT prefix, object_count, total_size "
                        "FROM folder_stats ORDER BY total_size DESC LIMIT 10"
                    ).fetchall()
                    if folders:
                        lines.append("## Top Folders\n")
                        for f in folders:
                            lines.append(
                                f"- {f['prefix']}: "
                                f"{format_number(f['object_count'])} objects, "
                                f"{format_bytes(f['total_size'])}"
                            )
                        lines.append("")

                # Recent growth
                if check_table_exists(db, "storage_history"):
                    history = db.execute(
                        "SELECT total_size FROM storage_history "
                        "WHERE prefix = '' ORDER BY timestamp DESC LIMIT 2"
                    ).fetchall()
                    if len(history) >= 2:
                        delta = (history[0]["total_size"] or 0) - (history[1]["total_size"] or 0)
                        if delta != 0:
                            direction = "grew" if delta > 0 else "shrank"
                            lines.append(
                                f"## Recent Change\n"
                                f"Storage {direction} by {format_bytes(abs(delta))} "
                                f"since last index.\n"
                            )

                # FTS availability
                has_fts = check_table_exists(db, "objects_fts")
                lines.append(
                    f"Search: {'available' if has_fts else 'not indexed'}"
                )

                return sanitize_result("\n".join(lines))

        except FileNotFoundError:
            return f"Bucket '{bucket}' has not been indexed yet."

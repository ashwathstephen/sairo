"""
Inspection tools: read file contents, extract schemas, sample data.

These tools let the AI look inside files without the user needing
to download or open anything manually.
"""

import csv
import io
import json
from typing import Optional

from mcp.server.fastmcp import Context

from db import get_db
from observability import track_tool_call
from security import (
    format_bytes,
    sanitize_file_content,
    sanitize_result,
    validate_bucket_name,
    validate_limit,
    validate_object_key,
    validate_sample_rows,
)


def _ctx_session(ctx):
    return ctx.request_context.lifespan_context["session"]


def _ctx_auth(ctx):
    return ctx.request_context.lifespan_context["auth"]


def _ctx_sairo(ctx):
    return ctx.request_context.lifespan_context["sairo"]


def register(mcp):
    """Register all inspection tools with the MCP server."""

    @mcp.tool(
        name="get_object_metadata",
        description=(
            "Get detailed metadata about a specific file: size, last modified date, "
            "ETag, content type, and storage class. Use this when the user asks about "
            "a specific file's properties, when it was last changed, or how big it is."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_object_metadata(
        bucket: str,
        key: str,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        key = validate_object_key(key)
        _ctx_auth(ctx).require_bucket_read(session, bucket)

        with track_tool_call("get_object_metadata", user=session.username, bucket=bucket):
            # Try local DB first for basic info
            info = {}
            try:
                with get_db(bucket) as db:
                    row = db.execute(
                        "SELECT key, size, last_modified, etag FROM objects WHERE key = ?",
                        (key,),
                    ).fetchone()
                    if row:
                        info = {
                            "key": row["key"],
                            "size": row["size"],
                            "size_human": format_bytes(row["size"]),
                            "last_modified": row["last_modified"],
                            "etag": row["etag"],
                        }
            except (FileNotFoundError, TimeoutError):
                pass

            # Enrich with S3 HeadObject for content-type, storage class
            try:
                s3_info = await _ctx_sairo(ctx).get_object_info(bucket, key, user_token=session.token)
                info.update({
                    k: v for k, v in s3_info.items()
                    if k in ("content_type", "storage_class", "version_id", "metadata")
                })
                if not info.get("size") and s3_info.get("size"):
                    info["size"] = s3_info["size"]
                    info["size_human"] = format_bytes(s3_info["size"])
            except Exception:
                pass

            if not info:
                return f"File not found: '{key}' in bucket '{bucket}'."

            lines = [f"**{info.get('key', key)}**\n"]
            if info.get("size_human"):
                lines.append(f"- Size: {info['size_human']} ({info.get('size', 0):,} bytes)")
            if info.get("last_modified"):
                lines.append(f"- Last modified: {info['last_modified']}")
            if info.get("content_type"):
                lines.append(f"- Content type: {info['content_type']}")
            if info.get("storage_class"):
                lines.append(f"- Storage class: {info['storage_class']}")
            if info.get("etag"):
                lines.append(f"- ETag: {info['etag']}")
            if info.get("version_id"):
                lines.append(f"- Version ID: {info['version_id']}")

            return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="read_object_content",
        description=(
            "Read the text content of a file from S3 storage. Works with text files, "
            "logs, JSON, CSV, YAML, XML, Markdown, code files, and any other text-based format. "
            "Returns the first portion of the file (up to 512KB by default). "
            "Use this when the user wants to see what's inside a file, check a config, "
            "read a log, or inspect any text-based data."
        ),
        annotations={"readOnlyHint": True},
    )
    async def read_object_content(
        bucket: str,
        key: str,
        max_bytes: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        key = validate_object_key(key)
        _ctx_auth(ctx).require_bucket_read(session, bucket)

        if max_bytes is None:
            max_bytes = 524288  # 512KB
        max_bytes = min(max_bytes, 5 * 1024 * 1024)  # Cap at 5MB

        with track_tool_call("read_object_content", user=session.username, bucket=bucket):
            try:
                result = await _ctx_sairo(ctx).preview_object(
                    bucket, key, max_bytes=max_bytes, user_token=session.token
                )
            except Exception as e:
                return f"Could not read '{key}': {str(e)}"

            content = result.get("content", "")
            if not content:
                return f"File '{key}' appears to be empty or binary."

            truncated = result.get("truncated", False)
            total_size = result.get("total_size", 0)

            header = f"**{key}**"
            if total_size:
                header += f" ({format_bytes(total_size)})"
            if truncated:
                header += f" — showing first {format_bytes(max_bytes)}"

            return sanitize_file_content(f"{header}\n\n```\n{content}\n```")

    @mcp.tool(
        name="read_object_tail",
        description=(
            "Read the END of a log file or text file. Shows the most recent lines. "
            "Use this when the user wants to check recent log entries, see the latest "
            "output of a running process, or look at the bottom of a large file. "
            "Much more useful than read_object_content for log files."
        ),
        annotations={"readOnlyHint": True},
    )
    async def read_object_tail(
        bucket: str,
        key: str,
        max_bytes: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        key = validate_object_key(key)
        _ctx_auth(ctx).require_bucket_read(session, bucket)

        if max_bytes is None:
            max_bytes = 65536  # 64KB
        max_bytes = min(max_bytes, 1 * 1024 * 1024)  # Cap at 1MB

        with track_tool_call("read_object_tail", user=session.username, bucket=bucket):
            try:
                result = await _ctx_sairo(ctx).preview_tail(
                    bucket, key, max_bytes=max_bytes, user_token=session.token
                )
            except Exception as e:
                return f"Could not read tail of '{key}': {str(e)}"

            content = result.get("content", "")
            if not content:
                return f"File '{key}' appears to be empty or binary."

            total_size = result.get("total_size", 0)
            header = f"**{key}** (last {format_bytes(max_bytes)})"
            if total_size:
                header += f" of {format_bytes(total_size)} total"

            return sanitize_file_content(f"{header}\n\n```\n{content}\n```")

    @mcp.tool(
        name="get_file_schema",
        description=(
            "Extract the column schema from a Parquet, ORC, or Avro data file. "
            "Returns column names, data types, row count, compression, and file structure "
            "details WITHOUT downloading the full file. "
            "Use this when the user asks about the structure of a data file, wants to know "
            "what columns are in a dataset, or is investigating a data pipeline's output format."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_file_schema(
        bucket: str,
        key: str,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        key = validate_object_key(key)
        _ctx_auth(ctx).require_bucket_read(session, bucket)

        # Validate file extension
        lower_key = key.lower()
        if not any(lower_key.endswith(ext) for ext in (".parquet", ".orc", ".avro")):
            return (
                f"'{key}' doesn't appear to be a columnar data file. "
                "This tool works with .parquet, .orc, and .avro files. "
                "For other file types, try read_object_content."
            )

        with track_tool_call("get_file_schema", user=session.username, bucket=bucket):
            try:
                result = await _ctx_sairo(ctx).get_file_metadata(
                    bucket, key, user_token=session.token
                )
            except Exception as e:
                return f"Could not extract schema from '{key}': {str(e)}"

            fmt = result.get("format", "unknown")
            columns = result.get("columns", [])
            row_count = result.get("row_count", "unknown")
            compression = result.get("compression", "unknown")

            lines = [f"**{key}** — {fmt.upper()} schema\n"]

            if row_count != "unknown":
                lines.append(f"- Rows: {row_count:,}" if isinstance(row_count, int) else f"- Rows: {row_count}")
            if compression != "unknown":
                lines.append(f"- Compression: {compression}")

            row_groups = result.get("row_groups") or result.get("stripes")
            if row_groups:
                label = "Row groups" if "row_groups" in result else "Stripes"
                lines.append(f"- {label}: {row_groups}")

            if columns:
                lines.append(f"\n**Columns** ({len(columns)}):\n")
                lines.append("| # | Name | Type |")
                lines.append("|---|------|------|")
                for i, col in enumerate(columns, 1):
                    name = col.get("name", f"col_{i}")
                    dtype = col.get("type", col.get("physical_type", "unknown"))
                    lines.append(f"| {i} | {name} | {dtype} |")
            else:
                lines.append("\nNo column information available.")

            return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="sample_csv_data",
        description=(
            "Read the first few rows of a CSV file as a formatted table. "
            "Shows headers and sample data so you can understand the file's contents. "
            "Use this when the user wants to see actual data from a CSV, "
            "understand what a CSV contains, or check the format of a data file."
        ),
        annotations={"readOnlyHint": True},
    )
    async def sample_csv_data(
        bucket: str,
        key: str,
        rows: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        key = validate_object_key(key)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        rows = validate_sample_rows(rows)

        with track_tool_call("sample_csv_data", user=session.username, bucket=bucket) as tc:
            try:
                # Fetch enough content for the requested rows
                max_bytes = min(rows * 2048, 1 * 1024 * 1024)  # ~2KB per row, max 1MB
                result = await _ctx_sairo(ctx).preview_object(
                    bucket, key, max_bytes=max_bytes, user_token=session.token
                )
            except Exception as e:
                return f"Could not read '{key}': {str(e)}"

            content = result.get("content", "")
            if not content:
                return f"File '{key}' appears to be empty."

            try:
                reader = csv.reader(io.StringIO(content))
                all_rows = []
                for i, row in enumerate(reader):
                    all_rows.append(row)
                    if i >= rows:  # +1 for header
                        break
            except csv.Error as e:
                return f"Could not parse '{key}' as CSV: {str(e)}"

            if not all_rows:
                return f"File '{key}' appears to be empty."

            headers = all_rows[0]
            data_rows = all_rows[1:]
            tc["result_rows"] = len(data_rows)

            total_size = result.get("total_size", 0)
            lines = [
                f"**{key}** — CSV with {len(headers)} columns"
                + (f", {format_bytes(total_size)}" if total_size else "")
                + f"\n\nShowing {len(data_rows)} of {rows} requested rows:\n"
            ]

            # Build markdown table
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
            for row in data_rows:
                # Pad or truncate to match header count
                padded = row[:len(headers)] + [""] * max(0, len(headers) - len(row))
                # Truncate long cell values
                padded = [v[:50] + "..." if len(v) > 50 else v for v in padded]
                lines.append("| " + " | ".join(padded) + " |")

            return sanitize_result("\n".join(lines))

    @mcp.tool(
        name="sample_json_data",
        description=(
            "Read and display sample data from a JSON or JSONL (newline-delimited JSON) file. "
            "For JSON: shows the structure and first few records. "
            "For JSONL: shows the first few lines as individual records. "
            "Use this when the user wants to inspect JSON data, understand API output "
            "format, or check what a JSON data file contains."
        ),
        annotations={"readOnlyHint": True},
    )
    async def sample_json_data(
        bucket: str,
        key: str,
        records: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        session = _ctx_session(ctx)
        bucket = validate_bucket_name(bucket)
        key = validate_object_key(key)
        _ctx_auth(ctx).require_bucket_read(session, bucket)
        records = validate_sample_rows(records, default=10, max_rows=50)

        with track_tool_call("sample_json_data", user=session.username, bucket=bucket) as tc:
            try:
                max_bytes = min(records * 4096, 1 * 1024 * 1024)
                result = await _ctx_sairo(ctx).preview_object(
                    bucket, key, max_bytes=max_bytes, user_token=session.token
                )
            except Exception as e:
                return f"Could not read '{key}': {str(e)}"

            content = result.get("content", "")
            if not content:
                return f"File '{key}' appears to be empty."

            content = content.strip()

            # Detect format: JSON array, JSON object, or JSONL
            if content.startswith("["):
                # JSON array
                try:
                    data = json.loads(content)
                    if isinstance(data, list):
                        sample = data[:records]
                        tc["result_rows"] = len(sample)
                        formatted = json.dumps(sample, indent=2, default=str)
                        return sanitize_result(
                            f"**{key}** — JSON array ({len(data)} records total, "
                            f"showing {len(sample)})\n\n```json\n{formatted}\n```"
                        )
                except json.JSONDecodeError:
                    pass

            if content.startswith("{"):
                # Single JSON object or JSONL
                lines_raw = content.split("\n")
                parsed = []
                for line in lines_raw:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed.append(json.loads(line))
                    except json.JSONDecodeError:
                        break
                    if len(parsed) >= records:
                        break

                tc["result_rows"] = len(parsed)

                if len(parsed) == 1 and not content.count("\n"):
                    # Single JSON object
                    formatted = json.dumps(parsed[0], indent=2, default=str)
                    return sanitize_result(
                        f"**{key}** — JSON object\n\n```json\n{formatted}\n```"
                    )
                else:
                    # JSONL
                    formatted = "\n".join(
                        json.dumps(obj, default=str) for obj in parsed
                    )
                    return sanitize_result(
                        f"**{key}** — JSONL (showing {len(parsed)} records)\n\n"
                        f"```json\n{formatted}\n```"
                    )

            return sanitize_result(
                f"**{key}** — Could not parse as JSON.\n\n"
                f"First 500 chars:\n```\n{content[:500]}\n```"
            )

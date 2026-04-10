"""
Security layer for MCP server.

Handles input validation, output sanitization, and request verification.
Designed to mitigate every known MCP attack vector (CVE-2025/2026 era).
"""

import re
from typing import Optional

# Maximum lengths for tool results returned to the LLM
MAX_RESULT_TEXT_LENGTH = 100_000
MAX_RESULT_ROWS = 1000
MAX_PREVIEW_BYTES = 5 * 1024 * 1024  # 5MB

# Query timeout for SQLite (seconds)
QUERY_TIMEOUT_MS = 5000

# Bucket name: alphanumeric, dots, hyphens, 3-63 chars (S3 spec)
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")

# General safe name: alphanumeric, dots, hyphens, underscores
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# Characters that could be used for prompt injection in tool results
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Patterns that look like prompt injection attempts in returned data
_INJECTION_PATTERNS = [
    re.compile(r"<\s*/?\s*(?:system|user|assistant|tool)\s*>", re.IGNORECASE),
    re.compile(r"\[INST\]|\[/INST\]", re.IGNORECASE),
    re.compile(r"<<\s*SYS\s*>>|<<\s*/SYS\s*>>", re.IGNORECASE),
]

# Allowed sort fields per context
ALLOWED_SORT_FIELDS = {
    "objects": {"name", "key", "size", "date", "last_modified"},
    "top_objects": {"size", "date"},
}

ALLOWED_SORT_ORDERS = {"asc", "desc"}

ALLOWED_PROVIDERS = {"aws", "r2", "b2", "wasabi", "minio", "ceph", "leaseweb"}


class ValidationError(Exception):
    """Raised when input validation fails."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def validate_bucket_name(name: str) -> str:
    """
    Validate and return a bucket name.
    S3 bucket naming rules: 3-63 chars, lowercase alphanumeric + dots + hyphens.
    """
    if not name or not isinstance(name, str):
        raise ValidationError("Bucket name is required")

    name = name.strip().lower()

    if ".." in name:
        raise ValidationError("Bucket name cannot contain '..'")

    if not _BUCKET_RE.match(name):
        raise ValidationError(
            f"Invalid bucket name: '{name}'. "
            "Must be 3-63 chars, lowercase alphanumeric, dots, and hyphens."
        )

    return name


def validate_prefix(prefix: Optional[str]) -> str:
    """
    Validate and normalize an S3 prefix (folder path).
    Returns empty string if None/empty.
    """
    if not prefix:
        return ""

    if not isinstance(prefix, str):
        raise ValidationError("Prefix must be a string")

    # Block null bytes
    if "\x00" in prefix:
        raise ValidationError("Prefix cannot contain null bytes")

    # Block path traversal
    if ".." in prefix:
        raise ValidationError("Prefix cannot contain '..'")

    # Normalize: strip leading slash, ensure trailing slash if non-empty
    prefix = prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    # Length check
    if len(prefix) > 1024:
        raise ValidationError("Prefix too long (max 1024 chars)")

    return prefix


def validate_object_key(key: str) -> str:
    """Validate an S3 object key."""
    if not key or not isinstance(key, str):
        raise ValidationError("Object key is required")

    if "\x00" in key:
        raise ValidationError("Object key cannot contain null bytes")

    if ".." in key.split("/"):
        raise ValidationError("Object key cannot contain '..' path segments")

    if len(key) > 1024:
        raise ValidationError("Object key too long (max 1024 chars)")

    return key


def validate_sort_field(field: Optional[str], context: str = "objects") -> Optional[str]:
    """Validate sort field against an allowlist."""
    if not field:
        return None

    field = field.strip().lower()
    allowed = ALLOWED_SORT_FIELDS.get(context, set())

    if field not in allowed:
        raise ValidationError(
            f"Invalid sort field: '{field}'. Allowed: {', '.join(sorted(allowed))}"
        )

    return field


def validate_sort_order(order: Optional[str]) -> str:
    """Validate sort order."""
    if not order:
        return "asc"

    order = order.strip().lower()

    if order not in ALLOWED_SORT_ORDERS:
        raise ValidationError("Sort order must be 'asc' or 'desc'")

    return order


def validate_limit(limit: Optional[int], max_limit: int = 1000, default: int = 100) -> int:
    """Validate a result limit."""
    if limit is None:
        return default

    if not isinstance(limit, int) or limit < 1:
        raise ValidationError("Limit must be a positive integer")

    return min(limit, max_limit)


def validate_days(days: Optional[int], max_days: int = 365, default: int = 90) -> int:
    """Validate a days parameter."""
    if days is None:
        return default

    if not isinstance(days, int) or days < 1:
        raise ValidationError("Days must be a positive integer")

    return min(days, max_days)


def validate_provider(provider: Optional[str]) -> str:
    """Validate a storage provider name."""
    if not provider:
        return "aws"

    provider = provider.strip().lower()

    if provider not in ALLOWED_PROVIDERS:
        raise ValidationError(
            f"Unknown provider: '{provider}'. "
            f"Supported: {', '.join(sorted(ALLOWED_PROVIDERS))}"
        )

    return provider


def validate_search_query(query: str) -> str:
    """Validate a search query string."""
    if not query or not isinstance(query, str):
        raise ValidationError("Search query is required")

    query = query.strip()

    if len(query) < 2:
        raise ValidationError("Search query must be at least 2 characters")

    if len(query) > 200:
        raise ValidationError("Search query too long (max 200 chars)")

    # Strip any FTS5 operators that could be injected
    query = re.sub(r'["\*\(\)\{\}\[\]]', "", query)

    return query


def validate_min_size(min_size: Optional[int], default: int = 1_048_576) -> int:
    """Validate a minimum file size in bytes."""
    if min_size is None:
        return default

    if not isinstance(min_size, int) or min_size < 0:
        raise ValidationError("min_size must be a non-negative integer")

    return min_size


def validate_sample_rows(rows: Optional[int], default: int = 20, max_rows: int = 100) -> int:
    """Validate number of sample rows."""
    if rows is None:
        return default

    if not isinstance(rows, int) or rows < 1:
        raise ValidationError("Rows must be a positive integer")

    return min(rows, max_rows)


def sanitize_result(text: str, max_length: int = MAX_RESULT_TEXT_LENGTH) -> str:
    """
    Sanitize text before returning it as a tool result to the LLM.

    - Strip control characters
    - Escape potential prompt injection patterns
    - Truncate to max length
    """
    if not text:
        return ""

    # Strip control characters (keep newlines, tabs, carriage returns)
    text = _CONTROL_CHARS_RE.sub("", text)

    # Replace patterns that could be interpreted as prompt injection
    # We fully replace the tag to ensure the original text cannot be parsed
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub("[sanitized-tag]", text)

    # Truncate
    if len(text) > max_length:
        text = text[:max_length] + f"\n\n... (truncated, showing first {max_length} chars)"

    return text


def sanitize_file_content(content: str, max_bytes: int = MAX_PREVIEW_BYTES) -> str:
    """Sanitize file content returned from S3 previews."""
    if not content:
        return ""

    # Encode to check byte length
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        content = encoded[:max_bytes].decode("utf-8", errors="replace")
        content += "\n\n... (truncated to size limit)"

    return sanitize_result(content)


def format_bytes(size: int) -> str:
    """Format a byte count as a human-readable string."""
    if size < 0:
        return "0 B"

    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(size) < 1024.0:
            if unit == "B":
                return f"{size} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0

    return f"{size:.1f} EB"


def format_number(n: int) -> str:
    """Format a number with comma separators."""
    return f"{n:,}"

"""
Authentication and authorization for the MCP server.

Phase 1: API token validation against Sairo's auth system.
Phase 2 (future): Full OAuth 2.1 resource server.

Every tool call is gated by:
1. Valid authentication (token resolves to a user)
2. Bucket-level authorization (user has permission for the target bucket)
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from sairo_client import SairoClient
from observability import logger


@dataclass
class UserSession:
    """Cached user session with permissions."""

    username: str
    role: str  # "admin" or "viewer"
    token: str
    bucket_permissions: dict[str, str] = field(default_factory=dict)
    cached_at: float = field(default_factory=time.monotonic)

    # Cache permissions for 5 minutes
    CACHE_TTL = 300

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_stale(self) -> bool:
        return (time.monotonic() - self.cached_at) > self.CACHE_TTL

    def can_read_bucket(self, bucket: str) -> bool:
        """Check if user has read access to a bucket."""
        if self.is_admin:
            return True
        perm = self.bucket_permissions.get(bucket)
        return perm in ("read", "write")

    def can_write_bucket(self, bucket: str) -> bool:
        """Check if user has write access to a bucket."""
        if self.is_admin:
            return True
        return self.bucket_permissions.get(bucket) == "write"


class AuthorizationError(Exception):
    """Raised when a user lacks permission for an operation."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class AuthManager:
    """
    Manages authentication and authorization for MCP sessions.

    Validates tokens against the Sairo API and caches user sessions
    to avoid repeated auth calls on every tool invocation.
    """

    def __init__(self, sairo_client: SairoClient):
        self._client = sairo_client
        self._sessions: dict[str, UserSession] = {}
        # Max cached sessions to prevent memory growth
        self._max_sessions = 100

    async def authenticate(self, token: str) -> UserSession:
        """
        Validate a token and return a UserSession.
        Uses cache if available and not stale.
        """
        if not token:
            raise AuthorizationError("Authentication required. Provide a Sairo API token.")

        # Check cache
        cached = self._sessions.get(token)
        if cached and not cached.is_stale:
            return cached

        # Validate against Sairo API
        user_info = await self._client.validate_token(token)
        if not user_info:
            # Clear stale cache entry
            self._sessions.pop(token, None)
            raise AuthorizationError(
                "Invalid or expired token. Generate a new API token in Sairo's admin panel."
            )

        username = user_info.get("username", "")
        role = user_info.get("role", "viewer")

        # Fetch bucket permissions for non-admin users
        permissions = {}
        if role != "admin":
            permissions = await self._client.get_user_permissions(username)

        session = UserSession(
            username=username,
            role=role,
            token=token,
            bucket_permissions=permissions,
        )

        # Evict oldest if at capacity
        if len(self._sessions) >= self._max_sessions:
            oldest_key = min(self._sessions, key=lambda k: self._sessions[k].cached_at)
            del self._sessions[oldest_key]

        self._sessions[token] = session

        logger.info(
            f"Authenticated user: {username} (role={role})",
            extra={"user": username},
        )

        return session

    def require_bucket_read(self, session: UserSession, bucket: str):
        """Raise AuthorizationError if user can't read the bucket."""
        if not session.can_read_bucket(bucket):
            raise AuthorizationError(
                f"You don't have read access to bucket '{bucket}'. "
                "Ask your Sairo admin to grant you permissions."
            )

    def require_bucket_write(self, session: UserSession, bucket: str):
        """Raise AuthorizationError if user can't write to the bucket."""
        if not session.can_write_bucket(bucket):
            raise AuthorizationError(
                f"You don't have write access to bucket '{bucket}'. "
                "This operation requires write permissions."
            )

    def require_admin(self, session: UserSession):
        """Raise AuthorizationError if user is not an admin."""
        if not session.is_admin:
            raise AuthorizationError(
                "This operation requires admin privileges."
            )

    def invalidate(self, token: str):
        """Remove a session from cache."""
        self._sessions.pop(token, None)

    def invalidate_all(self):
        """Clear all cached sessions."""
        self._sessions.clear()

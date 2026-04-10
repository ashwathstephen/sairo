"""
Async HTTP client for the Sairo API.

Used for operations that must go through the main Sairo server:
- S3 object previews and downloads (requires S3 client)
- Crawl triggers (requires write access to index DBs)
- Audit log queries (centralized in users.db)
- Auth token validation
"""

import os
from typing import Any, Optional

import httpx

SAIRO_API_URL = os.environ.get("SAIRO_API_URL", "http://localhost:8000")
SAIRO_API_TOKEN = os.environ.get("SAIRO_API_TOKEN", "")
REQUEST_TIMEOUT = float(os.environ.get("MCP_REQUEST_TIMEOUT", "30"))


class SairoClient:
    """Async client for the Sairo REST API."""

    def __init__(
        self,
        base_url: str = SAIRO_API_URL,
        service_token: str = SAIRO_API_TOKEN,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
        )

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self, user_token: Optional[str] = None) -> dict[str, str]:
        """Build request headers. Use user token if provided, else service token."""
        token = user_token or self.service_token
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _endpoint_prefix(self, endpoint_id: Optional[str] = None) -> str:
        """Build the endpoint routing prefix."""
        if endpoint_id and endpoint_id != "default":
            return f"/api/e/{endpoint_id}"
        return "/api"

    async def _request(
        self,
        method: str,
        path: str,
        user_token: Optional[str] = None,
        **kwargs,
    ) -> httpx.Response:
        """Make an authenticated request to the Sairo API."""
        if not self._client:
            raise RuntimeError("SairoClient not started. Call start() first.")

        resp = await self._client.request(
            method, path, headers=self._headers(user_token), **kwargs
        )
        return resp

    # --- Auth ---

    async def validate_token(self, token: str) -> Optional[dict]:
        """
        Validate a token against Sairo's auth endpoint.
        Returns user info {username, role} or None if invalid.
        """
        try:
            resp = await self._request("GET", "/api/auth/me", user_token=token)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    async def get_user_permissions(self, username: str) -> dict[str, str]:
        """
        Get bucket permissions for a user.
        Returns {bucket_name: permission_level} dict.
        """
        try:
            resp = await self._request(
                "GET", f"/api/auth/users/{username}/permissions"
            )
            if resp.status_code == 200:
                data = resp.json()
                # Normalize to {bucket: permission} dict
                if isinstance(data, list):
                    return {p["bucket"]: p["permission"] for p in data}
                return data
            return {}
        except Exception:
            return {}

    # --- Object Operations ---

    async def preview_object(
        self,
        bucket: str,
        key: str,
        max_bytes: int = 524288,
        endpoint_id: Optional[str] = None,
        user_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Get a text preview of an object."""
        prefix = self._endpoint_prefix(endpoint_id)
        resp = await self._request(
            "GET",
            f"{prefix}/buckets/{bucket}/preview",
            params={"key": key, "max_bytes": max_bytes},
            user_token=user_token,
        )
        resp.raise_for_status()
        return resp.json()

    async def preview_tail(
        self,
        bucket: str,
        key: str,
        max_bytes: int = 65536,
        endpoint_id: Optional[str] = None,
        user_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Get the tail of a log/text file."""
        prefix = self._endpoint_prefix(endpoint_id)
        resp = await self._request(
            "GET",
            f"{prefix}/buckets/{bucket}/preview-tail",
            params={"key": key, "max_bytes": max_bytes},
            user_token=user_token,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_file_metadata(
        self,
        bucket: str,
        key: str,
        endpoint_id: Optional[str] = None,
        user_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Get schema metadata for Parquet/ORC/Avro files."""
        prefix = self._endpoint_prefix(endpoint_id)
        resp = await self._request(
            "GET",
            f"{prefix}/buckets/{bucket}/file-metadata",
            params={"key": key},
            user_token=user_token,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_object_info(
        self,
        bucket: str,
        key: str,
        endpoint_id: Optional[str] = None,
        user_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Get S3 object metadata (size, date, etag, content-type)."""
        prefix = self._endpoint_prefix(endpoint_id)
        resp = await self._request(
            "GET",
            f"{prefix}/buckets/{bucket}/object-info",
            params={"key": key},
            user_token=user_token,
        )
        resp.raise_for_status()
        return resp.json()

    # --- Crawl Operations ---

    async def trigger_crawl(
        self,
        bucket: str,
        endpoint_id: Optional[str] = None,
        user_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Trigger a full re-index of a bucket."""
        prefix = self._endpoint_prefix(endpoint_id)
        resp = await self._request(
            "POST",
            f"{prefix}/buckets/{bucket}/crawl",
            user_token=user_token,
        )
        resp.raise_for_status()
        return resp.json()

    # --- Audit Log ---

    async def get_audit_log(
        self,
        bucket: Optional[str] = None,
        username: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 50,
        user_token: Optional[str] = None,
    ) -> list[dict]:
        """Query the audit log."""
        params: dict[str, Any] = {"limit": limit}
        if bucket:
            params["bucket"] = bucket
        if username:
            params["username"] = username
        if action:
            params["action"] = action

        resp = await self._request(
            "GET", "/api/audit-log", params=params, user_token=user_token
        )
        resp.raise_for_status()
        data = resp.json()
        # Handle both list and {"entries": [...]} response formats
        if isinstance(data, dict) and "entries" in data:
            return data["entries"]
        return data

    # --- Health ---

    async def health_check(self) -> bool:
        """Check if the Sairo API is healthy."""
        try:
            resp = await self._request("GET", "/healthz")
            return resp.status_code == 200
        except Exception:
            return False

    # --- Buckets ---

    async def list_buckets(
        self,
        endpoint_id: Optional[str] = None,
        user_token: Optional[str] = None,
    ) -> list[dict]:
        """List buckets from the Sairo API."""
        prefix = self._endpoint_prefix(endpoint_id)
        resp = await self._request(
            "GET", f"{prefix}/buckets", user_token=user_token
        )
        resp.raise_for_status()
        return resp.json()

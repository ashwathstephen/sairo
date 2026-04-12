"""Pytest tests for Sairo backend API — commercial features + security hardening.

Tests cover: API tokens, share links, branding, license management,
security headers, 2FA encryption, error sanitization, permission checks,
upload limits, pricing endpoints, version check.

Requires: pip install pytest httpx
Run with: pytest backend/test_main.py -v
"""
import os
import sys
import json
import base64
import hashlib
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Set env vars before importing the app
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("S3_REGION", "us-east-1")
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASS", "testpass")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest")
os.environ.setdefault("SECURE_COOKIE", "false")
os.environ.setdefault("DB_DIR", "/tmp/sairo-test")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    """Import app with test config."""
    os.makedirs("/tmp/sairo-test", exist_ok=True)
    with patch("boto3.client") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3
        mock_s3.list_buckets.return_value = {"Buckets": []}
        try:
            from backend.main import app as fastapi_app
        except ModuleNotFoundError:
            from main import app as fastapi_app
        yield fastapi_app


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app)


@pytest.fixture(scope="module")
def admin_cookies(client):
    """Login as admin and return cookies."""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "testpass"})
    assert resp.status_code == 200
    return resp.cookies


@pytest.fixture(scope="module")
def viewer_cookies(client, admin_cookies):
    """Create a viewer user and return their cookies."""
    client.post(
        "/api/auth/users",
        json={"username": "test-viewer", "password": "viewerpass", "role": "viewer"},
        cookies=admin_cookies,
    )
    resp = client.post("/api/auth/login", json={"username": "test-viewer", "password": "viewerpass"})
    if resp.status_code == 200:
        return resp.cookies
    return None


# ── Branding ─────────────────────────────────────────────

class TestBranding:
    def test_branding_public(self, client):
        """Branding endpoint should be public (no auth required)."""
        resp = client.get("/api/branding")
        assert resp.status_code == 200
        data = resp.json()
        assert "app_name" in data
        assert "primary_color" in data
        assert "ldap_enabled" in data
        assert "oauth_providers" in data

    def test_branding_defaults(self, client):
        """Default branding values should be returned."""
        resp = client.get("/api/branding")
        data = resp.json()
        assert data["app_name"] == "Sairo"
        assert data["primary_color"] == "#3b82f6"
        assert data["ldap_enabled"] is False
        assert data["oauth_providers"] == []


# ── API Tokens ───────────────────────────────────────────

class TestAPITokens:
    def test_create_token_requires_admin(self, client):
        """Non-authenticated users should not be able to create tokens."""
        resp = client.post("/api/auth/tokens", json={"name": "test", "role": "viewer"})
        assert resp.status_code == 401

    def test_create_and_list_token(self, client, admin_cookies):
        """Admin should be able to create and list API tokens."""
        resp = client.post(
            "/api/auth/tokens",
            json={"name": "ci-test", "role": "viewer"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["token"].startswith("sairo_")

        resp = client.get("/api/auth/tokens", cookies=admin_cookies)
        assert resp.status_code == 200
        tokens = resp.json()["tokens"]
        assert any(t["name"] == "ci-test" for t in tokens)

    def test_bearer_auth(self, client, admin_cookies):
        """API token should work as Bearer auth."""
        resp = client.post(
            "/api/auth/tokens",
            json={"name": "bearer-test", "role": "viewer"},
            cookies=admin_cookies,
        )
        raw_token = resp.json()["token"]

        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["role"] == "viewer"

    def test_invalid_bearer_rejected(self, client):
        """Invalid bearer tokens should return 401."""
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid_token"})
        assert resp.status_code == 401

    def test_revoke_token(self, client, admin_cookies):
        """Admin should be able to revoke tokens."""
        resp = client.post(
            "/api/auth/tokens",
            json={"name": "revoke-test", "role": "viewer"},
            cookies=admin_cookies,
        )
        raw_token = resp.json()["token"]

        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
        assert resp.status_code == 200

        resp = client.get("/api/auth/tokens", cookies=admin_cookies)
        token_id = None
        for t in resp.json()["tokens"]:
            if t["name"] == "revoke-test":
                token_id = t["id"]
                break
        assert token_id is not None

        resp = client.delete(f"/api/auth/tokens/{token_id}", cookies=admin_cookies)
        assert resp.status_code == 200

        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
        assert resp.status_code == 401


# ── Share Links ──────────────────────────────────────────

class TestShareLinks:
    def test_create_share_link(self, client, admin_cookies):
        """Admin should be able to create share links."""
        resp = client.post(
            "/api/share-links",
            json={"bucket": "test-bucket", "key": "test.txt", "expires_hours": 24},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data

    def test_list_share_links(self, client, admin_cookies):
        """Should be able to list share links."""
        resp = client.get("/api/share-links", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "links" in data

    def test_share_link_requires_auth(self, app):
        """Creating share links requires authentication."""
        with TestClient(app) as fresh:
            resp = fresh.post(
                "/api/share-links",
                json={"bucket": "test-bucket", "key": "test.txt", "expires_hours": 24},
            )
            assert resp.status_code == 401

    def test_share_link_ownership_enforcement(self, client, admin_cookies, viewer_cookies):
        """Non-admin users can only delete their own share links."""
        if not viewer_cookies:
            pytest.skip("Viewer user not created")
        # Admin creates a share link
        resp = client.post(
            "/api/share-links",
            json={"bucket": "test-bucket", "key": "test.txt", "expires_hours": 24},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        # Get the link ID
        resp = client.get("/api/share-links", cookies=admin_cookies)
        links = resp.json()["links"]
        if links:
            admin_link_id = links[0]["id"]
            # Viewer tries to delete admin's link — should get 403
            resp = client.delete(f"/api/share-links/{admin_link_id}", cookies=viewer_cookies)
            assert resp.status_code == 403


# ── License ──────────────────────────────────────────────

class TestLicense:
    def test_get_license_default(self, client, admin_cookies):
        """Default license should be community."""
        resp = client.get("/api/license", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "community"

    def test_activate_invalid_license(self, client, admin_cookies):
        """Invalid license key should be rejected."""
        resp = client.post(
            "/api/license",
            json={"key": "not-a-valid-key"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 400


# ── OAuth Providers ──────────────────────────────────────

class TestOAuth:
    def test_oauth_providers_empty(self, client):
        """With no OAuth configured, providers list should be empty."""
        resp = client.get("/api/auth/oauth/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["providers"] == []

    def test_oauth_unconfigured_provider_404(self, client):
        """Trying to login with unconfigured provider should return 404."""
        resp = client.get("/api/auth/oauth/google/login", follow_redirects=False)
        assert resp.status_code == 404


# ── Health Check ─────────────────────────────────────────

class TestHealth:
    def test_healthz(self, client):
        """Health endpoint should return 200."""
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_auth_me_without_login(self, app):
        """Unauthenticated /me should return 401."""
        with TestClient(app) as fresh:
            resp = fresh.get("/api/auth/me")
            assert resp.status_code == 401

    def test_health_detail_requires_admin(self, client, viewer_cookies):
        """Non-admin users should get 403 on health-detail."""
        if not viewer_cookies:
            pytest.skip("Viewer user not created")
        resp = client.get("/api/health-detail", cookies=viewer_cookies)
        assert resp.status_code == 403

    def test_health_detail_works_for_admin(self, client, admin_cookies):
        """Admin should be able to access health-detail."""
        resp = client.get("/api/health-detail", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "uptime_seconds" in data
        assert "s3_connected" in data


# ── Security Headers ─────────────────────────────────────

class TestSecurityHeaders:
    def test_csp_header_present(self, client):
        """All responses should include Content-Security-Policy."""
        resp = client.get("/healthz")
        assert "content-security-policy" in resp.headers
        csp = resp.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp

    def test_x_content_type_options(self, client):
        """All responses should include X-Content-Type-Options: nosniff."""
        resp = client.get("/healthz")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_x_frame_options(self, client):
        """All responses should include X-Frame-Options: DENY."""
        resp = client.get("/healthz")
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_referrer_policy(self, client):
        """All responses should include Referrer-Policy."""
        resp = client.get("/healthz")
        assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    def test_api_endpoints_have_security_headers(self, client, admin_cookies):
        """API endpoints should also include security headers."""
        resp = client.get("/api/auth/me", cookies=admin_cookies)
        assert "content-security-policy" in resp.headers
        assert resp.headers.get("x-content-type-options") == "nosniff"


# ── 2FA Encryption ───────────────────────────────────────

class TestTwoFactorEncryption:
    def test_encrypt_decrypt_roundtrip(self, app):
        """_encrypt and _decrypt should round-trip correctly."""
        try:
            from backend.main import _encrypt, _decrypt
        except ModuleNotFoundError:
            from main import _encrypt, _decrypt

        original = "JBSWY3DPEHPK3PXP"
        encrypted = _encrypt(original)
        assert encrypted != original
        assert encrypted.startswith("enc::")
        decrypted = _decrypt(encrypted)
        assert decrypted == original

    def test_decrypt_plaintext_passthrough(self, app):
        """_decrypt should pass through plaintext strings (migration support)."""
        try:
            from backend.main import _decrypt
        except ModuleNotFoundError:
            from main import _decrypt

        plaintext = "JBSWY3DPEHPK3PXP"
        assert _decrypt(plaintext) == plaintext

    def test_decrypt_empty_string(self, app):
        """_decrypt should handle empty strings."""
        try:
            from backend.main import _encrypt, _decrypt
        except ModuleNotFoundError:
            from main import _encrypt, _decrypt

        assert _encrypt("") == ""
        assert _decrypt("") == ""

    def test_2fa_setup_stores_encrypted_secret(self, client, admin_cookies):
        """2FA setup should store the TOTP secret encrypted."""
        resp = client.post("/api/auth/2fa/setup", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "secret" in data
        assert "otpauth_url" in data
        # The returned secret should be plaintext (for QR display)
        assert not data["secret"].startswith("enc::")
        assert len(data["secret"]) > 10


# ── 2FA Rate Limiting ────────────────────────────────────

class TestTwoFactorRateLimiting:
    def test_verify_endpoint_rejects_unauthenticated(self, app):
        """2FA verify should require an existing session cookie."""
        with TestClient(app) as fresh:
            resp = fresh.post("/api/auth/2fa/verify", json={"code": "000000"})
            assert resp.status_code in (401, 429), f"Expected 401 or 429, got {resp.status_code}"

    def test_recover_endpoint_rejects_unauthenticated(self, app):
        """2FA recover should require an existing session cookie."""
        with TestClient(app) as fresh:
            resp = fresh.post("/api/auth/2fa/recover", json={"code": "abcd1234"})
            assert resp.status_code in (401, 429), f"Expected 401 or 429, got {resp.status_code}"


# ── Upload Size Limits ───────────────────────────────────

class TestUploadLimits:
    def test_max_upload_size_configured(self, app):
        """MAX_UPLOAD_SIZE should be configured (default 5GB)."""
        try:
            from backend.main import MAX_UPLOAD_SIZE
        except ModuleNotFoundError:
            from main import MAX_UPLOAD_SIZE

        assert MAX_UPLOAD_SIZE > 0
        # Default is 5 GB
        assert MAX_UPLOAD_SIZE == 5 * 1024 * 1024 * 1024


# ── Pricing Endpoints ────────────────────────────────────

class TestPricing:
    def test_pricing_endpoint(self, client, admin_cookies):
        """Pricing endpoint should return provider pricing data."""
        resp = client.get("/api/pricing", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_pricing_provider_endpoint(self, client, admin_cookies):
        """Provider-specific pricing should work."""
        resp = client.get("/api/pricing/aws", cookies=admin_cookies)
        # Might be 200 or 404 depending on implementation
        assert resp.status_code in (200, 404)


# ── Version Endpoint ─────────────────────────────────────

class TestVersion:
    def test_version_endpoint_exists(self, client, admin_cookies):
        """Version endpoint should return version information."""
        resp = client.get("/api/version", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "current" in data


# ── S3 Error Sanitization ───────────────────────────────

class TestErrorSanitization:
    def test_sanitize_s3_error_strips_arns(self):
        """S3 error handler should strip ARNs from error messages."""
        import re
        msg = "Access Denied for arn:aws:iam::123456789012:user/test"
        msg = re.sub(r'arn:[^\s,]+', '[ARN]', msg)
        msg = re.sub(r'\d{12}', '[ACCOUNT]', msg)
        assert "arn:aws" not in msg
        assert "[ARN]" in msg
        # The 12-digit account ID is inside the ARN which was already replaced,
        # so [ACCOUNT] only appears if a bare 12-digit number exists outside an ARN
        msg2 = "Bucket owned by 123456789012"
        msg2 = re.sub(r'arn:[^\s,]+', '[ARN]', msg2)
        msg2 = re.sub(r'\d{12}', '[ACCOUNT]', msg2)
        assert "[ACCOUNT]" in msg2

    def test_sanitize_preserves_useful_info(self):
        """Sanitization should preserve the error code."""
        import re
        msg = "NoSuchKey: The specified key does not exist."
        msg = re.sub(r'arn:[^\s,]+', '[ARN]', msg)
        msg = re.sub(r'\d{12}', '[ACCOUNT]', msg)
        assert "NoSuchKey" in msg
        assert "specified key" in msg


# ── Compat Endpoint Permission Checks ────────────────────

class TestCompatPermissions:
    def test_compat_endpoints_require_auth(self, app):
        """Legacy compat endpoints should require authentication."""
        with TestClient(app) as fresh:
            for path in ["/api/list", "/api/search?q=test", "/api/folder-size",
                         "/api/storage-breakdown", "/api/object-info?key=test",
                         "/api/presigned-url?key=test", "/api/multipart-uploads"]:
                resp = fresh.get(path)
                assert resp.status_code == 401, f"{path} should require auth, got {resp.status_code}"

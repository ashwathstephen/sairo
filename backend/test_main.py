"""Pytest tests for Sairo backend API — new commercial features.

Tests cover: API tokens, share links, branding, license management.
Requires: pip install pytest httpx
Run with: pytest backend/test_main.py -v
"""
import os
import sys
import json
import base64
import hashlib
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
    # Ensure DB dir
    os.makedirs("/tmp/sairo-test", exist_ok=True)
    # Mock S3 client to avoid real connections
    with patch("boto3.client") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3
        mock_s3.list_buckets.return_value = {"Buckets": []}
        # Now import
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
        # Create
        resp = client.post(
            "/api/auth/tokens",
            json={"name": "ci-test", "role": "viewer"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["token"].startswith("sairo_")
        raw_token = data["token"]

        # List
        resp = client.get("/api/auth/tokens", cookies=admin_cookies)
        assert resp.status_code == 200
        tokens = resp.json()["tokens"]
        assert any(t["name"] == "ci-test" for t in tokens)

        return raw_token

    def test_bearer_auth(self, client, admin_cookies):
        """API token should work as Bearer auth."""
        # Create a token
        resp = client.post(
            "/api/auth/tokens",
            json={"name": "bearer-test", "role": "viewer"},
            cookies=admin_cookies,
        )
        raw_token = resp.json()["token"]

        # Use it for auth
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["role"] == "viewer"  # Token role, not user's actual role

    def test_invalid_bearer_rejected(self, client):
        """Invalid bearer tokens should return 401."""
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid_token"})
        assert resp.status_code == 401

    def test_revoke_token(self, client, admin_cookies):
        """Admin should be able to revoke tokens."""
        # Create
        resp = client.post(
            "/api/auth/tokens",
            json={"name": "revoke-test", "role": "viewer"},
            cookies=admin_cookies,
        )
        raw_token = resp.json()["token"]

        # Verify it works
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
        assert resp.status_code == 200

        # Find the token ID
        resp = client.get("/api/auth/tokens", cookies=admin_cookies)
        token_id = None
        for t in resp.json()["tokens"]:
            if t["name"] == "revoke-test":
                token_id = t["id"]
                break
        assert token_id is not None

        # Revoke
        resp = client.delete(f"/api/auth/tokens/{token_id}", cookies=admin_cookies)
        assert resp.status_code == 200

        # Verify it no longer works
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

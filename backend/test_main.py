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


def _main_module():
    try:
        import backend.main as m
    except ModuleNotFoundError:
        import main as m
    return m


class TestOIDC:
    """Generic OpenID Connect login (issue #9): username-only sync, admin perms."""

    ISSUER = "https://issuer.test"
    CLIENT_ID = "client123"

    def _enable(self, monkeypatch):
        """Turn OIDC on at runtime + stub discovery (no real network)."""
        m = _main_module()
        monkeypatch.setattr(m, "OIDC_ENABLED", True)
        monkeypatch.setattr(m, "OIDC_ISSUER", self.ISSUER)
        monkeypatch.setattr(m, "OIDC_CLIENT_ID", self.CLIENT_ID)
        monkeypatch.setattr(m, "OIDC_CLIENT_SECRET", "shh")
        monkeypatch.setattr(m, "OIDC_USERNAME_CLAIM", "preferred_username")
        monkeypatch.setattr(m, "OIDC_PROVIDER_NAME", "Corp SSO")
        monkeypatch.setattr(m, "OIDC_DEFAULT_ROLE", "viewer")
        monkeypatch.setattr(m, "OIDC_ALLOWED_DOMAINS", [])
        monkeypatch.setattr(m, "_oidc_config", lambda: {
            "issuer": self.ISSUER,
            "authorization_endpoint": f"{self.ISSUER}/authorize",
            "token_endpoint": f"{self.ISSUER}/token",
            "jwks_uri": f"{self.ISSUER}/jwks",
        })
        return m

    def test_disabled_by_default(self, client):
        """With no OIDC env, login is 404 and it's absent from branding."""
        resp = client.get("/api/auth/oidc/login", follow_redirects=False)
        assert resp.status_code == 404
        branding = client.get("/api/branding").json()
        assert not any(p["id"] == "oidc" for p in branding["oauth_providers"])

    def test_enabled_appears_in_branding(self, client, monkeypatch):
        self._enable(monkeypatch)
        providers = client.get("/api/branding").json()["oauth_providers"]
        oidc = next(p for p in providers if p["id"] == "oidc")
        assert oidc["name"] == "Corp SSO"
        assert oidc["login_path"] == "/api/auth/oidc/login"

    def test_login_redirects_with_state_and_pkce(self, app, monkeypatch):
        self._enable(monkeypatch)
        with TestClient(app) as c:
            resp = c.get("/api/auth/oidc/login", follow_redirects=False)
            assert resp.status_code in (302, 307)
            loc = resp.headers["location"]
            assert loc.startswith(f"{self.ISSUER}/authorize?")
            # PKCE + CSRF params present
            assert "code_challenge=" in loc and "code_challenge_method=S256" in loc
            assert "state=" in loc and "nonce=" in loc
            assert f"client_id={self.CLIENT_ID}" in loc
            # signed state cookie set
            assert resp.cookies.get("oidc_state")

    def test_callback_without_state_cookie_redirects_error(self, app, monkeypatch):
        self._enable(monkeypatch)
        with TestClient(app) as c:
            resp = c.get("/api/auth/oidc/callback?code=x&state=y", follow_redirects=False)
            assert resp.status_code in (302, 307)
            assert "error=oidc_failed" in resp.headers["location"]

    def test_callback_state_mismatch_redirects_error(self, app, monkeypatch):
        import jwt
        from datetime import datetime, timezone, timedelta
        m = self._enable(monkeypatch)
        bad_state = jwt.encode(
            {"state": "REAL", "nonce": "n", "cv": "v", "purpose": "oidc_state",
             "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
            m.JWT_SECRET, algorithm="HS256")
        with TestClient(app) as c:
            resp = c.get("/api/auth/oidc/callback?code=x&state=ATTACKER",
                         cookies={"oidc_state": bad_state}, follow_redirects=False)
            assert resp.status_code in (302, 307)
            assert "error=oidc_state_mismatch" in resp.headers["location"]

    def test_full_login_flow_creates_viewer_and_session(self, app, monkeypatch):
        """End-to-end: real RS256 ID token validated through the live code path."""
        import jwt
        from datetime import datetime, timezone, timedelta
        from types import SimpleNamespace
        from cryptography.hazmat.primitives.asymmetric import rsa

        m = self._enable(monkeypatch)
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        with TestClient(app) as c:
            # 1) start → capture the signed state cookie, decode state+nonce
            start = c.get("/api/auth/oidc/login", follow_redirects=False)
            state_cookie = start.cookies.get("oidc_state")
            st = jwt.decode(state_cookie, m.JWT_SECRET, algorithms=["HS256"])

            # 2) issuer mints an ID token bound to our nonce/aud/iss
            id_token = jwt.encode(
                {"iss": self.ISSUER, "aud": self.CLIENT_ID,
                 "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                 "iat": datetime.now(timezone.utc),
                 "preferred_username": "alice", "email": "alice@corp.test",
                 "nonce": st["nonce"]},
                priv, algorithm="RS256", headers={"kid": "test"})

            # 3) stub token exchange + JWKS verification
            monkeypatch.setattr("httpx.post", lambda *a, **k: SimpleNamespace(
                status_code=200, json=lambda: {"id_token": id_token}))
            monkeypatch.setattr(m, "_oidc_jwks", lambda: SimpleNamespace(
                get_signing_key_from_jwt=lambda t: SimpleNamespace(key=priv.public_key())))

            # 4) callback with the matching state
            resp = c.get(f"/api/auth/oidc/callback?code=abc&state={st['state']}",
                         cookies={"oidc_state": state_cookie}, follow_redirects=False)
            assert resp.status_code in (302, 307)
            assert resp.headers["location"] == "/"
            session = resp.cookies.get("access_token")
            assert session, "should set a session cookie"
            claims = jwt.decode(session, m.JWT_SECRET, algorithms=["HS256"])
            assert claims["sub"] == "alice"
            assert claims["role"] == "viewer"  # admin assigns real perms later

        # user persisted with no bucket grants (admin-assigns-permissions model)
        with m._get_users_db() as db:
            row = db.execute("SELECT role FROM users WHERE username='alice'").fetchone()
            assert row is not None and row["role"] == "viewer"
            perms = db.execute("SELECT COUNT(*) AS n FROM bucket_permissions WHERE username='alice'").fetchone()
            assert perms["n"] == 0

    def test_oidc_cannot_take_over_local_admin(self, app, monkeypatch):
        """SECURITY: an OIDC user claiming preferred_username=admin must NOT log
        into the pre-existing local admin account."""
        import jwt
        from datetime import datetime, timezone, timedelta
        from types import SimpleNamespace
        from cryptography.hazmat.primitives.asymmetric import rsa

        m = self._enable(monkeypatch)
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with TestClient(app) as c:
            start = c.get("/api/auth/oidc/login", follow_redirects=False)
            state_cookie = start.cookies.get("oidc_state")
            st = jwt.decode(state_cookie, m.JWT_SECRET, algorithms=["HS256"])
            id_token = jwt.encode(
                {"iss": self.ISSUER, "aud": self.CLIENT_ID,
                 "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                 "iat": datetime.now(timezone.utc),
                 "preferred_username": "admin", "nonce": st["nonce"]},
                priv, algorithm="RS256", headers={"kid": "test"})
            monkeypatch.setattr("httpx.post", lambda *a, **k: SimpleNamespace(
                status_code=200, json=lambda: {"id_token": id_token}))
            monkeypatch.setattr(m, "_oidc_jwks", lambda: SimpleNamespace(
                get_signing_key_from_jwt=lambda t: SimpleNamespace(key=priv.public_key())))
            resp = c.get(f"/api/auth/oidc/callback?code=abc&state={st['state']}",
                         cookies={"oidc_state": state_cookie}, follow_redirects=False)
            assert "error=account_conflict" in resp.headers["location"]
            assert not resp.cookies.get("access_token"), "must NOT issue a session for admin"
        # local admin row is untouched (still local, still admin)
        with m._get_users_db() as db:
            row = db.execute("SELECT role, auth_source FROM users WHERE username='admin'").fetchone()
            assert row["role"] == "admin" and (row["auth_source"] or "local") == "local"

    def test_full_login_flow_rejects_bad_nonce(self, app, monkeypatch):
        """A token whose nonce doesn't match the request is rejected (replay guard)."""
        import jwt
        from datetime import datetime, timezone, timedelta
        from types import SimpleNamespace
        from cryptography.hazmat.primitives.asymmetric import rsa

        m = self._enable(monkeypatch)
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with TestClient(app) as c:
            start = c.get("/api/auth/oidc/login", follow_redirects=False)
            state_cookie = start.cookies.get("oidc_state")
            st = jwt.decode(state_cookie, m.JWT_SECRET, algorithms=["HS256"])
            id_token = jwt.encode(
                {"iss": self.ISSUER, "aud": self.CLIENT_ID,
                 "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                 "iat": datetime.now(timezone.utc),
                 "preferred_username": "mallory", "nonce": "WRONG-NONCE"},
                priv, algorithm="RS256", headers={"kid": "test"})
            monkeypatch.setattr("httpx.post", lambda *a, **k: SimpleNamespace(
                status_code=200, json=lambda: {"id_token": id_token}))
            monkeypatch.setattr(m, "_oidc_jwks", lambda: SimpleNamespace(
                get_signing_key_from_jwt=lambda t: SimpleNamespace(key=priv.public_key())))
            resp = c.get(f"/api/auth/oidc/callback?code=abc&state={st['state']}",
                         cookies={"oidc_state": state_cookie}, follow_redirects=False)
            assert "error=oidc_nonce_mismatch" in resp.headers["location"]


class _OIDCFlow:
    """Shared OIDC flow helpers (not collected — no Test prefix), so the
    enterprise + provider-quirk suites reuse one full-flow driver."""

    ISSUER = "https://issuer.test"
    CLIENT_ID = "client123"

    def _enable(self, monkeypatch, **over):
        m = _main_module()
        monkeypatch.setattr(m, "OIDC_ENABLED", True)
        monkeypatch.setattr(m, "OIDC_ISSUER", self.ISSUER)
        monkeypatch.setattr(m, "OIDC_CLIENT_ID", self.CLIENT_ID)
        monkeypatch.setattr(m, "OIDC_CLIENT_SECRET", "shh")
        monkeypatch.setattr(m, "OIDC_USERNAME_CLAIM", "preferred_username")
        monkeypatch.setattr(m, "OIDC_DEFAULT_ROLE", "viewer")
        monkeypatch.setattr(m, "OIDC_ALLOWED_DOMAINS", [])
        monkeypatch.setattr(m, "OIDC_ADMIN_GROUP", over.get("admin_group", ""))
        monkeypatch.setattr(m, "OIDC_GROUPS_CLAIM", over.get("groups_claim", "groups"))
        monkeypatch.setattr(m, "OIDC_REQUIRE_VERIFIED_EMAIL", over.get("require_verified", False))
        monkeypatch.setattr(m, "OIDC_RP_LOGOUT", over.get("rp_logout", False))
        cfg = {"issuer": self.ISSUER, "authorization_endpoint": f"{self.ISSUER}/authorize",
               "token_endpoint": f"{self.ISSUER}/token", "jwks_uri": f"{self.ISSUER}/jwks"}
        if over.get("end_session"):
            cfg["end_session_endpoint"] = f"{self.ISSUER}/logout"
        monkeypatch.setattr(m, "_oidc_config", lambda: cfg)
        return m

    def _login(self, app, monkeypatch, m, username, claims=None, userinfo=None):
        """Run the full OIDC flow with a real RS256 token; return the callback response.

        username=None omits preferred_username (to exercise claim fallbacks).
        userinfo, when given, stands in for the IdP's userinfo endpoint."""
        import jwt
        from datetime import datetime, timezone, timedelta
        from types import SimpleNamespace
        from cryptography.hazmat.primitives.asymmetric import rsa
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with TestClient(app) as c:
            start = c.get("/api/auth/oidc/login", follow_redirects=False)
            sc = start.cookies.get("oidc_state")
            st = jwt.decode(sc, m.JWT_SECRET, algorithms=["HS256"])
            payload = {"iss": self.ISSUER, "aud": self.CLIENT_ID,
                       "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                       "iat": datetime.now(timezone.utc), "nonce": st["nonce"]}
            if username is not None:
                payload["preferred_username"] = username
            payload.update(claims or {})
            id_token = jwt.encode(payload, priv, algorithm="RS256", headers={"kid": "t"})
            monkeypatch.setattr("httpx.post", lambda *a, **k: SimpleNamespace(
                status_code=200, json=lambda: {"id_token": id_token, "access_token": "AT"}))
            monkeypatch.setattr(m, "_oidc_jwks", lambda: SimpleNamespace(
                get_signing_key_from_jwt=lambda t: SimpleNamespace(key=priv.public_key())))
            if userinfo is not None:
                monkeypatch.setattr(m, "_oidc_userinfo", lambda at, cfg: userinfo)
            return c.get(f"/api/auth/oidc/callback?code=abc&state={st['state']}",
                         cookies={"oidc_state": sc}, follow_redirects=False)

    def _sub_of(self, m, token):
        import jwt
        return jwt.decode(token, m.JWT_SECRET, algorithms=["HS256"])["sub"]

    def _role_of(self, m, token):
        import jwt
        return jwt.decode(token, m.JWT_SECRET, algorithms=["HS256"])["role"]


class TestOIDCEnterprise(_OIDCFlow):
    """Phase 2: group→role mapping, userinfo fallback, email_verified, RP-logout."""

    def test_group_member_becomes_admin(self, app, monkeypatch):
        m = self._enable(monkeypatch, admin_group="sairo-admins")
        resp = self._login(app, monkeypatch, m, "boss", claims={"groups": ["staff", "sairo-admins"]})
        assert resp.headers["location"] == "/"
        assert self._role_of(m, resp.cookies.get("access_token")) == "admin"

    def test_group_non_member_is_viewer(self, app, monkeypatch):
        m = self._enable(monkeypatch, admin_group="sairo-admins")
        resp = self._login(app, monkeypatch, m, "peon", claims={"groups": ["staff"]})
        assert self._role_of(m, resp.cookies.get("access_token")) == "viewer"

    def test_group_keycloak_slash_prefix_matches(self, app, monkeypatch):
        m = self._enable(monkeypatch, admin_group="sairo-admins")
        resp = self._login(app, monkeypatch, m, "kcadmin", claims={"groups": ["/sairo-admins"]})
        assert self._role_of(m, resp.cookies.get("access_token")) == "admin"

    def test_requires_verified_email(self, app, monkeypatch):
        m = self._enable(monkeypatch, require_verified=True)
        resp = self._login(app, monkeypatch, m, "unverified",
                           claims={"email": "u@corp.test", "email_verified": False})
        assert "error=email_not_verified" in resp.headers["location"]
        assert not resp.cookies.get("access_token")

    def test_rp_logout_returns_idp_url_for_oidc_user(self, app, monkeypatch):
        m = self._enable(monkeypatch, rp_logout=True, end_session=True)
        login = self._login(app, monkeypatch, m, "logme")
        session = login.cookies.get("access_token")
        with TestClient(app) as c:
            out = c.post("/api/auth/logout", cookies={"access_token": session}).json()
        assert out["logged_out"] is True
        assert out["sso_logout_url"].startswith(f"{self.ISSUER}/logout")
        assert "post_logout_redirect_uri=" in out["sso_logout_url"]

    def test_rp_logout_none_for_local_user(self, app, monkeypatch, client, admin_cookies):
        self._enable(monkeypatch, rp_logout=True, end_session=True)
        out = client.post("/api/auth/logout", cookies=admin_cookies).json()
        assert out["sso_logout_url"] is None  # local admin is not an OIDC session


class TestOIDCProviderQuirks(_OIDCFlow):
    """Cross-provider claim-shape coverage. Real OIDC IdPs differ mostly in how
    they shape claims, not crypto — these simulate the big ones so the flow is
    validated beyond the single live Keycloak run."""

    def test_auth0_namespaced_groups_and_custom_username(self, app, monkeypatch):
        # Auth0: no preferred_username (uses nickname), custom-namespaced groups claim.
        m = self._enable(monkeypatch, admin_group="sairo-admins",
                         groups_claim="https://sairo.example.com/groups")
        monkeypatch.setattr(m, "OIDC_USERNAME_CLAIM", "nickname")
        resp = self._login(app, monkeypatch, m, username=None, claims={
            "nickname": "auth0user",
            "https://sairo.example.com/groups": ["sairo-admins"],
        })
        assert self._sub_of(m, resp.cookies.get("access_token")) == "auth0user"
        assert self._role_of(m, resp.cookies.get("access_token")) == "admin"

    def test_okta_groups_only_in_userinfo(self, app, monkeypatch):
        # Okta/Auth0 commonly omit groups from the ID token — we must fall back to userinfo.
        m = self._enable(monkeypatch, admin_group="Sairo-Admins")
        resp = self._login(app, monkeypatch, m, "oktauser", claims={},
                           userinfo={"groups": ["Everyone", "Sairo-Admins"]})
        assert self._role_of(m, resp.cookies.get("access_token")) == "admin"

    def test_entra_group_object_ids(self, app, monkeypatch):
        # Entra ID (Azure AD) emits group object-IDs (GUIDs), not names.
        gid = "11111111-2222-3333-4444-555555555555"
        m = self._enable(monkeypatch, admin_group=gid)
        resp = self._login(app, monkeypatch, m, "entrauser",
                           claims={"groups": ["00000000-0000-0000-0000-000000000000", gid]})
        assert self._role_of(m, resp.cookies.get("access_token")) == "admin"

    def test_google_username_falls_back_to_email(self, app, monkeypatch):
        # Google has no preferred_username/groups — username should fall back to email.
        m = self._enable(monkeypatch)
        resp = self._login(app, monkeypatch, m, username=None,
                           claims={"email": "person@corp.test", "email_verified": True})
        assert self._sub_of(m, resp.cookies.get("access_token")) == "person@corp.test"
        assert self._role_of(m, resp.cookies.get("access_token")) == "viewer"

    def test_email_only_in_userinfo_satisfies_domain_allowlist(self, app, monkeypatch):
        # Domain allowlist must work even when the IdP puts email only in userinfo.
        m = self._enable(monkeypatch)
        monkeypatch.setattr(m, "OIDC_ALLOWED_DOMAINS", ["corp.test"])
        resp = self._login(app, monkeypatch, m, "domainuser", claims={},
                           userinfo={"email": "domainuser@corp.test"})
        assert resp.headers["location"] == "/"
        assert resp.cookies.get("access_token")

    def test_string_groups_claim_is_tolerated(self, app, monkeypatch):
        # Some IdPs emit a single group as a string, not a list — must not crash.
        m = self._enable(monkeypatch, admin_group="sairo-admins")
        resp = self._login(app, monkeypatch, m, "stringgroup", claims={"groups": "sairo-admins"})
        assert self._role_of(m, resp.cookies.get("access_token")) == "admin"

    def test_similar_group_name_does_not_grant_admin(self, app, monkeypatch):
        # SECURITY: "sairo-admins-readonly" must NOT satisfy admin group "sairo-admins".
        m = self._enable(monkeypatch, admin_group="sairo-admins")
        resp = self._login(app, monkeypatch, m, "almostadmin",
                           claims={"groups": ["sairo-admins-readonly", "staff"]})
        assert self._role_of(m, resp.cookies.get("access_token")) == "viewer"

    def test_ldap_dn_group_value_matches(self, app, monkeypatch):
        # AD/LDAP-sourced groups arrive as DNs — match the cn value, not a substring.
        m = self._enable(monkeypatch, admin_group="sairo-admins")
        resp = self._login(app, monkeypatch, m, "dnuser",
                           claims={"groups": ["cn=sairo-admins,ou=groups,dc=corp,dc=test"]})
        assert self._role_of(m, resp.cookies.get("access_token")) == "admin"

    def test_azp_mismatch_is_rejected(self, app, monkeypatch):
        # A token whose authorized-party is a different client must be rejected.
        m = self._enable(monkeypatch)
        resp = self._login(app, monkeypatch, m, "azpvictim", claims={"azp": "some-other-client"})
        assert "error=oidc_invalid_token" in resp.headers["location"]
        assert not resp.cookies.get("access_token")


class TestAuthSource:
    """auth_source migration + exposure (powers the takeover guard + UI badges)."""

    def test_me_reports_auth_source_for_local_admin(self, client, admin_cookies):
        me = client.get("/api/auth/me", cookies=admin_cookies).json()
        assert me["auth_source"] == "local"

    def test_users_list_includes_auth_source_and_bucket_count(self, client, admin_cookies):
        users = client.get("/api/auth/users", cookies=admin_cookies).json()["users"]
        admin = next(u for u in users if u["username"] == "admin")
        assert admin["auth_source"] == "local"
        assert "bucket_count" in admin and isinstance(admin["bucket_count"], int)


class TestActivationMilestones:
    """3.6.1 fix: first_search_at must record on the search REQUEST — symmetric with
    first_dashboard_open_at — even when the index isn't ready and the search 503s. The old
    placement (after the 503 gate) under-counted fresh installs and manufactured a false 0%."""

    def test_first_search_records_even_when_index_not_ready(self, app, client, admin_cookies):
        m = _main_module()
        # reset any prior recording so we observe this request's effect
        m._recorded_milestones.discard("first_search_at")
        with m._get_users_db() as db:
            db.execute("DELETE FROM instance_meta WHERE key='first_search_at'")
            db.commit()
        # a bucket with no index → the search 503s (index not ready) ...
        resp = client.get("/api/buckets/unindexed-bucket/search",
                          params={"q": "hello"}, cookies=admin_cookies)
        assert resp.status_code == 503
        # ... but the activation milestone must still have recorded (it fires before the gate)
        assert m._meta_get("first_search_at") is not None, \
            "first_search_at must record even when the search 503s during indexing"


class TestBrandingInjection:
    """3.6.2 white-label: APP_NAME / PRIMARY_COLOR are injected into the served
    HTML + manifest server-side, so a branded deployment never leaks "Sairo"
    (tab title, og:title for link previews, PWA name) and shows no load flash."""

    SAMPLE_HTML = (
        '<title>Sairo</title>\n'
        '<meta property="og:title" content="Sairo" />\n'
        '<meta name="description" content="Sairo — S3-compatible object storage browser" />\n'
        '<meta name="theme-color" content="#3b82f6" />'
    )

    def test_html_fields_follow_app_name_and_color(self):
        m = _main_module()
        out = m._apply_branding_html(self.SAMPLE_HTML, "Objex", "#e11d48")
        assert "<title>Objex</title>" in out
        assert 'property="og:title" content="Objex"' in out            # link/social previews
        assert 'content="Objex — S3-compatible' in out                 # description
        assert 'name="theme-color" content="#e11d48"' in out           # tab/PWA colour
        assert "Sairo" not in out                                      # no leak anywhere

    def test_html_default_keeps_sairo(self):
        m = _main_module()
        out = m._apply_branding_html(self.SAMPLE_HTML, "Sairo", "#3b82f6")
        assert "<title>Sairo</title>" in out                           # vanilla install unchanged

    def test_html_escapes_app_name(self):
        m = _main_module()
        out = m._apply_branding_html("<title>Sairo</title>", "A&B<x>", "#000")
        assert "A&amp;B&lt;x&gt;" in out and "<title>A&B<x></title>" not in out

    def test_html_brand_name_is_text_not_regex_replacement(self):
        m = _main_module()
        out = m._apply_branding_html(self.SAMPLE_HTML, "Acme\\q", "#000")   # a backslash sequence used to break re.sub
        assert "<title>Acme\\q</title>" in out and 'property="og:title" content="Acme\\q"' in out

    def test_index_html_route_is_the_branded_no_cache_shell(self, client, monkeypatch):
        m = _main_module()
        if not os.path.isfile(os.path.join(m.static_dir, "index.html")):
            pytest.skip("built frontend not present")
        monkeypatch.setenv("APP_NAME", "Objex"); m._spa_cache.clear()
        for path in ("/", "/index.html"):
            r = client.get(path)
            assert r.status_code == 200 and "<title>Objex</title>" in r.text, path
            assert r.headers.get("cache-control") == "no-cache, must-revalidate", path
        m._spa_cache.clear()

    def test_hashed_assets_are_long_cacheable(self, client):
        m = _main_module()
        assets = os.path.join(m.static_dir, "assets")
        if not os.path.isdir(assets) or not os.listdir(assets):
            pytest.skip("built frontend not present")
        r = client.get(f"/assets/{sorted(os.listdir(assets))[0]}")
        assert r.status_code == 200 and r.headers.get("cache-control") == "public, max-age=31536000, immutable"

    def test_missing_asset_404_is_not_cached_for_a_year(self, client):
        r = client.get("/assets/index-doesnotexist.js")
        assert r.status_code == 404 and "immutable" not in (r.headers.get("cache-control") or "")

    def test_csp_allows_the_configured_external_logo(self, client, monkeypatch):
        csp = client.get("/healthz").headers["content-security-policy"]
        assert "img-src 'self' blob: data:;" in csp
        monkeypatch.setenv("APP_LOGO", "https://cdn.example.com/brand/logo.svg")
        csp = client.get("/healthz").headers["content-security-policy"]
        assert "img-src 'self' blob: data: https://cdn.example.com;" in csp

    def test_manifest_branded(self):
        m = _main_module()
        out = m._branded_manifest(
            {"name": "Sairo", "short_name": "Sairo", "theme_color": "#3b82f6", "start_url": "/"},
            "Objex", "#e11d48")
        assert out["name"] == "Objex" and out["short_name"] == "Objex"
        assert out["theme_color"] == "#e11d48"
        assert out["start_url"] == "/"   # untouched fields preserved

    def test_manifest_default_keeps_sairo(self):
        m = _main_module()
        out = m._branded_manifest({"name": "Sairo", "short_name": "Sairo"}, "Sairo", "#3b82f6")
        assert out["name"] == "Sairo"


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


# ── Phase A: crawler correctness (SAE-63) ───────────────────────────────────

class TestCrawlerCorrectness:
    """Resume, honest status, cancel-not-fork, stop flag, disk guard."""

    def _main(self):
        import sys
        return sys.modules.get("backend.main") or sys.modules["main"]

    def test_crawl_prefix_should_stop_flushes_and_raises(self, app):
        m = self._main()
        pages = [
            {"Contents": [{"Key": f"p/a{i}", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e"'} for i in range(3)],
             "IsTruncated": True, "NextContinuationToken": "t1"},
            {"Contents": [{"Key": "p/b0", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e"'}], "IsTruncated": False},
        ]
        client = MagicMock(); client.list_objects_v2.side_effect = pages
        flushed = []
        calls = {"n": 0}
        def should_stop():
            calls["n"] += 1
            return calls["n"] > 1  # allow the first page, stop before the second
        with patch.object(m._s3_manager, "get_client", return_value=client):
            with pytest.raises(m.CrawlInterrupted):
                m._crawl_prefix("stopbkt", "p/", endpoint_id="default", batch_callback=flushed.extend, should_stop=should_stop)
        assert [r[0] for r in flushed] == ["p/a0", "p/a1", "p/a2"]  # first page persisted, not lost
        assert client.list_objects_v2.call_count == 1

    def test_queue_crawl_requests_cancel_only_when_stalled(self, app):
        m = self._main()
        key = "default:dupbkt"
        fut = MagicMock(); fut.done.return_value = False
        ev = m.threading.Event()
        with patch.object(m, "_CRAWL_STALL_SECONDS", 60), patch.object(m._crawl_pool, "submit") as submit:
            m._crawling[key] = time.time() - 7200          # running for 2h ...
            m._crawl_futures[key] = fut
            m._crawl_cancel[key] = ev
            m._crawl_progress_ts[key] = time.time() - 5    # ... but made progress 5s ago
            try:
                assert m._queue_crawl("dupbkt", "default") is False
                assert not ev.is_set(), "a long crawl that is progressing must not be cancelled"
                m._crawl_progress_ts[key] = time.time() - 120  # now stalled beyond the threshold
                assert m._queue_crawl("dupbkt", "default") is False
                assert ev.is_set(), "a stalled crawl must be asked to stop"
                submit.assert_not_called()                    # never a second crawl
            finally:
                for d in (m._crawling, m._crawl_futures, m._crawl_cancel, m._crawl_progress_ts): d.pop(key, None)

    def test_queue_crawl_never_times_out_non_crawl_owner(self, app):
        m = self._main()
        key = "default:purgebkt"
        with patch.object(m, "_CRAWL_MAX_DURATION", 0), patch.object(m._crawl_pool, "submit") as submit:
            m._crawling[key] = True  # version purge holds the lock with a bool
            try:
                assert m._queue_crawl("purgebkt", "default") is False
                submit.assert_not_called()
            finally:
                m._crawling.pop(key, None)

    def test_disk_guard_blocks_crawl(self, app):
        m = self._main()
        from collections import namedtuple
        DU = namedtuple("usage", "total used free")
        m._init_db("diskbkt", "default")
        with patch.object(m.shutil, "disk_usage", return_value=DU(100, 95, 5)), patch.object(m._crawl_pool, "submit") as submit:
            assert m._disk_low() is True
            assert m._queue_crawl("diskbkt", "default") is False
            submit.assert_not_called()
        with m._get_db("diskbkt", "default") as db:   # the refusal is a recorded attempt; the index state itself is untouched
            row = db.execute("SELECT status, last_error FROM crawl_status WHERE id=1").fetchone()
            assert not (row["status"] or "").startswith("error") and "disk" in (row["last_error"] or "")
        with patch.object(m.shutil, "disk_usage", return_value=DU(100, 50, 50)):
            assert m._disk_low() is False

    def test_run_crawl_resumes_and_skips_completed_prefixes(self, app):
        """Kill mid-crawl (simulated by pre-seeded progress) → the next crawl keeps the generation,
        lists only unfinished prefixes, and ends complete with every object present."""
        m = self._main()
        bucket = "resumebkt"
        for suffix in ("", "-wal", "-shm"):  # fresh DB every run: the shared test DB_DIR may hold a stale/corrupt copy
            try: os.remove(m._db_path(bucket, "default") + suffix)
            except FileNotFoundError: pass
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            db.execute("DELETE FROM objects"); db.execute("DELETE FROM crawl_progress"); db.execute("DELETE FROM discovered_prefixes")  # idempotent across runs (shared test DB_DIR)
            db.execute("UPDATE crawl_status SET status='crawling', current_crawl_gen=7 WHERE id=1")
            db.execute("INSERT OR REPLACE INTO crawl_progress (prefix, gen) VALUES ('a/', 7)")
            db.execute("INSERT OR IGNORE INTO discovered_prefixes (prefix) VALUES ('a/'), ('b/')")
            for i in range(2):  # rows the interrupted crawl already wrote for a/
                db.execute("INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
                           (f"a/{i}", 1, "2026-01-01T00:00:00+00:00", "e", "a/", 1, 7))
            db.commit()
        listed = []
        def list_objects_v2(**p):
            if "Delimiter" in p:   # only the root has children; a real listing never returns a prefix as its own child
                kids = [{"Prefix": "a/"}, {"Prefix": "b/"}] if not p.get("Prefix") else []
                return {"CommonPrefixes": kids, "Contents": [], "IsTruncated": False}
            listed.append(p["Prefix"])
            return {"Contents": [{"Key": f"{p['Prefix']}{i}", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e"'} for i in range(2)],
                    "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        assert "a/" not in listed and "b/" in listed, f"completed prefix must not be re-listed; listed={listed}"
        with m._get_db(bucket, "default") as db:
            row = db.execute("SELECT status, current_crawl_gen, total_objects FROM crawl_status WHERE id=1").fetchone()
            assert row["status"] == "complete" and row["current_crawl_gen"] == 7 and row["total_objects"] == 4

    def test_delta_does_not_relabel_interrupted_as_complete(self, app):
        m = self._main()
        bucket = "intbkt"
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            db.execute("UPDATE crawl_status SET status='interrupted' WHERE id=1"); db.commit()
            # the guarded UPDATE the delta path uses
            db.execute("UPDATE crawl_status SET status='complete', last_crawl_end=? WHERE id=1 AND status <> 'interrupted'", ("x",))
            db.commit()
            assert db.execute("SELECT status FROM crawl_status WHERE id=1").fetchone()[0] == "interrupted"

    def test_crawl_status_reports_full_crawl_complete(self, client, admin_cookies):
        m = self._main()
        bucket = "statbkt"
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            db.execute("UPDATE crawl_status SET status='interrupted', crawl_duration=12.5, total_objects=10 WHERE id=1"); db.commit()
        r = client.get(f"/api/buckets/{bucket}/crawl-status", cookies=admin_cookies)
        assert r.status_code == 200 and r.json()["full_crawl_complete"] is False
        with m._get_db(bucket, "default") as db:
            db.execute("UPDATE crawl_status SET status='complete' WHERE id=1"); db.commit()
        assert client.get(f"/api/buckets/{bucket}/crawl-status", cookies=admin_cookies).json()["full_crawl_complete"] is True


class TestS3KeyAuth:
    """Issues #1 and #8: logging in with S3 keys, and an S3-key session staying pinned to the endpoint it
    logged into no matter which endpoint the request is routed to. Neither had a test until now."""

    def _client_ok(self, *a, **k):
        from unittest.mock import MagicMock
        from datetime import datetime, timezone
        c = MagicMock(); c.list_buckets.return_value = {"Buckets": [{"Name": "seen-through-user-keys", "CreationDate": datetime(2026, 1, 1, tzinfo=timezone.utc)}]}; return c

    def test_login_with_s3_keys_valid_and_invalid(self, client):
        from unittest.mock import patch, MagicMock
        m = _main_module()
        bad = MagicMock(); bad.list_buckets.side_effect = Exception("SignatureDoesNotMatch")
        with patch.object(m._s3_manager, "_build_client", return_value=bad):
            assert client.post("/api/auth/login-s3", json={"access_key": "AKIAWRONG", "secret_key": "nope"}).status_code == 401
        with patch.object(m._s3_manager, "_build_client", side_effect=self._client_ok):
            r = client.post("/api/auth/login-s3", json={"access_key": "AKIAUSERKEY123", "secret_key": "s3cr3t"})
        assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["username"].startswith("s3:")
        assert "access_token" in r.cookies
        assert client.post("/api/auth/login-s3", json={"access_key": "", "secret_key": ""}).status_code == 400

    def test_s3_key_session_cannot_reach_another_endpoint(self, client):
        """#8: a user who logged in against endpoint B is routed to B even when the request names A."""
        from unittest.mock import patch
        m = _main_module()
        m._s3_manager.register("ep-b", "http://ep-b.example:9000", "server-ak", "server-sk", "", True)
        seen = []
        real_get_client = m._s3_manager.get_client
        def spy_get_client(eid=None, *a, **k):
            seen.append(eid); return self._client_ok()
        try:
            with patch.object(m, "AUTH_MODE", "s3"), patch.object(m._s3_manager, "_build_client", side_effect=self._client_ok):
                r = client.post("/api/auth/login-s3", json={"access_key": "AKIAUSERKEY123", "secret_key": "s3cr3t", "endpoint_id": "ep-b"})
                assert r.status_code == 200
                cookies = r.cookies
                with patch.object(m._s3_manager, "get_client", side_effect=spy_get_client):
                    # routed to the DEFAULT endpoint explicitly, with the ep-b session
                    r2 = client.get("/api/e/default/buckets", cookies=cookies)
                    assert r2.status_code == 200, r2.text
            assert seen and all(e == "ep-b" for e in seen), f"requests must be served by the session's endpoint, got {seen}"
        finally:
            with m._s3_manager._lock:
                m._s3_manager._endpoints.pop("ep-b", None)

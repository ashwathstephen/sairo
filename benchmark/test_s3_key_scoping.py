#!/usr/bin/env python3
"""Issue #8: S3-key auth must scope a user to ONLY the buckets their keys can access,
across multi-endpoint setups. Drives the real FastAPI stack (login → middleware →
endpoints) in AUTH_MODE=s3 against a FAKE S3 that enforces per-key IAM scoping, so the
behaviour is verified deterministically without a live MinIO/Ceph.

Scenario: provider has bucket-a (only TKEYUSERA can touch) and bucket-b (only TKEYUSERB).
A user logging in with TKEYUSERA must see/manage bucket-a only — never bucket-b, and
never via the /api/e/ endpoint-routing path either.
"""
import os, sys, json
from datetime import datetime, timezone

os.environ.update({
    "AUTH_MODE": "s3",
    "S3_ENDPOINT": "http://fake-s3.local:9000",
    "S3_ACCESS_KEY": "serverroot", "S3_SECRET_KEY": "serverrootsecret",
    "S3_REGION": "us-east-1", "JWT_SECRET": "scopingtestsecretscopingtestsecret01",
    "SECURE_COOKIE": "false", "DB_DIR": "/tmp/sairo-scoping", "RECRAWL_INTERVAL": "100000",
})
import shutil; shutil.rmtree("/tmp/sairo-scoping", ignore_errors=True); os.makedirs("/tmp/sairo-scoping", exist_ok=True)
import logging; logging.getLogger("sairo").setLevel(logging.ERROR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ── Fake S3 enforcing per-key scoping (stands in for MinIO/Ceph IAM) ──
ACCESS = {  # which access key may touch which buckets
    "TKEYUSERA": {"bucket-a"},
    "TKEYUSERB": {"bucket-b"},
    "serverroot": {"bucket-a", "bucket-b"},  # the server/root creds see everything
}
ALL_BUCKETS = ["bucket-a", "bucket-b"]

class FakeS3:
    def __init__(self, access_key):
        self.ak = access_key
    def _allowed(self):
        return ACCESS.get(self.ak, set())
    def list_buckets(self):
        if self.ak not in ACCESS:  # unknown key = invalid credentials (real S3 raises)
            raise ClientError({"Error": {"Code": "InvalidAccessKeyId", "Message": "bad creds"}}, "ListBuckets")
        names = [b for b in ALL_BUCKETS if b in self._allowed()]
        return {"Buckets": [{"Name": n, "CreationDate": datetime(2026, 1, 1, tzinfo=timezone.utc)} for n in names],
                "Owner": {"DisplayName": self.ak}}
    def head_bucket(self, Bucket=None, **kw):
        if Bucket in self._allowed():
            return {}
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadBucket")
    def __getattr__(self, name):  # any other call: succeed if scoped, else 403
        def _m(*a, **k):
            b = k.get("Bucket")
            if b is not None and b not in self._allowed():
                raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, name)
            return {}
        return _m

# Patch the client factory so every client is a scoped FakeS3 keyed by its access key.
main._s3_manager._build_client = lambda info, ak, sk: FakeS3(ak)
main._s3_manager._clients.clear(); main._s3_manager._user_clients.clear()

results = []
def check(name, ok, detail=""):
    results.append(ok); print(f"  {'✓ PASS' if ok else '✗ FAIL'}  {name:52} {detail}")

def login(c, ak, sk):
    r = c.post("/api/auth/login-s3", json={"access_key": ak, "secret_key": sk})
    return r

def names(resp_json):
    return sorted(b["name"] for b in resp_json.get("buckets", []))

def run():
    c = TestClient(main.app)

    # 1. login with USERA's keys → succeeds, and the JWT carries the encrypted keys
    r = login(c, "TKEYUSERA", "secretuseraaaaaaaaaa")
    import jwt as _jwt
    tok = r.cookies.get("access_token")
    claims = _jwt.decode(tok, os.environ["JWT_SECRET"], algorithms=["HS256"]) if tok else {}
    ak_back = main._decrypt(claims.get("s3ak", ""))
    check("login-s3(USERA) succeeds", r.status_code == 200, f"status={r.status_code}")
    check("JWT carries encrypted user keys", ak_back == "TKEYUSERA" and claims.get("s3ak", "").startswith("enc::"),
          f"decrypted_ak={ak_back}")

    # 2. /api/buckets returns ONLY bucket-a (scoped by USERA's keys)
    r = c.get("/api/buckets")
    check("USERA sees only bucket-a", r.status_code == 200 and names(r.json()) == ["bucket-a"], f"got={names(r.json())}")

    # 3. USERA may NOT touch bucket-b — even though the local index path would serve it
    rb = c.get("/api/buckets/bucket-b/crawl-status")
    check("USERA blocked from bucket-b (index not a bypass)", rb.status_code == 403, f"status={rb.status_code}")
    ra = c.get("/api/buckets/bucket-a/crawl-status")
    check("USERA allowed on bucket-a", ra.status_code == 200, f"status={ra.status_code}")

    # 4. USERA cannot escape scope via the /api/e/ endpoint-routing path (IDOR)
    re = c.get("/api/e/default/buckets/bucket-b/crawl-status")
    check("USERA blocked from bucket-b via /api/e/ (IDOR closed)", re.status_code == 403, f"status={re.status_code}")

    # 5. /api/all-buckets shows only the bound endpoint with the user's buckets
    r = c.get("/api/all-buckets")
    eps = r.json().get("endpoints", [])
    all_names = sorted(b["name"] for ep in eps for b in ep.get("buckets", []))
    check("USERA all-buckets scoped to bucket-a", r.status_code == 200 and all_names == ["bucket-a"], f"got={all_names}")

    # 6. switch to USERB — must see ONLY bucket-b (no cross-user cache leak)
    c2 = TestClient(main.app)
    login(c2, "TKEYUSERB", "secretuserbbbbbbbbbb")
    r = c2.get("/api/buckets")
    check("USERB sees only bucket-b (no cache leak)", r.status_code == 200 and names(r.json()) == ["bucket-b"], f"got={names(r.json())}")
    rb = c2.get("/api/buckets/bucket-a/crawl-status")
    check("USERB blocked from bucket-a", rb.status_code == 403, f"status={rb.status_code}")

    # 7. invalid keys are rejected
    bad = c.post("/api/auth/login-s3", json={"access_key": "TKEYNOPE", "secret_key": "x"})
    check("invalid keys rejected", bad.status_code == 401, f"status={bad.status_code}")

    print(f"\n  RESULT: {sum(results)}/{len(results)} S3-key scoping checks PASS")
    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(run())

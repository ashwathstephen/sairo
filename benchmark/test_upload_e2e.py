#!/usr/bin/env python3
"""
End-to-end upload validation for issue #6 against a REAL S3 (local MinIO).
Exercises all three upload paths and verifies object integrity (md5):
  1. single-PUT direct (presigned PUT) — small files
  2. multipart direct (initiate/sign/PUT-parts/complete) — large files, browser-simulated
  3. proxy streaming (POST /upload) — the fallback, must not buffer whole file

Run with MinIO at :9400 and bucket 'uptest'. boto3 talks to MinIO for real;
Sairo API is driven in-process via TestClient. Presigned PUTs go straight to MinIO.
"""
import hashlib, io, json, os, sys, time, urllib.request

os.environ.update({
    "S3_ENDPOINT": "http://localhost:9400",
    "S3_ACCESS_KEY": "minioadmin",
    "S3_SECRET_KEY": "minioadmin",
    "S3_REGION": "us-east-1",
    "ADMIN_USER": "admin",
    "ADMIN_PASS": "uploadtest",
    "JWT_SECRET": os.urandom(24).hex(),
    "SECURE_COOKIE": "false",
    "DB_DIR": "/tmp/sairo-upload-e2e",
    "RECRAWL_INTERVAL": "100000",
})
import logging
logging.getLogger("sairo").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main  # noqa: E402  (real boto3 → MinIO, NOT mocked)
from fastapi.testclient import TestClient  # noqa: E402

BUCKET = "uptest"
results = []
def check(name, ok, detail=""):
    results.append(ok); print(f"  {'✓ PASS' if ok else '✗ FAIL'}  {name:38} {detail}")

def put_url(url, body):
    req = urllib.request.Request(url, data=body, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.headers.get("ETag")

def md5(b): return hashlib.md5(b).hexdigest()

def s3_object_md5(client, key):
    body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return md5(body), len(body)

def run():
    client = TestClient(main.app)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "uploadtest"})
    assert r.status_code == 200, f"login failed: {r.text[:200]}"
    cookies = r.cookies
    s3 = main._s3_manager.get_client("default")

    # ── 1. single-PUT direct upload (small file) ──
    small = os.urandom(2 * 1024 * 1024)  # 2 MB
    key = "e2e/small.bin"
    resp = client.post(f"/api/buckets/{BUCKET}/presigned-upload",
                       json={"keys": ["small.bin"], "prefix": "e2e/"}, cookies=cookies)
    url = resp.json()["urls"][0]["url"]
    st, _ = put_url(url, small)
    client.post(f"/api/buckets/{BUCKET}/notify-upload",
                json={"uploads": [{"key": key, "size": len(small)}]}, cookies=cookies)
    got_md5, got_size = s3_object_md5(s3, key)
    check("single-PUT direct upload (2MB)", st in (200, 204) and got_md5 == md5(small) and got_size == len(small),
          f"size={got_size}, md5 match={got_md5 == md5(small)}")

    # ── 2. multipart direct upload (large file, browser-simulated) ──
    big = os.urandom(120 * 1024 * 1024)  # 120 MB → > 100MB threshold → multipart
    key2 = "e2e/big.bin"
    init = client.post(f"/api/buckets/{BUCKET}/multipart/initiate",
                       json={"key": "big.bin", "prefix": "e2e/", "content_type": "application/octet-stream"},
                       cookies=cookies).json()
    upload_id = init["upload_id"]
    part_size = 16 * 1024 * 1024
    nparts = (len(big) + part_size - 1) // part_size
    nums = list(range(1, nparts + 1))
    signed = client.post(f"/api/buckets/{BUCKET}/multipart/sign",
                        json={"key": key2, "upload_id": upload_id, "part_numbers": nums}, cookies=cookies).json()
    url_by_part = {u["part_number"]: u["url"] for u in signed["urls"]}
    parts = []
    for pn in nums:
        chunk = big[(pn - 1) * part_size: pn * part_size]
        st, etag = put_url(url_by_part[pn], chunk)
        parts.append({"PartNumber": pn, "ETag": etag})
    comp = client.post(f"/api/buckets/{BUCKET}/multipart/complete",
                       json={"key": key2, "upload_id": upload_id, "parts": parts}, cookies=cookies)
    client.post(f"/api/buckets/{BUCKET}/notify-upload",
                json={"uploads": [{"key": key2, "size": len(big)}]}, cookies=cookies)
    got_md5, got_size = s3_object_md5(s3, key2)
    check("multipart direct upload (120MB, 8 parts)",
          comp.status_code == 200 and got_md5 == md5(big) and got_size == len(big),
          f"parts={nparts}, size={got_size}, md5 match={got_md5 == md5(big)}")

    # ── 2b. multipart ABORT cleanup ──
    init2 = client.post(f"/api/buckets/{BUCKET}/multipart/initiate",
                        json={"key": "abort-me.bin", "prefix": "e2e/"}, cookies=cookies).json()
    ab = client.post(f"/api/buckets/{BUCKET}/multipart/abort",
                     json={"key": "e2e/abort-me.bin", "upload_id": init2["upload_id"]}, cookies=cookies)
    in_progress = s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])
    check("multipart abort cleans up", ab.status_code == 200 and not any(u["UploadId"] == init2["upload_id"] for u in in_progress),
          f"in-progress uploads remaining={len(in_progress)}")

    # ── 3. proxy streaming upload (large file via POST /upload) ──
    proxy_data = os.urandom(80 * 1024 * 1024)  # 80 MB through the proxy
    r = client.post(f"/api/buckets/{BUCKET}/upload?",
                    data={"prefix": "e2e/"},
                    files={"files": ("proxy.bin", io.BytesIO(proxy_data), "application/octet-stream")},
                    cookies=cookies)
    got_md5, got_size = s3_object_md5(s3, "e2e/proxy.bin")
    check("proxy streaming upload (80MB)", r.status_code == 200 and got_md5 == md5(proxy_data) and got_size == len(proxy_data),
          f"size={got_size}, md5 match={got_md5 == md5(proxy_data)}")

    # ── 4. index updated (notify-upload + proxy both reflect in the index) ──
    st = client.get(f"/api/buckets/{BUCKET}/crawl-status", cookies=cookies)
    # listing the e2e/ prefix should show the 3 uploaded objects
    lst = client.get(f"/api/buckets/{BUCKET}/list?prefix=e2e/&limit=1000", cookies=cookies)
    files_listed = json.loads(lst.text.splitlines()[0]).get("files", []) if lst.status_code == 200 else []
    names = {f["name"] for f in files_listed}
    check("index reflects direct + proxy uploads", {"small.bin", "big.bin", "proxy.bin"}.issubset(names),
          f"listed={sorted(names)}")

    print(f"\n  RESULT: {sum(results)}/{len(results)} upload e2e checks PASS")
    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(run())

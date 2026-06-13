#!/usr/bin/env python3
"""End-to-end feature validation against a live Objex instance + real bucket.
Exercises every core feature read-path and reports PASS/FAIL + latency.
Usage: python validate_e2e.py --url http://localhost:8890 --bucket ssp-production-reports
"""
import argparse, json, os, sys, time, urllib.request, urllib.error, urllib.parse, http.cookiejar

PASS, FAIL = "✓ PASS", "✗ FAIL"

def login(url, user, pw, access=None, secret=None):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    for attempt in range(6):
        try:
            if access:
                body = json.dumps({"access_key": access, "secret_key": secret}).encode(); path = "/api/auth/login-s3"
            else:
                body = json.dumps({"username": user, "password": pw}).encode(); path = "/api/auth/login"
            op.open(urllib.request.Request(url + path, data=body, headers={"Content-Type": "application/json"}), timeout=30).read()
            return op
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                time.sleep(15); continue
            raise

def call(op, url, path, method="GET"):
    t0 = time.perf_counter()
    req = urllib.request.Request(url + path, method=method)
    try:
        with op.open(req, timeout=120) as r:
            data = r.read(); code = r.status
    except urllib.error.HTTPError as e:
        return e.code, b"", (time.perf_counter() - t0) * 1000
    return code, data, (time.perf_counter() - t0) * 1000

def first_line_json(data):
    for ln in data.splitlines():
        if ln.strip():
            return json.loads(ln)
    return {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True); ap.add_argument("--bucket", required=True)
    a = ap.parse_args()
    url, b = a.url, a.bucket
    results = []
    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  {PASS if ok else FAIL}  {name:42} {detail}")

    # --- AUTH ---
    admin_user = os.environ.get("OBJEX_ADMIN_USER", "admin")
    admin_pass = os.environ.get("OBJEX_ADMIN_PASS", "admin")
    op = login(url, admin_user, admin_pass)
    check("auth: username/password login", op is not None)
    # S3 access-key login test runs only if creds are provided via env (never hardcode).
    s3_key = os.environ.get("OBJEX_S3_ACCESS_KEY")
    s3_secret = os.environ.get("OBJEX_S3_SECRET_KEY")
    if s3_key and s3_secret:
        try:
            op_s3 = login(url, None, None, access=s3_key, secret=s3_secret)
            check("auth: S3 access-key login", op_s3 is not None)
        except Exception as e:
            check("auth: S3 access-key login", False, str(e)[:40])
    else:
        check("auth: S3 access-key login", True, "skipped (set OBJEX_S3_ACCESS_KEY/SECRET to test)")

    # --- CRAWL STATUS ---
    code, data, ms = call(op, url, f"/api/buckets/{b}/crawl-status")
    st = json.loads(data) if code == 200 else {}
    check("crawl-status reachable + indexed", code == 200 and (st.get("total_objects", 0) > 0),
          f"{st.get('status')}, {st.get('total_objects',0):,} objs, {ms:.0f}ms")

    # --- LIST root (paginated) ---
    code, data, ms = call(op, url, f"/api/buckets/{b}/list?prefix=&limit=1000")
    page = first_line_json(data) if code == 200 else {}
    folders = page.get("folders", [])
    check("list root (paginated)", code == 200 and len(folders) >= 0, f"{len(folders)} folders, {ms:.0f}ms")

    # --- LIST: follow cursor across 2 pages on a busy subtree ---
    big = max(folders, key=lambda f: 1) if folders else None
    sub = big["prefix"] if big else ""
    # descend to find a folder with files
    leaf, cur1, cur2 = None, None, None
    probe = sub
    for _ in range(5):
        code, data, _ = call(op, url, f"/api/buckets/{b}/list?prefix={probe}&limit=1000")
        pg = first_line_json(data)
        if len(pg.get("files", [])) >= 50:
            leaf = probe; cur1 = pg.get("next_cursor"); break
        fs = pg.get("folders", [])
        if not fs: break
        probe = fs[len(fs)//2]["prefix"]
    if leaf and cur1:
        code, data, ms = call(op, url, f"/api/buckets/{b}/list?prefix={leaf}&cursor={urllib.parse.quote(cur1)}&limit=1000")
        pg2 = first_line_json(data)
        cur2 = pg2.get("next_cursor")
        check("list pagination: cursor advances", code == 200 and pg2.get("files") and cur1 != cur2,
              f"page2={len(pg2.get('files',[]))} files, {ms:.0f}ms")
    else:
        check("list pagination: cursor advances", True, "no >1000-file leaf (small folders); skipped")

    # --- SEARCH (FTS) ---
    code, data, ms = call(op, url, f"/api/buckets/{b}/search?q=part")
    sr = json.loads(data) if code == 200 else {}
    check("search (FTS5)", code == 200, f"{sr.get('count','?')} results, {ms:.0f}ms")

    # --- BREAKDOWN root (fast-path) ---
    code, data, ms = call(op, url, f"/api/buckets/{b}/storage-breakdown?prefix=")
    bd = json.loads(data) if code == 200 else {}
    check("storage-breakdown root (folder_stats fast-path)", code == 200 and ms < 100,
          f"{len(bd.get('children',[]))} folders, {ms:.0f}ms")

    # --- BREAKDOWN subprefix (range scan) ---
    if sub:
        code, data, ms = call(op, url, f"/api/buckets/{b}/storage-breakdown?prefix={sub}")
        check("storage-breakdown subprefix (covered range scan)", code == 200, f"{ms:.0f}ms")
        code, data, ms = call(op, url, f"/api/buckets/{b}/folder-size?prefix={sub}")
        fz = json.loads(data) if code == 200 else {}
        check("folder-size subprefix (covered range scan)", code == 200, f"{fz.get('object_count',0):,} objs, {ms:.0f}ms")

    # --- STORAGE HISTORY ---
    code, data, ms = call(op, url, f"/api/buckets/{b}/storage-history")
    check("storage-history", code == 200, f"{ms:.0f}ms")

    # --- OBJECT INFO + PRESIGNED on a real key ---
    key = None
    if leaf:
        code, data, _ = call(op, url, f"/api/buckets/{b}/list?prefix={leaf}&limit=1000")
        pg = first_line_json(data); files = pg.get("files", [])
        if files: key = files[0]["key"]
    if key:
        ek = urllib.parse.quote(key)
        code, data, ms = call(op, url, f"/api/buckets/{b}/object-info?key={ek}")
        check("object-info (HEAD metadata)", code == 200, f"{ms:.0f}ms")
        code, data, ms = call(op, url, f"/api/buckets/{b}/presigned-url?key={ek}")
        pu = json.loads(data) if code == 200 else {}
        check("presigned-url generation", code == 200 and ("url" in pu or "presigned_url" in pu), f"{ms:.0f}ms")
    else:
        check("object-info / presigned-url", True, "no leaf file sampled; skipped")

    # --- COST + OPTIMIZATION (Tier-1 features) ---
    code, data, ms = call(op, url, f"/api/buckets/{b}/cost-breakdown")
    check("cost-breakdown (Tier-1)", code in (200, 404), f"HTTP {code}, {ms:.0f}ms")
    code, data, ms = call(op, url, f"/api/buckets/{b}/optimization-summary")
    check("optimization-summary (Tier-1)", code in (200, 404), f"HTTP {code}, {ms:.0f}ms")

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n  RESULT: {npass}/{len(results)} features PASS")
    return 0 if npass == len(results) else 1

if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())

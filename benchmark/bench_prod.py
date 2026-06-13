#!/usr/bin/env python3
"""
Production read-only A/B benchmark — times the hot query paths against a live
Objex instance and a real bucket. Read-only: login, list, search, breakdown,
folder-size. No uploads/deletes/crawl-mutation. Run once per code version
(before/after) against the SAME base URL+bucket, then diff the JSON outputs.

Usage:
  python bench_prod.py --url http://localhost:8890 --bucket coolco-id5 --label after
  python bench_prod.py --url http://localhost:8891 --bucket coolco-id5 --label before --compare 'results/prod-after-*.json'
"""
import argparse, glob, json, statistics, time, urllib.request, urllib.error, http.cookiejar, os

def client(url, user, pw):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    body = json.dumps({"username": user, "password": pw}).encode()
    # Login is rate-limited (10/min); retry with backoff if we hit 429.
    for attempt in range(8):
        try:
            req = urllib.request.Request(url + "/api/auth/login", data=body,
                                         headers={"Content-Type": "application/json"})
            op.open(req, timeout=30).read()
            return op
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 7:
                print(f"  login rate-limited (429), waiting 20s (attempt {attempt+1})...")
                time.sleep(20)
                continue
            raise

def get(op, url, path):
    t0 = time.perf_counter()
    with op.open(url + path, timeout=120) as r:
        data = r.read()
    return (time.perf_counter() - t0) * 1000.0, data

def timed(op, url, path, iters=15):
    ts, last = [], b""
    for _ in range(iters):
        ms, last = get(op, url, path)
        ts.append(ms)
    ts.sort()
    n = len(ts)
    return {"p50_ms": round(ts[n//2], 1), "p95_ms": round(ts[min(n-1, int(n*0.95))], 1),
            "min_ms": round(ts[0], 1), "max_ms": round(ts[-1], 1),
            "bytes": len(last), "iters": n}

def find_file_heavy_prefix(op, url, bucket, root_children, max_depth=4):
    """Walk down the biggest child until we find a prefix with direct files."""
    # try each big child, descending into its biggest sub-child each level
    prefix = ""
    for child in sorted(root_children, key=lambda c: -c["object_count"]):
        p = child["prefix"]
        for _ in range(max_depth):
            _, data = get(op, url, f"/api/buckets/{bucket}/list?prefix={p}&limit=1000")
            page = json.loads(data.splitlines()[0])
            if len(page.get("files", [])) >= 50:
                return p, len(page.get("files", [])), page.get("next_cursor") is not None
            folders = page.get("folders", [])
            if not folders:
                break
            # descend into a middle folder (more likely to have leaf files)
            p = folders[len(folders)//2]["prefix"]
    return None, 0, False

def run(url, bucket, label, compare):
    op = client(url, os.environ.get("OBJEX_ADMIN_USER", "admin"),
                os.environ.get("OBJEX_ADMIN_PASS", "admin"))
    M = {}
    # discover structure
    _, data = get(op, url, f"/api/buckets/{bucket}/storage-breakdown?prefix=")
    root = json.loads(data)
    children = root.get("children", [])
    big = max((c for c in children if c.get("prefix") and c["prefix"] != "(root files)"),
              key=lambda c: c["object_count"])
    big_prefix = big["prefix"]
    print(f"  bucket={bucket}  objects~{root.get('object_count'):,}  biggest_subtree={big_prefix} ({big['object_count']:,})")

    leaf, leaf_files, leaf_more = find_file_heavy_prefix(op, url, bucket, children)
    print(f"  file-heavy leaf for listing test: {leaf} ({leaf_files} direct files, more_pages={leaf_more})")

    scenarios = {
        "list_root_legacy":      f"/api/buckets/{bucket}/list?prefix=",
        "list_subtree_legacy":   f"/api/buckets/{bucket}/list?prefix={big_prefix}",
        "breakdown_root":        f"/api/buckets/{bucket}/storage-breakdown?prefix=",
        "breakdown_subtree":     f"/api/buckets/{bucket}/storage-breakdown?prefix={big_prefix}",
        "folder_size_subtree":   f"/api/buckets/{bucket}/folder-size?prefix={big_prefix}",
        "search_common":         f"/api/buckets/{bucket}/search?q=part",
        "search_date":           f"/api/buckets/{bucket}/search?q=date",
    }
    if leaf:
        # legacy (whole-folder) vs paginated first page on the SAME file-heavy leaf
        scenarios["list_leaf_legacy"]    = f"/api/buckets/{bucket}/list?prefix={leaf}"
        scenarios["list_leaf_paginated"] = f"/api/buckets/{bucket}/list?prefix={leaf}&limit=1000"

    for name, path in scenarios.items():
        M[name] = timed(op, url, path)
        print(f"    {name:24s} p50={M[name]['p50_ms']:>8.1f}ms  p95={M[name]['p95_ms']:>8.1f}ms  bytes={M[name]['bytes']:,}")

    res = {"label": label, "url": url, "bucket": bucket, "biggest_subtree": big_prefix,
           "leaf": leaf, "metrics": M}
    os.makedirs(os.path.join(os.path.dirname(__file__), "results"), exist_ok=True)
    out = os.path.join(os.path.dirname(__file__), "results", f"prod-{label}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(res, open(out, "w"), indent=2)
    print(f"  saved {out}")

    if compare:
        m = sorted(glob.glob(compare))
        if m:
            base = json.load(open(m[-1]))
            print(f"\n  {'='*70}\n  BEFORE ({base['label']}) -> AFTER ({label})  bucket={bucket}\n  {'='*70}")
            print(f"  {'metric':24s} {'before p50':>12s} {'after p50':>12s} {'speedup':>10s} {'bytes b->a':>22s}")
            for k in M:
                if k in base["metrics"]:
                    b, a = base["metrics"][k]["p50_ms"], M[k]["p50_ms"]
                    sp = f"{b/a:.1f}x" if a else "-"
                    bb, ba = base["metrics"][k]["bytes"], M[k]["bytes"]
                    print(f"  {k:24s} {b:>10.1f}ms {a:>10.1f}ms {sp:>10s} {bb:>10,} -> {ba:<10,}")
    return res

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--compare", default=None)
    args = ap.parse_args()
    run(args.url, args.bucket, args.label, args.compare)

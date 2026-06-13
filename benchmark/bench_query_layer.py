#!/usr/bin/env python3
"""
Query-layer benchmark — measures Sairo's hot paths at scale with NO infra.

Seeds a synthetic SQLite index with N objects (default 1,000,000) using the
backend's real functions, then times the paths users actually feel — folder
listing, search, storage breakdown, folder size — through the real HTTP API
(FastAPI TestClient, in-process), plus the crawl write path and the post-crawl
rebuilds via internal functions.

This is the measurement the previous benchmark suite never did: it caps at the
50K objects `mc cp` can seed, while the O(N) problems only bite past ~1M.

Usage:
    python bench_query_layer.py --objects 1000000 --label baseline
    python bench_query_layer.py --objects 1000000 --label after-phase0 --compare results/qlbench-baseline-*.json

Runs fully offline: boto3 is mocked, no MinIO/server needed.
"""
import argparse
import gc
import glob
import json
import os
import statistics
import sys
import tempfile
import time
import unittest.mock
from datetime import datetime, timezone

# ─── Bootstrap env + mock boto3 BEFORE importing main ───
_DB_DIR = tempfile.mkdtemp(prefix="sairo-qlbench-")
os.environ.update({
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_ACCESS_KEY": "bench",
    "S3_SECRET_KEY": "bench",
    "ADMIN_USER": "admin",
    "ADMIN_PASS": "benchpass",
    "JWT_SECRET": os.urandom(24).hex(),  # ephemeral per-run secret for the in-process test app (never a real credential)
    "SECURE_COOKIE": "false",
    "DB_DIR": _DB_DIR,
    "RECRAWL_INTERVAL": "100000",   # don't let the auto-recrawl thread interfere
})
_mock_boto3 = unittest.mock.MagicMock()
_mock_client = unittest.mock.MagicMock()
_mock_boto3.client.return_value = _mock_client
_mock_client.list_buckets.return_value = {"Buckets": []}
sys.modules["boto3"] = _mock_boto3

import logging  # noqa: E402
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sairo").setLevel(logging.WARNING)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EXT = [".parquet", ".csv", ".json", ".log", ".txt", ".avro", ".gz"]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bulk_insert(bucket, rows_iter, batch_size=50000):
    """Insert objects with FTS triggers disabled (mirrors how a real crawl bulk-loads)."""
    main._init_db(bucket)
    inserted = 0
    with main._get_db(bucket) as db:
        main._disable_fts_triggers(db)
        batch = []
        for row in rows_iter:
            batch.append(row)
            if len(batch) >= batch_size:
                db.executemany(
                    "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) "
                    "VALUES (?,?,?,?,?,?,?)", batch)
                inserted += len(batch)
                batch = []
        if batch:
            db.executemany(
                "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) "
                "VALUES (?,?,?,?,?,?,?)", batch)
            inserted += len(batch)
        total = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
        db.execute("UPDATE crawl_status SET status='complete', total_objects=?, total_size=?, "
                   "last_crawl_end=? WHERE id=1", (total[0], total[1], _now()))
        db.commit()
    return inserted


def _flat_rows(n):
    """N files all directly under one prefix (worst case for unpaginated listing)."""
    for i in range(n):
        key = f"bigdata/file_{i:09d}{EXT[i % len(EXT)]}"
        yield (key, (i % 10000 + 1) * 1024, "2026-05-15T12:00:00Z", f'"e{i:08x}"', "bigdata/", 1, 1)


def _tree_rows(n):
    """Realistic skewed tree: one dominant prefix + others, 2-3 levels deep."""
    # 60% events (skewed hot prefix), 20% logs, 12% data, 8% config
    dist = [("events", 0.60, 3), ("logs", 0.20, 2), ("data", 0.12, 3), ("config", 0.08, 2)]
    i = 0
    for top, pct, depth in dist:
        count = int(n * pct)
        for j in range(count):
            if depth == 3:
                mid = f"dt={2026}-{(j % 12)+1:02d}-{(j % 28)+1:02d}"
                sub = f"part-{j % 200:04d}"
                key = f"{top}/{mid}/{sub}/obj_{j:08d}{EXT[i % len(EXT)]}"
                prefix = f"{top}/{mid}/{sub}/"
            else:
                sub = f"shard-{j % 500:04d}"
                key = f"{top}/{sub}/obj_{j:08d}{EXT[i % len(EXT)]}"
                prefix = f"{top}/{sub}/"
            yield (key, (j % 50000 + 1) * 2048, "2026-05-15T12:00:00Z", f'"t{i:08x}"', prefix, key.count("/"), 1)
            i += 1


def _time_fn(fn, iterations):
    times = []
    for _ in range(iterations):
        gc.collect()
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)  # ms
    times.sort()
    n = len(times)
    return {
        "p50_ms": round(times[n // 2], 2),
        "p95_ms": round(times[min(n - 1, int(n * 0.95))], 2),
        "min_ms": round(times[0], 2),
        "max_ms": round(times[-1], 2),
        "iterations": n,
    }


def _explain(bucket, sql, params):
    with main._get_db(bucket) as db:
        plan = db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(r[-1] for r in plan)


def run(objects, label, compare_path):
    results = {"label": label, "objects": objects, "timestamp": _now(),
               "python": sys.version.split()[0], "metrics": {}}
    M = results["metrics"]

    print(f"\n{'='*64}\n Query-layer benchmark — {objects:,} objects  (label={label})\n{'='*64}")

    # ─── SEED ───
    print(f"[seed] flat folder ({objects:,} files under bigdata/) ...", flush=True)
    t0 = time.perf_counter()
    _bulk_insert("bench-flat", _flat_rows(objects))
    flat_seed = time.perf_counter() - t0
    print(f"       done in {flat_seed:.1f}s")

    print(f"[seed] skewed tree ({objects:,} files) ...", flush=True)
    t0 = time.perf_counter()
    _bulk_insert("bench-tree", _tree_rows(objects))
    print(f"       done in {time.perf_counter() - t0:.1f}s")

    # ─── POST-CRAWL REBUILDS (timed) ───
    print("[rebuild] folder_stats / prefix_children / FTS (timed) ...", flush=True)
    for bucket in ("bench-flat", "bench-tree"):
        t0 = time.perf_counter()
        main._rebuild_folder_stats(bucket)
        fs = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        main._rebuild_prefix_children(bucket)
        pc = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        with main._get_db(bucket) as db:
            db.execute("INSERT INTO objects_fts(objects_fts) VALUES('rebuild')")
            db.commit()
        fts = (time.perf_counter() - t0) * 1000
        M[f"rebuild_{bucket}"] = {"folder_stats_ms": round(fs, 1),
                                  "prefix_children_ms": round(pc, 1),
                                  "fts_rebuild_ms": round(fts, 1)}
        print(f"       {bucket}: folder_stats={fs:.0f}ms prefix_children={pc:.0f}ms fts={fts:.0f}ms")

    # DB file sizes
    M["db_size"] = {}
    for bucket in ("bench-flat", "bench-tree"):
        p = main._db_path(bucket)
        sz = sum(os.path.getsize(p + s) for s in ("", "-wal", "-shm") if os.path.exists(p + s))
        M["db_size"][bucket + "_mb"] = round(sz / 1048576, 1)

    # ─── HTTP API READ PATHS ───
    client = TestClient(main.app)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "benchpass"})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    cookies = r.cookies

    def api(path):
        return client.get(path, cookies=cookies)

    # sanity: confirm endpoints work + capture the big-listing payload size
    big = api("/api/buckets/bench-flat/list?prefix=bigdata/")
    assert big.status_code == 200, f"list failed: {big.status_code} {big.text[:200]}"
    M["flat_listing"] = {
        "response_bytes": len(big.content),
        "response_mb": round(len(big.content) / 1048576, 2),
    }
    print(f"[api] flat folder listing payload = {M['flat_listing']['response_mb']} MB", flush=True)

    print("[api] timing read endpoints ...", flush=True)
    READ_ITERS = 15
    scenarios = {
        "list_flat_1M_folder":   "/api/buckets/bench-flat/list?prefix=bigdata/",
        "list_tree_root":        "/api/buckets/bench-tree/list?prefix=",
        "list_tree_subfolder":   "/api/buckets/bench-tree/list?prefix=events/",
        "search_tree":           "/api/buckets/bench-tree/search?q=obj_0001",
        "breakdown_root":        "/api/buckets/bench-tree/storage-breakdown?prefix=",
        "breakdown_subprefix":   "/api/buckets/bench-tree/storage-breakdown?prefix=events/",
        "folder_size_subprefix": "/api/buckets/bench-tree/folder-size?prefix=events/",
        "storage_history":       "/api/buckets/bench-tree/storage-history",
    }
    for name, path in scenarios.items():
        # paginated endpoints may need a smaller default; measure as-is (default call)
        M[name] = _time_fn(lambda p=path: api(p), READ_ITERS)
        print(f"       {name:24s} p50={M[name]['p50_ms']:>9.2f}ms  p95={M[name]['p95_ms']:>9.2f}ms")

    # ─── PAGINATED FIRST-PAGE (if supported — measures the fixed path) ───
    paged = api("/api/buckets/bench-flat/list?prefix=bigdata/&limit=1000")
    if paged.status_code == 200 and len(paged.content) < len(big.content):
        M["list_flat_first_page"] = _time_fn(
            lambda: api("/api/buckets/bench-flat/list?prefix=bigdata/&limit=1000"), READ_ITERS)
        M["list_flat_first_page"]["response_mb"] = round(len(paged.content) / 1048576, 3)
        print(f"       {'list_flat_first_page':24s} p50={M['list_flat_first_page']['p50_ms']:>9.2f}ms "
              f"({M['list_flat_first_page']['response_mb']} MB)")
    else:
        M["list_flat_first_page"] = {"supported": False}

    # ─── WRITE PATH (crawl) ───
    print("[write] incremental upsert throughput ...", flush=True)
    WM = min(200000, objects)
    fresh = list(_flat_rows(WM))
    fresh6 = [(k, s, lm, et, pf, dp) for (k, s, lm, et, pf, dp, _g) in fresh]  # 6-tuples
    main._init_db("bench-write")
    with main._get_db("bench-write") as db:
        main._disable_fts_triggers(db)
        t0 = time.perf_counter()
        for i in range(0, len(fresh6), 10000):
            main._incremental_upsert(db, fresh6[i:i+10000], gen=1)
        db.commit()
        first_insert = time.perf_counter() - t0
    with main._get_db("bench-write") as db:
        t0 = time.perf_counter()
        for i in range(0, len(fresh6), 10000):
            main._incremental_upsert(db, fresh6[i:i+10000], gen=2)  # all unchanged → crawl_gen rewrite
        db.commit()
        recrawl_unchanged = time.perf_counter() - t0
    M["write"] = {
        "objects": WM,
        "first_insert_sec": round(first_insert, 2),
        "first_insert_per_sec": round(WM / first_insert),
        "recrawl_unchanged_sec": round(recrawl_unchanged, 2),
        "recrawl_unchanged_per_sec": round(WM / recrawl_unchanged),
    }
    print(f"       first insert:      {M['write']['first_insert_per_sec']:,} obj/s")
    print(f"       recrawl unchanged: {M['write']['recrawl_unchanged_per_sec']:,} obj/s")

    # ─── QUERY PLANS (qualitative: is the index used / covering?) ───
    M["query_plans"] = {
        "list": _explain("bench-flat",
                         "SELECT key, size, last_modified FROM objects WHERE prefix = ? ORDER BY key",
                         ("bigdata/",)),
        "folder_size": _explain("bench-tree",
                         "SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects WHERE key LIKE ?",
                         ("events/%",)),
    }

    # ─── SAVE ───
    os.makedirs(os.path.join(os.path.dirname(__file__), "results"), exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(os.path.dirname(__file__), "results", f"qlbench-{label}-{ts}.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n[saved] {out}")

    if compare_path:
        matches = sorted(glob.glob(compare_path))
        if matches:
            with open(matches[-1]) as f:
                base = json.load(f)
            _print_comparison(base, results)

    return results


def _print_comparison(base, cur):
    print(f"\n{'='*78}\n BEFORE ({base['label']}, {base['objects']:,} obj)  →  AFTER "
          f"({cur['label']}, {cur['objects']:,} obj)\n{'='*78}")
    bm, cm = base["metrics"], cur["metrics"]
    print(f"{'metric':28s} {'before':>12s} {'after':>12s} {'change':>12s}")
    print("-" * 68)

    def line(name, b, c, unit="ms", lower_better=True):
        if b is None or c is None:
            return
        if b and c:
            factor = b / c if lower_better else c / b
            arrow = f"{factor:.1f}x faster" if factor >= 1 else f"{1/factor:.1f}x SLOWER"
        else:
            arrow = "—"
        print(f"{name:28s} {b:>10.1f}{unit:>2s} {c:>10.1f}{unit:>2s} {arrow:>12s}")

    for name in ("list_flat_1M_folder", "list_tree_root", "list_tree_subfolder", "search_tree",
                 "breakdown_root", "breakdown_subprefix", "folder_size_subprefix", "storage_history"):
        if name in bm and name in cm and "p50_ms" in bm[name] and "p50_ms" in cm[name]:
            line(name, bm[name]["p50_ms"], cm[name]["p50_ms"])
    if "flat_listing" in bm and "flat_listing" in cm:
        line("flat_payload", bm["flat_listing"]["response_mb"], cm["flat_listing"]["response_mb"], unit="MB")
    for rb in ("rebuild_bench-flat", "rebuild_bench-tree"):
        if rb in bm and rb in cm:
            line(rb + ".fts", bm[rb]["fts_rebuild_ms"], cm[rb]["fts_rebuild_ms"])
    if "write" in bm and "write" in cm:
        line("recrawl_unchanged/s", bm["write"]["recrawl_unchanged_per_sec"],
             cm["write"]["recrawl_unchanged_per_sec"], unit="", lower_better=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--objects", type=int, default=1_000_000)
    ap.add_argument("--label", default="run")
    ap.add_argument("--compare", default=None, help="glob to a prior results json to diff against")
    args = ap.parse_args()
    run(args.objects, args.label, args.compare)

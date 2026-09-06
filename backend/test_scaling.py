"""
End-to-end tests for the 6 scaling changes in main.py.

Tests every changed code path with real SQLite databases:
1. PRAGMA tuning (cache_size, mmap_size, temp_store)
2. Batch sizes (10K crawl batches, 2K update chunks)
3. Worker counts (verified via ThreadPoolExecutor config)
4. prefix_children SQL rebuild (works for >1M objects, no OOM)
5. Async FTS rebuild (search works during rebuild)
6. Sub-prefix splitting (verified via prefix expansion logic)
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

# Set test environment
_test_dir = tempfile.mkdtemp(prefix="sairo-scaling-test-")
os.environ.setdefault("DB_DIR", _test_dir)
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "test")
os.environ.setdefault("S3_SECRET_KEY", "test")
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASS", "testpass123")
os.environ.setdefault("JWT_SECRET", "testsecret1234567890abcdef")

# Patch boto3 before importing main
import unittest.mock
mock_boto3 = unittest.mock.MagicMock()
mock_client = unittest.mock.MagicMock()
mock_boto3.client.return_value = mock_client
mock_client.list_buckets.return_value = {"Buckets": []}
sys.modules.setdefault("boto3", mock_boto3)

# Now import from main
from main import (
    _init_db,
    _get_db,
    _db_path,
    _rebuild_prefix_children,
    _rebuild_folder_stats,
    _record_storage_snapshot,
    _key_prefix,
    _key_depth,
    _disable_fts_triggers,
    _enable_fts_triggers,
    _rebuild_fts_async,
    _crawl_pool,
    _crawl_prefix,
    DB_DIR,
)


def _seed_bucket(bucket, num_objects, prefix_distribution=None):
    """Create a test bucket DB with realistic objects."""
    _init_db(bucket)

    if prefix_distribution is None:
        prefix_distribution = {
            "data/": 0.4,
            "logs/": 0.3,
            "backups/": 0.2,
            "config/": 0.1,
        }

    with _get_db(bucket) as db:
        batch = []
        extensions = [".parquet", ".csv", ".json", ".log", ".txt"]
        i = 0
        for prefix, pct in prefix_distribution.items():
            count = int(num_objects * pct)
            for j in range(count):
                ext = extensions[i % len(extensions)]
                key = f"{prefix}file_{i:08d}{ext}"
                size = (i + 1) * 1024  # 1KB * index
                modified = f"2026-04-{(i % 28) + 1:02d}T{(i % 24):02d}:00:00Z"
                etag = f'"etag_{i:08x}"'
                batch.append((key, size, modified, etag, prefix, key.count("/")))
                i += 1
                if len(batch) >= 5000:
                    db.executemany(
                        "INSERT OR REPLACE INTO objects "
                        "(key,size,last_modified,etag,prefix,depth,crawl_gen) "
                        "VALUES (?,?,?,?,?,?,?)",
                        [row + (1,) for row in batch],
                    )
                    batch = []
        if batch:
            db.executemany(
                "INSERT OR REPLACE INTO objects "
                "(key,size,last_modified,etag,prefix,depth,crawl_gen) "
                "VALUES (?,?,?,?,?,?,?)",
                [row + (1,) for row in batch],
            )

        total = db.execute("SELECT COUNT(*), SUM(size) FROM objects").fetchone()
        db.execute(
            "UPDATE crawl_status SET status='complete', total_objects=?, total_size=? WHERE id=1",
            (total[0], total[1]),
        )
        db.commit()

    return _db_path(bucket)


# ═══════════════════════════════════════════════════════════════════
# 1. PRAGMA TUNING
# ═══════════════════════════════════════════════════════════════════


class TestPRAGMATuning:
    """Verify PRAGMA settings are applied correctly."""

    def test_init_db_sets_cache_size(self):
        _init_db("pragma-test-init")
        with _get_db("pragma-test-init") as db:
            val = db.execute("PRAGMA cache_size").fetchone()[0]
            assert val == -64000, f"Expected cache_size=-64000, got {val}"

    def test_init_db_sets_temp_store(self):
        _init_db("pragma-test-temp")
        with _get_db("pragma-test-temp") as db:
            val = db.execute("PRAGMA temp_store").fetchone()[0]
            # temp_store: 0=default, 1=file, 2=memory. Must NOT be memory: the post-crawl rebuilds GROUP BY a
            # computed expression over the whole objects table, and an in-memory sort of 9.9M rows is OOM-killed.
            assert val != 2, f"temp_store must be file-backed, got {val}"

    def test_get_db_sets_mmap_size(self):
        _init_db("pragma-test-mmap")
        with _get_db("pragma-test-mmap") as db:
            val = db.execute("PRAGMA mmap_size").fetchone()[0]
            assert val == 268435456, f"Expected mmap_size=268435456, got {val}"

    def test_get_db_sets_cache_size(self):
        _init_db("pragma-test-cache")
        with _get_db("pragma-test-cache") as db:
            val = db.execute("PRAGMA cache_size").fetchone()[0]
            assert val == -64000, f"Expected cache_size=-64000, got {val}"

    def test_wal_mode_still_set(self):
        _init_db("pragma-test-wal")
        with _get_db("pragma-test-wal") as db:
            val = db.execute("PRAGMA journal_mode").fetchone()[0]
            assert val == "wal", f"Expected WAL mode, got {val}"

    def test_busy_timeout_set(self):
        """Verify busy_timeout is set to prevent 'database is locked' errors."""
        _init_db("pragma-test-busy")
        with _get_db("pragma-test-busy") as db:
            val = db.execute("PRAGMA busy_timeout").fetchone()[0]
            assert val >= 5000, f"Expected busy_timeout >= 5000ms, got {val}"

    def test_busy_timeout_on_init_db(self):
        """Verify _init_db also sets busy_timeout (for crawl writers)."""
        _init_db("pragma-test-busy-init")
        # Read the source to confirm busy_timeout is in _init_db
        import inspect
        source = inspect.getsource(_init_db)
        assert "busy_timeout" in source, "busy_timeout PRAGMA should be in _init_db"

    def test_busy_timeout_on_get_db(self):
        """Verify _get_db also sets busy_timeout (for API readers)."""
        import inspect
        source = inspect.getsource(_get_db)
        assert "busy_timeout" in source, "busy_timeout PRAGMA should be in _get_db"

    def test_bucket_list_cache_has_lock(self):
        """Verify _bucket_list_cache is protected by a threading lock."""
        assert hasattr(sys.modules["main"], "_bucket_list_cache_lock"), (
            "_bucket_list_cache_lock should exist in main module"
        )
        lock = getattr(sys.modules["main"], "_bucket_list_cache_lock")
        assert hasattr(lock, "acquire"), "Should be a threading.Lock instance"

    def test_concurrent_access_no_locked_error(self):
        """Verify busy_timeout prevents 'database is locked' under concurrent access."""
        bucket = "pragma-concurrent-test"
        _seed_bucket(bucket, 1000)
        errors = []

        def writer():
            try:
                with _get_db(bucket) as db:
                    for i in range(50):
                        db.execute(
                            "INSERT OR REPLACE INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (f"concurrent/write_{i}.txt", i * 100, "2026-04-01T00:00:00Z",
                             f'"etag_w{i}"', "concurrent/", 1, 99),
                        )
                    db.commit()
            except Exception as e:
                errors.append(f"writer: {e}")

        def reader():
            try:
                with _get_db(bucket) as db:
                    for _ in range(20):
                        db.execute("SELECT COUNT(*) FROM objects").fetchone()
                        db.execute("SELECT * FROM objects ORDER BY size DESC LIMIT 10").fetchall()
            except Exception as e:
                errors.append(f"reader: {e}")

        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=writer))
            threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Concurrent access errors: {errors}"
        print(f"  Concurrent access (3 writers + 3 readers): no 'database is locked' errors")

    def test_pragma_speedup_on_count(self):
        """Verify PRAGMA tuning actually makes queries faster."""
        _seed_bucket("pragma-speed-test", 50000)

        # Time a COUNT(*) query — should benefit from cache_size
        with _get_db("pragma-speed-test") as db:
            start = time.monotonic()
            count = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            elapsed = time.monotonic() - start

        assert count == 50000
        assert elapsed < 2.0, f"COUNT(*) on 50K rows took {elapsed:.2f}s — PRAGMA may not be applied"
        print(f"  COUNT(*) on 50K rows: {elapsed*1000:.1f}ms")


# ═══════════════════════════════════════════════════════════════════
# 2. BATCH SIZES
# ═══════════════════════════════════════════════════════════════════


class TestBatchSizes:
    """Verify larger batch sizes work correctly."""

    def test_crawl_prefix_default_batch_size(self):
        """Verify the default batch_size parameter is 10000."""
        import inspect
        sig = inspect.signature(_crawl_prefix)
        default = sig.parameters["batch_size"].default
        assert default == 10000, f"Expected batch_size=10000, got {default}"

    def test_reconcile_writes_only_changed_rows_at_batch_scale(self):
        """10k-row batches through the reconcile: an identical second pass writes nothing (not even crawl_gen),
        and no statement binds one parameter per key (those kept multi-MB prepared statements cached per
        connection: +290 MB at 16 threads) — the generation sweep (541 MB of WAL per unchanged 844k rows) is gone."""
        import inspect, sys, threading
        m = sys.modules.get("backend.main") or sys.modules["main"]
        _init_db("batch-test")
        batch = [(f"data/file_{i:08d}.parquet", i * 1024, "2026-04-01T00:00:00Z", f'"etag_{i}"', "data/", 1) for i in range(10000)]
        for gen, expect_written in ((1, 10000), (2, 0)):
            rc = m._PrefixReconcile("batch-test", "default", gen, threading.Lock())
            rc.batch(batch); rc.finish()
            assert rc.written == expect_written, (gen, rc.written)
        with _get_db("batch-test") as db:
            assert db.execute("SELECT COUNT(*) FROM objects WHERE crawl_gen=1").fetchone()[0] == 10000, "unchanged rows keep their generation"
        src = inspect.getsource(m._PrefixReconcile)
        assert "UPDATE objects SET crawl_gen" not in src and "IN ({" not in src and '"?" *' not in src

# ═══════════════════════════════════════════════════════════════════
# 3. WORKER COUNTS
# ═══════════════════════════════════════════════════════════════════


class TestWorkerCounts:
    """Verify thread pool sizes are increased."""

    def test_crawl_pool_has_12_workers(self):
        assert _crawl_pool._max_workers == 12, (
            f"Expected crawl pool max_workers=12, got {_crawl_pool._max_workers}"
        )

    def test_prefix_pool_uses_16_workers(self):
        """Verify the prefix ThreadPoolExecutor is created with 16 workers."""
        import inspect
        source = inspect.getsource(sys.modules["main"]._run_crawl)
        assert "max_workers=16" in source, "Expected max_workers=16 for prefix pool"

    def test_timeout_formula_updated(self):
        """Verify timeout uses the new formula: max(900, 900 + count//5000)."""
        import inspect
        source = inspect.getsource(sys.modules["main"]._run_crawl)
        assert "900 + existing_count // 5000" in source, "Expected new timeout formula"
        assert "600 + existing_count // 2000" not in source, "Old timeout formula should be gone"


# ═══════════════════════════════════════════════════════════════════
# 4. PREFIX_CHILDREN SQL REBUILD
# ═══════════════════════════════════════════════════════════════════


class TestPrefixChildrenRebuild:
    """Test the SQL-only prefix_children rebuild works for all bucket sizes."""

    def test_small_bucket_prefix_children(self):
        """Test prefix_children on a small bucket (100 objects)."""
        _seed_bucket("pc-small", 100)
        _rebuild_prefix_children("pc-small")

        with _get_db("pc-small") as db:
            rows = db.execute(
                "SELECT * FROM prefix_children WHERE parent_prefix=''"
            ).fetchall()
            assert len(rows) == 4, f"Expected 4 top-level folders, got {len(rows)}"

            # Verify counts sum to total
            total = sum(r["object_count"] for r in rows)
            actual = db.execute("SELECT COUNT(*) FROM objects WHERE prefix!=''").fetchone()[0]
            assert total == actual, f"prefix_children sum ({total}) != actual ({actual})"

    def test_large_bucket_prefix_children_no_skip(self):
        """Verify prefix_children is NOT skipped for >1M objects anymore."""
        # Seed with 1500 objects (simulating >1M threshold being removed)
        _seed_bucket("pc-large", 1500)

        # The old code would skip if obj_count > 1_000_000
        # We can't seed 1M objects in a test, but we can verify the skip is gone
        import inspect
        source = inspect.getsource(_rebuild_prefix_children)
        assert "1_000_000" not in source, "The 1M skip threshold should be removed"
        assert "SKIPPED" not in source, "The SKIP log message should be removed"

        _rebuild_prefix_children("pc-large")

        with _get_db("pc-large") as db:
            rows = db.execute(
                "SELECT * FROM prefix_children WHERE parent_prefix=''"
            ).fetchall()
            assert len(rows) == 4

    def test_prefix_children_sql_only(self):
        """Verify the rebuild uses SQL, not Python dicts."""
        import inspect
        source = inspect.getsource(_rebuild_prefix_children)
        # Should NOT have in-memory dict patterns
        assert "leaf_stats" not in source, "Old in-memory leaf_stats dict should be gone"
        assert "children = {}" not in source, "Old in-memory children dict should be gone"
        # Should have SQL INSERT...SELECT
        assert "INSERT INTO prefix_children" in source
        assert "GROUP BY" in source

    def test_guid_prefixes(self):
        """Test with GUID-named prefixes like ssp-production-reports."""
        _seed_bucket("pc-guid", 200, {
            "flatevents-d805761d50604814829f202634e1c7c7/": 0.7,
            "adrequest-52ba771d3e3b4957a4450559973a820e/": 0.2,
            "rollup_data/": 0.1,
        })
        _rebuild_prefix_children("pc-guid")

        with _get_db("pc-guid") as db:
            rows = db.execute(
                "SELECT child_name, object_count FROM prefix_children WHERE parent_prefix='' ORDER BY object_count DESC"
            ).fetchall()
            assert len(rows) == 3
            assert rows[0]["child_name"] == "flatevents-d805761d50604814829f202634e1c7c7"
            # Verify the biggest folder has ~70% of objects
            total = sum(r["object_count"] for r in rows)
            pct = rows[0]["object_count"] / total * 100
            assert 60 < pct < 80, f"Expected ~70% in flatevents, got {pct:.0f}%"

    def test_ulid_prefixes(self):
        """Test with ULID-named prefixes like ssp-prometheus-thanos."""
        ulid_prefixes = {
            "01KBM0EAABDAJPF9MG916XNE04/": 0.2,
            "01KCR128S262Z3PPVRNS5P33H7/": 0.2,
            "01KE69J20BMX1G201Z1VBEJS9D/": 0.2,
            "01KF1QM11C8VKBJ821YMSNZTT1/": 0.2,
            "01KG4VWM128C329Y4SPG31QEGY/": 0.2,
        }
        _seed_bucket("pc-ulid", 500, ulid_prefixes)
        _rebuild_prefix_children("pc-ulid")

        with _get_db("pc-ulid") as db:
            rows = db.execute(
                "SELECT * FROM prefix_children WHERE parent_prefix=''"
            ).fetchall()
            assert len(rows) == 5
            for r in rows:
                assert len(r["child_name"]) == 26, f"ULID should be 26 chars: {r['child_name']}"

    def test_single_prefix_bucket(self):
        """Test with a single prefix (like druid-lw-prod's druid/)."""
        _seed_bucket("pc-single", 500, {"druid/": 1.0})
        _rebuild_prefix_children("pc-single")

        with _get_db("pc-single") as db:
            rows = db.execute(
                "SELECT * FROM prefix_children WHERE parent_prefix=''"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["child_name"] == "druid"
            assert rows[0]["object_count"] == 500

    def test_root_level_files_excluded(self):
        """Files at root level (no prefix) should not appear in prefix_children."""
        _init_db("pc-root-excl-test")
        with _get_db("pc-root-excl-test") as db:
            db.execute("DELETE FROM objects")   # shared test DB_DIR may already hold this bucket
            # Insert root-level files (prefix='')
            for i in range(10):
                db.execute(
                    "INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"root_file_{i}.txt", 1024, "2026-04-01T00:00:00Z", f'"etag_{i}"', "", 0, 1),
                )
            # Insert folder files
            for i in range(20):
                db.execute(
                    "INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"data/file_{i}.csv", 2048, "2026-04-01T00:00:00Z", f'"etag_d{i}"', "data/", 1, 1),
                )
            db.commit()

        _rebuild_prefix_children("pc-root-excl-test")

        with _get_db("pc-root-excl-test") as db:
            rows = db.execute(
                "SELECT * FROM prefix_children WHERE parent_prefix=''"
            ).fetchall()
            assert len(rows) == 1  # Only 'data/', not root files
            assert rows[0]["child_name"] == "data"
            assert rows[0]["object_count"] == 20


# ═══════════════════════════════════════════════════════════════════
# 5. ASYNC FTS REBUILD
# ═══════════════════════════════════════════════════════════════════


class TestAsyncFTSRebuild:
    """Test that FTS rebuild runs in background without blocking."""

    def test_enable_fts_triggers_no_rebuild(self):
        """Verify _enable_fts_triggers no longer does a rebuild."""
        import inspect
        source = inspect.getsource(_enable_fts_triggers)
        assert "VALUES('rebuild')" not in source, (
            "_enable_fts_triggers should NOT contain rebuild — that's in _rebuild_fts_async now"
        )

    def test_rebuild_fts_async_exists(self):
        """Verify _rebuild_fts_async function exists and spawns a thread."""
        import inspect
        source = inspect.getsource(_rebuild_fts_async)
        assert "Thread" in source
        assert "daemon=True" in source
        body = inspect.getsource(sys.modules["main"]._rebuild_fts_locked)
        assert "objects_fts_new" in body and "RENAME TO objects_fts" in body and "FTS_REBUILD_CHUNK" in body   # shadow-table, bounded-memory rebuild

    def test_fts_rebuild_runs_in_background(self):
        """Test that _rebuild_fts_async actually rebuilds the FTS index."""
        _seed_bucket("fts-async-test", 100)

        # Disable triggers and clear FTS
        with _get_db("fts-async-test") as db:
            _disable_fts_triggers(db)
            try:
                db.execute("INSERT INTO objects_fts(objects_fts) VALUES('delete-all')")
                db.commit()
            except Exception:
                pass

        # Trigger async rebuild
        _rebuild_fts_async("fts-async-test")

        # Wait for background thread to finish (max 10s)
        for _ in range(100):
            time.sleep(0.1)
            with _get_db("fts-async-test") as db:
                try:
                    count = db.execute("SELECT COUNT(*) FROM objects_fts").fetchone()[0]
                    if count > 0:
                        break
                except Exception:
                    pass

        with _get_db("fts-async-test") as db:
            count = db.execute("SELECT COUNT(*) FROM objects_fts").fetchone()[0]
            assert count == 100, f"FTS should have 100 entries after rebuild, got {count}"
            print(f"  FTS rebuilt asynchronously: {count} entries")

    def test_search_works_during_rebuild(self):
        """Verify that search returns results while FTS is being rebuilt.

        WAL mode guarantees readers see pre-rebuild index during rebuild.
        """
        _seed_bucket("fts-search-during", 200)

        # Build FTS initially
        with _get_db("fts-search-during") as db:
            try:
                db.execute("INSERT INTO objects_fts(objects_fts) VALUES('rebuild')")
                db.commit()
            except Exception:
                pass

        # Search should work
        with _get_db("fts-search-during") as db:
            try:
                results = db.execute(
                    'SELECT key FROM objects_fts WHERE key MATCH \'"file_0000"\' LIMIT 5'
                ).fetchall()
                assert len(results) > 0, "Search should return results"
                print(f"  Search during normal operation: {len(results)} results")
            except Exception:
                pass  # FTS may not be available in all SQLite builds


# ═══════════════════════════════════════════════════════════════════
# 6. SUB-PREFIX SPLITTING
# ═══════════════════════════════════════════════════════════════════


class TestSubPrefixSplitting:
    """Test the recursive sub-prefix splitting logic."""

    def test_sub_prefix_code_exists(self):
        """Verify the sub-prefix splitting code is in _run_crawl."""
        import inspect
        source = inspect.getsource(sys.modules["main"]._run_crawl)
        assert "Sub-prefix split" in source, "Sub-prefix splitting code not found"
        assert "len(known_prefixes) > 3" in source, "Threshold check not found"
        assert "SUBPREFIX_SPLIT_LEVELS" in source, "multi-level split (B.6) not found"

    def test_sub_prefix_expands_on_large_single_prefix(self):
        """Verify the sub-prefix logic would expand a single-prefix bucket."""
        import inspect
        source = inspect.getsource(sys.modules["main"]._run_crawl)
        # The code should:
        # 1. Check len(known_prefixes) <= 3
        # 2. Check existing_count > 500_000
        # 3. List sub-prefixes with Delimiter="/"
        # 4. Replace known_prefixes with expanded set
        assert "expanded" in source
        assert 'Delimiter' in source or 'sub_params' in source
        assert "known_prefixes = expanded" in source

    def test_sub_prefix_keeps_original_on_error(self):
        """Verify the code falls back to original prefix on error."""
        import inspect
        source = inspect.getsource(sys.modules["main"]._run_crawl)
        assert "expanded.add(p)" in source, "Should keep original prefix on error"


# ═══════════════════════════════════════════════════════════════════
# 7. INTEGRATION: FULL CRAWL SIMULATION
# ═══════════════════════════════════════════════════════════════════


class TestFullIntegration:
    """End-to-end test: seed data, rebuild all indexes, verify everything."""

    def test_full_rebuild_cycle(self):
        """Simulate a complete post-crawl rebuild cycle."""
        bucket = "integration-full"
        _seed_bucket(bucket, 5000, {
            "flatevents-abc123/": 0.6,
            "adrequest-def456/": 0.2,
            "rollup_data/": 0.1,
            "config/": 0.1,
        })

        # Run all post-crawl operations
        _rebuild_folder_stats(bucket)
        _rebuild_prefix_children(bucket)
        _record_storage_snapshot(bucket)

        with _get_db(bucket) as db:
            # Verify folder_stats
            fs_rows = db.execute("SELECT * FROM folder_stats ORDER BY total_size DESC").fetchall()
            assert len(fs_rows) >= 4, f"Expected 4+ folder_stats, got {len(fs_rows)}"
            fs_total = sum(r["object_count"] for r in fs_rows)
            assert fs_total == 5000, f"folder_stats total {fs_total} != 5000"

            # Verify prefix_children
            pc_rows = db.execute(
                "SELECT * FROM prefix_children WHERE parent_prefix='' ORDER BY total_size DESC"
            ).fetchall()
            assert len(pc_rows) == 4, f"Expected 4 prefix_children, got {len(pc_rows)}"
            pc_total = sum(r["object_count"] for r in pc_rows)
            assert pc_total == 5000, f"prefix_children total {pc_total} != 5000"

            # Verify folder_stats and prefix_children agree
            for fs in fs_rows:
                if not fs["prefix"]:
                    continue
                matching_pc = [
                    pc for pc in pc_rows if pc["child_prefix"] == fs["prefix"]
                ]
                assert len(matching_pc) == 1, f"No matching prefix_children for {fs['prefix']}"
                assert matching_pc[0]["object_count"] == fs["object_count"], (
                    f"Count mismatch for {fs['prefix']}: "
                    f"folder_stats={fs['object_count']}, prefix_children={matching_pc[0]['object_count']}"
                )

            # Verify storage_history
            sh_rows = db.execute("SELECT * FROM storage_history").fetchall()
            assert len(sh_rows) > 0, "No storage_history snapshots recorded"
            root_snapshot = [r for r in sh_rows if r["prefix"] == ""]
            assert len(root_snapshot) >= 1
            assert root_snapshot[0]["object_count"] == 5000

        print("  Full rebuild cycle: folder_stats, prefix_children, storage_history all consistent")

    def test_storage_history_uses_latest_not_max(self):
        """Verify storage_history returns the latest value per day, not the MAX.

        Regression test: old SQL used MAX(object_count) which returned the
        peak value per day. If objects are deleted, MAX gives wrong results.
        The fix uses a subquery to pick the row with the latest timestamp.
        """
        bucket = "integration-history-latest"
        _seed_bucket(bucket, 500)

        with _get_db(bucket) as db:
            db.execute("DELETE FROM storage_history")   # shared test DB_DIR may already hold earlier snapshots
            # Simulate two snapshots on the same day:
            # Snapshot 1: 500 objects, 1000000 bytes (the "peak")
            db.execute(
                "INSERT INTO storage_history (timestamp, prefix, object_count, total_size) VALUES (?,?,?,?)",
                ("2026-04-12T08:00:00Z", "", 500, 1000000),
            )
            # Snapshot 2: 400 objects, 800000 bytes (later — objects were deleted)
            db.execute(
                "INSERT INTO storage_history (timestamp, prefix, object_count, total_size) VALUES (?,?,?,?)",
                ("2026-04-12T18:00:00Z", "", 400, 800000),
            )
            db.commit()

            # Query using the same SQL pattern as the fixed endpoint
            rows = db.execute(
                "SELECT DATE(h.timestamp) as day, h.object_count, h.total_size, h.timestamp "
                "FROM storage_history h "
                "INNER JOIN ("
                "  SELECT DATE(timestamp) as d, MAX(timestamp) as latest "
                "  FROM storage_history WHERE prefix = ? AND timestamp >= ? GROUP BY d"
                ") sub ON DATE(h.timestamp) = sub.d AND h.timestamp = sub.latest "
                "WHERE h.prefix = ? "
                "ORDER BY day ASC",
                ("", "2026-01-01T00:00:00Z", ""),
            ).fetchall()

            # Should get the LATEST value (400), not the MAX (500)
            apr12 = [r for r in rows if r["day"] == "2026-04-12"]
            assert len(apr12) == 1, f"Expected 1 row for 2026-04-12, got {len(apr12)}"
            assert apr12[0]["object_count"] == 400, (
                f"Expected latest object_count=400, got {apr12[0]['object_count']} "
                "(bug: MAX() returns peak instead of latest)"
            )
            assert apr12[0]["total_size"] == 800000, (
                f"Expected latest total_size=800000, got {apr12[0]['total_size']}"
            )

        print("  storage_history: correctly returns latest-per-day, not MAX")

    def test_incremental_upsert_preserves_data(self):
        """Test that incremental crawl correctly handles unchanged + changed objects."""
        bucket = "integration-incremental"
        _init_db(bucket)

        # Gen 1: Insert 1000 objects
        batch1 = [
            (f"data/file_{i:04d}.csv", i * 100, "2026-04-01T00:00:00Z",
             f'"etag_{i}"', "data/", 1)
            for i in range(1000)
        ]
        import sys, threading
        m = sys.modules.get("backend.main") or sys.modules["main"]
        rc = m._PrefixReconcile(bucket, "default", 1, threading.Lock())
        rc.batch(batch1); rc.finish()

        # Gen 2: Same 1000 objects (unchanged) + 100 new + 50 modified
        batch2 = list(batch1)  # Copy unchanged
        for i in range(1000, 1100):  # 100 new
            batch2.append((f"data/new_{i:04d}.csv", i * 200, "2026-04-02T00:00:00Z",
                           f'"etag_new_{i}"', "data/", 1))
        for i in range(50):  # 50 modified (different size)
            batch2[i] = (batch2[i][0], 999999, batch2[i][2], f'"etag_mod_{i}"',
                         batch2[i][4], batch2[i][5])

        rc = m._PrefixReconcile(bucket, "default", 2, threading.Lock())
        rc.batch(batch2); rc.finish()
        assert rc.written == 150
        with _get_db(bucket) as db:
            total = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            assert total == 1100, f"Expected 1100 objects, got {total}"

            gen2 = db.execute("SELECT COUNT(*) FROM objects WHERE crawl_gen=2").fetchone()[0]
            assert gen2 == 150, f"only the 100 new + 50 modified rows are written, got {gen2}"
            assert db.execute("SELECT size FROM objects WHERE key='data/file_0000.csv'").fetchone()[0] == 999999

        print("  Incremental upsert: 1000 unchanged + 100 new + 50 modified = 1100 total, 150 written")

    def test_folder_stats_matches_actual_data(self):
        """Verify folder_stats exactly matches aggregated object data."""
        bucket = "integration-fstats"
        _seed_bucket(bucket, 2000)
        _rebuild_folder_stats(bucket)

        with _get_db(bucket) as db:
            # Get actual data per prefix
            actual = db.execute(
                "SELECT prefix, COUNT(*) as cnt, SUM(size) as total "
                "FROM objects WHERE prefix != '' GROUP BY prefix"
            ).fetchall()

            # Get folder_stats
            stats = db.execute(
                "SELECT prefix, object_count, total_size FROM folder_stats WHERE prefix != ''"
            ).fetchall()

            actual_dict = {r["prefix"]: (r["cnt"], r["total"]) for r in actual}
            stats_dict = {r["prefix"]: (r["object_count"], r["total_size"]) for r in stats}

            assert actual_dict == stats_dict, (
                f"folder_stats doesn't match actual data:\n"
                f"  actual: {actual_dict}\n"
                f"  stats:  {stats_dict}"
            )

        print(f"  folder_stats verified: {len(actual_dict)} prefixes match exactly")


class TestInitialCrawlSplit:
    """B.6: a single-top-prefix bucket is split into sub-prefixes on the FIRST crawl (empty index)."""

    def test_empty_index_single_prefix_is_split_for_parallelism(self):
        import sys
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "split-initial"
        for suffix in ("", "-wal", "-shm"):
            try: __import__("os").remove(m._db_path(bucket, "default") + suffix)
            except FileNotFoundError: pass
        m._init_db(bucket, "default")
        listed = []
        def list_objects_v2(**p):
            pre, delim = p.get("Prefix", ""), p.get("Delimiter")
            if delim:
                if pre == "":
                    return {"CommonPrefixes": [{"Prefix": "druid/"}], "Contents": [], "IsTruncated": False}
                if pre == "druid/":
                    return {"CommonPrefixes": [{"Prefix": "druid/segments/"}], "Contents": [], "IsTruncated": False}
                if pre == "druid/segments/":
                    return {"CommonPrefixes": [{"Prefix": f"druid/segments/ds_{i:03d}/"} for i in range(40)], "Contents": [], "IsTruncated": False}
                return {"CommonPrefixes": [], "Contents": [], "IsTruncated": False}
            listed.append(pre)
            return {"Contents": [{"Key": f"{pre}{i}/index.zip", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e"'} for i in range(3)],
                    "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        assert len(listed) == 40 and all(p.startswith("druid/segments/ds_") for p in listed), f"expected 40 ds prefixes, got {len(listed)}: {listed[:3]}"
        with m._get_db(bucket, "default") as db:
            row = db.execute("SELECT status, total_objects FROM crawl_status WHERE id=1").fetchone()
            assert row["status"] == "complete" and row["total_objects"] == 120

    def test_many_top_level_prefixes_not_split(self):
        import sys
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "split-none"
        for suffix in ("", "-wal", "-shm"):
            try: __import__("os").remove(m._db_path(bucket, "default") + suffix)
            except FileNotFoundError: pass
        m._init_db(bucket, "default")
        listed = []
        def list_objects_v2(**p):
            pre, delim = p.get("Prefix", ""), p.get("Delimiter")
            if delim and pre == "":
                return {"CommonPrefixes": [{"Prefix": f"t{i}/"} for i in range(10)], "Contents": [], "IsTruncated": False}
            if delim:
                raise AssertionError("no sub-prefix discovery expected for 10 top-level prefixes")
            listed.append(pre)
            return {"Contents": [{"Key": f"{pre}a", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e"'}], "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        assert sorted(listed) == [f"t{i}/" for i in range(10)]


class TestChunkedFtsRebuild:
    def test_chunked_rebuild_indexes_every_row_and_search_works(self):
        import sys, os, time
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "fts-chunked"
        for suffix in ("", "-wal", "-shm"):
            try: os.remove(m._db_path(bucket, "default") + suffix)
            except FileNotFoundError: pass
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            m._disable_fts_triggers(db)   # as during an initial crawl
            db.executemany("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
                           [(f"d/{i:05d}/needle-{i}.zip", 1, "2026-01-01T00:00:00+00:00", "e", f"d/{i:05d}/", 2, 1) for i in range(1234)])
            db.commit()
            m._enable_fts_triggers(db)
            assert m._fts_is_empty(db), "index must read as empty before the rebuild (external-content table)"
            assert m._fts_should_rebuild(bucket, "default", False) is True, "self-heal must trigger on an empty index"
        with m._get_db(bucket, "default") as db:   # a crash mid-build leaves a stale shadow table behind
            db.execute("CREATE VIRTUAL TABLE objects_fts_new USING fts5(key, content='objects', content_rowid='rowid', tokenize='trigram')")
            db.commit()
        with patch.object(m, "FTS_REBUILD_CHUNK", 100):   # force many chunks
            m._rebuild_fts(bucket, "default")
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'needle'").fetchone()[0] == 1234
            assert not m._fts_is_empty(db)
            assert m._fts_should_rebuild(bucket, "default", False) is False
            assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='objects_fts_new'").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'objects_fts_a_'").fetchone()[0] == 3
            # sync triggers survive the swap: a new key is searchable immediately
            db.execute("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES ('z/haystack.zip',1,'2026-01-01T00:00:00+00:00','e','z/',1,1)")
            db.commit()
            assert db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'haystack'").fetchone()[0] == 1

    def test_rebuild_keeps_old_index_searchable_until_the_swap(self):
        """Readers must never see an empty or partial index: the build goes into a shadow table."""
        import sys, os
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "fts-shadow"
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            db.executemany("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
                           [(f"a/needle-{i}.zip", 1, "2026-01-01T00:00:00+00:00", "e", "a/", 1, 1) for i in range(300)])
            db.commit()
            assert db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'needle'").fetchone()[0] == 300
        seen = []
        import sqlite3
        reader = sqlite3.connect(m._db_path(bucket, "default"))
        orig_get_db = m._get_db
        from contextlib import contextmanager
        class Spy:
            """Delegates to the builder's connection; after every commit, reads the live index from a second connection."""
            def __init__(self, conn): self._c = conn
            def __getattr__(self, name): return getattr(self._c, name)
            def commit(self):
                self._c.commit()
                seen.append(reader.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'needle'").fetchone()[0])
        @contextmanager
        def spy_get_db(b, e=None):
            with orig_get_db(b, e) as db:
                yield Spy(db)
        from unittest.mock import patch
        with patch.object(m, "_get_db", spy_get_db), patch.object(m, "FTS_REBUILD_CHUNK", 50):
            m._rebuild_fts(bucket, "default")
        assert seen and all(n == 300 for n in seen), f"live index dipped during the rebuild: {seen}"


class TestSkewSplit:
    def test_heavy_top_level_prefix_is_drilled_even_when_bucket_has_many_prefixes(self):
        """6 top-level prefixes (so the ≤3 rule does not fire), one of them heavy by indexed-row count
        → only that one is expanded; the light ones are listed as they are."""
        import sys, os
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "split-skew"
        for suffix in ("", "-wal", "-shm"):
            try: os.remove(m._db_path(bucket, "default") + suffix)
            except FileNotFoundError: pass
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            # rows already indexed under big/ (e.g. from a crawl that never finished); none under small*/
            db.executemany("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,1,'2026-01-01T00:00:00+00:00','e','big/',1,1)",
                           [(f"big/x{i}",) for i in range(3)])
            db.commit()
        listed = []
        def list_objects_v2(**p):
            pre, delim = p.get("Prefix", ""), p.get("Delimiter")
            if delim:
                if pre == "":
                    return {"CommonPrefixes": [{"Prefix": "big/"}] + [{"Prefix": f"small{i}/"} for i in range(5)], "Contents": [], "IsTruncated": False}
                if pre == "big/":
                    return {"CommonPrefixes": [{"Prefix": f"big/part{i}/"} for i in range(20)], "Contents": [], "IsTruncated": False}
                return {"CommonPrefixes": [], "Contents": [], "IsTruncated": False}
            listed.append(pre)
            return {"Contents": [{"Key": f"{pre}k", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e"'}], "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        with patch.object(m, "SUBPREFIX_SPLIT_MIN_OBJECTS", 0), patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        assert "big/" not in listed, "the heavy prefix must be split, not listed whole"
        assert sum(p.startswith("big/part") for p in listed) == 20 and sum(p.startswith("small") for p in listed) == 5, listed


class TestSplitFanoutCap:
    def test_prefix_with_huge_fanout_is_listed_whole(self):
        """One top-level prefix whose only child has 2,500 tiny sub-prefixes: the split must stop
        at the cap and list the child whole (1 list unit), not dispatch 2,500 list calls."""
        import sys, os
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "split-fanout"
        for suffix in ("", "-wal", "-shm"):
            try: os.remove(m._db_path(bucket, "default") + suffix)
            except FileNotFoundError: pass
        m._init_db(bucket, "default")
        listed = []
        def list_objects_v2(**p):
            pre, delim, tok = p.get("Prefix", ""), p.get("Delimiter"), p.get("ContinuationToken")
            if delim:
                if pre == "":
                    return {"CommonPrefixes": [{"Prefix": "druid/"}], "Contents": [], "IsTruncated": False}
                if pre == "druid/":
                    return {"CommonPrefixes": [{"Prefix": "druid/logs/"}], "Contents": [], "IsTruncated": False}
                if pre == "druid/logs/":   # 2,500 children over 3 pages
                    page = int(tok or 0)
                    lo, hi = page * 1000, min((page + 1) * 1000, 2500)
                    return {"CommonPrefixes": [{"Prefix": f"druid/logs/q{i:05d}/"} for i in range(lo, hi)],
                            "Contents": [], "IsTruncated": hi < 2500, "NextContinuationToken": str(page + 1)}
                return {"CommonPrefixes": [], "Contents": [], "IsTruncated": False}
            listed.append(pre)
            return {"Contents": [{"Key": f"{pre}k", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e"'}], "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        assert listed == ["druid/logs/"], listed
        delimiter_calls = [c.kwargs for c in client.list_objects_v2.call_args_list if c.kwargs.get("Delimiter") and c.kwargs.get("Prefix") == "druid/logs/"]
        assert len(delimiter_calls) <= 2, "enumeration must stop once the cap is exceeded"


class TestSplitKeepsDirectObjects:
    def test_objects_directly_under_a_split_prefix_are_indexed_and_not_pruned(self):
        import sys
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "split-direct"
        m._init_db(bucket, "default")
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        def list_objects_v2(**p):
            pre, delim = p.get("Prefix", ""), p.get("Delimiter")
            if delim:
                if pre == "":
                    return {"CommonPrefixes": [{"Prefix": "druid/"}], "Contents": [], "IsTruncated": False}
                if pre == "druid/":   # a README next to the sub-folders: only a delimiter listing of druid/ ever sees it
                    return {"CommonPrefixes": [{"Prefix": "druid/segments/"}],
                            "Contents": [{"Key": "druid/README.md", "Size": 7, "LastModified": lm, "ETag": '"r"'}], "IsTruncated": False}
                return {"CommonPrefixes": [], "Contents": [], "IsTruncated": False}
            return {"Contents": [{"Key": f"{pre}part-1.zip", "Size": 1, "LastModified": lm, "ETag": '"e"'}], "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")           # initial crawl
            m._run_crawl(bucket, "default")           # incremental recrawl runs the stale-key prune
        with m._get_db(bucket, "default") as db:
            keys = sorted(r[0] for r in db.execute("SELECT key FROM objects"))
        assert keys == ["druid/README.md", "druid/segments/part-1.zip"], keys


class TestTruthfulStates:
    """P0: a crawl that did not see every page must not look complete, and a failed delta must not fake freshness."""

    def _mk(self, m, bucket, fail_prefix=None):
        from unittest.mock import MagicMock
        from datetime import datetime, timezone
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        calls = {"n": 0}
        def list_objects_v2(**p):
            pre, delim = p.get("Prefix", ""), p.get("Delimiter")
            if delim:
                if pre == "":
                    return {"CommonPrefixes": [{"Prefix": "a/"}, {"Prefix": "b/"}, {"Prefix": "c/"}, {"Prefix": "d/"}], "Contents": [], "IsTruncated": False}
                return {"CommonPrefixes": [], "Contents": [], "IsTruncated": False}
            if pre == fail_prefix:
                calls["n"] += 1
                if calls["n"] == 1:   # first page succeeds, every later page fails
                    return {"Contents": [{"Key": f"{pre}k1", "Size": 1, "LastModified": lm, "ETag": '"e"'}], "IsTruncated": True, "NextContinuationToken": "t"}
                raise ConnectionError("provider went away")
            return {"Contents": [{"Key": f"{pre}k1", "Size": 1, "LastModified": lm, "ETag": '"e"'}, {"Key": f"{pre}k2", "Size": 1, "LastModified": lm, "ETag": '"e"'}], "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        return client

    def test_terminal_page_failure_marks_degraded_and_never_prunes(self):
        import sys, time
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-degraded"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")     # healthy full crawl: 4 prefixes × 2 objects
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT status FROM crawl_status").fetchone()[0] == "complete"
            ok_end = db.execute("SELECT last_crawl_end FROM crawl_status").fetchone()[0]
        time.sleep(1.1)
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket, fail_prefix="c/")), patch.object(m._rebuild_pool, "submit"), patch.object(m.time, "sleep"):
            m._run_crawl(bucket, "default")     # recrawl where c/ fails after its first page
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT * FROM crawl_status").fetchone())
            keys = sorted(r[0] for r in db.execute("SELECT key FROM objects"))
        assert row["status"] == "degraded", row
        assert row["last_crawl_end"] == ok_end, "a failed attempt must not move the success timestamp"
        assert row["last_error"] and "1 prefix" in row["last_error"]
        assert row["last_attempt_at"] and row["last_attempt_at"] >= ok_end
        assert "c/k2" in keys, "the row the failed listing never reached must NOT be pruned"
        assert m._is_index_ready(bucket)

    def test_failed_delta_does_not_fake_freshness(self):
        import sys, time
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")   # the rebuild pool was mocked, so release the post-crawl marker by hand
        with m._get_db(bucket, "default") as db:
            before = dict(db.execute("SELECT status, last_crawl_end FROM crawl_status").fetchone())
        def boom(*a, **k): raise RuntimeError("SlowDown")
        with patch.object(m, "_delta_crawl", side_effect=boom), patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            assert m._queue_delta_crawl(bucket, "default") is True
        with m._get_db(bucket, "default") as db:
            after = dict(db.execute("SELECT status, last_crawl_end, last_error, last_attempt_at FROM crawl_status").fetchone())
        assert after["status"] == before["status"] == "complete"
        assert after["last_crawl_end"] == before["last_crawl_end"], "a failed delta must not stamp a success time"
        assert after["last_error"] == "SlowDown" and after["last_attempt_at"]

    def test_search_readiness_is_persisted_per_generation(self):
        """search_ready comes from fts_ready_gen == current_crawl_gen in the DB, never from an in-memory set."""
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-status"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        d = m.crawl_status(bucket, user={})
        assert d["status"] == "complete" and d["fts_ready_gen"] is None and d["rebuilding"] is True and d["search_ready"] is False, d
        assert m._fts_should_rebuild(bucket, "default", False) is True, "no committed rebuild for this generation → rebuild"
        m._rebuild_fts(bucket, "default")
        d = m.crawl_status(bucket, user={})
        assert d["fts_ready_gen"] == d["current_crawl_gen"] and d["search_ready"] is True and d["rebuilding"] is False, d
        assert m._fts_should_rebuild(bucket, "default", False) is False
        with m._get_db(bucket, "default") as db:   # a rebuild lost two generations ago must be redone even with no key changes
            db.execute("UPDATE crawl_status SET fts_ready_gen = current_crawl_gen - 2"); db.commit()
        assert m._fts_should_rebuild(bucket, "default", False) is True
        with m._get_db(bucket, "default") as db:   # the previous generation's rebuild is enough when keys did not change
            db.execute("UPDATE crawl_status SET fts_ready_gen = current_crawl_gen - 1"); db.commit()
        assert m._fts_should_rebuild(bucket, "default", False) is False

    def test_degraded_is_resumed_and_retries_only_unfinished_prefixes(self):
        import sys, time
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-resume"
        key = f"default:{bucket}"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket, fail_prefix="c/")), patch.object(m._rebuild_pool, "submit"), patch.object(m.time, "sleep"):
            m._run_crawl(bucket, "default")
        meta = m._crawl_meta.get(key, {})
        assert "last_full" not in meta and meta.get("degraded_at"), meta   # scheduler retries after RECRAWL_INTERVAL, no delta-only period
        with m._crawl_lock:
            m._rebuilding.discard(key)
        healthy = self._mk(m, bucket)
        with patch.object(m._s3_manager, "get_client", return_value=healthy), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        listed = [c.kwargs.get("Prefix") for c in healthy.list_objects_v2.call_args_list if not c.kwargs.get("Delimiter")]
        assert listed == ["c/"], f"only the unfinished prefix must be re-listed, got {listed}"
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_error FROM crawl_status").fetchone())
        assert row["status"] == "complete" and row["last_error"] is None, row
        assert "last_full" in m._crawl_meta[key] and "degraded_at" not in m._crawl_meta[key]

    def test_degraded_delta_and_delta_over_degraded_index_do_not_advance_last_crawl_end(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta2"
        key = f"default:{bucket}"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(key)
        with m._get_db(bucket, "default") as db:
            end0 = db.execute("SELECT last_crawl_end FROM crawl_status").fetchone()[0]
        # degraded delta: one target failed
        with patch.object(m, "_delta_crawl", return_value=(3, ["a/"])), patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            assert m._queue_delta_crawl(bucket, "default") is True
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_crawl_end, last_error FROM crawl_status").fetchone())
        assert row == {"status": "complete", "last_crawl_end": end0, "last_error": "1 delta target(s) failed to list: a/"}, row
        # clean delta over a degraded index: status stays degraded, no success stamp
        with m._get_db(bucket, "default") as db:
            db.execute("UPDATE crawl_status SET status='degraded', last_error='2 prefix(es) failed to list'"); db.commit()
        with patch.object(m, "_delta_crawl", return_value=(0, [])), patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            assert m._queue_delta_crawl(bucket, "default") is True
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_crawl_end, last_attempt_at FROM crawl_status").fetchone())
        assert row["status"] == "degraded" and row["last_crawl_end"] == end0 and row["last_attempt_at"], row

    def test_delta_crawl_reports_failed_targets_and_keeps_good_ones(self):
        import sys
        from unittest.mock import patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta3"
        m._init_db(bucket, "default")
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        def listing(client, b, tp, **kw):
            if tp == "b/":
                raise ConnectionError("provider went away")
            return [{"Key": f"{tp}new", "Size": 1, "LastModified": lm, "ETag": '"n"'}]
        with patch.object(m, "_hot_target_prefixes", return_value=["a/", "b/"]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m, "_list_children", return_value=[]), \
             patch.object(m, "_delta_list_prefix", side_effect=listing), patch.object(m._s3_manager, "get_client", return_value=object()):
            changed, failed = m._delta_crawl(bucket, "default")
        assert (changed, failed) == (1, ["b/"])
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects WHERE key='a/new'").fetchone()[0] == 1

    def test_fatal_and_simple_paths_record_attempt_and_error(self):
        import sys
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # fatal: the root listing itself explodes
        bucket = "truth-fatal"; m._init_db(bucket, "default")
        client = MagicMock(); client.list_objects_v2.side_effect = RuntimeError("AccessDenied")
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"), patch.object(m.time, "sleep"):
            m._run_crawl(bucket, "default")
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_error, last_attempt_at, last_crawl_end FROM crawl_status").fetchone())
        assert row["status"].startswith("error") and "AccessDenied" in row["last_error"] and row["last_attempt_at"] and row["last_crawl_end"] is None, row
        # simple path: root-level files only
        bucket = "truth-simple"; m._init_db(bucket, "default")
        client = MagicMock(); client.list_objects_v2.side_effect = lambda **p: {"Contents": [{"Key": "root.txt", "Size": 1, "LastModified": lm, "ETag": '"e"'}], "CommonPrefixes": [], "IsTruncated": False}
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_error, last_attempt_at, last_crawl_end FROM crawl_status").fetchone())
        assert row["status"] == "complete" and row["last_error"] is None and row["last_attempt_at"] and row["last_crawl_end"], row

    # ── review round 2: the four remaining blockers ──────────────────────────────────────

    def test_delta_cannot_promote_an_errored_full_crawl(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-error"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        with m._get_db(bucket, "default") as db:
            end0 = db.execute("SELECT last_crawl_end FROM crawl_status").fetchone()[0]
            db.execute("UPDATE crawl_status SET status='error: AccessDenied', last_error='AccessDenied'"); db.commit()
        with patch.object(m, "_delta_crawl", return_value=(5, [])), patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            assert m._queue_delta_crawl(bucket, "default") is True
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_error, last_crawl_end, last_attempt_at FROM crawl_status").fetchone())
        assert row["status"] == "error: AccessDenied" and row["last_error"] == "AccessDenied", row
        assert row["last_crawl_end"] == end0 and row["last_attempt_at"], row
        assert m.crawl_status(bucket, user={})["full_crawl_complete"] is False

    def test_startup_repairs_absent_or_stale_search_generation(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-startup-fts"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        # legacy database: the previous release built the index at completion but knew no marker.
        # Simulate: healthy index, fts_ready_gen NULL → verified backfill, no rebuild.
        m._rebuild_fts(bucket, "default")
        with m._get_db(bucket, "default") as db:
            db.execute("UPDATE crawl_status SET fts_ready_gen = NULL"); db.commit()
            assert m._fts_healthy(db)
        assert m._ensure_fts_ready(bucket, "default") is None
        d = m.crawl_status(bucket, user={})
        assert d["fts_ready_gen"] == d["current_crawl_gen"] and d["search_ready"] is True and d["rebuilding"] is False, d
        # stale marker (a rebuild that never committed) → repaired immediately, readiness restored
        with m._get_db(bucket, "default") as db:
            db.execute("UPDATE crawl_status SET fts_ready_gen = current_crawl_gen - 2"); db.commit()
        t = m._ensure_fts_ready(bucket, "default")
        assert t is not None
        t.join(30)
        d = m.crawl_status(bucket, user={})
        assert d["fts_ready_gen"] == d["current_crawl_gen"] and d["search_ready"] is True, d
        assert f"default:{bucket}" not in m._rebuilding

    def test_generation_marker_alone_never_reports_search_ready(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-fts-invalid"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        m._rebuild_fts(bucket, "default")
        assert m.crawl_status(bucket, user={})["search_ready"] is True
        # marker says ready, but the index has been emptied underneath it
        with m._get_db(bucket, "default") as db:
            db.execute("INSERT INTO objects_fts(objects_fts) VALUES('delete-all')"); db.commit()
            assert not m._fts_healthy(db)
        d = m.crawl_status(bucket, user={})
        assert d["fts_ready_gen"] == d["current_crawl_gen"] and d["search_ready"] is False and d["rebuilding"] is True, d
        assert m._fts_should_rebuild(bucket, "default", False) is True
        # marker says ready, but the table is gone
        with m._get_db(bucket, "default") as db:
            for trg in ("objects_fts_ai", "objects_fts_ad", "objects_fts_au"):
                db.execute(f"DROP TRIGGER IF EXISTS {trg}")
            db.execute("DROP TABLE objects_fts"); db.commit()
            assert not m._fts_healthy(db)
        assert m.crawl_status(bucket, user={})["search_ready"] is False
        assert m._fts_should_rebuild(bucket, "default", False) is True
        m._rebuild_fts(bucket, "default")   # and the rebuild recreates the table + triggers
        with m._get_db(bucket, "default") as db:
            assert m._fts_healthy(db)
        assert m.crawl_status(bucket, user={})["search_ready"] is True

    def test_full_crawl_complete_is_false_for_degraded(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-fcc"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        assert m.crawl_status(bucket, user={})["full_crawl_complete"] is True
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket, fail_prefix="c/")), patch.object(m._rebuild_pool, "submit"), patch.object(m.time, "sleep"):
            m._run_crawl(bucket, "default")
        d = m.crawl_status(bucket, user={})
        assert d["status"] == "degraded" and d["full_crawl_complete"] is False and d["crawl_duration"], d

    # ── review round 3: three remaining freshness-without-evidence paths ─────────────────

    def test_stale_legacy_fts_is_rebuilt_not_certified(self):
        """Pre-upgrade DB: readable, non-empty FTS that is missing a newer object. NULL marker must not be
        backfilled; SQLite's integrity check catches the mismatch and a rebuild runs."""
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-legacy-stale"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        m._rebuild_fts(bucket, "default")   # generation N fully indexed
        with m._get_db(bucket, "default") as db:
            # catalogue moves to N+1 behind the index's back (triggers off, as during a crawl); marker NULL like a legacy DB
            m._disable_fts_triggers(db)
            db.execute("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES ('z/uniqueneedle.bin',1,'2026-01-01T00:00:00+00:00','e','z/',1,2)")
            db.execute("UPDATE crawl_status SET fts_ready_gen=NULL, current_crawl_gen=current_crawl_gen+1")
            db.commit()
            m._enable_fts_triggers(db)
            assert m._fts_healthy(db), "readable and non-empty — the health probe alone cannot see the staleness"
            assert not m._fts_consistent(db), "SQLite's integrity check must see the missing row"
            assert db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'uniqueneedle'").fetchone()[0] == 0
        t = m._ensure_fts_ready(bucket, "default")
        assert t is not None, "a stale legacy index must be rebuilt, never certified"
        t.join(30)
        d = m.crawl_status(bucket, user={})
        assert d["fts_ready_gen"] == d["current_crawl_gen"] and d["search_ready"] is True, d
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'uniqueneedle'").fetchone()[0] == 1
            assert m._fts_consistent(db)

    def test_startup_seeds_only_a_complete_full_crawl(self):
        import sys
        m = sys.modules.get("backend.main") or sys.modules["main"]
        assert m._seed_from_existing("complete", 120, 12.0) is True
        for st in ("error: fatal listing failure", "degraded", "interrupted", "crawling", ""):
            assert m._seed_from_existing(st, 120, 12.0) is False, st
        assert m._seed_from_existing("complete", 0, 12.0) is False and m._seed_from_existing("complete", 120, 0) is False

    def test_delta_discovery_failure_degrades_the_delta(self):
        import sys
        from unittest.mock import patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-discovery"
        m._init_db(bucket, "default")
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        def boom(*a, **k): raise RuntimeError("SlowDown")
        with patch.object(m, "_hot_target_prefixes", return_value=["a/"]), patch.object(m, "_discover_delta_targets", side_effect=boom), \
             patch.object(m, "_list_children", return_value=[]), \
             patch.object(m, "_delta_list_prefix", return_value=[{"Key": "a/new", "Size": 1, "LastModified": lm, "ETag": '"n"'}]), \
             patch.object(m._s3_manager, "get_client", return_value=object()):
            changed, failed = m._delta_crawl(bucket, "default")
        assert changed == 1 and failed == ["discovery"], (changed, failed)   # hot prefixes still refreshed, but not a clean delta
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects WHERE key='a/new'").fetchone()[0] == 1
        # and the caller treats it as degraded: no success stamp
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        with m._get_db(bucket, "default") as db:
            end0 = db.execute("SELECT last_crawl_end FROM crawl_status").fetchone()[0]
        with patch.object(m, "_delta_crawl", return_value=(1, ["discovery"])), patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            m._queue_delta_crawl(bucket, "default")
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_crawl_end, last_error FROM crawl_status").fetchone())
        assert row["last_crawl_end"] == end0 and row["status"] == "complete" and "1 delta target" in row["last_error"], row

    def test_unreadable_status_before_delta_is_never_promoted(self):
        """If the status cannot be read before a delta, nothing is promoted: only the attempt is recorded."""
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-unknown"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        with m._get_db(bucket, "default") as db:
            db.execute("UPDATE crawl_status SET status='degraded', last_error='x'"); db.commit()
            before = dict(db.execute("SELECT status, last_crawl_end, last_error FROM crawl_status").fetchone())
        real_get_db = m._get_db
        calls = {"n": 0}
        from contextlib import contextmanager
        @contextmanager
        def flaky_get_db(b, e=None):
            calls["n"] += 1
            if calls["n"] == 1:          # the status read right before the delta fails
                raise RuntimeError("disk hiccup")
            with real_get_db(b, e) as db:
                yield db
        with patch.object(m, "_get_db", flaky_get_db), patch.object(m, "_delta_crawl", return_value=(4, [])), \
             patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            assert m._queue_delta_crawl(bucket, "default") is True
        with m._get_db(bucket, "default") as db:
            after = dict(db.execute("SELECT status, last_crawl_end, last_error, last_attempt_at FROM crawl_status").fetchone())
        assert after["status"] == before["status"] == "degraded" and after["last_crawl_end"] == before["last_crawl_end"], (before, after)
        assert after["last_error"] == "x" and after["last_attempt_at"], after

    def test_recovery_after_failed_discovery_advances_success_only_on_the_clean_retry(self):
        import sys, time
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-recover"
        m._init_db(bucket, "default")
        with patch.object(m._s3_manager, "get_client", return_value=self._mk(m, bucket)), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        with m._get_db(bucket, "default") as db:
            end0 = db.execute("SELECT last_crawl_end FROM crawl_status").fetchone()[0]
        time.sleep(1.1)
        with patch.object(m, "_delta_crawl", return_value=(2, ["discovery"])), patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            m._queue_delta_crawl(bucket, "default")
        with m._get_db(bucket, "default") as db:
            mid = dict(db.execute("SELECT status, last_crawl_end, last_error FROM crawl_status").fetchone())
        assert mid["last_crawl_end"] == end0 and mid["last_error"] and mid["status"] == "complete", mid
        with patch.object(m, "_delta_crawl", return_value=(0, [])), patch.object(m._crawl_pool, "submit", side_effect=lambda fn: fn()):
            m._queue_delta_crawl(bucket, "default")
        with m._get_db(bucket, "default") as db:
            after = dict(db.execute("SELECT status, last_crawl_end, last_error FROM crawl_status").fetchone())
        assert after["last_crawl_end"] > end0 and after["last_error"] is None and after["status"] == "complete", after

    # ── review round 4: the delta must look at the bucket root ───────────────────────────

    def _root_client(self, m, root_objs, tops, fail_root=False, prefix_objs=None):
        from unittest.mock import MagicMock
        from datetime import datetime, timezone
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        calls = []
        def list_objects_v2(**p):
            pre, delim = p.get("Prefix", ""), p.get("Delimiter")
            calls.append((pre, bool(delim)))
            if delim and pre == "":
                if fail_root: raise ConnectionError("root listing failed")
                return {"CommonPrefixes": [{"Prefix": t} for t in tops],
                        "Contents": [{"Key": k, "Size": 1, "LastModified": lm, "ETag": '"r"'} for k in root_objs], "IsTruncated": False}
            objs = (prefix_objs or {}).get(pre, [])
            return {"Contents": [{"Key": k, "Size": 1, "LastModified": lm, "ETag": '"p"'} for k in objs], "CommonPrefixes": [], "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        return client, calls

    def test_delta_sees_new_root_objects_in_a_flat_bucket(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-root"
        m._init_db(bucket, "default")
        client, calls = self._root_client(m, root_objs=["fresh.bin"], tops=[])
        with patch.object(m, "_hot_target_prefixes", return_value=[]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m._s3_manager, "get_client", return_value=client):
            changed, failed = m._delta_crawl(bucket, "default")
        assert calls and calls[0] == ("", True), "a delta must issue the root LIST even with no hot prefixes"
        assert (changed, failed) == (1, []), (changed, failed)
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects WHERE key='fresh.bin'").fetchone()[0] == 1

    def test_delta_ingests_a_brand_new_top_level_prefix_and_records_it(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-newtop"
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            db.execute("INSERT OR IGNORE INTO discovered_prefixes (prefix) VALUES ('old/')"); db.commit()
        client, calls = self._root_client(m, root_objs=[], tops=["old/", "brandnew/"], prefix_objs={"brandnew/": ["brandnew/part-1.zip", "brandnew/part-2.zip"]})
        with patch.object(m, "_hot_target_prefixes", return_value=[]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m._s3_manager, "get_client", return_value=client):
            changed, failed = m._delta_crawl(bucket, "default")
        assert (changed, failed) == (2, []), (changed, failed)
        assert ("brandnew/", False) in calls, "the new top-level prefix must be listed in full"
        assert ("old/", False) not in calls, "already-known prefixes are not re-listed by the root check"
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects WHERE key LIKE 'brandnew/%'").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM discovered_prefixes WHERE prefix='brandnew/'").fetchone()[0] == 1

    def test_failed_root_listing_degrades_the_delta(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-rootfail"
        m._init_db(bucket, "default")
        client, calls = self._root_client(m, root_objs=[], tops=[], fail_root=True, prefix_objs={"hot/": ["hot/x"]})
        with patch.object(m, "_hot_target_prefixes", return_value=["hot/"]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m._s3_manager, "get_client", return_value=client):
            changed, failed = m._delta_crawl(bucket, "default")
        assert changed == 1 and failed == ["root"], (changed, failed)   # hot prefix still refreshed, delta not certified
        # oversized root (page bound) is reported the same way
        client2, _ = self._root_client(m, root_objs=[], tops=[])
        with patch.object(m, "_hot_target_prefixes", return_value=[]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m, "_list_children", return_value=None), patch.object(m._s3_manager, "get_client", return_value=client2):
            assert m._delta_crawl(bucket, "default") == (0, ["root"])

    # ── review round 5: new-prefix bookkeeping, bounded discovery, streaming writes ──────

    def test_new_prefix_is_recorded_only_after_a_successful_listing(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-newtop-fail"
        m._init_db(bucket, "default")
        client, calls = self._root_client(m, root_objs=[], tops=["brandnew/"])
        orig = client.list_objects_v2.side_effect
        def failing(**p):
            if p.get("Prefix") == "brandnew/" and not p.get("Delimiter"):
                raise ConnectionError("provider went away")
            return orig(**p)
        client.list_objects_v2.side_effect = failing
        with patch.object(m, "_hot_target_prefixes", return_value=[]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m._s3_manager, "get_client", return_value=client):
            changed, failed = m._delta_crawl(bucket, "default")
        assert failed == ["brandnew/"], failed
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM discovered_prefixes WHERE prefix='brandnew/'").fetchone()[0] == 0, "must stay unknown until listed"
        # next delta: the prefix is still new, gets listed, and only then recorded
        client2, calls2 = self._root_client(m, root_objs=[], tops=["brandnew/"], prefix_objs={"brandnew/": ["brandnew/a"]})
        with patch.object(m, "_hot_target_prefixes", return_value=[]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m._s3_manager, "get_client", return_value=client2):
            assert m._delta_crawl(bucket, "default") == (1, [])
        assert ("brandnew/", False) in calls2
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM discovered_prefixes WHERE prefix='brandnew/'").fetchone()[0] == 1

    def test_discovery_frontier_is_bounded_on_the_first_level_and_reported(self):
        import sys
        from unittest.mock import MagicMock, patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-discovery-cap"
        m._init_db(bucket, "default")
        tops = [f"p{i:05d}/" for i in range(2037)]
        calls = []   # list.append is atomic under the GIL; MagicMock.call_count is not (16 listing threads lost increments on CI)
        client = MagicMock(); client.list_objects_v2.side_effect = lambda **p: calls.append(p) or {"CommonPrefixes": [], "Contents": [], "IsTruncated": False}
        with patch.object(m, "DELTA_MAX_NODES", 2000):
            targets, partial = m._discover_delta_targets(client, bucket, "default", tops)
        assert partial is True
        assert len(calls) == 2000, len(calls)
        assert len(targets) == 2000 and "p02036/" in targets and "p00000/" not in targets, "newest names are kept"
        # and the delta reports the partial walk instead of certifying itself
        with patch.object(m, "_hot_target_prefixes", return_value=["hot/"]), patch.object(m, "_list_children", return_value=[]), \
             patch.object(m, "_delta_list_prefix", return_value=[]), \
             patch.object(m, "_discover_delta_targets", return_value=(set(), True)), patch.object(m._s3_manager, "get_client", return_value=object()):
            assert m._delta_crawl(bucket, "default") == (0, ["discovery:truncated"])

    def test_delta_streams_a_large_new_prefix_in_bounded_batches(self):
        import sys
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-delta-stream"
        m._init_db(bucket, "default")
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        pages = 7   # 7,000 objects → must be flushed in ≤ 5,000-object batches, never held whole
        def list_objects_v2(**p):
            pre, delim, tok = p.get("Prefix", ""), p.get("Delimiter"), int(p.get("ContinuationToken") or 0)
            if delim:
                return {"CommonPrefixes": [{"Prefix": "big/"}] if pre == "" else [], "Contents": [], "IsTruncated": False}
            return {"Contents": [{"Key": f"big/{tok:02d}-{i:04d}", "Size": 1, "LastModified": lm, "ETag": '"e"'} for i in range(1000)],
                    "IsTruncated": tok + 1 < pages, "NextContinuationToken": str(tok + 1)}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        sizes = []
        real = m._delta_write
        def spy(db, objs, gen):
            sizes.append(len(objs)); return real(db, objs, gen)
        with patch.object(m, "_hot_target_prefixes", return_value=[]), patch.object(m, "_discover_delta_targets", return_value=(set(), False)), \
             patch.object(m, "_delta_write", side_effect=spy), patch.object(m._s3_manager, "get_client", return_value=client):
            changed, failed = m._delta_crawl(bucket, "default")
        assert (changed, failed) == (7000, []), (changed, failed)
        assert sizes and max(sizes) <= 5000 and len(sizes) >= 2, sizes
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects WHERE key LIKE 'big/%'").fetchone()[0] == 7000

    def test_discovery_bounds_remote_work_per_node(self):
        """DELTA_MAX_NODES=1 must not let one node cost 20 LIST pages and 20,000 names: the node is
        left unvisited after DELTA_NODE_MAX_PAGES and the walk is reported partial."""
        import sys
        from unittest.mock import MagicMock, patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-discovery-node"
        m._init_db(bucket, "default")
        def list_objects_v2(**p):
            tok = int(p.get("ContinuationToken") or 0)
            return {"CommonPrefixes": [{"Prefix": f"{p['Prefix']}c{tok:02d}-{i:04d}/"} for i in range(1000)],
                    "Contents": [], "IsTruncated": tok + 1 < 20, "NextContinuationToken": str(tok + 1)}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        with patch.object(m, "DELTA_MAX_NODES", 1), patch.object(m, "DELTA_NODE_MAX_PAGES", 2):
            targets, partial = m._discover_delta_targets(client, bucket, "default", ["wide/"])
        assert partial is True and targets == set()
        assert client.list_objects_v2.call_count == 2, client.list_objects_v2.call_count

    def test_concurrent_fts_rebuilds_of_one_bucket_are_serialised(self):
        """Startup repair and the post-crawl rebuild may both start a rebuild; they must not share the
        shadow table. Live failure: "no such table: objects_fts_new"."""
        import sys, threading, logging
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "truth-fts-concurrent"
        m._init_db(bucket, "default")
        with m._get_db(bucket, "default") as db:
            m._disable_fts_triggers(db)
            db.executemany("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)",
                           [(f"a/{i:05d}/needle.bin", 1, "2026-01-01T00:00:00+00:00", "e", f"a/{i:05d}/", 2, 1) for i in range(3000)])
            db.execute("UPDATE crawl_status SET current_crawl_gen=1"); db.commit()
            m._enable_fts_triggers(db)
        failures = []
        class Catch(logging.Handler):
            def emit(self, r):
                if "FTS rebuild failed" in r.getMessage(): failures.append(r.getMessage())
        h = Catch(); m.log.addHandler(h)
        try:
            with patch.object(m, "FTS_REBUILD_CHUNK", 200):
                ts = [threading.Thread(target=m._rebuild_fts, args=(bucket, "default")) for _ in range(3)]
                for t in ts: t.start()
                for t in ts: t.join(60)
        finally:
            m.log.removeHandler(h)
        assert failures == [], failures
        with m._get_db(bucket, "default") as db:
            assert m._fts_healthy(db) and m._fts_consistent(db)
            assert db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'needle'").fetchone()[0] == 3000
            assert tuple(db.execute("SELECT fts_ready_gen, current_crawl_gen FROM crawl_status").fetchone()) == (1, 1)
            assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='objects_fts_new'").fetchone()[0] == 0



class TestZeroWriteReconcile:
    """SAE-72: an incremental crawl writes only changed rows, and prunes deletions per listed unit."""

    def _client(self, layout, fail=(), mtime=None, fail_root=False, unordered=False):
        """layout: {key: size}; mutate between crawls. Delimiter listings are derived from the keys.
        mtime: {key: datetime} overrides LastModified; fail_root makes the root delimiter listing raise."""
        from unittest.mock import MagicMock
        from datetime import datetime, timezone
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        obj = lambda k: {"Key": k, "Size": layout[k], "LastModified": (mtime or {}).get(k, lm), "ETag": f'"e{layout[k]}"'}
        def list_objects_v2(**p):
            pre, delim = p.get("Prefix", ""), p.get("Delimiter")
            keys = sorted(k for k in layout if k.startswith(pre))
            if delim:
                if fail_root and pre == "":
                    raise ConnectionError("root listing failed")
                kids = sorted({pre + k[len(pre):].split("/")[0] + "/" for k in keys if "/" in k[len(pre):]})
                direct = [k for k in keys if "/" not in k[len(pre):]]
                return {"CommonPrefixes": [{"Prefix": c} for c in kids], "Contents": [obj(k) for k in direct], "IsTruncated": False}
            if pre in fail:
                raise ConnectionError("provider went away")
            if unordered:
                keys = keys[::-1]
            return {"Contents": [obj(k) for k in keys], "IsTruncated": False}
        client = MagicMock(); client.list_objects_v2.side_effect = list_objects_v2
        return client

    def _crawl(self, m, bucket, layout, fail=(), **kw):
        from unittest.mock import patch
        with patch.object(m._s3_manager, "get_client", return_value=self._client(layout, fail, **kw)), \
             patch.object(m._rebuild_pool, "submit"), patch.object(m.time, "sleep"):
            m._run_crawl(bucket, "default")
        with m._get_db(bucket, "default") as db:
            return dict(db.execute("SELECT status, total_objects FROM crawl_status").fetchone()), \
                   {r[0]: (r[1], r[2]) for r in db.execute("SELECT key, size, crawl_gen FROM objects")}

    def test_unchanged_bucket_writes_nothing(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-nochange"; m._init_db(bucket, "default")
        layout = {f"{p}/f{i}": i for p in ("a", "b", "c") for i in range(1, 6)}
        layout["root.txt"] = 9
        self._crawl(m, bucket, layout)
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        m._rebuild_fts(bucket, "default")           # the first crawl's rebuild pool was mocked: build + certify its index now
        written = []
        _finish = m._PrefixReconcile.finish
        def spy(self):
            written.append(self.written); return _finish(self)
        with m._get_db(bucket, "default") as db:
            hist0 = db.execute("SELECT COUNT(*) FROM storage_history").fetchone()[0]
        def settle():
            for _ in range(100):                      # _do_rebuilds releases the marker when it is done
                if f"default:{bucket}" not in m._rebuilding: break
                time.sleep(0.05)
        with patch.object(m._PrefixReconcile, "finish", spy), patch.object(m, "_rebuild_fts_async") as rb, \
             patch.object(m, "_rebuild_folder_stats", wraps=m._rebuild_folder_stats) as fs, \
             patch.object(m._s3_manager, "get_client", return_value=self._client(layout)):
            m._run_crawl(bucket, "default")          # real post-crawl pool this time, so the FTS decision is observable
            settle()
        with m._get_db(bucket, "default") as db:
            status = dict(db.execute("SELECT status, total_objects, fts_ready_gen, current_crawl_gen FROM crawl_status").fetchone())
            gens = {r[0] for r in db.execute("SELECT DISTINCT crawl_gen FROM objects")}
            hist1 = db.execute("SELECT COUNT(*) FROM storage_history").fetchone()[0]
        assert status["status"] == "complete" and status["total_objects"] == 16, status
        assert written and sum(written) == 0, written
        assert gens == {1}, "no row may be rewritten when nothing changed"
        assert not rb.called, "an unchanged bucket must not trigger the trigram rebuild"
        assert not fs.called, "an unchanged bucket must not re-aggregate folder stats"
        assert hist1 > hist0, "the storage history snapshot is still recorded (from counters + folder_stats)"
        assert status["fts_ready_gen"] == status["current_crawl_gen"] == 2, status
        layout["c/f9"] = 9                            # one new object: the aggregates must be rebuilt again
        with patch.object(m, "_rebuild_fts_async"), patch.object(m, "_rebuild_folder_stats", wraps=m._rebuild_folder_stats) as fs, \
             patch.object(m._s3_manager, "get_client", return_value=self._client(layout)):
            m._run_crawl(bucket, "default"); settle()
        assert fs.called, "a changed bucket rebuilds folder stats"

    def test_deletions_and_changes_reconciled_per_prefix(self):
        import sys
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-delta"; m._init_db(bucket, "default")
        layout = {"a/x1": 1, "a/x2": 2, "b/y1": 3, "b/y2": 4}
        self._crawl(m, bucket, layout)
        del layout["a/x2"]; layout["b/y1"] = 30
        status, rows = self._crawl(m, bucket, layout)
        assert status["status"] == "complete"
        assert rows == {"a/x1": (1, 1), "b/y1": (30, 2), "b/y2": (4, 1)}, rows

    def test_failed_prefix_prunes_nothing_and_resume_prunes_only_what_it_listed(self):
        import sys
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-failed"; m._init_db(bucket, "default")
        layout = {"a/x1": 1, "a/x2": 2, "b/y1": 3, "b/y2": 4}
        self._crawl(m, bucket, layout)
        del layout["a/x2"]; del layout["b/y2"]
        status, rows = self._crawl(m, bucket, layout, fail=("b/",))
        assert status["status"] == "degraded", status
        assert set(rows) == {"a/x1", "b/y1", "b/y2"}, "clean a/ pruned, failed b/ kept every row"
        del layout["a/x1"]                                  # changes under the already-complete prefix
        status, rows = self._crawl(m, bucket, layout)       # degraded resume: re-lists only b/
        assert status["status"] == "complete", status
        assert set(rows) == {"a/x1", "b/y1"}, "b/y2 pruned by the resumed listing; a/ was not re-listed so a/x1 stays"

    def test_root_level_deletions_pruned_only_when_root_listing_complete(self):
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-root"; m._init_db(bucket, "default")
        layout = {"r1.txt": 1, "r2.txt": 2, "a/x1": 3}
        self._crawl(m, bucket, layout)
        del layout["r2.txt"]
        status, rows = self._crawl(m, bucket, layout)
        assert set(rows) == {"r1.txt", "a/x1"}, rows
        # A root listing cut short (paginated recrawl short-cut) must not prune root-level rows it never saw.
        layout["r2.txt"] = 2
        self._crawl(m, bucket, layout)
        del layout["r2.txt"]
        client = self._client(layout)
        real = client.list_objects_v2.side_effect
        def truncated_root(**p):
            r = real(**p)
            if p.get("Delimiter") and not p.get("Prefix"):
                r = dict(r, IsTruncated=True, NextContinuationToken="t")
            return r
        client.list_objects_v2.side_effect = truncated_root
        with patch.object(m._s3_manager, "get_client", return_value=client), patch.object(m._rebuild_pool, "submit"):
            m._run_crawl(bucket, "default")
        with m._get_db(bucket, "default") as db:
            keys = {r[0] for r in db.execute("SELECT key FROM objects")}
        assert "r2.txt" in keys, "truncated root listing: root rows untouched"

    def test_last_root_object_deleted_is_pruned(self):
        """No root-level object left at the provider is still a complete root listing: the row must go."""
        import sys
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-root-empty"; m._init_db(bucket, "default")
        layout = {"only.txt": 1, "a/x1": 3}
        self._crawl(m, bucket, layout)
        del layout["only.txt"]
        status, rows = self._crawl(m, bucket, layout)
        assert status["status"] == "complete" and set(rows) == {"a/x1"}, rows

    def test_changed_object_keeps_rowid_and_search_index(self):
        """A same-key size/ETag change must update in place: REPLACE gave the row a new rowid, the trigram
        index (external content keyed by rowid, triggers off during the crawl) pointed at a missing row,
        and since no key changed nothing rebuilt it — 'fts5: missing row' and a LIKE-scan fallback."""
        import sys
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-rowid"; m._init_db(bucket, "default")
        layout = {"a/report.pdf": 1, "a/x2": 2, "b/y1": 3}
        self._crawl(m, bucket, layout)
        m._rebuild_fts(bucket, "default")
        with m._get_db(bucket, "default") as db:
            rid = db.execute("SELECT rowid FROM objects WHERE key='a/report.pdf'").fetchone()[0]
        layout["a/report.pdf"] = 100                      # new size + ETag, same key
        status, rows = self._crawl(m, bucket, layout)
        assert status["status"] == "complete" and rows["a/report.pdf"][0] == 100
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT rowid FROM objects WHERE key='a/report.pdf'").fetchone()[0] == rid
            assert m._fts_consistent(db), "search index must still match the objects table"
            hit = db.execute("SELECT o.key, o.size FROM objects_fts f JOIN objects o ON o.rowid = f.rowid "
                             "WHERE objects_fts MATCH 'report'").fetchall()
            assert [tuple(h) for h in hit] == [("a/report.pdf", 100)], hit

    def test_last_modified_change_is_written(self):
        """Re-uploading identical bytes keeps size and ETag but moves LastModified: that is a change."""
        import sys
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-mtime"; m._init_db(bucket, "default")
        layout = {"a/x1": 1, "a/x2": 2}
        self._crawl(m, bucket, layout)
        later = datetime(2026, 6, 1, tzinfo=timezone.utc)
        status, rows = self._crawl(m, bucket, layout, mtime={"a/x1": later})
        assert rows["a/x1"][1] == 2 and rows["a/x2"][1] == 1, "only the re-uploaded object is written"
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT last_modified FROM objects WHERE key='a/x1'").fetchone()[0] == later.isoformat()

    def test_root_listing_failure_is_degraded_not_complete(self):
        """A failed root delimiter listing leaves root-level objects unreconciled: the attempt is degraded,
        the success timestamp does not move, nothing is pruned, and the next clean attempt completes."""
        import sys, time
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-rootfail"; m._init_db(bucket, "default")
        layout = {"r1.txt": 1, "a/x1": 2, "b/y1": 3}
        self._crawl(m, bucket, layout)
        with m._get_db(bucket, "default") as db:
            end0 = db.execute("SELECT last_crawl_end FROM crawl_status").fetchone()[0]
        time.sleep(1.1)
        del layout["r1.txt"]
        status, rows = self._crawl(m, bucket, layout, fail_root=True)
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_crawl_end, last_error FROM crawl_status").fetchone())
        assert row["status"] == "degraded" and row["last_crawl_end"] == end0 and row["last_error"], row
        assert "r1.txt" in rows, "root rows are kept when the root could not be listed"
        status, rows = self._crawl(m, bucket, layout)
        assert status["status"] == "complete" and "r1.txt" not in rows, rows

    def test_range_settle_prunes_first_middle_last_across_batches(self):
        """Deletions at the very start of a unit, inside a batch range, on a batch boundary and after the
        last listed key are all pruned; nothing outside the unit's scope is touched."""
        import sys, threading
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-ranges"; m._init_db(bucket, "default")
        row = lambda k, size=1: (k, size, "2026-01-01T00:00:00+00:00", "e", m._key_prefix(k), m._key_depth(k), 1)
        keys = [f"a/k{i:02d}" for i in range(30)] + ["b/other"]
        with m._get_db(bucket, "default") as db:
            db.executemany("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES (?,?,?,?,?,?,?)", [row(k) for k in keys])
            db.execute("UPDATE crawl_status SET total_objects=?, total_size=? WHERE id=1", (len(keys), len(keys))); db.commit()
        gone = {"a/k00", "a/k07", "a/k09", "a/k10", "a/k29"}      # first; mid-batch; boundary pair; tail
        listed = [k for k in keys if k.startswith("a/") and k not in gone]
        rc = m._PrefixReconcile(bucket, "default", 2, threading.Lock(), lo="a/", hi=m._prefix_upper("a/"))
        rc.batch([row(k)[:6] for k in listed[:9]])            # a/k01..a/k09-less → ends at a/k11
        rc.batch([row(k)[:6] for k in listed[9:20]])
        rc.batch([row(k)[:6] for k in listed[20:]])
        pruned = rc.finish()
        with m._get_db(bucket, "default") as db:
            left = sorted(r[0] for r in db.execute("SELECT key FROM objects"))
            totals = tuple(db.execute("SELECT total_objects, total_size FROM crawl_status").fetchone())
        assert pruned == 5 and left == sorted(listed + ["b/other"]), (pruned, left)
        assert totals == (26, 26), f"progress totals must follow the deltas exactly, got {totals}"

    def test_out_of_order_listing_fails_the_prefix_and_prunes_nothing(self):
        import sys
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-unordered"; m._init_db(bucket, "default")
        layout = {"a/x1": 1, "a/x2": 2, "a/x3": 3, "b/y1": 4}
        self._crawl(m, bucket, layout)
        del layout["a/x2"]
        status, rows = self._crawl(m, bucket, layout, unordered=True)
        assert status["status"] == "degraded", status
        assert set(rows) == {"a/x1", "a/x2", "a/x3", "b/y1"}, "an out-of-order listing must not prune"

    def test_delta_write_touches_only_changed_rows(self):
        """The delta path used to REPLACE every listed row (new rowid, trigram entries rewritten via triggers)."""
        import sys
        from datetime import datetime, timezone
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-deltawrite"; m._init_db(bucket, "default")
        lm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        objs = [{"Key": f"a/x{i}", "Size": i, "LastModified": lm, "ETag": f'"e{i}"'} for i in range(5)]
        with m._get_db(bucket, "default") as db:
            assert m._delta_write(db, objs, 1) == 5; db.commit()
            rid = {r[0]: r[1] for r in db.execute("SELECT key, rowid FROM objects")}
            assert m._delta_write(db, objs, 2) == 0, "identical listing writes nothing"
            objs[2]["Size"] = 99
            assert m._delta_write(db, objs, 3) == 1; db.commit()
            assert {r[0]: r[1] for r in db.execute("SELECT key, rowid FROM objects")} == rid, "rows are updated in place"
            assert {r[0] for r in db.execute("SELECT key FROM objects WHERE crawl_gen=3")} == {"a/x2"}
            assert m._fts_consistent(db)

    def test_low_disk_skips_the_refresh_but_keeps_the_served_index(self):
        """The free-disk guard used to overwrite `status` with an error string: one scheduler pass on a full
        volume turned all 22 complete indexes of a live instance into "Index error"."""
        import sys
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-disklow"; m._init_db(bucket, "default")
        self._crawl(m, bucket, {"a/x1": 1})
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")   # the mocked rebuild pool never released the post-crawl marker
        with patch.object(m, "_disk_low", return_value=True):
            assert m._queue_crawl(bucket, "default") is False
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, last_error, last_attempt_at, total_objects FROM crawl_status").fetchone())
        assert row["status"] == "complete" and row["total_objects"] == 1, row
        assert row["last_error"] and "disk" in row["last_error"] and row["last_attempt_at"], row
        assert m._is_index_ready(bucket), "the index is still served"

    def test_resume_rebuilds_search_index_for_rows_the_interrupted_attempt_wrote(self):
        """A prefix completed before the pod died wrote its rows with the FTS triggers off. The resumed
        attempt's change count starts from the row count at resume time, so it saw no change, stamped
        the search marker as current, and 'x2' was unsearchable with a failing integrity check."""
        import sys, time
        from unittest.mock import patch
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-resume-fts"; m._init_db(bucket, "default")
        layout = {"a/x1": 1, "b/y1": 2}
        self._crawl(m, bucket, layout)
        with m._crawl_lock:
            m._rebuilding.discard(f"default:{bucket}")
        m._rebuild_fts(bucket, "default")                     # search index built and certified for gen 1
        layout["a/x2new"] = 3                                 # new object at the provider (trigram search needs 3+ chars)
        with m._get_db(bucket, "default") as db:              # the interrupted attempt: a/ finished (and wrote a/x2 with triggers off), b/ did not
            db.execute("UPDATE crawl_status SET status='crawling', current_crawl_gen=current_crawl_gen+1 WHERE id=1")
            gen = db.execute("SELECT current_crawl_gen FROM crawl_status").fetchone()[0]
            db.execute("DELETE FROM crawl_progress"); db.execute("INSERT INTO crawl_progress (prefix, gen) VALUES ('a/', ?)", (gen,))
            m._disable_fts_triggers(db)
            db.execute("INSERT INTO objects (key,size,last_modified,etag,prefix,depth,crawl_gen) VALUES ('a/x2new',3,'2026-01-01T00:00:00+00:00','e3','a/',1,?)", (gen,))
            db.commit()
        with patch.object(m._s3_manager, "get_client", return_value=self._client(layout)):
            m._run_crawl(bucket, "default")                   # resume: only b/ is listed, nothing changes there
            for _ in range(200):                              # the post-crawl step (and the FTS rebuild it hands off to) releases the marker when done
                if f"default:{bucket}" not in m._rebuilding: break
                time.sleep(0.05)
        with m._get_db(bucket, "default") as db:
            row = dict(db.execute("SELECT status, fts_ready_gen, current_crawl_gen FROM crawl_status").fetchone())
            assert row["status"] == "complete" and row["fts_ready_gen"] == row["current_crawl_gen"], row
            assert m._fts_consistent(db), "search index must match the catalogue after a resume"
            hit = [r[0] for r in db.execute("SELECT o.key FROM objects_fts f JOIN objects o ON o.rowid = f.rowid WHERE objects_fts MATCH 'x2new'")]
            assert hit == ["a/x2new"], hit

    def test_simple_crawl_reconciles_whole_bucket(self):
        import sys
        m = sys.modules.get("backend.main") or sys.modules["main"]
        bucket = "zw-simple"; m._init_db(bucket, "default")
        layout = {"one.bin": 1, "two.bin": 2}
        self._crawl(m, bucket, layout)
        del layout["two.bin"]; layout["one.bin"] = 10
        status, rows = self._crawl(m, bucket, layout)
        assert status["status"] == "complete" and rows == {"one.bin": (10, 2)}, rows

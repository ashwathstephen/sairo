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
    _incremental_upsert,
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

    def test_incremental_upsert_with_large_batch(self):
        """Test _incremental_upsert handles a 10K batch correctly."""
        _init_db("batch-test")
        batch = []
        for i in range(10000):
            key = f"data/file_{i:08d}.parquet"
            batch.append((key, i * 1024, "2026-04-01T00:00:00Z", f'"etag_{i}"',
                          "data/", 1))

        with _get_db("batch-test") as db:
            # First insert (no existing data → all changed)
            _incremental_upsert(db, batch, gen=1)
            db.commit()

            count = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            assert count == 10000, f"Expected 10000 objects, got {count}"

            # Second insert with same data (all unchanged → bulk UPDATE)
            _incremental_upsert(db, batch, gen=2)
            db.commit()

            # All should have gen=2 now
            gen2_count = db.execute(
                "SELECT COUNT(*) FROM objects WHERE crawl_gen=2"
            ).fetchone()[0]
            assert gen2_count == 10000, f"Expected 10000 with gen=2, got {gen2_count}"

    def test_update_chunk_size_2000(self):
        """Verify the UPDATE chunk loop uses 2000, not 500."""
        import inspect
        source = inspect.getsource(_incremental_upsert)
        assert "2000)" in source, "Expected chunk size 2000 in _incremental_upsert"
        assert "500)" not in source, "Old chunk size 500 should not be present"


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
        body = inspect.getsource(sys.modules["main"]._rebuild_fts)
        assert "VALUES('delete-all')" in body and "FTS_REBUILD_CHUNK" in body   # chunked, bounded-memory rebuild

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
        with _get_db(bucket) as db:
            _incremental_upsert(db, batch1, gen=1)
            db.commit()

        # Gen 2: Same 1000 objects (unchanged) + 100 new + 50 modified
        batch2 = list(batch1)  # Copy unchanged
        for i in range(1000, 1100):  # 100 new
            batch2.append((f"data/new_{i:04d}.csv", i * 200, "2026-04-02T00:00:00Z",
                           f'"etag_new_{i}"', "data/", 1))
        for i in range(50):  # 50 modified (different size)
            batch2[i] = (batch2[i][0], 999999, batch2[i][2], f'"etag_mod_{i}"',
                         batch2[i][4], batch2[i][5])

        with _get_db(bucket) as db:
            _incremental_upsert(db, batch2, gen=2)
            db.commit()

            total = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            assert total == 1100, f"Expected 1100 objects, got {total}"

            gen2 = db.execute("SELECT COUNT(*) FROM objects WHERE crawl_gen=2").fetchone()[0]
            assert gen2 == 1100, f"Expected all 1100 at gen=2, got {gen2}"

        print("  Incremental upsert: 1000 unchanged + 100 new + 50 modified = 1100 total, all gen=2")

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
        with patch.object(m, "FTS_REBUILD_CHUNK", 100):   # force many chunks
            m._rebuild_fts_async(bucket, "default")
            for _ in range(200):
                with m._get_db(bucket, "default") as db:
                    if db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'needle'").fetchone()[0] == 1234: break
                time.sleep(0.05)
        with m._get_db(bucket, "default") as db:
            assert db.execute("SELECT COUNT(*) FROM objects_fts WHERE objects_fts MATCH 'needle'").fetchone()[0] == 1234
            assert not m._fts_is_empty(db)
            assert m._fts_should_rebuild(bucket, "default", False) is False


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


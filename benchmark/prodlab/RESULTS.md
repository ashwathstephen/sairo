# Production-shaped lab — results

Environment: kind (1 node, Docker Desktop 12 CPU / 8 GB), chart defaults (1 replica, `Recreate`, PVC, CPU limit 1, memory limit 1Gi),
image `sairo:phase-a` (PR #30), simulator `s3sim` at 40 ms per list call, 19 buckets / 15,500,000 objects, largest 9,900,000,
Druid key layout (`druid/segments/<ds>/<interval>/<version>/<partition>/index.zip`, one object per leaf prefix).

## Run 1 — initial crawl after an empty volume (the "PVC reset" scenario), 2026-09-05

| Observation | Value | Meaning |
|---|---|---|
| Simulator capacity (raw HTTP, 8 clients) | 283 pages/s | the simulator is not the bottleneck |
| Sairo aggregate list rate, 12 buckets concurrently | 23–33 pages/s (≈25–30k objects/s) | **single-process ceiling**: boto3 XML parsing + SQLite inside one Python process (GIL) under the chart's 1-CPU limit; more threads do not help |
| Implied floor for a full crawl | 9.9M ≈ 5.5 min; 15.5M ≈ 8.6 min; 100M ≈ 55 min; 1B ≈ 9 h | per pod, CPU-bound, before any provider latency |
| Initial crawl of a single-top-prefix bucket (`druid/`) | **1 thread** | `_run_crawl` only splits sub-prefixes when `existing_count > 500_000`, which is never true on a first crawl or after a volume wipe → 9,900 sequential pages at provider RTT for the 9.9M bucket. This is the hours-long production initial crawl. |
| Progress reporting during that crawl | `total_objects = 0` for the whole crawl | the counter is updated per completed prefix and there is one prefix → the UI shows "Indexing… 0 objects" for the entire initial crawl |
| Small buckets (110k) | complete in 130–143 s each while competing | fine |
| 300k buckets | ≈380 s each while competing | fine |
| Pod RSS during the 12-bucket herd | peak 791 MB of the 1 GiB limit | OOM risk under the herd; matches the "crashes / OOM" symptom class |
| Index size | ≈1.35–1.4 GB per 1M objects at this key length (`/data` 5.1 GB at ≈3.7M) | 15.5M ≈ 21 GB → the 20 Gi default in A.5 is still too small for this production shape; 30 Gi recommended |
| Restarts / errors (first 10 min) | 0 "database is locked", 0 tracebacks (beyond the known bcrypt version warning) | |
| **OOMKilled** | exit 137 at 13:42:15Z, ≈700 s into the herd, RSS 999 MB / 1 GiB limit; 1,300,000 seg-main rows persisted | the "crashes / OOM" symptom class reproduced at production shape with chart defaults |
| Restart behaviour after the OOM (Phase A) | `Queued resume of interrupted crawl for default:seg-main` → `Sub-prefix split 'druid/' → 1 children` → `Crawl started (1 prefixes, 1,300,000 existing, incremental=True)` | resume worked as designed but had nothing to skip: with one prefix the resume unit is the whole bucket, and the split only went one level. B.6 (split on initial crawl, multi-level) is a prerequisite for resume to help on this layout. |

seg-main single-threaded baseline: **did not complete** before the OOM kill (1.3M rows in ≈700 s while competing with 11 other buckets ≈ 1,900 objects/s). Rerun as seg-main-only for a clean number (Run 2).

## Run 2 — seg-main only (9,900,000 objects), fresh volume, image `phase-a` (single-thread baseline for B.6)

| Metric | Value |
|---|---|
| Threads used for listing | **1** (`Crawl started … (1 prefixes, 0 existing)`) |
| `crawl_duration` | **908.6 s (15.1 min)** at 40 ms/page simulated latency ≈ 10.9k objects/s |
| Progress shown in UI during the crawl | `total_objects = 0` for the whole 15 minutes |
| RSS while listing | ≈ 128 MB, flat |
| RSS at completion (post-crawl rebuild) | **926 MB**, then the pod restarted (restart count 1) — the spike is in the post-crawl phase, not in listing. The folder-table rebuilds aggregate only the top level (tiny here); the **FTS trigram rebuild over 9.9M keys** is the main suspect (verified in Run 3, which runs the same rebuild) |
| Index size | 7.4 GB (≈ 0.75 GB per 1M objects for this layout once compacted) |

Note on production translation: at a real provider RTT of ~200–300 ms per page instead of 40 ms, the same single-threaded crawl is ≈ 9,900 × 0.25 s ≈ 40–50 min of pure waiting on the provider before CPU and write time; with throttling and the herd it becomes the hours observed.

## Run 3 — seg-main only, fresh volume, image `phase-b6` (B.6: multi-level split on the initial crawl)

Startup log: `Sub-prefix split 'druid/' → 1 children` → `Sub-prefix split 'druid/segments/' → 40 children` → `Expanded 1 prefixes → 40 sub-prefixes` → `Crawl started … (40 prefixes, 0 existing)`.

| Metric | Value |
|---|---|
| Listing rate with 16 threads | ≈ 23–24k objects/s (8.23M at 345 s) vs ≈ 10.9k/s single-threaded — **2.2× at 40 ms latency**, now bounded by the single-process CPU ceiling rather than by latency |
| Progress in UI | live (`objects=980000` at 42 s, …) |
| RSS while listing | 264–341 MB |
| **Failure at 8.53M** | `Crawl error: database is locked` — 16 concurrent batch writers starved the main thread's progress `UPDATE` past the 30 s busy timeout; the crawl errored and the scheduler restarted it as an *incremental* crawl (the `crawl_gen` write-amplification path). **B.6 parallelism without serialized writes is unsafe at this scale.** |

Fix: per-bucket write lock around the SQLite write section (listing stays parallel) — commit on `core/phase-b-initial-split`, image `phase-b7`. Run 4 below.

## Run 4 — seg-main only, fresh volume, image `phase-b7` (B.6 + per-bucket write lock)

| Metric | Value |
|---|---|
| Threads used for listing | 16 (40 sub-prefixes) |
| `crawl_duration` | **1,066 s** at 40 ms/page — *slower* than the 908.6 s single-thread baseline |
| Lock errors / restarts during listing | 0 / 0 |
| Progress in UI | live throughout |
| RSS while listing | 250–430 MB; peak 574 MB by completion |
| Index size | 7.3 GB |

Interpretation: with all writes serialized behind one lock, listing threads wait for the writer instead of overlapping with it, so at **low latency the crawl is write-bound and parallel listing buys nothing**. B.6's win is at real provider latency (hundreds of ms per page), where waiting on the provider dominated the single-threaded crawl; the production run measures that case. The next lever for the write path is a writer thread with a bounded queue so listing and writing overlap (SAE-74), plus larger transactions and the zero-write reconcile (SAE-72). **Post-crawl phase: OOMKilled again** (restart count 1, reason OOMKilled) — the one-shot FTS `rebuild` over 9.9M keys exceeds the 1 GiB limit deterministically at this size, in both the single-threaded and the parallel build. Because the rebuild dies before committing, the FTS table stays empty and `_fts_should_rebuild` retries on the next crawl: a **crash loop** for any bucket this large at the chart default. Fix: chunked rebuild (250k rows per transaction, then `optimize`) on the Phase B branch; verified in Run 4b.

## Production read-only run (real StorageGRID endpoint, 22 buckets), Phase A + B.6 + write lock

First attempt (before the write lock) listed 3,801 prefixes in ≈ 60 s with no throttling or lock errors. At least one production bucket has a **wide-and-shallow** layout (thousands of ULID-named top-level prefixes holding 4–7 objects each) — the opposite of the Druid shape; the lab's layout matrix now includes it (`spec-layouts.json`: druid / wide / hive / flat).

Second attempt (with the write lock), first 9 minutes: 22 buckets, 17 complete, 2.43M objects indexed, largest bucket at 1.24M and crawling, no throttling and no lock errors, **process RSS 1,124 MB** on the workstation (no cgroup limit here; this would have been an OOM kill at the chart's 1 GiB default — SAE-85 confirmed at real scale). Splitter on real layouts: `1 → 953`, `2 → 45`, `1 → 3` sub-prefixes on three buckets; 6 buckets are flat (no prefixes). Result: pending.

## Run 5 — layout matrix (`spec-layouts.json`, ~200k objects each), image `phase-b7`

| Layout | Shape | Crawler decision | Initial crawl |
|---|---|---|---|
| druid (deep) | 1 top prefix → 4 datasources → 500 intervals → 100 partitions | split 1 → 4 sub-prefixes (B.6) | 200,000 in **20.6 s** |
| flat | 200,000 root-level keys, no delimiter | "Simple crawl" (0 prefixes), **single thread, unsplittable** | 200,000 in **28.0 s** |
| hive | 400 `dt=` prefixes × 24 `hour=` × 20 parts | 400 prefixes, 16 threads | 192,000 in **36.3 s** |
| wide | 20,000 ULID-like top-level prefixes × 10 objects | 20,000 prefixes, 16 threads | 200,000 in **351.7 s** (≈ 57 prefixes/s; 10–17× the other layouts) |

The wide penalty is per-prefix overhead, not listing: one list call, one progress write and, before commit c610c76, a **full `COUNT(*)`/`SUM(size)` scan of the objects table after every completed prefix** (20,000 full scans here; production has a bucket with 3,800+ such prefixes). Fixed on the Phase B branch (time-bounded recount, image `phase-b9`); Run 5b re-measures.

## Run 4b — seg-main only, fresh volume, image `phase-b9` (B.6 + write lock + chunked FTS rebuild + time-bounded recount)

| Metric | Value |
|---|---|
| `crawl_duration` (listing + writes) | **452.7 s** — 2.0× the 908.6 s single-thread baseline and 2.4× the lock-only Run 4 (1,066 s): the per-prefix recount was the hidden cost even with 40 prefixes |
| Lock errors / restarts during listing | 0 / 0 |
| RSS while listing | ≈ 250–260 MB |
| Post-crawl phase | **OOMKilled again** — last log line was `Crawl complete: 9,900,000 objects … in 452.7s`; no rebuild step ever logged. Index afterwards: `objects` 9,900,000, `folder_stats` **0**, `prefix_children` **0**, FTS empty → the kill is in the *first* rebuild step, before the FTS rebuild even starts |

Flat buckets remain single-threaded by nature (no delimiter to split on); their ceiling is provider RTT × pages, which only events/inventory can remove.

**Root cause of the post-crawl OOM (measured in the pod against the finished 9.9M index):** `_record_storage_snapshot`, `_rebuild_folder_stats` and `_rebuild_prefix_children` each run `… GROUP BY SUBSTR(key,1,INSTR(key,'/'))` over the whole `objects` table. SQLite cannot use an index for that expression, so it sorts all 9.9M rows into a temp b-tree — and every connection set `PRAGMA temp_store = MEMORY`, making that sort a full-table copy in RAM.

| `temp_store` | Same GROUP BY on the 9.9M index | Result |
|---|---|---|
| FILE (SQLite default) | 1 group in **30.1 s**, RSS 11 → 328 MB | fine |
| MEMORY (what the code set) | — | **process killed, exit 137** |

The chunked FTS rebuild (Run 4 diagnosis) was a real problem but not the first one in line; the FTS phase never ran here. Fix: drop the `temp_store = MEMORY` pragma on both connection paths (file-backed temp; the page cache and mmap settings are unchanged). Image `phase-b10`, Run 4c below.

### Production run — layout reality and the restart/resume test (21:38 IST)

* Completed-prefix counts per bucket after 110 min: **62,414 / 23,724 / 8,577 / 405 / 324 / 261 …**; median **2 objects per prefix**, largest single prefix 326,991 objects. The real estate is dominated by *wide* layouts, not the deep Druid shape of the sample bucket.
* Largest bucket crawled at ≈ **90 objects/s** on the pre-fix code: the per-prefix `COUNT(*)`/`SUM` recount over a 2.6M-row table dominated (tens of thousands of full scans).
* Restarted the backend on commit c610c76 (recount fix). **Phase A resume at real scale:** 8 buckets resumed; the widest logged `Resuming interrupted crawl (gen 1): 65,958 prefix(es) already complete` and skipped them all. Throughput after restart: ≈ **45 prefix completions/s** (1,874 in the first 42 s) versus ≈ 15/s before — now bounded by provider RTT ÷ 16 threads, as it should be.
* Process RSS on this workstation reached 3.4 GB at peak during the herd (22 buckets, 16-connection pool), 1.0 GB right after restart.

## Run 4c — seg-main only, fresh volume, image `phase-b10` (b9 + file-backed temp store)

First run at this size to finish the whole pipeline inside the 1 GiB chart default.

| Metric | Value |
|---|---|
| `crawl_duration` (listing + writes) | **473.0 s** (452.7 s in 4b — the temp-store change costs nothing on the listing side) |
| Restarts, whole run | **0** (every previous run at this size was OOMKilled after `Crawl complete`) |
| RSS while listing | ≈ 250 MB |
| Post-crawl metadata rebuilds | storage snapshot + folder_stats + prefix_children ≈ 25 s each (three full scans of 9.9M rows, one GROUP BY each); RSS 557–634 MB |
| Chunked FTS rebuild (40 × 250k rows) | **308.3 s**; process RSS 342 MB during, 280 MB after; cgroup at 1,023 MB of 1,024 MB — that is page cache from the 10 GB file, reclaimable, and the kernel did reclaim it instead of killing |
| Index after | `objects` 9,900,000 · `folder_stats` 1 · `prefix_children` 1 · `storage_history` 2 · FTS populated (678,784 shadow rows); trigram search `ds_007` → 247,500 hits in **0.18 s** |
| Index size | 7.3 GB after listing → **10.1 GB** with the trigram index (≈ 1.0 GB per 1M objects at this key length) |
| One warning | `Delta crawl error: database is locked` — the 120 s auto-recrawl fired a delta crawl while the FTS rebuild held the writer; non-fatal (next interval retries) but a real gap: the scheduler must skip delta crawls while a bucket is in the post-crawl rebuild |

End-to-end wall clock for 9.9M objects at 40 ms/page: **≈ 14 min** (7.9 min listing + 1.3 min metadata + 5.1 min FTS), all within 1 GiB.

## Run 5b — layout matrix (`spec-layouts.json`, ~200k objects each), image `phase-b11` (time-bounded recount, file-backed temp store, synchronous FTS rebuild)

| Layout | Run 5 (`phase-b7`) | Run 5b (`phase-b11`) | Change |
|---|---|---|---|
| druid (deep, split 1 → 4) | 20.6 s | **19.4 s** | — |
| flat (200k root keys, 1 thread) | 28.0 s | **27.9 s** | — |
| hive (400 prefixes) | 36.3 s | **30.4 s** | −16 % |
| wide (20,000 prefixes × 10 objects) | 351.7 s | **134.3 s** | **−62 % (2.6×)** |

Restarts 0, lock errors 0, FTS rebuilt after each bucket. The wide layout is still 4–7× the others: the remaining per-prefix cost is one list call (40 ms simulated, ≈ 50 s floor across 16 threads for 20k prefixes), one progress-table write and one INFO log line per prefix (20,000 lines here; a production bucket with 62k prefixes logs 62k lines per crawl). Batching the per-prefix progress write and demoting the per-prefix log line are the next levers for wide buckets; the true fix for wide layouts is not listing them prefix by prefix at all (events/inventory in Phase C).

## Run 6 — skew layout (`spec-skew.json`: 6 top-level prefixes, `big/` holds 200k of 210k objects), image `phase-b12`, `SUBPREFIX_SPLIT_MIN_OBJECTS=50000`

Motivated by production: the 4.45M-row bucket has 6 top-level prefixes with one holding ~90 %; the ≤3-prefix rule never fired, so that prefix was listed by one thread, was the resume unit (a restart re-listed all of it) and froze the UI counter for its whole duration (stuck at 3,895,445 while the table held 4,454,082).

| Crawl | Decision | Threads on `big/` | `crawl_duration` |
|---|---|---|---|
| 1st (empty index — nothing to count yet) | 6 prefixes, no split | 1 | 17.0 s |
| 2nd (120 s later, 210k rows indexed) | `Skew split: 1 heavy prefix(es) → 100 sub-prefixes` → 105 prefixes | 16 | **8.3 s** (and this one is the incremental/upsert path) |

The heavy-prefix signal is a primary-key range count per top-level prefix over the rows already indexed (≈ 1 s per million rows, only when the bucket exceeds the threshold and has ≤ 64 top-level prefixes), so it also works for a bucket whose first crawl never completed — exactly the production case. The 100 children are now the resume units and the progress counter refreshes on time from inside long prefixes.

## Run 7 — deep-then-wide layout (`spec-deepwide.json`: `druid/indexing-logs/` holds 200,000 one-object prefixes), fan-out cap

Motivated by production: the ≤3-prefix rule drilled `druid/` → `druid/indexing-logs/`, which has **629,643 children holding ~300k objects**, turning one 300-page listing into 630k list calls (hours at provider RTT; the read-only run showed ≈50 completions/s).

| Image | Decision | Initial crawl of 200k objects |
|---|---|---|
| `phase-b12` (no cap) | `Expanded 1 → 200,000 sub-prefixes` | **1,096.4 s** |
| `phase-b13` (`SUBPREFIX_SPLIT_MAX_CHILDREN=1000`) | `Sub-prefix split 'druid/indexing-logs/' skipped: more than 1000 children, listing it whole` | **17.3 s** (63×) |

Live confirmation on the production bucket: the same log line, then the prefix listed whole at ≈1,500 objects/s on one thread (3.7M → 7.3M rows in 40 min) instead of ≈50 prefixes/s.

### Production run — the "silent 12 minutes" on the skewed bucket, explained (23:40 IST)

A Python-level thread dump (temporary local patch, never committed — the faulthandler variant segfaulted the process and was reverted the same night) showed the truth: the skewed bucket's 46 sub-prefixes had all completed except **two**, each on one thread blocked in an SSL read of a list page, while the same process had **28 more delimiter listings in flight** from three delta crawls (`_discover_delta_targets`) plus the other buckets' prefix threads, all through one 16-connection pool (1,143 "connection pool is full" warnings). Not a hang: a throughput collapse at the provider under ~30 concurrent listings from one client — the case for a provider-wide list budget (SAE-75, B.4), now with evidence. Two smaller gaps fixed on the branch: the stall detector was blind during discovery (it now sees every listed page), and the heavy child (327k objects) stayed a single unit because the skew loop stopped at 16 children instead of re-checking which children are heavy.

## Run 8 — seg-main only (9.9M), fresh volume, image `phase-b14` = PR #31 head (cabda73): shadow-table FTS rebuild

| Metric | Value |
|---|---|
| `crawl_duration` | **454.8 s** (4b: 452.7 s, 4c: 473.0 s — unchanged) |
| Restarts, whole run | **0** |
| Post-crawl metadata rebuilds | prefix_children 32.7 s (file-backed sort, `SQLITE_TMPDIR` on the data volume) |
| FTS rebuild into `objects_fts_new` + atomic swap | **310.1 s** (4c: 308.3 s with the in-place chunked variant) — same cost, but the old index stays searchable throughout and a kill mid-build leaves it intact |
| Process RSS after | 345 MB |
| Index size | 10.06 GB (the shadow table needs the old index's space only until the swap) |


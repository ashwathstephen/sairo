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

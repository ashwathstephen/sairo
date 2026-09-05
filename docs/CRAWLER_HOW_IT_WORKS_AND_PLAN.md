# The Sairo Crawler: How It Works Today, Where It Falls Short, and How It Gets Better

**Audience:** anyone who needs to understand why data shows up (or doesn't) in Sairo, without reading `backend/main.py`.
**Evidence date:** 2026-09-05, revision `64bdb2f` (v3.6.0). Every number below was measured today; nothing is estimated unless marked.

---

## 1. What the crawler is for

Sairo never stores your objects. It keeps a **local catalogue** (one SQLite file per bucket on the `/data` volume) with one row per object: key, size, last-modified, ETag, and the folder it belongs to. Everything fast in the UI comes from that catalogue: folder listing, search, the storage dashboard, cost analytics. The crawler is the process that keeps the catalogue in sync with the bucket.

Two things follow:

- **Object count, not terabytes, is the scaling variable.** A 1 PB video archive with 2 million objects is easy. A 40 TB Parquet bucket with 10 million objects is the hard case. Your production instance holds 15.5 million objects across 19 buckets, and one bucket alone holds 9.9 million.
- **The catalogue is a copy.** It can be behind the bucket, and it can be wrong after a crash. How far behind, and how it recovers, is the whole subject of this document.

## 2. How a crawl works today, step by step

1. **Discover folders.** List the bucket root with a delimiter to get the top-level prefixes (`ds=01/`, `logs/`, …). Prefixes seen in earlier crawls are remembered so discovery is cheap on repeat runs.
2. **List every folder in parallel.** Up to **16 threads per bucket** each walk one prefix with `ListObjectsV2`, 1,000 keys per page. Up to **12 buckets** crawl at the same time. That is up to 192 simultaneous list calls against your provider.
3. **Write in batches.** Each thread buffers up to 10,000 rows, then writes them into the bucket's SQLite file. New or changed rows (size or ETag differs) are inserted or replaced. Unchanged rows have a "generation" counter bumped so the crawler knows they were seen.
4. **Prune what disappeared.** After all prefixes finish, rows whose generation counter was not bumped are deleted (they no longer exist in the bucket). This is skipped if any prefix failed, so a transient error never deletes real data.
5. **Rebuild the derived tables.** Folder statistics and folder-children tables (what makes folder navigation instant) are rebuilt from scratch. The full-text search index is rebuilt if any key was added or removed.
6. **Mark complete.** Status becomes `complete`, and the duration is recorded. That duration decides how the bucket is treated from then on.

The search index deserves one note: during a crawl, the triggers that keep it in sync are switched off for speed and switched back on at the end. That is why step 5 needs a rebuild.

## 3. Small buckets and large buckets are treated differently

A background scheduler wakes every **120 seconds** (`RECRAWL_INTERVAL`) and decides, per bucket:

| Bucket | Rule | What happens |
|---|---|---|
| Never crawled | no record | Full crawl now |
| **Small**: last full crawl took **under 60 s** | `LARGE_BUCKET_SECONDS` | Full re-crawl every 120 s. Cheap and always fresh. |
| **Large**: last full crawl took **over 60 s** | | **Delta crawl** every 120 s, plus a **full reconcile every hour** (`FULL_CRAWL_INTERVAL`) |

A **delta crawl** does not walk the whole bucket. It looks at the 3,000 most recently modified objects already in the catalogue to find the "hot" prefixes where new data lands (for example today's `hour=14/` partition), re-lists only those (up to 40 of them), and also walks the top of each dataset to catch a brand-new partition. New objects in hot prefixes appear within one delta. Deletions and changes in cold prefixes wait for the hourly reconcile.

**What "fresh" means today, in numbers:**

| Situation | Time until it shows in the UI |
|---|---|
| New object in an active prefix of a large bucket | one scheduler tick (up to 120 s) + delta time (seconds to a minute) + UI poll (up to 30 s). **Typically 2 to 3 minutes.** |
| New object anywhere in a small bucket | up to 120 s + UI poll. **Typically under 3 minutes.** |
| Deleted object, or a change in a cold prefix of a large bucket | next hourly reconcile. **Up to an hour**, plus the time the full crawl itself takes. |
| First crawl of a new 10M-object bucket | hours, bounded by provider list speed (round-trip latency dominates) |

The browser refreshes the visible folder every 30 s only if a cheap status check says the index changed. There is no push.

## 4. Where it falls short (measured)

### 4.1 A restart during a crawl leaves a partial index that claims to be complete

Every deploy restarts the single pod (the chart uses `Recreate`). If a crawl is running, the process dies within a second; the current batch is lost and nothing records that the crawl was cut off.

On the next boot, the startup code sees rows in the catalogue and assumes a full crawl finished. For large buckets it goes straight to delta-only mode for an hour. Deltas only look at hot prefixes, so the folders the crawl never reached stay empty. The first delta then sets the status to `complete`.

Reproduced today on a 120,000-object bucket: killed at 64,000 rows, restarted, and the catalogue stayed at **64,000 for the whole window**, reported **`complete`**, listed **0 files** in a folder that held 4,000, and every further restart inside the hour reset the clock. It healed only when left alone until the hourly reconcile.

This is exactly what happened on July 13: three deploys in 54 minutes on top of a PVC change, on code that was identical to 3.6.0.

### 4.2 A crawl over two hours spawns a second copy of itself

If a crawl runs longer than 2 hours, the scheduler assumes the thread died, releases the lock, and starts another crawl of the same bucket while the first is still running. Two crawls then fight over one SQLite file and double the provider load. Whether the 9.9M bucket crosses two hours in production is not yet measured.

### 4.3 Every full crawl rewrites the whole catalogue even when nothing changed

Step 3's "bump the generation counter" is an UPDATE on every unchanged row. Measured on a production-shaped 844k-row catalogue: a full crawl with **zero changes writes 541 MB** to disk. At 9.9M rows that is roughly **6.3 GB written every hour** for nothing, on a persistent volume. The alternative (delete-missing per prefix using a temporary table) wrote **0 MB**.

### 4.4 The search index is rebuilt far too often

Because the sync triggers are switched off during every crawl, any single added key on the hourly reconcile forces a full rebuild of the search index: **16.8 s at 835k rows, about 200 s at 9.9M**. With the triggers left on, keeping the index in sync costs **1.6 s per 10,000 changed rows**, proportional to what changed rather than to the table size.

### 4.5 Contention and herd effects

Sixteen writer threads per bucket share one SQLite file, so writes wait on a 30-second busy timeout and the code has a retry path for "database is locked". After a volume reset all 19 buckets start at once, hammering the provider with up to 192 concurrent list calls.

### 4.6 Operational hazards

The Helm chart's PVC has no keep policy (a `helm uninstall` deletes every index), defaults to 5 GiB (the production index is larger), and a full disk silently turns the crawler into fake staleness because failed writes are swallowed.

### 4.7 The UI cannot tell you how fresh anything is

There is one spinner state, "Indexing…", and one completed state. Nothing says "updated 2 minutes ago" or "this index was interrupted and is partial".

## 5. What we saw in production, explained

- **"Crawler not firing"**: it was not supposed to; startup deliberately skips the full crawl when rows exist and trusts deltas.
- **"Always showing indexing"**: interrupted crawls left `crawling` in the status table, and the hourly reconcile of a 9.9M bucket legitimately runs for hours.
- **"Initial load too slow / data not showing"**: the folder-statistics tables are only rebuilt after a *completed* crawl, so after a kill they were empty and listings fell to slower paths or showed nothing for unreached folders.
- **Recovery after the rollback**: not the version. The pod was left alone long enough for the hourly reconcile to run to completion.

## 6. The plan, and what freshness becomes

Three slices, no new infrastructure, community single-container mode unchanged. Full specifications: Confluence, "Sairo Core Program", pages 02 to 04. Jira epics SAE-63, SAE-64, SAE-65.

### Slice A — make it correct (this week)

- Record each prefix as it completes; on restart, **resume** the same crawl and skip finished prefixes instead of pretending it was complete.
- Boot marks a cut-off crawl **`interrupted`**; the UI says so; deltas can no longer flip it to `complete`.
- A crawl past its time limit is **asked to stop** and resumed, never duplicated.
- On SIGTERM the crawl flushes its batch and records progress; the pod gets a 30 s grace period.
- The PVC is kept on uninstall, defaults to 20 GiB, and a crawl refuses to start under 10 % free disk.
- The restart reproduction becomes a nightly test.

**Result:** after any deploy, the catalogue converges to complete on its own and never lies about it.

### Slice B — remove the measured waste (next 1 to 2 weeks)

- Stop rewriting unchanged rows: **0 MB instead of 541 MB per unchanged crawl** (measured).
- Keep the search-index triggers on for incremental crawls; rebuild only on a brand-new index.
- One writer thread per bucket and one provider-wide list budget, each kept only if the counters show they help.
- A production-shaped benchmark with before/after numbers checked in.

**Result:** hourly reconciles of the 9.9M bucket stop costing gigabytes of writes and minutes of search rebuild; "database is locked" disappears.

### Slice C — near real time (following 2 weeks)

- Providers that emit **S3 event notifications** (MinIO, Ceph, StorageGRID via its SNS-compatible platform service, Backblaze, AWS via SQS) post each PUT/DELETE to Sairo. Measured today: **PUT to catalogue row in 2 ms median, 13 ms worst** over 240 events.
- The browser refreshes the visible folder within seconds using a revision counter (fast polling while the tab is visible; server push later only if measurement demands it).
- Every bucket shows its mode: **Live** (events, seconds), **Adaptive** (deltas, minutes), **Full scan**, **Interrupted**, or **Events degraded**.
- The hourly reconcile stays as the correctness guarantee; events only make it fast.

**Result on event-capable providers:** a new hour partition is visible in the browser within seconds of upload, not minutes.

| | Today | After Slice C (event-capable provider) |
|---|---|---|
| New object in an active prefix | 2 to 3 min | seconds (target p95 under 10 s) |
| Delete or cold-prefix change | up to 1 h | seconds; reconcile still verifies hourly |
| Index after a deploy mid-crawl | partial, labelled complete, for up to 1 h | resumed automatically, labelled honestly |
| Disk written per unchanged hourly reconcile (9.9M bucket) | ≈ 6.3 GB | ≈ 0 |

## 7. What changes for operators and other features

- **No re-install.** Every schema change is additive. Existing indexes upgrade in place; buckets left in `crawling` from an old kill will be marked `interrupted` and resumed once after the upgrade.
- **Helm upgrade** keeps the PVC and adds the grace period. To wipe an index you now delete the PVC deliberately.
- **MCP and CLI** read the same status values; the MCP status labels gain `interrupted`. Nothing else switches on those strings.
- **Rate limiting**: the event webhook must be exempted from the global per-IP limit (a provider sends one POST per event) and excluded from the telemetry request count.
- **Search during a crawl**: once triggers stay on for incremental crawls, objects arriving by event during a reconcile are searchable immediately; only the very first crawl of an empty index still rebuilds at the end.
- **Providers without events** (Wasabi, Hetzner, Scaleway, OVH, iDrive, Storj, R2 today) stay on Adaptive mode and the UI says so.

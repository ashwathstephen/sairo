# Sairo Core Program — Reliability, Scale and Near-Real-Time

**Status:** Approved 2026-09-05; Phase A in progress
**Baseline:** `64bdb2f` (v3.6.0)
**Jira:** epics SAE-63 (A) → SAE-64 (B) → SAE-65 (C); stories SAE-66…SAE-82, label `core-reliability`
**Confluence:** SED → "Sairo Core Program — Reliability, Scale and Near-Real-Time"
**Sequencing:** runs before the Enterprise Migration V1 POCs (SAE-65 blocks SAE-34).

## Decision

Sairo is a control plane over existing buckets. Its growth variable is object cardinality and churn, not stored bytes. Before any new infrastructure the current tool must (1) work as intended, (2) scale to more objects on the same architecture, (3) update close to real time. No PostgreSQL, ClickHouse, Kafka, Redis, WebSockets or roles split; each has a named gate (one bucket past ~100M rows; a second pod required). Community single-container mode is unchanged.

## POC results (2026-09-05)

### Spike A — restart mid-crawl
4-phase harness: 120,000 objects / 30 prefixes on MinIO behind a 1s latency proxy; kill after `total_objects ≥ 56,000`; restart; observe 70s; kill again; heal with `FULL_CRAWL_INTERVAL=30`.

- Killed at 64,000/120,000: `status='crawling'`, `folder_stats=0`, `prefix_children=0`, `crawl_duration=0`.
- Restart: `Seeded schedule for default:rca from existing index (64,000 objects)`; only delta crawls queued (full=0, delta=3 in 70s); count frozen at 64,000; status flipped to `complete`.
- API while "complete": folder `ds=29/` listed 0 files, S3 held 4,000.
- Third rollout re-seeded (clock reset). Control run healed to 120,000 in 61s once left alone.
- Buckets ≤ 50k objects self-heal after one interval (different scheduler branch).
- Process exits within 1s of SIGTERM; in-flight crawl dies without recording anything.

### Spike B — write amplification and FTS (843,749 rows, identical schema/pragmas)
| Path | Time | WAL written |
|---|---|---|
| Current: `crawl_gen` sweep, 0 changes | 15.8s | **541 MB** (≈6.3 GB at 9.9M rows) |
| Proposed: per-prefix anti-join, 0 stale | 8.1s | **0.0 MB** |
| Proposed: anti-join, 1% stale (8,437 deletes) | 12.9s | 15 MB |
| FTS full trigram rebuild (runs after every crawl with ≥1 key change; triggers dropped at L1415) | 16.8s (≈200s at 9.9M) | — |
| 10,000 upserts with FTS triggers on | 1.60s | — |

### Spike C — S3 event webhook → SQLite (MinIO `notify_webhook`)
- 240 mutations: PUT/DELETE → row visible **p50 2 ms, p95 2 ms, p99 5 ms, max 13 ms**.
- Event shape (eventVersion 2.0): `eventName`, `eventTime`, `s3.bucket.name`, `s3.object.{key,size,eTag,sequencer}`.
- Keys are URL-encoded (`ds=03/…` → `ds%3D03%2F…`): `unquote_plus` required.
- `sequencer` present; per-key monotonic compare is the duplicate/out-of-order guard.
- MinIO redelivers undelivered events (at-least-once confirmed in practice).
- Binding via standard `PutBucketNotificationConfiguration` with `QueueArn arn:minio:sqs::PRIMARY:webhook`.

## Phases

**A — Crawler correctness (SAE-63, ~1 week):** A.1 resume via `crawl_progress` table (SAE-66); A.2 `interrupted` status, never `complete` on partial (SAE-67); A.3 force-release cancels instead of forking (SAE-68); A.4 graceful SIGTERM + `terminationGracePeriodSeconds` (SAE-69); A.5 PVC keep policy, 20Gi, disk guard (SAE-70); A.6 restart harness checked in as nightly test (SAE-71).
Exit: harness passes — 120,000 within one interval after a mid-crawl kill; never `complete` while partial; zero duplicate crawls; `helm uninstall` keeps the PVC.

**B — SQLite scale headroom (SAE-64, ~1–2 weeks):** B.1 anti-join stale detection (SAE-72); B.2 incremental FTS, rebuild only on empty index (SAE-73); B.3 single writer per bucket (SAE-74); B.4 global S3 list budget (SAE-75); B.5 prod-shaped benchmark + before/after record (SAE-76).
Exit: 0-change crawl < 1 MB WAL; no rebuild on incremental; `database is locked` = 0; then the same on the real 9.9M bucket.

**C — Near-real-time (SAE-65, ~2 weeks):** C.1 webhook endpoint with token, URL-decoding, sequencer guard (SAE-77); C.2 optional SQS poller (SAE-78); C.3 event-aware scheduler, reconcile stays (SAE-79); C.4 SSE push + EventSource with polling fallback (SAE-80); C.5 freshness disclosure (SAE-81); C.6 provider guides (SAE-82).
Exit: PUT → visible in browser p95 < 5 s without reload; reconnect refetches; reconcile finds 0 mismatches after 1,000 random mutations.

## Ceilings marked in code
- `# ponytail: one writer per bucket; shard the bucket DB when one bucket passes ~100M rows`
- `# ponytail: in-process pub/sub is single-pod; PostgreSQL LISTEN/NOTIFY when a second pod exists`
- `# ponytail: providers without a sequencer get last-write-wins; the hourly reconcile repairs drift`

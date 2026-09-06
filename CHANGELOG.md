# Changelog

All notable changes to Sairo are documented here. This project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

**Upgrade note (Helm):** the default `persistence.size` moved from 5Gi to 20Gi. An existing release's PVC is patched in place, which only works when the StorageClass allows volume expansion; otherwise pass your current size (`--set persistence.size=5Gi`). The PVC now carries `helm.sh/resource-policy: keep`, so `helm uninstall` no longer deletes the index.

Crawler correctness (Core program, Phase A — SAE-63).

### Fixed

- **Re-applied from the reverted 3.6.1–3.6.3 patch releases** (rolled back in June because three rollouts in 54 minutes exposed the partial-index restart bug that Phase A has since fixed; the fixes themselves were never at fault): activation metric `first_search_at` recorded on the search request rather than a served response (fresh installs whose early searches 503'd during the first crawl under-counted to a false 0 %); complete white-label branding — tab title, `og:title`, description, `theme-color` and the PWA manifest take `APP_NAME` / `PRIMARY_COLOR` server-side so a white-label deployment is right on first paint; the SPA shell and manifest are served `Cache-Control: no-cache, must-revalidate` so a deploy is never masked by a stale browser cache (hashed assets stay long-cacheable).
- **A restart during a crawl no longer leaves a partial index that reports itself complete.** Completed prefixes are recorded per crawl generation (`crawl_progress`); on boot an unfinished crawl is marked `interrupted` and **resumed**, skipping finished prefixes, instead of being seeded as "fully crawled" and left delta-only for an hour. Delta crawls can no longer flip an interrupted index to `complete`. `crawl-status` gains `full_crawl_complete` and `crawl_elapsed`; the UI shows "Finishing an interrupted index".
- **A crawl running past the 2-hour limit is asked to stop and resumed, never duplicated.** The old force-release started a second concurrent crawl of the same bucket.
- **Graceful SIGTERM.** Running crawls stop at the next page, flush their batch and record progress; the chart sets `terminationGracePeriodSeconds: 30`.
- **Version-purge lock is never timed out** (it stored a boolean the timeout arithmetic misread as epoch 1).
- **A full disk no longer turns every indexed bucket into "Index error".** When the free-disk guard declines to start a crawl it now records the skipped attempt (`last_attempt_at`, `last_error`) and leaves the served index under its own state, instead of overwriting `status` with the error text on every bucket in one scheduler pass.

### Changed

- **Reconciles write only what changed (zero-write reconcile, SAE-72).** A full recrawl used to rewrite every unchanged row's generation stamp (one write per object per reconcile, ~6 GB of WAL per unchanged 9.9M-object pass). Listings arrive in key order, so each prefix now settles its index range batch by batch: rows whose size, ETag or LastModified changed are updated in place, rows the listing no longer returns are deleted as the listing passes them, and unchanged rows are not written at all. There is no listed-key buffer, so memory does not grow with prefix size. When a reconcile wrote and pruned nothing, the folder-stats and prefix-children re-aggregation (a full sort of every row, on every pass) is skipped as well, and the storage-history snapshot now reads the finalised counters and folder stats instead of re-grouping the table. Delta crawls likewise write only new or changed rows instead of replacing every listed one. A failed prefix prunes nothing; a resumed crawl prunes only the prefixes it listed. An unchanged bucket writes no object rows and skips the search-index rebuild.
- Helm: the data PVC carries `helm.sh/resource-policy: keep` (`persistence.keep: true`) so `helm uninstall`/reinstall keeps the index; default `persistence.size` is now **20Gi** (≈1 GB per 1M objects). Existing PVCs keep their size.
- Crawls refuse to start when the index volume has under `MIN_FREE_DISK_PCT` (10) percent free; `/healthz` reports `disk_low`.
- MCP `get_crawl_status` / bucket discovery label the new `interrupted` state.
- Helm: new `telemetry.enabled` value (default `true`) sets the `TELEMETRY` env var; the chart previously offered no way to opt out of the anonymous heartbeat.

### Upgrade note

Buckets left in `crawling` by an earlier restart are marked `interrupted` on first boot and re-crawled once (resuming where progress exists). Expect one full-ish crawl per such bucket after upgrading.

## [3.6.0] - 2026-06-29

Generic OpenID Connect SSO + a real per-bucket access UI (issue #9).

### Added

- **OpenID Connect (OIDC) SSO** — a standards-compliant client for any provider (Keycloak, Authentik, Okta, Auth0, Entra ID, Google, Dex, …). Endpoints are auto-discovered from `<issuer>/.well-known/openid-configuration`; every ID token is fully validated (JWKS signature, `iss`/`aud`/`exp`/`azp`, nonce) and the flow is protected with **state + nonce + PKCE (S256)**. Per issue #9, **only the username is synced** — new users are viewers with no access until an admin grants it. Configured entirely via `OIDC_*` env vars; works with a confidential **or** public (secretless) client. Tested end-to-end against Keycloak, Authentik, and Dex. See [docs/SSO.md](docs/SSO.md).
- **Optional OIDC group→role mapping** (`OIDC_ADMIN_GROUP` / `OIDC_GROUPS_CLAIM`) — off by default; when enabled, admin-group membership maps to the admin role and re-syncs each login. Matching is exact / path-segment / DN — never a loose substring.
- **Optional hardening toggles** — `OIDC_REQUIRE_VERIFIED_EMAIL`, `OIDC_ALLOWED_DOMAINS`, and RP-initiated single logout (`OIDC_RP_LOGOUT`).
- **Per-bucket access management UI** — Users now shows an auth-source badge (Local / SSO / LDAP / OAuth) and a bucket-grant count; "Manage access" opens a Read / Write / No-Access editor with search and bulk grant/revoke. (The backend permission API already existed; this makes it usable.)
- Friendly login-page error messages for SSO failures.

### Changed

- **`auth_source` is now tracked per user** (with a backfill migration). All federated logins — OIDC, OAuth, and LDAP — go through one hardened sync path.

### Security

- Closed an **account-takeover** vector: a federated (OIDC/OAuth/LDAP) login can no longer sign in as a username already owned by a different auth source — notably the local admin.

## [3.5.0] - 2026-06-27

Anonymous telemetry schema v2 — richer, still privacy-first.

### Added

- **Telemetry v2** — the anonymous heartbeat now also reports instance **health** (trailing-24h request + error counts, restart count, **crash count** — unclean exits like OOM/SIGKILL, counted separately from orchestrated restarts — index-disk used/total, a derived `ok`/`degraded`/`error` status), **activation milestones** (first bucket / object / API token / MCP connection timestamps), **engagement** (active-buckets-in-24h, last-write time, enabled feature slugs, update-available), and **instance validity** (`boot_count` + `id_persistence` so a durable install is distinguishable from throwaway-storage instances that mint a new id each restart). All additive; still aggregate **counts and timestamps only** — never bucket names, keys, paths, IPs, or any user content. Values are clamped before sending and the payload stays well under 4 KB. New `schema_version: "2"`. Documented at [Telemetry](https://sairo.dev/security/telemetry).
- A single process is elected (file lock) to emit the heartbeat, so a multi-worker deployment can't send duplicate pings; the Helm chart sets `SAIRO_STORAGE_EPHEMERAL` from `persistence.enabled` so `id_persistence` is accurate on Kubernetes.

### Changed

- **Heartbeat cadence is now hourly** (`TELEMETRY_INTERVAL`, default `3600`s) instead of daily, so the trailing-24h health metrics stay current. Telemetry remains fully opt-out via `TELEMETRY=false` (no ping is sent when disabled).

## [3.4.1] - 2026-06-27

Security/correctness fix for S3-key authentication with multiple endpoints (issue #8). Previously a user who logged in with S3 access keys could see and manage **every** bucket on **every** configured endpoint, because the server used its own stored endpoint credentials (and an unconditional admin role) for all operations and discarded the user's keys.

### Fixed

- **S3-key sessions now act with the user's own keys** — in `AUTH_MODE=s3`, every S3 call (bucket listing, object listing/preview/download, uploads, presigned URLs) is made with the **logged-in user's** access key, so the provider's IAM scopes exactly what they can see and do. Logging in with one account's keys shows only that account's buckets, not other endpoints'. The keys are kept encrypted in the session token (Fernet, same key that protects stored endpoint credentials) so this works statelessly across replicas.
- **The local index is no longer an access bypass** — object listings are served from Sairo's per-bucket index (built with server credentials), so S3-key requests are now independently gated by a `head_bucket` check with the user's keys (cached briefly) before any indexed data is returned.
- **Per-bucket permission checks could be bypassed via the multi-endpoint path** — `bucket_permission_middleware` ran *before* the `/api/e/{endpoint}/...` path rewrite, so prefixing a bucket URL with `/api/e/<id>/` skipped the permission check entirely (an IDOR on read routes). The middleware now runs after the rewrite, and S3-key sessions are bound to the endpoint they authenticated against.
- **Bucket-list cache no longer shared across users** — the 30s `/api/buckets` cache (keyed by nothing) is bypassed for S3-key sessions so one user's bucket list can't be served to another.

## [3.4.0] - 2026-06-15

Direct browser→S3 uploads for files of any size — closes the proxy-upload OOM / size-ceiling problem (issue #6). Validated end-to-end against MinIO (all paths, md5-verified) and in a real browser, with measured memory bounds.

### Added

- **Multipart direct upload** — files larger than 100 MB are split into parts that the browser PUTs **directly to S3** in parallel (no bytes through the server), with no single-PUT 5 GB ceiling (up to S3's 5 TB object limit). New endpoints: `multipart/initiate`, `multipart/sign`, `multipart/complete`, `multipart/abort`. Smaller files continue to use a single presigned PUT.
- **Just-in-time part signing** — each part's presigned URL is signed immediately before it is uploaded (and re-signed on retry), so a long-running multi-GB upload can never fail partway through from an expired URL. Per-part retries with backoff; the in-progress multipart upload is aborted on cancel/failure so no orphaned parts are left on S3.
- **Stop button** — an in-progress upload can be stopped from the modal, and navigating away mid-upload aborts the transfer (and its S3 multipart upload) instead of leaving it dangling.

### Changed

- **Proxy upload is now memory-bounded** — the fallback path (files routed through the server) streams each file straight to S3 instead of buffering it in memory. Peak RAM is independent of file size (measured ≈100 MB for a single in-flight file whether it is 500 MB or 50 GB, vs the old path that scaled 1:1 and OOM-restarted the pod). Total proxy memory is bounded by `UPLOAD_PROXY_CONCURRENCY` (default 3) × ≈100 MB.
- **CORS for direct upload now guarantees ETag exposure** — a bucket whose existing PUT CORS rule omits `ExposeHeaders: ETag` is upgraded in place, since the browser must read each part's ETag to complete a multipart upload.
- **Direct uploads now work under the Content-Security-Policy** — `connect-src` includes the configured S3 endpoint origin(s) so the browser can PUT directly to the (cross-origin) S3 endpoint. Previously `connect-src 'self'` silently blocked every direct upload (with no proxy fallback, since the same-origin signing request still succeeded).

### Fixed

- Multipart endpoints validate their inputs (part numbers 1–10000, well-formed parts) and return `400` instead of `500` on malformed requests; they are rate-limited and audit-logged. New tunables: `UPLOAD_PROXY_CONCURRENCY`, `MULTIPART_URL_EXPIRY`.

## [3.3.1] - 2026-06-14

Freshness-reliability patch for the adaptive delta crawler on large, actively-written buckets. Validated end-to-end against live production object storage (per-partition index counts compared to S3 ground truth).

### Fixed

- **New partitions are no longer missed** — delta discovery now fully paginates each level's child folders, so a freshly-created partition (e.g. a new hourly folder) is picked up even when it sorts beyond the first 1000 siblings. Previously a brand-new hour could stay invisible until the next full reconcile.
- **Discovery spans every dataset** — the delta crawler walks all top-level datasets breadth-first (listing each level in parallel) instead of only the parents of the most-recently-modified objects, so new data in a dataset that isn't the globally newest is still indexed promptly.
- **Correct newest-partition selection** — partition levels are ordered with a natural-sort key, so non-zero-padded names (`day=2` … `day=10`, `hour=9` … `hour=10`) select the true newest partition rather than the lexicographically-largest one.
- **Partition-vs-branch classification by fan-out** — levels with many children are treated as time partitions (follow the newest few); levels with few children are descriptive branches (follow all), so numerically-named branches are no longer mistaken for partitions and skipped.
- **No perpetual "indexing" state** — a cooldown is enforced after each delta crawl completes, so deltas on high-latency storage no longer queue back-to-back and a bucket settles to "complete" between cycles.
- **Restart no longer triggers redundant full re-crawls** — startup seeds the scheduler from the objects table rather than requiring `status = complete`, so a restart during a crawl resumes via fast deltas instead of re-listing the whole bucket.
- **Index stays "ready" during crawl transitions** — readiness falls back to the presence of indexed objects when crawl-status counters are transiently zero, preventing queries from falling back to slow live S3 listing mid-crawl.

### Changed

- **S3 connection pool sized for parallel listing** — the client pool defaults to 32 connections (env `S3_MAX_POOL_CONNECTIONS`) so parallel delta/crawl list calls don't serialize on a 10-connection pool. New delta tunables: `DELTA_BRANCH_FANOUT`, `DELTA_NEWEST_K`, `DELTA_MAX_DEPTH`, `DELTA_LIST_CONCURRENCY`, `DELTA_MAX_NODES`.

## [3.3.0] - 2026-06-13

Performance & freshness release: validated end-to-end against a 1M-object / 241 TB production bucket.

### Added

- **Keyset pagination for listings** — `/api/buckets/{bucket}/list` accepts `cursor` + `limit` and returns `next_cursor`. The UI fetches folders in pages and paints the first page immediately, so million-object folders open instantly instead of streaming the whole listing. Listing a 1M-file folder's first page dropped from ~1.4 s / 122 MB to ~73 ms / 0.12 MB. (Omitting `limit` keeps the legacy whole-folder response, so existing API/CLI clients are unaffected.)
- **Adaptive crawl scheduler** — large buckets are kept fresh with fast incremental **delta crawls** (re-listing only the prefixes where new data lands, in parallel) every interval, plus a periodic full reconcile; small buckets keep doing cheap full recrawls. A 1M-object bucket now picks up new objects in ~30 s instead of a ~7-minute full re-list. New tunables: `FULL_CRAWL_INTERVAL`, `LARGE_BUCKET_SECONDS`, `DELTA_SAMPLE`, `DELTA_MAX_TARGETS`.
- **Direct (presigned-PUT) uploads** — files upload straight to object storage via presigned URLs by default, with proxy upload as fallback. Removes the in-memory buffering path for large files.
- **Restart resumes from the existing index** — on startup, already-indexed buckets seed the scheduler from their crawl state instead of triggering a full re-crawl of every bucket.

### Changed

- **Covering index** — `objects` now uses a covering index `(prefix, key, size, last_modified)`, so folder listings and breakdowns are index-only (no temp B-tree sort, no row lookups). Replaces the prior `prefix`-only index; migrated automatically in place on first start (existing index reused, no re-crawl).
- **Storage breakdown & folder-size** use covered prefix-range scans instead of `LIKE` full-table scans — 1.5–2.2x faster on large subtrees; top-level breakdown ~2.5 ms.
- **FTS index rebuild** runs only when object keys were added or removed, skipping the expensive trigram rebuild on no-change recrawls.
- **SQLite tuning** — larger page size for new databases, memory-mapped I/O on all connections, `synchronous=NORMAL` on the write path, WAL autocheckpoint.

### Fixed

- **Empty folder stats after large-bucket crawls** — the post-crawl FTS rebuild could hold the SQLite writer long enough to starve the folder-stats/prefix-children rebuild, leaving top-level breakdown falling back to a ~1 s full scan. Rebuilds are now ordered and serialized so this can't happen (top-level breakdown returns in ~2.5 ms).
- **Stale folder shown after navigation** — a background refresh could overwrite the current folder's contents with a previous folder's data; refreshes are now aborted on navigation and guarded against the current view.
- Background refresh now updates crawl status and the last-crawl timestamp, so the UI reflects when the index was last refreshed.

## [3.2.0] - 2026-04-11

### Added

- **Cost Heatmaps** — Per-folder cost breakdown with 13 S3 provider pricing (AWS, R2, B2, Wasabi, Leaseweb, DigitalOcean, Hetzner, Scaleway, OVH, iDrive e2, Storj, MinIO, Ceph). Live AWS pricing via Bulk Pricing API with 24h cache. Provider auto-detection from endpoint URL.
- **Optimization Recommendations** — Lifecycle gap analysis with severity-based recommendations (no expiration, no abort rule, versioning without cleanup). Cold data detection by folder with age distribution. Duplicate file detection via filename + size matching. Tiering savings calculator for multi-class providers.
- **Multipart Cleanup** — Paginated listing with part sizes, stale/active classification (>24h threshold), bulk abort with safety guards. Active uploads (< 24h) protected from accidental deletion.
- **Insights Panel** — Consolidated Storage + Optimize tabs in a single modal. Lazy-loaded optimization data. Replaces the old "Dashboard" button.
- **Storage Class Transitions** — Lifecycle rules now support `transition_days` and `transition_storage_class` for moving data to cheaper storage tiers.
- **Pricing Module** — Shared pricing engine with region multipliers, minimum storage durations, and provider metadata. Used by both backend and MCP server.

### Changed

- **Settings Page: 24s → 0.3s** — Multipart uploads no longer block initial page load. Part sizes fetched lazily when the Multipart tab is clicked.
- **Cold Data Queries** — Added `last_modified` index on objects table for indexed cold data scans instead of full table scans.
- **Storage Dashboard** — Summary cards now show estimated monthly and annual costs. Per-folder cost overlay on bar chart and detail table.

## [3.1.0] - 2026-04-11

### Added

- **S3 Access Key Authentication** — New `AUTH_MODE=s3` option lets users log in with their S3 access key and secret key directly. Validates credentials via `list_buckets()`.
- **Login Toggle** — Sliding pill toggle on the login page to switch between Password and S3 Keys authentication.

## [3.0.0] - 2026-04-11

### Added

- **MCP Server (AI Storage Intelligence)** — Optional sidecar container exposing 26 tools, 4 prompts, and 2 resources via Model Context Protocol. Connect Claude Desktop, Cursor, or any MCP client to analyze storage with natural language. Includes cost estimation, duplicate detection, data freshness monitoring, pipeline health checks, and guided audit workflows.
- **Sub-Prefix Splitting** — Crawler automatically discovers and parallelizes sub-prefixes for buckets with few top-level folders but many objects (e.g., `druid/` with 9.5M objects splits into `druid/segments/`, `druid/indexing-logs/`, etc.)
- **Async FTS Rebuild** — Full-text search index rebuilds in a background thread after crawl completion. Search remains available during rebuild (WAL mode guarantees consistent reads).
- **MCP Security Layer** — Input validation against SQL injection, path traversal, prompt injection, and null bytes. 75 security-specific tests. Output sanitization strips control characters and prompt injection patterns.
- **MCP Observability** — Structured JSON logging, per-tool latency tracking, Prometheus-compatible metrics endpoint.
- **Scaling Test Suite** — 29 new tests verifying PRAGMA tuning, batch sizes, worker counts, prefix_children rebuild, async FTS, and sub-prefix splitting against real production data patterns.

### Changed

- **Folder Listing: 191,231x Faster** — `prefix_children` table now rebuilt via SQL-only aggregation instead of in-memory Python dicts. Removed the 1M-object skip threshold. Folder listing on 2M objects: 311ms → 0.002ms.
- **SQLite PRAGMA Tuning** — Added `cache_size=-64000` (64MB), `mmap_size=268435456` (256MB), `temp_store=MEMORY`. COUNT(*) on 557K objects: 2ms → 1.5ms.
- **Crawl Workers: 6 → 12** — Doubled concurrent bucket crawl capacity.
- **Prefix Workers: 4 → 16** — 4x more parallel prefix crawlers per bucket.
- **Batch Size: 2,000 → 10,000** — 5x fewer SQLite commits during crawl. Update chunk size 500 → 2,000.
- **Crawl Timeout Formula** — Updated from `600 + count/2000` to `900 + count/5000` for better scaling with larger prefix worker pools.

## [2.0.0] - 2026-02-26

### Added
- **Version Management** — Browse, restore, delete, and purge individual object versions
- **Version Scanner** — Background scan discovers hidden delete markers and ghost objects across all prefixes
- **Purge Versions** — Permanently destroy all versions and delete markers (admin only)
- **Storage Growth Trends** — Interactive SVG charts showing storage size over time with hover tooltips
- **Per-Folder Trends** — Drill down into storage growth for individual folders
- **File Metadata Preview** — View Parquet, ORC, and Avro schemas without downloading the file
- **Log Tail Preview** — Head/tail toggle for `.log`, `.out`, `.err` files
- **Session Management** — Expiry warnings, "Extend Session" action in toast notifications
- **Custom Dialogs** — Replaced all browser `alert()`/`confirm()`/`prompt()` with accessible custom components
- **Welcome Onboarding** — First-time tips overlay for new users
- **Delete Dialog Enhancements** — Shows file/folder list, "Purge All Versions" checkbox for admins
- **Show Deleted Toggle** — Reveals hidden versioned objects in the browser
- **Login Redesign** — Feature showcase sidebar on the login page

### Changed
- Password minimum length increased from 4 to 8 characters
- Storage dashboard Y-axis labels use adaptive precision to avoid duplicate labels
- Storage dashboard X-axis shows times (HH:MM) for same-day data, dates for multi-day
- Improved accessibility with ARIA attributes on all dialogs and interactive elements

### Fixed
- Tooltip contrast in light mode (hardcoded dark background with light text)
- Flat data chart rendering (synthetic range for identical values)
- Purge cleanup now removes stale entries from all index tables

## [1.0.0] - 2026-02-25

### Added
- **Object Browser** — Navigate buckets and prefixes with virtual scrolling (100K+ objects)
- **Full-Text Search** — SQLite-indexed search across all object keys
- **File Preview** — Images, text, CSV, JSON, PDF, and binary hex preview
- **Upload & Download** — Multipart upload with progress, drag-and-drop support
- **Storage Dashboard** — Visual breakdown by prefix with bar charts
- **Bucket Management** — Versioning, lifecycle rules, CORS, ACLs, policies, tagging
- **Object Operations** — Copy, move, rename, delete (files and folders)
- **Presigned URLs** — Time-limited shareable download links
- **Audit Log** — Full activity trail with filtering by action, user, and bucket
- **User Management** — RBAC with admin/viewer roles, bcrypt passwords
- **Dark Mode** — Full dark/light theme with system preference detection
- **Responsive Design** — Desktop, tablet, and phone layouts
- **Keyboard Shortcuts** — `/` search, `Backspace` navigate, `?` help
- **Favorites** — Bookmark paths for quick navigation
- **Background Crawler** — Prefix-parallel indexing with 6 concurrent buckets
- **Streaming Responses** — NDJSON for progressive UI rendering
- **Health Check** — `/healthz` endpoint for Kubernetes probes
- **Helm Chart** — Kubernetes deployment with comprehensive values
- **Docker** — Multi-stage build, non-root container (UID 1000)

# Changelog

All notable changes to Sairo are documented here. This project uses [Semantic Versioning](https://semver.org/).

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

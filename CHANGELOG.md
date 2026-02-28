# Changelog

All notable changes to Sairo are documented here. This project uses [Semantic Versioning](https://semver.org/).

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

# Restart harness (SAE-71)

Reproduces the "restart mid-crawl leaves a partial index that reports `complete`" failure and asserts the Phase A exit gates.

    docker compose up -d                      # MinIO :9000, toxiproxy :8474 / :19000
    ./driver.sh                               # ~5 min; writes out/rca/REPORT.txt; exit 0 = all gates pass

What it does: populates 30 prefixes × 4,000 objects, puts MinIO behind a 1 s latency proxy, starts the backend
(`TELEMETRY=false`, pinned `JWT_SECRET`), kills it (SIGTERM → 30 s → SIGKILL, like a `Recreate` rollout) once
`crawl_status.total_objects ≥ 56,000`, restarts it and watches the scheduler for 70 s, kills/restarts again, then
lets it heal with `FULL_CRAWL_INTERVAL=30`.

Gates: (1) objects reach 120,000 after restart; (2) `crawl-status` never `complete` while partial;
(3) zero "Force-releasing" duplicate crawls; (4) no tracebacks. On `main` @ 64bdb2f gates 1–2 fail; on Phase A they pass.

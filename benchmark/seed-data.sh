#!/bin/bash
# Sairo Benchmark — Data Seeder
# Creates realistic test data at various scales for performance benchmarking
#
# Buckets:
#   bench-small   — 1,000 objects (quick tests)
#   bench-medium  — 10,000 objects (standard tests)
#   bench-large   — 50,000 objects (stress tests)
#   bench-mixed   — 5,000 objects with realistic file types and deep nesting

set -euo pipefail

MC_ALIAS="${MC_ALIAS:-local}"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "=== Sairo Benchmark Data Seeder ==="
echo "Using MinIO alias: $MC_ALIAS"
echo "Temp dir: $TMPDIR"
echo ""

# Create sample files of various sizes
echo "Creating sample files..."
dd if=/dev/urandom of="$TMPDIR/1kb.bin" bs=1024 count=1 2>/dev/null
dd if=/dev/urandom of="$TMPDIR/10kb.bin" bs=1024 count=10 2>/dev/null
dd if=/dev/urandom of="$TMPDIR/100kb.bin" bs=1024 count=100 2>/dev/null
dd if=/dev/urandom of="$TMPDIR/1mb.bin" bs=1048576 count=1 2>/dev/null
dd if=/dev/urandom of="$TMPDIR/5mb.bin" bs=1048576 count=5 2>/dev/null
dd if=/dev/urandom of="$TMPDIR/10mb.bin" bs=1048576 count=10 2>/dev/null
dd if=/dev/urandom of="$TMPDIR/50mb.bin" bs=1048576 count=50 2>/dev/null

# Create text files for search testing
for i in $(seq 1 20); do
  echo "Report $i: quarterly-revenue-analysis-2024-q${i}.csv" > "$TMPDIR/text_$i.csv"
done

echo "Sample files created."
echo ""

# ─── bench-small: 1,000 objects ───
seed_small() {
  echo "=== Seeding bench-small (1,000 objects) ==="
  local count=0
  for dir in logs data config reports archives; do
    for sub in 2024-01 2024-02 2024-03 2024-04 2024-05 2024-06 2024-07 2024-08 2024-09 2024-10; do
      for i in $(seq 1 20); do
        mc cp "$TMPDIR/1kb.bin" "$MC_ALIAS/bench-small/$dir/$sub/file-$(printf '%04d' $i).dat" --quiet 2>/dev/null
        count=$((count + 1))
      done
    done
  done
  echo "  Seeded $count objects in bench-small"
}

# ─── bench-medium: 10,000 objects ───
seed_medium() {
  echo "=== Seeding bench-medium (10,000 objects) ==="
  local count=0
  # 10 top-level dirs × 10 months × 10 sub-dirs × 10 files = 10,000
  for dir in logs data config reports archives metrics events traces spans profiles; do
    for month in 01 02 03 04 05 06 07 08 09 10; do
      for sub in alpha bravo charlie delta echo foxtrot golf hotel india juliet; do
        for i in $(seq 1 10); do
          mc cp "$TMPDIR/1kb.bin" "$MC_ALIAS/bench-medium/$dir/2024-$month/$sub/obj-$(printf '%04d' $i).dat" --quiet 2>/dev/null
          count=$((count + 1))
        done
      done
    done
    echo "  $dir: seeded (running total: $count)"
  done
  echo "  Seeded $count objects in bench-medium"
}

# ─── bench-large: 50,000 objects ───
seed_large() {
  echo "=== Seeding bench-large (50,000 objects) ==="
  local count=0
  # 10 dirs × 12 months × ~417 files per month ≈ 50,000
  # Simplified: 10 dirs × 50 sub-prefixes × 100 files = 50,000
  for dir in events clicks impressions conversions sessions pageviews errors warnings transactions requests; do
    for sub in $(seq -w 1 50); do
      for i in $(seq 1 100); do
        mc cp "$TMPDIR/1kb.bin" "$MC_ALIAS/bench-large/$dir/partition-$sub/record-$(printf '%05d' $i).dat" --quiet 2>/dev/null
        count=$((count + 1))
      done
    done
    echo "  $dir: seeded (running total: $count)"
  done
  echo "  Seeded $count objects in bench-large"
}

# ─── bench-mixed: 5,000 objects with realistic file types ───
seed_mixed() {
  echo "=== Seeding bench-mixed (5,000 objects, realistic types) ==="
  local count=0

  # Parquet-like files (data lake pattern)
  for table in tracking events analytics ingest; do
    for year in 2024 2025; do
      for month in 01 02 03 04 05 06 07 08 09 10 11 12; do
        for part in $(seq -w 0 9); do
          mc cp "$TMPDIR/100kb.bin" "$MC_ALIAS/bench-mixed/warehouse/$table/data/year=$year/month=$month/part-$part-$(openssl rand -hex 4).parquet" --quiet 2>/dev/null
          count=$((count + 1))
        done
      done
    done
    echo "  $table: seeded (running total: $count)"
  done

  # Log files
  for svc in api-gateway auth-service worker scheduler; do
    for day in $(seq -w 1 90); do
      mc cp "$TMPDIR/10kb.bin" "$MC_ALIAS/bench-mixed/logs/$svc/2024/day-$day/app.log" --quiet 2>/dev/null
      count=$((count + 1))
    done
  done
  echo "  logs: seeded (running total: $count)"

  # Config files
  for env in production staging development; do
    for svc in api frontend worker cron monitoring alerting; do
      mc cp "$TMPDIR/1kb.bin" "$MC_ALIAS/bench-mixed/config/$env/$svc/config.yaml" --quiet 2>/dev/null
      mc cp "$TMPDIR/1kb.bin" "$MC_ALIAS/bench-mixed/config/$env/$svc/secrets.enc" --quiet 2>/dev/null
      count=$((count + 2))
    done
  done
  echo "  config: seeded (running total: $count)"

  # Backups (larger files)
  for i in $(seq 1 20); do
    mc cp "$TMPDIR/1mb.bin" "$MC_ALIAS/bench-mixed/backups/db/backup-2024-$(printf '%03d' $i).sql.gz" --quiet 2>/dev/null
    count=$((count + 1))
  done
  echo "  backups: seeded (running total: $count)"

  # CSV reports with searchable names
  for dept in engineering marketing sales finance operations; do
    for q in Q1 Q2 Q3 Q4; do
      for type in revenue-analysis cost-breakdown headcount-report budget-forecast; do
        mc cp "$TMPDIR/text_1.csv" "$MC_ALIAS/bench-mixed/reports/$dept/2024-$q/$type.csv" --quiet 2>/dev/null
        count=$((count + 1))
      done
    done
  done
  echo "  reports: seeded (running total: $count)"

  echo "  Seeded $count objects in bench-mixed"
}

# ─── Run seeding ───
echo "Starting data seeding..."
echo ""

# Run all in sequence (each takes a few minutes)
seed_small
echo ""
seed_mixed
echo ""

echo "=== Small + Mixed seeding complete ==="
echo ""
echo "For medium/large buckets (takes longer), run:"
echo "  $0 medium   — seeds 10,000 objects"
echo "  $0 large    — seeds 50,000 objects"
echo ""

# Allow running specific bucket seeds
if [ "${1:-}" = "medium" ]; then
  seed_medium
elif [ "${1:-}" = "large" ]; then
  seed_large
elif [ "${1:-}" = "all" ]; then
  seed_medium
  echo ""
  seed_large
fi

echo "=== Done ==="

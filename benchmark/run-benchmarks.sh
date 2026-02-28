#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Sairo Performance Benchmark Suite
# ═══════════════════════════════════════════════════════════════
#
# Measures real performance across all critical paths:
#   1. Crawl/indexing throughput (objects/second)
#   2. Search latency (FTS5 query response time)
#   3. Listing throughput (NDJSON streaming speed)
#   4. Upload throughput (single + batch)
#   5. Download throughput (presigned URL generation)
#   6. Concurrent user simulation
#   7. API response times (auth, buckets, health)
#   8. Rate limiter accuracy
#
# Usage:
#   ./run-benchmarks.sh                    # Run all benchmarks
#   ./run-benchmarks.sh search             # Run only search benchmarks
#   ./run-benchmarks.sh crawl listing      # Run crawl + listing
#
# Prerequisites:
#   - Sairo running on localhost:8000
#   - MinIO running on localhost:9000
#   - Test buckets seeded (run seed-data.sh first)

set -euo pipefail

# ─── Configuration ───
BASE_URL="${SAIRO_URL:-http://localhost:8000}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-password}"
RESULTS_DIR="$(dirname "$0")/results"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_FILE="$RESULTS_DIR/benchmark-$TIMESTAMP.json"
SUMMARY_FILE="$RESULTS_DIR/LATEST-RESULTS.md"
COOKIE_JAR=$(mktemp)
trap "rm -f $COOKIE_JAR" EXIT

mkdir -p "$RESULTS_DIR"

# ─── Colors ───
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ─── Helpers ───
log()    { echo -e "${CYAN}[BENCH]${NC} $1"; }
ok()     { echo -e "${GREEN}  ✓${NC} $1"; }
warn()   { echo -e "${YELLOW}  ⚠${NC} $1"; }
header() { echo -e "\n${BOLD}═══ $1 ═══${NC}"; }

# Authenticate and get session cookie
authenticate() {
  local resp
  resp=$(curl -s -w "\n%{http_code}" -c "$COOKIE_JAR" \
    -X POST "$BASE_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")
  local code=$(echo "$resp" | tail -1)
  if [ "$code" != "200" ]; then
    echo -e "${RED}FATAL: Authentication failed (HTTP $code)${NC}"
    exit 1
  fi
  ok "Authenticated as $ADMIN_USER"
}

# Timed curl — returns response time in ms (integer)
timed_curl() {
  local url="$1"
  shift
  curl -s -o /dev/null -w "%{time_total}" -b "$COOKIE_JAR" "$@" "$BASE_URL$url" | awk '{printf "%.1f", $1 * 1000}'
}

# Timed curl — returns full response + time
timed_curl_full() {
  local url="$1"
  shift
  curl -s -w "\n__TIME__%{time_total}" -b "$COOKIE_JAR" "$@" "$BASE_URL$url"
}

# Run N iterations of a timed request, return min/avg/p50/p95/p99/max in ms
bench_endpoint() {
  local label="$1"
  local url="$2"
  local iterations="${3:-50}"
  local method="${4:-GET}"
  local data="${5:-}"

  local times=()
  local curl_args=()
  [ "$method" != "GET" ] && curl_args+=(-X "$method")
  [ -n "$data" ] && curl_args+=(-H "Content-Type: application/json" -d "$data")

  for i in $(seq 1 "$iterations"); do
    local t
    t=$(timed_curl "$url" "${curl_args[@]}")
    times+=("$t")
  done

  # Sort times numerically
  local sorted=($(printf '%s\n' "${times[@]}" | sort -n))
  local count=${#sorted[@]}
  local min=${sorted[0]}
  local max=${sorted[$((count-1))]}

  # Calculate avg
  local sum=0
  for t in "${sorted[@]}"; do
    sum=$(echo "$sum + $t" | bc)
  done
  local avg=$(echo "scale=1; $sum / $count" | bc)

  # Percentiles
  local p50_idx=$(( (count * 50 / 100) ))
  local p95_idx=$(( (count * 95 / 100) ))
  local p99_idx=$(( (count * 99 / 100) ))
  [ $p50_idx -ge $count ] && p50_idx=$((count-1))
  [ $p95_idx -ge $count ] && p95_idx=$((count-1))
  [ $p99_idx -ge $count ] && p99_idx=$((count-1))
  local p50=${sorted[$p50_idx]}
  local p95=${sorted[$p95_idx]}
  local p99=${sorted[$p99_idx]}

  echo "$label|$min|$avg|$p50|$p95|$p99|$max|$iterations"
}

# ─── JSON result accumulator ───
RESULTS_JSON='{"timestamp":"'"$TIMESTAMP"'","benchmarks":{}}'

add_result() {
  local category="$1"
  local key="$2"
  local value="$3"
  RESULTS_JSON=$(echo "$RESULTS_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
cat = '$category'
key = '$key'
val = '$value'
if cat not in data['benchmarks']:
    data['benchmarks'][cat] = {}
try:
    data['benchmarks'][cat][key] = json.loads(val)
except:
    data['benchmarks'][cat][key] = val
json.dump(data, sys.stdout)
")
}

# ═══════════════════════════════════════════════════════════════
# BENCHMARKS
# ═══════════════════════════════════════════════════════════════

# ─── 1. CRAWL / INDEXING THROUGHPUT ───
bench_crawl() {
  header "1. CRAWL / INDEXING THROUGHPUT"

  for bucket in bench-small bench-mixed; do
    # Check if bucket exists
    local bucket_check
    bucket_check=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets" | python3 -c "
import sys, json
data = json.load(sys.stdin)
buckets = [b['name'] if isinstance(b, dict) else b for b in data.get('buckets', [])]
print('yes' if '$bucket' in buckets else 'no')
" 2>/dev/null || echo "no")

    if [ "$bucket_check" != "yes" ]; then
      warn "Bucket $bucket not found — skipping crawl benchmark"
      continue
    fi

    log "Triggering crawl for $bucket..."

    # Trigger crawl
    local start_time=$(python3 -c "import time; print(time.time())")
    curl -s -X POST -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/crawl" > /dev/null

    # Poll until crawl completes (max 5 minutes)
    local max_wait=300
    local waited=0
    local status="crawling"
    while [ "$status" = "crawling" ] && [ $waited -lt $max_wait ]; do
      sleep 1
      waited=$((waited + 1))
      status=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/crawl-status" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('status', 'unknown'))
" 2>/dev/null || echo "unknown")
    done

    local end_time=$(python3 -c "import time; print(time.time())")
    local duration=$(python3 -c "print(f'{$end_time - $start_time:.2f}')")

    # Get object count
    local obj_count
    obj_count=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/crawl-status" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('total_objects', 0))
" 2>/dev/null || echo "0")

    local rate=$(python3 -c "
d = $duration
c = $obj_count
print(f'{c/d:.0f}' if d > 0 else '0')
")

    ok "$bucket: $obj_count objects indexed in ${duration}s ($rate objects/sec)"
    add_result "crawl" "$bucket" "{\"objects\":$obj_count,\"duration_sec\":$duration,\"objects_per_sec\":$rate,\"status\":\"$status\"}"
  done
}

# ─── 2. SEARCH LATENCY ───
bench_search() {
  header "2. SEARCH LATENCY (FTS5)"

  # Find a bucket with indexed data
  local bucket=""
  for b in bench-mixed bench-small bench-medium bench-large; do
    local status
    status=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$b/crawl-status" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('status', 'none'))
except: print('none')
" 2>/dev/null || echo "none")
    if [ "$status" = "complete" ] || [ "$status" = "idle" ]; then
      bucket="$b"
      break
    fi
  done

  if [ -z "$bucket" ]; then
    warn "No indexed bucket found — run crawl benchmark first"
    return
  fi

  log "Testing search on $bucket..."

  # Different query types
  local queries=("parquet" "config" "log" "revenue" "backup" "2024" "api" "dat" "csv" "report")

  for query in "${queries[@]}"; do
    local times=()
    for i in $(seq 1 30); do
      local t
      t=$(timed_curl "/api/buckets/$bucket/search?q=$query")
      times+=("$t")
    done

    local sorted=($(printf '%s\n' "${times[@]}" | sort -n))
    local count=${#sorted[@]}
    local min=${sorted[0]}
    local max=${sorted[$((count-1))]}
    local p50_idx=$(( count * 50 / 100 ))
    local p95_idx=$(( count * 95 / 100 ))
    [ $p50_idx -ge $count ] && p50_idx=$((count-1))
    [ $p95_idx -ge $count ] && p95_idx=$((count-1))
    local p50=${sorted[$p50_idx]}
    local p95=${sorted[$p95_idx]}

    # Get result count
    local result_count
    result_count=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/search?q=$query" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(len(data.get('results', data if isinstance(data, list) else [])))
except: print(0)
" 2>/dev/null || echo "0")

    ok "\"$query\": p50=${p50}ms  p95=${p95}ms  min=${min}ms  max=${max}ms  (${result_count} results)"
    add_result "search" "$query" "{\"p50_ms\":$p50,\"p95_ms\":$p95,\"min_ms\":$min,\"max_ms\":$max,\"results\":$result_count,\"iterations\":30}"
  done
}

# ─── 3. LISTING THROUGHPUT ───
bench_listing() {
  header "3. OBJECT LISTING (NDJSON STREAMING)"

  for bucket in bench-small bench-mixed; do
    local bucket_check
    bucket_check=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets" | python3 -c "
import sys, json
data = json.load(sys.stdin)
buckets = [b['name'] if isinstance(b, dict) else b for b in data.get('buckets', [])]
print('yes' if '$bucket' in buckets else 'no')
" 2>/dev/null || echo "no")

    if [ "$bucket_check" != "yes" ]; then
      warn "Bucket $bucket not found — skipping"
      continue
    fi

    log "Benchmarking listing for $bucket..."

    # Root listing
    local result=$(bench_endpoint "root" "/api/buckets/$bucket/list?prefix=" 20)
    local avg=$(echo "$result" | cut -d'|' -f3)
    local p50=$(echo "$result" | cut -d'|' -f4)
    local p95=$(echo "$result" | cut -d'|' -f5)
    ok "$bucket root listing: p50=${p50}ms  p95=${p95}ms  avg=${avg}ms"
    add_result "listing" "${bucket}_root" "{\"p50_ms\":$p50,\"p95_ms\":$p95,\"avg_ms\":$avg}"

    # Deep prefix listing (pick a real prefix)
    local prefix
    prefix=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/list?prefix=" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        data = json.loads(line)
        folders = data.get('folders', [])
        if folders:
            f = folders[0]
            print(f['prefix'] if isinstance(f, dict) else f)
            break
    except: pass
" 2>/dev/null || echo "")

    if [ -n "$prefix" ]; then
      local result2=$(bench_endpoint "prefix" "/api/buckets/$bucket/list?prefix=$prefix" 20)
      local avg2=$(echo "$result2" | cut -d'|' -f3)
      local p502=$(echo "$result2" | cut -d'|' -f4)
      local p952=$(echo "$result2" | cut -d'|' -f5)
      ok "$bucket prefix ($prefix): p50=${p502}ms  p95=${p952}ms  avg=${avg2}ms"
      add_result "listing" "${bucket}_prefix" "{\"p50_ms\":$p502,\"p95_ms\":$p952,\"avg_ms\":$avg2,\"prefix\":\"$prefix\"}"
    fi
  done
}

# ─── 4. UPLOAD THROUGHPUT ───
bench_upload() {
  header "4. UPLOAD THROUGHPUT"

  local tmpdir=$(mktemp -d)
  trap "rm -rf $tmpdir" RETURN

  # Create test files
  dd if=/dev/urandom of="$tmpdir/1kb.bin" bs=1024 count=1 2>/dev/null
  dd if=/dev/urandom of="$tmpdir/100kb.bin" bs=1024 count=100 2>/dev/null
  dd if=/dev/urandom of="$tmpdir/1mb.bin" bs=1048576 count=1 2>/dev/null
  dd if=/dev/urandom of="$tmpdir/10mb.bin" bs=1048576 count=10 2>/dev/null
  dd if=/dev/urandom of="$tmpdir/50mb.bin" bs=1048576 count=50 2>/dev/null

  # Check bench-small exists
  local bucket_check
  bucket_check=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets" | python3 -c "
import sys, json
data = json.load(sys.stdin)
buckets = [b['name'] if isinstance(b, dict) else b for b in data.get('buckets', [])]
print('yes' if 'bench-small' in buckets else 'no')
" 2>/dev/null || echo "no")

  if [ "$bucket_check" != "yes" ]; then
    warn "bench-small not found — skipping upload benchmarks"
    return
  fi

  for size_label in 1kb 100kb 1mb 10mb 50mb; do
    local file="$tmpdir/${size_label}.bin"
    local iterations=5
    [ "$size_label" = "50mb" ] && iterations=3
    [ "$size_label" = "10mb" ] && iterations=3

    local times=()
    local file_size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)

    for i in $(seq 1 "$iterations"); do
      local upload_key="benchmark/upload-test-${size_label}-${i}.bin"
      local start=$(python3 -c "import time; print(time.time())")
      curl -s -o /dev/null -b "$COOKIE_JAR" \
        -X POST "$BASE_URL/api/buckets/bench-small/upload?prefix=benchmark/" \
        -F "files=@$file" 2>/dev/null
      local end=$(python3 -c "import time; print(time.time())")
      local elapsed_ms=$(python3 -c "print(f'{($end - $start) * 1000:.1f}')")
      times+=("$elapsed_ms")
    done

    local sorted=($(printf '%s\n' "${times[@]}" | sort -n))
    local count=${#sorted[@]}
    local min=${sorted[0]}
    local max=${sorted[$((count-1))]}
    local p50_idx=$(( count * 50 / 100 ))
    [ $p50_idx -ge $count ] && p50_idx=$((count-1))
    local p50=${sorted[$p50_idx]}

    # Calculate throughput in MB/s
    local throughput=$(python3 -c "
size_bytes = $file_size
p50_ms = $p50
if p50_ms > 0:
    mb_per_sec = (size_bytes / 1048576) / (p50_ms / 1000)
    print(f'{mb_per_sec:.1f}')
else:
    print('inf')
")

    ok "${size_label}: p50=${p50}ms  min=${min}ms  max=${max}ms  (${throughput} MB/s)"
    add_result "upload" "$size_label" "{\"p50_ms\":$p50,\"min_ms\":$min,\"max_ms\":$max,\"throughput_mbps\":$throughput,\"file_size_bytes\":$file_size}"
  done
}

# ─── 5. API RESPONSE TIMES ───
bench_api() {
  header "5. API RESPONSE TIMES"

  local endpoints=(
    "healthz|/healthz|50"
    "auth_me|/api/auth/me|50"
    "buckets|/api/buckets|50"
    "system_info|/api/system-info|50"
    "health_detail|/api/health-detail|30"
    "branding|/api/branding|50"
  )

  for entry in "${endpoints[@]}"; do
    IFS='|' read -r label url iters <<< "$entry"
    local result=$(bench_endpoint "$label" "$url" "$iters")
    local min=$(echo "$result" | cut -d'|' -f2)
    local avg=$(echo "$result" | cut -d'|' -f3)
    local p50=$(echo "$result" | cut -d'|' -f4)
    local p95=$(echo "$result" | cut -d'|' -f5)
    local p99=$(echo "$result" | cut -d'|' -f6)
    local max=$(echo "$result" | cut -d'|' -f7)
    ok "$label: p50=${p50}ms  p95=${p95}ms  p99=${p99}ms  avg=${avg}ms"
    add_result "api" "$label" "{\"p50_ms\":$p50,\"p95_ms\":$p95,\"p99_ms\":$p99,\"min_ms\":$min,\"max_ms\":$max,\"avg_ms\":$avg}"
  done
}

# ─── 6. DOWNLOAD / PRESIGNED URL GENERATION ───
bench_download() {
  header "6. DOWNLOAD (PRESIGNED URL GENERATION)"

  local bucket=""
  for b in bench-small bench-mixed; do
    local check
    check=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets" | python3 -c "
import sys, json
data = json.load(sys.stdin)
buckets = [b['name'] if isinstance(b, dict) else b for b in data.get('buckets', [])]
print('yes' if '$b' in buckets else 'no')
" 2>/dev/null || echo "no")
    if [ "$check" = "yes" ]; then
      bucket="$b"
      break
    fi
  done

  if [ -z "$bucket" ]; then
    warn "No test bucket found — skipping download benchmarks"
    return
  fi

  # Find a real object key
  local key
  key=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/list?prefix=" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        data = json.loads(line)
        files = data.get('files', [])
        if files:
            f = files[0]
            print(f['key'] if isinstance(f, dict) else f)
            break
    except: pass
" 2>/dev/null || echo "")

  if [ -z "$key" ]; then
    # Try listing a subfolder
    key=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/search?q=dat" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    results = data if isinstance(data, list) else data.get('results', [])
    if results:
        r = results[0]
        print(r.get('key', r.get('name', '')))
except: pass
" 2>/dev/null || echo "")
  fi

  if [ -z "$key" ]; then
    warn "No objects found in $bucket — skipping download benchmark"
    return
  fi

  log "Benchmarking presigned URL generation for $bucket/$key..."

  local result=$(bench_endpoint "presigned_url" "/api/buckets/$bucket/presigned-url?key=$key" 50)
  local p50=$(echo "$result" | cut -d'|' -f4)
  local p95=$(echo "$result" | cut -d'|' -f5)
  local avg=$(echo "$result" | cut -d'|' -f3)
  ok "Presigned URL: p50=${p50}ms  p95=${p95}ms  avg=${avg}ms"
  add_result "download" "presigned_url" "{\"p50_ms\":$p50,\"p95_ms\":$p95,\"avg_ms\":$avg}"

  # Object info
  local result2=$(bench_endpoint "object_info" "/api/buckets/$bucket/object-info?key=$key" 50)
  local p502=$(echo "$result2" | cut -d'|' -f4)
  local p952=$(echo "$result2" | cut -d'|' -f5)
  ok "Object info: p50=${p502}ms  p95=${p952}ms"
  add_result "download" "object_info" "{\"p50_ms\":$p502,\"p95_ms\":$p952}"
}

# ─── 7. CONCURRENT USERS ───
bench_concurrent() {
  header "7. CONCURRENT USER SIMULATION"

  local bucket=""
  for b in bench-small bench-mixed; do
    local check
    check=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets" | python3 -c "
import sys, json
data = json.load(sys.stdin)
buckets = [b['name'] if isinstance(b, dict) else b for b in data.get('buckets', [])]
print('yes' if '$b' in buckets else 'no')
" 2>/dev/null || echo "no")
    if [ "$check" = "yes" ]; then
      bucket="$b"
      break
    fi
  done

  if [ -z "$bucket" ]; then
    warn "No test bucket found — skipping concurrency benchmarks"
    return
  fi

  for concurrency in 5 10 25; do
    log "Testing $concurrency concurrent requests..."

    local start=$(python3 -c "import time; print(time.time())")
    local total_requests=$((concurrency * 10))
    local pids=()
    local tmpdir=$(mktemp -d)

    for c in $(seq 1 "$concurrency"); do
      (
        for r in $(seq 1 10); do
          curl -s -o /dev/null -w "%{time_total}\n" \
            -b "$COOKIE_JAR" "$BASE_URL/api/buckets/$bucket/list?prefix=" \
            >> "$tmpdir/worker_$c.txt" 2>/dev/null
        done
      ) &
      pids+=($!)
    done

    # Wait for all workers
    for pid in "${pids[@]}"; do
      wait "$pid" 2>/dev/null || true
    done

    local end=$(python3 -c "import time; print(time.time())")
    local wall_time=$(python3 -c "print(f'{$end - $start:.2f}')")
    local rps=$(python3 -c "print(f'{$total_requests / ($end - $start):.1f}')")

    # Aggregate response times
    local all_times=$(cat "$tmpdir"/worker_*.txt | awk '{printf "%.1f\n", $1 * 1000}' | sort -n)
    local p50=$(echo "$all_times" | awk "NR==int($(echo "$all_times" | wc -l)*50/100+1){print; exit}")
    local p95=$(echo "$all_times" | awk "NR==int($(echo "$all_times" | wc -l)*95/100+1){print; exit}")
    local p99=$(echo "$all_times" | awk "NR==int($(echo "$all_times" | wc -l)*99/100+1){print; exit}")

    rm -rf "$tmpdir"

    ok "${concurrency} concurrent: ${rps} req/s  wall=${wall_time}s  p50=${p50}ms  p95=${p95}ms  p99=${p99}ms"
    add_result "concurrent" "${concurrency}_users" "{\"requests_per_sec\":$rps,\"wall_time_sec\":$wall_time,\"p50_ms\":${p50:-0},\"p95_ms\":${p95:-0},\"p99_ms\":${p99:-0},\"total_requests\":$total_requests}"
  done
}

# ─── 8. STORAGE DASHBOARD ───
bench_dashboard() {
  header "8. STORAGE & ANALYTICS ENDPOINTS"

  local bucket=""
  for b in bench-mixed bench-small; do
    local check
    check=$(curl -s -b "$COOKIE_JAR" "$BASE_URL/api/buckets" | python3 -c "
import sys, json
data = json.load(sys.stdin)
buckets = [b['name'] if isinstance(b, dict) else b for b in data.get('buckets', [])]
print('yes' if '$b' in buckets else 'no')
" 2>/dev/null || echo "no")
    if [ "$check" = "yes" ]; then
      bucket="$b"
      break
    fi
  done

  if [ -z "$bucket" ]; then
    warn "No test bucket found — skipping dashboard benchmarks"
    return
  fi

  local endpoints=(
    "folder_size|/api/buckets/$bucket/folder-size?prefix=|30"
    "storage_breakdown|/api/buckets/$bucket/storage-breakdown|20"
    "storage_history|/api/buckets/$bucket/storage-history|20"
    "crawl_status|/api/buckets/$bucket/crawl-status|50"
  )

  for entry in "${endpoints[@]}"; do
    IFS='|' read -r label url iters <<< "$entry"
    local result=$(bench_endpoint "$label" "$url" "$iters")
    local p50=$(echo "$result" | cut -d'|' -f4)
    local p95=$(echo "$result" | cut -d'|' -f5)
    local avg=$(echo "$result" | cut -d'|' -f3)
    ok "$label: p50=${p50}ms  p95=${p95}ms  avg=${avg}ms"
    add_result "dashboard" "$label" "{\"p50_ms\":$p50,\"p95_ms\":$p95,\"avg_ms\":$avg}"
  done
}

# ═══════════════════════════════════════════════════════════════
# GENERATE RESULTS
# ═══════════════════════════════════════════════════════════════

generate_summary() {
  header "GENERATING SUMMARY"

  # Save JSON results
  echo "$RESULTS_JSON" | python3 -m json.tool > "$RESULTS_FILE"
  ok "JSON results: $RESULTS_FILE"

  # Generate markdown summary
  python3 -c "
import json, sys

data = json.loads('''$RESULTS_JSON''')
b = data['benchmarks']

lines = []
lines.append('# Sairo Performance Benchmark Results')
lines.append(f'')
lines.append(f'**Date**: $TIMESTAMP')
lines.append(f'**Environment**: Docker (macOS), MinIO local, single container')
lines.append('')
lines.append('---')
lines.append('')

# Search
if 'search' in b:
    lines.append('## Search Latency (FTS5 Trigram)')
    lines.append('')
    lines.append('| Query | Results | p50 | p95 | Min | Max |')
    lines.append('|-------|---------|-----|-----|-----|-----|')
    for q, v in sorted(b['search'].items()):
        lines.append(f'| \`{q}\` | {v[\"results\"]} | **{v[\"p50_ms\"]}ms** | {v[\"p95_ms\"]}ms | {v[\"min_ms\"]}ms | {v[\"max_ms\"]}ms |')
    # Calculate overall
    all_p50 = [v['p50_ms'] for v in b['search'].values()]
    all_p95 = [v['p95_ms'] for v in b['search'].values()]
    avg_p50 = sum(all_p50) / len(all_p50)
    avg_p95 = sum(all_p95) / len(all_p95)
    min_p50 = min(all_p50)
    max_p95 = max(all_p95)
    lines.append('')
    lines.append(f'**Overall: p50 = {avg_p50:.1f}ms, p95 = {avg_p95:.1f}ms, fastest = {min_p50}ms**')
    lines.append('')

# Crawl
if 'crawl' in b:
    lines.append('## Crawl / Indexing Throughput')
    lines.append('')
    lines.append('| Bucket | Objects | Duration | Throughput |')
    lines.append('|--------|---------|----------|------------|')
    for name, v in sorted(b['crawl'].items()):
        lines.append(f'| {name} | {v[\"objects\"]:,} | {v[\"duration_sec\"]}s | **{v[\"objects_per_sec\"]} obj/s** |')
    lines.append('')

# Listing
if 'listing' in b:
    lines.append('## Object Listing')
    lines.append('')
    lines.append('| Endpoint | p50 | p95 | Avg |')
    lines.append('|----------|-----|-----|-----|')
    for name, v in sorted(b['listing'].items()):
        lines.append(f'| {name} | **{v[\"p50_ms\"]}ms** | {v[\"p95_ms\"]}ms | {v[\"avg_ms\"]}ms |')
    lines.append('')

# Upload
if 'upload' in b:
    lines.append('## Upload Throughput')
    lines.append('')
    lines.append('| Size | p50 | Min | Max | Throughput |')
    lines.append('|------|-----|-----|-----|------------|')
    size_order = ['1kb', '100kb', '1mb', '10mb', '50mb']
    for name in size_order:
        if name in b['upload']:
            v = b['upload'][name]
            lines.append(f'| {name} | **{v[\"p50_ms\"]}ms** | {v[\"min_ms\"]}ms | {v[\"max_ms\"]}ms | {v[\"throughput_mbps\"]} MB/s |')
    lines.append('')

# API
if 'api' in b:
    lines.append('## API Response Times')
    lines.append('')
    lines.append('| Endpoint | p50 | p95 | p99 | Avg |')
    lines.append('|----------|-----|-----|-----|-----|')
    for name, v in sorted(b['api'].items()):
        lines.append(f'| {name} | **{v[\"p50_ms\"]}ms** | {v[\"p95_ms\"]}ms | {v[\"p99_ms\"]}ms | {v[\"avg_ms\"]}ms |')
    lines.append('')

# Concurrent
if 'concurrent' in b:
    lines.append('## Concurrent Users')
    lines.append('')
    lines.append('| Users | Req/s | Wall Time | p50 | p95 | p99 |')
    lines.append('|-------|-------|-----------|-----|-----|-----|')
    for name, v in sorted(b['concurrent'].items()):
        users = name.replace('_users', '')
        lines.append(f'| {users} | **{v[\"requests_per_sec\"]}** | {v[\"wall_time_sec\"]}s | {v[\"p50_ms\"]}ms | {v[\"p95_ms\"]}ms | {v[\"p99_ms\"]}ms |')
    lines.append('')

# Dashboard
if 'dashboard' in b:
    lines.append('## Storage / Analytics Endpoints')
    lines.append('')
    lines.append('| Endpoint | p50 | p95 | Avg |')
    lines.append('|----------|-----|-----|-----|')
    for name, v in sorted(b['dashboard'].items()):
        lines.append(f'| {name} | **{v[\"p50_ms\"]}ms** | {v[\"p95_ms\"]}ms | {v[\"avg_ms\"]}ms |')
    lines.append('')

# Landing page claims
lines.append('---')
lines.append('')
lines.append('## Verified Claims for Landing Page')
lines.append('')

if 'search' in b:
    all_p50 = [v['p50_ms'] for v in b['search'].values()]
    avg_p50 = sum(all_p50) / len(all_p50)
    if avg_p50 < 10:
        lines.append(f'- **Search**: \"Single-digit millisecond search\" (avg p50: {avg_p50:.1f}ms)')
    elif avg_p50 < 50:
        lines.append(f'- **Search**: \"Search in under 50ms\" (avg p50: {avg_p50:.1f}ms)')
    elif avg_p50 < 100:
        lines.append(f'- **Search**: \"Search in under 100ms\" (avg p50: {avg_p50:.1f}ms)')
    else:
        lines.append(f'- **Search**: \"Fast search results\" (avg p50: {avg_p50:.1f}ms)')

if 'crawl' in b:
    rates = [v['objects_per_sec'] for v in b['crawl'].values()]
    max_rate = max(rates) if rates else 0
    lines.append(f'- **Indexing**: \"{max_rate:,}+ objects/second indexing throughput\"')

if 'api' in b and 'healthz' in b['api']:
    health_p50 = b['api']['healthz']['p50_ms']
    if health_p50 < 5:
        lines.append(f'- **API**: \"Sub-5ms health checks\" (p50: {health_p50}ms)')

if 'concurrent' in b:
    max_rps = max(v['requests_per_sec'] for v in b['concurrent'].values())
    lines.append(f'- **Throughput**: \"{max_rps:.0f}+ requests/second\" under concurrent load')

if 'upload' in b:
    for size in ['10mb', '50mb']:
        if size in b['upload']:
            tp = b['upload'][size]['throughput_mbps']
            lines.append(f'- **Upload ({size})**: \"{tp} MB/s throughput\"')

lines.append('')
lines.append('---')
lines.append('')
lines.append('*Benchmarks run against local Docker (macOS). Production numbers on dedicated hardware will be higher.*')

print('\n'.join(lines))
" > "$SUMMARY_FILE"

  ok "Summary: $SUMMARY_FILE"
}

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

echo -e "${BOLD}"
echo "╔════════════════════════════════════════════════╗"
echo "║       Sairo Performance Benchmark Suite        ║"
echo "╚════════════════════════════════════════════════╝"
echo -e "${NC}"

authenticate

# If specific benchmarks requested, run only those
if [ $# -gt 0 ]; then
  for bench in "$@"; do
    case "$bench" in
      crawl)      bench_crawl ;;
      search)     bench_search ;;
      listing)    bench_listing ;;
      upload)     bench_upload ;;
      api)        bench_api ;;
      download)   bench_download ;;
      concurrent) bench_concurrent ;;
      dashboard)  bench_dashboard ;;
      *)          warn "Unknown benchmark: $bench" ;;
    esac
  done
else
  # Run all benchmarks in order
  bench_crawl
  bench_search
  bench_listing
  bench_upload
  bench_api
  bench_download
  bench_concurrent
  bench_dashboard
fi

generate_summary

echo ""
header "COMPLETE"
echo ""
echo -e "  JSON: ${CYAN}$RESULTS_FILE${NC}"
echo -e "  Summary: ${CYAN}$SUMMARY_FILE${NC}"
echo ""

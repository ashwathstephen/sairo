#!/usr/bin/env bash
# Zero-write reconcile gate (SAE-72): fresh 9.9M-object bucket, then three consecutive unchanged full
# reconciles in one pod. Asserted per cycle: complete, rows == provider truth, ZERO object rows written
# (no row carries the new generation), search marker current without a trigram rebuild, cycle peak
# anonymous memory < 800 MiB (cgroup `anon`: what the process actually holds; memory.current also
# counts the reclaimable page cache of a 6 GB database and sits at the limit regardless), no container
# restart. Reported per cycle: wall time, bytes written by the process during the listing phase (up to
# "Crawl complete": the reconcile itself) and in total (through the post-crawl folder-stats /
# prefix-children rebuilds and the search-marker stamp), peak /data growth, WAL file size, memory peaks.
# With BASELINE_MB / BASELINE_S set (same workload, previous image, same windows), each cycle must
# write <= 10 % of the baseline's listing-phase bytes and take no longer than the baseline.
# Exit code = failures.
#   ./zero_write_gate.sh <image-tag>          env: SPEC=spec-main-only.json CYCLES=3 BASELINE_MB= BASELINE_S=
set -u
TAG=$1; HERE=$(cd "$(dirname "$0")" && pwd); FAILS=0; PEAK_MAX=$((800*1024*1024))
SPEC=${SPEC:-spec-main-only.json}; CYCLES=${CYCLES:-3}; BASELINE_MB=${BASELINE_MB:-}; BASELINE_S=${BASELINE_S:-}
B=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['buckets'][0]['name'])" "$HERE/$SPEC")
say(){ echo "[$(date +%T)] $*"; }
pass(){ say "PASS: $*"; }
fail(){ say "FAIL: $*"; FAILS=$((FAILS+1)); }
check(){ if eval "$1"; then pass "$2"; else fail "$2 (got: $(eval "echo $3" 2>/dev/null))"; fi; }
kubectl config use-context kind-sairo-lab >/dev/null
helm upgrade sairo "$HERE/../../charts/sairo" -n lab --reuse-values \
  --set-string 'extraEnv[0].name=SUBPREFIX_SPLIT_MIN_OBJECTS,extraEnv[0].value=50000' \
  --set-string 'extraEnv[1].name=FULL_CRAWL_INTERVAL,extraEnv[1].value=240' \
  --set crawler.recrawlInterval=60 >/dev/null || { echo "helm upgrade (intervals) failed"; exit 1; }
"$HERE/run_variant.sh" "$TAG" "$HERE/$SPEC" --for 1800 >/dev/null 2>&1
ENVS=$(kubectl -n lab exec deploy/sairo -- sh -c 'echo "$FULL_CRAWL_INTERVAL/$RECRAWL_INTERVAL"' 2>/dev/null)
[ "$ENVS" = "240/60" ] || { echo "FAIL: pod intervals are '$ENVS', expected 240/60"; exit 1; }
sim(){ kubectl -n lab exec deploy/s3sim -- python3 -c "import json,urllib.request as u; r=u.Request('http://127.0.0.1:9001$1', data=json.dumps($2).encode() if '$2' else None, headers={'Content-Type':'application/json'}); print(u.urlopen(r).read().decode())" 2>/dev/null; }
truth(){ sim "/truth?bucket=$B" '' | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])"; }
# One exec per refresh. `refresh` reads the cheap status columns; `refresh full` adds the two 9.9M-row
# counts (rows, rows written this generation), used only at checkpoints so polling does not load the DB.
FIELDS="{}"
refresh(){ FIELDS=$(kubectl -n lab exec -i deploy/sairo -- python3 - <<PY 2>/dev/null
import sqlite3, os, json; c=sqlite3.connect("/data/$B.db")
r=dict(zip(("status","last_crawl_end","last_error","gen","fts_gen"), c.execute("SELECT status,last_crawl_end,last_error,current_crawl_gen,fts_ready_gen FROM crawl_status").fetchone()))
if "${1:-}" == "full":
    r["rows"]=c.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    r["rows_written"]=c.execute("SELECT COUNT(*) FROM objects WHERE crawl_gen=(SELECT current_crawl_gen FROM crawl_status)").fetchone()[0]
r["wal_mb"]="%.1f" % (os.path.getsize("/data/$B.db-wal")/1048576)
print(json.dumps(r))
PY
); }
field(){ python3 -c "import sys,json; print(json.load(sys.stdin).get('$1'))" <<<"$FIELDS"; }
pod(){ kubectl -n lab exec deploy/sairo -- sh -c "$1" 2>/dev/null; }
wbytes(){ pod 'awk "/^write_bytes/{print \$2}" /proc/1/io'; }
anon(){ pod 'awk "/^anon /{print \$2}" /sys/fs/cgroup/memory.stat'; }
cur(){ pod 'cat /sys/fs/cgroup/memory.current'; }
datamb(){ pod 'du -sm /data | cut -f1'; }
restarts(){ kubectl -n lab get pod -l app=sairo -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'; }
logs(){ kubectl -n lab logs deploy/sairo --tail=50000 2>/dev/null; }
completes(){ logs | grep -c "\[default:$B\] Crawl complete"; }
lastdur(){ logs | grep "\[default:$B\] Crawl complete" | tail -1 | sed -E 's/.* in ([0-9.]+)s.*/\1/'; }
snapshot(){ echo "status=$(field status) rows=$(field rows) gen=$(field gen) fts=$(field fts_gen) rows_written=$(field rows_written) wal=$(field wal_mb)MB anon=$(( $(anon)/1048576 ))MB current=$(( $(cur)/1048576 ))MB"; }
wait_for(){ local t=0; while [ $t -lt $2 ]; do eval "$1" && return 0; sleep 10; t=$((t+10)); done; fail "$3 timed out after $2 s"; return 1; }

refresh full; say "initial: $(snapshot) dur=$(lastdur)s"
check '[ "$(field status)" = complete ]' "initial crawl complete" '$(field status)'
check '[ "$(field rows)" = "$(truth)" ]' "initial rows == truth" '$(field rows) vs $(truth)'
check '[ "$(field rows_written)" = "$(field rows)" ]' "initial crawl wrote every row" '$(field rows_written)'
SAMPLES=$HERE/out/mem_samples.csv; echo "cycle,ts,anon,current,data_mb" > "$SAMPLES"

for C in $(seq 1 "$CYCLES"); do
  refresh; GEN0=$(field gen); N=$(completes)
  wait_for 'refresh; [ "$(field gen)" -gt '$GEN0' ]' 600 "cycle $C reconcile start"
  W0=$(wbytes); D0=$(datamb)
  ( while :; do a=$(anon); c=$(cur); d=$(datamb); [ -n "$a" ] && echo "$C,$(date +%s),$a,$c,$d" >> "$SAMPLES"; sleep 5; done ) & SAMPLER=$!
  wait_for 'refresh; [ "$(field status)" = complete ] && [ "$(completes)" -gt '$N' ]' 2400 "cycle $C reconcile"
  kill $SAMPLER 2>/dev/null; wait $SAMPLER 2>/dev/null
  W1=$(wbytes)
  PEAK=$(awk -F, -v c=$C '$1==c && $3>m{m=$3} END{print m+0}' "$SAMPLES")
  PEAKCUR=$(awk -F, -v c=$C '$1==c && $4>m{m=$4} END{print m+0}' "$SAMPLES")
  PEAKDATA=$(awk -F, -v c=$C '$1==c && $5>m{m=$5} END{print m+0}' "$SAMPLES")
  refresh full; DUR=$(lastdur); WR=$(( (W1-W0)/1048576 ))
  check '[ "$(field status)" = complete ]' "cycle $C complete" '$(field status)'
  check '[ "$(field rows)" = "$(truth)" ]' "cycle $C rows == truth" '$(field rows) vs $(truth)'
  check '[ "$(field rows_written)" = 0 ]' "cycle $C wrote zero object rows (unchanged bucket)" '$(field rows_written)'
  # The search marker is stamped at the end of the post-crawl step, so this also closes the total write window.
  wait_for 'refresh; [ "$(field fts_gen)" = "$(field gen)" ]' 600 "cycle $C search marker"
  W2=$(wbytes); WT=$(( (W2-W0)/1048576 )); refresh full
  say "cycle $C: $(snapshot) dur=${DUR}s written=${WR}MB total_written=${WT}MB data_growth=$((PEAKDATA-D0))MB peak_anon=$((PEAK/1048576))MB peak_current=$((PEAKCUR/1048576))MB"
  if [ -n "$BASELINE_MB" ]; then check '[ '$WR' -le '$((BASELINE_MB/10))' ]' "cycle $C listing phase wrote <= 10 % of the baseline ($BASELINE_MB MB)" '${WR}MB'; fi
  if [ -n "$BASELINE_S" ]; then check 'python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) <= float(sys.argv[2]) else 1)" '$DUR' '$BASELINE_S'' "cycle $C no slower than the baseline (${BASELINE_S}s)" '${DUR}s'; fi
  check '[ "$(field fts_gen)" = "$(field gen)" ]' "cycle $C search generation current" '$(field fts_gen) vs $(field gen)'
  check '[ "$(logs | grep "\[default:$B\]" | grep -c "FTS rebuild skipped")" -ge '$C' ]' "cycle $C skipped the trigram rebuild" '$(logs | grep -c "FTS rebuild skipped")'
  check '[ '$PEAK' -lt '$PEAK_MAX' ]' "cycle $C peak anonymous memory < 800 MiB" '$((PEAK/1048576))MB'
  check '[ "$(restarts)" = 0 ]' "cycle $C no container restart" '$(restarts)'
done
LOGS=$(logs)
OPS=$(echo "$LOGS" | grep -cE "database is locked|Crawl error:|Delta crawl error|FTS rebuild failed|Post-crawl .* failed")
TB=$(( $(echo "$LOGS" | grep -c "^Traceback") - $(echo "$LOGS" | grep -A5 "^Traceback" | grep -c "module 'bcrypt'") ))
check '[ "$OPS" = 0 ] && [ "$TB" -le 0 ]' "zero lock errors, crawl/delta errors, rebuild failures, or unexpected tracebacks" '"ops=$OPS tracebacks=$TB"'
say "memory.peak overall (incl. page cache): $(( $(pod 'cat /sys/fs/cgroup/memory.peak')/1048576 ))MB"
say "RESULT: $FAILS failure(s)"; exit $FAILS

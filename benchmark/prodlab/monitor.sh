#!/bin/bash
# Lab monitor: watches all buckets' crawl-status, pod RSS and DB size until every bucket is complete
# (or --for SECONDS elapses). Writes a CSV + human log under out/. Read-only against the backend.
#   ./monitor.sh [--for 3600] [--interval 30]
set -u
HERE=$(cd "$(dirname "$0")" && pwd); OUT=$HERE/out; mkdir -p "$OUT"
FOR=7200; INT=30
while [ $# -gt 0 ]; do case "$1" in --for) FOR=$2; shift 2;; --interval) INT=$2; shift 2;; *) shift;; esac; done
NS=lab; PORT=18000
kubectl -n $NS port-forward deploy/sairo $PORT:8000 >/dev/null 2>&1 & PF=$!; trap 'kill $PF 2>/dev/null' EXIT
for i in $(seq 1 30); do curl -sf http://127.0.0.1:$PORT/healthz >/dev/null && break; sleep 1; [ $i -eq 30 ] && { echo "backend unreachable via port-forward"; exit 1; }; done
CJ=$OUT/cj; curl -s -c $CJ -X POST http://127.0.0.1:$PORT/api/auth/login -H 'Content-Type: application/json' -d '{"username":"admin","password":"labpass123"}' >/dev/null
api() { curl -s -b $CJ "http://127.0.0.1:$PORT$1"; }
BUCKETS=$(api /api/buckets | python3 -c 'import sys,json; d=json.load(sys.stdin); bs=d if isinstance(d,list) else d.get("buckets",d); print(" ".join((b["name"] if isinstance(b,dict) else b) for b in bs))')
echo "buckets: $BUCKETS"
CSV=$OUT/progress.csv; echo "ts,bucket,status,total_objects,crawl_duration,crawl_elapsed,full_crawl_complete" > $CSV
POD() { kubectl -n $NS get pod -l app=sairo -o jsonpath='{.items[0].metadata.name}'; }
t0=$(date +%s)
while :; do
  now=$(date +%s); ts=$(date +%T); pod=$(POD)
  rss=$(kubectl -n $NS exec $pod -- sh -c 'grep VmRSS /proc/1/status' 2>/dev/null | awk '{printf "%.0f", $2/1024}')
  dbsz=$(kubectl -n $NS exec $pod -- sh -c 'du -sm /data 2>/dev/null | cut -f1' 2>/dev/null)
  done_n=0; total_n=0; line=""
  for b in $BUCKETS; do
    j=$(api "/api/buckets/$b/crawl-status")
    read st tot dur el fcc <<<"$(echo "$j" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status",""), d.get("total_objects") or 0, d.get("crawl_duration") or 0, d.get("crawl_elapsed") or "", d.get("full_crawl_complete",""))')"
    echo "$ts,$b,$st,$tot,$dur,$el,$fcc" >> $CSV
    total_n=$((total_n+1)); [ "$st" = "complete" ] && done_n=$((done_n+1))
    [ "$b" = "seg-main" ] && line="seg-main: $st objects=$tot dur=${dur}s elapsed=${el}s"
  done
  echo "[$ts +$((now-t0))s] complete=$done_n/$total_n | $line | RSS=${rss}MB /data=${dbsz}MB" | tee -a $OUT/monitor.log
  [ "$done_n" -eq "$total_n" ] && { echo "ALL COMPLETE after $((now-t0))s" | tee -a $OUT/monitor.log; break; }
  [ $((now-t0)) -ge $FOR ] && { echo "time budget reached" | tee -a $OUT/monitor.log; break; }
  sleep $INT
done
echo "--- crawl summary lines from the pod log ---"
kubectl -n $NS logs $(POD) 2>/dev/null | grep -E "Crawl complete|Resuming|interrupted|Sub-prefix|Expanded|Force-releasing|database is locked|Error" | tail -40 | tee -a $OUT/monitor.log

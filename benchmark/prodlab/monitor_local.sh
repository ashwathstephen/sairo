#!/bin/bash
# Monitor a locally running backend (read-only production run). Bucket names are masked as b01..bNN in all output.
#   ./monitor_local.sh <port> <password> <out_dir> [--for SECONDS] [--interval 60] [--pid PIDFILE] [--dbdir DIR]
set -u
PORT=$1; PASS=$2; OUT=$3; shift 3; FOR=14400; INT=60; PIDF=""; DBDIR=""
while [ $# -gt 0 ]; do case "$1" in --for) FOR=$2; shift 2;; --interval) INT=$2; shift 2;; --pid) PIDF=$2; shift 2;; --dbdir) DBDIR=$2; shift 2;; *) shift;; esac; done
mkdir -p "$OUT"; CJ=$OUT/cj
curl -s -c $CJ -X POST http://127.0.0.1:$PORT/api/auth/login -H 'Content-Type: application/json' -d "{\"username\":\"admin\",\"password\":\"$PASS\"}" >/dev/null
api() { curl -s -b $CJ "http://127.0.0.1:$PORT$1"; }
BUCKETS=$(api /api/buckets | python3 -c 'import sys,json; d=json.load(sys.stdin); print(" ".join(b["name"] for b in d["buckets"]))')
i=0; declare -A MASK; for b in $BUCKETS; do i=$((i+1)); MASK[$b]=$(printf "b%02d" $i); done
echo "buckets: $i (names masked)"
CSV=$OUT/progress.csv; echo "ts,bucket,status,total_objects,crawl_duration,crawl_elapsed,full_crawl_complete" > $CSV
t0=$(date +%s)
while :; do
  now=$(date +%s); ts=$(date +%T); rss=""; dbsz=""
  [ -n "$PIDF" ] && rss=$(ps -o rss= -p $(cat $PIDF) 2>/dev/null | awk '{printf "%.0f", $1/1024}')
  [ -n "$DBDIR" ] && dbsz=$(du -sm "$DBDIR" 2>/dev/null | cut -f1)
  done_n=0; total_n=0; tot_objs=0; biggest=""
  for b in $BUCKETS; do
    j=$(api "/api/buckets/$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$b")/crawl-status")
    read st tot dur el fcc <<<"$(echo "$j" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status",""), d.get("total_objects") or 0, d.get("crawl_duration") or 0, d.get("crawl_elapsed") or "", d.get("full_crawl_complete",""))' 2>/dev/null || echo "? 0 0 0 ?")"
    echo "$ts,${MASK[$b]},$st,$tot,$dur,$el,$fcc" >> $CSV
    total_n=$((total_n+1)); tot_objs=$((tot_objs+tot)); [ "$st" = "complete" ] && done_n=$((done_n+1))
    [ -z "$biggest" ] && biggest="${MASK[$b]}:$st:$tot"
    [ "$tot" -gt "${biggest##*:}" ] 2>/dev/null && biggest="${MASK[$b]}:$st:$tot"
  done
  echo "[$ts +$((now-t0))s] complete=$done_n/$total_n objects_indexed=$tot_objs largest=$biggest RSS=${rss}MB db=${dbsz}MB" | tee -a $OUT/monitor.log
  [ "$done_n" -eq "$total_n" ] && { echo "ALL COMPLETE after $((now-t0))s" | tee -a $OUT/monitor.log; break; }
  [ $((now-t0)) -ge $FOR ] && { echo "time budget reached" | tee -a $OUT/monitor.log; break; }
  sleep $INT
done

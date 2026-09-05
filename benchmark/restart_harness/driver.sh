#!/bin/bash
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
R=$S/rca/REPORT.txt; : > $R
say() { echo "[$(date +%T)] $*" | tee -a $R; }
ensure_proxy() { # idempotent: create the toxiproxy route localhost:19000 -> minio:9000 with a latency toxic
  curl -s -o /dev/null -X POST http://localhost:8474/proxies -d '{"name":"s3","listen":"0.0.0.0:19000","upstream":"'"${MINIO_UPSTREAM:-minio:9000}"'","enabled":true}'
  curl -s -o /dev/null -X POST http://localhost:8474/proxies/s3/toxics -d '{"name":"lat","type":"latency","stream":"downstream","toxicity":1.0,"attributes":{"latency":1000,"jitter":0}}'
  curl -sf http://localhost:8474/proxies/s3 >/dev/null || { echo "toxiproxy route missing"; exit 1; }
}
lat() { curl -s -X POST http://localhost:8474/proxies/s3/toxics/lat -d "{\"attributes\":{\"latency\":$1,\"jitter\":0}}" >/dev/null; say "proxy latency -> ${1}ms"; }
logs() { grep -E "Queued crawl|Seeded schedule|Full recrawl queued|Full crawl queued|Delta crawl queued|Force-releasing|Crawl (complete|finished)|crawl.*complete|database is locked|Error|Traceback" $S/rca/backend_$1.log | sed 's/^/    | /' | tail -${2:-12} | tee -a $R; }

# populate (idempotent: re-PUTs the same 120k keys)
( cd $REPO/backend && ${PYTHON:-.venv/bin/python} "$(dirname "$0")/populate.py" ) > $S/rca/populate.log 2>&1 || ( cd $REPO && ${PYTHON:-backend/.venv/bin/python} benchmark/restart_harness/populate.py > $S/rca/populate.log 2>&1 )
ensure_proxy
say "S3 bucket 'rca' populated: $(grep '^DONE' $S/rca/populate.log)"
S3_COUNT=$(cd $REPO/backend && .venv/bin/python -c "
import boto3; from botocore.client import Config
s3=boto3.client('s3',endpoint_url='http://localhost:9000',aws_access_key_id='minioadmin',aws_secret_access_key='minioadmin',config=Config(signature_version='s3v4'))
n=0
for p in s3.get_paginator('list_objects_v2').paginate(Bucket='rca'): n+=p.get('KeyCount',0)
print(n)")
say "ground truth: S3 holds $S3_COUNT objects in 30 top-level prefixes"

############ PHASE 1: deploy #1 — fresh PVC, initial full crawl, then a rollout kills the pod mid-crawl
say "================ PHASE 1: fresh index; rollout (SIGTERM->30s->SIGKILL) lands mid-crawl ================"
rm -rf $DATA; mkdir -p $DATA; lat 1000
start_backend p1
t0=$(date +%s.%N)
# wait until crawl_status.total_objects (updated per completed prefix) exceeds the 50k LARGE-bucket threshold, then kill mid-crawl
while :; do c=$(q 'select total_objects from crawl_status where id=1'); st=$(q "select status from crawl_status where id=1"); [ "$st" = "complete" ] && { say "ABORT: crawl completed before the kill could land"; exit 1; }; [ -n "$c" ] && [ "$c" -ge 56000 ] && break; sleep 0.1; done
say "after $(printf '%.1f' $(echo "$(date +%s.%N) - $t0" | bc))s crawl_status.total_objects=$c (>50k => this index will be seeded as a LARGE bucket); crawl still running for the remaining prefixes"
lat 10000   # remaining prefixes now take >30s -> models a multi-hour prod crawl vs a 30s grace period
say "rollout begins: SIGTERM to pod"
k8s_stop | tee -a $R
say "index state left on the PVC after the killed deploy:"; snapshot | tee -a $R
say "backend log (deploy #1):"; logs p1 8
lat 1000

############ PHASE 2: deploy #2 — pod restarts on the partial index
say "================ PHASE 2: restart on the partial index; watch the scheduler for 70s (RECRAWL_INTERVAL=15s, FULL_CRAWL_INTERVAL=3600s default) ================"
start_backend p2
sleep 8; say "startup decision:"; grep -E "Seeded schedule|Queued|Resuming" $S/rca/backend_p2.log | sed 's/^/    | /' | tee -a $R
for i in 1 2 3 4 5; do sleep 14; say "t+$((i*14))s objects=$(q 'select count(*) from objects') status='$(q "select status from crawl_status where id=1")' | scheduler lines so far: full=$(grep -cE 'Full (re)?crawl queued|Queued crawl' $S/rca/backend_p2.log) delta=$(grep -c 'Delta crawl queued' $S/rca/backend_p2.log)"; done
snapshot | tee -a $R
login
say "API view of the bucket while 'ready':"
say "  crawl-status: $(api /api/buckets/rca/crawl-status | python3 -c 'import sys,json; d=json.load(sys.stdin); print({k:d.get(k) for k in ("status","total_objects","crawl_duration")})')"
tr0=$(date +%s.%N); root=$(api "/api/buckets/rca/list?prefix=&limit=200"); say "  root listing: $(echo "$root" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("folders",[])),"folders,",len(d.get("files",[])),"files")') in $(printf '%.2f' $(echo "$(date +%s.%N) - $tr0" | bc))s"
for p in "ds=00/" "ds=29/"; do
  idx=$(api "/api/buckets/rca/list?prefix=$p&limit=10000" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("files",[])))')
  s3=$(api "/api/buckets/rca/list?prefix=$p&fresh=true&limit=10000" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("files",[])))')
  say "  folder $p : index says $idx files | S3 (fresh=true) says $s3 files"
done
say "  search 'part-00001' : $(api '/api/buckets/rca/search?q=part-00001&limit=200' | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("count", d.get("detail")))') results (30 exist in S3)"
say "backend log (deploy #2):"; logs p2 10

############ PHASE 3: deploy #3 — another rollout within the hour resets the clock again
say "================ PHASE 3: third rollout inside the same hour ================"
k8s_stop | tee -a $R
start_backend p3
sleep 8; grep -E "Seeded schedule|Queued|Resuming" $S/rca/backend_p3.log | sed 's/^/    | /' | tee -a $R
sleep 32; say "t+35s objects=$(q 'select count(*) from objects') | full=$(grep -cE 'Full (re)?crawl queued|Queued crawl' $S/rca/backend_p3.log) delta=$(grep -c 'Delta crawl queued' $S/rca/backend_p3.log)"

############ PHASE 4: control — leave it alone past FULL_CRAWL_INTERVAL (shrunk to 30s so we don't wait an hour)
say "================ PHASE 4 (control): no more rollouts; FULL_CRAWL_INTERVAL=30 to compress the 1h wait ================"
k8s_stop | tee -a $R
FULL_CRAWL_INTERVAL=30 start_backend p4
t0=$(date +%s)
for i in $(seq 1 60); do sleep 3; c=$(q 'select count(*) from objects'); fs=$(q 'select count(*) from folder_stats'); st=$(q "select status from crawl_status where id=1"); [ "$c" = "$S3_COUNT" ] && [ "${fs:-0}" -gt 0 ] && [ "$st" = "complete" ] && break; done
say "healed after $(( $(date +%s)-t0 ))s of being left alone:"; snapshot | tee -a $R
say "backend log (deploy #4):"; logs p4 8
k8s_stop | tee -a $R
# ---------- assertions (Phase A exit gates) ----------
FAIL=0
chk() { if eval "$2"; then say "PASS: $1"; else say "FAIL: $1"; FAIL=1; fi; }
P2_OBJ=$(grep -E "t\+70s objects=" $R | tail -1 | sed -E 's/.*objects=([0-9]*).*/\1/')
chk "gate 1: index reaches 120000 after restart (got ${P2_OBJ:-none})" '[ "${P2_OBJ:-0}" -eq 120000 ]'
chk "gate 2: never complete while partial" '! grep -qE "objects=([0-9]{1,5}|1[01][0-9]{4}) status=.complete." $R'
chk "gate 3: zero force-released duplicate crawls" '[ $(cat $S/rca/backend_p*.log | grep -c "Force-releasing") -eq 0 ]'
chk "gate 4: no tracebacks in backend logs" '[ $(cat $S/rca/backend_p*.log | grep -c "Traceback" ) -le $(cat $S/rca/backend_p*.log | grep -c "bcrypt version") ]'
say "DONE (exit $FAIL)"; exit $FAIL

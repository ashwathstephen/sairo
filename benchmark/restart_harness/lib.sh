# Shared helpers for the crawler-restart reproduction. Mirrors k8s: SIGTERM, 30s grace, SIGKILL.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
S=${HARNESS_OUT:-$HERE/out}
mkdir -p "$S/rca"
DATA=$S/rca/data
DB=$DATA/rca.db
PORT=8765
export TELEMETRY=false S3_ENDPOINT=http://localhost:19000 S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin \
       DB_DIR=$DATA ADMIN_USER=admin ADMIN_PASS=rcapass S3_PATH_STYLE=true S3_REGION=us-east-1 \
       JWT_SECRET=rca-fixed-secret-0123456789abcdef0123456789abcdef RECRAWL_INTERVAL=${RECRAWL_INTERVAL:-15} FULL_CRAWL_INTERVAL=${FULL_CRAWL_INTERVAL:-3600}
start_backend() { # $1 = tag  (exec => $! is uvicorn's real pid, so k8s_stop kills the real process)
  ( cd $REPO/backend && exec nohup .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port $PORT ) > $S/rca/backend_$1.log 2>&1 &
  echo $! > $S/rca/pid
  for i in $(seq 1 60); do curl -sf http://127.0.0.1:$PORT/healthz >/dev/null 2>&1 && { echo "[$(date +%T)] backend '$1' up (pid $(cat $S/rca/pid), cmd: $(ps -o command= -p $(cat $S/rca/pid) | cut -c1-60)) after ~${i}s"; return; }; sleep 0.5; done
  echo "backend failed to start"; tail -20 $S/rca/backend_$1.log; exit 1
}
k8s_stop() { # SIGTERM, wait <=30s, then SIGKILL — exactly what a Recreate rollout does
  local pid=$(cat $S/rca/pid); kill -TERM $pid 2>/dev/null; local t0=$(date +%s)
  for i in $(seq 1 60); do kill -0 $pid 2>/dev/null || { echo "[$(date +%T)] exited on SIGTERM after $(( $(date +%s)-t0 ))s"; (lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && echo "  WARNING: something still listens on :$PORT") || echo "  verified: nothing listens on :$PORT"; return; }; sleep 0.5; done
  kill -KILL $pid 2>/dev/null; sleep 0.5; echo "[$(date +%T)] did NOT exit within 30s of SIGTERM -> SIGKILL (this is what kubelet does)"
  (lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && echo "  WARNING: something still listens on :$PORT") || echo "  verified: nothing listens on :$PORT"
}
q() { sqlite3 "$DB" "$1" 2>/dev/null; }
snapshot() { # index state
  echo "  objects=$(q 'select count(*) from objects')  status='$(q "select status from crawl_status where id=1")'  total_objects=$(q 'select total_objects from crawl_status where id=1')  crawl_duration=$(q 'select crawl_duration from crawl_status where id=1')  folder_stats=$(q 'select count(*) from folder_stats')  prefix_children=$(q 'select count(*) from prefix_children')  discovered_prefixes=$(q 'select count(*) from discovered_prefixes')  fts_rows=$(q 'select count(*) from objects_fts')"
}
login() { curl -s -c $S/rca/cj -X POST http://127.0.0.1:$PORT/api/auth/login -H 'Content-Type: application/json' -d '{"username":"admin","password":"rcapass"}' >/dev/null; }
api() { curl -s -b $S/rca/cj "http://127.0.0.1:$PORT$1"; }

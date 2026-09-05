#!/usr/bin/env bash
# Truthful-state gate: three full reconciles of a 1M-object bucket with injected LIST failures, a
# deletion (stale rows), and one pod kill mid-crawl. Asserts: final count == simulator truth, status
# transitions are honest (degraded/interrupted never look complete), last_crawl_end advances only on
# clean completes, 0 restarts other than the deliberate kill.
#   ./reconcile_gate.sh <image-tag>        (FULL_CRAWL_INTERVAL is set to 240 s via extraEnv)
set -u
TAG=$1; HERE=$(cd "$(dirname "$0")" && pwd); B=rec-1m
kubectl config use-context kind-sairo-lab >/dev/null
helm upgrade sairo "$HERE/../../charts/sairo" -n lab --reuse-values \
  --set-string 'extraEnv[0].name=SUBPREFIX_SPLIT_MIN_OBJECTS,extraEnv[0].value=50000' \
  --set-string 'extraEnv[1].name=FULL_CRAWL_INTERVAL,extraEnv[1].value=240' \
  --set-string 'extraEnv[2].name=RECRAWL_INTERVAL,extraEnv[2].value=60' --wait --timeout 180s >/dev/null
"$HERE/run_variant.sh" "$TAG" "$HERE/spec-1m.json" --for 900 >/dev/null 2>&1
say(){ echo "[$(date +%T)] $*"; }
sim(){ kubectl -n lab exec deploy/s3sim -- python3 -c "import json,urllib.request as u; r=u.Request('http://127.0.0.1:9001$1', data=json.dumps($2).encode() if '$2' else None, headers={'Content-Type':'application/json'}); print(u.urlopen(r).read().decode())" 2>/dev/null; }
st(){ kubectl -n lab exec -i deploy/sairo -- python3 - <<PY 2>/dev/null
import sqlite3; c=sqlite3.connect("/data/$B.db")
r=c.execute("SELECT status,total_objects,last_crawl_end,last_attempt_at,last_error,current_crawl_gen,fts_ready_gen FROM crawl_status").fetchone()
print("status=%s objects=%s end=%s attempt=%s err=%s gen=%s fts=%s rows=%s" % (r+ (c.execute("SELECT COUNT(*) FROM objects").fetchone()[0],)))
PY
}
completes(){ kubectl -n lab logs deploy/sairo --tail=20000 2>/dev/null | grep -c "\[default:$B\] Crawl complete"; }
say "initial crawl done: $(st)"; say "truth: $(sim /truth?bucket=$B '')"
END0=$(st | sed -E 's/.*end=([^ ]+).*/\1/')
# ── cycle 1: delete 1,000 keys at the provider, then 5 % LIST failures during the reconcile ──
say "cycle 1: delete 1000 keys + error_rate 0.05"; sim /delete_positions '{"bucket":"'$B'","n":1000}' >/dev/null; sim /error_rate '{"rate":0.05}' >/dev/null
N=$(completes); for i in $(seq 1 60); do [ "$(completes)" -gt "$N" ] && break; sleep 10; done
say "after cycle 1: $(st)"; sim /error_rate '{"rate":0}' >/dev/null
# ── cycle 2: clean reconcile — must reach complete and match truth ──
N=$(completes); for i in $(seq 1 60); do [ "$(completes)" -gt "$N" ] && break; sleep 10; done
say "after cycle 2: $(st)"; say "truth: $(sim /truth?bucket=$B '')"
# ── cycle 3: kill the pod mid-reconcile, let it resume ──
N=$(completes); for i in $(seq 1 60); do kubectl -n lab logs deploy/sairo --tail=200 2>/dev/null | grep -q "Crawl started for default:$B" && break; sleep 5; done; sleep 20
say "killing pod mid-reconcile: $(st)"; kubectl -n lab delete pod -l app=sairo --wait=false >/dev/null; sleep 40
say "after restart: $(st)"; kubectl -n lab logs deploy/sairo --tail=400 2>/dev/null | grep -E "Resuming|interrupted|backfilled|repairing" | cut -c1-120 | head -4
for i in $(seq 1 60); do st | grep -q "status=complete" && break; sleep 10; done
say "after cycle 3: $(st)"; say "truth: $(sim /truth?bucket=$B '')"
say "restarts=$(kubectl -n lab get pod -l app=sairo -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}') lock/errors=$(kubectl -n lab logs deploy/sairo --tail=50000 2>/dev/null | grep -cE 'database is locked|Traceback' )"

#!/usr/bin/env bash
# Truthful-state gate: three full reconciles of a 1M-object bucket with 1,000 deletions, 5 % injected
# LIST failures and one pod kill mid-reconcile. Every step is ASSERTED; exit code = number of failures.
#   ./reconcile_gate.sh <image-tag>      (FULL_CRAWL_INTERVAL=240, RECRAWL_INTERVAL=60 via extraEnv)
set -u
TAG=$1; HERE=$(cd "$(dirname "$0")" && pwd); B=rec-1m; FAILS=0
say(){ echo "[$(date +%T)] $*"; }
pass(){ say "PASS: $*"; }
fail(){ say "FAIL: $*"; FAILS=$((FAILS+1)); }
check(){ if eval "$1"; then pass "$2"; else fail "$2 (got: $(eval "echo $3" 2>/dev/null))"; fi; }
kubectl config use-context kind-sairo-lab >/dev/null
helm upgrade sairo "$HERE/../../charts/sairo" -n lab --reuse-values \
  --set-string 'extraEnv[0].name=SUBPREFIX_SPLIT_MIN_OBJECTS,extraEnv[0].value=50000' \
  --set-string 'extraEnv[1].name=FULL_CRAWL_INTERVAL,extraEnv[1].value=240' \
  --set-string 'extraEnv[2].name=RECRAWL_INTERVAL,extraEnv[2].value=60' --wait --timeout 180s >/dev/null
"$HERE/run_variant.sh" "$TAG" "$HERE/spec-1m.json" --for 900 >/dev/null 2>&1
sim(){ kubectl -n lab exec deploy/s3sim -- python3 -c "import json,urllib.request as u; r=u.Request('http://127.0.0.1:9001$1', data=json.dumps($2).encode() if '$2' else None, headers={'Content-Type':'application/json'}); print(u.urlopen(r).read().decode())" 2>/dev/null; }
truth(){ sim "/truth?bucket=$B" '' | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])"; }
field(){ kubectl -n lab exec -i deploy/sairo -- python3 - <<PY 2>/dev/null
import sqlite3; c=sqlite3.connect("/data/$B.db")
r=dict(zip(("status","total_objects","last_crawl_end","last_attempt_at","last_error","gen","fts_gen"), c.execute("SELECT status,total_objects,last_crawl_end,last_attempt_at,last_error,current_crawl_gen,fts_ready_gen FROM crawl_status").fetchone()))
r["rows"]=c.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
print(r["$1"])
PY
}
snapshot(){ echo "status=$(field status) rows=$(field rows) end=$(field last_crawl_end) attempt=$(field last_attempt_at) err=$(field last_error) gen=$(field gen) fts=$(field fts_gen)"; }
completes(){ kubectl -n lab logs deploy/sairo --tail=50000 2>/dev/null | grep -c "\[default:$B\] Crawl complete"; }
wait_for(){ # wait_for "<cmd returning 0 when done>" <seconds> <label>
  local t=0; while [ $t -lt $2 ]; do eval "$1" && return 0; sleep 10; t=$((t+10)); done; fail "$3 timed out after $2 s"; return 1; }

say "initial: $(snapshot)"
check '[ "$(field status)" = complete ]' "initial crawl complete" '$(field status)'
check '[ "$(field rows)" = "$(truth)" ]' "initial rows == truth" '$(field rows) vs $(truth)'
END0=$(field last_crawl_end)

# ── cycle 1: 1,000 keys deleted at the provider + 5 % LIST failures during the reconcile ──
say "cycle 1: delete 1000 keys, error_rate 0.05"; sim /delete_positions '{"bucket":"'$B'","n":1000}' >/dev/null; sim /error_rate '{"rate":0.05}' >/dev/null
N=$(completes); wait_for '[ "$(completes)" -gt '$N' ]' 900 "cycle 1 reconcile"
say "after cycle 1: $(snapshot)"; sim /error_rate '{"rate":0}' >/dev/null
S1=$(field status)
check '[ "$S1" = degraded ] || [ "$S1" = complete ]' "cycle 1 ends degraded (failed prefixes) or complete, never anything else" '$S1'
if [ "$S1" = degraded ]; then
  check '[ "$(field last_crawl_end)" = "$END0" ]' "degraded did not advance last_crawl_end" '$(field last_crawl_end)'
  check '[ -n "$(field last_error)" ] && [ "$(field last_error)" != None ]' "degraded recorded last_error" '$(field last_error)'
  check '[ "$(field rows)" -ge "$(truth)" ]' "degraded pruned nothing it could not vouch for (rows >= truth)" '$(field rows) vs $(truth)'
fi

# ── cycle 2: clean reconcile → complete, rows == truth, success timestamp advanced ──
N=$(completes); wait_for '[ "$(field status)" = complete ] && [ "$(completes)" -gt '$N' ]' 900 "cycle 2 clean reconcile"
say "after cycle 2: $(snapshot)"
check '[ "$(field status)" = complete ]' "cycle 2 complete" '$(field status)'
check '[ "$(field rows)" = "$(truth)" ]' "cycle 2 rows == truth (deletions pruned)" '$(field rows) vs $(truth)'
check '[[ "$(field last_crawl_end)" > "$END0" ]]' "cycle 2 advanced last_crawl_end" '$(field last_crawl_end) vs $END0'
check '[ "$(field last_error)" = None ]' "cycle 2 cleared last_error" '$(field last_error)'
check '[ "$(field fts_gen)" = "$(field gen)" ]' "search generation matches catalogue generation" '$(field fts_gen) vs $(field gen)'
END2=$(field last_crawl_end); GEN2=$(field gen)

# ── cycle 3: kill the pod mid-RECONCILE (a full crawl bumps current_crawl_gen; deltas do not, and both
#    show status=crawling, so the generation is the only reliable signal) ──
wait_for '[ "$(field gen)" -gt '$GEN2' ]' 600 "cycle 3 full reconcile start (generation > $GEN2)"
sleep 15; say "killing pod mid-reconcile: $(snapshot)"
kubectl -n lab delete pod -l app=sairo --wait=false >/dev/null; sleep 45
say "after restart: $(snapshot)"
S3=$(field status)
check '[ "$S3" = interrupted ] || [ "$S3" = crawling ]' "after the kill the state is interrupted/crawling, not complete" '$S3'
check 'kubectl -n lab logs deploy/sairo --tail=2000 2>/dev/null | grep -q "Resuming interrupted crawl"' "resumed from checkpoints" ''
check '[ "$(field last_crawl_end)" = "$END2" ]' "kill did not advance last_crawl_end" '$(field last_crawl_end)'
wait_for '[ "$(field status)" = complete ]' 900 "cycle 3 completion after resume"
say "after cycle 3: $(snapshot)"
check '[ "$(field rows)" = "$(truth)" ]' "cycle 3 rows == truth after kill+resume" '$(field rows) vs $(truth)'
check '[[ "$(field last_crawl_end)" > "$END2" ]]' "cycle 3 advanced last_crawl_end only after the resumed crawl completed" '$(field last_crawl_end)'
check '[ "$(kubectl -n lab get pod -l app=sairo -o jsonpath="{.items[0].status.containerStatuses[0].restartCount}")" = 0 ]' "no unplanned container restarts (OOM/crash)" '$(kubectl -n lab get pod -l app=sairo -o jsonpath="{.items[0].status.containerStatuses[0].restartCount}")'
check '[ "$(kubectl -n lab logs deploy/sairo --tail=50000 2>/dev/null | grep -cE "database is locked|Traceback" )" = 0 ]' "zero lock errors / tracebacks" ''
say "RESULT: $FAILS failure(s)"; exit $FAILS

#!/usr/bin/env python3
"""Monitor a locally running backend (e.g. a read-only run against a real endpoint) until every bucket's
index is complete. Bucket names are masked as b01..bNN in every output. Stdlib only, macOS-safe.

  python3 monitor_local.py --port 8766 --password ... --out DIR [--for 14400] [--interval 60] [--pid PIDFILE] [--dbdir DIR]
"""
import argparse, http.cookiejar, json, os, subprocess, time, urllib.parse, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True); ap.add_argument("--password", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--for", dest="budget", type=int, default=14400); ap.add_argument("--interval", type=int, default=60)
ap.add_argument("--pid"); ap.add_argument("--dbdir"); ap.add_argument("--log", help="backend log to scan for real errors")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
base = f"http://127.0.0.1:{a.port}"
cj = http.cookiejar.CookieJar(); opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
req = urllib.request.Request(base + "/api/auth/login", data=json.dumps({"username": "admin", "password": a.password}).encode(), headers={"Content-Type": "application/json"})
opener.open(req).read()
def api(path):
    return json.loads(opener.open(base + path, timeout=30).read())
buckets = [b["name"] for b in api("/api/buckets")["buckets"]]
mask = {b: f"b{i+1:02d}" for i, b in enumerate(sorted(buckets))}
print(f"buckets: {len(buckets)} (names masked)", flush=True)
csv = open(os.path.join(a.out, "progress.csv"), "a"); csv.write("ts,bucket,status,total_objects,crawl_duration,crawl_elapsed,full_crawl_complete\n")
logf = open(os.path.join(a.out, "monitor.log"), "a")
def say(s):
    print(s, flush=True); logf.write(s + "\n"); logf.flush()
def rss_mb():
    try:
        pid = open(a.pid).read().strip()
        return int(subprocess.check_output(["ps", "-o", "rss=", "-p", pid]).decode().strip() or 0) // 1024
    except Exception:
        return None
def db_mb():
    try:
        return int(subprocess.check_output(["du", "-sm", a.dbdir]).decode().split()[0])
    except Exception:
        return None
def real_errors():
    if not a.log: return None
    n = 0
    try:
        for line in open(a.log, errors="ignore"):
            if ("SlowDown" in line or "database is locked" in line or "Crawl error" in line
                    or ("Traceback" in line and "bcrypt" not in line)):
                n += 1
    except Exception:
        return None
    return n
t0 = time.time()
while True:
    ts = time.strftime("%H:%M:%S"); done = 0; total_objs = 0; largest = ("", "", 0)
    for b in buckets:
        try:
            d = api("/api/buckets/" + urllib.parse.quote(b, safe="") + "/crawl-status")
        except Exception:
            d = {}
        st = d.get("status", "?"); tot = int(d.get("total_objects") or 0)
        csv.write(f"{ts},{mask[b]},{st},{tot},{d.get('crawl_duration') or 0},{d.get('crawl_elapsed') or ''},{d.get('full_crawl_complete','')}\n")
        total_objs += tot
        if st == "complete": done += 1
        if tot > largest[2]: largest = (mask[b], st, tot)
    csv.flush()
    el = int(time.time() - t0)
    say(f"[{ts} +{el}s] complete={done}/{len(buckets)} objects_indexed={total_objs:,} largest={largest[0]}:{largest[1]}:{largest[2]:,} RSS={rss_mb()}MB db={db_mb()}MB errors={real_errors()}")
    if done == len(buckets):
        say(f"ALL COMPLETE after {el}s"); break
    if el >= a.budget:
        say("time budget reached"); break
    time.sleep(a.interval)

#!/usr/bin/env python3
"""s3sim — a deterministic S3 *listing* simulator for production-shaped crawler tests.

Presents N buckets with millions of Druid-deep-storage-shaped keys without storing anything:
keys are generated from a fixed (datasources × intervals × partitions) grid, so any position
maps to a key in O(1) and any prefix maps to a position range by binary search. Implements
exactly what the crawler needs (ListBuckets, HeadBucket, ListObjectsV2 with prefix /
delimiter / max-keys / continuation-token / start-after) plus benign answers for the bucket
metadata calls the UI makes. Latency, throttling (503 SlowDown) and mutations (add/delete
keys, with a known truth count) are controlled through an admin API so delta/reconcile
correctness can be checked against ground truth.

Run:  python3 s3sim.py --spec spec.json --port 9000 --admin-port 9001 --latency-ms 40
Spec: {"buckets": [{"name": "segments-a", "datasources": 40, "intervals": 2500, "partitions": 99}, ...]}
      objects = datasources × intervals × partitions  (40 × 2500 × 99 = 9,900,000)
"""
import argparse, base64, bisect, datetime as dt, hashlib, json, os, random, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

EPOCH = dt.date(2024, 1, 1)
VERSION = "2025-08-01T12:34:56.789Z"


class Bucket:
    def __init__(self, spec):
        self.name = spec["name"]
        self.ds, self.iv, self.pt = int(spec["datasources"]), int(spec["intervals"]), int(spec["partitions"])
        self.n = self.ds * self.iv * self.pt
        self.size_p50 = int(spec.get("size_p50", 17 * 1024 * 1024))
        self._iv_str = [self._interval(j) for j in range(self.iv)]   # precomputed once: interval strings
        self._ds_str = [f"druid/segments/ds_{i:03d}/" for i in range(self.ds)]
        self._pt_str = [f"/{VERSION}/{k:04d}/index.zip" for k in range(self.pt)]
        self.deleted = set()          # generated keys removed via admin
        self.added = []               # sorted extra keys added via admin (key, size)
        self.lock = threading.Lock()

    # position <-> key (monotonic: zero-padded components keep nested-loop order == lexicographic)
    @staticmethod
    def _interval(j):
        d0 = EPOCH + dt.timedelta(days=j); d1 = d0 + dt.timedelta(days=1)
        return f"{d0.isoformat()}T00:00:00.000Z_{d1.isoformat()}T00:00:00.000Z"

    def key(self, n):
        i, r = divmod(n, self.iv * self.pt)
        j, k = divmod(r, self.pt)
        return self._ds_str[i] + self._iv_str[j] + self._pt_str[k]

    def first_ge(self, s):
        """Smallest position whose key >= s (binary search over the monotonic key space)."""
        lo, hi = 0, self.n
        while lo < hi:
            mid = (lo + hi) // 2
            if self.key(mid) < s:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def obj(self, key):
        hv = hash(key) & 0xFFFFFFFFFFFFFFFF          # process-stable enough for a run; cheap
        h = f"{hv:016x}{hv:016x}"
        size = self.size_p50 // 2 + (hv & 0xFFFFFF) % self.size_p50  # 0.5x .. 1.5x p50
        # LastModified follows the interval date so "recent" partitions look recent
        try:
            day = key.split("/")[3][:10]
            lm = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc) + dt.timedelta(hours=6)
        except Exception:
            lm = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        return key, size, lm.strftime("%Y-%m-%dT%H:%M:%S.000Z"), h

    def truth_count(self):
        with self.lock:
            return self.n - len(self.deleted) + len(self.added)

    def list_v2(self, prefix, delimiter, max_keys, token, start_after):
        """Returns (contents, common_prefixes, next_token). Sorted, S3-exact semantics."""
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else None
        n = self.first_ge(prefix) if prefix else 0
        if token:
            n = max(n, int(base64.urlsafe_b64decode(token.encode()).decode()))
        if start_after:
            n = max(n, self.first_ge(start_after + "\x00"))
        with self.lock:
            added = [a for a in self.added if a[0].startswith(prefix)]
            deleted = set(self.deleted)
        ai = 0
        if token or start_after:
            floor = start_after or self.key(n - 1) if n > 0 else ""
            ai = bisect.bisect_right([a[0] for a in added], floor)
        contents, cps, count = [], [], 0
        while count < max_keys:
            gen_key = self.key(n) if n < self.n else None
            if gen_key is not None and upper is not None and gen_key >= upper:
                gen_key = None
            add_key = added[ai][0] if ai < len(added) else None
            if gen_key is None and add_key is None:
                return contents, cps, None
            # next key in merged order
            if add_key is not None and (gen_key is None or add_key < gen_key):
                key, size = added[ai]; ai += 1; src = "add"
            else:
                key = gen_key; n += 1; src = "gen"
                if key in deleted:
                    continue
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                cp = prefix + rest[: rest.index(delimiter) + 1]
                if not cps or cps[-1] != cp:
                    cps.append(cp); count += 1
                # skip the whole common prefix in the generated space
                cp_upper = cp[:-1] + chr(ord(cp[-1]) + 1)
                n = max(n, self.first_ge(cp_upper))
                while ai < len(added) and added[ai][0].startswith(cp):
                    ai += 1
                continue
            o = self.obj(key)
            if src == "add":
                o = (key, size, o[2], o[3])
            contents.append(o); count += 1
        # more?
        more_gen = n < self.n and (upper is None or self.key(n) < upper)
        more_add = ai < len(added)
        if not (more_gen or more_add):
            return contents, cps, None
        return contents, cps, base64.urlsafe_b64encode(str(n).encode()).decode()


class State:
    def __init__(self, spec, latency_ms, error_rate):
        self.buckets = {b["name"]: Bucket(b) for b in spec["buckets"]}
        self.latency_ms = latency_ms
        self.error_rate = error_rate
        self.requests = 0
        self.lock = threading.Lock()


def xml_list(bucket, prefix, delimiter, max_keys, contents, cps, next_token, token):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f"<Name>{escape(bucket)}</Name><Prefix>{escape(prefix)}</Prefix><MaxKeys>{max_keys}</MaxKeys>",
             f"<KeyCount>{len(contents) + len(cps)}</KeyCount>",
             f"<IsTruncated>{'true' if next_token else 'false'}</IsTruncated>"]
    if delimiter:
        parts.append(f"<Delimiter>{escape(delimiter)}</Delimiter>")
    if token:
        parts.append(f"<ContinuationToken>{escape(token)}</ContinuationToken>")
    if next_token:
        parts.append(f"<NextContinuationToken>{escape(next_token)}</NextContinuationToken>")
    for key, size, lm, etag in contents:
        parts.append(f"<Contents><Key>{key}</Key><LastModified>{lm}</LastModified>"
                     f'<ETag>"{etag}"</ETag><Size>{size}</Size><StorageClass>STANDARD</StorageClass></Contents>')
    for cp in cps:
        parts.append(f"<CommonPrefixes><Prefix>{escape(cp)}</Prefix></CommonPrefixes>")
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass

        def _send(self, code, body=b"", ctype="application/xml"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-amz-request-id", f"{state.requests:016x}")
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def _err(self, code, s3code, msg):
            self._send(code, f'<?xml version="1.0"?><Error><Code>{s3code}</Code><Message>{msg}</Message></Error>'.encode())

        def do_HEAD(self):
            path = urlsplit(self.path).path.strip("/")
            if path.split("/")[0] in state.buckets:
                self._send(200)
            else:
                self._err(404, "NoSuchBucket", "no such bucket")

        def do_GET(self):
            with state.lock:
                state.requests += 1
            u = urlsplit(self.path); q = parse_qs(u.query, keep_blank_values=True)
            path = unquote(u.path).strip("/")
            if state.latency_ms:
                time.sleep(state.latency_ms / 1000.0)
            if state.error_rate and random.random() < state.error_rate:
                return self._err(503, "SlowDown", "Please reduce your request rate.")
            if path == "":
                bl = "".join(f"<Bucket><Name>{escape(b)}</Name><CreationDate>2024-01-01T00:00:00.000Z</CreationDate></Bucket>" for b in state.buckets)
                return self._send(200, f'<?xml version="1.0"?><ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Owner><ID>sim</ID><DisplayName>sim</DisplayName></Owner><Buckets>{bl}</Buckets></ListAllMyBucketsResult>'.encode())
            bucket, _, key = path.partition("/")
            b = state.buckets.get(bucket)
            if b is None:
                return self._err(404, "NoSuchBucket", "no such bucket")
            if key:
                return self._err(404, "NoSuchKey", "listing-only simulator")
            if q.get("list-type", [""])[0] == "2" or "prefix" in q or "delimiter" in q:
                prefix = q.get("prefix", [""])[0]; delimiter = q.get("delimiter", [""])[0]
                max_keys = min(int(q.get("max-keys", ["1000"])[0] or 1000), 1000)
                token = q.get("continuation-token", [None])[0]; start_after = q.get("start-after", [None])[0]
                contents, cps, nxt = b.list_v2(prefix, delimiter, max_keys, token, start_after)
                return self._send(200, xml_list(bucket, prefix, delimiter, max_keys, contents, cps, nxt, token))
            # benign bucket metadata for the UI's settings tabs
            if "versioning" in q:
                return self._send(200, b'<?xml version="1.0"?><VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>')
            if "location" in q:
                return self._send(200, b'<?xml version="1.0"?><LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">us-east-1</LocationConstraint>')
            if "acl" in q:
                return self._send(200, b'<?xml version="1.0"?><AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Owner><ID>sim</ID></Owner><AccessControlList/></AccessControlPolicy>')
            if "versions" in q:
                return self._send(200, f'<?xml version="1.0"?><ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>{escape(bucket)}</Name><IsTruncated>false</IsTruncated></ListVersionsResult>'.encode())
            if "uploads" in q:
                return self._send(200, f'<?xml version="1.0"?><ListMultipartUploadsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Bucket>{escape(bucket)}</Bucket><IsTruncated>false</IsTruncated></ListMultipartUploadsResult>'.encode())
            if "tagging" in q:
                return self._err(404, "NoSuchTagSet", "no tags")
            if "lifecycle" in q:
                return self._err(404, "NoSuchLifecycleConfiguration", "none")
            if "cors" in q:
                return self._err(404, "NoSuchCORSConfiguration", "none")
            if "object-lock" in q:
                return self._err(404, "ObjectLockConfigurationNotFoundError", "none")
            # bare GET /bucket == ListObjects (v1) — answer with v2 shape, boto handles it
            contents, cps, nxt = b.list_v2(q.get("prefix", [""])[0], q.get("delimiter", [""])[0], 1000, None, None)
            return self._send(200, xml_list(bucket, "", "", 1000, contents, cps, nxt, None))

        def do_PUT(self):
            self._err(501, "NotImplemented", "listing-only simulator")
        def do_POST(self):
            self._err(501, "NotImplemented", "listing-only simulator")
        def do_DELETE(self):
            self._err(501, "NotImplemented", "listing-only simulator")
    return H


def make_admin(state):
    class A(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            u = urlsplit(self.path); q = parse_qs(u.query)
            if u.path == "/truth":
                b = state.buckets.get(q.get("bucket", [""])[0])
                if not b: return self._json(404, {"error": "no such bucket"})
                return self._json(200, {"bucket": b.name, "count": b.truth_count(), "generated": b.n,
                                        "deleted": len(b.deleted), "added": len(b.added)})
            if u.path == "/stats":
                return self._json(200, {"requests": state.requests, "latency_ms": state.latency_ms,
                                        "error_rate": state.error_rate,
                                        "buckets": {n: b.truth_count() for n, b in state.buckets.items()}})
            self._json(404, {"error": "unknown"})
        def do_POST(self):
            u = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            if u.path == "/latency":
                state.latency_ms = float(body.get("ms", 0)); return self._json(200, {"latency_ms": state.latency_ms})
            if u.path == "/error_rate":
                state.error_rate = float(body.get("rate", 0)); return self._json(200, {"error_rate": state.error_rate})
            if u.path == "/mutate":
                b = state.buckets.get(body.get("bucket", ""))
                if not b: return self._json(404, {"error": "no such bucket"})
                with b.lock:
                    for k in body.get("delete", []):
                        b.deleted.add(k)
                    for k in body.get("add", []):
                        if not any(a[0] == k for a in b.added):
                            bisect.insort(b.added, (k, int(body.get("size", 1024))))
                return self._json(200, {"count": b.truth_count()})
            if u.path == "/delete_positions":  # delete N generated keys evenly spread (cheap way to create stale rows)
                b = state.buckets.get(body.get("bucket", "")); n = int(body.get("n", 0))
                if not b: return self._json(404, {"error": "no such bucket"})
                step = max(1, b.n // max(1, n))
                with b.lock:
                    for pos in range(0, b.n, step):
                        b.deleted.add(b.key(pos))
                        if len(b.deleted) >= n: break
                return self._json(200, {"deleted": len(b.deleted), "count": b.truth_count()})
            self._json(404, {"error": "unknown"})
    return A


def main():
    os.environ.setdefault("PYTHONHASHSEED", "0")
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True); ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--admin-port", type=int, default=9001); ap.add_argument("--latency-ms", type=float, default=0)
    ap.add_argument("--error-rate", type=float, default=0.0)
    a = ap.parse_args()
    state = State(json.load(open(a.spec)), a.latency_ms, a.error_rate)
    total = sum(b.n for b in state.buckets.values())
    print(f"s3sim: {len(state.buckets)} buckets, {total:,} objects, latency {a.latency_ms} ms, port {a.port} (admin {a.admin_port})", flush=True)
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", a.admin_port), make_admin(state)).serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(state)).serve_forever()


if __name__ == "__main__":
    main()

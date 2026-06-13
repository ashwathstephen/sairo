#!/usr/bin/env python3
"""
Memory benchmark for issue #6: peak RSS of the proxy upload mechanism.
  --mode before : read the whole file into a bytes object (old: `await f.read()`),
                  then upload from memory  -> RSS scales with file size (OOM at scale)
  --mode after  : stream from the file via upload_fileobj (new)  -> RSS bounded
Each mode runs in its own process; we report peak RSS via getrusage.
Targets local MinIO at :9400, bucket 'uptest'.
"""
import argparse, io, os, platform, resource, sys, tempfile, time
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config

MULTIPART_THRESHOLD = 100 * 1024 * 1024

def peak_rss_mb():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Darwin reports bytes; Linux reports kilobytes
    return rss / (1024 * 1024) if platform.system() == "Darwin" else rss / 1024

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["before", "after"], required=True)
    ap.add_argument("--size-mb", type=int, default=500)
    a = ap.parse_args()

    s3 = boto3.client("s3", endpoint_url="http://localhost:9400",
                      aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin",
                      config=Config(signature_version="s3v4"))
    size = a.size_mb * 1024 * 1024
    tmp = tempfile.NamedTemporaryFile(delete=False)
    # write the file in chunks so creating it doesn't itself spike RSS
    chunk = os.urandom(8 * 1024 * 1024)
    written = 0
    while written < size:
        tmp.write(chunk[: min(len(chunk), size - written)]); written += len(chunk)
    tmp.flush(); tmp.close()
    key = f"membench/{a.mode}-{a.size_mb}mb.bin"

    base = peak_rss_mb()
    t0 = time.monotonic()
    if a.mode == "before":
        # OLD behavior: whole file read into a bytes object, then uploaded from RAM
        with open(tmp.name, "rb") as f:
            body = f.read()                       # <-- the OOM source: full file in memory
        s3.put_object(Bucket="uptest", Key=key, Body=body)
        del body
    else:
        # NEW behavior: stream straight from the (disk-backed) file handle, with the
        # SAME bounded config the backend uses (8MB chunks, max_concurrency=4).
        cfg = TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024,
                             max_concurrency=4, use_threads=True)
        with open(tmp.name, "rb") as f:
            s3.upload_fileobj(f, "uptest", key, Config=cfg)
    elapsed = time.monotonic() - t0
    peak = peak_rss_mb()
    os.unlink(tmp.name)
    print(f"  mode={a.mode:6}  file={a.size_mb}MB  baseline_rss={base:.0f}MB  peak_rss={peak:.0f}MB  "
          f"delta={peak-base:.0f}MB  time={elapsed:.1f}s")

if __name__ == "__main__":
    main()

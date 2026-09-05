# Populate bucket 'rca' with 30 top-level prefixes x 4000 objects = 120,000 keys (via DIRECT minio port, fast).
import boto3, concurrent.futures, time, sys
from botocore.client import Config
s3 = boto3.client("s3", endpoint_url="http://localhost:9000", aws_access_key_id="minioadmin",
                  aws_secret_access_key="minioadmin", config=Config(signature_version="s3v4", max_pool_connections=64,
                  retries={"max_attempts": 5}))
try: s3.create_bucket(Bucket="rca")
except Exception: pass
PREFIXES, PER = 30, 4000
keys = [f"ds={p:02d}/part-{i:05d}.parquet" for p in range(PREFIXES) for i in range(PER)]
t0 = time.time(); done = 0
def put(k): s3.put_object(Bucket="rca", Key=k, Body=b"x"); return 1
with concurrent.futures.ThreadPoolExecutor(max_workers=48) as ex:
    for _ in ex.map(put, keys):
        done += 1
        if done % 10000 == 0: print(f"{done:,}/{len(keys):,} in {time.time()-t0:.0f}s", flush=True)
print(f"DONE {done:,} objects in {time.time()-t0:.0f}s", flush=True)

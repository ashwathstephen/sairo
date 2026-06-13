---
title: I manage 170TB of object storage. Here's the tool I had to build.
published: false
tags: opensource, devops, s3, selfhosted
cover_image: 
---

I run about 170TB of data across 10 buckets on S3-compatible storage. Data pipelines dump Druid segments, Kafka flat events, Prometheus TSDB blocks — millions of objects, growing daily.

Nobody on the team could answer basic questions: How much is this costing us? What's the oldest data in that bucket? Which folder grew 40TB last month?

AWS Console times out on large buckets. Cyberduck crashes. MinIO Console doesn't have search. s3cmd gives you a wall of text you can't do anything with.

So I built what I needed.

## The first attempt broke

Started simple — a Flask app that called `ListObjectsV2`, cached the results, and rendered a file browser.

Worked great on a test bucket with 500 objects.

Hit production: 8.2 million objects in one bucket. The process got OOM-killed at 4GB. You can't hold 10 million S3 keys in memory and also serve a web UI.

## What actually works at scale

The architecture that survived production:

**SQLite per bucket.** Not Postgres, not Redis. One `.db` file per bucket, WAL mode for concurrent reads and writes, zero external dependencies. SQLite with the right PRAGMAs (`cache_size = -64000`, `mmap_size = 256MB`, `temp_store = MEMORY`) handles millions of rows without breaking a sweat.

**Streaming crawl.** `ListObjectsV2` with 10K batch size, 16 parallel prefix workers, incremental upsert that only writes changed objects. A 10M-object bucket indexes in about 12 minutes.

**FTS5 for search.** SQLite's full-text search extension. Type a filename or pattern, get instant results across millions of keys. The index rebuilds in the background so search keeps working during a crawl.

**Single container.** FastAPI backend, React frontend, SQLite storage. No Docker Compose. No database server. One `docker run` and you're in.

## The hard parts

### Indexing 10M objects without dying

The naive approach — load all keys, insert one by one — dies around 2 million objects. The fix: 10K batch inserts with `INSERT OR REPLACE`, crawl generation tracking to detect deleted objects, and 2K-chunk updates for unchanged keys. After the crawl, any object with a stale generation gets purged.

### "Database is locked" at 3am

16 crawl threads writing to SQLite while API threads read from it. Classic contention.

`journal_mode = WAL` lets readers and a single writer coexist. But that's not enough when you have 16 threads competing for the write lock. Adding `busy_timeout = 5000` makes the writer wait 5 seconds instead of failing immediately. The "database is locked" error went from appearing every crawl cycle to zero.

### Flat-key buckets are a nightmare

One of our Druid buckets stores segments like `druid/datasource/2024-01-01/.../segment_id`. That's 8.2 million objects under essentially one prefix.

When you call `ListObjectsV2` with `Delimiter=/`, S3 returns one "folder" with everything in it. You're back to scanning millions of objects sequentially.

The fix: sub-prefix splitting. If a prefix has more than 500K objects and 3 or fewer children, recursively expand it by listing sub-prefixes with `Delimiter=/`. That turns one sequential 8.2M scan into 200 parallel 40K scans.

### Making the storage chart not lie

I take a storage snapshot after every crawl and aggregate to daily for the trend chart. The original query used `MAX(object_count)` per day — simple, fast.

But if you delete objects mid-day and crawl again, `MAX()` picks the peak value, not the current state. The chart shows growth when storage actually shrank.

Fixed it with a self-join that picks the row with the latest timestamp per day instead of the highest value. Small change, but data accuracy matters when you're making cost decisions.

### Cost estimation across 13 providers

Every S3-compatible provider has different pricing. AWS has 6+ storage classes with region multipliers. Cloudflare R2 has no egress fees. Wasabi has minimum storage duration charges.

Built a pricing engine with static data for 13 providers (AWS, Cloudflare R2, Backblaze B2, Wasabi, MinIO, Ceph, DigitalOcean, Hetzner, Scaleway, OVH, IDrive, Storj, Leaseweb) plus live AWS pricing from their bulk API with 24h caching.

The result: per-folder cost breakdown. "Your `logs/` folder costs $847/month, and 60% of it is data older than 90 days." Then it calculates how much you'd save by moving cold data to a cheaper storage class.

## What it looks like today

**Object browser** — streaming load that shows results as they arrive, not after a 30-second wait. File preview for images, PDFs, CSV, JSON, and Parquet/ORC schemas.

**Full-text search** — type a query, get results instantly across all indexed objects.

**Insights dashboard** — storage breakdown by folder with size bars, growth trends over time, estimated monthly and annual costs with provider-specific pricing.

**Optimization engine** — detects cold data (files untouched for 30+ days), recommends storage class tiering with savings estimates, flags versioned buckets with no cleanup rules, finds stale multipart uploads wasting space.

**Multi-endpoint** — connect to AWS, MinIO, Ceph, R2, and any S3-compatible backend from one UI. Switch between endpoints without re-deploying.

**Access control** — role-based access with bucket-level permissions, API tokens, shareable file links, two-factor authentication, LDAP and OAuth support.

**MCP server** — plug into AI assistants for conversational storage analysis. Ask questions about your buckets, costs, and optimization opportunities.

## Try it

```bash
docker run -d -p 8888:8888 \
  -e S3_ENDPOINT=https://s3.amazonaws.com \
  -e S3_ACCESS_KEY=YOUR_KEY \
  -e S3_SECRET_KEY=YOUR_SECRET \
  -v sairo-data:/data \
  ghcr.io/ashwathstephen/sairo:latest
```

Open `localhost:8888`. Check Docker logs for the generated admin password.

Works on ARM and x86. Tested at 170TB with 10M+ objects.

**GitHub:** https://github.com/ashwathstephen/sairo
**Docs:** https://sairo.dev

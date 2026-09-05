# Production-shaped lab (kind + S3 listing simulator)

Reproduces the production shape without production data: **19 buckets, 15,500,000 objects, one bucket of
9,900,000**, Druid-deep-storage key layout (`druid/segments/<ds>/<interval>/<version>/<partition>/index.zip`),
configurable provider latency and throttling, and injectable adds/deletes with a known truth count. The
backend runs from the real Helm chart on a real PVC with `Recreate`, so rollouts behave like production.

    kind create cluster --name sairo-lab
    docker build -t sairo:phase-a . && kind load docker-image sairo:phase-a --name sairo-lab
    kubectl create ns lab
    kubectl -n lab create configmap s3sim --from-file=s3sim.py --from-file=spec.json=spec-prod.json
    kubectl -n lab apply -f s3sim.yaml            # Deployment + Service (see below)
    helm upgrade --install sairo charts/sairo -n lab \
      --set image.repository=sairo --set image.tag=phase-a --set image.pullPolicy=Never \
      --set s3.endpoint=http://s3sim:9000 --set s3.pathStyle=true --set s3.accessKey=lab --set s3.secretKey=lab \
      --set telemetry.enabled=false --set auth.adminPass=labpass123 --set auth.jwtSecret=<fixed> --set auth.secureCookie=false
    ./monitor.sh --for 5400            # progress CSV + log under out/

Simulator admin API (port 9001 inside the cluster; `kubectl -n lab port-forward svc/s3sim 9001:9001`):
`GET /truth?bucket=`, `GET /stats`, `POST /latency {"ms":..}`, `POST /error_rate {"rate":..}`,
`POST /mutate {"bucket":..,"add":[..],"delete":[..]}`, `POST /delete_positions {"bucket":..,"n":..}`.

Why a simulator: 15.5M real objects on MinIO would take hours to create and tens of GB; the simulator
generates keys from a fixed grid so any position is O(1) and any prefix is a binary search. It answers
exactly the calls the crawler makes (ListBuckets, HeadBucket, ListObjectsV2 with prefix / delimiter /
max-keys / continuation-token / start-after) and benign metadata for the UI. What it cannot do: byte
operations, real provider latency profiles, real throttling policy. Use the read-only production run for those.

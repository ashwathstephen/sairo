#!/bin/bash
# One-command lab run: fresh volume + image tag + bucket spec, then monitor to completion.
#   ./run_variant.sh <image-tag> <spec.json> [--for SECONDS]
# Assumes: kind cluster 'sairo-lab', namespace 'lab', s3sim + sairo already installed (see README), image loaded into kind.
set -euo pipefail
TAG=$1; SPEC=$2; shift 2
HERE=$(cd "$(dirname "$0")" && pwd)
kubectl config use-context kind-sairo-lab >/dev/null
kubectl -n lab scale deploy/sairo --replicas=0 >/dev/null
kubectl -n lab wait --for=delete pod -l app=sairo --timeout=120s >/dev/null 2>&1 || true
kubectl -n lab delete pvc sairo-data --wait=true >/dev/null 2>&1 || true
kubectl -n lab create configmap s3sim --from-file=s3sim.py="$HERE/s3sim.py" --from-file=spec.json="$SPEC" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n lab rollout restart deploy/s3sim >/dev/null
kubectl -n lab rollout status deploy/s3sim --timeout=180s >/dev/null
helm upgrade sairo "$HERE/../../charts/sairo" -n lab --reuse-values --set image.tag="$TAG" --set replicaCount=1 --wait --timeout 240s >/dev/null
[ -d "$HERE/out" ] && mv "$HERE/out" "$HERE/out_$(date +%Y%m%d-%H%M%S)"
mkdir -p "$HERE/out"; echo "tag=$TAG spec=$(basename "$SPEC") started=$(date -u +%FT%TZ)" > "$HERE/out/RUN"
kubectl -n lab logs deploy/s3sim --tail=1
exec "$HERE/monitor.sh" "$@"

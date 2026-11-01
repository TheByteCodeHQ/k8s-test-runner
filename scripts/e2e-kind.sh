#!/usr/bin/env bash
# End-to-end check on a throwaway local cluster: build the images, deploy, run the sample suite.
# Needs docker, kind and kubectl. It only ever touches the cluster it creates (kind-ktr-e2e).
set -euo pipefail

CLUSTER=ktr-e2e
CTX="kind-${CLUSTER}"
K="kubectl --context ${CTX}"
TOKEN="e2e-$(date +%s)"

cleanup() { kill "${PF_PID:-0}" 2>/dev/null || true; kind delete cluster --name "${CLUSTER}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

kind create cluster --name "${CLUSTER}" --wait 120s
docker build -t ktr-server:dev .
docker build -t ktr-sample-suite:dev examples/sample-suite
kind load docker-image ktr-server:dev ktr-sample-suite:dev --name "${CLUSTER}"

${K} apply -f deploy/namespace.yaml
${K} -n test-runner create secret generic ktr-api-token --from-literal=token="${TOKEN}"
${K} apply -k deploy
${K} -n test-runner rollout status deploy/ktr-server --timeout=180s

${K} -n test-runner port-forward svc/ktr-server 18080:80 >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do curl -fs http://127.0.0.1:18080/healthz >/dev/null && break; sleep 1; done

AUTH="Authorization: Bearer ${TOKEN}"
RUN_ID=$(curl -fs -X POST -H "${AUTH}" -H 'Content-Type: application/json' \
  -d '{"suite":"sample"}' http://127.0.0.1:18080/api/runs | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "started ${RUN_ID}"

for _ in $(seq 1 90); do
  STATUS=$(curl -fs -H "${AUTH}" "http://127.0.0.1:18080/api/runs/${RUN_ID}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  case "${STATUS}" in pending|running) sleep 2 ;; *) break ;; esac
done
echo "final status: ${STATUS}"

curl -fs -H "${AUTH}" "http://127.0.0.1:18080/api/runs/${RUN_ID}/results" | python3 -c '
import sys, json
r = json.load(sys.stdin)
print(r["total"], "tests:", r["passed"], "passed,", r["failed"], "failed")
assert (r["total"], r["passed"], r["failed"]) == (6, 4, 2), "unexpected results"
'
echo "e2e OK"

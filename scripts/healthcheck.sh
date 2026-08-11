#!/usr/bin/env bash
# Copyright 2026 JO3NZV and Claude Code
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# End-to-end smoke test for this node: exercises every app kind (container/
# js/site/proxy/cron) through the console's own real deploy path using
# disposable "zzzhealthcheck-*" resources, confirms already-registered real
# apps are still healthy, and re-checks a handful of specific regressions
# this project has actually hit before (CPU overcommit, the Recreate
# rollout strategy, extra-ports host-port collision, the Traefik access
# log path). Meant to be run right after upgrade.sh, to catch "the upgrade
# silently broke something" before a real app hits it -- but safe to run
# any time: read-only against real apps, and always cleans up its own test
# resources even if a check fails partway through.
#
# Every functional test goes through the console's own HTTP API
# (X-SpinDeck-Peer-Token, the same mechanism peer nodes use for
# replication) and real Traefik routing with a synthetic Host header --
# never a forged session cookie -- so this script has no dependency on
# console internals beyond its already-documented peer API, and is safe to
# keep running long-term as the app evolves.
#
# Usage: sudo ./scripts/healthcheck.sh (from the repo root)
# Exit code: 0 if every check passed, 1 if anything failed (see the
# summary printed at the end either way).
set -uo pipefail  # deliberately NOT -e: every check should run and be
                   # reported, not abort the whole script on the first miss

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo ./scripts/healthcheck.sh" >&2
  exit 1
fi

# This script now lives in scripts/, one level below the repo root --
# doesn't strictly need to be there (this script has no repo-relative
# path references of its own), but matches every sibling script's
# convention in case that ever changes.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

ENV_FILE=/etc/spinapp-ui/env
if [ ! -f "$ENV_FILE" ]; then
  echo "SpinDeck doesn't look installed on this host ($ENV_FILE missing)." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
# Deliberately re-exported *after* sourcing the env file, not before: the
# env file itself sets KUBECONFIG=/etc/spinapp-ui/kubeconfig.yaml (the
# console's own least-privilege kubeconfig -- see spinapp-ui-rbac.yaml),
# which would otherwise silently win and leave every check below running
# against a ServiceAccount that can't even list Nodes or read kube-system/
# cert-manager/spin-operator -- confirmed live: those checks came back
# "no permission" and, worse, that failure was invisible (see the pod-
# health check's own explicit exit-code check below for why that matters).
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
NAMESPACE="${SPINAPP_UI_NAMESPACE:-default}"
CONSOLE_PORT="${PORT:-8080}"
PEER_TOKEN="${SPINAPP_UI_PEER_TOKEN:-}"
EXTERNAL_HOST="${SPINAPP_UI_EXTERNAL_HOST:-}"
HOST_IP="${SPINAPP_UI_HOST_IP:-}"
CONSOLE_BASE="http://127.0.0.1:${CONSOLE_PORT}"

if [ -z "$PEER_TOKEN" ]; then
  echo "SPINAPP_UI_PEER_TOKEN is not set in $ENV_FILE -- functional tests need it" \
    "(the console's own /internal/* API, same as peer replication uses)." >&2
  exit 1
fi

TRAEFIK_SCHEME="$(python3 -c '
import json
try:
    print(json.load(open("/opt/spinapp-ui/config.json")).get("traefik_scheme", "http"))
except Exception:
    print("http")
' 2>/dev/null)"
if [ "$TRAEFIK_SCHEME" = "https" ]; then
  TRAEFIK_PORT=443
  CURL_SCHEME_ARGS=(-k)
  TRAEFIK_URL_SCHEME="https"
else
  TRAEFIK_PORT=80
  CURL_SCHEME_ARGS=()
  TRAEFIK_URL_SCHEME="http"
fi

RUN_ID="$(date +%s)"
PASS=0
FAIL=0
FAILURES=()
CLEANUP_CONTAINERS=()
CLEANUP_JS=()
CLEANUP_SITES=()
CLEANUP_PROXIES=()
CLEANUP_CRONS=()
CLEANUP_JOBS=()

log()     { echo -e "\n==> $*"; }
ok()      { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
bad()     { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); FAILURES+=("$*"); }
warn()    { echo "  [WARN] $*"; }

peer_post() {
  # peer_post <path> <json-file-or-'-'>
  local path="$1" data="$2"
  curl -s -X POST -H "X-SpinDeck-Peer-Token: $PEER_TOKEN" -H "Content-Type: application/json" \
    --data "@${data}" "${CONSOLE_BASE}${path}"
}

peer_delete() {
  curl -s -X POST -H "X-SpinDeck-Peer-Token: $PEER_TOKEN" "${CONSOLE_BASE}$1"
}

curl_via_traefik() {
  # curl_via_traefik <host-header> <path> -- prints body, returns curl's exit code
  local host="$1" path="$2"
  curl -s "${CURL_SCHEME_ARGS[@]}" --max-time 10 -H "Host: ${host}" \
    "${TRAEFIK_URL_SCHEME}://127.0.0.1:${TRAEFIK_PORT}${path}"
}

curl_via_traefik_until() {
  # curl_via_traefik_until <host-header> <path> <expected-substring> --
  # retries for a few seconds before giving up. There's an inherent, real
  # gap between an IngressRoute/Service existing in the k8s API (which the
  # kubectl wait's above already confirmed) and Traefik's own Kubernetes
  # CRD provider actually noticing and applying it -- confirmed live this
  # is usually well under a second but occasionally a few seconds,
  # especially right after several IngressRoutes changed in quick
  # succession (as this script's own back-to-back app creations do). A
  # single unretried curl right after kubectl wait returns is a real
  # source of flakiness here, not a hypothetical one. Prints the last
  # response body; returns 0 if the expected substring ever showed up.
  local host="$1" path="$2" expect="$3" body=""
  for _ in 1 2 3 4 5 6 7 8; do
    body="$(curl_via_traefik "$host" "$path")"
    if echo "$body" | grep -q "$expect"; then
      echo "$body"
      return 0
    fi
    sleep 1
  done
  echo "$body"
  return 1
}

SCRATCH="$(mktemp -d)"

cleanup() {
  log "Cleaning up test resources"
  for j in "${CLEANUP_JOBS[@]:-}"; do
    [ -z "$j" ] && continue
    kubectl delete job "$j" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1
  done
  for n in "${CLEANUP_CONTAINERS[@]:-}"; do
    [ -z "$n" ] && continue
    peer_delete "/internal/containers/$n/delete" >/dev/null 2>&1
  done
  for n in "${CLEANUP_JS[@]:-}" "${CLEANUP_SITES[@]:-}" "${CLEANUP_PROXIES[@]:-}"; do
    [ -z "$n" ] && continue
    peer_delete "/internal/apps/$n/delete" >/dev/null 2>&1
  done
  for n in "${CLEANUP_CRONS[@]:-}"; do
    [ -z "$n" ] && continue
    peer_delete "/internal/crons/$n/delete" >/dev/null 2>&1
  done
  rm -rf "$SCRATCH"
}
# A second `trap ... EXIT` call doesn't stack in bash -- it silently
# replaces the first, so both cleanup steps have to live in one trap
# (confirmed this the hard way: an earlier version registered SCRATCH's
# rm -rf as its own separate trap, which meant the *test-resource*
# cleanup above would never have run at all if the script had ever
# exited early -- e.g. Ctrl-C mid-run -- leaving zzzhealthcheck-* apps
# stranded on the node).
trap cleanup EXIT

# ---------------------------------------------------------------------------
log "[1/7] Infrastructure layer"
# ---------------------------------------------------------------------------
if kubectl wait --for=condition=Ready node --all --timeout=10s >/dev/null 2>&1; then
  ok "node Ready"
else
  bad "node not Ready (kubectl get nodes)"
fi

for ns in kube-system cert-manager spin-operator; do
  # kubectl's own exit code is checked explicitly here, not inferred from
  # empty output -- an RBAC-denied or otherwise-failed `get pods` also
  # produces no matching stdout lines, which would otherwise silently read
  # as "no unhealthy pods found" (confirmed live: this is exactly what
  # happened while KUBECONFIG was still pointed at the console's own
  # restricted kubeconfig, see the comment above KUBECONFIG's export).
  PODS_RAW="$(kubectl get pods -n "$ns" --no-headers 2>&1)"
  if [ $? -ne 0 ]; then
    bad "could not list pods in $ns: $(echo "$PODS_RAW" | head -1)"
    continue
  fi
  NOT_READY="$(echo "$PODS_RAW" | grep -vE ' Running | Completed ' || true)"
  if [ -z "$NOT_READY" ]; then
    ok "all pods healthy in $ns"
  else
    bad "pod(s) not Running in $ns: $(echo "$NOT_READY" | awk '{print $1}' | paste -sd, -)"
  fi
done

REGISTRY_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:5000/v2/ 2>/dev/null)"
if [ "$REGISTRY_CODE" = "200" ]; then
  ok "local OCI registry responding (localhost:5000)"
else
  bad "local OCI registry not responding as expected (got HTTP $REGISTRY_CODE)"
fi

# ---------------------------------------------------------------------------
log "[2/7] Console process"
# ---------------------------------------------------------------------------
if systemctl is-active --quiet spinapp-ui; then
  ok "spinapp-ui.service is active"
else
  bad "spinapp-ui.service is NOT active"
fi

LOGIN_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${CONSOLE_BASE}/login")"
# 200 (Basic Auth login form) or 302 (OIDC mode redirects straight to
# Keycloak, see login()) both mean the console itself handled the request
# -- confirmed live this node runs OIDC mode, where 200 never happens.
if [ "$LOGIN_CODE" = "200" ] || [ "$LOGIN_CODE" = "302" ]; then
  ok "console HTTP responding (/login -> $LOGIN_CODE)"
else
  bad "console HTTP not responding as expected (/login -> $LOGIN_CODE)"
fi

# ---------------------------------------------------------------------------
log "[3/7] Existing registered apps (read-only)"
# ---------------------------------------------------------------------------
EXISTING_BAD="$(python3 -c '
import json, subprocess

def items(kind):
    r = subprocess.run(
        ["kubectl", "get", kind, "-n", "'"$NAMESPACE"'", "-l", "spinapp-ui.dev/app", "-o", "json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    return json.loads(r.stdout).get("items", [])

bad = []
for d in items("deployment"):
    name = d["metadata"]["name"]
    spec_replicas = d["spec"].get("replicas", 1)
    ready = d.get("status", {}).get("readyReplicas", 0)
    if spec_replicas > 0 and ready < spec_replicas:
        bad.append(f"deployment/{name} ({ready}/{spec_replicas} ready)")
for c in items("cronjob"):
    pass  # CronJobs have no meaningful "ready" state to check here
for s in items("spinapp"):
    name = s["metadata"]["name"]
    r = subprocess.run(
        ["kubectl", "get", "pod", "-n", "'"$NAMESPACE"'", "-l", f"core.spinkube.dev/app-name={name}",
         "-o", "jsonpath={.items[*].status.phase}"],
        capture_output=True, text=True,
    )
    phases = (r.stdout or "").split()
    if not phases or any(p not in ("Running",) for p in phases):
        bad.append(f"spinapp/{name} (pod phase(s): {phases or [\"none\"]})")
print("\n".join(bad))
' 2>/dev/null)"

if [ -z "$EXISTING_BAD" ]; then
  ok "all pre-existing registered apps are healthy"
else
  while IFS= read -r line; do
    [ -n "$line" ] && warn "pre-existing app not healthy: $line (not this script's own doing -- check separately)"
  done <<< "$EXISTING_BAD"
fi

# ---------------------------------------------------------------------------
log "[4/7] Container app: create, CPU/rollout regressions, extra-ports, Traefik routing, delete"
# ---------------------------------------------------------------------------
CNAME="zzzhealthcheck-container-${RUN_ID}"
CFQDN="${CNAME}.invalid"
cat > "$SCRATCH/container.json" <<EOF
{
  "name": "${CNAME}",
  "image": "nginx:alpine",
  "port": 80,
  "cpu_limit": "200m",
  "mem_limit": "64Mi",
  "replicas": 1,
  "fqdn": "${CFQDN}",
  "tls_mode": "terminate",
  "extra_ports": [{"port": 22, "target_port": 80, "protocol": "tcp"}]
}
EOF
RESP="$(peer_post /internal/containers "$SCRATCH/container.json")"
# Reserved-port regression check (see the 2026-08-11 security audit):
# extra_ports targeting SSH's port must be rejected, and *nothing* created.
if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if not d.get("ok") else 1)' 2>/dev/null; then
  ok "extra-ports reserved-port check still rejects port 22 (regression check)"
else
  bad "extra-ports reserved-port check did NOT reject port 22 -- security regression! response: $RESP"
  CLEANUP_CONTAINERS+=("$CNAME")
fi

# Retry without the offending extra port, for the rest of this section.
python3 -c "
import json
d = json.load(open('$SCRATCH/container.json'))
del d['extra_ports']
json.dump(d, open('$SCRATCH/container.json', 'w'))
"
RESP="$(peer_post /internal/containers "$SCRATCH/container.json")"
CLEANUP_CONTAINERS+=("$CNAME")
if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' 2>/dev/null; then
  ok "container app created ($CNAME)"
else
  bad "container app creation failed: $RESP"
fi

if kubectl wait --for=condition=Available "deployment/$CNAME" -n "$NAMESPACE" --timeout=60s >/dev/null 2>&1; then
  ok "container app's Deployment reached Available"

  STRATEGY="$(kubectl get deployment "$CNAME" -n "$NAMESPACE" -o jsonpath='{.spec.strategy.type}')"
  if [ "$STRATEGY" = "Recreate" ]; then
    ok "Deployment strategy is Recreate (regression check)"
  else
    bad "Deployment strategy is '$STRATEGY', expected Recreate -- regression!"
  fi

  CPU_REQ="$(kubectl get deployment "$CNAME" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}')"
  CPU_LIM="$(kubectl get deployment "$CNAME" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}')"
  if [ -n "$CPU_REQ" ] && [ "$CPU_REQ" != "$CPU_LIM" ]; then
    ok "CPU overcommit still in effect (request=$CPU_REQ, limit=$CPU_LIM, regression check)"
  else
    bad "CPU request/limit no longer differ (request=$CPU_REQ, limit=$CPU_LIM) -- overcommit regression!"
  fi

  if BODY="$(curl_via_traefik_until "$CFQDN" / "Welcome to nginx")"; then
    ok "Traefik routing works end-to-end (container app reachable via FQDN)"
  else
    bad "container app not reachable via Traefik (Host: $CFQDN) -- got: $(echo "$BODY" | head -c 200)"
  fi
else
  bad "container app's Deployment never reached Available"
fi

FREE_PORT=39123
cat > "$SCRATCH/container_freeport.json" <<EOF
{"name": "${CNAME}", "image": "nginx:alpine", "port": 80, "cpu_limit": "200m", "mem_limit": "64Mi",
 "replicas": 1, "fqdn": "${CFQDN}", "tls_mode": "terminate",
 "extra_ports": [{"port": ${FREE_PORT}, "target_port": 80, "protocol": "tcp"}]}
EOF
RESP="$(peer_post /internal/containers "$SCRATCH/container_freeport.json")"
if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' 2>/dev/null; then
  ok "extra-ports still works for a genuinely free port"
else
  bad "extra-ports rejected a free port unexpectedly: $RESP"
fi
kubectl delete service "${CNAME}-raw" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1

peer_delete "/internal/containers/$CNAME/delete" >/dev/null 2>&1
sleep 2
if ! kubectl get deployment "$CNAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  ok "container app deleted cleanly"
  CLEANUP_CONTAINERS=()
else
  bad "container app's Deployment still exists after delete"
fi

# ---------------------------------------------------------------------------
log "[5/7] JS app: create, Wasm Pod health, Traefik routing, delete"
# ---------------------------------------------------------------------------
JNAME="zzzhealthcheck-js-${RUN_ID}"
JMARKER="healthcheck-js-ok-${RUN_ID}"
cat > "$SCRATCH/js.json" <<EOF
{
  "name": "${JNAME}",
  "code": "addEventListener('fetch', (event) => { event.respondWith(new Response('${JMARKER}')); });"
}
EOF
RESP="$(peer_post /internal/apps/js "$SCRATCH/js.json")"
CLEANUP_JS+=("$JNAME")
if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' 2>/dev/null; then
  ok "JS app created and built ($JNAME)"
else
  bad "JS app creation/build failed: $RESP"
fi

if kubectl wait --for=condition=Ready pod -l "core.spinkube.dev/app-name=$JNAME" -n "$NAMESPACE" --timeout=60s >/dev/null 2>&1; then
  ok "JS app's Pod reached Ready (Wasm shim healthy)"
  if [ -n "$EXTERNAL_HOST" ]; then
    if BODY="$(curl_via_traefik_until "$EXTERNAL_HOST" "/${JNAME}/" "$JMARKER")"; then
      ok "Traefik routing works end-to-end (JS app reachable via /${JNAME}/)"
    else
      bad "JS app not reachable via Traefik -- got: $(echo "$BODY" | head -c 200)"
    fi
  else
    warn "SPINAPP_UI_EXTERNAL_HOST not set, skipping Traefik routing check for JS app"
  fi
else
  bad "JS app's Pod never reached Ready -- possible stuck containerd-shim-spin-v2 (see spin-shim-recovery.sh)"
fi

peer_delete "/internal/apps/$JNAME/delete" >/dev/null 2>&1
sleep 2
if ! kubectl get spinapp "$JNAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  ok "JS app deleted cleanly"
  CLEANUP_JS=()
else
  bad "JS app's SpinApp object still exists after delete"
fi

# ---------------------------------------------------------------------------
log "[6/7] Site app: create, Wasm Pod health, Traefik routing, delete"
# ---------------------------------------------------------------------------
SNAME="zzzhealthcheck-site-${RUN_ID}"
SMARKER="healthcheck-site-ok-${RUN_ID}"
cat > "$SCRATCH/site.json" <<EOF
{
  "name": "${SNAME}",
  "files": {"index.html": "${SMARKER}"}
}
EOF
RESP="$(peer_post /internal/sites "$SCRATCH/site.json")"
CLEANUP_SITES+=("$SNAME")
if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' 2>/dev/null; then
  ok "site app created and built ($SNAME)"
else
  bad "site app creation/build failed: $RESP"
fi

if kubectl wait --for=condition=Ready pod -l "core.spinkube.dev/app-name=$SNAME" -n "$NAMESPACE" --timeout=60s >/dev/null 2>&1; then
  ok "site app's Pod reached Ready (Wasm shim healthy)"
  if [ -n "$EXTERNAL_HOST" ]; then
    if BODY="$(curl_via_traefik_until "$EXTERNAL_HOST" "/${SNAME}/" "$SMARKER")"; then
      ok "Traefik routing works end-to-end (site app reachable via /${SNAME}/)"
    else
      bad "site app not reachable via Traefik -- got: $(echo "$BODY" | head -c 200)"
    fi
  else
    warn "SPINAPP_UI_EXTERNAL_HOST not set, skipping Traefik routing check for site app"
  fi
else
  bad "site app's Pod never reached Ready -- possible stuck containerd-shim-spin-v2 (see spin-shim-recovery.sh)"
fi

peer_delete "/internal/apps/$SNAME/delete" >/dev/null 2>&1
sleep 2
if ! kubectl get spinapp "$SNAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  ok "site app deleted cleanly"
  CLEANUP_SITES=()
else
  bad "site app's SpinApp object still exists after delete"
fi

# ---------------------------------------------------------------------------
log "[7/7] Proxy app (SSRF-boundary regression) + Cron app"
# ---------------------------------------------------------------------------
# Only validation-boundary checks here, not a real proxied-traffic test:
# check_backend_host_not_internal() correctly rejects loopback AND this
# node's own IP (SPINAPP_UI_HOST_IP) as backends by design (see the
# 2026-08 fix), and this is a single-node test environment with no other
# real LAN host to point a "genuinely reachable, genuinely not this node"
# backend at -- deliberately not worked around by adding a temporary IP
# alias or similar, since that's more risk than a health-check script
# should be taking on a live node. What's tested instead is exactly the
# boundary itself: a plausible LAN address one octet away from this
# node's own is accepted (nothing needs to actually answer there for that
# -- validate_proxy_config() only classifies the address), while this
# node's own IP and loopback are still rejected.
if [ -n "$HOST_IP" ]; then
  ALT_LAN_IP="$(python3 -c "
import ipaddress
addr = ipaddress.ip_address('$HOST_IP')
last = int(str(addr).rsplit('.', 1)[1])
alt = last + 1 if last < 254 else last - 1
print('.'.join(str(addr).split('.')[:3] + [str(alt)]))
")"
  PNAME="zzzhealthcheck-proxy-${RUN_ID}"
  cat > "$SCRATCH/proxy.json" <<EOF
{"name": "${PNAME}", "backend_origin": "http://${ALT_LAN_IP}:8080", "rules": [], "rewrites": []}
EOF
  RESP="$(peer_post /internal/proxies "$SCRATCH/proxy.json")"
  CLEANUP_PROXIES+=("$PNAME")
  if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' 2>/dev/null; then
    ok "proxy app accepts a plausible LAN backend ($ALT_LAN_IP, SSRF-boundary regression check)"
  else
    bad "proxy app rejected a plain LAN backend ($ALT_LAN_IP) -- should be allowed: $RESP"
  fi

  if kubectl wait --for=condition=Ready pod -l "core.spinkube.dev/app-name=$PNAME" -n "$NAMESPACE" --timeout=60s >/dev/null 2>&1; then
    ok "proxy app's Pod reached Ready (deploy pipeline itself works, independent of backend reachability)"
  else
    bad "proxy app's Pod never reached Ready"
  fi

  for label_addr in "$HOST_IP (this node's own IP)" "127.0.0.1 (loopback)"; do
    addr="${label_addr%% *}"
    cat > "$SCRATCH/proxy_blocked.json" <<EOF
{"name": "${PNAME}-blk", "backend_origin": "http://${addr}:${CONSOLE_PORT}", "rules": [], "rewrites": []}
EOF
    RESP="$(peer_post /internal/proxies "$SCRATCH/proxy_blocked.json")"
    if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if not d.get("ok") else 1)' 2>/dev/null; then
      ok "proxy app still rejects $label_addr as a backend (SSRF regression check)"
    else
      bad "proxy app did NOT reject $label_addr as a backend -- SSRF regression! response: $RESP"
      peer_delete "/internal/apps/${PNAME}-blk/delete" >/dev/null 2>&1
    fi
  done

  peer_delete "/internal/apps/$PNAME/delete" >/dev/null 2>&1
  sleep 2
  if ! kubectl get spinapp "$PNAME" -n "$NAMESPACE" >/dev/null 2>&1; then
    ok "proxy app deleted cleanly"
    CLEANUP_PROXIES=()
  else
    bad "proxy app's SpinApp object still exists after delete"
  fi
else
  warn "SPINAPP_UI_HOST_IP not set, skipping proxy app test"
fi

XNAME="zzzhealthcheck-cron-${RUN_ID}"
XMARKER="healthcheck-cron-ok-${RUN_ID}"
cat > "$SCRATCH/cron.json" <<EOF
{
  "name": "${XNAME}",
  "code": "print(\"${XMARKER}\")",
  "schedule": "@yearly",
  "language": "python"
}
EOF
RESP="$(peer_post /internal/crons "$SCRATCH/cron.json")"
CLEANUP_CRONS+=("$XNAME")
if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' 2>/dev/null; then
  ok "cron app created ($XNAME)"
else
  bad "cron app creation failed: $RESP"
fi

if kubectl get cronjob "$XNAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  ok "CronJob object exists"
  JOBNAME="${XNAME}-manualrun-${RUN_ID}"
  CLEANUP_JOBS+=("$JOBNAME")
  if kubectl create job "$JOBNAME" --from="cronjob/$XNAME" -n "$NAMESPACE" >/dev/null 2>&1 \
    && kubectl wait --for=condition=complete "job/$JOBNAME" -n "$NAMESPACE" --timeout=90s >/dev/null 2>&1; then
    ok "manually-triggered cron run completed successfully"
    LOGS="$(kubectl logs -n "$NAMESPACE" "job/$JOBNAME" 2>/dev/null)"
    if echo "$LOGS" | grep -q "$XMARKER"; then
      ok "cron run produced expected output"
    else
      bad "cron run completed but output didn't match -- got: $(echo "$LOGS" | head -c 200)"
    fi
  else
    bad "manually-triggered cron run did not complete within 90s"
  fi
else
  bad "CronJob object was not created"
fi

peer_delete "/internal/crons/$XNAME/delete" >/dev/null 2>&1
sleep 1
if ! kubectl get cronjob "$XNAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  ok "cron app deleted cleanly"
  CLEANUP_CRONS=()
else
  bad "cron app's CronJob still exists after delete"
fi

# ---------------------------------------------------------------------------
log "Traefik access log freshness"
# ---------------------------------------------------------------------------
LOG_PATH="${SPINAPP_UI_TRAEFIK_ACCESS_LOG:-/var/log/traefik/access.log}"
if [ -f "$LOG_PATH" ]; then
  AGE=$(( $(date +%s) - $(stat -c %Y "$LOG_PATH") ))
  if [ "$AGE" -lt 3600 ]; then
    ok "Traefik access log ($LOG_PATH) updated ${AGE}s ago -- this script's own requests should have just refreshed it"
  else
    bad "Traefik access log ($LOG_PATH) hasn't been updated in ${AGE}s despite this script's own requests -- path mismatch between env and the live HelmChartConfig? (see the 2026-08-11 mg3 incident)"
  fi
else
  bad "Traefik access log not found at $LOG_PATH (SPINAPP_UI_TRAEFIK_ACCESS_LOG)"
fi

# ---------------------------------------------------------------------------
echo
echo "==================================================================="
echo "  Result: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "-------------------------------------------------------------------"
  for f in "${FAILURES[@]}"; do
    echo "  - $f"
  done
fi
echo "==================================================================="

[ "$FAIL" -eq 0 ]

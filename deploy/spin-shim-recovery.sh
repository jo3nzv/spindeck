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
# Detects and recovers from a known upstream bug in containerd-shim-spin
# (the Wasm runtime js/site/proxy/cron apps run under):
# containerd-shim-spin-v2 processes sometimes get stuck and never exit after
# their container fails to start, silently blocking *every* future Spin
# container launch on the node (not just the one they belonged to) until
# someone manually kills them. Symptoms: a SpinApp Pod stuck in
# CrashLoopBackOff with completely empty logs across every restart --
# genuinely broken user code almost always logs *something* before
# crashing, so "zero output, ever" plus a real restart count is a specific
# enough signal to act on automatically without risking masking an actual
# bug in someone's app.
#
# Run periodically by spin-shim-recovery.timer. Safe to re-run -- a no-op
# when nothing matches. Scoped to $SPINAPP_UI_NAMESPACE (default: "default"
# -- exclusively where SpinDeck deploys apps) and only ever acts on a Pod
# after confirming it has a matching stuck containerd-shim-spin-v2 process,
# so it can never touch system components (Traefik, cert-manager, ... all
# in other namespaces) or non-Spin apps. Deliberately not filtered by the
# spinapp-ui.dev/app label -- that's only ever set on the SpinApp/
# Deployment/CronJob object itself, not on the Pods those create (js/site/
# proxy Pods get spin-operator's own core.spinkube.dev/app-name label
# instead), so a label selector here would silently miss exactly the app
# kinds most likely to hit this bug.
set -uo pipefail

ENV_FILE=/etc/spinapp-ui/env
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
NAMESPACE="${SPINAPP_UI_NAMESPACE:-default}"

# How many restarts before we consider a Pod "stuck" rather than just
# having a rough start -- avoids reacting to the very first failure, which
# might be a transient blip that resolves on its own via kubelet's normal
# retry.
RESTART_THRESHOLD=3
# Once we've auto-recovered a given Pod name, don't do it again for this
# long -- if the underlying app is *actually* broken (not a stuck shim),
# this stops us from silently recycling it forever and masking a real bug
# behind an ever-climbing restart count that never surfaces as "still
# broken after we already tried the fix".
COOLDOWN_SECONDS=3600
STATE_DIR=/run/spinapp-ui/shim-recovery
mkdir -p "$STATE_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

PODS_JSON="$(kubectl get pods -n "$NAMESPACE" -o json 2>/dev/null)"
[ -z "$PODS_JSON" ] && exit 0

# Each line is "<pod-name> <cooldown-key>". The cooldown key is the
# owning ReplicaSet/Job name (falls back to the Pod's own name if for some
# reason there's no owner), NOT the Pod name itself -- a recovered Pod
# gets a brand new random-suffixed name every time we recreate it, so
# keying the cooldown off the Pod name would never actually rate-limit
# anything: each recreation would look like a fresh, never-before-seen
# candidate. The owner name stays stable across recreations as long as
# the app itself isn't redeployed (a genuine redeploy rolling to a new
# ReplicaSet/Job *should* reset the cooldown -- that's a different app
# version, not the same stuck instance repeating).
CANDIDATES="$(python3 -c '
import json, sys
data = json.loads(sys.argv[1])
for pod in data.get("items", []):
    name = pod["metadata"]["name"]
    for cs in pod.get("status", {}).get("containerStatuses", []) or []:
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") != "CrashLoopBackOff":
            continue
        if cs.get("restartCount", 0) < '"$RESTART_THRESHOLD"':
            continue
        owners = pod["metadata"].get("ownerReferences") or []
        key = owners[0]["name"] if owners else name
        print(f"{name} {key}")
        break
' "$PODS_JSON")"

[ -z "$CANDIDATES" ] && exit 0

while read -r POD COOLDOWN_KEY; do
  [ -z "$POD" ] && continue
  STATE_FILE="$STATE_DIR/$COOLDOWN_KEY"
  if [ -f "$STATE_FILE" ]; then
    LAST_RUN="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
    NOW="$(date +%s)"
    if [ $((NOW - LAST_RUN)) -lt "$COOLDOWN_SECONDS" ]; then
      continue
    fi
  fi

  # Empty logs (current *and* every previous restart) is the specific part
  # of the signature -- a genuinely broken image/app almost always prints
  # something before dying. kubectl logs --previous only looks at the last
  # restart, which is enough: if that one was silent, the stuck-shim
  # pattern doesn't change between restarts, so earlier ones would be too.
  LOGS="$(kubectl logs "$POD" -n "$NAMESPACE" --previous 2>/dev/null)"
  CURRENT_LOGS="$(kubectl logs "$POD" -n "$NAMESPACE" 2>/dev/null)"
  if [ -n "$LOGS" ] || [ -n "$CURRENT_LOGS" ]; then
    continue
  fi

  # crictl ps's own --name flag filters by *container* name (e.g.
  # "airport-weather", the SpinApp name -- confirmed live via `crictl ps
  # -a -o json`), never the full Pod name (which has the random
  # ReplicaSet-hash + Pod-hash suffix, e.g. "airport-weather-774d4df8b4-
  # nrl8b") -- so "--name ^${POD}$" never actually matched anything,
  # silently making every candidate hit the "could not resolve" branch
  # below and skip real recovery. Filtering the full container list by
  # the io.kubernetes.pod.name *label* instead (which does hold the
  # full Pod name) is the fix, and reading podSandboxId straight off
  # each container entry also skips the extra `crictl inspectp` round
  # trip the old version needed.
  SANDBOX_ID="$(k3s crictl ps -a -o json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
pod = sys.argv[1]
for c in d.get("containers") or []:
    if c.get("labels", {}).get("io.kubernetes.pod.name") == pod:
        print(c.get("podSandboxId", ""))
        break
' "$POD")"

  if [ -z "$SANDBOX_ID" ]; then
    log "skip $POD: could not resolve its pod sandbox id"
    continue
  fi

  KILLED=0
  for PID in $(pgrep -f "containerd-shim-spin-v2 .*-id $SANDBOX_ID( |$)" 2>/dev/null); do
    kill -9 "$PID" 2>/dev/null && KILLED=$((KILLED + 1))
  done

  # Only reached for a container that isn't running under containerd-shim-
  # spin-v2 at all (e.g. a "container"-kind Docker app, or a genuinely
  # broken Spin app whose shim actually exited cleanly) -- not this
  # script's problem to fix, and deleting the Pod wouldn't address
  # whatever's really wrong with it. Leave it for kubelet's normal retry
  # loop / a human to look at instead of masking it.
  if [ "$KILLED" -eq 0 ]; then
    log "skip $POD: CrashLoopBackOff + empty logs, but no stuck containerd-shim-spin-v2 process found for sandbox $SANDBOX_ID -- not the known issue, leaving it alone"
    continue
  fi

  rm -rf "/run/containerd/spin/k8s.io/${SANDBOX_ID:?}"
  kubectl delete pod "$POD" -n "$NAMESPACE" --force --grace-period=0 >/dev/null 2>&1

  date +%s > "$STATE_FILE"
  log "recovered $POD: killed $KILLED stuck containerd-shim-spin-v2 process(es) for sandbox $SANDBOX_ID, recreated the pod"
done <<< "$CANDIDATES"

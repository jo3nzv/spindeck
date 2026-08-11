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
# Upgrades this node's OS packages and Kubernetes-stack components (k3s,
# cert-manager, the SpinKube operator) to whatever's currently latest.
# Traefik has no separate step -- it's k3s's own bundled HelmChart-
# managed addon, so upgrading k3s is what brings a newer Traefik along.
#
# This is the highest-blast-radius script in this repo -- unlike
# update.sh (console application layer only, safe to run blind) or
# install.sh (idempotent, but its cert-manager/spin-operator versions
# are pinned constants, not "whatever's newest right now"), this
# upgrades the actual Kubernetes control plane and its cluster-wide
# operators on a single-node production cluster with no staging
# environment to test against first. Read this whole script, and make
# sure you have a recent backup.sh archive, before running it. Run
# ./healthcheck.sh afterward -- it exercises every app kind end-to-end
# with disposable resources and re-checks a few specific regressions this
# project has actually hit, so a broken upgrade surfaces immediately
# instead of on the next real deploy.
#
# After a successful run, install.sh's own CERT_MANAGER_VERSION/
# SPIN_OPERATOR_VERSION constants are bumped in place (via sed) to
# match what actually got installed here, so a future *fresh* install
# starts from the same versions instead of silently drifting from what
# upgraded nodes are actually running. Review and commit that diff
# afterward -- this script deliberately doesn't commit it for you.
# Also writes /opt/spinapp-ui/versions.json (cert-manager/spin-operator/
# Traefik versions) for the dashboard's "システム状態" card to read --
# see app.py's get_component_versions().
#
# Usage: sudo ./scripts/upgrade.sh (from the repo root)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo ./scripts/upgrade.sh" >&2
  exit 1
fi

# This script now lives in scripts/, one level below the repo root that
# deploy/, install.sh, etc. are relative to -- ".." here resolves back
# up to that root regardless of where this script was invoked from.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if ! command -v k3s >/dev/null 2>&1; then
  echo "k3s doesn't look installed on this host -- run ./scripts/install.sh first." >&2
  exit 1
fi

# Not sourced from /etc/profile.d/k3s-kubeconfig.sh here (that's only set
# up for interactive login shells by install.sh, and a script invoked via
# `sudo ./scripts/upgrade.sh` doesn't necessarily go through one) -- helm in
# particular has no other fallback and fails outright without this.
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

log() { echo -e "\n==> $*"; }

log "[1/5] OS packages"
# Plain "upgrade", not "dist-upgrade"/"full-upgrade" -- deliberately
# won't remove or replace packages to satisfy a dependency change, only
# ever upgrades in place. Safer default for an unattended production
# host; revisit if that turns out to leave too much on the table.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" upgrade
if [ -f /var/run/reboot-required ]; then
  echo
  echo "NOTE: a reboot is required to finish applying OS updates (kernel or"
  echo "similar). Not doing that automatically here -- a reboot on this"
  echo "single-node cluster takes every app down until it comes back up,"
  echo "so that should be a deliberate, scheduled action, not a side effect"
  echo "of running this script. Reboot by hand when convenient."
fi

log "[2/5] k3s"
BEFORE_K3S_VERSION="$(k3s --version | head -1 | awk '{print $3}')"
# Same invocation as install.sh's own first-install line (see there),
# just without the "only if not already installed" guard -- re-running
# k3s's official install script against an existing install is its
# documented self-upgrade path: it downloads the current latest stable
# release, replaces the binary if it differs, and restarts the k3s
# service itself. A no-op (prints "no change") if already current.
curl -sfL https://get.k3s.io | sh -s - server --config /etc/rancher/k3s/config.yaml
AFTER_K3S_VERSION="$(k3s --version | head -1 | awk '{print $3}')"
if [ "$BEFORE_K3S_VERSION" != "$AFTER_K3S_VERSION" ]; then
  echo "k3s: $BEFORE_K3S_VERSION -> $AFTER_K3S_VERSION"
else
  echo "k3s: already at latest ($BEFORE_K3S_VERSION)"
fi
if ! kubectl wait --for=condition=Ready node --all --timeout=300s; then
  echo "Node didn't report Ready within 5 minutes after the k3s upgrade." >&2
  echo "Check: kubectl get nodes / journalctl -u k3s -f" >&2
  exit 1
fi

log "[3/5] cert-manager"
LATEST_CERT_MANAGER="$(curl -sf https://api.github.com/repos/cert-manager/cert-manager/releases/latest | grep -oP '"tag_name":\s*"v\K[^"]+')"
if [ -z "$LATEST_CERT_MANAGER" ]; then
  echo "Couldn't determine cert-manager's latest release from the GitHub API" >&2
  echo "(rate-limited, or offline?) -- skipping cert-manager this run." >&2
else
  echo "cert-manager -> v${LATEST_CERT_MANAGER}"
  kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/v${LATEST_CERT_MANAGER}/cert-manager.yaml"
  if ! kubectl wait --for=condition=Available --timeout=300s -n cert-manager deployment --all; then
    echo "cert-manager deployments didn't report Available within 5 minutes" >&2
    echo "(often just a slow first image pull). Check: kubectl get pods -n cert-manager" >&2
    exit 1
  fi
  sed -i "s/^CERT_MANAGER_VERSION=.*/CERT_MANAGER_VERSION=\"${LATEST_CERT_MANAGER}\"/" scripts/install.sh
fi

log "[4/5] SpinKube operator"
# Same CRDs/RuntimeClass/Executor manifests install.sh applies -- kubectl
# apply is idempotent, so this is a no-op if they haven't changed.
kubectl apply -f deploy/k8s-manifests/spinkube-crds.yaml
kubectl apply -f deploy/k8s-manifests/spinkube-runtime.yaml
# No --version pin (unlike install.sh's) -- omitting it is how you ask
# Helm for whatever the chart's current latest release is.
if ! helm upgrade --install spin-operator \
  --namespace spin-operator --create-namespace \
  oci://ghcr.io/spinframework/charts/spin-operator \
  --wait --timeout 5m; then
  echo "spin-operator didn't come up within 5 minutes. Check: kubectl get pods -n spin-operator" >&2
  exit 1
fi
LATEST_SPIN_OPERATOR="$(helm list -n spin-operator -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["chart"].split("spin-operator-", 1)[1])')"
echo "spin-operator -> chart ${LATEST_SPIN_OPERATOR}"
sed -i "s/^SPIN_OPERATOR_VERSION=.*/SPIN_OPERATOR_VERSION=\"${LATEST_SPIN_OPERATOR}\"/" scripts/install.sh

log "[5/5] Recording component versions for the dashboard"
# The console's own restricted ServiceAccount can't read the cert-
# manager/spin-operator/kube-system namespaces (see spinapp-ui-rbac.yaml)
# -- rather than widen that just so the dashboard can show these version
# strings, this admin-kubeconfig'd script writes them to a small file the
# console just reads (see app.py's get_component_versions()). If the
# GitHub API call for cert-manager failed above (LATEST_CERT_MANAGER
# empty), that step never actually ran, so fall back to install.sh's
# still-accurate (unchanged) existing pin instead of writing a blank.
CERT_MANAGER_RECORDED="${LATEST_CERT_MANAGER:-}"
if [ -z "$CERT_MANAGER_RECORDED" ]; then
  CERT_MANAGER_RECORDED="$(sed -nE 's/^CERT_MANAGER_VERSION="(.*)"$/\1/p' scripts/install.sh)"
fi
TRAEFIK_VERSION="$(helm list -n kube-system -o json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next((r["app_version"] for r in d if r["name"]=="traefik"), ""))' 2>/dev/null || true)"
python3 -c "
import json, datetime
json.dump(
    {
        'cert_manager': '${CERT_MANAGER_RECORDED}',
        'spin_operator': '${LATEST_SPIN_OPERATOR}',
        'traefik': '${TRAEFIK_VERSION}',
        'updated_at': datetime.datetime.now().astimezone().isoformat(),
    },
    open('/opt/spinapp-ui/versions.json', 'w'),
    indent=2,
)
"
echo "Wrote /opt/spinapp-ui/versions.json"

echo
echo "==> Done."
if git diff --quiet -- scripts/install.sh; then
  echo "install.sh's pinned versions were already up to date."
else
  echo "install.sh's CERT_MANAGER_VERSION/SPIN_OPERATOR_VERSION were updated"
  echo "to match what's now running -- review and commit this change:"
  git diff -- scripts/install.sh
fi

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
# SpinDeck one-shot installer for a bare Ubuntu 24.04 or 26.04 host (VM,
# LXC container, or bare metal -- nothing needs to be pre-installed).
#
# Usage:
#   git clone <this repo's URL> spindeck && cd spindeck
#   sudo ./scripts/install.sh
#
# See docs/INSTALL.md for what this does and how to configure it, in
# English and Japanese. Safe to re-run: each step skips work that's
# already done.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo ./scripts/install.sh" >&2
  exit 1
fi

# This script now lives in scripts/, one level below the repo root that
# deploy/, spinapp-ui/, js-template/, etc. are relative to -- ".." here
# resolves back up to that root regardless of where this script was
# invoked from.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

SPIN_VERSION="4.0.2"
SHIM_VERSION="0.25.1"
CERT_MANAGER_VERSION="1.21.1"
SPIN_OPERATOR_VERSION="0.6.1"
NODE_MAJOR="22"

log() { echo -e "\n==> $*"; }

# Whether k3s is already installed distinguishes a from-scratch install
# (gets the new /data + dual-stack defaults below) from a re-run/upgrade
# on an existing host (keeps behaving exactly as it always has, unless
# the admin explicitly opts in). Both SPINDECK_DATA_DIR and
# SPINDECK_DUAL_STACK partly affect things that get re-applied on every
# run (registry/Traefik-log hostPaths, Traefik's ipFamilyPolicy) even
# though k3s's own data-dir/cluster-cidr don't -- a "safe to re-run"
# script must never start silently relocating an already-running host's
# data, or flipping its networking mode, just because a newer checkout's
# default changed underneath it.
FRESH_INSTALL=true
if command -v k3s >/dev/null 2>&1; then
  FRESH_INSTALL=false
fi

# Where this install's persistent data (k3s itself, the local registry,
# Traefik's access logs, New Site uploads, container-app hostPath data --
# normally scattered across /var/lib, /var/log, /opt) lives. Defaults to
# /data on a from-scratch install (this repo's standard layout, meant for
# a dedicated data disk mounted there); stays opt-in/empty on a host that
# already has k3s installed (see FRESH_INSTALL above). Set explicitly
# either way, including to an empty string to opt out even on a fresh
# install. Note `${VAR-default}` (no colon) below, not `${VAR:-default}`:
# colon-less means only a truly *unset* variable gets the default, so
# `SPINDECK_DATA_DIR= ./scripts/install.sh` (explicitly set to empty) is how you
# opt out on a fresh install.
if [ "$FRESH_INSTALL" = true ]; then
  SPINDECK_DATA_DIR="${SPINDECK_DATA_DIR-/data}"
else
  SPINDECK_DATA_DIR="${SPINDECK_DATA_DIR:-}"
fi
if [ -n "$SPINDECK_DATA_DIR" ]; then
  REGISTRY_DATA_DIR="$SPINDECK_DATA_DIR/registry"
  TRAEFIK_LOG_DIR="$SPINDECK_DATA_DIR/traefik-logs"
  SITES_DATA_DIR="$SPINDECK_DATA_DIR/sites-data"
  CONTAINER_DATA_DIR="$SPINDECK_DATA_DIR/containers"
  # The /config "Analytics" page reads this file directly off disk (see
  # app.py's TRAEFIK_ACCESS_LOG_PATH) -- it has no way to know Traefik's
  # actual log path moved unless told via this env var too. Missing this
  # doesn't error loudly: parse_traefik_logs() just silently returns zero
  # entries from a nonexistent (or stale, pre-move) default path, which
  # reads as "some traffic isn't being counted" rather than an obvious
  # misconfiguration -- confirmed live on a host that had set
  # SPINDECK_DATA_DIR.
  TRAEFIK_ACCESS_LOG_VAL="$TRAEFIK_LOG_DIR/access.log"
  mkdir -p "$SPINDECK_DATA_DIR/k3s" "$REGISTRY_DATA_DIR" "$TRAEFIK_LOG_DIR" "$SITES_DATA_DIR" "$CONTAINER_DATA_DIR"
  if command -v k3s >/dev/null; then
    echo "WARNING: SPINDECK_DATA_DIR=$SPINDECK_DATA_DIR, but k3s is already installed on this"
    echo "host -- k3s's own --data-dir only takes effect on its very first install,"
    echo "so k3s's data stays under /var/lib/rancher/k3s. The registry/Traefik-log/"
    echo "site-data/container-data paths below still move under \$SPINDECK_DATA_DIR."
  fi
else
  REGISTRY_DATA_DIR="/var/lib/spinkube-registry"
  TRAEFIK_LOG_DIR="/var/log/traefik"
  SITES_DATA_DIR=""   # empty = leave app.py's own default in place
  TRAEFIK_ACCESS_LOG_VAL=""   # empty = leave app.py's own default in place
  CONTAINER_DATA_DIR=""   # empty = leave app.py's own default (/data/containers) in place
fi

# Optional: also accept IPv6 incoming traffic (dual-stack -- IPv4 keeps
# working the same as always). Defaults on for a from-scratch install
# (same FRESH_INSTALL reasoning as SPINDECK_DATA_DIR above); stays
# opt-in/empty on an already-installed host, since k3s's own
# cluster-cidr/service-cidr can't be turned on post-install anyway and
# Traefik's ipFamilyPolicy/the console's bind host WOULD otherwise flip
# out from under a live host on every re-run. fd42::/56 and fd43::/112
# are this repo's fixed IPv6 Pod/Service CIDR choice (ULA -- non-
# routable, same role as the existing 10.42.0.0/16 / 10.43.0.0/16 IPv4
# choices); the node's own real, routable IPv6 address is what actually
# reaches the internet, via flannel's IPv6 masquerading. Set explicitly
# to an empty string to opt out even on a fresh install (e.g. a host with
# no IPv6 uplink at all -- dual-stack itself is harmless without one, see
# the "no outbound IPv6 detected" note below, but there's no reason to
# pay even the small extra config surface for it if you know you'll
# never use it).
if [ "$FRESH_INSTALL" = true ]; then
  SPINDECK_DUAL_STACK="${SPINDECK_DUAL_STACK-1}"
else
  SPINDECK_DUAL_STACK="${SPINDECK_DUAL_STACK:-}"
fi
if [ -n "$SPINDECK_DUAL_STACK" ]; then
  IP_FAMILY_POLICY="PreferDualStack"
  BIND_HOST_VAL="::"
  NODE_IPV6="$(ip -6 route get 2001:4860:4860::8888 2>/dev/null | grep -oP 'src \K\S+' || true)"
  if [ -z "$NODE_IPV6" ]; then
    echo "NOTE: dual-stack networking is enabled (the default), but this host"
    echo "doesn't appear to have working outbound IPv6 (couldn't detect a source"
    echo "address via \`ip -6 route get\`) -- this is fine, IPv4 keeps working"
    echo "exactly as always either way. The egress NetworkPolicy's IPv6 rules"
    echo "are simply skipped rather than applied with a wrong or missing node"
    echo "address; fix IPv6 connectivity and re-run to pick them up, or set"
    echo "SPINDECK_DUAL_STACK= to disable dual-stack entirely."
  fi
  if command -v k3s >/dev/null; then
    echo "WARNING: SPINDECK_DUAL_STACK is set, but k3s is already installed on"
    echo "this host -- k3s's own cluster-cidr/service-cidr only take effect on"
    echo "its very first install, so this cluster stays IPv4-only regardless."
  fi
  echo "Dual-stack networking enabled -- this node's detected IPv6 address:" \
    "${NODE_IPV6:-(none detected)}"
else
  IP_FAMILY_POLICY="SingleStack"
  BIND_HOST_VAL=""   # empty = leave app.py's own default (0.0.0.0) in place
  NODE_IPV6=""
fi

# ---------------------------------------------------------------------------
log "[1/15] apt prerequisites"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl ca-certificates gnupg git cron python3-venv python3-pip openssl

# ---------------------------------------------------------------------------
log "[2/15] Node.js ${NODE_MAJOR}.x (for the JS build pipeline)"
if ! command -v node >/dev/null || [ "$(node -v | sed 's/^v//' | cut -d. -f1)" -lt "$NODE_MAJOR" ]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
fi

# ---------------------------------------------------------------------------
log "[3/15] k3s"
mkdir -p /etc/rancher/k3s
# KubeletInUserNamespace works around missing /dev/kmsg in LXC containers;
# harmless on a VM/bare-metal host too. registries.yaml trusts localhost:5000
# (our local registry, step 9) as insecure HTTP.
cp deploy/k3s/k3s-config.yaml /etc/rancher/k3s/config.yaml
if [ -n "$SPINDECK_DATA_DIR" ]; then
  echo "data-dir: $SPINDECK_DATA_DIR/k3s" >> /etc/rancher/k3s/config.yaml
fi
if [ -n "$SPINDECK_DUAL_STACK" ]; then
  cat >> /etc/rancher/k3s/config.yaml <<'YAML'
cluster-cidr:
  - 10.42.0.0/16
  - fd42::/56
service-cidr:
  - 10.43.0.0/16
  - fd43::/112
cluster-dns:
  - 10.43.0.10
  - fd43::10
flannel-ipv6-masq: true
YAML
fi
cp deploy/k3s/registries.yaml /etc/rancher/k3s/registries.yaml
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | sh -s - server --config /etc/rancher/k3s/config.yaml
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
if [ ! -f /etc/profile.d/k3s-kubeconfig.sh ]; then
  echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' > /etc/profile.d/k3s-kubeconfig.sh
fi

log "waiting for the node to be Ready..."
until kubectl get nodes 2>/dev/null | grep -q ' Ready'; do sleep 2; done

# ---------------------------------------------------------------------------
log "[4/15] Spin CLI v${SPIN_VERSION} + containerd-shim-spin-v2 v${SHIM_VERSION}"
if ! command -v spin >/dev/null; then
  curl -fsSL -o /tmp/spin.tar.gz \
    "https://github.com/spinframework/spin/releases/download/v${SPIN_VERSION}/spin-v${SPIN_VERSION}-linux-amd64.tar.gz"
  tar -xzf /tmp/spin.tar.gz -C /usr/local/bin spin
  chmod +x /usr/local/bin/spin
  rm -f /tmp/spin.tar.gz
fi
if ! command -v containerd-shim-spin-v2 >/dev/null; then
  curl -fsSL -o /tmp/shim.tar.gz \
    "https://github.com/spinframework/containerd-shim-spin/releases/download/v${SHIM_VERSION}/containerd-shim-spin-v2-linux-x86_64.tar.gz"
  tar -xzf /tmp/shim.tar.gz -C /usr/local/bin
  chmod +x /usr/local/bin/containerd-shim-spin-v2
  rm -f /tmp/shim.tar.gz
fi

log "wiring the Spin shim into containerd + restarting k3s"
mkdir -p /var/lib/rancher/k3s/agent/etc/containerd
cp deploy/k3s/containerd-config.toml.tmpl /var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl
systemctl restart k3s
until kubectl get nodes 2>/dev/null | grep -q ' Ready'; do sleep 2; done

# ---------------------------------------------------------------------------
log "[5/15] helm"
if ! command -v helm >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

# ---------------------------------------------------------------------------
log "[6/15] cert-manager v${CERT_MANAGER_VERSION}"
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/v${CERT_MANAGER_VERSION}/cert-manager.yaml"
if ! kubectl wait --for=condition=Available --timeout=300s -n cert-manager deployment --all; then
  echo
  echo "cert-manager deployments didn't report Available within 5 minutes"
  echo "(often just a slow first image pull, nothing actually wrong). Current status:"
  kubectl get pods -n cert-manager
  echo
  echo "If those reach 1/1 Running shortly after, just re-run ./scripts/install.sh --"
  echo "every step in it is safe to repeat."
  exit 1
fi

# ---------------------------------------------------------------------------
log "[7/15] SpinKube: CRDs, RuntimeClass/Executor, spin-operator v${SPIN_OPERATOR_VERSION}"
kubectl apply -f deploy/k8s-manifests/spinkube-crds.yaml
kubectl apply -f deploy/k8s-manifests/spinkube-runtime.yaml
if ! helm upgrade --install spin-operator \
  --namespace spin-operator --create-namespace \
  --version "${SPIN_OPERATOR_VERSION}" \
  oci://ghcr.io/spinframework/charts/spin-operator \
  --wait --timeout 5m; then
  echo
  echo "spin-operator didn't come up within 5 minutes. Current status:"
  kubectl get pods -n spin-operator
  echo
  echo "If it reaches 1/1 Running shortly after, just re-run ./scripts/install.sh --"
  echo "every step in it is safe to repeat."
  exit 1
fi

# ---------------------------------------------------------------------------
log "[8/15] Local OCI registry (hostNetwork, localhost:5000)"
sed "s#__REGISTRY_DATA_DIR__#${REGISTRY_DATA_DIR}#" \
  deploy/k8s-manifests/registry.yaml.tmpl | kubectl apply -f -

# ---------------------------------------------------------------------------
log "[9/15] Egress NetworkPolicy (contain SpinApps/CronJobs to the public internet + your LAN)"
# Deployed apps/cron jobs must not be able to reach this cluster's own
# internals (the k8s API, other ClusterIP services) even though the Proxy
# feature intentionally lets them reach the rest of your LAN -- see the
# NetworkPolicy's own comments for the full reasoning. __NODE_IP__ is substituted with
# this node's own LAN IP (needed because the k8s API's ClusterIP actually
# routes to that address, not just the ClusterIP itself).
NODE_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || true)"
if [ -z "$NODE_IP" ]; then
  echo "Couldn't auto-detect this node's LAN IP -- skipping the egress"
  echo "NetworkPolicy. Apply deploy/k8s-manifests/spinapp-egress-policy.yaml.tmpl"
  echo "manually (substitute __NODE_IP__ yourself) once you know it."
else
  # The IPv6 block (see the .tmpl's own comments) is only meaningful with
  # SPINDECK_DUAL_STACK -- otherwise no Pod can originate IPv6 traffic in
  # the first place, so the whole block is stripped rather than applied
  # with a placeholder that was never substituted.
  if [ -n "$NODE_IPV6" ]; then
    IPV6_SED_EXPR="/__IPV6_BLOCK_START__/d;/__IPV6_BLOCK_END__/d;s/__NODE_IPV6__/${NODE_IPV6}/g"
  else
    IPV6_SED_EXPR="/__IPV6_BLOCK_START__/,/__IPV6_BLOCK_END__/d"
  fi
  sed -e "s/__NODE_IP__/${NODE_IP}/g" -e "$IPV6_SED_EXPR" \
    deploy/k8s-manifests/spinapp-egress-policy.yaml.tmpl | kubectl apply -f -
fi

# ---------------------------------------------------------------------------
log "[10/15] Traefik: JSON access logs, trusted X-Forwarded-For, persistent log file"
mkdir -p "$TRAEFIK_LOG_DIR"
chown 65532:65532 "$TRAEFIK_LOG_DIR"   # Traefik's container runs as this non-root UID
sed -e "s#__TRAEFIK_LOG_DIR__#${TRAEFIK_LOG_DIR}#g" -e "s/__IP_FAMILY_POLICY__/${IP_FAMILY_POLICY}/g" \
  deploy/k8s-manifests/traefik-helmchartconfig.yaml.tmpl | kubectl apply -f -

# ---------------------------------------------------------------------------
log "[11/15] SpinDeck console install"
mkdir -p /opt/spinapp-ui
python3 -m venv /opt/spinapp-ui/venv
/opt/spinapp-ui/venv/bin/pip install --upgrade pip -q
/opt/spinapp-ui/venv/bin/pip install -q -r spinapp-ui/requirements.txt
cp spinapp-ui/app.py /opt/spinapp-ui/app.py
cp spinapp-ui/i18n.py /opt/spinapp-ui/i18n.py
cp spinapp-ui/backup_export.py /opt/spinapp-ui/backup_export.py
rm -rf /opt/spinapp-ui/templates
cp -r spinapp-ui/templates /opt/spinapp-ui/templates
mkdir -p /opt/spinapp-ui/js-template-src
rm -rf /opt/spinapp-ui/js-template-src/hello-js
cp -r js-template /opt/spinapp-ui/js-template-src/hello-js
(cd /opt/spinapp-ui/js-template-src/hello-js && npm install --no-audit --no-fund --loglevel=error)

# For the dashboard's "システム状態" card (see app.py's get_component_
# versions()) -- same file upgrade.sh keeps up to date after this, just
# seeded here from the versions this install itself just applied above,
# so a fresh install shows real data immediately instead of blanks until
# the first upgrade.sh run.
TRAEFIK_VERSION="$(helm list -n kube-system -o json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next((r["app_version"] for r in d if r["name"]=="traefik"), ""))' 2>/dev/null || true)"
python3 -c "
import json, datetime
json.dump(
    {
        'cert_manager': '${CERT_MANAGER_VERSION}',
        'spin_operator': '${SPIN_OPERATOR_VERSION}',
        'traefik': '${TRAEFIK_VERSION}',
        'updated_at': datetime.datetime.now().astimezone().isoformat(),
    },
    open('/opt/spinapp-ui/versions.json', 'w'),
    indent=2,
)
"

# ---------------------------------------------------------------------------
log "[12/15] Kubeconfig for spinapp-ui (least-privilege, not cluster-admin)"
# spinapp-ui only ever touches a fixed list of resource kinds inside the
# "default" namespace, so it gets its own ServiceAccount+Role instead of the raw
# /etc/rancher/k3s/k3s.yaml admin kubeconfig (system:masters) -- a bug or
# compromise in the spinapp-ui process itself then can't reach any other
# namespace or any cluster-scoped resource. Re-applying this on an existing
# install is safe/idempotent (same Role each time); only the *env file's*
# KUBECONFIG= line is treated as one-time setup, same as everything else in
# step 13 below.
kubectl apply -f deploy/k8s-manifests/spinapp-ui-rbac.yaml
until kubectl get secret spinapp-ui-token -n default -o jsonpath='{.data.token}' 2>/dev/null | grep -q .; do sleep 1; done
SPINAPP_UI_SA_SERVER="$(kubectl config view --kubeconfig=/etc/rancher/k3s/k3s.yaml -o jsonpath='{.clusters[0].cluster.server}')"
SPINAPP_UI_SA_CA="$(kubectl config view --kubeconfig=/etc/rancher/k3s/k3s.yaml --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
SPINAPP_UI_SA_TOKEN="$(kubectl get secret spinapp-ui-token -n default -o jsonpath='{.data.token}' | base64 -d)"
# On a from-scratch install /etc/spinapp-ui doesn't exist yet -- step 13
# below is the one that normally creates it, but this step now runs first
# and needs the directory itself to write the kubeconfig into.
mkdir -p /etc/spinapp-ui
cat > /etc/spinapp-ui/kubeconfig.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
- name: k3s
  cluster:
    server: ${SPINAPP_UI_SA_SERVER}
    certificate-authority-data: ${SPINAPP_UI_SA_CA}
contexts:
- name: spinapp-ui
  context:
    cluster: k3s
    namespace: default
    user: spinapp-ui
current-context: spinapp-ui
users:
- name: spinapp-ui
  user:
    token: ${SPINAPP_UI_SA_TOKEN}
EOF
chmod 600 /etc/spinapp-ui/kubeconfig.yaml

# ---------------------------------------------------------------------------
log "[13/15] Configuration (/etc/spinapp-ui/env)"
mkdir -p /etc/spinapp-ui
# Definitely-defined even when the "already exists" branch below runs
# instead of the fresh-install one (set -u would otherwise fail on these
# being read, unset, in the final summary at the very end of this script).
CONSOLE_AUTH_MODE_VAL=""
ADMIN_USERNAME_VAL=""
ADMIN_PASSWORD_VAL=""
ADMIN_PASSWORD_GENERATED=""
ADMIN_ACCOUNT_CREATED=""
if [ -f /etc/spinapp-ui/env ]; then
  echo "/etc/spinapp-ui/env already exists -- leaving it untouched."
  if ! grep -q '^KUBECONFIG=/etc/spinapp-ui/kubeconfig.yaml$' /etc/spinapp-ui/env; then
    echo "NOTE: /etc/spinapp-ui/kubeconfig.yaml (a least-privilege"
    echo "ServiceAccount, not cluster-admin) was generated by step 12 above,"
    echo "but this existing env file's KUBECONFIG= still points elsewhere --"
    echo "only a brand-new env file switches to it automatically. To adopt it:"
    echo "  sed -i 's|^KUBECONFIG=.*|KUBECONFIG=/etc/spinapp-ui/kubeconfig.yaml|' /etc/spinapp-ui/env"
    echo "  systemctl restart spinapp-ui"
  fi
  if [ -n "$SITES_DATA_DIR" ] || [ -n "$TRAEFIK_ACCESS_LOG_VAL" ] || [ -n "$BIND_HOST_VAL" ] || [ -n "$CONTAINER_DATA_DIR" ]; then
    echo "NOTE: SPINDECK_DATA_DIR/SPINDECK_DUAL_STACK are set, but since"
    echo "/etc/spinapp-ui/env already existed, this run did NOT add"
    echo "SPINAPP_UI_SITES_DATA_DIR / SPINAPP_UI_TRAEFIK_ACCESS_LOG /"
    echo "SPINAPP_UI_BIND_HOST / SPINAPP_UI_CONTAINER_DATA_DIR to it (only a"
    echo "brand-new env file gets those written automatically). Add whichever"
    echo "of these you need by hand:"
    [ -n "$SITES_DATA_DIR" ] && echo "  SPINAPP_UI_SITES_DATA_DIR=${SITES_DATA_DIR}"
    [ -n "$TRAEFIK_ACCESS_LOG_VAL" ] && echo "  SPINAPP_UI_TRAEFIK_ACCESS_LOG=${TRAEFIK_ACCESS_LOG_VAL}"
    [ -n "$BIND_HOST_VAL" ] && echo "  SPINAPP_UI_BIND_HOST=${BIND_HOST_VAL}"
    [ -n "$CONTAINER_DATA_DIR" ] && echo "  SPINAPP_UI_CONTAINER_DATA_DIR=${CONTAINER_DATA_DIR}"
    echo "then: systemctl restart spinapp-ui"
  fi
else
  CONSOLE_AUTH_MODE_VAL="${SPINDECK_CONSOLE_AUTH_MODE:-}"
  EXTERNAL_HOST_VAL="${SPINDECK_EXTERNAL_HOST:-}"
  ADMIN_USERNAME_VAL="${SPINDECK_ADMIN_USERNAME:-}"
  ADMIN_PASSWORD_VAL="${SPINDECK_ADMIN_PASSWORD:-}"
  OIDC_ISSUER_VAL="${SPINDECK_OIDC_ISSUER:-}"
  OIDC_CLIENT_ID_VAL="${SPINDECK_OIDC_CLIENT_ID:-}"
  OIDC_CLIENT_SECRET_VAL="${SPINDECK_OIDC_CLIENT_SECRET:-}"
  OIDC_REDIRECT_URI_VAL="${SPINDECK_OIDC_REDIRECT_URI:-}"

  if [ -t 0 ] && [ -z "$EXTERNAL_HOST_VAL" ]; then
    echo "Interactive setup (press Enter to accept the default in [brackets]):"
    read -rp "  Public hostname apps will be served under [apps.example.com]: " EXTERNAL_HOST_VAL
    read -rp "  Console login: (b)asic auth [local account, no external dependency] or (o)idc/Keycloak? [b]: " AUTH_CHOICE
    if [ "$AUTH_CHOICE" = "o" ] || [ "$AUTH_CHOICE" = "O" ]; then
      CONSOLE_AUTH_MODE_VAL="oidc"
      read -rp "  Keycloak (OIDC) issuer URL, e.g. https://auth.example.com/realms/myrealm []: " OIDC_ISSUER_VAL
      read -rp "  OIDC client ID [https://admin.example.com]: " OIDC_CLIENT_ID_VAL
      read -rp "  OIDC client secret []: " OIDC_CLIENT_SECRET_VAL
      read -rp "  OIDC redirect URI [https://admin.example.com/oidc/callback]: " OIDC_REDIRECT_URI_VAL
    else
      CONSOLE_AUTH_MODE_VAL="basic"
      read -rp "  Admin username [admin]: " ADMIN_USERNAME_VAL
      read -rsp "  Admin password (leave blank to auto-generate) []: " ADMIN_PASSWORD_VAL
      echo
    fi
  fi

  # Basic auth (a local, self-contained account) is the default -- most
  # installs have no Keycloak/OIDC provider sitting around ready to go,
  # and this still lets /config switch to OIDC later with zero data loss
  # either way (see save_console_auth_mode()/CONSOLE_AUTH_MODE in app.py).
  CONSOLE_AUTH_MODE_VAL="${CONSOLE_AUTH_MODE_VAL:-basic}"
  EXTERNAL_HOST_VAL="${EXTERNAL_HOST_VAL:-apps.example.com}"
  OIDC_ISSUER_VAL="${OIDC_ISSUER_VAL:-https://auth.example.com/realms/myrealm}"
  OIDC_CLIENT_ID_VAL="${OIDC_CLIENT_ID_VAL:-https://admin.example.com}"
  OIDC_CLIENT_SECRET_VAL="${OIDC_CLIENT_SECRET_VAL:-changeme}"
  if [ "$CONSOLE_AUTH_MODE_VAL" = "basic" ]; then
    # Unused for anything functional when basic auth is the login method
    # (the OIDC login route is never exercised) -- except one thing:
    # app.py derives SESSION_COOKIE_SECURE from this URL's scheme (see its
    # own comment on that line). An https:// placeholder here would
    # silently make the browser refuse to store the session cookie at
    # all, since this console is only reachable over plain http by
    # default (CONSOLE_VIA_TRAEFIK off, TRAEFIK_SCHEME "http") -- this was
    # a real bug hit on a fresh basic-auth install. http:// keeps the
    # placeholder harmless and keeps login working; switching to OIDC
    # later from /config and filling in a real https redirect URI fixes
    # this the same way the OIDC bootstrap-phase flow below already does.
    OIDC_REDIRECT_URI_VAL="${OIDC_REDIRECT_URI_VAL:-http://admin.example.com/oidc/callback}"
  else
    OIDC_REDIRECT_URI_VAL="${OIDC_REDIRECT_URI_VAL:-https://admin.example.com/oidc/callback}"
  fi
  ADMIN_USERNAME_VAL="${ADMIN_USERNAME_VAL:-admin}"
  ADMIN_PASSWORD_GENERATED=""
  if [ "$CONSOLE_AUTH_MODE_VAL" = "basic" ] && [ -z "$ADMIN_PASSWORD_VAL" ]; then
    ADMIN_PASSWORD_VAL="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    ADMIN_PASSWORD_GENERATED=1
  fi
  FLASK_SECRET_VAL="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

  # SPINAPP_UI_HOST_IP: this node's own LAN IP, needed for per-app OIDC
  # auth's forwardAuth Middleware to call back into this process (see
  # ensure_forwardauth_reachability() in app.py) -- only actually used if
  # an app turns that on, but there's no reason to leave it blank when
  # $NODE_IP (auto-detected above for the egress NetworkPolicy) already
  # has the right answer. Empty if that detection failed -- same as
  # leaving this unset, app.py's own /config messaging tells the admin
  # what to fill in and where if they ever need per-app OIDC.
  cat > /etc/spinapp-ui/env <<EOF
KUBECONFIG=/etc/spinapp-ui/kubeconfig.yaml
SPINAPP_UI_NAMESPACE=default
SPINAPP_UI_FLASK_SECRET=${FLASK_SECRET_VAL}
SPINAPP_UI_OIDC_ISSUER=${OIDC_ISSUER_VAL}
SPINAPP_UI_OIDC_CLIENT_ID=${OIDC_CLIENT_ID_VAL}
SPINAPP_UI_OIDC_CLIENT_SECRET=${OIDC_CLIENT_SECRET_VAL}
SPINAPP_UI_OIDC_REDIRECT_URI=${OIDC_REDIRECT_URI_VAL}
SPINAPP_UI_REGISTRY=localhost:5000
SPINAPP_UI_EXTERNAL_HOST=${EXTERNAL_HOST_VAL}
SPINAPP_UI_JS_TEMPLATE=/opt/spinapp-ui/js-template-src/hello-js
SPINAPP_UI_HOST_IP=${NODE_IP}
PORT=8080
EOF
  if [ -n "$SITES_DATA_DIR" ]; then
    echo "SPINAPP_UI_SITES_DATA_DIR=${SITES_DATA_DIR}" >> /etc/spinapp-ui/env
  fi
  if [ -n "$TRAEFIK_ACCESS_LOG_VAL" ]; then
    echo "SPINAPP_UI_TRAEFIK_ACCESS_LOG=${TRAEFIK_ACCESS_LOG_VAL}" >> /etc/spinapp-ui/env
  fi
  if [ -n "$BIND_HOST_VAL" ]; then
    echo "SPINAPP_UI_BIND_HOST=${BIND_HOST_VAL}" >> /etc/spinapp-ui/env
  fi
  if [ -n "$CONTAINER_DATA_DIR" ]; then
    echo "SPINAPP_UI_CONTAINER_DATA_DIR=${CONTAINER_DATA_DIR}" >> /etc/spinapp-ui/env
  fi
  chmod 600 /etc/spinapp-ui/env

  # config.json (not /etc/spinapp-ui/env -- these are the same /config
  # settings save_persisted_config() writes at runtime, see app.py) is
  # left alone if it already exists, same "never overwrite what's there"
  # rule as the env file above. Skipped entirely for oidc mode: app.py's
  # own CONSOLE_AUTH_MODE default ("oidc") already does the right thing
  # with no config.json at all.
  if [ "$CONSOLE_AUTH_MODE_VAL" = "basic" ]; then
    if [ -f /opt/spinapp-ui/config.json ]; then
      echo "NOTE: /opt/spinapp-ui/config.json already exists -- did NOT"
      echo "create a $ADMIN_USERNAME_VAL account in it. Add one from /config"
      echo "(or the console's own login screen) once it's up, if needed."
    else
      ADMIN_ACCOUNT_CREATED=1
      ADMIN_PASSWORD_HASH="$(printf '%s' "$ADMIN_PASSWORD_VAL" | openssl passwd -apr1 -stdin)"
      mkdir -p /opt/spinapp-ui
      SPINDECK_ADMIN_USERNAME_FOR_PY="$ADMIN_USERNAME_VAL" \
      SPINDECK_ADMIN_PASSWORD_HASH_FOR_PY="$ADMIN_PASSWORD_HASH" \
      python3 -c '
import json, os

config = {
    "default_fqdn": "",
    "fqdns": [],
    "external_host": "",
    "peers": [],
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "console_allowed_users": [],
    "console_auth_mode": "basic",
    "console_basic_auth_users": [{
        "username": os.environ["SPINDECK_ADMIN_USERNAME_FOR_PY"],
        "password_hash": os.environ["SPINDECK_ADMIN_PASSWORD_HASH_FOR_PY"],
    }],
    "traefik_scheme": "http",
    "console_via_traefik": False,
}
with open("/opt/spinapp-ui/config.json", "w") as f:
    f.write(json.dumps(config, indent=2) + "\n")
'
      chmod 600 /opt/spinapp-ui/config.json
    fi
  fi

  if [ "$CONSOLE_AUTH_MODE_VAL" = "oidc" ] && [ "$OIDC_CLIENT_SECRET_VAL" = "changeme" ]; then
    echo
    echo "  *** /etc/spinapp-ui/env was written with PLACEHOLDER OIDC values. ***"
    echo "  *** Edit it with your real Keycloak details, then:                ***"
    echo "  ***   systemctl restart spinapp-ui                                ***"
  fi
fi

# ---------------------------------------------------------------------------
log "[14/15] systemd unit"
cp deploy/spinapp-ui.service /etc/systemd/system/spinapp-ui.service
systemctl daemon-reload
systemctl enable --now spinapp-ui

# ---------------------------------------------------------------------------
log "[15/15] Stuck-shim auto-recovery timer"
# Works around a known upstream containerd-shim-spin bug where a Spin app's
# shim process can get stuck and never exit after a failed launch, silently
# blocking every future Spin container start on the node until it's killed
# by hand -- see deploy/spin-shim-recovery.sh's own comments for the
# investigation this came out of. Detects the specific "CrashLoopBackOff
# with zero log output ever" signature and recovers automatically;
# harmless no-op otherwise.
cp deploy/spin-shim-recovery.sh /opt/spinapp-ui/spin-shim-recovery.sh
chmod +x /opt/spinapp-ui/spin-shim-recovery.sh
cp deploy/spin-shim-recovery.service /etc/systemd/system/spin-shim-recovery.service
cp deploy/spin-shim-recovery.timer /etc/systemd/system/spin-shim-recovery.timer
systemctl daemon-reload
systemctl enable --now spin-shim-recovery.timer

echo
echo "==> Done."
echo "SpinDeck should now be reachable at http://<this-host-ip>:8080/"
if [ -n "$ADMIN_ACCOUNT_CREATED" ]; then
  echo
  echo "  Console login (local account):"
  echo "    Username: $ADMIN_USERNAME_VAL"
  echo "    Password: $ADMIN_PASSWORD_VAL"
  if [ -n "$ADMIN_PASSWORD_GENERATED" ]; then
    echo "    (auto-generated -- write it down, it's not stored anywhere in"
    echo "    plaintext and won't be shown again. Change it from /config"
    echo "    any time.)"
  fi
fi
echo "Check status with: systemctl status spinapp-ui"

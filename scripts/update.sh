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
# Redeploys just the SpinDeck console (app.py, templates, the JS build
# template, the console's own RBAC, and the systemd unit) from this
# checkout onto an already-installed node, then restarts the service.
#
# This is the "I changed some code, now push it to a node" script -- for a
# from-scratch install (k3s, SpinKube, cert-manager, registry, Traefik,
# etc.) use install.sh instead (also safe to re-run, but much slower and
# touches the whole cluster, not just the console).
#
# Typical use on a peer node (see README's "Peer replication"): pull the
# latest commit, then run this.
#
#   cd spindeck && git pull
#   sudo ./scripts/update.sh
#
# Safe to re-run. Does NOT touch k3s/SpinKube/cert-manager/registry/Traefik
# or /etc/spinapp-ui/env -- only the console application layer. The one
# exception is deploy/k8s-manifests/spinapp-ui-rbac.yaml: that's the
# console's *own* ServiceAccount/Role/RoleBinding (what permissions the
# console itself runs with), not general cluster infrastructure, so a code
# change that needs a new permission (like sync_traefik_web_exposure()'s
# kube-system access) needs this reapplied to actually take effect --
# easy to forget as a separate manual step, so it's included here too now.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo ./scripts/update.sh" >&2
  exit 1
fi

# This script now lives in scripts/, one level below the repo root that
# deploy/, spinapp-ui/, js-template/, etc. are relative to -- ".." here
# resolves back up to that root regardless of where this script was
# invoked from.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [ ! -d /opt/spinapp-ui/venv ] || [ ! -f /etc/systemd/system/spinapp-ui.service ]; then
  echo "SpinDeck doesn't look installed on this host yet" >&2
  echo "(/opt/spinapp-ui/venv or the systemd unit is missing)." >&2
  echo "Run ./scripts/install.sh first, then use update.sh for future updates." >&2
  exit 1
fi

log() { echo -e "\n==> $*"; }

# One generation only -- app.py.bak/templates.bak are overwritten every
# run, not kept per-run-timestamped, so repeated updates don't quietly
# accumulate an ever-growing pile of old backups under /opt/spinapp-ui.
# Only ever gives you a rollback target for the update you *just* ran,
# not deeper history -- if you need to go back further than one step,
# use `git log`/`git checkout` on this checkout instead and re-run
# update.sh from there.
log "[1/7] Backing up current app.py + templates (app.py.bak/templates.bak)"
cp /opt/spinapp-ui/app.py /opt/spinapp-ui/app.py.bak
rm -rf /opt/spinapp-ui/templates.bak
cp -r /opt/spinapp-ui/templates /opt/spinapp-ui/templates.bak

log "[2/7] Python dependencies"
/opt/spinapp-ui/venv/bin/pip install --upgrade pip -q
/opt/spinapp-ui/venv/bin/pip install -q -r spinapp-ui/requirements.txt

log "[3/7] Console code (app.py + i18n.py + templates)"
# Fail before touching the live deployment if this checkout doesn't even
# parse -- much cheaper than finding out via a crash-looping systemd unit.
/opt/spinapp-ui/venv/bin/python -m py_compile spinapp-ui/app.py
cp spinapp-ui/app.py /opt/spinapp-ui/app.py
cp spinapp-ui/i18n.py /opt/spinapp-ui/i18n.py
cp spinapp-ui/backup_export.py /opt/spinapp-ui/backup_export.py
rm -rf /opt/spinapp-ui/templates
cp -r spinapp-ui/templates /opt/spinapp-ui/templates

log "[4/7] Console RBAC (ServiceAccount/Role/RoleBinding)"
# The console's own permissions, not general cluster infrastructure --
# see this script's own header comment for why this (alone, among
# deploy/k8s-manifests/*) is reapplied here rather than being install.sh-
# only. Same Role/RoleBinding/ServiceAccount each time this hasn't
# changed, so this is a no-op update in that case (kubectl apply is
# idempotent) -- it only matters when a code change actually needs a new
# permission. Uses the admin kubeconfig explicitly (not
# /etc/spinapp-ui/kubeconfig.yaml, the console's own *restricted* one --
# it obviously can't grant itself new permissions) the same way
# install.sh's own "Kubeconfig for spinapp-ui" step does.
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl apply -f deploy/k8s-manifests/spinapp-ui-rbac.yaml

log "[5/7] JS build template (New App)"
mkdir -p /opt/spinapp-ui/js-template-src
rm -rf /opt/spinapp-ui/js-template-src/hello-js
cp -r js-template /opt/spinapp-ui/js-template-src/hello-js
(cd /opt/spinapp-ui/js-template-src/hello-js && npm install --no-audit --no-fund --loglevel=error)

log "[6/7] Stuck-shim auto-recovery timer"
# Only if install.sh already set this up on this host (older installs
# won't have the timer unit yet, and this script never creates new
# systemd units from scratch -- see its own header comment) -- copying an
# updated script into place is still worth doing even then, so that's
# unconditional.
cp deploy/spin-shim-recovery.sh /opt/spinapp-ui/spin-shim-recovery.sh
chmod +x /opt/spinapp-ui/spin-shim-recovery.sh
if [ -f /etc/systemd/system/spin-shim-recovery.timer ]; then
  cp deploy/spin-shim-recovery.service /etc/systemd/system/spin-shim-recovery.service
  cp deploy/spin-shim-recovery.timer /etc/systemd/system/spin-shim-recovery.timer
fi

log "[7/7] systemd unit + restart"
cp deploy/spinapp-ui.service /etc/systemd/system/spinapp-ui.service
systemctl daemon-reload
systemctl restart spinapp-ui

echo
echo "==> Done. Previous app.py/templates kept as app.py.bak/templates.bak"
echo "    under /opt/spinapp-ui/ for a quick manual rollback if this update"
echo "    turns out to be broken (one generation back only)."
sleep 1
systemctl --no-pager --lines=0 status spinapp-ui

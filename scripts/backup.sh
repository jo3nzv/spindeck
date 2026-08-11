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
# Snapshots this node into a single tar.gz under /data/backup/, so a
# broken node can be replaced quickly by another one:
#
#   - Only the persistent app-data directories app.py itself actually
#     manages -- CONTAINER_DATA_BASE_DIR (every "container" app's
#     persistent hostPath volume: the actual runtime data an app has
#     written, e.g. a database's files -- not reconstructable from
#     config alone) and SITES_DATA_DIR (belt-and-suspenders -- "site"
#     apps' files are also already inside apps-export.json below via
#     site_export_payload(), so this is redundant, not the only copy).
#     Deliberately an *allow-list* of exactly these two, not "/data
#     minus known-unneeded folders": a $SPINDECK_DATA_DIR=/data install
#     (install.sh's own FRESH_INSTALL default) also puts k3s's own
#     15GB+ datastore, the local OCI registry's blobs, Traefik's log
#     files, and (if /data is its own filesystem) a lost+found/ directory
#     under /data -- none of which install.sh can't already recreate
#     from scratch (a fresh k3s + re-importing apps-export.json rebuilds
#     everything, registry blobs included, from source), and a blocklist
#     of "everything currently known to not matter" would only ever be
#     as complete as whatever happened to exist under /data on the last
#     node someone checked (confirmed live on a real peer node: also had
#     log/ and lost+found/ neither of which install.sh, or this comment
#     before this rewrite, knew to name). Reading the *actual configured*
#     paths (below) instead of assuming both live under /data at all
#     also covers an install that never set $SPINDECK_DATA_DIR (site
#     files then default to /opt/spinapp-ui/sites-data, not under /data).
#   - Every registered app's config, across all 5 kinds (Container/JS/
#     Site/Proxy/Cron) -- the same JSON /export/all downloads as a file
#     from the console UI, produced here via backup_export.py instead
#     (no HTTP/session available from a cron job). Also includes auth
#     secrets/hashes and (for "container" apps) the live Deployment+
#     Service YAML.
#   - /opt/spinapp-ui/config.json (console-wide settings: registered
#     FQDNs, peer list, SMTP, console login accounts, container registry
#     credentials, ...) and /etc/spinapp-ui/env (OIDC client secret,
#     peer token, Flask session secret, host IP, ...) -- everything
#     needed for a replacement node to come up as this node's continuation
#     rather than a blank install. The one value that's genuinely tied to
#     *this* physical/VM host rather than this SpinDeck instance's
#     identity is SPINAPP_UI_HOST_IP inside env -- update that by hand to
#     the new host's own LAN IP after restoring if it differs.
#
# Both config.json and env hold real secrets in plaintext (SMTP/registry
# passwords, OIDC client secret, peer token, console account hashes) --
# same as the two files themselves already are on disk (config.json is
# chmod 600; env should be root-only too) -- so treat this archive with
# the same care, especially anywhere it leaves this host.
#
# Usage: sudo ./scripts/backup.sh (from the repo root)
# Restore onto a fresh install: extract the tar.gz -- ./containers/<name>/
# entries go back under the new node's own CONTAINER_DATA_BASE_DIR
# (/data/containers unless overridden), ./sites-data/<name>/ under its
# SITES_DATA_DIR, config.json/env replace the fresh install's own copies
# at /opt/spinapp-ui/config.json and /etc/spinapp-ui/env (restart
# spinapp-ui after), apps-export.json is a file you upload via the
# console's own /import page (or paste its contents there) -- config.json
# alone doesn't recreate the apps themselves, just the console's own
# settings.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo ./scripts/backup.sh" >&2
  exit 1
fi

if [ ! -x /opt/spinapp-ui/venv/bin/python3 ] || [ ! -f /opt/spinapp-ui/backup_export.py ]; then
  echo "SpinDeck doesn't look installed on this host yet (or is older than" >&2
  echo "this script -- run ./scripts/update.sh first to deploy backup_export.py)." >&2
  exit 1
fi

BACKUP_DIR="/data/backup"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$BACKUP_DIR/spindeck-backup-$TIMESTAMP.tar.gz"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

log() { echo -e "\n==> $*"; }

mkdir -p "$BACKUP_DIR"

log "[1/2] Exporting registered apps (containers/js/sites/proxies/crons)"
# Same environment app.py itself runs under (KUBECONFIG in particular --
# a bare invocation without it silently talks to the wrong cluster
# credentials instead of failing loudly, see backup_export.py's own
# comment). Also where SPINAPP_UI_CONTAINER_DATA_DIR/SPINAPP_UI_SITES_
# DATA_DIR (if this install overrode either) come from, for step 2.
set -a
# shellcheck disable=SC1091
source /etc/spinapp-ui/env
set +a
/opt/spinapp-ui/venv/bin/python3 /opt/spinapp-ui/backup_export.py > "$WORKDIR/apps-export.json"
cp /opt/spinapp-ui/config.json "$WORKDIR/config.json"
cp /etc/spinapp-ui/env "$WORKDIR/env"

log "[2/2] Archiving persistent app data + console config"
# Matches app.py's own CONTAINER_DATA_BASE_DIR/SITES_DATA_DIR fallbacks
# exactly (see app.py, near CONTAINER_DATA_BASE_DIR/SITES_DATA_DIR).
CONTAINER_DATA_DIR="${SPINAPP_UI_CONTAINER_DATA_DIR:-/data/containers}"
SITES_DATA_DIR="${SPINAPP_UI_SITES_DATA_DIR:-/opt/spinapp-ui/sites-data}"

TAR_ARGS=()
if [ -d "$CONTAINER_DATA_DIR" ]; then
  TAR_ARGS+=(-C "$(dirname "$CONTAINER_DATA_DIR")" "$(basename "$CONTAINER_DATA_DIR")")
fi
if [ -d "$SITES_DATA_DIR" ]; then
  TAR_ARGS+=(-C "$(dirname "$SITES_DATA_DIR")" "$(basename "$SITES_DATA_DIR")")
fi
TAR_ARGS+=(-C "$WORKDIR" apps-export.json config.json env)

tar -czf "$ARCHIVE" "${TAR_ARGS[@]}"
chmod 600 "$ARCHIVE"

echo
echo "==> Done: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

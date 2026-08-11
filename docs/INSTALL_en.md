# Installation Guide

📖 [日本語版はこちら](INSTALL.md) / Japanese version here

This guide walks through building the **entire system** (a single-node
k3s/SpinKube cluster plus the SpinDeck console) on a fresh, nothing-yet-
installed **Ubuntu 24.04 / 26.04** host (VM, LXC container, or bare
metal — any of these work). This repository's `install.sh` automates
every step below in one shot. It's safe to run repeatedly — each step
either skips work that's already done or just re-applies the same
content, so if it fails partway through, running `./scripts/install.sh` again
picks up where it left off.

## Quick start

```bash
sudo apt-get update && sudo apt-get install -y git
git clone <this repository's URL> spindeck
cd spindeck
sudo ./scripts/install.sh
```

That's it — the cluster and console will be fully working. It takes
roughly 10-20 minutes depending on your network speed.

Along the way, it interactively asks for your public hostname and login
method. The default is the console's own built-in local accounts
(username/password) — just press Enter through the prompts and it
creates one account with username `admin` and an auto-generated
password, shown on screen once installation finishes (the password is
never stored anywhere in plaintext, so be sure to note it down — you can
also change it later from `/config`). If you want to use Keycloak or
another OIDC login provider, choose `o` at the prompt and enter your
Keycloak details right there (having a Keycloak client already set up
beforehand makes this go smoothly — see "Setting up Keycloak" below).

### Non-interactive install

Setting the following environment variables before running `install.sh`
skips the interactive prompts (handy for scripted or CI-driven
provisioning). For local accounts (the default):

```bash
export SPINDECK_EXTERNAL_HOST=apps.example.com
export SPINDECK_CONSOLE_AUTH_MODE=basic     # this is already the default, so you can actually omit it
export SPINDECK_ADMIN_USERNAME=admin        # defaults to "admin" if omitted
export SPINDECK_ADMIN_PASSWORD=              # auto-generated if omitted (shown on screen)
sudo -E ./scripts/install.sh
```

For Keycloak (OIDC):

```bash
export SPINDECK_EXTERNAL_HOST=apps.example.com
export SPINDECK_CONSOLE_AUTH_MODE=oidc
export SPINDECK_OIDC_ISSUER=https://auth.example.com/realms/myrealm
export SPINDECK_OIDC_CLIENT_ID=https://admin.example.com
export SPINDECK_OIDC_CLIENT_SECRET=your-real-secret
export SPINDECK_OIDC_REDIRECT_URI=https://admin.example.com/oidc/callback
sudo -E ./scripts/install.sh
```

(`sudo -E` carries these environment variables through to the `sudo`'d
process.)

### Where data lives (defaults to `/data`)

**On a fresh install, by default**, k3s itself, the local registry,
Traefik's access log, New Site uploads, and Container apps' persistent
data are all gathered under `/data` (`/data/k3s`, `/data/registry`,
`/data/traefik-logs`, `/data/sites-data`, `/data/containers`) instead of
being scattered across `/var/lib/rancher/k3s`,
`/var/lib/spinkube-registry`, `/var/log/traefik`,
`/opt/spinapp-ui/sites-data`, and `/data/containers`. This suits hosts
that have a separate disk mounted at `/data`.

To change this default, set `SPINDECK_DATA_DIR` explicitly before
running `install.sh` (either to a different path, or to an empty string
to fall back to the old scattered layout):

```bash
export SPINDECK_DATA_DIR=/mnt/bigdisk   # gather everything somewhere else
# or
export SPINDECK_DATA_DIR=              # don't gather anything, keep the old layout
sudo -E ./scripts/install.sh
```

**The `/data` default only applies on a fresh install** — re-running
`install.sh` on a host that already has k3s installed leaves data
locations unchanged unless you explicitly set `SPINDECK_DATA_DIR` (this
is so a script that's safe to re-run never moves a running host's data
around just because a default value changed). Even if you explicitly set
`SPINDECK_DATA_DIR` and re-run on an existing host, k3s's own data
location (`--data-dir`) can only be set the first time `k3s server`
starts, so that one doesn't move (the other four — registry, Traefik
logs, site data, and container data — can be moved on an existing host
too, by setting it explicitly and re-running `install.sh`).

> **Note if `/etc/spinapp-ui/env` already exists**: `SPINDECK_DATA_DIR`
> also needs to tell the console itself where Traefik's access log lives
> (`SPINAPP_UI_TRAEFIK_ACCESS_LOG`), where New Site uploads go
> (`SPINAPP_UI_SITES_DATA_DIR`), and where Container apps' persistent
> data lives (`SPINAPP_UI_CONTAINER_DATA_DIR`) — but these are only
> written automatically when `/etc/spinapp-ui/env` **doesn't exist
> yet** (so `install.sh` never rewrites an existing config file on its
> own). If you run `install.sh` (as a fresh install) while this file
> already exists, it prints guidance for which lines to add by hand.
> Forgetting this sync shows up as a confusing symptom — some access
> traffic missing from `/config`'s "Analytics" — rather than an error
> (it silently keeps reading a stale or nonexistent path instead).

### Incoming IPv6 support (dual-stack by default)

**On a fresh install, by default**, k3s, Traefik, and the console itself
(direct `:8080` access) all listen on IPv6 in addition to IPv4 (IPv4
keeps working exactly as before — this never becomes IPv6-only).
Installing on a host with no outbound IPv6 route is harmless (you'll
just see a related note in the output).

To disable this default, explicitly set an empty string before running
`install.sh`:

```bash
export SPINDECK_DUAL_STACK=
sudo -E ./scripts/install.sh
```

**The dual-stack default also only applies on a fresh install** — for
the same reason as `SPINDECK_DATA_DIR`: k3s's cluster-cidr/service-cidr
can only be set the first time `k3s server` starts, and Traefik's
network configuration shouldn't be silently changed on a running host
either, so a host that already has k3s installed stays IPv4-only unless
you explicitly set `SPINDECK_DUAL_STACK`. Pod/Service IPv6 addresses use
a fixed ULA range (`fd42::/56`, `fd43::/112`) and reach the outside world
through NAT to the node's own real IPv6 address. The egress
`NetworkPolicy` (the "pivot point" mitigation described above)
automatically gets its IPv6 block rules added too.

On a host installed fresh with dual-stack, Traefik is reachable over
HTTPS (port 443) via its IPv6 address, and deployed apps are reachable
correctly through it.

### Choosing a login method (local accounts vs. Keycloak)

The console's login method is chosen at the `install.sh` prompt, and can
be switched at any time afterward from `/config`'s "コンソールのログイン方式"
("Console login method") section:

- **Local accounts** — The default method. For environments that can't
  or don't want to set up an external identity provider, the console
  itself holds multiple username/password accounts. No Keycloak setup
  needed at all. Just pressing Enter through the `install.sh` prompt
  selects this and creates one admin account with username `admin` and
  an auto-generated password (shown on screen once installation
  finishes — be sure to note it down; both the username and password can
  be changed later from `/config`).
- **Keycloak (OIDC)** — If you already have Keycloak (or another
  OIDC-compliant product) running, choose `o` at the `install.sh` prompt
  to switch to this. See "Setting up Keycloak" below for what to
  prepare beforehand.

If you want to switch between local accounts and Keycloak after
installing, switching *to* "local accounts" requires at least one
account to already exist under `/config`'s "ローカルアカウント管理"
("Local account management") (a guard against ending up in a state where
no one can log in). Be sure to add another account before deleting the
first admin account `install.sh` created.

If you're using Keycloak, prepare the following beforehand:

1. In the Keycloak admin console, create one **confidential client** in
   the realm SpinDeck will use.
2. For the Client ID, it's simplest and recommended to use the exact URL
   SpinDeck will be published at (e.g. `https://admin.example.com`).
3. Register `https://admin.example.com/oidc/callback` under **Valid
   Redirect URIs** (include the trailing `/oidc/callback`). If you plan
   to stand up new nodes repeatedly going forward, register the
   bootstrap URI from "Bootstrapping a new node" below at the same time,
   so you never need to touch Keycloak again per node.
4. Note down the issued Client Secret.

> SpinDeck handles a Client ID containing a colon (`:`) correctly — which
> it always will if you use the URL directly — by forcing the
> `client_secret_post` method for client authentication (avoiding the
> problem where HTTP Basic client auth would otherwise split
> `user:pass` on the first colon).

### Bootstrapping a new node (when the OIDC redirect URI is https)

Setting `SPINAPP_UI_OIDC_REDIRECT_URI` to `https://...` is the normal
approach, but a freshly stood-up node has no custom domain, certificate,
or HTTPS set up yet — https isn't reachable. If the redirect URI is set
to https at this stage, logging into Keycloak itself would succeed, but
the callback (which needs https) can't be reached — a chicken-and-egg
problem.

To avoid this, proceed in two stages:

1. **Bootstrap**: Register `http://<node's IP>:8080/oidc/callback` under
   the Keycloak client's Valid Redirect URIs, alongside the production
   https URI. At install time, use
   `SPINDECK_OIDC_REDIRECT_URI=http://<node's IP>:8080/oidc/callback`
   for this, to get to a working login state first.
2. **Switch to production**: Once you can log in, finish the initial
   setup you need (registering an FQDN, placing a certificate, switching
   on HTTPS). Once that's settled, change the redirect URI to the
   production `https://...` one from `/config`'s "Keycloak (OIDC)"
   section (saving automatically restarts the service, and logins use
   https from then on).

With this approach, you only ever need to register two URIs in Keycloak
once — standing up any number of nodes afterward never requires touching
Keycloak again.

## What the script does

`install.sh` runs the following 15 steps in order. If it fails partway
through, just re-run it following whatever message is shown.

1. **apt prerequisites** — Installs `curl`, `ca-certificates`, `gnupg`,
   `git`, `cron`, `python3-venv`, `python3-pip`, and `openssl`.

2. **Node.js 22.x** — Installed via the NodeSource repository (needed
   for the JS→Wasm build pipeline, `esbuild`/`jco`).

3. **k3s** — Installs k3s using `deploy/k3s/k3s-config.yaml`
   (`KubeletInUserNamespace=true` — works around the missing `/dev/kmsg`
   issue on LXC containers, harmless on other environments) and
   `deploy/k3s/registries.yaml` (trusts the local registry
   `localhost:5000` as insecure HTTP). If `SPINDECK_DATA_DIR` is set,
   appends `data-dir: $SPINDECK_DATA_DIR/k3s` to this config file before
   installing. If `SPINDECK_DUAL_STACK` is set, also appends the
   dual-stack `cluster-cidr`/`service-cidr`/`cluster-dns` settings here.

4. **Spin CLI + containerd-shim-spin-v2** — Downloaded directly from
   their respective GitHub releases (pinned to the same versions this
   repository was built and verified against: Spin v4.0.2, shim
   v0.25.1). Wired into containerd via
   `deploy/k3s/containerd-config.toml.tmpl` (which also works around
   containerd 2.1+'s Transfer Service breaking `wasi/wasm` image pulls).
   k3s is restarted afterward.

5. **Helm** — Installed via the official `get-helm-3` script.

6. **cert-manager v1.20.0** — Installed because spin-operator's webhook
   certificates need it.

7. **SpinKube** — Installs the `SpinApp`/`SpinAppExecutor` CRDs, the
   `wasmtime-spin-v2` RuntimeClass, a `containerd-shim-spin`
   `SpinAppExecutor` instance (`deploy/k8s-manifests/spinkube-crds.yaml`/
   `spinkube-runtime.yaml` — extracted from an actually-running v0.6.1
   environment rather than fetched at install time, so there's no need
   to hunt down the correct upstream release-asset URLs), and the
   `spin-operator` v0.6.1 itself via its Helm chart.

8. **Local OCI registry** — Installs a plain `registry:2` Deployment
   with `hostNetwork: true`. `REGISTRY_HTTP_ADDR` binds it to
   `127.0.0.1:5000` only, so the node's own containerd can reach it but
   Pods or the wider network cannot. No auth or TLS (fine, since it's
   genuinely `localhost`-only). Image storage is applied from
   `deploy/k8s-manifests/registry.yaml.tmpl`, defaulting to
   `/var/lib/spinkube-registry`, or `$SPINDECK_DATA_DIR/registry` if
   `SPINDECK_DATA_DIR` is set.

9. **Egress `NetworkPolicy`** — Restricts every deployed SpinApp/CronJob
   Pod's outbound traffic to the public internet and your own LAN, while
   blocking access to this cluster's own internals (the k8s API server,
   other ClusterIP services) and cloud metadata endpoints. Applied from
   `deploy/k8s-manifests/spinapp-egress-policy.yaml.tmpl`, with
   `__NODE_IP__` replaced by this node's own LAN IP (auto-detected via
   `ip -4 route get 1.1.1.1`). See that file's own comments for why this
   is needed and why it's scoped this way — it closes off the "this
   system itself becomes a pivot point" risk that's inherent to any
   self-service code-execution feature. If `SPINDECK_DUAL_STACK` is set,
   the equivalent IPv6 block rules are added too (omitted entirely if
   unset, or if this node's IPv6 can't be auto-detected).

10. **Traefik** — Enables JSON access logs, configures it to trust
    `X-Forwarded-For` from the Pod CIDR (needed because k3s's built-in
    load balancer SNATs connections), and writes the access log to a
    **persistent** hostPath file (`/var/log/traefik/access.log` by
    default, or `$SPINDECK_DATA_DIR/traefik-logs/access.log` if
    `SPINDECK_DATA_DIR` is set; owned by UID 65532 since Traefik's
    container runs as non-root) so log history survives Traefik Pod
    restarts. If `SPINDECK_DUAL_STACK` is set, also sets Traefik's
    Service to `ipFamilyPolicy: PreferDualStack` so it listens on IPv6
    too.

11. **The SpinDeck console itself** — Creates a Python venv at
    `/opt/spinapp-ui/venv`, copies `app.py` and all templates into
    `/opt/spinapp-ui/`, copies the JS build template into
    `/opt/spinapp-ui/js-template-src/hello-js/`, and runs `npm install`
    there once (the console hardlink-copies this directory for every
    subsequent app deploy, so `npm install` is only ever needed this
    one time).

12. **Least-privilege kubeconfig for the console** — Applies
    `deploy/k8s-manifests/spinapp-ui-rbac.yaml` (a dedicated
    `ServiceAccount`+`Role`+`RoleBinding` scoped to only the resource
    kinds the console actually uses, within the `default` namespace),
    and generates `/etc/spinapp-ui/kubeconfig.yaml` (mode 600) from that
    ServiceAccount's token. From this point on, the console runs under
    this least-privilege kubeconfig instead of
    `/etc/rancher/k3s/k3s.yaml` (cluster-wide admin). Safe to re-run —
    it just produces the same result again.

13. **Writing the config file** — Writes `/etc/spinapp-ui/env` (from
    interactive input, `SPINDECK_*` environment variables, or
    placeholders you can edit later). Completely skipped if this file
    already exists, so re-running the script never overwrites real,
    already-configured secrets (the switch to the newly-generated
    least-privilege kubeconfig from step 12 also isn't applied to an
    existing env file automatically — follow the printed guidance to
    switch it by hand). If `SPINDECK_DATA_DIR` is set, also writes
    `SPINAPP_UI_SITES_DATA_DIR=$SPINDECK_DATA_DIR/sites-data` as the
    destination for New Site uploads.

14. **systemd unit** — Installs and starts the `spinapp-ui` service.

15. **Stuck-Wasm-shim auto-recovery timer** — Places
    `deploy/spin-shim-recovery.sh` under `/opt/spinapp-ui/` and enables
    `spin-shim-recovery.timer` (every 15 minutes). A safety net that
    automatically detects and recovers from a known bug in the
    `containerd-shim-spin` dependency (an external project), where a
    process can stick around after a failed container launch and
    silently block every subsequent Wasm container launch as collateral
    damage. See `deploy/spin-shim-recovery.sh`'s own comments for
    details.

## After installing

- Open `http://<your server's IP>:8080/` — with the default (local
  accounts), log in with the username/password shown on screen when
  installation finished. If you chose Keycloak, you'll be redirected to
  log into Keycloak (leaving the placeholder values in place causes an
  OIDC error — if that happens, edit `/etc/spinapp-ui/env` and run
  `systemctl restart spinapp-ui`).
- Once logged in, register a domain from `/config` — the first one you
  register becomes the default domain shown on the dashboard/preview,
  and each app can optionally be pinned to any of the registered ones.
- If something looks wrong, start by checking status with these
  commands:
  - `systemctl status spinapp-ui` — is the console itself running?
  - `kubectl get nodes` — is the k3s cluster itself healthy?
  - `kubectl get pods -A` — are every component's Pods (Traefik,
    cert-manager, spin-operator, etc.) `Running`?
  - `journalctl -u spinapp-ui -f` — watch the console's logs live
- If this is your first time using SpinDeck itself (how to create an
  app, how to set up authentication, etc.), see
  [`docs/GUIDE_en.md`](GUIDE_en.md) — a beginner-friendly walkthrough
  of exactly what to click and when.

## Uninstalling

```bash
systemctl disable --now spinapp-ui
rm -rf /opt/spinapp-ui /etc/spinapp-ui /etc/systemd/system/spinapp-ui.service
/usr/local/bin/k3s-uninstall.sh   # tears down the entire cluster (including whatever data-dir was configured)
```

This stops the `spinapp-ui` service, removes its related directories,
and tears down the entire cluster via `k3s-uninstall.sh` (an uninstall
script k3s generates automatically). All deployed apps' data (under
`/opt/spinapp-ui`, registry images, etc.) is lost too, so back up
anything you need first.

If you had `SPINDECK_DATA_DIR` set (the default on a fresh install is
`/data`), `k3s-uninstall.sh` removes k3s's own data from whatever
`data-dir` was configured, but the registry, Traefik logs, New Site
files, and Container apps' persistent data
(`$SPINDECK_DATA_DIR/{registry,traefik-logs,sites-data,containers}`)
are just plain directories outside of k3s's own management, so
`k3s-uninstall.sh` doesn't remove them. Delete them by hand with `rm -rf`
if you don't need them.

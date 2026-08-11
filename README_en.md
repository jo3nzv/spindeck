# SpinDeck

📖 [日本語版はこちら](README.md) / Japanese version here

**A self-service web console for publishing apps from nothing but your
browser — no `kubectl` required.** Under the hood it uses
[SpinKube](https://www.spinkube.dev/) (a platform for running WebAssembly
/ Wasm apps on Kubernetes), and automates the whole build → deploy →
publish pipeline as soon as you paste in your code.

Current version: **1.0.1** (also shown in the admin console's sidebar;
see the [release notes](RELEASE_NOTES.md)). Versioning follows
[semver](https://semver.org/). Released under the
[Apache License 2.0](LICENSE).

## What is SpinDeck?

Whenever you want to stand up "just one small API," "a simple frontend
for an internal tool," or "a single scheduled batch job," you'd normally
need to learn a pile of things first: provisioning a server, containerizing
the app, writing Kubernetes manifests, configuring routing, managing
certificates. SpinDeck folds all of that into a single web console, so you
can do it **without any Kubernetes knowledge at all**.

- Paste your code into the in-browser editor, click Save, and your app is
  live within seconds to tens of seconds.
- No SSH or command-line work is needed beyond the initial install —
  everything, including the `/config` page, is operated entirely from the
  web UI.
- Authentication (password protection, integration with your existing
  login provider), custom domains, HTTPS, access analytics, and
  multi-node availability are all built in and ready for real production
  use.

Install it on a single server (VM, LXC container, or bare metal — any of
these work) and you can have your first app live the same day.

## Key features

### Five ways to create an app

- **New App** — Paste [Cloudflare Workers](https://developers.cloudflare.com/workers/)-style
  JavaScript into a VS Code-like in-browser editor (Monaco), and it's
  automatically built and deployed as a Wasm component. Great for small
  APIs or webhook receivers.
- **New Site** — Write a multi-file HTML/CSS/JS site directly in the
  browser, no build step required (subdirectories supported). Include a
  `.php` file and it automatically switches to a configuration that runs
  PHP, with no extra setup. Binary file uploads (images, PDFs, etc.) are
  also supported.
- **New Proxy** — Relay (reverse-proxy) requests through SpinDeck to
  another server or system already running on your network. Choose
  per-path behavior ("pass through as-is," "return 404," "replace with a
  different path's content"), plus advanced rules that rewrite URLs
  appearing in the response.
- **New Cron** — Run a Python or bash script on a schedule as a
  Kubernetes `CronJob` (since SpinKube itself has no cron/timer feature,
  this is the one kind that runs as a plain container instead of Wasm).
  Sending mail (`send_mail(to, subject, body)`) and fetching a URL
  (bash's `download(url, destination)`) work out of the box, no library
  needed.
- **New Container** — Run any Docker image as-is, as a regular
  container (`Deployment`) that bypasses the Wasm sandbox entirely. You
  can configure CPU/memory limits, an overridden start command (for
  images whose default command exits immediately), environment
  variables, replica count (automatically load-balanced), a dedicated
  domain (leave it blank to skip external exposure entirely and use the
  app only from its own 🖥️ console tab — in that case no port/Service is
  created either), whether HTTPS is terminated by SpinDeck or passed
  straight through to the container, persistent storage you can toggle
  on/off with a configurable mount path (`/data` by default), and
  per-app Basic/OIDC authentication (terminate mode only — the domain
  field stays free-form). Adding **sidecar containers** lets you deploy
  multiple cooperating containers in the same Pod, like WordPress +
  MySQL (they can talk to each other over `localhost`, and each gets its
  own independent persistent-storage mount path), all without being
  subject to the egress policy that otherwise restricts pod-to-pod
  traffic inside the cluster. You can also drop into an interactive
  in-browser terminal (equivalent to `kubectl exec`) or a log-viewer tab
  to work with the container directly, or edit the raw Deployment/Service
  YAML for anything a form can't express (fine-grained multi-container
  tuning, extra volumes, etc.). Unlike the other kinds, this one runs a
  public image verbatim, so only point it at images you trust.

New App/Site/Proxy/Cron all come with an **in-browser live preview** and
are published at `https://<your public hostname>/<app-name>/` via an
automatically-managed Traefik route. New Container is different: it's
published as an independent host with its own dedicated domain (see
[`docs/GUIDE_en.md`](docs/GUIDE_en.md) for details).

### Authentication & access control

- **Per-app HTTP Basic auth** — Simple protection with a single
  username/password pair.
- **Per-app OIDC auth** — Reuse the console's own Keycloak login, and
  specify a list of usernames allowed to view a given app.
- **Per-app dedicated domain (FQDN)** — Bind an app to a specific
  domain, independent of the console's own login.
- **Restricting who can log into the console itself** — From `/config`,
  restrict console login to a specific list of usernames (by default,
  anyone who can log into the configured Keycloak realm can administer
  it).
- **Choice of console login method** — Local username/password accounts
  are the default (one admin account is created automatically when you
  run `install.sh`, and more can be added), with the option to switch to
  an OIDC identity provider like Keycloak if you have one available.
  Switchable either way from `/config` at any time.

### Operations

- **Analytics dashboard** — Request counts, error rates, response
  times, top destinations, and a per-app status breakdown, all derived
  purely from Traefik's access log with no separate monitoring stack
  required.
- **Native HTTPS support** — Traefik itself can terminate TLS directly
  (the "HTTPS" section under `/config`). Just place a certificate/key
  file on the server and switch it on — replacing the files later is
  picked up automatically, no restart needed. The admin console itself
  can optionally be published the same way too (the default remains
  direct access on `:8080` only).
- **Peer replication** — Register other SpinDeck nodes from `/config`
  and app/site/proxy deploy, edit, and delete operations are
  automatically mirrored to them (for availability). Each node stays a
  fully independent, standalone k3s/SpinKube environment — the clusters
  themselves are never merged, only deploy/delete operations are
  synced.
- **Automatic recovery from stuck Wasm runtime processes** —
  `containerd-shim-spin` (an external project this depends on) has a
  known bug where, after a failed container launch, its process can fail
  to exit cleanly and stick around, silently blocking every subsequent
  Wasm container launch on the node as collateral damage.
  `spin-shim-recovery.timer`, which runs every 15 minutes, detects any
  app stuck in a CrashLoopBackOff loop with zero log output ever, and
  automatically cleans up the stuck process and restarts the app.
- Other settings — the public hostname, registered domains, and more —
  can all be changed from the **`/config`** page with no redeploy
  needed.

### Security

- **Secure by default** — CSRF protection, secure session cookies,
  security headers, and brute-force protection (attempt limiting) on
  console login are all built in.
- **The console itself runs with least privilege** — The console
  process that runs `kubectl` doesn't hold cluster-wide admin
  permissions; it runs under a dedicated ServiceAccount/Role scoped to
  only the namespace and resource kinds it actually uses. Even if the
  console itself ever had a vulnerability, the blast radius stays
  minimal.
- **Egress restriction via `NetworkPolicy`** — Every deployed app's and
  CronJob's outbound traffic is restricted to the public internet and
  your own LAN, unable to reach the Kubernetes cluster's own internals
  (the API server, other services) or a cloud metadata endpoint. This
  contains, at the network level, the "pivot point" risk that's
  inherent to any feature that lets users run their own code.
- **Guardrails on New Container's raw YAML editing too** — Settings
  that could escape to the host — `privileged`, sharing the host's
  network/PID/IPC namespaces, an unmanaged `hostPath` mount, etc. — are
  rejected even in free-edit mode.
- The container registry is bound to `localhost` only and is
  unreachable from outside or from the pod network.

## Quick start

On a fresh Ubuntu 24.04 or 26.04 host (VM, LXC container, or bare metal —
any of these work):

```bash
sudo apt-get update && sudo apt-get install -y git
git clone <this repository's URL> spindeck
cd spindeck
sudo ./scripts/install.sh
```

This automatically installs and configures the entire stack: k3s,
SpinKube, the local container registry, Traefik, and the console itself.
It takes roughly 10-20 minutes depending on your connection speed.

Once installation finishes, open `http://<your server's IP>:8080/` and
log in. For step-by-step details and the Keycloak (OIDC) setup this
needs, see [`docs/INSTALL_en.md`](docs/INSTALL_en.md); for how to actually use it,
see [`docs/GUIDE_en.md`](docs/GUIDE_en.md).

## Requirements

- Ubuntu 24.04 or 26.04 (VM, LXC container, or bare metal all work;
  other distributions are untested)
- Internet connectivity (needed to fetch packages and container images)
- Two login methods to choose from. The default is the console's own
  built-in local accounts (username/password, with an admin account
  created automatically when you run `install.sh`) — no Keycloak needed.
  If you have an OIDC-compliant login provider available (built with
  [Keycloak](https://www.keycloak.org/) in mind, but any OIDC-compliant
  product should work), you can switch to that instead, and switch back
  and forth from `/config` at any time.
- A minimal setup fits on a single machine (it runs on as little as 2
  CPU cores / 4GB RAM, but give it more headroom depending on how many
  apps you run and how much load they see).

## Architecture

```
                 ┌─────────────────────────────────────────┐
   Browser  ────▶ │ Traefik (k3s's built-in ingress)          │
                 │  Per-app route: /<name>/... → Service      │
                 └───────────────┬───────────────────────────┘
                                 │
                 ┌───────────────▼───────────────────────────┐
                 │ k3s (single node) + SpinKube                │
                 │  SpinApp CRD → Wasm component               │
                 │  (Spin/containerd-shim-spin),                │
                 │  pulled from the local OCI registry          │
                 │  (localhost:5000)                            │
                 │  * New Container is the only kind that runs  │
                 │    as a plain Deployment (no Wasm involved)  │
                 └───────────────┬───────────────────────────┘
                                 │ kubectl / spin registry push
                 ┌───────────────▼───────────────────────────┐
                 │ SpinDeck console (systemd, Flask)            │
                 │  /opt/spinapp-ui — builds JS/sites into      │
                 │  Wasm, deploys them as SpinApps, and         │
                 │  manages routing                             │
                 └───────────────────────────────────────────┘
```

The console itself (`app.py`) doesn't run as a Kubernetes Pod — it runs
directly on the target host as a systemd service. It builds whatever code
a user wrote into a Wasm component on the spot, pushes it to the local
registry, then calls the Kubernetes (k3s) API to create/update the
SpinApp/Service/IngressRoute and so on (New Container is the one
exception: no build step at all, it just creates/updates a `Deployment`
directly from the specified public Docker image).

## Configuration

Runtime configuration lives in two places: `/etc/spinapp-ui/env`
(Keycloak, registry, namespace, etc. — see
[`deploy/env.example`](deploy/env.example) for every variable), and the
console's own **`/config`** page (registered domains, HTTPS,
authentication, peer nodes, and other settings that don't need a
restart to take effect).

## Documentation

- [`docs/INSTALL_en.md`](docs/INSTALL_en.md) — Detailed installation walkthrough:
  what each step of `install.sh` does, how to run it non-interactively,
  and how to set up Keycloak.
- [`docs/GUIDE_en.md`](docs/GUIDE_en.md) — User guide: a gentle
  explanation of every feature, for anyone new to the system.

## Repository layout

```
scripts/
  install.sh                 One-shot installer for a fresh Ubuntu 24.04/26.04 host
  update.sh                  Redeploys app.py/templates/js-template to an
                            already-installed node (a peer, etc.) and
                            restarts it -- for rolling out code changes
                            (not for a fresh install)
  backup.sh                  Bundles /data (persistent data) and every
                            registered app's config into a tar.gz under
                            /data/backup/
  upgrade.sh                 Updates OS packages + k3s/cert-manager/SpinKube
                            operator to their latest versions (Traefik has
                            no separate step, it comes bundled with k3s).
                            The highest-blast-radius script here, since it
                            affects the whole cluster
  healthcheck.sh              End-to-end smoke test: actually deploys and
                            deletes a disposable instance of every app kind
                            (container/js/site/proxy/cron), and re-checks
                            known regressions (CPU/Recreate/extra-ports/
                            SSRF boundary). Meant to run after upgrade.sh
docs/
  INSTALL.md / INSTALL_en.md Detailed walkthrough of what install.sh does
                            (Japanese/English)
  GUIDE.md / GUIDE_en.md     User guide -- what to actually do once
                            installation is done (Japanese/English)
spinapp-ui/                The Flask console itself (app.py + templates/)
js-template/               JS→Wasm build template used by "New App"
deploy/
  spinapp-ui.service        systemd unit
  env.example                Config file template (fill in real values)
  k3s/                      k3s + containerd configuration
  k8s-manifests/            SpinKube's CRDs/RuntimeClass, the local
                            registry, Traefik config, the egress
                            NetworkPolicy, and the console's own
                            least-privilege RBAC
```

## License

Released under the [Apache License 2.0](LICENSE). See [`NOTICE`](NOTICE)
for the copyright notice.

The major software this project depends on
([SpinKube](https://www.spinkube.dev/), [Spin](https://www.fermyon.com/spin),
[k3s](https://k3s.io/), [Traefik](https://traefik.io/traefik/), and
various Python libraries built on Flask) is each distributed under its
own separate license. This repository automates installing and
configuring them — it does not bundle, modify, or redistribute them
itself.

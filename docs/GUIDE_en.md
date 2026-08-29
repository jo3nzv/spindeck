# SpinDeck User Guide

📖 [日本語版はこちら](GUIDE.md) / Japanese version here

This guide is an introduction for anyone new to SpinDeck. For
installation itself, see [`INSTALL_en.md`](INSTALL_en.md). Here we
cover "what you can actually do, and how, once installation is done,"
explained for people who aren't already familiar with Kubernetes or
Wasm.

## Table of Contents

1. [What does SpinDeck do?](#what-does-spindeck-do)
2. [The basic idea](#the-basic-idea)
3. [Logging in for the first time](#logging-in-for-the-first-time)
4. [Creating an app](#creating-an-app)
   - [New App (JavaScript)](#new-app-javascript)
   - [New Site (static site / PHP)](#new-site-static-site--php)
   - [New Proxy (reverse proxy)](#new-proxy-reverse-proxy)
   - [New Cron (scheduled job)](#new-cron-scheduled-job)
   - [New Container (Docker container)](#new-container-docker-container)
5. [Protecting an app (authentication)](#protecting-an-app-authentication)
6. [Publishing under a custom domain (FQDN)](#publishing-under-a-custom-domain-fqdn)
7. [Publishing over HTTPS](#publishing-over-https)
8. [Protecting the console itself (login method, access restriction)](#protecting-the-console-itself-login-method-access-restriction)
9. [Viewing access analytics](#viewing-access-analytics)
10. [Multi-node setups (peer replication)](#multi-node-setups-peer-replication)
11. [Exporting and importing apps](#exporting-and-importing-apps)
12. [Backups (`backup.sh`)](#backups-backupsh)
13. [Health checks (`healthcheck.sh`)](#health-checks-healthchecksh)
14. [Troubleshooting](#troubleshooting)
15. [Glossary](#glossary)

---

## What does SpinDeck do?

SpinDeck is **a web console that lets you publish apps from nothing but
your browser, without ever touching the server directly (Kubernetes,
the command line, none of it)**.

- Paste your code into the in-browser editor, and your app is
  automatically built and deployed, published at a URL like
  `https://your-domain/app-name/`.
- You can create five kinds of apps: "JavaScript code," "a static
  HTML/CSS/JS (+ PHP) site," "a relay to another server (reverse
  proxy)," "a scheduled Python/bash script," and "any Docker image."
- Password protection, or authentication via your organization's login
  provider (Keycloak), can both be turned on from the UI with no extra
  setup. The console's own login can likewise use its built-in
  username/password accounts instead of Keycloak.
- Under the hood, whatever code arrives gets built and run via
  [SpinKube](https://www.spinkube.dev/) (a Kubernetes extension that
  runs apps in a lightweight execution format called WebAssembly, or
  Wasm). You don't need to know any of these details to use SpinDeck.

In short: it's an in-house PaaS (Platform as a Service) for "I want to
build a small API or site and get it published right away," usable with
zero infrastructure knowledge.

## The basic idea

There are three ideas worth knowing before you start using SpinDeck.

### 1. There are five kinds of "app"

| Kind | What it does | When to use it |
| --- | --- | --- |
| **New App** | Runs JavaScript code | A small API, a webhook receiver, Cloudflare-Workers-style processing |
| **New Site** | Publishes a set of HTML/CSS/JS (+ PHP) files | A static site, a simple frontend for an internal tool |
| **New Proxy** | Relays requests to another server | You want to expose an already-running internal system through SpinDeck |
| **New Cron** | Runs a Python/bash script on a schedule | A daily batch job, sending a scheduled email, polling an external API |
| **New Container** | Runs any Docker image as-is | You want to run a published Docker image verbatim, or need a setup the other kinds can't express |

New App/Site/Proxy/Cron all run on top of SpinKube (WebAssembly), but
New Container runs as a plain Docker container instead (see
[New Container (Docker container)](#new-container-docker-container) for
details).

### 2. How an app gets published

New App/Site/Proxy/Cron are all published under one shared URL:
`https://<public hostname>/<app name>/`. For example, if the public
hostname is `apps.example.com` and the app is named `hello`, that's
`https://apps.example.com/hello/`. This "public hostname" is configured
and changed from the `/config` page (more on this below). If you need
to, you can also assign a dedicated domain to an individual app (see
[Publishing under a custom domain (FQDN)](#publishing-under-a-custom-domain-fqdn)).

New Container is a bit different — it's published by occupying an
entire dedicated domain (it doesn't hang off a path). If you don't
specify a dedicated domain, it can also be used as a "compute-only"
container that's never exposed externally at all (see
[New Container (Docker container)](#new-container-docker-container) for
details).

### 3. Changes take effect immediately

Clicking "Save" automatically kicks off a build and deploy, taking
effect within seconds to tens of seconds. There's no need to restart a
server or run a deploy command by hand.

## Logging in for the first time

The admin has chosen either "Keycloak (OIDC)" or "local accounts" as the
console's login method, from `/config` (see
[Protecting the console itself](#protecting-the-console-itself-login-method-access-restriction)
for details). The first-time experience is the same either way.

1. Open SpinDeck's URL in a browser (right after installation, that's
   `http://<server's IP>:8080/`; once things are further along and a
   custom domain is configured, it'll be `https://<domain>/`).
2. **With Keycloak**: you're automatically redirected to Keycloak's
   (your organization's login system) login screen. Log in with your
   usual account. **With local accounts**: SpinDeck's own login form is
   shown. Enter the username/password your admin issued you.
3. Once logged in, the dashboard is shown. The "システム状態" (System
   Status) card near the top shows this node's hostname, OS, uptime, CPU
   load average, memory usage, and the versions of k3s/cert-manager/
   SpinKube operator/Traefik (the latter three are recorded when
   `upgrade.sh` runs, so an install that's never run it shows "—" for
   those). Below that is the list of registered apps — at first, nothing
   should be registered yet.

> If you can't log in or get an error, ask whoever installed the system
> to check (for Keycloak, the `SPINAPP_UI_OIDC_*` values in
> `/etc/spinapp-ui/env`; for local accounts, whether your
> username/password is registered under `/config`'s
> "ローカルアカウント管理" (Local account management)).

## Creating an app

### New App (JavaScript)

The quickest way to build an API or webhook.

1. Open **➕ New App** from the top navigation.
2. Enter an app name (lowercase letters, digits, and hyphens only, e.g.
   `hello-api`).
3. Write JavaScript code in the editor. Even just tweaking the sample
   code that's there by default is enough to see it work. You can write
   in a style similar to [Cloudflare Workers](https://developers.cloudflare.com/workers/)
   (`addEventListener('fetch', ...)` or
   `export default { fetch(request) { ... } }`).
4. The **Preview** tab lets you check it works right in the browser
   (it's a 404 before the first deploy, so save first).
5. Click "Build & Deploy" and your code is built into Wasm and
   automatically published at `https://<public hostname>/<app name>/`.

> Code that calls external APIs (`fetch()`) works out of the box with no
> special setup. Just be mindful of how widely that ends up exposed
> (see [Protecting an app (authentication)](#protecting-an-app-authentication)
> below).

### New Site (static site / PHP)

For when you want to publish HTML/CSS/JS files directly with no build
step.

1. Open **🌐 New Site**.
2. Enter an app name and start adding files (`index.html` is required;
   subdirectories are supported).
3. Binary files like images and PDFs can be uploaded too.
4. Include a `.php` file and it automatically switches to a
   configuration that runs PHP (no extra setup needed).
5. Click "Build & Deploy" to publish.

### New Proxy (reverse proxy)

For when you want to publish or relay another system already running
internally (e.g. an internal tool, a web app on another server) through
SpinDeck.

1. Open **🔀 New Proxy**.
2. Enter an app name and the destination **Backend Origin** (e.g.
   `http://10.0.0.50:8080`). Adding a path (e.g.
   `http://10.0.0.50:8080/app`) also works if the destination expects
   requests under a specific subpath.
3. Add per-path rules as needed for behavior (pass through as-is,
   return 404, or replace with a different path's content).
4. If you need to rewrite URLs appearing in the response for a
   subpath-served destination, use "書き換えルール" (rewrite rules,
   advanced).

> **Security note**: You can't set Backend Origin to a cloud metadata
> address or an obviously internal-only address (e.g. an internal
> Kubernetes cluster IP) — this errors out on save. This is only a
> simple check meant to catch mistakes, not a complete defense — make
> sure only people you trust can use this feature.

### New Cron (scheduled job)

Run a Python or bash script on a cron schedule.

1. Open **⏰ New Cron**.
2. Enter an app name, a schedule (cron syntax, e.g. `0 9 * * *` for
   9am daily), and the code to run.
3. `send_mail(to, subject, body)` for sending mail, or bash's
   `download(url, destination, [User-Agent])` for fetching a URL, both
   work with no extra setup.
4. Past run results can be checked from the "実行ログ" (Run log) tab on
   the edit page.

### New Container (Docker container)

Runs any Docker image as-is, as a regular container (a Kubernetes
`Deployment`) bypassing the Wasm sandbox entirely. Use this when you
want to run a published Docker image (nginx, postgres, etc.) verbatim,
or need a setup the other kinds can't express.

> **Only specify images you trust.** Unlike the other kinds (which run
> inside a Wasm sandbox), the image you specify may run with root
> privileges. By default only unauthenticated public images (Docker
> Hub, GHCR, etc.) can be used, but hosts registered under `/config`'s
> "New Container 用レジストリ認証" (New Container registry credentials)
> can be pulled using those saved credentials (e.g. your own private
> registry).

1. Open **📦 New Container**.
2. Enter an app name and image (e.g. `nginx:latest`). A **start
   command** is optional. Bare `python`/`node`/etc. images whose default
   command exits immediately will restart in a loop, so specify a
   command that keeps running, like `sleep infinity`, in that case (a
   checkbox can fill this in for you). **Environment variables** can
   also be specified, one `KEY=VALUE` per line.
3. Set **CPU limit** and **memory limit** (e.g. `500m`, `512Mi`), and
   **replica count** (2 or more automatically load-balances via a
   Kubernetes `Service`, round-robin). The memory amount you specify is
   reserved AND capped (so it never has to fight other apps for memory,
   but the deploy itself fails outright if there isn't enough free).
   CPU is capped only — the actual reservation (what the scheduler
   sets aside) is always a small fixed value, so multiple apps can
   overcommit CPU, at the cost of possibly not reaching your specified
   cap when the node's CPU is busy.
4. With **永続ストレージを使う** (Use persistent storage) checked,
   persistent data is mounted into the container (`/data` by default,
   changeable via "マウント先パス" (Mount path) — for MySQL/MariaDB,
   for example, use `/var/lib/mysql`, i.e. **the path the image
   actually writes its data to**. Some images look like they're working
   even at `/data` but don't actually persist their data there). The
   host-side location is `/data/containers/<app name>/` (no size limit
   — bounded only by the node's disk). Unchecking it means the
   container has no persistence (data is lost every time the Pod is
   recreated). If you set the replica count to 2 or more, every replica
   mounts the same persistent data simultaneously, so apps that don't
   support concurrent writes (most databases, or anything assuming a
   single instance) should keep the replica count at 1.
5. **Dedicated domain (FQDN)** and **port** are both optional. Setting
   them means the app occupies that entire FQDN (unlike the other
   kinds, it doesn't hang off a path). You can enter anything, but you
   can't use an FQDN another app — of any kind — already uses.
   **Leaving it blank means no external exposure via this port/TLS
   handling (port 443 through Traefik) at all** (no port/Service gets
   created either), making it a container usable only from the 🖥️
   console tab described below (for compute-only use cases, for
   example). A port is only required if you set an FQDN. Since "追加の
   公開ポート" (Extra published ports, described later) works
   independently of the FQDN/port settings, if you don't need
   port-443-via-Traefik at all and only want to expose a separate port
   like a VPN, you can leave this section blank and only set that up.
6. If you set an FQDN, you can choose the **TLS handling**:
   - **Terminated by kubespin (Traefik)** (recommended) — Follows
     `/config`'s HTTP/HTTPS setting, same as any other app. Basic/OIDC
     auth and access-log analytics are only available in this mode.
   - **Passed through to the container** — Always listens on port 443
     regardless of the site-wide setting, and Traefik forwards the raw
     data without decrypting TLS. The container itself has to supply
     its own certificate and terminate HTTPS.
7. In "terminate" mode, you can also enable **"混在コンテンツを自動修正する"**
   (auto-fix mixed content). This is a stopgap for when the container's
   own generated HTML still has old absolute `http://` URLs (CSS/JS/
   images, etc.) that get blocked by the browser on an HTTPS page (it
   adds a response header that tells the browser to automatically
   rewrite `http://` sub-resource requests to `https://`). Fixing the
   app's own configuration to use `https://` is the real fix, but this
   is a workaround for legacy apps that can't be fixed right away (e.g.
   an old WordPress site migrated from HTTP to HTTPS).
8. Click "Deploy" to publish.

The edit page's "Update & Redeploy" **always recreates the Pod**, even
if nothing in the form actually changed. This matters for cases SpinDeck
can't detect on its own — like re-pushing new content under the same
image tag — clicking "Update & Redeploy" is guaranteed to create a fresh
Pod and pull the latest image (note that `imagePullPolicy` follows the
image tag's own default behavior, so a fixed tag other than `latest`
won't actually re-pull — whatever's already cached on the node gets
used as-is. If you need to guarantee an update, use the `latest` tag or
deploy under a new tag). On every update, a Container app **stops the
old Pod first, then creates the new one** (not Kubernetes' default
rolling update, but an always-stop-then-recreate approach). This means
there's always a brief moment of unavailability during an update, but in
exchange it never needs both the old and new Pod's CPU/memory at the
same time (so the Pod can always be recreated reliably even when the
node's resources are tight).

#### Sidecar containers (multiple cooperating containers, e.g. WordPress + MySQL)

If you want to deploy multiple cooperating containers — like WordPress
— as a single app, the "サイドカーコンテナ" (Sidecar containers) section
under the "設定" (Settings) tab lets you add extra containers that run
alongside it in the same Pod.

> **Why one Pod instead of two separate apps?**: SpinDeck restricts
> every app's Pod from communicating with other Pods/Services inside the
> cluster, so that self-service code can't be used as a pivot point into
> the internal network. Because of this, two separate New Container apps
> can't talk to each other. Containers within the same Pod can
> communicate over `localhost`, though, so they can cooperate without
> being subject to that restriction.

- The main container (e.g. `wordpress:latest`) is configured normally —
  image, environment variables, port, etc. Persistent storage can be
  toggled on/off via "永続ストレージを使う," and "マウント先パス"
  (default `/data`) can also be changed.
- "+ サイドカーコンテナを追加" (+ Add a sidecar container) lets you
  configure the image, start command, environment variables, and
  CPU/memory limits for a cooperating container (e.g. `mysql:8`). Up to
  6 can be added (7 containers total in one Pod, including the main
  one).
- The main container can reach a sidecar at `127.0.0.1:<port number>`
  (e.g. `WORDPRESS_DB_HOST=127.0.0.1:3306`).
- If you need persistence (e.g. for a database), check "永続ストレージを
  使う" and enter **the path the image actually writes its data to** as
  the "マウント先パス" (e.g. `/var/lib/mysql` for MySQL/MariaDB,
  `/var/lib/postgresql/data` for PostgreSQL). Leaving it blank defaults
  to `/data`, but most database images write their data to their own
  specific path rather than `/data` — **if you don't specify the
  correct path for your image, it can look like it's working while
  actually not persisting anything, and lose its data every time the
  Pod is recreated.** The main container and each sidecar automatically
  get their own separate directory, so there's no risk of data mixing
  between them.
- **Keep the replica count at 1** — setting it to 2 or more spins up
  separate sidecars per replica too, which breaks sharing for a
  database and similar setups.
- The 🖥️ console (in-browser terminal) and 📜 log tabs both have a
  "接続先のコンテナ" (target container) selector, letting you drop into
  or view logs for any sidecar individually, not just the main
  container.

If you need something more complex (8+ containers, extra volumes,
etc.), you can also edit the raw Deployment YAML directly from the
"YAML" sub-tab.

#### Extra published ports (for non-HTTP protocols like VPN)

The "追加の公開ポート" (Extra published ports) section under the
"設定" tab lets you add TCP/UDP ports directly, separate from the
FQDN/port described above (which always goes through Traefik on port
443). Use this when you need to expose a non-HTTP protocol Traefik can't
relay — like SoftEther VPN's L2TP/IPsec (`500/udp`, `4500/udp`).

> **This bypasses Traefik entirely.** Ports added here get no FQDN
> filtering and no TLS termination at all. The port you specify becomes
> directly reachable by anyone who can reach this node (not even the
> host firewall, e.g. ufw, can block it — it's exposed the same way
> Traefik's own 80/443 are, directly via k3s's klipper-lb). Only use
> this for something you trust.

- "+ ポートを追加" (+ Add a port) lets you specify an external port
  number (1-65535) and protocol (TCP/UDP). Up to 10 can be added.
- The "転送先" (Forward-to) port is optional — leaving it blank forwards
  to the container on the same port number as the external one. Only
  fill it in if the container actually listens on a different port than
  the one you want exposed externally (e.g. exposing `4500/udp`
  externally while the container listens on `14500` internally).
- Ports 80 and 443 can't be used as external ports (Traefik itself uses
  them). There's no restriction on the forward-to port.
- **The same external port number + protocol combination can't be
  reused across different Container apps** (this errors out on save).
  Unlike Traefik-routed ports, extra ports are raw ports with no
  FQDN-based distinction, so multiple apps can't share the same number.
- Adding/removing these creates/deletes a separate `<app name>-raw`
  LoadBalancer-type Service, distinct from the container's main
  Service. It's automatically removed when the app is deleted.
- On a node where `SPINDECK_DUAL_STACK` was enabled at install time,
  these also listen on IPv6 (same treatment as Traefik's own 80/443).
  On a non-dual-stack node, it's IPv4 only.

#### 🖥️ Console (in-browser terminal)

The edit page for an existing app has a "🖥️ コンソール" (Console) tab
that opens an interactive terminal, equivalent to `kubectl exec`,
directly in the browser. If there are multiple replicas, you can choose
which one to connect to. This is unrestricted shell access to exactly
what this container is actually running.

#### 📜 Logs

The edit page for an existing app also has a "📜 ログ" (Logs) tab that
shows output equivalent to `kubectl logs`, directly in the browser (the
most recent 500 lines, prefixed by Pod name when there are multiple
replicas). Click "更新" (Refresh) to fetch again.

#### YAML editing (advanced)

The "YAML" sub-tab under "設定" lets you edit the raw Deployment and
Service YAML directly. Env vars, multiple containers, a custom command —
anything the form above doesn't expose can be written freely here.
`metadata.name`/`metadata.namespace` are automatically overwritten on
save, but there's essentially no other restriction. Settings that would
let you escape to the host are rejected, though: `privileged`,
`allowPrivilegeEscalation`, `capabilities.add`, `hostNetwork`/`hostPID`/
`hostIPC`, a `hostPath` mount to anywhere other than the one directory
the console manages, and `automountServiceAccountToken` (always
disabled).

> **Removing the `spinapp-ui.dev/app` label makes that app invisible to
> the app list, delete, and the 🖥️ console (exec).** The YAML tab always
> starts from whatever's actually running right now (or, for a new app,
> a template with the same content the form would produce), so it's
> best to edit only the parts you actually need to change and leave
> everything else as-is.

## Protecting an app (authentication)

This section applies to New App / New Site / New Proxy / New Container
(New Cron is never exposed externally in the first place).

If you don't want an app to stay reachable by anyone, the **🔒 認証**
(Authentication) tab on each app's edit page offers two kinds of
protection (they can't be combined — pick one):

- **HTTP Basic auth** — Simple protection with a single
  username/password pair. Shows the browser's standard login dialog.
- **OIDC auth (allowed users only)** — Reuses the same Keycloak login as
  the console itself, and lets you specify a list of "usernames allowed
  to view this app." A user who isn't logged in is sent to Keycloak's
  login screen (this method isn't available if the console itself is
  set to local-account mode — it only works when Keycloak is
  configured).

If you leave an app unprotected either way, anyone can access it (be
especially careful with New Proxy relaying an internal system).

Only for New Container, authentication can only be set **when "TLS
handling" is "terminated by kubespin (Traefik)"** ("passed through to
the container" means Traefik never decrypts the contents, so
authentication can't be inserted). The dedicated domain can be any
free-form FQDN (no need to pre-register it under `/config`), but if you
use OIDC auth, register
`https://<domain>/_spindeck/oidc/callback` as an additional Redirect URI
on the Keycloak client for that domain.

## Publishing under a custom domain (FQDN)

This section applies to New App / New Site / New Proxy. By default,
these apps are all published under one shared hostname —
`https://<public hostname>/<app name>/` — but you can also assign each
app its own dedicated domain (e.g. `myapp.example.com`).

1. Under `/config`'s "アプリ専用ドメイン (FQDN)" (Per-app dedicated
   domains) section, register the domain you want to use ahead of time.
2. On each app's edit page, select a registered domain from the
   dropdown next to the Name field.
3. Once selected, that app only responds to requests for the specified
   domain, not just the path (`/app-name/`) any more (the path itself
   stays there too).

> To actually use a domain, you'll need to separately set up its DNS
> record to point at SpinDeck's server (more precisely, Traefik's IP).

New Container's dedicated domain works differently — it needs no
pre-registration under `/config` (free-form entry), has no path at all,
and the app occupies the entire domain by itself. See
[New Container (Docker container)](#new-container-docker-container) for
details.

## Publishing over HTTPS

`/config`'s "HTTPS" section lets you switch SpinDeck to listen directly
on HTTPS (port 443).

1. Place a certificate file and private key file you've already
   obtained at a fixed path on the server (default
   `/etc/spinapp-ui/tls/cert.pem` / `/etc/spinapp-ui/tls/key.pem`) —
   obtaining/renewing the certificate itself is outside SpinDeck's
   scope, use whatever method you normally rely on (Let's Encrypt, an
   internal CA, a purchased certificate, etc.).
2. Confirm `/config`'s "HTTPS" section shows the certificate as
   "設定済み" (configured).
3. Choose "HTTP (80番) で待ち受ける" (Listen on HTTP/port 80) or "HTTPS
   (443番) で待ち受ける" (Listen on HTTPS/port 443) and save. Switching
   this automatically re-applies routing for every existing app.
   Switching to "HTTPS" also automatically stops external exposure of
   port 80 (only 443 becomes reachable externally, regardless of any
   host-level firewall settings).
4. Just replacing the certificate files is picked up automatically
   within tens of seconds (no restart needed).

If you also want to publish the admin console (this console itself)
through the same route, you can enable that from a checkbox in the same
"HTTPS" section (this needs `SPINAPP_UI_HOST_IP` to already be
configured, and Keycloak's Redirect URI to be set up). Leaving it off
still leaves direct access to `http://<server's IP>:8080/` working.

> If New Container's "TLS handling" is set to "terminated by kubespin
> (Traefik)," it simply follows this "HTTPS" section's setting (port 80
> while still HTTP, port 443 once switched to HTTPS). "Passed through to
> the container" always listens on port 443 regardless of this setting
> (since the container itself handles the certificate) — meaning by
> default, port 443 stays externally exposed even while still in HTTP
> mode. If you never use passthrough mode at all, unchecking `/config`'s
> "New Containerの『コンテナへパススルー』モードを許可する" ("Allow New
> Container's 'passed through to the container' mode") checkbox removes
> every externally exposed port, including 443, while in HTTP mode (you
> can't uncheck it if an app is already using passthrough mode).

## Protecting the console itself (login method, access restriction)

`/config`'s "コンソールのログイン方式" (Console login method) section
lets you switch the console's own login method at any time (only one is
active at a time):

- **Local accounts** — The default method (one admin account is
  created automatically at install time). Logs in with a
  username/password pair the console itself holds, with no external
  identity provider. The same "ローカルアカウント管理" (Local account
  management) section lets you register, delete, and change passwords
  for multiple accounts.
- **Keycloak (OIDC)** — Anyone who can log into the configured Keycloak
  realm can log into the console and manage every app.

Either way, if you want to restrict who has access:

- **With Keycloak**: `/config`'s "コンソールへのアクセス制限"
  (Console access restriction) section lets you specify a list of
  usernames allowed to log in (an empty list means anyone who can log
  into Keycloak can administer it).
- **With local accounts**: since only registered accounts can log in at
  all, the "access restriction" list isn't used — just delete accounts
  from "ローカルアカウント管理" to narrow things down.

> Either way, an action that would lock you out (saving a list that
> excludes yourself, deleting yourself, deleting the last remaining
> account, switching to local accounts while zero accounts exist, etc.)
> is rejected on save.

`/config`'s "セッションのアイドルタイムアウト" (Session idle timeout)
section sets how many minutes of inactivity after login it takes for a
session to expire automatically (30 minutes by default). It applies
uniformly across Keycloak, local accounts, and per-app OIDC logins alike,
and keeps extending as long as activity continues — it's an idle timeout,
not a fixed expiry from login time. No restart needed to change it.

## Viewing access analytics

**📈 Analytics** shows recent access activity — request-count trends,
error rate, response times, per-app request counts, and a recent access
log — with no separate monitoring tool needed.

The dropdown next to the time-range selector lets you filter down to a
single app (choose "全アプリ," All apps, to clear it). Traffic to the
console's own internal endpoints (`/_spindeck/*`, OIDC forwardauth,
etc.) isn't a registered app, so it's automatically excluded from both
the totals and the filter.

The top of the page always shows exactly which log file is being read
(path, size, last-modified time). A warning appears if the file can't be
found, or hasn't been updated in over an hour — meant to help you notice
when Traefik's actual write location and the console's read location
have drifted apart (e.g. only one side's setting got changed).

## Multi-node setups (peer replication)

If you want higher availability using multiple SpinDeck nodes, you can
register other nodes from `/config`'s "ピアノード" (Peer nodes) section.
Once registered, app create/edit/delete operations are automatically
mirrored to those nodes too. Each node remains a fully independent
cluster — the Kubernetes clusters themselves are never merged.

## Exporting and importing apps

The **⬇️ エクスポート** (Export) link in the top-right of each app's
edit page (Container/JS/Site/Proxy/Cron Job, all of them) downloads that
one app's configuration as a JSON file. The dashboard's **⬇️ 全体を
エクスポート** (Export everything) link downloads every registered app
bundled into a single JSON file.

Uploading an exported file from the sidebar's **⬆️ Import** page (or
pasting the JSON directly) creates/restores apps on that node. An
existing app with the same name has its settings overwritten on the
spot. This is meant for backups before rebuilding a node, or manually
migrating from one node to another.

Importing only applies to the node you run it on — even in a multi-node
(peer replication) setup, it isn't automatically propagated to other
nodes. Import on each node individually if needed.

Exported files contain sensitive information as-is — Basic auth
password hashes, the list of OIDC-allowed users, etc. (deliberately, so
things work immediately after importing). Handle these files carefully.

## Backups (`backup.sh`)

Running `sudo ./scripts/backup.sh` from a checkout of this repository bundles
the following into a single `tar.gz` saved under `/data/backup/`:

- The actual persistent-storage data for Container apps (e.g. a
  database's files — the part that can't be reconstructed from config
  alone), and Site apps' files themselves (the same content also lives
  in `apps-export.json` below, so this is a belt-and-suspenders extra
  copy). This targets only the specific data directories actually in
  use, not the whole of `/data` (k3s's own datastore, the local
  registry's images, Traefik's logs, `lost+found`, and other things
  that often end up living under `/data` alongside SpinDeck's real
  data — none of these are included, since running `install.sh` on a
  new node recreates all of them anyway).
- Every registered app's configuration (`apps-export.json`) — exactly
  the same content as the "⬇️ Export everything" link above (every
  Container/JS/Site/Proxy/Cron Job, including Site apps' actual file
  contents and every password hash). Generated directly, without going
  through HTTP, so this can run from cron etc. without logging into the
  console.
- `/opt/spinapp-ui/config.json` (the full set of `/config` settings —
  registered FQDNs, peer list, SMTP, console login accounts, registry
  credentials, etc.) and `/etc/spinapp-ui/env` (OIDC settings, peer
  token, host IP, etc.) — so another node can immediately take over as
  this node's "successor."

Since passwords and tokens are included in plaintext, the archive itself
is `chmod 600`'d. Handle it with care.

**How to restore**: extract the `tar.gz`, then (1) place the contents of
`containers/<app name>/` under the new node's own
`/data/containers/<app name>/`, (2) overwrite the new node's own
`/opt/spinapp-ui/config.json`/`/etc/spinapp-ui/env` with `config.json`/
`env` and restart `spinapp-ui`, and (3) upload `apps-export.json` to the
sidebar's **⬆️ Import** page. That's the whole process. Only
`SPINAPP_UI_HOST_IP` inside `env` holds the original node's own LAN IP,
so rewrite it to the new node's actual IP address (every other value can
be used as-is).

## Health checks (`healthcheck.sh`)

Running `sudo ./scripts/healthcheck.sh` actually creates one disposable
`zzzhealthcheck-`-named instance of every app kind (Container/JS/Site/
Proxy/Cron Job), verifies it including real HTTP access through
Traefik, and deletes everything again at the end (never touching any
existing registered app). It also individually re-checks specific
regressions this project has actually hit before (CPU overcommit, the
Deployment rollout strategy, extra-ports host-port collision
protection, the Proxy app's backend validation boundary, and more).
It's meant to be run after `upgrade.sh`, but it's safe to run at any
time. A pass/fail summary is printed at the end, and the exit code is 1
if anything failed (usable for automated runs via cron, etc.).

## Troubleshooting

- **Can't log in / getting an OIDC error** — Ask an admin to check the
  Keycloak settings (Issuer, Client ID, Client Secret, Redirect URI). If
  using local accounts, have them confirm your username/password is
  registered under `/config`'s "ローカルアカウント管理" (Local account
  management).
- **OIDC login is slow / stuck loading** — If it hangs or is slow right
  after submitting your Keycloak credentials (when the browser returns
  from Keycloak back to the app), there may be a network reachability
  problem between the server running the console and Keycloak. In
  particular, if Keycloak's hostname has both IPv4 and IPv6 addresses,
  and only the IPv6 route is actually broken (configured but never
  actually gets a response), there can be a delay while it tries IPv6,
  gives up, and falls back to IPv4. Ask an admin to check reachability
  to Keycloak from the console's server with both `curl -6` and
  `curl -4`.
- **An app fails to deploy** — Each app's edit page has a "ログ"
  (Logs) tab showing the build log. Most causes — like a JavaScript
  syntax error — show up there (New Container's "📜 ログ" tab is
  runtime output equivalent to `kubectl logs`, not a build log — use it
  to investigate a container that crashes right after starting).
- **Getting a 404** — Check whether the deploy has actually finished
  (give it a moment after clicking Save), and check the URL path
  (e.g. the trailing slash on `/app-name/`).
- **A New Container app disappeared from the list / delete / 🖥️
  console** — You may have edited away the `spinapp-ui.dev/app` label
  from the YAML tab. Check the actual label with
  `kubectl get deployment -A -l spinapp-ui.dev/app`, fix the YAML, and
  save again.
- **Can't connect to a New Container app's 🖥️ console** — If the
  container's image has no shell at all (`bash` or `sh`, as with a
  distroless image), connecting just shows an error.
- **Still stuck** — If you can log into the server, check
  `kubectl get pods -A` (overall cluster status) and
  `systemctl status spinapp-ui` (the console's own status).

## Glossary

Terms that come up in SpinDeck's underlying machinery. None of this is
required knowledge to use it day-to-day, but it may help if one of these
words shows up in an error message.

- **Kubernetes (k8s)** — The underlying platform for managing and
  running a collection of apps together. SpinDeck runs on top of it.
- **k3s** — A lightweight distribution of Kubernetes, easy to run even
  on a single server. This is what SpinDeck uses.
- **Wasm (WebAssembly)** — A lightweight execution format for running
  apps quickly and safely. New App/Site/Proxy/Cron are all built into
  this format and run that way behind the scenes (New Container is the
  exception — it runs as a regular Docker container instead).
- **SpinKube / Spin** — The mechanism that runs Wasm apps on
  Kubernetes. SpinDeck uses this under the hood.
- **Deployment** — Kubernetes' basic mechanism for "keep a specified
  number of containers running." New Container runs as one of these.
- **Traefik** — The software sitting in front of SpinDeck that routes
  each incoming request to the right app (a reverse proxy).
- **Keycloak / OIDC** — One mechanism for handling login/authentication.
  SpinDeck itself has no password management of its own — you can
  either lean on a system like Keycloak, or use the console's built-in
  local accounts (username/password) instead.
- **CronJob** — Kubernetes' mechanism for "run this on a schedule." A
  job created via New Cron runs as one of these.

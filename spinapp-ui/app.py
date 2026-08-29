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

import base64
import errno
import fcntl
import hashlib
import html
import ipaddress
import json
import os
import pty
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import termios
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests
import urllib3.util.connection as _urllib3_connection
import yaml
from authlib.integrations.flask_client import OAuth

# Force every outbound HTTP connection this process makes (Authlib's calls
# to Keycloak, replicate_to_peers()'s calls to peer nodes, anything else
# built on requests/urllib3) to resolve IPv4 only. Confirmed live: a
# dual-stack Keycloak hostname (AAAA record present but that record's
# route is a black hole -- packets go out, nothing ever comes back, no
# ICMP rejection) made Authlib's token-exchange call in spindeck_oidc_
# callback() sit for the full connect timeout on the IPv6 attempt before
# ever falling back to the working IPv4 address, on a node where IPv6 is
# nominally configured but not actually routed correctly end-to-end. This
# project has no IPv6-only target anywhere in its own design (peers are
# plain RFC1918 IPs, Keycloak/OIDC issuers are admin-supplied hostnames),
# so there's no legitimate case that needs the IPv6 attempt at all --
# skipping it outright removes the failure mode instead of just bounding
# its worst case (see default_timeout on oauth.register() below, which
# still matters for genuine unreachability, just not this specific one).
_urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sock import Sock
from flask_wtf import CSRFProtect

from i18n import LANG_COOKIE_NAME, SUPPORTED_LANGS, resolve_lang, t as _t

# Bumped by hand on each release (semver: MAJOR.MINOR.PATCH). Shown in the
# sidebar footer (see inject_user()) so a screenshot or bug report always
# carries it -- no other runtime signal ties a given install back to a
# specific point in the repo's history.
SPINDECK_VERSION = "1.0.2"

app = Flask(__name__)
# "container" apps only -- a browser terminal (kubectl exec, bridged over a
# pty) into the running Pod. See sock.route("/containers/<name>/exec")
# below. Works fine on Werkzeug's dev server (no gunicorn/eventlet needed)
# as long as the server itself runs threaded (see app.run() at the bottom
# of this file) -- otherwise one open terminal tab would block every other
# request this console handles for as long as it's open.
sock = Sock(app)

KUBECONFIG = os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")
NAMESPACE = os.environ.get("SPINAPP_UI_NAMESPACE", "default")
EXECUTOR = os.environ.get("SPINAPP_UI_EXECUTOR", "containerd-shim-spin")

if os.environ.get("SPINAPP_UI_FLASK_SECRET"):
    app.secret_key = os.environ["SPINAPP_UI_FLASK_SECRET"]
else:
    app.secret_key = secrets.token_hex(32)
    print(
        "[startup] WARNING: SPINAPP_UI_FLASK_SECRET is not set -- using a "
        "random secret generated for this process only. Every session "
        "(console logins, per-app OIDC logins, the CSRF tokens embedded in "
        "every form) is invalidated on every restart, including the "
        "self-restart /config triggers after saving OIDC/peer-token "
        "settings. Set SPINAPP_UI_FLASK_SECRET in /etc/spinapp-ui/env to "
        "avoid this."
    )

# Secure would make the browser refuse to send the session cookie back
# over plain HTTP -- fine (desirable, even) once this console is only
# ever reached over https, but during a brand new node's bootstrap phase
# (see INSTALL.md's "新規ノードのブートストラップ", SPINAPP_UI_OIDC_
# REDIRECT_URI still http://<node-ip>:8080/... because no FQDN/cert/HTTPS
# is wired up yet) a hardcoded True here silently breaks login entirely --
# confirmed live: Keycloak's login succeeds, but the callback can't find
# the session-stored OAuth "state" (it never came back from the browser),
# surfacing as "mismatching_state: CSRF Warning!". Deriving this from
# OIDC_REDIRECT_URI's own scheme instead of hardcoding means the exact
# same http->https switch that bootstrap flow already ends with (editing
# this one env var, which restarts the process) is *also* what flips this
# back on -- no separate setting to remember. "Lax" (not "Strict") because
# Strict would drop the cookie on the top-level GET navigation Keycloak
# redirects the browser back with after login, breaking session["next"]/
# session["apps_next"] set just before redirecting to Keycloak.
OIDC_REDIRECT_URI = os.environ.get("SPINAPP_UI_OIDC_REDIRECT_URI")
app.config["SESSION_COOKIE_SECURE"] = urlsplit(OIDC_REDIRECT_URI or "").scheme == "https"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Hard outer ceiling on every request body, across every route -- without
# this, Werkzeug happily buffers an entire oversized request.form/
# request.files body into memory before any of this app's own per-field
# MAX_*_BYTES checks (MAX_SITE_TOTAL_BYTES, MAX_IMPORT_FILE_BYTES, ...)
# ever get a chance to run, since those all read the value first and
# measure it after. Found during the 2026-08 security audit that the
# "container" app's raw-YAML edit mode (yaml_manifest form field) had no
# size check at all, of any kind. Set well above the largest legitimate
# single upload (MAX_SITE_TOTAL_BYTES, 30MB) to leave room for multipart
# overhead without being effectively unbounded. Werkzeug returns a plain
# 413 for anything over this, before the request even reaches a view
# function's own code.
app.config["MAX_CONTENT_LENGTH"] = 40_000_000

# Flask-WTF's default (1 hour) ties a CSRF token to a time window on top of
# the session it's already bound to. The extra window isn't load-bearing
# for CSRF protection itself, and a rejected token here fails *before* any
# route handler runs -- so none of the render_error()-style "keep what you
# typed" fallbacks used throughout this app apply, unlike an ordinary
# validation error. Someone who leaves e.g. the cron/JS editor open for
# longer than an hour before hitting deploy would otherwise lose whatever
# they'd written to a bare 400 page.
app.config["WTF_CSRF_TIME_LIMIT"] = None

csrf = CSRFProtect(app)


@app.after_request
def set_security_headers(response):
    # Defends against clickjacking (e.g. a transparent iframe over /config
    # tricking a logged-in admin into clicking a save/delete button) without
    # touching script-src -- a stricter CSP would need every inline
    # <script>/onclick in these templates (there are many) rewritten to use
    # nonces first, which is a much bigger, separate change.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

OIDC_ISSUER = os.environ.get("SPINAPP_UI_OIDC_ISSUER")
OIDC_CLIENT_ID = os.environ.get("SPINAPP_UI_OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.environ.get("SPINAPP_UI_OIDC_CLIENT_SECRET")
# OIDC_REDIRECT_URI itself is set earlier (before the session cookie
# config, which derives SESSION_COOKIE_SECURE from it) -- kept here too
# since this is where the rest of the OIDC_* block lives.

# This k3s node's own IP, reachable from its Traefik Pods -- used to let
# Traefik's forwardAuth Middleware call back into this process (see
# ensure_forwardauth_reachability()). Only needed if any app uses OIDC auth.
FORWARDAUTH_HOST_IP = os.environ.get("SPINAPP_UI_HOST_IP", "")

# Where an admin places a TLS certificate/key pair on this host for
# TRAEFIK_SCHEME="https" (see sync_tls_secret()). This process reads them
# directly (it runs on the host, unlike Traefik itself, which is a Pod and
# can't see the host filesystem) and syncs their contents into a Secret
# Traefik's own Kubernetes provider watches -- so replacing the files here
# is all an admin ever has to do; nothing needs a restart, on either side.
TLS_CERT_PATH = Path(os.environ.get("SPINAPP_UI_TLS_CERT_PATH", "/etc/spinapp-ui/tls/cert.pem"))
TLS_KEY_PATH = Path(os.environ.get("SPINAPP_UI_TLS_KEY_PATH", "/etc/spinapp-ui/tls/key.pem"))

# The systemd EnvironmentFile these (and everything else) are loaded from.
# /config's OIDC settings write here directly rather than to CONFIG_PATH's
# JSON, since OIDC_* are only ever read from os.environ at process start
# (authlib's OAuth.register() doesn't support being re-pointed at a new
# issuer/client at runtime -- confirmed empirically: calling register()
# again with the same name is a silent no-op, it keeps the first
# registration). Saving here only takes effect after a restart, which
# config_page() triggers itself -- see restart_self_soon().
ENV_FILE_PATH = Path(os.environ.get("SPINAPP_UI_ENV_FILE", "/etc/spinapp-ui/env"))


def read_env_file():
    """Return {key: value} from ENV_FILE_PATH, or {} if missing/unreadable."""
    if not ENV_FILE_PATH.exists():
        return {}
    values = {}
    for line in ENV_FILE_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def update_env_file(updates):
    """Merge `updates` into ENV_FILE_PATH, preserving every other line
    (comments included) as-is; updates matching KEY= lines in place and
    appends any key not already present."""
    lines = ENV_FILE_PATH.read_text().splitlines() if ENV_FILE_PATH.exists() else []
    remaining = dict(updates)
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                continue
        new_lines.append(line)
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")
    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE_PATH.write_text("\n".join(new_lines) + "\n")


def restart_self_soon():
    """Restart the spinapp-ui systemd service shortly after the current
    request finishes, so config_page() can send its "saved, restarting"
    response back to the browser first."""
    def _restart():
        time.sleep(2)
        subprocess.run(["systemctl", "restart", "spinapp-ui"])
    threading.Thread(target=_restart, daemon=True).start()

oauth = OAuth(app)
oauth.register(
    name="keycloak",
    client_id=OIDC_CLIENT_ID,
    client_secret=OIDC_CLIENT_SECRET,
    server_metadata_url=f"{OIDC_ISSUER}/.well-known/openid-configuration",
    # client_id contains ":" (it's a URL), which breaks HTTP Basic auth's
    # username:password splitting -- must send credentials in the POST
    # body instead of an Authorization: Basic header.
    client_kwargs={
        "scope": "openid profile email",
        "token_endpoint_auth_method": "client_secret_post",
        # Every outbound call Authlib makes to Keycloak (discovery,
        # /auth, and critically the server-to-server token exchange in
        # spindeck_oidc_callback()/login()) otherwise has NO timeout at
        # all -- confirmed via OAuth2Session.request()'s own source
        # (authlib 1.7.2: `if self.default_timeout: kwargs.setdefault
        # ("timeout", self.default_timeout)`), so it's unbounded unless
        # set here. Same class of bug as the earlier /logout hang (see
        # that route's history) -- an unreachable/slow-to-respond
        # Keycloak from this specific node would otherwise hang the
        # request forever instead of surfacing a clean error. Confirmed
        # live: a container app's OIDC callback got stuck indefinitely
        # ("ずっと待ち状態") on a node where the token-endpoint call
        # apparently stalled, with no way to distinguish "still working"
        # from "will never return".
        "default_timeout": 15,
    },
)

JS_TEMPLATE_DIR = os.environ.get(
    "SPINAPP_UI_JS_TEMPLATE", "/opt/spinapp-ui/js-template-src/hello-js"
)
REGISTRY = os.environ.get("SPINAPP_UI_REGISTRY", "localhost:5000")

# Hostname shown in the dashboard and the editor's preview iframe when an
# app has no FQDN of its own pinned (see FQDN_ANNOTATION/REGISTERED_FQDNS
# below) -- purely display, never affects routing (routes match on
# PathPrefix unless an app opts into Host(...) via its own pinned FQDN).
# Sourced from /config's "default" FQDN once one is marked as such; falls
# back to the env var, and (for configs saved before default-FQDN existed)
# a legacy standalone "external_host" value, so nothing resets silently on
# upgrade.
CONFIG_PATH = Path(
    os.environ.get("SPINAPP_UI_CONFIG_PATH", "/opt/spinapp-ui/config.json")
)


def load_persisted_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (ValueError, OSError):
        return {}


_persisted = load_persisted_config()
# FQDNs registered on /config that an individual app can be pinned to (see
# FQDN_ANNOTATION) so its route only responds on that Host, instead of the
# default PathPrefix-only match that answers on any Host/IP.
REGISTERED_FQDNS = list(_persisted.get("fqdns", []))
DEFAULT_FQDN = _persisted.get("default_fqdn") or ""
# Base display host used when no FQDN is marked default -- sourced from
# config.json if a FQDN was ever registered before (so a since-removed
# default doesn't reset display URLs to the env var underneath an admin),
# otherwise the env var every install is expected to set.
MANUAL_EXTERNAL_HOST = _persisted.get("external_host") or os.environ.get(
    "SPINAPP_UI_EXTERNAL_HOST", "apps.example.com"
)
EXTERNAL_HOST = DEFAULT_FQDN or MANUAL_EXTERNAL_HOST

# Sibling SpinDeck nodes (each a fully independent, standalone k3s/SpinKube
# install -- see docs/TECHNICAL_NOTES.md "Peer replication") that app/site/
# proxy deploys and deletes are mirrored to, for availability. Each entry is
# a base URL (e.g. "https://node2.example.com:8080"); managed from /config
# the same way REGISTERED_FQDNS is. This is a flat, full-mesh setup: list
# every *other* node here on every node, and never list a node's own URL.
PEERS = list(_persisted.get("peers", []))

# Usernames (Keycloak preferred_username -- the same value session["user"]
# ["name"] holds) allowed to log into the console itself. Empty means "any
# authenticated Keycloak user" (the original, still-default behavior);
# non-empty switches check_auth() to an allow-list. Managed from /config
# the same way REGISTERED_FQDNS/PEERS are (add_console_user/
# remove_console_user actions in config_page()).
CONSOLE_ALLOWED_USERS = list(_persisted.get("console_allowed_users", []))

# "oidc" (default, unchanged behavior) or "basic" -- which mechanism
# /login uses. "basic" is a local username+password alternative for sites
# with no Keycloak/OIDC provider available; accounts live entirely in this
# console's own config.json, not Kubernetes or any external IdP. Exactly
# one mode is ever active -- not both at once (see login()/check_auth()).
# Hot-swappable like CONSOLE_ALLOWED_USERS/TRAEFIK_SCHEME (no restart:
# nothing bakes this into a client object at process start the way
# oauth.register() does for OIDC's own settings). Managed from /config via
# save_console_auth_mode/add_basic_auth_account/remove_basic_auth_account/
# set_basic_auth_password (see config_page()).
CONSOLE_AUTH_MODE = _persisted.get("console_auth_mode") or "oidc"
# [{"username": ..., "password_hash": "$apr1$..."}, ...] -- see
# hash_apr1_password()/verify_apr1_password() for the hash format.
CONSOLE_BASIC_AUTH_USERS = list(_persisted.get("console_basic_auth_users", []))

# Idle timeout (minutes) for every session cookie this app issues --
# console logins (both OIDC and Basic-Auth) and per-app OIDC logins alike,
# since they all go through the same Flask session mechanism. Login routes
# set session.permanent = True (see login()/oidc_callback()/
# spindeck_oidc_callback()); Flask's own SESSION_REFRESH_EACH_REQUEST
# default (True) then re-issues the cookie with a fresh
# now+PERMANENT_SESSION_LIFETIME expiry on every request, which is what
# makes this an *idle* timeout (sliding window) rather than a fixed
# time-since-login cap -- a continuously active session never expires.
# Before this setting existed, sessions had no expiry at all (default
# Flask non-permanent cookie, dies only when the browser closes). Editable
# from /config (action=save_session_timeout, see config_page()); applied
# to app.config["PERMANENT_SESSION_LIFETIME"] immediately, no restart
# needed -- same hot-swap pattern as CONSOLE_AUTH_MODE/TRAEFIK_SCHEME.
SESSION_IDLE_TIMEOUT_MINUTES = int(_persisted.get("session_idle_timeout_minutes") or 30)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES)

# "http" (default, unchanged behavior) or "https" -- which Traefik
# entryPoint every app/site/proxy route and the /_spindeck IngressRoute
# bind to (see route_entrypoints()/route_tls_block()). Switching requires
# a certificate already synced to the spindeck-tls Secret (see
# sync_tls_secret()); enforced in config_page()'s save_traefik_scheme
# action, not here, so importing this module never itself requires one.
TRAEFIK_SCHEME = _persisted.get("traefik_scheme") or "http"

# Whether "container" apps may use TLS "passthrough" mode (see
# apply_container_route_tcp()) -- True (default, existing behavior
# preserved) means the `websecure`/443 entryPoint stays externally exposed
# unconditionally, regardless of TRAEFIK_SCHEME, since a passthrough app's
# own IngressRouteTCP always binds there independent of the sitewide
# HTTP/HTTPS toggle. False means no app on this node is allowed to select
# passthrough mode, and 443's external exposure instead follows
# TRAEFIK_SCHEME exactly the same way 80 already does (see
# sync_traefik_web_exposure()) -- for an admin who never uses passthrough
# and wants "http mode" to mean *no* externally reachable port at all
# besides 80. Enforced in config_page()'s save_container_passthrough
# action (which also refuses to turn this off while any existing app is
# still in passthrough mode) and in create_container()/
# internal_apply_container()'s tls_mode validation, not here.
CONTAINER_PASSTHROUGH_ENABLED = bool(_persisted.get("container_passthrough_enabled", True))

# User-saved Container app templates -- get_container_config()'s shape
# plus a "key" (stable, generated once at save time) and a "label"
# (user-supplied, shown on the template management page), persisted in
# config.json (see save_container_as_template()/
# new_container_page()'s "?template=" handling, via find_container_
# template()). A list, not a dict keyed by name, so save order (oldest
# first) is preserved without extra bookkeeping.
CONTAINER_USER_TEMPLATES = list(_persisted.get("container_user_templates", []))

# Private Docker-registry credentials "container" apps may pull from --
# each entry {"host", "username", "password"}, keyed by host (see
# config_page()'s add_container_registry/remove_container_registry
# actions). "container" apps are otherwise deliberately scoped to public,
# unauthenticated images (see the CONTAINER_TLS_MODE_ANNOTATION block
# above) -- this is the one deliberate exception, added because a
# self-hosted registry an admin already trusts (unlike an arbitrary
# third-party one) isn't meaningfully different from Docker Hub/GHCR from
# a "public images only" trust-model standpoint, it's just also capable
# of requiring auth. Passwords are stored in plain text in config.json,
# same as SMTP_PASSWORD -- unavoidable, since Kubernetes' own
# imagePullSecrets Secret (see sync_container_registry_secret()) needs
# the actual credential, not a one-way hash the way per-app Basic Auth
# does. Synced into a single shared Secret referenced by every "container"
# app's Deployment (see deploy_container_app()), not one Secret per app --
# the credential set is realistically shared across apps pulling from the
# same registry, so this avoids re-entering it per app.
CONTAINER_REGISTRIES = list(_persisted.get("container_registries", []))

# Whether the console itself (not just each app) gets an extra route through
# Traefik at Host(EXTERNAL_HOST) -- see ensure_forwardauth_reachability().
# False (default) leaves today's behavior untouched: the console is only
# reachable at :8080 directly (and via peer-to-peer /internal/* calls, which
# always use that port explicitly regardless of this setting). Opt-in
# because turning it on widens who can *reach* the console (not who can log
# into it -- check_auth() still gates that): from "needs LAN/SSH-tunnel
# access to :8080" to "anyone who can resolve EXTERNAL_HOST".
CONSOLE_VIA_TRAEFIK = bool(_persisted.get("console_via_traefik", False))

# SMTP server + login used by cron jobs' "メール送信" tab (see
# render_cron_job_py()) -- global rather than per-job since it's normally
# the same mailbox for every job; only the From/To/subject/body differ per
# job. Editable from /config (action=save_smtp); no restart needed since,
# unlike OIDC, these are only ever read fresh at cron-deploy time, not
# baked into anything at process start.
SMTP_HOST = _persisted.get("smtp_host") or ""
SMTP_PORT = _persisted.get("smtp_port") or 587
SMTP_USER = _persisted.get("smtp_user") or ""
SMTP_PASSWORD = _persisted.get("smtp_password") or ""


def save_persisted_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "default_fqdn": DEFAULT_FQDN,
                "fqdns": REGISTERED_FQDNS,
                "external_host": MANUAL_EXTERNAL_HOST,
                "peers": PEERS,
                "smtp_host": SMTP_HOST,
                "smtp_port": SMTP_PORT,
                "smtp_user": SMTP_USER,
                "smtp_password": SMTP_PASSWORD,
                "console_allowed_users": CONSOLE_ALLOWED_USERS,
                "console_auth_mode": CONSOLE_AUTH_MODE,
                "console_basic_auth_users": CONSOLE_BASIC_AUTH_USERS,
                "session_idle_timeout_minutes": SESSION_IDLE_TIMEOUT_MINUTES,
                "traefik_scheme": TRAEFIK_SCHEME,
                "container_passthrough_enabled": CONTAINER_PASSTHROUGH_ENABLED,
                "container_user_templates": CONTAINER_USER_TEMPLATES,
                "container_registries": CONTAINER_REGISTRIES,
                "console_via_traefik": CONSOLE_VIA_TRAEFIK,
            },
            indent=2,
        )
        + "\n"
    )
    # write_text() leaves the file at the process umask's default (typically
    # 644 -- world-readable), but this file holds the plaintext SMTP
    # password, every registered container registry's plaintext password,
    # and every console Basic-Auth account's APR1 hash. Restrict it every
    # time it's (re)written, not just on first creation, since an admin
    # could otherwise widen it back via a stray `chmod` and never notice.
    os.chmod(CONFIG_PATH, 0o600)

# Kubernetes caps total metadata.annotations size at 256KiB; base64 inflates
# by ~4/3, so keep the raw source comfortably under that after encoding.
JS_SOURCE_ANNOTATION = "spinapp-ui.dev/js-source"
MAX_JS_SOURCE_BYTES = 150_000

# "proxy" apps store their structured config (backend origin + path rules +
# optional response-rewrite rules) rather than raw JS -- the actual worker
# code is generated from it fresh on every build (see render_proxy_js()), so
# the config is the editable source of truth, not the generated code.
PROXY_CONFIG_ANNOTATION = "spinapp-ui.dev/proxy-config"
MAX_PROXY_CONFIG_BYTES = 50_000
MAX_PROXY_RULES = 30
MAX_PROXY_REWRITES = 20
PROXY_MATCH_TYPES = {"exact", "prefix"}
PROXY_ACTIONS = {"passthrough", "block", "bounce"}

# "site" apps (static HTML/CSS/JS, optionally PHP) can have many files, which
# doesn't fit in a single annotation the way JS source does -- their file
# contents live on disk instead, keyed by app name.
SITES_DATA_DIR = Path(
    os.environ.get("SPINAPP_UI_SITES_DATA_DIR", "/opt/spinapp-ui/sites-data")
)
APP_KIND_ANNOTATION = "spinapp-ui.dev/app-kind"
MAX_SITE_FILES = 50
MAX_SITE_FILE_BYTES = 8_000_000
MAX_SITE_TOTAL_BYTES = 30_000_000

# Extensions whose content the editor sends/receives as base64 rather than
# raw text (images, PDFs, fonts -- anything not editable in Monaco).
BINARY_SITE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "ico", "bmp",
    "pdf", "woff", "woff2", "ttf", "otf", "eot",
    "zip", "gz", "mp3", "mp4", "webm",
}


def is_binary_site_path(path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in BINARY_SITE_EXTENSIONS

STATIC_FILESERVER_SOURCE = {
    "url": "https://github.com/spinframework/spin-fileserver/releases/download/v0.3.0/spin_static_fs.wasm",
    "digest": "sha256:ef88708817e107bf49985c7cefe4dd1f199bf26f6727819183d5c996baa3d148",
}
PHP_CGI_SOURCE = {
    "url": "https://github.com/vmware-labs/webassembly-language-runtimes/releases/download/php%2F8.2.6%2B20230714-11be424/php-cgi-8.2.6-slim.wasm",
    "digest": "sha256:4fd2e8c42ae529ba72f88a0f1e46de1fc69a4b4f01e01fedd65ca966b8ffe6fa",
}

SITE_PATH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]*$")

# "cron" jobs are a different resource kind entirely (batch/v1 CronJob, not
# SpinApp -- SpinKube has no cron/timer trigger, only http), so they're
# listed/edited/deleted separately from list_spinapps() et al. Like "proxy"
# apps, the user's own code isn't what actually gets deployed: it's stored
# as an annotation on the CronJob (source of truth for the editor, same
# 256KiB-budget reasoning as JS_SOURCE_ANNOTATION) and combined with the
# optional "メール送信" tab's send_mail() helper (see
# render_cron_job_source()) into the real file that goes in the ConfigMap
# mounted into the pod. python:3.12-slim doubles as the bash image too --
# it already has both /usr/bin/bash and python3 (confirmed empirically; no
# image ships bash+curl together without a custom build), so bash's own
# send_mail() shells out to a python3 heredoc rather than using curl.
CRON_PYTHON_IMAGE = os.environ.get("SPINAPP_UI_CRON_PYTHON_IMAGE", "python:3.12-slim")
CRON_CONFIGMAP_SUFFIX = "-cron-src"
CRON_CODE_ANNOTATION = "spinapp-ui.dev/cron-code"
CRON_LANGUAGE_ANNOTATION = "spinapp-ui.dev/cron-language"
CRON_LANGUAGES = {"python", "bash"}
CRON_DEFAULT_CODE = {
    "python": 'import datetime\n\nprint(f"hello from cron at {datetime.datetime.now()}")\n',
    "bash": 'echo "hello from cron at $(date)"\n',
}
MAX_CRON_SOURCE_BYTES = 150_000

# "メール送信" tab: From/To only -- subject/body are deliberately left to
# the user's own code (they're normally built from other variables), which
# just needs to set SUBJECT/BODY itself and call
# send_mail(TO_ADDR, SUBJECT, BODY). Stored as its own small annotation
# (JSON, base64) so the edit page can tell the tab was used at all vs. a
# plain cron job; SMTP server/login themselves are global
# (see SMTP_HOST et al above), not part of this.
CRON_MAIL_ANNOTATION = "spinapp-ui.dev/cron-mail"
MAX_CRON_MAIL_BYTES = 20_000
# Generous but bounded, so a stuck script can't tie up the node forever, and
# a runaway job can't multiply -- one attempt, no retries, no overlap.
CRON_ACTIVE_DEADLINE_SECONDS = 300
MAX_CRON_LOG_LINES = 500
MAX_CRON_RUNS_SHOWN = 10

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
EXTERNAL_HOST_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$")


def is_valid_name(value):
    """NAME_RE.match(value) directly is fine for every *form*-sourced name
    (request.form.get(..., "") always returns a str), but every
    /internal/* endpoint reads its payload from request.get_json()
    instead, where a field can come back as any JSON type -- an
    int/list/None "name" would make re.Pattern.match() raise TypeError
    (not return a clean non-match), turning a malformed or malicious peer
    payload into an unhandled 500 instead of the intended "invalid
    payload" 400. Use this instead of a bare NAME_RE.match(...) anywhere
    the value didn't just come from request.form/request.args (which are
    always strings already)."""
    return isinstance(value, str) and bool(NAME_RE.match(value))


# First path segment of every route this console itself serves (see the
# @app.route(...) decorators throughout this file) -- blocked as an app
# name at *creation* time only (not edit: an app's name is immutable once
# created, so an already-existing app never needs re-checking). Without
# this, CONSOLE_VIA_TRAEFIK's Host(console_host) route (priority 1,
# deliberately low so it never outranks a real app's more specific
# Host+PathPrefix route -- see ensure_forwardauth_reachability()) can
# itself get outranked: an app with no custom FQDN gets a bare
# PathPrefix('/<name>') rule with no explicit priority, and Traefik's
# auto-computed priority for that (based on the rule string's length) is
# *always* higher than an explicit priority: 1. An app named e.g. "login"
# would then intercept every request to the console's own /login --
# phishing a fake login page, or just denial of service against the real
# one. "static" is Flask's own default /static/<path:filename> route
# (unused today -- no static/ directory exists -- but still registered).
RESERVED_APP_NAMES = {
    "login", "logout", "oidc", "config", "analytics",
    "apps", "sites", "proxies", "crons", "containers", "internal", "static",
    "export", "import",
}

# "container" apps run an arbitrary, user-chosen public Docker image as a
# plain apps/v1 Deployment -- not a SpinApp (SpinKube only runs Wasm
# components) and not a CronJob (this is long-running, not a scheduled
# script). This is the first app kind whose primary object is a Deployment,
# which means it's also the first kind that can name-collide with the
# Deployment/Service the Spin operator auto-generates for a same-named
# SpinApp -- see create_container()'s explicit cross-kind check for why that
# matters. Scoped deliberately narrow: public images only (no
# imagePullSecrets/private-registry auth), exactly one dedicated FQDN per
# app (a bare Host(...) match, no PathPrefix -- it owns the whole hostname),
# and exactly one externally-reachable port, always 443 (no per-app custom
# entryPoints/ports). CPU/memory limits are user-supplied, but only
# memory's request matches its limit (Guaranteed-style, no overcommit --
# an OOM-kill has no graceful degradation) -- CPU's request is instead a
# small fixed value regardless of its own limit (Burstable, deliberately
# overcommittable: CPU contention just means cgroup throttling, not
# anything getting killed). See _container_spec_for()'s comment for the
# full reasoning, including why omitting the CPU request entirely
# doesn't work (the API server defaults a missing request back to the
# limit).
CONTAINER_TLS_MODE_ANNOTATION = "spinapp-ui.dev/container-tls-mode"
CONTAINER_TLS_MODES = {"terminate", "passthrough"}
# Only meaningful in "terminate" mode -- see apply_upgrade_insecure_middleware().
CONTAINER_UPGRADE_INSECURE_ANNOTATION = "spinapp-ui.dev/upgrade-insecure"
CONTAINER_DATA_MOUNT_PATH = "/data"
# Persistent data lives at <this>/<name>/ directly on the node's own
# filesystem (a plain hostPath volume), not a PVC -- deliberate, not a
# shortcut: this cluster's local-path StorageClass doesn't enforce any
# actual disk quota from the "storage" a PVC requests (confirmed live --
# it's purely advisory), and it also names each PV's backing directory
# after an internal PV UID, not the app's own name, so there was never a
# way to get a human-readable, predictable path out of it. A raw hostPath
# gives up nothing real (no quota enforcement either way) and directly
# provides the predictable path callers actually want. Every replica of a
# multi-replica app mounts this same path -- always safe to schedule since
# this is a single-node cluster, but concurrent-write safety is still up
# to the image (see the replicas field's help text).
CONTAINER_DATA_BASE_DIR = Path(os.environ.get("SPINAPP_UI_CONTAINER_DATA_DIR", "/data/containers"))
CPU_QUANTITY_RE = re.compile(r"^[1-9]\d*m?$")
MEM_QUANTITY_RE = re.compile(r"^[1-9]\d*(Mi|Gi)$")
# The fixed CPU *request* every "container" app's containers use,
# regardless of their own user-set cpu_limit -- see _container_spec_
# for()'s comment for why this deliberately isn't cpu_limit itself
# (CPU overcommit) or omitted entirely (doesn't work -- the API server
# defaults a missing request back to the limit).
CONTAINER_CPU_REQUEST = "10m"
# The raw-YAML edit mode's yaml_manifest field had no size check at all
# (of any kind) until the 2026-08 security audit found it -- every other
# free-text field in this app (MAX_JS_SOURCE_BYTES, MAX_PROXY_CONFIG_
# BYTES, MAX_SITE_TOTAL_BYTES, ...) already had one. Generous (a real
# Deployment+Service pair is a few KB), just bounded.
MAX_CONTAINER_YAML_BYTES = 500_000
# A generous but bounded cap -- this is a single-node cluster, so nothing
# stops cpu_limit/mem_limit * replicas from starving every other app on the
# node if left unbounded (same "bounded, not unlimited" reasoning as
# CRON_ACTIVE_DEADLINE_SECONDS).
MAX_CONTAINER_REPLICAS = 10
MAX_CONTAINER_LOG_LINES = 500
# Same "bounded, not unlimited" reasoning as MAX_CONTAINER_REPLICAS --
# sidecar containers (see _parse_sidecars_json()) share the same Pod, so
# nothing else stops an unbounded number of them from starving the node.
MAX_CONTAINER_SIDECARS = 6
ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# "Extra ports" -- TCP/UDP ports a "container" app exposes directly via a
# dedicated LoadBalancer Service (see _container_extra_ports_service_
# manifest()), bypassing Traefik entirely: no FQDN-based Host filtering,
# no TLS. Exists for protocols that aren't HTTP(S) and can't be routed by
# Traefik at all -- the motivating case is a VPN container needing raw
# UDP/500 and UDP/4500 for L2TP/IPsec. Deliberately a dedicated,
# server-validated form field (like fqdn/port/tls_mode), never something
# a "container" app's own YAML can set directly -- see validate_
# container_security_policy()'s Service `type` restriction (ClusterIP
# only) for why: that check exists specifically so a crafted/careless
# YAML submission can't stand up a NodePort/LoadBalancer Service and
# bypass Traefik/the host firewall (same DNAT-bypasses-ufw issue as
# k3s's own klipper-lb -- see TECHNICAL_NOTES.md's HTTPS section). Extra
# ports work the same way *on purpose*, but only for the specific,
# bounded, admin-entered port list below -- never for an arbitrary
# Service a user's own YAML could shape.
MAX_CONTAINER_EXTRA_PORTS = 10
CONTAINER_EXTRA_PORT_PROTOCOLS = {"tcp", "udp"}
# Ports statically reserved so an extra_ports entry can never even be
# *submitted* against them, regardless of whether anything is currently
# listening there. 80/443: Traefik's own LoadBalancer Service (see
# ensure_forwardauth_reachability()/sync_traefik_web_exposure()). The
# rest were added after a live security-audit test (2026-08-11) confirmed
# a "container" app's extra_ports entry can actually *win* a host port
# away from another hostPath/hostNetwork-bound service, not just fail
# harmlessly: pointing an extra_ports entry at the local OCI registry's
# port (5000, hostNetwork: true) left the real registry Pod stuck
# Pending and the requested port answering from the attacker's own
# container instead, within seconds -- recovered immediately once the
# squatting Service was deleted, but nothing stopped it from being
# created in the first place. Unlike find_extra_port_owner() below (which
# only ever sees *other "container" apps'* own extra_ports Services),
# nothing else in this codebase enumerates every host-bound port a
# system component might use, so those have to be named here by hand:
# 22 (SSH), 5000 (the local registry), 6443 (k3s/kube-apiserver), 10250
# (kubelet). This matters even for a port nothing is using *right now* --
# a squatter only needs to grab the port once, ahead of time, then sit
# there; the next time the legitimate owner restarts for any reason (a
# k3s upgrade restarts the apiserver/kubelet both -- see upgrade.sh) it
# loses the race for its own port instead of reclaiming it. This
# console's own listen port is included dynamically for the same reason:
# losing it to a squatter on the next restart would lock out console
# access entirely. See is_host_port_in_use() alongside this for the
# complementary *live* check (catches a currently-listening service this
# static list doesn't happen to name).
CONTAINER_RESERVED_EXTRA_PORTS = {80, 443, 22, 5000, 6443, 10250, int(os.environ.get("PORT", "8080"))}

def find_container_template(key):
    """The config dict for user-saved template `key` (see
    save_container_as_template()), or None if there's no match. Same
    config shape get_container_config() returns, plus "label" and
    "key"."""
    return next((t for t in CONTAINER_USER_TEMPLATES if t.get("key") == key), None)


# Traefik router names look like "<namespace>-<ingressroute-name>-<hash>@kubernetescrd".
# Since our IngressRoute name is always the SpinApp name, this recovers it.
ROUTER_NAME_RE = re.compile(
    r"^" + re.escape(NAMESPACE) + r"-(.+)-[0-9a-f]+@kubernetescrd$"
)
# IngressRoute names that exist but aren't a user-created app -- kept out
# of analytics entirely (excluded from every chart/table/the app filter
# dropdown, not just hidden in the UI) so they don't inflate "total
# requests"/pollute per-app breakdowns with console-internal traffic.
# "spindeck-internal" is ensure_forwardauth_reachability()'s own
# IngressRoute (the /_spindeck/* forwardauth+OIDC endpoints, hit on every
# request to every OIDC-protected app, plus the console itself when
# CONSOLE_VIA_TRAEFIK is on) -- not an app anyone registered.
ANALYTICS_EXCLUDED_APPS = {"spindeck-internal"}
# Just the keys now, in display order -- analytics.html looks up each
# one's label via t("analytics.range_" ~ key) so it's translated, rather
# than baking a Japanese label in here (this constant used to be a list
# of (key, label) pairs).
RANGE_OPTIONS = ["1h", "6h", "24h", "168h"]
RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24, "168h": 168}

# Persistent (hostPath-backed) copy of Traefik's JSON access log -- see
# deploy/k8s-manifests/traefik-helmchartconfig.yaml. Read directly off disk
# since this console and Traefik run on the same single node.
TRAEFIK_ACCESS_LOG_PATH = Path(
    os.environ.get("SPINAPP_UI_TRAEFIK_ACCESS_LOG", "/var/log/traefik/access.log")
)
MAX_ACCESS_LOG_READ_BYTES = 20_000_000
# How long TRAEFIK_ACCESS_LOG_PATH can go without a new line before the
# Analytics page flags it as possibly stale -- see get_traefik_log_diagnostics().
TRAEFIK_LOG_STALE_SECONDS = 3600

# Traefik's access log timestamps are always UTC (its pod's clock, independent
# of the host's timezone), so convert for display here rather than relying on
# the container's own local time.
DISPLAY_TZ = ZoneInfo("Asia/Tokyo")


def kubectl(args, input_text=None):
    env = dict(os.environ, KUBECONFIG=KUBECONFIG)
    return subprocess.run(
        ["kubectl", *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


# Shared secret every node in the mesh must be configured with identically
# (see /config's "Peer nodes" section / docs/TECHNICAL_NOTES.md). Guards the
# /internal/* routes below instead of the OIDC session, since peers call
# those directly, without a browser/Keycloak session.
PEER_TOKEN = os.environ.get("SPINAPP_UI_PEER_TOKEN", "")

# Deploys/builds can take a while (npm build + registry push); replication
# runs in a background thread so it never blocks the user's own request, so
# a generous timeout here costs nothing.
PEER_REQUEST_TIMEOUT = 300
PEER_RETRY_ATTEMPTS = 3
PEER_RETRY_DELAY_SECONDS = 15


def _peer_authorized():
    if not PEER_TOKEN:
        return False
    supplied = request.headers.get("X-SpinDeck-Peer-Token", "")
    return secrets.compare_digest(supplied, PEER_TOKEN)


def _replicate_one(peer, path, payload):
    url = peer.rstrip("/") + path
    for attempt in range(1, PEER_RETRY_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"X-SpinDeck-Peer-Token": PEER_TOKEN},
                timeout=PEER_REQUEST_TIMEOUT,
            )
            if resp.ok:
                return
            print(
                f"[peer-sync] {url} attempt {attempt}/{PEER_RETRY_ATTEMPTS} "
                f"failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        except requests.RequestException as exc:
            print(
                f"[peer-sync] {url} attempt {attempt}/{PEER_RETRY_ATTEMPTS} "
                f"error: {exc}"
            )
        if attempt < PEER_RETRY_ATTEMPTS:
            time.sleep(PEER_RETRY_DELAY_SECONDS)
    print(f"[peer-sync] giving up on {url} after {PEER_RETRY_ATTEMPTS} attempts")


def replicate_to_peers(path, payload):
    """Fire-and-forget fan-out of a deploy/delete action to every configured
    peer node (see PEERS above). Best-effort: local success never waits on
    or fails because of a peer being unreachable -- each peer call retries a
    few times on its own background thread and just logs if it still fails.
    Only ever called from the user-facing routes, never from the /internal/*
    handlers themselves, so a full mesh of peers can't fan out in a loop."""
    if not PEER_TOKEN or not PEERS:
        return
    for peer in list(PEERS):
        threading.Thread(
            target=_replicate_one, args=(peer, path, payload), daemon=True
        ).start()


OIDC_PUBLIC_ENDPOINTS = {
    "login",
    "oidc_callback",
    # Per-app OIDC auth (see below): anonymous visitors to an OIDC-protected
    # app hit these with no console session, so check_auth() must not bounce
    # them to the *console* login.
    "spindeck_forwardauth",
    "spindeck_oidc_login",
    "spindeck_oidc_callback",
    # Browsers request this automatically alongside every page load,
    # completely independent of whatever the user actually navigated to --
    # letting it fall through to check_auth()'s default "not logged in"
    # handling would overwrite session["next"] with "/favicon.ico" itself
    # whenever that request lands after the real page's, sending a
    # freshly-logged-in user to a 404 instead of the page they meant to
    # open. Confirmed live as a real, if intermittent, bug.
    "favicon",
    # The language toggle itself has to be reachable with no session at
    # all -- an anonymous visitor sitting on the login page is exactly
    # who most needs to switch language before they can even read the
    # form, let alone log in.
    "set_lang",
}

# Authenticated via X-SpinDeck-Peer-Token (see _peer_authorized()) instead of
# the OIDC session -- peers call these directly with no browser involved.
INTERNAL_PEER_ENDPOINTS = {
    "internal_apply_js",
    "internal_apply_proxy",
    "internal_apply_site",
    "internal_delete_app",
    "internal_apply_cron",
    "internal_delete_cron",
    "internal_apply_container",
    "internal_delete_container",
}


@app.before_request
def resolve_request_lang():
    """Sets g.lang for this request -- the explicit LANG_COOKIE_NAME
    cookie if one was ever set (via set_lang() below), otherwise a
    best-match against the browser's own Accept-Language header,
    otherwise i18n.DEFAULT_LANG. Registered *before* check_auth() (Flask
    runs before_request hooks in registration order) so g.lang is always
    set by the time any view -- including the login page itself, and
    check_auth()'s own render_template() calls -- runs, not just ones
    reachable after a successful login."""
    g.lang = resolve_lang(
        request.cookies.get(LANG_COOKIE_NAME), request.headers.get("Accept-Language")
    )


@app.route("/set-lang/<lang>", methods=["POST"])
@csrf.exempt
def set_lang(lang):
    """Sets LANG_COOKIE_NAME and redirects back to wherever the toggle was
    clicked from. POST (not GET) specifically so this can never be
    triggered by a bare link/prefetch/crawler -- the only client is the
    toggle form in base.html's sidebar, which submits the current page's
    own path as a hidden field rather than relying on the Referer header
    (present but not guaranteed, and easy to spoof either way -- this
    isn't a security boundary, just avoids bouncing to "/" on every
    switch). Exempted from CSRF the same way every other no-session-
    required POST in this app is: there's no session-scoped state here
    for a forged cross-site request to abuse, it only ever flips a
    display preference cookie.
    """
    # next is only ever meant to be the current page's own path (see
    # base.html/login.html's hidden field, which sets it to request.path,
    # never a full URL) -- reject anything else so a forged cross-site
    # POST (this route is CSRF-exempt) can't use it as an open redirect.
    # "//" or "/\" are rejected too: browsers treat a leading "//" as
    # protocol-relative (redirects off-host) and some historically
    # normalized a leading "/\" the same way.
    next_path = request.form.get("next", "")
    if not next_path.startswith("/") or next_path.startswith(("//", "/\\")):
        next_path = url_for("index")
    response = redirect(next_path)
    if lang in SUPPORTED_LANGS:
        response.set_cookie(
            LANG_COOKIE_NAME, lang, max_age=60 * 60 * 24 * 365, samesite="Lax"
        )
    return response


@app.before_request
def check_auth():
    if request.endpoint in OIDC_PUBLIC_ENDPOINTS:
        return None
    if request.endpoint in INTERNAL_PEER_ENDPOINTS:
        return None
    if not session.get("user"):
        session["next"] = request.full_path if request.query_string else request.path
        return redirect(url_for("login"))
    # "logout" stays reachable even for a not-allow-listed user -- otherwise
    # someone locked out by CONSOLE_ALLOWED_USERS has no way to clear their
    # session and try a different Keycloak account. CONSOLE_ALLOWED_USERS
    # itself is OIDC-only semantics (an allow-list narrowing down a wider
    # Keycloak realm) -- it has no meaning against CONSOLE_BASIC_AUTH_USERS,
    # where the registered-account list already *is* the allow-list.
    if (
        request.endpoint != "logout"
        and CONSOLE_AUTH_MODE == "oidc"
        and CONSOLE_ALLOWED_USERS
        and session["user"]["name"] not in CONSOLE_ALLOWED_USERS
    ):
        return render_template("console_forbidden.html", user=session["user"]), 403
    return None


@app.context_processor
def inject_user():
    return {
        "current_user": session.get("user"),
        "external_host": EXTERNAL_HOST,
        "spindeck_version": SPINDECK_VERSION,
        "t": lambda key, **kwargs: _t(g.lang, key, **kwargs),
        "current_lang": g.lang,
    }



# Keyed by the *submitted username* rather than source IP: request.remote_addr
# isn't trustworthy once CONSOLE_VIA_TRAEFIK is on (no ProxyFix/X-Forwarded-
# For handling here, deliberately -- trusting that header without also
# restricting which upstream is allowed to set it would let an attacker
# forge a fresh "IP" per attempt and bypass an IP-keyed limit entirely). A
# username-keyed limit still stops the attack that actually matters here --
# brute-forcing one account's password -- and degrades gracefully if a
# request is proxied. In-memory only (resets on restart/across the process,
# not persisted) -- acceptable since this is a single-process deployment
# and the goal is slowing down online guessing, not a durable audit log.
_LOGIN_FAILURES = {}
LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_WINDOW_SECONDS = 300
# _login_rate_limited() only ever prunes the *list* attached to a username
# that's actually being re-checked -- a key itself is never removed just
# because its list emptied out. That's fine for a real, repeatedly-tried
# username, but /login is unauthenticated and this dict is keyed on
# whatever string was submitted: a script sending a POST with a fresh,
# never-repeated random username every time would add a new permanent key
# forever and never trigger any pruning of it, growing this dict without
# bound for as long as the process stays up (found in the 2026-08 audit,
# specifically prompted by "could this take the console down" -- this is
# real, not hypothetical, in Basic-Auth console-login mode; OIDC mode
# never reaches this code path at all, see login()). Capped here instead:
# once at the limit, the single oldest tracked username is evicted before
# adding a new one (dict insertion order in CPython 3.7+ -- re-assigning
# an *existing* key's value, which is all _login_rate_limited() ever
# does, doesn't move it, so iteration order stays a reasonable proxy for
# "first added").
MAX_TRACKED_LOGIN_USERNAMES = 10_000


def _login_rate_limited(username):
    now = time.time()
    attempts = [t for t in _LOGIN_FAILURES.get(username, []) if now - t < LOGIN_LOCKOUT_WINDOW_SECONDS]
    _LOGIN_FAILURES[username] = attempts
    return len(attempts) >= LOGIN_LOCKOUT_THRESHOLD


def _record_login_failure(username):
    if username not in _LOGIN_FAILURES and len(_LOGIN_FAILURES) >= MAX_TRACKED_LOGIN_USERNAMES:
        _LOGIN_FAILURES.pop(next(iter(_LOGIN_FAILURES)), None)
    _LOGIN_FAILURES.setdefault(username, []).append(time.time())


@app.route("/favicon.ico")
def favicon():
    # No actual icon file -- just avoids the 404-via-clobbered-session[
    # "next"] bug this route's presence in OIDC_PUBLIC_ENDPOINTS exists to
    # fix (see that set's comment). 204 is a normal, cache-friendly answer
    # for "no favicon here" that every browser already handles fine.
    return "", 204


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    if CONSOLE_AUTH_MODE != "basic":
        return oauth.keycloak.authorize_redirect(OIDC_REDIRECT_URI)

    # Basic-Auth mode: a local username+password form, no Keycloak
    # involved at all. Accounts are CONSOLE_BASIC_AUTH_USERS (managed via
    # /config's ローカルアカウント管理 section). Error message is
    # deliberately generic -- doesn't reveal whether the username or the
    # password was wrong.
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if _login_rate_limited(username):
            error = "試行回数が多すぎます。しばらくしてから再度お試しください。"
        else:
            account = next(
                (a for a in CONSOLE_BASIC_AUTH_USERS if a["username"] == username), None
            )
            if account and verify_apr1_password(password, account["password_hash"]):
                _LOGIN_FAILURES.pop(username, None)
                session.permanent = True
                session["user"] = {"sub": None, "name": account["username"], "email": None}
                return redirect(session.pop("next", None) or url_for("index"))
            _record_login_failure(username)
            error = "ユーザー名またはパスワードが正しくありません。"
    return render_template("login.html", error=error), (401 if error else 200)


@app.route("/oidc/callback")
def oidc_callback():
    if request.args.get("error"):
        return (
            f"ログインがキャンセルまたは拒否されました ({esc(request.args['error'])})。"
            f'<a href="/login">再試行</a>',
            400,
        )
    try:
        token = oauth.keycloak.authorize_access_token()
    except Exception as exc:  # noqa: BLE001 - any OAuth/state/network failure
        return (
            f"ログインに失敗しました: {esc(str(exc))} <a href=\"/login\">再試行</a>",
            400,
        )
    session.permanent = True
    session["user"] = {
        "sub": token.get("userinfo", {}).get("sub"),
        "name": token.get("userinfo", {}).get("preferred_username")
        or token.get("userinfo", {}).get("name")
        or "user",
        "email": token.get("userinfo", {}).get("email"),
    }
    session["id_token"] = token.get("id_token")
    next_path = session.pop("next", None) or url_for("index")
    return redirect(next_path)


@app.route("/logout")
def logout():
    # Whether *this* session actually authenticated via Keycloak -- not
    # the current global CONSOLE_AUTH_MODE. Those can disagree: an admin
    # can flip the console between basic/oidc mode at any time without
    # invalidating already-logged-in sessions (mode-switching is
    # deliberately hot -- see CONSOLE_AUTH_MODE's own comment), so a
    # Basic-Auth session logging out *after* the admin switches the
    # console to OIDC mode would otherwise fall into the Keycloak branch
    # below despite never having gone through Keycloak at all.
    # "id_token" in session is only ever set by oidc_callback() -- never
    # by login()'s Basic-Auth path -- so it's a reliable per-session
    # marker regardless of what CONSOLE_AUTH_MODE currently says.
    # Confirmed live as a real bug: the previous CONSOLE_AUTH_MODE-based
    # branch made oauth.keycloak.load_server_metadata() below fire a real
    # outbound HTTP request to OIDC_ISSUER for a session that never used
    # OIDC -- if that issuer is unreachable or still install.sh's
    # placeholder value, the request has no timeout and hangs
    # indefinitely, leaving the browser stuck on "読み込み中" forever
    # instead of logging out.
    if "id_token" not in session:
        session.clear()
        return redirect(url_for("login"))
    id_token = session.pop("id_token", None)
    session.clear()
    metadata = oauth.keycloak.load_server_metadata()
    end_session_endpoint = metadata.get("end_session_endpoint")
    if end_session_endpoint:
        params = {"post_logout_redirect_uri": OIDC_REDIRECT_URI.rsplit("/oidc/callback", 1)[0]}
        if id_token:
            params["id_token_hint"] = id_token
        from urllib.parse import urlencode

        return redirect(f"{end_session_endpoint}?{urlencode(params)}")
    return redirect(url_for("index"))


# --- Per-app OIDC auth (allow-listed users) ---------------------------------
#
# Independent of both the console's own Keycloak login above and the
# per-app Basic Auth further down (see apply_basic_auth_secret()). An app
# opts in via its "spinapp-ui.dev/auth-mode: oidc" annotation (see
# resolve_auth_mode()), which gets it a <name>-oidc-auth forwardAuth
# Middleware (apply_oidc_auth_middleware()) that Traefik calls on every
# request. That Middleware's address always points back at this same
# process's /_spindeck/forwardauth route below, reached via a small
# Service+Endpoints (ensure_forwardauth_reachability()) using the same
# selectorless-Service technique apply_ws_bypass_route() already uses to
# reach a plain IP:port from inside the cluster.
#
# Deliberately reuses the console's own Keycloak client (oauth.keycloak) --
# the point is "any of these Keycloak users may view this app", not a
# separate identity provider -- but keeps its own session key
# (session["apps_user"], not session["user"]) and its own login/callback
# routes, so a console session and an app session never get confused with
# each other even when both cookies happen to share a browser.

_APP_AUTH_CACHE_LOCK = threading.Lock()
_APP_AUTH_CACHE = {}  # name -> (auth_mode, frozenset(allowed usernames))
_APP_AUTH_CACHE_LOADED_AT = 0.0
APP_AUTH_CACHE_TTL_SECONDS = 5


def _collect_auth_configs(kubectl_args):
    """Shared body of get_app_auth_config()'s two sources (SpinApp and
    "container" Deployment) -- same AUTH_MODE_ANNOTATION/
    AUTH_OIDC_USERS_ANNOTATION/FQDN_ANNOTATION convention on both, just a
    different Kubernetes kind. Returns (dict, ok) rather than raising, so
    one source failing doesn't discard the other."""
    result = kubectl(kubectl_args)
    if result.returncode != 0:
        return {}, False
    configs = {}
    for item in json.loads(result.stdout).get("items", []):
        annotations = item["metadata"].get("annotations", {}) or {}
        mode = resolve_auth_mode(annotations)
        users = frozenset(
            u.strip()
            for u in annotations.get(AUTH_OIDC_USERS_ANNOTATION, "").split(",")
            if u.strip()
        )
        fqdn = annotations.get(FQDN_ANNOTATION, "")
        configs[item["metadata"]["name"]] = (mode, users, fqdn)
    return configs, True


def get_app_auth_config(name):
    """Return (auth_mode, frozenset(allowed usernames), fqdn) for a SpinApp
    or "container" app (Deployment) -- same annotation convention, two
    different primary-object kinds (see _collect_auth_configs()). Called
    by spindeck_forwardauth() on *every* request to *every* OIDC-protected
    app, so a per-request `kubectl get` is out of the question -- instead
    this keeps a short-TTL cache of every app's auth config, refreshed
    with the same one-call-for-everything kubectl pattern list_spinapps()/
    list_container_apps() use. A stale cache means an allow-list change
    can take up to APP_AUTH_CACHE_TTL_SECONDS to take effect; if a
    refresh fails for one source, its half of the previous cache keeps
    serving rather than failing open (the other source's fresh half is
    still applied).

    The `fqdn` slot is what lets request_origin() trust a "container"
    app's own free-text FQDN (not just REGISTERED_FQDNS) for exactly the
    one app actually being accessed -- see spindeck_forwardauth()'s and
    spindeck_oidc_login()'s use of it.
    """
    global _APP_AUTH_CACHE_LOADED_AT
    with _APP_AUTH_CACHE_LOCK:
        if time.monotonic() - _APP_AUTH_CACHE_LOADED_AT > APP_AUTH_CACHE_TTL_SECONDS:
            spinapp_configs, spinapp_ok = _collect_auth_configs(
                ["get", "spinapp", "-n", NAMESPACE, "-o", "json"]
            )
            container_configs, container_ok = _collect_auth_configs(
                ["get", "deployment", "-n", NAMESPACE, "-l", APP_LABEL, "-o", "json"]
            )
            if spinapp_ok and container_ok:
                # Both sources fresh -- full replace, so a deleted/renamed
                # app correctly drops out of the cache too.
                _APP_AUTH_CACHE.clear()
                _APP_AUTH_CACHE.update(spinapp_configs)
                _APP_AUTH_CACHE.update(container_configs)
                _APP_AUTH_CACHE_LOADED_AT = time.monotonic()
            elif spinapp_ok or container_ok:
                # Only one source refreshed -- merge it in (better than
                # nothing) but deliberately leave _APP_AUTH_CACHE_LOADED_AT
                # unset, so the next request retries the failed half again
                # right away instead of serving it stale for the full TTL.
                _APP_AUTH_CACHE.update(spinapp_configs)
                _APP_AUTH_CACHE.update(container_configs)
            # else: both failed, previous cache keeps serving untouched.
        return _APP_AUTH_CACHE.get(name, ("none", frozenset(), ""))


def request_origin(trusted_host=None):
    """The externally-visible https://<host> this request should be treated
    as having come in as. Host comes from X-Forwarded-Host (Traefik sets it
    on every proxied request, not just forwardAuth subrequests). Apps can
    be reached at EXTERNAL_HOST *or* a custom per-app FQDN
    (FQDN_ANNOTATION); building redirect_uri/next URLs from this instead of
    a single fixed host is what lets OIDC auth (unlike per-app Basic Auth's
    fixed Traefik Middleware, this needs a browser round-trip to Keycloak)
    work under any of them.

    X-Forwarded-Host is only trusted as far as matching it against the
    admin's own configured host allowlist (EXTERNAL_HOST/
    MANUAL_EXTERNAL_HOST/REGISTERED_FQDNS) -- falling back to EXTERNAL_HOST
    for anything else -- rather than used verbatim. Confirmed live in this
    exact deployment (k3s + Traefik behind klipper-lb's LoadBalancer
    Service) that this header is *not* actually gated by entryPoint
    forwardedHeaders.trustedIPs the way it's documented to be: a request
    with a forged X-Forwarded-Host reached this function with that forged
    value intact regardless of trustedIPs/trustForwardHeader config, the
    same way spindeck_forwardauth()'s app-name resolution was (see that
    function's own docstring) before it stopped trusting a header for
    that. An arbitrary attacker-chosen host here would otherwise become
    the redirect_uri sent to Keycloak -- bounded somewhat by Keycloak's own
    exact-match redirect_uri allowlist, but this closes it at the source
    instead of relying on that as the only backstop.

    `trusted_host`, if given, is trusted as an *additional* valid host on
    top of the admin's own allowlist -- used by spindeck_forwardauth()/
    spindeck_oidc_login() to also accept the specific app actually being
    accessed's own FQDN (looked up via get_app_auth_config(), keyed off
    the same trusted `app` identifier those two already establish some
    other way -- never taken from a request header or an unauthenticated
    query param directly). This is what lets a "container" app's free-text
    FQDN (see find_fqdn_owner()'s docstring -- unlike REGISTERED_FQDNS,
    deliberately not restricted to the admin's pre-registered list) work
    with OIDC without widening trust to anything an attacker could name:
    the value still only takes effect if the *actual* X-Forwarded-Host of
    this exact request already equals it, so passing an arbitrary/unowned
    `trusted_host` changes nothing for a request that didn't already
    arrive on that host.

    Scheme is hardcoded to https, deliberately NOT tied to TRAEFIK_SCHEME
    (see route_entrypoints()/route_tls_block(), /config's "HTTPS"
    section) and never read from X-Forwarded-Proto. TRAEFIK_SCHEME
    describes what entryPoint Traefik itself binds to -- it is NOT the
    same question as "what scheme did the browser actually use", because
    a front-line reverse proxy terminating TLS in front of Traefik (this
    project's original architecture, and still how some deployments run
    even after native Traefik HTTPS support was added -- retiring that
    front proxy is a separate, optional step an admin takes on their own
    schedule) means Traefik itself keeps speaking plain HTTP
    (TRAEFIK_SCHEME="http") while every visitor's address bar, and
    Keycloak's registered redirect_uri, say https the entire time.
    Confirmed live: with TRAEFIK_SCHEME="http" behind such a front proxy,
    per-app OIDC's redirect_uri came out http://..., which Keycloak
    rejected outright. X-Forwarded-Proto isn't trusted here for the same
    reason X-Forwarded-Host and X-Forwarded-Uri aren't elsewhere in this
    file (see this function's own X-Forwarded-Host handling below, and
    spindeck_forwardauth()'s docstring) -- confirmed unreliable in this
    exact deployment regardless of entryPoint forwardedHeaders.trustedIPs
    config. In the long run, once an admin actually retires any
    front-line proxy and relies solely on TRAEFIK_SCHEME="https" for
    native Traefik TLS termination, hardcoding https here remains
    correct -- the one deployment shape this breaks is a deliberately
    all-plain-HTTP setup (no TLS anywhere, TRAEFIK_SCHEME="http", no
    front proxy) trying to use per-app OIDC, which is an unusual
    combination Keycloak itself doesn't love regardless. The console's
    own OIDC_REDIRECT_URI/EXTERNAL_HOST-based URLs elsewhere in this file
    are separately, statically configured (env-var values, not computed
    here) and unaffected by this.
    """
    host = request.headers.get("X-Forwarded-Host", request.host)
    valid_hosts = {EXTERNAL_HOST, MANUAL_EXTERNAL_HOST, *REGISTERED_FQDNS}
    if trusted_host:
        valid_hosts.add(trusted_host)
    if host not in valid_hosts:
        host = EXTERNAL_HOST
    return f"https://{host}"


@app.route("/_spindeck/forwardauth")
def spindeck_forwardauth():
    """Traefik-only: the address every <name>-oidc-auth forwardAuth
    Middleware points at. Traefik calls this once per request to a
    protected app and forwards it verbatim if we return 2xx; any other
    status (redirect to log in, or a 403 page) goes straight back to the
    browser instead.

    The app being accessed comes from this endpoint's own "app" query
    param, baked into the Middleware's `address` itself at creation time
    (see apply_oidc_auth_middleware()) -- NOT from X-Forwarded-Uri, despite
    that header nominally describing the same thing (and matching the
    PathPrefix(`/name`) routing rule apply_route() itself uses). Confirmed
    live that this matters: sending a request straight to Traefik with a
    forged X-Forwarded-Uri for a *different, unprotected* app got a
    same-shape response back regardless of trustForwardHeader/entryPoint
    trustedIPs -- i.e. whatever decided the auth outcome here was reading
    the attacker-controlled header, not Traefik's own routing decision.
    The query param can't be spoofed the same way: it's a fixed string this
    process itself wrote into the Middleware object, and Traefik always
    calls the *same* address for a given Middleware regardless of what the
    inbound request contains.
    """
    forwarded_uri = request.headers.get("X-Forwarded-Uri", "/")
    app_name = request.args.get("app", "")

    mode, allowed_users, app_fqdn = get_app_auth_config(app_name)
    if mode != "oidc":
        # This Middleware is only ever attached to an app while its mode is
        # oidc, so landing here otherwise means get_app_auth_config()'s
        # cache raced a mode change -- fail closed instead of guessing.
        return "アクセスが拒否されました。設定を確認しています、しばらくしてから再度お試しください。", 403

    original_url = f"{request_origin(trusted_host=app_fqdn)}{forwarded_uri}"

    apps_user = session.get("apps_user")
    if not apps_user:
        # app_name is forwarded so spindeck_oidc_login() can look up this
        # same app_fqdn itself (it gets no other reliable app identifier --
        # see that route's docstring) -- not trusted any differently than
        # here: it only ever adds *that app's own* fqdn to the trusted set,
        # so naming a different app changes nothing unless the browser's
        # real Host already matches that app's own real fqdn anyway.
        return redirect(
            f"/_spindeck/oidc/login?app={quote(app_name, safe='')}"
            f"&next={quote(original_url, safe='')}"
        )

    if apps_user["name"] not in allowed_users:
        return render_template(
            "apps_forbidden.html", user=apps_user, app_name=app_name, next_url=original_url
        ), 403

    return Response(status=200)


@app.route("/_spindeck/oidc/login")
def spindeck_oidc_login():
    # `app`, if present, came from spindeck_forwardauth()'s own redirect --
    # see its docstring for why trusting the fqdn this resolves to doesn't
    # widen request_origin()'s actual trust boundary (it still only takes
    # effect if the real X-Forwarded-Host already matches). A visitor
    # hitting this route directly with no `app` (or a bogus one) just gets
    # no extra trusted host, same as before this existed.
    _, _, app_fqdn = get_app_auth_config(request.args.get("app", ""))
    origin = request_origin(trusted_host=app_fqdn)
    next_url = request.args.get("next", "")
    if not next_url.startswith(f"{origin}/"):
        next_url = f"{origin}/"  # ignore anything not pointing back to this same host
    session["apps_next"] = next_url
    # ?switch=1 (the apps_forbidden.html "別のユーザーでログイン" link) forces a
    # fresh credential prompt instead of Keycloak silently re-issuing a code
    # for the same SSO session that just got rejected as not-allow-listed.
    kwargs = {"prompt": "login"} if request.args.get("switch") == "1" else {}
    # redirect_uri must be on the same host the visitor is actually on --
    # not a single fixed EXTERNAL_HOST -- so this also works for apps
    # pinned to a custom FQDN (see FQDN_ANNOTATION). Each such host needs
    # "<origin>/_spindeck/oidc/callback" registered as a valid redirect URI
    # on the SPINAPP_UI_OIDC_CLIENT_ID client in Keycloak; Authlib remembers
    # exactly which one it used (keyed off the OAuth "state" it also stores
    # in the session here), so spindeck_oidc_callback() doesn't need to
    # recompute or re-pass it.
    return oauth.keycloak.authorize_redirect(f"{origin}/_spindeck/oidc/callback", **kwargs)


@app.route("/_spindeck/oidc/callback")
def spindeck_oidc_callback():
    if request.args.get("error"):
        return (
            f"ログインがキャンセルまたは拒否されました ({esc(request.args['error'])})。",
            400,
        )
    try:
        token = oauth.keycloak.authorize_access_token()
    except Exception as exc:  # noqa: BLE001 - any OAuth/state/network failure
        return f"ログインに失敗しました: {esc(str(exc))}", 400
    session.permanent = True
    session["apps_user"] = {
        "sub": token.get("userinfo", {}).get("sub"),
        "name": token.get("userinfo", {}).get("preferred_username")
        or token.get("userinfo", {}).get("name")
        or "user",
        "email": token.get("userinfo", {}).get("email"),
    }
    next_url = session.pop("apps_next", None) or f"https://{EXTERNAL_HOST}/"
    return redirect(next_url)


def list_spinapps():
    result = kubectl(["get", "spinapp", "-n", NAMESPACE, "-o", "json"])
    if result.returncode != 0:
        return [], result.stderr
    data = json.loads(result.stdout)
    apps = []
    for item in data.get("items", []):
        spec = item.get("spec", {})
        status = item.get("status", {})
        annotations = item["metadata"].get("annotations", {}) or {}
        conditions = status.get("conditions", [])
        available = next(
            (c for c in conditions if c.get("type") == "Available"), None
        )
        kind = annotations.get(APP_KIND_ANNOTATION) or (
            "js" if JS_SOURCE_ANNOTATION in annotations else "image"
        )
        fqdn = annotations.get(FQDN_ANNOTATION, "")
        name = item["metadata"]["name"]
        copy_url = {
            "site": f"/sites/new?from={name}",
            "proxy": f"/proxies/new?from={name}",
            "js": f"/apps/new?from={name}",
        }.get(kind)
        apps.append(
            {
                "name": name,
                "image": spec.get("image", ""),
                "executor": spec.get("executor", ""),
                "replicas": spec.get("replicas", 1),
                "ready_replicas": status.get("readyReplicas", 0),
                "available": bool(available and available.get("status") == "True"),
                "kind": kind,
                "editable": kind in ("js", "site", "proxy"),
                "auth_enabled": annotations.get(AUTH_ENABLED_ANNOTATION) == "true",
                "auth_mode": resolve_auth_mode(annotations),
                "fqdn": fqdn,
                "url": f"https://{fqdn or EXTERNAL_HOST}/{name}/",
                "copy_url": copy_url,
            }
        )
    return apps, None


APP_LABEL = "spinapp-ui.dev/app"
AUTH_ENABLED_ANNOTATION = "spinapp-ui.dev/auth-enabled"
AUTH_USERNAME_ANNOTATION = "spinapp-ui.dev/auth-username"
FQDN_ANNOTATION = "spinapp-ui.dev/fqdn"
# "none" | "basic" | "oidc". Authoritative once present; apps created before
# this annotation existed only have AUTH_ENABLED/USERNAME (basic-auth-only),
# so resolve_auth_mode() below infers "basic"/"none" from those instead.
AUTH_MODE_ANNOTATION = "spinapp-ui.dev/auth-mode"
# Comma-separated Keycloak usernames (preferred_username, matching
# session["apps_user"]["name"]/session["user"]["name"]) allowed to view an
# auth-mode=oidc app. Empty/unset when auth-mode isn't "oidc".
AUTH_OIDC_USERS_ANNOTATION = "spinapp-ui.dev/auth-oidc-users"


def resolve_auth_mode(annotations):
    return (
        annotations[AUTH_MODE_ANNOTATION]
        if annotations.get(AUTH_MODE_ANNOTATION) in ("none", "basic", "oidc")
        else ("basic" if annotations.get(AUTH_ENABLED_ANNOTATION) == "true" else "none")
    )


def build_auth_annotations(auth_mode, auth_username, oidc_users):
    """The full set of auth-related annotations for a SpinApp manifest,
    given a resolved mode. AUTH_ENABLED_ANNOTATION is still written (kept
    in sync with auth_mode) purely for older spinapp-ui code -- a peer
    still running a pre-oidc-mode version, mid-rollout -- to keep reading
    something sane; see internal_apply_js()/_proxy()/_site()'s auth_mode
    fallback for the reverse direction.

    auth_username/oidc_users are defensively coerced to strings here
    (not just trusted as already-validated) because every /internal/*
    caller sources them from a JSON request body, where a field can come
    back as any JSON type -- e.g. a peer sending "oidc_users": [1, 2]
    would otherwise make ",".join(oidc_users) raise TypeError (an
    unhandled 500) instead of just... including "1,2" as the annotation
    value, which is harmless (it can never match a real username anyway,
    same as any other nonsense string). The user-facing form-mode path
    never produces non-string items here to begin with (parse_oidc_users()
    only ever returns strings), so this never changes real behavior.
    """
    oidc_users = [u if isinstance(u, str) else str(u) for u in (oidc_users if isinstance(oidc_users, list) else [])]
    auth_username = auth_username if isinstance(auth_username, str) else str(auth_username or "")
    return {
        AUTH_MODE_ANNOTATION: auth_mode,
        AUTH_ENABLED_ANNOTATION: "true" if auth_mode == "basic" else "false",
        AUTH_USERNAME_ANNOTATION: auth_username if auth_mode == "basic" else "",
        AUTH_OIDC_USERS_ANNOTATION: ",".join(oidc_users) if auth_mode == "oidc" else "",
    }


def parse_oidc_users(raw_text):
    """Accepts the _auth_tab.html textarea's newline- (or comma-) separated
    usernames, trimmed and de-duplicated, preserving input order."""
    seen = []
    for part in re.split(r"[\n,]+", raw_text):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def oidc_users_text(annotations):
    """The inverse of parse_oidc_users()+join(",") -- one username per line,
    for pre-filling _auth_tab.html's textarea on edit."""
    return "\n".join(u for u in annotations.get(AUTH_OIDC_USERS_ANNOTATION, "").split(",") if u)


def _labeled(kind, meta_name, namespace, name, spec):
    return {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": kind,
        "metadata": {
            "name": meta_name,
            "namespace": namespace,
            "labels": {APP_LABEL: name},
        },
        "spec": spec,
    }


def route_entrypoints():
    """The Traefik entryPoint every IngressRoute this process creates binds
    to -- centralized so switching TRAEFIK_SCHEME (see /config's "HTTPS"
    section) only ever means changing this one function's answer, not
    hunting down every "entryPoints": ["web"] literal. "websecure" (443) is
    already defined by k3s's bundled Traefik chart by default -- no
    deploy/k8s-manifests change was needed to add it."""
    return ["websecure"] if TRAEFIK_SCHEME == "https" else ["web"]


def route_tls_block():
    """The IngressRoute spec fragment (merged in via **route_tls_block())
    that enables TLS termination using the cluster's single `default`
    TLSStore (see sync_tls_secret()) -- empty for plain HTTP."""
    return {"tls": {}} if TRAEFIK_SCHEME == "https" else {}


def sync_traefik_web_exposure():
    """Toggle whether Traefik's "web" (80) and "websecure" (443)
    entryPoints are externally reachable at all, following TRAEFIK_SCHEME
    and CONTAINER_PASSTHROUGH_ENABLED. k3s's bundled Traefik is exposed via
    a LoadBalancer-type Service (klipper-lb), which opens its ports
    through iptables DNAT rules in the nat/FORWARD chains -- a path a host
    firewall like ufw, which by default only governs the INPUT chain
    (traffic destined for the host's own processes), never sees. The only
    place either port can be reliably closed is here, at the Service/
    Traefik level itself, not with a host firewall rule.

    - `web` (80): exposed when TRAEFIK_SCHEME == "http" (the only
      entryPoint any app route ever binds to in that mode), closed when
      "https" (every route already moved to "websecure"/443 via
      route_entrypoints(), so a still-open port 80 would mean an
      unauthenticated plaintext path to every app stays reachable
      regardless of what an admin's own host firewall says).
    - `websecure` (443): exposed unconditionally when
      CONTAINER_PASSTHROUGH_ENABLED is True (the historical/default
      behavior -- a "container" app's passthrough IngressRouteTCP always
      binds "websecure" independent of TRAEFIK_SCHEME, see
      apply_container_route_tcp()'s docstring, so this port has to stay
      open even in "http" mode for that to keep working). When
      CONTAINER_PASSTHROUGH_ENABLED is False (no app on this node is
      allowed to use passthrough -- enforced in create_container()/
      internal_apply_container()), 443 instead follows TRAEFIK_SCHEME
      exactly like 80 does, just inverted: closed in "http" mode, open in
      "https" mode -- so an admin who never uses passthrough and wants
      "http mode" to mean *zero* externally reachable HTTPS-only ports
      gets exactly that.

    Patches the live `traefik` HelmChartConfig's `ports.{web,websecure}.
    expose.default` in place -- fetches the current object, mutates just
    those two keys inside its `valuesContent` (itself a YAML string,
    parsed/re-serialized rather than treated as opaque text), and
    re-applies the whole object unchanged otherwise. Deliberately edits
    the live object instead of reapplying deploy/k8s-manifests/
    traefik-helmchartconfig.yaml.tmpl's own content fresh, which could
    stomp install-time customization this process never sees
    (SPINDECK_DATA_DIR's log path, SPINDECK_DUAL_STACK's ipFamilyPolicy,
    ...). `failurePolicy: reinstall` on the object means k3s's own
    helm-controller picks up the change and reinstalls the Traefik release
    on its own; this function only needs to write the new desired value;
    called from config_page()'s save_traefik_scheme and
    save_container_passthrough actions, alongside resync_all_routes(), so
    either toggle takes effect immediately. Returns an error string, or
    None on success.
    """
    result = kubectl(["get", "helmchartconfig", "traefik", "-n", "kube-system", "-o", "json"])
    if result.returncode != 0:
        return result.stderr
    try:
        obj = json.loads(result.stdout)
        values = yaml.safe_load(obj["spec"]["valuesContent"]) or {}
    except (json.JSONDecodeError, yaml.YAMLError, KeyError) as exc:
        return f"Traefikの設定(HelmChartConfig)の読み込みに失敗しました: {exc}"

    ports = values.setdefault("ports", {})
    ports.setdefault("web", {})["expose"] = {"default": TRAEFIK_SCHEME != "https"}
    websecure_default = True if CONTAINER_PASSTHROUGH_ENABLED else TRAEFIK_SCHEME == "https"
    ports.setdefault("websecure", {})["expose"] = {"default": websecure_default}
    obj["spec"]["valuesContent"] = yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
    obj = strip_k8s_managed_fields(obj)
    apply_result = kubectl(["apply", "-f", "-"], input_text=json.dumps(obj))
    return apply_result.stderr if apply_result.returncode != 0 else None


def get_existing_auth_hash(name):
    """Return the htpasswd hash half of the existing <name>-auth Secret's
    "users" entry (format "username:hash"), or None if there isn't one.
    Used so leaving the password field blank on edit keeps the old password
    instead of requiring it to be re-typed every time."""
    result = kubectl(
        ["get", "secret", f"{name}-auth", "-n", NAMESPACE, "-o", "jsonpath={.data.users}"]
    )
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        decoded = base64.b64decode(result.stdout).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    _, _, hash_part = decoded.partition(":")
    return hash_part or None


def hash_apr1_password(password):
    """Hash `password` for storage as a console Basic-Auth account
    (APR1/htpasswd format, random salt each call -- see
    verify_apr1_password() for the matching check). Separate from
    resolve_auth_password_hash() below on purpose: that function is
    tightly coupled to per-app Kubernetes Secrets (its "blank keeps the
    existing hash" fallback reads one via get_existing_auth_hash()), which
    doesn't apply here -- console accounts live in config.json, not a
    Secret. Returns (hash, None) or (None, error_string)."""
    result = subprocess.run(
        ["openssl", "passwd", "-apr1", "-stdin"],
        input=password,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return None, result.stderr
    return result.stdout.strip(), None


def verify_apr1_password(password, stored_hash):
    """Check `password` against an existing "$apr1$<salt>$<hash>" string
    (as produced by hash_apr1_password()) by re-hashing with the SAME salt
    extracted from stored_hash and comparing in constant time
    (secrets.compare_digest() -- the same pattern _peer_authorized() uses
    for the peer token). Never raises: a malformed or missing stored_hash
    (e.g. a corrupted config.json entry) just means "wrong password", not
    a 500."""
    match = re.match(r"^\$apr1\$([^$]*)\$", stored_hash or "")
    if not match:
        return False
    result = subprocess.run(
        ["openssl", "passwd", "-apr1", "-salt", match.group(1), "-stdin"],
        input=password,
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and secrets.compare_digest(result.stdout.strip(), stored_hash)


def resolve_auth_password_hash(name, password):
    """If a new password was submitted, hash it (via openssl, APR1/htpasswd
    format -- what Traefik's basicAuth Middleware expects). If left blank,
    fall back to the existing <name>-auth Secret's hash, so editing an app
    without retyping the password keeps it unchanged. Must be called
    *before* anything deletes that existing Secret (delete_route() does,
    for site apps -- see create_site()). Returns (hash, None) or
    (None, error_string).
    """
    if password:
        # -stdin instead of passing password as an argv element: a process
        # argument list is visible to any other process on the same host
        # (e.g. `ps aux`) for as long as this short-lived openssl process
        # runs, however briefly.
        hash_result = subprocess.run(
            ["openssl", "passwd", "-apr1", "-stdin"],
            input=password,
            capture_output=True, text=True, timeout=10,
        )
        if hash_result.returncode != 0:
            return None, hash_result.stderr
        return hash_result.stdout.strip(), None
    existing_hash = get_existing_auth_hash(name)
    if existing_hash:
        return existing_hash, None
    return None, "パスワードを入力してください。"


def apply_basic_auth_secret(name, enabled, username, htpasswd_hash):
    """Create/update or remove the per-app HTTP Basic Auth Secret +
    Middleware (<name>-auth), given an already-resolved htpasswd hash (see
    resolve_auth_password_hash()). Always removes first so toggling auth
    off, or changing the username, never leaves stale state behind.
    Returns an error string, or None on success (including the "disabled"
    no-op case).
    """
    kubectl(["delete", "secret", f"{name}-auth", "-n", NAMESPACE, "--ignore-not-found"])
    kubectl(["delete", "middleware", f"{name}-auth", "-n", NAMESPACE, "--ignore-not-found"])
    if not enabled:
        return None

    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"{name}-auth",
            "namespace": NAMESPACE,
            "labels": {APP_LABEL: name},
        },
        "stringData": {"users": f"{username}:{htpasswd_hash}"},
    }
    middleware = _labeled(
        "Middleware", f"{name}-auth", NAMESPACE, name,
        {"basicAuth": {"secret": f"{name}-auth"}},
    )
    for manifest in (secret, middleware):
        result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
        if result.returncode != 0:
            return result.stderr
    return None


FORWARDAUTH_SERVICE_NAME = "spindeck-forwardauth"
_forwardauth_ready = False
_forwardauth_ready_lock = threading.Lock()


def forwardauth_address(name):
    """The forwardAuth Middleware `address` for app `name`. The app name is
    baked into the URL itself (as a query param, not derived from any
    request header at auth-check time) precisely so spindeck_forwardauth()
    never has to trust anything the inbound request claims about which app
    is being accessed -- see that function's own docstring for why that
    distinction matters."""
    port = os.environ.get("PORT", "8080")
    return (
        f"http://{FORWARDAUTH_SERVICE_NAME}.{NAMESPACE}.svc.cluster.local:"
        f"{port}/_spindeck/forwardauth?app={quote(name, safe='')}"
    )


TLS_SECRET_NAME = "spindeck-tls"
_tls_last_synced_hash = None
_tls_sync_lock = threading.Lock()


def get_tls_cert_status():
    """(configured: bool, detail: str) for /config's HTTPS section. detail
    is either an expiry date (parsed via `openssl x509 -enddate`, the same
    "shell out to openssl, list-form args" pattern resolve_auth_password_
    hash() already uses) or an error string -- never raises."""
    if not TLS_CERT_PATH.exists() or not TLS_KEY_PATH.exists():
        return False, "証明書ファイルが見つかりません。"
    result = subprocess.run(
        ["openssl", "x509", "-enddate", "-noout", "-in", str(TLS_CERT_PATH)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return False, f"証明書を読み取れませんでした: {result.stderr.strip()}"
    # Output looks like "notAfter=Jan 15 12:00:00 2027 GMT".
    enddate = result.stdout.strip().removeprefix("notAfter=")
    return True, enddate


def sync_tls_secret(force=False):
    """Idempotently mirror TLS_CERT_PATH/TLS_KEY_PATH's *current* contents
    into a kubernetes.io/tls Secret, and ensure the single cluster-wide
    `default` TLSStore (Traefik CRD constraint: only one, named exactly
    "default", is ever used -- confirmed via `kubectl explain tlsstore`)
    points at it. Traefik's own Kubernetes provider watches Secrets it
    references and hot-reloads the served certificate with no restart on
    either side -- so once this has run once, replacing the files on disk
    and waiting for the next poll (see tls_poll_loop()) is the entire
    "deploy a renewed cert" workflow.

    Skips the kubectl round-trip when the files' combined content hasn't
    changed since the last successful sync (force=True, used by /config's
    "今すぐ反映" button, bypasses that). Returns an error string, or None
    on success (including the "nothing to do" no-op case).
    """
    global _tls_last_synced_hash
    if not TLS_CERT_PATH.exists() or not TLS_KEY_PATH.exists():
        return "証明書ファイルが見つかりません。"
    with _tls_sync_lock:
        cert_text = TLS_CERT_PATH.read_text()
        key_text = TLS_KEY_PATH.read_text()
        digest = hashlib.sha256((cert_text + "\n" + key_text).encode("utf-8")).hexdigest()
        if not force and digest == _tls_last_synced_hash:
            return None

        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "kubernetes.io/tls",
            "metadata": {"name": TLS_SECRET_NAME, "namespace": NAMESPACE},
            "stringData": {"tls.crt": cert_text, "tls.key": key_text},
        }
        tls_store = {
            "apiVersion": "traefik.io/v1alpha1",
            "kind": "TLSStore",
            "metadata": {"name": "default", "namespace": NAMESPACE},
            "spec": {"defaultCertificate": {"secretName": TLS_SECRET_NAME}},
        }
        for manifest in (secret, tls_store):
            result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
            if result.returncode != 0:
                return result.stderr
        _tls_last_synced_hash = digest
        return None


def tls_poll_loop():
    """Runs forever in a daemon thread (started once at process startup,
    see the bottom of this file) so replacing the certificate files on
    disk gets picked up without any admin action -- polling rather than
    e.g. inotify since a cert renewal happening a few tens of seconds
    late is irrelevant and polling needs no extra dependency."""
    while True:
        try:
            sync_tls_secret()
        except Exception as exc:  # noqa: BLE001 - must never kill this thread
            print(f"[tls-sync] error: {exc}")
        time.sleep(30)


def ensure_forwardauth_reachability(force=False):
    """Idempotently create the plain Service+Endpoints that let Traefik Pods
    reach this same process's /_spindeck/* routes (see the "Per-app OIDC
    auth" block above /login), plus the IngressRoute that exposes them.
    Reuses apply_ws_bypass_route()'s technique for reaching a plain IP:port
    from inside the cluster -- a selectorless Service with its Endpoints
    set explicitly -- just pointed at this host (FORWARDAUTH_HOST_IP)
    instead of an external backend. Runs once per process (cached in
    _forwardauth_ready); called lazily from apply_oidc_auth_middleware()
    rather than at startup, so installs that never use OIDC auth never pay
    for it. The /_spindeck IngressRoute carries no auth Middleware of its
    own -- login/callback/forwardauth must all be reachable by a visitor
    with no session yet, so chaining forwardAuth onto it would deadlock.

    When CONSOLE_VIA_TRAEFIK is set, the same IngressRoute also gets a
    second route to the same Service/backend -- i.e. the console itself,
    not just /_spindeck -- at the lowest possible priority (matching
    apply_site_route()'s catch-all convention) so it can never shadow any
    app's more specific Host+PathPrefix route. This is what lets the
    console be reached over Traefik's entryPoint (and therefore over TLS,
    once TRAEFIK_SCHEME="https") instead of only directly at :8080 -- see
    /config's "HTTPS" section.

    That second route's Host() match comes from OIDC_REDIRECT_URI's own
    hostname, not EXTERNAL_HOST -- the console's login can only ever
    complete on whatever host OIDC_REDIRECT_URI points at (that's what
    login() hands to authorize_redirect()), so matching any other host
    would just be wrong whenever an admin wants the console on a
    different FQDN than the one apps default to. Reusing it also means
    this stays correct automatically whenever OIDC_REDIRECT_URI is
    changed via /config's existing Keycloak form, with no separate
    "console FQDN" setting to keep in sync.

    force=True re-applies all objects even if _forwardauth_ready is
    already set -- used by resync_all_routes() after a TRAEFIK_SCHEME
    change, and by config_page() after CONSOLE_VIA_TRAEFIK or EXTERNAL_HOST
    changes, since those need picking up too and this function is
    otherwise a one-shot.
    """
    global _forwardauth_ready
    if _forwardauth_ready and not force:
        return None
    with _forwardauth_ready_lock:
        if _forwardauth_ready and not force:
            return None
        if not FORWARDAUTH_HOST_IP:
            return (
                "OIDC認証を使うには SPINAPP_UI_HOST_IP 環境変数（このk3sノード自身の"
                "IPアドレス）の設定が必要です。/etc/spinapp-ui/env に追記してから"
                "spinapp-ui サービスを再起動してください。"
            )
        port = int(os.environ.get("PORT", "8080"))
        routes = [{
            "kind": "Rule",
            "match": "PathPrefix(`/_spindeck`)",
            # Explicit high priority: Traefik's *default* priority for a
            # rule with none set is roughly its own string length, so a
            # "container" app's Host(`<fqdn>`)-only route (see
            # apply_container_route()'s docstring -- no PathPrefix, matches
            # every path on that host) can easily out-rank this route once
            # the fqdn is long enough (any fqdn 17+ characters already
            # produces a longer rule string than "PathPrefix(`/_spindeck`)"
            # itself). Without this, /_spindeck/oidc/login|callback for
            # such an app would get misrouted to the app's own backend
            # instead of reaching this process, breaking OIDC entirely for
            # it. A large fixed number comfortably beats any realistic
            # rule-length default.
            "priority": 1000000,
            "services": [{"name": FORWARDAUTH_SERVICE_NAME, "port": port}],
        }]
        if CONSOLE_VIA_TRAEFIK:
            console_host = urlsplit(OIDC_REDIRECT_URI or "").hostname
            if not console_host:
                return (
                    "管理画面をTraefik経由で公開するには、Keycloak (OIDC) の"
                    "Redirect URIが正しく設定されている必要があります。"
                    "/config の「Keycloak (OIDC)」を確認してください。"
                )
            routes.append({
                "kind": "Rule",
                "match": f"Host(`{console_host}`)",
                "priority": 1,
                "services": [{"name": FORWARDAUTH_SERVICE_NAME, "port": port}],
            })
        manifests = [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": FORWARDAUTH_SERVICE_NAME, "namespace": NAMESPACE},
                "spec": {"ports": [{"port": port, "targetPort": port, "protocol": "TCP"}]},
            },
            # A Service with no selector needs its Endpoints set explicitly
            # -- same technique apply_ws_bypass_route() uses to point at a
            # plain IP:port outside the cluster (here, the k3s node itself).
            {
                "apiVersion": "v1",
                "kind": "Endpoints",
                "metadata": {"name": FORWARDAUTH_SERVICE_NAME, "namespace": NAMESPACE},
                "subsets": [{
                    "addresses": [{"ip": FORWARDAUTH_HOST_IP}],
                    "ports": [{"port": port, "protocol": "TCP"}],
                }],
            },
            {
                "apiVersion": "traefik.io/v1alpha1",
                "kind": "IngressRoute",
                "metadata": {"name": "spindeck-internal", "namespace": NAMESPACE},
                "spec": {
                    "entryPoints": route_entrypoints(),
                    "routes": routes,
                    **route_tls_block(),
                },
            },
        ]
        for manifest in manifests:
            result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
            if result.returncode != 0:
                return result.stderr
        _forwardauth_ready = True
        return None


def apply_oidc_auth_middleware(name, enabled):
    """Create/update or remove the per-app OIDC forwardAuth Middleware
    (<name>-oidc-auth). Mirrors apply_basic_auth_secret()'s "always delete
    first" pattern, so toggling auth off (or switching basic<->oidc) never
    leaves stale state behind. The Middleware always points at this same
    process's /_spindeck/forwardauth (forwardauth_address()), which decides
    per-request whether the logged-in apps_user is allow-listed for `name`
    (see get_app_auth_config()). Returns an error string, or None on
    success (including the "disabled" no-op case).
    """
    kubectl(["delete", "middleware", f"{name}-oidc-auth", "-n", NAMESPACE, "--ignore-not-found"])
    if not enabled:
        return None
    reach_error = ensure_forwardauth_reachability()
    if reach_error:
        return reach_error
    middleware = _labeled(
        "Middleware", f"{name}-oidc-auth", NAMESPACE, name,
        {
            "forwardAuth": {
                "address": forwardauth_address(name),
                # Still needed -- X-Forwarded-Host/-Proto/-Uri feed
                # request_origin()/the post-login redirect target, and
                # those aren't security-sensitive the way "which app's
                # allow-list applies" is (see spindeck_forwardauth()):
                # forwardauth_address()'s own ?app= query param, not any
                # header, is what that decision is based on now.
                "trustForwardHeader": True,
                # Without this, Traefik rewrites our Location header's
                # domain to the auth server's own address (forwardauth_
                # address(), a cluster-internal *.svc.cluster.local
                # hostname) before relaying it to the browser -- so the
                # login redirect ends up pointing at an address the
                # browser can't resolve at all instead of back at the app
                # the visitor was actually on. Confirmed live: without
                # this flag, https://apps.example.com/myapp's login
                # redirect landed on
                # http://spindeck-forwardauth.default.svc.cluster.local:8080/....
                "preserveLocationHeader": True,
            }
        },
    )
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(middleware))
    return result.stderr if result.returncode != 0 else None


def apply_upgrade_insecure_middleware(name, enabled):
    """Create/update or remove a per-app <name>-upgrade-insecure Traefik
    Middleware adding `Content-Security-Policy: upgrade-insecure-requests`
    to every response. Workaround for an app whose own generated HTML
    hardcodes absolute http:// URLs for its own assets (common in an app
    moved from HTTP to HTTPS after the fact without updating its own
    saved base-URL setting -- e.g. WordPress's siteurl/home options) --
    browsers block those as mixed active content once the page itself
    loads over https, even though Traefik itself already terminates TLS
    correctly. This header tells the *browser* to silently rewrite any
    such http:// subresource request to https:// before ever sending it,
    without needing to touch the app's own code or database. Currently
    "container"-kind only (see its 🔒 tab's "混在コンテンツを自動修正する"
    checkbox) -- only meaningful in "terminate" TLS mode, since a
    passthrough connection's response headers are never seen by Traefik
    to modify. Always removes first, same "toggling off never leaves
    stale state" reasoning as apply_basic_auth_secret(). Returns an
    error string, or None on success (including the "disabled" no-op
    case)."""
    kubectl(["delete", "middleware", f"{name}-upgrade-insecure", "-n", NAMESPACE, "--ignore-not-found"])
    if not enabled:
        return None
    middleware = _labeled(
        "Middleware", f"{name}-upgrade-insecure", NAMESPACE, name,
        {"headers": {"customResponseHeaders": {"Content-Security-Policy": "upgrade-insecure-requests"}}},
    )
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(middleware))
    return result.stderr if result.returncode != 0 else None


def _auth_middlewares(name, auth_mode):
    """The (at most one) auth Middleware reference to prepend to an app's
    route -- basic and oidc are mutually exclusive per app, chosen via the
    _auth_tab.html radio buttons."""
    if auth_mode == "basic":
        return [{"name": f"{name}-auth"}]
    if auth_mode == "oidc":
        return [{"name": f"{name}-oidc-auth"}]
    return []


def apply_route(name, auth_mode="none", force_no_cache=False, fqdn=None):
    """Expose a SpinApp's Service at https://EXTERNAL_HOST/<name>/ by
    creating a per-app Traefik Middleware (strips the /<name> prefix so the
    app sees requests as if they were at "/") and an IngressRoute. If
    auth_mode is "basic" or "oidc", the matching <name>-auth /
    <name>-oidc-auth Middleware (see apply_basic_auth_secret() /
    apply_oidc_auth_middleware(), either of which must be called first) is
    chained in front of the strip middleware on every route. If
    force_no_cache (used for "site" apps -- see apply_site_route()'s
    docstring for why), a <name>-nocache Middleware overriding
    Cache-Control is chained in too.

    By default the route matches on PathPrefix alone (deliberately NOT
    Host), so it also answers when hit directly by node IP -- useful for
    local testing before an external reverse-proxy/DNS is wired up. If
    fqdn is given (one of REGISTERED_FQDNS, chosen per-app from /config),
    a Host(...) match is ANDed in, so the app only responds on that
    specific hostname.

    Always strips "/<name>" before the request reaches the app's own Spin
    component/JS worker -- for "proxy" apps whose *backend* needs to see a
    (possibly different) prefix of its own, that's handled entirely inside
    the generated JS (see render_proxy_js()'s BACKEND_PREFIX), specifically
    so a proxy app's `rules` config keeps matching against the same
    prefix-free path it always has, regardless of what the backend itself
    expects to see.
    """
    match = f"PathPrefix(`/{name}`)"
    if fqdn:
        match = f"Host(`{fqdn}`) && {match}"

    manifests = []
    middlewares = _auth_middlewares(name, auth_mode)
    if force_no_cache:
        manifests.append(_labeled(
            "Middleware", f"{name}-nocache", NAMESPACE, name,
            {"headers": {"customResponseHeaders": {"Cache-Control": "no-cache, must-revalidate"}}},
        ))
        middlewares.append({"name": f"{name}-nocache"})
    middlewares.append({"name": f"{name}-strip"})
    manifests.append(_labeled(
        "Middleware", f"{name}-strip", NAMESPACE, name,
        {"stripPrefix": {"prefixes": [f"/{name}"]}},
    ))

    manifests.append(_labeled(
        "IngressRoute", name, NAMESPACE, name,
        {
            "entryPoints": route_entrypoints(),
            "routes": [
                {
                    "kind": "Rule",
                    "match": match,
                    "services": [{"name": name, "port": 80}],
                    "middlewares": middlewares,
                }
            ],
            **route_tls_block(),
        },
    ))
    for manifest in manifests:
        result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
        if result.returncode != 0:
            return result.stderr
    return None


def apply_ws_bypass_route(name, backend_origin, ws_path, auth_mode="none", fqdn=None, backend_prefix=""):
    """For "proxy" apps whose backend serves a WebSocket at ws_path (e.g. a
    photo gallery's live-update socket), add a second, higher-priority
    Traefik route that forwards just that one path straight to
    backend_origin -- bypassing the app's own Spin/Wasm component. Spin's
    JS HTTP component only implements request/response fetch(); there is
    no way for it to hijack a connection into a raw WebSocket upgrade/
    duplex stream, so any path that needs one has to go around it at the
    Traefik layer instead. Removes the bypass (if previously created) when
    ws_path is falsy. Returns an error string, or None on success.

    Unlike the main route (see apply_route()), this bypass never goes
    through the app's own JS worker at all -- it's a raw Traefik-to-backend
    forward -- so if backend_prefix is set, it has to be applied here at
    the Traefik layer instead: strip "/<name>" same as always, then an
    addPrefix Middleware puts backend_prefix back on before the request
    ever reaches backend_origin.
    """
    ws_ingressroute_name = f"{name}-ws"
    backend_svc_name = f"{name}-ws-backend"
    ws_path = (ws_path or "").strip()
    if not ws_path:
        kubectl(["delete", "ingressroute", ws_ingressroute_name, "-n", NAMESPACE, "--ignore-not-found"])
        kubectl(["delete", "service,endpoints", backend_svc_name, "-n", NAMESPACE, "--ignore-not-found"])
        return None

    parts = urlsplit(backend_origin)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        ip = socket.gethostbyname(host)
    except OSError as exc:
        return f"WebSocketバックエンドのホスト名を解決できませんでした ({host}): {exc}"

    match = f"PathPrefix(`/{name}{ws_path}`)"
    if fqdn:
        match = f"Host(`{fqdn}`) && {match}"
    middlewares = _auth_middlewares(name, auth_mode)
    middlewares.append({"name": f"{name}-strip"})

    manifests = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": backend_svc_name, "namespace": NAMESPACE, "labels": {APP_LABEL: name}},
            "spec": {"ports": [{"port": port, "targetPort": port, "protocol": "TCP"}]},
        },
        # A Service with no selector needs its Endpoints set explicitly --
        # this is how we point it at a plain IP:port outside the cluster
        # rather than at pod labels.
        {
            "apiVersion": "v1",
            "kind": "Endpoints",
            "metadata": {"name": backend_svc_name, "namespace": NAMESPACE, "labels": {APP_LABEL: name}},
            "subsets": [{"addresses": [{"ip": ip}], "ports": [{"port": port, "protocol": "TCP"}]}],
        },
    ]
    if backend_prefix:
        middlewares.append({"name": f"{name}-ws-backend-prefix"})
        manifests.append(_labeled(
            "Middleware", f"{name}-ws-backend-prefix", NAMESPACE, name,
            {"addPrefix": {"prefix": backend_prefix}},
        ))
    manifests.append(
        _labeled(
            "IngressRoute", ws_ingressroute_name, NAMESPACE, name,
            {
                "entryPoints": route_entrypoints(),
                "routes": [
                    {
                        "kind": "Rule",
                        "match": match,
                        # Must outrank the app's own PathPrefix(`/<name>`)
                        # route so this more specific path wins.
                        "priority": 100,
                        "services": [{"name": backend_svc_name, "port": port}],
                        "middlewares": middlewares,
                    }
                ],
                **route_tls_block(),
            },
        )
    )
    for manifest in manifests:
        result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
        if result.returncode != 0:
            return result.stderr
    return None


def apply_site_route(name, file_paths, auth_mode="none", fqdn=None):
    """Like apply_route(), but for "site" apps that have at least one .php
    file (see build_site_spin_toml()). Non-PHP files already get correct
    Content-Type natively from spin.toml's static-fileserver component and
    its per-file literal routes, so the only thing left to fix up here at
    the Traefik layer is directory-index resolution: WAGI/php-cgi never
    resolves a bare "/" or "/sub/" request to that directory's index.php
    (confirmed empirically -- see build_site_spin_toml's docstring), so
    rewrite those to append "index.php" before the usual stripPrefix. If
    auth_mode is "basic" or "oidc", the matching auth Middleware (see
    apply_basic_auth_secret() / apply_oidc_auth_middleware(), either of
    which must be called first) is chained in front of every route.
    """
    auth_mw = _auth_middlewares(name, auth_mode)
    # static-fileserver hardcodes Cache-Control: max-age=60 on every file it
    # serves (which is every non-.php file here). An intermediate cache in
    # front of this cluster (e.g. Apache Traffic Server) can hold onto that
    # far longer than 60s (own TTL overrides, negative caching, etc.),
    # making a just-deleted/just-edited file appear to "still exist" for a
    # long time. Overriding to no-cache forces revalidation (ETag is still
    # sent, so a 304 is possible) on every request instead.
    nocache_mw = [{"name": f"{name}-nocache"}]

    manifests = [
        _labeled(
            "Middleware", f"{name}-strip", NAMESPACE, name,
            {"stripPrefix": {"prefixes": [f"/{name}"]}},
        ),
        _labeled(
            "Middleware", f"{name}-nocache", NAMESPACE, name,
            {"headers": {"customResponseHeaders": {"Cache-Control": "no-cache, must-revalidate"}}},
        ),
    ]
    routes = []

    # Highest priority: directory-index resolution. For every directory that
    # has an index.php (including the root), rewrite bare "/name" or
    # "/name/dir/" to append "index.php" before stripping the prefix.
    index_dirs = sorted(
        {os.path.dirname(p) for p in file_paths if os.path.basename(p) == "index.php"}
    )
    for i, dir_part in enumerate(index_dirs):
        prefix = f"/{name}/{dir_part}" if dir_part else f"/{name}"
        mw_name = f"{name}-idx-{i}"
        manifests.append(_labeled(
            "Middleware", mw_name, NAMESPACE, name,
            {
                "replacePathRegex": {
                    "regex": f"^{prefix}/?$",
                    "replacement": f"{prefix}/index.php",
                }
            },
        ))
        route_match = f"Path(`{prefix}`) || Path(`{prefix}/`)"
        if fqdn:
            route_match = f"Host(`{fqdn}`) && ({route_match})"
        routes.append({
            "kind": "Rule",
            "match": route_match,
            "priority": 200,
            "services": [{"name": name, "port": 80}],
            "middlewares": auth_mw + nocache_mw + [{"name": mw_name}, {"name": f"{name}-strip"}],
        })

    # Lowest priority: catch-all, same as apply_route() -- handles .php
    # files directly and anything not covered above.
    catchall_match = f"PathPrefix(`/{name}`)"
    if fqdn:
        catchall_match = f"Host(`{fqdn}`) && {catchall_match}"
    routes.append({
        "kind": "Rule",
        "match": catchall_match,
        "priority": 1,
        "services": [{"name": name, "port": 80}],
        "middlewares": auth_mw + nocache_mw + [{"name": f"{name}-strip"}],
    })

    manifests.append(_labeled(
        "IngressRoute", name, NAMESPACE, name,
        {"entryPoints": route_entrypoints(), "routes": routes, **route_tls_block()},
    ))

    for manifest in manifests:
        result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
        if result.returncode != 0:
            return result.stderr
    return None


def delete_route(name):
    # service,endpoints here are only ever the ones apply_ws_bypass_route()
    # creates (labeled APP_LABEL) -- the SpinApp's own Service is owned/
    # labeled by the Spin operator instead, so this can't touch it.
    kubectl(
        [
            "delete",
            "ingressroute,middleware,secret,service,endpoints",
            "-n",
            NAMESPACE,
            "-l",
            f"{APP_LABEL}={name}",
            "--ignore-not-found",
        ]
    )


def get_js_source(name):
    """Return the stored JS source for a SpinApp, or None if it has none
    (e.g. it was registered from a container image, not pasted JS)."""
    result = kubectl(
        [
            "get",
            "spinapp",
            name,
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={{.metadata.annotations['{}']}}".format(
                JS_SOURCE_ANNOTATION.replace(".", r"\.")
            ),
        ]
    )
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return base64.b64decode(result.stdout).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def get_proxy_config(name):
    """Return the stored structured config ({backend_origin, rules,
    rewrites}) for a "proxy"-kind SpinApp, or None if it has none."""
    result = kubectl(
        [
            "get",
            "spinapp",
            name,
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={{.metadata.annotations['{}']}}".format(
                PROXY_CONFIG_ANNOTATION.replace(".", r"\.")
            ),
        ]
    )
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return json.loads(base64.b64decode(result.stdout).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_traefik_logs(range_key):
    """Read Traefik's persistent JSON access log file and keep only the
    lines routed through one of our per-app IngressRoutes, within the
    requested time range.

    Reads TRAEFIK_ACCESS_LOG_PATH directly off disk rather than `kubectl
    logs`, since the latter only has whatever log history the current
    Traefik pod happens to still hold -- it's empty again after any pod
    restart/reschedule. Traefik is configured (see
    deploy/k8s-manifests/traefik-helmchartconfig.yaml) to write access logs
    to a hostPath-backed file instead, so history survives restarts; this
    only works because the console and Traefik run on the same single node.
    """
    hours = RANGE_HOURS.get(range_key, 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    try:
        size = TRAEFIK_ACCESS_LOG_PATH.stat().st_size
        with TRAEFIK_ACCESS_LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
            if size > MAX_ACCESS_LOG_READ_BYTES:
                # Cap how much of a continuously-growing file we scan per
                # request; a discarded partial first line is fine, we only
                # care about the rest.
                f.seek(size - MAX_ACCESS_LOG_READ_BYTES)
                f.readline()
            lines = f.readlines()
    except OSError as exc:
        return [], str(exc)

    entries = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        ts = obj.get("time")
        if ts:
            try:
                if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
                    continue
            except ValueError:
                pass
        match = ROUTER_NAME_RE.match(obj.get("RouterName", ""))
        if not match:
            continue
        app_name = match.group(1)
        # apply_ws_bypass_route()'s "<name>-ws" IngressRoute is a second,
        # separate router for the same app (exists purely so Traefik can
        # skip the app's auth/etc. middlewares for the WebSocket upgrade
        # request itself) -- fold its traffic back under the app's own
        # name rather than showing it as a distinct pseudo-app.
        if app_name.endswith("-ws"):
            app_name = app_name[: -len("-ws")]
        if app_name in ANALYTICS_EXCLUDED_APPS:
            continue
        obj["_app"] = app_name
        entries.append(obj)
    return entries, None


def get_traefik_log_diagnostics():
    """Surface exactly what path parse_traefik_logs() is reading from,
    and whether that file looks alive -- lets a drift between Traefik's
    own HelmChartConfig (the write side, --accesslog.filepath) and
    SPINAPP_UI_TRAEFIK_ACCESS_LOG (the read side) show up on the
    Analytics page itself instead of requiring a live kubectl dive to
    notice. Found exactly this drift on a real node (mg3, 2026-08):
    env pointed at /data/traefik-logs/access.log while Traefik itself
    still wrote to /var/log/traefik/access.log -- Analytics kept
    "working" (no error, just an increasingly stale file) since the file
    existed and was readable, it just never grew.
    """
    diag = {
        "path": str(TRAEFIK_ACCESS_LOG_PATH),
        "exists": False,
        "size_bytes": None,
        "mtime": None,
        "age_seconds": None,
        "stale": False,
    }
    try:
        st = TRAEFIK_ACCESS_LOG_PATH.stat()
    except OSError:
        return diag
    diag["exists"] = True
    diag["size_bytes"] = st.st_size
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    diag["mtime"] = mtime.astimezone(DISPLAY_TZ)
    diag["age_seconds"] = (datetime.now(timezone.utc) - mtime).total_seconds()
    diag["stale"] = diag["age_seconds"] > TRAEFIK_LOG_STALE_SECONDS
    return diag


def status_bucket(code):
    if code >= 500:
        return "critical"
    if code >= 400:
        return "warning"
    return "good"


def build_analytics(range_key, app_filter=None):
    hours = RANGE_HOURS.get(range_key, 24)
    entries, err = parse_traefik_logs(range_key)

    # Every app with *any* traffic in this range, regardless of
    # app_filter -- always the full set, so the dropdown that lets you
    # pick app_filter in the first place doesn't shrink to just whatever
    # is currently selected.
    available_apps = sorted({e["_app"] for e in entries})
    if app_filter:
        entries = [e for e in entries if e["_app"] == app_filter]

    per_app = Counter(e["_app"] for e in entries)
    status_by_app = defaultdict(lambda: {"good": 0, "warning": 0, "critical": 0})
    total_status = {"good": 0, "warning": 0, "critical": 0}
    durations = []
    hourly = Counter()
    top_paths = Counter()
    recent = []

    for e in entries:
        status_code = e.get("DownstreamStatus", 0)
        bucket = status_bucket(status_code)
        status_by_app[e["_app"]][bucket] += 1
        total_status[bucket] += 1
        dur_ns = e.get("Duration")
        if dur_ns:
            durations.append(dur_ns)
        ts = e.get("time") or e.get("StartUTC")
        dt = None
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(DISPLAY_TZ)
                hourly[dt.strftime("%Y-%m-%dT%H:00")] += 1
            except ValueError:
                pass

        method = e.get("RequestMethod", "-")
        path = e.get("RequestPath", "-")
        top_paths[(e["_app"], method, path)] += 1
        recent.append(
            {
                "time": dt,
                "time_label": dt.strftime("%m/%d %H:%M:%S") if dt else "-",
                "app": e["_app"],
                "method": method,
                "path": path,
                "status": status_code,
                "status_bucket": bucket,
                "duration_ms": (dur_ns / 1e6) if dur_ns else 0.0,
            }
        )

    recent.sort(key=lambda r: r["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    recent = recent[:150]

    now = datetime.now(DISPLAY_TZ).replace(minute=0, second=0, microsecond=0)
    buckets = []
    for i in range(hours - 1, -1, -1):
        hour_dt = now - timedelta(hours=i)
        key = hour_dt.strftime("%Y-%m-%dT%H:00")
        label = hour_dt.strftime("%m/%d %H:00") if hours > 24 else hour_dt.strftime("%H:00")
        buckets.append({"label": label, "count": hourly.get(key, 0)})

    total = len(entries)
    error_rate = (
        (total_status["warning"] + total_status["critical"]) / total * 100
        if total
        else 0.0
    )
    avg_duration_ms = (sum(durations) / len(durations) / 1e6) if durations else 0.0

    top_paths_list = [
        {"app": app_name, "method": method, "path": path, "count": count}
        for (app_name, method, path), count in top_paths.most_common(15)
    ]
    return {
        "error": err,
        "total": total,
        "active_apps": len(per_app),
        "error_rate": error_rate,
        "avg_duration_ms": avg_duration_ms,
        "buckets": buckets,
        "top_apps": per_app.most_common(10),
        "status_by_app": dict(status_by_app),
        "top_paths": top_paths_list,
        "recent": recent,
        "available_apps": available_apps,
    }


def esc(value):
    return html.escape(str(value))


def render_line_chart(buckets, height=170):
    width = 880
    pad_l, pad_r, pad_t, pad_b = 8, 8, 16, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    values = [b["count"] for b in buckets]
    vmax = max(values) if values and max(values) > 0 else 1
    n = len(buckets)

    def x_at(i):
        return pad_l + (i * plot_w / max(n - 1, 1))

    def y_at(v):
        return pad_t + plot_h - (v / vmax * plot_h)

    points = [(x_at(i), y_at(v)) for i, v in enumerate(values)]
    line_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area_d = (
        line_d
        + f" L {points[-1][0]:.1f},{pad_t + plot_h:.1f}"
        + f" L {points[0][0]:.1f},{pad_t + plot_h:.1f} Z"
    )

    grid = []
    for frac in (0, 0.5, 1.0):
        gy = pad_t + plot_h * (1 - frac)
        grid.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
            f'stroke="var(--gridline)" stroke-width="1"/>'
        )
        grid.append(
            f'<text x="{pad_l}" y="{gy - 4:.1f}" font-size="10" fill="var(--text-muted)">'
            f'{int(round(vmax * frac))}</text>'
        )

    x_labels = []
    label_every = max(1, n // 6)
    for i, b in enumerate(buckets):
        if i % label_every == 0 or i == n - 1:
            x_labels.append(
                f'<text x="{x_at(i):.1f}" y="{height - 6}" font-size="10" '
                f'fill="var(--text-muted)" text-anchor="middle">{esc(b["label"])}</text>'
            )

    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="transparent">'
        f'<title>{esc(buckets[i]["label"])}: {values[i]} req</title></circle>'
        for i, (x, y) in enumerate(points)
    )

    last_x, last_y = points[-1]
    last_label = (
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="var(--series-1)" '
        f'stroke="var(--surface-1)" stroke-width="2"/>'
        f'<text x="{last_x:.1f}" y="{last_y - 10:.1f}" font-size="11" font-weight="600" '
        f'fill="var(--text-primary)" text-anchor="end">{values[-1]}</text>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Requests over time">'
        f'{"".join(grid)}'
        f'<path d="{area_d}" fill="var(--series-1)" fill-opacity="0.10" stroke="none"/>'
        f'<path d="{line_d}" fill="none" stroke="var(--series-1)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'{dots}{last_label}{"".join(x_labels)}'
        f"</svg>"
    )


def render_bar_chart(items, height_per_row=32):
    if not items:
        return None
    width = 880
    pad_l, pad_r, pad_t = 130, 60, 8
    n = len(items)
    height = pad_t + n * height_per_row + 8
    plot_w = width - pad_l - pad_r
    vmax = max((c for _, c in items), default=1) or 1

    rows = []
    for i, (label, count) in enumerate(items):
        y = pad_t + i * height_per_row
        bar_h = 20
        bar_w = max(2.0, count / vmax * plot_w)
        rows.append(
            f'<text x="{pad_l - 10}" y="{y + bar_h / 2 + 4:.1f}" font-size="12" '
            f'fill="var(--text-secondary)" text-anchor="end" class="mono">{esc(label)}</text>'
            f'<rect x="{pad_l}" y="{y}" width="{bar_w:.1f}" height="{bar_h}" rx="4" '
            f'fill="var(--series-1)"><title>{esc(label)}: {count} req</title></rect>'
            f'<text x="{pad_l + bar_w + 8:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="12" '
            f'font-weight="600" fill="var(--text-primary)">{count}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Requests per app">{"".join(rows)}</svg>'
    )


STATUS_COLORS = {
    "good": "var(--status-good)",
    "warning": "var(--status-warning)",
    "critical": "var(--status-critical)",
}
STATUS_LABELS = {"good": "2xx/3xx", "warning": "4xx", "critical": "5xx"}


def render_status_chart(status_by_app, height_per_row=28):
    if not status_by_app:
        return None
    width = 880
    pad_l, pad_r, pad_t = 130, 16, 8
    gap = 2
    apps = sorted(
        status_by_app.items(), key=lambda kv: -sum(kv[1].values())
    )[:10]
    n = len(apps)
    height = pad_t + n * height_per_row + 34
    plot_w = width - pad_l - pad_r
    vmax = max((sum(v.values()) for _, v in apps), default=1) or 1

    rows = []
    for i, (app_name, counts) in enumerate(apps):
        y = pad_t + i * height_per_row
        bar_h = 18
        rows.append(
            f'<text x="{pad_l - 10}" y="{y + bar_h / 2 + 4:.1f}" font-size="12" '
            f'fill="var(--text-secondary)" text-anchor="end" class="mono">{esc(app_name)}</text>'
        )
        segs = [(k, counts.get(k, 0)) for k in ("good", "warning", "critical") if counts.get(k, 0) > 0]
        x = float(pad_l)
        for j, (key, count) in enumerate(segs):
            seg_w = count / vmax * plot_w
            is_last = j == len(segs) - 1
            rect_w = seg_w if is_last else max(seg_w - gap, 1.0)
            rows.append(
                f'<rect x="{x:.1f}" y="{y}" width="{rect_w:.1f}" height="{bar_h}" '
                f'fill="{STATUS_COLORS[key]}"><title>{esc(app_name)} — {STATUS_LABELS[key]}: {count}</title></rect>'
            )
            x += seg_w

    legend_y = height - 14
    legend = []
    lx = pad_l
    for key in ("good", "warning", "critical"):
        legend.append(
            f'<circle cx="{lx + 5}" cy="{legend_y}" r="5" fill="{STATUS_COLORS[key]}"/>'
            f'<text x="{lx + 15}" y="{legend_y + 4}" font-size="11" fill="var(--text-secondary)">'
            f"{STATUS_LABELS[key]}</text>"
        )
        lx += 95

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Status breakdown per app">'
        f'{"".join(rows)}{"".join(legend)}</svg>'
    )


# Written by install.sh/upgrade.sh (both run with the admin kubeconfig,
# unlike this process's own deliberately-restricted one -- see
# spinapp-ui-rbac.yaml) each time they touch cert-manager/spin-operator/
# Traefik. Read-only here: a static file instead of a live query, so
# this dashboard feature doesn't need the console's own ServiceAccount
# granted read access to the cert-manager/spin-operator/kube-system
# namespaces just to show a version string -- see get_component_
# versions()'s docstring for the fuller reasoning.
COMPONENT_VERSIONS_PATH = Path(
    os.environ.get("SPINAPP_UI_VERSIONS_PATH", "/opt/spinapp-ui/versions.json")
)


def get_host_status():
    """Live host-level info for the dashboard's "システム状態" card --
    CPU load/memory usage, hostname, OS, uptime, k3s's own version.
    Zero Kubernetes API calls: read directly off /proc/os-release, or a
    subprocess straight to the k3s binary -- this process already runs
    as host root (see the design philosophy note on the console being a
    host systemd process, not a Pod), so none of this needs any RBAC at
    all, let alone the console's own restricted ServiceAccount. Every
    field is independently best-effort (a dict value is None on
    failure) so one missing/unreadable source doesn't blank the rest of
    the card.
    """
    status = {}

    try:
        load1, load5, load15 = os.getloadavg()
        status["load"] = f"{load1:.2f} / {load5:.2f} / {load15:.2f}"
    except OSError:
        status["load"] = None
    status["cpu_count"] = os.cpu_count()

    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts:
                    meminfo[key] = int(parts[0])  # kB
        total_kb = meminfo.get("MemTotal", 0)
        avail_kb = meminfo.get("MemAvailable", 0)
        if total_kb:
            used_kb = total_kb - avail_kb
            status["mem_used_gb"] = used_kb / 1024 / 1024
            status["mem_total_gb"] = total_kb / 1024 / 1024
            status["mem_percent"] = used_kb / total_kb * 100
        else:
            status["mem_used_gb"] = status["mem_total_gb"] = status["mem_percent"] = None
    except OSError:
        status["mem_used_gb"] = status["mem_total_gb"] = status["mem_percent"] = None

    try:
        status["hostname"] = socket.gethostname()
    except OSError:
        status["hostname"] = None

    try:
        os_release = {}
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    os_release[k] = v.strip('"')
        status["os"] = os_release.get("PRETTY_NAME")
    except OSError:
        status["os"] = None

    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
        status["uptime"] = str(timedelta(seconds=int(uptime_seconds)))
    except (OSError, ValueError, IndexError):
        status["uptime"] = None

    try:
        result = subprocess.run(
            ["k3s", "--version"], capture_output=True, text=True, timeout=5
        )
        # "k3s version v1.36.3+k3s1 (01b6f04a)" -- 3rd whitespace-separated
        # field.
        status["k3s_version"] = (
            result.stdout.splitlines()[0].split()[2] if result.returncode == 0 else None
        )
    except (OSError, subprocess.SubprocessError, IndexError):
        status["k3s_version"] = None

    return status


def get_component_versions():
    """cert-manager/SpinKube-operator/Traefik versions, as last recorded
    by install.sh/upgrade.sh into COMPONENT_VERSIONS_PATH -- see that
    constant's comment for why this is a file read rather than a live
    `kubectl`/`helm` query. Can lag behind reality if any of those three
    were ever changed some other way, but upgrade.sh is the sanctioned
    way to change them, so in practice this stays accurate. Returns {}
    (every field missing) if the file doesn't exist yet -- an install
    that predates this feature, or one where upgrade.sh has simply never
    been run -- the template handles that by just omitting those rows,
    not erroring."""
    try:
        return json.loads(COMPONENT_VERSIONS_PATH.read_text())
    except (OSError, ValueError):
        return {}


@app.route("/")
def index():
    apps, error = list_spinapps()
    crons, cron_error = list_cronjobs()
    containers, container_error = list_container_apps()
    # Just a count here -- the templates themselves have their own
    # management page (container_templates_page()) now, linked below,
    # since listing every one inline on the dashboard got unwieldy once
    # more than a couple exist.
    container_template_count = len(CONTAINER_USER_TEMPLATES)
    return render_template(
        "index.html",
        apps=apps,
        error=error,
        crons=crons,
        cron_error=cron_error,
        containers=containers,
        container_error=container_error,
        container_template_count=container_template_count,
        host_status=get_host_status(),
        component_versions=get_component_versions(),
        namespace=NAMESPACE,
        active_nav="dashboard",
    )


@app.route("/analytics")
def analytics():
    range_key = request.args.get("range", "24h")
    if range_key not in RANGE_HOURS:
        range_key = "24h"
    app_filter = request.args.get("app", "").strip()
    data = build_analytics(range_key, app_filter=app_filter or None)
    # Not one of this range's actual apps (stale bookmark/URL after the
    # app was renamed-by-recreation or deleted) -- fall back to
    # unfiltered rather than silently showing an empty dashboard with no
    # indication why.
    if app_filter and app_filter not in data["available_apps"]:
        app_filter = ""
        data = build_analytics(range_key)
    return render_template(
        "analytics.html",
        namespace=NAMESPACE,
        active_nav="analytics",
        range_key=range_key,
        range_options=RANGE_OPTIONS,
        app_filter=app_filter,
        stats=data,
        log_error=data.get("error"),
        log_diag=get_traefik_log_diagnostics(),
        line_svg=render_line_chart(data["buckets"]),
        bar_svg=render_bar_chart(data["top_apps"]),
        status_svg=render_status_chart(data["status_by_app"]),
    )


def resync_all_routes():
    """Re-apply every existing app's Traefik routing objects (IngressRoute
    + the WebSocket bypass one, where relevant) so they pick up the
    *current* route_entrypoints()/route_tls_block() -- called right after
    TRAEFIK_SCHEME changes (see config_page()'s save_traefik_scheme
    action), since switching that global doesn't retroactively touch
    IngressRoute objects already applied under the old entryPoint; Traefik
    only hot-reloads on an actual re-apply.

    Deliberately does *not* go through deploy_js_spinapp()/
    deploy_proxy_spinapp()/deploy_site_spinapp() -- those always rebuild
    and re-push a fresh image first, which is both slow and pointless here
    since nothing about any app's own content is changing, only which
    entryPoint its routes bind to. Calls apply_route()/apply_site_route()/
    apply_ws_bypass_route() directly instead, using each app's *current*
    auth-mode/fqdn (from list_spinapps(), already resolved via
    resolve_auth_mode()) and, for proxy apps, its stored backend config
    (get_proxy_config()). apply_basic_auth_secret()/
    apply_oidc_auth_middleware() are deliberately not re-run: neither
    depends on entryPoint, and re-running them would be pure overhead.

    "image"-kind apps (kind not in RESYNCABLE_KINDS below) are skipped:
    those were never created through spinapp-ui's own New App/Site/Proxy
    flows (editable=False in list_spinapps()), so this process has no
    stored config to know how -- or whether -- to route them at all.

    Returns a list of (name, error) for anything that failed; empty list
    means every resyncable app's routes now reflect the current
    TRAEFIK_SCHEME.
    """
    apps, list_error = list_spinapps()
    if list_error:
        return [("*", list_error)]

    failures = []
    for app_info in apps:
        name = app_info["name"]
        kind = app_info["kind"]
        auth_mode = app_info["auth_mode"]
        fqdn = app_info["fqdn"] or None
        if kind == "proxy":
            config = get_proxy_config(name) or {}
            route_error = apply_route(name, auth_mode=auth_mode, fqdn=fqdn)
            if not route_error:
                route_error = apply_ws_bypass_route(
                    name, config.get("backend_origin", ""), config.get("ws_path", ""),
                    auth_mode=auth_mode, fqdn=fqdn, backend_prefix=config.get("backend_prefix", ""),
                )
            if route_error:
                failures.append((name, route_error))
        elif kind == "site":
            files = get_site_files(name)
            if files is None:
                failures.append((name, "サイトのファイル一覧を取得できませんでした。"))
                continue
            has_php = any(p.endswith(".php") for p in files)
            route_error = (
                apply_site_route(name, list(files.keys()), auth_mode=auth_mode, fqdn=fqdn)
                if has_php
                else apply_route(name, auth_mode=auth_mode, force_no_cache=True, fqdn=fqdn)
            )
            if route_error:
                failures.append((name, route_error))
        elif kind == "js":
            route_error = apply_route(name, auth_mode=auth_mode, fqdn=fqdn)
            if route_error:
                failures.append((name, route_error))
        # kind == "image": intentionally skipped, see docstring.

    if _forwardauth_ready:
        reach_error = ensure_forwardauth_reachability(force=True)
        if reach_error:
            failures.append(("_spindeck", reach_error))

    return failures


@app.route("/config", methods=["GET", "POST"])
def config_page():
    global EXTERNAL_HOST, DEFAULT_FQDN, PEERS
    global SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, TRAEFIK_SCHEME
    global CONSOLE_VIA_TRAEFIK, CONSOLE_AUTH_MODE, CONTAINER_PASSTHROUGH_ENABLED
    global SESSION_IDLE_TIMEOUT_MINUTES
    errors = []
    saved = False
    generated_peer_token = None
    route_resync_errors = []
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "add_fqdn":
            new_fqdn = request.form.get("new_fqdn", "").strip()
            new_fqdn = re.sub(r"^https?://", "", new_fqdn).rstrip("/")
            if not new_fqdn or not EXTERNAL_HOST_RE.match(new_fqdn):
                errors.append("正しいFQDNを入力してください (例: app.example.com)。")
            elif new_fqdn in REGISTERED_FQDNS:
                errors.append("そのFQDNは既に登録されています。")
            else:
                REGISTERED_FQDNS.append(new_fqdn)
                # First FQDN ever registered becomes the default automatically.
                if not DEFAULT_FQDN:
                    DEFAULT_FQDN = new_fqdn
                    EXTERNAL_HOST = new_fqdn
                try:
                    save_persisted_config()
                except OSError as exc:
                    REGISTERED_FQDNS.remove(new_fqdn)
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "remove_fqdn":
            target = request.form.get("fqdn", "")
            if target in REGISTERED_FQDNS:
                REGISTERED_FQDNS.remove(target)
                if DEFAULT_FQDN == target:
                    DEFAULT_FQDN = ""
                    EXTERNAL_HOST = MANUAL_EXTERNAL_HOST
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "set_default":
            target = request.form.get("fqdn", "")
            if target and target not in REGISTERED_FQDNS:
                errors.append("そのFQDNは登録されていません。")
            else:
                DEFAULT_FQDN = target
                EXTERNAL_HOST = target or MANUAL_EXTERNAL_HOST
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "save_oidc":
            # If console_via_traefik is on, changing oidc_redirect_uri's
            # host here also moves the console's own Traefik route --
            # handled by the startup check at the bottom of this file
            # (this whole process restarts a couple seconds after
            # returning, via restart_self_soon() below), not here.
            new_issuer = request.form.get("oidc_issuer", "").strip().rstrip("/")
            new_client_id = request.form.get("oidc_client_id", "").strip()
            new_client_secret = request.form.get("oidc_client_secret", "")
            new_redirect_uri = request.form.get("oidc_redirect_uri", "").strip()

            if not new_issuer or not re.match(r"^https?://", new_issuer):
                errors.append("Issuer は http(s):// から始まる URL を入力してください。")
            if not new_client_id:
                errors.append("Client ID を入力してください。")
            if not new_redirect_uri or not re.match(r"^https?://", new_redirect_uri):
                errors.append("Redirect URI は http(s):// から始まる URL を入力してください。")

            if not errors:
                updates = {
                    "SPINAPP_UI_OIDC_ISSUER": new_issuer,
                    "SPINAPP_UI_OIDC_CLIENT_ID": new_client_id,
                    "SPINAPP_UI_OIDC_REDIRECT_URI": new_redirect_uri,
                }
                if new_client_secret:
                    updates["SPINAPP_UI_OIDC_CLIENT_SECRET"] = new_client_secret
                try:
                    update_env_file(updates)
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
                    restart_self_soon()
        elif action == "save_peer_token":
            new_token = request.form.get("peer_token", "").strip()
            if not new_token:
                errors.append("トークンを入力してください。")
            elif len(new_token) < 16:
                errors.append("トークンは16文字以上にしてください。")
            else:
                try:
                    update_env_file({"SPINAPP_UI_PEER_TOKEN": new_token})
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
                    generated_peer_token = new_token
                    restart_self_soon()
        elif action == "generate_peer_token":
            new_token = secrets.token_hex(32)
            try:
                update_env_file({"SPINAPP_UI_PEER_TOKEN": new_token})
            except OSError as exc:
                errors.append(f"設定の保存に失敗しました: {exc}")
            else:
                saved = True
                generated_peer_token = new_token
                restart_self_soon()
        elif action == "add_peer":
            new_peer = request.form.get("new_peer", "").strip().rstrip("/")
            if not new_peer or not re.match(r"^https?://", new_peer):
                errors.append(
                    "ピアのURLは http(s):// から始めて入力してください "
                    "(例: https://node2.example.com:8080)。"
                )
            elif new_peer in PEERS:
                errors.append("そのピアは既に登録されています。")
            elif not PEER_TOKEN:
                errors.append(
                    "SPINAPP_UI_PEER_TOKEN が未設定です。先に全ノードへ同じ値を"
                    "設定してください（/etc/spinapp-ui/env）。"
                )
            else:
                PEERS.append(new_peer)
                try:
                    save_persisted_config()
                except OSError as exc:
                    PEERS.remove(new_peer)
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "remove_peer":
            target = request.form.get("peer", "")
            if target in PEERS:
                PEERS.remove(target)
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "save_smtp":
            new_host = request.form.get("smtp_host", "").strip()
            new_port_raw = request.form.get("smtp_port", "").strip()
            new_user = request.form.get("smtp_user", "").strip()
            new_password = request.form.get("smtp_password", "")
            try:
                new_port = int(new_port_raw) if new_port_raw else 587
            except ValueError:
                new_port = -1
            if not new_host:
                errors.append("SMTPホストを入力してください。")
            elif not (1 <= new_port <= 65535):
                errors.append("ポートは1〜65535の数値で入力してください。")
            else:
                SMTP_HOST = new_host
                SMTP_PORT = new_port
                SMTP_USER = new_user
                if new_password:
                    SMTP_PASSWORD = new_password
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "add_console_user":
            new_user = request.form.get("new_console_user", "").strip()
            current_username = (session.get("user") or {}).get("name")
            if not new_user:
                errors.append("ユーザー名を入力してください。")
            elif new_user in CONSOLE_ALLOWED_USERS:
                errors.append("そのユーザーは既に登録されています。")
            elif current_username not in CONSOLE_ALLOWED_USERS and new_user != current_username:
                # The moment this list becomes non-empty, check_auth() starts
                # enforcing it -- so if it doesn't include whoever is saving
                # it, they lock themselves out of /config (and everywhere
                # else) with no way back in except editing config.json by
                # hand on the host. Require them to include themselves.
                errors.append(
                    f"ご自身のユーザー名（{current_username}）もリストに含めてから"
                    "保存してください（自分自身がロックアウトされるのを防ぐため）。"
                )
            else:
                CONSOLE_ALLOWED_USERS.append(new_user)
                try:
                    save_persisted_config()
                except OSError as exc:
                    CONSOLE_ALLOWED_USERS.remove(new_user)
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "remove_console_user":
            target = request.form.get("console_user", "")
            current_username = (session.get("user") or {}).get("name")
            if target == current_username:
                errors.append("自分自身をリストから削除することはできません（ロックアウトを防ぐため）。")
            elif target in CONSOLE_ALLOWED_USERS:
                CONSOLE_ALLOWED_USERS.remove(target)
                try:
                    save_persisted_config()
                except OSError as exc:
                    CONSOLE_ALLOWED_USERS.append(target)
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "save_console_auth_mode":
            new_mode = request.form.get("console_auth_mode", "oidc")
            if new_mode not in ("oidc", "basic"):
                new_mode = "oidc"
            if new_mode == "basic" and not CONSOLE_BASIC_AUTH_USERS:
                # Flipping the mode itself must never be the thing that
                # makes login impossible for the next person -- require at
                # least one account to exist first (add it below, then
                # come back and switch).
                errors.append(
                    "先に「ローカルアカウント管理」で少なくとも1つのアカウントを"
                    "追加してから切り替えてください。"
                )
            else:
                CONSOLE_AUTH_MODE = new_mode
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "save_session_timeout":
            old_timeout = SESSION_IDLE_TIMEOUT_MINUTES
            new_timeout_raw = request.form.get("session_idle_timeout_minutes", "").strip()
            try:
                new_timeout = int(new_timeout_raw)
            except ValueError:
                new_timeout = -1
            if not (1 <= new_timeout <= 10080):  # 1分〜7日
                errors.append("アイドルタイムアウトは1〜10080分（7日）の数値で入力してください。")
            else:
                SESSION_IDLE_TIMEOUT_MINUTES = new_timeout
                app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=new_timeout)
                try:
                    save_persisted_config()
                except OSError as exc:
                    SESSION_IDLE_TIMEOUT_MINUTES = old_timeout
                    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=old_timeout)
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "add_basic_auth_account":
            new_username = request.form.get("new_basic_username", "").strip()
            new_password = request.form.get("new_basic_password", "")
            if not new_username:
                errors.append("ユーザー名を入力してください。")
            elif any(a["username"] == new_username for a in CONSOLE_BASIC_AUTH_USERS):
                errors.append("そのユーザー名は既に登録されています。")
            elif not new_password:
                errors.append("パスワードを入力してください。")
            else:
                password_hash, hash_error = hash_apr1_password(new_password)
                if hash_error:
                    errors.append(f"パスワードのハッシュ化に失敗しました: {hash_error}")
                else:
                    CONSOLE_BASIC_AUTH_USERS.append(
                        {"username": new_username, "password_hash": password_hash}
                    )
                    try:
                        save_persisted_config()
                    except OSError as exc:
                        CONSOLE_BASIC_AUTH_USERS.pop()
                        errors.append(f"設定の保存に失敗しました: {exc}")
                    else:
                        saved = True
        elif action == "remove_basic_auth_account":
            target = request.form.get("target_username", "")
            current_username = (session.get("user") or {}).get("name")
            matching = next(
                (a for a in CONSOLE_BASIC_AUTH_USERS if a["username"] == target), None
            )
            if not matching:
                pass
            elif CONSOLE_AUTH_MODE == "basic" and len(CONSOLE_BASIC_AUTH_USERS) <= 1:
                errors.append(
                    "ローカルアカウントが1件も無くなると誰もログインできなくなるため、"
                    "最後の1件は削除できません。"
                )
            elif CONSOLE_AUTH_MODE == "basic" and target == current_username:
                errors.append("自分自身のアカウントを削除することはできません（ロックアウトを防ぐため）。")
            else:
                CONSOLE_BASIC_AUTH_USERS.remove(matching)
                try:
                    save_persisted_config()
                except OSError as exc:
                    CONSOLE_BASIC_AUTH_USERS.append(matching)
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
        elif action == "set_basic_auth_password":
            target = request.form.get("target_username", "")
            new_password = request.form.get("new_password", "")
            matching = next(
                (a for a in CONSOLE_BASIC_AUTH_USERS if a["username"] == target), None
            )
            if not matching:
                errors.append("対象のアカウントが見つかりません。")
            elif not new_password:
                errors.append("パスワードを入力してください。")
            else:
                password_hash, hash_error = hash_apr1_password(new_password)
                if hash_error:
                    errors.append(f"パスワードのハッシュ化に失敗しました: {hash_error}")
                else:
                    old_hash = matching["password_hash"]
                    matching["password_hash"] = password_hash
                    try:
                        save_persisted_config()
                    except OSError as exc:
                        matching["password_hash"] = old_hash
                        errors.append(f"設定の保存に失敗しました: {exc}")
                    else:
                        saved = True
        elif action == "save_traefik_scheme":
            new_scheme = request.form.get("traefik_scheme", "http")
            if new_scheme not in ("http", "https"):
                new_scheme = "http"
            if new_scheme == "https":
                cert_ok, cert_detail = get_tls_cert_status()
                if not cert_ok:
                    errors.append(f"HTTPSに切り替えるには証明書が必要です: {cert_detail}")
                else:
                    sync_error = sync_tls_secret(force=True)
                    if sync_error:
                        errors.append(f"証明書の反映に失敗しました: {sync_error}")
            if not errors:
                TRAEFIK_SCHEME = new_scheme
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
                    # Existing apps' IngressRoute objects don't pick up the
                    # new entryPoint/tls on their own -- see
                    # resync_all_routes()'s own docstring for why.
                    route_resync_errors = resync_all_routes()
                    # Port 80's external exposure follows the same toggle
                    # -- see sync_traefik_web_exposure()'s own docstring
                    # for why a host firewall can't do this instead.
                    web_exposure_error = sync_traefik_web_exposure()
                    if web_exposure_error:
                        route_resync_errors.append(("_traefik", web_exposure_error))
        elif action == "save_container_passthrough":
            new_enabled = request.form.get("container_passthrough_enabled") == "on"
            if not new_enabled and CONTAINER_PASSTHROUGH_ENABLED:
                # Turning this off would silently strand any app currently
                # using it -- see sync_traefik_web_exposure()'s docstring:
                # 443 would stop following "always open" and start
                # following TRAEFIK_SCHEME instead, closing it outright in
                # "http" mode. Same self-lockout-prevention family as
                # CONSOLE_ALLOWED_USERS/CONSOLE_BASIC_AUTH_USERS's guards.
                passthrough_apps = [
                    a["name"] for a in list_container_apps()[0] if a["tls_mode"] == "passthrough"
                ]
                if passthrough_apps:
                    errors.append(
                        "パススルーモードを使用中のアプリがあるため無効化できません: "
                        + "、".join(passthrough_apps)
                    )
            if not errors:
                CONTAINER_PASSTHROUGH_ENABLED = new_enabled
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
                    web_exposure_error = sync_traefik_web_exposure()
                    if web_exposure_error:
                        route_resync_errors.append(("_traefik", web_exposure_error))
        elif action == "add_container_registry":
            new_host = request.form.get("registry_host", "").strip()
            new_host = re.sub(r"^https?://", "", new_host).rstrip("/")
            new_username = request.form.get("registry_username", "").strip()
            new_password = request.form.get("registry_password", "")
            if not new_host or not EXTERNAL_HOST_RE.match(new_host):
                errors.append("正しいレジストリのホスト名を入力してください (例: registry.example.com)。")
            elif not new_username or not new_password:
                errors.append("ユーザー名とパスワードを入力してください。")
            else:
                # Replace-by-host, not append-only -- re-adding an already
                # registered host (e.g. to rotate its password) updates it
                # in place instead of accumulating a duplicate entry that
                # sync_container_registry_secret() would then just
                # silently overwrite with in list order anyway.
                previous_registries = list(CONTAINER_REGISTRIES)
                CONTAINER_REGISTRIES[:] = [r for r in CONTAINER_REGISTRIES if r.get("host") != new_host]
                CONTAINER_REGISTRIES.append(
                    {"host": new_host, "username": new_username, "password": new_password}
                )
                try:
                    save_persisted_config()
                except OSError as exc:
                    CONTAINER_REGISTRIES[:] = previous_registries
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
                    registry_sync_error = sync_container_registry_secret()
                    if registry_sync_error:
                        errors.append(f"レジストリ認証情報の反映に失敗しました: {registry_sync_error}")
        elif action == "remove_container_registry":
            target_host = request.form.get("registry_host", "")
            previous_registries = list(CONTAINER_REGISTRIES)
            CONTAINER_REGISTRIES[:] = [r for r in CONTAINER_REGISTRIES if r.get("host") != target_host]
            if len(CONTAINER_REGISTRIES) != len(previous_registries):
                try:
                    save_persisted_config()
                except OSError as exc:
                    CONTAINER_REGISTRIES[:] = previous_registries
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
                    registry_sync_error = sync_container_registry_secret()
                    if registry_sync_error:
                        errors.append(f"レジストリ認証情報の反映に失敗しました: {registry_sync_error}")
        elif action == "sync_tls_now":
            sync_error = sync_tls_secret(force=True)
            if sync_error:
                errors.append(f"証明書の反映に失敗しました: {sync_error}")
            else:
                saved = True
        elif action == "save_console_via_traefik":
            new_value = request.form.get("console_via_traefik") == "on"
            if new_value and not FORWARDAUTH_HOST_IP:
                errors.append(
                    "有効にするには SPINAPP_UI_HOST_IP 環境変数（このk3sノード自身の"
                    "IPアドレス）の設定が必要です。/etc/spinapp-ui/env に追記してから"
                    "spinapp-ui サービスを再起動してください。"
                )
            else:
                CONSOLE_VIA_TRAEFIK = new_value
                try:
                    save_persisted_config()
                except OSError as exc:
                    errors.append(f"設定の保存に失敗しました: {exc}")
                else:
                    saved = True
                    if FORWARDAUTH_HOST_IP:
                        reach_error = ensure_forwardauth_reachability(force=True)
                        if reach_error:
                            errors.append(reach_error)

    env_file_values = read_env_file()
    tls_cert_configured, tls_cert_detail = get_tls_cert_status()
    # For the export section's "individual" dropdown -- same three list_*()
    # calls index() already makes for the dashboard's own tables, just
    # narrowed down to names grouped by kind (matching EXPORT_BUILDERS'
    # keys) instead of the full per-kind detail those tables show.
    spinapps, _ = list_spinapps()
    crons, _ = list_cronjobs()
    containers, _ = list_container_apps()
    export_items = {
        "js": [a["name"] for a in spinapps if a["kind"] == "js"],
        "site": [a["name"] for a in spinapps if a["kind"] == "site"],
        "proxy": [a["name"] for a in spinapps if a["kind"] == "proxy"],
        "cron": [c["name"] for c in crons],
        "container": [c["name"] for c in containers],
    }
    return render_template(
        "config.html",
        namespace=NAMESPACE,
        active_nav="config",
        export_items=export_items,
        errors=errors,
        saved=saved,
        restarting=saved and request.form.get("action") in (
            "save_oidc", "save_peer_token", "generate_peer_token",
        ),
        fqdns=REGISTERED_FQDNS,
        default_fqdn=DEFAULT_FQDN,
        oidc_issuer=env_file_values.get("SPINAPP_UI_OIDC_ISSUER", OIDC_ISSUER or ""),
        oidc_client_id=env_file_values.get("SPINAPP_UI_OIDC_CLIENT_ID", OIDC_CLIENT_ID or ""),
        oidc_redirect_uri=env_file_values.get("SPINAPP_UI_OIDC_REDIRECT_URI", OIDC_REDIRECT_URI or ""),
        console_allowed_users=CONSOLE_ALLOWED_USERS,
        console_auth_mode=CONSOLE_AUTH_MODE,
        basic_auth_users=CONSOLE_BASIC_AUTH_USERS,
        session_idle_timeout_minutes=SESSION_IDLE_TIMEOUT_MINUTES,
        peers=PEERS,
        peer_token_configured=bool(PEER_TOKEN),
        generated_peer_token=generated_peer_token,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_user=SMTP_USER,
        smtp_password_configured=bool(SMTP_PASSWORD),
        traefik_scheme=TRAEFIK_SCHEME,
        container_passthrough_enabled=CONTAINER_PASSTHROUGH_ENABLED,
        # host/username only -- passwords are never sent back to the
        # browser once saved, same reasoning as smtp_password_configured
        # above (re-adding the same host overwrites its credentials, so
        # there's no "edit in place" UI that would need the old value).
        container_registries=[
            {"host": r.get("host", ""), "username": r.get("username", "")}
            for r in CONTAINER_REGISTRIES
        ],
        tls_cert_configured=tls_cert_configured,
        tls_cert_detail=tls_cert_detail,
        tls_cert_path=str(TLS_CERT_PATH),
        tls_key_path=str(TLS_KEY_PATH),
        route_resync_errors=route_resync_errors,
        console_via_traefik=CONSOLE_VIA_TRAEFIK,
        forwardauth_host_ip_configured=bool(FORWARDAUTH_HOST_IP),
        console_host=urlsplit(OIDC_REDIRECT_URI or "").hostname or "",
    )


@app.route("/apps/new")
def new_app_page():
    from_name = request.args.get("from", "").strip()
    form_code = None
    form_fqdn = ""
    if from_name and NAME_RE.match(from_name):
        form_code = get_js_source(from_name)
        result = kubectl(["get", "spinapp", from_name, "-n", NAMESPACE, "-o", "json"])
        if result.returncode == 0:
            annotations = json.loads(result.stdout)["metadata"].get("annotations", {}) or {}
            form_fqdn = annotations.get(FQDN_ANNOTATION, "")
    return render_template(
        "app_new.html",
        namespace=NAMESPACE,
        active_nav="new",
        fqdns=REGISTERED_FQDNS,
        form_code=form_code,
        form_fqdn=form_fqdn,
    )


@app.route("/apps/<name>/edit")
def edit_app(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    code = get_js_source(name)
    if code is None:
        apps, error = list_spinapps()
        crons, cron_error = list_cronjobs()
        containers, container_error = list_container_apps()
        return render_template(
            "index.html",
            apps=apps,
            error=error,
            crons=crons,
            cron_error=cron_error,
            containers=containers,
            container_error=container_error,
            errors=["この SpinApp は JS ソースを保持していないため編集できません。"],
            namespace=NAMESPACE,
            active_nav="dashboard",
        ), 400
    result = kubectl(["get", "spinapp", name, "-n", NAMESPACE, "-o", "json"])
    annotations = {}
    if result.returncode == 0:
        annotations = json.loads(result.stdout)["metadata"].get("annotations", {}) or {}
    return render_template(
        "app_edit.html",
        namespace=NAMESPACE,
        active_nav="new",
        edit_name=name,
        is_edit=True,
        form_name=name,
        form_code=code,
        auth_mode=resolve_auth_mode(annotations),
        auth_username=annotations.get(AUTH_USERNAME_ANNOTATION, ""),
        oidc_users_text=oidc_users_text(annotations),
        fqdns=REGISTERED_FQDNS,
        form_fqdn=annotations.get(FQDN_ANNOTATION, ""),
    )


def delete_app_locally(name):
    """The actual teardown, shared by the user-facing delete route and the
    peer-replicated /internal/apps/<name>/delete."""
    kubectl(["delete", "spinapp", name, "-n", NAMESPACE, "--ignore-not-found"])
    delete_route(name)
    site_dir = SITES_DATA_DIR / name
    if site_dir.is_dir():
        shutil.rmtree(site_dir, ignore_errors=True)


@app.route("/apps/<name>/delete", methods=["POST"])
def delete_app(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    delete_app_locally(name)
    replicate_to_peers(f"/internal/apps/{name}/delete", {})
    return redirect(url_for("index"))


@app.route("/internal/apps/<name>/delete", methods=["POST"])
@csrf.exempt
def internal_delete_app(name):
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    if not NAME_RE.match(name):
        return jsonify(ok=False, error="invalid name"), 400
    delete_app_locally(name)
    return jsonify(ok=True)


MAX_APP_LOG_LINES = 500


@app.route("/apps/<name>/logs")
def app_logs(name):
    """Plain-text tail of every pod's logs for this SpinApp (used by the
    "ログ" tab on each editor page). Pods are found by the label
    core.spinkube.dev/app-name=<name>, which the Spin operator sets on
    every pod it creates for a SpinApp -- not by name, since the pod name
    has a random suffix."""
    if not NAME_RE.match(name):
        return "invalid name", 400
    result = kubectl(
        [
            "logs",
            "-l",
            f"core.spinkube.dev/app-name={name}",
            "-n",
            NAMESPACE,
            f"--tail={MAX_APP_LOG_LINES}",
            "--prefix=true",
        ]
    )
    output = result.stdout or ""
    if result.returncode != 0:
        output += ("\n" if output else "") + (result.stderr or "ログの取得に失敗しました。")
    return Response(output or "(ログはまだありません)", mimetype="text/plain")


def _toml_str(value):
    return json.dumps(value)


def build_site_spin_toml(name, file_paths):
    """Generate a spin.toml for a static/PHP site given the list of relative
    file paths it contains (files themselves are expected under ./site/).

    No .php files -> a single static-fileserver component on the catch-all
    "/..." route (it auto-resolves "index.html" for directory requests,
    serves subdirectories, and sets correct Content-Type per file itself).

    Any .php file present -> BOTH a static-fileserver component AND a WAGI
    php-cgi component:
      - static-fileserver gets one EXPLICIT literal route per *non*-.php
        file (e.g. "/pixel.png", "/style.css") -- Spin matches literal
        routes before the "/..." wildcard, so these take priority.
      - php-cgi gets the catch-all "/..." route, same as the no-PHP case,
        and only ever actually executes for .php files reached through it
        (plus directory-index requests, rewritten to their index.php by
        apply_site_route() at the Traefik layer -- see there).

    This split is load-bearing, not a simplification of convenience: an
    earlier version had php-cgi alone handle EVERY file when any .php was
    present (relying on it echoing non-PHP files' raw bytes verbatim when
    they have no "<?php" tag), on the theory that WAGI's php-cgi component
    can only ever use the bare "/..." route -- confirmed empirically that
    giving it an exact literal route, or even a per-file wildcard like
    "/index.php/...", makes every request crash with a wasm trap. That
    theory is still correct, but it turned out incomplete: php-cgi garbles
    or crashes (`Exited with i32 exit status 1`) on genuinely binary
    content (confirmed with a real PDF upload -- worked fine for a plain
    .css file's *text* bytes, but not arbitrary binary). The actual fix
    isn't "php-cgi echoes non-PHP files", it's routing non-PHP files to
    static-fileserver directly and confirmed empirically that a
    static-fileserver component with literal per-file routes happily
    coexists in the same app as a php-cgi component on "/..." -- Spin's
    literal-route-wins-over-wildcard matching applies across components
    just as well as within one. This also means the previous
    Content-Type-override Traefik middlewares (STATIC_CONTENT_TYPES) are
    no longer needed for the has-PHP case: static-fileserver already sets
    correct Content-Type for every non-PHP file itself.
    """
    php_paths = [p for p in file_paths if p.endswith(".php")]
    static_paths = [p for p in file_paths if not p.endswith(".php")]

    lines = [
        "spin_manifest_version = 2",
        "",
        "[application]",
        f"name = {_toml_str(name)}",
        'version = "0.1.0"',
        "",
    ]

    if php_paths:
        for p in static_paths:
            lines.append("[[trigger.http]]")
            lines.append(f"route = {_toml_str('/' + p)}")
            lines.append('component = "static"')
            lines.append("")
        lines.append("[[trigger.http]]")
        lines.append('route = "/..."')
        lines.append('component = "php"')
        lines.append('executor = { type = "wagi" }')
        lines.append("")

        lines.append("[component.static]")
        lines.append('files = [{ source = "site", destination = "/" }]')
        lines.append("[component.static.source]")
        lines.append(f'url = {_toml_str(STATIC_FILESERVER_SOURCE["url"])}')
        lines.append(f'digest = {_toml_str(STATIC_FILESERVER_SOURCE["digest"])}')
        lines.append("")

        lines.append("[component.php]")
        # Wide open, same as JS apps' component (see build_and_push_js()'s
        # template) -- not a weaker default kept out of caution, since the
        # real enforcement layer for outbound traffic from any console-
        # created workload is spinapp-egress-policy.yaml's NetworkPolicy
        # (podSelector: {} in the "default" namespace, so it already
        # covers PHP site Pods exactly like every other kind). This
        # Wasm-level allow-list being empty never added a second layer of
        # protection here, so opening it costs nothing -- but confirmed
        # live it's NOT sufficient to make outbound PHP calls actually
        # work: PHP_CGI_SOURCE's php-cgi-8.2.6-slim.wasm build has no
        # openssl/curl extension AND no working socket even for plain
        # http:// (file_get_contents() against a real host still returns
        # false with this wide open, get_loaded_extensions() shows no
        # network-capable extension at all). That's a limitation of the
        # slim WASI PHP build itself, not something any spin.toml setting
        # can work around -- a PHP site that needs to call an external API
        # has to do it client-side (fetch() against a New Proxy app, which
        # has working outbound) instead, at least until a PHP build with
        # real socket support is available for this executor.
        lines.append('allowed_outbound_hosts = ["http://*:*", "https://*:*"]')
        lines.append('files = [{ source = "site", destination = "/" }]')
        lines.append("[component.php.source]")
        lines.append(f'url = {_toml_str(PHP_CGI_SOURCE["url"])}')
        lines.append(f'digest = {_toml_str(PHP_CGI_SOURCE["digest"])}')
        lines.append("")
    else:
        lines.append("[[trigger.http]]")
        lines.append('route = "/..."')
        lines.append('component = "static"')
        lines.append("")

        lines.append("[component.static]")
        lines.append('files = [{ source = "site", destination = "/" }]')
        lines.append("[component.static.source]")
        lines.append(f'url = {_toml_str(STATIC_FILESERVER_SOURCE["url"])}')
        lines.append(f'digest = {_toml_str(STATIC_FILESERVER_SOURCE["digest"])}')
        lines.append("")

    return "\n".join(lines)


def validate_site_files(files):
    """files: dict[relative_path, str content] -- content is raw text for
    normal files, base64 for files with a BINARY_SITE_EXTENSIONS extension
    (see is_binary_site_path()). Returns a list of error strings (empty if
    valid)."""
    errors = []
    if not files:
        errors.append("最低1つのファイルが必要です。")
        return errors
    if len(files) > MAX_SITE_FILES:
        errors.append(f"ファイル数が多すぎます ({MAX_SITE_FILES}個まで)。")
    total = 0
    for path, content in files.items():
        if not SITE_PATH_RE.match(path) or ".." in path.split("/"):
            errors.append(f"不正なファイルパスです: {path}")
            continue
        if is_binary_site_path(path):
            try:
                size = len(base64.b64decode(content, validate=True))
            except (ValueError, base64.binascii.Error):
                errors.append(f"{path} のデータが不正です。")
                continue
        else:
            size = len(content.encode("utf-8"))
        total += size
        if size > MAX_SITE_FILE_BYTES:
            errors.append(
                f"{path} が大きすぎます ({MAX_SITE_FILE_BYTES // 1_000_000}MB まで)。"
            )
    if total > MAX_SITE_TOTAL_BYTES:
        errors.append(
            f"合計サイズが大きすぎます ({MAX_SITE_TOTAL_BYTES // 1_000_000}MB まで)。"
        )
    return errors


def get_site_files(name):
    """Return {relative_path: content} for a site stored on disk, or None if
    no such site directory exists. Binary files (per
    is_binary_site_path()) are base64-encoded, matching what
    validate_site_files()/build_and_push_site() expect on the way back in."""
    site_dir = SITES_DATA_DIR / name
    if not site_dir.is_dir():
        return None
    files = {}
    for f in site_dir.rglob("*"):
        if f.is_file():
            rel = f.relative_to(site_dir).as_posix()
            if is_binary_site_path(rel):
                files[rel] = base64.b64encode(f.read_bytes()).decode("ascii")
            else:
                try:
                    files[rel] = f.read_text("utf-8")
                except UnicodeDecodeError:
                    continue
    return files


def build_and_push_site(name, files, log):
    """Write `files` (dict[relative_path, str]) to SITES_DATA_DIR/<name>/,
    generate a spin.toml, and push to the local registry. Returns the pushed
    image reference. Raises RuntimeError (with build output appended to
    `log`) on failure.
    """
    site_dir = SITES_DATA_DIR / name
    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)
    for rel_path, content in files.items():
        # Callers are expected to have already run validate_site_files()
        # (SITE_PATH_RE + a "no .. segment" check), but re-checking the
        # traversal part here too -- right where rel_path actually turns
        # into a filesystem path -- means a caller that forgets can't turn
        # into an arbitrary-path write on this host (confirmed one already
        # had: internal_apply_site() was missing that call entirely).
        if ".." in Path(rel_path).parts or Path(rel_path).is_absolute():
            raise RuntimeError(f"不正なファイルパスです: {rel_path}")
        dest = site_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if is_binary_site_path(rel_path):
            dest.write_bytes(base64.b64decode(content))
        else:
            dest.write_text(content, "utf-8")

    build_dir = tempfile.mkdtemp(prefix="spinapp-site-")
    try:
        proj_dir = os.path.join(build_dir, "proj")
        os.makedirs(proj_dir)
        shutil.copytree(site_dir, os.path.join(proj_dir, "site"))
        with open(os.path.join(proj_dir, "spin.toml"), "w") as f:
            f.write(build_site_spin_toml(name, list(files.keys())))

        tag = secrets.token_hex(4)
        image_ref = f"{REGISTRY}/{name}:{tag}"
        push = subprocess.run(
            ["spin", "registry", "push", "-k", image_ref],
            cwd=proj_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        log.append(push.stdout)
        log.append(push.stderr)
        if push.returncode != 0:
            raise RuntimeError("レジストリへの push に失敗しました")

        return image_ref
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


# Matches spinapp-egress-policy.yaml.tmpl's own "except" list exactly
# (see that file's comments) -- these two, plus link-local/loopback/
# multicast/reserved/unspecified and this node's own IP, are the only
# ranges check_backend_host_not_internal() actually rejects. Hardcoded
# here the same way the NetworkPolicy template itself hardcodes them
# (only __NODE_IP__ is install.sh-substituted there; these two aren't).
CLUSTER_POD_CIDR = ipaddress.ip_network("10.42.0.0/16")
CLUSTER_SERVICE_CIDR = ipaddress.ip_network("10.43.0.0/16")


def check_backend_host_not_internal(hostname):
    """Returns an error string if `hostname` -- or, if it's a plain DNS
    name, whatever it currently resolves to -- is a cluster-internal
    address (this cluster's own Pod/Service CIDR, or this node's own IP
    -- see FORWARDAUTH_HOST_IP), or a loopback/link-local/multicast/
    reserved/unspecified one (this covers cloud metadata endpoints, e.g.
    169.254.169.254, which fall under link-local). Best-effort input
    validation only, run once at proxy-config save time: it catches
    "typed an internal IP into Backend Origin by mistake or by malice"
    immediately, but it is NOT a substitute for network-level isolation
    -- DNS can be repointed after this check runs (rebinding), and the
    actual fetch happens later, at request time, from inside the deployed
    Wasm component, entirely outside this function's control. The real
    enforcement is the "default" namespace's egress NetworkPolicy (see
    deploy/k8s-manifests/spinapp-egress-policy.yaml.tmpl), which blocks
    these same address ranges at the network layer regardless of what any
    proxy app's stored config says.

    Deliberately does NOT reject the wider RFC1918 private space (a plain
    addr.is_private check, which this function used to use, would also
    catch e.g. 192.168.1.50 -- a real LAN backend that isn't this
    cluster's own IP at all, just because it happens to also be a
    private address) -- confirmed live this broke importing a real,
    already-working proxy app's exported config right back in. The
    NetworkPolicy's own header comment is explicit that the wider LAN
    (a NAS, an internal API, ...) is a legitimate, intentional Proxy
    Backend Origin target, not something to treat as equivalent to
    reaching the cluster's own internals -- this function now matches
    that same boundary instead of being needlessly stricter than the
    actual enforcement layer.
    """
    if not hostname:
        return None
    try:
        addrs = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            return f"ホスト名を解決できませんでした: {hostname} ({exc})"
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    for addr in addrs:
        is_this_node = bool(FORWARDAUTH_HOST_IP) and str(addr) == FORWARDAUTH_HOST_IP
        is_cluster_cidr = (addr.version == 4) and (
            addr in CLUSTER_POD_CIDR or addr in CLUSTER_SERVICE_CIDR
        )
        if (
            addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
            or is_cluster_cidr
            or is_this_node
        ):
            return (
                f"Backend Originにクラスタ内部/予約アドレスは指定できません "
                f"({hostname} -> {addr})。クラウドのメタデータエンドポイントや"
                f"クラスタ内部サービスへの意図しないアクセスを防ぐためのガードです。"
            )
    return None


def validate_proxy_config(backend_origin, rules, rewrites, ws_path=""):
    """Validate a proxy app's structured config. Returns
    (errors, normalized_origin, backend_prefix); normalized_origin has no
    trailing slash/path/query/fragment. A path typed into backend_origin
    itself (e.g. "http://host:8080/example") becomes backend_prefix --
    the prefix the backend expects to see on every request, prepended by
    the generated JS worker (see render_proxy_js()) *after* `rules` has
    already matched against the plain, prefix-free path, and used to
    translate the backend's own self-referential redirects/cookie Paths
    back to this app's own PROXY_PREFIX. Equivalent to typing the same
    backend_origin without the path and entering it as a separate
    "Backend Path Prefix" field would have been, before that field was
    folded into this one for a single input instead of two.
    """
    errors = []
    ws_path = (ws_path or "").strip()
    if ws_path and (not ws_path.startswith("/") or " " in ws_path or len(ws_path) > 200):
        errors.append(
            'WebSocketパスは "/" から始まる空白なしの文字列にしてください (例: /api/v1/ws)。'
        )
    parts = urlsplit(backend_origin.strip())
    backend_prefix = ""
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.query
        or parts.fragment
    ):
        errors.append(
            'Backend Origin は "http(s)://host[:port][/path]" の形式で入力してください。'
        )
        origin = backend_origin.strip().rstrip("/")
    else:
        origin = f"{parts.scheme}://{parts.netloc}"
        backend_prefix = parts.path.rstrip("/")
        host_error = check_backend_host_not_internal(parts.hostname)
        if host_error:
            errors.append(host_error)

    if len(rules) > MAX_PROXY_RULES:
        errors.append(f"ルールは {MAX_PROXY_RULES} 件までです。")
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            errors.append(f"ルール {i}: 形式が不正です。")
            continue
        if r.get("match") not in PROXY_MATCH_TYPES:
            errors.append(f"ルール {i}: match は exact/prefix のいずれかにしてください。")
        if not isinstance(r.get("path"), str) or not r["path"].startswith("/"):
            errors.append(f'ルール {i}: path は "/" から始まる文字列にしてください。')
        if r.get("action") not in PROXY_ACTIONS:
            errors.append(f"ルール {i}: action は passthrough/block/bounce のいずれかにしてください。")
        if r.get("action") == "bounce" and (
            not isinstance(r.get("target"), str) or not r["target"].startswith("/")
        ):
            errors.append(f'ルール {i}: bounce には "/" から始まる target が必要です。')

    if len(rewrites) > MAX_PROXY_REWRITES:
        errors.append(f"書き換えルールは {MAX_PROXY_REWRITES} 件までです。")
    for i, rw in enumerate(rewrites, 1):
        if not isinstance(rw, dict):
            errors.append(f"書き換え {i}: 形式が不正です。")
            continue
        pattern = rw.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            errors.append(f"書き換え {i}: pattern が空です。")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                # JS regex syntax isn't identical to Python's, but this catches
                # the overwhelming majority of typos before a build is wasted.
                errors.append(f"書き換え {i}: pattern が正規表現として不正です ({exc})。")
        if not isinstance(rw.get("replace"), str):
            errors.append(f"書き換え {i}: replace は文字列にしてください。")

    return errors, origin, backend_prefix


# {{PREFIX}} in a rewrite's "replace" is substituted with this app's own
# "/<name>" prefix at generation time (see render_proxy_js()) -- rules and
# rewrites are otherwise written without knowing the app's name in advance.
PROXY_JS_TEMPLATE = """// Generated by SpinDeck's "New Proxy" -- reverse proxy with path rules and
// optional response rewriting. This file is regenerated from the app's
// structured config on every deploy; edit it from the SpinDeck dashboard
// rather than hand-editing the generated code, or your changes will be
// overwritten on the next redeploy.

const BACKEND_ORIGIN = __BACKEND_ORIGIN__;
const PROXY_PREFIX = __PREFIX__;
// Non-empty only when the backend is itself prefix-aware (told about its
// own base path via its own "Site URL"-style setting, and therefore
// expects to *receive* that prefix rather than have it stripped -- see
// deploy_proxy_spinapp()'s "backend_prefix" option). Usually equal to
// PROXY_PREFIX, but doesn't have to be: two different SpinDeck proxy apps
// can front the same backend under two different public names while both
// needing to talk to it using the *one* prefix it's actually configured
// with (confirmed live -- this is exactly that situation).
const BACKEND_PREFIX = __BACKEND_PREFIX__;
const RULES = __RULES_JSON__;
const REWRITE_RULES = [
__REWRITES_JS__
];
const REWRITTEN_TYPES = ['text/html', 'text/css', 'javascript', 'manifest+json', 'application/json'];
// A prefix-aware backend doesn't just redirect/cookie using its own prefix
// (see rewriteLocation()/rewriteSetCookie()) -- it bakes that same prefix
// into HTML/JS/JSON response bodies too (asset paths, client-side router
// config, embedded page state...), so response bodies need rewriting even
// when the app owner hasn't configured any REWRITE_RULES of their own
// (confirmed live: a shared-album page's client-side navigation carried
// BACKEND_PREFIX straight through, sending the browser to a *different*
// SpinDeck proxy app's public path entirely).
const NEEDS_BODY_REWRITE = REWRITE_RULES.length > 0 || (BACKEND_PREFIX && BACKEND_PREFIX !== PROXY_PREFIX);

function shouldRewrite(contentType) {
  if (!contentType) return false;
  const ct = contentType.toLowerCase();
  return REWRITTEN_TYPES.some((t) => ct.includes(t));
}

function rewriteText(text) {
  // Blanket substring swap first: BACKEND_PREFIX -> PROXY_PREFIX wherever it
  // appears verbatim. Skipped when they're equal (the common case, and
  // REWRITE_RULES-only apps) so nothing changes for apps that never set
  // backend_prefix at all.
  if (BACKEND_PREFIX && BACKEND_PREFIX !== PROXY_PREFIX) {
    text = text.split(BACKEND_PREFIX).join(PROXY_PREFIX);
  }
  return REWRITE_RULES.reduce((acc, r) => acc.replace(r.re, r.replace), text);
}

function rewriteLocation(location) {
  if (!location) return location;
  // User-defined REWRITE_RULES run first: this is the only hook for a
  // Location that neither starts with BACKEND_ORIGIN nor "/" below --
  // e.g. a backend that has baked its *own* public URL (missing our
  // PROXY_PREFIX) into a redirect_uri= query param of a redirect to a
  // *third-party* host (an OIDC provider, for example). The automatic
  // prefixing below can't know to touch a substring buried inside some
  // other host's query string, but a REWRITE_RULES regex the app owner
  // wrote for exactly that string can.
  location = rewriteText(location);
  if (location.startsWith(BACKEND_ORIGIN)) {
    location = location.slice(BACKEND_ORIGIN.length) || '/';
  } else if (!location.startsWith('/')) {
    return location; // relative Location: resolves fine against the already-prefixed page URL
  }
  // A BACKEND_PREFIX-aware backend's own self-referential redirects already
  // carry *its* prefix, not ours -- swap it for PROXY_PREFIX rather than
  // stacking ours on top of it.
  if (BACKEND_PREFIX && location.startsWith(BACKEND_PREFIX)) {
    location = location.slice(BACKEND_PREFIX.length) || '/';
  }
  return location.startsWith(PROXY_PREFIX) ? location : PROXY_PREFIX + location;
}

// A backend that scopes a cookie to a narrower Path= than "/" (common for
// e.g. OIDC state/nonce/PKCE cookies) is scoping it relative to *its own*
// unprefixed view of the world -- the browser is at PROXY_PREFIX+path, so
// without this the cookie's Path never matches the pages it's actually
// needed on and the browser silently drops it from later requests
// (confirmed live: this broke a backend's OIDC login, which came back with
// valid state/nonce/pkce cookies scoped to e.g. "/api/v1/oidc", making them
// invisible on "/<prefix>/api/v1/oidc/..." and failing state validation on
// the callback). A cookie with no explicit Path doesn't need this: browsers
// default that to the *browsed* URL's own directory, which is already
// correctly prefixed since that's what's in the address bar.
function rewriteSetCookie(cookie) {
  return cookie.replace(/(;\\s*Path=)([^;]*)/i, (whole, attr, path) => {
    if (!path.startsWith('/')) return whole;
    if (BACKEND_PREFIX && path.startsWith(BACKEND_PREFIX)) {
      path = path.slice(BACKEND_PREFIX.length) || '/';
    }
    return attr + (path.startsWith(PROXY_PREFIX) ? path : PROXY_PREFIX + path);
  });
}

// Headers only exposes multiple same-name values (as opposed to merging
// them into one comma-joined string, which would corrupt Set-Cookie -- its
// values can themselves contain commas, e.g. in an Expires= date) via the
// newer getSetCookie() method. Fall back to leaving Set-Cookie untouched
// if this runtime's Headers doesn't have it, rather than risk mangling
// cookies by guessing at how to split a merged string.
function rewriteSetCookies(headers) {
  if (typeof headers.getSetCookie !== 'function') return;
  const cookies = headers.getSetCookie();
  if (!cookies.length) return;
  headers.delete('set-cookie');
  for (const cookie of cookies) {
    headers.append('set-cookie', rewriteSetCookie(cookie));
  }
}

// Rules are evaluated in order; the first match wins. No match falls
// through to a plain passthrough at the original path.
function resolveBackendPath(pathname) {
  const p = pathname === '' ? '/' : pathname;
  for (const rule of RULES) {
    const prefixBase = rule.path.endsWith('/') ? rule.path : rule.path + '/';
    const hit = rule.match === 'exact' ? p === rule.path : (p === rule.path || p.startsWith(prefixBase));
    if (!hit) continue;
    if (rule.action === 'block') return null;
    if (rule.action === 'bounce') return rule.target;
    return p; // passthrough
  }
  return p;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const backendPath = resolveBackendPath(url.pathname);
    if (backendPath === null) {
      return new Response('Not Found', { status: 404 });
    }
    // BACKEND_PREFIX is appended *after* RULES have already matched against
    // the plain prefix-free backendPath above, so `rules` configuration
    // keeps meaning the same thing regardless of whether backend_prefix is
    // set -- only what's actually sent to BACKEND_ORIGIN changes.
    const backendUrl = BACKEND_ORIGIN + BACKEND_PREFIX + backendPath + url.search;

    const headers = new Headers(request.headers);
    headers.delete('host');
    headers.delete('content-length');
    // The Spin/StarlingMonkey runtime's fetch() doesn't transparently
    // inflate compressed bodies the way a browser's fetch does: if upstream
    // sends one back (e.g. because we forwarded a real browser's
    // "Accept-Encoding: gzip"), upstream.text() below throws trying to
    // UTF-8-decode the raw compressed bytes. Forcing identity avoids that.
    headers.set('accept-encoding', 'identity');

    const init = {
      method: request.method,
      headers,
      redirect: 'manual',
    };
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      init.body = await request.arrayBuffer();
    }

    const upstream = await fetch(backendUrl, init);

    const responseHeaders = new Headers(upstream.headers);
    const location = responseHeaders.get('location');
    if (location) {
      responseHeaders.set('location', rewriteLocation(location));
    }
    rewriteSetCookies(responseHeaders);

    const contentType = upstream.headers.get('content-type');
    if (!NEEDS_BODY_REWRITE || !shouldRewrite(contentType)) {
      return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: responseHeaders,
      });
    }

    const text = await upstream.text();
    responseHeaders.delete('content-length');
    responseHeaders.delete('content-encoding');
    return new Response(rewriteText(text), {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  },
};
"""


def render_proxy_js(name, backend_origin, rules, rewrites, backend_prefix=""):
    """Generate the Cloudflare-Workers-style JS source for a "proxy" app
    from its structured config. Traefik strips "/<name>" before this code
    ever sees a request (see apply_route()), so PROXY_PREFIX only matters
    for putting the prefix back into rewritten response bodies/redirects,
    since the browser is still addressing "/<name>/...".
    """
    prefix = f"/{name}"
    backend_prefix = (backend_prefix or "").strip()
    rules_json = json.dumps(
        [
            {
                "match": r["match"],
                "path": r["path"],
                "action": r["action"],
                "target": r.get("target", ""),
            }
            for r in rules
        ]
    )
    rewrite_lines = [
        "  { re: new RegExp(%s, 'g'), replace: %s },"
        % (
            json.dumps(rw["pattern"]),
            json.dumps(rw.get("replace", "").replace("{{PREFIX}}", prefix)),
        )
        for rw in rewrites
    ]
    code = PROXY_JS_TEMPLATE
    code = code.replace("__BACKEND_ORIGIN__", json.dumps(backend_origin))
    code = code.replace("__PREFIX__", json.dumps(prefix))
    code = code.replace("__BACKEND_PREFIX__", json.dumps(backend_prefix))
    code = code.replace("__RULES_JSON__", rules_json)
    code = code.replace("__REWRITES_JS__", "\n".join(rewrite_lines))
    return code


def _link_or_copy(src, dst):
    """Hardlink src to dst, falling back to a real copy if they're on
    different filesystems (os.link() can never cross a device boundary --
    that's an OS-level constraint, not something retryable). The build
    host's /tmp and JS_TEMPLATE_DIR happening to share a filesystem was
    never guaranteed; confirmed live that a host where they don't (/tmp on
    tmpfs, template dir on the root disk) makes every hardlink attempt
    fail with EXDEV ("Invalid cross-device link"), breaking every JS/Proxy
    app build outright. Any other OSError still propagates normally.
    """
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)


def build_and_push_js(name, code, log):
    """Build a Cloudflare-Workers-style JS handler into a Spin/Wasm component
    and push it to the local registry. Returns the pushed image reference.
    Raises RuntimeError (with build output already appended to `log`) on failure.
    """
    build_dir = tempfile.mkdtemp(prefix="spinapp-js-")
    try:
        # Hardlink-copy the pre-built template (node_modules included) so we
        # skip `npm install` on every request; only src/index.js changes.
        # Only exclude these dirs at the template root -- node_modules
        # contains packages that ship their own "dist"/"build" output dirs,
        # which must NOT be filtered out.
        exclude_top_level = {".git", "dist", "build", ".spin"}
        template_root = os.path.normpath(JS_TEMPLATE_DIR)

        def ignore(dir_path, names):
            if os.path.normpath(dir_path) == template_root:
                return [n for n in names if n in exclude_top_level]
            return []

        proj_dir = os.path.join(build_dir, "proj")
        shutil.copytree(
            JS_TEMPLATE_DIR,
            proj_dir,
            copy_function=_link_or_copy,
            ignore=ignore,
        )

        # src/index.js is a fixed wrapper (hardlinked from the shared
        # template) that bridges both worker styles onto addEventListener
        # and loads the cache-shim; only user-code.js is ever written here.
        # IMPORTANT: never open src/index.js with "w" -- it is hardlinked to
        # the shared template's file (same inode), so truncating it here
        # would corrupt the template for every future build.
        user_code_path = os.path.join(proj_dir, "src", "user-code.js")
        if os.path.exists(user_code_path):
            os.unlink(user_code_path)
        with open(user_code_path, "w") as f:
            f.write(code)

        build = subprocess.run(
            ["npm", "run", "build"],
            cwd=proj_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )
        log.append(build.stdout)
        log.append(build.stderr)
        if build.returncode != 0:
            raise RuntimeError("ビルドに失敗しました")

        tag = secrets.token_hex(4)
        image_ref = f"{REGISTRY}/{name}:{tag}"
        push = subprocess.run(
            ["spin", "registry", "push", "-k", image_ref],
            cwd=proj_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        log.append(push.stdout)
        log.append(push.stderr)
        if push.returncode != 0:
            raise RuntimeError("レジストリへの push に失敗しました")

        return image_ref
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def deploy_js_spinapp(name, code, auth_mode, auth_username, htpasswd_hash, oidc_users, fqdn):
    """Build+push the JS app and apply its SpinApp/auth/route. Shared by the
    user-facing create_js_app and the peer-replicated /internal/apps/js, so
    both end up with identical state. Returns (ok, error, build_log)."""
    log = []
    try:
        image_ref = build_and_push_js(name, code, log)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as build log
        return False, f"JS アプリのビルドに失敗しました: {exc}", "\n".join(filter(None, log))

    manifest = {
        "apiVersion": "core.spinkube.dev/v1alpha1",
        "kind": "SpinApp",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "annotations": {
                JS_SOURCE_ANNOTATION: base64.b64encode(
                    code.encode("utf-8")
                ).decode("ascii"),
                **build_auth_annotations(auth_mode, auth_username, oidc_users),
                FQDN_ANNOTATION: fqdn or "",
            },
        },
        "spec": {
            "image": image_ref,
            "executor": EXECUTOR,
            "replicas": 1,
        },
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
    if result.returncode != 0:
        return False, f"デプロイに失敗しました: {result.stderr.strip()}", None

    auth_error = apply_basic_auth_secret(name, auth_mode == "basic", auth_username, htpasswd_hash)
    if auth_error:
        return False, f"認証設定に失敗しました: {auth_error.strip()}", None

    oidc_error = apply_oidc_auth_middleware(name, auth_mode == "oidc")
    if oidc_error:
        return False, f"認証設定に失敗しました: {oidc_error.strip()}", None

    route_error = apply_route(name, auth_mode=auth_mode, fqdn=fqdn)
    if route_error:
        return (
            False,
            f"SpinApp は作成されましたが、公開ルートの設定に失敗しました: {route_error.strip()}",
            None,
        )

    return True, None, None


@app.route("/apps/js", methods=["POST"])
def create_js_app():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "")
    is_edit = request.form.get("mode") == "edit"
    template_name = "app_edit.html" if is_edit else "app_new.html"
    auth_mode = request.form.get("auth_mode", "none")
    if auth_mode not in ("none", "basic", "oidc"):
        auth_mode = "none"
    auth_username = request.form.get("auth_username", "").strip()
    auth_password = request.form.get("auth_password", "")
    oidc_users = parse_oidc_users(request.form.get("auth_oidc_users", ""))
    fqdn = request.form.get("fqdn", "").strip()

    def render_error(error_list, build_log=None, status=400):
        return render_template(
            template_name,
            errors=error_list,
            build_log=build_log,
            namespace=NAMESPACE,
            active_nav="new",
            edit_name=name,
            is_edit=is_edit,
            form_name=name,
            form_code=code,
            auth_mode=auth_mode,
            auth_username=auth_username,
            oidc_users_text="\n".join(oidc_users),
            fqdns=REGISTERED_FQDNS,
            form_fqdn=fqdn,
        ), status

    errors = []
    if not NAME_RE.match(name):
        errors.append(
            "name は小文字英数字とハイフンのみ、63文字以内で入力してください。"
        )
    elif not is_edit and name in RESERVED_APP_NAMES:
        errors.append(f'"{name}" は予約済みの名前のため使用できません。')
    if not code.strip():
        errors.append("code が空です。")
    if len(code.encode("utf-8")) > MAX_JS_SOURCE_BYTES:
        errors.append(
            f"code が大きすぎます ({MAX_JS_SOURCE_BYTES // 1000}KB まで)。"
        )
    if auth_mode == "basic" and not auth_username:
        errors.append("Basic認証を有効にする場合は username を入力してください。")
    if auth_mode == "oidc" and not oidc_users:
        errors.append("OIDC認証を有効にする場合は許可するユーザー名を1人以上入力してください。")
    if fqdn and fqdn not in REGISTERED_FQDNS:
        errors.append("選択された FQDN は登録されていません。")

    if errors:
        return render_error(errors)

    htpasswd_hash = None
    if auth_mode == "basic":
        htpasswd_hash, auth_hash_error = resolve_auth_password_hash(name, auth_password)
        if auth_hash_error:
            return render_error([auth_hash_error])

    ok, error, build_log = deploy_js_spinapp(
        name, code, auth_mode, auth_username, htpasswd_hash, oidc_users, fqdn or None
    )
    if not ok:
        return render_error([error], build_log=build_log)

    replicate_to_peers(
        "/internal/apps/js",
        {
            "name": name,
            "code": code,
            "auth_mode": auth_mode,
            "auth_username": auth_username,
            "htpasswd_hash": htpasswd_hash,
            "oidc_users": oidc_users,
            "fqdn": fqdn or None,
        },
    )
    return redirect(url_for("index"))


def _apply_js_payload(data):
    """Validate+deploy a "js" app from a plain dict shaped like
    js_export_payload()'s output (same shape replicate_to_peers() sends to
    /internal/apps/js). Shared by internal_apply_js() (peer replication)
    and import_page() (file-based import) so both go through identical
    validation. Returns (ok, error, status) -- status is only meaningful
    when ok is False (400 for a payload/validation problem, 500 for a
    deploy-time failure)."""
    name = data.get("name", "")
    code = data.get("code", "")
    if not is_valid_name(name) or not isinstance(code, str) or not code or name in RESERVED_APP_NAMES:
        return False, "invalid payload", 400
    if len(code.encode("utf-8")) > MAX_JS_SOURCE_BYTES:
        return False, "code too large", 400

    auth_mode = data.get("auth_mode") or ("basic" if data.get("auth_enabled") else "none")
    ok, error, _ = deploy_js_spinapp(
        name,
        code,
        auth_mode,
        data.get("auth_username", ""),
        data.get("htpasswd_hash"),
        data.get("oidc_users") or [],
        data.get("fqdn") or None,
    )
    return ok, error, (200 if ok else 500)


@app.route("/internal/apps/js", methods=["POST"])
@csrf.exempt
def internal_apply_js():
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    ok, error, status = _apply_js_payload(request.get_json(force=True, silent=True) or {})
    return jsonify(ok=ok, error=error), status


@app.route("/proxies/new")
def new_proxy_page():
    from_name = request.args.get("from", "").strip()
    form_backend_origin = ""
    form_rules = []
    form_rewrites = []
    form_ws_path = ""
    form_fqdn = ""
    if from_name and NAME_RE.match(from_name):
        config = get_proxy_config(from_name) or {}
        form_backend_origin = config.get("backend_origin", "")
        form_rules = config.get("rules", [])
        form_rewrites = config.get("rewrites", [])
        form_ws_path = config.get("ws_path", "")
        result = kubectl(["get", "spinapp", from_name, "-n", NAMESPACE, "-o", "json"])
        if result.returncode == 0:
            annotations = json.loads(result.stdout)["metadata"].get("annotations", {}) or {}
            form_fqdn = annotations.get(FQDN_ANNOTATION, "")
    return render_template(
        "proxy_new.html",
        namespace=NAMESPACE,
        active_nav="new_proxy",
        fqdns=REGISTERED_FQDNS,
        form_backend_origin=form_backend_origin,
        form_rules=form_rules,
        form_rewrites=form_rewrites,
        form_ws_path=form_ws_path,
        form_fqdn=form_fqdn,
    )


@app.route("/proxies/<name>/edit")
def edit_proxy(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    config = get_proxy_config(name)
    if config is None:
        apps, error = list_spinapps()
        crons, cron_error = list_cronjobs()
        containers, container_error = list_container_apps()
        return render_template(
            "index.html",
            apps=apps,
            error=error,
            crons=crons,
            cron_error=cron_error,
            containers=containers,
            container_error=container_error,
            errors=["この SpinApp はプロキシ設定を保持していないため編集できません。"],
            namespace=NAMESPACE,
            active_nav="dashboard",
        ), 400
    result = kubectl(["get", "spinapp", name, "-n", NAMESPACE, "-o", "json"])
    annotations = {}
    if result.returncode == 0:
        annotations = json.loads(result.stdout)["metadata"].get("annotations", {}) or {}
    return render_template(
        "proxy_edit.html",
        namespace=NAMESPACE,
        active_nav="new_proxy",
        edit_name=name,
        is_edit=True,
        form_name=name,
        form_backend_origin=config.get("backend_origin", "") + config.get("backend_prefix", ""),
        form_rules=config.get("rules", []),
        form_rewrites=config.get("rewrites", []),
        form_ws_path=config.get("ws_path", ""),
        auth_mode=resolve_auth_mode(annotations),
        auth_username=annotations.get(AUTH_USERNAME_ANNOTATION, ""),
        oidc_users_text=oidc_users_text(annotations),
        fqdns=REGISTERED_FQDNS,
        form_fqdn=annotations.get(FQDN_ANNOTATION, ""),
    )


def deploy_proxy_spinapp(
    name, backend_origin, rules, rewrites, auth_mode, auth_username, htpasswd_hash, oidc_users,
    fqdn, ws_path="", backend_prefix="",
):
    """Render the proxy's generated worker code, build+push it, and apply
    its SpinApp/auth/route. Shared by the user-facing create_proxy and the
    peer-replicated /internal/proxies. Returns (ok, error, build_log)."""
    proxy_config_json = json.dumps(
        {
            "backend_origin": backend_origin,
            "rules": rules,
            "rewrites": rewrites,
            "ws_path": ws_path,
            "backend_prefix": backend_prefix,
        }
    )
    code = render_proxy_js(name, backend_origin, rules, rewrites, backend_prefix)

    log = []
    try:
        image_ref = build_and_push_js(name, code, log)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as build log
        return False, f"プロキシのビルドに失敗しました: {exc}", "\n".join(filter(None, log))

    manifest = {
        "apiVersion": "core.spinkube.dev/v1alpha1",
        "kind": "SpinApp",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "annotations": {
                APP_KIND_ANNOTATION: "proxy",
                PROXY_CONFIG_ANNOTATION: base64.b64encode(
                    proxy_config_json.encode("utf-8")
                ).decode("ascii"),
                **build_auth_annotations(auth_mode, auth_username, oidc_users),
                FQDN_ANNOTATION: fqdn or "",
            },
        },
        "spec": {
            "image": image_ref,
            "executor": EXECUTOR,
            "replicas": 1,
        },
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
    if result.returncode != 0:
        return False, f"デプロイに失敗しました: {result.stderr.strip()}", None

    auth_error = apply_basic_auth_secret(name, auth_mode == "basic", auth_username, htpasswd_hash)
    if auth_error:
        return False, f"認証設定に失敗しました: {auth_error.strip()}", None

    oidc_error = apply_oidc_auth_middleware(name, auth_mode == "oidc")
    if oidc_error:
        return False, f"認証設定に失敗しました: {oidc_error.strip()}", None

    route_error = apply_route(name, auth_mode=auth_mode, fqdn=fqdn)
    if route_error:
        return (
            False,
            f"SpinApp は作成されましたが、公開ルートの設定に失敗しました: {route_error.strip()}",
            None,
        )

    ws_error = apply_ws_bypass_route(
        name, backend_origin, ws_path, auth_mode=auth_mode, fqdn=fqdn, backend_prefix=backend_prefix
    )
    if ws_error:
        return (
            False,
            f"SpinApp は作成されましたが、WebSocket用ルートの設定に失敗しました: {ws_error.strip()}",
            None,
        )

    return True, None, None


@app.route("/proxies", methods=["POST"])
def create_proxy():
    name = request.form.get("name", "").strip()
    is_edit = request.form.get("mode") == "edit"
    template_name = "proxy_edit.html" if is_edit else "proxy_new.html"
    auth_mode = request.form.get("auth_mode", "none")
    if auth_mode not in ("none", "basic", "oidc"):
        auth_mode = "none"
    auth_username = request.form.get("auth_username", "").strip()
    auth_password = request.form.get("auth_password", "")
    oidc_users = parse_oidc_users(request.form.get("auth_oidc_users", ""))
    fqdn = request.form.get("fqdn", "").strip()
    backend_origin = request.form.get("backend_origin", "").strip()
    ws_path = request.form.get("ws_path", "").strip()

    try:
        rules = json.loads(request.form.get("rules", "[]"))
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")
    except (ValueError, TypeError):
        rules = []
        rules_parse_error = ["rules の形式が不正です。"]
    else:
        rules_parse_error = []

    try:
        rewrites = json.loads(request.form.get("rewrites", "[]"))
        if not isinstance(rewrites, list):
            raise ValueError("rewrites must be a list")
    except (ValueError, TypeError):
        rewrites = []
        rewrites_parse_error = ["rewrites の形式が不正です。"]
    else:
        rewrites_parse_error = []

    def render_error(error_list, build_log=None, status=400):
        return render_template(
            template_name,
            errors=error_list,
            build_log=build_log,
            namespace=NAMESPACE,
            active_nav="new_proxy",
            edit_name=name,
            is_edit=is_edit,
            form_name=name,
            form_backend_origin=backend_origin,
            form_rules=rules,
            form_rewrites=rewrites,
            form_ws_path=ws_path,
            auth_mode=auth_mode,
            auth_username=auth_username,
            oidc_users_text="\n".join(oidc_users),
            fqdns=REGISTERED_FQDNS,
            form_fqdn=fqdn,
        ), status

    errors = []
    if not NAME_RE.match(name):
        errors.append(
            "name は小文字英数字とハイフンのみ、63文字以内で入力してください。"
        )
    elif not is_edit and name in RESERVED_APP_NAMES:
        errors.append(f'"{name}" は予約済みの名前のため使用できません。')
    errors.extend(rules_parse_error)
    errors.extend(rewrites_parse_error)
    config_errors, normalized_origin, backend_prefix = validate_proxy_config(
        backend_origin, rules, rewrites, ws_path
    )
    errors.extend(config_errors)
    if auth_mode == "basic" and not auth_username:
        errors.append("Basic認証を有効にする場合は username を入力してください。")
    if auth_mode == "oidc" and not oidc_users:
        errors.append("OIDC認証を有効にする場合は許可するユーザー名を1人以上入力してください。")
    if fqdn and fqdn not in REGISTERED_FQDNS:
        errors.append("選択された FQDN は登録されていません。")

    proxy_config_json = json.dumps(
        {
            "backend_origin": normalized_origin,
            "rules": rules,
            "rewrites": rewrites,
            "ws_path": ws_path,
            "backend_prefix": backend_prefix,
        }
    )
    if len(proxy_config_json.encode("utf-8")) > MAX_PROXY_CONFIG_BYTES:
        errors.append(f"設定が大きすぎます ({MAX_PROXY_CONFIG_BYTES // 1000}KB まで)。")

    if errors:
        return render_error(errors)

    htpasswd_hash = None
    if auth_mode == "basic":
        htpasswd_hash, auth_hash_error = resolve_auth_password_hash(name, auth_password)
        if auth_hash_error:
            return render_error([auth_hash_error])

    ok, error, build_log = deploy_proxy_spinapp(
        name, normalized_origin, rules, rewrites,
        auth_mode, auth_username, htpasswd_hash, oidc_users, fqdn or None,
        ws_path, backend_prefix,
    )
    if not ok:
        return render_error([error], build_log=build_log)

    replicate_to_peers(
        "/internal/proxies",
        {
            "name": name,
            "backend_origin": normalized_origin,
            "rules": rules,
            "rewrites": rewrites,
            "auth_mode": auth_mode,
            "auth_username": auth_username,
            "htpasswd_hash": htpasswd_hash,
            "oidc_users": oidc_users,
            "fqdn": fqdn or None,
            "ws_path": ws_path,
            "backend_prefix": backend_prefix,
        },
    )
    return redirect(url_for("index"))


def _apply_proxy_payload(data):
    """Validate+deploy a "proxy" app from a plain dict shaped like
    proxy_export_payload()'s output (same shape replicate_to_peers() sends
    to /internal/proxies). Shared by internal_apply_proxy() (peer
    replication) and import_page() (file-based import). Returns (ok,
    error, status) -- see _apply_js_payload()'s docstring."""
    name = data.get("name", "")
    rules = data.get("rules") or []
    rewrites = data.get("rewrites") or []
    if (
        not is_valid_name(name)
        or name in RESERVED_APP_NAMES
        or not isinstance(rules, list)
        or not isinstance(rewrites, list)
        or not isinstance(data.get("backend_origin", ""), str)
    ):
        return False, "invalid payload", 400

    ws_path = data.get("ws_path", "") or ""
    if not isinstance(ws_path, str):
        return False, "invalid payload", 400
    # backend_origin here is already the sending node's own normalized_origin
    # (path-free -- see create_proxy()), so its own backend_prefix -- sent as
    # its own payload field -- is authoritative, not whatever this re-parse
    # would derive (nothing, since the path's already gone).
    config_errors, normalized_origin, _ = validate_proxy_config(
        data.get("backend_origin", ""), rules, rewrites, ws_path
    )
    if config_errors:
        return False, "; ".join(config_errors), 400
    # validate_proxy_config() checks rules/rewrites shape but not overall
    # size, same as create_proxy()'s own separate MAX_PROXY_CONFIG_BYTES
    # check after it.
    proxy_config_size = len(json.dumps({
        "backend_origin": normalized_origin, "rules": rules, "rewrites": rewrites,
        "ws_path": ws_path, "backend_prefix": data.get("backend_prefix", ""),
    }).encode("utf-8"))
    if proxy_config_size > MAX_PROXY_CONFIG_BYTES:
        return False, "config too large", 400

    auth_mode = data.get("auth_mode") or ("basic" if data.get("auth_enabled") else "none")
    ok, error, _ = deploy_proxy_spinapp(
        name, normalized_origin, rules, rewrites,
        auth_mode,
        data.get("auth_username", ""),
        data.get("htpasswd_hash"),
        data.get("oidc_users") or [],
        data.get("fqdn") or None,
        ws_path,
        data.get("backend_prefix", ""),
    )
    return ok, error, (200 if ok else 500)


@app.route("/internal/proxies", methods=["POST"])
@csrf.exempt
def internal_apply_proxy():
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    ok, error, status = _apply_proxy_payload(request.get_json(force=True, silent=True) or {})
    return jsonify(ok=ok, error=error), status


@app.route("/sites/new")
def new_site_page():
    from_name = request.args.get("from", "").strip()
    site_files = None
    form_fqdn = ""
    if from_name and NAME_RE.match(from_name):
        site_files = get_site_files(from_name)
        result = kubectl(["get", "spinapp", from_name, "-n", NAMESPACE, "-o", "json"])
        if result.returncode == 0:
            annotations = json.loads(result.stdout)["metadata"].get("annotations", {}) or {}
            form_fqdn = annotations.get(FQDN_ANNOTATION, "")
    return render_template(
        "site_new.html",
        namespace=NAMESPACE,
        active_nav="new_site",
        fqdns=REGISTERED_FQDNS,
        site_files=site_files,
        form_fqdn=form_fqdn,
    )


@app.route("/sites/<name>/edit")
def edit_site(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    files = get_site_files(name)
    if files is None:
        apps, error = list_spinapps()
        crons, cron_error = list_cronjobs()
        containers, container_error = list_container_apps()
        return render_template(
            "index.html",
            apps=apps,
            error=error,
            crons=crons,
            cron_error=cron_error,
            containers=containers,
            container_error=container_error,
            errors=["このアプリはサイトのファイルを保持していないため編集できません。"],
            namespace=NAMESPACE,
            active_nav="dashboard",
        ), 400
    result = kubectl(["get", "spinapp", name, "-n", NAMESPACE, "-o", "json"])
    annotations = {}
    if result.returncode == 0:
        annotations = json.loads(result.stdout)["metadata"].get("annotations", {}) or {}
    return render_template(
        "site_edit.html",
        namespace=NAMESPACE,
        active_nav="new_site",
        edit_name=name,
        is_edit=True,
        form_name=name,
        site_files=files,
        auth_mode=resolve_auth_mode(annotations),
        auth_username=annotations.get(AUTH_USERNAME_ANNOTATION, ""),
        oidc_users_text=oidc_users_text(annotations),
        fqdns=REGISTERED_FQDNS,
        form_fqdn=annotations.get(FQDN_ANNOTATION, ""),
    )


def deploy_site_spinapp(name, files, auth_mode, auth_username, htpasswd_hash, oidc_users, fqdn):
    """Build+push the site and apply its SpinApp/auth/route. Shared by the
    user-facing create_site and the peer-replicated /internal/sites.
    Returns (ok, error)."""
    log = []
    try:
        image_ref = build_and_push_site(name, files, log)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller
        return False, f"サイトの登録に失敗しました: {exc}"

    manifest = {
        "apiVersion": "core.spinkube.dev/v1alpha1",
        "kind": "SpinApp",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "annotations": {
                APP_KIND_ANNOTATION: "site",
                **build_auth_annotations(auth_mode, auth_username, oidc_users),
                FQDN_ANNOTATION: fqdn or "",
            },
        },
        "spec": {
            "image": image_ref,
            "executor": EXECUTOR,
            "replicas": 1,
        },
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
    if result.returncode != 0:
        return False, f"デプロイに失敗しました: {result.stderr.strip()}"

    # Always tear down any previously-applied routes (and the old auth
    # Secret/Middleware) first: editing a site can change whether it has
    # .php files, which switches between apply_route() and
    # apply_site_route() -- the latter creates extra per-directory
    # Middlewares that would otherwise be left orphaned if a later edit
    # drops back to the plain static-only case.
    delete_route(name)

    auth_error = apply_basic_auth_secret(name, auth_mode == "basic", auth_username, htpasswd_hash)
    if auth_error:
        return False, f"認証設定に失敗しました: {auth_error.strip()}"

    oidc_error = apply_oidc_auth_middleware(name, auth_mode == "oidc")
    if oidc_error:
        return False, f"認証設定に失敗しました: {oidc_error.strip()}"

    has_php = any(p.endswith(".php") for p in files)
    route_error = (
        apply_site_route(name, list(files.keys()), auth_mode=auth_mode, fqdn=fqdn)
        if has_php
        else apply_route(name, auth_mode=auth_mode, force_no_cache=True, fqdn=fqdn)
    )
    if route_error:
        return (
            False,
            f"SpinApp は作成されましたが、公開ルートの設定に失敗しました: {route_error.strip()}",
        )

    return True, None


@app.route("/sites", methods=["POST"])
def create_site():
    name = request.form.get("name", "").strip()
    is_edit = request.form.get("mode") == "edit"
    template_name = "site_edit.html" if is_edit else "site_new.html"
    auth_mode = request.form.get("auth_mode", "none")
    if auth_mode not in ("none", "basic", "oidc"):
        auth_mode = "none"
    auth_username = request.form.get("auth_username", "").strip()
    auth_password = request.form.get("auth_password", "")
    oidc_users = parse_oidc_users(request.form.get("auth_oidc_users", ""))
    fqdn = request.form.get("fqdn", "").strip()

    try:
        files = json.loads(request.form.get("files", "{}"))
        if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()
        ):
            raise ValueError("files must be a flat string->string object")
    except (ValueError, TypeError):
        files = {}
        files_error = ["files の形式が不正です。"]
    else:
        files_error = []

    def render_error(error_list, status=400):
        return render_template(
            template_name,
            errors=error_list,
            namespace=NAMESPACE,
            active_nav="new_site",
            edit_name=name,
            is_edit=is_edit,
            form_name=name,
            site_files=files,
            auth_mode=auth_mode,
            auth_username=auth_username,
            oidc_users_text="\n".join(oidc_users),
            fqdns=REGISTERED_FQDNS,
            form_fqdn=fqdn,
        ), status

    errors = []
    if not NAME_RE.match(name):
        errors.append(
            "name は小文字英数字とハイフンのみ、63文字以内で入力してください。"
        )
    elif not is_edit and name in RESERVED_APP_NAMES:
        errors.append(f'"{name}" は予約済みの名前のため使用できません。')
    errors.extend(files_error)
    errors.extend(validate_site_files(files))
    if auth_mode == "basic" and not auth_username:
        errors.append("Basic認証を有効にする場合は username を入力してください。")
    if auth_mode == "oidc" and not oidc_users:
        errors.append("OIDC認証を有効にする場合は許可するユーザー名を1人以上入力してください。")
    if fqdn and fqdn not in REGISTERED_FQDNS:
        errors.append("選択された FQDN は登録されていません。")
    if errors:
        return render_error(errors)

    # Resolve the auth password hash BEFORE delete_route() below, which
    # would otherwise delete the existing <name>-auth Secret first and make
    # "leave password blank to keep it" impossible to honor.
    htpasswd_hash = None
    if auth_mode == "basic":
        htpasswd_hash, auth_hash_error = resolve_auth_password_hash(name, auth_password)
        if auth_hash_error:
            return render_error([auth_hash_error])

    ok, error = deploy_site_spinapp(
        name, files, auth_mode, auth_username, htpasswd_hash, oidc_users, fqdn or None
    )
    if not ok:
        return render_error([error])

    replicate_to_peers(
        "/internal/sites",
        {
            "name": name,
            "files": files,
            "auth_mode": auth_mode,
            "auth_username": auth_username,
            "htpasswd_hash": htpasswd_hash,
            "oidc_users": oidc_users,
            "fqdn": fqdn or None,
        },
    )
    return redirect(url_for("index"))


def _apply_site_payload(data):
    """Validate+deploy a "site" app from a plain dict shaped like
    site_export_payload()'s output (same shape replicate_to_peers() sends
    to /internal/sites). Shared by internal_apply_site() (peer
    replication) and import_page() (file-based import). Returns (ok,
    error, status) -- see _apply_js_payload()'s docstring."""
    name = data.get("name", "")
    files = data.get("files")
    if (
        not is_valid_name(name)
        or name in RESERVED_APP_NAMES
        or not isinstance(files, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    ):
        return False, "invalid payload", 400

    # Unlike the other _apply_*_payload() handlers (_apply_proxy_payload()
    # re-validates via validate_proxy_config(), _apply_cron_payload()
    # re-checks the schedule/mail shape), this one was missing the
    # equivalent of create_site()'s validate_site_files() call -- meaning a
    # file path wasn't checked against SITE_PATH_RE/".." here the way it is
    # on the user-facing route. build_and_push_site() joins each key
    # straight onto SITES_DATA_DIR/<name>/ with no traversal guard of its
    # own, so a peer (or anyone with a leaked/guessed PEER_TOKEN, or an
    # imported file with a crafted path) could otherwise write to an
    # arbitrary path on the host via a "../../..." file key -- this check
    # is the only gate standing in front of that.
    files_errors = validate_site_files(files)
    if files_errors:
        return False, "; ".join(files_errors), 400

    auth_mode = data.get("auth_mode") or ("basic" if data.get("auth_enabled") else "none")
    ok, error = deploy_site_spinapp(
        name,
        files,
        auth_mode,
        data.get("auth_username", ""),
        data.get("htpasswd_hash"),
        data.get("oidc_users") or [],
        data.get("fqdn") or None,
    )
    return ok, error, (200 if ok else 500)


@app.route("/internal/sites", methods=["POST"])
@csrf.exempt
def internal_apply_site():
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    ok, error, status = _apply_site_payload(request.get_json(force=True, silent=True) or {})
    return jsonify(ok=ok, error=error), status


def is_valid_cron_schedule(value):
    """Light structural check only (5 whitespace-separated fields, or an
    "@..." shorthand like @hourly) -- the k8s API server has its own cron
    parser and is the real source of truth; its rejection message (surfaced
    via deploy_cron_job()'s kubectl error) covers anything this misses."""
    value = value.strip()
    if not value:
        return False
    if value.startswith("@"):
        return True
    return len(value.split()) == 5


def list_cronjobs():
    """List every CronJob this console manages (labeled APP_LABEL), for the
    dashboard's "Cron Jobs" table. Status is a best-effort read of the
    CronJob's own status fields -- k8s doesn't track per-run success/failure
    on the CronJob itself beyond lastScheduleTime/lastSuccessfulTime, so
    "failed" here really means "last scheduled run didn't report success",
    which also covers a still-running-longer-than-expected run."""
    result = kubectl(["get", "cronjob", "-n", NAMESPACE, "-l", APP_LABEL, "-o", "json"])
    if result.returncode != 0:
        return [], result.stderr
    data = json.loads(result.stdout)
    crons = []
    for item in data.get("items", []):
        spec = item.get("spec", {})
        status = item.get("status", {})
        annotations = item["metadata"].get("annotations", {}) or {}
        name = item["metadata"]["name"]
        enabled = not spec.get("suspend", False)
        last_schedule = status.get("lastScheduleTime")
        last_successful = status.get("lastSuccessfulTime")
        if not enabled:
            state = "suspended"
        elif status.get("active"):
            state = "running"
        elif not last_schedule:
            state = "pending"
        elif last_successful and last_successful >= last_schedule:
            state = "ok"
        else:
            state = "failed"
        crons.append(
            {
                "name": name,
                "schedule": spec.get("schedule", ""),
                "enabled": enabled,
                "state": state,
                "last_schedule": last_schedule,
                "language": annotations.get(CRON_LANGUAGE_ANNOTATION) or "python",
                "copy_url": f"/crons/new?from={name}",
            }
        )
    return crons, None


def get_cron_language(name):
    """Return "python" or "bash" for a cron job, defaulting to "python" for
    anything predating this annotation (or any unrecognized value)."""
    result = kubectl(
        [
            "get", "cronjob", name, "-n", NAMESPACE, "-o",
            "jsonpath={{.metadata.annotations['{}']}}".format(
                CRON_LANGUAGE_ANNOTATION.replace(".", r"\.")
            ),
        ]
    )
    if result.returncode == 0 and result.stdout in CRON_LANGUAGES:
        return result.stdout
    return "python"


def get_cron_source(name):
    """Return the user's own code (not the generated file that actually
    gets deployed -- see render_cron_job_source()), stored as an annotation
    on the CronJob itself the same way JS_SOURCE_ANNOTATION works for JS
    apps."""
    result = kubectl(
        [
            "get", "cronjob", name, "-n", NAMESPACE, "-o",
            "jsonpath={{.metadata.annotations['{}']}}".format(
                CRON_CODE_ANNOTATION.replace(".", r"\.")
            ),
        ]
    )
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return base64.b64decode(result.stdout).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def get_cron_mail(name):
    """Return the stored {"from","to","subject","body"} for a cron job's
    "メール送信" tab, or None if that tab was left blank (a plain cron job)."""
    result = kubectl(
        [
            "get", "cronjob", name, "-n", NAMESPACE, "-o",
            "jsonpath={{.metadata.annotations['{}']}}".format(
                CRON_MAIL_ANNOTATION.replace(".", r"\.")
            ),
        ]
    )
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return json.loads(base64.b64decode(result.stdout).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _bash_quote(value):
    """POSIX/bash-safe single-quoted string literal -- wraps in '...' and
    escapes any embedded single quote as '\\'' (close-quote, literal quote,
    reopen-quote). Safe for arbitrary text (newlines, $, `, ", \\, ...)."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def render_cron_job_source(user_code, mail, name, language):
    """Combine the optional "メール送信" helper prelude with the user's own
    code into the actual file that gets deployed (job.py or job.sh -- see
    deploy_cron_job()). `mail` is None for a plain cron job, or {"from","to"}
    when that tab was filled in -- see CRON_MAIL_ANNOTATION. SMTP
    server/login are global (see SMTP_HOST et al), not part of `mail`.
    Subject/body are deliberately *not* injected (unlike from/to) -- the
    whole point is the user builds those from their own variables, so
    their code just needs to set SUBJECT/BODY itself and then call
    send_mail(TO_ADDR, SUBJECT, BODY) (Python) or
    send_mail "$TO_ADDR" "$SUBJECT" "$BODY" (bash -- shells out to a
    python3 heredoc under the hood, since no available base image ships
    bash+curl together; see the comment above CRON_PYTHON_IMAGE).

    `name` (this cron job's name) only feeds the Message-ID header, as
    "<time-based-id>@<name>.<display host>" -- unique enough without a
    real domain, and traceable back to which cron job sent it. It also
    becomes the default User-Agent for bash's download() helper (below).
    """
    msgid_domain = f"{name}.{EXTERNAL_HOST}"

    if language == "bash":
        # bash jobs always get download() -- like send_mail(), it shells
        # out to a python3 heredoc (urllib.request) since curl isn't in the
        # image (see the comment above CRON_PYTHON_IMAGE). Unlike
        # send_mail(), it's unconditional: it doesn't depend on SMTP config
        # or the メール送信 tab, so every bash job gets it whether or not
        # mail is used.
        header = f'''#!/usr/bin/env bash
set -euo pipefail

DEFAULT_USER_AGENT={_bash_quote(f"SpinDeck-cron/{name}")}

download() {{
  local url="$1" dest="$2" user_agent="${{3:-$DEFAULT_USER_AGENT}}"
  DOWNLOAD_URL="$url" DOWNLOAD_DEST="$dest" DOWNLOAD_UA="$user_agent" python3 <<'PYEOF'
import os
import urllib.request

req = urllib.request.Request(
    os.environ["DOWNLOAD_URL"], headers={{"User-Agent": os.environ["DOWNLOAD_UA"]}}
)
with urllib.request.urlopen(req, timeout=30) as resp, open(os.environ["DOWNLOAD_DEST"], "wb") as f:
    f.write(resp.read())
PYEOF
}}

'''
        if not mail:
            return header + user_code
        prelude = f'''SMTP_HOST={_bash_quote(SMTP_HOST)}
SMTP_PORT={_bash_quote(SMTP_PORT)}
SMTP_USER={_bash_quote(SMTP_USER)}
SMTP_PASSWORD={_bash_quote(SMTP_PASSWORD)}
FROM_ADDR={_bash_quote(mail.get("from", ""))}
TO_ADDR={_bash_quote(mail.get("to", ""))}
MSGID_DOMAIN={_bash_quote(msgid_domain)}

send_mail() {{
  local to_addr="$1" subject="$2" body="$3"
  SEND_MAIL_TO="$to_addr" SEND_MAIL_SUBJECT="$subject" SEND_MAIL_BODY="$body" SMTP_HOST="$SMTP_HOST" SMTP_PORT="$SMTP_PORT" SMTP_USER="$SMTP_USER" SMTP_PASSWORD="$SMTP_PASSWORD" FROM_ADDR="$FROM_ADDR" MSGID_DOMAIN="$MSGID_DOMAIN" python3 <<'PYEOF'
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

msg = MIMEText(os.environ["SEND_MAIL_BODY"], "plain", "utf-8")
msg["Subject"] = os.environ["SEND_MAIL_SUBJECT"]
msg["From"] = os.environ["FROM_ADDR"]
msg["To"] = os.environ["SEND_MAIL_TO"]
msg["Date"] = formatdate(localtime=True)
msg["Message-ID"] = make_msgid(domain=os.environ["MSGID_DOMAIN"])
with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=30) as smtp:
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
    smtp.send_message(msg)
PYEOF
}}

'''
        return header + prelude + user_code

    # python
    if not mail:
        return user_code
    prelude = f'''import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

SMTP_HOST = {SMTP_HOST!r}
SMTP_PORT = {SMTP_PORT!r}
SMTP_USER = {SMTP_USER!r}
SMTP_PASSWORD = {SMTP_PASSWORD!r}
FROM_ADDR = {mail.get("from", "")!r}
TO_ADDR = {mail.get("to", "")!r}
MSGID_DOMAIN = {msgid_domain!r}


def send_mail(to_addr, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDR
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=MSGID_DOMAIN)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


'''
    return prelude + user_code


def list_cron_runs(name):
    """Most recent Jobs this CronJob has spawned (for the edit page's "実行
    履歴" tab), newest first."""
    result = kubectl(["get", "job", "-n", NAMESPACE, "-l", f"{APP_LABEL}={name}", "-o", "json"])
    if result.returncode != 0:
        return []
    try:
        items = json.loads(result.stdout).get("items", [])
    except ValueError:
        return []
    items.sort(key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)
    runs = []
    for item in items[:MAX_CRON_RUNS_SHOWN]:
        status = item.get("status", {})
        if status.get("succeeded"):
            state = "succeeded"
        elif status.get("failed"):
            state = "failed"
        elif status.get("active"):
            state = "running"
        else:
            state = "pending"
        runs.append(
            {
                "name": item["metadata"]["name"],
                "started": status.get("startTime", ""),
                "completed": status.get("completionTime", ""),
                "state": state,
            }
        )
    return runs


def deploy_cron_job(name, code, schedule, enabled, mail=None, language="python"):
    """Apply the ConfigMap (generated job.py/job.sh -- see
    render_cron_job_source()) + CronJob for a cron job. Shared by the
    user-facing create_cron and the peer-replicated /internal/crons, so
    both end up with identical state. Returns (ok, error)."""
    if language not in CRON_LANGUAGES:
        language = "python"
    filename = "job.py" if language == "python" else "job.sh"
    command = ["python3", f"/app/{filename}"] if language == "python" else ["bash", f"/app/{filename}"]

    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{name}{CRON_CONFIGMAP_SUFFIX}",
            "namespace": NAMESPACE,
            "labels": {APP_LABEL: name},
        },
        "data": {filename: render_cron_job_source(code, mail, name, language)},
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(configmap))
    if result.returncode != 0:
        return False, f"コードの保存に失敗しました: {result.stderr.strip()}"

    manifest = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {APP_LABEL: name},
            "annotations": {
                CRON_CODE_ANNOTATION: base64.b64encode(code.encode("utf-8")).decode("ascii"),
                CRON_LANGUAGE_ANNOTATION: language,
                CRON_MAIL_ANNOTATION: (
                    base64.b64encode(json.dumps(mail).encode("utf-8")).decode("ascii")
                    if mail
                    else ""
                ),
            },
        },
        "spec": {
            "schedule": schedule,
            "suspend": not enabled,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": CRON_ACTIVE_DEADLINE_SECONDS,
                    "template": {
                        "metadata": {"labels": {APP_LABEL: name}},
                        "spec": {
                            "restartPolicy": "Never",
                            # This job has no legitimate reason to talk to
                            # the k8s API -- don't hand it a token that
                            # could otherwise be read straight off disk (a
                            # real container, unlike the Wasm SpinApps,
                            # actually has unrestricted filesystem access).
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": "job",
                                    "image": CRON_PYTHON_IMAGE,
                                    "command": command,
                                    "volumeMounts": [
                                        {"name": "src", "mountPath": "/app", "readOnly": True}
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "64Mi"},
                                        "limits": {"cpu": "500m", "memory": "256Mi"},
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "runAsNonRoot": True,
                                        "runAsUser": 1000,
                                    },
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "src",
                                    "configMap": {"name": f"{name}{CRON_CONFIGMAP_SUFFIX}"},
                                }
                            ],
                        },
                    },
                },
            },
        },
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
    if result.returncode != 0:
        return False, f"デプロイに失敗しました: {result.stderr.strip()}"
    return True, None


def delete_cron_locally(name):
    """The actual teardown, shared by the user-facing delete route and the
    peer-replicated /internal/crons/<name>/delete. Deleting the CronJob
    cascades (via ownerReferences + the default GC policy) to every Job and
    Pod it created; the source ConfigMap isn't owned by anything so it needs
    an explicit delete."""
    kubectl(["delete", "cronjob", name, "-n", NAMESPACE, "--ignore-not-found"])
    kubectl(
        ["delete", "configmap", f"{name}{CRON_CONFIGMAP_SUFFIX}", "-n", NAMESPACE, "--ignore-not-found"]
    )


def _mail_form_fields(mail):
    """Flatten a get_cron_mail() dict (or None) into the template kwargs
    every cron-editor route needs to prefill the "メール送信" tab."""
    mail = mail or {}
    return {
        "form_mail_from": mail.get("from", ""),
        "form_mail_to": mail.get("to", ""),
    }


def list_container_apps():
    """List every "container" Deployment this console manages, for the
    dashboard's "Containers" table. Scoped to APP_LABEL, which -- unlike
    every other kind's primary object -- genuinely distinguishes our own
    Deployments from the Spin operator's auto-generated ones: the operator
    never applies this label, so it can't accidentally show up here."""
    result = kubectl(["get", "deployment", "-n", NAMESPACE, "-l", APP_LABEL, "-o", "json"])
    if result.returncode != 0:
        return [], result.stderr
    data = json.loads(result.stdout)
    apps = []
    for item in data.get("items", []):
        annotations = item["metadata"].get("annotations", {}) or {}
        spec = item.get("spec", {})
        status = item.get("status", {})
        name = item["metadata"]["name"]
        containers = spec.get("template", {}).get("spec", {}).get("containers") or [{}]
        container = containers[0]
        ports = container.get("ports") or [{}]
        fqdn = annotations.get(FQDN_ANNOTATION, "")
        tls_mode = annotations.get(CONTAINER_TLS_MODE_ANNOTATION, "terminate")
        # passthrough is always 443/TLS regardless of the sitewide toggle
        # (see apply_container_route_tcp()'s docstring); terminate mode
        # follows it exactly like every other kind.
        scheme = "https" if tls_mode == "passthrough" else TRAEFIK_SCHEME
        apps.append(
            {
                "name": name,
                "image": container.get("image", ""),
                "port": ports[0].get("containerPort", ""),
                "replicas": spec.get("replicas", 1),
                "ready_replicas": status.get("readyReplicas", 0),
                "fqdn": fqdn,
                "tls_mode": tls_mode,
                "auth_mode": resolve_auth_mode(annotations),
                # None (not just "") when there's no fqdn -- a pure
                # compute/batch container with no external exposure at
                # all (see deploy_container_app()'s docstring) -- so
                # index.html can tell "no URL" apart from a blank string.
                "url": f"{scheme}://{fqdn}/" if fqdn else None,
                "copy_url": f"/containers/new?from={name}",
            }
        )
    return apps, None


def find_fqdn_owner(fqdn, exclude_name=None):
    """The name of an existing app (any kind) already using `fqdn`, or None.
    js/site/proxy happily share one FQDN via distinct PathPrefixes, so
    they're free to reuse REGISTERED_FQDNS entries -- but a "container" app
    takes over the bare Host with no PathPrefix at all, so sharing an FQDN
    with *any* other app (of any kind) breaks routing for one or both.
    `exclude_name` skips an app's own current FQDN when re-checking on
    edit. Cron apps have no FQDN, so list_cronjobs() isn't consulted."""
    apps, _ = list_spinapps()
    containers, _ = list_container_apps()
    for other in apps + containers:
        if other["name"] == exclude_name:
            continue
        if other.get("fqdn") and other["fqdn"] == fqdn:
            return other["name"]
    return None


def find_extra_port_owner(port, protocol, exclude_name=None):
    """The name of an existing "container" app already using
    {port, protocol} as one of its "extra ports" (see
    MAX_CONTAINER_EXTRA_PORTS's docstring), or None. Checked because,
    unlike Traefik-routed ports (Host/PathPrefix-distinguished, safe to
    share across apps), two apps' raw LoadBalancer Services can't both
    actually claim the same node port on this single-node cluster --
    confirmed live that k3s's klipper-lb DaemonSet Pod for the *second*
    one just sits Pending forever (only one hostPort/<port> to hand out),
    with no error surfaced anywhere in Kubernetes or this console: the
    second app's own `kubectl apply` for its Service still succeeds
    (a LoadBalancer Service is valid to create even if it can never
    actually get an external IP), so this has to be caught here at
    validation time instead, the same way find_fqdn_owner() catches an
    FQDN collision before it ever reaches kubectl. `exclude_name` skips
    an app's own current extra ports when re-checking on edit.

    One kubectl call for every "<name>-raw" Service (filtered by name
    suffix client-side, not a field-selector -- arbitrary spec fields
    like `type` aren't reliably selectable server-side), not a per-app
    get_container_config() call each -- same "single query for
    everything" pattern as list_container_apps()/find_fqdn_owner()."""
    result = kubectl(["get", "service", "-n", NAMESPACE, "-l", APP_LABEL, "-o", "json"])
    if result.returncode != 0:
        return None
    suffix = CONTAINER_EXTRA_PORTS_SERVICE_SUFFIX
    for item in json.loads(result.stdout).get("items", []):
        svc_name = item["metadata"]["name"]
        if not svc_name.endswith(suffix):
            continue
        owner = svc_name[: -len(suffix)]
        if owner == exclude_name:
            continue
        for p in item.get("spec", {}).get("ports", []):
            if p.get("port") == port and p.get("protocol", "TCP").upper() == protocol.upper():
                return owner
    return None


def is_host_port_in_use(port, protocol):
    """Best-effort check for whether *this host* (not just another
    "container" app -- see find_extra_port_owner() above) already has
    `port` bound, by trying to bind it here and seeing whether that
    fails. Complements CONTAINER_RESERVED_EXTRA_PORTS's static list
    (which only names ports known ahead of time): catches a currently-
    running service on some other port nobody thought to hardcode --
    though NOT a port a squatter grabs pre-emptively before its real
    owner's next restart, which is exactly why the static list still
    matters even with this check in place (see its own docstring).
    Never raises: any OSError (already bound, permission denied on a
    privileged port, ...) is treated the same as "in use" -- refusing a
    borderline case is the safe direction here, not letting it through.
    Checks both address families -- extra_ports Services are dual-stack
    (see _container_extra_ports_service_manifest()'s "PreferDualStack"),
    and a service bound only on "::" (IPv6-any, which on most Linux
    defaults does NOT also cover IPv4 the way it might on other OSes)
    would otherwise be invisible to an IPv4-only check."""
    sock_type = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
    for family, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            with socket.socket(family, sock_type) as s:
                if family == socket.AF_INET6:
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                s.bind((addr, port))
        except OSError:
            return True
    return False


def get_container_config(name):
    """The editor/copy-source config for a "container" app, read back from
    its Deployment (source of truth, same as every other kind). Returns
    None if it doesn't exist -- or exists but isn't a "container" app, which
    matters here specifically (unlike list_container_apps()'s label-scoped
    query) because this looks a Deployment up by name alone, and a same-named
    Deployment could in principle belong to something else entirely (the
    Spin operator's auto-generated one for a same-named SpinApp, if the
    cross-kind collision check in create_container() were ever bypassed)."""
    result = kubectl(["get", "deployment", name, "-n", NAMESPACE, "-o", "json"])
    if result.returncode != 0:
        return None
    dep = json.loads(result.stdout)
    annotations = dep["metadata"].get("annotations", {}) or {}
    if annotations.get(APP_KIND_ANNOTATION) != "container":
        return None
    spec = dep.get("spec", {})
    containers = spec.get("template", {}).get("spec", {}).get("containers") or [{}]
    container = containers[0]
    ports = container.get("ports") or [{}]
    limits = container.get("resources", {}).get("limits", {})

    def env_to_text(c):
        return "\n".join(f"{e['name']}={e.get('value', '')}" for e in c.get("env") or [])

    sidecars = []
    for sc in containers[1:]:
        sc_limits = sc.get("resources", {}).get("limits", {})
        # The "data" volume's own mount, wherever this sidecar chose to put
        # it -- NOT assumed to be CONTAINER_DATA_MOUNT_PATH, since a
        # sidecar's whole reason to override this is that its image
        # expects its own path (see _parse_sidecars_json()'s docstring).
        data_mount = next(
            (vm for vm in sc.get("volumeMounts") or [] if vm.get("name") == "data"), None
        )
        sidecars.append({
            "name": sc.get("name", ""),
            "image": sc.get("image", ""),
            "command": shlex.join(sc["command"]) if sc.get("command") else "",
            "cpu_limit": sc_limits.get("cpu", ""),
            "mem_limit": sc_limits.get("memory", ""),
            "env": env_to_text(sc),
            "storage": data_mount is not None,
            "mount_path": (data_mount or {}).get("mountPath", "") or CONTAINER_DATA_MOUNT_PATH,
        })

    primary_data_mount = next(
        (vm for vm in container.get("volumeMounts") or [] if vm.get("name") == "data"), None
    )
    extra_ports = []
    raw_svc_result = kubectl(
        ["get", "service", f"{name}{CONTAINER_EXTRA_PORTS_SERVICE_SUFFIX}", "-n", NAMESPACE, "-o", "json"]
    )
    if raw_svc_result.returncode == 0:
        raw_svc = json.loads(raw_svc_result.stdout)
        extra_ports = [
            {
                "port": p["port"],
                "target_port": p.get("targetPort", p["port"]),
                "protocol": p.get("protocol", "TCP").lower(),
            }
            for p in raw_svc.get("spec", {}).get("ports", [])
        ]
    return {
        "image": container.get("image", ""),
        "port": ports[0].get("containerPort", ""),
        "cpu_limit": limits.get("cpu", ""),
        "mem_limit": limits.get("memory", ""),
        "replicas": spec.get("replicas", 1),
        "fqdn": annotations.get(FQDN_ANNOTATION, ""),
        "tls_mode": annotations.get(CONTAINER_TLS_MODE_ANNOTATION, "terminate"),
        "upgrade_insecure": annotations.get(CONTAINER_UPGRADE_INSECURE_ANNOTATION) == "true",
        "auth_mode": resolve_auth_mode(annotations),
        "auth_username": annotations.get(AUTH_USERNAME_ANNOTATION, ""),
        "oidc_users_text": oidc_users_text(annotations),
        "command": shlex.join(container["command"]) if container.get("command") else "",
        "env": env_to_text(container),
        "sidecars": sidecars,
        "storage": primary_data_mount is not None,
        "mount_path": (primary_data_mount or {}).get("mountPath", "") or CONTAINER_DATA_MOUNT_PATH,
        "extra_ports": extra_ports,
        # container.get("name") rather than a hardcoded "app" -- form mode
        # always names the primary container "app", but YAML mode lets it
        # be renamed, and this list is what exec/logs validate a requested
        # container against (see _container_exec_bridge()/container_logs()).
        "container_names": [container.get("name", "app")] + [sc["name"] for sc in sidecars],
    }


def list_container_pods(name):
    """Pod name + phase/ready for every Pod belonging to a "container" app
    -- feeds the "コンソール" tab's replica picker (multiple pods when
    replicas > 1). Returns [] on a kubectl error rather than (list, error)
    like the other list_*() helpers, since this is only ever consumed by
    the /containers/<name>/pods JSON endpoint, which just wants a plain
    array back either way."""
    result = kubectl(
        ["get", "pod", "-n", NAMESPACE, "-l", f"{APP_LABEL}={name}", "-o", "json"]
    )
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    pods = []
    for item in data.get("items", []):
        statuses = item.get("status", {}).get("containerStatuses") or [{}]
        pods.append(
            {
                "name": item["metadata"]["name"],
                "phase": item.get("status", {}).get("phase", "Unknown"),
                "ready": bool(statuses[0].get("ready")),
            }
        )
    return pods


K8S_OBJECT_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,251}[a-z0-9])?$")


def pod_belongs_to_container_app(pod_name, name):
    """Confirms `pod_name` is actually one of `name`'s own Pods (via
    APP_LABEL) before letting the exec WebSocket route touch it -- without
    this, a crafted `pod` query param could exec into an unrelated Pod
    elsewhere in the namespace. K8S_OBJECT_NAME_RE (not NAME_RE) since a
    Deployment-generated Pod name is "<name>-<hash>-<hash>", longer than
    the 63 chars NAME_RE caps app names at."""
    if not K8S_OBJECT_NAME_RE.match(pod_name):
        return False
    result = kubectl(
        [
            "get", "pod", pod_name, "-n", NAMESPACE,
            "-o",
            "jsonpath={{.metadata.labels['{}']}}".format(APP_LABEL.replace(".", r"\.")),
        ]
    )
    return result.returncode == 0 and result.stdout.strip() == name


@app.route("/containers/<name>/pods")
def container_pods(name):
    if not NAME_RE.match(name):
        return jsonify(error="invalid name"), 400
    config = get_container_config(name)
    if config is None:
        return jsonify(error="not found"), 404
    # container_names feeds the コンソール/ログ tabs' "接続先のコンテナ"
    # dropdown -- "app" plus every sidecar, in the same order they appear
    # in the Deployment (see get_container_config()).
    return jsonify(pods=list_container_pods(name), container_names=config["container_names"])


# The single sentinel byte a real terminal keystroke stream will never
# produce on its own -- lets the client multiplex "resize" control messages
# over the same WebSocket as raw terminal input without a JSON envelope
# (and without a separate control channel) for every keystroke.
EXEC_RESIZE_PREFIX = "\x01"
# bash if present, sh otherwise -- portable across full distros and
# minimal ones (alpine/busybox) that don't ship bash at all. `command -v`
# (POSIX) rather than `which` (not guaranteed present on minimal images).
CONTAINER_EXEC_SHELL_CMD = "command -v bash >/dev/null 2>&1 && exec bash || exec sh"


@sock.route("/containers/<name>/exec")
def container_exec(ws, name):
    # Thin wrapper: flask-sock's route decorator returns None rather than
    # the original function (unlike @app.route), which makes the decorated
    # name useless to call directly (e.g. from a test script) -- keeping
    # the real logic in a plain, undecorated function avoids that.
    _container_exec_bridge(ws, name)


def _container_exec_bridge(ws, name):
    """Interactive browser terminal into a "container" app's Pod -- kubectl
    exec -it, bridged over a local pty so the remote shell gets real
    terminal semantics (prompt, ctrl-C, tab completion, resize). Reachable
    only for an authenticated console session: check_auth() is a normal
    @app.before_request hook, so it already ran (the WebSocket handshake
    is itself a plain HTTP GET) before this view function is ever entered
    -- same gate as every other route, nothing new to bypass here.

    Not a privilege escalation: whoever can reach this already has full
    control over what image/command this container runs (they created or
    can edit the app), so exec-ing into it doesn't grant them anything
    they couldn't already achieve by putting a reverse shell in their own
    image.
    """
    config = get_container_config(name)
    if not NAME_RE.match(name) or config is None:
        ws.close()
        return
    pod = (request.args.get("pod") or "").strip()
    if not pod or not pod_belongs_to_container_app(pod, name):
        ws.close()
        return
    # Which of the Pod's containers to attach to -- defaults to the
    # primary one (container_names[0], NOT a hardcoded "app": YAML mode
    # can rename it -- see get_container_config()'s own comment on that
    # field). Validated against this app's own known container list
    # rather than trusted blindly, same reasoning as
    # pod_belongs_to_container_app() above: a crafted `container` query
    # param shouldn't be able to name something that isn't actually part
    # of this app's own Pod spec.
    container = (request.args.get("container") or "").strip() or config["container_names"][0]
    if container not in config["container_names"]:
        ws.close()
        return

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [
            "kubectl", "exec", "-it", pod, "-n", NAMESPACE, "-c", container, "--",
            "sh", "-c", CONTAINER_EXEC_SHELL_CMD,
        ],
        env=dict(os.environ, KUBECONFIG=KUBECONFIG),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        # New session/process group so a resize can SIGWINCH exactly this
        # kubectl process (and nothing outside it) via os.killpg().
        preexec_fn=os.setsid,
        close_fds=True,
    )
    # The child has its own dup'd copy from stdin=/stdout=/stderr= above --
    # without closing the parent's, the parent would keep the slave end
    # open even after the child exits, and os.read(master_fd) would then
    # block forever instead of seeing EOF.
    os.close(slave_fd)

    stop = threading.Event()

    def pump_pty_to_ws():
        try:
            while not stop.is_set():
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    ws.send(chunk.decode("utf-8", errors="replace"))
                except Exception:
                    break
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:
                pass

    reader = threading.Thread(target=pump_pty_to_ws, daemon=True)
    reader.start()

    try:
        while not stop.is_set():
            try:
                data = ws.receive()
            except Exception:
                break
            if data is None:
                break
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            if data.startswith(EXEC_RESIZE_PREFIX):
                try:
                    cols_str, rows_str = data[1:].split(",", 1)
                    fcntl.ioctl(
                        master_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", int(rows_str), int(cols_str), 0, 0),
                    )
                    os.killpg(proc.pid, signal.SIGWINCH)
                except (ValueError, OSError, ProcessLookupError):
                    pass
            else:
                try:
                    os.write(master_fd, data.encode("utf-8", errors="replace"))
                except OSError:
                    break
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            os.close(master_fd)
        except OSError:
            pass
        reader.join(timeout=2)


def _parse_env_text(text):
    """Parses a "KEY=VALUE per line" textarea (blank lines and #-comments
    ignored) into Kubernetes' own env-var shape ([{"name":, "value":}, ...]).
    Returns (env_list, error_list) -- never raises, so a malformed line
    surfaces as a normal inline form error instead of a 500."""
    env = []
    errors = []
    for i, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"環境変数の{i}行目 ({line!r}) は KEY=VALUE の形式で入力してください。")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not ENV_VAR_NAME_RE.match(key):
            errors.append(
                f"環境変数の{i}行目: キー \"{key}\" は英字またはアンダースコアで始まり、"
                "英数字とアンダースコアのみ使用できます。"
            )
            continue
        env.append({"name": key, "value": value})
    return env, errors


CONTAINER_MOUNT_PATH_RE = re.compile(r"^/[^\0]*[^/\0]$|^/$")


def _parse_sidecars_json(raw):
    """Parses the hidden "sidecars_json" field (built client-side from the
    "+ サイドカーコンテナを追加" UI -- see _container_editor_assets.html)
    into a validated list of sidecar container dicts, or (None, error) on
    the first problem found. Each entry: {"name":, "image":, "command":
    (list or None), "cpu_limit":, "mem_limit":, "env": [...], "storage":
    bool, "mount_path":}. Names must be unique (both among themselves and
    against "app", the primary container's fixed name) since Kubernetes
    rejects a Pod spec with duplicate container names outright.

    mount_path matters because CONTAINER_DATA_MOUNT_PATH ("/data") is only
    where the *primary* container's image expects to find it -- a sidecar
    like a database image almost always hardcodes its own path instead
    (MySQL: /var/lib/mysql, PostgreSQL: /var/lib/postgresql/data, ...).
    Defaulting every sidecar to /data regardless of what its own image
    actually reads from would silently produce a *non-persistent*
    "persistent storage" checkbox -- confirmed live: MySQL happily
    initializes and runs against its own container-local /var/lib/mysql
    with the shared hostPath volume mounted at /data sitting right next
    to it, completely unused, so all data vanishes on the next Pod
    recreation. Letting the mount path itself be overridden per sidecar is
    the actual fix, not just a nice-to-have."""
    if not raw or not raw.strip():
        return [], None
    try:
        items = json.loads(raw)
    except ValueError:
        return None, "サイドカーコンテナの内容が壊れています。ページを再読み込みしてやり直してください。"
    if not isinstance(items, list):
        return None, "サイドカーコンテナの内容が不正です。"
    if len(items) > MAX_CONTAINER_SIDECARS:
        return None, f"サイドカーコンテナは{MAX_CONTAINER_SIDECARS}個までです。"

    sidecars = []
    seen_names = {"app"}
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return None, f"サイドカーコンテナ{i}: 内容が不正です。"
        name = (item.get("name") or "").strip()
        image = (item.get("image") or "").strip()
        if not NAME_RE.match(name):
            return None, f"サイドカーコンテナ{i}: 名前は小文字英数字とハイフンのみ、63文字以内で入力してください。"
        if name in seen_names:
            return None, f"サイドカーコンテナ{i}: 名前 \"{name}\" が重複しています（app、または他のサイドカーと同じ名前は使えません）。"
        seen_names.add(name)
        if not image:
            return None, f"サイドカーコンテナ \"{name}\": image を入力してください。"
        cpu_limit = (item.get("cpu_limit") or "").strip() or "200m"
        mem_limit = (item.get("mem_limit") or "").strip() or "256Mi"
        if not CPU_QUANTITY_RE.match(cpu_limit):
            return None, f"サイドカーコンテナ \"{name}\": CPU上限は \"200m\" や \"1\" のような形式で入力してください。"
        if not MEM_QUANTITY_RE.match(mem_limit):
            return None, f"サイドカーコンテナ \"{name}\": メモリ上限は \"256Mi\" や \"1Gi\" のような形式で入力してください。"
        command_raw = (item.get("command") or "").strip()
        command = None
        if command_raw:
            try:
                command = shlex.split(command_raw)
            except ValueError as exc:
                return None, f"サイドカーコンテナ \"{name}\": 起動コマンドの形式が正しくありません: {exc}"
        env, env_errors = _parse_env_text(item.get("env") or "")
        if env_errors:
            return None, f"サイドカーコンテナ \"{name}\": {env_errors[0]}"
        storage = bool(item.get("storage"))
        mount_path = (item.get("mount_path") or "").strip() or CONTAINER_DATA_MOUNT_PATH
        if storage and not CONTAINER_MOUNT_PATH_RE.match(mount_path):
            return None, f"サイドカーコンテナ \"{name}\": マウント先パスは \"/\" から始まる絶対パスで入力してください。"
        sidecars.append({
            "name": name,
            "image": image,
            "command": command,
            "cpu_limit": cpu_limit,
            "mem_limit": mem_limit,
            "env": env,
            "storage": storage,
            "mount_path": mount_path,
        })
    return sidecars, None


def _validate_sidecars_structure(sidecars):
    """Structural validation for the *already-parsed* "sidecars" list
    arriving via /internal/containers (peer replication sends the same
    shape _parse_sidecars_json() produces -- command as a list, env as
    [{"name":,"value":}, ...] -- not the raw form-submitted JSON text
    _parse_sidecars_json() itself expects, so that function can't be
    reused as-is here).

    Exists because _container_deployment_manifest()'s sidecar-building
    loop indexes each dict directly (sc["name"], sc["image"], ...) rather
    than using .get() -- deliberately, so a *bug* in that code shows up
    immediately as a crash during development instead of silently
    building a half-populated manifest. That means a malformed peer
    payload (or a bug in a future change to replicate_to_peers()'s own
    payload construction) would otherwise reach that indexing as an
    unhandled KeyError -- a 500, not a clean "invalid payload" 400. Peers
    are already required to present a valid X-SpinDeck-Peer-Token before
    reaching this far, so this is defense in depth against a
    misbehaving/compromised peer or a future bug, not the primary
    trust boundary. Returns an error string, or None if `sidecars` is
    well-formed (including None/empty, both valid "no sidecars")."""
    if sidecars is None:
        return None
    if not isinstance(sidecars, list):
        return "invalid sidecars payload"
    if len(sidecars) > MAX_CONTAINER_SIDECARS:
        return "invalid sidecars payload"
    seen_names = {"app"}
    for item in sidecars:
        if not isinstance(item, dict):
            return "invalid sidecars payload"
        name = item.get("name")
        image = item.get("image")
        command = item.get("command")
        env = item.get("env")
        cpu_limit = item.get("cpu_limit")
        mem_limit = item.get("mem_limit")
        # isinstance(..., str) checked before every re.match() below --
        # re.match() raises TypeError (not a clean False) on a non-string
        # argument, e.g. a peer payload sending cpu_limit as a JSON number
        # instead of a string, which would otherwise be an unhandled 500
        # here rather than the intended "invalid payload" 400.
        if (
            not isinstance(name, str) or not NAME_RE.match(name) or name in seen_names
            or not isinstance(image, str) or not image
            or not isinstance(cpu_limit, str) or not CPU_QUANTITY_RE.match(cpu_limit)
            or not isinstance(mem_limit, str) or not MEM_QUANTITY_RE.match(mem_limit)
            or (command is not None and not isinstance(command, list))
            or (env is not None and not isinstance(env, list))
            or not isinstance(item.get("storage"), bool)
            or not isinstance(item.get("mount_path"), str)
        ):
            return "invalid sidecars payload"
        seen_names.add(name)
    return None


def _parse_extra_ports_json(raw):
    """Parses the hidden "extra_ports_json" field (built client-side --
    see _container_editor_assets.html's addExtraPort()/
    serializeExtraPorts()) into a validated list of
    {"port","target_port","protocol"} dicts, or (None, error) on the
    first problem found. See MAX_CONTAINER_EXTRA_PORTS's docstring for
    what these are and why they're validated this narrowly."""
    if not raw or not raw.strip():
        return [], None
    try:
        items = json.loads(raw)
    except ValueError:
        return None, "追加ポートの内容が壊れています。ページを再読み込みしてやり直してください。"
    error = _validate_extra_ports_structure(items)
    if error:
        return None, error
    return [
        {"port": p["port"], "target_port": p.get("target_port", p["port"]), "protocol": p["protocol"]}
        for p in items
    ], None


def _validate_extra_ports_structure(extra_ports):
    """Structural validation for the *already-parsed* "extra_ports" list,
    shared by _parse_extra_ports_json() (form submission) and
    _apply_container_payload() (peer replication / import -- same
    "trust but verify a peer/import payload's shape" reasoning as
    _validate_sidecars_structure()). Returns an error string, or None if
    well-formed (including None/empty, both valid "no extra ports").

    "port" is the externally-reachable one (see CONTAINER_RESERVED_
    EXTRA_PORTS/dedup below -- both keyed on it, since that's what
    actually contends for a slot on the node). "target_port" is just
    where the container itself listens -- defaults to "port" when
    omitted (see _parse_extra_ports_json()), so a plain "same port in
    and out" entry (the common case) never needs to specify it, and no
    reserved/dedup checks apply to it: two entries can freely share a
    target_port (e.g. forwarding both 500/udp and 4500/udp externally to
    the same internal listener), and it has no meaning to Traefik at
    all."""
    if extra_ports is None:
        return None
    if not isinstance(extra_ports, list):
        return "追加ポートの内容が不正です。"
    if len(extra_ports) > MAX_CONTAINER_EXTRA_PORTS:
        return f"追加ポートは{MAX_CONTAINER_EXTRA_PORTS}個までです。"
    seen = set()
    for item in extra_ports:
        if not isinstance(item, dict):
            return "追加ポートの内容が不正です。"
        port = item.get("port")
        target_port = item.get("target_port", port)
        protocol = item.get("protocol")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            return "追加ポートの番号は1〜65535の数値で指定してください。"
        if not isinstance(target_port, int) or isinstance(target_port, bool) or not (1 <= target_port <= 65535):
            return "転送先ポートの番号は1〜65535の数値で指定してください。"
        if port in CONTAINER_RESERVED_EXTRA_PORTS:
            return f"ポート{port}はこのシステム自身が使用するため追加ポートには指定できません。"
        if protocol not in CONTAINER_EXTRA_PORT_PROTOCOLS:
            return "追加ポートのプロトコルはtcp/udpのいずれかにしてください。"
        key = (port, protocol)
        if key in seen:
            return f"追加ポート {protocol}/{port} が重複しています。"
        seen.add(key)
    return None


def _sidecars_for_yaml_template(display_sidecars):
    """Converts get_container_config()'s "sidecars" (command/env as plain
    display strings, for pre-filling the form) into the shape
    _container_deployment_manifest() actually wants (command as a list,
    env as [{"name":,"value":}, ...]) -- used only when building the
    "新規作成" (copy-from-existing) page's starting YAML template, so a
    copied app's sidecars show up there too instead of silently vanishing."""
    sidecars = []
    for sc in display_sidecars:
        sidecars.append({
            "name": sc["name"],
            "image": sc["image"],
            "command": shlex.split(sc["command"]) if sc.get("command") else None,
            "cpu_limit": sc.get("cpu_limit") or "200m",
            "mem_limit": sc.get("mem_limit") or "256Mi",
            "env": _parse_env_text(sc.get("env", ""))[0],
            "storage": bool(sc.get("storage")),
            "mount_path": sc.get("mount_path") or CONTAINER_DATA_MOUNT_PATH,
        })
    return sidecars


def _container_spec_for(name, image, cpu_limit, mem_limit, command=None, env=None,
                         port=None, volume_mount=None):
    """Builds one entry of a Pod's `containers: [...]` list -- shared by the
    primary container and every sidecar, since they need identical
    resource/securityContext handling and differ only in which of the
    optional bits (ports, command, env, a volume mount) apply."""
    spec = {
        "name": name,
        "image": image,
        # memory: requests == limits, same reasoning as ever (a
        # memory overcommit's failure mode is an OOM-kill -- there's no
        # graceful degradation, so this cluster still never books more
        # memory than it can actually give every app at once).
        #
        # cpu: request pinned to a small fixed CONTAINER_CPU_REQUEST
        # ("10m"), NOT omitted -- explicit choice to allow CPU
        # overcommit, since unlike memory, CPU contention just means
        # cgroup throttling (a container gets less CPU time than it
        # asked for, temporarily) rather than anything getting killed.
        # Confirmed live that simply *omitting* requests.cpu doesn't
        # achieve this: the API server auto-defaults a missing cpu
        # request to match the limit whenever a limit is set (verified
        # by inspecting the live Pod spec, not just what got applied --
        # requests.cpu came back equal to cpu_limit despite never being
        # sent), so a genuinely low, explicit value is required instead.
        # This also sidesteps the Recreate-strategy deadlock class this
        # very node hit (see deploy_container_app()'s comment) by
        # construction: the scheduler only ever needs to find
        # CONTAINER_CPU_REQUEST of headroom for a restart, not this
        # app's full cpu_limit, so a busy node is far less likely to run
        # out of schedulable CPU. QoS class becomes Burstable (not
        # Guaranteed) as a result -- a container can still never *use*
        # more than cpu_limit, only reserve less than it while idle.
        "resources": {
            "requests": {"cpu": CONTAINER_CPU_REQUEST, "memory": mem_limit},
            "limits": {"cpu": cpu_limit, "memory": mem_limit},
        },
        # Deliberately NOT runAsNonRoot/runAsUser, and
        # deliberately NOT dropping capabilities (unlike
        # deploy_cron_job()): this kind's whole point is
        # running an arbitrary public image, and many
        # (nginx, postgres, mysql, ...) legitimately need
        # root -- and the capabilities that come with it
        # (CAP_CHOWN, CAP_SETUID/GID, ...) -- at startup.
        # Confirmed empirically: capabilities.drop=[ALL]
        # breaks even official nginx outright (its
        # entrypoint's chown() on /var/cache/nginx fails
        # with EPERM despite running as uid 0, since
        # dropped capabilities strip root's effective
        # privileges too). privileged/allowPrivilege
        # Escalation:false stay -- no legitimate public
        # image needs those for normal startup.
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "privileged": False,
        },
    }
    if volume_mount:
        spec["volumeMounts"] = [volume_mount]
    # Omitted (not an empty list) when there's no port -- a "pure compute"
    # container that never listens on anything shouldn't declare a
    # containerPort it doesn't have (see deploy_container_app()'s handling
    # of port=None, which also skips creating a Service entirely).
    if port is not None:
        spec["ports"] = [{"containerPort": port, "protocol": "TCP"}]
    # Many public images (bare python/node/alpine, ...) have a default
    # ENTRYPOINT/CMD that just prints something and exits immediately --
    # useless for a long-running Deployment (Kubernetes just restarts the
    # crash-looping Pod forever). Overriding `command` (Kubernetes' name
    # for what Docker calls ENTRYPOINT) lets the image be used anyway,
    # e.g. "sleep infinity" to keep it alive for コンソール(exec)-only use.
    if command:
        spec["command"] = command
    if env:
        spec["env"] = env
    return spec


def _container_deployment_manifest(
    name, image, port, cpu_limit, mem_limit, fqdn, tls_mode, replicas,
    command=None, env=None, sidecars=None, storage=True, mount_path=None,
):
    """`sidecars` (see _parse_sidecars_json()) lets a WordPress+MySQL-style
    stack run as one Pod instead of two independent apps -- deliberately
    same-Pod, not two separate "container" apps talking over a Service:
    the egress NetworkPolicy every Pod in this namespace gets (see
    docs/TECHNICAL_NOTES.md "Egress hardening") blocks Pod-to-Pod/Service
    traffic within the cluster on purpose, so two separate apps could
    never actually reach each other this way. Containers in the same Pod
    talk over localhost instead, which never leaves the Pod's network
    namespace and so isn't subject to that policy at all. The primary
    container ("app") is the only one that gets the containerPort/Service
    routing; `storage`/`mount_path` control whether and where it also
    gets the shared volume, same choice a sidecar has (default: mounted
    at /data, matching every single-container app that predates this
    field existing at all) -- unlike a sidecar's, its mount is never
    subPath-namespaced, since that would silently relocate every existing
    app's data out from under it. A sidecar only gets /data at all if it
    opted in (its own "storage" flag), and always under a subPath named
    after itself so it can't collide with the primary container's own
    files there.
    """
    primary_volume_mount = None
    if storage:
        primary_volume_mount = {
            "name": "data", "mountPath": mount_path or CONTAINER_DATA_MOUNT_PATH,
        }
    container_specs = [
        _container_spec_for(
            "app", image, cpu_limit, mem_limit, command=command, env=env,
            port=port, volume_mount=primary_volume_mount,
        )
    ]
    for sc in (sidecars or []):
        volume_mount = None
        if sc.get("storage"):
            volume_mount = {
                "name": "data",
                "mountPath": sc.get("mount_path") or CONTAINER_DATA_MOUNT_PATH,
                "subPath": sc["name"],
            }
        container_specs.append(_container_spec_for(
            sc["name"], sc["image"], sc["cpu_limit"], sc["mem_limit"],
            command=sc.get("command"), env=sc.get("env"), volume_mount=volume_mount,
        ))
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {APP_LABEL: name},
            "annotations": {
                APP_KIND_ANNOTATION: "container",
                FQDN_ANNOTATION: fqdn,
                CONTAINER_TLS_MODE_ANNOTATION: tls_mode,
            },
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {APP_LABEL: name}},
            "template": {
                "metadata": {"labels": {APP_LABEL: name}},
                "spec": {
                    # No legitimate reason for a user-supplied image to talk
                    # to the k8s API -- same reasoning as deploy_cron_job().
                    "automountServiceAccountToken": False,
                    "containers": container_specs,
                    # Every replica mounts this *same* host directory --
                    # always fine to schedule (single-node cluster, so
                    # every replica lands on the one node this path lives
                    # on anyway), but whether concurrent access is *safe*
                    # is entirely up to the image: fine for something that
                    # only reads /data or does its own locking, unsafe for
                    # anything that assumes exclusive access (see the
                    # replicas field's help text). A sidecar with its own
                    # DB-like state (e.g. MySQL) should stay at replicas=1
                    # for the same reason a single-container DB image
                    # would -- multiple replicas means multiple independent
                    # instances, not one shared one.
                    "volumes": [
                        {
                            "name": "data",
                            "hostPath": {
                                "path": str(CONTAINER_DATA_BASE_DIR / name),
                                "type": "DirectoryOrCreate",
                            },
                        }
                    ],
                },
            },
        },
    }


def _container_service_manifest(name, port):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {APP_LABEL: name}},
        "spec": {
            "selector": {APP_LABEL: name},
            "ports": [{"port": port, "targetPort": port, "protocol": "TCP"}],
        },
    }


CONTAINER_EXTRA_PORTS_SERVICE_SUFFIX = "-raw"


def _container_extra_ports_service_manifest(name, extra_ports):
    """A second, separate Service (<name>-raw) for a "container" app's
    "extra ports" (see MAX_CONTAINER_EXTRA_PORTS's docstring) -- type
    LoadBalancer, the same mechanism Traefik's own Service uses to get a
    real externally-reachable port on this node via k3s's built-in
    klipper-lb (see TECHNICAL_NOTES.md's HTTPS section), so each listed
    port becomes directly reachable with no Traefik/FQDN/TLS involved at
    all. Deliberately a *different* Service from _container_service_
    manifest()'s (which stays ClusterIP, targeted only by Traefik's own
    IngressRoute/IngressRouteTCP) -- an app with no extra ports configured
    never gets a LoadBalancer Service at all.

    ipFamilyPolicy: PreferDualStack matches Traefik's own Service (see
    that Helm chart's default) -- without it, a Service defaults to
    SingleStack (this cluster's primary family, IPv4), so on a node
    actually installed with SPINDECK_DUAL_STACK this would otherwise
    listen on IPv4 only despite the cluster supporting IPv6 too.
    "Prefer" (not "Require") degrades gracefully to IPv4-only on any
    node that *isn't* dual-stack -- confirmed live against this specific
    node (single-stack, no dual-stack) that a Service requesting this
    still comes up fine, just IPv4-only, the same way Traefik's own
    already does here."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{name}{CONTAINER_EXTRA_PORTS_SERVICE_SUFFIX}",
            "namespace": NAMESPACE,
            "labels": {APP_LABEL: name},
        },
        "spec": {
            "type": "LoadBalancer",
            "ipFamilyPolicy": "PreferDualStack",
            "selector": {APP_LABEL: name},
            "ports": [
                {
                    "name": f"{p['protocol']}-{p['port']}",
                    "port": p["port"],
                    "targetPort": p.get("target_port", p["port"]),
                    "protocol": p["protocol"].upper(),
                }
                for p in extra_ports
            ],
        },
    }


# Server-generated bookkeeping a user editing a "container" app's raw YAML
# never needs to see or edit -- present only in what `kubectl get -o json`
# returns, never meaningful input to `kubectl apply`.
_K8S_NOISE_METADATA_KEYS = (
    "resourceVersion", "uid", "creationTimestamp", "generation",
    "selfLink", "managedFields",
)


def strip_k8s_managed_fields(obj):
    """Drop server-managed fields from a `kubectl get -o json` object before
    showing it to a user as editable YAML (see
    container_deployment_service_yaml()) -- purely cosmetic, doesn't affect
    what kubectl apply would accept either way, just keeps the editor free
    of noise nobody should be hand-editing."""
    obj = dict(obj)
    obj.pop("status", None)
    metadata = dict(obj.get("metadata") or {})
    for key in _K8S_NOISE_METADATA_KEYS:
        metadata.pop(key, None)
    annotations = dict(metadata.get("annotations") or {})
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if annotations:
        metadata["annotations"] = annotations
    else:
        metadata.pop("annotations", None)
    obj["metadata"] = metadata
    return obj


def container_deployment_service_yaml(name):
    """The live Deployment + Service for a "container" app, cleaned up via
    strip_k8s_managed_fields() and rendered as a "---"-separated YAML
    document pair -- what the コンソール edit page's "YAML" sub-tab starts
    from. None if the Deployment doesn't exist."""
    dep_result = kubectl(["get", "deployment", name, "-n", NAMESPACE, "-o", "json"])
    if dep_result.returncode != 0:
        return None
    docs = [strip_k8s_managed_fields(json.loads(dep_result.stdout))]
    svc_result = kubectl(["get", "service", name, "-n", NAMESPACE, "-o", "json"])
    if svc_result.returncode == 0:
        docs.append(strip_k8s_managed_fields(json.loads(svc_result.stdout)))
    return yaml.safe_dump_all(docs, sort_keys=False)


def default_container_yaml_template(name, image, port, cpu_limit, mem_limit, replicas,
                                     command=None, env=None, sidecars=None,
                                     storage=True, mount_path=None):
    """The starting point for the "新規作成" page's "YAML" sub-tab -- the
    exact same Deployment/Service a form-mode submission would produce,
    just rendered as editable YAML instead of applied directly. Reuses the
    real manifest builders (not a hand-maintained duplicate) so the
    template can never drift from what form mode actually generates.
    Always shows a port in the template itself (even if the real form
    field is blank) since it's a much more useful starting example than a
    portless one -- the user is free to delete it."""
    docs = [
        _container_deployment_manifest(
            name or "my-container", image or "nginx:latest", port or 8080,
            cpu_limit or "500m", mem_limit or "512Mi", "", "terminate", replicas or 1,
            command=command, env=env, sidecars=sidecars, storage=storage, mount_path=mount_path,
        ),
        _container_service_manifest(name or "my-container", port or 8080),
    ]
    return yaml.safe_dump_all(docs, sort_keys=False)


def validate_container_security_policy(deployment_manifest, name=None, service_manifest=None):
    """Checks a "container" app's about-to-be-applied Deployment (and,
    where given, Service) against the policy enforced regardless of how
    the manifest was produced (form or raw YAML, see
    parse_container_yaml()): no privileged containers, no
    privilege-escalation, no added Linux capabilities, no sharing the
    host's network/PID/IPC namespaces, no hostPath volume other than
    the one the console itself manages (CONTAINER_DATA_BASE_DIR/<name>) --
    otherwise YAML mode could mount e.g. /var/run/docker.sock, /etc, or /
    straight through -- and no Service `type` other than ClusterIP (the
    default when unset). Form mode's own _container_service_manifest()
    never sets `type` at all, so this only ever actually rejects something
    in YAML mode: `NodePort`/`LoadBalancer` would open the container's
    port directly on every node's host interfaces via the same iptables
    DNAT mechanism Traefik's own Service uses (see
    sync_traefik_web_exposure()'s docstring) -- completely bypassing this
    app's own tls_mode/auth_mode choice *and* any host firewall, since
    that path never goes through the INPUT chain a tool like ufw governs.
    These checks exist specifically so that an authenticated-but-
    compromised console session (stolen cookie, OIDC misconfig, ...)
    can't use YAML mode to escalate from "control this one Deployment" to
    "control the host" or "expose this port outside the access-control
    model entirely" -- everything else about the Pod spec (securityContext
    otherwise, env, multiple containers, command, ...) and the Service
    (ports, selector, ...) is still left entirely to the user in YAML
    mode. Returns a list of Japanese violation messages (empty means OK)."""
    violations = []
    pod_spec = deployment_manifest.get("spec", {}).get("template", {}).get("spec", {}) or {}
    containers = pod_spec.get("containers", []) or []
    for container in containers:
        cname = container.get("name", "?")
        security_context = container.get("securityContext") or {}
        if security_context.get("privileged"):
            violations.append(f'コンテナ "{cname}": privileged は許可されていません。')
        if security_context.get("allowPrivilegeEscalation"):
            violations.append(f'コンテナ "{cname}": allowPrivilegeEscalation は許可されていません。')
        added_caps = (security_context.get("capabilities") or {}).get("add") or []
        if added_caps:
            violations.append(
                f'コンテナ "{cname}": capabilities.add ({", ".join(added_caps)}) は許可されていません。'
            )
    for ns_field, label in (("hostNetwork", "hostNetwork"), ("hostPID", "hostPID"), ("hostIPC", "hostIPC")):
        if pod_spec.get(ns_field):
            violations.append(f"{label}: true は許可されていません(ホストの名前空間を共有できてしまいます)。")
    allowed_hostpath = str(CONTAINER_DATA_BASE_DIR / name) if name else None
    for volume in pod_spec.get("volumes", []) or []:
        host_path = (volume.get("hostPath") or {}).get("path")
        if host_path is not None and host_path != allowed_hostpath:
            violations.append(
                f'volume "{volume.get("name", "?")}": hostPath ({host_path}) は許可されていません '
                f"(永続ストレージ用に自動管理される1つのディレクトリ以外のhostPathは使用できません)。"
            )
    if service_manifest is not None:
        service_type = (service_manifest.get("spec", {}) or {}).get("type")
        if service_type not in (None, "ClusterIP"):
            violations.append(
                f"Service type ({service_type}) は許可されていません "
                f"(ClusterIP以外はTraefikを経由せずポートを直接ホストへ公開してしまいます)。"
            )
    return violations


def parse_container_yaml(name, yaml_text):
    """Parses a "---"-separated YAML submission for a "container" app's
    Deployment (+ optional Service). Requires exactly one Deployment, and
    at most one Service -- a pure compute/batch container that never
    listens on anything has no need for one at all (see
    deploy_container_app()'s port=None handling) -- but rejects a
    copy-pasted extra Service. metadata.name/namespace on both are forced
    to `name`/NAMESPACE regardless of what the YAML says -- not a freedom
    restriction, a structural necessity: every other function that
    manages this app (get_container_config(), delete_container_locally(),
    the exec route's pod lookup, ...) addresses it by this exact name via
    `kubectl get/delete deployment <name>`, so a mismatched name would
    silently orphan the app from the console's own bookkeeping. Returns
    (deployment_dict, service_dict_or_None, None) or
    (None, None, error_string)."""
    try:
        docs = [d for d in yaml.safe_load_all(yaml_text) if d]
    except yaml.YAMLError as exc:
        return None, None, f"YAMLの解析に失敗しました: {exc}"

    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    services = [d for d in docs if d.get("kind") == "Service"]
    if len(deployments) != 1:
        return None, None, f"Deployment はちょうど1つ含めてください（{len(deployments)}件見つかりました）。"
    if len(services) > 1:
        return None, None, f"Service は1つまでにしてください（{len(services)}件見つかりました）。"

    deployment = deployments[0]
    service = services[0] if services else None
    for doc in (deployment, service):
        if doc is None:
            continue
        doc.setdefault("metadata", {})
        doc["metadata"]["name"] = name
        doc["metadata"]["namespace"] = NAMESPACE
    return deployment, service, None


def apply_container_route(name, fqdn, port, auth_mode="none", upgrade_insecure=False):
    """"terminate" mode: a plain HTTP IngressRoute, using
    route_entrypoints()/route_tls_block() -- same helpers every other kind
    uses -- so this follows the sitewide TRAEFIK_SCHEME toggle (/config's
    "HTTPS" section) exactly like js/site/proxy do: entryPoint "web" (80,
    no TLS) when it's "http", "websecure" (443, cluster cert) when it's
    "https". Host-only match, no PathPrefix and no stripPrefix Middleware
    -- this app owns its entire FQDN, unlike every other kind which shares
    EXTERNAL_HOST under /<name>. Removes any leftover passthrough
    IngressRouteTCP first, so switching tls_mode on edit never leaves both
    routes present at once.

    auth_mode chains the same per-app Basic/OIDC Middleware every other
    kind uses (_auth_middlewares() -- the <name>-auth Secret/Middleware or
    <name>-oidc-auth Middleware must already exist, applied by
    apply_basic_auth_secret()/apply_oidc_auth_middleware() before this is
    called, same ordering deploy_js_spinapp() already uses). Only
    meaningful here in "terminate" mode -- deploy_container_app() is what
    actually enforces auth_mode="none" whenever tls_mode="passthrough",
    since a passthrough connection never reaches Traefik's HTTP/Middleware
    layer at all for this to chain onto. upgrade_insecure works the same
    way, chaining in the <name>-upgrade-insecure Middleware (must already
    exist via apply_upgrade_insecure_middleware()) -- auth middleware
    runs first so an unauthenticated visitor never even reaches the app
    far enough to receive (or need) the rewritten-URL response."""
    kubectl(["delete", "ingressroutetcp", f"{name}-tcp", "-n", NAMESPACE, "--ignore-not-found"])
    middlewares = _auth_middlewares(name, auth_mode)
    if upgrade_insecure:
        middlewares = middlewares + [{"name": f"{name}-upgrade-insecure"}]
    manifest = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {APP_LABEL: name}},
        "spec": {
            "entryPoints": route_entrypoints(),
            "routes": [
                {
                    "kind": "Rule",
                    "match": f"Host(`{fqdn}`)",
                    "services": [{"name": name, "port": port}],
                    "middlewares": middlewares,
                }
            ],
            **route_tls_block(),
        },
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
    return None if result.returncode == 0 else result.stderr


def apply_container_route_tcp(name, fqdn, port):
    """"passthrough" mode: an IngressRouteTCP with tls.passthrough=true,
    matched by SNI alone (HostSNI) -- Traefik forwards the raw TLS bytes
    straight to the container's own port without ever decrypting them, so
    the container must terminate TLS itself. Always entryPoint "websecure"
    (443) hardcoded, deliberately NOT route_entrypoints() -- unlike
    apply_container_route()'s terminate mode, this never touches the
    cluster's shared cert/TLSStore at all (the container brings its own),
    so it's independent of the sitewide TRAEFIK_SCHEME toggle; 443 is
    always listening regardless of that setting (see route_entrypoints()'s
    docstring). Confirmed empirically (see the implementation plan) that
    this coexists cleanly with ordinary HTTP IngressRoutes on the same
    entryPoint: Traefik's TCP router layer inspects the ClientHello SNI
    first, and only a connection whose SNI matches this route's HostSNI is
    ever diverted here -- every other SNI (including no SNI at all) falls
    through to Traefik's normal HTTP/TLS-termination path untouched.
    Removes any leftover terminate-mode IngressRoute first, mirroring
    apply_container_route()."""
    kubectl(["delete", "ingressroute", name, "-n", NAMESPACE, "--ignore-not-found"])
    manifest = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRouteTCP",
        "metadata": {"name": f"{name}-tcp", "namespace": NAMESPACE, "labels": {APP_LABEL: name}},
        "spec": {
            "entryPoints": ["websecure"],
            "routes": [
                {
                    "match": f"HostSNI(`{fqdn}`)",
                    "services": [{"name": name, "port": port}],
                }
            ],
            "tls": {"passthrough": True},
        },
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
    return None if result.returncode == 0 else result.stderr


CONTAINER_REGISTRY_SECRET_NAME = "spinapp-ui-container-registry-auth"


def sync_container_registry_secret():
    """Create/update (or remove, if CONTAINER_REGISTRIES is now empty) the
    single shared imagePullSecrets Secret every "container" app's
    Deployment references (see deploy_container_app()) -- one
    dockerconfigjson Secret containing every registered registry's
    credentials, keyed by host, rather than one Secret per app or per
    registry. Docker's own credential-lookup-by-host behavior means a
    Deployment pulling a public Docker Hub/GHCR image is unaffected by
    referencing this Secret even when it contains unrelated hosts'
    credentials -- it only ever actually gets used for an image whose
    registry host has a matching entry.

    Called from config_page()'s add_container_registry/
    remove_container_registry actions. Existing "container" apps don't
    get retroactively re-applied when this changes (same "takes effect on
    next save" tradeoff as most other /config settings) -- only new
    Pods/reschedules of already-running apps would actually need to
    re-pull anyway. Returns an error string, or None on success (including
    the "now empty, deleted" case)."""
    kubectl(["delete", "secret", CONTAINER_REGISTRY_SECRET_NAME, "-n", NAMESPACE, "--ignore-not-found"])
    if not CONTAINER_REGISTRIES:
        return None
    auths = {}
    for reg in CONTAINER_REGISTRIES:
        host = reg.get("host", "")
        username = reg.get("username", "")
        password = reg.get("password", "")
        auths[host] = {
            "username": username,
            "password": password,
            "auth": base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii"),
        }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "kubernetes.io/dockerconfigjson",
        "metadata": {"name": CONTAINER_REGISTRY_SECRET_NAME, "namespace": NAMESPACE},
        "stringData": {".dockerconfigjson": json.dumps({"auths": auths})},
    }
    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(secret))
    return None if result.returncode == 0 else result.stderr


def deploy_container_app(
    name, image, port, cpu_limit, mem_limit, fqdn, tls_mode, replicas,
    yaml_manifest=None, auth_mode="none", auth_username="", htpasswd_hash=None, oidc_users=None,
    command=None, env=None, sidecars=None, storage=True, mount_path=None,
    upgrade_insecure=False, extra_ports=None,
):
    """Apply the Deployment + Service for a "container" app (persistent
    data is a hostPath volume baked directly into the Deployment manifest
    -- see _container_deployment_manifest() -- not a separate object),
    then the per-app auth Secret/Middleware (if any), then whichever one
    of apply_container_route()/apply_container_route_tcp() matches
    tls_mode (each of those also tears down the other route kind) --
    unless fqdn is blank, meaning this app isn't externally exposed at
    all (a pure compute/batch container, reachable only via the コンソール
    tab's kubectl exec), in which case both route kinds are torn down and
    neither is (re)created. The Service already load-balances across every
    ready replica with no extra work -- it's a plain label-selector
    Service, same as any other multi-replica Deployment -- and (as long as
    `port` is given) is created regardless of fqdn, since exec doesn't
    need it but a later edit adding an fqdn should have it ready already.
    If `port` is None, there's nothing for a Service to target at all (a
    pure compute/batch container with no listening port whatsoever, not
    just no external exposure) -- no Service is created, and any existing
    one from a prior edit is deleted. Shared by the user-facing
    create_container and the peer-replicated /internal/containers, so both
    end up with identical state.

    If `yaml_manifest` is given (the コンソール edit/new page's "YAML"
    sub-tab), it -- not image/cpu_limit/mem_limit/replicas/env/sidecars --
    is the source of truth for the Deployment/Service: parsed via
    parse_container_yaml() (which also forces metadata.name/namespace),
    letting the user fully control the Pod spec (multiple containers, env,
    extra volumes, command, ...). Either way, validate_container_security_
    policy() runs against whatever Deployment is about to be applied --
    the one policy line that's enforced regardless of which path produced
    the manifest (see that function's docstring).

    auth_mode/auth_username/htpasswd_hash/oidc_users are, like fqdn/
    tls_mode/port, always dedicated fields -- never part of the YAML --
    and their annotations are force-applied onto `deployment` below
    regardless of source, so they never drift out of sync with the actual
    routing/auth objects this function applies from them. Basic/OIDC auth
    only has anywhere to attach in "terminate" mode (a passthrough
    connection's HTTP payload, including any Middleware chain, is never
    seen by Traefik at all) -- rejected outright otherwise, since silently
    ignoring a configured-but-inactive auth setting could leave someone
    believing a passthrough app is protected when it isn't.

    extra_ports (a list of {"port","protocol"} dicts, see
    MAX_CONTAINER_EXTRA_PORTS's docstring) is, like fqdn/tls_mode/port,
    always a dedicated field -- it's applied to a completely separate
    LoadBalancer Service (see _container_extra_ports_service_manifest())
    from `service` above, torn down when empty, regardless of whether
    this call came from form or YAML mode.

    Returns (ok, error)."""
    if auth_mode != "none" and tls_mode == "passthrough":
        return False, "パススルーモードでは認証を設定できません。終端モードを選ぶか、認証を無効にしてください。"
    if upgrade_insecure and tls_mode == "passthrough":
        return False, "パススルーモードでは混在コンテンツの自動修正を設定できません。終端モードを選ぶか、無効にしてください。"

    if yaml_manifest is not None:
        deployment, service, parse_error = parse_container_yaml(name, yaml_manifest)
        if parse_error:
            return False, parse_error
    else:
        deployment = _container_deployment_manifest(
            name, image, port, cpu_limit, mem_limit, fqdn, tls_mode, replicas,
            command=command, env=env, sidecars=sidecars, storage=storage, mount_path=mount_path,
        )
        # A "pure compute" container with no port has nothing for a
        # Service to usefully target -- skip creating one at all (see the
        # unconditional delete below for the edit-time "had a port,
        # cleared it" transition).
        service = _container_service_manifest(name, port) if port is not None else None

    deployment.setdefault("metadata", {}).setdefault("annotations", {})
    deployment["metadata"]["annotations"].update({
        APP_KIND_ANNOTATION: "container",
        FQDN_ANNOTATION: fqdn,
        CONTAINER_TLS_MODE_ANNOTATION: tls_mode,
        CONTAINER_UPGRADE_INSECURE_ANNOTATION: "true" if upgrade_insecure else "false",
        **build_auth_annotations(auth_mode, auth_username, oidc_users or []),
    })
    # Forced unconditionally, same as name/namespace in parse_container_yaml()
    # -- not a freedom restriction, a structural necessity: no legitimate
    # user-supplied image needs to talk to the k8s API, so a YAML submission
    # that sets this true (directly, or indirectly via serviceAccountName)
    # should never actually get a token mounted regardless of what it asks
    # for. Also closes off the one way YAML mode could otherwise hand an
    # already-authenticated-but-compromised console session a path to
    # broader cluster access than "control this one Deployment".
    pod_spec = deployment.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    pod_spec["automountServiceAccountToken"] = False
    # Forced the same way regardless of form/YAML origin, like every other
    # field in this block -- CONTAINER_REGISTRIES is a console-wide /config
    # setting (see sync_container_registry_secret()'s docstring), not
    # something a per-app YAML submission should be able to opt out of
    # (harmless either way: a Deployment referencing this Secret still
    # pulls public images anonymously exactly as before, Docker only
    # consults it for a host that actually has a matching entry) or
    # silently keep stale if an admin later removes every registry.
    if CONTAINER_REGISTRIES:
        pod_spec["imagePullSecrets"] = [{"name": CONTAINER_REGISTRY_SECRET_NAME}]
    else:
        pod_spec.pop("imagePullSecrets", None)
    # Forced Recreate (kill old Pod(s) first, then create new), overriding
    # Kubernetes' own default RollingUpdate strategy -- confirmed live
    # this was a real deadlock, not a theoretical one: RollingUpdate's
    # default maxSurge/maxUnavailable (25%, which for replicas=1 rounds
    # to "always keep the old Pod up until the new one is Ready, and
    # allow one extra Pod during the transition") needs roughly *double*
    # this app's CPU/memory requests to be free on the node for the
    # brief overlap -- and every "container" app already runs Guaranteed
    # QoS (requests == limits, see MAX_CONTAINER_REPLICAS's docstring:
    # "avoiding overcommit matters more than letting a container burst
    # above its own request") specifically to avoid ever needing spare
    # node capacity like that. RollingUpdate's temporary 2x-capacity
    # requirement directly contradicted that already-stated design goal;
    # a resource-constrained node (the common case here -- this is a
    # single-node cluster, not a pool with slack to burst into) can
    # easily have enough room for the app itself but not for a second
    # temporary copy of it, so the new Pod sits FailedScheduling
    # ("Insufficient cpu"/"Insufficient memory") forever -- and since
    # RollingUpdate never kills the old Pod until the new one is Ready,
    # this is a genuine deadlock, not just a slow rollout: it doesn't
    # resolve on its own, and needs an admin to manually free capacity
    # (e.g. `kubectl delete pod` on the old one) to break it. Recreate
    # avoids this entirely by never running two copies at once, at the
    # cost of a guaranteed brief gap in availability during every
    # deploy/restart -- an explicit, deliberate tradeoff (not a
    # regression) once the always-restart-on-Update behavior below made
    # hitting this deadlock far more frequent (every Update click, not
    # just ones that happened to also change the Pod template).
    deployment.setdefault("spec", {})["strategy"] = {"type": "Recreate"}
    # Forces a real rolling restart on every deploy, regardless of whether
    # anything else in the Pod template actually changed -- `kubectl
    # apply`ing a byte-identical Deployment spec is a no-op (no new Pod at
    # all), which is surprising for a "container" app specifically: unlike
    # js/site/proxy (built fresh from source into a content-addressed
    # image on every save, so the image reference itself always changes),
    # a "container" app's image is a user-supplied, often mutable tag
    # (":latest", or any other tag someone re-pushed new content under)
    # -- re-pushing the same tag with fixed content, then clicking "Update
    # & Redeploy" with no other field changed, would otherwise silently
    # keep the old, broken Pod running forever with no error anywhere.
    # Same "kubectl rollout restart" trick that command itself uses
    # (a timestamp in the Pod template's own annotations, which changes
    # the template hash and so always triggers a rolling update). A
    # standalone restart-with-no-other-change button existed briefly but
    # was removed -- see TECHNICAL_NOTES.md's "Update" section -- since
    # its lightweight kubectl-patch-only implementation didn't rebuild
    # the rest of the Deployment spec (confirmed live: a Pod restarted
    # that way kept stale resources.requests.cpu after the CPU-overcommit
    # change below shipped, until a real "Update & Redeploy" ran).
    # "Update & Redeploy" always restarting anyway covers the same need.
    deployment.setdefault("spec", {}).setdefault("template", {}).setdefault("metadata", {}).setdefault(
        "annotations", {}
    )["kubectl.kubernetes.io/restartedAt"] = datetime.now(timezone.utc).isoformat()

    violations = validate_container_security_policy(deployment, name=name, service_manifest=service)
    if violations:
        return False, " / ".join(violations)

    result = kubectl(["apply", "-f", "-"], input_text=json.dumps(deployment))
    if result.returncode != 0:
        return False, result.stderr
    if service is not None:
        result = kubectl(["apply", "-f", "-"], input_text=json.dumps(service))
        if result.returncode != 0:
            return False, result.stderr
    else:
        kubectl(["delete", "service", name, "-n", NAMESPACE, "--ignore-not-found"])

    extra_ports_service_name = f"{name}{CONTAINER_EXTRA_PORTS_SERVICE_SUFFIX}"
    if extra_ports:
        result = kubectl(
            ["apply", "-f", "-"],
            input_text=json.dumps(_container_extra_ports_service_manifest(name, extra_ports)),
        )
        if result.returncode != 0:
            return False, result.stderr
    else:
        kubectl(["delete", "service", extra_ports_service_name, "-n", NAMESPACE, "--ignore-not-found"])

    auth_error = apply_basic_auth_secret(name, auth_mode == "basic", auth_username, htpasswd_hash)
    if auth_error:
        return False, f"認証設定に失敗しました: {auth_error.strip()}"
    oidc_error = apply_oidc_auth_middleware(name, auth_mode == "oidc")
    if oidc_error:
        return False, f"認証設定に失敗しました: {oidc_error.strip()}"
    upgrade_insecure_error = apply_upgrade_insecure_middleware(name, upgrade_insecure)
    if upgrade_insecure_error:
        return False, f"混在コンテンツ自動修正の設定に失敗しました: {upgrade_insecure_error.strip()}"

    if not fqdn:
        kubectl(["delete", "ingressroute", name, "-n", NAMESPACE, "--ignore-not-found"])
        kubectl(["delete", "ingressroutetcp", f"{name}-tcp", "-n", NAMESPACE, "--ignore-not-found"])
        return True, None

    if tls_mode == "terminate":
        error = apply_container_route(name, fqdn, port, auth_mode, upgrade_insecure)
    else:
        error = apply_container_route_tcp(name, fqdn, port)
    if error:
        return False, error
    return True, None


def delete_container_locally(name):
    """The actual teardown, shared by the user-facing delete route and the
    peer-replicated /internal/containers/<name>/delete. One label-selected
    delete covers the Deployment, Service, whichever of IngressRoute/
    IngressRouteTCP exists (the other was never created), and the per-app
    auth Secret/Middleware(s) (<name>-auth / <name>-oidc-auth, if auth was
    ever enabled -- same label-selected cleanup delete_route() already
    uses for js/site/proxy). Persistent data is a hostPath volume, not a
    PVC (see _container_deployment_manifest()), so nothing in Kubernetes
    itself cleans up CONTAINER_DATA_BASE_DIR/<name> on delete -- remove it
    directly, same as "site" apps' on-disk files being removed via
    shutil.rmtree() on delete.

    Waits for this app's Pod(s) to actually be gone before that rmtree --
    confirmed live (2026-08 integration testing) this matters: `kubectl
    delete deployment` only deletes the Deployment object itself
    synchronously, the ReplicaSet/Pod cascade is garbage-collected in the
    background. Deleting right after an edit (which, under the Recreate
    strategy, is *already* mid-replacing the Pod) can catch a brand new
    Pod that kubelet hasn't finished setting up yet -- its
    `DirectoryOrCreate` hostPath volume gets created a moment *after*
    rmtree already ran here, leaving an empty orphaned directory behind
    permanently (harmless on its own -- no data in it -- but real disk
    clutter that accumulates over time). Best-effort with a short
    timeout: an already-broken deletion elsewhere shouldn't be made to
    hang here waiting for Pods that were never going to disappear anyway.
    """
    kubectl(
        [
            "delete",
            "deployment,service,ingressroute,ingressroutetcp,secret,middleware",
            "-n", NAMESPACE,
            "-l", f"{APP_LABEL}={name}",
            "--ignore-not-found",
        ]
    )
    kubectl(
        [
            "wait", "--for=delete", "pod",
            "-n", NAMESPACE,
            "-l", f"{APP_LABEL}={name}",
            "--timeout=15s",
        ]
    )
    shutil.rmtree(CONTAINER_DATA_BASE_DIR / name, ignore_errors=True)


@app.route("/crons/new")
def new_cron_page():
    from_name = request.args.get("from", "").strip()
    form_code = None
    form_schedule = ""
    form_enabled = True
    form_language = "python"
    mail = None
    if from_name and NAME_RE.match(from_name):
        form_code = get_cron_source(from_name)
        form_language = get_cron_language(from_name)
        mail = get_cron_mail(from_name)
        result = kubectl(["get", "cronjob", from_name, "-n", NAMESPACE, "-o", "json"])
        if result.returncode == 0:
            spec = json.loads(result.stdout).get("spec", {})
            form_schedule = spec.get("schedule", "")
            form_enabled = not spec.get("suspend", False)
    return render_template(
        "cron_new.html",
        namespace=NAMESPACE,
        active_nav="new_cron",
        form_code=form_code,
        form_schedule=form_schedule,
        form_enabled=form_enabled,
        form_language=form_language,
        default_code=CRON_DEFAULT_CODE.get(form_language, CRON_DEFAULT_CODE["python"]),
        **_mail_form_fields(mail),
    )


@app.route("/crons/<name>/edit")
def edit_cron(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    code = get_cron_source(name)
    if code is None:
        apps, error = list_spinapps()
        crons, cron_error = list_cronjobs()
        containers, container_error = list_container_apps()
        return render_template(
            "index.html",
            apps=apps,
            error=error,
            crons=crons,
            cron_error=cron_error,
            containers=containers,
            container_error=container_error,
            errors=["この Cron Job のソースが見つかりませんでした。"],
            namespace=NAMESPACE,
            active_nav="dashboard",
        ), 400
    result = kubectl(["get", "cronjob", name, "-n", NAMESPACE, "-o", "json"])
    spec = {}
    if result.returncode == 0:
        spec = json.loads(result.stdout).get("spec", {})
    form_language = get_cron_language(name)
    return render_template(
        "cron_edit.html",
        namespace=NAMESPACE,
        active_nav="new_cron",
        edit_name=name,
        is_edit=True,
        form_name=name,
        form_code=code,
        form_schedule=spec.get("schedule", ""),
        form_enabled=not spec.get("suspend", False),
        form_language=form_language,
        default_code=CRON_DEFAULT_CODE.get(form_language, CRON_DEFAULT_CODE["python"]),
        runs=list_cron_runs(name),
        **_mail_form_fields(get_cron_mail(name)),
    )


@app.route("/crons", methods=["POST"])
def create_cron():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "")
    schedule = request.form.get("schedule", "").strip()
    enabled = request.form.get("enabled") == "1"
    language = request.form.get("language", "python").strip()
    if language not in CRON_LANGUAGES:
        language = "python"
    is_edit = request.form.get("mode") == "edit"
    template_name = "cron_edit.html" if is_edit else "cron_new.html"

    mail_from = request.form.get("mail_from", "").strip()
    mail_to = request.form.get("mail_to", "").strip()
    mail_fields_raw = {
        "form_mail_from": mail_from,
        "form_mail_to": mail_to,
    }

    def render_error(error_list, status=400):
        return render_template(
            template_name,
            errors=error_list,
            namespace=NAMESPACE,
            active_nav="new_cron",
            edit_name=name,
            is_edit=is_edit,
            form_name=name,
            form_code=code,
            form_schedule=schedule,
            form_enabled=enabled,
            form_language=language,
            default_code=CRON_DEFAULT_CODE.get(language, CRON_DEFAULT_CODE["python"]),
            runs=list_cron_runs(name) if is_edit else None,
            **mail_fields_raw,
        ), status

    errors = []
    if not NAME_RE.match(name):
        errors.append("name は小文字英数字とハイフンのみ、63文字以内で入力してください。")
    if not code.strip():
        errors.append("code が空です。")
    if len(code.encode("utf-8")) > MAX_CRON_SOURCE_BYTES:
        errors.append(f"code が大きすぎます ({MAX_CRON_SOURCE_BYTES // 1000}KB まで)。")
    if not is_valid_cron_schedule(schedule):
        errors.append(
            "schedule は5フィールドのcron式 (例: */5 * * * *) か @hourly などの形式で入力してください。"
        )

    mail = None
    mail_values = [mail_from, mail_to]
    if any(v.strip() for v in mail_values):
        if not all(v.strip() for v in mail_values):
            errors.append(
                "メール送信を使う場合は From/To を両方入力してください "
                "(使わない場合は両方とも空欄のままにしてください)。"
            )
        elif not SMTP_HOST:
            errors.append("メール送信を使う前に /config でSMTPサーバーを設定してください。")
        else:
            mail = {"from": mail_from, "to": mail_to}
            if len(json.dumps(mail).encode("utf-8")) > MAX_CRON_MAIL_BYTES:
                errors.append(f"メールの内容が大きすぎます ({MAX_CRON_MAIL_BYTES // 1000}KB まで)。")
                mail = None

    if errors:
        return render_error(errors)

    ok, error = deploy_cron_job(name, code, schedule, enabled, mail, language)
    if not ok:
        return render_error([error])

    # Unlike apps/sites/proxies (redundant HTTP backends -- fine to run on
    # every node at once), a cron job *does* something each time it fires,
    # so running it on every peer on the same schedule would multiply side
    # effects (duplicate emails, duplicate writes, ...). Peers still get the
    # code+schedule+mail-fields synced for backup/parity, but see
    # internal_apply_cron(): every peer-synced copy is always deployed
    # suspended, never this node's own `enabled` value.
    replicate_to_peers(
        "/internal/crons",
        {"name": name, "code": code, "schedule": schedule, "mail": mail, "language": language},
    )
    return redirect(url_for("index"))


def _apply_cron_payload(data, enabled=False):
    """Validate+deploy a "cron" job from a plain dict shaped like
    cron_export_payload()'s output (same shape replicate_to_peers() sends
    to /internal/crons). Shared by internal_apply_cron() (peer
    replication, always enabled=False -- see below) and import_page()
    (file-based import, which restores the exported `enabled` state --
    see cron_export_payload()'s docstring for why that's the right
    default there but not for peer sync). Returns (ok, error, status) --
    see _apply_js_payload()'s docstring."""
    name = data.get("name", "")
    code = data.get("code", "")
    schedule = data.get("schedule", "")
    mail = data.get("mail")
    language = data.get("language", "python")
    if language not in CRON_LANGUAGES:
        language = "python"
    if (
        not is_valid_name(name)
        or not isinstance(code, str) or not code
        or not isinstance(schedule, str) or not is_valid_cron_schedule(schedule)
    ):
        return False, "invalid payload", 400
    if mail is not None and not isinstance(mail, dict):
        return False, "invalid payload", 400
    if len(code.encode("utf-8")) > MAX_CRON_SOURCE_BYTES:
        return False, "code too large", 400
    if mail is not None and len(json.dumps(mail).encode("utf-8")) > MAX_CRON_MAIL_BYTES:
        return False, "mail config too large", 400

    ok, error = deploy_cron_job(name, code, schedule, enabled=enabled, mail=mail, language=language)
    return ok, error, (200 if ok else 500)


@app.route("/internal/crons", methods=["POST"])
@csrf.exempt
def internal_apply_cron():
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    # Always suspended -- see the comment above create_cron()'s
    # replicate_to_peers() call. A peer's own admin has to explicitly flip
    # it on from that node's own UI; every re-sync (e.g. a code edit on the
    # origin node) resets it back to suspended, by design, so a code change
    # can't silently start firing on a peer unattended.
    #
    # This peer's own SMTP_* settings (its own /config), not the origin
    # node's, are what get baked into send_mail() -- each node sends
    # through its own configured mail server, once its admin enables it.
    ok, error, status = _apply_cron_payload(request.get_json(force=True, silent=True) or {}, enabled=False)
    return jsonify(ok=ok, error=error), status


@app.route("/crons/<name>/delete", methods=["POST"])
def delete_cron(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    delete_cron_locally(name)
    replicate_to_peers(f"/internal/crons/{name}/delete", {})
    return redirect(url_for("index"))


@app.route("/internal/crons/<name>/delete", methods=["POST"])
@csrf.exempt
def internal_delete_cron(name):
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    if not NAME_RE.match(name):
        return jsonify(ok=False, error="invalid name"), 400
    delete_cron_locally(name)
    return jsonify(ok=True)


@app.route("/containers/templates")
def container_templates_page():
    """Dedicated management page for user-saved Container app templates
    (see save_container_as_template()) -- listing them inline on the
    dashboard (an earlier iteration of this feature) got unwieldy once
    more than a couple exist, so this is its own page, linked from the
    dashboard instead."""
    return render_template(
        "container_templates.html",
        namespace=NAMESPACE,
        active_nav="dashboard",
        container_templates=CONTAINER_USER_TEMPLATES,
    )


@app.route("/containers/new")
def new_container_page():
    from_name = request.args.get("from", "").strip()
    template_key = request.args.get("template", "").strip()
    config = None
    if from_name and NAME_RE.match(from_name):
        config = get_container_config(from_name)
    elif template_key:
        config = find_container_template(template_key)
    config = config or {}
    return render_template(
        "container_new.html",
        namespace=NAMESPACE,
        active_nav="new_container",
        is_edit=False,
        fqdns=REGISTERED_FQDNS,
        traefik_scheme=TRAEFIK_SCHEME,
        container_passthrough_enabled=CONTAINER_PASSTHROUGH_ENABLED,
        max_replicas=MAX_CONTAINER_REPLICAS,
        form_image=config.get("image", ""),
        form_port=config.get("port", ""),
        form_command=config.get("command", ""),
        form_env=config.get("env", ""),
        form_sidecars_json=json.dumps(config.get("sidecars") or []),
        form_extra_ports_json=json.dumps(config.get("extra_ports") or []),
        form_storage=config.get("storage", True),
        form_mount_path=config.get("mount_path") or CONTAINER_DATA_MOUNT_PATH,
        form_cpu_limit=config.get("cpu_limit") or "500m",
        form_mem_limit=config.get("mem_limit") or "512Mi",
        form_replicas=config.get("replicas") or 1,
        form_fqdn=config.get("fqdn", ""),
        form_tls_mode=config.get("tls_mode") or "terminate",
        form_upgrade_insecure=config.get("upgrade_insecure", False),
        auth_mode=config.get("auth_mode") or "none",
        auth_username=config.get("auth_username", ""),
        oidc_users_text=config.get("oidc_users_text", ""),
        form_yaml_manifest=default_container_yaml_template(
            from_name, config.get("image", ""), config.get("port", ""),
            config.get("cpu_limit") or "500m", config.get("mem_limit") or "512Mi",
            config.get("replicas") or 1,
            command=shlex.split(config["command"]) if config.get("command") else None,
            env=_parse_env_text(config.get("env", ""))[0],
            sidecars=_sidecars_for_yaml_template(config.get("sidecars") or []),
            storage=config.get("storage", True),
            mount_path=config.get("mount_path") or CONTAINER_DATA_MOUNT_PATH,
        ),
    )


@app.route("/containers/<name>/edit")
def edit_container(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    config = get_container_config(name)
    if config is None:
        apps, error = list_spinapps()
        crons, cron_error = list_cronjobs()
        containers, container_error = list_container_apps()
        return render_template(
            "index.html",
            apps=apps,
            error=error,
            crons=crons,
            cron_error=cron_error,
            containers=containers,
            container_error=container_error,
            errors=["この Container アプリが見つかりませんでした。"],
            namespace=NAMESPACE,
            active_nav="dashboard",
        ), 400
    return render_template(
        "container_edit.html",
        namespace=NAMESPACE,
        active_nav="new_container",
        edit_name=name,
        is_edit=True,
        form_name=name,
        fqdns=REGISTERED_FQDNS,
        traefik_scheme=TRAEFIK_SCHEME,
        container_passthrough_enabled=CONTAINER_PASSTHROUGH_ENABLED,
        max_replicas=MAX_CONTAINER_REPLICAS,
        form_image=config["image"],
        form_port=config["port"],
        form_command=config["command"],
        form_env=config["env"],
        form_sidecars_json=json.dumps(config["sidecars"]),
        form_extra_ports_json=json.dumps(config.get("extra_ports") or []),
        form_storage=config["storage"],
        form_mount_path=config["mount_path"],
        form_cpu_limit=config["cpu_limit"],
        form_mem_limit=config["mem_limit"],
        form_replicas=config["replicas"],
        form_fqdn=config["fqdn"],
        form_tls_mode=config["tls_mode"],
        form_upgrade_insecure=config["upgrade_insecure"],
        auth_mode=config["auth_mode"],
        auth_username=config["auth_username"],
        oidc_users_text=config["oidc_users_text"],
        form_yaml_manifest=container_deployment_service_yaml(name),
    )


@app.route("/containers", methods=["POST"])
def create_container():
    name = request.form.get("name", "").strip()
    edit_mode = request.form.get("edit_mode", "form")
    if edit_mode not in ("form", "yaml"):
        edit_mode = "form"
    image = request.form.get("image", "").strip()
    port_raw = request.form.get("port", "").strip()
    command_raw = request.form.get("command", "").strip()
    env_raw = request.form.get("env", "")
    sidecars_raw = request.form.get("sidecars_json", "")
    extra_ports_raw = request.form.get("extra_ports_json", "")
    storage_raw = "storage" in request.form
    mount_path_raw = request.form.get("mount_path", "").strip()
    cpu_limit = request.form.get("cpu_limit", "").strip()
    mem_limit = request.form.get("mem_limit", "").strip()
    replicas_raw = request.form.get("replicas", "1").strip()
    yaml_manifest = request.form.get("yaml_manifest", "")
    fqdn = request.form.get("fqdn", "").strip()
    tls_mode = request.form.get("tls_mode", "terminate").strip()
    upgrade_insecure = "upgrade_insecure" in request.form
    auth_mode = request.form.get("auth_mode", "none")
    if auth_mode not in ("none", "basic", "oidc"):
        auth_mode = "none"
    auth_username = request.form.get("auth_username", "").strip()
    auth_password = request.form.get("auth_password", "")
    oidc_users = parse_oidc_users(request.form.get("auth_oidc_users", ""))
    is_edit = request.form.get("mode") == "edit"
    template_name = "container_edit.html" if is_edit else "container_new.html"

    def render_error(error_list, status=400):
        return render_template(
            template_name,
            errors=error_list,
            namespace=NAMESPACE,
            active_nav="new_container",
            edit_name=name,
            is_edit=is_edit,
            form_name=name,
            fqdns=REGISTERED_FQDNS,
            traefik_scheme=TRAEFIK_SCHEME,
            max_replicas=MAX_CONTAINER_REPLICAS,
            form_image=image,
            form_port=port_raw,
            form_command=command_raw,
            form_env=env_raw,
            form_sidecars_json=sidecars_raw,
            form_extra_ports_json=extra_ports_raw,
            form_storage=storage_raw,
            form_mount_path=mount_path_raw,
            form_cpu_limit=cpu_limit,
            form_mem_limit=mem_limit,
            form_replicas=replicas_raw,
            form_fqdn=fqdn,
            form_tls_mode=tls_mode,
            form_upgrade_insecure=upgrade_insecure,
            form_yaml_manifest=yaml_manifest,
            auth_mode=auth_mode,
            auth_username=auth_username,
            oidc_users_text="\n".join(oidc_users),
        ), status

    errors = []
    if not NAME_RE.match(name):
        errors.append("name は小文字英数字とハイフンのみ、63文字以内で入力してください。")
    elif not is_edit and name in RESERVED_APP_NAMES:
        errors.append(f'"{name}" は予約済みの名前のため使用できません。')

    # port is only required when fqdn is set -- IngressRoute/IngressRouteTCP
    # need a numeric target port to route to, but a pure compute/batch
    # container with no fqdn (see the fqdn validation below) has nothing
    # to route at all, so forcing a port number on it would be a made-up
    # requirement. Blank port -> no containerPort declared and no Service
    # created at all (see deploy_container_app()'s port=None handling).
    port = None
    if not port_raw:
        if fqdn:
            errors.append("専用ドメイン (FQDN) を指定する場合は、port も入力してください。")
    elif not port_raw.isdigit() or not (1 <= int(port_raw) <= 65535):
        errors.append("port は1〜65535の数値で入力してください。")
    else:
        port = int(port_raw)

    command = None
    if command_raw:
        try:
            command = shlex.split(command_raw)
        except ValueError as exc:
            errors.append(f"起動コマンドの形式が正しくありません: {exc}")

    # image/cpu/mem/replicas only matter in form mode -- in YAML mode the
    # submitted manifest is the sole source of truth for the Deployment
    # (see deploy_container_app()'s yaml_manifest parameter); validating
    # these fields against it would be redundant with -- and could
    # conflict with -- parse_container_yaml()/validate_container_security_
    # policy()'s own checks, which run unconditionally inside
    # deploy_container_app() regardless of edit_mode.
    replicas = None
    if edit_mode == "form":
        if not image:
            errors.append("image を入力してください。")
        if not CPU_QUANTITY_RE.match(cpu_limit):
            errors.append('CPU上限は "500m" や "1" のような形式で入力してください。')
        if not MEM_QUANTITY_RE.match(mem_limit):
            errors.append('メモリ上限は "512Mi" や "1Gi" のような形式で入力してください。')
        if not replicas_raw.isdigit() or not (1 <= int(replicas_raw) <= MAX_CONTAINER_REPLICAS):
            errors.append(f"レプリカ数は1〜{MAX_CONTAINER_REPLICAS}の数値で入力してください。")
        else:
            replicas = int(replicas_raw)
    elif not yaml_manifest.strip():
        errors.append("YAMLを入力してください。")
    elif len(yaml_manifest.encode("utf-8")) > MAX_CONTAINER_YAML_BYTES:
        errors.append(f"YAMLが大きすぎます ({MAX_CONTAINER_YAML_BYTES // 1000}KB まで)。")

    env = None
    sidecars = None
    mount_path = None
    if edit_mode == "form":
        env, env_errors = _parse_env_text(env_raw)
        errors.extend(env_errors)
        sidecars, sidecars_error = _parse_sidecars_json(sidecars_raw)
        if sidecars_error:
            errors.append(sidecars_error)
        mount_path = mount_path_raw or CONTAINER_DATA_MOUNT_PATH
        if storage_raw and not CONTAINER_MOUNT_PATH_RE.match(mount_path):
            errors.append("マウント先パスは \"/\" から始まる絶対パスで入力してください。")

    # extra_ports is a dedicated field regardless of edit_mode -- like
    # fqdn/tls_mode/port below, not tied to the フォーム/YAML sub-tab
    # split (see deploy_container_app()'s docstring).
    extra_ports, extra_ports_error = _parse_extra_ports_json(extra_ports_raw)
    if extra_ports_error:
        errors.append(extra_ports_error)
    elif extra_ports:
        for p in extra_ports:
            owner = find_extra_port_owner(p["port"], p["protocol"], exclude_name=name if is_edit else None)
            if owner:
                errors.append(
                    f'追加ポート {p["protocol"]}/{p["port"]} は既に別のアプリ("{owner}")で使用されています。'
                )
            elif is_host_port_in_use(p["port"], p["protocol"]):
                errors.append(
                    f'追加ポート {p["protocol"]}/{p["port"]} はこのホストの他のサービスが既に使用しているため指定できません。'
                )

    # fqdn is optional: blank means "no external exposure at all" (e.g. a
    # pure batch/compute container, only ever touched via the コンソール
    # tab) -- deploy_container_app() skips creating any routing object in
    # that case (see its docstring). When given, it must be well-formed
    # and not already claimed by any other app of any kind: unlike js/
    # site/proxy (which happily share one FQDN via distinct PathPrefixes),
    # a "container" app takes over the bare Host with no PathPrefix, so
    # sharing an FQDN with another app breaks routing for one or both.
    if fqdn and not EXTERNAL_HOST_RE.match(fqdn):
        errors.append("FQDNの形式が正しくありません。")
    elif fqdn:
        owner = find_fqdn_owner(fqdn, exclude_name=name if is_edit else None)
        if owner:
            errors.append(f'"{fqdn}" は既に別のアプリ("{owner}")で使用されています。')

    if tls_mode not in CONTAINER_TLS_MODES:
        errors.append("TLSの扱いの指定が不正です。")
    elif tls_mode == "passthrough" and not CONTAINER_PASSTHROUGH_ENABLED:
        errors.append(
            "パススルーモードは現在無効化されています(/configで有効化してください)。"
        )

    # Auth only has anywhere to attach in "terminate" mode -- a passthrough
    # connection's HTTP payload (including any Middleware chain) is never
    # seen by Traefik at all (see apply_container_route_tcp()'s docstring).
    # Checked here too (not just deploy_container_app()'s own copy of this
    # check) so the error surfaces as a normal inline form error instead of
    # the generic [error] rendering.
    if auth_mode != "none" and tls_mode == "passthrough":
        errors.append("パススルーモードでは認証を設定できません。終端モードを選ぶか、認証を無効にしてください。")
    if upgrade_insecure and tls_mode == "passthrough":
        errors.append("パススルーモードでは混在コンテンツの自動修正を設定できません。終端モードを選ぶか、無効にしてください。")
    if auth_mode == "basic" and not auth_username:
        errors.append("Basic認証を有効にする場合は username を入力してください。")
    if auth_mode == "oidc" and not oidc_users:
        errors.append("OIDC認証を有効にする場合は許可するユーザー名を1人以上入力してください。")
    # OIDC's login redirect_uri is built from request_origin(), which now
    # also trusts this specific app's own fqdn (looked up fresh from its
    # annotation via get_app_auth_config() at auth time -- see
    # spindeck_forwardauth()/spindeck_oidc_login()) in addition to
    # REGISTERED_FQDNS, so a "container" app's deliberately free-text fqdn
    # (see find_fqdn_owner()'s docstring) no longer needs to be a
    # pre-registered one for OIDC to work correctly. Each such host still
    # needs "https://<fqdn>/_spindeck/oidc/callback" registered as a valid
    # redirect URI on the Keycloak client, same as any REGISTERED_FQDNS
    # entry already does.

    # SpinKube's operator auto-generates a same-named Deployment/Service for
    # every SpinApp -- unlike js/site/proxy/cron (whose primary objects are
    # disjoint API kinds from each other), a "container" app's Deployment
    # would collide with that and fight the operator's own reconciliation.
    # Only relevant at creation; an app's name is immutable once created.
    if not is_edit and not errors:
        for kubectl_kind, label in (("spinapp", "App"), ("cronjob", "Cron Job")):
            check = kubectl(["get", kubectl_kind, name, "-n", NAMESPACE])
            if check.returncode == 0:
                errors.append(f'"{name}" は既に別の種類のアプリ({label})で使われています。')
                break

    if errors:
        return render_error(errors)

    htpasswd_hash = None
    if auth_mode == "basic":
        htpasswd_hash, auth_hash_error = resolve_auth_password_hash(name, auth_password)
        if auth_hash_error:
            return render_error([auth_hash_error])

    ok, error = deploy_container_app(
        name, image, port, cpu_limit, mem_limit, fqdn, tls_mode, replicas,
        yaml_manifest=yaml_manifest if edit_mode == "yaml" else None,
        auth_mode=auth_mode, auth_username=auth_username,
        htpasswd_hash=htpasswd_hash, oidc_users=oidc_users,
        command=command, env=env, sidecars=sidecars,
        storage=storage_raw, mount_path=mount_path,
        upgrade_insecure=upgrade_insecure, extra_ports=extra_ports,
    )
    if not ok:
        return render_error([error])

    replicate_to_peers(
        "/internal/containers",
        {
            "name": name,
            "image": image,
            "port": port,
            "cpu_limit": cpu_limit,
            "mem_limit": mem_limit,
            "replicas": replicas,
            "fqdn": fqdn,
            "tls_mode": tls_mode,
            "yaml_manifest": yaml_manifest if edit_mode == "yaml" else None,
            "auth_mode": auth_mode,
            "auth_username": auth_username,
            "htpasswd_hash": htpasswd_hash,
            "oidc_users": oidc_users,
            "command": command,
            "env": env,
            "sidecars": sidecars,
            "storage": storage_raw,
            "mount_path": mount_path,
            "upgrade_insecure": upgrade_insecure,
            "extra_ports": extra_ports,
        },
    )
    return redirect(url_for("index"))


def _apply_container_payload(data):
    """Validate+deploy a "container" app from a plain dict shaped like
    container_export_payload()'s output (same shape replicate_to_peers()
    sends to /internal/containers). Shared by internal_apply_container()
    (peer replication) and import_page() (file-based import). Returns
    (ok, error, status) -- see _apply_js_payload()'s docstring."""
    name = data.get("name", "")
    image = data.get("image", "")
    port = data.get("port")
    cpu_limit = data.get("cpu_limit", "")
    mem_limit = data.get("mem_limit", "")
    replicas = data.get("replicas", 1)
    fqdn = data.get("fqdn", "")
    tls_mode = data.get("tls_mode", "terminate")
    yaml_manifest = data.get("yaml_manifest")
    auth_mode = data.get("auth_mode", "none")
    auth_username = data.get("auth_username", "")
    htpasswd_hash = data.get("htpasswd_hash")
    oidc_users = data.get("oidc_users") or []
    command = data.get("command")
    env = data.get("env")
    sidecars = data.get("sidecars")
    storage = data.get("storage", True)
    mount_path = data.get("mount_path")
    upgrade_insecure = data.get("upgrade_insecure", False)
    extra_ports = data.get("extra_ports")
    if (
        not is_valid_name(name)
        or not isinstance(image, str)
        or not isinstance(fqdn, str)
        or (yaml_manifest is not None and not isinstance(yaml_manifest, str))
        or (port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)))
        or (port is None and fqdn)
        or (fqdn and not EXTERNAL_HOST_RE.match(fqdn))
        or tls_mode not in CONTAINER_TLS_MODES
        or auth_mode not in ("none", "basic", "oidc")
        or (command is not None and not isinstance(command, list))
        or (env is not None and not isinstance(env, list))
        or (sidecars is not None and not isinstance(sidecars, list))
        or not isinstance(storage, bool)
        or (mount_path is not None and not isinstance(mount_path, str))
        or not isinstance(upgrade_insecure, bool)
        or (upgrade_insecure and tls_mode == "passthrough")
        or (tls_mode == "passthrough" and not CONTAINER_PASSTHROUGH_ENABLED)
    ):
        return False, "invalid payload", 400
    sidecars_error = _validate_sidecars_structure(sidecars)
    if sidecars_error:
        return False, sidecars_error, 400
    extra_ports_error = _validate_extra_ports_structure(extra_ports)
    if extra_ports_error:
        return False, extra_ports_error, 400
    # Structural validation only when the origin node used form mode --
    # a yaml_manifest payload is validated by parse_container_yaml()/
    # validate_container_security_policy() inside deploy_container_app()
    # instead, exactly like create_container()'s own edit_mode branching.
    if yaml_manifest is None and (
        not image
        or not isinstance(cpu_limit, str) or not CPU_QUANTITY_RE.match(cpu_limit)
        or not isinstance(mem_limit, str) or not MEM_QUANTITY_RE.match(mem_limit)
        or not isinstance(replicas, int)
        or not (1 <= replicas <= MAX_CONTAINER_REPLICAS)
    ):
        return False, "invalid payload", 400
    if yaml_manifest is not None and len(yaml_manifest.encode("utf-8")) > MAX_CONTAINER_YAML_BYTES:
        return False, "yaml_manifest too large", 400
    ok, error = deploy_container_app(
        name, image, port, cpu_limit, mem_limit, fqdn, tls_mode, replicas,
        yaml_manifest=yaml_manifest,
        auth_mode=auth_mode, auth_username=auth_username,
        htpasswd_hash=htpasswd_hash, oidc_users=oidc_users,
        command=command, env=env, sidecars=sidecars,
        storage=storage, mount_path=mount_path,
        upgrade_insecure=upgrade_insecure, extra_ports=extra_ports,
    )
    return ok, error, (200 if ok else 500)


@app.route("/internal/containers", methods=["POST"])
@csrf.exempt
def internal_apply_container():
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    ok, error, status = _apply_container_payload(request.get_json(force=True, silent=True) or {})
    return jsonify(ok=ok, error=error), status


@app.route("/containers/<name>/delete", methods=["POST"])
def delete_container(name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    delete_container_locally(name)
    replicate_to_peers(f"/internal/containers/{name}/delete", {})
    return redirect(url_for("index"))


@app.route("/containers/<name>/save_as_template", methods=["POST"])
def save_container_as_template(name):
    """Snapshot a deployed "container" app's *current* live config (the
    same shape get_container_config() already returns for the "copy"
    feature) into CONTAINER_USER_TEMPLATES, so it shows up on the
    dedicated template management page (container_templates_page()) --
    unlike "copy" (?from=<name>), which re-reads the source app fresh
    every time and so stops working the moment that app is deleted, a
    saved template is an independent snapshot that outlives the original
    app entirely.
    """
    global CONTAINER_USER_TEMPLATES
    if not NAME_RE.match(name):
        return "invalid name", 400
    label = request.form.get("label", "").strip() or name
    config = get_container_config(name)
    if config is None:
        return redirect(url_for("edit_container", name=name))
    template = dict(config)
    template["key"] = f"user-{secrets.token_hex(4)}"
    template["label"] = f"📦 {label}"
    CONTAINER_USER_TEMPLATES = CONTAINER_USER_TEMPLATES + [template]
    save_persisted_config()
    return redirect(url_for("container_templates_page"))


@app.route("/containers/templates/<key>/delete", methods=["POST"])
def delete_container_template(key):
    global CONTAINER_USER_TEMPLATES
    CONTAINER_USER_TEMPLATES = [t for t in CONTAINER_USER_TEMPLATES if t.get("key") != key]
    save_persisted_config()
    return redirect(url_for("container_templates_page"))


@app.route("/internal/containers/<name>/delete", methods=["POST"])
@csrf.exempt
def internal_delete_container(name):
    if not _peer_authorized():
        return jsonify(ok=False, error="unauthorized"), 403
    if not NAME_RE.match(name):
        return jsonify(ok=False, error="invalid name"), 400
    delete_container_locally(name)
    return jsonify(ok=True)


@app.route("/containers/<name>/logs")
def container_logs(name):
    """Plain-text tail of every replica Pod's logs for this "container" app
    (used by the "📜 ログ" tab) -- same kubectl-logs-by-label pattern
    cron_logs() already uses, --prefix=true so a multi-replica app's
    output is still attributable to a specific Pod. `?container=` picks
    which of the Pod's containers to read (defaults to the primary one --
    without an explicit -c, kubectl logs on a multi-container Pod just
    errors out asking for one instead of picking a default), validated
    against this app's own known container list the same way
    _container_exec_bridge() validates its own `container` param."""
    if not NAME_RE.match(name):
        return "invalid name", 400
    config = get_container_config(name)
    if config is None:
        return "not found", 404
    container = (request.args.get("container") or "").strip() or config["container_names"][0]
    if container not in config["container_names"]:
        return "invalid container", 400
    result = kubectl(
        [
            "logs",
            "-l",
            f"{APP_LABEL}={name}",
            "-n",
            NAMESPACE,
            "-c",
            container,
            f"--tail={MAX_CONTAINER_LOG_LINES}",
            "--prefix=true",
        ]
    )
    output = result.stdout or ""
    if result.returncode != 0:
        output += ("\n" if output else "") + (result.stderr or "ログの取得に失敗しました。")
    return Response(output or "(ログはまだありません)", mimetype="text/plain")


@app.route("/crons/<name>/logs")
def cron_logs(name):
    """Plain-text tail of every job pod's logs for this cron (used by the
    "ログ" tab). Pods are found by the APP_LABEL, which every Job/Pod this
    CronJob spawns inherits from its jobTemplate (see deploy_cron_job())."""
    if not NAME_RE.match(name):
        return "invalid name", 400
    result = kubectl(
        [
            "logs",
            "-l",
            f"{APP_LABEL}={name}",
            "-n",
            NAMESPACE,
            f"--tail={MAX_CRON_LOG_LINES}",
            "--prefix=true",
        ]
    )
    output = result.stdout or ""
    if result.returncode != 0:
        output += ("\n" if output else "") + (result.stderr or "ログの取得に失敗しました。")
    return Response(output or "(ログはまだありません)", mimetype="text/plain")


# ---------------------------------------------------------------------------
# Export / Import
#
# Each <kind>_export_payload(name) builds a dict shaped exactly like the
# body replicate_to_peers() already sends to that kind's /internal/*
# endpoint on every create/edit -- that shape is already a complete,
# self-contained description of how to recreate the app (peer replication
# depends on that being true), so it doubles as the export format with no
# separate schema to invent or keep in sync. Importing an app dict is
# just handing it to that same kind's _apply_*_payload() (the validation+
# deploy logic _apply_js_payload()/_apply_site_payload()/
# _apply_proxy_payload()/_apply_cron_payload()/_apply_container_payload()
# also share with their /internal/* route), so export and import are
# exact inverses of each other and of peer replication -- one format,
# three consumers.
#
# Passwords/htpasswd hashes and OIDC allow-lists are included in the
# export as-is (not redacted) -- an export file is exactly as sensitive
# as the app it describes and should be handled accordingly; this is a
# deliberate choice (not an oversight) so a restored/migrated app comes
# back up already configured instead of silently dropping its auth.
#
# Import is local-only: unlike create_*()'s own replicate_to_peers()
# calls, importing a file never pushes anything to this node's peers.
# Restoring a backup or moving an app between nodes is something an
# admin does deliberately, one node at a time -- fanning an import out
# to peers automatically would be a surprising side effect, especially
# since a bulk "export everything" file could easily contain apps that
# make no sense on another node (e.g. one bound to a proxy's LAN-only
# backend).
# ---------------------------------------------------------------------------

MAX_IMPORT_FILE_BYTES = 20_000_000
MAX_IMPORT_APPS = 200


def _spinapp_annotations(name):
    """Full metadata.annotations dict for a SpinApp, or None if it
    doesn't exist. Used by the export builders for the "js"/"site"/
    "proxy" kinds, which each need several annotations at once (auth
    mode/username, OIDC users, fqdn) instead of the single-jsonpath reads
    get_js_source()/get_proxy_config() do."""
    result = kubectl(["get", "spinapp", name, "-n", NAMESPACE, "-o", "json"])
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)["metadata"].get("annotations", {}) or {}


def _oidc_users_list(annotations):
    return [u for u in annotations.get(AUTH_OIDC_USERS_ANNOTATION, "").split(",") if u]


def js_export_payload(name):
    """The export/import (and peer-replication) payload for a "js" app,
    or None if `name` isn't one. See this section's module-level
    docstring."""
    code = get_js_source(name)
    if code is None:
        return None
    annotations = _spinapp_annotations(name) or {}
    auth_mode = resolve_auth_mode(annotations)
    return {
        "kind": "js",
        "name": name,
        "code": code,
        "auth_mode": auth_mode,
        "auth_username": annotations.get(AUTH_USERNAME_ANNOTATION, ""),
        "htpasswd_hash": get_existing_auth_hash(name) if auth_mode == "basic" else None,
        "oidc_users": _oidc_users_list(annotations),
        "fqdn": annotations.get(FQDN_ANNOTATION, "") or None,
    }


def site_export_payload(name):
    """The export/import (and peer-replication) payload for a "site" app,
    or None if `name` isn't one. See this section's module-level
    docstring."""
    files = get_site_files(name)
    if files is None:
        return None
    annotations = _spinapp_annotations(name) or {}
    auth_mode = resolve_auth_mode(annotations)
    return {
        "kind": "site",
        "name": name,
        "files": files,
        "auth_mode": auth_mode,
        "auth_username": annotations.get(AUTH_USERNAME_ANNOTATION, ""),
        "htpasswd_hash": get_existing_auth_hash(name) if auth_mode == "basic" else None,
        "oidc_users": _oidc_users_list(annotations),
        "fqdn": annotations.get(FQDN_ANNOTATION, "") or None,
    }


def proxy_export_payload(name):
    """The export/import (and peer-replication) payload for a "proxy"
    app, or None if `name` isn't one. See this section's module-level
    docstring."""
    config = get_proxy_config(name)
    if config is None:
        return None
    annotations = _spinapp_annotations(name) or {}
    auth_mode = resolve_auth_mode(annotations)
    return {
        "kind": "proxy",
        "name": name,
        "backend_origin": config.get("backend_origin", ""),
        "rules": config.get("rules", []),
        "rewrites": config.get("rewrites", []),
        "ws_path": config.get("ws_path", ""),
        "backend_prefix": config.get("backend_prefix", ""),
        "auth_mode": auth_mode,
        "auth_username": annotations.get(AUTH_USERNAME_ANNOTATION, ""),
        "htpasswd_hash": get_existing_auth_hash(name) if auth_mode == "basic" else None,
        "oidc_users": _oidc_users_list(annotations),
        "fqdn": annotations.get(FQDN_ANNOTATION, "") or None,
    }


def cron_export_payload(name):
    """The export/import (and peer-replication-shaped) payload for a
    "cron" job, or None if `name` doesn't exist. Unlike peer replication
    (internal_apply_cron() always forces enabled=False -- see its
    docstring), this includes the job's actual current enabled/suspended
    state, since restoring your own single-node backup to exactly how it
    was is the right default here -- the multi-node duplicate-side-effect
    concern that justifies always-suspended for peer sync doesn't apply
    to a deliberate, one-at-a-time, single-admin import."""
    code = get_cron_source(name)
    if code is None:
        return None
    result = kubectl(["get", "cronjob", name, "-n", NAMESPACE, "-o", "json"])
    if result.returncode != 0:
        return None
    obj = json.loads(result.stdout)
    return {
        "kind": "cron",
        "name": name,
        "code": code,
        "schedule": obj.get("spec", {}).get("schedule", ""),
        "mail": get_cron_mail(name),
        "language": get_cron_language(name),
        "enabled": not obj.get("spec", {}).get("suspend", False),
    }


def container_export_payload(name):
    """The export/import (and peer-replication-shaped) payload for a
    "container" app, or None if `name` isn't one. Always exports via the
    live Deployment+Service YAML (container_deployment_service_yaml() --
    the same source the edit page's "YAML" sub-tab starts from) rather
    than reconstructing form-mode fields (image/cpu_limit/mem_limit/
    command/env/sidecars/...): that YAML is guaranteed to faithfully
    round-trip *any* container app regardless of whether it was
    originally built via the form or hand-edited YAML (multiple
    containers, extra volumes, a renamed primary container, ...), with
    no separate reconstruction logic that could drift out of sync with
    what's actually live. fqdn/tls_mode/port/auth stay dedicated fields
    either way -- see deploy_container_app()'s docstring for why those
    are never part of the YAML itself."""
    config = get_container_config(name)
    if config is None:
        return None
    yaml_manifest = container_deployment_service_yaml(name)
    if yaml_manifest is None:
        return None
    auth_mode = config["auth_mode"]
    return {
        "kind": "container",
        "name": name,
        "port": config["port"] or None,
        # Unlike js/site/proxy's _apply_*_payload() (which normalize
        # data.get("fqdn") or None themselves), _apply_container_payload()
        # requires fqdn to always be a plain str (possibly "") -- matching
        # create_container()'s own always-str form field -- so this must
        # stay "" here, not None, or a portless/fqdn-less container fails
        # its isinstance(fqdn, str) check on import.
        "fqdn": config["fqdn"],
        "tls_mode": config["tls_mode"],
        "auth_mode": auth_mode,
        "auth_username": config["auth_username"],
        "htpasswd_hash": get_existing_auth_hash(name) if auth_mode == "basic" else None,
        "oidc_users": [u for u in config["oidc_users_text"].split("\n") if u],
        "upgrade_insecure": config["upgrade_insecure"],
        "extra_ports": config.get("extra_ports") or [],
        "yaml_manifest": yaml_manifest,
    }


EXPORT_BUILDERS = {
    "container": container_export_payload,
    "js": js_export_payload,
    "site": site_export_payload,
    "proxy": proxy_export_payload,
    "cron": cron_export_payload,
}

IMPORT_APPLIERS = {
    "container": lambda item: _apply_container_payload(item),
    "js": lambda item: _apply_js_payload(item),
    "site": lambda item: _apply_site_payload(item),
    "proxy": lambda item: _apply_proxy_payload(item),
    "cron": lambda item: _apply_cron_payload(item, enabled=bool(item.get("enabled", False))),
}


def _export_bundle(apps):
    return {
        "spindeck_export": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "apps": apps,
    }


def _json_download(data, filename):
    response = Response(
        json.dumps(data, indent=2, ensure_ascii=False),
        mimetype="application/json",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@app.route("/export/<kind>/<name>")
def export_app(kind, name):
    if not NAME_RE.match(name):
        return "invalid name", 400
    builder = EXPORT_BUILDERS.get(kind)
    if builder is None:
        return "invalid kind", 400
    payload = builder(name)
    if payload is None:
        return "not found", 404
    return _json_download(_export_bundle([payload]), f"{name}.spindeck.json")


def build_full_export_bundle():
    """The export bundle (see _export_bundle()) for every registered app
    of every kind -- what export_all() downloads as a file, factored out
    so backup_export.py (run standalone by backup.sh, no HTTP/session
    involved) can produce the exact same thing in-process."""
    apps = []
    containers, _ = list_container_apps()
    for c in containers:
        payload = container_export_payload(c["name"])
        if payload is not None:
            apps.append(payload)
    spinapps, _ = list_spinapps()
    for a in spinapps:
        builder = EXPORT_BUILDERS.get(a["kind"])
        # a["kind"] can also be "image" (a SpinApp registered outside the
        # console, e.g. straight via kubectl) -- not one of the console's
        # own editable kinds, so there's no export builder for it and it's
        # silently skipped, same as it already being marked non-editable
        # everywhere else (index.html, list_spinapps()).
        if builder is None:
            continue
        payload = builder(a["name"])
        if payload is not None:
            apps.append(payload)
    crons, _ = list_cronjobs()
    for c in crons:
        payload = cron_export_payload(c["name"])
        if payload is not None:
            apps.append(payload)
    return _export_bundle(apps)


@app.route("/export/all")
def export_all():
    filename = f"spindeck-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return _json_download(build_full_export_bundle(), filename)


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "GET":
        return render_template("import.html", namespace=NAMESPACE, active_nav="import")

    def render_error(error_list, status=400):
        return render_template(
            "import.html", namespace=NAMESPACE, active_nav="import", errors=error_list,
        ), status

    uploaded = request.files.get("file")
    pasted = request.form.get("json_text", "").strip()
    if uploaded and uploaded.filename:
        raw_bytes = uploaded.read(MAX_IMPORT_FILE_BYTES + 1)
        if len(raw_bytes) > MAX_IMPORT_FILE_BYTES:
            return render_error([f"ファイルが大きすぎます ({MAX_IMPORT_FILE_BYTES // 1_000_000}MB まで)。"])
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return render_error(["ファイルの文字コードが不正です (UTF-8で保存されたエクスポートファイルを指定してください)。"])
    elif pasted:
        if len(pasted.encode("utf-8")) > MAX_IMPORT_FILE_BYTES:
            return render_error([f"内容が大きすぎます ({MAX_IMPORT_FILE_BYTES // 1_000_000}MB まで)。"])
        raw = pasted
    else:
        return render_error(["ファイルを選択するか、JSONを貼り付けてください。"])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return render_error([f"JSONの形式が不正です: {exc}"])

    if isinstance(data, dict) and isinstance(data.get("apps"), list):
        apps = data["apps"]
    elif isinstance(data, dict) and isinstance(data.get("kind"), str):
        # A single exported app (not wrapped in the {"apps": [...]} bundle
        # shape) -- export_app() always wraps even a single app, but
        # accepting the unwrapped shape too costs nothing and is a more
        # forgiving format for anyone hand-crafting a file.
        apps = [data]
    else:
        return render_error(["SpinDeckのエクスポートファイルの形式ではありません。"])

    if len(apps) > MAX_IMPORT_APPS:
        return render_error([f"一度にインポートできるアプリは{MAX_IMPORT_APPS}件までです。"])

    results = []
    for item in apps:
        if not isinstance(item, dict):
            results.append({"kind": "?", "name": "?", "ok": False, "error": "不正なエントリです。"})
            continue
        kind = item.get("kind")
        name = item.get("name", "")
        applier = IMPORT_APPLIERS.get(kind)
        if applier is None:
            results.append({"kind": kind, "name": name, "ok": False, "error": "不明な種類 (kind) です。"})
            continue
        ok, error, _status = applier(item)
        results.append({"kind": kind, "name": name, "ok": ok, "error": error})

    return render_template(
        "import_result.html", namespace=NAMESPACE, active_nav="import", results=results,
    )


if __name__ == "__main__":
    threading.Thread(target=tls_poll_loop, daemon=True).start()
    if CONSOLE_VIA_TRAEFIK:
        # The console's Traefik route is keyed off OIDC_REDIRECT_URI's
        # hostname (see ensure_forwardauth_reachability()), which is only
        # re-read from the environment on a fresh process -- if an admin
        # changed it via /config's OIDC form (which restarts this
        # service), the route needs recomputing once here, since nothing
        # else calls this at startup and _forwardauth_ready resets to
        # False on every restart anyway.
        startup_reach_error = ensure_forwardauth_reachability(force=True)
        if startup_reach_error:
            print(f"[startup] WARNING: console_via_traefik route setup failed: {startup_reach_error}")
    # "::" is IPv6-wildcard, but also accepts IPv4 connections on Linux
    # unless bindv6only is set system-wide -- install.sh sets this to "::"
    # instead of the default "0.0.0.0" only when SPINDECK_DUAL_STACK was
    # used, so single-stack (IPv4-only) hosts are entirely unaffected.
    # threaded=True: without it, Werkzeug's dev server handles one request
    # at a time -- fine historically, but a container app's browser
    # terminal (see sock.route("/containers/<name>/exec")) holds a
    # WebSocket open for as long as the tab stays open, which would
    # otherwise stall every other admin action on this console for that
    # entire time.
    app.run(
        threaded=True,
        host=os.environ.get("SPINAPP_UI_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
    )

#!/usr/bin/env python3
"""Prints build_full_export_bundle()'s JSON (every registered app of
every kind, the same bundle /export/all downloads as a file) to stdout.

Run standalone by backup.sh -- no HTTP request, no console login, since
a cron job has neither. Must run with the same environment app.py itself
runs under (KUBECONFIG in particular -- see app.py's own KUBECONFIG
fallback comment for why a bare invocation without it silently talks to
the wrong cluster credentials instead of failing loudly), which is why
backup.sh sources /etc/spinapp-ui/env before calling this.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as spindeck  # noqa: E402

print(json.dumps(spindeck.build_full_export_bundle(), indent=2, ensure_ascii=False))

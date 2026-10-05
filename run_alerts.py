#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Spustí systémové alerty na CF Workeri (POST /api/run-alerts, x-alert-secret) a vrátené
'messages' pošle do Telegramu. GitHub Actions port — žiadna Mac/lokálna DB závislosť
(endpoint počíta všetko z D1 server-side). Pôvodný Mac skript:
25_SAVED_ALERTS/run_alerts_cron.py (launchd sk.trilipy.alerts, 2×/deň) — Mac verzia ostáva
zálohou, táto beží autonómne cez .github/workflows/alerts.yml."""
import json, os, sys, urllib.request

try:
    import tg
except Exception:
    tg = None

URL = "https://trilipy-kataster.kristiak-bohus.workers.dev/api/run-alerts"


def main():
    secret = os.environ.get("ALERT_SECRET")
    if not secret:
        print("chyba: env ALERT_SECRET nie je nastavený"); return 1
    req = urllib.request.Request(
        URL, method="POST",
        headers={"x-alert-secret": secret, "content-type": "application/json", "User-Agent": "TRI-LIPY-alerts-gha/1.0"},
        data=b"{}")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.loads(r.read().decode())
    except Exception as e:
        print(f"chyba volania: {e}"); return 1
    if not res.get("ok"):
        print(f"endpoint: {res}"); return 1
    msgs = res.get("messages") or []
    print(f"OK: checked={res.get('checked')} newTotal={res.get('newTotal')} messages={len(msgs)}")
    for m in msgs:
        if tg: tg.send(m, parse_mode="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

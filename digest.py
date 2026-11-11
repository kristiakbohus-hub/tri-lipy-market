#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ranný Telegram súhrn — ZÁLOHA v cloude (workflow digest.yml). Primárne ho posiela Mac o 7:30
(37_OPPORTUNITY/tg_digest_send.py). Appka (/api/tg-digest) pustí cloud až od 7:45 miestneho času
a len ak súhrn ešte nikto neposlal/nezabral — nikdy nepríde dvakrát. Obsah zostavuje appka.
"""
import json, os, sys, time, urllib.request, urllib.error

APP = "https://trilipy-kataster.kristiak-bohus.workers.dev/api/tg-digest"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def call(action):
    body = json.dumps({"action": action, "runner": "cloud"}).encode()
    for w in [0, 20, 60, 120]:
        if w: time.sleep(w)
        req = urllib.request.Request(APP, data=body, method="POST", headers={
            "content-type": "application/json", "x-alert-secret": os.environ.get("ALERT_SECRET", ""), "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401): return {"ok": False, "message": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            print(f"[digest] {action}: {str(e)[:100]} — opakujem")
    return {"ok": False, "message": "appka nedostupná"}


def main():
    import tg
    r = call("claim")
    if not r.get("ok"):
        print(f"[digest] appka: {r.get('message')}"); return 1
    if not r.get("claimed"):
        print(f"[digest] neposielam: {r.get('reason')}"); return 0
    msgs = r.get("messages") or []
    sent = 0
    for m in msgs:
        ok = False
        for w in [0, 20, 60]:
            if w: time.sleep(w)
            if tg.send(m, parse_mode=""): ok = True; break
        if not ok: break
        sent += 1; time.sleep(1.2)
    if sent == 0:
        call("fail"); print("[digest] Telegram zlyhal — deň uvoľnený"); return 1
    call("ack")
    print(f"[digest] odoslané {sent}/{len(msgs)} · {r.get('stats')}")
    return 0 if sent == len(msgs) else 1


if __name__ == "__main__":
    sys.exit(main())

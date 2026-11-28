#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ranný Telegram súhrn — ZÁLOHA v cloude (workflow digest.yml). Primárne ho posiela Mac o 7:30
(37_OPPORTUNITY/tg_digest_send.py), druhá záloha je Cron Trigger appky (digestCloudCron, e-mail + Telegram).
Appka (/api/tg-digest) pustí cloud až od 7:45 miestneho času a len ak súhrn ešte nikto neposlal/nezabral —
nikdy nepríde dvakrát. Obsah zostavuje appka.

2026-11-27 (oprava 4): táto záloha súhrn od 17. 11. NIKDY neposlala — Telegram z GitHub Actions zlyhal pri každom
pokuse (repo secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID chýbajú alebo nesedia) a chyba bola len v logu behu, ktorý
sa bez prihlásenia nedá prečítať. Teraz:
  - bez secrets sa deň vôbec nezaberá (nezdrží iného odosielateľa) a beh skončí chybou s jasným dôvodom;
  - dôvod zlyhania Telegramu ide do appky (`fail` s `reason` → kontrola doručenia o 8:15 ho ukáže vlastníkovi)
    a ako anotácia `::error::` (viditeľná cez verejné GitHub API check-runs/<id>/annotations aj bez prihlásenia).
Token sa nikdy nevypisuje (Telegram ho v odpovedi nevracia).
"""
import contextlib, io, json, os, sys, time, urllib.request, urllib.error

APP = "https://trilipy-kataster.kristiak-bohus.workers.dev/api/tg-digest"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def gh_error(msg):
    """Anotácia GitHub Actions — verejne čitateľná cez API aj bez prihlásenia (na rozdiel od logu behu)."""
    print(f"::error title=Ranný súhrn::{msg}".replace("\n", " "), flush=True)


def call(action, **extra):
    body = json.dumps({"action": action, "runner": "cloud", **extra}).encode()
    for w in [0, 20, 60, 120]:
        if w: time.sleep(w)
        req = urllib.request.Request(APP, data=body, method="POST", headers={
            "content-type": "application/json", "x-alert-secret": os.environ.get("ALERT_SECRET", ""), "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401): return {"ok": False, "message": f"HTTP {e.code}"}
            print(f"[digest] {action}: HTTP {e.code} — opakujem")
        except Exception as e:  # noqa: BLE001
            print(f"[digest] {action}: {str(e)[:100]} — opakujem")
    return {"ok": False, "message": "appka nedostupná"}


def main():
    import tg
    if not tg.configured():
        st = (call("status").get("status") or {})          # len čítanie — keď súhrn už odišiel, netreba hlásiť chybu
        if st.get("status") == "sent":
            print("[digest] neposielam: dnešný súhrn už bol odoslaný (repo secrets TELEGRAM_* chýbajú)"); return 0
        gh_error("chýbajú repo secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — súhrn sa z GitHubu neposiela "
                 "(deň sa nezaberá; zálohu robí Cron Trigger appky)")
        return 1
    r = call("claim")
    if not r.get("ok"):
        gh_error(f"appka: {r.get('message')}"); return 1
    if not r.get("claimed"):
        print(f"[digest] neposielam: {r.get('reason')}"); return 0
    msgs = r.get("messages") or []
    sent, err = 0, ""
    for m in msgs:
        ok = False
        for w in [0, 20, 60]:
            if w: time.sleep(w)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = tg.send(m, parse_mode="")
            out = buf.getvalue().strip()
            if out:
                print(out); err = out.splitlines()[-1][:200]
            if ok: break
        if not ok: break
        sent += 1; time.sleep(1.2)
    if sent == 0:
        reason = f"Telegram z GitHub Actions: {err or 'neznáma chyba'}"
        call("fail", reason=reason[:480])
        gh_error(f"{reason} — deň uvoľnený (skontroluj repo secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return 1
    call("ack", reason=f"Telegram {sent}/{len(msgs)} (GitHub Actions)")
    print(f"[digest] odoslané {sent}/{len(msgs)} · {r.get('stats')}")
    if sent < len(msgs):
        gh_error(f"odoslané len {sent}/{len(msgs)} správ: {err}")
    return 0 if sent == len(msgs) else 1


if __name__ == "__main__":
    sys.exit(main())

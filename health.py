#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stav zberu inzercie → Telegram (len pri probléme rána) + súhrn do GitHub Actions + plán behu.

Mesiac (8. 10.–7. 11. 2026) zber potichu nefungoval a nikto o tom nevedel. Odteraz:
  python3 health.py plan               # ktorý beh spustiť: rolling (ranný) / newest (popoludňajší) / skip
  python3 health.py report [--mode M]  # po zbere: problémy z reportu → Telegram (LEN ranný beh)
  python3 health.py mark-ingested [--mode M]   # značka „v appke" do reportu behu
  python3 health.py fail <URL> [--mode M]      # krok zlyhal → upozornenie vlastníkovi HNEĎ (pozri nižšie)
  python3 health.py done-today         # "yes", ak dnes už prebehol úspešný RANNÝ zber (spätná kompatibilita)

UPOZORNENIE PRI ZLYHANÍ (oprava 10, 2026-11-28): 27. 11. zlyhal ingest do appky a vlastník sa to dozvedel až
na druhý deň — krok „Hlásenie zlyhania“ síce bežal, ale repo secrets TELEGRAM_* chýbajú, takže tg.py nič
neposlal a skončil úspechom. Teraz `fail` skúsi všetky kanály a výsledok zapíše VEREJNE do anotácie behu:
  1. anotácia `::error::` s dôvodom (čitateľná cez verejné GitHub API aj bez prihlásenia),
  2. Telegram z Actions (repo secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — keď ich vlastník doplní),
  3. appka: POST /api/ingest-market {"step":"alert"} → e-mail + Telegram vlastníkovi z appky (po nasadení
     patchu 16_TRHOVE_CENY/oprava10_appka.patch; dovtedy appka odpovie 400 a nič sa nestane),
  4. anotácia `TRILIPY_ALERT telegram=0|1 app=0|1` — strážca na Macu (16_TRHOVE_CENY/zber_strazca.py, každých
     10 min) podľa nej pošle upozornenie z Macu, ak ho neposlal nikto iný (Mac má Telegram aj e-mail funkčné).
Nikdy sa nevypisuje token ani kľúč.
"""
import json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = {"rolling": "scrape-report.json", "newest": "scrape-report-newest.json"}
APP = os.environ.get("APP", "https://trilipy-kataster.kristiak-bohus.workers.dev")
UA = "tri-lipy-market-health/2.0 (+https://github.com/kristiakbohus-hub/tri-lipy-market)"

# Popoludňajší ľahký beh (oprava 10): od 12:00 do 21:00 miestneho času, najskôr 3 h po rannom.
LIGHT_FROM_H, LIGHT_TO_H, LIGHT_GAP_H = 12, 21, 3


def _tg(text):
    try:
        import tg
        return tg.send(text, parse_mode="")
    except Exception as e:  # noqa: BLE001 — hlásenie nesmie zhodiť workflow
        print(f"[health] Telegram zlyhal: {type(e).__name__}")
        return False


def _arg(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _path(mode):
    return os.path.join(HERE, REPORTS.get(mode or "rolling", REPORTS["rolling"]))


def _report(mode="rolling"):
    try:
        return json.load(open(_path(mode), encoding="utf-8"))
    except Exception:
        return None


def _remote_report(mode):
    """Report z AKTUÁLNEHO origin/main (workflow pred tým spraví git fetch). Checkout plánovaného behu môže byť
    hodiny starý (28. 11.: beh čakal vo fronte a mal commit spred 2 h) — preto nie súbor z pracovného adresára."""
    try:
        out = subprocess.run(["git", "-C", HERE, "show", f"origin/main:{REPORTS[mode]}"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        pass
    return _report(mode)


def _local_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Bratislava"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def _ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def morning_done(r, today):
    return bool(r and r.get("date") == today and r.get("mode") == "rolling" and r.get("ingested"))


def plan(event, schedule="", forced="", now=None, today=None, rolling=None, newest=None):
    """→ (mode, dôvod). mode: rolling | newest | skip.
    - ranný beh (rolling) má prednosť: kým dnes neprebehol, spustí ho KTORÝKOĽVEK termín (GitHub plánované behy
      mešká o hodiny a niektoré vynechá) — to je pôvodná poistka „Už dnes prebehlo?";
    - popoludňajší ľahký beh (newest): ranný je hotový, miestny čas 12–21 h, od ranného/posledného ľahkého behu
      aspoň 3 h a dnes ešte neprebehol plánovaný ľahký beh;
    - push (zmena kódu) = overovací beh: ranný, ak dnes ešte nebol, inak ľahký;
    - ručné spustenie (workflow_dispatch) s mode=rolling/newest beží vždy, mode=auto ako plánovaný."""
    now = now or _local_now()
    today = today or date.today().isoformat()          # runner beží v UTC — rovnaký „dnes" ako scraper.py
    r = rolling if rolling is not None else _remote_report("rolling")
    n = newest if newest is not None else _remote_report("newest")
    if forced in ("rolling", "newest"):
        return forced, f"ručne zvolený beh {forced}"
    if not morning_done(r, today):
        return "rolling", "dnešný ranný zber ešte nie je v appke"
    if event == "push":
        return "newest", "zmena kódu — overovací ľahký beh (ranný zber dnes už prebehol)"
    h = now.hour
    if not (LIGHT_FROM_H <= h < LIGHT_TO_H):
        return "skip", f"ranný zber je v appke; ľahký beh len {LIGHT_FROM_H}–{LIGHT_TO_H} h (teraz {h} h)"
    if n and n.get("date") == today and n.get("ingested") and n.get("trigger") == "schedule":
        return "skip", "dnešný popoludňajší beh už prebehol"
    last = max(_ts(r.get("finished_at") or ""), _ts((n or {}).get("finished_at") or ""))
    if last and now.timestamp() - last < LIGHT_GAP_H * 3600:
        return "skip", f"posledný zber skončil pred menej ako {LIGHT_GAP_H} h"
    return "newest", "popoludňajší ľahký beh (len nové inzeráty)"


def _post_app(body, secret, timeout=60):
    req = urllib.request.Request(f"{APP}/api/ingest-market", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "x-alert-secret": secret, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:  # noqa: BLE001
        return 0, {}


def _annot(level, title, msg):
    print(f"::{level} title={title}::" + " ".join(str(msg).split()), flush=True)


def fail(url, mode, argv):
    label = "Popoludňajší zber" if mode == "newest" else "Ranný zber"
    stages = {k: os.environ.get(f"STAGE_{k.upper()}", "") for k in ("scrape", "commit", "ingest", "report")}
    names = {"scrape": "zber inzerátov", "commit": "zápis do repa", "ingest": "ingest do appky", "report": "stav zberu"}
    bad = [names[k] for k, v in stages.items() if v == "failure"]
    detail = ""
    try:
        ir = json.load(open(os.path.join(HERE, "ingest-result.json"), encoding="utf-8"))
        if not ir.get("ok"):
            detail = ir.get("summary") or ""
    except Exception:  # noqa: BLE001
        pass
    when = _local_now().strftime("%d. %m. %H:%M")
    head = f"⚠️ {label} inzercie ZLYHAL ({when})" + (f" v kroku: {', '.join(bad)}" if bad else "")
    text = (head + (f"\n{detail}" if detail else "")
            + "\nAppka ostáva na dátach z posledného úspešného behu; ďalší pokus spraví najbližší termín."
            + (f"\n{url}" if url else ""))
    _annot("error", f"{label} zlyhal", text)
    tg_ok = _tg(text)
    app_ok = False
    secret = os.environ.get("ALERT_SECRET", "")
    if secret:
        code, j = _post_app({"step": "alert", "kind": "zber", "mode": mode, "text": text[:1500],
                             "run": os.environ.get("GITHUB_RUN_ID", "")}, secret)
        app_ok = code == 200 and bool(j.get("ok")) and bool(j.get("sent"))
        print(f"[health] appka upozornenie: HTTP {code}{' (odoslané)' if app_ok else ''}")
    # strojovo čitateľné pre strážcu na Macu (zber_strazca.py) — bez tajomstiev
    _annot("notice", "TRILIPY_ALERT", f"TRILIPY_ALERT telegram={int(tg_ok)} app={int(app_ok)}")
    return 0


def report(mode):
    r = _report(mode)
    if not r:
        if mode == "rolling":
            _tg("⚠️ Zber inzercie: chýba report behu (scrape-report.json).")
        print("[health] chýba report behu")
        return 0
    src = r.get("sources", {})
    lines = [f"{k}: {v.get('rows', 0)} nových/zmenených, {v.get('seen', 0)} potvrdených, {v.get('pages', 0)} strán, "
             f"chybovosť {v.get('error_rate', 0)} %, {v.get('seconds', 0)} s"
             + (" — ZASTAVENÉ časovým stropom" if v.get("stopped") else "")
             + (f" — BLOKOVANÉ ({v['blocked']})" if v.get("blocked") else "")
             + (f" — VYNECHANÉ ({v['skipped']})" if v.get("skipped") else "")
             for k, v in src.items()]
    info = r.get("info") or []          # zastavené / prechodné chyby pod prahom — len do súhrnu, NIE do Telegramu
    kind = "Popoludňajší zber (len nové)" if mode == "newest" else "Zber inzercie"
    summary = (f"{kind} {r.get('date')}: dnes videných {r.get('seen_today')}, nových {r.get('new_today')}, "
               f"aktívnych {r.get('active_listings')}, trvanie {round((r.get('seconds') or 0) / 60)} min"
               + (f", zverejnených riadkov {r.get('written')}" if mode == "newest" else ""))
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as f:
            f.write("### " + summary + "\n\n" + "\n".join(f"- {x}" for x in lines) + "\n")
            for p in r.get("problems") or []:
                f.write(f"- ⚠️ {p}\n")
            for p in info:
                f.write(f"- ℹ️ {p}\n")
    print(summary)
    for x in lines:
        print("  " + x)
    for x in info:
        print("  info: " + x)
    probs = r.get("problems") or []
    if probs and mode == "rolling":     # popoludňajší beh nič neposiela (rozhodnutie vlastníka 28. 11.)
        _tg("⚠️ Zber inzercie — problém:\n" + "\n".join(f"• {p}" for p in probs) + "\n\n" + summary)
    return 0


def mark_ingested(mode, trigger):
    p = _path(mode)
    r = json.load(open(p, encoding="utf-8"))
    r["ingested"] = True
    r["ingested_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    r["trigger"] = trigger or r.get("trigger")
    json.dump(r, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "report"
    mode = _arg(argv, "--mode", "rolling")
    if cmd == "done-today":
        print("yes" if morning_done(_remote_report("rolling"), date.today().isoformat()) else "no")
        return 0
    if cmd == "plan":
        m, why = plan(os.environ.get("EVENT", ""), os.environ.get("SCHEDULE", ""), os.environ.get("FORCED", ""))
        print(f"[plan] {m}: {why}")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"mode={m}\n")
        return 0
    if cmd == "fail":
        url = argv[2] if len(argv) > 2 and not argv[2].startswith("--") else ""
        return fail(url, mode, argv)
    if cmd == "mark-ingested":
        return mark_ingested(mode, os.environ.get("EVENT", ""))
    return report(mode)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

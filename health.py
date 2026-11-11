#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stav zberu inzercie → Telegram (len pri probléme) + súhrn do GitHub Actions.

Mesiac (8. 10.–7. 11. 2026) zber potichu nefungoval a nikto o tom nevedel. Odteraz:
  python3 health.py report        # po zbere: problémy zo scrape-report.json → Telegram
  python3 health.py fail <URL>    # krok workflowu zlyhal → Telegram s odkazom na beh
  python3 health.py done-today    # vypíše "yes", ak dnes už prebehol úspešný zber (ďalší termín sa preskočí)
"""
import json, os, sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "scrape-report.json")


def _tg(text):
    try:
        import tg
        return tg.send(text, parse_mode="")
    except Exception as e:  # noqa: BLE001 — hlásenie nesmie zhodiť workflow
        print(f"[health] Telegram zlyhal: {e}")
        return False


def _report():
    try:
        return json.load(open(REPORT, encoding="utf-8"))
    except Exception:
        return None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "report"
    r = _report()
    if cmd == "done-today":
        print("yes" if r and r.get("date") == date.today().isoformat() and r.get("mode") == "rolling" and r.get("ingested") else "no")
        return 0
    if cmd == "fail":
        url = argv[2] if len(argv) > 2 else ""
        _tg(f"⚠️ Zber inzercie ZLYHAL ({date.today().isoformat()}). Appka ostáva na včerajších dátach; "
            f"ďalší pokus o pár hodín.\n{url}")
        return 0
    if not r:
        _tg("⚠️ Zber inzercie: chýba report behu (scrape-report.json).")
        return 0
    src = r.get("sources", {})
    lines = [f"{k}: {v.get('rows', 0)} nových/zmenených, {v.get('seen', 0)} potvrdených, {v.get('pages', 0)} strán, {v.get('seconds', 0)} s"
             for k, v in src.items()]
    summary = (f"Zber inzercie {r.get('date')}: dnes videných {r.get('seen_today')}, nových {r.get('new_today')}, "
               f"aktívnych {r.get('active_listings')}, trvanie {round((r.get('seconds') or 0) / 60)} min")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as f:
            f.write("### " + summary + "\n\n" + "\n".join(f"- {x}" for x in lines) + "\n")
            for p in r.get("problems") or []:
                f.write(f"- ⚠️ {p}\n")
    print(summary)
    for x in lines:
        print("  " + x)
    probs = r.get("problems") or []
    if probs:
        _tg("⚠️ Zber inzercie — problém:\n" + "\n".join(f"• {p}" for p in probs) + "\n\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

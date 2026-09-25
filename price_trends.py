#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Historické cenové trendy z listings.db.
- market_history: denné/týždenné snapshoty mediánu €/m² per (okres, ptype, deal).
- Backfill: rekonštrukcia týždenných mediánov z first_seen/last_seen (inzeráty aktívne v danom týždni).
- Ongoing: snapshot_today() volá scraper po každom behu → denné body do budúcna.
- Trendy: %zmena 30/90 dní + medziročne (keď budú dáta) → market-trends.json pre appku (NL filter „ceny rastú", AVM korekcia).

Použitie:
  python3 price_trends.py --backfill        # rekonštruuj históriu (týždenne) z listings.db
  python3 price_trends.py --trends          # prepočítaj trendy + publikuj market-trends.json
  python3 price_trends.py --snapshot        # zapíš dnešný bod (volá aj scraper)
"""
import json, os, sqlite3, statistics, sys
from datetime import date, datetime, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "listings.db")
OUT = os.path.join(DIR, "market-trends.json")
MIN_N = 3   # min. inzerátov v skupine na medián

def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS market_history(
        day TEXT, okres TEXT, ptype TEXT, deal TEXT,
        median REAL, p25 REAL, p75 REAL, cnt INTEGER,
        PRIMARY KEY(day, okres, ptype, deal))""")

def _agg(vals):
    vals = sorted(v for v in vals if v)
    if len(vals) < MIN_N:
        return None
    return (round(statistics.median(vals), 1), round(vals[len(vals)//4], 1), round(vals[3*len(vals)//4], 1), len(vals))

def snapshot(c, day, active_on=None):
    """Zapíše medián €/m² per (okres, ptype, deal) k danému dňu.
    active_on=None → dnešný stav (last_seen=max); inak inzeráty aktívne v daný deň (first_seen<=day<=last_seen)."""
    _ensure(c)
    if active_on is None:
        rows = c.execute("SELECT obec,ptype,deal,ppm2 FROM listings WHERE ppm2 IS NOT NULL AND obec IS NOT NULL").fetchall()
    else:
        rows = c.execute("""SELECT obec,ptype,deal,ppm2 FROM listings
                            WHERE ppm2 IS NOT NULL AND obec IS NOT NULL
                              AND first_seen<=? AND last_seen>=?""", (active_on, active_on)).fetchall()
    groups = {}
    for obec, pt, dl, ppm2 in rows:
        groups.setdefault((obec, pt, dl), []).append(ppm2)
    n = 0
    for (obec, pt, dl), vals in groups.items():
        a = _agg(vals)
        if not a:
            continue
        c.execute("INSERT OR REPLACE INTO market_history(day,okres,ptype,deal,median,p25,p75,cnt) VALUES(?,?,?,?,?,?,?,?)",
                  (day, obec, pt, dl, a[0], a[1], a[2], a[3]))
        n += 1
    c.commit()
    return n

def backfill(c):
    _ensure(c)
    rng = c.execute("SELECT MIN(first_seen), MAX(last_seen) FROM listings").fetchone()
    if not rng or not rng[0]:
        print("Žiadne dáta."); return
    d0 = datetime.fromisoformat(rng[0]).date(); d1 = datetime.fromisoformat(rng[1]).date()
    # týždenné body (nedeľa ako referenčný deň)
    days, cur = [], d0
    while cur <= d1:
        days.append(cur); cur += timedelta(days=7)
    if days[-1] != d1:
        days.append(d1)
    tot = 0
    for d in days:
        iso = d.isoformat()
        tot += snapshot(c, iso, active_on=iso)
    print(f"Backfill: {len(days)} týždenných bodov ({d0}…{d1}), {tot} riadkov histórie.")

def trends(c):
    _ensure(c)
    days = [r[0] for r in c.execute("SELECT DISTINCT day FROM market_history ORDER BY day").fetchall()]
    if not days:
        print("Prázdna história — spusti --backfill."); return
    latest = days[-1]
    def near(target):  # najbližší deň k target
        return min(days, key=lambda d: abs((datetime.fromisoformat(d).date() - target).days))
    ld = datetime.fromisoformat(latest).date()
    d30, d90 = near(ld - timedelta(days=30)), near(ld - timedelta(days=90))
    def snap(day):
        return {(o, p, dl): (m, cnt) for o, p, dl, m, cnt in
                c.execute("SELECT okres,ptype,deal,median,cnt FROM market_history WHERE day=?", (day,)).fetchall()}
    cur, s30, s90 = snap(latest), snap(d30), snap(d90)
    MIN_TREND = 8   # porovnaj len skupiny s dosť inzerátmi na OBOCH koncoch (inak = šum nábehu)
    out = []
    for key, (m, cnt) in cur.items():
        o, p, dl = key
        if cnt < MIN_TREND:
            continue
        def chg(prev):
            pm, pc = prev.get(key, (None, 0))
            return round((m - pm) / pm * 100, 1) if (pm and pc >= MIN_TREND) else None
        out.append(dict(okres=o, ptype=p, deal=dl, median=m, cnt=cnt,
                        chg_30d=chg(s30), chg_90d=chg(s90)))
    out.sort(key=lambda x: -(x["chg_90d"] if x["chg_90d"] is not None else -999))
    payload = dict(generated=latest, ref_30d=d30, ref_90d=d90, points=len(days),
                   note="Trend spoľahlivý len pri cnt>=8 na oboch koncoch; skoré body (nábeh zberu) môžu byť None.",
                   trends=out)
    json.dump(payload, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    valid90 = [t for t in out if t["chg_90d"] is not None]
    rising = [t for t in valid90 if t["chg_90d"] > 0]
    print(f"Trendy: {len(out)} skupín (cnt>=8), {len(valid90)} s platným 90d, {len(rising)} rastúcich. {latest} vs {d30}/{d90} → {OUT}")
    for t in out[:8]:
        print(f"  {t['okres']:<16} {t['ptype']:<8} {t['deal']:<8} {t['median']:>7} €/m²  30d {t['chg_30d']}%  90d {t['chg_90d']}%")

def main():
    c = sqlite3.connect(DB)
    a = sys.argv[1:] or ["--trends"]
    if "--backfill" in a: backfill(c)
    if "--snapshot" in a: print("snapshot:", snapshot(c, date.today().isoformat()), "skupín")
    if "--trends" in a or a == ["--trends"]: trends(c)

if __name__ == "__main__":
    main()

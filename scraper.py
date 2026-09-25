#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRI LIPY — denný scraper trhových cien nehnuteľností.
Zbiera inzeráty → lokálna SQLite (história cien, delty) → agregát (medián €/m² per obec+typ)
+ príležitosti (zníženie ceny, dlho na trhu, pod trhom) → market-data.json.

Architektúra: app (Cloudflare Worker) je edge-gatovaná, NEDÁ sa POSTovať priamo do D1.
Preto tento skript publikuje market-data.json na VEREJNÉ URL (napr. GitHub raw),
a appka si ho stiahne (server fn refreshMarketData, /ceny) a uloží do D1.

Použitie:
  python3 scraper.py --mode delta     # denne (najnovšie strany)
  python3 scraper.py --mode full      # týždenne (plný re-scrape)
  python3 scraper.py --test           # test parsovania (1 stránka)
Len stdlib — beží aj cez launchd bez pip.

STAV: bazos.sk funguje (jednoduché HTML, obec+PSČ+dátum priamo v inzeráte).
nehnutelnosti.sk/reality.sk načítavajú cez JS/API — doplniť v ďalšej iterácii.
"""
import argparse, json, os, re, sqlite3, statistics, sys, time, urllib.request
from datetime import date, datetime
from html import unescape

DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "listings.db")
OUT = os.path.join(DIR, "market-data.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
# Telegram (zdieľaný modul v 17_UP_MONITOR)
sys.path.insert(0, os.path.join(os.path.dirname(DIR), "17_UP_MONITOR"))
try:
    import tg
except Exception:
    tg = None
TG_ALERTED = os.path.join(DIR, "tg_alerted.json")

BAZOS = [
    ("predam/pozemok", "pozemok", "predaj"),
    ("predam/dom", "dom", "predaj"),
    ("predam/byt", "byt", "predaj"),
    ("predam/chata", "chata", "predaj"),
    ("prenajmu/byt", "byt", "prenajom"),
    ("prenajmu/dom", "dom", "prenajom"),
]
BASE = "https://reality.bazos.sk/"

def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sk"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if i == tries - 1:
                print(f"  fetch fail {url}: {e}", file=sys.stderr); return ""
            time.sleep(2)
    return ""

AREA_RE = re.compile(r"(\d[\d\s]{1,7})\s*(?:m2|m²)", re.I)
AR_RE = re.compile(r"(\d[\d\s]{0,4})\s*(?:árov|ar[ae]?|á)\b", re.I)

def txt(s): return unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()
def num(s):
    s = re.sub(r"[^\d]", "", s or ""); return int(s) if s else None

# ——— Cena: rozlíšenie CENY ZA M² (jednotková) vs CELKOVEJ CENY ———
# Per-typ rozumné hranice €/m²; mimo = mis-parse (napr. €/m² zamenené za celkovú cenu) → ppm2 = None.
PPM2_BOUNDS = {"pozemok": (0.5, 1500.0), "byt": (150.0, 15000.0), "dom": (80.0, 15000.0), "chata": (20.0, 8000.0)}
def clamp_ppm2(ptype, v):
    if not v or v <= 0: return None
    lo, hi = PPM2_BOUNDS.get(ptype, (1.0, 15000.0))
    return round(v, 1) if lo <= v <= hi else None
def norm_price(ptype, price, area, is_unit):
    """→ (celkova_cena_eur, cena_za_m2). is_unit = inzerovaná cena je €/m² (typicky pozemky)."""
    if not price or price <= 0: return (None, None)
    if is_unit:                                   # cena je €/m² → celková = €/m² × výmera
        return (round(price * area) if area and area > 5 else None, clamp_ppm2(ptype, price))
    total = price if 300 <= price <= 30_000_000 else None   # celková cena (sanity)
    ppm2 = clamp_ppm2(ptype, price / area) if (total and area and area > 5) else None
    return (total, ppm2)

# chata/chalupa/zrub → samostatný typ (aj keď je inzerát v rubrike pozemok/dom)
_CHATA_RE = re.compile(r"chat|chalup|zrub|drevenic", re.I)
def refine_ptype(ptype, title):
    """Povýši pozemok/dom na 'chata', keď titulok jasne označuje rekreačnú stavbu."""
    if ptype in ("pozemok", "dom") and title and _CHATA_RE.search(title):
        return "chata"
    return ptype

def parse_bazos(html, ptype, deal):
    out = []
    for blk in html.split('<div class="inzeraty inzeratyflex">')[1:]:
        blk = blk[:2200]
        a = re.search(r'<h2 class=nadpis><a href="(/inzerat/(\d+)/[^"]*)">(.*?)</a>', blk, re.S)
        if not a: continue
        url = "https://reality.bazos.sk" + a.group(1); ext = a.group(2); title = txt(a.group(3))
        pr = re.search(r"inzeratycena.*?([\d\s]{2,})\s*€\s*(/?\s*m)?", blk, re.S)
        price = num(pr.group(1)) if pr else None
        is_unit = bool(pr and pr.group(2))   # „€/m²" → cena je jednotková (za m²), nie celková
        lok = re.search(r'inzeratylok">(.*?)</div>', blk, re.S)
        obec = psc = None
        if lok:
            parts = re.split(r"<br\s*/?>", lok.group(1))
            obec = txt(parts[0]) or None
            pm = re.search(r"(\d{3}\s?\d{2})", lok.group(1))
            psc = pm.group(1).replace(" ", "") if pm else None
        popis = re.search(r"class=popis>(.*?)</div>", blk, re.S)
        body = (txt(popis.group(1)) if popis else "") + " " + title
        area = None
        am = AREA_RE.search(body)
        if am: area = num(am.group(1))
        elif AR_RE.search(body): area = (num(AR_RE.search(body).group(1)) or 0) * 100
        dm = re.search(r"\[(\d+)\.\s*(\d+)\.\s*(\d{4})\]", blk)
        listed = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}" if dm else None
        pt = refine_ptype(ptype, title)
        total, ppm2 = norm_price(pt, price, area, is_unit)
        if total or ppm2:
            out.append(dict(source="bazos", ext_id=ext, url=url, title=title[:160], ptype=pt, deal=deal,
                            obec=obec, psc=psc, area_m2=area, price_eur=total, ppm2=ppm2, listed=listed))
    return out

def crawl_bazos(full):
    pages = 700 if full else 8   # full = kým nie sú prázdne strany (early-break); delta = najnovšie
    all_l = []
    for seg, ptype, deal in BAZOS:
        for p in range(pages):
            url = f"{BASE}{seg}/{p*20}/" if p else f"{BASE}{seg}/"
            got = parse_bazos(fetch(url), ptype, deal)
            if not got and p > 0: break
            all_l += got
            time.sleep(1.3)
        print(f"  bazos {ptype}: {sum(1 for l in all_l if l['ptype']==ptype)} inzerátov")
    return all_l

# ——— reality.sk (JSON-LD; detail stránky majú presnú obec) ———
REALITY = [("pozemky", "pozemok"), ("domy", "dom"), ("byty", "byt")]
JSONLD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)

def reality_search_urls(cat, page):
    url = f"https://www.reality.sk/{cat}/predaj/" + (f"?page={page+1}" if page else "")
    h = fetch(url)
    out = []
    for b in JSONLD.findall(h):
        if "itemListElement" not in b: continue
        try:
            d = json.loads(re.sub(r"[\x00-\x1f]", " ", b))
        except Exception:
            continue
        for it in d.get("itemListElement", []):
            u = None
            if isinstance(it, str): u = it
            elif isinstance(it, dict):
                item = it.get("item", it)
                u = item.get("url") if isinstance(item, dict) else (item if isinstance(item, str) else it.get("url"))
            if not u or not isinstance(u, str) or "reality.sk" not in u: continue
            m = re.search(r"/([A-Za-z0-9]{6,})/?$", u)
            if m: out.append((u, m.group(1)))
    return out

def parse_reality_detail(url, eid, ptype):
    h = fetch(url)
    if not h: return None
    name = obec = None; price = area = None
    for b in JSONLD.findall(h):
        try: d = json.loads(re.sub(r"[\x00-\x1f]", " ", b))
        except Exception: continue
        if not isinstance(d, dict): continue
        if d.get("@type") == "Product":
            name = txt(d.get("name"))
            off = d.get("offers") or {}
            try: price = int(float(off.get("price"))) if off.get("price") else None
            except Exception: price = None
            desc = txt(d.get("description"))
            am = AREA_RE.search((name or "") + " " + (desc or ""))
            if am: area = num(am.group(1))
        if d.get("@type") == "Residence":
            addr = d.get("address") or {}
            obec = txt(addr.get("addressLocality")) or txt(addr.get("streetAddress"))
    if price and price > 500:
        pt = refine_ptype(ptype, name)
        return dict(source="reality", ext_id=eid, url=url, title=(name or "")[:160], ptype=pt, deal="predaj",
                    obec=obec, psc=None, area_m2=area, price_eur=price,
                    ppm2=(round(price / area, 1) if area and area > 5 else None), listed=None)
    return None

def crawl_reality(c, full):
    seen = {r[0] for r in c.execute("SELECT ext_id FROM listings WHERE source='reality'").fetchall()}
    pages = 40 if full else 4
    new = []
    for cat, ptype in REALITY:
        for p in range(pages):
            urls = reality_search_urls(cat, p)
            fresh = [(u, e, ptype) for (u, e) in urls if e not in seen]
            if not urls and p > 0: break
            new += fresh
            time.sleep(1.2)
    cap = 800 if full else 200   # detail = 1 request/inzerát → obmedz na dávku
    out = []
    for i, (u, e, ptype) in enumerate(new[:cap]):
        r = parse_reality_detail(u, e, ptype)
        if r: out.append(r)
        if i and i % 50 == 0: print(f"  reality detaily {i}/{min(cap, len(new))}")
        time.sleep(0.8)
    print(f"  reality.sk: {len(out)} nových inzerátov (z {len(new)} nájdených URL)")
    return out

# ——— zoznamrealit.sk (detail má JSON-LD Product; cena €/m² pri pozemkoch, celková pri bytoch/domoch) ———
ZOZNAM = [("pozemky", "pozemok"), ("domy", "dom"), ("byty", "byt")]

def zoznam_search_urls(cat, page):
    url = f"https://www.zoznamrealit.sk/predaj/{cat}" + (f"?strana={page+1}" if page else "")
    h = fetch(url)
    seen = set(); res = []
    for m in re.finditer(r'href="(/[a-z0-9-]+-(\d{5,}))"', h):
        e = m.group(2)
        if e in seen: continue
        seen.add(e); res.append(("https://www.zoznamrealit.sk" + m.group(1), e))
    return res

def parse_zoznam_detail(url, eid, ptype):
    h = fetch(url)
    if not h: return None
    name = None; price = None
    for b in JSONLD.findall(h):
        try: d = json.loads(re.sub(r"[\x00-\x1f]", " ", b))
        except Exception: continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            name = txt(d.get("name")); off = d.get("offers") or {}
            try: price = float(off.get("price")) if off.get("price") else None
            except Exception: price = None
    if not price: return None
    area = num(AREA_RE.search(name).group(1)) if name and AREA_RE.search(name) else None
    if price < 3000 and area:              # malá cena = €/m² (pozemky)
        ppm2 = round(price, 1); total = round(price * area)
    else:                                   # veľká = celková
        total = round(price); ppm2 = (round(total / area, 1) if area and area > 5 else None)
    obec = None
    if name:
        parts = [p.strip() for p in name.split(",") if p.strip()]
        if parts: obec = parts[-1].title()
    if total > 500:
        pt = refine_ptype(ptype, name)
        return dict(source="zoznamrealit", ext_id=eid, url=url, title=(name or "")[:160], ptype=pt, deal="predaj",
                    obec=obec, psc=None, area_m2=area, price_eur=total, ppm2=ppm2, listed=None)
    return None

def crawl_zoznam(c, full):
    seen = {r[0] for r in c.execute("SELECT ext_id FROM listings WHERE source='zoznamrealit'").fetchall()}
    pages = 40 if full else 4
    new = []
    for cat, ptype in ZOZNAM:
        for p in range(pages):
            urls = zoznam_search_urls(cat, p)
            fresh = [(u, e, ptype) for (u, e) in urls if e not in seen]
            if not urls and p > 0: break
            new += fresh
            time.sleep(1.2)
    cap = 800 if full else 200
    out = []
    for i, (u, e, ptype) in enumerate(new[:cap]):
        r = parse_zoznam_detail(u, e, ptype)
        if r: out.append(r)
        if i and i % 50 == 0: print(f"  zoznam detaily {i}/{min(cap, len(new))}")
        time.sleep(0.8)
    print(f"  zoznamrealit: {len(out)} nových inzerátov (z {len(new)} URL)")
    return out

def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS listings(
      source TEXT, ext_id TEXT, url TEXT, title TEXT, ptype TEXT, deal TEXT, obec TEXT, psc TEXT,
      area_m2 REAL, price_eur REAL, ppm2 REAL, first_seen TEXT, last_seen TEXT, first_price REAL,
      PRIMARY KEY(source,ext_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS price_history(
      source TEXT, ext_id TEXT, day TEXT, price_eur REAL, ppm2 REAL,
      PRIMARY KEY(source,ext_id,day))""")
    c.execute("CREATE TABLE IF NOT EXISTS geocode(obec TEXT PRIMARY KEY, lat REAL, lng REAL, okres TEXT, kraj TEXT)")
    for col in ("okres", "kraj"):
        try: c.execute(f"ALTER TABLE geocode ADD COLUMN {col} TEXT")
        except Exception: pass
    # removed_at = dátum, keď re-verify zistil 301 (vymazané); last_checked = kedy sme URL naposledy preverili
    for col in ("removed_at", "last_checked"):
        try: c.execute(f"ALTER TABLE listings ADD COLUMN {col} TEXT")
        except Exception: pass
    return c

def _norm_okres(county):
    o = (county or "").strip()
    o = re.sub(r"^okres\s+", "", o, flags=re.I)
    if re.match(r"^Bratislava", o, re.I): o = "Bratislava"
    elif re.match(r"^Košice", o, re.I) and o != "Košice-okolie": o = "Košice"
    return o or None

def geocode(c, obec):
    """Obec → (lat,lng,okres,kraj) cez Nominatim (addressdetails), cache navždy (1 req/s, len raz per obec)."""
    if not obec: return (None, None, None, None)
    r = c.execute("SELECT lat,lng,okres,kraj FROM geocode WHERE obec=?", (obec,)).fetchone()
    if r and (r[2] is not None or r[3] is not None): return (r[0], r[1], r[2], r[3])
    lat = lng = okres = kraj = None
    if r: lat, lng = r[0], r[1]   # máme súradnice, dopĺňam len okres/kraj
    try:
        import urllib.parse
        u = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": obec + ", Slovensko", "format": "jsonv2", "addressdetails": 1, "limit": 1})
        req = urllib.request.Request(u, headers={"User-Agent": "tri-lipy-scraper/1.0 (kristiak.bohus@gmail.com)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            arr = json.loads(resp.read().decode("utf-8", "replace"))
            if arr:
                lat = float(arr[0]["lat"]); lng = float(arr[0]["lon"])
                ad = arr[0].get("address", {}) or {}
                okres = _norm_okres(ad.get("district") or ad.get("county") or ad.get("state_district"))
                kraj = re.sub(r"\s*kraj$", "", (ad.get("region") or ad.get("state") or ""), flags=re.I).strip() or None
        time.sleep(1.1)   # Nominatim policy
    except Exception as e:
        print(f"  geocode fail {obec}: {e}", file=sys.stderr)
    c.execute("INSERT OR REPLACE INTO geocode(obec,lat,lng,okres,kraj) VALUES(?,?,?,?,?)", (obec, lat, lng, okres, kraj)); c.commit()
    return (lat, lng, okres, kraj)

def upsert(c, rows, today):
    for r in rows:
        ex = c.execute("SELECT price_eur FROM listings WHERE source=? AND ext_id=?", (r["source"], r["ext_id"])).fetchone()
        if ex:
            old_price = ex[0]
            c.execute("UPDATE listings SET url=?,title=?,ptype=?,obec=?,psc=?,area_m2=?,price_eur=?,ppm2=?,last_seen=? WHERE source=? AND ext_id=?",
                      (r["url"], r["title"], r["ptype"], r["obec"], r["psc"], r["area_m2"], r["price_eur"], r["ppm2"], today, r["source"], r["ext_id"]))
            # price_history: snímka LEN pri zmene ceny (per-inzerát krivka pohybu ceny v čase)
            if r["price_eur"] is not None and old_price is not None and abs(float(r["price_eur"]) - float(old_price)) > 0.5:
                c.execute("INSERT OR REPLACE INTO price_history(source,ext_id,day,price_eur,ppm2) VALUES(?,?,?,?,?)",
                          (r["source"], r["ext_id"], today, r["price_eur"], r["ppm2"]))
        else:
            c.execute("INSERT INTO listings(source,ext_id,url,title,ptype,deal,obec,psc,area_m2,price_eur,ppm2,first_seen,last_seen,first_price) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["source"], r["ext_id"], r["url"], r["title"], r["ptype"], r["deal"], r["obec"], r["psc"],
                       r["area_m2"], r["price_eur"], r["ppm2"], r.get("listed") or today, today, r["price_eur"]))
            # počiatočná snímka ceny
            if r["price_eur"] is not None:
                c.execute("INSERT OR REPLACE INTO price_history(source,ext_id,day,price_eur,ppm2) VALUES(?,?,?,?,?)",
                          (r["source"], r["ext_id"], r.get("listed") or today, r["price_eur"], r["ppm2"]))
    c.commit()

def reverify_gone(c, today, cap=None):
    """Preverí bazos inzeráty nevidené dnes: HTTP 301/presmerovanie = vymazané (removed_at), 200 = žije (bump last_seen).
    Rotuje podľa last_checked, priorita = najčerstvejšie (najpravdepodobnejšie transakcie + najlepšie sold-comps)."""
    import urllib.error
    cap = cap or int(os.environ.get("REVERIFY_CAP", "500"))
    class _NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a): return None
    opener = urllib.request.build_opener(_NoRedir)
    def _status(url):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            with opener.open(req, timeout=12) as r: return r.status
        except urllib.error.HTTPError as e: return e.code
        except Exception: return None
    rows = c.execute(
        "SELECT source, ext_id, url FROM listings "
        "WHERE source='bazos' AND removed_at IS NULL AND last_seen < ? "
        "ORDER BY COALESCE(last_checked,'0000-00-00') ASC, last_seen DESC LIMIT ?", (today, cap)).fetchall()
    gone = alive = errs = 0
    miss = 0                      # po sebe idúce ne-(200/301) → možný rate-limit → stop
    for src, ext, url in rows:
        st = _status(url)
        if st in (301, 302, 303, 307, 308):
            c.execute("UPDATE listings SET removed_at=?, last_checked=? WHERE source=? AND ext_id=?", (today, today, src, ext)); gone += 1; miss = 0
        elif st == 200:
            # bump last_seen LEN ak bol nedávno videný (čerstvá cena); staré len over → nefalšuj čerstvosť
            c.execute("UPDATE listings SET last_seen=CASE WHEN last_seen >= date(?, '-21 day') THEN ? ELSE last_seen END, last_checked=? WHERE source=? AND ext_id=?",
                      (today, today, today, src, ext)); alive += 1; miss = 0
        else:
            c.execute("UPDATE listings SET last_checked=? WHERE source=? AND ext_id=?", (today, src, ext)); errs += 1; miss += 1
        if miss >= 20:
            print(f"  re-verify STOP: {miss} chýb po sebe (možný rate-limit) — po {gone+alive+errs} preverených", file=sys.stderr); break
        time.sleep(float(os.environ.get("REVERIFY_SLEEP", "0.35")))   # slušný rate limit voči bazos
    c.commit()
    print(f"  re-verify: {gone+alive+errs} preverených → {gone} vymazaných, {alive} žije, {errs} chýb/preskočené")
    return gone

def build_market_data(c, today):
    rows = c.execute("SELECT ptype,deal,obec,area_m2,price_eur,ppm2,first_seen,last_seen,first_price,url,title FROM listings WHERE ppm2 IS NOT NULL AND obec IS NOT NULL").fetchall()
    groups = {}
    for pt, dl, obec, area, price, ppm2, fs, ls, fp, url, title in rows:
        groups.setdefault((obec, pt, dl), []).append(ppm2)
    index, med_by = [], {}
    for (obec, pt, dl), vals in groups.items():
        if len(vals) < 3: continue
        vals.sort(); med = statistics.median(vals)
        med_by[(obec, pt, dl)] = med
        index.append(dict(okres=obec, obec=None, ptype=pt, deal=dl, day=today,
                          median=round(med, 1), p25=round(vals[len(vals)//4], 1), p75=round(vals[3*len(vals)//4], 1), cnt=len(vals)))
    opps = []
    for pt, dl, obec, area, price, ppm2, fs, ls, fp, url, title in rows:
        flags = []
        drop = round((fp - price) / fp * 100, 1) if fp and price and fp > price else 0
        try: dom = (datetime.fromisoformat(today) - datetime.fromisoformat(fs)).days if fs else 0
        except Exception: dom = 0
        med = med_by.get((obec, pt, dl))
        below = round((med - ppm2) / med * 100, 1) if med and ppm2 and ppm2 < med else 0
        # SANITY: extrémne below/drop alebo prinízka cena = takmer isto chyba parsovania /
        # nekomparovateľný inzerát (napr. 400 € za 500 m²) → signál ignoruj, aby nezaplavil radar.
        stavba = pt in ("dom", "byt", "chata", "chalupa")
        price_ok = bool(price and price >= (15000 if stavba else 2000))
        if below > 70 or not price_ok or (ppm2 or 0) < 2: below = 0
        if drop > 90 or not price_ok: drop = 0
        if drop >= 5: flags.append("drop")
        if dom >= 90 and price_ok: flags.append("long")
        if below >= 15: flags.append("below")
        if flags and price_ok:
            opps.append(dict(source="bazos", ext_id=None, url=url, title=title, ptype=pt, deal=dl,
                             okres=obec, obec=obec, area=area, price=price, ppm2=ppm2,
                             first_seen=fs, last_seen=ls, dom=dom, drop_pct=drop, below_pct=below, flags=",".join(flags)))
    opps.sort(key=lambda o: (o["below_pct"] + o["drop_pct"]), reverse=True)
    return dict(generated=today, index=index, opportunities=opps[:300],
                meta=dict(generated=today, counts=dict(listings=len(rows), index=len(index), opps=len(opps))))

def notify_price_alerts(data, limit=10):
    """Telegram alert na NOVÉ cenové pohyby (pokles / výrazne pod mediánom).
    Ochrana proti zaplaveniu: tg_alerted.json = {url: drop_pct pri poslednom alerte};
    znovu upozorní len ak pokles narástol o ≥5 p.b."""
    if not tg:
        return
    try:
        alerted = json.load(open(TG_ALERTED, encoding="utf-8")) if os.path.exists(TG_ALERTED) else {}
    except Exception:
        alerted = {}
    fresh = []
    for o in data.get("opportunities", []):
        url = o.get("url")
        if not url:
            continue
        drop, below = o.get("drop_pct", 0), o.get("below_pct", 0)
        if drop < 8 and below < 20:            # prah pre alert (menšie výkyvy ignoruj)
            continue
        prev = alerted.get(url)
        if prev is not None and drop - prev < 5:  # už sme hlásili a nič výrazne nové
            continue
        fresh.append(o)
        alerted[url] = drop
    if not fresh:
        json.dump(alerted, open(TG_ALERTED, "w", encoding="utf-8"))
        return
    fresh.sort(key=lambda o: (o.get("below_pct", 0) + o.get("drop_pct", 0)), reverse=True)
    lines = [f"💰 Cenové pohyby: {len(fresh)} nových"]
    for o in fresh[:limit]:
        tag = []
        if o.get("drop_pct", 0) >= 8: tag.append(f"↓{o['drop_pct']}%")
        if o.get("below_pct", 0) >= 20: tag.append(f"{o['below_pct']}% pod mediánom")
        t = (o.get("title") or "")[:70]
        price = f"{int(o['price']):,} €".replace(",", " ") if o.get("price") else "?"
        lines.append(f"\n🏠 {t}\n  {price} · {' · '.join(tag)}\n  {o.get('url')}")
    if len(fresh) > limit:
        lines.append(f"\n… a ďalších {len(fresh)-limit} (detail v appke /prilezitosti).")
    tg.send("\n".join(lines), parse_mode="")
    json.dump(alerted, open(TG_ALERTED, "w", encoding="utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["delta", "full"], default="delta")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    today = date.today().isoformat()
    if a.test:
        got = parse_bazos(fetch(f"{BASE}predam/pozemok/"), "pozemok", "predaj")
        print(f"TEST: {len(got)} inzerátov z 1. stránky. Ukážka:")
        for g in got[:4]: print("  ", {k: g[k] for k in ("title", "obec", "psc", "area_m2", "price_eur", "ppm2", "listed")})
        return
    c = db()
    full = (a.mode == "full")
    rows = crawl_bazos(full=full) + crawl_reality(c, full=full)
    # crawl_zoznam(c, full) — DOČASNE VYPNUTÉ: nekonzistentné názvy (| vs ,) → nespoľahlivá lokalita; treba doladiť
    upsert(c, rows, today)
    try:                                    # re-verify: označ vymazané (301) + potvrď žijúce (bump last_seen)
        reverify_gone(c, today)
    except Exception as e:
        print(f"  re-verify fail: {e}", file=sys.stderr)
    try:                                    # denný snapshot mediánov → historické trendy
        import price_trends
        n = price_trends.snapshot(c, today)
        price_trends.trends(c)              # prepočítaj + publikuj market-trends.json
        print(f"  trendy: snapshot {n} skupín")
    except Exception as e:
        print(f"  trendy fail: {e}", file=sys.stderr)
    data = build_market_data(c, today)
    data["mode"] = a.mode
    data["listings_chunks"] = write_listings_chunks(c, today)
    data["pricehistory_chunks"] = write_price_history_chunks(c)
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Hotovo: {data['meta']['counts']}, chunkov {data['listings_chunks']} → {OUT}")
    # TG alerty scrapera vypnuté — konsolidované do jedného ranného brífingu (deal_radar_tg.py --digest).
    # Zapnú sa len s explicitným --tg.
    if "--tg" in sys.argv:
        notify_price_alerts(data)
    publish()

def write_listings_chunks(c, today, size=3000):
    """Všetky dnes videné inzeráty (s lat/lng z geokódu obce) → market-listings-<i>.json chunky."""
    rows = c.execute("SELECT source,ext_id,url,title,ptype,deal,obec,psc,area_m2,price_eur,ppm2,first_seen,last_seen,first_price,removed_at FROM listings WHERE last_seen=? OR removed_at=?", (today, today)).fetchall()
    obce = sorted({r[6] for r in rows if r[6]})
    geo = {}
    for i, ob in enumerate(obce):
        geo[ob] = geocode(c, ob)
        if i and i % 25 == 0: print(f"  geokódujem obce {i}/{len(obce)}")
    out = []
    for r in rows:
        la, ln, okres, kraj = geo.get(r[6], (None, None, None, None))
        fp, pr = r[13], r[9]
        drop = round((fp - pr) / fp * 100, 1) if fp and pr and fp > pr else 0
        flags = ["drop"] if drop >= 5 else []
        out.append(dict(source=r[0], ext_id=r[1], url=r[2], title=r[3], ptype=r[4], deal=r[5], obec=r[6], okres=okres, kraj=kraj, psc=r[7],
                        lat=la, lng=ln, area_m2=r[8], price_eur=pr, ppm2=r[10], first_seen=r[11], last_seen=r[12],
                        first_price=fp, flags=",".join(flags), removed_at=r[14]))
    n = 0
    for i in range(0, len(out), size):
        json.dump(out[i:i + size], open(os.path.join(DIR, f"market-listings-{n}.json"), "w", encoding="utf-8"), ensure_ascii=False)
        n += 1
    print(f"  inzerátov {len(out)} v {n} chunkoch (geokódovaných obcí {len(obce)})")
    return n

def write_price_history_chunks(c, size=5000):
    """Per-inzerát krivky ceny (len inzeráty s ≥2 snímkami) → market-pricehistory-<i>.json chunky."""
    rows = c.execute(
        """SELECT ph.source, ph.ext_id, ph.day, ph.price_eur, ph.ppm2 FROM price_history ph
           WHERE (ph.source, ph.ext_id) IN (SELECT source, ext_id FROM price_history GROUP BY source, ext_id HAVING COUNT(*) > 1)
           ORDER BY ph.source, ph.ext_id, ph.day""").fetchall()
    out = [dict(source=r[0], ext_id=r[1], day=r[2], price_eur=r[3], ppm2=r[4]) for r in rows]
    n = 0
    for i in range(0, len(out), size):
        json.dump(out[i:i + size], open(os.path.join(DIR, f"market-pricehistory-{n}.json"), "w", encoding="utf-8"), ensure_ascii=False)
        n += 1
    print(f"  price_history {len(out)} snímok v {n} chunkoch")
    return n

def publish():
    """Manifest + chunky → git repo → push na GitHub (verejné raw URL pre appku)."""
    import shutil, subprocess, glob
    repo = os.path.join(DIR, "repo")
    if not os.path.isdir(os.path.join(repo, ".git")):
        print("  (repo/ nenájdené — preskakujem publish)"); return
    try:
        for old in glob.glob(os.path.join(repo, "market-listings-*.json")): os.remove(old)
        for old in glob.glob(os.path.join(repo, "market-pricehistory-*.json")): os.remove(old)
        for f in glob.glob(os.path.join(DIR, "market-pricehistory-*.json")): shutil.copy(f, os.path.join(repo, os.path.basename(f)))
        shutil.copy(OUT, os.path.join(repo, "market-data.json"))
        trends_f = os.path.join(DIR, "market-trends.json")
        if os.path.exists(trends_f): shutil.copy(trends_f, os.path.join(repo, "market-trends.json"))
        for f in glob.glob(os.path.join(DIR, "market-listings-*.json")): shutil.copy(f, os.path.join(repo, os.path.basename(f)))
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", f"data {date.today().isoformat()}"], check=False)
        subprocess.run(["git", "-C", repo, "push", "origin", "main"], check=True)
        print("  publikované na GitHub (manifest + chunky).")
    except Exception as e:
        print(f"  publish zlyhal: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()

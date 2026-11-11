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
  python3 scraper.py --mode rolling   # DENNE v cloude (2026-11-11): najnovšie strany + 1/7 celého katalógu
                                      #   (Bazoš, reality.sk, nehnutelnosti.sk, topreality.sk, zoznamrealit.sk)
  python3 scraper.py --mode delta     # len najnovšie strany (rýchly test)
  python3 scraper.py --mode full      # celý Bazoš naraz (hodiny)

PREČO ROLLING: cloud do 11. 11. bežal len `delta` = 8 strán/kategória = 160 najnovších inzerátov (na
Bazoši pár hodín z ~13 500 bytov), full sa od presunu do cloudu nespustil → 37 tis. inzerátov zamrzlo
na last_seen 20. 9. Rolling prejde každý deň 1/7 katalógu, takže každý aktívny inzerát sa uvidí
aspoň raz za týždeň a „aktívny" (videný za 21 dní) znamená naozaj aktívny.
  python3 scraper.py --test           # test parsovania (1 stránka)
Len stdlib — beží aj cez launchd bez pip.

STAV: bazos.sk funguje (jednoduché HTML, obec+PSČ+dátum priamo v inzeráte).
nehnutelnosti.sk/reality.sk načítavajú cez JS/API — doplniť v ďalšej iterácii.
"""
import argparse, json, math, os, re, sqlite3, statistics, sys, time, unicodedata, urllib.error, urllib.request
from datetime import date, datetime
from html import unescape

DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "listings.db")
OUT = os.path.join(DIR, "market-data.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
sys.path.insert(0, DIR)   # portals.py / price_trends.py vedľa skriptu (aj pri `python3 -I`)
import portals            # noqa: E402 — Blocked / OutOfTime (spoločná politika: blokovanie sa neobchádza)
from portals import Blocked, OutOfTime  # noqa: E402
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
GEO_UA = "tri-lipy-scraper/1.1 (+https://github.com/kristiakbohus-hub/tri-lipy-market)"   # Nominatim: identifikácia aplikácie

# ČASOVÝ STROP (2026-11-11): prvý týždenný beh trval > 2 h a job má limit 150 min — pri prekročení GitHub
# job zruší BEZ uloženia, commitu aj ingestu (a „failure" hlásenie sa pri zrušení nepošle). Po DEADLINE sa
# ďalšie stránky nesťahujú (zvyšok obmeny príde v ďalšom behu), HARD_DEADLINE ukončí aj re-verify.
DEADLINE = None
HARD_DEADLINE = None
GEO_EXTRA_MIN = 8          # geokódovanie obcí najviac do HARD_DEADLINE + 8 min


def out_of_time(hard=False, extra=0):
    t = HARD_DEADLINE if hard else DEADLINE
    return t is not None and time.time() > t + extra


# ——— HTTP so slušným opakovaním (2026-11-11) ———
# Dovtedy fetch() bral KAŽDÚ chybu rovnako (3 pokusy, 5/10 s) a vrátil "" — aj HTTP 404 za poslednou
# stranou Bazoša. V týždennej obmene sa tak koniec každej kategórie rátal ako 5 chýb po sebe
# (6 kategórií × 5 = presne tých 30 „stránok sa nepodarilo načítať" z 11. 11.) a stál ~13 min čakania.
RETRY_5XX = (5, 20)        # výpadok / timeout / 5xx: čakanie pred 2. a 3. pokusom
RETRY_429 = (30, 60)       # 429 bez Retry-After; Retry-After portálu sa rešpektuje (max. 120 s)
END_STATUS = (404, 410)    # neexistujúca stránka = koniec katalógu / zmazaný inzerát → neopakuje sa, nie je to chyba


def _retry_after(err, default):
    try:
        return min(120.0, max(1.0, float(err.headers.get("Retry-After"))))
    except Exception:
        return default


def get(url, timeout=20):
    """→ (status, html, final_url). status 0 = výpadok spojenia/timeout aj po opakovaniach.
    404/410 sa neopakuje (či ide o koniec katalógu, rozhodne volajúci). 429 a 5xx sa opakujú s čakaním
    (Retry-After). 403, 429 aj po čakaní a stránka s overením „nie som robot" → Blocked: zdroj sa ZASTAVÍ
    a zapíše do reportu — ochrana sa neobchádza (rovnaká politika ako portals.py)."""
    if out_of_time():
        raise OutOfTime("časový strop — zvyšok obmeny v ďalšom behu")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sk"})
    n5 = n429 = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                h = r.read().decode("utf-8", "replace")
                st, final = r.status, r.geturl()
            break
        except urllib.error.HTTPError as e:
            if e.code in END_STATUS:
                return e.code, "", url
            if e.code == 403:
                raise Blocked("HTTP 403")
            if e.code == 429:
                if n429 >= len(RETRY_429):
                    raise Blocked("HTTP 429 — portál opakovane žiada spomaliť")
                wait = _retry_after(e, RETRY_429[n429]); n429 += 1
                print(f"  HTTP 429 {url} — čakám {wait:.0f} s", file=sys.stderr)
                time.sleep(wait); continue
            if e.code >= 500 and n5 < len(RETRY_5XX):
                time.sleep(_retry_after(e, RETRY_5XX[n5])); n5 += 1; continue
            print(f"  fetch fail {url}: HTTP {e.code}", file=sys.stderr)
            return e.code, "", url
        except Exception as e:  # noqa: BLE001 — timeout, reset spojenia, DNS
            if n5 < len(RETRY_5XX):
                time.sleep(RETRY_5XX[n5]); n5 += 1; continue
            print(f"  fetch fail {url}: {e}", file=sys.stderr)
            return 0, "", url
    low = h[:5000].lower()
    if ("captcha" in low or "challenge-platform" in low or "cf-chl" in low) and len(h) < 60000:
        raise Blocked("stránka s overením (captcha/challenge)")
    return st, h, final


def fetch(url):
    """Len HTML (prázdne pri chybe) — pre staré volania a --test."""
    st, h, _ = get(url)
    return h if st == 200 else ""

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

BAZOS_COUNTER = re.compile(r"Zobrazených\s+(\d[\d\s]*)-(\d[\d\s]*)\s+inzerátov\s+z\s+(\d[\d\s]*)")
BAZOS_BLOCK = '<div class="inzeraty inzeratyflex">'
CAT_FAIL_RUN = 5       # toľko chýb po sebe v kategórii = kategória dnes bez dát (problém v reporte)
SOURCE_FAIL_RUN = 10   # toľko chýb po sebe naprieč kategóriami = portál nedostupný → zdroj sa ukončí


def bazos_counter(html):
    """„Zobrazených 21-60 inzerátov z 2 505" → (veľkosť strany, celkový počet); (None, None) ak chýba."""
    m = BAZOS_COUNTER.search(html or "")
    if not m:
        return None, None
    a, b, n = num(m.group(1)), num(m.group(2)), num(m.group(3))
    return ((b - a + 1) if a and b and b >= a else None), n


def crawl_bazos(mode, day_index=0, stat=None):
    """delta = najnovších 8 strán; full = všetko; rolling = najnovších 8 + každá 7. hlbšia strana podľa dňa
    (za týždeň celý katalóg).

    2026-11-11: Bazoš ukazuje 40 inzerátov na stranu, kód krokoval po 20 → každý deň sa sťahovali prekryté
    okná (2/7 katalógu, každý inzerát 2× týždenne = dvojnásobok požiadaviek). Veľkosť strany aj celkový
    počet sa teraz čítajú zo stránky („Zobrazených 1-40 inzerátov z 2 505"), krok = skutočná veľkosť strany.
    Koniec katalógu = HTTP 404 / stránka bez inzerátov / za celkovým počtom — NIE je to chyba."""
    st = stat if stat is not None else {}
    st.setdefault("pages", 0); st.setdefault("errors", 0)
    newest = 8
    all_l = []
    size = None                       # veľkosť strany (z počítadla; kým nie je známa, 20 = bezpečné prekrytie)
    src_err_run = 0
    try:
        for seg, ptype, deal in BAZOS:
            before = len(all_l)
            p = 0
            err_run = 0
            last = None               # index poslednej strany podľa celkového počtu
            while p < 1000:
                if out_of_time():
                    st["stopped"] = "časový strop — zvyšok obmeny v ďalšom behu"
                    break
                if mode == "delta" and p >= newest:
                    break
                if last is not None and p > last:
                    break             # za koncom katalógu (podľa počítadla) — nič nesťahuj
                if mode == "rolling" and p >= newest and p % 7 != day_index:
                    p += 1
                    continue
                step = size or 20
                url = f"{BASE}{seg}/{p * step}/" if p else f"{BASE}{seg}/"
                code, html, _ = get(url)
                st["pages"] += 1
                if code in END_STATUS:
                    break             # za poslednou stranou Bazoš vracia 404 = koniec katalógu, nie chyba
                if code != 200 or not html:
                    st["errors"] += 1
                    err_run += 1; src_err_run += 1
                    if src_err_run >= SOURCE_FAIL_RUN:   # doterajšie inzeráty sa ponechajú
                        st["error"] = f"{src_err_run} chýb po sebe — portál nedostupný, zber ukončený"
                        st.setdefault("failed", []).append(seg)
                        break
                    if err_run >= CAT_FAIL_RUN:
                        print(f"  bazos {seg}: {err_run} chýb po sebe na strane {p} — kategóriu ukončujem", file=sys.stderr)
                        st.setdefault("failed", []).append(seg)
                        break
                    time.sleep(10); p += 1; continue
                err_run = src_err_run = 0
                psize, total = bazos_counter(html)
                if p == 0 and psize:
                    size = psize
                if total is not None and size:
                    last = max(0, math.ceil(total / size) - 1)
                if html.count(BAZOS_BLOCK) == 0 and p > 0:
                    break             # prázdna stránka = koniec katalógu
                all_l += parse_bazos(html, ptype, deal)
                time.sleep(1.3)
                p += 1
            print(f"  bazos {seg}: {len(all_l) - before} inzerátov (strana {size or '?'} inz.)")
            if st.get("stopped") or st.get("error"):
                break
    except Blocked as e:
        st["stopped" if isinstance(e, OutOfTime) else "blocked"] = str(e)
    st["rows"] = len(all_l)
    if size:
        st["page_size"] = size
    return all_l

# ——— reality.sk (zoznam = JSON-LD ItemList s cenou, výmerou aj obcou; detail netreba) ———
# 2026-11-11 — prečo z 138 strán len 88 nových: (1) detail = 1 request/inzerát a strop 200/deň, takže
# z ~2 800 nových URL sa dostalo len na prvých 200 (hlbší katalóg nikdy); (2) regex ID nebral „-" a „_"
# (ID sú base64url, ~1/6 inzerátov sa ticho zahodila); (3) URL z drobčekovej navigácie (/pozemky/,
# /predaj/) sa brali ako inzeráty a míňali detailový strop; (4) cena „€/m²" pri pozemkoch sa brala ako
# celková; (5) strop 300 strán, kým portál ukáže 416. Zoznam pritom obsahuje všetko potrebné.
# POZOR: reality.sk a nehnutelnosti.sk zdieľajú databázu aj ID inzerátov (United Classifieds) — inzerát,
# ktorý už poznáme z nehnutelnosti.sk, sa len potvrdí tam (last_seen), nevzniká duplicitný riadok.
REALITY = [("pozemky", "pozemok"), ("domy", "dom"), ("byty", "byt")]
JSONLD = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
REALITY_ID = re.compile(r"/([A-Za-z0-9_-]{6,})/?$")
REALITY_PAGE = 24          # inzerátov na stranu
REALITY_MAX_PAGES = 416    # portál ukáže najviac ~10 000 výsledkov na hľadanie; ďalšia strana → 301 na /inzeraty/
REALITY_NEWEST = 4


def _area(v):
    """Výmera z JSON-LD (číslo alebo text „85,5") → float; None ak chýba / nezmysel."""
    if isinstance(v, (int, float)):
        a = float(v)
    else:
        m = re.search(r"\d+(?:[.,]\d+)?", str(v or "").replace("\xa0", "").replace(" ", ""))
        a = float(m.group(0).replace(",", ".")) if m else 0.0
    return a if a > 0 else None


def _ld_items(h):
    for b in JSONLD.findall(h or ""):
        try:
            d = json.loads(re.sub(r"[\x00-\x1f]", " ", b))
        except Exception:
            continue
        t = d.get("@type") if isinstance(d, dict) else None
        if "ItemList" in (t if isinstance(t, list) else [t]):   # len zoznam inzerátov, nie BreadcrumbList
            yield from d.get("itemListElement") or []


def reality_page(h, ptype):
    """→ (inzeráty, celkový počet). Inzerát = dict(ext_id, url, title, ptype, obec, area, price, ppm2);
    price/ppm2 None = cena dohodou."""
    out = []
    for it in _ld_items(h):
        if not isinstance(it, dict):
            continue
        me = it.get("mainEntity") if isinstance(it.get("mainEntity"), dict) else {}
        u = it.get("url") or me.get("url")
        m = REALITY_ID.search(u or "")
        if not m or "reality.sk/" not in u:
            continue
        name = txt(me.get("name"))
        off = me.get("offers")
        off = (off[0] if off else {}) if isinstance(off, list) else (off or {})
        try:
            price = float(off.get("price") or 0)
        except Exception:
            price = 0
        feats = {str(f.get("name")): f.get("value") for f in me.get("amenityFeature") or [] if isinstance(f, dict)}
        area = _area(feats.get("Plocha pozemku" if ptype == "pozemok" else "Úžitková plocha")) \
            or _area((me.get("floorSize") or {}).get("value"))
        obec = re.sub(r"^Reality\s+", "", txt((me.get("address") or {}).get("name"))) or None
        pt = refine_ptype(ptype, name)
        # malá cena = €/m² (pozemky: „210 €/m2"), rovnaké pravidlo ako zoznamrealit
        total, ppm2 = norm_price(pt, price, area, is_unit=bool(price and price < 3000))
        out.append(dict(ext_id=m.group(1), url=u, title=name[:160], ptype=pt, obec=obec, area=area, price=total, ppm2=ppm2))
    tm = re.search(r'class="page-title">.{0,600}?(\d[\d\s]*)\s+pon[úu]k', h or "", re.S)
    return out, (num(tm.group(1)) if tm else None)


def crawl_reality(c, mode, day_index=0, stat=None, nhn_known=()):
    """Zoznam (JSON-LD) → riadky priamo, bez detailu. Rolling = najnovšie 4 strany + každá 7. hlbšia.
    Inzerát známy z nehnutelnosti.sk (rovnaké ID) → len potvrdenie tam; známy bez ceny → len last_seen."""
    st = stat if stat is not None else {}
    st.setdefault("pages", 0); st.setdefault("errors", 0)
    known = {r[0] for r in c.execute("SELECT ext_id FROM listings WHERE source='reality'").fetchall()}
    nhn_known = set(nhn_known)
    full = (mode == "full")
    rows, still, via_nhn = {}, [], []
    src_err_run = 0
    try:
        for cat, ptype in REALITY:
            p = 0
            err_run = 0
            last = REALITY_MAX_PAGES - 1
            while p <= last:
                if out_of_time():
                    st["stopped"] = "časový strop — zvyšok obmeny v ďalšom behu"
                    break
                if mode == "delta" and p >= REALITY_NEWEST: break
                if mode == "rolling" and p >= REALITY_NEWEST and p % 7 != day_index:
                    p += 1; continue
                url = f"https://www.reality.sk/{cat}/predaj/" + (f"?page={p+1}" if p else "")
                code, h, final = get(url)
                st["pages"] += 1
                if code in END_STATUS or (code == 200 and "/inzeraty/" in final):
                    break             # za poslednou dostupnou stranou portál presmeruje na /inzeraty/ = koniec
                if code != 200 or not h:
                    st["errors"] += 1
                    err_run += 1; src_err_run += 1
                    if src_err_run >= SOURCE_FAIL_RUN:
                        st["error"] = f"{src_err_run} chýb po sebe — portál nedostupný, zber ukončený"
                        st.setdefault("failed", []).append(cat)
                        break
                    if err_run >= CAT_FAIL_RUN:
                        st.setdefault("failed", []).append(cat)
                        break
                    time.sleep(10); p += 1; continue
                err_run = src_err_run = 0
                items, total = reality_page(h, ptype)
                if p == 0 and total:
                    pages = math.ceil(total / REALITY_PAGE)
                    last = min(REALITY_MAX_PAGES, pages) - 1
                    if pages > REALITY_MAX_PAGES:
                        st.setdefault("capped", []).append(f"{cat}: {total}")
                if not items:
                    break
                for it in items:
                    e = it["ext_id"]
                    if e in nhn_known:
                        via_nhn.append(e)            # ten istý inzerát z nehnutelnosti.sk → potvrď tam
                    elif it["price"] or it["ppm2"]:
                        rows[e] = dict(source="reality", ext_id=e, url=it["url"], title=it["title"], ptype=it["ptype"],
                                       deal="predaj", obec=it["obec"], psc=None, area_m2=it["area"],
                                       price_eur=it["price"], ppm2=it["ppm2"], listed=None)
                    elif e in known:
                        still.append(e)              # cena dohodou — len potvrď, že žije
                time.sleep(1.2)
                p += 1
                if full and p >= 40: break
            if st.get("stopped") or st.get("error"):
                break
    except Blocked as e:
        st["stopped" if isinstance(e, OutOfTime) else "blocked"] = str(e)
    today = date.today().isoformat()
    bump_seen(c, "reality", still, today)
    bump_seen(c, "nehnutelnosti", via_nhn, today)
    new = sum(1 for e in rows if e not in known)
    st["rows"] = len(rows)
    st["seen"] = len(set(still)) + len(set(via_nhn))
    st["new"] = new
    st["via_nehnutelnosti"] = len(set(via_nhn))
    print(f"  reality.sk: {len(rows)} inzerátov s cenou (nových {new}), potvrdených {len(set(still))}"
          f" + {len(set(via_nhn))} cez nehnutelnosti.sk (rovnaké ID)")
    return list(rows.values())

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
    # okres/kraj priamo z portálu (nehnutelnosti/topreality/zoznamrealit ich uvádzajú) — presnejšie než geokód obce
    for col in ("removed_at", "last_checked", "okres", "kraj"):
        try: c.execute(f"ALTER TABLE listings ADD COLUMN {col} TEXT")
        except Exception: pass
    c.execute("CREATE INDEX IF NOT EXISTS ix_listings_seen ON listings(last_seen)")
    return c

def bump_seen(c, source, ext_ids, today):
    """Inzerát videný na zozname, ktorý už poznáme a nemáme k nemu nový riadok → len potvrď, že žije."""
    ids = list(dict.fromkeys(ext_ids))
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        c.execute(f"UPDATE listings SET last_seen=?, removed_at=NULL WHERE source=? AND ext_id IN ({','.join('?' * len(part))})",
                  [today, source, *part])
    c.commit()

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
        req = urllib.request.Request(u, headers={"User-Agent": GEO_UA})
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

def geocode_ok(c, obec, okres, allow_net):
    """(lat,lng,okres,kraj), počet sieťových dopytov. Pri známom okrese sa hľadá „obec, okres X" (rovnaké
    názvy obcí v rôznych okresoch); cache kľúč „obec|okres". Bez rozpočtu len z cache."""
    if not okres:
        r = c.execute("SELECT lat,lng,okres,kraj FROM geocode WHERE obec=?", (obec,)).fetchone()
        if r or not allow_net: return (tuple(r) if r else (None, None, None, None)), 0
        return geocode(c, obec), 1
    key = f"{obec}|{okres}"
    r = c.execute("SELECT lat,lng,okres,kraj FROM geocode WHERE obec=?", (key,)).fetchone()
    if r: return tuple(r), 0
    r = c.execute("SELECT lat,lng,okres,kraj FROM geocode WHERE obec=?", (obec,)).fetchone()
    if r and r[0] is not None and (r[2] or "") == okres: return tuple(r), 0   # plain cache sedí s okresom
    if not allow_net: return (None, None, okres, None), 0
    lat = lng = kraj = None
    try:
        import urllib.parse
        u = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": f"{obec}, okres {okres}, Slovensko", "format": "jsonv2", "addressdetails": 1, "limit": 1})
        req = urllib.request.Request(u, headers={"User-Agent": GEO_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            arr = json.loads(resp.read().decode("utf-8", "replace"))
            if arr:
                lat = float(arr[0]["lat"]); lng = float(arr[0]["lon"])
                ad = arr[0].get("address", {}) or {}
                kraj = re.sub(r"\s*kraj$", "", (ad.get("region") or ad.get("state") or ""), flags=re.I).strip() or None
        time.sleep(1.1)
    except Exception as e:
        print(f"  geocode fail {key}: {e}", file=sys.stderr)
    c.execute("INSERT OR REPLACE INTO geocode(obec,lat,lng,okres,kraj) VALUES(?,?,?,?,?)", (key, lat, lng, okres, kraj)); c.commit()
    return (lat, lng, okres, kraj), 1

_DEMAND = re.compile(r"(hladam|hladame|kupim|kupime|zhanam|zhaname)")
_NOT_DEMAND = re.compile(r"(najomc|najomnik|kupc|kupujuc|zaujemc)")
_RENT = re.compile(r"(prenaj|podnaj|najom|spolubyv)")


def demand_deal(title, deal):
    """Inzerát „hľadám / kúpim" je DOPYT, nie ponuka (2026-11-11: ~100 z 13 956, z toho 25 dopytov na kúpu) —
    kazil mediány €/m² a príležitosti. „Hľadáme nájomcu / kupca" je naopak ponuka. Rovnaké pravidlo má appka."""
    t = unicodedata.normalize("NFD", title or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    if _DEMAND.search(t) and not _NOT_DEMAND.search(t):
        return "dopyt_najom" if _RENT.search(t) else "dopyt"
    return deal


def upsert(c, rows, today):
    for r in rows:
        r["deal"] = demand_deal(r.get("title"), r.get("deal"))
        ex = c.execute("SELECT price_eur FROM listings WHERE source=? AND ext_id=?", (r["source"], r["ext_id"])).fetchone()
        if ex:
            old_price = ex[0]
            c.execute("UPDATE listings SET url=?,title=?,ptype=?,deal=?,obec=?,psc=?,area_m2=?,price_eur=?,ppm2=?,last_seen=?,removed_at=NULL,"
                      "okres=COALESCE(?,okres),kraj=COALESCE(?,kraj) WHERE source=? AND ext_id=?",
                      (r["url"], r["title"], r["ptype"], r["deal"], r["obec"], r["psc"], r["area_m2"], r["price_eur"], r["ppm2"], today,
                       r.get("okres"), r.get("kraj"), r["source"], r["ext_id"]))
            # price_history: snímka LEN pri zmene ceny (per-inzerát krivka pohybu ceny v čase)
            if r["price_eur"] is not None and old_price is not None and abs(float(r["price_eur"]) - float(old_price)) > 0.5:
                c.execute("INSERT OR REPLACE INTO price_history(source,ext_id,day,price_eur,ppm2) VALUES(?,?,?,?,?)",
                          (r["source"], r["ext_id"], today, r["price_eur"], r["ppm2"]))
        else:
            c.execute("INSERT INTO listings(source,ext_id,url,title,ptype,deal,obec,psc,area_m2,price_eur,ppm2,first_seen,last_seen,first_price,okres,kraj) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["source"], r["ext_id"], r["url"], r["title"], r["ptype"], r["deal"], r["obec"], r["psc"],
                       r["area_m2"], r["price_eur"], r["ppm2"], r.get("listed") or today, today, r["price_eur"],
                       r.get("okres"), r.get("kraj")))
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
        if out_of_time(hard=True):
            print("  re-verify: časový strop — zvyšok pri ďalšom behu", file=sys.stderr); break
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

ACTIVE_DAYS = 21   # „aktívny" = videný za posledné 3 týždne (pri týždennej obmene sa aktívny uvidí ≥3×)

def _dedup_key(obec, pt, dl, area, price):
    o = re.sub(r"[^a-z0-9]", "", (obec or "").lower())
    return (o, pt, dl, round((area or 0) / 10), round((price or 0) / 1000))

def build_market_data(c, today):
    # LEN aktívne inzeráty (nezmazané, videné za ACTIVE_DAYS). Dovtedy sa mediány aj príležitosti
    # počítali zo VŠETKÝCH inzerátov, aké kedy boli videné — aj z mesiace mŕtvych a zmazaných
    # (151 z 300 príležitostí bolo naposledy videných 20. 9.).
    raw = c.execute(
        "SELECT ptype,deal,obec,area_m2,price_eur,ppm2,first_seen,last_seen,first_price,url,title,source FROM listings "
        "WHERE ppm2 IS NOT NULL AND obec IS NOT NULL AND removed_at IS NULL AND last_seen >= date(?, ?) "
        "ORDER BY CASE source WHEN 'nehnutelnosti' THEN 0 WHEN 'topreality' THEN 1 WHEN 'reality' THEN 2 ELSE 3 END",
        (today, f"-{ACTIVE_DAYS} day")).fetchall()
    # ten istý inzerát na viacerých portáloch sa ráta raz (obec + typ + výmera ~10 m² + cena ~1 000 €)
    seen_k, rows = set(), []
    for r in raw:
        k = _dedup_key(r[2], r[0], r[1], r[3], r[4])
        if k in seen_k: continue
        seen_k.add(k); rows.append(r)
    groups = {}
    for pt, dl, obec, area, price, ppm2, fs, ls, fp, url, title, src in rows:
        groups.setdefault((obec, pt, dl), []).append(ppm2)
    index, med_by = [], {}
    for (obec, pt, dl), vals in groups.items():
        if len(vals) < 3: continue
        vals.sort(); med = statistics.median(vals)
        med_by[(obec, pt, dl)] = med
        index.append(dict(okres=obec, obec=None, ptype=pt, deal=dl, day=today,
                          median=round(med, 1), p25=round(vals[len(vals)//4], 1), p75=round(vals[3*len(vals)//4], 1), cnt=len(vals)))
    opps = []
    for pt, dl, obec, area, price, ppm2, fs, ls, fp, url, title, src in rows:
        if dl in ("dopyt", "dopyt_najom"):
            continue                                   # dopyt „hľadám / kúpim" nie je ponuka
        if ls < today and (datetime.fromisoformat(today) - datetime.fromisoformat(ls)).days > 7:
            continue                                   # príležitosť len z inzerátu videného tento týždeň
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
            opps.append(dict(source=src, ext_id=None, url=url, title=title, ptype=pt, deal=dl,
                             okres=obec, obec=obec, area=area, price=price, ppm2=ppm2,
                             first_seen=fs, last_seen=ls, dom=dom, drop_pct=drop, below_pct=below, flags=",".join(flags)))
    opps.sort(key=lambda o: (o["below_pct"] + o["drop_pct"]), reverse=True)
    return dict(generated=today, index=index, opportunities=opps[:300],
                meta=dict(generated=today, counts=dict(listings=len(rows), index=len(index), opps=len(opps)),
                          active_days=ACTIVE_DAYS, dedup_removed=len(raw) - len(rows)))

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
    ap.add_argument("--mode", choices=["delta", "full", "rolling"], default="delta")
    ap.add_argument("--only", default="", help="čiarkou oddelené zdroje (test): bazos,reality,nehnutelnosti,topreality,zoznamrealit")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    today = date.today().isoformat()
    if a.test:
        got = parse_bazos(fetch(f"{BASE}predam/pozemok/"), "pozemok", "predaj")
        print(f"TEST: {len(got)} inzerátov z 1. stránky. Ukážka:")
        for g in got[:4]: print("  ", {k: g[k] for k in ("title", "obec", "psc", "area_m2", "price_eur", "ppm2", "listed")})
        return
    c = db()
    t0 = time.time()
    global DEADLINE, HARD_DEADLINE
    budget_min = float(os.environ.get("SCRAPE_BUDGET_MIN", "0") or 0)
    if budget_min > 0:
        DEADLINE = t0 + budget_min * 60
        HARD_DEADLINE = DEADLINE + float(os.environ.get("REVERIFY_BUDGET_MIN", "10")) * 60
    day_index = date.today().toordinal() % 7
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    want = lambda name: not only or name in only
    report = {"date": today, "mode": a.mode, "day_index": day_index, "sources": {}}

    def run(name, fn):
        st = {"pages": 0, "rows": 0}
        t = time.time()
        try:
            got = fn(st)
        except Exception as e:                       # jeden zdroj nezhodí celý beh
            print(f"  {name} ZLYHAL: {e}", file=sys.stderr)
            st["error"] = str(e)[:200]; got = []
        st["seconds"] = round(time.time() - t)
        report["sources"][name] = st
        return got

    portals.DEADLINE = DEADLINE
    rows = []
    if want("bazos"):
        rows += run("bazos", lambda st: crawl_bazos(a.mode, day_index, st))
    okk = portals.okres_kraj_map()

    def portal(name, fn):
        def go(st):
            got, seen, pst = fn()
            st.update(pst)
            if seen: bump_seen(c, name, seen, today)
            return got
        return run(name, go)

    portal_mode = a.mode != "full"
    if portal_mode and want("nehnutelnosti"):
        rows += portal("nehnutelnosti", lambda: portals.crawl_nehnutelnosti(day_index, refine_ptype))
    if want("reality"):
        # reality.sk = tá istá databáza a ID ako nehnutelnosti.sk → beží PO nej, inzeráty videné tam (aj dnes)
        # len potvrdí a nerobí z nich druhý (duplicitný) riadok
        nhn = {r[0] for r in c.execute("SELECT ext_id FROM listings WHERE source='nehnutelnosti'")}
        nhn |= {r["ext_id"] for r in rows if r.get("source") == "nehnutelnosti"}
        rows += run("reality", lambda st: crawl_reality(c, a.mode, day_index, st, nhn))
    if portal_mode and want("topreality"):
        rows += portal("topreality", lambda: portals.crawl_topreality(day_index, refine_ptype, okk))
    if portal_mode and want("zoznamrealit"):
        known = {r[0] for r in c.execute("SELECT ext_id FROM listings WHERE source='zoznamrealit'")}
        rows += portal("zoznamrealit", lambda: portals.crawl_zoznamrealit(day_index, refine_ptype, known, okk))
    for k, v in report["sources"].items():
        v["error_rate"] = error_rate(v)
        print(f"  [{k}] strán {v.get('pages')} · inzerátov {v.get('rows')} · potvrdených {v.get('seen', 0)}"
              f" · chýb {v.get('errors', 0)} ({v['error_rate']} %)"
              f"{' · BLOKOVANÉ: ' + v['blocked'] if v.get('blocked') else ''}{' · ZASTAVENÉ: ' + v['stopped'] if v.get('stopped') else ''}"
              f"{' · CHYBA: ' + v['error'] if v.get('error') else ''} · {v['seconds']} s")
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
    report["active_listings"] = data["meta"]["counts"]["listings"]
    report["seen_today"] = c.execute("SELECT COUNT(*) FROM listings WHERE last_seen=?", (today,)).fetchone()[0]
    report["new_today"] = c.execute("SELECT COUNT(*) FROM listings WHERE first_seen=?", (today,)).fetchone()[0]
    report["by_source_active"] = dict(c.execute(
        "SELECT source, COUNT(*) FROM listings WHERE removed_at IS NULL AND last_seen >= date(?, ?) GROUP BY source",
        (today, f"-{ACTIVE_DAYS} day")).fetchall())
    data["listings_chunks"] = write_listings_chunks(c, today)
    data["pricehistory_chunks"] = write_price_history_chunks(c)
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    report["seconds"] = round(time.time() - t0)
    report["chunks"] = data["listings_chunks"]
    report["problems"] = health_problems(report)
    report["info"] = health_info(report)
    report["summary"] = [source_summary(k, v) for k, v in report["sources"].items()]
    report["ok"] = not report["problems"]
    # report behu ide do repa (verejne čitateľný) — bez neho sa dalo zistiť stav zberu len z logov
    json.dump(report, open(os.path.join(DIR, "scrape-report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Hotovo: {data['meta']['counts']}, chunkov {data['listings_chunks']} → {OUT}")
    # TG alerty scrapera vypnuté — konsolidované do jedného ranného brífingu (deal_radar_tg.py --digest).
    # Zapnú sa len s explicitným --tg.
    if "--tg" in sys.argv:
        notify_price_alerts(data)
    publish()

# Prahy zdravia behu (rolling, celé SR). Pod nimi niečo nie je v poriadku — pošle sa varovanie do Telegramu.
MIN_ROWS = {"bazos": 3000, "nehnutelnosti": 1500, "topreality": 1500, "reality": 1, "zoznamrealit": 1}
MIN_SEEN_TODAY = 8000
# Chybovosť: pár prechodných výpadkov za deň je normálny šum, nie problém (dovtedy pevný prah 20 chýb
# a koniec katalógu rátaný ako chyba → Telegram „problém" každý deň). Problém = viac ako 5 % požiadaviek
# (a aspoň 5 chýb), alebo celá kategória bez dát (CAT_FAIL_RUN chýb po sebe).
ERR_RATE_MAX = 5.0
ERR_MIN_COUNT = 5


def error_rate(st):
    """Chybovosť zdroja v % zo všetkých požiadaviek (strany zoznamu + detaily)."""
    req = (st.get("pages") or 0) + (st.get("detail") or 0)
    return round(100.0 * (st.get("errors") or 0) / req, 1) if req else 0.0


def health_problems(report):
    """Zoznam problémov behu (prázdny = v poriadku). Číta ho workflow a pri probléme pošle Telegram."""
    out = []
    if report.get("mode") != "rolling":
        return out
    for name, st in report.get("sources", {}).items():
        errs, rate = st.get("errors") or 0, error_rate(st)
        if st.get("blocked"):
            out.append(f"{name}: portál zablokoval prístup ({st['blocked']})")
        elif st.get("error"):
            out.append(f"{name}: zlyhal ({st['error'][:120]})")
        elif st.get("stopped"):
            pass                           # zastavené časovým stropom = plán, nie chyba (zvyšok v ďalšom behu) → info
        elif (st.get("rows") or 0) + (st.get("seen") or 0) < MIN_ROWS.get(name, 1):
            out.append(f"{name}: len {(st.get('rows') or 0) + (st.get('seen') or 0)} inzerátov (očakávané ≥ {MIN_ROWS.get(name, 1)})")
        if st.get("failed") and not st.get("error"):
            out.append(f"{name}: kategórie bez dát ({', '.join(st['failed'])}) — {CAT_FAIL_RUN} chýb po sebe")
        if errs >= ERR_MIN_COUNT and rate > ERR_RATE_MAX:
            out.append(f"{name}: {errs} z {(st.get('pages') or 0) + (st.get('detail') or 0)} požiadaviek zlyhalo ({rate} %)")
    stopped = any(st.get("stopped") for st in report.get("sources", {}).values())
    if (report.get("seen_today") or 0) < MIN_SEEN_TODAY and not stopped:
        out.append(f"dnes videných len {report.get('seen_today')} inzerátov (očakávané ≥ {MIN_SEEN_TODAY})")
    return out


def health_info(report):
    """Poznámky, ktoré NIE sú problém (bez Telegramu): časový strop, prechodné chyby pod prahom, orezané hľadania."""
    out = []
    for name, st in report.get("sources", {}).items():
        if st.get("stopped"):
            out.append(f"{name}: zastavené ({st['stopped']}) po {st.get('pages', 0)} stranách")
        errs, rate = st.get("errors") or 0, error_rate(st)
        if errs and not (errs >= ERR_MIN_COUNT and rate > ERR_RATE_MAX) and not st.get("failed"):
            out.append(f"{name}: {errs} prechodných chýb ({rate} %, prah {ERR_RATE_MAX:g} %)")
        if st.get("capped"):
            out.append(f"{name}: portál ukáže len časť výsledkov — {len(st['capped'])} hľadaní orezaných")
    return out


def source_summary(name, st):
    """Jeden riadok na zdroj do scrape-report.json (pre ľudí; stroje čítajú „sources")."""
    state = ("BLOKOVANÉ" if st.get("blocked") else "CHYBA" if st.get("error") else
             "zastavené časovým stropom" if st.get("stopped") else "ok")
    return (f"{name}: {state} · {st.get('pages', 0)} strán · {st.get('rows', 0)} inzerátov · "
            f"{st.get('seen', 0)} potvrdených · chybovosť {error_rate(st)} % · {round((st.get('seconds') or 0) / 60, 1)} min")


def write_listings_chunks(c, today, size=3000):
    """Všetky dnes videné inzeráty (s lat/lng z geokódu obce) → market-listings-<i>.json chunky."""
    rows = c.execute("SELECT source,ext_id,url,title,ptype,deal,obec,psc,area_m2,price_eur,ppm2,first_seen,last_seen,first_price,removed_at,okres,kraj FROM listings WHERE last_seen=? OR removed_at=?", (today, today)).fetchall()
    pairs = sorted({(r[6], r[15]) for r in rows if r[6]}, key=lambda x: (x[0], x[1] or ""))
    geo = {}
    budget = int(os.environ.get("GEOCODE_BUDGET", "400"))   # nové dopyty na Nominatim za beh (1/s); zvyšok ďalší deň
    for i, (ob, ok) in enumerate(pairs):
        # geokód (1 dopyt/s) je ohraničený aj časom: po HARD_DEADLINE + GEO_EXTRA_MIN už len z cache, aby
        # zostal čas na commit a ingest do appky v rámci limitu jobu (zvyšok obcí sa dopočíta ďalší deň)
        geo[(ob, ok)], used = geocode_ok(c, ob, ok, budget > 0 and not out_of_time(hard=True, extra=GEO_EXTRA_MIN * 60))
        budget -= used
        if i and i % 200 == 0: print(f"  geokód obcí {i}/{len(pairs)} (zostáva dopytov {max(budget, 0)})")
    out = []
    for r in rows:
        la, ln, g_okres, g_kraj = geo.get((r[6], r[15]), (None, None, None, None))
        okres, kraj = r[15] or g_okres, r[16] or g_kraj
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
    print(f"  inzerátov {len(out)} v {n} chunkoch (obcí {len(pairs)})")
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

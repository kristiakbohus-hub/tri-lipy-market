#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Realitné portály pre denný scraper (len stdlib, obyčajné HTTP požiadavky).

Overené 2026-11-11: nehnutelnosti.sk, topreality.sk aj zoznamrealit.sk dnes vracajú bežnú HTML
stránku aj obyčajnej požiadavke — simulovaný prehliadač (Playwright) netreba. Ak by portál začal
blokovať (HTTP 403/429, overenie „nie som robot", prázdna stránka), zber ho PRESKOČÍ a zapíše to do
reportu behu. Ochrana sa NEobchádza (rozhodnutie vlastníka 2026-11-11: pridať všetky portály, ale
bez obchádzania).

TÝŽDENNÁ OBMENA: každý deň sa prejde 1/7 katalógu SR (podľa okresu alebo stránky) + CELÝ Žilinský kraj
(11 okresov, od 2026-11-27 na každom portáli: nehnutelnosti po okresoch, topreality filter kraja,
zoznamrealit po okresoch), takže každý aktívny inzerát v SR sa uvidí aspoň raz za týždeň a v kraji denne.

Každý crawl vracia (rows, seen_ids, stat):
  rows     — inzeráty s cenou na zápis (rovnaký tvar ako bazos: source, ext_id, url, title, ptype,
             deal, obec, psc, area_m2, price_eur, ppm2, listed, okres, kraj)
  seen_ids — ext_id videné na zozname, pre ktoré nie je nový riadok (len posun last_seen)
  stat     — počty pre report behu (stránky, inzeráty, chyby, blokované, orezané)
"""
import json
import math
import re
import time
import unicodedata
import urllib.error
import urllib.request
from html import unescape

import os as _os0, sys as _sys0
_sys0.path.insert(0, _os0.path.dirname(_os0.path.abspath(__file__)))
import polite  # noqa: E402 — čestný User-Agent, robots.txt, pauzy (oprava 10, 2026-11-28)

# Do 27. 11. 2026 tu bol User-Agent prehliadača Chrome. Vlastník 28. 11.: žiadne falšovanie prehliadača ani UA.
# Overené 28. 11.: nehnutelnosti, topreality, reality aj zoznamrealit čestnému UA vracajú bežné stránky (200).
UA = polite.UA
JSONLD = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

# 79 okresov SR (názov ako v appke / Nominatime) → kraj
OKRESY = {
    "Bratislavský": ["Bratislava I", "Bratislava II", "Bratislava III", "Bratislava IV", "Bratislava V",
                     "Malacky", "Pezinok", "Senec"],
    "Trnavský": ["Dunajská Streda", "Galanta", "Hlohovec", "Piešťany", "Senica", "Skalica", "Trnava"],
    "Trenčiansky": ["Bánovce nad Bebravou", "Ilava", "Myjava", "Nové Mesto nad Váhom", "Partizánske",
                    "Považská Bystrica", "Prievidza", "Púchov", "Trenčín"],
    "Nitriansky": ["Komárno", "Levice", "Nitra", "Nové Zámky", "Šaľa", "Topoľčany", "Zlaté Moravce"],
    "Žilinský": ["Bytča", "Čadca", "Dolný Kubín", "Kysucké Nové Mesto", "Liptovský Mikuláš", "Martin",
                 "Námestovo", "Ružomberok", "Turčianske Teplice", "Tvrdošín", "Žilina"],
    "Banskobystrický": ["Banská Bystrica", "Banská Štiavnica", "Brezno", "Detva", "Krupina", "Lučenec",
                        "Poltár", "Revúca", "Rimavská Sobota", "Veľký Krtíš", "Zvolen", "Žarnovica",
                        "Žiar nad Hronom"],
    "Prešovský": ["Bardejov", "Humenné", "Kežmarok", "Levoča", "Medzilaborce", "Poprad", "Prešov", "Sabinov",
                  "Snina", "Stará Ľubovňa", "Stropkov", "Svidník", "Vranov nad Topľou"],
    "Košický": ["Gelnica", "Košice I", "Košice II", "Košice III", "Košice IV", "Košice-okolie", "Michalovce",
                "Rožňava", "Sobrance", "Spišská Nová Ves", "Trebišov"],
}
OKRES_LIST = [(o, k) for k, os_ in OKRESY.items() for o in os_]
# Oblasť appky = CELÝ Žilinský kraj (11 okresov) denne celý na každom portáli; ostatné okresy SR raz za týždeň.
# 2026-11-27: dovtedy denne len Čadca, KNM, Žilina, Bytča, Ružomberok a len na nehnutelnosti.sk —
# topreality, zoznamrealit a Bazoš išli len celoštátne (najnovšie strany + 1/7), takže zvyšok kraja
# (Martin, Liptov, Orava, Turiec) sa videl raz za týždeň a pokles ceny prišiel až o niekoľko dní.
KRAJ = "Žilinský"
KRAJ_OKRESY = OKRESY[KRAJ]
PRIORITY_OKRESY = set(KRAJ_OKRESY)


def _ascii(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", _ascii(s).lower()).strip("-")


def norm_okres(s):
    """„okres Košice - okolie" / „Košice - okolie" → „Košice-okolie"; „okres Senec" → „Senec"."""
    o = re.sub(r"^okres\s+", "", (s or "").strip(), flags=re.I)
    o = re.sub(r"\s*-\s*", "-", o)
    return o or None


def todays_okresy(day_index):
    """Okresy na dnes: prioritné (kraj appky) NAJPRV — keby zber narazil na časový strop, kraj je hotový —
    potom každý 7. z ostatných podľa poradia (týždenná obmena)."""
    if SMOKE:
        return [("Čadca", "Žilinský")]
    pri = [(o, k) for o, k in OKRES_LIST if o in PRIORITY_OKRESY]
    return pri + [(o, k) for i, (o, k) in enumerate(OKRES_LIST) if o not in PRIORITY_OKRESY and i % 7 == day_index]


# Skúšobný beh (PORTALS_SMOKE=1): jeden okres, 2 strany, 5 detailov — na overenie parsovania pred nasadením.
import os as _os
SMOKE = bool(_os.environ.get("PORTALS_SMOKE"))


class Blocked(Exception):
    """Portál blokuje (403/429/overenie „nie som robot"/robots.txt) → zdroj sa v behu zastaví, neobchádza sa.
    retry_after = koľko sekúnd portál žiada počkať (hlavička Retry-After), ak to povedal."""
    def __init__(self, msg="", retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class OutOfTime(Blocked):
    """Časový strop behu (nastaví scraper.main) — zvyšok obmeny príde v ďalšom behu, nie je to chyba."""


DEADLINE = None


def fetch(url, timeout=30, tries=3):
    """Vráti (status, html). Pri 403/429 alebo stránke s overením „nie som robot" hodí Blocked.
    Výpadok spojenia / 5xx sa skúsi znova (3×, čakanie 5 s, 15 s) — jeden výpadok nesmie stratiť okres."""
    if DEADLINE is not None and time.time() > DEADLINE:
        raise OutOfTime("časový strop — zvyšok obmeny v ďalšom behu")
    if not polite.allowed(url):
        why = "nedostupný" if polite.robots_state(url) == "unreachable" else "zakazuje túto adresu"
        raise Blocked(f"robots.txt {why} — zber portálu ukončený")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sk", "Accept": "text/html"})
    waited429 = False
    i = 0
    while i < tries:
        polite.pace(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                h = r.read().decode("utf-8", "replace")
                st = r.status
            break
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise Blocked("HTTP 403")
            if e.code == 429:
                # slušne (oprava 10): raz počkať (Retry-After, inak 60 s), doménu spomaliť; druhé 429 = koniec portálu
                ra = polite.retry_after(e)
                polite.slow_down(url)
                if waited429 or (ra is not None and ra > 300) or \
                        (DEADLINE is not None and time.time() + (ra or 60) > DEADLINE):
                    raise Blocked("HTTP 429 — portál žiada spomaliť", retry_after=ra)
                waited429 = True
                time.sleep(max(ra or 0, 60.0))
                continue
            if e.code >= 500 and i < tries - 1:
                time.sleep(5 + 10 * i); i += 1; continue
            if e.code == 503:
                raise Blocked("HTTP 503")
            return e.code, ""
        except Exception:  # noqa: BLE001 — timeout, reset spojenia, DNS
            if i < tries - 1:
                time.sleep(5 + 10 * i); i += 1; continue
            return 0, ""
    low = h[:4000].lower()
    if ("captcha" in low or "challenge-platform" in low or "cf-chl" in low) and len(h) < 60000:
        raise Blocked("stránka s overením (captcha/challenge)")
    return st, h


def _num(s):
    s = re.sub(r"[^\d]", "", str(s or ""))
    return int(s) if s else None


def _ppm2(price, area):
    return round(price / area, 1) if price and area and area > 5 else None


def _stat():
    return {"pages": 0, "rows": 0, "seen": 0, "errors": 0, "blocked": None, "capped": []}


# ——— nehnutelnosti.sk ————————————————————————————————————————————————
# Zoznam: JSON-LD SearchResultsPage.mainEntity.itemListElement (url, cena, výmera, kategória) + v RSC
# flight dátach pri každom inzeráte location {city, district, county}. Detail netreba.
# Portál ukáže najviac 33 strán (990 inzerátov) na hľadanie → hľadá sa po OKRESOCH.
NHN_CATS = [("pozemky", "pozemok", "predaj"), ("byty", "byt", "predaj"), ("domy", "dom", "predaj"),
            ("byty", "byt", "prenajom"), ("domy", "dom", "prenajom")]
NHN_MAX_PAGES = 33


def _nhn_locations(html):
    """{id: {city, district, county, unitPrice}} z RSC flight dát (JS string escapovaný JSON)."""
    out = {}
    for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S):
        try:
            txt = json.loads('"' + chunk + '"')
        except Exception:
            continue
        for m in re.finditer(r'"id":"([A-Za-z0-9_-]{6,})","title":"[^"]*","location":\{([^{}]*)\}', txt):
            loc = {}
            for k in ("city", "district", "county"):
                km = re.search(rf'"{k}":"([^"]*)"', m.group(2))
                loc[k] = km.group(1) if km else None
            out[m.group(1)] = loc
    return out


def _nhn_page(html):
    """→ (items, total) z jednej stránky výsledkov."""
    total = 0
    items = []
    for b in JSONLD.findall(html):
        try:
            d = json.loads(b)
        except Exception:
            continue
        for x in d.get("@graph", [d]) if isinstance(d, dict) else []:
            if x.get("@type") != "SearchResultsPage":
                continue
            m = re.search(r"z\s+([\d\s]+)\s+v ponuke", x.get("description") or "")
            total = _num(m.group(1)) if m else 0
            for li in (x.get("mainEntity") or {}).get("itemListElement", []):
                it = li.get("item") or {}
                eid = (it.get("@id") or "").rsplit("/", 1)[-1]
                if not eid:
                    continue
                price = _num((it.get("offers") or {}).get("price") or (it.get("priceSpecification") or {}).get("price"))
                area = (it.get("floorSize") or {}).get("value")
                items.append({"id": eid, "url": it.get("url"), "name": it.get("name") or "",
                              "category": it.get("category") or "", "price": price or None,
                              "area": float(area) if area else None, "valid_from": (it.get("validFrom") or "")[:10] or None})
    return items, total


def crawl_nehnutelnosti(day_index, refine_ptype, sleep=1.5, max_pages=NHN_MAX_PAGES):
    rows, seen, st = [], [], _stat()
    try:
        for okres, kraj in todays_okresy(day_index):
            loc = "okres-" + slug(okres)
            for cat, ptype, deal in NHN_CATS:
                pages = 1
                pg = 1
                while pg <= (min(pages, 2) if SMOKE else pages):
                    url = f"https://www.nehnutelnosti.sk/vysledky/{cat}/{loc}/{deal}" + (f"?page={pg}" if pg > 1 else "")
                    code, h = fetch(url)
                    time.sleep(sleep)
                    st["pages"] += 1
                    if code != 200 or not h:
                        st["errors"] += 1
                        break
                    items, total = _nhn_page(h)
                    if pg == 1:
                        pages = min(max_pages, max(1, math.ceil((total or 0) / 30)))
                        if total and total > max_pages * 30:
                            st["capped"].append(f"{okres}/{cat}/{deal}: {total}")
                    if not items:
                        break
                    locs = _nhn_locations(h)
                    for it in items:
                        lo = locs.get(it["id"], {})
                        pt = refine_ptype(ptype, it["name"])
                        rows.append(dict(source="nehnutelnosti", ext_id=it["id"], url=it["url"], title=it["name"][:160],
                                         ptype=pt, deal=deal, obec=lo.get("city") or None, psc=None,
                                         area_m2=it["area"], price_eur=it["price"],
                                         ppm2=_ppm2(it["price"], it["area"]), listed=it["valid_from"],
                                         okres=norm_okres(lo.get("district")) or okres, kraj=kraj))
                        st["rows"] += 1
                    pg += 1
    except Blocked as e:
        st["stopped" if isinstance(e, OutOfTime) else "blocked"] = str(e)
    return rows, seen, st


# ——— topreality.sk ——————————————————————————————————————————————————
# Karty v HTML: data-ga4-container-* (id, názov, cena, kategória, obec) + text „(okres)" a „NNN m2".
TR_CATS = [(800, "pozemok", "predaj"), (100, "byt", "predaj"), (200, "dom", "predaj")]
TR_DAILY_PAGES = 4


def _tr_cards(html):
    """Karta: atribúty data-ga4-container-* + span.location-city / span.location-district „(okres)" +
    span.value „N m<sup>2</sup>" (pozemok = area-plot, inak úžitková). Mena „EUR/m2" = cena je za m²."""
    out = []
    starts = list(re.finditer(r'<div class="col-lg-12[^"]*item-col[^"]*"([^>]*)>', html))
    for k, m in enumerate(starts):
        attrs = dict(re.findall(r'data-ga4-container-([a-z_0-9]+)="([^"]*)"', m.group(1)))
        eid = attrs.get("item_id_generic")
        if not eid:
            continue
        # LEN po začiatok ďalšej karty (2026-11-27): pevné okno 9000 znakov pretekalo do susednej karty —
        # karta bez „(okres)" (mesto = okresné mesto) dostala okres aj výmeru cudzieho inzerátu (Bytča → „Čadca")
        end = starts[k + 1].start() if k + 1 < len(starts) else m.end() + 9000
        body = html[m.end():min(end, m.end() + 9000)]
        href = re.search(r'href="(https://www\.topreality\.sk/[^"]*-r' + eid + r'\.html)"', body)
        city = re.search(r'class="location-city">([^<]+)<', body)
        dist = re.search(r'class="location-district">\(([^)<]+)\)<', body)
        areas = re.findall(r'class="tooltip-area ([a-z-]+)[^"]*"[\s\S]{0,400}?class="value">\s*([\d\s]+)\s*m<sup>', body)
        plot = next((_num(v) for k, v in areas if "plot" in k), None)
        other = next((_num(v) for k, v in areas if "plot" not in k), None)
        out.append({"id": eid, "url": href.group(1) if href else None, "name": unescape(attrs.get("item_name", "")),
                    "price": _num(attrs.get("price")) or None, "unit": "m2" in (attrs.get("currency") or "").lower(),
                    "obec": unescape(city.group(1)).strip() if city else (unescape(attrs.get("item_category3", "")) or None),
                    "category": unescape(attrs.get("item_category2", "")),
                    "okres": unescape(dist.group(1)).strip() if dist else None, "plot": plot, "usable": other})
    return out


TR_KRAJ_ID = "c500-Žilinský kraj"     # id kraja z našepkávača portálu (/user/new_estate/searchAjax.php), overené 2026-11-27
TR_KRAJ_MAX_PAGES = 400               # poistka; kraj má ~1 800 pozemkov = ~115 strán


def _tr_found(html):
    """„<strong>1 791</strong> inzerátov" → 1791 (None ak chýba)."""
    m = re.search(r'found-items">\s*<strong>([\d\s\xa0]+)</strong>', html or "")
    return _num(m.group(1)) if m else None


def _tr_row(c, ptype, deal, okres_kraj, in_kraj, refine_ptype):
    """Karta topreality → riadok inzerátu (rovnaký tvar ako ostatné portály)."""
    okres = norm_okres(c["okres"])
    if not okres and c["obec"] in okres_kraj:  # karta bez „(okres)" = obec je samotné okresné mesto
        okres = c["obec"]
    pt = refine_ptype(ptype, c["name"])
    area = (c["plot"] or c["usable"]) if ptype == "pozemok" else (c["usable"] or c["plot"])
    area = float(area) if area else None
    if c["unit"]:                      # cena za m² → celková = €/m² × výmera
        price, ppm2 = (round(c["price"] * area) if c["price"] and area else None), (float(c["price"]) if c["price"] else None)
    else:
        price, ppm2 = c["price"], _ppm2(c["price"], area)
    return dict(source="topreality", ext_id=c["id"], url=c["url"], title=c["name"][:160], ptype=pt,
                deal=deal, obec=c["obec"], psc=None, area_m2=area, price_eur=price,
                ppm2=ppm2, listed=None, okres=okres, kraj=okres_kraj.get(okres) or (KRAJ if in_kraj else None))


def crawl_topreality(day_index, refine_ptype, okres_kraj, sleep=1.3):
    """1) CELÝ Žilinský kraj denne (hľadanie s filtrom kraja, všetky strany), 2) celé SR: najnovšie strany
    + každá 7. hlbšia (týždenná obmena). Inzerát z kraja sa v celoštátnej časti už druhýkrát nezapíše."""
    rows, seen, st = [], [], _stat()
    st["kraj_pages"] = 0
    st["kraj_rows"] = 0
    got = set()

    def add(c, ptype, deal, in_kraj):
        if not c["url"] or c["id"] in got:
            return
        got.add(c["id"])
        rows.append(_tr_row(c, ptype, deal, okres_kraj, in_kraj, refine_ptype))
        st["rows"] += 1
        if in_kraj:
            st["kraj_rows"] += 1

    try:
        from urllib.parse import quote
        kraj_q = "&obec=" + quote(TR_KRAJ_ID) + "&n_search=search&searchType=string"
        for tid, ptype, deal in TR_CATS:              # 1) kraj appky — všetky strany
            pg, last = 1, TR_KRAJ_MAX_PAGES
            while pg <= (min(last, 2) if SMOKE else last):
                code, h = fetch(f"https://www.topreality.sk/vyhladavanie-nehnutelnosti-{pg}.html?type={tid}{kraj_q}")
                time.sleep(sleep)
                st["pages"] += 1
                st["kraj_pages"] += 1
                cards = _tr_cards(h) if code == 200 else []
                if not cards:
                    if code != 200:
                        st["errors"] += 1
                    break
                if pg == 1:
                    found = _tr_found(h)
                    if found:
                        last = min(TR_KRAJ_MAX_PAGES, math.ceil(found / len(cards)) + 1)   # +1 = poistka (prázdna → koniec)
                for c in cards:
                    add(c, ptype, deal, True)
                pg += 1
        for tid, ptype, deal in TR_CATS:              # 2) celé SR — týždenná obmena
            pg = 1
            while not (SMOKE and pg > 2):
                if pg > TR_DAILY_PAGES and pg % 7 != day_index:
                    pg += 1
                    if pg > 2000:
                        break
                    continue
                url = f"https://www.topreality.sk/vyhladavanie-nehnutelnosti-{pg}.html?type={tid}"
                code, h = fetch(url)
                time.sleep(sleep)
                st["pages"] += 1
                cards = _tr_cards(h) if code == 200 else []
                if not cards:
                    if code != 200:
                        st["errors"] += 1
                    break
                for c in cards:
                    add(c, ptype, deal, False)
                pg += 1
    except Blocked as e:
        st["stopped" if isinstance(e, OutOfTime) else "blocked"] = str(e)
    return rows, seen, st


TR_NEWEST_PAGES = 5      # popoludňajší beh: najviac 5 strán na kategóriu (kraj, od najnovších)


def crawl_topreality_newest(refine_ptype, okres_kraj, known, max_pages=TR_NEWEST_PAGES, sleep=1.3):
    """POPOLUDŇAJŠÍ ľahký beh (oprava 10, 2026-11-28): Žilinský kraj zoradený „pridania (najnovšie)"
    (`sort=id_desc`, robots.txt topreality to povoľuje); strany idú, kým prinášajú inzeráty, ktoré ešte
    nepoznáme (known = ID z databázy zberu). Známe inzeráty sa len potvrdia (seen)."""
    rows, seen, st = [], [], _stat()
    st["new"] = 0
    got = set()
    try:
        from urllib.parse import quote
        q = "&obec=" + quote(TR_KRAJ_ID) + "&n_search=search&searchType=string&sort=id_desc"
        for tid, ptype, deal in TR_CATS:
            for pg in range(1, (1 if SMOKE else max_pages) + 1):
                code, h = fetch(f"https://www.topreality.sk/vyhladavanie-nehnutelnosti-{pg}.html?type={tid}{q}")
                time.sleep(sleep)
                st["pages"] += 1
                cards = _tr_cards(h) if code == 200 else []
                if not cards:
                    if code != 200:
                        st["errors"] += 1
                    break
                fresh = 0
                for c in cards:
                    if c["id"] in got or not c["url"]:
                        continue
                    got.add(c["id"])
                    if c["id"] in known:
                        seen.append(c["id"]); st["seen"] += 1
                        continue
                    fresh += 1
                    rows.append(_tr_row(c, ptype, deal, okres_kraj, True, refine_ptype))
                    st["rows"] += 1
                st["new"] += fresh
                if not fresh:
                    break                  # strana bez nových = staršie už poznáme
    except Blocked as e:
        st["stopped" if isinstance(e, OutOfTime) else "blocked"] = str(e)
    return rows, seen, st


# ——— zoznamrealit.sk ————————————————————————————————————————————————
# Zoznam má len odkazy (JSON-LD ItemList); cena v detaile (JSON-LD Offer/Product), okres a obec
# v drobčekovej navigácii detailu (BreadcrumbList pozícia 4 = okres, 5 = obec). Detail len pre NOVÉ.
# Portál má aj zahraničné ponuky (Chorvátsko…) → berú sa len tie, ktorých okres je slovenský.
ZR_CATS = [("pozemky", "pozemok"), ("domy", "dom"), ("byty", "byt")]
ZR_DAILY_PAGES = 3
ZR_DETAIL_CAP = 300
_AREA = re.compile(r"(\d[\d\s.]{0,7})\s*(?:m2|m²)", re.I)


def _zr_list(html):
    out = []
    for b in JSONLD.findall(html):
        try:
            d = json.loads(b)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "ItemList":
            for li in d.get("itemListElement", []):
                it = li.get("item") or {}
                u = it.get("url") or ""
                m = re.search(r"-(\d{5,})/?$", u)
                if m:
                    out.append((u, m.group(1), it.get("name") or ""))
    return out


def _zr_detail(html):
    price = None; name = ""; desc = ""; crumbs = []
    for b in JSONLD.findall(html):
        try:
            d = json.loads(b)
        except Exception:
            continue
        for x in (d.get("@graph", [d]) if isinstance(d, dict) else []):
            t = x.get("@type")
            if t == "Offer" and x.get("price"):
                price = price or _num(str(x["price"]).split(".")[0])
            elif t == "Product":
                name = x.get("name") or name
                desc = x.get("description") or desc
                off = x.get("offers") or {}
                if isinstance(off, dict) and off.get("price"):
                    price = price or _num(str(off["price"]).split(".")[0])
            elif t == "BreadcrumbList":
                crumbs = [((li.get("item") or {}).get("name") or "") for li in x.get("itemListElement", [])]
    return price, name, desc, crumbs


ZR_KRAJ_MAX_PAGES = 60      # na okres a kategóriu (Žilina pozemky ≈ 100 ponúk = 4 strany)


def crawl_zoznamrealit(day_index, refine_ptype, seen_known, okres_kraj, sleep=1.2):
    """1) CELÝ Žilinský kraj denne: zoznam po okresoch (/predaj/<kat>/<okres>/<strana>) — nové inzeráty kraja
    idú do detailov PRVÉ; 2) celé SR: najnovšie strany + každá 7. hlbšia. Detail (cena) len pre nové, strop ZR_DETAIL_CAP."""
    rows, seen, st = [], [], _stat()
    st["detail"] = 0
    st["kraj_pages"] = 0
    new = []
    listed = set()

    def take(items, ptype):
        """→ počet inzerátov, ktoré dnes ešte neboli na zozname (0 = strana sa opakuje → koniec)."""
        fresh = 0
        for u, eid, nm in items:
            if eid in listed:
                continue
            listed.add(eid); fresh += 1
            if eid in seen_known:
                seen.append(eid); st["seen"] += 1
            else:
                new.append((u, eid, nm, ptype))
        return fresh

    def walk(base, max_pages, rolling, kraj):
        pg = 1
        while pg <= (1 if SMOKE else max_pages):
            if rolling and pg > ZR_DAILY_PAGES and pg % 7 != day_index:
                pg += 1
                continue
            code, h = fetch(base + (f"/{pg}" if pg > 1 else ""))
            time.sleep(sleep)
            st["pages"] += 1
            if kraj:
                st["kraj_pages"] += 1
            items = _zr_list(h) if code == 200 else []
            if not items:
                if code != 200 and code != 404:
                    st["errors"] += 1
                break
            yield items
            pg += 1

    try:
        for okres in (KRAJ_OKRESY[:1] if SMOKE else KRAJ_OKRESY):         # 1) kraj appky — všetky strany
            for cat, ptype in ZR_CATS:
                for items in walk(f"https://www.zoznamrealit.sk/predaj/{cat}/{slug(okres)}", ZR_KRAJ_MAX_PAGES, False, True):
                    if not take(items, ptype):
                        break                     # portál za poslednou stranou vracia znova tie isté inzeráty
        st["kraj_new"] = len(new)
        for cat, ptype in ZR_CATS:                                          # 2) celé SR — týždenná obmena
            for items in walk(f"https://www.zoznamrealit.sk/predaj/{cat}", 400, True, False):
                take(items, ptype)
        for u, eid, nm, ptype in new[:(5 if SMOKE else ZR_DETAIL_CAP)]:
            code, h = fetch(u)
            time.sleep(0.9)
            st["detail"] += 1
            if code != 200:
                st["errors"] += 1
                continue
            price, name, desc, crumbs = _zr_detail(h)
            okres = norm_okres(crumbs[3]) if len(crumbs) > 3 else None
            if not price or okres not in okres_kraj:          # bez ceny alebo mimo SR
                continue
            obec = crumbs[4] if len(crumbs) > 4 and crumbs[4] and crumbs[4] != name else None
            am = _AREA.search((name or nm) + " " + (desc or "")[:400])
            area = float(_num(am.group(1))) if am and _num(am.group(1)) else None
            if price < 3000 and area:                         # malá cena = €/m² (pozemky)
                total, ppm2 = round(price * area), float(price)
            else:
                total, ppm2 = price, _ppm2(price, area)
            rows.append(dict(source="zoznamrealit", ext_id=eid, url=u, title=(name or nm)[:160],
                             ptype=refine_ptype(ptype, name or nm), deal="predaj", obec=obec or okres, psc=None,
                             area_m2=area, price_eur=total, ppm2=ppm2, listed=None, okres=okres, kraj=okres_kraj.get(okres)))
            st["rows"] += 1
    except Blocked as e:
        st["stopped" if isinstance(e, OutOfTime) else "blocked"] = str(e)
    return rows, seen, st


def okres_kraj_map():
    return {o: k for o, k in OKRES_LIST}

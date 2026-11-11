#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Realitné portály pre denný scraper (len stdlib, obyčajné HTTP požiadavky).

Overené 2026-11-11: nehnutelnosti.sk, topreality.sk aj zoznamrealit.sk dnes vracajú bežnú HTML
stránku aj obyčajnej požiadavke — simulovaný prehliadač (Playwright) netreba. Ak by portál začal
blokovať (HTTP 403/429, overenie „nie som robot", prázdna stránka), zber ho PRESKOČÍ a zapíše to do
reportu behu. Ochrana sa NEobchádza (rozhodnutie vlastníka 2026-11-11: pridať všetky portály, ale
bez obchádzania).

TÝŽDENNÁ OBMENA: každý deň sa prejde 1/7 katalógu (podľa okresu alebo stránky) + prioritné okresy
(Kysuce, Žilina, Ružomberok) celé, takže každý aktívny inzerát sa uvidí aspoň raz za týždeň.

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

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
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
# denne celé (oblasť appky); ostatné okresy raz za týždeň
PRIORITY_OKRESY = {"Čadca", "Kysucké Nové Mesto", "Žilina", "Bytča", "Ružomberok"}


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
    """Okresy na dnes: prioritné + každý 7. podľa poradia (týždenná obmena)."""
    if SMOKE:
        return [("Čadca", "Žilinský")]
    return [(o, k) for i, (o, k) in enumerate(OKRES_LIST) if o in PRIORITY_OKRESY or i % 7 == day_index]


# Skúšobný beh (PORTALS_SMOKE=1): jeden okres, 2 strany, 5 detailov — na overenie parsovania pred nasadením.
import os as _os
SMOKE = bool(_os.environ.get("PORTALS_SMOKE"))


class Blocked(Exception):
    pass


def fetch(url, timeout=30, tries=3):
    """Vráti (status, html). Pri 403/429 alebo stránke s overením „nie som robot" hodí Blocked.
    Výpadok spojenia / 5xx sa skúsi znova (3×, čakanie 5 s, 15 s) — jeden výpadok nesmie stratiť okres."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sk", "Accept": "text/html"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                h = r.read().decode("utf-8", "replace")
                st = r.status
            break
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise Blocked(f"HTTP {e.code}")
            if e.code >= 500 and i < tries - 1:
                time.sleep(5 + 10 * i); continue
            if e.code == 503:
                raise Blocked("HTTP 503")
            return e.code, ""
        except Exception:  # noqa: BLE001 — timeout, reset spojenia, DNS
            if i < tries - 1:
                time.sleep(5 + 10 * i); continue
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
        st["blocked"] = str(e)
    return rows, seen, st


# ——— topreality.sk ——————————————————————————————————————————————————
# Karty v HTML: data-ga4-container-* (id, názov, cena, kategória, obec) + text „(okres)" a „NNN m2".
TR_CATS = [(800, "pozemok", "predaj"), (100, "byt", "predaj"), (200, "dom", "predaj")]
TR_DAILY_PAGES = 4


def _tr_cards(html):
    """Karta: atribúty data-ga4-container-* + span.location-city / span.location-district „(okres)" +
    span.value „N m<sup>2</sup>" (pozemok = area-plot, inak úžitková). Mena „EUR/m2" = cena je za m²."""
    out = []
    for m in re.finditer(r'<div class="col-lg-12[^"]*item-col[^"]*"([^>]*)>', html):
        attrs = dict(re.findall(r'data-ga4-container-([a-z_0-9]+)="([^"]*)"', m.group(1)))
        eid = attrs.get("item_id_generic")
        if not eid:
            continue
        body = html[m.end():m.end() + 9000]
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


def crawl_topreality(day_index, refine_ptype, okres_kraj, sleep=1.3):
    rows, seen, st = [], [], _stat()
    try:
        for tid, ptype, deal in TR_CATS:
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
                    if not c["url"]:
                        continue
                    okres = norm_okres(c["okres"])
                    pt = refine_ptype(ptype, c["name"])
                    area = (c["plot"] or c["usable"]) if ptype == "pozemok" else (c["usable"] or c["plot"])
                    area = float(area) if area else None
                    if c["unit"]:                      # cena za m² → celková = €/m² × výmera
                        price, ppm2 = (round(c["price"] * area) if c["price"] and area else None), (float(c["price"]) if c["price"] else None)
                    else:
                        price, ppm2 = c["price"], _ppm2(c["price"], area)
                    rows.append(dict(source="topreality", ext_id=c["id"], url=c["url"], title=c["name"][:160], ptype=pt,
                                     deal=deal, obec=c["obec"], psc=None, area_m2=area, price_eur=price,
                                     ppm2=ppm2, listed=None, okres=okres, kraj=okres_kraj.get(okres)))
                    st["rows"] += 1
                pg += 1
    except Blocked as e:
        st["blocked"] = str(e)
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


def crawl_zoznamrealit(day_index, refine_ptype, seen_known, okres_kraj, sleep=1.2):
    rows, seen, st = [], [], _stat()
    st["detail"] = 0
    new = []
    try:
        for cat, ptype in ZR_CATS:
            pg = 1
            while pg <= (1 if SMOKE else 400):
                if pg > ZR_DAILY_PAGES and pg % 7 != day_index:
                    pg += 1
                    continue
                url = f"https://www.zoznamrealit.sk/predaj/{cat}" + (f"/{pg}" if pg > 1 else "")
                code, h = fetch(url)
                time.sleep(sleep)
                st["pages"] += 1
                items = _zr_list(h) if code == 200 else []
                if not items:
                    if code != 200:
                        st["errors"] += 1
                    break
                for u, eid, nm in items:
                    if eid in seen_known:
                        seen.append(eid); st["seen"] += 1
                    else:
                        new.append((u, eid, nm, ptype))
                pg += 1
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
        st["blocked"] = str(e)
    return rows, seen, st


def okres_kraj_map():
    return {o: k for o, k in OKRES_LIST}

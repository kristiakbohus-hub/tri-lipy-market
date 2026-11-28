#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Slušný zber (oprava 10, 2026-11-28): čestný User-Agent, robots.txt, pauzy, spomalenie po 429.

Rozhodnutie vlastníka (28. 11.): blokovanie sa NEOBCHÁDZA — žiadne striedanie IP ani proxy, žiadne falšovanie
prehliadača ani User-Agentu, žiadne riešenie captchy. Portál, ktorý blokuje aj pri slušnom tempe, sa v behu
preskočí a report to prizná; ostatné portály sa zbierajú ďalej.

  UA              — čestná identifikácia zberu s odkazom na verejné repo (tam je aj kontakt). Do 27. 11. scraper
                    aj portals.py posielali User-Agent prehliadača Chrome.
  allowed(url)    — robots.txt (RFC 9309: skupina pre náš token, inak „*“; najdlhšie pravidlo vyhráva, pri zhode
                    Allow; `*` a `$` v pravidlách). robots.txt sa číta raz za beh na doménu. 4xx = bez obmedzení,
                    nedostupný (5xx/výpadok aj po opakovaní) = všetko zakázané (RFC 9309, kap. 2.3.1.4).
  pace(url)       — minimálny odstup požiadaviek na tú istú doménu (náhodný v rozsahu podľa domény, aspoň
                    Crawl-delay z robots.txt); po 429 sa doména do konca behu spomalí (slow_down).
  retry_after(e)  — hlavička Retry-After v sekundách (číslo aj HTTP dátum), None ak chýba.

Len stdlib.
"""
import email.utils
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA_TOKEN = "tri-lipy-market"
UA = f"{UA_TOKEN}/2.0 (+https://github.com/kristiakbohus-hub/tri-lipy-market)"

# Odstup medzi požiadavkami na doménu (s) — náhodne v rozsahu. Bazoš od 28. 11. výrazne pomalšie
# (27. 11.: 1,3 s/strana + re-verify 0,35 s → HTTP 429 po 151 stranách).
PACE = {
    "reality.bazos.sk": (5.0, 8.0),
    "www.nehnutelnosti.sk": (1.5, 2.5),
    "www.topreality.sk": (1.3, 2.3),
    "www.reality.sk": (1.2, 2.2),
    "www.zoznamrealit.sk": (1.2, 2.2),
}
DEFAULT_PACE = (1.5, 2.5)

_robots = {}        # host → _Rules
_next_ok = {}       # host → čas, odkedy smie ísť ďalšia požiadavka
_slow = {}          # host → násobok pauzy (po 429)
_sleep = time.sleep  # testy si ho nahradia
_fetch_robots = None  # testy: funkcia (url) → (status, text)


class _Rules:
    def __init__(self, rules=(), crawl_delay=None, disallow_all=False, source="ok"):
        self.rules = list(rules)          # [(allow: bool, pattern: str, regex)]
        self.crawl_delay = crawl_delay
        self.disallow_all = disallow_all
        self.source = source              # ok | missing | unreachable

    def allowed(self, path):
        if self.disallow_all:
            return False
        best = None                        # (dĺžka vzoru, allow)
        for allow, pat, rx in self.rules:
            if not pat:
                continue                   # prázdne „Disallow:“ = nič nezakazuje
            if rx.match(path):
                cand = (len(pat), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]


def _rx(pattern):
    """Vzor robots.txt → regex od začiatku cesty: `*` = čokoľvek, `$` na konci = koniec URL."""
    end = pattern.endswith("$")
    p = pattern[:-1] if end else pattern
    return re.compile("".join(".*" if ch == "*" else re.escape(ch) for ch in p) + ("$" if end else ""))


def parse_robots(text, token=UA_TOKEN):
    """robots.txt → _Rules pre náš token (ak má vlastnú skupinu), inak pre „*“."""
    groups, cur, in_agents = [], None, False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not in_agents or cur is None:
                cur = {"agents": [], "rules": [], "delay": None}
                groups.append(cur)
            cur["agents"].append(v.lower())
            in_agents = True
            continue
        in_agents = False
        if cur is None:
            continue
        if k in ("allow", "disallow"):
            cur["rules"].append((k == "allow", v, _rx(v)))
        elif k == "crawl-delay":
            try:
                cur["delay"] = float(v)
            except ValueError:
                pass
    tok = token.lower()
    mine = [g for g in groups if any(a and a != "*" and a in tok for a in g["agents"])]
    star = [g for g in groups if "*" in g["agents"]]
    pick = mine or star
    rules = [r for g in pick for r in g["rules"]]
    delays = [g["delay"] for g in pick if g["delay"] is not None]
    return _Rules(rules, max(delays) if delays else None)


def _get_robots(scheme, host):
    url = f"{scheme}://{host}/robots.txt"
    if _fetch_robots:
        return _fetch_robots(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read().decode("utf-8", "replace")


def rules_for(url):
    u = urllib.parse.urlsplit(url)
    host = u.netloc.lower()
    if host in _robots:
        return _robots[host]
    rules = None
    for i in range(3):
        try:
            st, text = _get_robots(u.scheme or "https", host)
            rules = parse_robots(text) if st == 200 else _Rules(source="missing")
            break
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                rules = _Rules(source="missing")   # robots.txt neexistuje → bez obmedzení
                break
        except Exception:  # noqa: BLE001 — výpadok, timeout
            pass
        _sleep(5 * (i + 1))
    if rules is None:                            # nedostupný aj po opakovaní → všetko zakázané (RFC 9309)
        rules = _Rules(disallow_all=True, source="unreachable")
    _robots[host] = rules
    return rules


def allowed(url):
    u = urllib.parse.urlsplit(url)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    return rules_for(url).allowed(path)


def robots_state(url):
    """Popis pre report: ok / missing / unreachable."""
    return rules_for(url).source


def pace(url):
    """Počká, kým smie ísť ďalšia požiadavka na doménu (náhodný odstup, Crawl-delay, spomalenie po 429)."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    lo, hi = PACE.get(host, DEFAULT_PACE)
    cd = rules_for(url).crawl_delay or 0
    gap = max(random.uniform(lo, hi), cd) * _slow.get(host, 1.0)
    now = time.time()
    wait = _next_ok.get(host, 0) - now
    if wait > 0:
        _sleep(wait)
    _next_ok[host] = max(now, _next_ok.get(host, 0)) + gap


def slow_down(url, factor=2.0, cap=4.0):
    host = urllib.parse.urlsplit(url).netloc.lower()
    _slow[host] = min(cap, _slow.get(host, 1.0) * factor)
    return _slow[host]


def retry_after(err):
    """Retry-After v sekundách (číslo alebo HTTP dátum); None ak hlavička chýba / je nečitateľná."""
    try:
        v = (err.headers.get("Retry-After") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not v:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", v):
        return float(v)
    try:
        dt = email.utils.parsedate_to_datetime(v)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:  # noqa: BLE001
        return None


def reset():
    """Len pre testy."""
    _robots.clear(); _next_ok.clear(); _slow.clear()

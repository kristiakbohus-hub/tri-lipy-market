#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Osobné údaje do verejného repa NEPATRIA (oprava 10, 2026-11-28).

Repo tri-lipy-market je verejné. Audit 27. 11. našiel v súboroch inzerátov telefónne čísla — maklér/predajca
ich píše do titulku („☎️09xx…“) a portál ich prenesie aj do adresy inzerátu (slug). Pred zápisom do
verejných súborov (market-listings-*.json, market-data.json) sa preto:
  - v titulku nahradí číslo značkou „[tel.]“ a e-mail značkou „[e-mail]“,
  - v adrese inzerátu (URL):
      topreality.sk   — číslo zo slugu preč; portál presmeruje na inzerát podľa ID (-r<ID>.html), overené 301
      nehnutelnosti.sk — slug preč, ostane /detail/<ID>; portál presmeruje na inzerát, overené 308
      ostatné (Bazoš…) — URL sa nezverejní (None): Bazoš na upravený slug vracia 404, nefunkčný odkaz nechceme.
Lokálna databáza zberu (listings.db v cache Actions, nie verejná) drží pôvodné hodnoty — re-verify Bazoša
potrebuje presnú adresu inzerátu.

Len stdlib. Staré verzie súborov v histórii repa čísla obsahujú — prepis histórie je rozhodnutie vlastníka.
"""
import re
import urllib.parse

# mobil: 09xx xxx xxx / +421 9xx xxx xxx / 00421…, oddeľovače medzera . / - (aj bez oddeľovačov, aj prilepené k textu)
_SEP = r"[\s./-]?"
PHONE_MOBILE = re.compile(r"(?<!\d)(?:(?:\+|00)\s?421" + _SEP + r"|0)9\d{2}(?:" + _SEP + r"\d){6}(?!\d)")
# pevná linka s predvoľbou +421 / 00421 (02, 041, 0905… bez nuly)
PHONE_INTL = re.compile(r"(?<!\d)(?:\+|00)\s?421" + _SEP + r"[2-5]\d{0,2}(?:" + _SEP + r"\d){6,8}(?!\d)")
# pevná linka v tvare s lomkou: 041/562 34 56, 02/123 456 78
PHONE_SLASH = re.compile(r"(?<!\d)0[2-5]\d{0,2}\s?/\s?\d{2,3}(?:[\s.-]?\d{2,3}){2,3}(?!\d)")   # aspoň 6 číslic za lomkou (nie „03/2025“)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PATTERNS = (PHONE_MOBILE, PHONE_INTL, PHONE_SLASH)


def has_contact(s):
    s = s or ""
    return any(p.search(s) for p in _PATTERNS) or bool(EMAIL.search(s))


def mask_text(s):
    """Titulok / text → bez telefónnych čísel a e-mailov."""
    if not s:
        return s
    out = EMAIL.sub("[e-mail]", s)
    for p in _PATTERNS:
        out = p.sub("[tel.]", out)
    return out


def _strip_from_slug(slug):
    out = slug
    for p in _PATTERNS:
        out = p.sub("tel", out)
    out = EMAIL.sub("email", out)
    out = re.sub(r"-{2,}", "-", out)
    return out


def public_url(source, url):
    """Adresa inzerátu na zverejnenie. Bez čísla v adrese → nezmenená; s číslom → podľa portálu (pozri hore)."""
    if not url:
        return url
    u = urllib.parse.urlsplit(url)
    if not has_contact(urllib.parse.unquote(u.path + ("?" + u.query if u.query else ""))):
        return url
    host = u.netloc.lower()
    if host.endswith("topreality.sk"):
        m = re.search(r"-r(\d+)\.html$", u.path)
        if m:
            return urllib.parse.urlunsplit((u.scheme, u.netloc, _strip_from_slug(urllib.parse.unquote(u.path)), "", ""))
        return None
    if host.endswith("nehnutelnosti.sk"):
        m = re.match(r"(/detail/[A-Za-z0-9_-]+)", u.path)
        return urllib.parse.urlunsplit((u.scheme, u.netloc, m.group(1), "", "")) if m else None
    return None


def public_row(row, title_key="title", url_key="url", source_key="source"):
    """Kópia riadku s maskovaným titulkom a verejnou adresou."""
    r = dict(row)
    if r.get(title_key):
        r[title_key] = mask_text(r[title_key])
    if r.get(url_key):
        r[url_key] = public_url(r.get(source_key), r[url_key])
    return r

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ingest zberu do appky (D1) — po krokoch, s opakovaním (oprava 10, 2026-11-28).

  python3 ingest.py --ref <SHA>      # SHA commitu s práve zverejnenými market-*.json

PREČO: 27. 11. (beh 46045248693) krok „Ingest do D1" padol po 47 s. Bash v workflowe opakoval každú chybu 4×
(20–80 s), ale HTTP 401 bral ako zlý kľúč a skončil HNEĎ. Appka však vracia 401 aj pri SPRÁVNOM kľúči, keď jej
D1 dočasne neodpovie (hash kľúča sa číta z D1 a chyba čítania = „neplatný kľúč") — v tom čase sa do appky
nasadzovali ťažké migrácie „Úspora D1". Ďalší beh s tým istým kľúčom prešiel.

Teraz:
  - každý krok (data, listings N, pricehistory N, compute) je samostatná požiadavka (limit Workera ~1000
    subrequestov) a opakuje sa s exponenciálnym čakaním 15 → 45 → 90 → 180 s (+ náhodný rozptyl),
  - 401 sa opakuje rovnako ako ostatné chyby (~5 min); keď 401 vráti aj posledný pokus PRVÉHO kroku (data),
    kľúč naozaj neplatí (alebo D1 dlho neodpovedá) a ďalšie kroky sa neskúšajú,
  - zlyhaný krok nezastaví ostatné (chunky inzerátov sú nezávislé upserty); na konci sa zlyhané kroky
    skúsia ešte raz; compute (AVM, obecné mediány) beží vždy na konci,
  - počty chunkov sa berú z lokálneho market-data.json (nezávisle od odpovede kroku data),
  - celkový časový strop INGEST_BUDGET_MIN (30 min), aby job stihol hlásenie pred limitom,
  - výsledok → ingest-result.json (lokálne, necommituje sa) pre hlásenie zlyhania; do logu nikdy kľúč.
Kód 0 = všetko prešlo, 1 = niečo zlyhalo (workflow pošle upozornenie).
"""
import argparse, json, os, random, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("APP", "https://trilipy-kataster.kristiak-bohus.workers.dev")
UA = "tri-lipy-market-ingest/2.0 (+https://github.com/kristiakbohus-hub/tri-lipy-market)"
WAITS = (15, 45, 90, 180)        # čakanie pred 2.–5. pokusom
_sleep = time.sleep
_post_impl = None                # testy: funkcia (body) → (status, json)


def _post(body, secret, timeout=150):
    if _post_impl:
        return _post_impl(body)
    req = urllib.request.Request(f"{APP}/api/ingest-market", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "x-alert-secret": secret, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            st = r.status
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        st = e.code
    except Exception as e:  # noqa: BLE001 — timeout, výpadok spojenia
        return 0, {"message": type(e).__name__}
    try:
        return st, json.loads(raw or "{}")
    except ValueError:
        return st, {"message": raw[:120]}


def _label(body):
    return body["step"] + (f" {body['chunk']}" if "chunk" in body else "")


def step(body, secret, deadline):
    """Jeden krok s opakovaním → (ok, posledný HTTP kód, odpoveď)."""
    code, j = 0, {}
    for attempt in range(len(WAITS) + 1):
        if attempt:
            wait = WAITS[attempt - 1] * random.uniform(1.0, 1.25)
            if time.time() + wait > deadline:
                print(f"  {_label(body)}: časový strop ingestu — ďalší pokus by ho presiahol", flush=True)
                break
            _sleep(wait)
        code, j = _post(body, secret)
        msg = str(j.get("message") or "")[:160]
        print(f"  {_label(body)} (pokus {attempt + 1}) -> HTTP {code}" + (f" · {msg}" if msg and code != 200 else ""), flush=True)
        if code == 200 and j.get("ok", True):
            return True, code, j
        if code == 400 and j.get("message") == "Neplatná požiadavka." and attempt >= 2:
            break                      # trvalá chyba požiadavky — ďalšie pokusy nepomôžu
    return False, code, j


def counts():
    """Počet chunkov z lokálneho manifestu (market-data.json práve zverejneného behu)."""
    try:
        d = json.load(open(os.path.join(HERE, "market-data.json"), encoding="utf-8"))
        return int(d.get("listings_chunks") or 0), int(d.get("pricehistory_chunks") or 0)
    except Exception:  # noqa: BLE001
        return None, None


def run(ref, secret, budget_min=30.0):
    t0 = time.time()
    deadline = t0 + budget_min * 60
    res = {"ref": ref, "steps": {}, "ok": False}

    def do(body):
        b = dict(body, ref=ref) if ref else dict(body)
        ok, code, j = step(b, secret, deadline)
        res["steps"][_label(body)] = {"ok": ok, "http": code, "message": str(j.get("message") or "")[:160] if not ok else ""}
        return ok, j

    ok_data, j = do({"step": "data"})
    if not ok_data and res["steps"]["data"]["http"] == 401:
        res["failed"] = ["data"]
        res["seconds"] = round(time.time() - t0)
        res["summary"] = ("Ingest do appky: appka odmietla kľúč (HTTP 401) aj po 5 pokusoch za ~5 min — buď nesedí repo "
                          "secret ALERT_SECRET s appkou, alebo appka dlho nevie prečítať kľúč z D1; ďalšie kroky sa neskúšali")
        json.dump(res, open(os.path.join(HERE, "ingest-result.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(res["summary"], flush=True)
        print("::error title=Ingest do appky::" + res["summary"], flush=True)
        return 1
    n_l, n_p = counts()
    if n_l is None:                    # bez lokálneho manifestu ber počty z odpovede kroku data
        n_l, n_p = int(j.get("chunks") or 0), int(j.get("phChunks") or 0)
    print(f"listings chunkov={n_l}, pricehistory chunkov={n_p}", flush=True)
    plan = [{"step": "listings", "chunk": i} for i in range(n_l)] + [{"step": "pricehistory", "chunk": i} for i in range(n_p)]
    failed = [] if ok_data else [{"step": "data"}]
    for body in plan:
        if not do(body)[0]:
            failed.append(body)
    if failed and time.time() < deadline - 120:
        print(f"Druhé kolo pre {len(failed)} zlyhaných krokov o 60 s…", flush=True)
        _sleep(60)
        failed = [b for b in failed if not do(b)[0]]
    ok_compute = do({"step": "compute"})[0]
    if not ok_compute:
        failed.append({"step": "compute"})
    res["ok"] = not failed
    res["failed"] = [_label(b) for b in failed]
    res["seconds"] = round(time.time() - t0)
    codes = sorted({res["steps"][_label(b)]["http"] for b in failed})
    res["summary"] = ("Ingest do appky: všetkých {} krokov prešlo".format(len(res["steps"])) if res["ok"] else
                      "Ingest do appky: {} z {} krokov neprešlo ({}; HTTP {})".format(
                          len(failed), len(plan) + 2, ", ".join(res["failed"][:6]) + ("…" if len(failed) > 6 else ""),
                          ", ".join(str(c) for c in codes)))
    if not res["ok"] and any(res["steps"][_label(b)]["http"] == 401 for b in failed):
        res["summary"] += " — appka odmietla kľúč (ALERT_SECRET) aj po opakovaní, alebo nevedela overiť kľúč v D1"
    json.dump(res, open(os.path.join(HERE, "ingest-result.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(res["summary"], flush=True)
    if not res["ok"]:
        print("::error title=Ingest do appky::" + res["summary"], flush=True)
    return 0 if res["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=os.environ.get("REF", ""))
    a = ap.parse_args(argv)
    secret = os.environ.get("SECRET") or os.environ.get("ALERT_SECRET") or ""
    if not secret:
        print("::error title=Ingest do appky::chýba repo secret ALERT_SECRET", flush=True)
        json.dump({"ok": False, "summary": "Ingest do appky: chýba repo secret ALERT_SECRET"},
                  open(os.path.join(HERE, "ingest-result.json"), "w", encoding="utf-8"), ensure_ascii=False)
        return 1
    ref = a.ref if a.ref and all(ch in "0123456789abcdef" for ch in a.ref) and 7 <= len(a.ref) <= 40 else ""
    return run(ref, secret, float(os.environ.get("INGEST_BUDGET_MIN", "30")))


if __name__ == "__main__":
    sys.exit(main())

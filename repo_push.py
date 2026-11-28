#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zápis výstupov zberu do repa BEZ konfliktov (oprava 10, 2026-11-28).

  python3 repo_push.py -m "data 2026-11-28" market-data.json 'market-listings-*.json' scrape-report.json

PREČO: 28. 11. plánovaný beh čakal vo fronte za iným behom a mal checkout commitu spred dvoch hodín. Jeho
`git pull --rebase` narazil na konflikt v market-*.json (tie isté súbory práve zapísal predchádzajúci beh),
trikrát zlyhal, HEAD ostal na cudzom commite → ingest nahral do appky dáta PREDCHÁDZAJÚCEHO behu a dáta
dvojhodinového zberu sa do repa nedostali.

Výstupy zberu sa vždy vytvárajú celé nanovo, takže sa nič nespája: súbory behu sa odložia, pracovný strom
sa postaví na AKTUÁLNY origin/main (zmeny iných — digest.py, ÚP monitor z Macu — ostanú), súbory behu sa
nahradia odloženými, commit, push. Vzor s hviezdičkou (market-listings-*.json) = celá rodina chunkov patrí behu:
chunky, ktoré beh nevytvoril, sa zmažú. Presný názov (market-trends.json), ktorý beh nevytvoril, ostane ako je.
Keď medzitým pushol niekto iný, celé sa to zopakuje (najviac 5×). Commit SHA → GITHUB_OUTPUT `sha=` (ingest číta presne ten).
"""
import argparse, fnmatch, glob, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))


def git(*args, check=True):
    p = subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def matches(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def push(message, patterns, attempts=5, branch="main"):
    saved = tempfile.mkdtemp(prefix="zber-")
    mine = sorted({os.path.basename(f) for p in patterns for f in glob.glob(os.path.join(HERE, p)) if os.path.isfile(f)})
    for f in mine:
        shutil.copy2(os.path.join(HERE, f), os.path.join(saved, f))
    print(f"[repo_push] súborov behu: {len(mine)}")
    for n in range(1, attempts + 1):
        git("fetch", "-q", "origin", branch)
        git("checkout", "-q", "-f", "-B", branch, f"origin/{branch}")
        git("reset", "-q", "--hard", f"origin/{branch}")
        families = [p for p in patterns if any(ch in p for ch in "*?[")]
        for f in os.listdir(HERE):                       # chunky z rodiny, ktoré tento beh nevytvoril
            if matches(f, families) and f not in mine and os.path.isfile(os.path.join(HERE, f)):
                os.remove(os.path.join(HERE, f))
        for f in mine:
            shutil.copy2(os.path.join(saved, f), os.path.join(HERE, f))
        for p in patterns:                               # vzor bez zhody (napr. 0 chunkov) nesmie zhodiť zápis
            git("add", "-A", "--", p, check=False)
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("[repo_push] žiadna zmena oproti origin/main")
            return git("rev-parse", "HEAD").stdout.strip()
        git("-c", "user.name=trilipy-bot", "-c", "user.email=bot@users.noreply.github.com", "commit", "-q", "-m", message)
        p = git("push", "-q", "origin", f"HEAD:{branch}", check=False)
        if p.returncode == 0:
            sha = git("rev-parse", "HEAD").stdout.strip()
            print(f"[repo_push] pushnuté {sha[:7]} (pokus {n})")
            return sha
        print(f"[repo_push] push odmietnutý (pokus {n}) — medzitým pushol niekto iný, opakujem nad novým stavom")
        time.sleep(5 * n)
    raise RuntimeError("push sa nepodaril ani na 5. pokus")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("patterns", nargs="+")
    a = ap.parse_args(argv)
    sha = push(a.message, a.patterns)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"sha={sha}\n")
    print(f"sha={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

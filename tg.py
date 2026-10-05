#!/usr/bin/env python3
"""Zdieľaný Telegram modul pre GitHub Actions (žiadny lokálny telegram.json — token/chat_id
z env premenných TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, nastavené ako repo secrets).
Port z 13_INFOKAT_CLAUDE/17_UP_MONITOR/tg.py (Mac verzia). Len stdlib — urllib.
Použitie: from tg import send; send("Ahoj *tučne*")
"""
import json, os, urllib.request, urllib.parse, urllib.error


def _load():
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(), (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def configured() -> bool:
    tok, cid = _load()
    return bool(tok) and bool(cid)


def _api(token: str, method: str, params: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def send(text: str, parse_mode: str = "Markdown", silent: bool = False) -> bool:
    """Pošle správu. Vráti True/False. Nikdy nevyhodí výnimku (alerty nesmú zhodiť workflow)."""
    tok, cid = _load()
    if not tok or not cid:
        print("[tg] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nie sú nastavené — preskakujem.")
        return False
    try:
        params = {"chat_id": cid, "text": text[:4000], "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if silent:
            params["disable_notification"] = "true"
        res = _api(tok, "sendMessage", params)
        if not res.get("ok"):
            print(f"[tg] chyba: {res.get('description')}")
        return bool(res.get("ok"))
    except urllib.error.HTTPError as e:
        print(f"[tg] HTTP {e.code}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        print(f"[tg] {e}")
        return False


if __name__ == "__main__":
    import sys
    ok = send(sys.argv[1] if len(sys.argv) > 1 else "🧪 Test z GitHub Actions tg.py")
    print("odoslané" if ok else "neodoslané")

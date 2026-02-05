import json
import os
import re
import time
import hashlib
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

TARGETS_FILE = "targets.json"
STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# 🚀 ottimizzazione
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}
GOTO_TIMEOUT_MS = 35_000
WAIT_AFTER_LOAD_MS = 1000
RETRIES_PER_URL = 1


def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configurato.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def try_click_cookie(page):
    for sel in [
        "button:has-text('Accetta')",
        "button:has-text('Accetto')",
        "button:has-text('Accept')",
        "text=Accetta",
        "text=Accept",
    ]:
        try:
            page.locator(sel).first.click(timeout=1000)
            return
        except Exception:
            pass


def extract_first_comment_text(page):
    """
    PRENDE SOLO IL PRIMO COMMENTO IN ALTO
    (basato su pagina reale it.investing.com/members/.../comments)
    """
    header = page.get_by_text("Commenti di", exact=False).first
    try:
        header.wait_for(timeout=5000)
    except Exception:
        return None

    container = header.locator("xpath=ancestor::div[1]")
    try:
        text = container.inner_text(timeout=2500)
    except Exception:
        return None

    text = norm(text)

    # Regex: STRUMENTO + "X ore fa" + TESTO COMMENTO
    m = re.search(
        r"Commenti di .*? "
        r"[A-Za-z0-9À-ÿ\.\-\s]{2,80}\s+"
        r"\d+\s+(?:minuti|minuto|ore|ora|giorni|giorno)\s+fa\s+"
        r"(?P<comment>.+?)"
        r"(?=\s+[A-Za-z0-9À-ÿ\.\-\s]{2,80}\s+\d+\s+(?:minuti|minuto|ore|ora|giorni|giorno)\s+fa|\Z)",
        text,
        flags=re.IGNORECASE,
    )

    if not m:
        return None

    comment = m.group("comment")

    # pulizia UI
    for junk in ["Rispondi", "Condividi", "Segnala", "Mi piace", "Reply", "Share", "Report", "Like"]:
        comment = comment.replace(junk, "")

    comment = norm(comment)
    return comment[:1000]


def main():
    targets = load_json(TARGETS_FILE, [])
    state = load_json(STATE_FILE, {})
    new_state = dict(state)

    if not targets:
        print("targets.json vuoto.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="it-IT")

        def route_handler(route):
            if route.request.resource_type in BLOCK_RESOURCE_TYPES:
                return route.abort()
            return route.continue_()

        context.route("**/*", route_handler)
        page = context.new_page()

        for t in targets:
            name = t.get("name", "Senza nome")
            url = t.get("url", "").strip()

            if "it.investing.com/members/" not in url or "/comments" not in url:
                continue

            print(f"\nControllo: {name}")

            old_sig = state.get(url)
            comment = None

            for _ in range(RETRIES_PER_URL + 1):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                    try_click_cookie(page)
                    page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

                    comment = extract_first_comment_text(page)
                    if comment:
                        break
                except PWTimeoutError:
                    pass

            if not comment:
                print("  impossibile leggere commento (skip)")
                continue

            sig = sha256(comment)

            if old_sig is None:
                print("  primo avvio → salvo stato")
                new_state[url] = sig
                continue

            if sig != old_sig:
                print("  🔔 CAMBIAMENTO REALE")
                msg = (
                    f"🔔 Nuovo commento\n"
                    f"📄 {name}\n"
                    f"🔗 {url}\n"
                    f"📝 {comment[:300]}\n"
                    f"🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                )
                send_telegram(msg)
                new_state[url] = sig
            else:
                print("  nessun cambiamento")
                new_state[url] = old_sig

        context.close()
        browser.close()

    save_json(STATE_FILE, new_state)


if __name__ == "__main__":
    main()

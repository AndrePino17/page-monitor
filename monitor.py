import json
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

TARGETS_FILE = "targets.json"
STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

def load_json(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path: str, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configurato (mancano TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()

def try_click_cookie(page):
    candidates = [
        "button:has-text('Accetta')",
        "button:has-text('Accetto')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "text=Accetta",
        "text=Accetto",
        "text=Accept",
        "text=I agree",
    ]
    for sel in candidates:
        try:
            page.locator(sel).first.click(timeout=1500)
            print("Cookie popup chiuso.")
            return
        except Exception:
            pass

def extract_signature(page) -> str:
    """
    Firma abbastanza stabile del commento più recente.
    NON hasha tutta la pagina (che cambia spesso),
    ma prova a prendere un blocco di testo tipo commento.
    """
    selectors = [
        "[data-test*='comment']",
        "[class*='comment']",
        "article",
        "div[class*='text']",
        "div[class*='content']",
    ]

    best = None
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n <= 0:
                continue

            for i in range(min(n, 20)):
                txt = normalize_text(loc.nth(i).inner_text(timeout=1500))

                if len(txt) < 40:
                    continue
                if len(txt) > 2500:
                    continue
                if "Accedi" in txt and "Registrati" in txt and len(txt) < 200:
                    continue

                best = txt
                break

            if best:
                break
        except Exception:
            continue

    if not best:
        # fallback controllato: solo parte del body
        try:
            body = normalize_text(page.locator("body").inner_text(timeout=2000))
            best = body[:2000]
        except Exception:
            best = page.content()[:2000]

    return sha256(best)

def main():
    targets = load_json(TARGETS_FILE, [])
    state = load_json(STATE_FILE, {})
    new_state = dict(state)

    if not targets:
        print("targets.json vuoto: niente da controllare.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            locale="it-IT",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()

        for t in targets:
            name = t.get("name", "Senza nome")
            url = (t.get("url") or "").strip()
            if not url:
                continue

            # SOLO investing members comments
            if "it.investing.com/members/" not in url or "/comments" not in url:
                print(f"Skip non-investing: {url}")
                continue

            print(f"Controllo: {name} -> {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try_click_cookie(page)
                page.wait_for_timeout(1500)

                sig = extract_signature(page)
                old = state.get(url)

                if old is None:
                    print("Primo avvio per questo URL: salvo stato (no notifica).")
                    new_state[url] = sig
                    continue

                if sig != old:
                    print("CAMBIAMENTO rilevato -> invio Telegram.")
                    msg = (
                        f"🔔 Nuovo commento rilevato\n"
                        f"📄 {name}\n"
                        f"🔗 {url}\n"
                        f"🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                    )
                    send_telegram(msg)
                    new_state[url] = sig
                else:
                    print("Nessun cambiamento.")

            except Exception as e:
                print(f"Errore su {url}: {e}")

        context.close()
        browser.close()

    save_json(STATE_FILE, new_state)

if __name__ == "__main__":
    main()

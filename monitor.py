import json
import os
import re
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
    s = re.sub(r"\s+", " ", s).strip()
    return s

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configurato (mancano env).")
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
    # prova a chiudere popup cookie / privacy se compare
    candidates = [
        "text=Accetta",
        "text=Accetto",
        "text=I agree",
        "text=Accept",
        "text=Accetta tutto",
        "text=Accept all",
        "text=OK",
        "button:has-text('Accetta')",
        "button:has-text('Accept')",
    ]
    for sel in candidates:
        try:
            page.locator(sel).first.click(timeout=1200)
            print("Cookie popup chiuso.")
            return
        except Exception:
            pass

def extract_latest_comment_signature(page) -> str:
    """
    Estrae una 'firma' (signature) del commento più recente.
    Se non riesce a trovare una card commento, fa fallback su una porzione di testo della pagina
    (meno preciso, ma evita di rompere tutto).
    """
    # Prova selettori comuni (può cambiare nel tempo: ne mettiamo tanti)
    selectors = [
        "[data-test*='comment']",
        "[class*='comment']",
        "article",
        "div:has-text('Commenti')",
    ]

    # 1) Prova a trovare blocchi che sembrano commenti e prendere il primo
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count <= 0:
                continue

            # prendiamo i primi elementi e cerchiamo testo "plausibile"
            for i in range(min(count, 15)):
                txt = loc.nth(i).inner_text(timeout=1000)
                txt = normalize_text(txt)
                # euristica: un commento di solito ha un minimo di lunghezza
                if 30 <= len(txt) <= 2000:
                    return sha256(txt)
        except Exception:
            continue

    # 2) Fallback: usa una parte del testo principale (meno preciso)
    try:
        body_txt = page.locator("body").inner_text(timeout=2000)
        body_txt = normalize_text(body_txt)
        # prendiamo solo una finestra (per ridurre rumore)
        window = body_txt[:4000]
        return sha256(window)
    except Exception:
        # ultimo fallback
        html = page.content()
        return sha256(html[:4000])

def main():
    targets = load_json(TARGETS_FILE, [])
    state = load_json(STATE_FILE, {})
    new_state = dict(state)

    first_run = (len(state) == 0)

    if not targets:
        print("targets.json vuoto: niente da fare.")
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
            url = t.get("url")
            if not url:
                continue

            # Qui la tua richiesta: SOLO Investing
            if "it.investing.com/members/" not in url:
                print(f"Skip non-investing: {url}")
                continue

            print(f"Controllo: {name} -> {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try_click_cookie(page)

                # aspetta un attimo che carichi contenuti dinamici
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PWTimeoutError:
                    pass

                sig = extract_latest_comment_signature(page)
                old = state.get(url)

                if old is None:
                    print("Primo avvio per questo URL: salvo stato (no notifica).")
                    new_state[url] = sig
                    continue

                if sig != old:
                    print("CAMBIAMENTO RILEVATO (nuovo commento / contenuto).")
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

    # salva stato
    save_json(STATE_FILE, new_state)

    if first_run:
        print("Primo avvio globale: salvato state.json (notifiche disattivate per evitare spam).")

if __name__ == "__main__":
    main()

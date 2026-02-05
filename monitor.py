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

# Se in un singolo run "cambiano" tante pagine, quasi sempre è rumore/inizializzazione:
# aggiorniamo lo state ma NON notifichiamo.
ANTI_SPAM_THRESHOLD = 2  # 2 o più cambiamenti nello stesso run => niente notifiche

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

def extract_stable_signature(page) -> str:
    """
    Estrae una firma (hash) più stabile possibile del commento più recente.
    Non può essere perfetta perché Investing cambia HTML spesso, ma:
    - prova più selettori "comment-like"
    - prende il primo blocco di testo plausibile
    - fa hash solo di quel blocco, NON di tutta la pagina (meno falsi positivi)
    """

    # Selettori euristici: proviamo a trovare elementi che contengono commenti
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

            # controlliamo i primi elementi: il commento più recente di solito sta in alto
            for i in range(min(n, 20)):
                txt = loc.nth(i).inner_text(timeout=1500)
                txt = normalize_text(txt)

                # euristiche per evitare header/menu
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
        # fallback: prendi un pezzo del body (ma limitato, non tutta la pagina)
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

    # Per evitare spam iniziale: se state è vuoto, NON notifichiamo nulla
    first_global_run = (len(state) == 0)

    if not targets:
        print("targets.json vuoto: niente da controllare.")
        return

    changes = []  # lista di (name, url) che risultano cambiati in questo run

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
            url = t.get("url", "").strip()
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

                # non aspettiamo networkidle (Investing non sta mai fermo); massimo un attimo
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

                sig = extract_stable_signature(page)
                old = state.get(url)

                if old is None:
                    print("Primo avvio per questo URL: salvo stato (no notifica).")
                    new_state[url] = sig
                    continue

                if sig != old:
                    print("CAMBIAMENTO rilevato.")
                    new_state[url] = sig
                    changes.append((name, url))
                else:
                    print("Nessun cambiamento.")

            except Exception as e:
                print(f"Errore su {url}: {e}")

        context.close()
        browser.close()

    # --- NOTIFICHE (anti-spam) ---
    if first_global_run:
        print("Primo avvio globale: aggiorno state.json senza notifiche.")
    else:
        if len(changes) >= ANTI_SPAM_THRESHOLD:
            print(f"Trovati {len(changes)} cambiamenti nello stesso run: anti-spam -> nessuna notifica.")
        elif len(changes) == 1:
            name, url = changes[0]
            msg = (
                f"🔔 Nuovo commento rilevato\n"
                f"📄 {name}\n"
                f"🔗 {url}\n"
                f"🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            )
            send_telegram(msg)
        else:
            print("0 cambiamenti: nessuna notifica.")

    # salva lo stato aggiornato
    save_json(STATE_FILE, new_state)

if __name__ == "__main__":
    main()

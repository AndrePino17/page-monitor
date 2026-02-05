import json
import os
import hashlib
import re
import time
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

# Ottimizzazione: carichiamo solo HTML/XHR minimi, blocchiamo immagini/font/media
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

# Tempi aggressivi (per non far durare i run 3+ minuti)
GOTO_TIMEOUT_MS = 45_000
WAIT_AFTER_LOAD_MS = 1200
RETRIES_PER_URL = 2

# Se trovo cambiamenti su più pagine nello stesso run:
# - 0 => manda tutte le notifiche (una per pagina)
# - 1 => manda un riepilogo unico
BATCH_MODE = 0


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


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def norm(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configurato (mancano env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }

    last = None
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=25)
            r.raise_for_status()
            return
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def try_click_cookie(page):
    # Non sempre c'è, ma quando c'è rallenta / rompe i selettori
    candidates = [
        "button:has-text('Accetta')",
        "button:has-text('Accetto')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "text=Accetta",
        "text=Accetto",
        "text=Accept",
    ]
    for sel in candidates:
        try:
            page.locator(sel).first.click(timeout=1200)
            return
        except Exception:
            pass


def extract_latest_comment_signature(page):
    """
    Estrae SOLO il commento più recente e crea una firma stabile.
    Ritorna: (signature, preview)
    Se non trova niente, ritorna (None, None)
    """

    # Strategie: cerchiamo blocchi che contengono testo "da commento"
    # NB: Investing cambia classi spesso, quindi usiamo euristiche.
    selectors = [
        # blocchi comment-like
        "[class*='comment']",
        "[data-test*='comment']",
        # fallback ragionevole
        "article",
        # ultimo fallback (evitiamo body, causa falsi positivi)
        "div",
    ]

    def clean_text(t: str) -> str:
        t = norm(t)
        # rimuovi parole UI che cambiano spesso
        junk = ["Rispondi", "Condividi", "Segnala", "Mi piace", "Reply", "Share", "Report", "Like"]
        for j in junk:
            t = t.replace(j, "")
        return norm(t)

    best = None

    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n <= 0:
                continue

            # Scansioniamo i primi elementi (commenti recenti di solito stanno sopra)
            for i in range(min(n, 35)):
                try:
                    raw = loc.nth(i).inner_text(timeout=1200)
                except Exception:
                    continue

                txt = clean_text(raw)
                if len(txt) < 40:
                    continue
                # filtri anti-menu/login
                if "Accedi" in txt and "Registrati" in txt and len(txt) < 250:
                    continue

                # Stabilizziamo: prendiamo solo i primi 900 caratteri
                best = txt[:900]
                break

            if best:
                break
        except Exception:
            continue

    if not best:
        return None, None

    preview = best[:260]
    return sha256(best), preview


def main():
    targets = load_json(TARGETS_FILE, [])
    state = load_json(STATE_FILE, {})
    new_state = dict(state)

    if not targets:
        print("targets.json vuoto: niente da controllare.")
        return

    changes = []  # (name, url, preview)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="it-IT", viewport={"width": 1280, "height": 720})

        # Blocco risorse pesanti per velocizzare
        def route_handler(route):
            try:
                if route.request.resource_type in BLOCK_RESOURCE_TYPES:
                    return route.abort()
                return route.continue_()
            except Exception:
                return route.continue_()

        context.route("**/*", route_handler)
        page = context.new_page()

        for t in targets:
            name = t.get("name", "Senza nome")
            url = (t.get("url") or "").strip()
            if not url:
                continue

            # Solo investing members comments
            if "it.investing.com/members/" not in url or "/comments" not in url:
                print(f"Skip non-investing: {url}")
                continue

            print(f"\nControllo: {name} -> {url}")

            old_sig = state.get(url)

            sig = None
            preview = None
            last_err = None

            for attempt in range(RETRIES_PER_URL + 1):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                    try_click_cookie(page)
                    page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

                    sig, preview = extract_latest_comment_signature(page)
                    if not sig:
                        raise RuntimeError("Non trovo il commento più recente (selettori non matchano).")
                    last_err = None
                    break
                except (PWTimeoutError, Exception) as e:
                    last_err = e
                    print(f"  Tentativo {attempt+1}/{RETRIES_PER_URL+1} fallito: {e}")
                    page.wait_for_timeout(800)

            if last_err is not None and sig is None:
                # Non aggiorniamo lo state se non leggiamo bene (evita sballo + falsi positivi)
                print(f"  Errore definitivo: {last_err}")
                continue

            if old_sig is None:
                print("  Primo avvio: salvo stato (NO notifica).")
                new_state[url] = sig
                continue

            if sig != old_sig:
                print("  CAMBIAMENTO reale: ultimo commento diverso.")
                new_state[url] = sig
                changes.append((name, url, preview))
            else:
                print("  Nessun cambiamento.")
                new_state[url] = old_sig

        context.close()
        browser.close()

    # Notifiche (senza anti-spam che ti azzera tutto)
    if changes:
        if BATCH_MODE == 1:
            # Un solo messaggio riepilogo
            lines = ["🔔 Nuovi commenti rilevati:"]
            for name, url, preview in changes:
                lines.append(f"\n📄 {name}\n🔗 {url}\n📝 {preview}")
            lines.append(f"\n🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
            send_telegram("\n".join(lines))
        else:
            # Una notifica per pagina (come vuoi tu)
            for name, url, preview in changes:
                msg = (
                    f"🔔 Nuovo commento rilevato\n"
                    f"📄 {name}\n"
                    f"🔗 {url}\n"
                    f"📝 {preview}\n"
                    f"🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                )
                send_telegram(msg)
    else:
        print("\nNessun cambiamento in questo run.")

    save_json(STATE_FILE, new_state)


if __name__ == "__main__":
    main()

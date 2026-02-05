import json
import os
import hashlib
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

STATE_FILE = Path("state.json")

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def telegram_send(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("⚠️ Telegram env mancanti (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=15) as resp:
        _ = resp.read()

def normalize_text(s: str) -> str:
    return " ".join(s.strip().split())

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def get_first_comment_text(page) -> str | None:
    # Selector più “generico” possibile per Investing (commento)
    # Se Investing cambia HTML, qui è l’unico punto da sistemare.
    selectors = [
        ".comment_text",                 # spesso presente
        "[data-test='comment-text']",     # a volte presente
        ".js-comment-text",              # fallback
    ]

    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=8000)
            txt = page.locator(sel).first.inner_text()
            txt = normalize_text(txt)
            if txt:
                return txt
        except PWTimeout:
            pass
        except Exception:
            pass
    return None

def main():
    # Metti qui TUTTI i link Investing (comments)
    URLS = [
        "https://it.investing.com/members/266854954/comments",
        # aggiungi gli altri...
    ]

    state = load_state()
    changed_urls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="it-IT",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )

        # blocca risorse pesanti (veloce)
        def route_filter(route):
            rt = route.request.resource_type
            if rt in {"image", "font", "media"}:
                return route.abort()
            return route.continue_()

        context.route("**/*", route_filter)
        page = context.new_page()

        for url in URLS:
            print(f"Controllo: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except PWTimeout:
                print("  timeout (skip)")
                continue
            except Exception:
                print("  errore caricamento (skip)")
                continue

            text = get_first_comment_text(page)
            if not text:
                print("  impossibile leggere commento (skip)")
                continue

            # confronto su contenuto (hash) → niente falsi positivi su “2 ore fa”
            curr = sha1(text)
            prev = state.get(url)

            if prev is None:
                # prima volta: salva senza notificare (evita spam iniziale)
                state[url] = curr
                print("  prima volta: salvo stato (no notifica)")
                continue

            if prev != curr:
                state[url] = curr
                changed_urls.append(url)
                print("  CAMBIAMENTO rilevato.")
            else:
                print("  nessun cambiamento.")

        browser.close()

    # Notifica: una sola per run (anti-spam), ma contiene TUTTI i link cambiati
    if changed_urls:
        msg = "🔔 Cambiamenti rilevati:\n" + "\n".join(changed_urls)
        telegram_send(msg)

    save_state(state)

if __name__ == "__main__":
    main()

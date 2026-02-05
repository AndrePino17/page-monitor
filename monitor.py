import json
import os
import hashlib
import requests
from datetime import datetime

# ===== CONFIG =====

TARGETS_FILE = "targets.json"
STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# ===== FUNZIONI =====

def fetch(url: str) -> str:
    r = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=30
    )
    r.raise_for_status()
    return r.text


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configurato")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
    }

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


# ===== MAIN =====

def main():
    # carica targets
    with open(TARGETS_FILE, "r", encoding="utf-8") as f:
        targets = json.load(f)

    # carica stato precedente
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    new_state = dict(state)

    for t in targets:
        name = t.get("name", "Senza nome")
        url = t["url"]

        print(f"Controllo: {name} -> {url}")

        try:
            content = fetch(url)
            content_hash = sha256(content)

            old_hash = state.get(url)

            # primo avvio: salva e basta
            if old_hash is None:
                print("Primo avvio, salvo stato")
                new_state[url] = content_hash
                continue

            # cambiamento rilevato
            if old_hash != content_hash:
                print("CAMBIAMENTO RILEVATO")

                message = (
                    "🔔 *Nuovo cambiamento rilevato*\n\n"
                    f"📄 {name}\n"
                    f"🔗 {url}\n\n"
                    f"🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                )

                send_telegram(message)
                new_state[url] = content_hash
            else:
                print("Nessun cambiamento")

        except Exception as e:
            print(f"Errore su {url}: {e}")

    # salva nuovo stato
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(new_state, f, indent=2, ensure_ascii=False)

    print("Fine controllo")


if __name__ == "__main__":
    main()

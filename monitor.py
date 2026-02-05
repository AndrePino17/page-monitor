import os
import json
import re
import hashlib
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"

STATE_FILE = "state.json"
TARGETS_FILE = "targets.json"

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=30
    )
    r.raise_for_status()

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def fetch(url: str) -> str:
    r = requests.get(
        url,
        headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"},
        timeout=30
    )
    r.raise_for_status()
    return r.text

def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def extract_investing_latest_comment(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    m = re.search(r"Commenti di .*?\n(.+?)\s+(\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2})\n(.+)", text, re.DOTALL)
    if not m:
        return None

    instrument = m.group(1).strip()
    when = m.group(2).strip()
    rest = m.group(3).strip()
    comment = rest.split("\n", 1)[0].strip()

    payload = f"{instrument} | {when}\n{comment}"
    return {
        "id": sha(payload),
        "instrument": instrument,
        "when": when,
        "comment": comment
    }

def extract_full_page_hash(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return {"id": sha(text)}

def main():
    targets = load_json(TARGETS_FILE, [])
    state = load_json(STATE_FILE, {})

    # primo avvio: salva lo stato e NON manda notifiche (evita spam)
    first_run = (state == {})

    for t in targets:
        name = t["name"]
        url = t["url"]
        ttype = t["type"]

        try:
            html = fetch(url)

            if ttype == "investing_member_comments":
                info = extract_investing_latest_comment(html)
                if not info:
                    if not first_run:
                        tg_send(f"⚠️ Non riesco a leggere l’ultimo commento ({name})\n🔗 {url}")
                    continue

                last_id = state.get(url)
                if last_id != info["id"]:
                    state[url] = info["id"]
                    if not first_run:
                        tg_send(
                            "🆕 Nuovo commento!\n"
                            f"{name}\n"
                            f"{info['instrument']} — {info['when']}\n"
                            f"{info['comment']}\n"
                            f"🔗 {url}"
                        )

            elif ttype == "full_page_hash":
                info = extract_full_page_hash(html)
                last_id = state.get(url)
                if last_id != info["id"]:
                    state[url] = info["id"]
                    if not first_run:
                        tg_send(f"🔔 Pagina cambiata: {name}\n🔗 {url}")

            else:
                if not first_run:
                    tg_send(f"⚠️ Tipo sconosciuto: {ttype}\n{name}\n🔗 {url}")

        except Exception as e:
            if not first_run:
                tg_send(f"❌ Errore su {name}\n🔗 {url}\nDettagli: {e}")

    save_json(STATE_FILE, state)

if __name__ == "__main__":
    main()

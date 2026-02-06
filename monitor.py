import os
import re
import json
import asyncio
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

STATE_FILE = "state.json"
TARGETS_FILE = "targets.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Performance / affidabilità
NAV_TIMEOUT_MS = 25_000
EXTRA_WAIT_MS = 250
MAX_CONCURRENCY = 3  # alza a 4-5 solo se non viene bloccato

COUNT_RE = re.compile(r"Totale\s+dei\s+commenti\s*:\s*(\d+)", re.IGNORECASE)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram non configurato (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID mancanti).")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code != 200:
            print("❌ Telegram error:", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("❌ Telegram exception:", e)
        return False


def normalize_targets(raw: Any) -> List[Dict[str, str]]:
    """
    Accetta:
    - lista di stringhe URL
    - lista di oggetti {name,type,url}
    Ritorna sempre lista di dict con almeno url e name.
    """
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out

    for item in raw:
        if isinstance(item, str):
            out.append({"name": item, "type": "unknown", "url": item})
        elif isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            out.append(
                {
                    "name": str(item.get("name") or url).strip(),
                    "type": str(item.get("type") or "unknown").strip(),
                    "url": url,
                }
            )
    return out


def parse_investing_count_and_first(body_text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Dal testo della pagina (innerText) estrae:
    - Totale dei commenti: N
    - primo commento (preview): prende la prima riga "di contenuto" dopo l’intestazione.
    """
    m = COUNT_RE.search(body_text)
    count = int(m.group(1)) if m else None

    # Ricava primo commento in modo semplice:
    # dopo "Commenti di" la struttura è spesso:
    # [strumento]
    # [tempo]
    # [testo commento]
    lines = [x.strip() for x in body_text.splitlines() if x.strip()]
    first_comment = None

    # trova indice "Commenti di"
    idx = -1
    for i, line in enumerate(lines):
        if line.lower().startswith("commenti di"):
            idx = i
            break

    if idx != -1:
        # scorri le righe successive e prendi la prima che NON è tempo e NON è un titolo/strumento "ovvio"
        time_re = re.compile(r"(\d+\s+(minuti|minuto|ore|ora|giorni|giorno)\s+fa)|(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)
        skip_re = re.compile(r"^(commenti|guida sui commenti|statistiche dell)", re.IGNORECASE)

        # euristica: il commento vero di solito è una riga "normale" non troppo corta
        for j in range(idx + 1, min(idx + 40, len(lines))):
            s = lines[j]
            if skip_re.search(s):
                continue
            if time_re.search(s):
                continue
            # spesso la riga "strumento" è molto breve, tipo "Banco Bpm" (2 parole),
            # mentre il commento può essere corto ma ha più contenuto. Non facciamo i fenomeni:
            # prendiamo la prima riga che non sia chiaramente un header/tempo.
            # Però scartiamo cose troppo "titolo" (solo lettere/spazi e <= 25 char) UNA VOLTA.
            if re.fullmatch(r"[A-Za-zÀ-ÿ0-9 .&'\-]+", s) and len(s) <= 25:
                # probabile titolo asset: skip 1 titolo e continua
                continue

            first_comment = s
            break

    return count, first_comment


async def fetch_text(page, url: str) -> str:
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_timeout(EXTRA_WAIT_MS)
    return await page.evaluate("() => document.body ? document.body.innerText : ''")


async def check_one(browser, target: Dict[str, str], sem: asyncio.Semaphore) -> Dict[str, Any]:
    url = target["url"]
    name = target.get("name", url)
    ttype = target.get("type", "unknown")

    async with sem:
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="it-IT",
            viewport={"width": 1280, "height": 720},
        )

        # blocca roba pesante (speed)
        await context.route(
            "**/*",
            lambda route, req: asyncio.create_task(
                route.abort()
                if req.resource_type in ("image", "media", "font", "stylesheet")
                else route.continue_()
            ),
        )

        page = await context.new_page()

        try:
            body_text = await fetch_text(page, url)

            if ttype == "investing_member_comments":
                count, first_comment = parse_investing_count_and_first(body_text)
                await context.close()
                return {
                    "ok": count is not None,
                    "type": ttype,
                    "url": url,
                    "name": name,
                    "count": count,
                    "first_comment": first_comment,
                    "error": None if count is not None else "Impossibile leggere 'Totale dei commenti' (pattern non trovato).",
                }

            # fallback: full page hash (per test.html)
            h = sha1_text(body_text)
            await context.close()
            return {
                "ok": True,
                "type": "full_page_hash",
                "url": url,
                "name": name,
                "hash": h,
                "error": None,
            }

        except PWTimeoutError:
            await context.close()
            return {"ok": False, "type": ttype, "url": url, "name": name, "error": "Timeout caricamento pagina."}
        except Exception as e:
            await context.close()
            return {"ok": False, "type": ttype, "url": url, "name": name, "error": f"Errore: {e}"}


async def main() -> int:
    raw_targets = load_json(TARGETS_FILE, [])
    targets = normalize_targets(raw_targets)

    if not targets:
        print("❌ targets.json vuoto o non valido.")
        return 1

    state: Dict[str, Any] = load_json(STATE_FILE, {})

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        results = await asyncio.gather(*(check_one(browser, t, sem) for t in targets))
        await browser.close()

    changes_msgs: List[str] = []
    any_state_change = False

    for r in results:
        print(f"Controllo: {r.get('name')} -> {r.get('url')}")

        if not r.get("ok"):
            print(f"  ❌ {r.get('error')}")
            continue

        url = r["url"]
        prev = state.get(url, {})

        if r["type"] == "investing_member_comments":
            new_count = r.get("count")
            prev_count = prev.get("count")

            # salva stato SEMPRE se leggiamo
            state[url] = {
                "type": "investing_member_comments",
                "count": new_count,
                "first_comment_hash": sha1_text(r["first_comment"]) if r.get("first_comment") else None,
                "first_comment_preview": r.get("first_comment"),
                "name": r.get("name"),
            }
            any_state_change = True

            if isinstance(prev_count, int) and isinstance(new_count, int) and new_count > prev_count:
                changes_msgs.append(
                    f"📈 {r.get('name')}\n"
                    f"Totale commenti: {prev_count} → {new_count}\n"
                    f"URL: {url}\n"
                    f"Ultimo commento (preview): {r.get('first_comment') or '(non letto)'}"
                )
                print(f"  ✅ AUMENTO: {prev_count} -> {new_count}")
            else:
                print(f"  - ok, totale={new_count} (prima={prev_count})")

        else:
            # full_page_hash
            new_hash = r.get("hash")
            prev_hash = prev.get("hash")
            state[url] = {"type": "full_page_hash", "hash": new_hash, "name": r.get("name")}
            any_state_change = True

            if prev_hash and new_hash and new_hash != prev_hash:
                changes_msgs.append(f"🔔 Cambiamento pagina: {r.get('name')}\nURL: {url}")
                print("  ✅ CAMBIAMENTO HASH")
            else:
                print("  - ok (hash invariato o primo run)")

    if any_state_change:
        save_json(STATE_FILE, state)

    if changes_msgs:
        msg = "✅ Cambiamenti rilevati:\n\n" + "\n\n".join(changes_msgs)
        send_telegram(msg)
    else:
        print("Nessun cambiamento rilevato -> nessuna notifica.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

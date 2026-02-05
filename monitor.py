import os
import re
import json
import hashlib
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError


STATE_FILE = "state.json"
TARGETS_FILE = "targets.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Limiti per velocità/affidabilità
NAV_TIMEOUT_MS = 25_000
WAIT_AFTER_LOAD_MS = 600   # piccolo buffer per contenuti dinamici
MAX_CONCURRENCY = 3        # aumenta se vuoi, ma Investing può bloccare se esageri


@dataclass
class PageResult:
    url: str
    ok: bool
    count: Optional[int] = None
    first_text: Optional[str] = None
    first_hash: Optional[str] = None
    error: Optional[str] = None


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


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram non configurato (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID mancanti).")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print("❌ Telegram error:", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("❌ Telegram exception:", e)
        return False


def extract_comment_count(html: str) -> Optional[int]:
    """
    Cerca il numero tipo:
      "Totale dei commenti: 793"
      "Totale del commenti: 793"  (a volte scritto così)
    """
    patterns = [
        r"Totale\s+dei\s+commenti\s*:\s*(\d+)",
        r"Totale\s+del\s+commenti\s*:\s*(\d+)",
        r"Totale\s+commenti\s*:\s*(\d+)",
    ]
    for p in patterns:
        m = re.search(p, html, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except:
                pass
    return None


async def extract_first_comment_text(page) -> Optional[str]:
    """
    Heuristics robusta:
    - prova a prendere il primo blocco "commento" visibile
    - altrimenti fallback: prende il primo testo lungo dopo "Commenti di"
    """
    js = """
    () => {
      // prova selettori tipici
      const candidates = [];
      const sels = [
        '[class*="comment"]',
        '[id*="comment"]',
        'article',
        'li'
      ];
      for (const sel of sels) {
        document.querySelectorAll(sel).forEach(el => {
          const t = (el.innerText || '').trim();
          if (t.length > 40 && t.length < 2000) candidates.push({t, el});
        });
      }

      // Ordina: preferisci quelli che contengono una data in formato 04.02.2026
      const dateRe = /\\b\\d{2}\\.\\d{2}\\.\\d{4}\\b/;
      candidates.sort((a,b) => {
        const ad = dateRe.test(a.t) ? 1 : 0;
        const bd = dateRe.test(b.t) ? 1 : 0;
        if (ad !== bd) return bd - ad;
        return b.t.length - a.t.length;
      });

      // Filtra cose chiaramente non commenti
      const badRe = /(Totale\\s+dei\\s+commenti|Guida\\s+sui\\s+Commenti|Statistiche\\s+dell\\'iscritto)/i;

      for (const c of candidates) {
        if (badRe.test(c.t)) continue;

        // Prendiamo solo le prime 3-6 righe per evitare che si porti dietro tutta la pagina
        const lines = c.t.split('\\n').map(x => x.trim()).filter(Boolean);
        const cut = lines.slice(0, 8).join('\\n');
        if (cut.length > 40) return cut;
      }

      // fallback: cerca "Commenti di" e poi prendi il primo chunk di testo significativo
      const body = (document.body && document.body.innerText) ? document.body.innerText : '';
      const idx = body.toLowerCase().indexOf('commenti di');
      if (idx >= 0) {
        const tail = body.slice(idx);
        const parts = tail.split('\\n').map(x => x.trim()).filter(Boolean);
        // salta le prime righe di header e prendi la prima riga "lunga"
        for (const p of parts) {
          if (p.length > 60 && p.length < 800 && !badRe.test(p)) return p;
        }
      }
      return null;
    }
    """
    try:
        txt = await page.evaluate(js)
        if txt:
            return str(txt).strip()
    except:
        pass
    return None


async def fetch_one(browser, url: str, sem: asyncio.Semaphore) -> PageResult:
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

        # blocca robe pesanti -> più veloce
        await context.route("**/*", lambda route, req: asyncio.create_task(
            route.abort()
            if req.resource_type in ("image", "media", "font", "stylesheet")
            else route.continue_()
        ))

        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

            html = await page.content()
            count = extract_comment_count(html)

            first_text = await extract_first_comment_text(page)
            first_hash = sha1(first_text) if first_text else None

            await context.close()

            if count is None:
                return PageResult(url=url, ok=False, error="Impossibile leggere numero commenti (count=None).")
            return PageResult(url=url, ok=True, count=count, first_text=first_text, first_hash=first_hash)

        except PWTimeoutError:
            await context.close()
            return PageResult(url=url, ok=False, error="Timeout caricamento pagina.")
        except Exception as e:
            await context.close()
            return PageResult(url=url, ok=False, error=f"Errore: {e}")


async def main():
    targets = load_json(TARGETS_FILE, [])
    if not isinstance(targets, list) or not targets:
        print(f"❌ targets.json vuoto o non valido: deve essere una LISTA di URL.")
        return 1

    # Stato precedente
    state: Dict[str, Any] = load_json(STATE_FILE, {})

    changes: List[PageResult] = []
    errors: List[PageResult] = []

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [fetch_one(browser, url, sem) for url in targets]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for res in results:
        print(f"Controllo: {res.url}")
        if not res.ok:
            print(f"  ❌ {res.error}")
            errors.append(res)
            continue

        prev = state.get(res.url, {})
        prev_count = prev.get("count")

        # Aggiorna lo stato SEMPRE quando riesce a leggere
        state[res.url] = {
            "count": res.count,
            "first_hash": res.first_hash,
            "first_text_preview": (res.first_text[:160] + "…") if res.first_text and len(res.first_text) > 160 else res.first_text,
        }

        # Regola tua: notifica SOLO se aumenta
        if isinstance(prev_count, int) and res.count is not None and res.count > prev_count:
            print(f"  ✅ AUMENTO: {prev_count} -> {res.count}")
            changes.append(res)
        else:
            # primo run o invariato
            print(f"  - ok, count={res.count} (prima={prev_count})")

    save_json(STATE_FILE, state)

    # Notifiche Telegram
    if changes:
        lines = [f"📈 Nuovi commenti rilevati ({len(changes)} pagine):"]
        for r in changes:
            prev_count = load_json(STATE_FILE, {}).get(r.url, {}).get("count")  # non essenziale
            lines.append(f"\n• {r.url}\n  Nuovo totale: {r.count}\n  Primo commento (preview):\n  {r.first_text[:220] if r.first_text else '(non letto)'}")
        send_telegram("\n".join(lines))
    else:
        print("Nessun aumento commenti -> nessuna notifica.")

    # Se vuoi essere avvisato anche degli errori (opzionale):
    # if errors:
    #     msg = "⚠️ Errori lettura su alcune pagine:\n" + "\n".join([f"• {e.url} -> {e.error}" for e in errors])
    #     send_telegram(msg)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

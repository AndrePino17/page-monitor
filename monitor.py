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

NAV_TIMEOUT_MS = 25_000
WAIT_AFTER_LOAD_MS = 500
MAX_CONCURRENCY = 3

# -------- utils --------

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

# -------- parsing Investing --------

def extract_comment_count(html: str) -> Optional[int]:
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
    js = r"""
    () => {
      const badRe = /(Totale\s+dei\s+commenti|Guida\s+sui\s+Commenti|Statistiche\s+dell'iscritto)/i;

      // Prendi blocchi che sembrano commenti (testo abbastanza lungo)
      const candidates = [];
      const sels = ['[class*="comment"]','[id*="comment"]','article','li','div'];
      for (const sel of sels) {
        document.querySelectorAll(sel).forEach(el => {
          const t = (el.innerText || '').trim();
          if (t.length > 40 && t.length < 2000 && !badRe.test(t)) {
            candidates.push(t);
          }
        });
      }

      // Preferisci testo che contiene una data dd.mm.yyyy
      const dateRe = /\b\d{2}\.\d{2}\.\d{4}\b/;
      candidates.sort((a,b) => {
        const ad = dateRe.test(a) ? 1 : 0;
        const bd = dateRe.test(b) ? 1 : 0;
        if (ad !== bd) return bd - ad;
        return b.length - a.length;
      });

      for (const t of candidates) {
        const lines = t.split('\n').map(x=>x.trim()).filter(Boolean);
        const cut = lines.slice(0, 8).join('\n');
        if (cut.length > 40) return cut;
      }

      // fallback molto grezzo: cerca "Commenti di" e prendi un chunk lungo dopo
      const body = (document.body && document.body.innerText) ? document.body.innerText : '';
      const idx = body.toLowerCase().indexOf('commenti di');
      if (idx >= 0) {
        const tail = body.slice(idx);
        const parts = tail.split('\n').map(x=>x.trim()).filter(Boolean);
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

# -------- model --------

@dataclass
class Target:
    name: str
    type: str
    url: str

@dataclass
class PageResult:
    target: Target
    ok: bool
    count: Optional[int] = None
    first_text: Optional[str] = None
    first_hash: Optional[str] = None
    page_hash: Optional[str] = None
    error: Optional[str] = None

# -------- fetchers --------

async def route_block_heavy(route, request):
    if request.resource_type in ("image", "media", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()

async def fetch_investing(context, t: Target, sem: asyncio.Semaphore) -> PageResult:
    async with sem:
        page = await context.new_page()
        try:
            await page.route("**/*", route_block_heavy)
            await page.goto(t.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

            html = await page.content()
            count = extract_comment_count(html)
            first_text = await extract_first_comment_text(page)
            first_hash = sha1(first_text) if first_text else None

            await page.close()

            if count is None:
                return PageResult(target=t, ok=False, error="Impossibile leggere numero commenti (count=None).")

            return PageResult(target=t, ok=True, count=count, first_text=first_text, first_hash=first_hash)

        except PWTimeoutError:
            try: await page.close()
            except: pass
            return PageResult(target=t, ok=False, error="Timeout caricamento pagina.")
        except Exception as e:
            try: await page.close()
            except: pass
            return PageResult(target=t, ok=False, error=f"Errore: {e}")

async def fetch_full_hash(context, t: Target, sem: asyncio.Semaphore) -> PageResult:
    async with sem:
        page = await context.new_page()
        try:
            await page.route("**/*", route_block_heavy)
            await page.goto(t.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

            html = await page.content()
            # hash dell'intera pagina (puoi anche normalizzare se vuoi)
            page_hash = sha1(html)

            await page.close()
            return PageResult(target=t, ok=True, page_hash=page_hash)

        except PWTimeoutError:
            try: await page.close()
            except: pass
            return PageResult(target=t, ok=False, error="Timeout caricamento pagina.")
        except Exception as e:
            try: await page.close()
            except: pass
            return PageResult(target=t, ok=False, error=f"Errore: {e}")

# -------- main --------

def parse_targets(raw) -> List[Target]:
    out: List[Target] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            # compatibilità vecchio formato: stringhe = full_page_hash
            out.append(Target(name=item, type="full_page_hash", url=item))
        elif isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            name = str(item.get("name", url)).strip()
            typ = str(item.get("type", "full_page_hash")).strip()
            out.append(Target(name=name, type=typ, url=url))
    return out

async def main():
    raw_targets = load_json(TARGETS_FILE, [])
    targets = parse_targets(raw_targets)

    if not targets:
        print("❌ targets.json vuoto o non valido. Deve essere una lista di oggetti {name,type,url}.")
        return 1

    state: Dict[str, Any] = load_json(STATE_FILE, {})

    changes: List[str] = []
    errors: List[str] = []

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ✅ UNA SOLA CONTEXT -> molto più veloce di crearne una per URL
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="it-IT",
            viewport={"width": 1280, "height": 720},
        )

        tasks = []
        for t in targets:
            if t.type == "investing_member_comments":
                tasks.append(fetch_investing(context, t, sem))
            elif t.type == "full_page_hash":
                tasks.append(fetch_full_hash(context, t, sem))
            else:
                # type sconosciuto -> fallback full hash
                tasks.append(fetch_full_hash(context, t, sem))

        results: List[PageResult] = await asyncio.gather(*tasks)

        await context.close()
        await browser.close()

    for res in results:
        t = res.target
        print(f"Controllo: {t.name} -> {t.url}")

        prev = state.get(t.url, {})
        if not res.ok:
            print(f"  ❌ {res.error}")
            errors.append(f"• {t.name}\n  {t.url}\n  {res.error}")
            continue

        # ----- investing: notifica SOLO se aumenta count -----
        if t.type == "investing_member_comments":
            prev_count = prev.get("count")

            state[t.url] = {
                "type": t.type,
                "name": t.name,
                "count": res.count,
                "first_hash": res.first_hash,
                "first_text_preview": (res.first_text[:180] + "…") if res.first_text and len(res.first_text) > 180 else res.first_text,
            }

            if isinstance(prev_count, int) and isinstance(res.count, int) and res.count > prev_count:
                print(f"  ✅ AUMENTO: {prev_count} -> {res.count}")
                preview = res.first_text[:250] if res.first_text else "(testo non letto)"
                changes.append(
                    f"📈 {t.name}\n{t.url}\nTotale commenti: {prev_count} → {res.count}\n\nPrimo commento (preview):\n{preview}"
                )
            else:
                print(f"  - ok, count={res.count} (prima={prev_count})")

        # ----- full_page_hash: notifica se cambia hash -----
        else:
            prev_hash = prev.get("page_hash")

            state[t.url] = {
                "type": t.type,
                "name": t.name,
                "page_hash": res.page_hash,
            }

            if isinstance(prev_hash, str) and isinstance(res.page_hash, str) and res.page_hash != prev_hash:
                print("  ✅ CAMBIO hash pagina")
                changes.append(f"🔔 Pagina cambiata: {t.name}\n{t.url}")
            else:
                print("  - ok, invariata")

    save_json(STATE_FILE, state)

    # Telegram
    if changes:
        msg = "✅ Cambiamenti rilevati:\n\n" + "\n\n".join(changes)
        send_telegram(msg)
    else:
        print("Nessun cambiamento -> nessuna notifica.")

    # opzionale: se vuoi notificare anche errori
    # if errors:
    #     send_telegram("⚠️ Errori su alcune pagine:\n\n" + "\n\n".join(errors))

    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

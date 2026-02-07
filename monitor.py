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

NAV_TIMEOUT_MS = 35_000
EXTRA_WAIT_MS = 1200

# ✅ Riduci al minimo per diminuire probabilità di blocco
MAX_CONCURRENCY = 1

# ✅ Regex robusta: : o ： e numeri con . o ,
COUNT_RE = re.compile(r"Totale\s+dei\s+commenti\s*[:：]\s*([0-9][0-9\.,]*)", re.IGNORECASE)


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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False}
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
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out

    for item in raw:
        if isinstance(item, str):
            out.append({"name": item, "type": "investing_member_comments", "url": item})
        elif isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            out.append(
                {
                    "name": str(item.get("name") or url).strip(),
                    "type": str(item.get("type") or "investing_member_comments").strip(),
                    "url": url,
                }
            )
    return out


def is_challenge(status: Optional[int], title: str, body_text: str) -> bool:
    if status in (401, 403, 429):
        return True
    t = (title or "").lower()
    b = (body_text or "").lower()
    if "ci siamo quasi" in t:
        return True
    if "cloudflare" in b and ("ray id" in b or "utente umano" in b or "controllo aggiuntivo" in b):
        return True
    return False


def parse_count_and_preview(body_text: str) -> Tuple[Optional[int], Optional[str]]:
    m = COUNT_RE.search(body_text)
    count = None
    if m:
        raw = m.group(1).replace(".", "").replace(",", "")
        try:
            count = int(raw)
        except:
            count = None

    # preview “semplice”
    lines = [x.strip() for x in body_text.splitlines() if x.strip()]
    preview = None

    idx = -1
    for i, line in enumerate(lines):
        if line.lower().startswith("commenti di"):
            idx = i
            break

    if idx != -1:
        time_re = re.compile(
            r"(\d+\s+(minuti|minuto|ore|ora|giorni|giorno)\s+fa)|(\d{2}\.\d{2}\.\d{4})",
            re.IGNORECASE,
        )
        skip_re = re.compile(r"^(commenti|guida sui commenti|statistiche dell)", re.IGNORECASE)

        for j in range(idx + 1, min(idx + 80, len(lines))):
            s = lines[j]
            if skip_re.search(s) or time_re.search(s):
                continue
            if len(s) <= 2:
                continue
            preview = s
            break

    return count, preview


async def fetch_page_text(page, url: str) -> Tuple[Optional[int], str, str]:
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_timeout(EXTRA_WAIT_MS)

    status = resp.status if resp else None
    title = await page.title()
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    return status, title, text


async def check_one(context, sem: asyncio.Semaphore, target: Dict[str, str]) -> Dict[str, Any]:
    url = target["url"]
    name = target.get("name", url)

    async with sem:
        # ✅ piccolo delay fisso per evitare “raffica”
        await asyncio.sleep(1.2)

        page = await context.new_page()
        try:
            # 1° tentativo
            status, title, text = await fetch_page_text(page, url)

            # retry 1 volta (pulito)
            if is_challenge(status, title, text):
                await page.close()
                await asyncio.sleep(2.0)
                page = await context.new_page()
                status, title, text = await fetch_page_text(page, url)

            # se ancora challenge: salva debug e stop
            if is_challenge(status, title, text):
                h = sha1_text(url)
                try:
                    await page.screenshot(path=f"debug_cf_{h}.png", full_page=True)
                    html = await page.content()
                    with open(f"debug_cf_{h}.html", "w", encoding="utf-8") as f:
                        f.write(html)
                except:
                    pass

                return {
                    "ok": False,
                    "url": url,
                    "name": name,
                    "error": f"Challenge/blocco (status={status}, title={title})",
                }

            # parsing
            count, preview = parse_count_and_preview(text)
            if count is None:
                h = sha1_text(url)
                try:
                    with open(f"debug_parse_{h}.txt", "w", encoding="utf-8") as f:
                        f.write(text[:20000])
                except:
                    pass

                return {
                    "ok": False,
                    "url": url,
                    "name": name,
                    "error": f"Pagina ok ma non trovo 'Totale dei commenti' (status={status}, title={title})",
                }

            return {
                "ok": True,
                "url": url,
                "name": name,
                "count": count,
                "preview": preview,
            }

        except PWTimeoutError:
            return {"ok": False, "url": url, "name": name, "error": "Timeout"}
        except Exception as e:
            return {"ok": False, "url": url, "name": name, "error": str(e)}
        finally:
            try:
                await page.close()
            except:
                pass


async def main() -> int:
    raw_targets = load_json(TARGETS_FILE, [])
    targets = normalize_targets(raw_targets)
    if not targets:
        print("❌ targets.json vuoto/non valido.")
        return 1

    state: Dict[str, Any] = load_json(STATE_FILE, {})
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])

        # ✅ CONTEXT UNICO = cookie/session riusati tra URL
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="it-IT",
            viewport={"width": 1280, "height": 720},
        )

        # blocca solo risorse pesanti
        async def route_handler(route):
            rt = route.request.resource_type
            if rt in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_handler)

        results = await asyncio.gather(*(check_one(context, sem, t) for t in targets))

        await context.close()
        await browser.close()

    changes_msgs: List[str] = []
    blocked: List[str] = []
    parse_errors: List[str] = []
    any_state_change = False

    for r in results:
        if not r.get("ok"):
            err = r.get("error", "errore sconosciuto")
            print("❌", r.get("name"), err)
            if "Challenge/blocco" in err:
                blocked.append(f"- {r.get('name')} | {r.get('url')}")
            else:
                parse_errors.append(f"- {r.get('name')} | {err}")
            continue

        url = r["url"]
        name = r.get("name", url)
        new_count = r.get("count")
        prev_count = state.get(url, {}).get("count")

        state[url] = {
            "type": "investing_member_comments",
            "count": new_count,
            "preview": r.get("preview"),
            "name": name,
        }
        any_state_change = True

        if isinstance(prev_count, int) and isinstance(new_count, int) and new_count > prev_count:
            changes_msgs.append(
                f"📈 {name}\n"
                f"Totale commenti: {prev_count} → {new_count}\n"
                f"URL: {url}\n"
                f"Preview: {r.get('preview') or '(n/a)'}"
            )

    if any_state_change:
        save_json(STATE_FILE, state)

    # ✅ Notifiche
    if changes_msgs:
        send_telegram("✅ Cambiamenti rilevati:\n\n" + "\n\n".join(changes_msgs))

    if blocked:
        send_telegram(
            "⛔ Blocco Cloudflare rilevato in questo run.\n"
            f"Pagine bloccate: {len(blocked)}/{len(targets)}\n\n"
            + "\n".join(blocked[:10])
        )

    # manda parse_errors solo se non è un run “quasi tutto bloccato”
    if parse_errors and len(blocked) < len(targets) // 2:
        send_telegram(
            "⚠️ Alcune pagine non sono parseabili (layout/testo diverso):\n"
            + "\n".join(parse_errors[:10])
        )

    print("✅ Fine run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

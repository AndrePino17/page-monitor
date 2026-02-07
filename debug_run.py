import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

TARGETS_FILE = "targets.json"
OUT_DIR = Path("debug_out")
OUT_DIR.mkdir(exist_ok=True)

NAV_TIMEOUT_MS = 45_000
WAIT_MS = 1500


def load_targets():
    with open(TARGETS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    urls = []
    for x in raw:
        if isinstance(x, str):
            urls.append(x)
        elif isinstance(x, dict) and x.get("url"):
            urls.append(x["url"])
    return urls


async def try_accept_cookies(page):
    # tentativo "soft" (se non trova nulla, non fa errori)
    patterns = [
        r"Accetta",
        r"Accetto",
        r"Accetta tutto",
        r"Accetta e continua",
        r"I Agree",
        r"Accept",
        r"Agree",
    ]
    for pat in patterns:
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I))
            await btn.click(timeout=1500)
            await page.wait_for_timeout(800)
            return True
        except:
            pass
    return False


async def main():
    urls = load_targets()[:3]  # prova su 3 pagine (puoi mettere [:1] per test super veloce)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="it-IT",
            viewport={"width": 1400, "height": 900},
        )

        page = await context.new_page()

        for i, url in enumerate(urls, start=1):
            print(f"\n=== DEBUG {i}: {url} ===")
            info = {"url": url}

            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await page.wait_for_timeout(WAIT_MS)

                # prova a cliccare banner cookie se c'è
                accepted = await try_accept_cookies(page)
                if accepted:
                    # ricarica per vedere se ora compare contenuto vero
                    try:
                        resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                        await page.wait_for_timeout(WAIT_MS)
                    except:
                        pass

                info["cookie_clicked"] = accepted
                info["response_status"] = resp.status if resp else None
                info["final_url"] = page.url
                info["title"] = await page.title()

                # Salva screenshot
                shot_path = OUT_DIR / f"page_{i}.png"
                await page.screenshot(path=str(shot_path), full_page=True)

                # Salva HTML
                html = await page.content()
                html_path = OUT_DIR / f"page_{i}.html"
                html_path.write_text(html, encoding="utf-8")

                # Salva testo
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                txt_path = OUT_DIR / f"page_{i}.txt"
                txt_path.write_text(text, encoding="utf-8")

                # Piccolo riassunto in console
                lines = [l for l in text.splitlines() if l.strip()]
                preview = "\n".join(lines[:35])

                print("status:", info["response_status"])
                print("final_url:", info["final_url"])
                print("title:", info["title"])
                print("cookie_clicked:", info["cookie_clicked"])
                print("preview:\n", preview)

                # Segnali utili
                info["contains_totale_commenti"] = "Totale dei commenti" in text
                info["preview_first_lines"] = lines[:40]

            except Exception as e:
                info["error"] = str(e)
                print("ERROR:", e)

            (OUT_DIR / f"page_{i}_meta.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
scraper_tennis.py v2 — con logging dettagliato per debug struttura HTML
"""

import re
import logging
import subprocess
import sys
import random
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

def _ensure_chromium():
    import os
    chromium_path = os.path.expanduser("~/.cache/ms-playwright/chromium-1117/chrome-linux/chrome")
    if not os.path.exists(chromium_path):
        logger.info("Chromium non trovato — installazione...")
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True)
            subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=True, capture_output=True)
            logger.info("Chromium installato")
        except subprocess.CalledProcessError as e:
            logger.error(f"Errore Chromium: {e.stderr.decode()}")

_ensure_chromium()

PROXIES = [
    {"server": "http://38.154.203.95:5863",   "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://198.105.121.200:6462",  "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://64.137.96.74:6641",     "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://209.127.138.10:5784",   "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://38.154.185.97:6370",    "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://84.247.60.125:6095",    "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://142.111.67.146:5611",   "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://191.96.254.138:6185",   "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://31.58.9.4:6077",        "username": "mztegxef", "password": "2pb5l0wg2h7w"},
    {"server": "http://198.46.161.42:5092",    "username": "mztegxef", "password": "2pb5l0wg2h7w"},
]

ALLOWED_CIRCUITS = ["atp", "wta", "challenger", "itf"]
BLOCKED_CIRCUITS = ["utr", "exhibition", "laver cup", "united cup", "davis cup", "fed cup", "bjk cup"]
URL_PROGRAMME = "https://www.tennisexplorer.com/matches/"


def _is_allowed_circuit(t: str) -> bool:
    t = t.lower()
    for b in BLOCKED_CIRCUITS:
        if b in t: return False
    for a in ALLOWED_CIRCUITS:
        if a in t: return True
    return False


async def _launch(proxy: dict):
    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        proxy=proxy
    )
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-US",
    )
    page = await ctx.new_page()
    return p, browser, page


async def _try_scrape(proxy: dict) -> list:
    p, browser, page = await _launch(proxy)
    matches = []
    try:
        resp = await page.goto(URL_PROGRAMME, timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        logger.info(f"[{proxy['server']}] status={status}")
        if status == 403:
            return None  # segnale: prova altro proxy

        await page.wait_for_timeout(3000)
        html = await page.content()
        logger.info(f"HTML len={len(html)}")

        if len(html) < 2000:
            logger.warning(f"HTML troppo corto: {html[:300]}")
            return None

        # LOG struttura per debug
        all_rows = await page.query_selector_all("tr")
        logger.info(f"TR totali: {len(all_rows)}")

        # Log classi delle prime 20 righe
        sample_classes = []
        for row in all_rows[:20]:
            cls = await row.get_attribute("class") or ""
            sample_classes.append(repr(cls))
        logger.info(f"Classi prime 20 TR: {sample_classes}")

        # Log link player trovati
        player_links_els = await page.query_selector_all("a[href*='/player/']")
        logger.info(f"Link /player/ trovati: {len(player_links_els)}")
        if player_links_els:
            sample = await player_links_els[0].inner_text()
            logger.info(f"Primo /player/: {sample}")

        # Log tabelle
        tables = await page.query_selector_all("table")
        logger.info(f"Tabelle: {len(tables)}")
        for t in tables[:5]:
            cls = await t.get_attribute("class") or ""
            tid = await t.get_attribute("id") or ""
            logger.info(f"  table class={cls!r} id={tid!r}")

        # Parsing
        current_tournament = ""
        current_surface = ""
        current_time = ""

        for row in all_rows:
            cls = await row.get_attribute("class") or ""

            # Riga torneo — prova con classi più ampie
            if any(x in cls for x in ["head", "bg", "title", "tourn", "h-"]):
                cells = await row.query_selector_all("td")
                if cells:
                    txt = (await cells[0].inner_text()).strip()
                    if not txt:
                        continue
                    m = re.search(r"\(([^)]+)\)\s*$", txt)
                    if m:
                        surf = m.group(1).lower()
                        current_surface = "Clay" if "clay" in surf else "Hard" if "hard" in surf else "Grass" if "grass" in surf else surf.capitalize()
                        current_tournament = txt[:m.start()].strip(" -–")
                    else:
                        current_tournament = txt
                        current_surface = ""
                continue

            # Match — cerca link /player/
            player_links = await row.query_selector_all("a[href*='/player/']")
            if len(player_links) < 2:
                continue

            home = (await player_links[0].inner_text()).strip()
            away = (await player_links[1].inner_text()).strip()
            if not home or not away:
                continue

            # Orario
            cells = await row.query_selector_all("td")
            for cell in cells[:3]:
                t2 = (await cell.inner_text()).strip()
                if re.match(r"\d{1,2}:\d{2}", t2):
                    current_time = t2
                    break

            # URL match
            ml = await row.query_selector("a[href*='/match/']")
            te_url = ""
            if ml:
                href = await ml.get_attribute("href") or ""
                te_url = f"https://www.tennisexplorer.com{href}" if href.startswith("/") else href

            if current_tournament and not _is_allowed_circuit(current_tournament):
                continue

            matches.append({
                "home": home, "away": away,
                "tournament": current_tournament,
                "surface": current_surface,
                "time": current_time,
                "te_url": te_url,
            })

        logger.info(f"Match trovati: {len(matches)}")
        return matches

    except Exception as e:
        logger.error(f"Errore scraping: {e}")
        return None
    finally:
        await browser.close()
        await p.stop()


async def scrape_programme() -> list:
    proxies = PROXIES.copy()
    random.shuffle(proxies)
    for proxy in proxies:
        result = await _try_scrape(proxy)
        if result is None:
            continue  # prova prossimo proxy
        return result
    logger.error("Tutti i proxy falliti")
    return []


async def scrape_match_html(te_url: str) -> str:
    if not te_url:
        return ""
    odds_url = te_url.rstrip("/") + "/odds/"
    proxy = random.choice(PROXIES)
    p, browser, page = await _launch(proxy)
    try:
        await page.goto(odds_url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        html = await page.content()
        await browser.close()
        await p.stop()
        return html
    except Exception as e:
        logger.error(f"Errore odds {odds_url}: {e}")
        try:
            await browser.close()
            await p.stop()
        except Exception:
            pass
        return ""


def format_programme_telegram(matches: list) -> str:
    if not matches:
        return "Nessun match trovato per oggi nei circuiti selezionati."
    today = datetime.now().strftime("%d/%m/%Y")
    lines = [
        f"<b>PROGRAMMA TENNIS — {today}</b>",
        f"<i>{len(matches)} match | ATP · WTA · Challenger · ITF</i>",
        "",
    ]
    current_tournament = ""
    for i, m in enumerate(matches, 1):
        if m["tournament"] != current_tournament:
            current_tournament = m["tournament"]
            surf = f" · {m['surface']}" if m["surface"] else ""
            lines.append(f"\n<b>{current_tournament}{surf}</b>")
        time_str = f"  {m['time']}" if m["time"] else ""
        lines.append(f"{i}. {m['home']} vs {m['away']}{time_str}")
    lines.append("")
    lines.append("Usa /analizza [numero] per analizzare un match.")
    return "\n".join(lines)

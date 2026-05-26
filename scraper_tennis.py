"""
scraper_tennis.py — TennisExplorer daily programme scraper
Estrae match del giorno: ATP, WTA, Challenger, ITF
"""

import re
import logging
import subprocess
import sys
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

def _ensure_chromium():
    import os
    chromium_path = os.path.expanduser("~/.cache/ms-playwright/chromium-1117/chrome-linux/chrome")
    if not os.path.exists(chromium_path):
        logger.info("Chromium non trovato — installazione in corso...")
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True)
            subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=True, capture_output=True)
            logger.info("Chromium installato")
        except subprocess.CalledProcessError as e:
            logger.error(f"Errore installazione Chromium: {e.stderr.decode()}")

_ensure_chromium()

ALLOWED_CIRCUITS = ["atp", "wta", "challenger", "itf"]
BLOCKED_CIRCUITS = ["utr", "exhibition", "laver cup", "united cup", "davis cup", "fed cup", "bjk cup"]
URL_PROGRAMME = "https://www.tennisexplorer.com/matches/"


def _is_allowed_circuit(tournament_text: str) -> bool:
    t = tournament_text.lower()
    for blocked in BLOCKED_CIRCUITS:
        if blocked in t:
            return False
    for allowed in ALLOWED_CIRCUITS:
        if allowed in t:
            return True
    return False


async def _get_browser_page():
    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    )
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-US",
    )
    page = await context.new_page()
    return p, browser, page


async def debug_page_html() -> str:
    """Scarica HTML grezzo della pagina programma per debug."""
    p, browser, page = await _get_browser_page()
    try:
        await page.goto(URL_PROGRAMME, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        html = await page.content()
        await browser.close()
        await p.stop()
        return html
    except Exception as e:
        await browser.close()
        await p.stop()
        return f"ERRORE: {e}"


async def scrape_programme() -> list[dict]:
    matches = []
    p, browser, page = await _get_browser_page()

    try:
        await page.goto(URL_PROGRAMME, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Dump struttura per capire i selettori reali
        html = await page.content()
        logger.info(f"HTML length: {len(html)}")

        # Prova selettori alternativi comuni su TennisExplorer
        # 1. Cerca righe con classe contenente "r_" o simili
        # 2. Cerca tabelle con id specifici

        # Selettori da provare in ordine
        selectors_to_try = [
            "table.result tr",
            "table#matches tr",
            ".matches-table tr",
            "tr.one", "tr.two",
            "div.matches tr",
            "table tr",
        ]

        rows = []
        for sel in selectors_to_try:
            try:
                found = await page.query_selector_all(sel)
                if found and len(found) > 5:
                    logger.info(f"Selettore funzionante: {sel} → {len(found)} righe")
                    rows = found
                    break
                else:
                    logger.info(f"Selettore {sel} → {len(found)} risultati")
            except Exception as e:
                logger.info(f"Selettore {sel} → errore: {e}")

        if not rows:
            logger.warning("Nessun selettore ha trovato righe — uso fallback testo")
            await browser.close()
            await p.stop()
            return []

        current_tournament = ""
        current_surface = ""
        current_time = ""

        for row in rows:
            try:
                cls = await row.get_attribute("class") or ""

                # Riga torneo
                if any(x in cls for x in ["head", "tournament", "bg_", "title"]):
                    cells = await row.query_selector_all("td")
                    if cells:
                        txt = await cells[0].inner_text()
                        txt = txt.strip()
                        surface_match = re.search(r"\(([^)]+)\)\s*$", txt)
                        if surface_match:
                            surf_raw = surface_match.group(1).lower()
                            if "clay" in surf_raw: current_surface = "Clay"
                            elif "hard" in surf_raw: current_surface = "Hard"
                            elif "grass" in surf_raw: current_surface = "Grass"
                            elif "carpet" in surf_raw: current_surface = "Carpet"
                            else: current_surface = surf_raw.capitalize()
                            current_tournament = txt[:surface_match.start()].strip(" -–")
                        else:
                            current_tournament = txt
                            current_surface = ""
                    continue

                # Riga match — cerca link giocatori
                player_links = await row.query_selector_all("a[href*='/player/']")
                if len(player_links) < 2:
                    continue

                home_name = (await player_links[0].inner_text()).strip()
                away_name = (await player_links[1].inner_text()).strip()

                if not home_name or not away_name:
                    continue

                # Orario
                cells = await row.query_selector_all("td")
                for cell in cells[:3]:
                    txt = (await cell.inner_text()).strip()
                    if re.match(r"\d{1,2}:\d{2}", txt):
                        current_time = txt
                        break

                # URL match
                match_link = await row.query_selector("a[href*='/match/']")
                te_url = ""
                if match_link:
                    href = await match_link.get_attribute("href")
                    if href:
                        te_url = f"https://www.tennisexplorer.com{href}" if href.startswith("/") else href

                if not _is_allowed_circuit(current_tournament):
                    continue

                matches.append({
                    "home": home_name,
                    "away": away_name,
                    "tournament": current_tournament,
                    "surface": current_surface,
                    "time": current_time,
                    "te_url": te_url,
                })

            except Exception as e:
                logger.debug(f"Errore riga: {e}")
                continue

    except Exception as e:
        logger.error(f"Errore scrape_programme: {e}")
    finally:
        await browser.close()
        await p.stop()

    logger.info(f"Trovati {len(matches)} match")
    return matches


async def scrape_match_html(te_url: str) -> str:
    if not te_url:
        return ""

    odds_url = te_url.rstrip("/") + "/odds/"
    p, browser, page = await _get_browser_page()

    try:
        await page.goto(odds_url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        html = await page.content()
        await browser.close()
        await p.stop()
        return html
    except Exception as e:
        logger.error(f"Errore scraping odds {odds_url}: {e}")
        await browser.close()
        await p.stop()
        return ""


def format_programme_telegram(matches: list[dict]) -> str:
    if not matches:
        return "❌ Nessun match trovato per oggi nei circuiti selezionati."

    today = datetime.now().strftime("%d/%m/%Y")
    lines = [f"🎾 <b>PROGRAMMA TENNIS — {today}</b>", f"<i>{len(matches)} match | ATP · WTA · Challenger · ITF</i>", ""]

    current_tournament = ""
    for i, m in enumerate(matches, 1):
        if m["tournament"] != current_tournament:
            current_tournament = m["tournament"]
            surf = f" · {m['surface']}" if m["surface"] else ""
            lines.append(f"\n🏆 <b>{current_tournament}{surf}</b>")
        time_str = f"⏰ {m['time']}" if m["time"] else ""
        lines.append(f"{i}. {m['home']} vs {m['away']}  {time_str}")

    lines.append("")
    lines.append("Usa /analizza [numero] per analizzare un match specifico.")
    return "\n".join(lines)

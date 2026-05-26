"""
scraper_tennis.py — TennisExplorer daily programme scraper
Estrae match del giorno: ATP, WTA, Challenger, ITF
Playwright headless — compatibile Railway
"""

import re
import logging
import subprocess
import sys
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# Installa Chromium al primo import se non presente
def _ensure_chromium():
    import os
    chromium_path = os.path.expanduser("~/.cache/ms-playwright/chromium-1117/chrome-linux/chrome")
    if not os.path.exists(chromium_path):
        logger.info("Chromium non trovato — installazione in corso...")
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True, capture_output=True
            )
            subprocess.run(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                check=True, capture_output=True
            )
            logger.info("Chromium installato con successo")
        except subprocess.CalledProcessError as e:
            logger.error(f"Errore installazione Chromium: {e.stderr.decode()}")

_ensure_chromium()

# Circuit whitelist (case-insensitive match)
ALLOWED_CIRCUITS = ["atp", "wta", "challenger", "itf"]

# Circuit blacklist esplicita
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


async def scrape_programme() -> list[dict]:
    """
    Scrapa il programma giornaliero da TennisExplorer.
    Restituisce lista di dict con:
      - home, away, tournament, surface, time, te_url
    """
    matches = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        try:
            await page.goto(URL_PROGRAMME, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_selector("table.result", timeout=15000)
        except PWTimeout:
            logger.error("Timeout caricamento TennisExplorer")
            await browser.close()
            return []
        except Exception as e:
            logger.error(f"Errore goto TennisExplorer: {e}")
            await browser.close()
            return []

        rows = await page.query_selector_all("table.result tr")

        current_tournament = ""
        current_surface = ""
        current_time = ""

        for row in rows:
            cls = await row.get_attribute("class") or ""

            if "head" in cls or "tournament" in cls:
                cells = await row.query_selector_all("td")
                if cells:
                    txt = await cells[0].inner_text()
                    txt = txt.strip()
                    surface_match = re.search(r"\(([^)]+)\)\s*$", txt)
                    if surface_match:
                        surf_raw = surface_match.group(1).lower()
                        if "clay" in surf_raw:
                            current_surface = "Clay"
                        elif "hard" in surf_raw:
                            current_surface = "Hard"
                        elif "grass" in surf_raw:
                            current_surface = "Grass"
                        elif "carpet" in surf_raw:
                            current_surface = "Carpet"
                        else:
                            current_surface = surf_raw.capitalize()
                        current_tournament = txt[:surface_match.start()].strip(" -–")
                    else:
                        current_tournament = txt
                        current_surface = ""
                continue

            if "one" in cls or "two" in cls or "bott" in cls or cls == "" or "r1" in cls or "r2" in cls:
                cells = await row.query_selector_all("td")
                if len(cells) < 3:
                    continue

                time_text = await cells[0].inner_text()
                time_text = time_text.strip()
                if re.match(r"\d{2}:\d{2}", time_text):
                    current_time = time_text

                player_links = await row.query_selector_all("a.t-name, a[href*='/player/']")
                if len(player_links) < 2:
                    continue

                home_name = (await player_links[0].inner_text()).strip()
                away_name = (await player_links[1].inner_text()).strip()

                match_link = await row.query_selector("a[href*='/match/']")
                te_url = ""
                if match_link:
                    href = await match_link.get_attribute("href")
                    if href:
                        te_url = f"https://www.tennisexplorer.com{href}" if href.startswith("/") else href

                if not home_name or not away_name:
                    continue

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

        await browser.close()

    logger.info(f"Trovati {len(matches)} match dopo filtro circuito")
    return matches


async def scrape_match_html(te_url: str) -> str:
    """
    Scarica l'HTML della pagina quote di un match specifico su TennisExplorer.
    """
    if not te_url:
        return ""

    odds_url = te_url.rstrip("/") + "/odds/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        try:
            await page.goto(odds_url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_selector("#oddsMenu-1-data", timeout=15000)
        except PWTimeout:
            logger.warning(f"Timeout odds page: {odds_url}")
            await browser.close()
            return ""
        except Exception as e:
            logger.error(f"Errore scraping odds {odds_url}: {e}")
            await browser.close()
            return ""

        html = await page.content()
        await browser.close()

    return html


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

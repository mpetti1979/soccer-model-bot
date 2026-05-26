"""
scraper_tennis.py v3 — parser corretto per struttura reale TennisExplorer
Classi TR: head flags (torneo), one/two/one fRow bott/two fRow bott (match)
"""

import re
import logging
import subprocess
import sys
import random
from datetime import datetime
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

def _ensure_chromium():
    import os
    path = os.path.expanduser("~/.cache/ms-playwright/chromium-1117/chrome-linux/chrome")
    if not os.path.exists(path):
        logger.info("Installazione Chromium...")
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True)
            subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=True, capture_output=True)
        except Exception as e:
            logger.error(f"Errore Chromium: {e}")

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
FAV_MIN_QUOTA = 1.36


def _is_allowed_circuit(t: str) -> bool:
    if not t:
        return True  # se non sappiamo il torneo, includiamo
    t = t.lower()
    for b in BLOCKED_CIRCUITS:
        if b in t: return False
    for a in ALLOWED_CIRCUITS:
        if a in t: return True
    return False  # circuito non riconosciuto = escludi


def _parse_surface(txt: str) -> tuple:
    """Estrae superficie e nome torneo da stringa tipo 'ATP Rome (Clay)'."""
    m = re.search(r"\(([^)]+)\)\s*$", txt)
    if m:
        surf_raw = m.group(1).lower()
        if "clay" in surf_raw: surface = "Clay"
        elif "hard" in surf_raw: surface = "Hard"
        elif "grass" in surf_raw: surface = "Grass"
        elif "carpet" in surf_raw: surface = "Carpet"
        else: surface = surf_raw.capitalize()
        tournament = txt[:m.start()].strip(" -–")
    else:
        tournament = txt.strip()
        surface = ""
    return tournament, surface


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


async def scrape_programme() -> list:
    proxies = PROXIES.copy()
    random.shuffle(proxies)

    for proxy in proxies:
        logger.info(f"Proxy: {proxy['server']}")
        p, browser, page = await _launch(proxy)
        matches = []
        try:
            resp = await page.goto(URL_PROGRAMME, timeout=30000, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            if status == 403:
                logger.warning(f"403 su {proxy['server']}")
                await browser.close(); await p.stop()
                continue

            await page.wait_for_timeout(3000)
            html = await page.content()
            if len(html) < 5000:
                logger.warning(f"HTML corto ({len(html)})")
                await browser.close(); await p.stop()
                continue

            logger.info(f"HTML OK: {len(html)} chars")

            # Struttura TennisExplorer:
            # TR class="head flags" → riga torneo (td[1] = nome torneo)
            # TR class="one fRow bott" / "one" / "two" / "two bott" → righe match

            all_rows = await page.query_selector_all("table.result tr")
            logger.info(f"Righe table.result: {len(all_rows)}")

            current_tournament = ""
            current_surface = ""
            current_time = ""

            for row in all_rows:
                cls = await row.get_attribute("class") or ""

                # Riga torneo: contiene "head"
                if "head" in cls:
                    # Il nome torneo è nel link o nel testo della prima td
                    # Struttura: <td class="..."><a href="...">Nome Torneo (Surface)</a></td>
                    link = await row.query_selector("td a")
                    if link:
                        txt = (await link.inner_text()).strip()
                    else:
                        cells = await row.query_selector_all("td")
                        txt = (await cells[0].inner_text()).strip() if cells else ""

                    if txt:
                        current_tournament, current_surface = _parse_surface(txt)
                        logger.debug(f"Torneo: {current_tournament} | {current_surface}")
                    current_time = ""
                    continue

                # Righe match: contiene "one" o "two"
                if not ("one" in cls or "two" in cls):
                    continue

                # Cerca link giocatori
                player_links = await row.query_selector_all("a[href*='/player/']")
                if len(player_links) < 2:
                    # Alcune righe hanno solo 1 giocatore (riga "one" con orario)
                    # L'orario è spesso nella riga "one fRow"
                    cells = await row.query_selector_all("td")
                    for cell in cells[:3]:
                        t2 = (await cell.inner_text()).strip()
                        if re.match(r"\d{1,2}:\d{2}", t2):
                            current_time = t2
                            break
                    continue

                home = (await player_links[0].inner_text()).strip()
                away = (await player_links[1].inner_text()).strip()
                if not home or not away:
                    continue

                # Orario (nelle prime celle)
                cells = await row.query_selector_all("td")
                for cell in cells[:4]:
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

                # Filtro circuito
                if not _is_allowed_circuit(current_tournament):
                    continue

                matches.append({
                    "home": home,
                    "away": away,
                    "tournament": current_tournament,
                    "surface": current_surface,
                    "time": current_time,
                    "te_url": te_url,
                })

            logger.info(f"Match trovati: {len(matches)}")
            await browser.close()
            await p.stop()

            if matches:
                return matches

            # Se 0 match ma HTML ok, c'è ancora un problema di parsing — ritorna lista vuota
            # invece di riprovare con altro proxy (stesso risultato)
            logger.warning("0 match con HTML valido — possibile problema struttura")
            return []

        except Exception as e:
            logger.error(f"Errore: {e}")
            try:
                await browser.close(); await p.stop()
            except Exception:
                pass
            continue

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
        await browser.close(); await p.stop()
        return html
    except Exception as e:
        logger.error(f"Errore odds: {e}")
        try:
            await browser.close(); await p.stop()
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

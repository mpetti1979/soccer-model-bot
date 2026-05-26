"""
scraper_tennis.py v5 — parser corretto: home e away su righe separate
Struttura TennisExplorer:
  TR class="head flags"     → torneo
  TR class="one fRow bott"  → home player + orario
  TR class="one"            → away player
  TR class="two fRow bott"  → home player + orario (match successivo)
  TR class="two"            → away player
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


def _is_allowed_circuit(t: str) -> bool:
    if not t: return True
    t = t.lower()
    # Escludi solo circuiti esplicitamente bloccati
    for b in BLOCKED_CIRCUITS:
        if b in t: return False
    # Includi tutto il resto (ATP, WTA, Challenger, ITF, slam, ecc.)
    return True


def _parse_surface(txt: str) -> tuple:
    m = re.search(r"\(([^)]+)\)\s*$", txt)
    if m:
        surf = m.group(1).lower()
        surface = "Clay" if "clay" in surf else "Hard" if "hard" in surf else "Grass" if "grass" in surf else "Carpet" if "carpet" in surf else surf.capitalize()
        return txt[:m.start()].strip(" -–"), surface
    return txt.strip(), ""


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
                await browser.close(); await p.stop()
                continue

            await page.wait_for_timeout(3000)
            html = await page.content()
            if len(html) < 5000:
                await browser.close(); await p.stop()
                continue

            rows = await page.query_selector_all("table.result tr")
            logger.info(f"Righe table.result: {len(rows)}")

            current_tournament = ""
            current_surface = ""
            current_time = ""
            pending_home = None  # nome giocatore home in attesa dell'away
            pending_time = ""
            pending_url = ""

            for row in rows:
                cls = await row.get_attribute("class") or ""

                # ── Riga torneo ──
                if "head" in cls:
                    link = await row.query_selector("a")
                    if link:
                        txt = (await link.inner_text()).strip()
                    else:
                        cells = await row.query_selector_all("td")
                        txt = (await cells[0].inner_text()).strip() if cells else ""
                    if txt:
                        current_tournament, current_surface = _parse_surface(txt)
                    pending_home = None
                    continue

                # ── Riga home (fRow = first row del match) ──
                if "fRow" in cls:
                    pending_home = None  # reset match precedente incompleto
                    player_link = await row.query_selector("a[href*='/player/']")
                    if not player_link:
                        continue
                    pending_home = (await player_link.inner_text()).strip()

                    # Orario: prima cella
                    cells = await row.query_selector_all("td")
                    pending_time = ""
                    for cell in cells[:3]:
                        t2 = (await cell.inner_text()).strip()
                        if re.match(r"\d{1,2}:\d{2}", t2):
                            pending_time = t2
                            break

                    # URL match (secondo link spesso)
                    all_links = await row.query_selector_all("a")
                    pending_url = ""
                    for lnk in all_links:
                        href = await lnk.get_attribute("href") or ""
                        if "/match/" in href:
                            pending_url = f"https://www.tennisexplorer.com{href}" if href.startswith("/") else href
                            break
                    continue

                # ── Riga away (riga successiva senza fRow) ──
                if ("one" in cls or "two" in cls) and "fRow" not in cls and pending_home:
                    player_link = await row.query_selector("a[href*='/player/']")
                    if not player_link:
                        pending_home = None
                        continue
                    away = (await player_link.inner_text()).strip()

                    if not _is_allowed_circuit(current_tournament):
                        pending_home = None
                        continue

                    matches.append({
                        "home": pending_home,
                        "away": away,
                        "tournament": current_tournament,
                        "surface": current_surface,
                        "time": pending_time,
                        "te_url": pending_url,
                    })
                    pending_home = None
                    continue

            logger.info(f"Match trovati: {len(matches)}")
            await browser.close()
            await p.stop()
            if matches:
                return matches
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

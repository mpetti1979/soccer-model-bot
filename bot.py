"""
Multi-Sport Betting Analysis Bot — v4.0
Sports: Tennis (LBA Pinnacle Workflow v2.6) + Calcio (Soccer Model Protocol v1.4)
Protocolli letti da Google Drive
"""

import os
import re
import base64
import logging
import asyncio
import json
import math
from datetime import datetime
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

DRIVE_FILE_SOCCER = "13cLHKqF_CJlU9M3YpL2p6JOHfPVn_ONx"
DRIVE_FILE_LBA    = "1CCtNowTNMYqSO-N6Km13jC9iJByWwaqyx4nThKZd680"

SHEET_ID = "1LFWu2qK42cVQDh9-keT23_M0tydr9CERPStKt_8OnFQ"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Cache protocolli (evita chiamate Drive ripetute)
_protocol_cache: dict[str, str] = {}

# ─────────────────────────────────────────────
# GOOGLE DRIVE — lettura protocolli .md
# ─────────────────────────────────────────────

def get_drive_service():
    if not GOOGLE_CREDENTIALS_JSON:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Drive service error: {e}")
        return None


def fetch_protocol(file_id: str) -> str:
    """Scarica un file da Drive (Google Doc o file caricato) e lo restituisce come testo."""
    if file_id in _protocol_cache:
        return _protocol_cache[file_id]
    service = get_drive_service()
    if not service:
        return "[Protocollo non disponibile — credenziali Drive mancanti]"
    try:
        # Prima controlla il mimeType del file
        meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime = meta.get("mimeType", "")
        logger.info(f"Protocollo {file_id} — tipo: {mime}")

        if mime == "application/vnd.google-apps.document":
            # Google Doc: esporta come testo plain
            request = service.files().export_media(fileId=file_id, mimeType="text/plain")
        else:
            # File caricato (.md, .txt, ecc.): download diretto
            request = service.files().get_media(fileId=file_id)

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        text = buf.getvalue().decode("utf-8", errors="replace")
        _protocol_cache[file_id] = text
        logger.info(f"Protocollo {file_id} caricato ({len(text)} chars)")
        return text
    except Exception as e:
        logger.error(f"Drive fetch error {file_id}: {e}")
        return f"[Errore lettura protocollo da Drive: {e}]"


def get_lba_protocol() -> str:
    return fetch_protocol(DRIVE_FILE_LBA)


def get_soccer_protocol() -> str:
    return fetch_protocol(DRIVE_FILE_SOCCER)


def clear_protocol_cache():
    _protocol_cache.clear()


# ─────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────

SHEET_HEADERS_TENNIS = [
    "data", "home", "away", "torneo", "verdetto",
    "lato_giocato", "quota_giocata", "esito", "pl"
]
SHEET_HEADERS_SOCCER = [
    "data", "home", "away", "verdetto_1x2", "verdetto_uo",
    "quota_giocata", "mercato", "esito", "pl"
]

def get_sheet(tab: str):
    if not GOOGLE_CREDENTIALS_JSON:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        headers = SHEET_HEADERS_TENNIS if tab == "Tennis" else SHEET_HEADERS_SOCCER
        try:
            ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(tab, rows=1000, cols=len(headers))
            ws.append_row(headers)
        return ws
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return None


# ─────────────────────────────────────────────
# PARSER HTML TENNISEXPLORER
# ─────────────────────────────────────────────

RETAIL_EXCLUDE = {"Pinnacle", "Betfair", "SBOBET", "Matchbook"}

def parse_tennisexplorer(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1", class_="bg")
    players = h1.get_text(strip=True).split(" - ") if h1 else ["Home", "Away"]
    home_name = players[0].strip() if len(players) > 0 else "Home"
    away_name = players[1].strip() if len(players) > 1 else "Away"

    match_info = ""
    detail_div = soup.find("div", class_="box boxBasic")
    if detail_div:
        match_info = detail_div.get_text(separator=" ", strip=True)[:200]

    odds_div = soup.find("div", id="oddsMenu-1-data")
    if not odds_div:
        return {"error": "Sezione odds non trovata nell'HTML"}
    table = odds_div.find("table")
    if not table:
        return {"error": "Tabella odds non trovata"}

    books = {}
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        first_classes = " ".join(cells[0].get("class", []))
        if "first" not in first_classes or "tl" not in first_classes:
            continue
        book_name = cells[0].get_text(strip=True)
        if not book_name or len(cells) < 3:
            continue

        def get_current(cell):
            raw = cell.decode_contents().split("<table")[0]
            m = re.search(r"(\d\.\d+)", raw)
            return float(m.group()) if m else None

        def get_history_flat(start_idx):
            history = []
            i = start_idx
            while i < len(cells):
                txt = cells[i].get_text(strip=True)
                if txt == "Opening odds":
                    if i + 2 < len(cells):
                        m = re.search(r"(\d\.\d+)", cells[i + 2].get_text())
                        if m:
                            history.append({"time": "open", "q": float(m.group())})
                    break
                elif re.match(r"\d{2}\.\d{2}\.", txt):
                    if i + 1 < len(cells):
                        m = re.search(r"(\d\.\d+)", cells[i + 1].get_text())
                        if m:
                            history.append({"time": txt, "q": float(m.group())})
                    i += 3
                    continue
                i += 1
            return history

        home_current = get_current(cells[1])
        away_current = None
        away_cell_idx = None
        for ci in range(2, len(cells)):
            raw = cells[ci].decode_contents()
            if "odds-in" in raw:
                m = re.search(r"(\d\.\d+)", raw.split("<table")[0])
                if m:
                    away_current = float(m.group())
                    away_cell_idx = ci + 1
                    break

        home_history = get_history_flat(2)
        home_open = next((h["q"] for h in home_history if h["time"] == "open"), None)
        away_history = get_history_flat(away_cell_idx) if away_cell_idx else []
        away_open = next((h["q"] for h in away_history if h["time"] == "open"), None)

        if home_current is None or away_current is None:
            continue

        books[book_name] = {
            "home_current": home_current,
            "away_current": away_current,
            "home_open": home_open,
            "away_open": away_open,
            "home_history": home_history,
            "away_history": away_history,
        }

    if not books:
        return {"error": "Nessun bookmaker estratto"}

    retail = {k: v for k, v in books.items() if k not in RETAIL_EXCLUDE}
    pinnacle = books.get("Pinnacle", {})

    def mean(vals):
        vals = [v for v in vals if v]
        return round(sum(vals) / len(vals), 3) if vals else None

    retail_home_curr = mean([v["home_current"] for v in retail.values()])
    retail_away_curr = mean([v["away_current"] for v in retail.values()])
    retail_home_open = mean([v["home_open"] for v in retail.values() if v["home_open"]])
    retail_away_open = mean([v["away_open"] for v in retail.values() if v["away_open"]])

    all_home = [v["home_current"] for v in books.values() if v["home_current"]]
    all_away = [v["away_current"] for v in books.values() if v["away_current"]]
    max_home = max(all_home) if all_home else None
    max_away = max(all_away) if all_away else None
    max_home_book = next((k for k, v in books.items() if v["home_current"] == max_home), "")
    max_away_book = next((k for k, v in books.items() if v["away_current"] == max_away), "")

    pinn_home_curr = pinnacle.get("home_current")
    pinn_away_curr = pinnacle.get("away_current")
    pinn_home_open = pinnacle.get("home_open")
    pinn_away_open = pinnacle.get("away_open")

    gap_home = round(pinn_home_curr - retail_home_curr, 3) if pinn_home_curr and retail_home_curr else None
    gap_away = round(pinn_away_curr - retail_away_curr, 3) if pinn_away_curr and retail_away_curr else None

    all_home_excl = [v["home_current"] for k, v in books.items() if k != "Pinnacle" and v["home_current"]]
    all_away_excl = [v["away_current"] for k, v in books.items() if k != "Pinnacle" and v["away_current"]]
    outlier_home = bool(pinn_home_curr and all_home_excl and pinn_home_curr >= max(all_home_excl))
    outlier_away = bool(pinn_away_curr and all_away_excl and pinn_away_curr >= max(all_away_excl))

    pinn_drift_home = round(pinn_home_curr - pinn_home_open, 3) if pinn_home_curr and pinn_home_open else None
    pinn_drift_away = round(pinn_away_curr - pinn_away_open, 3) if pinn_away_curr and pinn_away_open else None
    retail_drift_home = round(retail_home_curr - retail_home_open, 3) if retail_home_curr and retail_home_open else None
    retail_drift_away = round(retail_away_curr - retail_away_open, 3) if retail_away_curr and retail_away_open else None

    def pinn_combo(po, ro, pc, rc):
        if not all([po, ro, pc, rc]):
            return "N/A"
        oa = po >= ro
        ca = pc >= rc
        if oa and ca: return "GUIDA"
        elif oa and not ca: return "ANTICIPA"
        elif not oa and ca: return "ENTRA_TARDI"
        else: return "INSEGUE"

    combo_home = pinn_combo(pinn_home_open, retail_home_open, pinn_home_curr, retail_home_curr)
    combo_away = pinn_combo(pinn_away_open, retail_away_open, pinn_away_curr, retail_away_curr)

    def detect_pattern(history):
        quotes = [h["q"] for h in history if h["time"] != "open"]
        if not quotes or len(quotes) < 2:
            return "FLAT"
        total_move = quotes[-1] - quotes[0]
        if abs(total_move) < 0.03:
            return "FLAT"
        diffs = [quotes[i+1] - quotes[i] for i in range(len(quotes)-1)]
        signs = [1 if d > 0.005 else -1 if d < -0.005 else 0 for d in diffs]
        signs = [s for s in signs if s != 0]
        if not signs:
            return "FLAT"
        if len(signs) >= 3:
            early = signs[:-2]
            late = signs[-2:]
            if sum(early) > 0 and sum(late) < 0: return "INV"
            if sum(early) < 0 and sum(late) > 0: return "INV"
        if len(set(signs)) > 1:
            if len(diffs) >= 2 and abs(diffs[-1]) >= 0.05: return "SPIKE"
            return "RIM"
        if all(s > 0 for s in signs): return "UNI+"
        if all(s < 0 for s in signs): return "UNI-"
        return "FLAT"

    pattern_home = detect_pattern(pinnacle.get("home_history", []))
    pattern_away = detect_pattern(pinnacle.get("away_history", []))

    return {
        "home_name": home_name,
        "away_name": away_name,
        "match_info": match_info,
        "books": books,
        "pinnacle": {
            "home_curr": pinn_home_curr, "away_curr": pinn_away_curr,
            "home_open": pinn_home_open, "away_open": pinn_away_open,
            "drift_home": pinn_drift_home, "drift_away": pinn_drift_away,
            "outlier_home": outlier_home, "outlier_away": outlier_away,
        },
        "retail": {
            "home_curr": retail_home_curr, "away_curr": retail_away_curr,
            "home_open": retail_home_open, "away_open": retail_away_open,
            "drift_home": retail_drift_home, "drift_away": retail_drift_away,
        },
        "max_home": {"q": max_home, "book": max_home_book},
        "max_away": {"q": max_away, "book": max_away_book},
        "gap_pinn_vs_retail": {"home": gap_home, "away": gap_away},
        "combo": {"home": combo_home, "away": combo_away},
        "pattern": {"home": pattern_home, "away": pattern_away},
    }


def build_tennis_summary(data: dict) -> str:
    p = data["pinnacle"]
    r = data["retail"]
    g = data["gap_pinn_vs_retail"]
    lines = [
        f"MATCH: {data['home_name']} (Home) vs {data['away_name']} (Away)",
        f"INFO: {data.get('match_info', 'N/A')[:150]}",
        "",
        "=== QUOTA MAX ATTUALE ===",
        f"Home MAX: {data['max_home']['q']} ({data['max_home']['book']})",
        f"Away MAX: {data['max_away']['q']} ({data['max_away']['book']})",
        "",
        "=== PINNACLE ===",
        f"Home: apertura={p['home_open']} → attuale={p['home_curr']} | drift={p['drift_home']}",
        f"Away: apertura={p['away_open']} → attuale={p['away_curr']} | drift={p['drift_away']}",
        f"Outlier Home: {p['outlier_home']} | Outlier Away: {p['outlier_away']}",
        f"Pattern Home: {data['pattern']['home']} | Pattern Away: {data['pattern']['away']}",
        f"Combo Home: {data['combo']['home']} | Combo Away: {data['combo']['away']}",
        "",
        "=== RETAIL MEDIA ===",
        f"Home: apertura={r['home_open']} → attuale={r['home_curr']} | drift={r['drift_home']}",
        f"Away: apertura={r['away_open']} → attuale={r['away_curr']} | drift={r['drift_away']}",
        "",
        "=== GAP PINNACLE vs RETAIL ===",
        f"Home: {g['home']} | Away: {g['away']}",
        "",
        "=== TUTTI I BOOKMAKER ===",
    ]
    for book, v in data["books"].items():
        lines.append(f"{book}: Home={v['home_current']} Away={v['away_current']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# CLAUDE API CALLS
# ─────────────────────────────────────────────

async def claude_call(system: str, user_content, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 2000) -> str:
    """Chiama Claude con system prompt e contenuto utente (testo o lista multimodale)."""
    def _call():
        msgs = [{"role": "user", "content": user_content}]
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=msgs
        )
    try:
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=120.0)
        return response.content[0].text
    except asyncio.TimeoutError:
        return "⏱ Timeout analisi (120s). Riprova con /analisi."
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return f"❌ Errore API: {str(e)[:200]}"


def make_image_block(img_b64: str, mime: str = "image/jpeg") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}}


def make_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


# ─────────────────────────────────────────────
# TENNIS — ANALISI
# ─────────────────────────────────────────────

TENNIS_QUICK_SYSTEM = """Sei un analista di scommesse tennis. Produci una risposta RAPIDA e CONCISA.

Dati disponibili: HTML TennisExplorer (strutturati) e/o screenshot AsianOdds (visiva).

REGOLE CRITICHE LBA — MAI SBAGLIARE:
1. OUTLIER: Pinnacle MAX su lato X significa che gli sharp hanno già giocato MASSICCIAMENTE sul lato OPPOSTO. Outlier Home = PRO Away. Outlier Away = PRO Home. MAI interpretare outlier come valore sul lato outlier stesso.
2. DRIFT Pinnacle: quota sale su X = sharp giocano su Y (opposto). Quota scende su X = sharp giocano su X.
3. FAV: sempre il giocatore con quota Pinnacle più BASSA.
4. VERDETTO FINALE: deve essere coerente con i segnali. Se outlier è su Home → segnale PRO Away → verdetto su Away.

FORMATTAZIONE: usa solo tag HTML Telegram. <b>grassetto</b> per valori chiave. Niente asterischi, niente ##, niente tabelle.

Formato risposta quick — SOLO questo blocco:

🎾 <b>[Home] vs [Away]</b> | [Torneo/N/D]
📅 [Data/N/D]

⚡ <b>QUICK VERDICT</b>
FAV: <b>[giocatore più basso Pinnacle] @ [quota]</b>
Flusso sharp: <b>[FORTE/MEDIO/DEBOLE/ASSENTE]</b>
Outlier Pinnacle: [SÌ lato X / NO]
Drift Pinna: Home=[valore] Away=[valore]
Pattern: Home=[tipo] Away=[tipo]

<b>SEGNALI</b>
- Outlier/Flusso sharp: <b>[X/5]</b>
- Drift Pinnacle: <b>[X/5]</b>
- Pattern: <b>[X/5]</b>
- Gap Pinna/retail: <b>[X/5]</b>
─────────────────
Totale: <b>[X/20]</b>

🎯 <b>[giocatore] | [✅ GIOCA / ⚠️ ATTENZIONE / ❌ NO BET]</b>
Motivazione: [1 riga max]

Criteri rating X/5: 5=segnale fortissimo e univoco, 4=forte, 3=moderato, 2=debole, 1=quasi assente, 0=assente o contraddittorio.
Soglie totale (base 20): 17-20=⚡MOLTO FORTE, 13-16=✅FORTE, 8-12=⚠️MEDIO, 0-7=❌DEBOLE"""

TENNIS_EXTENDED_SYSTEM = """Sei un analista di scommesse tennis. La quick analysis è già stata fatta. Approfondisci SOLO i punti critici seguendo il protocollo LBA.

PROTOCOLLO:
{protocol}

REGOLE CRITICHE — MAI SBAGLIARE:
- Outlier su X = sharp su Y (opposto). MAI sul lato outlier.
- Pinnacle sale su X = sharp su Y. Pinnacle scende su X = sharp su X.

STRUTTURA OUTPUT — esattamente questa, niente di più:

<b>APPROFONDIMENTO LAYER</b>

<b>§2 Gap + Max quota</b>
[1-2 righe: gap numerico, outlier, interpretazione operativa]
Rating: <b>[X/5]</b>

<b>§3 Comportamento retail</b>
[1-2 righe: retail segue/oppone/fermo, cosa significa]
Rating: <b>[X/5]</b>

<b>§4-5 Drift + Pattern</b>
[1-2 righe: movimento Pinnacle, pattern, timing]
Rating: <b>[X/5]</b>

<b>§6 Posizione Pinnacle</b>
[1 riga: GUIDA/ENTRA_TARDI/ANTICIPA/INSEGUE e peso]
Rating: <b>[X/5]</b>

[Se Betfair disponibile:]
<b>§8 Betfair</b>
[1-2 righe: squilibrio volume, direzione, conferma o conflitto]
Rating: <b>[X/5]</b>

─────────────────
<b>STAKE LAYERS §10</b>
- Flusso sharp (drift converge): [✅/❌] +0.25u
- Gap≥10tick: [✅/❌] +0.25u
- OLS (se presente): [✅/❌/N/A] +0.25u
- Line AH (se presente): [✅/❌/N/A] +0.25u
- Betfair (se presente): [✅/❌/N/A] +0.25u
Stake totale: <b>[X.XX]u</b>

<b>CONFLITTI/NOTE</b>
[Solo se ci sono segnali contraddittori — altrimenti ometti questa sezione]

🎯 <b>[giocatore] | [✅ GIOCA / ⚠️ ATTENZIONE / ❌ NO BET]</b>

FORMATTAZIONE: solo tag HTML Telegram. Niente asterischi, ##, tabelle pipe."""

TENNIS_RECAP_SYSTEM = """Produci SOLO il recap Telegram nel formato standard LBA. Niente altro.

FORMATTAZIONE: usa SOLO tag HTML Telegram. Niente asterischi, niente ##.

Formato ESATTO:
🎾 <b>[TORNEO — ROUND | Superficie]</b>
🗓 [Data] | [Orario]
<b>[Giocatore1] 🏳 vs [Giocatore2] 🏳</b>

[Testo narrativo analisi — 3-5 righe, plain text]

⭐ Flusso sharp: <b>[X/5]</b>
⭐ Drift/Pattern: <b>[X/5]</b>
⭐ Exchange Betfair: <b>[X/5]</b> (ometti se non disponibile)
⭐ OLS: <b>[X/5]</b> (ometti se non disponibile)

🎯 <b>[Giocatore]</b> | Quota ~<b>[X.XX]</b>
📦 Stake: <b>[X.XX]u</b>"""


async def tennis_quick(state: dict) -> str:
    html_data = state.get("html_data")
    screenshots = state.get("screenshots", [])

    today = datetime.now().strftime("%d/%m/%Y")
    content = []
    content.append(make_text_block(f"DATA PARTITA: {today}"))
    if html_data:
        content.append(make_text_block("=== DATI HTML TENNISEXPLORER ===\n" + build_tennis_summary(html_data)))
    for img_b64, mime in screenshots:
        content.append(make_text_block("=== SCREENSHOT ASIANODDS ==="))
        content.append(make_image_block(img_b64, mime))
    if not content or len(content) == 1:
        return "❌ Nessun dato disponibile."

    return await claude_call(TENNIS_QUICK_SYSTEM, content, model="claude-haiku-4-5-20251001", max_tokens=1500)


async def tennis_extended(state: dict) -> str:
    protocol = get_lba_protocol()
    system = TENNIS_EXTENDED_SYSTEM.format(protocol=protocol)
    html_data = state.get("html_data")
    screenshots = state.get("screenshots", [])

    today = datetime.now().strftime("%d/%m/%Y")
    content = []
    content.append(make_text_block(f"DATA PARTITA: {today}"))
    if html_data:
        content.append(make_text_block("=== DATI HTML TENNISEXPLORER ===\n" + build_tennis_summary(html_data)))
    for img_b64, mime in screenshots:
        content.append(make_text_block("=== SCREENSHOT ASIANODDS ==="))
        content.append(make_image_block(img_b64, mime))
    if not content or len(content) == 1:
        return "❌ Nessun dato disponibile."

    return await claude_call(system, content, model="claude-haiku-4-5-20251001", max_tokens=2000)


async def tennis_recap(state: dict) -> str:
    last_analysis = state.get("last_extended", "")
    if not last_analysis:
        return "❌ Fai prima /analisi."
    content = [make_text_block(f"Analisi precedente:\n{last_analysis}\n\nProduci il recap Telegram.")]
    return await claude_call(TENNIS_RECAP_SYSTEM, content, model="claude-haiku-4-5-20251001", max_tokens=800)


# ─────────────────────────────────────────────
# CALCIO — ANALISI
# ─────────────────────────────────────────────

SOCCER_QUICK_SYSTEM = """Sei un analista di scommesse calcio. Produci una risposta RAPIDA dalla screenshot TOS.

Leggi dalla screenshot i dati TOS: Tot Vol, Sel Vol, price, book%, implied%, bookImp diff, impDifToNow, bookDiffToNow, PinnyPrice per tutti i selezionati (1, X, 2 e U/O 2.5).

FORMATTAZIONE: usa solo tag HTML Telegram. <b>grassetto</b> per valori chiave. Niente asterischi, niente ##, niente tabelle.

Formato risposta quick — SOLO questo blocco:

⚽ <b>[Home] vs [Away]</b> | [Torneo/N/D]
📅 [Data/N/D]

⚡ <b>QUICK 1X2</b>
PinnyPrice: 1=<b>[quota]</b> X=<b>[quota]</b> 2=<b>[quota]</b>
bookImp diff: 1=[val] X=[val] 2=[val]
impDifToNow: 1=[val] X=[val] 2=[val]
Flusso sharp: [descrizione 1 riga]

<b>SEGNALI 1X2</b>
- L1 Grafico: <b>[X/5]</b>
- L3 bookImpDiff: <b>[X/5]</b>
- L4 bookDiffToNow: <b>[X/5]</b>
- L5 impDifToNow: <b>[X/5]</b>
- L6 PinnyPrice gap: <b>[X/5]</b>
─────────────────
Totale: <b>[X/25]</b>
🎯 1X2: <b>[1/X/2 o NO BET]</b> | [Motivazione 1 riga]

⚡ <b>QUICK U/O 2.5</b>
PinnyPrice: U=<b>[quota]</b> O=<b>[quota]</b>
bookImp diff: U=[val] O=[val]
impDifToNow: U=[val] O=[val]

<b>SEGNALI U/O</b>
- L1 Grafico: <b>[X/5]</b>
- L3 bookImpDiff: <b>[X/5]</b>
- L4 bookDiffToNow: <b>[X/5]</b>
- L5 impDifToNow: <b>[X/5]</b>
- L6 PinnyPrice gap: <b>[X/5]</b>
─────────────────
Totale: <b>[X/25]</b>
🎯 U/O: <b>[Under/Over o NO BET]</b> | [Motivazione 1 riga]

Criteri rating X/5: 5=segnale fortissimo univoco, 4=forte, 3=moderato, 2=debole, 1=quasi assente, 0=assente/contraddittorio.
Soglie totale /25: 21-25=⚡MOLTO FORTE, 16-20=✅FORTE, 10-15=⚠️MEDIO, 0-9=❌DEBOLE"""

SOCCER_EXTENDED_SYSTEM = """Sei un analista di scommesse calcio. La quick analysis è già stata fatta. Approfondisci per 1X2 e U/O 2.5 seguendo il Soccer Model Protocol.

PROTOCOLLO:
{protocol}

STRUTTURA OUTPUT — esattamente questa per ogni mercato:

<b>ANALISI 1X2</b>

<b>L1 Grafico</b>: [classificazione: confermante/neutro/contraddittorio + motivazione 1 riga]
Rating: <b>[X/5]</b>

<b>L2 Volume</b>: [Tot Vol, Sel Vol dominante, concentrazione]
Rating: <b>[X/5]</b>

<b>L3 bookImpDiff</b>: [trend dx→sx, pattern, interpretazione]
Rating: <b>[X/5]</b>

<b>L4 bookDiffToNow</b>: [positivo/negativo, stabile/cala, R.10 se applicabile]
Rating: <b>[X/5]</b>

<b>L5 impDifToNow</b>: [valore attuale, sale/scende/piatto, R.14/R.15 se applicabile]
Rating: <b>[X/5]</b>

<b>L6 PinnyPrice gap</b>: [gap%, override se applicabile]
Rating: <b>[X/5]</b>

<b>L7 Chart</b>: [confermante/neutro/contraddittorio]
Rating: <b>[X/5]</b>

─────────────────
Totale 1X2: <b>[X/35]</b>
Grading: <b>[AAA/AA/A/B/C/NOP]</b>
🎯 <b>[1/X/2 o NOP]</b>

<b>ANALISI U/O 2.5</b>
[stessa struttura L1-L7]
─────────────────
Totale U/O: <b>[X/35]</b>
Grading: <b>[AAA/AA/A/B/C/NOP]</b>
🎯 <b>[Under/Over o NOP]</b>

Soglie grading: 30-35=AAA, 24-29=AA, 18-23=A, 12-17=B, 6-11=C, 0-5=NOP
FORMATTAZIONE: solo tag HTML Telegram. Niente asterischi, ##, tabelle pipe."""


async def soccer_quick(state: dict) -> str:
    screenshots = state.get("screenshots", [])
    if not screenshots:
        return "❌ Nessuna screenshot TOS."
    today = datetime.now().strftime("%d/%m/%Y")
    content = []
    content.append(make_text_block(f"DATA PARTITA: {today}"))
    for img_b64, mime in screenshots:
        content.append(make_image_block(img_b64, mime))
    content.append(make_text_block("Produci la quick analysis 1X2 e U/O 2.5 dal TOS."))
    return await claude_call(SOCCER_QUICK_SYSTEM, content, model="claude-haiku-4-5-20251001", max_tokens=1500)


async def soccer_extended(state: dict) -> str:
    protocol = get_soccer_protocol()
    system = SOCCER_EXTENDED_SYSTEM.format(protocol=protocol)
    screenshots = state.get("screenshots", [])
    if not screenshots:
        return "❌ Nessuna screenshot TOS."
    today = datetime.now().strftime("%d/%m/%Y")
    content = []
    content.append(make_text_block(f"DATA PARTITA: {today}"))
    for img_b64, mime in screenshots:
        content.append(make_image_block(img_b64, mime))
    content.append(make_text_block("Applica il Soccer Model Protocol completo con tutti i layer. Analizza 1X2 e U/O 2.5."))
    return await claude_call(system, content, model="claude-haiku-4-5-20251001", max_tokens=2000)


# ─────────────────────────────────────────────
# USER STATE
# ─────────────────────────────────────────────

user_state: dict[int, dict] = {}

def get_state(uid: int) -> dict:
    if uid not in user_state:
        user_state[uid] = {
            "mode": None,           # "tennis" | "calcio"
            "html_data": None,      # dati parsati HTML tennis
            "screenshots": [],      # lista di (b64, mime)
            "last_quick": "",
            "last_extended": "",
        }
    return user_state[uid]

def reset_state(uid: int):
    user_state[uid] = {
        "mode": None,
        "html_data": None,
        "screenshots": [],
        "last_quick": "",
        "last_extended": "",
    }


# ─────────────────────────────────────────────
# HANDLERS COMANDI
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Multi-Sport Bet Analyzer v4.0*\n\n"
        "Comandi:\n"
        "• /tennis — avvia modalità tennis\n"
        "• /calcio — avvia modalità calcio\n"
        "• /analisi — analisi estesa (dopo go)\n"
        "• /recap — recap Telegram (solo tennis)\n"
        "• /protocollo lba — leggi protocollo LBA\n"
        "• /protocollo soccer — leggi protocollo Soccer\n"
        "• /reset — azzera stato\n\n"
        "Workflow:\n"
        "1️⃣ Scegli /tennis o /calcio\n"
        "2️⃣ Carica file/screenshot\n"
        "3️⃣ Scrivi *go* per la quick analysis",
        parse_mode="Markdown"
    )


async def cmd_tennis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reset_state(uid)
    get_state(uid)["mode"] = "tennis"
    await update.message.reply_text(
        "🎾 *Modalità TENNIS attiva*\n\n"
        "Carica in qualsiasi ordine:\n"
        "• Screenshot AsianOdds (Moneyline + opzionale AH)\n"
        "• File HTML TennisExplorer\n\n"
        "Poi scrivi *go* per la quick analysis.",
        parse_mode="Markdown"
    )


async def cmd_calcio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reset_state(uid)
    get_state(uid)["mode"] = "calcio"
    await update.message.reply_text(
        "⚽ *Modalità CALCIO attiva*\n\n"
        "Carica la screenshot TOS (MATCH_ODDS + OVER_UNDER).\n\n"
        "Poi scrivi *go* per la quick analysis.",
        parse_mode="Markdown"
    )


async def cmd_analisi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    mode = state.get("mode")

    if not mode:
        await update.message.reply_text("❌ Prima scegli /tennis o /calcio.")
        return
    if not state.get("last_quick"):
        await update.message.reply_text("❌ Prima esegui una quick analysis (go).")
        return

    await update.message.reply_text("⏳ Analisi estesa in corso...")

    if mode == "tennis":
        result = await tennis_extended(state)
    else:
        result = await soccer_extended(state)

    state["last_extended"] = result

    # Telegram ha limite 4096 chars — splitta se necessario
    for chunk in split_message(result):
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(chunk, parse_mode="HTML")


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    if state.get("mode") != "tennis":
        await update.message.reply_text("❌ Il recap è disponibile solo in modalità tennis.")
        return
    await update.message.reply_text("⏳ Generazione recap...")
    result = await tennis_recap(state)
    try:
        await update.message.reply_text(result, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(result, parse_mode="HTML")


async def cmd_protocollo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Uso: /protocollo lba oppure /protocollo soccer")
        return

    sport = args[0].lower()
    await update.message.reply_text("⏳ Caricamento protocollo da Drive...")

    if sport == "lba":
        text = get_lba_protocol()
        title = "📋 LBA Pinnacle Workflow v2.6"
    elif sport == "soccer":
        text = get_soccer_protocol()
        title = "📋 Soccer Model Protocol v1.4"
    else:
        await update.message.reply_text("❌ Usa: /protocollo lba oppure /protocollo soccer")
        return

    header = f"{title}\n{'─'*40}\n"
    full = header + text
    for chunk in split_message(full):
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(chunk, parse_mode="HTML")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reset_state(uid)
    await update.message.reply_text("✅ Stato azzerato. Scegli /tennis o /calcio.")


async def cmd_aggiorna_protocolli(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forza ricaricamento protocolli da Drive (svuota cache)."""
    clear_protocol_cache()
    await update.message.reply_text("🔄 Cache protocolli svuotata. Prossima analisi rileggerà da Drive.")


# ─────────────────────────────────────────────
# HANDLERS MESSAGGI
# ─────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    doc = update.message.document

    if state.get("mode") != "tennis":
        await update.message.reply_text("❌ Prima avvia /tennis per caricare file HTML.")
        return

    if not doc.file_name.endswith(".html"):
        await update.message.reply_text("❌ Manda un file .html di TennisExplorer.")
        return

    await update.message.reply_text("⏳ Parsing HTML...")
    file = await doc.get_file()
    html_bytes = await file.download_as_bytearray()
    html = html_bytes.decode("utf-8", errors="replace")
    data = parse_tennisexplorer(html)

    if "error" in data:
        await update.message.reply_text(f"❌ Errore parsing: {data['error']}")
        return

    state["html_data"] = data
    p = data["pinnacle"]
    await update.message.reply_text(
        f"✅ HTML caricato: *{data['home_name']} vs {data['away_name']}*\n"
        f"Pinnacle: Home {p['home_open']}→{p['home_curr']} | Away {p['away_open']}→{p['away_curr']}\n"
        f"MAX: Home {data['max_home']['q']} | Away {data['max_away']['q']}\n\n"
        "Puoi aggiungere screenshot AsianOdds oppure scrivi *go*.",
        parse_mode="Markdown"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    mode = state.get("mode")

    if not mode:
        await update.message.reply_text("❌ Prima scegli /tennis o /calcio.")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(img_bytes).decode()

    state["screenshots"].append((img_b64, "image/jpeg"))
    n = len(state["screenshots"])

    if mode == "tennis":
        await update.message.reply_text(
            f"✅ Screenshot #{n} caricata (AsianOdds).\n"
            "Puoi aggiungere altre screenshot o HTML, oppure scrivi *go*.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ Screenshot #{n} caricata (TOS).\n"
            "Puoi aggiungere altre screenshot oppure scrivi *go*.",
            parse_mode="Markdown"
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    text = update.message.text.strip().lower()

    if text == "go":
        mode = state.get("mode")
        if not mode:
            await update.message.reply_text("❌ Prima scegli /tennis o /calcio.")
            return

        has_data = state.get("html_data") or state.get("screenshots")
        if not has_data:
            await update.message.reply_text("❌ Carica prima almeno un file o una screenshot.")
            return

        await update.message.reply_text("⚡ Quick analysis in corso...")

        if mode == "tennis":
            result = await tennis_quick(state)
        else:
            result = await soccer_quick(state)

        state["last_quick"] = result

        try:
            await update.message.reply_text(result, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(result, parse_mode="HTML")

        await update.message.reply_text(
            "Comandi disponibili:\n"
            "• /analisi — analisi estesa con protocollo completo\n"
            + ("• /recap — recap Telegram\n" if mode == "tennis" else "")
            + "• /reset — nuova partita"
        )
        return

    await update.message.reply_text(
        "Comandi disponibili:\n"
        "• /tennis — modalità tennis\n"
        "• /calcio — modalità calcio\n"
        "• *go* — avvia analisi\n"
        "• /analisi — analisi estesa\n"
        "• /recap — recap Telegram (tennis)\n"
        "• /protocollo lba / soccer\n"
        "• /reset — azzera",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────

def split_message(text: str, limit: int = 4000) -> list[str]:
    """Splitta testo lungo in chunks per Telegram."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    # Pre-carica protocolli in cache all'avvio — evita latenza alla prima richiesta
    logger.info("Pre-caricamento protocolli da Drive...")
    try:
        lba = get_lba_protocol()
        logger.info(f"LBA protocol caricato: {len(lba)} chars")
    except Exception as e:
        logger.error(f"Errore pre-caricamento LBA: {e}")
    try:
        soccer = get_soccer_protocol()
        logger.info(f"Soccer protocol caricato: {len(soccer)} chars")
    except Exception as e:
        logger.error(f"Errore pre-caricamento Soccer: {e}")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tennis", cmd_tennis))
    app.add_handler(CommandHandler("calcio", cmd_calcio))
    app.add_handler(CommandHandler("analisi", cmd_analisi))
    app.add_handler(CommandHandler("recap", cmd_recap))
    app.add_handler(CommandHandler("protocollo", cmd_protocollo))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("aggiorna", cmd_aggiorna_protocolli))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot v4.0 avviato")
    app.run_polling()


if __name__ == "__main__":
    main()

"""
Tennis Betting Analysis Bot — v3.0
Input: HTML TennisExplorer + Screenshot AsianOdds
Output: Verdetto immediato + analisi formattata Telegram
"""

import os
import re
import base64
import logging
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import anthropic
import math
import asyncio
import json
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

SHEET_ID = "1LFWu2qK42cVQDh9-keT23_M0tydr9CERPStKt_8OnFQ"
SHEET_HEADERS = [
    "data", "home", "away", "torneo", "verdetto", "n_segnali",
    "gap_home", "gap_away", "drift_pinn_home", "drift_pinn_away",
    "outlier_home", "outlier_away", "retail_drift_home", "retail_drift_away",
    "ols_forecast", "ols_delta_pct", "ols_class",
    "quota_max_home", "quota_max_away",
    "lato_giocato", "quota_giocata", "esito", "pl"
]

def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json:
        return None
    try:
        creds_dict = json.loads(creds_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet("DB")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet("DB", rows=1000, cols=len(SHEET_HEADERS))
            ws.append_row(SHEET_HEADERS)
        return ws
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return None

def save_match(state: dict, verdetto: str, n_segnali: int, lato: str, quota: float, esito: str, pl: float):
    ws = get_sheet()
    if not ws:
        return False
    d = state.get("html_data") or {}
    p = d.get("pinnacle", {})
    r = d.get("retail", {})
    ols = state.get("ols_data") or {}
    row = [
        datetime.now().strftime("%d/%m/%Y"),
        d.get("home_name", ""),
        d.get("away_name", ""),
        d.get("match_info", "")[:50],
        verdetto,
        n_segnali,
        d.get("gap_pinn_vs_retail", {}).get("home", ""),
        d.get("gap_pinn_vs_retail", {}).get("away", ""),
        p.get("drift_home", ""),
        p.get("drift_away", ""),
        str(p.get("outlier_home", "")),
        str(p.get("outlier_away", "")),
        r.get("drift_home", ""),
        r.get("drift_away", ""),
        ols.get("forecast", ""),
        ols.get("delta_pct", ""),
        ols.get("classification", ""),
        d.get("max_home", {}).get("q", ""),
        d.get("max_away", {}).get("q", ""),
        lato,
        quota,
        esito,
        pl,
    ]
    try:
        ws.append_row(row)
        return True
    except Exception as e:
        logger.error(f"Save error: {e}")
        return False

def get_pattern_context(gap_home: float, gap_away: float, outlier_home: bool, outlier_away: bool) -> str:
    """Legge lo storico e trova pattern simili."""
    ws = get_sheet()
    if not ws:
        return ""
    try:
        rows = ws.get_all_records()
        if len(rows) < 5:
            return ""
        # Pattern simile: stesso outlier e gap nella stessa fascia
        similar = []
        for row in rows:
            if not row.get("esito"):
                continue
            try:
                roh = str(row.get("outlier_home", "")).lower() == str(outlier_home).lower()
                roa = str(row.get("outlier_away", "")).lower() == str(outlier_away).lower()
                if roh and roa:
                    similar.append(row)
            except:
                continue
        if len(similar) < 3:
            return ""
        wins = sum(1 for r in similar if str(r.get("esito", "")).upper() == "W")
        pct = round(wins / len(similar) * 100)
        avg_pl = round(sum(float(r.get("pl", 0) or 0) for r in similar) / len(similar), 2)
        return (
            f"\n\n=== PATTERN STORICO ({len(similar)} casi simili) ===\n"
            f"Outlier home={outlier_home} away={outlier_away}\n"
            f"Win rate: {pct}% ({wins}/{len(similar)}) | P&L medio: {avg_pl:+.2f}u"
        )
    except Exception as e:
        logger.error(f"Pattern error: {e}")
        return ""


# ─────────────────────────────────────────────
# PARSER TENNISEXPLORER HTML
# ─────────────────────────────────────────────

RETAIL_EXCLUDE = {"Pinnacle", "Betfair", "SBOBET", "Matchbook"}

def parse_tennisexplorer(html: str) -> dict:
    """Estrae tutte le quote da TennisExplorer HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Nome giocatori
    h1 = soup.find("h1", class_="bg")
    players = h1.get_text(strip=True).split(" - ") if h1 else ["Home", "Away"]
    home_name = players[0].strip() if len(players) > 0 else "Home"
    away_name = players[1].strip() if len(players) > 1 else "Away"

    # Torneo / data
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
            """Read flat history cells: [date | quote | diff]* 'Opening odds' date quote"""
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

        # Find away block (next cell with odds-in div after home block)
        away_current = None
        away_cell_idx = None
        for ci in range(2, len(cells)):
            raw = cells[ci].decode_contents()
            if "odds-in" in raw:
                m = re.search(r"(\d\.\d+)", raw.split("<table")[0])
                if m:
                    away_current = float(m.group())
                    away_cell_idx = ci + 1  # history starts after this cell
                    break

        # Home history: starts at cell 2, but only up to away block
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

    # ── Calcoli ──
    retail = {k: v for k, v in books.items() if k not in RETAIL_EXCLUDE}
    pinnacle = books.get("Pinnacle", {})

    def mean(vals):
        vals = [v for v in vals if v]
        return round(sum(vals) / len(vals), 3) if vals else None

    retail_home_curr = mean([v["home_current"] for v in retail.values()])
    retail_away_curr = mean([v["away_current"] for v in retail.values()])
    retail_home_open = mean([v["home_open"] for v in retail.values() if v["home_open"]])
    retail_away_open = mean([v["away_open"] for v in retail.values() if v["away_open"]])

    # Max quote attuali su entrambi i lati (tra tutti i book)
    all_home = [v["home_current"] for v in books.values() if v["home_current"]]
    all_away = [v["away_current"] for v in books.values() if v["away_current"]]
    max_home = max(all_home) if all_home else None
    max_away = max(all_away) if all_away else None
    max_home_book = next((k for k, v in books.items() if v["home_current"] == max_home), "")
    max_away_book = next((k for k, v in books.items() if v["away_current"] == max_away), "")

    # Pinnacle vs retail (outlier check)
    pinn_home_curr = pinnacle.get("home_current")
    pinn_away_curr = pinnacle.get("away_current")
    pinn_home_open = pinnacle.get("home_open")
    pinn_away_open = pinnacle.get("away_open")

    gap_home = round(pinn_home_curr - retail_home_curr, 3) if pinn_home_curr and retail_home_curr else None
    gap_away = round(pinn_away_curr - retail_away_curr, 3) if pinn_away_curr and retail_away_curr else None

    # Pinnacle outlier: Pinnacle è MAX su quel lato tra TUTTI i book
    all_home_except_pinn = [v["home_current"] for k, v in books.items() if k != "Pinnacle" and v["home_current"]]
    all_away_except_pinn = [v["away_current"] for k, v in books.items() if k != "Pinnacle" and v["away_current"]]
    retail_home_max = max(all_home_except_pinn, default=None)
    retail_away_max = max(all_away_except_pinn, default=None)
    outlier_home = bool(pinn_home_curr and retail_home_max and pinn_home_curr >= retail_home_max)
    outlier_away = bool(pinn_away_curr and retail_away_max and pinn_away_curr >= retail_away_max)

    # Drift Pinnacle
    pinn_drift_home = round(pinn_home_curr - pinn_home_open, 3) if pinn_home_curr and pinn_home_open else None
    pinn_drift_away = round(pinn_away_curr - pinn_away_open, 3) if pinn_away_curr and pinn_away_open else None

    # Retail drift
    retail_drift_home = round(retail_home_curr - retail_home_open, 3) if retail_home_curr and retail_home_open else None
    retail_drift_away = round(retail_away_curr - retail_away_open, 3) if retail_away_curr and retail_away_open else None

    # ── Combo Pinnacle vs retail (apertura e attuale) ──
    def pinn_combo(pinn_open, retail_open, pinn_curr, retail_curr):
        if not all([pinn_open, retail_open, pinn_curr, retail_curr]):
            return "N/A"
        open_above = pinn_open >= retail_open
        curr_above = pinn_curr >= retail_curr
        if open_above and curr_above:
            return "GUIDA"        # Pinna sopra retail sia in apertura che ora
        elif open_above and not curr_above:
            return "ANTICIPA"     # Pinna era sopra, retail ha recuperato/superato
        elif not open_above and curr_above:
            return "ENTRA_TARDI"  # Pinna era sotto, ora è sopra
        else:
            return "INSEGUE"      # Pinna sotto retail in entrambi i momenti

    combo_home = pinn_combo(pinn_home_open, retail_home_open, pinn_home_curr, retail_home_curr)
    combo_away = pinn_combo(pinn_away_open, retail_away_open, pinn_away_curr, retail_away_curr)

    # ── Pattern movimento Pinnacle ──
    def detect_pattern(history):
        """Rileva pattern da lista di snapshot Pinnacle [{time, q}]"""
        quotes = [h["q"] for h in history if h["time"] != "open"]
        if not quotes or len(quotes) < 2:
            return "FLAT"
        total_move = quotes[-1] - quotes[0]
        if abs(total_move) < 0.03:
            return "FLAT"
        # Verifica unidirezionalità
        diffs = [quotes[i+1] - quotes[i] for i in range(len(quotes)-1)]
        signs = [1 if d > 0.005 else -1 if d < -0.005 else 0 for d in diffs]
        signs = [s for s in signs if s != 0]
        if not signs:
            return "FLAT"
        # Inversione tardiva: ultimi 2 movimenti invertono il trend precedente
        if len(signs) >= 3:
            early = signs[:-2]
            late = signs[-2:]
            early_dir = sum(early)
            late_dir = sum(late)
            if early_dir > 0 and late_dir < 0:
                return "INV"
            if early_dir < 0 and late_dir > 0:
                return "INV"
        # Rimbalzo: cambio di segno nel mezzo
        if len(set(signs)) > 1:
            # SPIKE: movimento brusco in ultimo tick
            if len(diffs) >= 2 and abs(diffs[-1]) >= 0.05:
                return "SPIKE"
            return "RIM"
        # Unidirezionale
        if all(s > 0 for s in signs):
            return "UNI+"
        if all(s < 0 for s in signs):
            return "UNI-"
        return "FLAT"

    pinn_home_hist = books.get("Pinnacle", {}).get("home_history", [])
    pinn_away_hist = books.get("Pinnacle", {}).get("away_history", [])
    pattern_home = detect_pattern(pinn_home_hist)
    pattern_away = detect_pattern(pinn_away_hist)

    return {
        "home_name": home_name,
        "away_name": away_name,
        "match_info": match_info,
        "books": books,
        "pinnacle": {
            "home_curr": pinn_home_curr,
            "away_curr": pinn_away_curr,
            "home_open": pinn_home_open,
            "away_open": pinn_away_open,
            "drift_home": pinn_drift_home,
            "drift_away": pinn_drift_away,
            "outlier_home": outlier_home,
            "outlier_away": outlier_away,
        },
        "retail": {
            "home_curr": retail_home_curr,
            "away_curr": retail_away_curr,
            "home_open": retail_home_open,
            "away_open": retail_away_open,
            "drift_home": retail_drift_home,
            "drift_away": retail_drift_away,
        },
        "max_home": {"q": max_home, "book": max_home_book},
        "max_away": {"q": max_away, "book": max_away_book},
        "gap_pinn_vs_retail": {"home": gap_home, "away": gap_away},
        "combo": {"home": combo_home, "away": combo_away},
        "pattern": {"home": pattern_home, "away": pattern_away},
    }


# ─────────────────────────────────────────────
# SYSTEM PROMPT ANALISI
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """Compila questo template con i dati ricevuti. Non aggiungere nulla che non sia nei dati. Non usare metafore o narrativa.

STEP 1 — Leggi questi campi esatti dai dati:
- Outlier Home / Outlier Away (True/False)
- Combo Home / Combo Away (GUIDA/ENTRA_TARDI/ANTICIPA/INSEGUE)
- Pattern Pinnacle Home / Pattern Pinnacle Away (UNI+/UNI-/RIM/SPIKE/INV/FLAT)
- drift Pinnacle Home / drift Pinnacle Away (numero)
- gap Home / gap Away (numero)
- OLS forecast e delta% (se presenti)

STEP 2 — Assegna segnale per ogni voce:
Outlier: True su X solo → PRO avversario · True entrambi o False → NEUTRO
Combo: GUIDA/ENTRA_TARDI → attivo · ANTICIPA/INSEGUE → debole
Pattern: UNI- su X → PRO X · UNI+ su X → PRO avversario · SPIKE → peso doppio · INV → invalida drift · RIM/FLAT → neutro
Drift: negativo su X → PRO X · positivo su X → PRO avversario · ignora se <0.05
OLS: forecast > Pinnacle → PRO soggetto · forecast < Pinnacle → PRO avversario

STEP 3 — Conta convergenza verso stesso giocatore → GIOCA/ATTENZIONE/NO BET

STEP 4 — Compila questo template ESATTO (sostituisci solo le parti in []):

🎾 [torneo o N/D] | [superficie o N/D]
📅 [data o N/D]
🏠 [Home] vs [Away]

Outlier: Home=[True/False] Away=[True/False] → [PRO X o NEUTRO]
Combo: Home=[valore] Away=[valore] → [attivo/debole su X]
Pattern: Home=[valore] Away=[valore] → [PRO X o neutro]
Drift Pinna: Home=[numero] Away=[numero] → [PRO X]
Gap Pinna/retail: Home=[numero] Away=[numero]
[Se OLS presente: OLS: forecast=[numero] vs Pinna=[numero] → PRO [X]]

⭐ Outlier: [stelle]
⭐ Combo: [stelle]
⭐ Pattern: [stelle]
⭐ Drift: [stelle]
⭐ OLS: [stelle o N/A]

🎯 [giocatore] | Quota MAX: [numero da max_home o max_away]
[✅ GIOCA / ⚠️ ATTENZIONE / ❌ NO BET] — [N] segnali convergenti

STELLE: ★★★★★=forte convergente · ★★★☆☆=medio · ★★☆☆☆=debole · ★☆☆☆☆=neutro/assente

Scrivi SOLO il template compilato. Niente altro."""


# ─────────────────────────────────────────────
# ANALISI CON CLAUDE
# ─────────────────────────────────────────────

def build_data_summary(data: dict) -> str:
    """Costruisce il testo dati da passare a Claude."""
    p = data["pinnacle"]
    r = data["retail"]
    g = data["gap_pinn_vs_retail"]

    lines = [
        f"MATCH: {data['home_name']} (Home) vs {data['away_name']} (Away)",
        f"INFO: {data.get('match_info', 'N/A')[:150]}",
        "",
        "=== QUOTA MAX ATTUALE (best odds) ===",
        f"Home MAX: {data['max_home']['q']} ({data['max_home']['book']})",
        f"Away MAX: {data['max_away']['q']} ({data['max_away']['book']})",
        "",
        "=== PINNACLE ===",
        f"Home: apertura={p['home_open']} → attuale={p['home_curr']} | drift={p['drift_home']}",
        f"Away: apertura={p['away_open']} → attuale={p['away_curr']} | drift={p['drift_away']}",
        f"Outlier Home (Pinnacle=MAX su tutti i book): {p['outlier_home']}",
        f"Outlier Away (Pinnacle=MAX su tutti i book): {p['outlier_away']}",
        f"Pattern Pinnacle Home: {data.get('pattern', {}).get('home', 'N/A')}",
        f"Pattern Pinnacle Away: {data.get('pattern', {}).get('away', 'N/A')}",
        f"Combo Home (Pinna vs retail): {data.get('combo', {}).get('home', 'N/A')} | apertura: Pinna={p['home_open']} vs retail={data['retail']['home_open']}",
        f"Combo Away (Pinna vs retail): {data.get('combo', {}).get('away', 'N/A')} | apertura: Pinna={p['away_open']} vs retail={data['retail']['away_open']}",
        "",
        "=== RETAIL MEDIA ===",
        f"Home: apertura={r['home_open']} → attuale={r['home_curr']} | drift={r['drift_home']}",
        f"Away: apertura={r['away_open']} → attuale={r['away_curr']} | drift={r['drift_away']}",
        "",
        "=== GAP PINNACLE vs RETAIL ===",
        f"Home: {g['home']} (positivo = Pinnacle più alto di retail su Home)",
        f"Away: {g['away']} (positivo = Pinnacle più alto di retail su Away)",
        "",
        "=== TUTTI I BOOKMAKER (current) ===",
    ]

    for book, v in data["books"].items():
        lines.append(f"{book}: Home={v['home_current']} Away={v['away_current']}")

    return "\n".join(lines)


async def analyze(data_summary: str, extra_context: str = "") -> str:
    """Chiama Claude per l'analisi."""
    user_msg = data_summary
    if extra_context:
        user_msg += f"\n\n=== DATI AGGIUNTIVI (screenshot/OCR) ===\n{extra_context}"

    try:
        logger.info("Chiamata API Anthropic in corso...")
        response = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}]
            ),
            timeout=30.0
        )
        logger.info("Risposta API ricevuta")
        return response.content[0].text
    except asyncio.TimeoutError:
        logger.error("Timeout API Anthropic dopo 30s")
        return "❌ Timeout analisi (30s). Riprova."
    except Exception as e:
        logger.error(f"Errore API Anthropic: {type(e).__name__}: {e}")
        return f"❌ Errore API: {type(e).__name__}: {str(e)[:200]}"


async def analyze_screenshot(image_b64: str, mime: str) -> str:
    """OCR + analisi da screenshot AsianOdds (senza HTML)."""
    def _call():
        return client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": image_b64}
                },
                {
                    "type": "text",
                    "text": (
                        "Sei un analista di scommesse tennis. Dalla screenshot di AsianOdds estrai:\n"
                        "1. Nomi giocatori\n"
                        "2. Tutte le quote per bookmaker (Home e Away)\n"
                        "3. Identifica Pinnacle e la sua posizione vs gli altri\n"
                        "4. Calcola media retail (escludi Pinnacle, Betfair, SBOBET)\n"
                        "5. Identifica quota MAX su entrambi i lati\n"
                        "6. Verifica se Pinnacle è outlier (MAX su un lato)\n\n"
                        "Poi produci l'analisi completa nel formato richiesto.\n"
                        "Se vedi anche il grafico Pinnacle con la timeline, analizza anche il drift."
                    )
                }
            ]
        }]
        )
    response = await asyncio.to_thread(_call)
    return response.content[0].text


# ─────────────────────────────────────────────
# USER STATE (per combinare HTML + screenshot)
# ─────────────────────────────────────────────

user_state: dict[int, dict] = {}

def get_state(uid: int) -> dict:
    if uid not in user_state:
        user_state[uid] = {"html_data": None, "pending_photo": None, "ols_data": None}
    return user_state[uid]


# ─────────────────────────────────────────────
# HANDLERS TELEGRAM
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎾 *Tennis Bet Analyzer v3.0*\n\n"
        "Manda:\n"
        "• HTML TennisExplorer (come documento .html)\n"
        "• Screenshot AsianOdds (come foto)\n"
        "• Entrambi per analisi completa\n\n"
        "Digita /reset per azzerare lo stato.",
        parse_mode="Markdown"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"html_data": None, "pending_photo": None, "ols_data": None}
    await update.message.reply_text("✅ Stato azzerato.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    doc = update.message.document

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

    summary = (
        f"✅ HTML parsato: *{data['home_name']} vs {data['away_name']}*\n"
        f"📊 Pinnacle Home: {data['pinnacle']['home_open']} → {data['pinnacle']['home_curr']} "
        f"(drift {data['pinnacle']['drift_home']})\n"
        f"📊 Pinnacle Away: {data['pinnacle']['away_open']} → {data['pinnacle']['away_curr']} "
        f"(drift {data['pinnacle']['drift_away']})\n"
        f"🔝 MAX Home: {data['max_home']['q']} ({data['max_home']['book']})\n"
        f"🔝 MAX Away: {data['max_away']['q']} ({data['max_away']['book']})\n\n"
        "Manda screenshot AsianOdds per completare, oppure scrivi *go* per analizzare subito."
    )
    await update.message.reply_text(summary, parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)

    await update.message.reply_text("⏳ Analisi in corso...")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(img_bytes).decode()

    if state["html_data"]:
        # Abbiamo già l'HTML — usiamo OCR solo come contesto aggiuntivo
        ocr_response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text", "text": "Estrai dal grafico Pinnacle (timeline): snapshot con orario e quota per Home e Away. Formato: HH:MM Home=X.XX Away=X.XX per ogni riga visibile. Solo i dati, niente altro."}
                ]
            }]
        )
        extra = ocr_response.content[0].text
        data_summary = build_data_summary(state["html_data"])
        result = await analyze(data_summary, extra_context=extra)
    else:
        # Solo screenshot
        result = await analyze_screenshot(img_b64, "image/jpeg")

    state["html_data"] = None  # reset dopo analisi
    await update.message.reply_text(result, parse_mode="Markdown")



# ─────────────────────────────────────────────
# OLS ENGINE
# ─────────────────────────────────────────────

def no_vig(q1: float, q2: float) -> tuple[float, float]:
    """Rimuove il vig, ritorna quote fair decimali (non probabilità)."""
    p1, p2 = 1/q1, 1/q2
    tot = p1 + p2
    return round(1/(p1/tot), 4), round(1/(p2/tot), 4)

def ols_simple(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """OLS lineare: y = a + b*x. Ritorna (a, b, r2)."""
    n = len(xs)
    if n < 2:
        return 0, 0, 0
    mx = sum(xs) / n
    my = sum(ys) / n
    ssxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ssxx = sum((x - mx) ** 2 for x in xs)
    if ssxx == 0:
        return my, 0, 0
    b = ssxy / ssxx
    a = my - b * mx
    y_pred = [a + b * x for x in xs]
    ss_res = sum((y - yp) ** 2 for y, yp in zip(ys, y_pred))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return round(a, 6), round(b, 6), round(r2, 4)

def classify_delta(delta_pct: float) -> str:
    a = abs(delta_pct)
    if a < 5:
        return "Sub-threshold"
    elif a < 12:
        return "Debole"
    elif a < 22:
        return "Moderato"
    else:
        return "Forte"

def parse_ols_input(text: str) -> dict:
    """
    Formato input:
      ols par 106 ava 276   (o varianti: ols\n106 276)
      150 250 204
      172 200 176
      ...
    Colonne righe storiche: q_sogg q_avv rank_avv (valori ×100 o decimali)
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    
    # Prima riga: estrai rank soggetto e rank avversario oggi
    first = lines[0].lower()
    nums_first = re.findall(r"\d+\.?\d*", first)
    if len(nums_first) < 2:
        return {"error": "Prima riga deve contenere rank soggetto e rank avversario (es: ols par 106 ava 276)"}
    
    rank_subj = float(nums_first[-2])
    rank_opp = float(nums_first[-1])
    
    # Righe storiche
    rows = []
    for line in lines[1:]:
        nums = re.findall(r"\d+\.?\d*", line)
        if len(nums) < 3:
            continue
        q_s, q_o, rank_a = float(nums[0]), float(nums[1]), float(nums[2])
        # Se valori > 10 sono ×100
        if q_s > 10:
            q_s /= 100
        if q_o > 10:
            q_o /= 100
        rows.append((q_s, q_o, rank_a))
    
    if len(rows) < 3:
        return {"error": f"Servono almeno 3 righe storiche, trovate {len(rows)}"}
    
    # No-vig e costruzione dataset OLS
    xs, ys = [], []
    for q_s, q_o, rank_a in rows:
        fair_s, _ = no_vig(q_s, q_o)
        # Cap rank a 1500
        rank_cap = min(rank_a, 1500)
        xs.append(math.log(rank_cap))
        ys.append(math.log(fair_s))
    
    a, b, r2 = ols_simple(xs, ys)
    
    # Forecast oggi
    rank_opp_cap = min(rank_opp, 1500)
    log_forecast = a + b * math.log(rank_opp_cap)
    forecast = round(math.exp(log_forecast), 3)
    
    return {
        "subject": "soggetto",
        "rank_subj": rank_subj,
        "rank_opp": rank_opp,
        "rows": rows,
        "a": a, "b": b, "r2": r2,
        "forecast": forecast,
        "delta_pct": None,  # calcolato dopo con Pinnacle attuale
        "classification": None,
    }

def finalize_ols(ols_data: dict, pinnacle_q: float) -> dict:
    """Calcola Δ% tra forecast OLS e Pinnacle attuale."""
    if not pinnacle_q:
        return ols_data
    delta_pct = round((ols_data["forecast"] - pinnacle_q) / pinnacle_q * 100, 2)
    ols_data["delta_pct"] = delta_pct
    ols_data["pinnacle_q"] = pinnacle_q
    ols_data["classification"] = classify_delta(delta_pct)
    return ols_data

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    text = update.message.text.strip()

    # GO: analisi con tutto il disponibile
    if text.lower() == "go":
        if not state["html_data"] and not state["ols_data"]:
            await update.message.reply_text("❌ Manda prima HTML o OLS.")
            return
        await update.message.reply_text("⏳ Analisi in corso...")
        
        ols_data = state.get("ols_data")
        data_summary = ""
        
        if state["html_data"]:
            # Finalize OLS with actual Pinnacle quote if available
            if ols_data:
                pinn_away = state["html_data"]["pinnacle"].get("away_curr")
                pinn_home = state["html_data"]["pinnacle"].get("home_curr")
                # Use away as default (soggetto spesso è l'away nel dataset)
                pinn_q = pinn_away or pinn_home
                ols_data = finalize_ols(ols_data, pinn_q)
            data_summary = build_data_summary(state["html_data"])
        
        ols_summary = ""
        if ols_data and ols_data.get("delta_pct") is not None:
            ols_summary = (
                f"\n\n=== OLS ===\n"
                f"Soggetto rank oggi: {ols_data['rank_subj']} | Avv rank: {ols_data['rank_opp']}\n"
                f"Forecast fair: {ols_data['forecast']} | Pinnacle attuale: {ols_data.get('pinnacle_q', 'N/A')}\n"
                f"Δ%: {ols_data['delta_pct']:+.1f}% | R²: {ols_data['r2']} | Classificazione: {ols_data['classification']}\n"
                f"Interpretazione: forecast {'> mercato → modello vede soggetto più forte' if ols_data['delta_pct'] > 0 else '< mercato → modello vede soggetto più debole'}"
            )
        
        result = await analyze(data_summary + ols_summary)
        # Salva verdetto in stato per /risultato
        if "GIOCA" in result.upper():
            state["last_verdetto"] = "GIOCA"
        elif "ATTENZIONE" in result.upper():
            state["last_verdetto"] = "ATTENZIONE"
        else:
            state["last_verdetto"] = "NO BET"
        state["last_segnali"] = result.count("✓")
        # Salva quota favorito per /risultato
        if state["html_data"]:
            p = state["html_data"].get("pinnacle", {})
            hq = p.get("home_curr")
            aq = p.get("away_curr")
            if hq and aq:
                if hq <= aq:
                    state["last_fav"] = "home"
                    state["last_quota_fav"] = hq
                else:
                    state["last_fav"] = "away"
                    state["last_quota_fav"] = aq
        state["html_data"] = None
        state["ols_data"] = None
        try:
            await update.message.reply_text(result, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(result)
        return

    # OLS input: prima riga inizia con "ols"
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("ols"):
        ols_result = parse_ols_input(text)
        if "error" in ols_result:
            await update.message.reply_text(f"❌ OLS: {ols_result['error']}")
            return
        # Se abbiamo già l'HTML, finalizza subito il Δ%
        if state["html_data"]:
            pinn_q = state["html_data"]["pinnacle"].get("away_curr") or state["html_data"]["pinnacle"].get("home_curr")
            ols_result = finalize_ols(ols_result, pinn_q)
        state["ols_data"] = ols_result
        delta_str = f"Δ%={ols_result['delta_pct']:+.1f}% ({ols_result['classification']})" if ols_result.get("delta_pct") is not None else "Δ% calcolato al go"
        msg = (
            f"✅ OLS caricato — {len(ols_result['rows'])} partite storiche\n"
            f"Rank oggi: par={ols_result['rank_subj']} ava={ols_result['rank_opp']}\n"
            f"📈 Forecast: *{ols_result['forecast']:.3f}* | R²={ols_result['r2']:.3f}\n"
            f"{delta_str}\n\n"
            "Scrivi *go* per analisi completa."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    await update.message.reply_text(
        "Manda:\n• HTML TennisExplorer (.html)\n• Screenshot AsianOdds (foto)\n• Blocco OLS (inizia con `ols`)\n• *go* per analizzare",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
# RISULTATO HANDLER
# ─────────────────────────────────────────────

async def risultato(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Uso: /risultato W  oppure  /risultato L
    W = ha vinto il favorito, L = ha vinto l'underdog
    Quota presa da Pinnacle in memoria, stake fisso 1u
    """
    uid = update.effective_user.id
    state = get_state(uid)
    args = context.args

    if not args:
        await update.message.reply_text("Uso: `/risultato W` oppure `/risultato L`", parse_mode="Markdown")
        return

    esito = args[0].upper()
    if esito not in ("W", "L"):
        await update.message.reply_text("❌ Esito deve essere W o L")
        return

    # Quota Pinnacle del favorito (quota più bassa = favorito)
    d = state.get("html_data") or {}
    p = d.get("pinnacle", {})
    home_q = p.get("home_curr")
    away_q = p.get("away_curr")

    if home_q and away_q:
        if home_q <= away_q:
            fav = "home"
            quota_fav = home_q
        else:
            fav = "away"
            quota_fav = away_q
    elif state.get("last_quota_fav"):
        fav = state.get("last_fav", "N/A")
        quota_fav = state["last_quota_fav"]
    else:
        await update.message.reply_text("❌ Nessuna quota Pinnacle in memoria. Analizza prima il match.")
        return

    pl = round((quota_fav - 1) if esito == "W" else -1, 3)
    verdetto = state.get("last_verdetto", "N/A")
    n_segnali = state.get("last_segnali", 0)

    ok = save_match(state, verdetto, n_segnali, fav, quota_fav, esito, pl)

    if ok:
        emoji = "✅" if esito == "W" else "❌"
        await update.message.reply_text(
            f"{emoji} Salvato\n"
            f"Favorito: {fav} | Quota Pinnacle: {quota_fav} | Esito: {esito} | P&L: {pl:+.2f}u",
            parse_mode="Markdown"
        )
        state["html_data"] = None
        state["ols_data"] = None
        state["last_verdetto"] = None
        state["last_segnali"] = 0
        state["last_quota_fav"] = None
        state["last_fav"] = None
    else:
        await update.message.reply_text("❌ Errore salvataggio su Google Sheets")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra statistiche generali dal DB."""
    ws = get_sheet()
    if not ws:
        await update.message.reply_text("❌ Connessione Sheets non disponibile")
        return
    try:
        rows = [r for r in ws.get_all_records() if r.get("esito")]
        if not rows:
            await update.message.reply_text("Nessun risultato nel DB ancora.")
            return
        total = len(rows)
        wins = sum(1 for r in rows if str(r.get("esito", "")).upper() == "W")
        total_pl = round(sum(float(r.get("pl", 0) or 0) for r in rows), 3)
        roi = round(total_pl / total * 100, 1) if total else 0
        # Breakdown per outlier
        oul_rows = [r for r in rows if str(r.get("outlier_home")) == "True" or str(r.get("outlier_away")) == "True"]
        oul_wins = sum(1 for r in oul_rows if str(r.get("esito", "")).upper() == "W")
        msg = (
            f"📊 *Statistiche LBA DB*\n\n"
            f"Totale bet: {total}\n"
            f"Win rate: {round(wins/total*100)}% ({wins}/{total})\n"
            f"P&L totale: {total_pl:+.3f}u\n"
            f"ROI: {roi:+.1f}%\n\n"
            f"🔍 *Con outlier Pinnacle* ({len(oul_rows)} bet):\n"
            f"Win rate: {round(oul_wins/len(oul_rows)*100) if oul_rows else 0}% ({oul_wins}/{len(oul_rows)})"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("risultato", risultato))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot avviato")
    app.run_polling()


if __name__ == "__main__":
    main()

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

    # Estrai info match: data, ora, torneo, round, superficie
    match_date = ""
    match_time = ""
    match_tournament = ""
    match_round = ""
    match_surface = ""
    for tag in soup.find_all(["div", "span", "p", "td"]):
        txt = tag.get_text(strip=True)
        m = re.match(r"(Today|\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2}),\s*(.+)", txt, re.IGNORECASE)
        if m:
            match_date = m.group(1)
            match_time = m.group(2)
            rest = m.group(3)
            parts_info = [p.strip() for p in rest.split(",")]
            if parts_info:
                match_tournament = parts_info[0]
            if len(parts_info) > 1:
                match_round = parts_info[1]
            if len(parts_info) > 2:
                match_surface = parts_info[2]
            break
    if not match_surface:
        for tag in soup.find_all(["div", "span", "td"]):
            txt = tag.get_text(strip=True).lower()
            if txt in ("clay", "hard", "grass", "carpet"):
                match_surface = txt.capitalize()
                break

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

    # FAV = quota Pinnacle più bassa
    if pinn_home_curr and pinn_away_curr:
        if pinn_home_curr <= pinn_away_curr:
            fav_name, fav_side, fav_q = home_name, "Home", pinn_home_curr
            und_name, und_side, und_q = away_name, "Away", pinn_away_curr
        else:
            fav_name, fav_side, fav_q = away_name, "Away", pinn_away_curr
            und_name, und_side, und_q = home_name, "Home", pinn_home_curr
    else:
        fav_name, fav_side, fav_q = home_name, "Home", pinn_home_curr
        und_name, und_side, und_q = away_name, "Away", pinn_away_curr

    return {
        "home_name": home_name,
        "away_name": away_name,
        "match_info": match_info,
        "match_date": match_date,
        "match_time": match_time,
        "match_tournament": match_tournament,
        "match_round": match_round,
        "match_surface": match_surface,
        "fav_name": fav_name, "fav_side": fav_side, "fav_q": fav_q,
        "und_name": und_name, "und_side": und_side, "und_q": und_q,
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


def format_quote_snapshot(data: dict, tol: float = 0.05) -> str:
    """Riepilogo numerico quote: Pinnacle vs retail, apertura vs ora."""
    p = data["pinnacle"]
    r = data["retail"]

    def pos(gap):
        if gap is None: return "N/D"
        if gap > tol: return f"SOPRA ({gap:+.3f})"
        elif gap < -tol: return f"SOTTO ({gap:+.3f})"
        else: return f"IN LINEA ({gap:+.3f})"

    def delta_str(v):
        if v is None: return "N/D"
        return f"{v:+.3f}"

    gap_ape_home = round(p['home_open'] - r['home_open'], 3) if p['home_open'] and r['home_open'] else None
    gap_ape_away = round(p['away_open'] - r['away_open'], 3) if p['away_open'] and r['away_open'] else None
    gap_ora_home = round(p['home_curr'] - r['home_curr'], 3) if p['home_curr'] and r['home_curr'] else None
    gap_ora_away = round(p['away_curr'] - r['away_curr'], 3) if p['away_curr'] and r['away_curr'] else None

    lines = [
        "📊 QUOTE SNAPSHOT",
        f"{'':18} APE     ORA     Δ",
        f"Pinna  {data['home_name'][:8]:10} {str(p['home_open']):7} {str(p['home_curr']):7} {delta_str(p['drift_home'])}",
        f"Retail {data['home_name'][:8]:10} {str(r['home_open']):7} {str(r['home_curr']):7} {delta_str(r['drift_home'])}",
        f"Gap    Home:       {str(gap_ape_home):7} {str(gap_ora_home):7}",
        "",
        f"Pinna  {data['away_name'][:8]:10} {str(p['away_open']):7} {str(p['away_curr']):7} {delta_str(p['drift_away'])}",
        f"Retail {data['away_name'][:8]:10} {str(r['away_open']):7} {str(r['away_curr']):7} {delta_str(r['drift_away'])}",
        f"Gap    Away:       {str(gap_ape_away):7} {str(gap_ora_away):7}",
        "",
        f"Pinna vs retail APERTURA: Home {pos(gap_ape_home)} | Away {pos(gap_ape_away)}",
        f"Pinna vs retail ORA:      Home {pos(gap_ora_home)} | Away {pos(gap_ora_away)}",
    ]
    return "\n".join(lines)


def build_tennis_summary(data: dict) -> str:
    p = data["pinnacle"]
    r = data["retail"]
    g = data["gap_pinn_vs_retail"]
    torneo = " | ".join(filter(None, [
        data.get("match_tournament", ""),
        data.get("match_round", ""),
        data.get("match_surface", ""),
    ]))
    data_ora = " ".join(filter(None, [data.get("match_date",""), data.get("match_time","")]))
    lines = [
        f"MATCH: {data['home_name']} (Home) vs {data['away_name']} (Away)",
        f"TORNEO: {torneo or 'N/D'}",
        f"DATA/ORA: {data_ora or 'N/D'}",
        f"FAV (quota Pinnacle più bassa): {data['fav_name']} ({data['fav_side']}) @ {data['fav_q']}",
        f"UND: {data['und_name']} ({data['und_side']}) @ {data['und_q']}",
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
# RATING DETERMINISTICO TENNIS
# ─────────────────────────────────────────────

def compute_tennis_ratings(data: dict) -> dict:
    """
    Calcola rating deterministici X/5 dai dati parsati.
    Restituisce dict con rating, interpretazioni e segnale finale.
    """
    p = data["pinnacle"]
    g = data["gap_pinn_vs_retail"]
    pattern = data.get("pattern", {})
    combo = data.get("combo", {})

    results = {}

    # ── 1. OUTLIER + FLUSSO SHARP ──
    outlier_home = p.get("outlier_home", False)
    outlier_away = p.get("outlier_away", False)

    if outlier_home and not outlier_away:
        outlier_rating = 5
        outlier_signal = f"PRO {data['away_name']} (Away)"
        outlier_note = f"Pinnacle MAX su Home → sharp su Away"
    elif outlier_away and not outlier_home:
        outlier_rating = 5
        outlier_signal = f"PRO {data['home_name']} (Home)"
        outlier_note = f"Pinnacle MAX su Away → sharp su Home"
    elif outlier_home and outlier_away:
        outlier_rating = 2
        outlier_signal = "NEUTRO (entrambi outlier)"
        outlier_note = "Mercato conteso"
    else:
        outlier_rating = 1
        outlier_signal = "NEUTRO (nessun outlier)"
        outlier_note = "Pinnacle non è MAX su nessun lato"

    results["outlier"] = {
        "rating": outlier_rating,
        "signal": outlier_signal,
        "note": outlier_note,
        "home": outlier_home,
        "away": outlier_away,
    }

    # ── 2. DRIFT PINNACLE ──
    drift_home = p.get("drift_home") or 0
    drift_away = p.get("drift_away") or 0

    # Determina direzione e intensità
    def drift_rating_single(drift):
        abs_d = abs(drift)
        if abs_d >= 0.10: return 5
        elif abs_d >= 0.07: return 4
        elif abs_d >= 0.05: return 3
        elif abs_d >= 0.03: return 2
        elif abs_d >= 0.01: return 1
        else: return 0

    # Segnale drift: sale su X = PRO Y, scende su X = PRO X
    drift_signals = []
    if abs(drift_home) >= 0.01:
        if drift_home > 0:
            drift_signals.append(f"Home sale {drift_home:+.2f} → PRO {data['away_name']}")
        else:
            drift_signals.append(f"Home scende {drift_home:+.2f} → PRO {data['home_name']}")
    if abs(drift_away) >= 0.01:
        if drift_away > 0:
            drift_signals.append(f"Away sale {drift_away:+.2f} → PRO {data['home_name']}")
        else:
            drift_signals.append(f"Away scende {drift_away:+.2f} → PRO {data['away_name']}")

    # Rating drift = max dei due lati pesato sulla convergenza
    rh = drift_rating_single(drift_home)
    ra = drift_rating_single(drift_away)

    # Se entrambi convergono stesso lato = bonus
    home_pro_away = drift_home > 0.01
    away_pro_home = drift_away > 0.01
    home_pro_home = drift_home < -0.01
    away_pro_away = drift_away < -0.01

    if (home_pro_away and away_pro_away) or (home_pro_home and away_pro_home):
        # Doppia convergenza
        drift_rating = min(5, max(rh, ra) + 1)
        drift_convergence = "doppia convergenza"
    elif drift_signals:
        drift_rating = max(rh, ra)
        drift_convergence = "singola"
    else:
        drift_rating = 0
        drift_convergence = "nessun drift significativo"

    results["drift"] = {
        "rating": drift_rating,
        "home": drift_home,
        "away": drift_away,
        "signals": drift_signals,
        "convergence": drift_convergence,
    }

    # ── 3. PATTERN ──
    pat_home = pattern.get("home", "FLAT")
    pat_away = pattern.get("away", "FLAT")

    PATTERN_WEIGHT = {
        "UNI+": 4, "UNI-": 4,
        "SPIKE": 5,
        "INV": 2,
        "RIM": 2,
        "FLAT": 0,
    }

    pw_home = PATTERN_WEIGHT.get(pat_home, 0)
    pw_away = PATTERN_WEIGHT.get(pat_away, 0)
    pattern_rating = min(5, max(pw_home, pw_away))

    # Interpretazione pattern
    pat_notes = []
    if pat_home == "UNI-":
        pat_notes.append(f"Home UNI- (sale) → PRO {data['away_name']}")
    elif pat_home == "UNI+":
        pat_notes.append(f"Home UNI+ (scende) → PRO {data['home_name']}")
    elif pat_home == "SPIKE":
        pat_notes.append(f"Home SPIKE → peso massimo")
    if pat_away == "UNI-":
        pat_notes.append(f"Away UNI- (sale) → PRO {data['home_name']}")
    elif pat_away == "UNI+":
        pat_notes.append(f"Away UNI+ (scende) → PRO {data['away_name']}")
    elif pat_away == "SPIKE":
        pat_notes.append(f"Away SPIKE → peso massimo")

    results["pattern"] = {
        "rating": pattern_rating,
        "home": pat_home,
        "away": pat_away,
        "notes": pat_notes,
    }

    # ── 4. GAP PINNACLE vs RETAIL ──
    gap_home = g.get("home") or 0
    gap_away = g.get("away") or 0
    max_gap = max(abs(gap_home), abs(gap_away))
    max_gap_ticks = round(max_gap * 100)

    if max_gap_ticks >= 15:
        gap_rating = 5
        gap_label = "FORTE MASSIMO"
    elif max_gap_ticks >= 10:
        gap_rating = 4
        gap_label = "FORTE"
    elif max_gap_ticks >= 7:
        gap_rating = 3
        gap_label = "ZONA GRIGIA ALTA"
    elif max_gap_ticks >= 5:
        gap_rating = 2
        gap_label = "ZONA GRIGIA"
    else:
        gap_rating = 1
        gap_label = "SUB-SOGLIA"

    results["gap"] = {
        "rating": gap_rating,
        "home": gap_home,
        "away": gap_away,
        "ticks": max_gap_ticks,
        "label": gap_label,
    }

    # ── 5. COMBO (posizione Pinnacle vs retail) ──
    combo_home = combo.get("home", "N/A")
    combo_away = combo.get("away", "N/A")

    COMBO_WEIGHT = {
        "GUIDA": 4,
        "ENTRA_TARDI": 4,
        "ANTICIPA": 2,
        "INSEGUE": 1,
        "N/A": 0,
    }
    combo_rating = min(5, max(
        COMBO_WEIGHT.get(combo_home, 0),
        COMBO_WEIGHT.get(combo_away, 0)
    ))

    results["combo"] = {
        "rating": combo_rating,
        "home": combo_home,
        "away": combo_away,
    }

    # ── TOTALE E SEGNALE FINALE ──
    total = (
        results["outlier"]["rating"] +
        results["drift"]["rating"] +
        results["pattern"]["rating"] +
        results["gap"]["rating"]
    )
    max_total = 20

    if total >= 17:
        verdict_strength = "⚡ MOLTO FORTE"
    elif total >= 13:
        verdict_strength = "✅ FORTE"
    elif total >= 8:
        verdict_strength = "⚠️ MEDIO"
    else:
        verdict_strength = "❌ DEBOLE"

    # Determina giocatore segnalato (maggioranza segnali)
    # Conta segnali PRO home vs PRO away
    pro_home_count = 0
    pro_away_count = 0

    # Outlier
    if "PRO" in results["outlier"]["signal"]:
        if "Home" in results["outlier"]["signal"]:
            pro_home_count += results["outlier"]["rating"]
        else:
            pro_away_count += results["outlier"]["rating"]

    # Drift
    for sig in results["drift"]["signals"]:
        if f"PRO {data['home_name']}" in sig:
            pro_home_count += 1
        elif f"PRO {data['away_name']}" in sig:
            pro_away_count += 1

    # Pattern
    for note in results["pattern"]["notes"]:
        if f"PRO {data['home_name']}" in note:
            pro_home_count += 1
        elif f"PRO {data['away_name']}" in note:
            pro_away_count += 1

    if pro_home_count > pro_away_count:
        signal_player = data["home_name"]
        signal_side = "Home"
    elif pro_away_count > pro_home_count:
        signal_player = data["away_name"]
        signal_side = "Away"
    else:
        signal_player = "N/D (segnali contraddittori)"
        signal_side = "N/D"

    # Verdetto operativo
    if total >= 13 and (pro_home_count != pro_away_count):
        verdict = "✅ GIOCA"
    elif total >= 8:
        verdict = "⚠️ ATTENZIONE"
    else:
        verdict = "❌ NO BET"

    results["total"] = total
    results["max_total"] = max_total
    results["strength"] = verdict_strength
    results["signal_player"] = signal_player
    results["signal_side"] = signal_side
    results["verdict"] = verdict
    results["pro_home"] = pro_home_count
    results["pro_away"] = pro_away_count

    return results


def format_tennis_ratings(data: dict, ratings: dict) -> str:
    """Formatta i rating calcolati in testo per il prompt Claude."""
    lines = [
        "=== RATING DETERMINISTICI (calcolati da Python — NON modificare) ===",
        "",
        f"OUTLIER: Home={ratings['outlier']['home']} Away={ratings['outlier']['away']}",
        f"  → {ratings['outlier']['signal']}",
        f"  → {ratings['outlier']['note']}",
        f"  Rating: {ratings['outlier']['rating']}/5",
        "",
        f"DRIFT: Home={ratings['drift']['home']:+.2f} Away={ratings['drift']['away']:+.2f} [{ratings['drift']['convergence']}]",
    ]
    for s in ratings["drift"]["signals"]:
        lines.append(f"  → {s}")
    lines.append(f"  Rating: {ratings['drift']['rating']}/5")
    lines.append("")
    lines.append(f"PATTERN: Home={ratings['pattern']['home']} Away={ratings['pattern']['away']}")
    for n in ratings["pattern"]["notes"]:
        lines.append(f"  → {n}")
    lines.append(f"  Rating: {ratings['pattern']['rating']}/5")
    lines.append("")
    lines.append(f"GAP: Home={ratings['gap']['home']} Away={ratings['gap']['away']} ({ratings['gap']['ticks']} tick) → {ratings['gap']['label']}")
    lines.append(f"  Rating: {ratings['gap']['rating']}/5")
    lines.append("")
    lines.append(f"COMBO: Home={ratings['combo']['home']} Away={ratings['combo']['away']}")
    lines.append(f"  Rating: {ratings['combo']['rating']}/5")
    lines.append("")
    lines.append("─────────────────")
    lines.append(f"TOTALE: {ratings['total']}/{ratings['max_total']} → {ratings['strength']}")
    lines.append(f"SEGNALE: PRO {ratings['signal_player']} ({ratings['signal_side']})")
    lines.append(f"VERDETTO PYTHON: {ratings['verdict']}")
    lines.append("")
    lines.append("ISTRUZIONE: usa questi rating e questo segnale ESATTAMENTE.")
    lines.append(f"Il verdetto finale DEVE essere su {ratings['signal_player']}.")
    lines.append("Non puoi cambiare il segnale — puoi solo aggiungere motivazione narrativa.")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# OLS ENGINE
# ─────────────────────────────────────────────

def no_vig(q1: float, q2: float) -> tuple:
    p1, p2 = 1/q1, 1/q2
    tot = p1 + p2
    return round(1/(p1/tot), 4), round(1/(p2/tot), 4)

def ols_simple(xs: list, ys: list) -> tuple:
    n = len(xs)
    if n < 2: return 0, 0, 0
    mx = sum(xs)/n
    my = sum(ys)/n
    ssxy = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    ssxx = sum((x-mx)**2 for x in xs)
    if ssxx == 0: return my, 0, 0
    b = ssxy/ssxx
    a = my - b*mx
    y_pred = [a+b*x for x in xs]
    ss_res = sum((y-yp)**2 for y,yp in zip(ys,y_pred))
    ss_tot = sum((y-my)**2 for y in ys)
    r2 = 1 - ss_res/ss_tot if ss_tot > 0 else 0
    return round(a,6), round(b,6), round(r2,4)

def parse_ols_input(text: str) -> dict:
    """
    Formato:
      ols 106 276
      153 237 550
      161 220 400
      ...
    Prima riga: ols rank_sogg_UTR rank_avv_UTR
    Righe storiche: quota_sogg*100 quota_avv*100 rank_avv_storico
    Quote divise per 100. Righe: 5-8.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return {"error": "Input vuoto"}

    # Prima riga — estrai rank sogg e avv oggi
    first = lines[0]
    nums_first = re.findall(r"\d+\.?\d*", first)
    if len(nums_first) < 2:
        return {"error": "Prima riga: ols [rank_sogg] [rank_avv] — es: ols 106 276"}

    rank_subj = float(nums_first[-2])
    rank_opp = float(nums_first[-1])

    # Righe storiche — supporta sia righe separate che trattini
    raw_lines = []
    for line in lines[1:]:
        # Splitta per trattino se presente
        parts = line.split("-")
        for part in parts:
            part = part.strip()
            if part:
                raw_lines.append(part)

    rows = []
    for line in raw_lines:
        nums = re.findall(r"\d+\.?\d*", line)
        if len(nums) < 3:
            continue
        q_s, q_o, rank_a = float(nums[0]), float(nums[1]), float(nums[2])
        if q_s > 10: q_s /= 100
        if q_o > 10: q_o /= 100
        rows.append((q_s, q_o, rank_a))

    if len(rows) < 5:
        return {"error": f"Servono 5-8 righe storiche, trovate {len(rows)}"}
    if len(rows) > 8:
        rows = rows[:8]

    # OLS
    xs, ys = [], []
    for q_s, q_o, rank_a in rows:
        fair_s, _ = no_vig(q_s, q_o)
        rank_cap = min(rank_a, 1500)
        xs.append(math.log(rank_cap))
        ys.append(math.log(fair_s))

    a, b, r2 = ols_simple(xs, ys)

    rank_opp_cap = min(rank_opp, 1500)
    log_forecast = a + b * math.log(rank_opp_cap)
    forecast = round(math.exp(log_forecast), 3)

    return {
        "rank_subj": rank_subj,
        "rank_opp": rank_opp,
        "rows": rows,
        "a": a, "b": b, "r2": r2,
        "forecast": forecast,
        "delta_pct": None,
        "classification": None,
    }

def finalize_ols(ols_data: dict, pinnacle_q: float) -> dict:
    if not pinnacle_q:
        return ols_data
    delta_pct = round((ols_data["forecast"] - pinnacle_q) / pinnacle_q * 100, 2)
    abs_d = abs(delta_pct)
    if abs_d < 5: classification = "Sub-threshold"
    elif abs_d < 12: classification = "Debole"
    elif abs_d < 22: classification = "Moderato"
    else: classification = "Forte"

    # Segnale OLS
    if delta_pct > 0:
        ols_signal = "PRO soggetto (forecast > mercato)"
        ols_rating = 5 if abs_d >= 22 else 4 if abs_d >= 12 else 3 if abs_d >= 5 else 1
    else:
        ols_signal = "PRO avversario (forecast < mercato)"
        ols_rating = 5 if abs_d >= 22 else 4 if abs_d >= 12 else 3 if abs_d >= 5 else 1

    ols_data["delta_pct"] = delta_pct
    ols_data["pinnacle_q"] = pinnacle_q
    ols_data["classification"] = classification
    ols_data["signal"] = ols_signal
    ols_data["rating"] = ols_rating if ols_data["r2"] >= 0.60 else max(1, ols_rating - 2)
    ols_data["active"] = ols_data["r2"] >= 0.60 and abs_d >= 5
    return ols_data


# ─────────────────────────────────────────────
# CLAUDE API CALLS
# ─────────────────────────────────────────────

async def claude_call(system: str, user_content, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 2000, timeout: float = 120.0) -> str:
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
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
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

RATING — REGOLA ASSOLUTA:
Se nei dati ricevi il blocco "=== RATING DETERMINISTICI (calcolati da Python — NON modificare) ===" devi usare ESATTAMENTE quei valori numerici X/5 e il totale X/20. NON ricalcolare. NON modificare. Copia i numeri così come sono.
Il VERDETTO PYTHON indicato nel blocco è vincolante: se dice GIOCA, il tuo output deve essere GIOCA. Se dice NO BET, output NO BET.

FORMATTAZIONE: usa solo tag HTML Telegram. <b>grassetto</b> per valori chiave. Niente asterischi, niente ##, niente tabelle.

Formato risposta quick — SOLO questo blocco:

🎾 <b>[Home] vs [Away]</b> | [Torneo/N/D]
📅 [Data/N/D]

⚡ <b>QUICK VERDICT</b>
FAV: <b>[nome FAV dai dati Python] @ [quota Pinnacle FAV]</b> | UND: <b>[nome UND] @ [quota]</b>
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
    content_parts = []
    content_parts.append(make_text_block(f"DATA PARTITA: {today}"))

    # Calcola rating deterministici se abbiamo HTML
    if html_data:
        ratings = compute_tennis_ratings(html_data)
        # Aggiungi OLS se disponibile
        ols_data = state.get("ols_data")
        if ols_data:
            # Finalizza OLS se non ancora fatto
            if ols_data.get("delta_pct") is None:
                p = html_data["pinnacle"]
                hq = p.get("home_curr")
                aq = p.get("away_curr")
                pinn_fav = min(hq, aq) if hq and aq else hq or aq
                ols_data = finalize_ols(ols_data, pinn_fav)
                state["ols_data"] = ols_data
            ratings["ols"] = ols_data
        state["last_ratings"] = ratings
        ols_text = ""
        if ols_data and ols_data.get("delta_pct") is not None:
            ols_text = (
                f"\n\n=== OLS ===\n"
                f"Rank sogg: {ols_data['rank_subj']} | Rank avv: {ols_data['rank_opp']}\n"
                f"Forecast: {ols_data['forecast']:.3f} | Pinnacle FAV: {ols_data.get('pinnacle_q','N/A')}\n"
                f"Δ%: {ols_data['delta_pct']:+.1f}% | R²: {ols_data['r2']:.3f} | {ols_data['classification']}\n"
                f"Segnale OLS: {ols_data['signal']} | Rating: {ols_data['rating']}/5 | {'✅ ATTIVO' if ols_data.get('active') else '⚠️ non attivo'}\n"
            )
        content_parts.append(make_text_block(
            "=== DATI HTML TENNISEXPLORER ===\n" +
            build_tennis_summary(html_data) +
            "\n\n" +
            format_tennis_ratings(html_data, ratings) +
            ols_text
        ))

    for img_b64, mime in screenshots:
        content_parts.append(make_text_block("=== SCREENSHOT ASIANODDS ==="))
        content_parts.append(make_image_block(img_b64, mime))

    if not content_parts or len(content_parts) == 1:
        return "❌ Nessun dato disponibile."

    return await claude_call(TENNIS_QUICK_SYSTEM, content_parts, model="claude-haiku-4-5-20251001", max_tokens=1500)


async def tennis_extended(state: dict) -> str:
    protocol = get_lba_protocol()
    # Tronca protocollo per evitare timeout — regole operative stanno nei primi 2000 chars
    system = TENNIS_EXTENDED_SYSTEM.format(protocol=protocol[:2000])
    html_data = state.get("html_data")
    today = datetime.now().strftime("%d/%m/%Y")

    # Estesa usa solo quick + ratings — HTML summary già incluso nella quick
    parts = [f"DATA PARTITA: {today}"]
    quick = state.get("last_quick", "")
    if quick:
        parts.append(f"=== QUICK ANALYSIS GIÀ ESEGUITA ===\n{quick}")
    # Passa anche i rating deterministici se disponibili
    ratings = state.get("last_ratings")
    if ratings and html_data:
        parts.append(format_tennis_ratings(html_data, ratings))
    if not quick:
        return "❌ Esegui prima la quick analysis (go)."

    content = [make_text_block("\n\n".join(parts))]
    return await claude_call(system, content, model="claude-haiku-4-5-20251001", max_tokens=2000, timeout=180)


async def tennis_recap(state: dict) -> str:
    # Usa estesa se disponibile, altrimenti parte dalla quick
    base = state.get("last_extended", "") or state.get("last_quick", "")
    if not base:
        return "❌ Esegui prima la quick analysis (go)."
    html_data = state.get("html_data")
    extra = ""
    if html_data:
        extra = "\n\n=== DATI AGGIUNTIVI ===\n" + build_tennis_summary(html_data)
    content = [make_text_block(f"Analisi disponibile:\n{base}{extra}\n\nProduci il recap Telegram.")]
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
    today = datetime.now().strftime("%d/%m/%Y")
    quick = state.get("last_quick", "")
    if not quick:
        return "❌ Esegui prima la quick analysis (go)."

    # Estesa usa solo testo — immagine già processata nella quick
    parts = [
        f"DATA PARTITA: {today}",
        f"=== QUICK ANALYSIS GIÀ ESEGUITA ===\n{quick}",
        "Approfondisci layer per layer per 1X2 e U/O 2.5."
    ]
    content = [make_text_block("\n\n".join(parts))]
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

    try:
        if mode == "tennis":
            result = await tennis_extended(state)
        else:
            result = await soccer_extended(state)
    except asyncio.TimeoutError:
        await update.message.reply_text("⏱ Timeout analisi. Riprova con /analisi.")
        return
    except Exception as e:
        logger.error(f"tennis_extended error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Errore analisi estesa: {str(e)[:300]}")
        return

    state["last_extended"] = result

    # Telegram ha limite 4096 chars — splitta se necessario
    for chunk in split_message(result):
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(chunk)


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    if state.get("mode") != "tennis":
        await update.message.reply_text("❌ Il recap è disponibile solo in modalità tennis.")
        return
    if not state.get("last_quick") and not state.get("last_extended"):
        await update.message.reply_text("❌ Esegui prima la quick analysis (go).")
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
    snapshot = format_quote_snapshot(data)
    torneo = " | ".join(filter(None, [
        data.get("match_tournament",""), data.get("match_round",""), data.get("match_surface","")
    ]))
    data_ora = " ".join(filter(None, [data.get("match_date",""), data.get("match_time","")]))
    msg = (
        f"✅ <b>{data['home_name']} vs {data['away_name']}</b>\n"
        f"🏆 {torneo or 'N/D'} | 📅 {data_ora or 'N/D'}\n"
        f"⚖️ FAV: <b>{data['fav_name']}</b> ({data['fav_side']}) @ {data['fav_q']} | "
        f"UND: <b>{data['und_name']}</b> @ {data['und_q']}\n\n"
        f"<code>{snapshot}</code>\n\n"
        "Puoi aggiungere screenshot AsianOdds o dati OLS, oppure scrivi <b>go</b>."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


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

    # Input OLS: prima riga inizia con "ols"
    if text.lower().startswith("ols"):
        if state.get("mode") != "tennis":
            await update.message.reply_text("❌ OLS disponibile solo in modalità /tennis.")
            return
        ols_result = parse_ols_input(update.message.text.strip())
        if "error" in ols_result:
            await update.message.reply_text(f"❌ OLS: {ols_result['error']}")
            return
        # Finalizza con quota Pinnacle del favorito se HTML già caricato
        html_data = state.get("html_data")
        if html_data:
            p = html_data["pinnacle"]
            hq = p.get("home_curr")
            aq = p.get("away_curr")
            pinn_fav = min(hq, aq) if hq and aq else hq or aq
            ols_result = finalize_ols(ols_result, pinn_fav)
        state["ols_data"] = ols_result
        delta_str = f"Δ%={ols_result['delta_pct']:+.1f}% ({ols_result['classification']}) | {ols_result['signal']}" if ols_result.get("delta_pct") is not None else "Δ% calcolato al go"
        active_str = "✅ ATTIVO" if ols_result.get("active") else "⚠️ R²<0.60 o sub-threshold"
        await update.message.reply_text(
            f"✅ OLS caricato — {len(ols_result['rows'])} partite storiche\n"
            f"Rank: sogg={ols_result['rank_subj']} avv={ols_result['rank_opp']}\n"
            f"Forecast: {ols_result['forecast']:.3f} | R²={ols_result['r2']:.3f}\n"
            f"{delta_str}\n"
            f"Layer OLS: {active_str}\n\n"
            "Scrivi go per analisi completa."
        )
        return

    await update.message.reply_text(
        "Comandi disponibili:\n"
        "• /tennis — modalità tennis\n"
        "• /calcio — modalità calcio\n"
        "• go — avvia analisi\n"
        "• /analisi — analisi estesa\n"
        "• /recap — recap Telegram (tennis)\n"
        "• /protocollo lba / soccer\n"
        "• /reset — azzera"
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

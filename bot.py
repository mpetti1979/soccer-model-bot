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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
        if not book_name or len(cells) < 16:
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
        # Home history starts at cell 2
        home_history = get_history_flat(2)
        home_open = next((h["q"] for h in home_history if h["time"] == "open"), None)

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

        away_history = get_history_flat(away_cell_idx) if away_cell_idx else []
        away_open = next((h["q"] for h in away_history if h["time"] == "open"), None)

        if home_current is None:
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

    # Pinnacle outlier: Pinnacle >= max retail sul quel lato
    retail_home_max = max([v["home_current"] for v in retail.values() if v["home_current"]], default=None)
    retail_away_max = max([v["away_current"] for v in retail.values() if v["away_current"]], default=None)
    outlier_home = pinn_home_curr and retail_home_max and (pinn_home_curr >= retail_home_max - 0.01)
    outlier_away = pinn_away_curr and retail_away_max and (pinn_away_curr >= retail_away_max - 0.01)

    # Drift Pinnacle
    pinn_drift_home = round(pinn_home_curr - pinn_home_open, 3) if pinn_home_curr and pinn_home_open else None
    pinn_drift_away = round(pinn_away_curr - pinn_away_open, 3) if pinn_away_curr and pinn_away_open else None

    # Retail drift
    retail_drift_home = round(retail_home_curr - retail_home_open, 3) if retail_home_curr and retail_home_open else None
    retail_drift_away = round(retail_away_curr - retail_away_open, 3) if retail_away_curr and retail_away_open else None

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
    }


# ─────────────────────────────────────────────
# SYSTEM PROMPT ANALISI
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """Sei un analista di scommesse tennis specializzato nel flusso sharp money Pinnacle.

Ricevi dati strutturati da TennisExplorer (e opzionalmente OCR da screenshot AsianOdds) e produci un'analisi completa.

## SEGNALI DA VALUTARE (max 3 segnali meccanici + OLS opzionale)

### Segnale 1 — FLUSSO PINNACLE (obbligatorio)
- Pinnacle drift home: se scende → soldi su Home → segnale PRO Away
- Pinnacle drift away: se scende → soldi su Away → segnale PRO Home
- Regola: quota che SCENDE su Pinnacle = soldi che entrano su quel lato = segnale sull'ALTRO lato
- Forza: |drift| > 0.10 = forte, 0.05-0.10 = medio, < 0.05 = debole

### Segnale 2 — OUTLIER PINNACLE (obbligatorio)
- Se Pinnacle è il book con quota MAX su un lato = non ha paura dell'esposizione su quel lato = soldi sharp sull'altro
- Es: Pinnacle MAX su Away → sharp money su Home → segnale PRO Home
- Verifica con gap Pinnacle vs media retail

### Segnale 3 — DRIFT RETAIL (obbligatorio)
- Media retail apertura vs attuale: in quale direzione si è mosso il mercato?
- Convergenza o divergenza col drift Pinnacle?
- Se retail e Pinnacle driftano nella stessa direzione = segnale più forte

### Segnale 4 — OLS (opzionale, solo se dati storici forniti)
- Confronto forecast OLS vs Pinnacle attuale sul soggetto
- forecast < mercato → mercato quota soggetto più alto del modello → non ha paura di ricevere gioco sul soggetto → sharp sull'avversario → SEGNALE PRO avversario
- forecast > mercato → mercato quota soggetto più basso del modello → non ha paura di ricevere gioco sull'avversario → sharp sul soggetto → SEGNALE PRO soggetto
- Aggiungi come 4° segnale con R² e Δ%

## OUTPUT FORMAT (Telegram markdown)

```
🎾 [TORNEO] — [ROUND] | [SUPERFICIE]
📅 [DATA] | [ORA]
🇮🇹 [HOME] vs [AWAY] 🏳️

[Testo narrativo 2-3 righe: descrivi il flusso in modo chiaro. Inizia sempre dal dato più forte. Mai gergo tecnico grezzo — racconta cosa sta succedendo.]

⭐ Flusso Pinnacle: ★★★☆☆
⭐ Outlier: ★★★★☆
⭐ Drift retail: ★★★☆☆
⭐ OLS: N/A (o ★★★★★ se disponibile)

🎯 [GIOCATORE SEGNALATO] | Quota ~[QUOTA MAX]
📦 Stake: [0.5% standard | 0.8% forte | 1.0% molto forte]

✅ BET / ❌ NO BET — [N]/[TOT] segnali convergenti
```

## REGOLE STAKE
- 1/3 segnali convergenti → NO BET
- 2/3 segnali convergenti → 0.5% bankroll
- 3/3 segnali convergenti → 0.8%
- 3/3 + OLS convergente → 1.0%

## REGOLA QUOTA MAX
Usa sempre la quota MAX attuale sul lato segnalato (dal campo max_home o max_away nei dati).

Rispondi SOLO con il messaggio formattato. Zero spiegazioni aggiuntive."""


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
        f"Outlier Home (Pinnacle=MAX): {p['outlier_home']}",
        f"Outlier Away (Pinnacle=MAX): {p['outlier_away']}",
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

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}]
    )
    return response.content[0].text


async def analyze_screenshot(image_b64: str, mime: str) -> str:
    """OCR + analisi da screenshot AsianOdds (senza HTML)."""
    response = client.messages.create(
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
        ocr_response = client.messages.create(
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
        state["html_data"] = None
        state["ols_data"] = None
        await update.message.reply_text(result, parse_mode="Markdown")
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
# MAIN
# ─────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot avviato")
    app.run_polling()


if __name__ == "__main__":
    main()

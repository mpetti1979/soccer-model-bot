import os
import logging
import requests
import anthropic
import base64
import re
import math
import json
import io
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

PROTOCOLS = {
    "soccer": "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/soccer_model_protocol.html",
    "tennis": "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/tennis_lba_protocol.html",
}

STATE_IDLE = "idle"
STATE_SPORT_SELECTED = "sport_selected"
STATE_WAITING_OLS = "waiting_ols"
STATE_READY = "ready"

USER_SESSIONS = {}

def get_user(user_id):
    if user_id not in USER_SESSIONS:
        USER_SESSIONS[user_id] = {
            "sport": None,
            "images": [],
            "html_source": None,
            "ols_dataset": None,
            "state": STATE_IDLE,
        }
    return USER_SESSIONS[user_id]

def reset_user(user_id):
    USER_SESSIONS[user_id] = {
        "sport": None,
        "images": [],
        "html_source": None,
        "ols_dataset": None,
        "state": STATE_IDLE,
    }

def load_protocol(sport: str) -> str:
    url = PROTOCOLS.get(sport)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error(f"Error loading protocol {sport}: {e}")
        return None

def detect_media_type(image_bytes: bytes) -> str:
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    elif image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    return "image/jpeg"

def split_message(text: str, max_length: int = 4000) -> list:
    if len(text) <= max_length:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = text.rfind('\n', 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks


def is_ols_format(text: str) -> bool:
    text_lower = text.lower()
    return "fav:" in text_lower and "und:" in text_lower

def parse_ols_dataset(text: str) -> dict:
    result = {"fav_rank": None, "und_rank": None, "rows": []}
    lines = text.strip().split('\n')
    in_rows = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        ll = line.lower()
        if ll.startswith("fav:"):
            try:
                result["fav_rank"] = int(ll.replace("fav:", "").strip())
            except:
                pass
        elif ll.startswith("und:"):
            try:
                result["und_rank"] = int(ll.replace("und:", "").strip())
            except:
                pass
        elif "avversari" in ll:
            in_rows = True
        elif in_rows:
            parts = line.split()
            if len(parts) == 3:
                try:
                    result["rows"].append({
                        "q_fav": int(parts[0]) / 100,
                        "q_avv": int(parts[1]) / 100,
                        "rank_avv": int(parts[2])
                    })
                except:
                    pass
    return result

def run_ols(parsed_ols: dict) -> dict:
    rows = parsed_ols["rows"]
    und_rank = parsed_ols["und_rank"]
    if len(rows) < 3:
        return {"error": "Minimo 3 righe storiche richieste"}

    xs, ys = [], []
    for row in rows:
        total = (1 / row["q_fav"]) + (1 / row["q_avv"])
        p_fav = (1 / row["q_fav"]) / total
        xs.append(math.log(row["rank_avv"]))
        ys.append(math.log(p_fav))

    n = len(xs)
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xy = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    ss_xx = sum((xs[i] - x_mean) ** 2 for i in range(n))

    if ss_xx == 0:
        return {"error": "Varianza nulla"}

    b = ss_xy / ss_xx
    a = y_mean - b * x_mean

    y_pred = [a + b * x for x in xs]
    ss_res = sum((ys[i] - y_pred[i]) ** 2 for i in range(n))
    ss_tot = sum((ys[i] - y_mean) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    fair_value_fav = None
    if und_rank:
        ln_p = a + b * math.log(und_rank)
        fair_value_fav = round(1 / math.exp(ln_p), 3)

    return {
        "a": round(a, 4), "b": round(b, 4),
        "r2": round(r2, 3),
        "fair_value_fav": fair_value_fav,
        "n_rows": n
    }


def analyze_tennis(user: dict, protocol_text: str) -> tuple:
    """Analisi tennis v2.1. Ritorna (testo_analisi, html_grafico_o_None, filename_html)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # OLS (opzionale)
    ols_info = "\nOLS: Non fornito — step opzionale non eseguito."
    if user["ols_dataset"]:
        parsed = parse_ols_dataset(user["ols_dataset"])
        if parsed["rows"] and parsed["und_rank"]:
            ols_result = run_ols(parsed)
            ols_info = f"""
OLS DATASET (step opzionale):
- FAV rank UTR: {parsed['fav_rank']}
- UND rank UTR: {parsed['und_rank']}
- Righe storiche: {len(parsed['rows'])}
- Fair value FAV: {ols_result.get('fair_value_fav')}
- R²: {ols_result.get('r2')}

REGOLA OLS DIREZIONE (MAI SBAGLIARE):
- fair_value_FAV > Pinnacle_FAV → modello vede FAV sottovalutato → segnale PRO FAV
- fair_value_FAV < Pinnacle_FAV → modello vede FAV sopravvalutato → segnale PRO UND
- OLS converge con segnale Pinnacle → Punto 5 OLS ATTIVO; diverge → NON ATTIVO
"""
        else:
            ols_info = "\nOLS: Dataset insufficiente (minimo 3 righe)."

    # HTML grezzo (max 80000 chars) — nessun parsing, direttamente a Claude
    html_raw = ""
    if user["html_source"]:
        html_raw = f"\n\nHTML GREZZO TENNISEXPLORER (max 80000 chars):\n{user['html_source'][:80000]}"

    system_prompt = f"""Sei un analista betting tennis. Applica il Protocollo LBA — Pinnacle Workflow v2.1 in modo preciso.

=== DEFINIZIONI BASE ===
- K1 = primo giocatore elencato nella pagina
- K2 = secondo giocatore elencato
- FAV = giocatore con quota Pinnacle PIÙ BASSA
- UND = giocatore con quota Pinnacle PIÙ ALTA
- Gap = Pinnacle_now − avg_retail_now (su quel segno)
- avg_retail = media ESCLUDENDO Pinnacle e Betfair
- Soglia gap: ≥10 tick (0.10) forte · 5-9 tick zona grigia · <5 sub-soglia

=== I 5 PUNTI DI ANALISI ===

PUNTO 1 — Gap attuale Pinnacle vs retail
- Pinnacle MAX su X di ≥10 tick → non vuole esposizione su X → segnale PRO Y
- 5-9 tick → zona grigia, segnale debole
- <5 tick → sub-soglia

PUNTO 2 — Comportamento retail (Δ apertura → attuale)
- Calcola Δ_retail_K1 = avg_now_K1 - avg_open_K1
- Retail stesso senso sharp → conferma; direzione opposta → più robusto; fermo → molto robusto

PUNTO 3 — Leadership apertura (chi muove per primo)
- lag = Pinnacle_open - avg_retail_ante_Pinnacle
- lag < -0.05 → GUIDA (gap = segnale sharp puro)
- -0.05 ≤ lag ≤ +0.10 → RECEPISCE (gap valido)
- lag > +0.10 → INSEGUE (gap potenzialmente artefatto → leggere Punto 4)

PUNTO 4 — Drift e timing Pinnacle
- Δ_Pinnacle_K1 = Pinnacle_now_K1 - Pinnacle_open_K1
- X scende nel tempo → PRO X; X sale → PRO Y; flat → neutro
- Timing: pre-match (ultime 2h) = peso massimo; notte = peso alto; apertura = medio

PUNTO 5 — Direzionalità
- Unidirezionale → segnale forte; con rimbalzo → indebolito; flat → neutro
- Rimbalzo pre-match = segnale originale indebolito

=== SCORING CONVERGENZA ===
- 4-5 punti stessa direzione → GIOCA
- 3 punti + gap ≥10 tick → GIOCA
- 2-3 punti + gap zona grigia → ATTENZIONE (stake ridotto)
- 0-1 punti / gap sub-soglia → NO BET
- Rimbalzo pre-match → NO BET o rivalutare

=== OUTPUT PARTE 1 — ANALISI TESTUALE ===
Produci questa analisi:

MATCH: [K1] vs [K2] · [Torneo] · [Data] · [Superficie]
FAV: [nome] | Pinnacle now: [x.xx] | Pinnacle open: [x.xx] | avg retail now: [x.xx] ([N] book) | Gap: [±x.xx]
UND: [nome] | Pinnacle now: [x.xx] | Pinnacle open: [x.xx] | avg retail now: [x.xx] ([N] book) | Gap: [±x.xx]

PUNTO 1 · Gap: [✓/—/✗] [descrizione + tick] → PRO [nome] / zona grigia / sub-soglia
PUNTO 2 · Retail: [✓/—/✗] [Δ_K1 e Δ_K2] → [segue/diverge/fermo]
PUNTO 3 · Leadership: [GUIDA/RECEPISCE/INSEGUE] — lag=[x.xx] → [impatto sul gap]
PUNTO 4 · Drift+timing: [✓/—/✗] Δ_FAV=[x.xx] Δ_UND=[x.xx] · timing=[fase] → PRO [nome] / neutro
PUNTO 5 · Direzionalità: [UNIDIREZIONALE/RIMBALZO/FLAT] → [forte/indebolito/neutro]

CONVERGENZA: [N]/5 punti PRO [nome] · gap max [x.xx]
VERDICT: GIOCA [nome] @ [quota] [stake]u / ATTENZIONE [nome] @ [quota] [stake ridotto]u / NO BET

---

=== OUTPUT PARTE 2 — FILE HTML ===
Dopo l'analisi testuale, produci il blocco delimitato esattamente così:

HTML_OUTPUT_START
[file HTML completo standalone]
HTML_OUTPUT_END

Il file HTML deve rispettare queste specifiche obbligatorie:

STRUTTURA (in ordine):
1. Match header: nome match · torneo · data · ora · superficie
2. Verdict Box: pallini ✓/—/✗ per i 5 punti + verdetto GIOCA/ATTENZIONE/NO BET + griglia metriche
3. Grafico Chart.js con 6 dataset:
   - Curva solida FAV Pinnacle (verde #2E7D32)
   - Curva solida UND Pinnacle (rosso #C62828)
   - Tratteggiata [6,4] avg_now FAV (verde, opacity 0.5)
   - Tratteggiata [6,4] avg_now UND (rosso, opacity 0.5)
   - Tratteggiata [2,4] avg_open FAV (verde, opacity 0.15)
   - Tratteggiata [2,4] avg_open UND (rosso, opacity 0.15)
   I punti del grafico devono essere colorati: verde se quota scende, rosso se sale, grigio per il primo punto
4. Card metriche (griglia 2 colonne): quota attuale, avg retail, gap (rosso≥10tick, arancio 5-9, grigio<5), Δ Pinnacle, Δ retail
5. Tabella 5 punti: numero, nome, badge colorato ✓/—/✗
6. Timeline Pinnacle: riga per rilevazione con pallino fase (mattina=blu, pomeriggio=arancio, sera=rosso, notte=viola), barra proporzionale, delta, evidenzia rimbalzi in #FFF8E1

COLORI VERDETTO: GIOCA → #2E7D32 · ATTENZIONE → #E65100 · NO BET → #999
TECNICO: Chart.js 4.4.1 da cdnjs · font -apple-system · bg body #f8f8f6 · card bg #fff con box-shadow 0 1px 4px · border-radius 10px · padding body 12px · mobile-first 375px

NOME FILE (includi in un commento HTML <!-- filename: cognome_k1_cognome_k2_pinnacle.html -->)

PROTOCOLLO COMPLETO:
{protocol_text[:4000]}"""

    instruction = f"""Analizza il match tennis con il Pinnacle Workflow v2.1.
{ols_info}
{html_raw}"""

    content = []
    for img_bytes in user["images"]:
        image_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = detect_media_type(img_bytes)
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}})
    content.append({"type": "text", "text": instruction})

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    full_response = message.content[0].text

    # Estrai HTML dal blocco delimitato
    html_chart = None
    filename_html = "pinnacle_drift.html"
    html_match = re.search(r'HTML_OUTPUT_START\s*(.*?)\s*HTML_OUTPUT_END', full_response, re.DOTALL)
    if html_match:
        html_chart = html_match.group(1).strip()
        # Estrai filename dal commento HTML se presente
        fn_match = re.search(r'filename:\s*([\w_-]+\.html)', html_chart)
        if fn_match:
            filename_html = fn_match.group(1)

    # Testo analisi = tutto prima di HTML_OUTPUT_START
    clean_text = re.sub(r'\s*HTML_OUTPUT_START.*$', '', full_response, flags=re.DOTALL).strip()

    return clean_text, html_chart, filename_html

def analyze_soccer(user: dict, protocol_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    content = []
    for img_bytes in user["images"]:
        image_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = detect_media_type(img_bytes)
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}})
    content.append({"type": "text", "text": "Analizza questo screenshot calcio applicando il protocollo Soccer Model. Concludi SEMPRE con il VERDICT block."})

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2500,
        system=f"""Sei un analista betting calcio. Applica il protocollo Soccer Model.
PROTOCOLLO:
{protocol_text}
Rispondi SEMPRE in italiano. SEMPRE includi il VERDICT finale.""",
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text

async def send_long_message(update: Update, text: str):
    for chunk in split_message(text):
        await update.message.reply_text(chunk)

HELP_TEXT = (
    "👋 *Betting Analysis Bot*\n\n"
    "Seleziona lo sport:\n\n"
    "⚽ Scrivi *soccer*\n"
    "🎾 Scrivi *tennis*\n\n"
    "Scrivi *reset* per ricominciare."
)

OLS_FORMAT_HELP = (
    "📐 *Dataset OLS* (opzionale):\n\n"
    "```\n"
    "fav: 285\n"
    "und: 310\n"
    "avversari passati:\n"
    "237 153 259\n"
    "198 210 180\n"
    "```\n\n"
    "• `fav:` → rank UTR FAV oggi\n"
    "• `und:` → rank UTR UND oggi\n"
    "• Righe: q\\_fav q\\_avv rank\\_avv (÷100 per quota)\n\n"
    "Oppure scrivi *no* per procedere senza OLS."
)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["sport"]:
        await update.message.reply_text("⚠️ Seleziona prima lo sport: scrivi *soccer* o *tennis*.")
        return

    doc = update.message.document
    if not doc:
        return

    filename = doc.file_name or ""
    ext = filename.lower().split(".")[-1] if "." in filename else ""

    if user["sport"] == "tennis" and ext in ("html", "htm", "txt"):
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        raw = file_bytes.decode("utf-8", errors="ignore")
        # HTML grezzo direttamente a Claude — nessun parsing locale
        user["html_source"] = raw[:80000]
        user["state"] = STATE_WAITING_OLS
        html_len = len(user["html_source"])
        logger.info(f"[DOC] user_id={user_id} html_source len={html_len} state={user['state']}")
        await update.message.reply_text(
            f"📄 File *{filename}* caricato ({html_len} chars).\n\n" + OLS_FORMAT_HELP,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Formato non supportato. Allega un file .html o .txt con le quote.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["sport"]:
        await update.message.reply_text("⚠️ Seleziona prima lo sport: scrivi *soccer* o *tennis*.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    user["images"].append(bytes(image_bytes))
    count = len(user["images"])
    sport_emoji = "⚽" if user["sport"] == "soccer" else "🎾"

    if user["sport"] == "tennis" and user["state"] == STATE_SPORT_SELECTED:
        user["state"] = STATE_WAITING_OLS
        await update.message.reply_text(
            f"📥 Screenshot {count} ricevuto ({sport_emoji} TENNIS).\n\n" + OLS_FORMAT_HELP,
            parse_mode="Markdown"
        )
    elif user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        await update.message.reply_text(
            f"📥 Screenshot {count} aggiunto.\n"
            f"Manda il dataset OLS oppure scrivi *no* per procedere senza.",
            parse_mode="Markdown"
        )
    else:
        if user["state"] == STATE_SPORT_SELECTED:
            user["state"] = STATE_READY
        await update.message.reply_text(
            f"📥 Screenshot {count} ricevuto ({sport_emoji} {user['sport'].upper()}).\n\n"
            f"Manda altri screenshot oppure scrivi *analizza*."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user = get_user(user_id)
    text = update.message.text.strip()
    text_lower = text.lower()

    # Sport selection
    if text_lower in ("soccer", "tennis", "ippica"):
        reset_user(user_id)
        user = get_user(user_id)
        user["sport"] = text_lower
        user["state"] = STATE_SPORT_SELECTED
        sport_emoji = "⚽" if text_lower == "soccer" else "🎾"
        msg = (
            f"{sport_emoji} Sport selezionato: *{text_lower.upper()}*\n\n"
            f"{'Allega il file .html o .txt con le quote TennisExplorer.' if text_lower == 'tennis' else 'Manda screenshot e poi scrivi *analizza*.'}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # Reset
    if text_lower == "reset":
        reset_user(user_id)
        await update.message.reply_text("🗑 Reset completato.\n\n" + HELP_TEXT, parse_mode="Markdown")
        return

    # No OLS
    if text_lower in ("no", "skip") and user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        user["ols_dataset"] = None
        user["state"] = STATE_READY
        has_data = bool(user["images"]) or bool(user["html_source"])
        await update.message.reply_text(
            f"✅ Procedo senza OLS.\n"
            f"Dati in coda: {'✅ HTML caricato' if user['html_source'] else '❌ nessun HTML'} | "
            f"{'✅ ' + str(len(user['images'])) + ' screenshot' if user['images'] else '❌ nessuno screenshot'}\n"
            f"Scrivi *analizza* quando pronto.",
            parse_mode="Markdown"
        )
        return

    # Analizza
    if text_lower == "analizza":
        if not user["sport"]:
            await update.message.reply_text("⚠️ Seleziona prima lo sport.")
            return
        if not user["images"] and not user["html_source"]:
            await update.message.reply_text("❌ Nessun dato. Allega prima un file HTML o screenshot.")
            return
        if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
            await update.message.reply_text(
                "⚠️ Vuoi aggiungere OLS?\n\n" + OLS_FORMAT_HELP, parse_mode="Markdown"
            )
            return

        sport = user["sport"]
        sport_emoji = "⚽" if sport == "soccer" else "🎾"
        await update.message.reply_text(f"🔍 Analizzo {sport_emoji} {sport.upper()}...")

        try:
            protocol = load_protocol(sport)
            if not protocol:
                await update.message.reply_text("❌ Errore caricamento protocollo.")
                return

            if sport == "tennis":
                result_text, html_chart, filename_html = analyze_tennis(user, protocol)
            else:
                result_text = analyze_soccer(user, protocol)
                html_chart = None
                filename_html = None

            user["images"] = []
            user["html_source"] = None
            user["ols_dataset"] = None
            user["state"] = STATE_SPORT_SELECTED

            await send_long_message(update, result_text)

            if html_chart:
                html_bytes = html_chart.encode("utf-8")
                html_file = io.BytesIO(html_bytes)
                html_file.name = filename_html
                await update.message.reply_document(
                    document=html_file,
                    filename=filename_html,
                    caption="📊 Pinnacle Workflow v2.1 — aprilo nel browser"
                )

        except Exception as e:
            logger.error(f"Errore analisi: {e}")
            await update.message.reply_text(f"❌ Errore: {str(e)}")
        return

    # OLS formato nuovo
    if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS and is_ols_format(text):
        user["ols_dataset"] = text
        user["state"] = STATE_READY
        parsed = parse_ols_dataset(text)
        await update.message.reply_text(
            f"📐 Dataset OLS ricevuto:\n"
            f"• FAV rank UTR: {parsed['fav_rank']}\n"
            f"• UND rank UTR: {parsed['und_rank']}\n"
            f"• Righe storiche: {len(parsed['rows'])}\n\n"
            f"Scrivi *analizza* per procedere.",
            parse_mode="Markdown"
        )
        return

    # HTML incollato direttamente nel chat
    if user["sport"] == "tennis" and len(text) > 100 and "<" in text and ">" in text:
        user["html_source"] = text[:80000]
        user["state"] = STATE_WAITING_OLS
        await update.message.reply_text(
            "📄 HTML ricevuto.\n\n" + OLS_FORMAT_HELP, parse_mode="Markdown"
        )
        return

    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()

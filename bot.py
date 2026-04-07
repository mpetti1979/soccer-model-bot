import os
import json
import logging
import requests
import anthropic
import base64
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
SHEET_ID = "1YY4qeOGfDFChLiHsoEEewFWPDw3edI3R"
SHEET_NAME = "DB"

PROTOCOLS = {
    "soccer": "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/soccer_model_protocol.html",
    "tennis": "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/tennis_lba_protocol.html",
}

STATE_IDLE = "idle"
STATE_SPORT_SELECTED = "sport_selected"
STATE_WAITING_OLS = "waiting_ols"
STATE_READY = "ready"
STATE_WAITING_RESULT = "waiting_result"

user_data = {}

# ── Google Sheets ──────────────────────────────────────────────
def get_sheets_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def get_next_row_number(service) -> int:
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_NAME}!A:A"
    ).execute()
    values = result.get("values", [])
    return len(values) + 1

def append_row_to_sheet(row_data: list) -> bool:
    try:
        service = get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A:T",
            valueInputOption="USER_ENTERED",
            body={"values": [row_data]}
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return False

def build_db_row(pending: dict, esito: str) -> list:
    """Costruisce la riga DB dal pending match + esito W/L."""
    p = pending
    morf = p.get("morf", "nd")
    outl_und = p.get("outl_und", "nd")
    outl_fav = p.get("outl_fav", "nd")
    modello = p.get("modello_segnale", "nd")

    # Logica OK columns
    def calc_ok(segnale_pro_fav, esito_w):
        if segnale_pro_fav is None:
            return "nd"
        if esito_w and segnale_pro_fav:
            return "SI"
        if esito_w and not segnale_pro_fav:
            return "NO"
        if not esito_w and segnale_pro_fav:
            return "NO"
        return "SI"

    esito_w = (esito.upper() == "W")

    morf_pro_fav = p.get("morf_pro_fav")
    outl_und_present = (outl_und == "SI")
    outl_fav_present = (outl_fav == "SI")
    mod_pro_fav = p.get("mod_pro_fav")

    morf_ok = calc_ok(morf_pro_fav, esito_w) if morf_pro_fav is not None else "nd"
    outl_und_ok = calc_ok(True, esito_w) if outl_und_present else "nd"
    outl_fav_ok = calc_ok(False, esito_w) if outl_fav_present else "nd"
    mod_ok = calc_ok(mod_pro_fav, esito_w) if mod_pro_fav is not None else "nd"

    row = [
        p.get("num", ""),
        p.get("data", datetime.now().strftime("%d/%m")),
        p.get("torneo", ""),
        p.get("sup", ""),
        p.get("fav", ""),
        p.get("und", ""),
        p.get("q_fav", ""),
        p.get("q_und", ""),
        p.get("r2", "nd"),
        p.get("delta_pct", "nd"),
        p.get("fascia", "nd"),
        morf,
        outl_und,
        p.get("elo_delta", "nd"),
        p.get("elo_arrow", "nd"),
        esito.upper(),
        mod_ok,
        morf_ok,
        outl_und_ok,
        p.get("elo_ok", "nd"),
    ]
    return row

# ── Utilities ──────────────────────────────────────────────────
def get_user(user_id):
    if user_id not in user_data:
        user_data[user_id] = {
            "sport": None, "images": [], "html_source": None,
            "ols_dataset": None, "state": STATE_IDLE,
            "pending_match": None,
        }
    return user_data[user_id]

def reset_user(user_id):
    user_data[user_id] = {
        "sport": None, "images": [], "html_source": None,
        "ols_dataset": None, "state": STATE_IDLE,
        "pending_match": None,
    }

def load_protocol(sport):
    url = PROTOCOLS.get(sport)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error(f"Protocol load error: {e}")
        return None

def detect_media_type(image_bytes):
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    elif image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    return "image/jpeg"

def split_message(text, max_length=4000):
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

def extract_tennis_data(html):
    header = html[:5000]
    betting_markers = ["Betting odds", "Home/Away ("]
    odds_start = -1
    for marker in betting_markers:
        idx = html.find(marker)
        if idx > 0:
            odds_start = max(0, idx - 100)
            break
    if odds_start == -1:
        for marker in ["bet365", "Pinnacle", "10Bet"]:
            idx = html.find(marker)
            if idx > 0:
                odds_start = max(0, idx - 200)
                break
    odds_section = html[odds_start:odds_start+60000] if odds_start != -1 else html[3000:18000]
    return (
        "=== MATCH INFO ===\n" + header +
        "\n\n=== BETTING ODDS ===\n" + odds_section
    )

def parse_analysis_for_db(analysis_text: str) -> dict:
    """Estrae i dati chiave dall'analisi per il DB. Usa valori di default se non trovati."""
    import re
    data = {}

    def find(patterns, text, default="nd"):
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return default

    data["torneo"] = find([r"🎾\s*(.+?)\s*—", r"Torneo[:\s]+(.+?)[\n|]"], analysis_text)
    data["sup"] = find([r"Clay|Hard|Grass|Erba|Terra"], analysis_text)
    data["fav"] = find([r"FAV[:\s]+([A-Za-z\s\.]+?)[@\n]", r"👤\s*FAV[:\s]+([A-Za-z\s\.]+?)@"], analysis_text)
    data["und"] = find([r"UND[:\s]+([A-Za-z\s\.]+?)[@\n]", r"👤\s*UND[:\s]+([A-Za-z\s\.]+?)@"], analysis_text)
    data["q_fav"] = find([r"FAV[^\n]+@\s*([\d\.]+)", r"Q_FAV[:\s]+([\d\.]+)"], analysis_text)
    data["q_und"] = find([r"UND[^\n]+@\s*([\d\.]+)", r"Q_UND[:\s]+([\d\.]+)"], analysis_text)
    data["r2"] = find([r"R²[:\s]+([\d\.]+)", r"R2[:\s]+([\d\.]+)"], analysis_text)
    data["delta_pct"] = find([r"Δ%[:\s]+([-\d\.]+)%?", r"Delta%[:\s]+([-\d\.]+)"], analysis_text)
    data["fascia"] = find([r"Fascia[:\s]+(forte|mod|debole|sub|non op)"], analysis_text)
    data["morf"] = find([r"Tipo[:\s]+\*?\*?([A-Z]{1,2})\*?\*?", r"Morfologia.*?Tipo[:\s]+([A-Z]{1,2})"], analysis_text)

    outl_und_raw = find([r"Outl_UND[:\s]+\*?\*?(SI|nd)\*?\*?"], analysis_text)
    outl_fav_raw = find([r"Outl_FAV[:\s]+\*?\*?(SI|nd)\*?\*?"], analysis_text)
    data["outl_und"] = outl_und_raw.upper() if outl_und_raw != "nd" else "nd"
    data["outl_fav"] = outl_fav_raw.upper() if outl_fav_raw != "nd" else "nd"

    # Segnale morfologia (pro_fav = True se PRO FAV, False se PRO UND, None se EV/nd)
    morf_sig = find([r"Segnale empirico[:\s]+\*?\*?(PRO FAV|PRO UND|EV|nd)\*?\*?"], analysis_text)
    if "FAV" in morf_sig.upper():
        data["morf_pro_fav"] = True
    elif "UND" in morf_sig.upper():
        data["morf_pro_fav"] = False
    else:
        data["morf_pro_fav"] = None

    # Segnale modello
    mod_sig = find([r"Modello[^\n]*?(PRO FAV|PRO UND|nd)"], analysis_text)
    if "FAV" in mod_sig.upper():
        data["mod_pro_fav"] = True
    elif "UND" in mod_sig.upper():
        data["mod_pro_fav"] = False
    else:
        data["mod_pro_fav"] = None

    data["modello_segnale"] = mod_sig
    data["data"] = datetime.now().strftime("%d/%m")
    data["elo_delta"] = "nd"
    data["elo_arrow"] = "nd"
    data["elo_ok"] = "nd"

    return data

# ── Analysis ───────────────────────────────────────────────────
def analyze_screenshots(user, protocol_text, sport):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if sport == "tennis":
        ols_info = (
            f"\n\nOLS DATASET:\n{user['ols_dataset']}\nCalculate full OLS pipeline (Steps 1-7 Section 5)."
            if user["ols_dataset"]
            else "\n\nOLS DATASET: Not provided. Mark model fields as 'nd'."
        )
        html_info = (
            f"\n\nTENNISEXPLORER DATA:\n{extract_tennis_data(user['html_source'])}"
            if user["html_source"] else ""
        )
        instruction = (
            "Analizza i dati tennis applicando il protocollo LBA. "
            "Segui Section 10, concludi con VERDICT (Section 11) e RIEPILOGO RAPIDO (Section 15)."
            + ols_info + html_info
        )
    else:
        instruction = (
            "Analizza questo screenshot calcio applicando il protocollo Soccer Model. "
            "Segui Section 11 e concludi con VERDICT (Section 12)."
        )

    system_prompt = f"""You are a sports betting analyst bot.

PROTOCOL:
{protocol_text}

Rules:
1. Read ALL data carefully
2. Apply ALL protocol rules mechanically
3. Respond ONLY in Italian using exact output format from protocol
4. ALWAYS include VERDICT block and RIEPILOGO RAPIDO at the end"""

    content = []
    for img_bytes in user["images"]:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": detect_media_type(img_bytes),
                "data": base64.standard_b64encode(img_bytes).decode("utf-8")
            }
        })
    content.append({"type": "text", "text": instruction})

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text

# ── Telegram handlers ──────────────────────────────────────────
async def send_long_message(update, text):
    for chunk in split_message(text):
        await update.message.reply_text(chunk)

HELP_TEXT = (
    "👋 *Betting Analysis Bot*\n\n"
    "⚽ Scrivi *soccer*\n"
    "🎾 Scrivi *tennis*\n\n"
    "Poi manda screenshot/URL e scrivi *analizza*.\n"
    "Scrivi *reset* per ricominciare."
)

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
            f"📥 Screenshot {count} ricevuto ({sport_emoji} TENNIS).\n\n"
            "Hai il dataset OLS?\n"
            "• Mandalo come testo `237 153 250`\n"
            "• Oppure scrivi *no*"
        )
    elif user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        await update.message.reply_text(f"📥 Screenshot {count} aggiunto. Scrivi *analizza* o manda OLS.")
    else:
        if user["state"] == STATE_SPORT_SELECTED:
            user["state"] = STATE_READY
        await update.message.reply_text(
            f"📥 Screenshot {count} ricevuto ({sport_emoji} {user['sport'].upper()}).\n"
            "Scrivi *analizza* per procedere."
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
        emoji = "⚽" if text_lower == "soccer" else "🎾"
        await update.message.reply_text(
            f"{emoji} *{text_lower.upper()}* selezionato.\n\n"
            f"{'Manda screenshot AsianOdds o URL TennisExplorer.' if text_lower == 'tennis' else 'Manda screenshot e scrivi *analizza*.'}"
        )
        return

    # Reset
    if text_lower == "reset":
        reset_user(user_id)
        await update.message.reply_text("🗑 Reset.\n\n" + HELP_TEXT)
        return

    # Risultato W/L dopo analisi tennis
    if text_lower in ("w", "l") and user["sport"] == "tennis" and user["state"] == STATE_WAITING_RESULT:
        pending = user.get("pending_match")
        if not pending:
            await update.message.reply_text("❌ Nessun match in attesa di risultato.")
            return

        esito = text_lower.upper()
        await update.message.reply_text(f"📝 Salvo risultato *{esito}* su Google Sheets...")

        try:
            # Ottieni numero riga
            service = get_sheets_service()
            num = get_next_row_number(service) - 1  # -1 perché header
            pending["num"] = num
            row = build_db_row(pending, esito)
            ok = append_row_to_sheet(row)
            if ok:
                await update.message.reply_text(
                    f"✅ Riga #{num} salvata nel DB!\n\n"
                    f"*{pending.get('fav','?')} vs {pending.get('und','?')}*\n"
                    f"Esito: {esito} | Morf: {pending.get('morf','nd')} | "
                    f"Outl_UND: {pending.get('outl_und','nd')} | Outl_FAV: {pending.get('outl_fav','nd')}"
                )
            else:
                await update.message.reply_text("❌ Errore scrittura Sheets. Controlla i log.")
        except Exception as e:
            await update.message.reply_text(f"❌ Errore: {str(e)}")

        user["pending_match"] = None
        user["state"] = STATE_SPORT_SELECTED
        return

    # No al dataset OLS
    if text_lower in ("no", "skip") and user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        user["ols_dataset"] = None
        user["state"] = STATE_READY
        await update.message.reply_text("✅ Procedo senza OLS. Scrivi *analizza*.")
        return

    # Analizza
    if text_lower == "analizza":
        if not user["sport"]:
            await update.message.reply_text("⚠️ Seleziona prima lo sport.")
            return
        if not user["images"] and not user["html_source"]:
            await update.message.reply_text("❌ Nessun dato. Manda screenshot o URL.")
            return
        if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
            await update.message.reply_text("⚠️ Hai OLS? Mandalo o scrivi *no*.")
            return

        sport = user["sport"]
        emoji = "⚽" if sport == "soccer" else "🎾"
        await update.message.reply_text(f"🔍 Analizzo {emoji} {sport.upper()}...")

        try:
            protocol = load_protocol(sport)
            if not protocol:
                await update.message.reply_text("❌ Errore caricamento protocollo.")
                return

            result = analyze_screenshots(user, protocol, sport)

            # Per tennis: salva dati per DB e chiedi risultato
            if sport == "tennis":
                pending = parse_analysis_for_db(result)
                user["pending_match"] = pending
                user["state"] = STATE_WAITING_RESULT
            else:
                user["state"] = STATE_SPORT_SELECTED

            user["images"] = []
            user["html_source"] = None
            user["ols_dataset"] = None

            await send_long_message(update, result)

            if sport == "tennis":
                await update.message.reply_text(
                    "⏳ *Quando conosci il risultato scrivi:*\n\n"
                    "✅ *W* — se FAV vince\n"
                    "❌ *L* — se FAV perde\n\n"
                    "Il bot salverà automaticamente la riga nel DB."
                )

        except Exception as e:
            logger.error(f"Error: {e}")
            await update.message.reply_text(f"❌ Errore: {str(e)}")
        return

    # URL TennisExplorer
    if user["sport"] == "tennis" and "tennisexplorer.com" in text_lower:
        url = text.strip()
        if not url.startswith("http"):
            url = "https://" + url
        await update.message.reply_text("🔗 Recupero dati da TennisExplorer...")
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            user["html_source"] = r.text
            user["state"] = STATE_WAITING_OLS
            await update.message.reply_text(
                "📄 Dati caricati.\n\n"
                "Hai il dataset OLS?\n"
                "• Mandalo come testo `237 153 250`\n"
                "• Oppure scrivi *no*"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Fetch error: {str(e)}")
        return

    # Testo lungo tennis
    if user["sport"] == "tennis" and len(text) > 100:
        if "<" in text and ">" in text:
            user["html_source"] = text
            user["state"] = STATE_WAITING_OLS
            await update.message.reply_text("📄 HTML ricevuto. Hai OLS? Mandalo o scrivi *no*.")
        else:
            user["ols_dataset"] = text
            user["state"] = STATE_READY
            rows = [r.strip() for r in text.strip().split('\n') if r.strip()]
            await update.message.reply_text(f"📐 Dataset OLS — {len(rows)} righe. Scrivi *analizza*.")
        return

    # Dataset OLS corto
    if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS and len(text) > 10:
        user["ols_dataset"] = text
        user["state"] = STATE_READY
        rows = [r.strip() for r in text.strip().split('\n') if r.strip()]
        await update.message.reply_text(f"📐 Dataset OLS — {len(rows)} righe. Scrivi *analizza*.")
        return

    await update.message.reply_text(HELP_TEXT)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()

import os
import logging
import requests
import anthropic
import base64
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

# Stati utente
STATE_IDLE = "idle"
STATE_SPORT_SELECTED = "sport_selected"       # sport scelto, attendo dati
STATE_WAITING_OLS = "waiting_ols"             # ho ricevuto screenshot/html, attendo risposta su OLS
STATE_READY = "ready"                         # tutto pronto, attendo "analizza"

user_data = {}

def get_user(user_id):
    if user_id not in user_data:
        user_data[user_id] = {
            "sport": None,
            "images": [],
            "html_source": None,
            "ols_dataset": None,
            "state": STATE_IDLE,
        }
    return user_data[user_id]

def reset_user(user_id):
    user_data[user_id] = {
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
    else:
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

def is_command(text: str) -> bool:
    """Distingue comandi brevi da testo dati lungo."""
    commands = {"soccer", "tennis", "ippica", "analizza", "reset", "no", "skip"}
    return text.strip().lower() in commands

def analyze_screenshots(user: dict, protocol_text: str, sport: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if sport == "tennis":
        ols_info = ""
        if user["ols_dataset"]:
            ols_info = f"\n\nOLS DATASET PROVIDED BY USER:\n{user['ols_dataset']}\nCalculate the full OLS model pipeline (Steps 1-7 from Section 5) using this dataset."
        else:
            ols_info = "\n\nOLS DATASET: Not provided. Proceed with morfologia + outlier analysis only. Do not calculate OLS model. Mark model fields as 'nd'."

        html_info = ""
        if user["html_source"]:
            html_info = f"\n\nTENNISEXPLORER HTML SOURCE (use instead of screenshot for odds data):\n{user['html_source'][:8000]}"

        instruction = (
            "Analizza i dati tennis applicando il protocollo LBA. "
            "Segui l'output format della Section 10 e concludi SEMPRE con il VERDICT block della Section 11."
            + ols_info + html_info
        )
    else:
        instruction = (
            "Analizza questo screenshot calcio applicando il protocollo Soccer Model. "
            "Segui l'output format della Section 11 e concludi SEMPRE con il VERDICT block della Section 12."
        )

    system_prompt = f"""You are a sports betting analyst bot with a protocol document containing all rules.

PROTOCOL DOCUMENT:
{protocol_text}

Rules:
1. Read ALL provided data carefully (screenshots and/or text sources)
2. Extract all relevant data
3. Apply ALL protocol rules mechanically
4. Respond ONLY in Italian using the exact output format defined in the protocol
5. ALWAYS append the VERDICT block at the very end — mandatory, never omit it

Be precise with numbers. Never skip any section. The VERDICT is the most important part."""

    content = []
    for img_bytes in user["images"]:
        image_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = detect_media_type(img_bytes)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": image_b64}
        })

    content.append({"type": "text", "text": instruction})

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2500,
        system=system_prompt,
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
    "Poi manda screenshot (e/o HTML per tennis) e scrivi *analizza*.\n"
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
            f"Hai il dataset OLS? (quote + rank per il modello)\n\n"
            f"• Mandalo come testo nel formato: `237 153 250`\n"
            f"• Oppure scrivi *no* per procedere solo con morfologia + outlier"
        )
    elif user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        await update.message.reply_text(
            f"📥 Screenshot {count} aggiunto.\n"
            f"Quando pronto scrivi *analizza* (o manda il dataset OLS prima)."
        )
    else:
        # Soccer o stato già avanzato
        if user["state"] == STATE_SPORT_SELECTED:
            user["state"] = STATE_READY
        await update.message.reply_text(
            f"📥 Screenshot {count} ricevuto ({sport_emoji} {user['sport'].upper()}).\n\n"
            f"Manda altri screenshot oppure scrivi *analizza* per procedere."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user = get_user(user_id)
    text = update.message.text.strip()
    text_lower = text.lower()

    # — Comandi sport —
    if text_lower in ("soccer", "tennis", "ippica"):
        reset_user(user_id)
        user = get_user(user_id)
        user["sport"] = text_lower
        user["state"] = STATE_SPORT_SELECTED
        sport_emoji = "⚽" if text_lower == "soccer" else "🎾"
        await update.message.reply_text(
            f"{sport_emoji} Sport selezionato: *{text_lower.upper()}*\n\n"
            f"{'Manda screenshot AsianOdds (e/o HTML TennisExplorer).' if text_lower == 'tennis' else 'Manda uno o più screenshot e poi scrivi *analizza*.'}"
        )
        return

    # — Reset —
    if text_lower == "reset":
        reset_user(user_id)
        await update.message.reply_text("🗑 Reset completato.\n\n" + HELP_TEXT)
        return

    # — Tennis: risposta "no" al dataset OLS —
    if text_lower in ("no", "skip") and user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        user["ols_dataset"] = None
        user["state"] = STATE_READY
        await update.message.reply_text(
            "✅ Procedo senza dataset OLS.\n\n"
            "Analisi basata su morfologia + outlier.\n"
            "Scrivi *analizza* quando pronto."
        )
        return

    # — Analizza —
    if text_lower == "analizza":
        if not user["sport"]:
            await update.message.reply_text("⚠️ Seleziona prima lo sport: scrivi *soccer* o *tennis*.")
            return
        if not user["images"] and not user["html_source"]:
            await update.message.reply_text("❌ Nessun dato in coda. Manda prima uno screenshot o HTML.")
            return
        if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
            await update.message.reply_text(
                "⚠️ Hai il dataset OLS?\n\n"
                "• Mandalo come testo\n"
                "• Oppure scrivi *no* per procedere senza"
            )
            return

        sport = user["sport"]
        sport_emoji = "⚽" if sport == "soccer" else "🎾"
        await update.message.reply_text(f"🔍 Analizzo {sport_emoji} {sport.upper()}...")

        try:
            protocol = load_protocol(sport)
            if not protocol:
                await update.message.reply_text("❌ Errore nel caricamento del protocollo.")
                return

            result = analyze_screenshots(user, protocol, sport)
            # Reset immagini dopo analisi ma mantieni sport
            user["images"] = []
            user["html_source"] = None
            user["ols_dataset"] = None
            user["state"] = STATE_SPORT_SELECTED

            await send_long_message(update, result)

        except Exception as e:
            logger.error(f"Error: {e}")
            await update.message.reply_text(f"❌ Errore: {str(e)}")
        return

    # — Tennis: URL TennisExplorer —
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
                "📄 Dati TennisExplorer caricati.\n\n"
                "Hai il dataset OLS? (quote + rank)\n\n"
                "• Mandalo come testo nel formato: `237 153 250`\n"
                "• Oppure scrivi *no* per procedere solo con morfologia + outlier"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Errore nel fetch URL: {str(e)}")
        return

    # — Tennis: testo lungo = HTML TennisExplorer o dataset OLS —
    if user["sport"] == "tennis" and len(text) > 100:
        # Testo lungo → capire se è HTML o dataset OLS
        if "<" in text and ">" in text:
            # È HTML
            user["html_source"] = text
            user["state"] = STATE_WAITING_OLS
            await update.message.reply_text(
                "📄 HTML TennisExplorer ricevuto.\n\n"
                "Hai il dataset OLS? (quote + rank)\n\n"
                "• Mandalo come testo nel formato: `237 153 250`\n"
                "• Oppure scrivi *no* per procedere solo con morfologia + outlier"
            )
        else:
            # È dataset OLS (numeri)
            user["ols_dataset"] = text
            user["state"] = STATE_READY
            rows = [r.strip() for r in text.strip().split('\n') if r.strip()]
            await update.message.reply_text(
                f"📐 Dataset OLS ricevuto — {len(rows)} righe.\n\n"
                f"Scrivi *analizza* per procedere."
            )
        return

    # — Tennis: dataset OLS corto (poche righe) —
    if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS and len(text) > 10:
        user["ols_dataset"] = text
        user["state"] = STATE_READY
        rows = [r.strip() for r in text.strip().split('\n') if r.strip()]
        await update.message.reply_text(
            f"📐 Dataset OLS ricevuto — {len(rows)} righe.\n\n"
            f"Scrivi *analizza* per procedere."
        )
        return

    # — Default —
    await update.message.reply_text(HELP_TEXT)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()

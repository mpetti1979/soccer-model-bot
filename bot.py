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
PROTOCOL_URL = "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/soccer_model_protocol.html"

user_images = {}

def load_protocol():
    try:
        r = requests.get(PROTOCOL_URL, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error(f"Error loading protocol: {e}")
        return None

def detect_media_type(image_bytes: bytes) -> str:
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    elif image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    else:
        return "image/jpeg"

def split_message(text: str, max_length: int = 4000) -> list:
    """Spezza il testo in chunk da max_length caratteri senza tagliare a metà riga."""
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

def analyze_screenshots(images: list, protocol_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = f"""You are a sports betting analyst bot. You have been given a protocol document that contains all the rules for analyzing soccer betting screenshots.

PROTOCOL DOCUMENT:
{protocol_text}

Your job:
1. Read ALL screenshots provided carefully (AsianOdds and/or TradeOnSport)
2. Extract all relevant data visible in each image
3. Combine data from all sources for a complete analysis
4. Apply ALL rules from the protocol mechanically
5. Respond ONLY in Italian using the exact output format defined in Section 11 of the protocol
6. ALWAYS append the VERDICT block from Section 12 at the very end — this is mandatory

Be precise with numbers. Calculate outlier gaps exactly. Do not skip any section of the output format.
The VERDICT block is the most important part — never omit it."""

    content = []
    for i, img_bytes in enumerate(images):
        image_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = detect_media_type(img_bytes)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_b64,
            }
        })

    content.append({
        "type": "text",
        "text": f"Analizza {'questi ' + str(len(images)) + ' screenshot' if len(images) > 1 else 'questo screenshot'} applicando il protocollo. Integra i dati di tutte le fonti. Rispondi in italiano con il formato Section 11 e concludi SEMPRE con il VERDICT block della Section 12."
    })

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2500,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text

async def send_long_message(update: Update, text: str):
    """Invia un messaggio lungo spezzandolo se necessario."""
    chunks = split_message(text)
    for chunk in chunks:
        await update.message.reply_text(chunk)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_images:
        user_images[user_id] = []

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    user_images[user_id].append(bytes(image_bytes))

    count = len(user_images[user_id])
    if count == 1:
        await update.message.reply_text("📥 Screenshot 1 ricevuto.\n\nManda altri screenshot oppure scrivi *analizza* per procedere.")
    else:
        await update.message.reply_text(f"📥 Screenshot {count} ricevuto.\n\nScrivi *analizza* per procedere o manda altri screenshot.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip().lower()

    if text == "analizza":
        if user_id not in user_images or len(user_images[user_id]) == 0:
            await update.message.reply_text("❌ Nessuno screenshot in coda. Mandami prima uno screenshot di AsianOdds o TOS.")
            return

        count = len(user_images[user_id])
        await update.message.reply_text(f"🔍 Analizzo {count} screenshot con il protocollo...")

        try:
            protocol = load_protocol()
            if not protocol:
                await update.message.reply_text("❌ Errore nel caricamento del protocollo. Riprova tra poco.")
                return

            images = user_images[user_id].copy()
            user_images[user_id] = []

            result = analyze_screenshots(images, protocol)
            await send_long_message(update, result)

        except Exception as e:
            logger.error(f"Error: {e}")
            await update.message.reply_text(f"❌ Errore durante l'analisi: {str(e)}")

    elif text == "reset":
        user_images[user_id] = []
        await update.message.reply_text("🗑 Buffer svuotato. Puoi mandare nuovi screenshot.")

    else:
        await update.message.reply_text(
            "👋 *Soccer Model Bot*\n\n"
            "📸 Manda uno o più screenshot di AsianOdds e/o TOS\n"
            "▶️ Scrivi *analizza* per avviare l'analisi\n"
            "🗑 Scrivi *reset* per svuotare il buffer"
        )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()

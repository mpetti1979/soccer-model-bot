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

def load_protocol():
    try:
        r = requests.get(PROTOCOL_URL, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error(f"Error loading protocol: {e}")
        return None

def analyze_screenshot(image_bytes: bytes, protocol_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    system_prompt = f"""You are a sports betting analyst bot. You have been given a protocol document that contains all the rules for analyzing soccer betting screenshots.

PROTOCOL DOCUMENT:
{protocol_text}

Your job:
1. Read the screenshot carefully (AsianOdds or TradeOnSport)
2. Extract all relevant data visible in the image
3. Apply ALL rules from the protocol mechanically
4. Respond ONLY in Italian using the exact output format defined in Section 11 of the protocol

Be precise with numbers. Calculate outlier gaps exactly. Do not skip any section of the output format."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analizza questo screenshot applicando il protocollo. Rispondi in italiano con il formato esatto definito nella Section 11."
                    }
                ],
            }
        ],
    )
    return message.content[0].text

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📥 Screenshot ricevuto. Carico il protocollo e analizzo...")

    try:
        # Download photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        # Load protocol
        protocol = load_protocol()
        if not protocol:
            await update.message.reply_text("❌ Errore nel caricamento del protocollo. Riprova tra poco.")
            return

        # Analyze
        result = analyze_screenshot(bytes(image_bytes), protocol)
        await update.message.reply_text(result)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Errore durante l'analisi: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Soccer Model Bot attivo.\n\nMandami uno screenshot di AsianOdds o TradeOnSport e analizzo automaticamente."
    )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()

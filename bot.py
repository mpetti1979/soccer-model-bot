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

SHEET_TENNIS_ID = "1YY4qeOGfDFChLiHsoEEewFWPDw3edI3R"
SHEET_IPPICA_ID = "10zP1OA8CZNEOIR4drFHY9-ANgKASJk0qFcOVoNqiH3w"

PROTOCOLS = {
    "soccer": "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/soccer_model_protocol.html",
    "tennis": "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/tennis_lba_protocol.html",
    "ippica": "https://raw.githubusercontent.com/mpetti1979/soccer-protocols/refs/heads/main/ippica_protocol.html",
}

# ── Stati ──────────────────────────────────────────────────────
STATE_IDLE = "idle"
STATE_SPORT_SELECTED = "sport_selected"
STATE_WAITING_OLS = "waiting_ols"
STATE_READY = "ready"
STATE_WAITING_RESULT = "waiting_result"
# Ippica specifici
STATE_IPPICA_WAITING_PDF = "ippica_waiting_pdf"
STATE_IPPICA_PDF_RECEIVED = "ippica_pdf_received"
STATE_IPPICA_WAITING_BET365 = "ippica_waiting_bet365"
STATE_IPPICA_WAITING_RESULTS = "ippica_waiting_results"

user_data = {}

# ── Google Sheets ──────────────────────────────────────────────
def get_sheets_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)

def append_to_sheet(sheet_id, sheet_name, row_data):
    try:
        service = get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!A:AG",
            valueInputOption="USER_ENTERED",
            body={"values": [row_data]}
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return False

# ── User data ──────────────────────────────────────────────────
def get_user(user_id):
    if user_id not in user_data:
        user_data[user_id] = {
            "sport": None, "images": [], "html_source": None,
            "ols_dataset": None, "state": STATE_IDLE,
            "pending_match": None,
            # Ippica
            "ippica_pdf_partenti": None,
            "ippica_pdf_quote": None,
            "ippica_corse_operative": [],
            "ippica_screenshots": [],
            "ippica_segnali": [],
            "ippica_ippodromo": None,
            "ippica_data": None,
            "ippica_sessione": None,
        }
    return user_data[user_id]

def reset_user(user_id):
    user_data[user_id] = {
        "sport": None, "images": [], "html_source": None,
        "ols_dataset": None, "state": STATE_IDLE,
        "pending_match": None,
        "ippica_pdf_partenti": None,
        "ippica_pdf_quote": None,
        "ippica_corse_operative": [],
        "ippica_screenshots": [],
        "ippica_segnali": [],
        "ippica_ippodromo": None,
        "ippica_data": None,
        "ippica_sessione": None,
    }

# ── Utilities ──────────────────────────────────────────────────
def load_protocol(sport):
    url = PROTOCOLS.get(sport)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error(f"Protocol error: {e}")
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
    return "=== MATCH INFO ===\n" + header + "\n\n=== BETTING ODDS ===\n" + odds_section

# ── Claude API calls ───────────────────────────────────────────
def call_claude(system_prompt, user_content, max_tokens=4000):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return message.content[0].text

def analyze_tennis(user, protocol_text):
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
    system = f"You are a sports betting analyst bot.\n\nPROTOCOL:\n{protocol_text}\n\nRespond ONLY in Italian using exact output format from protocol. Always include VERDICT and RIEPILOGO RAPIDO."
    content = []
    for img_bytes in user["images"]:
        content.append({"type": "image", "source": {"type": "base64", "media_type": detect_media_type(img_bytes), "data": base64.standard_b64encode(img_bytes).decode("utf-8")}})
    content.append({"type": "text", "text": instruction})
    return call_claude(system, content)

def analyze_soccer(user, protocol_text):
    instruction = "Analizza questo screenshot calcio applicando il protocollo Soccer Model. Segui Section 11 e concludi con VERDICT (Section 12)."
    system = f"You are a sports betting analyst bot.\n\nPROTOCOL:\n{protocol_text}\n\nRespond ONLY in Italian. Always include VERDICT block."
    content = []
    for img_bytes in user["images"]:
        content.append({"type": "image", "source": {"type": "base64", "media_type": detect_media_type(img_bytes), "data": base64.standard_b64encode(img_bytes).decode("utf-8")}})
    content.append({"type": "text", "text": instruction})
    return call_claude(system, content)

def ippica_fase1(pdf_partenti_b64, pdf_quote_b64, protocol_text):
    """Fase 1: analizza PDF e restituisce palinsesto filtrato."""
    system = f"""Sei il sistema ippico di analisi. Hai il protocollo completo.

PROTOCOLLO:
{protocol_text}

Il tuo compito ORA è solo la FASE 1:
1. Leggi il PDF quote Snai: estrai per ogni corsa il numero di partenti e il montepremi
2. Filtra le corse operative (partenti ≥ 8 AND montepremi ≥ 3080)
3. Identifica flag (CNAZ, GENT, CONTEST se >12 partenti)
4. Leggi il PDF partenti per identificare: ippodromo, data, disciplina (TR/GL), codice sessione
5. Output ESATTAMENTE in questo formato JSON:

{{
  "ippodromo": "Napoli",
  "data": "07/04/2026",
  "disciplina": "TR",
  "sessione": "G36",
  "corse_operative": [
    {{"id": "C2", "ora": "15:10", "mp": 6600, "partenti": 14, "flag": "GENT", "stato": "OPERATIVA"}},
    {{"id": "C4", "ora": "16:05", "mp": 7700, "partenti": 8, "flag": "", "stato": "OPERATIVA"}}
  ],
  "corse_skip": [
    {{"id": "C1", "ora": "14:45", "mp": 6600, "partenti": 16, "motivo": "CONTEST >12"}}
  ]
}}

Rispondi SOLO con il JSON, nessun altro testo."""

    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_partenti_b64}},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_quote_b64}},
        {"type": "text", "text": "Esegui Fase 1. Rispondi SOLO con il JSON."}
    ]
    return call_claude(system, content, max_tokens=2000)

def ippica_fase2_3(pdf_partenti_b64, pdf_quote_b64, corsa_id, screenshot_bytes_list, protocol_text, data_oggi):
    """Fase 2+3: calcola score e trova segnale per una corsa."""
    images_content = []
    for img_bytes in screenshot_bytes_list:
        images_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": detect_media_type(img_bytes), "data": base64.standard_b64encode(img_bytes).decode("utf-8")}
        })

    system = f"""Sei il sistema ippico di analisi. Data odierna: {data_oggi}

PROTOCOLLO COMPLETO:
{protocol_text}

Il tuo compito è FASE 2 + FASE 3 per la corsa {corsa_id}:

FASE 2: Calcola score per TUTTI i cavalli della corsa {corsa_id}
- Leggi i PDF per trovare: data ultima corsa, QF_t1, Mp_t1, C* V* P* Vincite* (6 mesi header)
- Calcola Pt.GG, Log Indice, Pt.Q per ogni cavallo
- Lo screenshot Bet365 fornisce QP e QV per calcolare XQ e Pt.XQ
- Score = Pt.GG + Log Indice + Pt.Q + Pt.XQ
- Ordina per score decrescente

FASE 3: Applica filtro Bet365 sui top 3
- QP range 1.70-3.50 AND QP < QV → candidato operativo
- Prendi il primo che supera il filtro

Output ESATTAMENTE in questo formato JSON:
{{
  "corsa_id": "C2",
  "ora": "15:10",
  "mp_oggi": 6600,
  "partenti": 14,
  "flag": "GENT",
  "cavalli": [
    {{"num": 3, "nome": "GENNY GIO", "pt_gg": 3, "log": 1, "pt_q": 1, "xq_grezzo": 2.1, "pt_xq": 1, "score": 6, "qa_snai": 5.5, "qp_bet365": 2.0, "qv_bet365": 5.5, "qf_t1": 6.0, "mp_t1": 5060, "giorni_ultima": 6, "ultima_corsa_estera": false}},
    {{"num": 4, "nome": "GELSOMORO OP", "pt_gg": 2, "log": 0, "pt_q": 0, "xq_grezzo": 1.8, "pt_xq": 1, "score": 3, "qa_snai": 3.0, "qp_bet365": 1.85, "qv_bet365": 3.0, "qf_t1": 11.0, "mp_t1": 5060, "giorni_ultima": 14, "ultima_corsa_estera": false}}
  ],
  "segnale": {{
    "trovato": true,
    "num": 3,
    "nome": "GENNY GIO",
    "score": 6,
    "qp": 2.0,
    "qv": 5.5,
    "fascia_q": "Q1",
    "xq_grezzo": 2.1,
    "fascia_xq": "XQ+1",
    "pt_gg": 3,
    "log": 1,
    "pt_q": 1,
    "pt_xq": 1,
    "qa_snai": 5.5,
    "qf_t1": 6.0,
    "mp_t1": 5060,
    "fascia_qv_t1": "P",
    "fascia_m_t1": "M2",
    "fascia_qv_oggi": "P",
    "fascia_m_oggi": "M2",
    "mov": "E=",
    "stake": 0.25
  }},
  "no_bet_motivo": null
}}

Se nessun segnale: segnale.trovato=false, no_bet_motivo="motivo"
Rispondi SOLO con il JSON."""

    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_partenti_b64}},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_quote_b64}},
    ] + images_content + [
        {"type": "text", "text": f"Esegui Fase 2 e Fase 3 per corsa {corsa_id}. Rispondi SOLO con il JSON."}
    ]
    return call_claude(system, content, max_tokens=3000)

def ippica_prpa(segnali, risultati_text, protocol_text):
    """Genera PRPA dai segnali e risultati."""
    system = f"""Sei il sistema ippico. Genera la PRPA completa.

PROTOCOLLO:
{protocol_text}

SEGNALI DELLA SESSIONE:
{json.dumps(segnali, ensure_ascii=False, indent=2)}

RISULTATI FORNITI:
{risultati_text}

Genera la PRPA completa in italiano includendo:
1. Tabella esiti per ogni segnale (Cavallo | Esito P/NP | Posizione | QV Vincitore | P/L)
2. Statistiche totali
3. Analisi per fascia Score, Mov, Fascia Q
4. Note critiche

Poi genera il JSON per DB in questo formato:
RIGHE_DB_JSON:
[
  {{"data": "07/04/2026", "sessione": "G36", "ippodromo": "Napoli", "disciplina": "TR", "corsa": "C2", "cavallo": "3 GENNY GIO", "qp": 2.0, "qv": 5.5, "rapporto": 2.75, "fascia_q": "Q1", "fascia_xq": "XQ+1", "pt_gg": 3, "log": 1, "pt_q": 1, "pt_xq": 1, "score": 6, "mp": 6600, "tipo_corsa": "C.Gent.", "partenti": 14, "qf_t1": 6.0, "mp_t1": 5060, "fascia_qv_t1": "P", "fascia_m_t1": "M2", "fascia_qv_oggi": "P", "fascia_m_oggi": "M2", "mov": "E=", "stake": 0.25, "esito": "P", "posizione": 1, "qv_vincitore": 5.5, "fascia_q_vincitore": "Q1", "pl": 0.25, "piazzato": 1}}
]"""
    return call_claude(system, [{"type": "text", "text": "Genera PRPA e JSON righe DB."}], max_tokens=4000)

# ── Parse helpers ──────────────────────────────────────────────
def parse_json_response(text):
    import re
    text = text.strip()
    # Try to find JSON block
    match = re.search(r'\{.*\}|\[.*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except:
            pass
    try:
        return json.loads(text)
    except:
        return None

def parse_tennis_for_db(analysis_text):
    import re
    data = {}
    def find(patterns, text, default="nd"):
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE | re.DOTALL)
            if m:
                try: return m.group(1).strip()
                except: return m.group(0).strip()
        return default
    def find_plain(pattern, text, default="nd"):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(0).strip() if m else default

    data["torneo"] = find([r"🎾\s*(.+?)\s*—", r"Torneo[:\s]+(.+?)[\n|]"], analysis_text)
    data["sup"] = find_plain(r"\b(Clay|Hard|Grass|Erba|Terra battuta)\b", analysis_text)
    data["fav"] = find([r"👤\s*\*?FAV\*?[:\s]+([A-Za-z][A-Za-z\s\.]+?)\s*@"], analysis_text)
    data["und"] = find([r"👤\s*\*?UND\*?[:\s]+([A-Za-z][A-Za-z\s\.]+?)\s*@"], analysis_text)
    data["q_fav"] = find([r"FAV[^\n]+@\s*([\d\.]+)"], analysis_text)
    data["q_und"] = find([r"UND[^\n]+@\s*([\d\.]+)"], analysis_text)
    data["r2"] = find([r"R²[:\s]+([\d\.]+)", r"R2[:\s]+([\d\.]+)"], analysis_text)
    data["delta_pct"] = find([r"Δ%[:\s]+([-\d\.]+)%?"], analysis_text)
    data["fascia"] = find([r"Fascia[:\s]+(forte|mod|debole|sub|non\s*op)"], analysis_text)
    data["morf"] = find([r"Tipo[:\s]+\*?\*?([A-Z]{1,2})\b"], analysis_text)
    outl_und = find([r"Outl_UND[:\s|]+\*?\*?(SI|nd)\b"], analysis_text)
    outl_fav = find([r"Outl_FAV[:\s|]+\*?\*?(SI|nd)\b"], analysis_text)
    data["outl_und"] = "SI" if str(outl_und).upper() == "SI" else "nd"
    data["outl_fav"] = "SI" if str(outl_fav).upper() == "SI" else "nd"
    morf_sig = find([r"Segnale empirico[:\s]+\*?\*?(PRO FAV|PRO UND|EV|nd)\b"], analysis_text)
    data["morf_pro_fav"] = True if "FAV" in str(morf_sig).upper() and "UND" not in str(morf_sig).upper() else (False if "UND" in str(morf_sig).upper() else None)
    mod_sig = find([r"Modello[^\n]{0,30}?(PRO FAV|PRO UND|nd)\b"], analysis_text)
    data["mod_pro_fav"] = True if "FAV" in str(mod_sig).upper() and "UND" not in str(mod_sig).upper() else (False if "UND" in str(mod_sig).upper() else None)
    data["data"] = datetime.now().strftime("%d/%m")
    data["elo_delta"] = "nd"
    data["elo_arrow"] = "nd"
    data["elo_ok"] = "nd"
    return data

def build_tennis_db_row(pending, esito):
    esito_w = esito.upper() == "W"
    def calc_ok(pro_fav, w):
        if pro_fav is None: return "nd"
        return "SI" if (w == pro_fav) else "NO"
    row = [
        pending.get("data", ""), "", pending.get("torneo", ""), pending.get("sup", ""),
        pending.get("fav", ""), pending.get("und", ""), pending.get("q_fav", ""), pending.get("q_und", ""),
        pending.get("r2", "nd"), pending.get("delta_pct", "nd"), pending.get("fascia", "nd"),
        pending.get("morf", "nd"), pending.get("outl_und", "nd"),
        pending.get("elo_delta", "nd"), pending.get("elo_arrow", "nd"),
        esito.upper(),
        calc_ok(pending.get("mod_pro_fav"), esito_w),
        calc_ok(pending.get("morf_pro_fav"), esito_w),
        calc_ok(True, esito_w) if pending.get("outl_und") == "SI" else "nd",
        "nd"
    ]
    return row

def build_ippica_db_rows(prpa_text):
    """Estrae le righe DB dal testo PRPA."""
    import re
    rows = []
    match = re.search(r'RIGHE_DB_JSON:\s*(\[.*?\])', prpa_text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            for r in data:
                row = [
                    r.get("data",""), r.get("sessione",""), r.get("ippodromo",""),
                    r.get("disciplina",""), r.get("corsa",""), r.get("cavallo",""),
                    r.get("qp",""), r.get("qv",""), r.get("rapporto",""),
                    r.get("fascia_q",""), r.get("fascia_xq",""),
                    r.get("pt_gg",""), r.get("log",""), r.get("pt_q",""), r.get("pt_xq",""),
                    r.get("score",""), r.get("mp",""), r.get("tipo_corsa",""),
                    r.get("partenti",""), r.get("qf_t1",""), r.get("mp_t1",""),
                    r.get("fascia_qv_t1",""), r.get("fascia_m_t1",""),
                    r.get("fascia_qv_oggi",""), r.get("fascia_m_oggi",""),
                    r.get("mov",""), r.get("stake",""),
                    r.get("esito",""), r.get("posizione",""),
                    r.get("qv_vincitore",""), r.get("fascia_q_vincitore",""),
                    r.get("pl",""), r.get("piazzato","")
                ]
                rows.append(row)
        except Exception as e:
            logger.error(f"PRPA JSON parse error: {e}")
    return rows

# ── Telegram handlers ──────────────────────────────────────────
async def send_long_message(update, text):
    for chunk in split_message(text):
        await update.message.reply_text(chunk)

HELP_TEXT = (
    "👋 *Betting Analysis Bot*\n\n"
    "⚽ Scrivi *soccer* — analisi calcio\n"
    "🎾 Scrivi *tennis* — analisi tennis LBA\n"
    "🏇 Scrivi *ippica* — sistema ippico trotto\n\n"
    "Scrivi *reset* per ricominciare."
)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce PDF allegati (per ippica)."""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if user["sport"] != "ippica":
        await update.message.reply_text("⚠️ Scrivi *ippica* prima di mandare PDF.")
        return

    doc = update.message.document
    if not doc.mime_type == "application/pdf":
        await update.message.reply_text("⚠️ Manda un file PDF.")
        return

    file = await context.bot.get_file(doc.file_id)
    pdf_bytes = await file.download_as_bytearray()
    pdf_b64 = base64.standard_b64encode(bytes(pdf_bytes)).decode("utf-8")

    filename = doc.file_name.upper()

    # Determina se è PDF quote o PDF partenti
    if "QUO" in filename or "QUOTE" in filename:
        user["ippica_pdf_quote"] = pdf_b64
        await update.message.reply_text("📄 PDF quote Snai ricevuto.")
    else:
        user["ippica_pdf_partenti"] = pdf_b64
        await update.message.reply_text("📄 PDF partenti Snai ricevuto.")

    # Se li ho entrambi → esegui Fase 1
    if user["ippica_pdf_partenti"] and user["ippica_pdf_quote"]:
        await update.message.reply_text("🔍 Ho entrambi i PDF. Eseguo Fase 1 — filtro corse operative...")
        try:
            protocol = load_protocol("ippica")
            result_text = ippica_fase1(
                user["ippica_pdf_partenti"],
                user["ippica_pdf_quote"],
                protocol
            )
            result = parse_json_response(result_text)

            if not result:
                await update.message.reply_text(f"❌ Errore parsing Fase 1. Risposta:\n{result_text[:500]}")
                return

            user["ippica_ippodromo"] = result.get("ippodromo", "?")
            user["ippica_data"] = result.get("data", datetime.now().strftime("%d/%m/%Y"))
            user["ippica_sessione"] = result.get("sessione", "?")
            user["ippica_corse_operative"] = result.get("corse_operative", [])

            # Componi messaggio palinsesto
            corse_op = user["ippica_corse_operative"]
            corse_skip = result.get("corse_skip", [])

            msg = f"🏇 *{user['ippica_ippodromo']} — {result.get('disciplina','TR')} — {user['ippica_data']}*\n"
            msg += f"Sessione: {user['ippica_sessione']}\n\n"
            msg += "✅ *CORSE OPERATIVE:*\n"
            for c in corse_op:
                flag_str = f" [{c.get('flag','')}]" if c.get('flag') else ""
                msg += f"• {c['id']} ore {c['ora']} | Mp€{c['mp']:,} | {c['partenti']} part.{flag_str}\n"

            if corse_skip:
                msg += "\n⛔ *SKIP:*\n"
                for c in corse_skip:
                    msg += f"• {c['id']} — {c.get('motivo','')}\n"

            if corse_op:
                ids = [c["id"] for c in corse_op]
                msg += f"\n📸 Mandami gli screenshot Bet365 in ordine:\n"
                for cid in ids:
                    msg += f"  {cid}\n"
                msg += "\nMandali tutti insieme in sequenza."
                user["state"] = STATE_IPPICA_WAITING_BET365
            else:
                msg += "\n❌ Nessuna corsa operativa oggi."
                user["state"] = STATE_SPORT_SELECTED

            await send_long_message(update, msg)

        except Exception as e:
            logger.error(f"Fase 1 error: {e}")
            await update.message.reply_text(f"❌ Errore Fase 1: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["sport"]:
        await update.message.reply_text("⚠️ Seleziona prima lo sport.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    # Ippica: accumula screenshot Bet365
    if user["sport"] == "ippica":
        user["ippica_screenshots"].append(bytes(image_bytes))
        count = len(user["ippica_screenshots"])
        total = len(user["ippica_corse_operative"])
        await update.message.reply_text(
            f"📸 Screenshot {count}/{total} ricevuto.\n"
            f"{'Scrivi *analizza* per procedere.' if count >= total else 'Continua a mandare gli screenshot.'}"
        )
        return

    # Tennis/Soccer
    user["images"].append(bytes(image_bytes))
    count = len(user["images"])
    sport_emoji = "⚽" if user["sport"] == "soccer" else "🎾"

    if user["sport"] == "tennis" and user["state"] == STATE_SPORT_SELECTED:
        user["state"] = STATE_WAITING_OLS
        await update.message.reply_text(
            f"📥 Screenshot {count} ricevuto.\n\nHai il dataset OLS?\n"
            "• Mandalo come testo `237 153 250`\n• Oppure scrivi *no*"
        )
    elif user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        await update.message.reply_text(f"📥 Screenshot {count} aggiunto. Scrivi *analizza* o manda OLS.")
    else:
        if user["state"] == STATE_SPORT_SELECTED:
            user["state"] = STATE_READY
        await update.message.reply_text(f"📥 Screenshot {count} ({sport_emoji}). Scrivi *analizza*.")

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
        if text_lower == "soccer":
            await update.message.reply_text("⚽ *SOCCER* selezionato.\nManda screenshot e scrivi *analizza*.")
        elif text_lower == "tennis":
            await update.message.reply_text("🎾 *TENNIS* selezionato.\nManda screenshot AsianOdds o URL TennisExplorer.")
        else:
            await update.message.reply_text(
                "🏇 *IPPICA* selezionato.\n\n"
                "Manda i due PDF Snai:\n"
                "• PDF partenti (nome file senza QUO)\n"
                "• PDF quote (nome file con QUO)\n\n"
                "Puoi mandarli in qualsiasi ordine."
            )
        return

    # Reset
    if text_lower == "reset":
        reset_user(user_id)
        await update.message.reply_text("🗑 Reset.\n\n" + HELP_TEXT)
        return

    # Tennis: no OLS
    if text_lower in ("no", "skip") and user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        user["ols_dataset"] = None
        user["state"] = STATE_READY
        await update.message.reply_text("✅ Procedo senza OLS. Scrivi *analizza*.")
        return

    # Tennis: W/L result
    if text_lower in ("w", "l") and user["sport"] == "tennis" and user["state"] == STATE_WAITING_RESULT:
        pending = user.get("pending_match")
        if not pending:
            await update.message.reply_text("❌ Nessun match in attesa.")
            return
        esito = text_lower.upper()
        await update.message.reply_text(f"📝 Salvo {esito} su Google Sheets...")
        try:
            row = build_tennis_db_row(pending, esito)
            ok = append_to_sheet(SHEET_TENNIS_ID, "DB", row)
            if ok:
                await update.message.reply_text(f"✅ Salvato! {pending.get('fav','?')} vs {pending.get('und','?')} — {esito}")
            else:
                await update.message.reply_text("❌ Errore scrittura Sheets.")
        except Exception as e:
            await update.message.reply_text(f"❌ Errore: {str(e)}")
        user["pending_match"] = None
        user["state"] = STATE_SPORT_SELECTED
        return

    # Ippica: risultati PRPA (link o testo)
    if user["sport"] == "ippica" and user["state"] == STATE_IPPICA_WAITING_RESULTS:
        await update.message.reply_text("🔍 Genero PRPA...")
        try:
            protocol = load_protocol("ippica")
            prpa_text = ippica_prpa(user["ippica_segnali"], text, protocol)
            await send_long_message(update, prpa_text)
            # Salva righe DB
            rows = build_ippica_db_rows(prpa_text)
            if rows:
                saved = 0
                for row in rows:
                    if append_to_sheet(SHEET_IPPICA_ID, "DB_SEGNALI", row):
                        saved += 1
                await update.message.reply_text(f"✅ {saved}/{len(rows)} righe salvate nel DB ippica.")
            user["state"] = STATE_SPORT_SELECTED
        except Exception as e:
            await update.message.reply_text(f"❌ Errore PRPA: {str(e)}")
        return

    # Analizza
    if text_lower == "analizza":
        if not user["sport"]:
            await update.message.reply_text("⚠️ Seleziona prima lo sport.")
            return

        # Ippica analisi
        if user["sport"] == "ippica":
            corse_op = user["ippica_corse_operative"]
            screenshots = user["ippica_screenshots"]
            if not screenshots:
                await update.message.reply_text("❌ Nessuno screenshot. Manda gli screenshot Bet365.")
                return
            if len(screenshots) < len(corse_op):
                await update.message.reply_text(
                    f"⚠️ Ho {len(screenshots)} screenshot ma {len(corse_op)} corse operative.\n"
                    f"Continua a mandare screenshot o scrivi *analizza* per procedere con quelli ricevuti."
                )
                # Procedi comunque con quelli disponibili
            await update.message.reply_text(f"🔍 Analizzo {min(len(screenshots), len(corse_op))} corse...")
            try:
                protocol = load_protocol("ippica")
                segnali = []
                for i, corsa in enumerate(corse_op[:len(screenshots)]):
                    await update.message.reply_text(f"📊 Elaboro {corsa['id']}...")
                    result_text = ippica_fase2_3(
                        user["ippica_pdf_partenti"],
                        user["ippica_pdf_quote"],
                        corsa["id"],
                        [screenshots[i]],
                        protocol,
                        user["ippica_data"]
                    )
                    result = parse_json_response(result_text)
                    if result:
                        segnali.append(result)
                        s = result.get("segnale", {})
                        if s.get("trovato"):
                            msg = (f"✅ *{corsa['id']}* — {s['num']} {s['nome']}\n"
                                   f"Score: {s['score']} | QP: {s['qp']} | QV: {s['qv']}\n"
                                   f"Fascia Q: {s['fascia_q']} | XQ: {s['fascia_xq']} | Mov: {s['mov']}\n"
                                   f"Stake: {s['stake']}u")
                        else:
                            msg = f"❌ *{corsa['id']}* — NO BET: {result.get('no_bet_motivo','?')}"
                        await update.message.reply_text(msg)
                    else:
                        await update.message.reply_text(f"⚠️ {corsa['id']}: errore parsing risposta")

                user["ippica_segnali"] = segnali

                # Riepilogo
                riepilogo = f"🏇 *RIEPILOGO — {user['ippica_ippodromo']} {user['ippica_data']}*\n\n"
                totale_stake = 0
                for r in segnali:
                    s = r.get("segnale", {})
                    if s.get("trovato"):
                        riepilogo += f"• {r['corsa_id']} | {s['num']} {s['nome']} | Score {s['score']} | QP {s['qp']} | Mov {s['mov']} | {s['stake']}u\n"
                        totale_stake += s.get("stake", 0)
                    else:
                        riepilogo += f"• {r['corsa_id']} | NO BET\n"
                riepilogo += f"\nEsposizione totale: {totale_stake}u"
                await send_long_message(update, riepilogo)
                await update.message.reply_text(
                    "⏳ *Manda i risultati per la PRPA.*\n\n"
                    "Puoi mandare:\n"
                    "• Link risultati Snai\n"
                    "• Testo con posizioni (es. 'C2: 3° posto, C4: RP')"
                )
                user["state"] = STATE_IPPICA_WAITING_RESULTS
                user["ippica_screenshots"] = []

            except Exception as e:
                logger.error(f"Ippica analisi error: {e}")
                await update.message.reply_text(f"❌ Errore: {str(e)}")
            return

        # Tennis analisi
        if user["sport"] == "tennis":
            if not user["images"] and not user["html_source"]:
                await update.message.reply_text("❌ Nessun dato. Manda screenshot o URL.")
                return
            if user["state"] == STATE_WAITING_OLS:
                await update.message.reply_text("⚠️ Hai OLS? Mandalo o scrivi *no*.")
                return
            await update.message.reply_text("🔍 Analizzo 🎾 TENNIS...")
            try:
                protocol = load_protocol("tennis")
                result = analyze_tennis(user, protocol)
                pending = parse_tennis_for_db(result)
                user["pending_match"] = pending
                user["state"] = STATE_WAITING_RESULT
                user["images"] = []
                user["html_source"] = None
                user["ols_dataset"] = None
                await send_long_message(update, result)
                await update.message.reply_text("⏳ Risultato? Scrivi *W* (FAV vince) o *L* (FAV perde)")
            except Exception as e:
                logger.error(f"Tennis error: {e}")
                await update.message.reply_text(f"❌ Errore: {str(e)}")
            return

        # Soccer analisi
        if user["sport"] == "soccer":
            if not user["images"]:
                await update.message.reply_text("❌ Nessuno screenshot.")
                return
            await update.message.reply_text("🔍 Analizzo ⚽ SOCCER...")
            try:
                protocol = load_protocol("soccer")
                result = analyze_soccer(user, protocol)
                user["images"] = []
                user["state"] = STATE_SPORT_SELECTED
                await send_long_message(update, result)
            except Exception as e:
                logger.error(f"Soccer error: {e}")
                await update.message.reply_text(f"❌ Errore: {str(e)}")
            return

    # URL TennisExplorer
    if user["sport"] == "tennis" and "tennisexplorer.com" in text_lower:
        url = text.strip()
        if not url.startswith("http"):
            url = "https://" + url
        await update.message.reply_text("🔗 Recupero dati TennisExplorer...")
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            user["html_source"] = r.text
            user["state"] = STATE_WAITING_OLS
            await update.message.reply_text("📄 Dati caricati.\n\nHai OLS?\n• Mandalo come testo\n• Oppure scrivi *no*")
        except Exception as e:
            await update.message.reply_text(f"❌ Fetch error: {str(e)}")
        return

    # Tennis testo lungo
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

    # Tennis dataset OLS corto
    if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS and len(text) > 10:
        user["ols_dataset"] = text
        user["state"] = STATE_READY
        rows = [r.strip() for r in text.strip().split('\n') if r.strip()]
        await update.message.reply_text(f"📐 Dataset OLS — {len(rows)} righe. Scrivi *analizza*.")
        return

    await update.message.reply_text(HELP_TEXT)

def main():
    from telegram.ext import MessageHandler, filters
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot started with soccer/tennis/ippica support")
    app.run_polling()

if __name__ == "__main__":
    main()

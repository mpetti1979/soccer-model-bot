import os
import logging
import requests
import anthropic
import base64
import re
import math
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
STATE_SPORT_SELECTED = "sport_selected"
STATE_WAITING_OLS = "waiting_ols"
STATE_READY = "ready"

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

def is_ols_format(text: str) -> bool:
    """Rileva se il testo è nel nuovo formato OLS con fav:/und:/avversari passati."""
    text_lower = text.lower()
    return "fav:" in text_lower and "und:" in text_lower

def parse_ols_dataset(text: str) -> dict:
    """
    Parsa il nuovo formato OLS:
    fav: 285
    und: 310
    avversari passati:
    237 153 259
    198 210 180
    """
    result = {
        "fav_rank": None,
        "und_rank": None,
        "rows": []
    }
    lines = text.strip().split('\n')
    in_rows = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        line_lower = line.lower()
        # Parsa fav rank
        if line_lower.startswith("fav:"):
            val = line_lower.replace("fav:", "").strip()
            try:
                result["fav_rank"] = int(val)
            except:
                pass
            continue
        # Parsa und rank
        if line_lower.startswith("und:"):
            val = line_lower.replace("und:", "").strip()
            try:
                result["und_rank"] = int(val)
            except:
                pass
            continue
        # Inizio sezione avversari
        if "avversari" in line_lower:
            in_rows = True
            continue
        # Righe numeriche storiche
        if in_rows:
            parts = line.split()
            if len(parts) == 3:
                try:
                    q_fav = int(parts[0]) / 100
                    q_avv = int(parts[1]) / 100
                    rank_avv = int(parts[2])
                    result["rows"].append({
                        "q_fav": q_fav,
                        "q_avv": q_avv,
                        "rank_avv": rank_avv
                    })
                except:
                    pass
    return result

def run_ols(parsed_ols: dict) -> dict:
    """
    Regressione OLS: ln(p_fav) = a + b * ln(rank_avv)
    Ritorna fair value FAV per il match attuale.
    """
    rows = parsed_ols["rows"]
    fav_rank = parsed_ols["fav_rank"]
    und_rank = parsed_ols["und_rank"]

    if len(rows) < 3:
        return {"error": "Dati insufficienti (minimo 3 righe storiche)"}

    # No-vig conversion e trasformazione log
    xs = []
    ys = []
    for row in rows:
        q_fav = row["q_fav"]
        q_avv = row["q_avv"]
        rank_avv = row["rank_avv"]
        # No-vig
        total = (1/q_fav) + (1/q_avv)
        p_fav = (1/q_fav) / total
        # Log transform
        x = math.log(rank_avv)
        y = math.log(p_fav)
        xs.append(x)
        ys.append(y)

    n = len(xs)
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xy = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    ss_xx = sum((xs[i] - x_mean) ** 2 for i in range(n))

    if ss_xx == 0:
        return {"error": "Varianza X nulla, impossibile calcolare OLS"}

    b = ss_xy / ss_xx
    a = y_mean - b * x_mean

    # R²
    y_pred = [a + b * x for x in xs]
    ss_res = sum((ys[i] - y_pred[i]) ** 2 for i in range(n))
    ss_tot = sum((ys[i] - y_mean) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    # Fair value per match attuale (rank avversario = und_rank)
    x_now = math.log(und_rank) if und_rank else None
    if x_now:
        ln_p_forecast = a + b * x_now
        p_forecast = math.exp(ln_p_forecast)
        fair_value_fav = 1 / p_forecast
    else:
        fair_value_fav = None

    return {
        "a": round(a, 4),
        "b": round(b, 4),
        "r2": round(r2, 3),
        "fair_value_fav": round(fair_value_fav, 3) if fair_value_fav else None,
        "fav_rank": fav_rank,
        "und_rank": und_rank,
        "n_rows": n
    }

def generate_pinnacle_chart_html(match_name: str, pinnacle_history_fav: list,
                                  pinnacle_history_und: list, avg_fav: float,
                                  avg_und: float, gap_fav: float, gap_und: float) -> str:
    """
    Genera HTML con grafico Chart.js drift Pinnacle vs avg mercato.
    pinnacle_history_fav: lista di dict {time, quote}
    """
    # Determina segnale
    signal_text = ""
    if gap_fav >= 0.08:
        signal_text = f"⚠️ Pinnacle MAX quota su FAV ({gap_fav:+.2f}) → segnale PRO UND"
    elif gap_und >= 0.08:
        signal_text = f"⚠️ Pinnacle MAX quota su UND ({gap_und:+.2f}) → segnale PRO FAV"
    elif gap_fav >= 0.05 or gap_und >= 0.05:
        signal_text = "⚠️ Zona grigia — segnale debole"
    else:
        signal_text = "✅ Pinnacle in range mercato — NO SIGNAL"

    # Prepara dati JS
    labels_fav = [r.get("time", f"T{i}") for i, r in enumerate(pinnacle_history_fav)]
    data_fav = [r.get("quote", 0) for r in pinnacle_history_fav]
    labels_und = [r.get("time", f"T{i}") for i, r in enumerate(pinnacle_history_und)]
    data_und = [r.get("quote", 0) for r in pinnacle_history_und]

    # Unifica labels
    all_labels = sorted(set(labels_fav + labels_und))
    labels_js = str(all_labels).replace("'", '"')

    # Allinea dati alle labels unificate
    fav_map = {r.get("time", f"T{i}"): r.get("quote", None) for i, r in enumerate(pinnacle_history_fav)}
    und_map = {r.get("time", f"T{i}"): r.get("quote", None) for i, r in enumerate(pinnacle_history_und)}

    fav_data_js = [fav_map.get(l, "null") for l in all_labels]
    und_data_js = [und_map.get(l, "null") for l in all_labels]

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<title>Pinnacle Drift — {match_name}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  body {{ font-family: Arial, sans-serif; background: #1a1a2e; color: #eee; padding: 20px; }}
  h2 {{ color: #e94560; }}
  .signal {{ font-size: 1.1em; margin: 10px 0; padding: 10px; background: #16213e; border-radius: 8px; }}
  .metrics {{ display: flex; gap: 20px; margin-top: 20px; }}
  .card {{ background: #16213e; border-radius: 8px; padding: 15px; flex: 1; }}
  .card h3 {{ margin: 0 0 8px; font-size: 0.9em; color: #aaa; }}
  .card .val {{ font-size: 1.4em; font-weight: bold; }}
  .card .gap {{ font-size: 0.9em; margin-top: 4px; }}
  .pos {{ color: #e94560; }}
  .neg {{ color: #4ecca3; }}
  canvas {{ max-height: 400px; }}
</style>
</head>
<body>
<h2>🎾 Pinnacle Drift — {match_name}</h2>
<div class="signal">{signal_text}</div>
<canvas id="chart"></canvas>
<div class="metrics">
  <div class="card">
    <h3>Pinnacle FAV</h3>
    <div class="val">{data_fav[-1] if data_fav else 'nd'}</div>
    <div class="gap {'pos' if gap_fav > 0 else 'neg'}">vs avg mercato: {gap_fav:+.3f}</div>
  </div>
  <div class="card">
    <h3>Avg Mercato FAV</h3>
    <div class="val">{avg_fav:.2f}</div>
  </div>
  <div class="card">
    <h3>Pinnacle UND</h3>
    <div class="val">{data_und[-1] if data_und else 'nd'}</div>
    <div class="gap {'pos' if gap_und > 0 else 'neg'}">vs avg mercato: {gap_und:+.3f}</div>
  </div>
  <div class="card">
    <h3>Avg Mercato UND</h3>
    <div class="val">{avg_und:.2f}</div>
  </div>
</div>
<script>
const ctx = document.getElementById('chart').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: {labels_js},
    datasets: [
      {{
        label: 'Pinnacle FAV',
        data: {fav_data_js},
        borderColor: '#4e9af1',
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 3,
        tension: 0.3,
        spanGaps: true
      }},
      {{
        label: 'Pinnacle UND',
        data: {und_data_js},
        borderColor: '#f1a04e',
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 3,
        tension: 0.3,
        spanGaps: true
      }},
      {{
        label: 'Avg Mercato FAV',
        data: Array({len(all_labels)}).fill({avg_fav}),
        borderColor: '#4e9af1',
        backgroundColor: 'transparent',
        borderWidth: 1,
        borderDash: [6, 4],
        pointRadius: 0
      }},
      {{
        label: 'Avg Mercato UND',
        data: Array({len(all_labels)}).fill({avg_und}),
        borderColor: '#f1a04e',
        backgroundColor: 'transparent',
        borderWidth: 1,
        borderDash: [6, 4],
        pointRadius: 0
      }}
    ]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ labels: {{ color: '#eee' }} }},
      tooltip: {{ mode: 'index', intersect: false }}
    }},
    scales: {{
      x: {{ ticks: {{ color: '#aaa' }}, grid: {{ color: '#333' }} }},
      y: {{ ticks: {{ color: '#aaa' }}, grid: {{ color: '#333' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""
    return html

def analyze_tennis(user: dict, protocol_text: str) -> tuple:
    """
    Analisi tennis completa con:
    - Estrazione quote da HTML TennisExplorer
    - Calcolo avg mercato + gap Pinnacle
    - OLS se fornito
    - Layer stake additivi
    Ritorna (testo_analisi, html_grafico, stake_totale)
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Prepara OLS info
    ols_result = None
    ols_info = ""
    if user["ols_dataset"]:
        parsed = parse_ols_dataset(user["ols_dataset"])
        if parsed["rows"] and parsed["und_rank"]:
            ols_result = run_ols(parsed)
            ols_info = f"""
OLS DATASET PARSED:
- FAV rank UTR attuale: {parsed['fav_rank']}
- UND rank UTR attuale: {parsed['und_rank']}
- Righe storiche: {len(parsed['rows'])}
- Dettaglio: {parsed['rows']}

OLS RESULTS:
- Fair value FAV: {ols_result.get('fair_value_fav')}
- R²: {ols_result.get('r2')}
- a={ols_result.get('a')}, b={ols_result.get('b')}

Applica la regola direzione OLS:
- Se fair_value_fav > quota Pinnacle FAV → segnale PRO FAV
- Se fair_value_fav < quota Pinnacle FAV → segnale PRO UND (avversario)
"""
        else:
            ols_info = "\n\nOLS: Dataset fornito ma insufficiente (mancano und_rank o righe storiche)."
    else:
        ols_info = "\n\nOLS: Non fornito. Procedi senza modello OLS."

    html_info = ""
    if user["html_source"]:
        html_info = f"\n\nTENNISEXPLORER HTML SOURCE:\n{user['html_source'][:10000]}"

    instruction = f"""Analizza il match tennis applicando il protocollo LBA e il Pinnacle Workflow v1.0.

ISTRUZIONI OBBLIGATORIE:
1. Estrai dall'HTML: nome FAV, nome UND, quote Pinnacle (FAV e UND), quote tutti gli altri book
2. Calcola avg mercato FAV e UND ESCLUDENDO Pinnacle e Betfair
3. Calcola gap: gap_fav = Pinnacle_FAV - avg_FAV | gap_und = Pinnacle_UND - avg_UND
4. Applica regola Pinnacle:
   - gap_fav ≥ +0.08 → sharp money su UND → segnale PRO UND
   - gap_und ≥ +0.08 → sharp money su FAV → segnale PRO FAV
   - entrambi < 0.05 → NO SIGNAL
5. Calcola stake layer additivi (MAX 1.00u totale):
   - L1 +0.25u: se grafico drift Pinnacle mostra salita su un lato (segnale pro opposto)
   - L2 +0.25u: se gap Pinnacle vs avg ≥ 0.08 (conferma numerica)
   - L3 +0.25u: se OLS converge con segnale Pinnacle
   - Se nessun layer attivo o segnali divergenti → NO BET
6. Output finale OBBLIGATORIO in questo formato:

MATCH: [FAV] vs [UND]
Quote Pinnacle: FAV=[x.xx] UND=[x.xx]
Avg mercato: FAV=[x.xx] (N book) | UND=[x.xx] (N book)
Gap Pinnacle: FAV=[±x.xx] | UND=[±x.xx]

SEGNALE PINNACLE: PRO [giocatore] / NO SIGNAL
L1 (drift grafico): [ATTIVO +0.25u / NON ATTIVO]
L2 (gap ≥0.08): [ATTIVO +0.25u / NON ATTIVO]
L3 (OLS): [ATTIVO +0.25u / NON ATTIVO / NON FORNITO]

STAKE TOTALE: [x.xx]u
VERDICT: [GIOCA su GIOCATORE @ quota / NO BET]

Includi anche la storia Pinnacle in formato JSON per il grafico:
PINNACLE_HISTORY_FAV: [{{"time":"T1","quote":x.xx}}, ...]
PINNACLE_HISTORY_UND: [{{"time":"T1","quote":x.xx}}, ...]
AVG_FAV: x.xx
AVG_UND: x.xx
GAP_FAV: x.xx
GAP_UND: x.xx
MATCH_NAME: [FAV] vs [UND]

{ols_info}
{html_info}"""

    content = []
    for img_bytes in user["images"]:
        image_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = detect_media_type(img_bytes)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": image_b64}
        })
    content.append({"type": "text", "text": instruction})

    system_prompt = f"""Sei un analista betting tennis. Applica il protocollo LBA e il Pinnacle Workflow v1.0.

PROTOCOLLO:
{protocol_text}

Regole critiche:
- FAV = giocatore con quota Pinnacle più bassa
- Regola direzione OLS (MAI sbagliare): forecast > mercato = PRO soggetto; forecast < mercato = PRO avversario
- Regola Pinnacle: Pinnacle MAX quota su un lato = sharp money sull'altro = segnale PRO opposto
- NO BET se nessun layer attivo
- Rispondi SEMPRE in italiano"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    analysis_text = message.content[0].text

    # Estrai dati per grafico dal testo analisi
    html_chart = None
    try:
        match_name = re.search(r'MATCH_NAME:\s*(.+)', analysis_text)
        avg_fav = re.search(r'AVG_FAV:\s*([\d.]+)', analysis_text)
        avg_und = re.search(r'AVG_UND:\s*([\d.]+)', analysis_text)
        gap_fav = re.search(r'GAP_FAV:\s*([-\d.]+)', analysis_text)
        gap_und = re.search(r'GAP_UND:\s*([-\d.]+)', analysis_text)
        hist_fav = re.search(r'PINNACLE_HISTORY_FAV:\s*(\[.*?\])', analysis_text, re.DOTALL)
        hist_und = re.search(r'PINNACLE_HISTORY_UND:\s*(\[.*?\])', analysis_text, re.DOTALL)

        import json
        if all([match_name, avg_fav, avg_und, gap_fav, gap_und, hist_fav, hist_und]):
            html_chart = generate_pinnacle_chart_html(
                match_name=match_name.group(1).strip(),
                pinnacle_history_fav=json.loads(hist_fav.group(1)),
                pinnacle_history_und=json.loads(hist_und.group(1)),
                avg_fav=float(avg_fav.group(1)),
                avg_und=float(avg_und.group(1)),
                gap_fav=float(gap_fav.group(1)),
                gap_und=float(gap_und.group(1))
            )
    except Exception as e:
        logger.error(f"Errore generazione grafico: {e}")
        html_chart = None

    # Pulisci testo dai dati tecnici del grafico
    clean_text = re.sub(r'PINNACLE_HISTORY_FAV:.*?(?=\n[A-Z]|\Z)', '', analysis_text, flags=re.DOTALL)
    clean_text = re.sub(r'PINNACLE_HISTORY_UND:.*?(?=\n[A-Z]|\Z)', '', clean_text, flags=re.DOTALL)
    clean_text = re.sub(r'(AVG_FAV|AVG_UND|GAP_FAV|GAP_UND|MATCH_NAME):.*\n?', '', clean_text)
    clean_text = clean_text.strip()

    return clean_text, html_chart

def analyze_soccer(user: dict, protocol_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    instruction = (
        "Analizza questo screenshot calcio applicando il protocollo Soccer Model. "
        "Segui l'output format della Section 11 e concludi SEMPRE con il VERDICT block della Section 12."
    )
    system_prompt = f"""You are a sports betting analyst bot with a protocol document containing all rules.

PROTOCOL DOCUMENT:
{protocol_text}

Rules:
1. Read ALL provided data carefully
2. Extract all relevant data
3. Apply ALL protocol rules mechanically
4. Respond ONLY in Italian using the exact output format defined in the protocol
5. ALWAYS append the VERDICT block at the very end

Be precise with numbers. Never skip any section."""

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
        model="claude-haiku-4-5-20251001",
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
    "Poi manda screenshot (e/o URL TennisExplorer) e scrivi *analizza*.\n"
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
    "341 290 95\n"
    "```\n\n"
    "Dove:\n"
    "• `fav:` → rank UTR del FAV oggi\n"
    "• `und:` → rank UTR dell'UND oggi\n"
    "• Righe: quota\\_fav quota\\_avv rank\\_avv (÷100 per quota)\n\n"
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
        user["html_source"] = file_bytes.decode("utf-8", errors="ignore")
        user["state"] = STATE_WAITING_OLS
        await update.message.reply_text(
            f"📄 File *{filename}* caricato.\n\n"
            + OLS_FORMAT_HELP
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
            f"📥 Screenshot {count} ricevuto ({sport_emoji} TENNIS).\n\n"
            + OLS_FORMAT_HELP
        )
    elif user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
        await update.message.reply_text(
            f"📥 Screenshot {count} aggiunto.\n"
            f"Manda il dataset OLS oppure scrivi *no* per procedere senza."
        )
    else:
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
            f"{'Allega il file .html o .txt con le quote (TennisExplorer o simili).' if text_lower == 'tennis' else 'Manda uno o più screenshot e poi scrivi *analizza*.'}"
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
            "Analisi basata su Pinnacle drift + gap mercato.\n"
            "Scrivi *analizza* quando pronto."
        )
        return

    # — Analizza —
    if text_lower == "analizza":
        if not user["sport"]:
            await update.message.reply_text("⚠️ Seleziona prima lo sport: scrivi *soccer* o *tennis*.")
            return
        if not user["images"] and not user["html_source"]:
            await update.message.reply_text("❌ Nessun dato in coda. Manda prima uno screenshot o URL TennisExplorer.")
            return
        if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS:
            await update.message.reply_text(
                "⚠️ Vuoi aggiungere il dataset OLS?\n\n"
                + OLS_FORMAT_HELP
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

            if sport == "tennis":
                result_text, html_chart = analyze_tennis(user, protocol)
            else:
                result_text = analyze_soccer(user, protocol)
                html_chart = None

            # Reset dopo analisi
            user["images"] = []
            user["html_source"] = None
            user["ols_dataset"] = None
            user["state"] = STATE_SPORT_SELECTED

            await send_long_message(update, result_text)

            # Invia grafico HTML se disponibile
            if html_chart:
                import io
                html_bytes = html_chart.encode("utf-8")
                html_file = io.BytesIO(html_bytes)
                html_file.name = "pinnacle_drift.html"
                await update.message.reply_document(
                    document=html_file,
                    filename="pinnacle_drift.html",
                    caption="📊 Grafico Pinnacle Drift — aprilo nel browser"
                )

        except Exception as e:
            logger.error(f"Error: {e}")
            await update.message.reply_text(f"❌ Errore: {str(e)}")
        return

    # — Tennis: nuovo formato OLS (fav: / und: / avversari passati:) —
    if user["sport"] == "tennis" and user["state"] == STATE_WAITING_OLS and is_ols_format(text):
        user["ols_dataset"] = text
        user["state"] = STATE_READY
        parsed = parse_ols_dataset(text)
        n_rows = len(parsed["rows"])
        await update.message.reply_text(
            f"📐 Dataset OLS ricevuto:\n"
            f"• FAV rank UTR: {parsed['fav_rank']}\n"
            f"• UND rank UTR: {parsed['und_rank']}\n"
            f"• Righe storiche: {n_rows}\n\n"
            f"Scrivi *analizza* per procedere."
        )
        return

    # — Tennis: HTML sorgente incollato —
    if user["sport"] == "tennis" and len(text) > 100 and "<" in text and ">" in text:
        user["html_source"] = text
        user["state"] = STATE_WAITING_OLS
        await update.message.reply_text(
            "📄 HTML TennisExplorer ricevuto.\n\n"
            + OLS_FORMAT_HELP
        )
        return

    # — Default —
    await update.message.reply_text(HELP_TEXT)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()

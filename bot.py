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

def parse_tennis_html(raw_html: str) -> str:
    """Estrae quote e storico Pinnacle dall'HTML TennisExplorer."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, 'html.parser')
        out = []

        # Titolo match
        title = soup.find('h1')
        if title:
            out.append('MATCH: ' + title.get_text(strip=True))

        # Info torneo/data
        for div in soup.find_all(['div', 'p', 'td'], class_=['date', 'course', 'box-row', 'match-info']):
            t = div.get_text(separator=' ', strip=True)
            if t and len(t) < 200:
                out.append(t)

        # Betting odds
        odds_div = soup.find('div', {'id': 'oddsMenu-1-data'})
        if odds_div:
            rows = odds_div.find_all('tr')
            players = ['K1', 'K2']
            if rows:
                header = rows[0]
                p = [td.get_text(strip=True) for td in header.find_all('td', class_=['k1', 'k2'])]
                if len(p) >= 2:
                    players = p

            out.append(f'\n--- QUOTE ATTUALI (K1={players[0]}, K2={players[1]}) ---')
            out.append(f'{"Bookmaker":<15} {players[0]:<10} {players[1]:<10}')
            out.append('-' * 40)

            pinnacle_history_k1 = []
            pinnacle_history_k2 = []

            for row in rows[1:]:
                first = row.find('td', class_='first')
                if not first:
                    continue
                bname = first.get_text(strip=True)
                k1_td = row.find('td', class_='k1')
                k2_td = row.find('td', class_='k2')
                if not k1_td or not k2_td:
                    continue

                k1_text = k1_td.get_text(separator=' ', strip=True)
                k2_text = k2_td.get_text(separator=' ', strip=True)

                k1_nums = re.findall(r'\b(\d+\.\d{2})\b', k1_text)
                k2_nums = re.findall(r'\b(\d+\.\d{2})\b', k2_text)
                k1_current = k1_nums[0] if k1_nums else 'nd'
                k2_current = k2_nums[0] if k2_nums else 'nd'

                out.append(f'{bname:<15} {k1_current:<10} {k2_current:<10}')

                if 'pinnacle' in bname.lower():
                    timestamps_k1 = re.findall(r'(\d{2}\.\d{2}\. \d{2}:\d{2})\s+([\d.]+)', k1_text)
                    timestamps_k2 = re.findall(r'(\d{2}\.\d{2}\. \d{2}:\d{2})\s+([\d.]+)', k2_text)
                    pinnacle_history_k1 = timestamps_k1
                    pinnacle_history_k2 = timestamps_k2

            if pinnacle_history_k1 or pinnacle_history_k2:
                out.append(f'\n--- STORICO PINNACLE ---')
                out.append(f'{"Timestamp":<20} {players[0]:<10} {players[1]:<10}')
                out.append('-' * 45)
                k1_map = {t: v for t, v in pinnacle_history_k1}
                k2_map = {t: v for t, v in pinnacle_history_k2}
                all_times = sorted(set(list(k1_map.keys()) + list(k2_map.keys())))
                for ts in all_times:
                    v1 = k1_map.get(ts, 'nd')
                    v2 = k2_map.get(ts, 'nd')
                    out.append(f'{ts:<20} {v1:<10} {v2:<10}')

        return '\n'.join(out)

    except Exception as e:
        logger.error(f"parse_tennis_html error: {e}")
        return raw_html[:20000]

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

def generate_pinnacle_chart_html(match_name, hist_fav, hist_und, avg_fav, avg_und, gap_fav, gap_und, fav_name, und_name):
    """Genera HTML Chart.js con drift Pinnacle vs avg mercato."""

    if gap_fav >= 0.08:
        signal = f"⚠️ Pinnacle MAX quota su {fav_name} → segnale PRO {und_name} (gap {gap_fav:+.3f})"
        signal_color = "#e94560"
    elif gap_und >= 0.08:
        signal = f"⚠️ Pinnacle MAX quota su {und_name} → segnale PRO {fav_name} (gap {gap_und:+.3f})"
        signal_color = "#e94560"
    elif gap_fav >= 0.05 or gap_und >= 0.05:
        signal = f"⚠️ Zona grigia — segnale debole"
        signal_color = "#f1a04e"
    else:
        signal = "✅ Pinnacle in range mercato — NO SIGNAL"
        signal_color = "#4ecca3"

    all_times = sorted(set([r["time"] for r in hist_fav] + [r["time"] for r in hist_und]))
    fav_map = {r["time"]: r["quote"] for r in hist_fav}
    und_map = {r["time"]: r["quote"] for r in hist_und}

    fav_data = [fav_map.get(t, "null") for t in all_times]
    und_data = [und_map.get(t, "null") for t in all_times]

    labels_js = json.dumps(all_times)
    fav_js = json.dumps(fav_data)
    und_js = json.dumps(und_data)
    n = len(all_times)

    fav_current = hist_fav[-1]["quote"] if hist_fav else "nd"
    und_current = hist_und[-1]["quote"] if hist_und else "nd"

    from datetime import datetime
    now = datetime.now().strftime("%d.%m.%Y · ore %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pinnacle Drift — {match_name}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f1a; color: #eee; padding: 20px; }}
  h2 {{ color: #fff; font-size: 1.1em; margin-bottom: 4px; }}
  .sub {{ color: #888; font-size: 0.85em; margin-bottom: 16px; }}
  .signal {{ font-size: 0.95em; margin: 12px 0; padding: 12px 16px; background: #1a1a2e; border-left: 4px solid {signal_color}; border-radius: 4px; color: {signal_color}; }}
  .chart-wrap {{ background: #1a1a2e; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
  .metrics {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .card {{ background: #1a1a2e; border-radius: 10px; padding: 14px; }}
  .card .label {{ font-size: 0.75em; color: #888; margin-bottom: 4px; }}
  .card .val {{ font-size: 1.5em; font-weight: 700; color: #fff; }}
  .card .gap {{ font-size: 0.85em; margin-top: 4px; }}
  .pos {{ color: #e94560; }}
  .neg {{ color: #4ecca3; }}
  canvas {{ max-height: 320px; }}
</style>
</head>
<body>
<h2>🎾 {match_name}</h2>
<div class="sub">{now} · {len([x for x in fav_data if x != 'null'])} book mercato</div>
<div class="signal">{signal}</div>
<div class="chart-wrap">
  <canvas id="chart"></canvas>
</div>
<div class="metrics">
  <div class="card">
    <div class="label">Pinnacle UND — {und_name}</div>
    <div class="val">{und_current}</div>
    <div class="gap {'pos' if gap_und > 0 else 'neg'}">vs avg {avg_und:.3f} · {gap_und:+.3f}</div>
  </div>
  <div class="card">
    <div class="label">Pinnacle FAV — {fav_name}</div>
    <div class="val">{fav_current}</div>
    <div class="gap {'pos' if gap_fav > 0 else 'neg'}">vs avg {avg_fav:.3f} · {gap_fav:+.3f}</div>
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
        label: '{fav_name} (Pinnacle)',
        data: {fav_js},
        borderColor: '#f1a04e',
        backgroundColor: 'transparent',
        borderWidth: 2.5,
        pointRadius: 5,
        tension: 0.2,
        spanGaps: true
      }},
      {{
        label: '{und_name} (Pinnacle)',
        data: {und_js},
        borderColor: '#4e9af1',
        backgroundColor: 'transparent',
        borderWidth: 2.5,
        pointRadius: 5,
        tension: 0.2,
        spanGaps: true
      }},
      {{
        label: 'avg {fav_name}',
        data: Array({n}).fill({avg_fav}),
        borderColor: '#f1a04e',
        backgroundColor: 'transparent',
        borderWidth: 1,
        borderDash: [6, 4],
        pointRadius: 0
      }},
      {{
        label: 'avg {und_name}',
        data: Array({n}).fill({avg_und}),
        borderColor: '#4e9af1',
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
      legend: {{ labels: {{ color: '#ccc', font: {{ size: 11 }} }} }},
      tooltip: {{ mode: 'index', intersect: false }}
    }},
    scales: {{
      x: {{ ticks: {{ color: '#888', font: {{ size: 10 }} }}, grid: {{ color: '#222' }} }},
      y: {{ ticks: {{ color: '#888', font: {{ size: 10 }} }}, grid: {{ color: '#222' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""
    return html

def analyze_tennis(user: dict, protocol_text: str) -> tuple:
    """Analisi tennis completa. Ritorna (testo_analisi, html_grafico_o_None)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # OLS
    ols_info = ""
    ols_result = None
    if user["ols_dataset"]:
        parsed = parse_ols_dataset(user["ols_dataset"])
        if parsed["rows"] and parsed["und_rank"]:
            ols_result = run_ols(parsed)
            ols_info = f"""
OLS DATASET:
- FAV rank UTR: {parsed['fav_rank']}
- UND rank UTR: {parsed['und_rank']}
- Righe storiche: {len(parsed['rows'])}
- Fair value FAV: {ols_result.get('fair_value_fav')}
- R²: {ols_result.get('r2')}

REGOLA OLS (MAI SBAGLIARE):
- fair_value_FAV > quota Pinnacle FAV → modello vede FAV sottovalutato → segnale PRO FAV → L3 ATTIVO
- fair_value_FAV < quota Pinnacle FAV → modello vede FAV sopravvalutato → segnale PRO UND → L3 ATTIVO solo se converge con L1/L2
- Se L3 diverge da L1/L2 → L3 NON ATTIVO
"""
        else:
            ols_info = "\nOLS: Dataset insufficiente."
    else:
        ols_info = "\nOLS: Non fornito → L3 = NON FORNITO"

    html_data = ""
    if user["html_source"]:
        html_data = f"\n\nDATI MATCH (estratti da HTML TennisExplorer):\n{user['html_source'][:15000]}"

    system_prompt = f"""Sei un analista betting tennis. Applica il Pinnacle Workflow v1.0 in modo preciso.

REGOLE CRITICHE — MAI SBAGLIARE:

1. FAV = giocatore con la QUOTA PINNACLE PIÙ BASSA (non ranking ATP)
   UND = giocatore con la QUOTA PINNACLE PIÙ ALTA

2. REGOLA PINNACLE DIREZIONE:
   - Pinnacle quota MAX su FAV (gap_FAV ≥ +0.08) → sharp money ricevuto su UND → Pinnacle alza FAV per proteggersi → SEGNALE PRO UND
   - Pinnacle quota MAX su UND (gap_UND ≥ +0.08) → sharp money ricevuto su FAV → Pinnacle alza UND per proteggersi → SEGNALE PRO FAV
   - Entrambi i gap < 0.05 → NO SIGNAL

3. CALCOLO GAP:
   gap_FAV = Pinnacle_FAV - avg_mercato_FAV
   gap_UND = Pinnacle_UND - avg_mercato_UND
   avg_mercato = media quote ESCLUDENDO Pinnacle e Betfair

4. LAYER STAKE (additivi, max 1.00u):
   L1 +0.25u: drift grafico Pinnacle sale su un lato (segnale pro opposto) — valuta dal storico
   L2 +0.25u: gap Pinnacle vs avg ≥ +0.08 (conferma numerica)
   L3 +0.25u: OLS converge con segnale L1/L2
   Se nessun layer attivo O segnali divergenti → NO BET (0.00u)

5. OUTPUT OBBLIGATORIO in questo formato esatto:

MATCH: [FAV] vs [UND]
FAV: [nome] | Pinnacle: [x.xx] | Avg mercato: [x.xx] ([N] book) | Gap: [±x.xx]
UND: [nome] | Pinnacle: [x.xx] | Avg mercato: [x.xx] ([N] book) | Gap: [±x.xx]

SEGNALE PINNACLE: PRO [nome] / NO SIGNAL
Motivazione: [una riga]

L1 (drift grafico): [ATTIVO +0.25u — motivazione] / [NON ATTIVO — motivazione]
L2 (gap ≥0.08): [ATTIVO +0.25u] / [NON ATTIVO]
L3 (OLS): [ATTIVO +0.25u] / [NON ATTIVO] / [NON FORNITO]

STAKE TOTALE: [x.xx]u
VERDICT: GIOCA [nome] @ [quota] / NO BET

---
DATI_GRAFICO_JSON:
{{
  "match_name": "[FAV] vs [UND]",
  "fav_name": "[nome FAV]",
  "und_name": "[nome UND]",
  "hist_fav": [{{"time":"HH:MM","quote":x.xx}}, ...],
  "hist_und": [{{"time":"HH:MM","quote":x.xx}}, ...],
  "avg_fav": x.xx,
  "avg_und": x.xx,
  "gap_fav": x.xx,
  "gap_und": x.xx
}}

PROTOCOLLO:
{protocol_text[:3000]}"""

    instruction = f"""Analizza il match tennis con il Pinnacle Workflow v1.0.
{ols_info}
{html_data}"""

    content = []
    for img_bytes in user["images"]:
        image_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = detect_media_type(img_bytes)
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}})
    content.append({"type": "text", "text": instruction})

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    analysis_text = message.content[0].text

    # Estrai JSON grafico
    html_chart = None
    try:
        json_match = re.search(r'DATI_GRAFICO_JSON:\s*(\{.*?\})\s*$', analysis_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(1))
            html_chart = generate_pinnacle_chart_html(
                match_name=data["match_name"],
                hist_fav=data["hist_fav"],
                hist_und=data["hist_und"],
                avg_fav=float(data["avg_fav"]),
                avg_und=float(data["avg_und"]),
                gap_fav=float(data["gap_fav"]),
                gap_und=float(data["gap_und"]),
                fav_name=data["fav_name"],
                und_name=data["und_name"]
            )
    except Exception as e:
        logger.error(f"Errore generazione grafico: {e}")

    # Pulisci testo dai dati grafico
    clean_text = re.sub(r'DATI_GRAFICO_JSON:.*$', '', analysis_text, flags=re.DOTALL).strip()

    return clean_text, html_chart

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
        if ext in ("html", "htm"):
            user["html_source"] = parse_tennis_html(raw)
        else:
            user["html_source"] = raw[:15000]
        user["state"] = STATE_WAITING_OLS
        await update.message.reply_text(
            f"📄 File *{filename}* caricato.\n\n" + OLS_FORMAT_HELP,
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
                result_text, html_chart = analyze_tennis(user, protocol)
            else:
                result_text = analyze_soccer(user, protocol)
                html_chart = None

            user["images"] = []
            user["html_source"] = None
            user["ols_dataset"] = None
            user["state"] = STATE_SPORT_SELECTED

            await send_long_message(update, result_text)

            if html_chart:
                html_bytes = html_chart.encode("utf-8")
                html_file = io.BytesIO(html_bytes)
                html_file.name = "pinnacle_drift.html"
                await update.message.reply_document(
                    document=html_file,
                    filename="pinnacle_drift.html",
                    caption="📊 Grafico Pinnacle Drift — aprilo nel browser"
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

    # HTML incollato
    if user["sport"] == "tennis" and len(text) > 100 and "<" in text and ">" in text:
        user["html_source"] = parse_tennis_html(text)
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

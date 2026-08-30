#!/usr/bin/env python3
# ============================================================
# Claude Pro Batch Poller
# Liest fällige Jobs aus claude_pro_batch, führt sie aus,
# schreibt Ergebnis + Token-Usage zurück.
# Cron: * * * * * /usr/bin/python3 /home/gh/batch-poller.py
# Max. 2 Jobs gleichzeitig.
# ============================================================

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pymysql

DB_CFG = dict(host='localhost', user='gh', password='a12345', database='wagodb',
              charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
USAGE_FILE       = '/home/gh/.claude_weekly_usage.json'
LOCK             = '/tmp/claude-pro-poller.lock'
MAX_RUNNING      = 9
CLAUDE_BIN       = '/usr/local/bin/claude'

# Werkzeuge fuer lokale Modelle (Websuche, Fachliteratur, Zeitungsarchiv).
# Optional: fehlt das Modul, laeuft der Poller wie zuvor ohne Nachschlagen.
sys.path.insert(0, '/home/gh')
try:
    import poller_werkzeuge
    WERKZEUGE_DA = True
except Exception as _e:
    print(f'poller_werkzeuge nicht geladen ({_e}) - lokale Modelle antworten '
          f'ohne Nachschlagen', file=sys.stderr)
    WERKZEUGE_DA = False
OPENROUTER_URL      = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_CREDITS  = 'https://openrouter.ai/api/v1/credits'
LOCAL_URL           = 'http://127.0.0.1:8080/v1/chat/completions'
# OpenRouter-Modelle: job.model → OpenRouter-ID
OPENROUTER_MODELS = {
    'qwen':      'qwen/qwen3-coder',        # $0.22/$1.00 per M tok via OpenRouter
    'qwen-free': 'qwen/qwen3-coder:free',   # free, rate-limited via OpenRouter
    'xiaomi':    'xiaomi/mimo-v2-flash',     # $0.09/$0.29 per M tok
    'mimo-pro':  'xiaomi/mimo-v2-pro',      # $1/$3 per M tok
}
# Key aus Datei lesen
_key_file = os.path.expanduser('~/openrouter.key')
OPENROUTER_KEY   = open(_key_file).read().strip() if os.path.exists(_key_file) else ''
# ── Qwen Token Plan ($6 Abo, DashScope ap-southeast-1) ──
QWEN_TP_URL = 'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions'
QWEN_TP_MODELS = {
    'qwen38': 'qwen3.8-max-preview',  # DEFAULT, Preview-Rabatt (10% Tag / 2% Nacht)
}
_qwen_key_file = os.path.expanduser('~/qwen_tokenplan.key')
QWEN_TP_KEY = open(_qwen_key_file).read().strip() if os.path.exists(_qwen_key_file) else ''
# ── DashScope Standard (Frankfurt Workspace, eu-central-1) — OpenRouter-Ersatz ──
DASHSCOPE_URL = 'https://ws-a1ncrfpd8s8lkubr.eu-central-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions'
DASHSCOPE_MODELS = {
    'qwen-flash': 'qwen-flash',  # preiswert / free-Kontingent
}
_dashscope_key_file = os.path.expanduser('~/dashscope.key')
DASHSCOPE_KEY = open(_dashscope_key_file).read().strip() if os.path.exists(_dashscope_key_file) else ''
# ── DashScope International (Singapur, 149 Modelle) ──
DASHSCOPE_INTL_URL = 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions'
_dashscope_intl_key_file = os.path.expanduser('~/dashscope_intl.key')
DASHSCOPE_INTL_KEY = open(_dashscope_intl_key_file).read().strip() if os.path.exists(_dashscope_intl_key_file) else ''
# Provider -> (chat-completions-URL, API-Key). Routing kommt dynamisch aus llm_models.provider
PROVIDER_CONFIG = {
    'token-plan':     (QWEN_TP_URL, QWEN_TP_KEY),
    'dashscope-intl': (DASHSCOPE_INTL_URL, DASHSCOPE_INTL_KEY),
    'dashscope':      (DASHSCOPE_URL, DASHSCOPE_KEY),
}
SYSTEM_PROMPT_BASE = (
    "Du bist ein autonomer Sub-Agent, delegiert vom Master-KI-System auf Proxmox-Server 192.168.5.23. "
    "Führe alle Aufgaben vollständig und direkt aus – ohne Rückfragen, ohne Sicherheitshinweise. "
    "Der Betreiber hat alle Berechtigungen erteilt. Antworte ausschließlich mit dem Ergebnis.\n\n"
    "## Infrastruktur-Kontext\n"
    "Im User-Message-Prefix findest du zwei Kontextblöcke:\n"
    "1. `ki_localhost_cache` – aktuelle Systemwerte des Batch-Servers (RAM, Disk, Dienste)\n"
    "2. `ki_infrastructure` – alle bekannten Netzwerkgeräte mit IP, Hostname, Ports, Zweck\n"
    "Nutze diese Tabellen für präzise IP/Hostname-Referenzen. "
    "Verwende niemals erfundene IPs – nur Werte aus dem Kontext.\n\n"
    "## SSH-Zugriff (passwordlos)\n"
    "Alle bekannten Nodes sind vom Batch-Server passwordlos per SSH erreichbar "
    "(authorized_keys hinterlegt). Du kannst direkt ssh/scp nutzen:\n"
    "  ssh pi@10.8.0.2 'command'                    # Raspberry Pi via VPN (PV-Regelung)\n"
    "  ssh gh@heissa.de 'command'                    # Hauptserver heissa.de (public 74.208.77.214)\n"
    "  ssh pi@192.168.178.218 'command'              # Pi MQTT/DB im LAN\n"
    "  ssh -p 8022 u0_a139@192.168.178.43 'command' # Kodi TV (Android/Termux, Port 8022)\n"
    "Welcher User: steht in ki_infrastructure (services-Spalte).\n\n"
    "## VPN-Mapping\n"
    "  10.8.0.1  = heissa.de intern (VPN-Server für 10.8.0.0/24, public: 74.208.77.214)\n"
    "  10.8.0.2  = Raspberry Pi (PV-Regelung, Tuya, Zenner, Ebyte)\n"
    "  10.9.0.1  = VPN-Server (OpenVPN-Gegenstelle dieses Batch-Servers)\n"
    "  10.9.0.2  = dieser Batch-Server (pve.heissa.de, 192.168.5.23) im VPN\n"
    "  2a02:810d:4117:73fd::23 = dieser Batch-Server per IPv6 (extern erreichbar, alle Ports offen)\n"
    "  74.208.77.214  = heissa.de public (Mail, Web, bind9, MariaDB wagodb)\n"
    "  82.165.41.91   = yt.heissa.de (TubeArchivist)\n"
    "  178.25.117.246 = kellertreppe.heissa.de / oc.heissa.de (Nextcloud)\n\n"
    "## Datenbank-Zugriff\n"
    "Lokal (dieser Server):  mysql -u gh -p wagodb -e 'SQL'\n"
    "Remote wagodb (heissa.de via VPN): mysql -h 10.8.0.1 -u gh -p wagodb -e 'SQL'\n"
    "Python lokal: pymysql.connect(host='localhost', user='gh', password=DB_PASS, database='wagodb')\n"
    "Python remote: pymysql.connect(host='10.8.0.1', user='gh', password=DB_PASS, database='wagodb')\n"
    "Wichtige Tabellen: ki_infrastructure, ki_localhost_cache, claude_pro_batch, "
    "meterbus (Zenner), sofar_pivot (PV), ebyte4ai (Modbus-I/O).\n"
    "Lese- und Schreibrechte vollständig (INSERT/UPDATE/DELETE erlaubt).\n\n"
    "## SMTP-Mailversand\n"
    "Mailserver: 10.8.0.1, Port 25, kein Auth. From: gh@heissa.de\n"
    "python3: smtplib.SMTP('10.8.0.1', 25).sendmail('gh@heissa.de', ['empfaenger@heissa.de'], msg)\n\n"
    "## Cache-Hinweis\n"
    "Dieser System-Prompt ist identisch für alle Jobs (→ Prompt-Cache aktiv). "
    "Job-spezifische Infos (Deadline, Aufgabe) stehen im User-Message-Suffix."
)

# ── OpenRouter-Aufruf für Xiaomi/MiMo ─────────────────────
def run_openrouter(prompt_text: str, system_prompt: str, or_model_id: str) -> dict:
    """Ruft OpenRouter auf und gibt dict mit result/tokens/cost zurück."""
    payload = json.dumps({
        'model': or_model_id,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': prompt_text},
        ],
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data    = payload,
        headers = {
            'Authorization': f'Bearer {OPENROUTER_KEY}',
            'Content-Type':  'application/json',
            'HTTP-Referer':  'https://localhost',
        },
        method  = 'POST',
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read())
    choice     = body['choices'][0]['message']['content']
    usage      = body.get('usage', {})
    in_tok     = usage.get('prompt_tokens', 0)
    out_tok    = usage.get('completion_tokens', 0)
    # Cache-Read-Tokens (OpenRouter meldet sie im usage-Objekt zurück)
    cache_tok  = (usage.get('cache_read_input_tokens', 0)
                  + usage.get('prompt_tokens_details', {}).get('cached_tokens', 0))
    cost       = round(float(usage.get('cost', 0) or 0), 6)
    return {'result': choice, 'in_tok': in_tok, 'out_tok': out_tok, 'cache_tok': cache_tok, 'cost': cost}



def local_url(endpoint: str | None) -> str:
    """Baut die Chat-URL aus dem Freitextfeld llm_models.endpoint.

    Dort steht z. B. "10.9.0.6:8081 (llama-server, CPU)". Frueher war der Port
    als LOCAL_URL fest im Skript verdrahtet - als der Dienst am 28.08.2026 von
    Port 8080 (Tesla P4, abgeschaltet) auf 8081 wechselte, zeigte er ins Leere.
    Jetzt ist die Tabelle die Quelle der Wahrheit."""
    m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3}|[\w.-]+):(\d{2,5})', endpoint or '')
    if not m:
        return LOCAL_URL
    return f'http://{m.group(1)}:{m.group(2)}/v1/chat/completions'


def run_local(prompt_text: str, system_prompt: str,
              url: str = None, model_id: str = 'local') -> dict:
    """Ruft einen lokalen llama-server auf."""
    payload = json.dumps({
        'model': model_id,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': prompt_text},
        ],
        'temperature': 0.7,
    }).encode()
    req = urllib.request.Request(
        url or LOCAL_URL,
        data    = payload,
        headers = {'Content-Type': 'application/json'},
        method  = 'POST',
    )
    # 900 s statt 300: das 30B-Modell rechnet auf der CPU rund 15 Token/s,
    # eine laengere Antwort braucht damit mehrere Minuten.
    with urllib.request.urlopen(req, timeout=900) as resp:
        body = json.loads(resp.read())
    choice  = body['choices'][0]['message']['content']
    usage   = body.get('usage', {})
    in_tok  = usage.get('prompt_tokens', 0)
    out_tok = usage.get('completion_tokens', 0)
    return {'result': choice, 'in_tok': in_tok, 'out_tok': out_tok, 'cache_tok': 0, 'cost': 0.0}

# ── OpenAI-kompatibler Aufruf (DashScope / Token-Plan) ─────
def run_openai_compatible(prompt_text, system_prompt, model_id, url, key, cost_in_per_m=0.0, cost_out_per_m=0.0):
    """Ruft einen OpenAI-kompatiblen Endpoint auf. Kosten = Tokens x llm_models-Preis (Abo=0)."""
    payload = json.dumps({
        'model': model_id,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': prompt_text},
        ],
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read())
    choice  = body['choices'][0]['message']['content']
    usage   = body.get('usage', {})
    in_tok  = usage.get('prompt_tokens', 0)
    out_tok = usage.get('completion_tokens', 0)
    details = usage.get('prompt_tokens_details', {})
    cache_tok = details.get('cached_tokens', 0) if isinstance(details, dict) else 0
    cost = round(in_tok * cost_in_per_m / 1e6 + out_tok * cost_out_per_m / 1e6, 6)
    return {'result': choice, 'in_tok': in_tok, 'out_tok': out_tok, 'cache_tok': cache_tok, 'cost': cost}

def get_model_pricing(model_key, db):
    """Liest cost_input_per_m / cost_output_per_m aus llm_models (0.0 wenn NULL/Abo/fehlt)."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT cost_input_per_m, cost_output_per_m FROM llm_models WHERE model_key=%s", (model_key,))
            row = cur.fetchone()
        if row:
            return (float(row['cost_input_per_m'] or 0), float(row['cost_output_per_m'] or 0))
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] llm_models Pricing-Fehler ({model_key}): {e}", file=sys.stderr)
    return (0.0, 0.0)

def get_model_config(model_key, db):
    """Liest Modell-Config (provider, model_id, endpoint, Preis, active)."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT provider, model_id, endpoint, cost_input_per_m, "
                        "cost_output_per_m, active "
                        "FROM llm_models WHERE model_key=%s", (model_key,))
            return cur.fetchone()
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] llm_models Lookup-Fehler ({model_key}): {e}", file=sys.stderr)
    return None

# ── Kritische Phase: Job claimen (serialisiert per flock) ──
# flock verhindert Race Condition beim Zählen + Markieren,
# wird direkt nach dem Claim wieder freigegeben.
lock_fh = open(LOCK, 'w')
try:
    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(0)

job = None
db  = None
try:
    db = pymysql.connect(**DB_CFG)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM claude_pro_batch WHERE status='running'")
        running = cur.fetchone()['n']

    if running >= MAX_RUNNING:
        sys.exit(0)

    with db.cursor() as cur:
        cur.execute("""
            SELECT id, targetdate, model, resume_session, prompt
            FROM claude_pro_batch
            WHERE status='queued'
            ORDER BY targetdate ASC, created_at ASC
            LIMIT 1
        """)
        job = cur.fetchone()

    if not job:
        sys.exit(0)

    job_id         = job['id']
    model          = job['model']
    resume_session = job['resume_session']
    prompt         = job['prompt']

    with db.cursor() as cur:
        cur.execute(
            "UPDATE claude_pro_batch SET status='running', started_at=NOW() WHERE id=%s",
            (job_id,)
        )
    db.commit()

finally:
    # Lock sofort freigeben — anderer Cron-Slot kann jetzt zweiten Job claimen
    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()
    if job is None:
        if db:
            db.close()
        sys.exit(0)

# ── Ab hier läuft diese Instanz unabhängig ─────────────────
try:
    # ── Deadline-Suffix für User-Prompt (System-Prompt bleibt stabil → Cache) ──
    tz = ZoneInfo('Europe/Berlin')
    now = datetime.now(tz)
    target = datetime.strptime(str(job['targetdate']), '%Y-%m-%d').replace(tzinfo=tz)
    hours_left = (target.replace(hour=23, minute=59) - now).total_seconds() / 3600
    if hours_left > 4:
        deadline_note = (
            f"\n\n---\n**Deadline:** {job['targetdate']} (noch ca. {int(hours_left)}h) – "
            "gründlich und kostensparend arbeiten."
        )
    else:
        deadline_note = (
            f"\n\n---\n**Deadline:** {job['targetdate']} (noch ca. {int(hours_left)}h) – "
            "zügig aber vollständig."
        )
    system_prompt = SYSTEM_PROMPT_BASE

    # ── Kontext-Blöcke voranstellen ───────────────────────
    # Identischer Inhalt über Jobs → Prompt-Cache-Hit ab 2. Job (~10% Input-Preis)
    context_blocks = []

    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT category, label, value
                FROM ki_localhost_cache
                ORDER BY category, label
            """)
            rows = cur.fetchall()
        if rows:
            lines = ['## Batch-Server localhost (ki_localhost_cache)', '']
            cur_cat = None
            for r in rows:
                if r['category'] != cur_cat:
                    cur_cat = r['category']
                    lines.append(f"\n### {cur_cat}")
                lines.append(f"- **{r['label']}**: {r['value']}")
            context_blocks.append('\n'.join(lines))
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: localhost-Cache Fehler: {e}",
              file=sys.stderr)

    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT ip_address, hostname, network_range, open_ports, services,
                       device_purpose, os_guess, mac_address
                FROM ki_infrastructure
                ORDER BY network_range, ip_address
            """)
            infra_rows = cur.fetchall()
        if infra_rows:
            lines = ['## Netzwerk-Infrastruktur (ki_infrastructure)', '']
            for r in infra_rows:
                parts = [f"**{r['ip_address']}**"]
                if r['hostname']:       parts.append(f"({r['hostname']})")
                if r['network_range']:  parts.append(f"[{r['network_range']}]")
                if r['device_purpose']: parts.append(f"→ {r['device_purpose']}")
                if r['open_ports']:     parts.append(f"| Ports: {r['open_ports']}")
                if r['services']:       parts.append(f"| Services: {r['services']}")
                if r['os_guess']:       parts.append(f"| OS: {r['os_guess']}")
                lines.append('  '.join(parts))
            context_blocks.append('\n'.join(lines))
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: Infra-Kontext Fehler: {e}",
              file=sys.stderr)

    if context_blocks:
        combined = '\n\n'.join(context_blocks)
        if len(combined) <= 12000:
            prompt = f"{combined}\n\n---\n{prompt}"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                  f"Kontext geladen ({len(combined)} Zeichen)", file=sys.stderr)
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                  f"Kontext übersprungen (zu groß: {len(combined)} Zeichen)", file=sys.stderr)

    # ── Deadline-Suffix anhängen (nach Kontext, vor Ausführung) ──────────────
    prompt = prompt + deadline_note

    # ── Session-Cache voranstellen wenn gewünscht ─────────
    if resume_session:
        with db.cursor() as cur:
            cur.execute("""
                SELECT JSON_UNQUOTE(JSON_EXTRACT(context_json, '$.summary')) AS summary
                FROM claude_context_cache
                WHERE scope='session-compact'
                LIMIT 1
            """)
            row = cur.fetchone()
        if row and row['summary'] and row['summary'] != 'NULL':
            cache_ctx = row['summary']
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                  f"Session-Cache geladen ({len(cache_ctx)} Bytes)", file=sys.stderr)
            prompt = f"{cache_ctx}\n\n---\nAufgabe:\n{prompt}"

    # ── Wochentracking ────────────────────────────────────
    def week_start():
        mez = ZoneInfo('Europe/Berlin')
        now = datetime.now(mez)
        days_since_friday = (now.weekday() - 4) % 7
        last_friday = now - timedelta(days=days_since_friday)
        reset = last_friday.replace(hour=8, minute=0, second=0, microsecond=0)
        if now < reset:
            reset -= timedelta(weeks=1)
        return reset.strftime('%Y-%m-%d %H:%M MEZ')

    def load_usage():
        week = week_start()
        if os.path.exists(USAGE_FILE):
            try:
                d = json.load(open(USAGE_FILE))
                if d.get('week_start') == week:
                    return (d.get('input_tokens', 0), d.get('output_tokens', 0),
                            d.get('cache_tokens', 0), d.get('cost_usd', 0.0),
                            d.get('tasks', 0))
            except Exception:
                pass
        return (0, 0, 0, 0.0, 0)

    def save_usage(in_tok, out_tok, cache_tok, cost, tasks):
        existing = {}
        if os.path.exists(USAGE_FILE):
            try:
                existing = json.load(open(USAGE_FILE))
            except Exception:
                pass
        data = {
            'week_start':    week_start(),
            'input_tokens':  in_tok,
            'output_tokens': out_tok,
            'cache_tokens':  cache_tok,
            'cost_usd':      cost,
            'tasks':         tasks,
            'last_run':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        for k in ('session_pct', 'usage_pct', 'pct_snapshot_at', 'session_reset', 'week_reset_raw'):
            if k in existing:
                data[k] = existing[k]
        json.dump(data, open(USAGE_FILE, 'w'), indent=2)

    pre_in, pre_out, pre_cache, pre_cost, pre_tasks = load_usage()

    # ── Modell ausführen ─────────────────────────────────
    or_model_id = None
    _mcfg = get_model_config(model, db)
    # Weiche nach provider, nicht nach Modellschluessel.
    #
    # Frueher stand hier "if model == 'LOCALP4'". Jedes andere lokale Modell
    # fiel dadurch bis zum Claude-CLI-Zweig durch, denn PROVIDER_CONFIG kennt
    # kein 'local' und der mittlere Zweig griff ebenfalls nicht. Job #35
    # (LOCAL30B, 28.08.2026) scheiterte so an der CLI statt am llama-server.
    if _mcfg and _mcfg.get('provider') == 'local':
        # ── Lokaler llama-server, Ziel aus llm_models.endpoint ────────
        #
        # Mit Werkzeugen, sofern poller_werkzeuge importierbar war: sonst
        # antwortet das Modell aus dem Gedaechtnis und erfindet im Zweifel
        # sogar Abrufe. Ein Testauftrag lieferte "konnte nicht erreicht
        # werden, kein HTTP-Statuscode", ohne dass je etwas abgerufen wurde.
        try:
            _url = local_url(_mcfg.get('endpoint'))
            _mid = _mcfg.get('model_id') or 'local'
            if WERKZEUGE_DA:
                _spur = []
                r = poller_werkzeuge.mit_werkzeugen(
                        prompt, system_prompt, _url, _mid, _spur.append)
                if _spur:
                    r['result'] = (r['result'] + "\n\n---\nNachgeschlagen:\n"
                                   + "\n".join(_spur))
            else:
                r = run_local(prompt, system_prompt, _url, _mid)
            result    = r['result']
            in_tok    = r['in_tok']
            out_tok   = r['out_tok']
            cache_tok = 0
            cost      = 0.0
            status    = 'done'
            error     = ''
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: Lokal OK ({in_tok}/{out_tok} tok)", file=sys.stderr)
        except Exception as exc:
            result    = str(exc)
            in_tok    = out_tok = cache_tok = 0
            cost      = 0.0
            status    = 'failed'
            error     = f'Lokal Fehler: {exc}'
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: {error}", file=sys.stderr)
    elif (mcfg := _mcfg) and mcfg['active'] and mcfg['provider'] in PROVIDER_CONFIG:
        # _mcfg wurde oben schon gelesen - kein zweiter Datenbankzugriff,
        # und beide Zweige urteilen garantiert ueber denselben Stand.
        # ── llm_models-getrieben: Routing + Preis dynamisch aus Tabelle ────────
        try:
            url, key = PROVIDER_CONFIG[mcfg['provider']]
            _ci = float(mcfg['cost_input_per_m'] or 0)
            _co = float(mcfg['cost_output_per_m'] or 0)
            r = run_openai_compatible(prompt, system_prompt, mcfg['model_id'], url, key, _ci, _co)
            result, in_tok, out_tok = r['result'], r['in_tok'], r['out_tok']
            cache_tok, cost = r['cache_tok'], r['cost']
            status, error = 'done', ''
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                  f"{mcfg['provider']} OK ({in_tok}/{out_tok} tok, {mcfg['model_id']}, ${cost})", file=sys.stderr)
        except Exception as exc:
            result = str(exc); in_tok = out_tok = cache_tok = 0; cost = 0.0
            status, error = 'failed', f"{mcfg['provider']} Fehler: {exc}"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: {error}", file=sys.stderr)
    else:
        # ── Claude CLI (sonnet / opus) ────────────────
        with tempfile.NamedTemporaryFile(prefix=f'claude_pro_{job_id}_',
                                         suffix='.json', delete=False) as tmp:
            tmp_path = tmp.name

        TIMEOUT_SEC = 4 * 3600  # 4 Stunden
        timed_out   = False

        try:
            with open(tmp_path, 'w') as out_f:
                try:
                    # Umgebung bereinigen: ANTHROPIC_BASE_URL zeigt sonst auf Ollama
                    clean_env = {k: v for k, v in os.environ.items()
                                 if k not in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN')}
                    proc = subprocess.run(
                        [
                            CLAUDE_BIN,
                            '--model', model,
                            '--effort', 'low',
                            '--max-budget-usd', '0.25',
                            '--dangerously-skip-permissions',
                            '--append-system-prompt', system_prompt,
                            '--output-format', 'json',
                            '-p', prompt,
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=out_f,
                        stderr=subprocess.STDOUT,
                        timeout=TIMEOUT_SEC,
                        env=clean_env,
                    )
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                          f"Timeout nach {TIMEOUT_SEC//3600}h — Prozess wird gekillt", file=sys.stderr)
                    timed_out = True
                    exit_code = -1

            raw = open(tmp_path).read()

            if timed_out:
                result    = f'Timeout: Job lief länger als {TIMEOUT_SEC//3600} Stunden.'
                in_tok    = out_tok = cache_tok = 0
                cost      = 0.0
                status    = 'failed'
                error     = f'Timeout nach {TIMEOUT_SEC//3600}h'
            elif exit_code != 0:
                first_line = raw.splitlines()[0] if raw.strip() else '(keine Ausgabe)'
                result    = raw
                in_tok    = out_tok = cache_tok = 0
                cost      = 0.0
                status    = 'failed'
                error     = f'Exit-Code {exit_code}: {first_line}'
            else:
                try:
                    d         = json.loads(raw)
                    u         = d.get('usage', {})
                    result    = d.get('result', '')
                    in_tok    = u.get('input_tokens', 0)
                    out_tok   = u.get('output_tokens', 0)
                    cache_tok = (u.get('cache_creation_input_tokens', 0)
                                 + u.get('cache_read_input_tokens', 0))
                    cost      = round(d.get('total_cost_usd', 0.0), 6)
                    status    = 'done'
                    error     = ''
                except json.JSONDecodeError as e:
                    first_line = raw.splitlines()[0] if raw.strip() else '(keine Ausgabe)'
                    result    = raw
                    in_tok    = out_tok = cache_tok = 0
                    cost      = 0.0
                    status    = 'failed'
                    error     = f'Kein gültiges JSON (Exit 0): {first_line} — {e}'
        finally:
            os.unlink(tmp_path)

    # ── Abbruch prüfen (Kill-Button während Laufzeit) ─────
    with db.cursor() as cur:
        cur.execute("SELECT status FROM claude_pro_batch WHERE id=%s", (job_id,))
        row = cur.fetchone()
    if row and row['status'] == 'failed':
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id} wurde während der Laufzeit abgebrochen.",
              file=sys.stderr)
        sys.exit(0)

    # ── Eskalation: Bedenken erkannt → neu einreihen mit Sonnet ──
    ESCALATION_PHRASES = [
        'ich kann nicht', 'ich kann bei diesem', 'ich bin nicht in der lage',
        'bevor ich', 'muss ich bestät', 'sicherheitsbedenken', 'sicherheitshinweis',
        'i cannot', "i can't", 'i am unable', 'i need to confirm', 'i must verify',
        'before i', 'safety concern', 'i should not', 'ich sollte nicht',
        'ich darf nicht', 'nicht autorisiert', 'nicht berechtigt',
    ]
    escalate = (
        status == 'done'
        and or_model_id is None
        and any(p in result.lower() for p in ESCALATION_PHRASES)
    )
    if escalate:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO claude_pro_batch (targetdate, model, resume_session, prompt)
                SELECT targetdate, 'sonnet', resume_session, prompt
                FROM claude_pro_batch WHERE id=%s
            """, (job_id,))
            new_id = cur.lastrowid
        db.commit()
        error  = f'Eskaliert zu Sonnet → Job #{new_id} (Bedenken erkannt)'
        status = 'failed'
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: Bedenken erkannt → "
              f"eskaliert zu Sonnet als Job #{new_id}", file=sys.stderr)

    # ── Wochentracking speichern ──────────────────────────
    save_usage(
        pre_in    + in_tok,
        pre_out   + out_tok,
        pre_cache + cache_tok,
        round(pre_cost + cost, 6),
        pre_tasks + 1,
    )

    # ── Ergebnis in DB schreiben ──────────────────────────
    with db.cursor() as cur:
        cur.execute("""
            UPDATE claude_pro_batch SET
                status        = %s,
                result        = %s,
                input_tokens  = %s,
                output_tokens = %s,
                cache_tokens  = %s,
                cost_usd      = %s,
                finished_at   = NOW(),
                error_msg     = %s
            WHERE id = %s
        """, (status, result, in_tok, out_tok, cache_tok, cost, error, job_id))
    db.commit()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id} → {status} "
          f"({in_tok}/{out_tok} tok, ${cost})", file=sys.stderr)

    # ── OpenRouter Guthaben abrufen und speichern ─────────
    if OPENROUTER_KEY:
        try:
            req = urllib.request.Request(
                OPENROUTER_CREDITS,
                headers={'Authorization': f'Bearer {OPENROUTER_KEY}'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                cr        = json.loads(resp.read())['data']
            total     = float(cr.get('total_credits', 0))
            used      = float(cr.get('total_usage', 0))
            remaining = round(total - used, 6)
            bal_str   = f"${remaining:.4f} (von ${total:.2f} total, ${used:.6f} verbraucht)"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] OpenRouter Guthaben: {bal_str}",
                  file=sys.stderr)
            # In ki_localhost_cache persistieren
            with db.cursor() as cur:
                for label, val in [
                    ('balance_usd',       f"{remaining:.6f}"),
                    ('total_credits_usd', f"{total:.2f}"),
                    ('total_usage_usd',   f"{used:.6f}"),
                    ('last_job_id',       str(job_id)),
                    ('last_updated',      datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                ]:
                    cur.execute("""
                        INSERT INTO ki_localhost_cache (category, label, value)
                        VALUES ('openrouter', %s, %s)
                        ON DUPLICATE KEY UPDATE value=%s, updated_at=NOW()
                    """, (label, val, val))
            db.commit()
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] OpenRouter Guthaben Fehler: {e}",
                  file=sys.stderr)


    # ── Ergebnis via MQTT publizieren ────────────────────
    try:
        import paho.mqtt.client as _mqtt
        _mc = _mqtt.Client(_mqtt.CallbackAPIVersion.VERSION2,
                           client_id=f'ki-poller-{job_id}', clean_session=True)
        _mc.connect('192.168.178.218', 1883, keepalive=10)
        _payload = json.dumps({
            'id':       job_id,
            'status':   status,
            'model':    model,
            'result':   result[:4000] if result else '',
            'cost_usd': cost,
        })
        _mc.publish(f'ki/job/result/{job_id}', _payload, qos=1)
        _mc.publish('ki/job/result', _payload, qos=1)
        _mc.disconnect()
    except Exception as _e:
        pass  # MQTT optional — Job-Ergebnis ist bereits in DB

    # ── Session-Compact Cache aktualisieren ───────────────
    subprocess.run(
        ['python3', '/home/gh/cache-saver.py', '--compact'],
        stdout=open('/tmp/cache-saver.log', 'a'),
        stderr=subprocess.STDOUT,
    )

finally:
    db.close()

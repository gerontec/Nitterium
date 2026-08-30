#!/usr/bin/env python3
"""
durchsatz_optimum.py - sucht die beste Thread/Slot-Einstellung fuer den
Analyse-Server auf dem Dell.

Anlass: sar zeigte 77 Prozent Leerlauf bei 20 CPUs, waehrend llama-server im
Mittel nur gut drei Threads zog. Beim 7B-Modell war Aufdrehen sinnlos, weil es
an der Speicherbandbreite hing. Beim MoE-Modell rechnet je Token nur ein
Bruchteil der Gewichte - das Verhaeltnis von Rechenarbeit zu Speicherzugriff
ist ein anderes, also lohnt die Messung neu.

Zwei Stufen statt aller Kombinationen, das spart zwei Drittel der Zeit:
  1. Slots fest, Threads variieren  -> beste Threadzahl
  2. Beste Threadzahl fest, Slots variieren -> bestes Gespann

Waehrend der Messung werden Cron und Chat-Konsole angehalten, sonst rechnet
jemand mit. Am Ende wird alles wiederhergestellt und die beste Einstellung
dauerhaft in die Unit geschrieben - ausser bei --nur-messen.

    python3 durchsatz_optimum.py
    python3 durchsatz_optimum.py --posts 20 --nur-messen
"""

import argparse
import concurrent.futures
import configparser
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import pymysql

DELL = "gh@10.9.0.6"
PORT = 8081
LLM_URL = f"http://10.9.0.6:{PORT}/v1/chat/completions"
MODELL_DATEI = "/home/gh/models/qwen3-30b-a3b-q6k.gguf"
UNIT = "/etc/systemd/system/llama-analyze.service"

THREAD_STUFEN = [8, 12, 16, 20]
SLOT_STUFEN = [4, 8, 12, 16]
CTX_JE_SLOT = 2048        # Analyseanfragen liegen bei 600-900 Token

THEMEN = ["Ukraine-Krieg", "Nahost/Iran", "USA-Politik", "Deutschland-Innenpolitik",
          "EU-Politik", "Migration", "Energie/Wirtschaft", "Medien/Presse",
          "Corona/Gesundheit", "Klima", "Sonstiges"]
HALTUNGEN = ["zustimmend", "ablehnend", "neutral berichtend", "spoettisch", "fragend"]
PROMPT = """Du analysierst einen deutschsprachigen Tweet. Antworte NUR mit einem
JSON-Objekt, alle Werte auf Deutsch, keine Erklaerung davor oder danach.

Felder:
  thema: genau eines aus {themen}
  akteure: bis zu drei genannte Personen oder Organisationen, kommagetrennt, sonst ""
  quelle: genannte Quelle (Medium, Behoerde, Person) oder ""
  haltung: genau eines aus {haltungen}
  zusammenfassung: ein deutscher Satz, hoechstens 12 Woerter

Tweet:
{text}"""


def zugang() -> dict:
    c = configparser.ConfigParser()
    c.read("/etc/heissa-db.ini")
    a = c["wagodb"] if c.has_section("wagodb") else {}
    return dict(unix_socket=a.get("socket", "/var/run/mysqld/mysqld.sock"),
                user=a.get("user", "gh"),
                password=os.environ.get("WAGODB_PASSWORD") or a.get("password", ""),
                database=a.get("database", "wagodb"), charset="utf8mb4")


def stichprobe(n: int) -> list[str]:
    conn = pymysql.connect(**zugang())
    with conn.cursor() as cur:
        cur.execute("""SELECT title, content FROM nitter_posts
                        WHERE CHAR_LENGTH(CONCAT(title, content)) > 120
                        ORDER BY MD5(CONCAT(post_id, 'durchsatz2026')) LIMIT %s""", (n,))
        zeilen = cur.fetchall()
    conn.close()
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", f"{a} {b}")).strip()[:900]
            for a, b in zeilen]


def ssh(befehl: str) -> str:
    """Auf dem Dell ausfuehren."""
    return subprocess.run(["ssh", DELL, befehl], capture_output=True,
                          text=True, timeout=180).stdout.strip()


def lokal(befehl: str) -> str:
    """Auf heissa.de ausfuehren - hier laeuft das Skript."""
    return subprocess.run(["bash", "-c", befehl], capture_output=True,
                          text=True, timeout=180).stdout.strip()


def server_start(threads: int, slots: int) -> None:
    ssh("sudo systemctl stop llama-analyze")
    ssh("pkill -f 'llama-server .*--port 8081' ; true")
    time.sleep(2)
    ssh(f"nohup /home/gh/llama.cpp/build/bin/llama-server "
        f"--model {MODELL_DATEI} --n-gpu-layers 0 --threads {threads} "
        f"--ctx-size {slots * CTX_JE_SLOT} --parallel {slots} "
        f"--host 0.0.0.0 --port {PORT} --no-warmup > /tmp/sweep.log 2>&1 &")


def bereit(sekunden: int = 420) -> bool:
    ende = time.time() + sekunden
    while time.time() < ende:
        try:
            with urllib.request.urlopen(
                    f"http://10.9.0.6:{PORT}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(4)
    return False


def eine_anfrage(text: str) -> bool:
    body = json.dumps({
        "messages": [{"role": "user", "content": PROMPT.format(
            themen=", ".join(THEMEN), haltungen=", ".join(HALTUNGEN), text=text)}],
        "max_tokens": 140, "temperature": 0.1,
        "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(LLM_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return bool(json.load(r)["choices"][0]["message"]["content"])
    except Exception:
        return False


def messen(posts: list[str], workers: int) -> tuple[float, int]:
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        gut = sum(1 for ok in pool.map(eine_anfrage, posts) if ok)
    dauer = time.time() - t0
    return len(posts) / dauer if dauer else 0.0, gut


def lauf(threads: int, slots: int, posts: list[str]) -> dict | None:
    print(f"  {threads:>2} Threads, {slots:>2} Slots ... ", end="", flush=True)
    t0 = time.time()
    server_start(threads, slots)
    if not bereit():
        print("Server kam nicht hoch")
        return None
    laden = time.time() - t0
    rate, gut = messen(posts, slots)
    print(f"{rate:5.3f} Posts/s   ({gut}/{len(posts)} ok, "
          f"Laden {laden:.0f} s)")
    return {"threads": threads, "slots": slots, "rate": rate, "ok": gut}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", type=int, default=24)
    ap.add_argument("--nur-messen", action="store_true",
                    help="Ergebnis nicht in die Unit schreiben")
    args = ap.parse_args()

    posts = stichprobe(args.posts)
    if not posts:
        print("keine Posts gefunden", file=sys.stderr)
        return 1
    print(f"Stichprobe: {len(posts)} Posts, Modell Qwen3-30B-A3B\n")

    # Mitrechner anhalten - sonst misst man deren Last mit.
    #
    # Achtung: dieses Skript laeuft AUF heissa.de. Der erste Anlauf rief hier
    # "ssh gh@heissa.de ..." auf, also die Maschine zu sich selbst - das schlug
    # still fehl, der Analyzer lief weiter und die Messung war wertlos.
    # heissa-Seite darum lokal, nur der Dell wird ueber ssh angesprochen.
    print("Cron anhalten und laufende Analyzer beenden ...")
    lokal("crontab -l | sed 's|^\\(\\*/15 .*content_analyzer.*\\)$|#SWEEP \\1|' | crontab -")
    lokal("pkill -f '[c]ontent_analyzer.py' ; true")
    time.sleep(3)
    noch = lokal("pgrep -f '[c]ontent_analyzer.py' | wc -l")
    print(f"  laufende Analyzer: {noch or '0'}")
    print(f"  Cron pausiert: "
          f"{'ja' if '#SWEEP' in lokal('crontab -l') else 'NEIN - Messung waere verfaelscht!'}")

    ergebnisse: list[dict] = []
    try:
        print("\n=== Stufe 1: Threads (4 Slots fest) ===")
        for t in THREAD_STUFEN:
            r = lauf(t, 4, posts)
            if r:
                ergebnisse.append(r)
        if not ergebnisse:
            print("nichts gemessen", file=sys.stderr)
            return 1
        beste_threads = max(ergebnisse, key=lambda r: r["rate"])["threads"]
        print(f"  -> beste Threadzahl: {beste_threads}")

        print(f"\n=== Stufe 2: Slots ({beste_threads} Threads fest) ===")
        for s in SLOT_STUFEN:
            if s == 4:
                continue                    # in Stufe 1 schon gemessen
            r = lauf(beste_threads, s, posts)
            if r:
                ergebnisse.append(r)
    finally:
        # Auch wenn unterwegs etwas schiefgeht: der Dienst darf nicht
        # abgeschaltet zurueckbleiben, sonst steht die Analyse still.
        print("\nDienst und Cron wieder einschalten ...")
        ssh("pkill -f 'llama-server .*--port 8081' ; true")
        ssh("sudo systemctl start llama-analyze")
        lokal("crontab -l | sed 's|^#SWEEP ||' | crontab -")

    ergebnisse.sort(key=lambda r: -r["rate"])
    print("\n" + "=" * 52)
    print(f"{'Threads':>8} {'Slots':>6} {'Posts/s':>10} {'ggue. jetzt':>12}")
    print("-" * 52)
    jetzt = next((r["rate"] for r in ergebnisse
                  if r["threads"] == 8 and r["slots"] == 4), None)
    for r in ergebnisse:
        v = f"{(r['rate']/jetzt - 1)*100:+7.0f} %" if jetzt else "     --"
        print(f"{r['threads']:>8} {r['slots']:>6} {r['rate']:>10.3f} {v:>12}")
    print("=" * 52)

    b = ergebnisse[0]
    offen = 270000
    print(f"\nBeste Einstellung: {b['threads']} Threads, {b['slots']} Slots "
          f"= {b['rate']:.3f} Posts/s")
    if b["rate"] > 0:
        print(f"270000 offene Posts entsprechend {offen/b['rate']/86400:.1f} Tagen"
              + (f" statt {offen/jetzt/86400:.1f}" if jetzt else ""))

    if args.nur_messen:
        print("\n--nur-messen: Unit bleibt unveraendert.")
        ssh("sudo systemctl restart llama-analyze")
        return 0

    print("\nUnit anpassen ...")
    ssh(f"sudo sed -i -E "
        f"'s/--threads [0-9]+/--threads {b['threads']}/; "
        f"s/--ctx-size [0-9]+/--ctx-size {b['slots']*CTX_JE_SLOT}/; "
        f"s/--parallel [0-9]+/--parallel {b['slots']}/' {UNIT}")
    ssh("sudo systemctl daemon-reload && sudo systemctl restart llama-analyze")
    time.sleep(8)
    print("llama-analyze:", ssh("systemctl is-active llama-analyze"))
    print(ssh(f"grep -E 'threads|parallel' {UNIT}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

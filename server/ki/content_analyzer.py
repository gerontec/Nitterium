#!/usr/bin/env python3
"""
content_analyzer.py – Inhaltsanalyse der Posts mit dem lokalen LLM.

Loest die reine Sentiment-Auswertung ab: statt Tonfall (positiv/negativ) wird
je Post Thema, Akteure, Quelle, Haltung und eine Ein-Satz-Zusammenfassung
erfasst. Das Modell laeuft auf der Tesla P4 in dell-3660 und ist ueber den
WireGuard-Tunnel erreichbar (10.9.0.6:8080, llama.cpp, Mistral 7B).

  python3 content_analyzer.py                      # alles Offene, alle Accounts
  python3 content_analyzer.py --account ZentraleV  # nur dieser Account
  python3 content_analyzer.py --limit 50           # Teilmenge
  python3 content_analyzer.py --stats              # nur auswerten, nichts rechnen

Die GPU teilt sich das Geraet mit den Kamera-Daemons. Fuer grosse Laeufe lohnt
sich `~/python/cam_remote.sh stop` davor und `start` danach - dann liegt das
Modell komplett im VRAM und der Durchsatz vervierfacht sich.
"""

import argparse
import concurrent.futures
import fcntl
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pymysql

def _zugang() -> dict:
    """Zugangsdaten aus /etc/heissa-db.ini statt im Quelltext - das Repo ist
    oeffentlich, und ein einmal gepushtes Passwort bekommt man aus der
    Git-Historie kaum wieder heraus. WAGODB_PASSWORD sticht die Datei."""
    import configparser
    import os
    c = configparser.ConfigParser()
    c.read("/etc/heissa-db.ini")
    a = c["wagodb"] if c.has_section("wagodb") else {}
    return dict(unix_socket=a.get("socket", "/var/run/mysqld/mysqld.sock"),
                user=a.get("user", "gh"),
                password=os.environ.get("WAGODB_PASSWORD") or a.get("password", ""),
                database=a.get("database", "wagodb"), charset="utf8mb4")


DB = _zugang()
LLM_URL   = "http://10.9.0.6:8081/v1/chat/completions"   # CPU-Dienst; GPU: Port 8080
LOG_FILE  = Path("/home/gh/python/content_analyzer.log")
LOCK_FILE = Path("/home/gh/python/content_analyzer.lock")


def _modellname() -> str:
    """Fragt den llama-server, was er gerade ausliefert.

    Frueher stand hier eine Konstante. Als der Dienst am 28.08.2026 von
    Mistral auf Qwen umgestellt wurde, schrieb der Analyzer weiter
    "mistral-7b-q4_k_m" in die Datenbank - die Herkunftsangabe war falsch,
    ohne dass es jemand bemerkt haette. Jetzt wird sie gelesen.

    Startet der Dienst gerade neu, ist er fuer ein paar Sekunden weg. Ein
    einzelner Versuch schrieb dann "unbekannt" in die Datenbank - am
    29.08.2026 traf das sechs Zeilen, deren Analyse voellig in Ordnung war.
    Darum drei Versuche, und als letzte Rueckfalloption der zuletzt
    verwendete Name aus der Tabelle statt eines Platzhalters."""
    basis = LLM_URL.rsplit("/v1/", 1)[0]
    for versuch in range(3):
        try:
            with urllib.request.urlopen(basis + "/v1/models", timeout=15) as r:
                kennung = (json.load(r).get("data") or [{}])[0].get("id", "")
            name = kennung.split("/")[-1].removesuffix(".gguf").strip()
            if name:
                return name[:60]
        except Exception:
            if versuch < 2:
                time.sleep(5)

    # Der Dienst antwortet nicht. Dann scheitert ohnehin jede Analyse und es
    # wird nichts geschrieben - fuer den Grenzfall aber lieber der letzte
    # bekannte Name als ein Platzhalter, der spaeter im Bericht auftaucht.
    try:
        conn = pymysql.connect(**DB)
        with conn.cursor() as cur:
            cur.execute("SELECT modell FROM nitter_content "
                        "WHERE modell IS NOT NULL AND modell <> '' "
                        "ORDER BY analyzed_at DESC LIMIT 1")
            zeile = cur.fetchone()
        conn.close()
        if zeile and zeile[0]:
            return str(zeile[0])[:60]
    except Exception:
        pass
    return "unbekannt"


MODELL = _modellname()

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

log = logging.getLogger("content")


def setup_log() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_FILE)],
    )


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nitter_content (
                post_id        VARCHAR(200) NOT NULL PRIMARY KEY,
                account        VARCHAR(100) NOT NULL,
                thema          VARCHAR(60)  NULL,
                akteure        VARCHAR(255) NULL,
                quelle         VARCHAR(255) NULL,
                haltung        VARCHAR(40)  NULL,
                zusammenfassung TEXT        NULL,
                modell         VARCHAR(60)  NULL,
                analyzed_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_account (account),
                INDEX idx_thema   (thema),
                INDEX idx_haltung (haltung)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
    conn.commit()


def pending(conn, account: str | None, limit: int) -> list[tuple]:
    sql = """SELECT p.post_id, p.account, p.title, p.content
               FROM nitter_posts p
          LEFT JOIN nitter_content c ON c.post_id = p.post_id
              WHERE c.post_id IS NULL
                AND (p.title <> '' OR p.content <> '')"""
    params: list = []
    if account:
        sql += " AND p.account = %s"
        params.append(account)
    sql += " ORDER BY p.published_at DESC"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def ask_llm(text: str, timeout: int = 90) -> dict | None:
    body = json.dumps({
        "messages": [{"role": "user", "content": PROMPT.format(
            themen=", ".join(THEMEN), haltungen=", ".join(HALTUNGEN),
            text=text[:900])}],
        "max_tokens": 140,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(LLM_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            answer = json.load(resp)["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
        log.warning(f"LLM nicht erreichbar oder unbrauchbar: {e}")
        return None

    # Das Modell haengt gelegentlich Text an das JSON - erstes Objekt herausschneiden
    m = re.search(r"\{.*?\}", answer, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def normalise(d: dict) -> dict:
    def pick(value, allowed, fallback):
        v = str(value or "").strip()
        for a in allowed:
            if a.lower() == v.lower():
                return a
        for a in allowed:                      # Teiltreffer, z. B. "Ukraine"
            if a.split("-")[0].split("/")[0].lower() in v.lower():
                return a
        return fallback

    return {
        "thema":   pick(d.get("thema"), THEMEN, "Sonstiges"),
        "haltung": pick(d.get("haltung"), HALTUNGEN, "neutral berichtend"),
        "akteure": str(d.get("akteure") or "")[:255],
        "quelle":  str(d.get("quelle") or "")[:255],
        "zusammenfassung": str(d.get("zusammenfassung") or "")[:1000],
    }


def save(conn, post_id: str, account: str, res: dict, versuche: int = 3) -> None:
    """Schreibt ein Ergebnis. MariaDB wirft unter Last gelegentlich 1020
    ("Record has changed") - dann kurz warten und erneut versuchen."""
    for versuch in range(1, versuche + 1):
        try:
            _save_once(conn, post_id, account, res)
            return
        except pymysql.err.OperationalError as e:
            if versuch == versuche:
                log.warning(f"speichern fehlgeschlagen fuer {post_id}: {e}")
                return
            time.sleep(0.5 * versuch)


def _save_once(conn, post_id: str, account: str, res: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO nitter_content
                       (post_id, account, thema, akteure, quelle, haltung,
                        zusammenfassung, modell)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE thema=VALUES(thema),
                            akteure=VALUES(akteure), quelle=VALUES(quelle),
                            haltung=VALUES(haltung),
                            zusammenfassung=VALUES(zusammenfassung),
                            modell=VALUES(modell)""",
                    (post_id, account, res["thema"], res["akteure"], res["quelle"],
                     res["haltung"], res["zusammenfassung"], MODELL))
    conn.commit()


def show_stats(conn, account: str | None) -> None:
    where, params = ("WHERE account = %s", [account]) if account else ("", [])
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM nitter_content {where}", params)
        total = cur.fetchone()[0]
        print(f"Analysiert: {total} Posts" + (f" von @{account}" if account else ""))
        for feld in ("thema", "haltung"):
            cur.execute(f"""SELECT {feld}, COUNT(*) n FROM nitter_content {where}
                            GROUP BY {feld} ORDER BY n DESC LIMIT 12""", params)
            print(f"\n{feld.capitalize()}:")
            for wert, n in cur.fetchall():
                anteil = 100 * n / total if total else 0
                print(f"  {n:>6}  {anteil:>5.1f}%  {wert}")
        cur.execute(f"""SELECT quelle, COUNT(*) n FROM nitter_content {where}
                        {'AND' if where else 'WHERE'} quelle <> ''
                        GROUP BY quelle ORDER BY n DESC LIMIT 12""", params)
        print("\nMeistgenannte Quellen:")
        for q, n in cur.fetchall():
            print(f"  {n:>6}  {q}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account")
    ap.add_argument("--limit", type=int, default=0, help="hoechstens so viele Posts")
    ap.add_argument("--stats", action="store_true", help="nur Auswertung zeigen")
    ap.add_argument("--llm-url", default=None,
                    help="Endpunkt; 8081 = CPU-Dienst, 8080 = GPU (gedrosselt)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallele Anfragen; llama-server haelt 4 Slots (Standard 4)")
    args = ap.parse_args()

    setup_log()
    if args.llm_url:
        globals()["LLM_URL"] = args.llm_url

    conn = pymysql.connect(**DB)
    ensure_table(conn)

    # Auswerten geht immer, auch waehrend ein Lauf schreibt
    if args.stats:
        show_stats(conn, args.account)
        return

    # Einmal-Sperre: zwei gleichzeitige Laeufe schreiben sonst dieselben Zeilen
    lock = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Ein anderer Lauf ist noch aktiv – nichts zu tun.")
        return

    todo = pending(conn, args.account, args.limit)
    if not todo:
        log.info("Keine offenen Posts – fertig.")
        return

    log.info(f"{len(todo)} Posts zu analysieren (Modell {MODELL} auf 10.9.0.6, "
             f"{args.workers} parallel)")
    t0, ok, fail, done = time.time(), 0, 0, 0

    def arbeite(zeile):
        post_id, account, title, content = zeile
        text = strip_html(title) or strip_html(content)
        if not text:
            return post_id, account, None
        return post_id, account, ask_llm(text)

    # Frueher stand hier eine Notbremse: nach 20 Fehlschlaegen hintereinander
    # brach der Lauf ab, weil ein kurz abwesender llama-server sonst 400 Posts
    # in acht Sekunden als Fehlschlag verbucht. Am 29.08.2026 wieder ausgebaut.
    # Sie hat mehr geschadet als genutzt: jeder Neustart des Modelldienstes
    # beendete den laufenden Analyzer, was unter anderem eine Messreihe
    # wertlos machte. Verloren geht ohne sie nichts - fehlgeschlagene Posts
    # werden nicht in nitter_content geschrieben und beim naechsten Lauf
    # erneut geholt; es kostet nur Leerlauf, solange das Modell weg ist.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for post_id, account, raw in pool.map(arbeite, todo):
            done += 1
            if raw is None:
                fail += 1
            else:
                save(conn, post_id, account, normalise(raw))
                ok += 1
            if done % 25 == 0 or done == len(todo):
                tempo = ok / max(time.time() - t0, 1)
                rest  = (len(todo) - done) / max(tempo, 0.01) / 60
                log.info(f"  {done}/{len(todo)}  ({100*done/len(todo):.1f}%)  "
                         f"{tempo:.2f} Posts/s  noch ~{rest:.0f} min  Fehler={fail}")

    log.info(f"=== Fertig: {ok} analysiert, {fail} Fehlschlaege, "
             f"{(time.time()-t0)/60:.1f} min ===")
    conn.close()


if __name__ == "__main__":
    main()

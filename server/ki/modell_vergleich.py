#!/usr/bin/env python3
"""
modell_vergleich.py - stellt zwei lokale LLMs an derselben Stichprobe gegenueber.

Gemessen wird Tempo UND Verlaesslichkeit. Einen Goldstandard fuer die
Inhaltsanalyse gibt es nicht, deshalb wird geprueft, was sich ohne
menschliches Urteil objektiv feststellen laesst - und dort, wo eine richtige
Antwort bekannt ist (Kontrollsaetze, Rechtsfragen), wird sie hart gegengerechnet.

  Stufe 1  Inhalt   Thema, Akteure, Quelle, Haltung wie im Produktivbetrieb
  Stufe 2  KIVI     Vorpruefung nach Art der Landesmedienanstalten
  Stufe 3  Wissen   deutsches Medienrecht aus dem Training

  python3 modell_vergleich.py                  # alles, 30 Posts
  python3 modell_vergleich.py --posts 60
  python3 modell_vergleich.py --stufen inhalt  # nur eine Stufe
  python3 modell_vergleich.py --modelle qwen3-30b-a3b

Der Analyse-Dienst wird angehalten und am Ende wieder gestartet. Den Cron auf
heissa.de bitte vorher pausieren, sonst laufen dessen Anfragen ins Leere.
"""

import argparse
import collections
import concurrent.futures
import json
import math
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pymysql

def _zugang() -> dict:
    """Zugangsdaten aus /etc/heissa-db.ini, nicht aus dem Quelltext - siehe
    content_analyzer.py. WAGODB_PASSWORD sticht die Datei."""
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
DELL = "gh@10.9.0.6"
PORT = 8081                      # derselbe Port wie der Dienst, damit die
                                 # nftables-Regel unveraendert bleiben kann
LLM_URL = f"http://10.9.0.6:{PORT}/v1/chat/completions"

MODELLE = {
    "mistral-7b":    "/home/gh/models/mistral-7b-q4_k_m.gguf",
    "qwen3-30b-a3b": "/home/gh/models/qwen3-30b-a3b-q6k.gguf",
}

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

DEUTSCH = {"der", "die", "das", "und", "in", "von", "zu", "den", "mit", "auf",
           "fuer", "für", "ist", "im", "dem", "nicht", "ein", "eine", "als",
           "auch", "es", "an", "werden", "aus", "er", "hat", "dass", "sie",
           "nach", "wird", "bei", "einer", "um", "am", "sind", "noch", "wie",
           "einem", "ueber", "über", "einen", "so", "zum", "haben", "nur",
           "oder", "aber", "vor", "zur", "gegen", "vom", "kein", "keine"}
ENGLISCH = {"the", "and", "of", "to", "is", "in", "that", "for", "with", "on",
            "are", "this", "was", "has", "his", "her", "their", "about",
            "criticizes", "reports", "says", "claims", "against"}


def _roh_frage(inhalt: str, timeout: int = 180, max_tokens: int = 140,
               json_modus: bool = True) -> str | None:
    body = {"messages": [{"role": "user", "content": inhalt}],
            "max_tokens": max_tokens, "temperature": 0.1}
    if json_modus:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(LLM_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)["choices"][0]["message"]["content"]
    except Exception:
        return None


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()

# --- KIVI-Stufe -------------------------------------------------------------
# Nachbau der Arbeitsweise, die die Landesmedienanstalten mit ihrem Tool KIVI
# einsetzen: die Maschine faellt kein Urteil, sie bereitet die menschliche
# Pruefung vor. Sie markiert einen Verdacht, nennt die Kategorie und - das ist
# der entscheidende Teil - die woertliche Stelle, an der der Verdacht haengt.
#
# Oeffentlich dokumentiert ist auch die Schwaeche des Verfahrens: KIVI stolpert
# ueber historischen Kontext und Satire, und es meldete eine Presseerklaerung
# des Zentralrats der Muslime, die Terroranschlaege verurteilte, allein weil
# "Terror" und "Islam" zusammen vorkamen. Genau daran wird hier gemessen:
# ein groesseres Modell muss den Unterschied zwischen "sagt es" und "berichtet
# darueber" bzw. "verurteilt es" erkennen.

KATEGORIEN = [
    "Volksverhetzung/Menschenwuerde",
    "Kennzeichen verfassungswidriger Organisationen",
    "Gewaltdarstellung",
    "Pornografie/Jugendschutz",
    "Beleidigung/Ehrverletzung",
    "Desinformation",
    "kein Verdacht",
]
STUFEN  = ["keiner", "niedrig", "mittel", "hoch"]
KONTEXT = ["eigene Aussage", "berichtet darueber", "verurteilt es",
           "Satire/Ironie", "Zitat"]

PROMPT_KIVI = """Du bist die automatische Vorpruefung einer Medienaufsicht.
Du faellst KEIN Urteil und triffst keine rechtliche Entscheidung - du bereitest
nur die Pruefung durch einen Menschen vor. Antworte NUR mit einem JSON-Objekt.

Wichtig: Ein Beitrag, der ueber einen Verstoss BERICHTET, ihn ZITIERT oder ihn
VERURTEILT, ist selbst kein Verstoss. Pruefe zuerst, wer hier spricht.

Felder:
  kategorie: genau eines aus {kategorien}
  verdacht: genau eines aus {stufen}
  beleg: die woertliche Textstelle aus dem Beitrag, die den Verdacht ausloest,
         unveraendert kopiert; wenn kein Verdacht, dann ""
  kontext: genau eines aus {kontext}
  begruendung: ein deutscher Satz, hoechstens 15 Woerter

Beitrag:
{text}"""

# Kontrollsaetze mit bekannter richtiger Antwort. Die Haelfte ist bewusst so
# gebaut, dass eine Stichwortsuche anschlaegt, die Aussage aber harmlos ist -
# das ist der Zentralrat-Fall. "erwartet" ist die Stufe, die hoechstens
# herauskommen darf.
KONTROLLE = [
    # (Text, erwartete Hoechststufe, erwarteter Kontext, Notiz)
    ("Der Zentralrat der Muslime verurteilt den islamistischen Terroranschlag "
     "von gestern aufs Schaerfste und spricht den Opfern sein Beileid aus.",
     "keiner", "verurteilt es", "Verurteilung mit Reizwoertern"),
    ("Die Ausstellung zeigt Hakenkreuz-Fahnen aus dem Reichsparteitagsgelaende "
     "und dokumentiert die Verbrechen des NS-Regimes fuer Schulklassen.",
     "keiner", "berichtet darueber", "Bildung, Sozialadaequanz §86 Abs. 4"),
    ("Die Polizei meldet, ein Mann habe im Netz zur Gewalt gegen Gefluechtete "
     "aufgerufen; die Staatsanwaltschaft ermittelt wegen Volksverhetzung.",
     "keiner", "berichtet darueber", "Bericht ueber eine Straftat"),
    ("Historiker streiten darueber, wie viele Opfer der Holocaust in Ungarn "
     "forderte; neue Archivfunde erlauben genauere Schaetzungen.",
     "keiner", "berichtet darueber", "historische Forschung"),
    ("Klar, die Erde ist eine Scheibe und der Kanzler ist ein Reptiloid. "
     "Wer das nicht glaubt, ist bestimmt gekauft. /s",
     "niedrig", "Satire/Ironie", "erkennbare Ironie"),
    ("Ein Sprecher der Partei sagte woertlich: 'Diese Leute gehoeren hier "
     "nicht her.' Wir dokumentieren die Aussage im vollen Wortlaut.",
     "niedrig", "Zitat", "Zitat mit Distanzierung"),
    ("Diese Leute gehoeren hier nicht her und muessen alle raus, notfalls "
     "mit Gewalt. Wir holen uns unser Land zurueck.",
     "hoch", "eigene Aussage", "eigene Aussage, Gewaltaufruf"),
    ("Du bist der duemmste Vollidiot, den dieses Parlament je gesehen hat, "
     "und gehoerst weggesperrt.",
     "mittel", "eigene Aussage", "Beleidigung"),
    ("Impfstoffe enthalten nachweislich Mikrochips zur Fernsteuerung, das "
     "hat ein Insider bestaetigt. Teilt das, bevor es geloescht wird!",
     "mittel", "eigene Aussage", "Desinformation"),
    ("Der Bundestag hat gestern das Heizungsgesetz mit 399 zu 275 Stimmen "
     "beschlossen; die Opposition kuendigte eine Klage an.",
     "keiner", "berichtet darueber", "reine Nachricht"),
    ("Die Studie der Universitaet Mainz kommt zu dem Ergebnis, dass "
     "Desinformation vor allem ueber private Gruppenchats verbreitet wird.",
     "keiner", "berichtet darueber", "Reizwort im Fachkontext"),
    ("Endlich sagt es mal jemand: Bei so viel Kompetenz in der Regierung "
     "wundert mich gar nichts mehr. Grossartige Leistung, wirklich.",
     "niedrig", "Satire/Ironie", "Ironie ohne Verstoss"),
]


def frage_kivi(text: str, timeout: int = 180) -> str | None:
    return _roh_frage(PROMPT_KIVI.format(
        kategorien=", ".join(KATEGORIEN), stufen=", ".join(STUFEN),
        kontext=", ".join(KONTEXT), text=text), timeout)


def auswerten_kivi(antwort: str | None, text: str) -> dict:
    e = {"json_ok": False, "kategorie": None, "verdacht": None, "kontext": None,
         "beleg": "", "begruendung": "", "schema_ok": False, "beleg_echt": None}
    if not antwort:
        return e
    m = re.search(r"\{.*\}", antwort, re.S)
    if not m:
        return e
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return e
    e["json_ok"] = True
    e["kategorie"] = str(d.get("kategorie") or "").strip()
    e["verdacht"] = str(d.get("verdacht") or "").strip().lower()
    e["kontext"] = str(d.get("kontext") or "").strip()
    e["beleg"] = str(d.get("beleg") or "").strip()
    e["begruendung"] = str(d.get("begruendung") or "").strip()
    e["schema_ok"] = (e["kategorie"] in KATEGORIEN and e["verdacht"] in STUFEN
                      and e["kontext"] in KONTEXT)

    # Steht die zitierte Stelle wirklich im Text? Ein Beleg, den es nicht gibt,
    # ist fuer eine juristische Pruefung wertlos - das ist die harte Kennzahl.
    if e["beleg"]:
        norm = lambda s: re.sub(r"[^a-z0-9aeoeuess ]", " ",
                                s.lower().replace("ä", "ae").replace("ö", "oe")
                                 .replace("ü", "ue").replace("ß", "ss"))
        b, t = norm(e["beleg"]).split(), norm(text)
        treffer = sum(1 for w in b if len(w) > 3 and w in t)
        lang = [w for w in b if len(w) > 3]
        e["beleg_echt"] = (treffer / len(lang)) if lang else None
    return e


def stufe_index(v: str | None) -> int:
    return STUFEN.index(v) if v in STUFEN else -1
# --- Wissensprobe -----------------------------------------------------------
# Beide Modelle laufen offline: llama-server kann nichts nachschlagen, die
# Gewichtsdatei ist alles. Was ein Modell an deutschem Medienrecht mitbringt,
# stammt also vollstaendig aus dem Training - und genau das entscheidet, ob es
# die KIVI-Stufe ueberhaupt sinnvoll ausfuellen kann.
#
# Bewertet wird stur nach Stichworten: enthaelt die Antwort die Begriffe, ohne
# die sie fachlich falsch waere? Das misst Wissen, nicht Formulierungskunst.

WISSEN = [
    ("Was verbietet Paragraf 86a StGB? Antworte in zwei Saetzen.",
     [("kennzeichen",), ("verfassungswidrig", "verboten"),
      ("hakenkreuz", "organisation", "partei")]),
    ("Was besagt die Sozialadaequanzklausel in Paragraf 86 Absatz 4 StGB? "
     "Antworte in zwei Saetzen.",
     [("kunst", "wissenschaft", "forschung", "lehre"),
      ("aufklaerung", "berichterstattung", "bildung", "geschicht")]),
    ("Welche Tathandlungen nennt Paragraf 130 StGB (Volksverhetzung)? "
     "Antworte in zwei Saetzen.",
     [("aufstacheln", "aufstachelt", "hass"),
      ("menschenwuerde", "beschimpf", "verleumd"),
      ("gewalt", "willkuer")]),
    ("Wofuer steht die Abkuerzung JMStV und was regelt er? Zwei Saetze.",
     [("jugendmedienschutz",), ("staatsvertrag",),
      ("rundfunk", "telemedien", "internet")]),
    ("Wer beaufsichtigt in Deutschland private Rundfunk- und Telemedien-"
     "angebote? Nenne die zustaendige Stelle in einem Satz.",
     [("landesmedienanstalt", "landesanstalt", "medienanstalt")]),
    ("Was ist der Unterschied zwischen Beleidigung nach Paragraf 185 StGB "
     "und ueber Paragraf 186 StGB? Zwei Saetze.",
     [("werturteil", "meinung", "aeusserung", "herabsetz"),
      ("tatsache", "tatsachenbehauptung", "unwahr")]),
]


def bewerte_wissen(antwort: str | None, erwartet: list[tuple]) -> float:
    """Anteil der Pflichtbegriffe, die vorkommen. Jede Gruppe ist ein ODER."""
    if not antwort:
        return 0.0
    a = antwort.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return sum(1 for gruppe in erwartet if any(w in a for w in gruppe)) / len(erwartet)


def frage_wissen(frage: str, timeout: int = 180) -> str | None:
    return _roh_frage(frage, timeout, max_tokens=200, json_modus=False)

# --- Stufe 1: Inhalt --------------------------------------------------------

def stichprobe(anzahl: int) -> list[dict]:
    """Feste, reproduzierbare Auswahl quer ueber alle Accounts - beide Modelle
    bekommen dieselben Posts in derselben Reihenfolge."""
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT post_id, account, title, content
              FROM nitter_posts
             WHERE CHAR_LENGTH(CONCAT(title, content)) > 120
             ORDER BY MD5(CONCAT(post_id, 'vergleich2026'))
             LIMIT %s""", (anzahl,))
        rows = cur.fetchall()
    conn.close()
    return [{"post_id": r[0], "account": r[1],
             "text": strip_html(f"{r[2]} {r[3]}")[:900]} for r in rows]


def frage_inhalt(text: str) -> str | None:
    return _roh_frage(PROMPT.format(themen=", ".join(THEMEN),
                                    haltungen=", ".join(HALTUNGEN), text=text))


def auswerten_inhalt(antwort: str | None, text: str) -> dict:
    """Nichts wird gerettet - im Gegensatz zum Produktivcode zaehlt der Rohzustand."""
    e = {"json_ok": False, "thema": None, "haltung": None, "akteure": "",
         "quelle": "", "zusammenfassung": "", "schema_ok": False,
         "kurz": False, "deutsch": None, "belegt": None, "genannt": 0}
    if not antwort:
        return e
    m = re.search(r"\{.*?\}", antwort, re.S)
    if not m:
        return e
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return e
    e["json_ok"] = True
    for f in ("thema", "haltung", "akteure", "quelle", "zusammenfassung"):
        e[f] = str(d.get(f) or "").strip()
    e["schema_ok"] = e["thema"] in THEMEN and e["haltung"] in HALTUNGEN

    woerter = e["zusammenfassung"].split()
    e["kurz"] = 0 < len(woerter) <= 12
    klein = {w.strip(".,!?:;\"'").lower() for w in woerter}
    if woerter:
        e["deutsch"] = len(klein & DEUTSCH) >= len(klein & ENGLISCH)

    # Bodenhaftung: steht jeder genannte Name wirklich im Tweet?
    tl = text.lower()
    namen = [n.strip() for teil in (e["akteure"], e["quelle"])
             for n in teil.split(",") if len(n.strip()) > 2]
    e["genannt"] = len(namen)
    if namen:
        treffer = sum(1 for n in namen
                      if n.lower() in tl
                      or any(w.lower() in tl for w in n.split() if len(w) > 3))
        e["belegt"] = treffer / len(namen)
    return e


# --- Ausfuehrung ------------------------------------------------------------

def parallel(fn, elemente, workers):
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(fn, elemente))
    return out, time.time() - t0


def server_start(pfad: str) -> None:
    subprocess.run(["ssh", DELL, "sudo systemctl stop llama-analyze"],
                   check=False, capture_output=True)
    server_stop()
    subprocess.run(["ssh", DELL,
        f"nohup /home/gh/llama.cpp/build/bin/llama-server --model {pfad} "
        f"--n-gpu-layers 0 --threads 8 --ctx-size 4096 --parallel 4 "
        f"--host 0.0.0.0 --port {PORT} --no-warmup "
        f"> /tmp/bench_server.log 2>&1 &"], check=False, capture_output=True)


def server_bereit(sekunden: int = 600) -> bool:
    """25 GB von der SSD einzulesen dauert Minuten - so lange darf es dauern."""
    ende = time.time() + sekunden
    while time.time() < ende:
        try:
            with urllib.request.urlopen(f"http://10.9.0.6:{PORT}/health",
                                        timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(5)
    return False


def server_stop() -> None:
    subprocess.run(["ssh", DELL, "pkill -f 'llama-server .*--port 8081' ; true"],
                   check=False, capture_output=True)


# --- Kennzahlen -------------------------------------------------------------

def anteil(werte, bedingung=lambda x: x) -> float:
    g = [w for w in werte if w is not None]
    return 100.0 * sum(1 for w in g if bedingung(w)) / len(g) if g else 0.0


def entropie(werte) -> float:
    """Normierte Vielfalt der Themen: 0 = alles ein Topf, 100 = breit gestreut."""
    z = collections.Counter(w for w in werte if w)
    if len(z) < 2:
        return 0.0
    n = sum(z.values())
    h = -sum((k / n) * math.log(k / n) for k in z.values())
    return 100.0 * h / math.log(len(THEMEN))


def kennzahlen_inhalt(a, b, dauer, n) -> dict:
    stabil = None
    if b:
        paare = [(x, y) for x, y in zip(a, b) if x["json_ok"] and y["json_ok"]]
        if paare:
            stabil = 100.0 * sum(1 for x, y in paare
                                 if x["thema"] == y["thema"]
                                 and x["haltung"] == y["haltung"]) / len(paare)
    bel = [e["belegt"] for e in a if e["belegt"] is not None]
    return {
        "Posts je Sekunde": n / dauer if dauer else 0.0,
        "Sekunden je Post": dauer / n if n else 0.0,
        "gueltiges JSON":   anteil([e["json_ok"] for e in a]),
        "Schema getroffen": anteil([e["schema_ok"] for e in a]),
        "hoechstens 12 W.": anteil([e["kurz"] for e in a]),
        "auf Deutsch":      anteil([e["deutsch"] for e in a]),
        "Bodenhaftung":     100.0 * sum(bel) / len(bel) if bel else 0.0,
        "Namen je Post":    sum(e["genannt"] for e in a) / n if n else 0.0,
        "Stabilitaet":      stabil,
        "Anteil Sonstiges": anteil([e["thema"] for e in a], lambda t: t == "Sonstiges"),
        "Trennschaerfe":    entropie([e["thema"] for e in a]),
    }


def kennzahlen_kivi(stich, kontroll, dauer, n, inhalt_a) -> dict:
    """Die Kontrollsaetze sind der harte Teil: dort ist die richtige Antwort
    bekannt, alles andere misst nur Verhalten."""
    treffer_stufe = treffer_kontext = 0
    falsch_positiv = harmlos = 0
    for e, (_t, erwartet, kontext, _n) in zip(kontroll, KONTROLLE):
        if stufe_index(e["verdacht"]) >= 0 and stufe_index(e["verdacht"]) <= stufe_index(erwartet):
            treffer_stufe += 1
        if e["kontext"] == kontext:
            treffer_kontext += 1
        if erwartet == "keiner":
            harmlos += 1
            if stufe_index(e["verdacht"]) >= STUFEN.index("mittel"):
                falsch_positiv += 1

    # Satire-Falle: Posts, die die Inhaltsstufe als spoettisch einstuft -
    # wie oft schlaegt die Pruefung dort ueberzogen an?
    spott = [k for k, i in zip(stich, inhalt_a) if i["haltung"] == "spoettisch"]
    bel = [e["beleg_echt"] for e in stich if e["beleg_echt"] is not None]
    return {
        "Posts je Sekunde":  n / dauer if dauer else 0.0,
        "gueltiges JSON":    anteil([e["json_ok"] for e in stich]),
        "Schema getroffen":  anteil([e["schema_ok"] for e in stich]),
        "Beleg im Text":     100.0 * sum(bel) / len(bel) if bel else 0.0,
        "Verdacht gemeldet": anteil([e["verdacht"] for e in stich],
                                    lambda v: stufe_index(v) >= STUFEN.index("niedrig")),
        "Kontrolle: Stufe":  100.0 * treffer_stufe / len(KONTROLLE),
        "Kontrolle: Kontext": 100.0 * treffer_kontext / len(KONTROLLE),
        "Falschalarm harml.": 100.0 * falsch_positiv / harmlos if harmlos else 0.0,
        "Satire ueberzogen": anteil([e["verdacht"] for e in spott],
                                    lambda v: stufe_index(v) >= STUFEN.index("mittel"))
                             if spott else None,
    }


def tabelle(titel: str, daten: dict, roh_prozent: set) -> None:
    namen = list(daten)
    if not namen:
        return
    breite = max(len(k) for k in daten[namen[0]])
    print(f"\n{titel}")
    print("=" * (breite + 2 + 16 * len(namen)))
    print(f"{'Kennzahl':<{breite}}  " + "".join(f"{n:>16}" for n in namen))
    print("-" * (breite + 2 + 16 * len(namen)))
    for k in daten[namen[0]]:
        zeile = f"{k:<{breite}}  "
        for n in namen:
            v = daten[n].get(k)
            if v is None:
                zeile += f"{'--':>16}"
            elif k in roh_prozent:
                zeile += f"{v:>16.2f}"
            else:
                zeile += f"{v:>15.1f}%"
        print(zeile)
    print("=" * (breite + 2 + 16 * len(namen)))


# --- Hauptlauf --------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--stufen", default="inhalt,kivi,wissen",
                    help="Kommaliste: inhalt, kivi, wissen")
    ap.add_argument("--modelle", default=",".join(MODELLE))
    ap.add_argument("--bericht", default="/home/gh/python/modell_vergleich.md")
    args = ap.parse_args()

    stufen = {s.strip() for s in args.stufen.split(",")}
    gewaehlt = [m.strip() for m in args.modelle.split(",") if m.strip() in MODELLE]
    posts = stichprobe(args.posts) if "inhalt" in stufen or "kivi" in stufen else []
    if (("inhalt" in stufen or "kivi" in stufen) and not posts):
        print("keine Posts gefunden", file=sys.stderr)
        return 1
    if posts:
        print(f"Stichprobe: {len(posts)} Posts aus "
              f"{len({p['account'] for p in posts})} Accounts")
    print(f"Stufen: {', '.join(sorted(stufen))} | Modelle: {', '.join(gewaehlt)}\n")

    erg_inhalt, erg_kivi, erg_wissen, roh = {}, {}, {}, {}

    for name in gewaehlt:
        print(f"=== {name} ===")
        print("  Modell einlesen ...", flush=True)
        t0 = time.time()
        server_start(MODELLE[name])
        if not server_bereit():
            print(f"  Server kam nicht hoch - uebersprungen "
                  f"(Log: ssh {DELL} tail /tmp/bench_server.log)\n")
            server_stop()
            continue
        print(f"  bereit nach {time.time() - t0:.0f} s")
        roh[name] = {}
        inhalt_a = []

        if "inhalt" in stufen:
            antw, dauer = parallel(lambda p: frage_inhalt(p["text"]), posts, args.workers)
            inhalt_a = [auswerten_inhalt(a, p["text"]) for a, p in zip(antw, posts)]
            print(f"  Inhalt   Durchlauf 1: {dauer:6.1f} s")
            antw2, _ = parallel(lambda p: frage_inhalt(p["text"]), posts, args.workers)
            inhalt_b = [auswerten_inhalt(a, p["text"]) for a, p in zip(antw2, posts)]
            print(f"  Inhalt   Durchlauf 2: fertig")
            erg_inhalt[name] = kennzahlen_inhalt(inhalt_a, inhalt_b, dauer, len(posts))
            roh[name]["inhalt"] = inhalt_a

        if "kivi" in stufen:
            antw, dauer = parallel(lambda p: frage_kivi(p["text"]), posts, args.workers)
            kivi_stich = [auswerten_kivi(a, p["text"]) for a, p in zip(antw, posts)]
            antw, _ = parallel(lambda k: frage_kivi(k[0]), KONTROLLE, args.workers)
            kivi_kontroll = [auswerten_kivi(a, k[0]) for a, k in zip(antw, KONTROLLE)]
            print(f"  KIVI     Stichprobe + {len(KONTROLLE)} Kontrollsaetze: {dauer:6.1f} s")
            if not inhalt_a:
                inhalt_a = [{"haltung": None} for _ in posts]
            erg_kivi[name] = kennzahlen_kivi(kivi_stich, kivi_kontroll, dauer,
                                             len(posts), inhalt_a)
            roh[name]["kivi"] = kivi_stich
            roh[name]["kontrolle"] = kivi_kontroll

        if "wissen" in stufen:
            antw, dauer = parallel(lambda w: frage_wissen(w[0]), WISSEN, args.workers)
            punkte = [bewerte_wissen(a, w[1]) for a, w in zip(antw, WISSEN)]
            print(f"  Wissen   {len(WISSEN)} Rechtsfragen: {dauer:6.1f} s")
            erg_wissen[name] = {"Medienrecht gesamt": 100.0 * sum(punkte) / len(punkte)}
            for (frage, _), p in zip(WISSEN, punkte):
                erg_wissen[name][frage.split("?")[0][:26]] = 100.0 * p
            roh[name]["wissen"] = list(zip([w[0] for w in WISSEN], antw))

        server_stop()
        print()

    roh_prozent = {"Posts je Sekunde", "Sekunden je Post", "Namen je Post"}
    if erg_inhalt:
        tabelle("STUFE 1  Inhaltsanalyse", erg_inhalt, roh_prozent)
    if erg_kivi:
        tabelle("STUFE 2  KIVI-Vorpruefung", erg_kivi, roh_prozent)
    if erg_wissen:
        tabelle("STUFE 3  Medienrecht aus dem Training", erg_wissen, set())

    schreibe_bericht(args.bericht, posts, roh)
    print(f"\nAntworten nebeneinander: {args.bericht}")
    subprocess.run(["ssh", DELL, "sudo systemctl start llama-analyze"],
                   check=False, capture_output=True)
    print("llama-analyze wieder gestartet.")
    return 0


def schreibe_bericht(pfad, posts, roh) -> None:
    """Das inhaltliche Urteil bleibt beim Menschen - hier stehen die Antworten
    beider Modelle nebeneinander."""
    namen = list(roh)
    with open(pfad, "w") as f:
        f.write("# Modellvergleich\n\nErzeugt: "
                f"{time.strftime('%d.%m.%Y %H:%M')}\n\n")

        if any("kontrolle" in roh[n] for n in namen):
            f.write("## Kontrollsaetze (richtige Antwort bekannt)\n\n")
            for i, (text, erwartet, kontext, notiz) in enumerate(KONTROLLE):
                f.write(f"### {i+1}. {notiz}\n\n> {text}\n\n"
                        f"erwartet: hoechstens **{erwartet}**, Kontext **{kontext}**\n\n")
                for n in namen:
                    e = roh[n].get("kontrolle", [{}] * len(KONTROLLE))[i]
                    if not e:
                        continue
                    f.write(f"- **{n}**: {e.get('verdacht') or '?'} / "
                            f"{e.get('kategorie') or '?'} / {e.get('kontext') or '?'}  \n"
                            f"  Beleg: _{e.get('beleg') or '-'}_  \n"
                            f"  {e.get('begruendung') or '-'}\n")
                f.write("\n---\n\n")

        if posts and any("inhalt" in roh[n] or "kivi" in roh[n] for n in namen):
            f.write("## Echte Posts\n\n")
            for i, p in enumerate(posts[:12]):
                f.write(f"### @{p['account']}\n\n> {p['text'][:400]}\n\n")
                for n in namen:
                    ie = roh[n].get("inhalt", [None] * len(posts))[i]
                    ke = roh[n].get("kivi", [None] * len(posts))[i]
                    f.write(f"**{n}**  \n")
                    if ie:
                        f.write(f"Inhalt: {ie['thema'] or '?'} / {ie['haltung'] or '?'} | "
                                f"Akteure: {ie['akteure'] or '-'} | "
                                f"Quelle: {ie['quelle'] or '-'}  \n"
                                f"_{ie['zusammenfassung'] or '-'}_  \n")
                    if ke:
                        f.write(f"KIVI: {ke['verdacht'] or '?'} / {ke['kategorie'] or '?'} / "
                                f"{ke['kontext'] or '?'} - {ke['begruendung'] or '-'}  \n")
                    f.write("\n")
                f.write("---\n\n")

        if any("wissen" in roh[n] for n in namen):
            f.write("## Rechtsfragen im Wortlaut\n\n")
            for i, (frage, _) in enumerate(WISSEN):
                f.write(f"### {frage}\n\n")
                for n in namen:
                    a = roh[n].get("wissen", [(None, None)] * len(WISSEN))[i][1]
                    f.write(f"**{n}**: {a or '(keine Antwort)'}\n\n")
                f.write("---\n\n")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
poller_werkzeuge.py - Werkzeuge fuer die Auftraege des Batch-Schedulers.

Ohne diese Datei schickt der Poller eine nackte Chat-Anfrage an den lokalen
llama-server. Das Modell kann dann nichts nachschlagen - und schlimmer: es
merkt es nicht. Ein Testauftrag am 28.08.2026 ("rufe diese Seite auf") lieferte
"konnte nicht erreicht werden, kein HTTP-Statuscode", obwohl nie ein Abruf
stattgefunden hat. Erfundene Fehlschlaege sind schlechter als ein ehrliches
"kann ich nicht".

Angeboten werden fuenf Werkzeuge:

  suche_web            DuckDuckGo
  hole_seite           beliebige Adresse, PDF wird mitgelesen (Ausschnitt)
  datei_speichern      laedt vollstaendig nach /home/gh/downloads und meldet
                       die echte Groesse aus dem Dateisystem
  wissenschaft_suchen  K10plus, Crossref, OpenAlex, EuropePMC, arXiv, DOAJ
  zeitung_suchen       Volltext des Toelzer Kurier ueber web2.heissa.de

Bis auf datei_speichern sind alle nur lesend; geschrieben wird ausschliesslich
in den Ablageordner.

Die Adressgrenze aus netz_werkzeug bleibt aktiv: oeffentliche Adressen ja,
das eigene Heimnetz nein. Der Poller laeuft auf dem Dell mitten im LAN, eine
Adresse aus einem Auftragstext koennte sonst auf 192.168.x zielen.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/home/gh")
import netz_werkzeug as nw
from wissenschaft import wissenschaft_suchen

MAX_RUNDEN = 8
MAX_ERGEBNIS = 6000
ZEITUNG_API = "https://web2.heissa.de/zeitung/api.php"

HINWEIS = """Du kannst nachschlagen, bevor du antwortest.

suche_web            allgemeine Suche ueber DuckDuckGo
hole_seite           holt eine Adresse und liefert den Text; ist er
                     abgeschnitten, mit ab= an derselben Adresse weiterlesen
wissenschaft_suchen  Lehrbuecher und Fachliteratur (K10plus, Crossref,
                     OpenAlex, Europe PMC, arXiv, DOAJ). Deutsche Begriffe
                     werden automatisch ins Englische uebersetzt, denn
                     Fachliteratur ist englisch.
zeitung_suchen       Volltext des Toelzer Kurier, 161 Ausgaben vom 14.02.2026
                     bis 28.08.2026 - erste Wahl bei allem mit Ortsbezug
                     (Bad Toelz, Lenggries, Wolfratshausen, Geretsried).

So gehst du vor: Erst pruefen, ob du es ohnehin weisst - Algorithmen,
Mathematik, Programmierung, Lehrbuchwissen beantwortest du direkt. Musst du
nachschlagen, waehle anhand der Textauszuege die eine passende Quelle und lies
sie ganz, statt zwischen halb gelesenen Seiten zu springen. Meldet hole_seite
"[nichts zu holen]", war die Seite leer - sie zaehlt nicht als Beleg.
Hoechstens zwei Suchlaeufe.

datei_speichern      laedt eine Adresse vollstaendig herunter und legt sie
                     als Datei ab. Fuer "beschaffe", "lade herunter",
                     "sichere" ist das das richtige Werkzeug - hole_seite
                     liefert nur die ersten Zeichen. Gib immer "erwartet" mit:
                     eine Wortfolge, die im richtigen Werk stehen muss. Das
                     Werkzeug prueft sie und widerspricht dir, wenn du die
                     falsche Datei erwischt hast. Waehle als "erwartet" eine
                     Wendung aus dem Inneren des Werks, nicht den Titel: der
                     Titel steht auch auf jeder Katalogseite und beweist
                     darum nichts.

datei_lesen          liest Quelltext unterhalb von /home/gh
diagramm_erstellen   uebersetzt Graphviz-Quelltext (digraph { ... }) in ein
                     PDF im Webordner. Sollst du einen Ablauf zeichnen, lies
                     erst den Quelltext mit datei_lesen und zeichne dann, was
                     wirklich dort steht.

Rate niemals eine Adresse. Kennnummern von Archiven wie Projekt Gutenberg
kannst du nicht auswendig - suche sie, bevor du laedst.

Erfinde niemals einen Abruf. Hast du nichts nachgeschlagen, sag es. Und nenne
keine Zeichenzahl, Dateigroesse oder Seitenzahl, die dir kein Werkzeug
zurueckgegeben hat - lieber "unbekannt" als eine plausible Zahl."""


def zeitung_suchen(begriff: str, von: str = "", bis: str = "") -> str:
    p = {"view": "suche", "q": begriff, "format": "md",
         "limit": 6, "treffer": 2, "kontext": 400}
    if von:
        p["von"] = von
    if bis:
        p["bis"] = bis
    try:
        return nw._roh_holen(ZEITUNG_API + "?" + urllib.parse.urlencode(p))[:MAX_ERGEBNIS]
    except Exception as e:
        return f"Zeitungssuche fehlgeschlagen: {e}"


ABLAGE = "/home/gh/downloads"


def datei_speichern(url: str, name: str = "", erwartet: str = "") -> str:
    """Laedt eine Adresse vollstaendig in eine Datei und meldet, was wirklich
    ankam.

    Anlass: ein Auftrag "beschaffe Faust I" endete damit, dass das Modell
    103826 Zeichen meldete. Die Seite hat 160804, das Lesewerkzeug liefert
    hoechstens 6000 - die Zahl war frei erfunden. Herunterladen ist eben keine
    Leseoperation: ein Werkzeug, das Text in den Kontext schaufelt, kann ein
    ganzes Buch gar nicht liefern und lockt zum Raten. Hier wird die Datei
    abgelegt, und die Groesse stammt vom Dateisystem."""
    import hashlib
    import os
    import re as _re

    nw._pruefe_ziel(url)
    os.makedirs(ABLAGE, exist_ok=True)
    if not name:
        name = urllib.parse.unquote(
            urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]) or "download"
    name = _re.sub(r"[^\w.\-]+", "_", name)[:120]
    if "." not in name:
        name += ".txt"
    ziel = os.path.join(ABLAGE, name)
    if os.path.realpath(ziel).rsplit("/", 1)[0] != os.path.realpath(ABLAGE):
        return f"Abgelehnt: {name} zeigt aus {ABLAGE} heraus."

    try:
        roh = nw._roh_holen(url, grenze=40_000_000)
    except Exception as e:
        return f"[nichts geladen] {url}: {e}"

    art = "html" if roh.lstrip()[:1] == "<" else "text"
    text = nw._entschaerfe_html(roh) if art == "html" else roh
    with open(ziel, "w") as f:
        f.write(text)
    groesse = os.path.getsize(ziel)
    pruef = hashlib.sha256(text.encode()).hexdigest()[:16]

    # Identitaetspruefung. Anlass: ein Auftrag "lade Faust I" endete damit,
    # dass das Modell eine Gutenberg-Nummer erfand (54701 = ein Buch ueber
    # Canterbury Cathedral), die Datei herunterlud und sie als Faust meldete.
    # Der Beleg stand in den ersten Zeilen der Rueckgabe und wurde ignoriert.
    # Darum wird jetzt maschinell geprueft statt darum gebeten.
    # Umfangspruefung. Anlass: der naechste Anlauf desselben Auftrags lud die
    # Gutenberg-Uebersichtsseite statt der Textdatei - 2654 Bytes, und die
    # Titelzeile stand auch dort, also ging die Inhaltspruefung durch. Ein
    # ganzes Werk ist nie so kurz.
    warnung = ""
    if len(text) < 12000:
        warnung = (f"\nACHTUNG: nur {len(text)} Zeichen. Das ist zu wenig fuer "
                   f"ein Buch oder ein Drama - vermutlich hast du eine "
                   f"Uebersichts- oder Katalogseite erwischt statt der "
                   f"eigentlichen Datei. Solche Seiten verweisen auf die "
                   f"Textdatei (oft mit der Endung .txt); lade diese.")

    urteil = ""
    if erwartet:
        gefunden = erwartet.lower() in text.lower()
        urteil = (f"\nPRUEFUNG: {erwartet!r} kommt im Text "
                  + ("vor - die Datei passt zum Auftrag."
                     if gefunden else
                     "NICHT vor. Das ist hoechstwahrscheinlich das falsche "
                     "Werk. Melde es nicht als Erfolg, sondern suche die "
                     "richtige Quelle und lade erneut."))
    else:
        urteil = ("\nPRUEFUNG: nicht durchgefuehrt - du hast keinen "
                  "Erwartungsbegriff angegeben. Pruefe den Textanfang selbst, "
                  "bevor du meldest, dass du das Richtige geladen hast.")

    return (f"Gespeichert: {ziel}\n"
            f"Groesse: {groesse} Bytes, {len(text)} Zeichen "
            f"(Quelle war {art}, {len(roh)} Zeichen roh)\n"
            f"SHA256 (Anfang): {pruef}\n"
            f"Anfang des Textes:\n{text[:600]}"
            f"{warnung}{urteil}\n\n"
            f"Diese Zahlen stammen vom Dateisystem. Nenne keine anderen.")


QUELLEN = "/home/gh"
DIAGRAMME = "/var/www/html/api/batch"


def datei_lesen(pfad: str, ab: int = 0, zeichen: int = 60000) -> str:
    """Liest eine Datei unterhalb von /home/gh. Nur lesend.

    Damit kann das Modell den Quelltext betrachten, ueber den es berichten
    soll, statt sich den Ablauf auszudenken.

    Die Obergrenze lag anfangs bei 12000 Zeichen und erzwang ein Stueckeln.
    Am 29.08.2026 las das Modell daraufhin die ersten 12000 Zeichen von
    batch-poller.py, sprang dann versehentlich hinter das Dateiende und
    zeichnete ein Ablaufdiagramm, in dem alles Gelesene stimmte und alles
    Uebersprungene erfunden war - ein "provider"-Feld, das der Auftrag gar
    nicht hat, und eine Wiederholschleife, die es nicht gibt. Eine Quelldatei
    von 33000 Zeichen sind rund 9000 Token und passen laengst in einen Slot;
    das Stueckeln war eine kuenstliche Huerde. Jetzt kommt sie am Stueck."""
    import os
    ziel = os.path.realpath(os.path.join(QUELLEN, pfad.lstrip("/")))
    wurzel = os.path.realpath(QUELLEN)
    if ziel != wurzel and not ziel.startswith(wurzel + os.sep):
        return f"Abgelehnt: {pfad} liegt ausserhalb von {QUELLEN}."
    if not os.path.isfile(ziel):
        return f"{pfad} gibt es nicht."
    if os.path.getsize(ziel) > 4_000_000:
        return f"{pfad} ist zu gross zum Lesen."
    try:
        with open(ziel, errors="replace") as f:
            roh = f.read()
    except Exception as e:
        return f"{pfad} nicht lesbar: {e}"
    ab = max(0, int(ab))
    zeichen = max(500, min(120000, int(zeichen)))
    stueck = roh[ab:ab + zeichen]
    rest = len(roh) - (ab + len(stueck))
    weiter = ab + len(stueck)
    if rest > 0:
        # Deutlich und mit genau EINER Zahl: die frueher mitgelieferte
        # Gesamtlaenge wurde als naechster Offset missverstanden.
        kopf = (f"[{ziel} - ACHTUNG, DIESER TEXT IST UNVOLLSTAENDIG. "
                f"Es fehlen noch {rest} Zeichen. Rufe datei_lesen erneut auf "
                f"mit ab={weiter} und urteile erst danach ueber die Datei.]\n")
        stueck += (f"\n\n[Hier bricht der Auszug ab. Es fehlen {rest} Zeichen. "
                   f"Weiterlesen mit ab={weiter}. Berichte nichts ueber Teile, "
                   f"die du nicht gelesen hast.]")
    else:
        # Bewusst ohne Zahlenangabe: die frueher genannte Gesamtlaenge wurde
        # zweimal als naechster Offset missverstanden, worauf das Modell hinter
        # das Dateiende sprang.
        kopf = (f"[{ziel} - VOLLSTAENDIG. Die Datei ist hier ganz enthalten, "
                f"von der ersten bis zur letzten Zeile. Rufe datei_lesen fuer "
                f"diese Datei NICHT noch einmal auf.]\n")
    return kopf + stueck


def diagramm_erstellen(dot: str, name: str = "flowchart.pdf") -> str:
    """Rendert Graphviz-Quelltext zu PDF und legt es im Webordner ab.

    Der Rueckgabewert enthaelt die Fehlermeldung von dot im Wortlaut, falls
    der Quelltext nicht uebersetzt - dann kann das Modell ihn selbst
    berichtigen, statt zu behaupten, es habe geklappt."""
    import os
    import re as _re
    import subprocess
    import tempfile

    name = _re.sub(r"[^\w.\-]+", "_", name or "flowchart.pdf")[:80]
    if not name.endswith(".pdf"):
        name += ".pdf"
    ziel = os.path.join(DIAGRAMME, name)
    if os.path.realpath(ziel).rsplit("/", 1)[0] != os.path.realpath(DIAGRAMME):
        return f"Abgelehnt: {name} zeigt aus {DIAGRAMME} heraus."
    if not dot or "{" not in dot:
        return "Abgelehnt: das sieht nicht nach Graphviz-Quelltext aus."

    with tempfile.NamedTemporaryFile("w", suffix=".dot", delete=False) as f:
        f.write(dot)
        quelle = f.name
    try:
        r = subprocess.run(["dot", "-Tpdf", "-o", ziel, quelle],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        os.unlink(quelle)
        return f"dot nicht ausfuehrbar: {e}"
    os.unlink(quelle)

    if r.returncode != 0:
        return (f"Graphviz meldet einen Fehler, es wurde NICHTS erzeugt:\n"
                f"{(r.stderr or '').strip()[:800]}\n"
                f"Berichtige den Quelltext und rufe erneut auf.")
    if not os.path.exists(ziel) or os.path.getsize(ziel) < 500:
        return "dot lief durch, aber die Datei ist leer oder fehlt."

    seiten = ""
    try:
        i = subprocess.run(["pdfinfo", ziel], capture_output=True, text=True,
                           timeout=30)
        m = _re.search(r"Pages:\s+(\d+)", i.stdout or "")
        if m:
            seiten = f", {m.group(1)} Seite(n)"
    except Exception:
        pass
    return (f"Erstellt: {ziel}\n"
            f"Groesse: {os.path.getsize(ziel)} Bytes{seiten}\n"
            f"Abrufbar unter: http://192.168.5.23/api/batch/{name}\n"
            f"Diese Angaben stammen vom Dateisystem.")


WERKZEUGE = nw.WERKZEUGE + [
    {"type": "function", "function": {
        "name": "datei_lesen",
        "description": "Liest eine Datei unterhalb von /home/gh, z. B. "
                       "batch-poller.py. Ist der Text abgeschnitten, mit ab= "
                       "weiterlesen.",
        "parameters": {"type": "object", "properties": {
            "pfad": {"type": "string", "description": "z. B. batch-poller.py"},
            "ab": {"type": "integer", "description": "Zeichenposition"}},
            "required": ["pfad"]}}},
    {"type": "function", "function": {
        "name": "diagramm_erstellen",
        "description": "Rendert Graphviz-Quelltext (digraph { ... }) zu einer "
                       "PDF-Datei im Webordner. Meldet Uebersetzungsfehler im "
                       "Wortlaut zurueck.",
        "parameters": {"type": "object", "properties": {
            "dot": {"type": "string",
                    "description": "vollstaendiger Graphviz-Quelltext"},
            "name": {"type": "string", "description": "Dateiname, z. B. flowchart.pdf"}},
            "required": ["dot"]}}},
    {"type": "function", "function": {
        "name": "datei_speichern",
        "description": "Laedt eine Adresse vollstaendig herunter und legt sie "
                       "als Datei ab. Nimm das fuer Auftraege wie "
                       "'beschaffe', 'lade herunter', 'sichere' - hole_seite "
                       "liefert nur einen Ausschnitt und taugt dafuer nicht.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "name": {"type": "string", "description": "Dateiname, optional"},
            "erwartet": {"type": "string",
                         "description": "Wortfolge, die im richtigen Werk "
                                        "vorkommen muss - z. B. ein Titel oder "
                                        "eine bekannte Zeile. Das Werkzeug "
                                        "prueft es und sagt dir, ob es passt."}},
            "required": ["url", "erwartet"]}}},
    {"type": "function", "function": {
        "name": "wissenschaft_suchen",
        "description": "Sucht in Lehrbuechern und Fachliteratur statt im "
                       "offenen Netz. Deutsche Begriffe werden vor der Suche "
                       "ins Englische uebersetzt.",
        "parameters": {"type": "object", "properties": {
            "begriff": {"type": "string"},
            "art": {"type": "string",
                    "description": "alles | buecher | studien | medizin | preprints"}},
            "required": ["begriff"]}}},
    {"type": "function", "function": {
        "name": "zeitung_suchen",
        "description": "Durchsucht den Volltext aller Ausgaben des Toelzer "
                       "Kurier (14.02.2026 bis 28.08.2026). Erste Wahl bei "
                       "regionalem Bezug.",
        "parameters": {"type": "object", "properties": {
            "begriff": {"type": "string"},
            "von": {"type": "string", "description": "ab YYYY-MM-DD"},
            "bis": {"type": "string", "description": "bis YYYY-MM-DD"}},
            "required": ["begriff"]}}},
]

AUSFUEHRUNG = {
    "datei_lesen": lambda a: datei_lesen(a.get("pfad", ""),
                                         int(a.get("ab") or 0)),
    "diagramm_erstellen": lambda a: diagramm_erstellen(a.get("dot", ""),
                                                       a.get("name", "flowchart.pdf")),
    "datei_speichern": lambda a: datei_speichern(a.get("url", ""),
                                                 a.get("name", ""),
                                                 a.get("erwartet", "")),
    "suche_web": lambda a: nw.suche_web(a.get("begriff", "")),
    "hole_seite": lambda a: nw.hole_seite(a.get("url", ""),
                                          ab=int(a.get("ab") or 0)),
    "wissenschaft_suchen": lambda a: wissenschaft_suchen(a.get("begriff", ""),
                                                         a.get("art", "alles")),
    "zeitung_suchen": lambda a: zeitung_suchen(a.get("begriff", ""),
                                               a.get("von", ""), a.get("bis", "")),
}


def mit_werkzeugen(prompt_text: str, system_prompt: str, url: str,
                   model_id: str, protokoll=None) -> dict:
    """Fuehrt einen Auftrag aus und laesst das Modell dabei nachschlagen.

    Gibt dieselbe Struktur zurueck wie run_local im Poller, damit der Aufrufer
    nichts weiter wissen muss. protokoll(text) wird je Werkzeugaufruf gerufen -
    der Poller haengt das an das Ergebnis, damit im Job sichtbar bleibt, worauf
    sich die Antwort stuetzt."""
    def notiz(t: str) -> None:
        if protokoll:
            protokoll(t)

    nachrichten = [
        {"role": "system", "content": (system_prompt or "") + "\n\n" + HINWEIS},
        {"role": "user", "content": prompt_text},
    ]
    ein = aus = 0

    for runde in range(MAX_RUNDEN):
        antwort, i, o = _anfragen(nachrichten, url, model_id, mit_tools=True)
        ein += i
        aus += o
        if antwort is None:
            return {"result": "(Modell nicht erreichbar)", "in_tok": ein,
                    "out_tok": aus, "cache_tok": 0, "cost": 0.0}

        aufrufe = antwort.get("tool_calls") or []
        if not aufrufe:
            return {"result": (antwort.get("content") or "").strip(),
                    "in_tok": ein, "out_tok": aus, "cache_tok": 0, "cost": 0.0}

        nachrichten.append({k: v for k, v in antwort.items()
                            if k in ("role", "content", "tool_calls")})
        for aufruf in aufrufe:
            name = aufruf.get("function", {}).get("name", "?")
            try:
                args = json.loads(aufruf["function"].get("arguments") or "{}")
            except (json.JSONDecodeError, KeyError):
                args = {}
            t0 = time.time()
            try:
                ergebnis = str(AUSFUEHRUNG[name](args))
            except nw.Verweigert as e:
                ergebnis = f"Abruf nicht ausgefuehrt: {e}"
            except KeyError:
                ergebnis = f"unbekanntes Werkzeug {name}"
            except Exception as e:
                ergebnis = f"fehlgeschlagen: {e}"
            kurz = ", ".join(f"{k}={str(v)[:70]}" for k, v in args.items())
            notiz(f"[{runde+1}] {name}({kurz}) -> {len(ergebnis)} Zeichen "
                  f"in {time.time()-t0:.1f} s")
            nachrichten.append({"role": "tool",
                                "tool_call_id": aufruf.get("id", name),
                                "name": name,
                                "content": ergebnis[:MAX_ERGEBNIS]})

    # Runden aufgebraucht: Werkzeuge wegnehmen und antworten lassen, statt
    # den Auftrag ohne Ergebnis zurueckzugeben.
    notiz("Rundenende - Antwort aus dem bisher Gelesenen")
    nachrichten.append({"role": "user", "content":
                        "Genug nachgeschlagen. Antworte jetzt aus dem, was du "
                        "gelesen hast, und sage offen, was offen blieb."})
    letzte, i, o = _anfragen(nachrichten, url, model_id, mit_tools=False)
    return {"result": ((letzte or {}).get("content") or "(keine Antwort)").strip(),
            "in_tok": ein + i, "out_tok": aus + o, "cache_tok": 0, "cost": 0.0}


def _anfragen(nachrichten: list, url: str, model_id: str,
              mit_tools: bool) -> tuple[dict | None, int, int]:
    # 4000 statt 1200: ein vollstaendiges Graphviz-Diagramm als Aufrufargument
    # braucht mehr. Am 29.08.2026 brach die Erzeugung bei 1262 Token ab, das
    # JSON des Werkzeugaufrufs blieb unvollstaendig und kam leer an.
    koerper = {"model": model_id, "messages": nachrichten,
               "temperature": 0.3, "max_tokens": 4000}
    if mit_tools:
        koerper["tools"] = WERKZEUGE
        koerper["tool_choice"] = "auto"
    req = urllib.request.Request(url, data=json.dumps(koerper).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            d = json.load(r)
    except Exception as e:
        print(f"llama-server: {e}", file=sys.stderr)
        return None, 0, 0
    u = d.get("usage", {})
    return (d["choices"][0]["message"], u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0))

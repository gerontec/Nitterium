#!/usr/bin/env python3
"""
last_lokal.py - erzeugt auf dem Dell selbst Last fuer den llama-server.

Zweimal ist ein Messversuch daran gescheitert, dass der Lastgeber auf heissa
lief: der Test startet den Modelldienst neu, der Analyzer dort verliert die
Verbindung und beendet sich, und gemessen wird eine stille Maschine. Beide
Male standen 0,0 Token/s im Ergebnis.

Dieser Lastgeber laeuft auf demselben Rechner wie der Dienst, haengt an keiner
Datenbank und ueberlebt nichts, was er nicht selbst sieht. Er haelt dauerhaft
so viele Anfragen offen, wie der Server Slots hat.

Die Anfrage ist der echten Inhaltsanalyse nachgebildet: rund 450 Token Eingabe,
etwa 77 Token Ausgabe - die am 28.08.2026 gemessenen Werte.

    python3 last_lokal.py --sekunden 120 --parallel 4
"""

import argparse
import json
import threading
import time
import urllib.request

# Ein Beispieltext in der Laenge echter Beitraege. Inhaltlich belanglos - es
# geht um Rechenlast, nicht um das Ergebnis.
TEXT = ("Die Gemeinde hat gestern Abend im Gemeinderat den Haushaltsplan fuer "
        "das kommende Jahr beraten. Strittig war vor allem die Sanierung der "
        "Turnhalle, deren Kosten sich gegenueber der ersten Schaetzung nahezu "
        "verdoppelt haben. Der Kaemmerer verwies auf gestiegene Baupreise und "
        "auf Auflagen zum Brandschutz, die nachtraeglich hinzugekommen seien. "
        "Mehrere Raete forderten, das Vorhaben zu verschieben und stattdessen "
        "zunaechst die Heizungsanlage der Grundschule zu erneuern, die seit "
        "zwei Wintern stoerungsanfaellig ist. Die Verwaltung soll bis zur "
        "naechsten Sitzung eine Gegenueberstellung beider Varianten vorlegen, "
        "einschliesslich der zu erwartenden Foerdermittel des Landes. ") * 2

PROMPT = f"""Du analysierst einen deutschsprachigen Beitrag. Antworte NUR mit
einem JSON-Objekt, alle Werte auf Deutsch.

Felder:
  thema: ein Stichwort
  akteure: bis zu drei genannte Personen oder Organisationen
  quelle: genannte Quelle oder ""
  haltung: zustimmend, ablehnend, neutral berichtend, spoettisch oder fragend
  zusammenfassung: ein deutscher Satz, hoechstens 12 Woerter

Beitrag:
{TEXT}"""


def eine(url: str) -> bool:
    koerper = json.dumps({
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 140, "temperature": 0.1,
        "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(url, data=koerper,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            json.load(r)
        return True
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sekunden", type=int, default=120)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--url", default="http://127.0.0.1:8081/v1/chat/completions")
    args = ap.parse_args()

    ende = time.time() + args.sekunden
    zaehler = {"ok": 0, "fehl": 0}
    sperre = threading.Lock()

    def schleife() -> None:
        while time.time() < ende:
            gut = eine(args.url)
            with sperre:
                zaehler["ok" if gut else "fehl"] += 1

    faeden = [threading.Thread(target=schleife, daemon=True)
              for _ in range(args.parallel)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join(timeout=args.sekunden + 300)
    print(f"Last beendet: {zaehler['ok']} Anfragen ok, "
          f"{zaehler['fehl']} fehlgeschlagen")


if __name__ == "__main__":
    main()

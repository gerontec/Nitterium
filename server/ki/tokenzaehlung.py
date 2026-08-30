#!/usr/bin/env python3
"""
tokenzaehlung.py - wie viele Token erzeugt die Inhaltsanalyse wirklich?

Der Durchsatz ist linear in den erzeugten Token: die Maschine haengt am
Speicherbus, und je Token muessen die aktiven Gewichte einmal hindurch.
max_tokens steht auf 140. Gebraucht wird aber nur ein JSON mit fuenf Feldern,
darunter ein Satz von hoechstens zwoelf Woertern.

Liegt die tatsaechliche Laenge deutlich unter 140, ist nichts zu holen - dann
hoert das Modell von selbst auf. Liegt sie am Anschlag, redet es zu viel, und
eine kuerzere Vorgabe waere unmittelbar bares Tempo.

llama.cpp liefert die Zahlen im Feld "usage" jeder Antwort mit.
"""

import configparser
import json
import os
import re
import statistics
import sys
import urllib.request

import pymysql

LLM_URL = "http://10.9.0.6:8081/v1/chat/completions"
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


def posts(n: int) -> list[str]:
    conn = pymysql.connect(**zugang())
    with conn.cursor() as cur:
        cur.execute("""SELECT title, content FROM nitter_posts
                        WHERE CHAR_LENGTH(CONCAT(title, content)) > 120
                        ORDER BY MD5(CONCAT(post_id, 'token2026')) LIMIT %s""", (n,))
        z = cur.fetchall()
    conn.close()
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", f"{a} {b}")).strip()[:900]
            for a, b in z]


def einer(text: str) -> tuple[int, int, str] | None:
    body = json.dumps({
        "messages": [{"role": "user", "content": PROMPT.format(
            themen=", ".join(THEMEN), haltungen=", ".join(HALTUNGEN), text=text)}],
        "max_tokens": 140, "temperature": 0.1,
        "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(LLM_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.load(r)
    except Exception as e:
        print(f"  Fehler: {e}", file=sys.stderr)
        return None
    u = d.get("usage", {})
    grund = (d.get("choices") or [{}])[0].get("finish_reason", "?")
    return u.get("prompt_tokens", 0), u.get("completion_tokens", 0), grund


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    ein, aus, gruende = [], [], {}
    for i, t in enumerate(posts(n), 1):
        r = einer(t)
        if not r:
            continue
        p, c, g = r
        ein.append(p)
        aus.append(c)
        gruende[g] = gruende.get(g, 0) + 1
        print(f"  {i:>2}. Eingabe {p:>4} Token, Ausgabe {c:>4} Token  ({g})",
              flush=True)

    if not aus:
        return 1
    print(f"\nEingabe  Mittel {statistics.mean(ein):6.1f}  "
          f"Median {statistics.median(ein):6.1f}  Max {max(ein)}")
    print(f"Ausgabe  Mittel {statistics.mean(aus):6.1f}  "
          f"Median {statistics.median(aus):6.1f}  Max {max(aus)}")
    print(f"Abbruchgruende: {gruende}")
    am_anschlag = sum(1 for c in aus if c >= 140)
    print(f"\nAm Limit von 140 Token: {am_anschlag} von {len(aus)}")
    if am_anschlag > len(aus) * 0.2:
        print("-> Das Modell redet bis zur Grenze. Eine knappere Vorgabe oder "
              "ein straffer formulierter Prompt wuerde direkt Tempo bringen.")
    else:
        print(f"-> Das Modell hoert von selbst bei rund "
              f"{statistics.median(aus):.0f} Token auf. max_tokens ist nicht "
              f"der Engpass, hier ist nichts zu holen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

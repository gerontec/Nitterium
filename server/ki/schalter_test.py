#!/usr/bin/env python3
"""
schalter_test.py - prueft llama.cpp-Schalter, die am Speicherengpass ansetzen.

Der Thread/Slot-Sweep hat gezeigt: 8 Threads und 4 Slots sind bereits das
Optimum, mehr von beidem macht es schlechter. Der Engpass ist die
Speicherbandbreite, nicht die Rechenleistung. Also wird hier an den Schaltern
gedreht, die den Speicherverkehr betreffen - und an der Kernbindung.

Der i7-12700 ist ein Hybrid: CPU 0-15 sind acht P-Kerne mit 4,8-4,9 GHz,
CPU 16-19 vier E-Kerne mit 3,6 GHz. Ohne Bindung verteilt der Kernel frei;
landet ein Thread auf einem E-Kern, warten alle anderen auf ihn.

Gemessen wird wie beim Sweep: dieselben echten Posts, dieselbe Nebenlaeufigkeit.

    python3 schalter_test.py
    python3 schalter_test.py --posts 16
"""

import argparse
import sys

sys.path.insert(0, "/home/gh/python")
from durchsatz_optimum import (bereit, lokal, messen, ssh, stichprobe,
                               MODELL_DATEI, PORT)

THREADS = 8
SLOTS = 4
CTX = SLOTS * 2048

VARIANTEN = [
    ("Ausgangslage", ""),
    ("nur P-Kerne (0-15)", "--cpu-range 0-15 --cpu-strict 1"),
    ("ein Thread je P-Kern", "--cpu-mask 5555 --cpu-strict 1"),
    ("hohe Prioritaet", "--prio 2"),
    ("mlock (Gewichte festnageln)", "--mlock"),
    ("KV-Cache q8_0", "--cache-type-k q8_0 --cache-type-v q8_0"),
    ("FlashAttention an", "--flash-attn on"),
    ("ubatch 128", "--ubatch-size 128"),
]


def start(extra: str) -> None:
    ssh("sudo systemctl stop llama-analyze")
    ssh("pkill -f 'llama-server .*--port 8081' ; true")
    ssh(f"nohup /home/gh/llama.cpp/build/bin/llama-server "
        f"--model {MODELL_DATEI} --n-gpu-layers 0 --threads {THREADS} "
        f"--ctx-size {CTX} --parallel {SLOTS} {extra} "
        f"--host 0.0.0.0 --port {PORT} --no-warmup > /tmp/schalter.log 2>&1 &")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", type=int, default=24)
    args = ap.parse_args()

    posts = stichprobe(args.posts)
    print(f"Stichprobe: {len(posts)} Posts | {THREADS} Threads, {SLOTS} Slots\n")

    print("Cron anhalten und laufende Analyzer beenden ...")
    lokal("crontab -l | sed 's|^\\(\\*/15 .*content_analyzer.*\\)$|#SCHALTER \\1|' | crontab -")
    lokal("pkill -f '[c]ontent_analyzer.py' ; true")
    print(f"  Cron pausiert: "
          f"{'ja' if '#SCHALTER' in lokal('crontab -l') else 'NEIN - Abbruch!'}\n")

    ergebnisse = []
    try:
        for name, extra in VARIANTEN:
            print(f"  {name:<30} ", end="", flush=True)
            start(extra)
            if not bereit():
                print("Server kam nicht hoch "
                      f"({ssh('tail -2 /tmp/schalter.log')[:90]})")
                continue
            rate, gut = messen(posts, SLOTS)
            print(f"{rate:5.3f} Posts/s  ({gut}/{len(posts)} ok)")
            ergebnisse.append((name, extra, rate))
    finally:
        print("\nDienst und Cron wieder einschalten ...")
        ssh("pkill -f 'llama-server .*--port 8081' ; true")
        ssh("sudo systemctl start llama-analyze")
        lokal("crontab -l | sed 's|^#SCHALTER ||' | crontab -")

    if not ergebnisse:
        return 1
    basis = next((r for n, _, r in ergebnisse if n == "Ausgangslage"), None)
    ergebnisse.sort(key=lambda e: -e[2])
    print("\n" + "=" * 62)
    print(f"{'Variante':<32}{'Posts/s':>10}{'ggue. Basis':>14}")
    print("-" * 62)
    for name, _, rate in ergebnisse:
        v = f"{(rate/basis - 1)*100:+6.1f} %" if basis else "     --"
        print(f"{name:<32}{rate:>10.3f}{v:>14}")
    print("=" * 62)

    beste = ergebnisse[0]
    if basis and beste[2] > basis * 1.03:
        print(f"\nBeste Variante: {beste[0]}  ->  {beste[1]}")
        print(f"270000 Posts in {270000/beste[2]/86400:.1f} statt "
              f"{270000/basis/86400:.1f} Tagen")
    else:
        print("\nKeine Variante bringt mehr als 3 Prozent. Die Ausgangslage "
              "bleibt - der Engpass liegt woanders (Bandbreite, Modellgroesse).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

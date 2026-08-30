#!/usr/bin/env python3
"""
bandbreite.py - misst die nutzbare Speicherbandbreite (STREAM-Triade).

a = b + s*c ueber Felder, die weit groesser sind als der L3-Cache (25 MB).
Je Durchgang werden zwei Felder gelesen und eines geschrieben, also 3 x Groesse
an Bytes bewegt. Einmal mit einem Prozess (Grenze eines Kerns) und einmal mit
mehreren (Grenze des Speicherbusses).

    python3 bandbreite.py            # 1, 2, 4, 8, 12 Prozesse
    python3 bandbreite.py --mb 512
"""

import argparse
import multiprocessing as mp
import time

import numpy as np


def triade(mb: int, durchgaenge: int, ergebnis) -> None:
    n = (mb * 1024 * 1024) // 8            # float64
    b = np.ones(n, dtype=np.float64)
    c = np.full(n, 2.0, dtype=np.float64)
    a = np.empty(n, dtype=np.float64)
    np.add(b, c, out=a)                    # einmal warmlaufen, Seiten anlegen

    t0 = time.perf_counter()
    for _ in range(durchgaenge):
        np.multiply(c, 3.0, out=a)
        np.add(a, b, out=a)
    dauer = time.perf_counter() - t0

    # 2 Lesen + 1 Schreiben je Operation, zwei Operationen je Durchgang
    bytes_bewegt = durchgaenge * 2 * 3 * n * 8
    ergebnis.value = bytes_bewegt / dauer / 1e9


def messen(prozesse: int, mb: int, durchgaenge: int) -> float:
    werte = [mp.Value("d", 0.0) for _ in range(prozesse)]
    jobs = [mp.Process(target=triade, args=(mb, durchgaenge, w)) for w in werte]
    for j in jobs:
        j.start()
    for j in jobs:
        j.join()
    return sum(w.value for w in werte)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=256, help="Feldgroesse je Prozess")
    ap.add_argument("--durchgaenge", type=int, default=6)
    args = ap.parse_args()

    print(f"STREAM-Triade, {args.mb} MB je Feld, {args.durchgaenge} Durchgaenge\n")
    print(f"{'Prozesse':>9} {'GB/s':>9} {'je Prozess':>12}")
    print("-" * 32)
    beste = 0.0
    for p in (1, 2, 4, 8, 12):
        gb = messen(p, args.mb, args.durchgaenge)
        beste = max(beste, gb)
        print(f"{p:>9} {gb:>9.1f} {gb/p:>12.1f}")
    print("-" * 32)
    print(f"nutzbare Bandbreite: {beste:.1f} GB/s")


if __name__ == "__main__":
    main()

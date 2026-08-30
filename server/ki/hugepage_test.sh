#!/bin/bash
# hugepage_test.sh - misst, ob grosse Speicherseiten der Inferenz helfen.
#
# Frage: die 25 GB Gewichte liegen per mmap als dateigestuetzte 4-KB-Seiten
# im Cache (RssFile 24,7 GB, AnonHugePages 0). Ein Durchlauf beruehrt damit
# rund 6,5 Millionen Seiten. Die Bandbreitenmessung sprach fuer eine
# Latenzbindung - 37 von 73 GB/s genutzt, und trotzdem hilft weder mehr
# Threads noch mehr Slots. Genau dort setzen grosse Seiten an.
#
# Mit --no-mmap laedt llama.cpp die Gewichte in anonymen Speicher, und der
# kann THP bekommen. Preis: die Dienste teilen sich die Datei nicht mehr im
# Seitencache (Pss war die Haelfte von Rss), jede Instanz braucht eigene 25 GB.
#
# Die Last wird LOKAL erzeugt. Zwei fruehere Anlaeufe scheiterten daran, dass
# der Lastgeber der Analyzer auf heissa war: dieser Test startet den
# Modelldienst neu, der Analyzer dort verliert die Verbindung und beendet
# sich, und gemessen wurde eine stille Maschine - beide Male 0,0 Token/s.
#
# Und: ohne nachweisbare Last wird abgebrochen statt eine Null gemeldet.

set -u
DAUER=${1:-45}
BIN=/home/gh/llama.cpp/build/bin/llama-server
MODELL=/home/gh/models/qwen3-30b-a3b-q6k.gguf
LASTGEBER=/home/gh/last_lokal.py

warten_bis_bereit() {
    for _ in $(seq 1 120); do
        sleep 4
        curl -s -o /dev/null -m 4 http://127.0.0.1:8081/health && return 0
    done
    return 1
}

messen() {
    local name="$1"
    # Last starten und kurz anlaufen lassen, damit alle Slots belegt sind
    nohup python3 "$LASTGEBER" --sekunden $((DAUER + 40)) --parallel 4 \
        > /tmp/last_lokal.log 2>&1 &
    local lastpid=$!
    sleep 15

    local vorher nachher perfout r w
    vorher=$(curl -s -m 8 http://127.0.0.1:8081/metrics | awk '/^llamacpp:tokens_predicted_total /{print $2}')
    perfout=$(sudo perf stat -a -e uncore_imc_free_running/data_read/,uncore_imc_free_running/data_write/ -- sleep "$DAUER" 2>&1)
    nachher=$(curl -s -m 8 http://127.0.0.1:8081/metrics | awk '/^llamacpp:tokens_predicted_total /{print $2}')
    r=$(echo "$perfout" | awk '/data_read/{gsub(/\./,"",$1); gsub(/,/,".",$1); print $1}')
    w=$(echo "$perfout" | awk '/data_write/{gsub(/\./,"",$1); gsub(/,/,".",$1); print $1}')

    local p ahp anon datei
    p=$(pgrep -f "[l]lama-server .*8081" | head -1)
    ahp=$(awk '/^AnonHugePages/{print $2}' /proc/"$p"/smaps_rollup 2>/dev/null)
    anon=$(awk '/^RssAnon/{print $2}' /proc/"$p"/status 2>/dev/null)
    datei=$(awk '/^RssFile/{print $2}' /proc/"$p"/status 2>/dev/null)

    if [ "$((nachher - vorher))" -le 0 ]; then
        echo "  $name: KEINE LAST MESSBAR - Ergebnis waere wertlos, Abbruch."
        tail -3 /tmp/last_lokal.log 2>/dev/null | sed 's/^/      /'
        kill $lastpid 2>/dev/null
        return 1
    fi

    python3 -c "
t = $nachher - $vorher
gb = ($r + $w) * 1.048576 / 1000 / $DAUER
print(f'  {\"$name\":<24} {t/$DAUER:6.1f} Tok/s  {gb:5.1f} GB/s  '
      f'anon {${anon:-0}/1048576:5.1f} GB  Datei {${datei:-0}/1048576:5.1f} GB  '
      f'HugePages {${ahp:-0}/1024:.0f} MB')
"
    kill $lastpid 2>/dev/null
    wait $lastpid 2>/dev/null
    return 0
}

echo "=== Huge-Pages-Vergleich, je $DAUER s unter lokal erzeugter Last ==="
echo

echo "Auftragsdienst anhalten (braucht sonst dieselben 25 GB) ..."
sudo systemctl stop llama-jobs

# --- 1. Ausgangslage: mmap, keine grossen Seiten ---
sudo systemctl restart llama-analyze
warten_bis_bereit || { echo "Dienst kam nicht hoch"; exit 1; }
messen "mmap (Ausgangslage)"

# --- 2. anonym geladen, THP erzwungen ---
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null
sudo systemctl stop llama-analyze
sudo pkill -f "[l]lama-server .*--port 8081"
sleep 3
echo "  Modell anonym laden (25 GB werden von der NVMe kopiert) ..."
sudo -u gh nohup $BIN --model $MODELL --no-mmap --mlock \
    --n-gpu-layers 0 --threads 8 --ctx-size 8192 --parallel 4 \
    --host 0.0.0.0 --port 8081 --no-warmup --metrics \
    > /tmp/hugepage.log 2>&1 &
warten_bis_bereit || { echo "no-mmap-Dienst kam nicht hoch"; tail -5 /tmp/hugepage.log; }
messen "no-mmap + THP always"

# --- Aufraeumen: alles zurueck wie vorher ---
echo
echo "Zuruecksetzen ..."
sudo pkill -f "[l]lama-server .*--port 8081"
echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null
sleep 3
sudo systemctl start llama-analyze
sudo systemctl start llama-jobs
sleep 15
echo "  analyze: $(systemctl is-active llama-analyze)  jobs: $(systemctl is-active llama-jobs)"
echo "  THP:     $(cat /sys/kernel/mm/transparent_hugepage/enabled)"

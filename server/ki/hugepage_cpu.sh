#!/bin/bash
# hugepage_cpu.sh - Huge-Pages-Test mit dem CPU-only-Build.
#
# Der Versuch mit dem CUDA-Build lief ins Leere: dort landeten die Gewichte
# bei --no-mmap in CUDA-Pinned-Host-Speicher (CUDA_Host model buffer size =
# 23680 MiB), nicht in gewoehnlichem anonymem Speicher. THP griff nie,
# AnonHugePages blieb 0, und --mlock scheiterte an der memlock-Grenze von
# 8 MB. Gemessen wurde am Ende nur mmap gegen no-mmap, nicht Huge Pages.
#
# Hier laeuft das CPU-only-Binary (keine CUDA-Bindung), --no-mmap reserviert
# also per malloc, und der Dienst wird ueber systemd-run mit
# LimitMEMLOCK=infinity gestartet, damit --mlock ueberhaupt greifen kann.
#
# Drei Konfigurationen, damit sich Bauweg und Speicherform trennen lassen:
#   1. CUDA-Build, mmap        - der Ist-Zustand, als Bezugspunkt
#   2. CPU-only, mmap          - was allein der Bauweg bringt
#   3. CPU-only, no-mmap+THP   - was Huge Pages darueber hinaus bringen
#
# Ohne nachweisbare Last wird abgebrochen statt eine Null gemeldet.

set -u
DAUER=${1:-45}
CUDA_BIN=/home/gh/llama.cpp/build/bin/llama-server
CPU_BIN=/home/gh/llama.cpp/build-cpu/bin/llama-server
MODELL=/home/gh/models/qwen3-30b-a3b-q6k.gguf
LASTGEBER=/home/gh/last_lokal.py

aufraeumen() {
    sudo systemctl stop llama-test 2>/dev/null
    sudo pkill -f "[l]lama-server .*--port 8081" 2>/dev/null
    sleep 3
}

starten() {   # $1 = Binary, $2... = zusaetzliche Schalter
    local bin="$1"; shift
    aufraeumen
    sudo systemd-run --unit=llama-test --collect \
        --property=User=gh --property=LimitMEMLOCK=infinity \
        --property=WorkingDirectory=/home/gh \
        "$bin" --model "$MODELL" "$@" \
        --n-gpu-layers 0 --threads 8 --ctx-size 8192 --parallel 4 \
        --host 0.0.0.0 --port 8081 --no-warmup --metrics > /dev/null 2>&1
    for _ in $(seq 1 150); do
        sleep 4
        curl -s -o /dev/null -m 4 http://127.0.0.1:8081/health && return 0
    done
    return 1
}

messen() {
    local name="$1"
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

    local p anon datei ahp
    p=$(pgrep -f "[l]lama-server .*8081" | head -1)
    anon=$(awk '/^RssAnon/{print $2}'  /proc/"$p"/status 2>/dev/null)
    datei=$(awk '/^RssFile/{print $2}' /proc/"$p"/status 2>/dev/null)
    ahp=$(awk '/^AnonHugePages/{print $2}' /proc/"$p"/smaps_rollup 2>/dev/null)

    kill $lastpid 2>/dev/null
    if [ "$((nachher - vorher))" -le 0 ]; then
        echo "  $name: KEINE LAST MESSBAR - Abbruch, das Ergebnis waere wertlos."
        return 1
    fi
    # Ausgabe mit awk statt python -c: die verschachtelten Anfuehrungszeichen
    # eines mehrzeiligen f-Strings in einem Shell-String haben den ersten
    # Versuch mit einem Syntaxfehler beendet.
    awk -v n="$name" -v tok="$((nachher - vorher))" -v d="$DAUER" \
        -v r="$r" -v w="$w" -v a="${anon:-0}" -v f="${datei:-0}" -v h="${ahp:-0}" \
        'BEGIN { printf "  %-26s %6.1f Tok/s  %5.1f GB/s  anon %5.1f GB  Datei %5.1f GB  HugePages %6.0f MB\n",
                 n, tok/d, (r+w)*1.048576/1000/d, a/1048576, f/1048576, h/1024 }'
}

echo "=== Huge Pages und Bauweg, je $DAUER s unter lokaler Last ==="
echo
sudo systemctl stop llama-analyze llama-jobs

echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null
starten "$CUDA_BIN" && messen "CUDA-Build, mmap"
starten "$CPU_BIN"  && messen "CPU-only, mmap"

echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null
starten "$CPU_BIN" --no-mmap --mlock && messen "CPU-only, no-mmap+THP"

echo
echo "Zuruecksetzen ..."
aufraeumen
echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null
sudo systemctl start llama-analyze
sudo systemctl start llama-jobs
sleep 15
echo "  analyze: $(systemctl is-active llama-analyze)  jobs: $(systemctl is-active llama-jobs)"
echo "  THP:     $(cat /sys/kernel/mm/transparent_hugepage/enabled)"

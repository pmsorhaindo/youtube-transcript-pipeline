#!/usr/bin/env bash
# Resumable full captions pass: slow pacing, cooldown between slices, periodic linking.
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
PY=${PYTHON:-python3}
LOG=${YTP_LOG:-transcript_run.log}
while true; do
  pending=$($PY scripts/transcripts_youtube.py status 2>/dev/null | $PY -c 'import json,sys;print(json.load(sys.stdin)["status"].get("pending",0))')
  echo "[$(date '+%F %T')] loop start, pending=$pending" >> "$LOG"
  [ "$pending" -eq 0 ] && break
  $PY scripts/transcripts_youtube.py run --sleep 10 --limit 300 >> "$LOG" 2>&1
  $PY scripts/transcripts_youtube.py link >> "$LOG" 2>&1
  echo "[$(date '+%F %T')] slice done, cooling 5 min" >> "$LOG"
  sleep 300
done
$PY scripts/transcripts_youtube.py link >> "$LOG" 2>&1
echo "[$(date '+%F %T')] ALL DONE" >> "$LOG"

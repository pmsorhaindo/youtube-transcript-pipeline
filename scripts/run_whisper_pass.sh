#!/usr/bin/env bash
# After the captions pass: retry failed captions, then Whisper (small, <=60 min) on no_captions, then link.
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
PY=${PYTHON:-python3}
LOG=${YTP_LOG:-transcript_run.log}
echo "[$(date '+%F %T')] retry-failed start" >> "$LOG"
nice -n 15 $PY scripts/transcripts_youtube.py run --retry-failed --sleep 6 >> "$LOG" 2>&1
nice -n 15 $PY scripts/transcripts_youtube.py link >> "$LOG" 2>&1
echo "[$(date '+%F %T')] whisper pass start (small, <=60 min)" >> "$LOG"
nice -n 15 $PY scripts/transcripts_youtube.py run --whisper --whisper-model small --max-whisper-min 60 --sleep 4 >> "$LOG" 2>&1
nice -n 15 $PY scripts/transcripts_youtube.py link >> "$LOG" 2>&1
echo "[$(date '+%F %T')] WHISPER PASS DONE" >> "$LOG"

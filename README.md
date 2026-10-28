# youtube-transcript-pipeline

Checkpointed, resumable English transcript fetcher for a large list of YouTube videos
(e.g. your watch history, likes and playlists from Google Takeout).

- **Captions first** via [yt-dlp](https://github.com/yt-dlp/yt-dlp): manual English captions,
  then auto English (original ASR preferred, machine-translated auto-EN flagged as such).
- **Whisper fallback** via [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for videos
  with no English captions (audio downloaded to a temp dir, translated to English, then deleted).
- **Checkpointing**: an append-only JSONL job log, one line per processed video. Kill it any time
  and rerun; it picks up where it stopped.
- **Rate-limit pacing**: randomised sleep between videos, exponential backoff on HTTP 429,
  and an automatic stop after 8 consecutive failures (throttled or expired cookies).
- **Cookie fallback**: tries anonymously first and retries with your cookies only for
  age/login/bot-check gates.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt      # faster-whisper is optional
cp .env.example .env                 # adjust paths
```

yt-dlp may need a JS runtime (Node) on `PATH` for some YouTube extractions.

## Input

Put any of these JSONL files in `$YTP_DATA_DIR/sources/youtube/` (one JSON object per line):

- `watch_history.jsonl` – `{"id": "<videoId>", "title": "...", "is_short": false}`
- `liked.jsonl` – `{"id": "<videoId>", "title": "..."}`
- `playlist_items.jsonl` – `{"video_id": "<videoId>"}`

## Usage

```bash
set -a; . ./.env; set +a
python scripts/transcripts_youtube.py init            # build the job list (status=pending)
python scripts/transcripts_youtube.py run --limit 50  # fetch captions, checkpoint per video
python scripts/transcripts_youtube.py run --whisper --whisper-model small --max-whisper-min 60
python scripts/transcripts_youtube.py run --ids VIDEO_ID1,VIDEO_ID2   # ad hoc
python scripts/transcripts_youtube.py status
python scripts/transcripts_youtube.py link [--no-fts] # add transcript refs to input rows (+ optional SQLite FTS)
```

Helpers: `scripts/run_captions_loop.sh` (slow, sliced full pass with cooldowns) and
`scripts/run_whisper_pass.sh` (retry failures, then Whisper on caption-less videos).

Output: one `<videoId>.json` per video in `$YTP_OUT_DIR` with `text`, timed `segments`,
`method` (`captions_manual`, `captions_auto`, `captions_auto_translated`, `whisper`) and metadata.

`link` optionally patches a SQLite FTS5 table `diet_fts(id, source, body, tags)` at `$YTP_FTS_DB`;
skipped if the file doesn't exist.

## Cookies (never commit them)

Some videos (age-restricted, members-only, bot checks) need a signed-in session. Export cookies for
youtube.com in Netscape format (e.g. with a "cookies.txt" browser extension or
`yt-dlp --cookies-from-browser firefox --cookies cookies.txt`), save the file to the path in
`YTP_COOKIES`, and `chmod 600` it. The `.gitignore` excludes `data/`, `private/`, `*cookies*.txt`
and `.env`. Use a throwaway account if you can; heavy automated use can get an account flagged.

## License

MIT

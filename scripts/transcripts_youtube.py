#!/usr/bin/env python3
"""Checkpointed English transcript fetcher for the YouTube part of the archive.

Subcommands
  init                 collect unique video IDs (watch + liked + playlist) -> job file (pending)
  run [--limit N] [--ids a,b] [--whisper] [--max-whisper-min M]
                       process pending/failed videos; checkpoint after each one (resume-safe)
  link [--no-fts]      write transcript refs onto watch/liked/playlist/records rows + patch FTS
  status               counts by status/method

Paths (env-overridable, see .env.example):
Job state: $YTP_DATA_DIR/sources/youtube/transcript_job.jsonl  (append-only log, last line per id wins;
           `status`/`init` compact it to one line per video).
Output:    media/transcripts/youtube/<id>.json
Cookies:   sources/youtube/private/cookies.txt (Netscape; chmod 600; never logged)

Fetch order per video
  1. yt-dlp metadata anonymously; if login/age/bot gate -> retry with cookies
     (--cookies-mode always uses cookies from the start).
  2. manual EN captions (en, en-*), else auto EN (en-orig / en; translated auto-en flagged)
  3. no EN captions -> no_captions (or Whisper if --whisper and duration <= cap)
  4. unavailable only after the cookie retry also fails with a permanent error.
"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, tempfile, time, random, sqlite3
from datetime import datetime, timezone
from pathlib import Path

# All locations are configurable; defaults are relative to the current directory.
ROOT = Path(os.environ.get("YTP_DATA_DIR", "./data")).resolve()
YT = Path(os.environ.get("YTP_SOURCES_DIR", ROOT / "sources" / "youtube"))
JOB = YT / "transcript_job.jsonl"
OUT = Path(os.environ.get("YTP_OUT_DIR", ROOT / "media" / "transcripts" / "youtube"))
COOKIES = Path(os.environ.get("YTP_COOKIES", YT / "private" / "cookies.txt"))
FTS_DB = Path(os.environ.get("YTP_FTS_DB", ROOT / "search" / "diet.db"))

GATE = re.compile(r"sign in to confirm|age|inappropriate|login|cookies|members-only|not a bot|confirm your", re.I)
PERMANENT = re.compile(r"private video|video unavailable|has been removed|terminated|not available in your country|"
                       r"copyright|does not exist|is no longer available|This video is unavailable|premieres in|"
                       r"available to this channel's members|members-only", re.I)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jsonl_atomic(p: Path, rows: list[dict]) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)


def load_job() -> dict[str, dict]:
    st: dict[str, dict] = {}
    for r in read_jsonl(JOB):
        st[r["id"]] = {**st.get(r["id"], {}), **r}
    return st


def append_job(row: dict) -> None:
    with JOB.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())


def compact(st: dict[str, dict]) -> None:
    write_jsonl_atomic(JOB, list(st.values()))


def collect() -> dict[str, dict]:
    vids: dict[str, dict] = {}
    for w in read_jsonl(YT / "watch_history.jsonl"):
        vids.setdefault(w["id"], {"id": w["id"], "title": w.get("title"), "is_short": w.get("is_short"), "sources": []})["sources"].append("watch")
    for l in read_jsonl(YT / "liked.jsonl"):
        v = vids.setdefault(l["id"], {"id": l["id"], "title": l.get("title"), "is_short": None, "sources": []})
        v["sources"].append("liked"); v["title"] = v["title"] or l.get("title")
    for it in read_jsonl(YT / "playlist_items.jsonl"):
        v = vids.setdefault(it["video_id"], {"id": it["video_id"], "title": None, "is_short": None, "sources": []})
        if "playlist" not in v["sources"]:
            v["sources"].append("playlist")
    for v in vids.values():
        v["sources"] = sorted(set(v["sources"]))
    return vids


# ------------------------------------------------------------------ fetching
def ydl_opts(cookies: bool) -> dict:
    o = {"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True,
         "js_runtimes": {"node": {}}, "socket_timeout": 30, "retries": 3, "extractor_retries": 2}
    if cookies:
        o["cookiefile"] = str(COOKIES)
    return o


def extract(vid: str, cookies: bool) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(ydl_opts(cookies)) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        info = y.sanitize_info(info)
    if cookies:
        os.chmod(COOKIES, 0o600)
    return info


def fetch_url(url: str, cookies: bool) -> bytes:
    import yt_dlp
    with yt_dlp.YoutubeDL(ydl_opts(cookies)) as y:
        return y.urlopen(url).read()


def pick_track(info: dict) -> tuple[str, str, list] | None:
    """-> (method, lang, formats). Prefer manual English, then auto original-English, then auto-translated."""
    subs = info.get("subtitles") or {}
    for k in sorted(subs, key=lambda k: (k != "en", k)):
        if (k == "en" or k.startswith("en-")) and subs[k]:
            return "captions_manual", k, subs[k]
    auto = info.get("automatic_captions") or {}
    orig_lang = (info.get("language") or "").split("-")[0]
    if "en-orig" in auto:
        return "captions_auto", "en-orig", auto["en-orig"]
    if "en" in auto and auto["en"]:
        # auto 'en' is the ASR track when the video is English; a machine translation otherwise
        if orig_lang in ("", "en") and not any(k.endswith("-orig") and not k.startswith("en") for k in auto):
            return "captions_auto", "en", auto["en"]
        return "captions_auto_translated", "en", auto["en"]
    return None


def parse_json3(raw: bytes) -> list[dict]:
    d = json.loads(raw)
    segs = []
    for ev in d.get("events") or []:
        if "segs" not in ev:
            continue
        t = "".join(s.get("utf8", "") for s in ev["segs"]).replace("\n", " ").strip()
        if not t:
            continue
        segs.append({"start": round(ev.get("tStartMs", 0) / 1000, 2),
                     "dur": round(ev.get("dDurationMs", 0) / 1000, 2), "text": t})
    return segs


def parse_vtt(raw: bytes) -> list[dict]:
    segs, cur = [], None
    def ts(s):
        h, m, rest = (["0"] + s.split(":"))[-3:]
        return int(h) * 3600 + int(m) * 60 + float(rest)
    for line in raw.decode("utf-8", "replace").splitlines():
        if "-->" in line:
            a, b = [x.strip().split(" ")[0] for x in line.split("-->")]
            cur = {"start": round(ts(a), 2), "dur": round(ts(b) - ts(a), 2), "text": ""}
            segs.append(cur)
        elif cur is not None and line.strip():
            cur["text"] = (cur["text"] + " " + re.sub(r"<[^>]+>", "", line).strip()).strip()
    out = []
    for s in segs:  # drop rolling duplicates from auto vtt
        if s["text"] and (not out or s["text"] != out[-1]["text"]):
            out.append(s)
    return out


def segs_to_text(segs: list[dict]) -> str:
    text, prev = [], ""
    for s in segs:
        t = s["text"]
        if t != prev:
            text.append(t)
        prev = t
    return re.sub(r"\s+", " ", " ".join(text)).strip()


def captions(vid: str, info: dict, ck: bool, args, row: dict):
    pick = pick_track(info)
    if not pick:
        return None
    method, lang, fmts = pick
    by_ext = {f.get("ext"): f for f in fmts}
    for ext in ("json3", "vtt"):
        f = by_ext.get(ext)
        if not f:
            continue
        raw = None
        for k in range(args.caption_retries + 1):
            try:
                raw = fetch_url(f["url"], ck)
                break
            except Exception as e:  # noqa
                msg = str(e)[:200]
                row.update(status="failed", error=f"caption download: {msg}")
                if "429" in msg and k < args.caption_retries:
                    wait = args.backoff * (2 ** k)
                    print(f"  429 on captions for {vid}; backing off {wait:.0f}s", flush=True)
                    time.sleep(wait)
                else:
                    break
        if raw is None:
            continue
        try:
            segs = parse_json3(raw) if ext == "json3" else parse_vtt(raw)
        except Exception:  # noqa
            segs = []
        text = segs_to_text(segs)
        if text:
            return method, lang, text, segs
        row.update(status="failed", error=f"empty caption track ({ext}, cookies={ck})")
    return None


def whisper(vid: str, cookies: bool, model_name: str) -> tuple[str, list[dict], str]:
    import yt_dlp
    from faster_whisper import WhisperModel
    with tempfile.TemporaryDirectory() as td:
        o = {**ydl_opts(cookies), "skip_download": False, "format": "bestaudio/best",
             "outtmpl": f"{td}/%(id)s.%(ext)s"}
        with yt_dlp.YoutubeDL(o) as y:
            y.download([f"https://www.youtube.com/watch?v={vid}"])
        audio = next(Path(td).iterdir())
        m = WhisperModel(model_name, device="cpu", compute_type="int8")
        it, inf = m.transcribe(str(audio), task="translate", vad_filter=True, beam_size=5)
        segs = [{"start": round(s.start, 2), "dur": round(s.end - s.start, 2), "text": s.text.strip()} for s in it]
    return segs_to_text(segs), segs, inf.language


def process(vid: str, meta: dict, args) -> dict:
    row = {"id": vid, "updated_at": now()}
    info, used_cookies, err = None, False, None
    attempts = [True] if args.cookies_mode == "always" else [False, True]
    for ck in attempts:
        try:
            info = extract(vid, ck); used_cookies = ck; err = None
            break
        except Exception as e:  # noqa
            err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:300]
            # any anonymous failure (gate, bot-check, or 'unavailable') gets one cookie retry
    if info is None:
        perm = bool(PERMANENT.search(err or ""))
        row.update(status="unavailable" if perm else "failed", error=err, cookie_retry=True)
        return row
    row.update(title=info.get("title"), duration=info.get("duration"), age_limit=info.get("age_limit"),
               used_cookies=used_cookies, video_lang=info.get("language"))
    pick = pick_track(info)
    if pick:
        res = captions(vid, info, used_cookies, args, row)
        if res is None and not used_cookies:
            # anonymous caption URLs can come back empty/429 (esp. age-gated) -> re-extract signed-in
            try:
                info = extract(vid, True); used_cookies = True
                row.update(used_cookies=True, age_limit=info.get("age_limit"))
                pick = pick_track(info)
                res = captions(vid, info, True, args, row) if pick else None
            except Exception as e:  # noqa
                row.update(status="failed", error=f"cookie re-extract: {str(e)[:200]}")
        if res is not None:
            method, lang, text, segs = res
            doc = {"id": vid, "title": info.get("title"), "channel": info.get("channel"),
                   "duration": info.get("duration"), "method": method, "lang": lang,
                   "video_lang": info.get("language"), "age_limit": info.get("age_limit"),
                   "used_cookies": used_cookies, "fetched_at": now(), "chars": len(text),
                   "text": text, "segments": segs}
            write_doc(vid, doc)
            row.update(status="done", method=method, lang=lang, chars=len(text), error=None)
            return row
    if not pick:
        row.update(status="no_captions", error=None)
    if args.whisper and (info.get("duration") or 1e9) <= args.max_whisper_min * 60 and not info.get("is_live"):
        try:
            text, segs, src_lang = whisper(vid, used_cookies, args.whisper_model)
            if text:
                doc = {"id": vid, "title": info.get("title"), "channel": info.get("channel"),
                       "duration": info.get("duration"), "method": "whisper", "lang": "en",
                       "video_lang": src_lang, "whisper_model": args.whisper_model,
                       "age_limit": info.get("age_limit"), "used_cookies": used_cookies,
                       "fetched_at": now(), "chars": len(text), "text": text, "segments": segs}
                write_doc(vid, doc)
                row.update(status="done", method="whisper", lang="en", chars=len(text), error=None)
        except Exception as e:  # noqa
            row["whisper_error"] = str(e)[:200]
    return row


def write_doc(vid: str, doc: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{vid}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# ------------------------------------------------------------------ commands
def cmd_init(args) -> None:
    st = load_job()
    vids = collect()
    added = 0
    for vid, v in vids.items():
        if vid not in st:
            st[vid] = {"id": vid, "status": "pending", "title": v["title"], "is_short": v["is_short"],
                       "sources": v["sources"], "updated_at": now()}
            added += 1
        else:
            st[vid]["sources"] = v["sources"]
    compact(st)
    print(f"videos={len(vids)} job_rows={len(st)} added={added}")


def cmd_run(args) -> None:
    if not COOKIES.exists():
        sys.exit(f"missing cookie jar {COOKIES} (set YTP_COOKIES; see README)")
    YT.mkdir(parents=True, exist_ok=True)
    st = load_job()
    if args.ids:
        todo = [i.strip() for i in args.ids.split(",") if i.strip()]
        for i in todo:
            st.setdefault(i, {"id": i, "status": "pending", "sources": ["adhoc"]})
    else:
        want = {"pending"} | ({"failed"} if args.retry_failed else set()) | ({"no_captions"} if args.whisper else set())
        todo = [i for i, r in st.items() if r.get("status") in want]
    if args.limit:
        todo = todo[: args.limit]
    print(f"processing {len(todo)}", flush=True)
    consecutive_fail = 0
    for n, vid in enumerate(todo, 1):
        meta = st.get(vid, {})
        if meta.get("status") == "done" and not args.force and (OUT / f"{vid}.json").exists():
            continue
        row = process(vid, meta, args)
        row["attempts"] = int(meta.get("attempts") or 0) + 1
        st[vid] = {**meta, **row}
        append_job(st[vid])  # checkpoint
        print(json.dumps({"n": n, "id": vid, "status": row["status"], "method": row.get("method"),
                          "chars": row.get("chars"), "cookies": row.get("used_cookies"),
                          "age": row.get("age_limit"), "err": (row.get("error") or "")[:120],
                          "title": (row.get("title") or meta.get("title") or "")[:60]}, ensure_ascii=False), flush=True)
        consecutive_fail = consecutive_fail + 1 if row["status"] == "failed" else 0
        if consecutive_fail >= 8:
            print("8 consecutive failures - likely throttled or cookies expired; stopping (resume later)", flush=True)
            break
        time.sleep(args.sleep + random.random() * args.sleep)
    compact(load_job())


def transcript_ref(vid: str) -> dict | None:
    p = OUT / f"{vid}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return {"path": f"media/transcripts/youtube/{vid}.json", "method": d["method"], "lang": d["lang"],
            "chars": d["chars"], "fetched_at": d["fetched_at"]}


def cmd_link(args) -> None:
    refs = {p.stem: transcript_ref(p.stem) for p in OUT.glob("*.json")}
    refs = {k: v for k, v in refs.items() if v}
    counts = {}
    for name, key in (("watch_history.jsonl", "id"), ("liked.jsonl", "id"),
                      ("playlist_items.jsonl", "video_id"), ("records.jsonl", "id")):
        p = YT / name
        rows = read_jsonl(p)
        c = 0
        for r in rows:
            ref = refs.get(r.get(key))
            if ref and r.get("kind") not in ("post_view", "search", "subscription", "comment", "live_chat", "clip", "music_library"):
                if r.get("transcript") != ref:
                    r["transcript"] = ref; c += 1
                if name == "records.jsonl":
                    tags = r.setdefault("tags", [])
                    if "transcript" not in tags:
                        tags.append("transcript")
        if c:
            write_jsonl_atomic(p, rows)
        counts[name] = c
    db = None
    if not args.no_fts:
        db = FTS_DB
        if not db.exists():
            print(f"no FTS db at {db}; skipping FTS patch"); db = None
    if not args.no_fts and db:
        conn = sqlite3.connect(db)
        n = 0
        for vid in refs:
            d = json.loads((OUT / f"{vid}.json").read_text(encoding="utf-8"))
            row = conn.execute("SELECT rowid, body, tags FROM diet_fts WHERE id=? AND source='youtube'", (vid,)).fetchone()
            if not row:
                continue
            body = row[1].split("\n\nTranscript:\n")[0] + "\n\nTranscript:\n" + d["text"]
            tags = row[2] if "transcript" in (row[2] or "").split() else ((row[2] or "") + " transcript").strip()
            conn.execute("UPDATE diet_fts SET body=?, tags=? WHERE rowid=?", (body, tags, row[0]))
            n += 1
        conn.commit(); conn.close()
        counts["fts_rows_patched"] = n
    print(json.dumps(counts))


def cmd_status(args) -> None:
    from collections import Counter
    st = load_job()
    compact(st)
    print(json.dumps({"total": len(st), "status": Counter(r.get("status") for r in st.values()),
                      "method": Counter(r.get("method") for r in st.values() if r.get("status") == "done")}, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init")
    r = sp.add_parser("run")
    r.add_argument("--limit", type=int); r.add_argument("--ids")
    r.add_argument("--retry-failed", action="store_true"); r.add_argument("--force", action="store_true")
    r.add_argument("--cookies-mode", choices=["fallback", "always"], default="fallback")
    r.add_argument("--sleep", type=float, default=4.0)
    r.add_argument("--caption-retries", type=int, default=3); r.add_argument("--backoff", type=float, default=30)
    r.add_argument("--whisper", action="store_true"); r.add_argument("--whisper-model", default="large-v3")
    r.add_argument("--max-whisper-min", type=float, default=120)
    l = sp.add_parser("link"); l.add_argument("--no-fts", action="store_true")
    sp.add_parser("status")
    a = ap.parse_args()
    {"init": cmd_init, "run": cmd_run, "link": cmd_link, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()

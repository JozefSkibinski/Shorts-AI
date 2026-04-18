# Shorts-AI

This repo contains two things:

1. **The scraper** (top-level `scraper.py` / `main.py`) — already shipped.
   Opens YouTube Shorts in an incognito Playwright context and logs every
   Short above a view threshold to `output/shorts.jsonl` + `output/data.txt`
   + `output/stats.txt`.
2. **`shorts_ai/`** — the RAG + tool-use agent being built on top of that
   corpus per `MVP Build Spec`. Phase 1 (data foundation) lives there.

---

## Pre-Phase-1 answers (what the scraper actually emits)

The MVP spec asks four scoping questions before any new code is written.
Answering them up front so Phase 1 lines up with reality.

### 1. What does the existing scraper emit?

- **`output/shorts.jsonl`** — one JSON object per line, append-only. This
  is the structured source of truth the bridge ingests. Schema (matches
  the `ShortRecord` dataclass in `scraper.py`):

  ```json
  {
    "video_id":          "abc123XYZ_-",
    "url":               "https://www.youtube.com/shorts/<id>",
    "channel":           "Channel Name",
    "title":             "Short title",
    "description":       "caption text...",
    "hashtags":          ["#viral", "#funny"],
    "keywords":          ["viral", "funny", "..."],
    "view_count":        12345678,
    "duration_seconds":  38.0,
    "transcript":        "full transcript text or null",
    "hook":              "first ~7s of speech or null",
    "metadata_source":   "yt-dlp | html:/shorts/... | interceptor | none",
    "transcript_source": "captions | whisper | none"
  }
  ```

- **`output/data.txt`** — compact human-readable mirror of the same data.
  Not used by the bridge; for human eyeballing only.
- **`output/stats.txt`** — ranked aggregates regenerated at end of every
  scrape. Reference for what "performance" looks like before any ML.
- **Update cadence**: append-only as the scraper scrolls. Re-running the
  scraper resumes from `shorts.jsonl` and only adds new IDs. No deletes,
  no in-place updates.

### 2. Is there video file storage, or only metadata?

Only metadata. The scraper never downloads the video file — it pulls
metadata via yt-dlp's `extract_info(download=False)` and pulls audio
only when transcribing a missing-caption Short (and deletes it
immediately after Whisper runs).

**Implication for Phase 1**: the enrichment pipeline must download
the video itself (yt-dlp) when it needs visual embeddings or audio
fingerprints. We do this on-demand per video and clean up afterwards;
no persistent media store in MVP.

### 3. Niche coverage of the corpus

Untyped today — niches are not classified at scrape time. Whatever
surfaces in the YouTube Shorts feed during a run gets logged. So the
corpus is whatever Shorts the algorithm serves to a fresh signed-out
session, weighted by current popularity.

**Implication for Phase 1**: every video gets niche-classified by the
enrichment pipeline (single Haiku call on title + transcript +
hashtags against the 20-slug taxonomy). Coverage of the 20 starter
slugs will be uneven; we expect a long tail and `other` will be
common until the corpus grows.

### 4. GPU available for Whisper / CLIP?

Assume **CPU only** as the default deployment target. The scraper
already runs `faster-whisper` `tiny` on CPU at ~1–3s per Short and
that's acceptable for the enrichment throughput we need.

For Phase 1:

- Whisper: `faster-whisper` with `small.en` on CPU (int8). Spec says
  `WHISPER_MODEL=small.en` is the default, override to `medium.en` via
  env var when a GPU is available.
- CLIP: `open_clip` `ViT-B-32` runs on CPU but slow (~1–2s per frame).
  We'll embed only 3 frames per Short (first, +3s, midpoint).
- Anything heavier than that goes through API alternatives later (e.g.
  Voyage embeddings) — not in the MVP.

---

## Distributed scraping

When run on N machines simultaneously, each scraper writes **batch files**
(gzipped JSONL) under `output/batches/<YYYY>/<MM>/<DD>/<machine_id>/<session>-<hour>.jsonl.gz`
in addition to the legacy `shorts.jsonl`. Each batch is bracketed by a
`batch_header` (machine identity, session envelope) and a `batch_footer`
(record count, ads filtered).

The persistent machine UUID lives in `$XDG_STATE_HOME/shorts-scraper/machine_id`
and survives restarts — re-running the scraper on the same box keeps the
same identity, so the ingestor can attribute snapshots correctly.

Upload strategy is left to the deploy: sync the `batches/` tree to S3/R2
with any tool you prefer (`aws s3 sync`, `rclone`, `rsync` to a shared
volume). The ingestor reads the same layout from either a local
directory or an S3 bucket — no code changes.

## Ingestion (one-per-fleet central process)

```bash
cd shorts_ai
# local staging (dev, or single-machine deploys):
python -m app.ingestor.main --root ../output/batches --watch

# S3-backed staging:
python -m app.ingestor.main --s3-bucket shorts-raw --s3-prefix batches/ --watch
```

What it does:

1. Parses each batch (gzip + JSONL; missing footer is tolerated, missing
   or garbage header is quarantined).
2. Upserts `scraper_machines` / `scraper_sessions` from the header.
3. For every observation:
   - exact-dedup upsert into `videos` (keyed by `source_video_id`);
     `first_seen_at` is set on insert and never overwritten, `last_seen_at`
     is refreshed on every observation.
   - appends a `video_metrics_snapshots` row tagged with
     `source_machine_id` + `source_session_id`. Observations from the
     same machine within 5 minutes with view-count delta < 100 are
     collapsed to prevent per-loop bloat.
4. Enqueues enrichment via Celery exactly once per new video.
5. Records the batch in `raw_batches` (`pending`/`ingested`/`failed`/`quarantined`)
   and moves successfully-ingested keys to the `archived/` prefix.

**Do not delete raw batches after ingest** — keep them so you can replay
with improved dedup logic later.

## Near-duplicate clustering

Enrichment computes `phash_first`, `phash_mid`, and `audio_chromaprint`
alongside the existing CLIP / text embeddings. A nightly Celery beat
task (`cluster_unassigned`, scheduled 03:10 UTC) groups videos with ≥2
confirming signals (pHash Hamming < 5, same chromaprint, CLIP cosine
< 0.08, text cosine < 0.10) into `content_clusters`. The representative
for each cluster is the member with the highest `peak_velocity`.

Run manually:

```bash
cd shorts_ai
python -c "from workers.celery_app import cluster_unassigned; print(cluster_unassigned())"
```

Collapse top-performers by cluster (the query the agent uses):

```sql
SELECT DISTINCT ON (v.content_cluster_id) v.id, v.url, va.peak_velocity
FROM videos v
JOIN video_analyses va ON va.video_id = v.id
WHERE v.niche_id = $1 AND v.is_ad = false
ORDER BY v.content_cluster_id, va.peak_velocity DESC
LIMIT 20;
```

## Fleet health dashboard

```sql
SELECT
  date_trunc('hour', captured_at)   AS hour,
  COUNT(DISTINCT source_machine_id) AS active_machines,
  COUNT(DISTINCT video_id)          AS unique_videos,
  COUNT(*)                          AS total_snapshots,
  ROUND(COUNT(*)::numeric
        / NULLIF(COUNT(DISTINCT video_id), 0), 2) AS snapshots_per_video
FROM video_metrics_snapshots
WHERE captured_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1 DESC;
```

`snapshots_per_video` > 1.5 means machines are seeing overlap (good —
velocity data). Close to 1.0 means the feeds are suspiciously disjoint —
either niche specialization is too aggressive or session rotation
isn't clearing state fast enough.

---

## Phase 1 — running the data foundation

```bash
cd shorts_ai
cp .env.example .env                           # then fill in ANTHROPIC_API_KEY etc.
docker compose up -d postgres redis
pip install -r requirements.txt
alembic upgrade head
python -m app.scripts.seed_niches
celery -A workers.celery_app worker --loglevel=info &
python -m app.scraper_bridge --backfill        # ingest existing scraper output
python -m app.scraper_bridge --watch &         # tail new lines as the scraper runs
uvicorn app.api.main:app --reload
```

Verify:

```bash
curl http://localhost:8000/health
curl 'http://localhost:8000/trending/finance_money?timeframe_hours=72&limit=5'
```

Tests (no DB required):

```bash
cd shorts_ai && pytest tests/
```

Phase 1 exit criteria (per spec):

- 1000+ scraped videos fully enriched in DB
- `SELECT COUNT(*) FROM video_analyses WHERE text_embedding IS NULL` returns 0
- Vector search works
- Velocity metrics populated and sortable per niche

---

## The scraper (existing)

Automated YouTube Shorts scraper. Opens `youtube.com/shorts` in an incognito
Playwright context (no cookies, no account, no algorithm pollution),
auto-scrolls through the feed, and logs every Short over a view threshold.

For each qualifying Short (default: **>= 1,000,000 views**) it records:

- channel name
- title
- hashtags (parsed from title, description, and tags)
- description / caption
- video length
- direct link (`https://www.youtube.com/shorts/<id>`)
- full transcript (when captions are available)
- hook — the first ~7 seconds of the transcript

## Install

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```bash
# Default: scan up to 500 Shorts, keep only >=1M views, headed window
python main.py

# Headless, larger run, custom output folder
python main.py --headless --max-videos 2000 --output-dir data/

# Lower the view threshold
python main.py --min-views 500000
```

Output goes to:

- `output/data.txt` — compact one-block-per-Short log
- `output/shorts.jsonl` — structured JSON per line (machine-readable mirror)
- `output/stats.txt` — ranked statistics, regenerated at end of every run

`data.txt` and `shorts.jsonl` are append-only, so you can kill a run
at any time without losing progress. Re-running the scraper auto-skips
Shorts that are already in `shorts.jsonl`.

Each `data.txt` block looks like:

```
[12,345,678v 38s] Channel Name | Title of the Short
  url:  https://www.youtube.com/shorts/<id>
  tags: #viral #funny
  src:  yt-dlp / captions
  desc: short description
  hook: first seven seconds of speech
  text: full transcript on one line
--
```

Empty fields (no description, no hook, etc.) are omitted to keep the
file small.

## Statistics

`output/stats.txt` ranks the dataset to surface what performs best.
Sections:

- Top hashtags by total views and by avg views (min 3 videos)
- Top yt-dlp keyword tags by total views
- Top channels by total and avg views
- Duration buckets (which lengths perform best)
- Hook vocabulary by avg views — which words show up in the best hooks

Recompute against an existing `shorts.jsonl` without scraping:

```bash
python main.py --stats-only
```

## How view counts are resolved

The browser is used only for **incognito scroll discovery**. For each
Short the ID is then resolved through three independent sources, first
match wins:

1. **yt-dlp** — `extract_info` returns the exact integer `view_count`.
2. **HTML parse** — fetch `/shorts/<id>` (and `/watch?v=<id>` as backup)
   and extract `videoDetails.viewCount` from `ytInitialPlayerResponse`.
3. **Response interceptor** — any `/youtubei/v1/player` response caught
   while the feed is playing.

The `Source:` line in each `data.txt` block tells you which source
produced that record, so if views look wrong you know where to look.

## How the transcript/hook is resolved

1. **Captions** — `youtube-transcript-api` (no API key needed).
2. **Whisper** — if captions are unavailable, the Short's audio is
   downloaded with yt-dlp and transcribed locally with `faster-whisper`.
   The `tiny` model runs on CPU in ~1–3s per Short and is loaded once
   per run.

The hook is always the concatenated text from the first 7 seconds of
whichever transcript succeeded. Each `data.txt` block includes a
`Transcript source:` line (`captions`, `whisper`, or `none`).

Relevant flags:

- `--whisper-model tiny|base|small|medium` (default `tiny`)
- `--no-whisper` to skip the audio-transcription fallback

Whisper needs `ffmpeg` on the system PATH — install via your package
manager (`brew install ffmpeg`, `apt install ffmpeg`, etc).

## Notes

- The browser launches with `--incognito` and a fresh context with no
  `storage_state`, so no cookies or history follow between runs.
- Scrolling uses `ArrowDown`, which YouTube treats as "next Short".
  Random 1.5–2.8s delays between advances mimic human pacing.

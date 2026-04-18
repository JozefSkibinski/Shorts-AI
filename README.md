# Shorts-AI

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

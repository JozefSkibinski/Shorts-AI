# Shorts-AI

Automated YouTube Shorts scraper. Opens `youtube.com/shorts` in an incognito
Playwright context (no cookies, no account, no algorithm pollution),
auto-scrolls through the feed, and logs every Short over a view threshold.

For each qualifying Short (default: **>= 1,000,000 views**) it records:

- channel name
- title
- hashtags (parsed from title + description)
- description / caption
- video length (seconds)
- direct link (`https://www.youtube.com/shorts/<id>`)
- full transcript (when captions are available)
- hook (the first ~7 seconds of the transcript)

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

Results are streamed to:

- `output/shorts.csv`  — one row per qualifying Short
- `output/shorts.jsonl` — same data, JSON per line (keeps list fields intact)

Rows are appended as they are found, so you can kill the run at any time
without losing progress.

## Notes

- The browser launches with `--incognito` and a fresh context with no
  `storage_state`, so no cookies or history follow between runs.
- Scrolling uses `ArrowDown`, which YouTube treats as "next Short". Random
  1.5–2.8s delays are used between advances to mimic human pacing.
- Transcripts come from `youtube-transcript-api` (no API key needed). Shorts
  without captions simply get `transcript: null`.
- View counts are parsed from the visible label (e.g. `1.2M views`), so the
  threshold is approximate by ~1% at the boundary.

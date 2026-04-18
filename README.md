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

- `output/data.txt` — human-readable blocks, one per qualifying Short
- `output/shorts.jsonl` — same data as structured JSON per line

Both files are append-only — you can kill the run at any time without
losing progress.

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

## Notes

- The browser launches with `--incognito` and a fresh context with no
  `storage_state`, so no cookies or history follow between runs.
- Scrolling uses `ArrowDown`, which YouTube treats as "next Short".
  Random 1.5–2.8s delays between advances mimic human pacing.
- Transcripts come from `youtube-transcript-api` (no API key needed).
  Shorts without captions record `(no captions)` in the hook/transcript
  fields.

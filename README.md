# Shorts-AI — Discord YouTube-Trends Bot

A production-ready Discord bot that surfaces YouTube search-trend data from
Google Trends through slash commands.

## Features

| Command | What it does |
|---|---|
| `/youtube_trends` | Top + rising YouTube searches related to a keyword, optionally scoped by region and timeframe. |
| `/top_youtube_trends` | Top YouTube trend queries for a region, seeded from a broad term (defaults to `"youtube"`). |
| `/compare_trends` | Compares 2-5 keywords and, if you want, attaches a line chart of their interest over time. |

- Slash commands with autocomplete for region & timeframe.
- Embeds are colour-coded, paginated where useful, and include the source/region/timeframe footer.
- Rate limits, missing data, bad region codes, network errors and Discord API hiccups are all handled gracefully.
- Every blocking call to `pytrends` is dispatched to a worker thread so the Discord gateway stays responsive.
- Logging is configured centrally; set `LOG_LEVEL=DEBUG` when debugging.

## Project layout

```
Shorts-AI/
├── main.py                         # Entry point
├── requirements.txt
├── .env.example
├── README.md
└── bot/
    ├── __init__.py
    ├── bot.py                      # commands.Bot subclass + lifecycle
    ├── config.py                   # Settings loaded from .env / environment
    ├── logger.py                   # Root logging setup
    ├── cogs/
    │   ├── __init__.py
    │   └── trends.py               # The three slash commands
    ├── services/
    │   ├── __init__.py
    │   └── trends_service.py       # Async wrapper around pytrends
    └── utils/
        ├── __init__.py
        ├── validators.py           # Input validation & normalisation
        ├── embeds.py               # Discord embed builders
        └── charts.py               # matplotlib PNG rendering
```

## Setup

### 1. Clone & create a virtual environment

```bash
git clone https://github.com/BeFraid1/Shorts-AI.git
cd Shorts-AI
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create a Discord application & bot

1. Go to <https://discord.com/developers/applications> and create a new application.
2. Inside the application, open the **Bot** tab and add a bot.
3. Copy the bot **token** (you'll paste it into `.env` in the next step).
4. Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands`
   scopes. Under bot permissions pick at least `Send Messages` and
   `Embed Links`. Open the generated URL and invite the bot to your server.

### 4. Configure environment variables

```bash
cp .env.example .env
# then edit .env and set DISCORD_BOT_TOKEN=...
```

Optional values:

- `DISCORD_GUILD_ID` — while developing, set this to your test server's ID so
  slash commands appear instantly. Leave blank for a global deployment (first
  sync can take up to an hour to propagate).
- `PYTRENDS_PROXY` — set an HTTPS proxy if your server IP is being rate-limited.
- `PYTRENDS_TIMEOUT_CONNECT` / `PYTRENDS_TIMEOUT_READ` — tune network timeouts.
- `LOG_LEVEL` — `DEBUG`, `INFO` (default), `WARNING`, `ERROR`.

### 5. Run the bot

```bash
python main.py
```

You should see log lines like:

```
2026-04-20 12:34:56,789 | INFO     | bot.bot | Synced 3 slash commands globally
2026-04-20 12:34:56,789 | INFO     | bot.bot | Logged in as ShortsAI#1234 (id=...)
```

Invite the bot to a server and try `/youtube_trends keyword: python`.

## Usage examples

```
/youtube_trends keyword: "lofi beats" region: United States timeframe: Past 30 days
/top_youtube_trends region: India seed: cricket limit: 15
/compare_trends keywords: "python, rust, go, zig" region: Worldwide timeframe: Past 12 months
```

## Limitations of Google Trends / pytrends

- **No official "YouTube trends" endpoint.** Google Trends exposes a `gprop`
  filter that scopes queries to YouTube search, but it doesn't publish a
  ready-made list of "today's top YouTube searches". `/top_youtube_trends`
  therefore approximates that list by asking Trends for related *rising*
  queries to a broad seed term (`"youtube"` by default) with the YouTube
  property filter applied. Change the `seed` option to pivot the list.
- **Relative, not absolute, numbers.** Trends values are on a 0–100 scale
  relative to the maximum search volume for the query set. They are not
  raw view/search counts and cannot be compared across separate requests.
- **Rate limiting.** Google aggressively throttles Trends. The bot catches
  `TooManyRequestsError` and returns a friendly embed; if you hit this often
  consider rotating `PYTRENDS_PROXY`.
- **Unofficial API.** `pytrends` scrapes the public Trends site. Google may
  change the HTML/JSON shape without notice, which occasionally causes
  transient failures until `pytrends` releases a patch.
- **Sparse long-tail data.** Niche keywords in small regions may simply have
  no data. The bot surfaces this clearly instead of pretending.
- **Chart honesty.** The `/compare_trends` chart shows *relative interest*
  over the selected timeframe — overlapping lines are fine, but the absolute
  ordering of spikes is what matters.

## Suggestions for future improvements

- **Cache results** (e.g. `cachetools.TTLCache`) keyed by `(keyword, geo, tf)`
  to cut rate-limit risk and speed up repeated requests.
- **Persistent storage** (SQLite/Postgres) for historical trend snapshots so
  users can diff week-over-week changes.
- **Background scheduler** (`discord.ext.tasks`) that posts daily top
  trends to a specific channel.
- **Per-guild defaults** (region, timeframe) saved to a config table.
- **Autocomplete for keywords** using Google Trends "suggestions" endpoint
  (`pytrends.suggestions`) to help users discover canonical topic IDs.
- **Interactive pagination** with Discord UI views so users can page through
  large result sets inline.
- **Healthcheck endpoint** (small `aiohttp` server) so uptime monitoring
  platforms can ping the bot.
- **Unit tests** for `validators.py` and `trends_service.py` using
  `responses`/`respx` to fake Google Trends responses.
- **Docker deployment** with a slim base image for reproducible hosting.
- **Alternative data sources** — combine `pytrends` with the YouTube Data
  API v3 (`search.list` + `videos.list`) for true YouTube video trends
  instead of search trends.

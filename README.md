# Shorts-AI — Discord YouTube-Trends Bot

A production-ready Discord bot that surfaces YouTube search-trend data from
Google Trends through slash commands — plus real YouTube video trends via the
official YouTube Data API when a key is configured.

## Features

| Command | What it does |
|---|---|
| `/youtube_trends` | Top + rising YouTube searches related to a keyword. Paginated. Keyword autocomplete via Google Trends suggestions. |
| `/top_youtube_trends` | Top YouTube trend queries for a region, seeded from a broad term. |
| `/compare_trends` | Compares 2-5 keywords and attaches a line chart of interest over time. |
| `/trends_diff` | Week-over-week diff of related queries for a keyword (uses the snapshot store). |
| `/youtube_videos` | Real YouTube videos ordered by view count — **YouTube Data API, not Google Trends**. |
| `/trends_settings show` / `set` / `disable_digest` | Per-guild defaults (region, timeframe, daily-digest channel). |

Operational niceties:

- In-memory TTL cache on every trend lookup (`cachetools`), separate cache for suggestions.
- Persistent SQLite store for historical snapshots and per-guild settings.
- Daily top-trends digest posted to guild-configured channels via `discord.ext.tasks`.
- Interactive paginator for large result sets (prev / next / close, timeout-safe).
- `aiohttp` healthcheck server at `/health`, `/ready`, `/metrics`.
- Unit tests (`pytest`) for validators, cache, and service error paths.
- Multi-stage `Dockerfile` based on `python:3.11-slim` with a non-root user.

## Project layout

```
Shorts-AI/
├── main.py                         # Entry point
├── requirements.txt                # Runtime deps
├── requirements-dev.txt            # Test deps
├── Dockerfile
├── .dockerignore
├── .env.example
├── pytest.ini
├── README.md
├── bot/
│   ├── __init__.py
│   ├── bot.py                      # commands.Bot subclass + lifecycle
│   ├── config.py                   # Settings from .env / environment
│   ├── logger.py                   # Root logging setup
│   ├── healthcheck.py              # aiohttp healthcheck server
│   ├── cogs/
│   │   ├── trends.py               # /youtube_trends, /top_*, /compare_*, /trends_diff
│   │   ├── videos.py               # /youtube_videos (YouTube Data API)
│   │   ├── settings.py             # /trends_settings group
│   │   └── scheduler.py            # Daily digest (discord.ext.tasks)
│   ├── services/
│   │   ├── cache.py                # TTLKeyCache (cachetools wrapper)
│   │   ├── trends_service.py       # Async pytrends wrapper + cache
│   │   └── youtube_data_service.py # YouTube Data API v3 client
│   ├── storage/
│   │   ├── database.py             # aiosqlite-backed Database abstraction
│   │   ├── guild_settings_repo.py  # Per-guild defaults
│   │   └── snapshot_repo.py        # Historical snapshots + diff helper
│   ├── ui/
│   │   └── pagination.py           # PaginatedEmbedView
│   └── utils/
│       ├── validators.py
│       ├── embeds.py
│       └── charts.py
└── tests/
    ├── test_validators.py
    ├── test_cache.py
    └── test_trends_service.py
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
# For tests:
pip install -r requirements-dev.txt
```

### 3. Create the Discord bot

Follow the Discord Developer Portal flow to make an application, add a bot,
copy the token, and invite the bot with scopes `bot` + `applications.commands`
and permissions `Send Messages` + `Embed Links`.

### 4. Configure environment

```bash
cp .env.example .env
# then edit .env and at minimum set DISCORD_BOT_TOKEN
```

All environment variables are documented in `.env.example`. The interesting
new ones introduced by this upgrade:

| Variable | Default | What it controls |
|---|---|---|
| `CACHE_TTL_SECONDS` | `600` | TTL for trend result cache. |
| `CACHE_MAX_SIZE` | `512` | Max entries in the result cache. |
| `SUGGESTION_CACHE_TTL_SECONDS` | `3600` | TTL for autocomplete suggestions. |
| `DATABASE_URL` | `sqlite:///data/shorts_ai.db` | Storage location. |
| `DAILY_DIGEST_ENABLED` | `false` | Master switch for the scheduler. |
| `DAILY_DIGEST_HOUR_UTC` / `_MINUTE_UTC` | `13` / `0` | When to post the digest. |
| `HEALTHCHECK_ENABLED` | `true` | Whether to start the aiohttp server. |
| `HEALTHCHECK_HOST` / `HEALTHCHECK_PORT` | `0.0.0.0` / `8080` | Where to bind. |
| `YOUTUBE_API_KEY` | *(empty)* | Enable `/youtube_videos`. Get one from Google Cloud. |

### 5. Run

```bash
python main.py
```

Expected log output:

```
SQLite database ready at data/shorts_ai.db
Database schema initialised
Synced N slash commands globally
Healthcheck listening on http://0.0.0.0:8080
Logged in as ShortsAI#1234 (id=...)
```

### Docker

```bash
# Build
docker build -t shorts-ai .

# Run
docker run -d --name shorts-ai \
  --env-file .env \
  -v shorts_ai_data:/app/data \
  -p 8080:8080 \
  shorts-ai
```

The SQLite database lives under `/app/data/shorts_ai.db` inside the container,
so mount a named volume (or a host path) to persist snapshots and guild
settings across container restarts.

Sanity check the healthcheck endpoint:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/ready
curl http://localhost:8080/metrics
```

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The test suite covers `validators`, the TTL cache, and the error-translation
paths in `trends_service`. Google Trends HTTP traffic is mocked via `responses`
or by patching the bound sync methods directly, so the tests never hit the
network.

## Data sources — what comes from where

Two fundamentally different data sources live side-by-side; the cog layer
picks the right one for each command.

- **Google Trends (via `pytrends`)** — powers `/youtube_trends`,
  `/top_youtube_trends`, `/compare_trends`, `/trends_diff`. Returns
  *relative search interest* on a 0-100 scale. Not raw counts, not
  cross-request comparable.
- **YouTube Data API v3 (optional)** — powers `/youtube_videos`. Returns
  *real videos* ranked by view count, with titles, channel names, view and
  like counts, and thumbnails. Requires `YOUTUBE_API_KEY`.

## Database schema

Two tables, created idempotently on startup (`Database.init_schema`):

- **`guild_settings`** — per-guild defaults.
  `guild_id` PK, `default_geo`, `default_timeframe`, `digest_channel_id`,
  `digest_seed`, `updated_at`.
- **`trend_snapshots`** — every `/youtube_trends`, `/top_youtube_trends` and
  `/compare_trends` invocation writes one row here. Columns: `captured_at`,
  `source` (`trends` / `youtube_api`), `kind` (`related` / `top` / `compare`),
  `keyword` (single keyword or comma-joined canonical list), `geo`,
  `timeframe`, `payload` (JSON). Two indexes cover the two lookup patterns:
  `(keyword, geo, timeframe, captured_at DESC)` for point lookups and
  `(kind, captured_at DESC)` for retention scans.

Because the schema is tiny and writes are low-volume, we use `CREATE TABLE IF
NOT EXISTS` as the migration mechanism. Introduce Alembic or numbered SQL
files when that stops being sufficient.

### Postgres later

`build_database()` dispatches on URL scheme. To add Postgres support, write a
`PostgresDatabase(Database)` class around `asyncpg`, have it return
`$1`-style parameters or translate internally, and register it in
`build_database()` — no repo changes needed.

## Limitations

- **No native "top YouTube trends" endpoint.** `/top_youtube_trends`
  approximates it with related rising queries for a broad seed keyword.
- **Google Trends numbers are relative.** 0-100 normalised against the peak
  in the query set; they are not raw view/search counts.
- **Rate limits.** Google throttles Trends per IP; the cache helps, but set
  `PYTRENDS_PROXY` if you hit this frequently.
- **Unofficial pytrends API.** Google can change the underlying HTML/JSON
  without notice.
- **YouTube Data API quota.** Default 10 000 units/day; `search.list` is
  100 units per call, `videos.list` is 1 unit.

## Design notes / tradeoffs

- **Cache per data class, not per service.** Two separate `TTLKeyCache`
  instances (results, suggestions) so a burst of trend queries can't evict
  suggestion data and vice versa.
- **Thin DB abstraction over aiosqlite.** Enough to keep the repos clean,
  not so much that we're reimplementing SQLAlchemy. Swapping to Postgres is
  a focused change inside `database.py`.
- **Services own caching, cogs own UX.** Pagination, embeds, and validation
  live in cogs/ui/utils. Cogs never touch `pytrends`/`aiohttp` directly.
- **Scheduler is a cog.** Using `discord.ext.tasks` means we ride along with
  the gateway reconnect logic, so we don't need a separate scheduler process.
- **Snapshot writes never break the user response.** Every `insert()` is
  wrapped in try/except/log at the cog layer; if SQLite hiccups, the user
  still gets their embed.
- **Healthcheck shares the event loop.** `aiohttp` runs in the same loop as
  discord.py — no threads, no cross-loop concerns.
- **YouTube Data API is optional, not mandatory.** If the key is missing,
  only `/youtube_videos` refuses; everything else keeps working.

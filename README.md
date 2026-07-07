# Hackathon Scout

A tiny agent that watches the web for **healthcare / biotech / pharma hackathons** and pings you the moment something new appears. Chat messages carry name + link only — all metadata lands in a Notion table.

## How it works

```
Devpost API ──────────┐
MLH calendars ────────┤   dedupe → keyword filter → parse dates/
grand-challenge API ──┤   deadline/country → catalog + Notion     ┌─ Telegram (all runs)
Kaggle API (opt.) ────┼→  sync → drop already-seen →         ────►┤
Aggregator pages ─────┤   Claude relevance filter → notify        └─ Discord (daily 12:00
DuckDuckGo sweeps ────┘              ↓                                Munich digest only)
                        seen.json  ·  events.json ──────────────────► Notion database
                        (notified)    (full catalog, powers /all)
```

## Project structure

```
agent.py                        the scanner: sources → filter → enrich → notify
parser.py                       date-range / deadline / country extraction (EN+DE)
notion_sync.py                  upserts the catalog into a Notion database
poller.py                       Telegram command handler (/search, /all)
config.yaml                     keywords, sources, sweep queries — edit freely, no code
requirements.txt                requests, beautifulsoup4, pyyaml, anthropic
seen.json                       ids already announced (reset to re-announce)
events.json                     full event catalog, committed by the scan run
latest_digest.md                snapshot of the most recent digest
.env.example                    template for local runs (secrets live in Actions)
.github/workflows/
  scan.yml                      daily scan at 12:00 GMT+2 (DST-proof dual cron)
  command-poller.yml            polls Telegram every 5 min for commands
```

## Commands & channels

Telegram: `/search` runs a scan whose new finds go to Telegram only; `/all` lists every known upcoming event. Discord has no commands — it just receives the daily digest.

## Setup

Add repository secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`, `ANTHROPIC_API_KEY` (relevance filter), optionally `KAGGLE_USERNAME` + `KAGGLE_KEY`. Then trigger *Hackathon scan* manually once to seed `events.json`. The workflow's **reset_seen** input forgets announced events so everything is re-sent on that run. Local test: `SCOUT_DRY=1 python agent.py`.
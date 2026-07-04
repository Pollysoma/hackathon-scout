# 🩺 Hackathon Scout

A tiny agent that watches the web for **healthcare / biotech / pharma hackathons** and pings you the moment something new appears — so you never again find out about a hackathon four days before it starts, after registration closed.

It runs for **free on GitHub Actions** (no server needed), remembers what it already told you, and sends new finds to **Telegram** or **e-mail**.

## How it works
1
11

```
Devpost API ─┐
MLH calendar ─┤                                       ┌─ Telegram
Aggregators  ─┼→ dedupe → keyword filter → new only? ─┼─ E-mail digest
Web sweep    ─┘         (optional Claude re-check)    └─ Action logs
                              ↓
                        seen.json (memory)
```

Sources out of the box:

1. **Devpost** — JSON endpoint queried with health/bio/pharma terms
2. **MLH** — current + next season calendars (student hackathons like MedHacks)
3. **Listing pages** — generic scraper for aggregators, configured in `config.yaml`:
   `allhackathons.com` (health theme) and `digital-health-events.de` (Germany) are pre-wired
4. **Web sweep** — DuckDuckGo queries like "pharma hackathon 2026" to catch one-off events with their own websites

Every source is best-effort: if a site changes its markup or blocks a request, the run logs it and continues with the rest.

## Quick start (local test, ~2 minutes)

```bash
pip install -r requirements.txt
SCOUT_DRY=1 python agent.py      # dry run: prints the digest, saves nothing
python agent.py                  # real run: prints digest (no notifier configured yet), saves seen.json
python agent.py                  # → "Nothing new today. ✅"
```

## Deploy free on GitHub Actions (~10 minutes)

1. Create a new GitHub repo (private is fine) and push this folder to it.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add your notification secrets (see below).
3. That's it. The workflow in `.github/workflows/scan.yml` runs **daily at 07:00 Munich time** and can also be triggered manually via the **Actions** tab → *Hackathon scan* → *Run workflow*. Do a manual run first — the initial run reports everything currently out there, and subsequent runs only report what's new.

The workflow commits `seen.json` back to the repo after each run — that's the agent's memory, and the daily commit also keeps GitHub from pausing the schedule for inactivity (it disables cron on repos with no activity for ~60 days).

### Option A: Telegram (recommended — easiest)

1. Message **@BotFather** on Telegram → `/newbot` → follow prompts → copy the **token**.
2. Send your new bot any message (e.g. "hi"), then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the `"chat":{"id": ...}` number.
3. Add secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

### Option B: E-mail

Add secrets: `SMTP_HOST`, `SMTP_PORT` (usually 587), `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.
For Gmail, use an [app password](https://support.google.com/accounts/answer/185833), not your normal password.

### Option C: Neither

No secrets → the digest is printed into the Action log and saved as `latest_digest.md` in the repo, which you can skim any time.

### Optional: Claude as a relevance filter

Keyword matching occasionally lets an off-topic event through (a climate hackathon tagged "health", a listicle from the web sweep). If you add an `ANTHROPIC_API_KEY` secret, each batch of *new* candidates gets one cheap classification call (`claude-haiku-4-5`) before you're notified — typically well under €1/month at daily runs. Get a key at <https://console.anthropic.com>; API docs: <https://docs.claude.com/en/api/overview>. Turn off via `llm_filter: false` in `config.yaml`.

## Tuning

Everything lives in `config.yaml`:

- **`keywords`** — substring matches (EN + DE included). Add `"rare disease"`, `wearable`, `mikrobiom`... whatever you care about.
- **`exclude_keywords`** — hard blocklist.
- **`devpost_search_terms`** / **`web_queries`** — what gets searched.
- **`listing_pages`** — add any page that lists events. You just need the URL and a substring that its event links contain (open the page, right-click an event link, *Copy link address*, find the common pattern). Set `trusted: true` if the page is already topic-filtered.

## Ideas for v2

- **More sources:** `hackathon-base.org`, `grand-challenge.org` (medical-imaging challenges), Kaggle health competitions, EIT Health & MIT Hacking Medicine event pages, Eventbrite/lu.ma keyword pages, the events pages of Bayer G4A / Roche / Novartis BIOME.
- **Smarter dates:** parse start dates and re-ping you 1 week before registration deadlines.
- **Distance-aware:** geocode locations and flag events within X hours of Munich.
- **Weekly summary mode:** batch into a Sunday digest instead of daily pings.

## Caveats (honest ones)

- Scrapers rot. Devpost's endpoint is unofficial; MLH/aggregator markup can change. The generic listing-page scraper is deliberately loose (it harvests links by pattern) so it survives redesigns better, but expect to touch this once or twice a year.
- Some sites rate-limit or block datacenter IPs (GitHub runners). Failures are logged, never fatal.
- This is polite, low-frequency personal use (1 request/day/site) — keep it that way.

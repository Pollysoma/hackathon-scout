#!/usr/bin/env python3
"""
Hackathon Scout — finds new healthcare / biotech / pharma hackathons and pings you.

Sources (all best-effort; one failing never kills the run):
  1. Devpost's public JSON endpoint (unofficial, but stable for years)
  2. MLH season calendars
  3. grand-challenge.org public REST API (biomedical challenges)
  4. Kaggle competitions API (optional — needs KAGGLE_USERNAME + KAGGLE_KEY)
  5. Configurable "listing pages" (aggregator sites, harvested by link pattern)
  6. Keyword web sweeps via DuckDuckGo, incl. site:-restricted sweeps for
     JS-rendered / API-less pages (Eventbrite, lu.ma, hackathon-base.org,
     MIT Hacking Medicine, EIT Health, Bayer G4A, Roche, Novartis BIOME)

Pipeline:
  scrape -> normalize/dedupe -> keyword filter -> parse dates/deadline/country
  -> update events.json catalog -> Notion sync -> drop already-seen ->
  optional Claude relevance filter -> notify

Chat messages carry name + link ONLY; all metadata lives in Notion / events.json.

Channel routing (CHANNELS env, set by the workflow):
  all       -> Telegram + Discord   (scheduled daily digest)
  telegram  -> Telegram only        (/search-triggered runs)

State:
  seen.json    — ids already notified (delete / reset to re-announce everything)
  events.json  — the catalog that powers /all and the Notion sync

Set SCOUT_DRY=1 for a test run that neither notifies nor writes state.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

import notion_sync
from parser import (chunks, country_from_location, dates_from_text,
                    find_reg_deadline, parse_date_range)

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "seen.json"
CATALOG_FILE = ROOT / "events.json"
DIGEST_FILE = ROOT / "latest_digest.md"
HEADERS = {  # browser-like — several sources 403 plain bot user-agents
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}
TIMEOUT = 30
DRY = os.environ.get("SCOUT_DRY") == "1"
CHANNELS = os.environ.get("CHANNELS", "all").strip().lower()  # all | telegram

CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
KEYWORDS = [k.lower() for k in CONFIG.get("keywords", [])]
EXCLUDE = [k.lower() for k in CONFIG.get("exclude_keywords", []) or []]
EVENT_WORDS = [w.lower() for w in CONFIG.get(
    "event_words", ["hackathon", "hackaton", "datathon", "makeathon", "hacks", "hacking", "hack:"]
)]


def log(msg: str) -> None:
    """stderr, so the digest on stdout stays clean."""
    print(f"[scout] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|source$|igshid)")


def norm_url(url: str) -> str:
    """Canonicalize a URL so the same event isn't reported twice.

    Tracking params are dropped, but meaningful query params are KEPT —
    some platforms address distinct events only via the query string.
    """
    try:
        p = urlparse(url.strip())
        netloc = p.netloc.lower().removeprefix("www.")
        path = p.path.rstrip("/")
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not _TRACKING_PARAMS.match(k.lower())]
        query = urlencode(sorted(q))
        return urlunparse(("https", netloc, path, "", query, ""))
    except Exception:
        return url.strip().rstrip("/")


def event_id(ev: dict) -> str:
    return hashlib.sha1(norm_url(ev["url"]).encode()).hexdigest()[:16]


def is_bare_root(url: str) -> bool:
    """True if the URL is just a domain root with no meaningful path or query —
    almost always an organizer/aggregator homepage (biohackathon.org/,
    eventbrite.com/), not one specific event. Such hits carry no date and
    pollute the feed, so they're dropped before dedup."""
    try:
        p = urlparse(url)
        return p.path.strip("/") == "" and not p.query
    except Exception:
        return False


def hit(text: str, needles: list[str]) -> bool:
    t = text.lower()
    return any(n in t for n in needles)


def is_relevant(ev: dict) -> bool:
    haystack = " ".join(str(ev.get(k, "")) for k in ("title", "themes", "context", "location"))
    if EXCLUDE and hit(haystack, EXCLUDE):
        return False
    if ev.get("trusted"):  # came from an already topic-filtered source
        return True
    return hit(haystack, KEYWORDS)


# --------------------------------------------------------------------------- #
#  Sources — each returns a list of dicts:
#  {title, url, dates, location, themes, context?, trusted?, source,
#   start?, end?, reg_deadline?}   (dates may be pre-parsed by API sources)
# --------------------------------------------------------------------------- #

def source_devpost() -> list[dict]:
    """Devpost's JSON endpoint that powers devpost.com/hackathons (unofficial)."""
    out: list[dict] = []
    for term in CONFIG.get("devpost_search_terms", []):
        for page in (1, 2):
            try:
                r = requests.get(
                    "https://devpost.com/api/hackathons",
                    params={"search": term, "page": page, "status[]": ["upcoming", "open"]},
                    headers=HEADERS, timeout=TIMEOUT,
                )
                r.raise_for_status()
                hacks = r.json().get("hackathons", [])
                if not hacks:
                    break
                for h in hacks:
                    out.append({
                        "title": (h.get("title") or "").strip(),
                        "url": h.get("url") or "",
                        "dates": h.get("submission_period_dates") or "",
                        "location": (h.get("displayed_location") or {}).get("location", ""),
                        "themes": ", ".join(t.get("name", "") for t in (h.get("themes") or [])),
                        "source": "Devpost",
                    })
            except Exception as e:
                log(f"devpost[{term} p{page}]: {e}")
                break
    return out


def source_mlh() -> list[dict]:
    """Major League Hacking season calendars (mostly student hackathons)."""
    out: list[dict] = []
    year = dt.date.today().year
    for season in (year, year + 1):
        url = f"https://mlh.io/seasons/{season}/events"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select("div.event"):
                name = card.select_one(".event-name")
                link = card.select_one("a.event-link") or card.select_one("a[href]")
                if not (name and link):
                    continue
                date = card.select_one(".event-date")
                loc = card.select_one(".event-location")
                out.append({
                    "title": name.get_text(strip=True),
                    "url": link.get("href", ""),
                    "dates": date.get_text(strip=True) if date else "",
                    "location": loc.get_text(" ", strip=True) if loc else "",
                    "themes": "",
                    "source": f"MLH {season} season",
                })
        except Exception as e:
            log(f"mlh[{season}]: {e}")
    return out


def source_grand_challenge() -> list[dict]:
    """grand-challenge.org public REST API — biomedical imaging challenges.

    Endpoint verified against github.com/comic/grand-challenge.org:
    GET /api/v1/challenges/ (DRF, limit/offset pagination, anonymous read)
    returns {title, url, slug, status, start_date, end_date, incentives, ...}.
    Everything on the platform is biomedical -> trusted (skip keyword filter);
    we only keep challenges that haven't ended yet.
    """
    if not CONFIG.get("grand_challenge", True):
        return []
    out: list[dict] = []
    today = dt.date.today().isoformat()
    url = "https://grand-challenge.org/api/v1/challenges/?limit=100"
    hdrs = {**HEADERS, "Accept": "application/json"}
    try:
        for _ in range(5):  # follow pagination, politely capped
            r = requests.get(url, headers=hdrs, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            for c in data.get("results", []):
                start = (c.get("start_date") or "")[:10]
                end = (c.get("end_date") or "")[:10]
                status = str(c.get("status") or "").lower()
                # Deliberate source-level filter, independent of the
                # drop_past_events config knob (which governs notifications):
                # the platform hosts ~15 years of completed challenges, and
                # importing those would flood the catalog and Notion.
                if end and end < today:
                    continue
                if not end and status and ("complet" in status or "closed" in status):
                    continue
                link = c.get("url") or (
                    f"https://{c['slug']}.grand-challenge.org/" if c.get("slug") else "")
                if not link:
                    continue
                out.append({
                    "title": (c.get("title") or c.get("slug") or "").strip(),
                    "url": link,
                    "dates": " - ".join(v for v in (start, end) if v),
                    "start": start or None,
                    "end": end or None,
                    "location": "Online",
                    "themes": ", ".join(map(str, c.get("incentives") or [])),
                    "context": (c.get("description") or "")[:200],
                    "trusted": True,
                    "source": "grand-challenge.org",
                })
            url = data.get("next")
            if not url:
                break
            time.sleep(0.5)
    except Exception as e:
        log(f"grand-challenge: {e}")
    return out


def source_kaggle() -> list[dict]:
    """Kaggle competitions API (same endpoint the official CLI uses).

    Optional: silently skipped unless KAGGLE_USERNAME and KAGGLE_KEY are set
    (create a token at kaggle.com -> Settings -> API -> Create New Token).
    """
    user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    if not (user and key):
        return []
    out: list[dict] = []
    for term in CONFIG.get("kaggle_search_terms", []) or []:
        try:
            r = requests.get(
                "https://www.kaggle.com/api/v1/competitions/list",
                params={"search": term, "page": 1},
                auth=(user, key), headers=HEADERS, timeout=TIMEOUT,
            )
            r.raise_for_status()
            for c in r.json() or []:
                ref = str(c.get("ref") or "")
                if not ref:
                    continue
                link = ref if ref.startswith("http") else f"https://www.kaggle.com/competitions/{ref}"
                deadline = str(c.get("deadline") or "")[:10]
                out.append({
                    "title": (c.get("title") or ref).strip(),
                    "url": link,
                    "dates": f"until {deadline}" if deadline else "",
                    "end": deadline or None,
                    "reg_deadline": deadline or None,  # Kaggle: entry ~ submission deadline
                    "location": "Online",
                    "themes": str(c.get("category") or ""),
                    "context": str(c.get("description") or "")[:200],
                    # Already scoped by the health/bio search terms above, and
                    # Kaggle titles rarely contain our keywords (they're just
                    # sponsor + task, e.g. "RSNA Breast Cancer Detection"), so
                    # skip the keyword gate like grand-challenge; the LLM filter
                    # prunes any tangential search hits.
                    "trusted": True,
                    "source": "Kaggle",
                })
        except Exception as e:
            log(f"kaggle[{term}]: {e}")
    return out


def source_listing_pages() -> list[dict]:
    """Generic scraper for aggregator pages, driven entirely by config.yaml.

    For each page: harvest every <a> whose href contains `link_contains`,
    optionally require the anchor text to contain one of `must_contain`,
    and keep a bit of surrounding text as context for the relevance filter.
    """
    out: list[dict] = []
    for page in CONFIG.get("listing_pages", []) or []:
        name, url = page.get("name", "listing"), page["url"]
        link_contains = page.get("link_contains", "")
        must = [w.lower() for w in page.get("must_contain", []) or []]
        trusted = bool(page.get("trusted", False))
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            seen_here: set[str] = set()
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if link_contains and link_contains not in href:
                    continue
                text = a.get_text(" ", strip=True)
                if len(text) < 6:
                    continue
                if must and not hit(text, must):
                    continue
                if href.startswith("/"):  # make relative links absolute
                    p = urlparse(url)
                    href = f"{p.scheme}://{p.netloc}{href}"
                key = norm_url(href)
                if not href.startswith("http") or key in seen_here:
                    continue
                seen_here.add(key)
                # a little surrounding text (dates/location/tags) — climb the DOM
                # only as far as needed, and never inherit huge page-level text
                ctx, node = "", a.parent
                for _ in range(2):
                    if node is None or node.name in ("body", "html"):
                        break
                    full = node.get_text(" ", strip=True)
                    if len(full) > len(text) + 15:  # found extra info...
                        if len(full) <= max(3 * len(text), 400):  # ...still card-sized
                            ctx = full[:200]
                        break
                    node = node.parent
                out.append({
                    "title": text[:130],
                    "url": href,
                    "dates": "",
                    "location": "",
                    "themes": "",
                    "context": ctx,
                    "trusted": trusted,
                    "source": name,
                })
        except Exception as e:
            log(f"{name}: {e}")
    return out


def source_web_sweep() -> list[dict]:
    """Keyword sweeps via DuckDuckGo's HTML endpoint (no API key; best-effort).

    config web_queries entries are either a plain string or a mapping:
      - "biotech hackathon {year}"                       # normal sweep
      - {q: "site:g4a.health {year}", loose: true,       # loose: don't require
         trusted: true}                                  #   an event word in the
                                                         #   title; trusted: skip
                                                         #   the keyword filter
    site:-restricted sweeps are how we cover JS-rendered / API-less pages
    (Eventbrite, lu.ma, hackathon-base.org, MIT Hacking Medicine, EIT Health)
    and corporate programs that avoid the word "hackathon" (G4A, BIOME, Roche).
    The Claude relevance filter prunes whatever noise slips through.
    """
    out: list[dict] = []
    year = dt.date.today().year
    for entry in CONFIG.get("web_queries", []) or []:
        if isinstance(entry, str):
            q, loose, trusted = entry, False, False
        else:
            q = entry.get("q", "")
            loose = bool(entry.get("loose", False))
            trusted = bool(entry.get("trusted", False))
        if not q:
            continue
        q = q.format(year=year, next_year=year + 1)
        try:
            r = requests.post("https://html.duckduckgo.com/html/",
                              data={"q": q}, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a.result__a")[:8]:
                href = a.get("href", "")
                if "duckduckgo.com/l/" in href:  # decode DDG redirect links
                    qs = parse_qs(urlparse(href).query)
                    href = (qs.get("uddg") or [""])[0]
                if not href.startswith("http"):
                    continue
                title = a.get_text(" ", strip=True)
                if not loose and not hit(title, EVENT_WORDS):
                    continue
                out.append({
                    "title": title[:130], "url": href, "dates": "",
                    "location": "", "themes": "", "trusted": trusted,
                    "source": f"Web sweep: {q}",
                })
        except Exception as e:
            log(f"web[{q}]: {e}")
        time.sleep(1.2)  # be gentle — DDG throttles rapid-fire posts
    return out


SOURCES = [source_devpost, source_mlh, source_grand_challenge, source_kaggle,
           source_listing_pages, source_web_sweep]


# --------------------------------------------------------------------------- #
#  Enrichment — parse dates / deadlines / country (all best-effort)
# --------------------------------------------------------------------------- #

def _iso(d) -> str | None:
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else (d or None)


def drop_past(events: list[dict]) -> list[dict]:
    """Remove events that have clearly already happened.

    An event is dropped when its end date is before today, or — when it has no
    end — when its start is more than `past_grace_days` in the past (covers
    single-day events and organizer pages listing only old editions, like the
    2020-2025 entries on biohackathon.org). Events with NO parseable date pass
    through: we can't tell they're over and don't want to lose real upcoming
    ones we simply failed to date."""
    if not CONFIG.get("drop_past_events", True):
        return events
    today = dt.date.today().isoformat()
    grace = (dt.date.today() - dt.timedelta(days=int(CONFIG.get("past_grace_days", 1)))).isoformat()
    kept, dropped = [], 0
    for e in events:
        end, start = e.get("end"), e.get("start")
        if end and end < today:
            dropped += 1
            continue
        if not end and start and start < grace:
            dropped += 1
            continue
        kept.append(e)
    if dropped:
        log(f"drop_past: removed {dropped} already-finished event(s)")
    return kept


def enrich(ev: dict) -> dict:
    year = dt.date.today().year
    if not (ev.get("start") or ev.get("end")):
        # structured `dates` field first (range-aware), prose only as fallback
        # (context often contains a registration deadline that would pollute it)
        start, end = parse_date_range(str(ev.get("dates") or ""), default_year=year)
        if not (start or end):
            start, end = dates_from_text(
                f"{ev.get('context') or ''} {ev.get('title') or ''}", default_year=year)
        ev["start"], ev["end"] = _iso(start), _iso(end)
    if not ev.get("reg_deadline"):
        blob = " ".join(str(ev.get(k) or "") for k in ("context", "dates"))
        ev["reg_deadline"] = _iso(find_reg_deadline(blob, default_year=year))
    if not ev.get("country"):
        ev["country"] = country_from_location(ev.get("location") or "")
    return ev


# --------------------------------------------------------------------------- #
#  Catalog (events.json) — powers /all and the Notion sync
# --------------------------------------------------------------------------- #

_CATALOG_FIELDS = ("title", "url", "dates", "location", "themes", "source",
                   "start", "end", "reg_deadline", "country")


def load_catalog() -> dict[str, dict]:
    if not CATALOG_FILE.exists():
        return {}
    try:
        return json.loads(CATALOG_FILE.read_text(encoding="utf-8")).get("events", {})
    except Exception as e:
        log(f"events.json unreadable, starting fresh: {e}")
        return {}


def save_catalog(events: dict[str, dict]) -> None:
    CATALOG_FILE.write_text(
        json.dumps({"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "events": events}, indent=1, ensure_ascii=False, sort_keys=True),
        encoding="utf-8")


def update_catalog(catalog: dict[str, dict], events: list[dict]) -> None:
    today = dt.date.today().isoformat()
    for ev in events:
        eid = event_id(ev)
        rec = catalog.setdefault(eid, {"id": eid, "first_seen": today})
        for f in _CATALOG_FIELDS:
            if ev.get(f):  # never blank out known info with an empty re-scrape
                rec[f] = ev[f]
        rec["last_seen"] = today


def prune_catalog(catalog: dict[str, dict]) -> None:
    cutoff = (dt.date.today() - dt.timedelta(days=int(CONFIG.get("prune_days", 120)))).isoformat()
    for eid in [k for k, r in catalog.items()
                if (r.get("end") or r.get("start") or r.get("last_seen") or "9999") < cutoff]:
        del catalog[eid]


# --------------------------------------------------------------------------- #
#  Optional LLM relevance filter (cuts false positives; needs ANTHROPIC_API_KEY)
#  API docs: https://docs.claude.com/en/api/overview
# --------------------------------------------------------------------------- #

def llm_filter(events: list[dict]) -> list[dict]:
    if not events or not CONFIG.get("llm_filter", True):
        return events
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return events
    try:
        import anthropic

        listing = "\n".join(
            f"{i}: {e['title']} | {e.get('themes', '')} {e.get('context', '')} | {e['url']}"
            for i, e in enumerate(events)
        )
        prompt = (
            "You screen event listings for someone who only cares about hackathons, "
            "datathons, challenges and innovation programs in HEALTHCARE, BIOLOGY/BIOTECH, "
            "PHARMA, MEDTECH or LIFE SCIENCES (any country; English or German is fine). "
            "Corporate accelerator/challenge programs (e.g. Bayer G4A, Novartis BIOME, "
            "Roche) count even without the word 'hackathon'. "
            "Given the numbered list below, reply with ONLY a JSON array of the index "
            "numbers to KEEP. Drop anything off-topic and pages that clearly aren't a "
            "specific event or program (e.g. generic blog posts, news articles about "
            "past events, or listicles).\n\n" + listing
        )
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=CONFIG.get("llm_model", "claude-haiku-4-5"),
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        # Take the LAST bracketed list in the reply (the answer follows any
        # reasoning). Distinguish three cases:
        #   • a real index list "[0, 2]"  -> keep those
        #   • a deliberate empty list "[]" -> keep nothing (the model's verdict)
        #   • no list at all               -> parse failure, keep everything
        #     (an extra off-topic Notion row beats dropping the whole batch)
        arrays = re.findall(r"\[\s*(?:\d[\d,\s]*)?\]", text)  # digits OR empty
        if not arrays:
            log("llm_filter: no parseable array in reply — keeping all (fail-open)")
            return events
        keep = set(json.loads(arrays[-1]))
        kept = [e for i, e in enumerate(events) if i in keep]
        if keep and not kept:  # indices given but none valid -> treat as parse fail
            log(f"llm_filter: indices {sorted(keep)} out of range — keeping all")
            return events
        log(f"llm_filter: {len(events)} -> {len(kept)}")
        return kept
    except Exception as e:
        log(f"llm_filter skipped: {e}")
        return events


# --------------------------------------------------------------------------- #
#  Digest + notifications — chat gets NAME + LINK only, the rest is in Notion
# --------------------------------------------------------------------------- #

def build_lines(events: list[dict]) -> list[str]:
    lines: list[str] = []
    for e in events:
        lines.append(f"• {e['title']}")
        lines.append(f"  {e['url']}")
        lines.append("")
    return lines


def build_digest(events: list[dict]) -> str:
    today = dt.date.today().isoformat()
    return "\n".join([f"# 🩺 Hackathon Scout — {today}", "",
                      f"**{len(events)} new event(s) found.**", ""] + build_lines(events))


def notify_telegram(events: list[dict]) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    text = "\n".join([f"🩺 {len(events)} new health hackathon(s):", ""] + build_lines(events))
    ok = True
    for chunk in chunks(text, 3500):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
                timeout=TIMEOUT,
            )
            ok = ok and r.ok
        except Exception as e:
            log(f"telegram send failed: {e}")
            ok = False
    return ok


def notify_discord(events: list[dict]) -> bool:
    token, channel = os.environ.get("DISCORD_BOT_TOKEN"), os.environ.get("DISCORD_CHANNEL_ID")
    if not (token and channel):
        return False
    text = "\n".join([f"🩺 {len(events)} new health hackathon(s):", ""] + build_lines(events))
    ok = True
    for chunk in chunks(text, 1900):  # Discord caps at 2000
        try:
            r = requests.post(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                headers={"Authorization": f"Bot {token}"},
                json={"content": chunk, "flags": 4},  # 4 = SUPPRESS_EMBEDS: no
                timeout=TIMEOUT,                      # preview card per bare URL
            )
            ok = ok and r.ok
        except Exception as e:
            log(f"discord send failed: {e}")
            ok = False
    return ok


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def munich_gate() -> bool:
    """Scheduled runs fire at 10:00 AND 11:00 UTC; only the slot that equals
    12:00 Europe/Berlin (10 UTC during CEST, 11 UTC during CET) may proceed.
    Decided from the *scheduled* cron slot (SCHEDULE_CRON, passed by scan.yml),
    not the runner's wall clock, so a queued/late runner start can't make both
    slots skip. Manual / /search-dispatched runs always pass."""
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    berlin = dt.datetime.now(ZoneInfo("Europe/Berlin"))
    cest = berlin.utcoffset() == dt.timedelta(hours=2)
    cron = (os.environ.get("SCHEDULE_CRON") or "").strip()
    if cron.startswith(("0 10", "0 11")):
        ok = cron.startswith("0 10") if cest else cron.startswith("0 11")
    else:  # cron not passed through — fall back to the wall clock
        ok = berlin.hour == 12
    if not ok:
        log(f"schedule gate: slot '{cron or f'{berlin.hour}:xx'}' isn't 12:00 Munich — skipping")
    return ok


def main() -> None:
    if not munich_gate():
        print("Skipped (wrong cron slot for 12:00 Munich).")
        return

    seen: set[str] = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    catalog = load_catalog()

    raw: list[dict] = []
    for src in SOURCES:
        found = src()
        log(f"{src.__name__}: {len(found)} hits")
        raw.extend(found)

    uniq: dict[str, dict] = {}
    for e in raw:
        if not (e.get("url") and e.get("title")):
            continue
        key = norm_url(e["url"])
        if is_bare_root(key):  # domain root (after stripping tracking params) —
            continue           # an organizer/aggregator homepage, not an event
        e["url"] = key  # store canonical form -> stable Notion upsert-by-Link
        uniq.setdefault(key, e)

    enriched = [enrich(e) for e in uniq.values() if is_relevant(e)]
    relevant = drop_past(enriched)  # for notifications: don't announce finished events
    fresh = [e for e in relevant if event_id(e) not in seen]
    log(f"{len(raw)} scraped -> {len(uniq)} unique -> "
        f"{len(enriched)} relevant -> {len(relevant)} current -> {len(fresh)} new")

    shown = llm_filter(fresh)

    if DRY:
        print(build_digest(shown) if shown else "Nothing new today.")
        log("dry run: no notifications sent, no state written")
        return

    # Catalog + Notion first — even if chat delivery fails, /all and the Notion
    # table stay complete. Update from `enriched` (pre-drop_past): a newly-parsed
    # past end date must reach an existing record so its Notion status flips to
    # Past instead of lingering "Upcoming" until prune. (Brand-new already-over
    # events aren't in `fresh`, so they're still never announced.)
    update_catalog(catalog, enriched)
    prune_catalog(catalog)
    try:
        notion_sync.sync(list(catalog.values()))  # upserts; notion_hash skips no-ops
    except Exception as e:
        log(f"notion sync failed: {e}")
    save_catalog(catalog)  # after sync, so notion_hash values persist

    delivered = True
    if shown:
        digest = build_digest(shown)
        DIGEST_FILE.write_text(digest, encoding="utf-8")
        print(digest)  # always in the Actions log
        sent_tg = notify_telegram(shown)
        sent_dc = notify_discord(shown) if CHANNELS == "all" else False
        configured = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) or (
            CHANNELS == "all" and bool(os.environ.get("DISCORD_BOT_TOKEN")))
        if not configured:
            log(f"WARNING: no chat channel configured for CHANNELS={CHANNELS} — "
                "new events go only to this log (check TELEGRAM_*/DISCORD_* secrets)")
        # In CI, no configured channel means a broken deployment: leave events
        # UNSEEN so they resend once secrets are fixed. Locally (seeding a fresh
        # events.json) there's nothing to deliver to, so mark them seen.
        in_ci = bool(os.environ.get("GITHUB_ACTIONS"))
        delivered = (sent_tg or sent_dc) if configured else (not in_ci)
    else:
        if fresh:  # the LLM filter vetoed every new find — say so, don't
            # pretend the scan was empty (they're marked seen; if the verdict
            # looks wrong in the log, re-run with reset_seen)
            log(f"llm_filter dropped all {len(fresh)} new event(s) as off-topic")
            print(f"{len(fresh)} new find(s), all judged off-topic by the LLM filter.")
        else:
            print("Nothing new today.")

    # Mark as seen only what was actually delivered (or needed no delivery):
    # if every chat channel failed, the "shown" events stay unseen and are
    # re-announced on the next run instead of being lost silently. Exclusion is
    # by id, not object identity, so it survives llm_filter returning new dicts.
    shown_ids = {event_id(e) for e in shown}
    to_mark = fresh if delivered else [e for e in fresh if event_id(e) not in shown_ids]
    if to_mark:
        seen.update(event_id(e) for e in to_mark)
        STATE_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")
    if not delivered:
        log("all chat channels failed — new events NOT marked seen; will retry")


if __name__ == "__main__":
    main()
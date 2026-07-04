#!/usr/bin/env python3
"""
Hackathon Scout — finds new healthcare / biotech / pharma hackathons and pings you.

Sources (all best-effort; one failing never kills the run):
  1. Devpost's public JSON endpoint (unofficial, but stable for years)
  2. MLH season calendars
  3. Configurable "listing pages" (aggregator sites, harvested by link pattern)
  4. A keyword web sweep via DuckDuckGo's HTML endpoint (no API key needed)

Pipeline:
  scrape -> normalize/dedupe -> keyword filter -> drop already-seen ->
  optional Claude relevance filter -> notify (Telegram / e-mail / stdout)

State lives in seen.json next to this file, so re-runs only report NEW events.
Set SCOUT_DRY=1 to do a test run that neither notifies nor updates state.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "seen.json"
DIGEST_FILE = ROOT / "latest_digest.md"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) HackathonScout/1.0 (personal event alerts)"
}
TIMEOUT = 30
DRY = os.environ.get("SCOUT_DRY") == "1"

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

def norm_url(url: str) -> str:
    """Canonicalize a URL so the same event isn't reported twice."""
    try:
        p = urlparse(url.strip())
        netloc = p.netloc.lower().removeprefix("www.")
        path = p.path.rstrip("/")
        return urlunparse(("https", netloc, path, "", "", ""))  # scheme-insensitive
    except Exception:
        return url.strip().rstrip("/")


def event_id(ev: dict) -> str:
    return hashlib.sha1(norm_url(ev["url"]).encode()).hexdigest()[:16]


def hit(text: str, needles: list[str]) -> bool:
    t = text.lower()
    return any(n in t for n in needles)


def is_relevant(ev: dict) -> bool:
    haystack = " ".join(str(ev.get(k, "")) for k in ("title", "themes", "context", "location"))
    if EXCLUDE and hit(haystack, EXCLUDE):
        return False
    if ev.get("trusted"):  # came from a page that is already topic-filtered
        return True
    return hit(haystack, KEYWORDS)


def chunks(text: str, n: int):
    """Split text into <=n char pieces on line boundaries (Telegram caps at 4096)."""
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) > n and buf:
            yield "\n".join(buf)
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        yield "\n".join(buf)


# --------------------------------------------------------------------------- #
#  Sources — each returns a list of dicts:
#  {title, url, dates, location, themes, context?, trusted?, source}
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
                ctx = ""
                node = a.parent
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
    """Keyword sweep via DuckDuckGo's HTML endpoint (no API key; best-effort)."""
    out: list[dict] = []
    year = dt.date.today().year
    for q in CONFIG.get("web_queries", []) or []:
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
                if not hit(title, EVENT_WORDS):
                    continue
                out.append({
                    "title": title[:130], "url": href, "dates": "",
                    "location": "", "themes": "", "source": f"Web sweep: {q}",
                })
        except Exception as e:
            log(f"web[{q}]: {e}")
    return out


SOURCES = [source_devpost, source_mlh, source_listing_pages, source_web_sweep]


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
            "datathons and innovation challenges in HEALTHCARE, BIOLOGY/BIOTECH, PHARMA, "
            "MEDTECH or LIFE SCIENCES (any country; English or German is fine). "
            "Given the numbered list below, reply with ONLY a JSON array of the index "
            "numbers to KEEP. Drop anything off-topic and pages that clearly aren't a "
            "specific event (e.g. generic blog posts or listicles).\n\n" + listing
        )
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=CONFIG.get("llm_model", "claude-haiku-4-5"),
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\[[\d,\s]*\]", text)
        if not m:
            return events
        keep = set(json.loads(m.group(0)))
        kept = [e for i, e in enumerate(events) if i in keep]
        log(f"llm_filter: {len(events)} -> {len(kept)}")
        return kept
    except Exception as e:
        log(f"llm_filter skipped: {e}")
        return events


# --------------------------------------------------------------------------- #
#  Digest + notifications
# --------------------------------------------------------------------------- #

def build_digest(events: list[dict]) -> str:
    today = dt.date.today().isoformat()
    lines = [f"# 🩺 Hackathon Scout — {today}", "", f"**{len(events)} new event(s) found.**", ""]
    by_src: dict[str, list[dict]] = {}
    for e in events:
        by_src.setdefault(e["source"], []).append(e)
    for src in sorted(by_src):
        lines.append(f"## {src}")
        for e in by_src[src]:
            meta = " · ".join(v for v in (e.get("dates"), e.get("location"), e.get("themes")) if v)
            lines.append(f"- **{e['title']}**" + (f" — {meta}" if meta else ""))
            lines.append(f"  <{e['url']}>")
        lines.append("")
    return "\n".join(lines)


def notify_telegram(events: list[dict]) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    lines = [f"🩺 {len(events)} new health hackathon(s):", ""]
    for e in events:
        meta = " · ".join(v for v in (e.get("dates"), e.get("location")) if v)
        lines.append(f"• {e['title']}" + (f" ({meta})" if meta else ""))
        lines.append(f"  {e['url']}")
        lines.append("")
    ok = True
    for chunk in chunks("\n".join(lines), 3500):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        ok = ok and r.ok
    return ok


def notify_email(digest: str, count: int) -> bool:
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    if not (host and user and pw and to):
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = MIMEText(digest, "plain", "utf-8")
    msg["Subject"] = f"🩺 {count} new health hackathon(s) found"
    msg["From"], msg["To"] = user, to
    with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return True


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main() -> None:
    seen: set[str] = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()

    raw: list[dict] = []
    for src in SOURCES:
        found = src()
        log(f"{src.__name__}: {len(found)} hits")
        raw.extend(found)

    uniq: dict[str, dict] = {}
    for e in raw:
        if e.get("url") and e.get("title"):
            uniq.setdefault(norm_url(e["url"]), e)

    relevant = [e for e in uniq.values() if is_relevant(e)]
    fresh = [e for e in relevant if event_id(e) not in seen]
    log(f"{len(raw)} scraped -> {len(uniq)} unique -> {len(relevant)} relevant -> {len(fresh)} new")

    shown = llm_filter(fresh)

    if not shown:
        print("Nothing new today. ✅")
    else:
        digest = build_digest(shown)
        if DRY:
            print(digest)
            log("dry run: no notifications sent, state not saved")
            return
        DIGEST_FILE.write_text(digest, encoding="utf-8")
        delivered = notify_telegram(shown)
        if not delivered:
            try:
                delivered = notify_email(digest, len(shown))
            except Exception as e:
                log(f"email failed: {e}")
        if not delivered:
            print(digest)  # fallback: at least it's in the logs

    if not DRY and fresh:
        # mark everything we evaluated as seen (incl. LLM-dropped items,
        # so they aren't re-scored on every run)
        seen.update(event_id(e) for e in fresh)
        STATE_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()

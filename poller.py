#!/usr/bin/env python3
"""
Command poller — runs every 5 minutes on GitHub Actions and reacts to
TELEGRAM commands only (Discord is push-only: it just receives the daily
12:00-Munich digest and has no commands):

  /search  (aliases: /update, /scan)  -> dispatches the scan workflow with
                                         channels=telegram, so the results of
                                         THIS scan go to Telegram only
  /all                                -> replies immediately with the full list
                                         of known upcoming hackathons
                                         (from the committed events.json)

Dedupe: Telegram updates are confirmed via the getUpdates offset, so each
message reaches this poller exactly once. Commands older than MAX_AGE are
ignored (but still confirmed) so a pile of stale messages can't trigger a
scan storm after downtime.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

from parser import chunks, event_status, fmt_span

ROOT = Path(__file__).resolve().parent
CATALOG_FILE = ROOT / "events.json"
TIMEOUT = 30
MAX_AGE = dt.timedelta(minutes=30)
ALL_LIMIT = 60  # cap /all output; the Notion table has everything

SEARCH_CMDS = ("/search", "/update", "/scan")  # /update + /scan kept as aliases
ALL_CMDS = ("/all",)

GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REF = os.environ.get("GITHUB_REF_NAME", "master")

_scan_dispatched = False


def log(msg: str) -> None:
    print(f"[poller] {msg}", file=sys.stderr)


def command_of(text: str) -> str | None:
    """'/search@MyBot now please' -> 'search'; None if not a known command."""
    first = (text or "").strip().split()[:1]
    if not first:
        return None
    word = first[0].lower().split("@")[0]
    if word in SEARCH_CMDS:
        return "search"
    if word in ALL_CMDS:
        return "all"
    return None


# --------------------------------------------------------------------------- #
#  /all — read the committed catalog and format the upcoming list
# --------------------------------------------------------------------------- #

def load_records() -> list[dict]:
    if not CATALOG_FILE.exists():
        return []
    try:
        return list(json.loads(CATALOG_FILE.read_text(encoding="utf-8")).get("events", {}).values())
    except Exception as e:
        log(f"events.json unreadable: {e}")
        return []


def all_text() -> str:
    today = dt.date.today()
    recs = [r for r in load_records() if event_status(r, today) in ("Upcoming", "Unknown")]
    if not recs:
        return ("The catalog is empty so far — send /search to run a scan first "
                "(the list fills up as scans run).")
    recs.sort(key=lambda r: (r.get("start") is None, r.get("start") or "9999", r.get("title", "")))
    shown = recs[:ALL_LIMIT]
    lines = [f"{len(recs)} known upcoming hackathon(s), soonest first:", ""]
    for r in shown:
        span = fmt_span(r.get("start"), r.get("end")) or r.get("dates") or "dates tbd"
        place = r.get("location") or r.get("country") or ""
        meta = " · ".join(v for v in (span, place) if v)
        lines.append(f"• {r['title']}")
        if meta:
            lines.append(f"  {meta}")
        lines.append(f"  {r['url']}")
        lines.append("")
    if len(recs) > ALL_LIMIT:
        lines.append(f"…and {len(recs) - ALL_LIMIT} more — see the Notion table for the full list.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Trigger the scan workflow (telegram-only channel routing)
# --------------------------------------------------------------------------- #

def trigger_scan() -> bool:
    global _scan_dispatched
    if _scan_dispatched:
        return True
    r = requests.post(
        f"https://api.github.com/repos/{REPO}/actions/workflows/scan.yml/dispatches",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
        json={"ref": REF, "inputs": {"channels": "telegram"}},
        timeout=TIMEOUT,
    )
    if r.status_code == 204:
        _scan_dispatched = True
        log("scan dispatched (channels=telegram)")
        return True
    log(f"dispatch failed: {r.status_code} {r.text[:200]}")
    return False


SEARCH_ACK = ("Search triggered — new finds arrive here in ~2 min. "
              "(Only *new* events are posted; use /all for everything known.)")
SEARCH_FAIL = "Couldn't trigger the scan workflow — check the Actions logs."


# --------------------------------------------------------------------------- #
#  Telegram
# --------------------------------------------------------------------------- #

def tg_send(token: str, chat: str, text: str) -> None:
    for chunk in chunks(text, 3500):
        for attempt in (1, 2):
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": chunk,
                                    "disable_web_page_preview": True},
                              timeout=TIMEOUT)
            if r.ok:
                break
            if r.status_code == 429 and attempt == 1:  # rate-limited mid-reply
                try:                                   # honor retry_after once
                    wait = int((r.json().get("parameters") or {}).get("retry_after", 3))
                except Exception:
                    wait = 3
                time.sleep(min(wait, 30))
                continue
            log(f"tg_send: HTTP {r.status_code} on a chunk — {r.text[:120]}")
            break


def poll_telegram() -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"limit": 100, "allowed_updates": '["message"]'},
                         timeout=TIMEOUT)
        r.raise_for_status()  # 401 (revoked token), 409, 429 etc. return JSON
        updates = r.json().get("result", [])  # too — don't mistake them for "no updates"
    except Exception as e:
        log(f"telegram poll failed: {e}")
        return

    now = dt.datetime.now(dt.timezone.utc)
    max_id = None
    for upd in updates:
        max_id = max(max_id or 0, upd.get("update_id", 0))
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != str(chat):
            continue
        cmd = command_of(msg.get("text", ""))
        if not cmd:
            continue
        sent = dt.datetime.fromtimestamp(msg.get("date", 0), tz=dt.timezone.utc)
        if now - sent > MAX_AGE:
            log(f"telegram: skipping stale /{cmd}")
            continue
        log(f"telegram: handling /{cmd}")
        try:  # one failing command must not block offset confirmation below —
            # otherwise the same batch is refetched every poll and the poller
            # wedges; worst case a rare transient error costs one command,
            # which the user simply re-sends.
            if cmd == "all":
                tg_send(token, chat, all_text())
            else:
                tg_send(token, chat, SEARCH_ACK if trigger_scan() else SEARCH_FAIL)
        except Exception as e:
            log(f"telegram: /{cmd} handler failed: {e}")

    if max_id is not None:  # confirm everything so it isn't re-delivered next poll
        try:
            requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": max_id + 1, "limit": 1}, timeout=TIMEOUT)
        except Exception as e:
            log(f"telegram offset confirm failed: {e}")


if __name__ == "__main__":
    poll_telegram()
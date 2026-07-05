#!/usr/bin/env python3
"""
Command poller — runs every 5 minutes on GitHub Actions and reacts to chat
commands on Telegram and Discord:

  /update  (or the old /scan)  -> dispatches the scan workflow; new finds are
                                  broadcast to every configured channel
  /all                         -> replies immediately with the full list of
                                  known upcoming hackathons (from events.json)

Dedupe:
  Telegram — updates are confirmed via getUpdates offset, so each message is
             delivered to this poller exactly once.
  Discord  — the bot marks handled messages with a ✅ reaction and skips any
             message it already reacted to.

Commands older than MAX_AGE are ignored (but still marked handled) so a pile
of stale messages can't trigger a scan storm after downtime.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests

from parser import event_status, fmt_span

ROOT = Path(__file__).resolve().parent
CATALOG_FILE = ROOT / "events.json"
TIMEOUT = 30
MAX_AGE = dt.timedelta(minutes=30)
ALL_LIMIT = 60  # cap /all output; the Notion table has everything

UPDATE_CMDS = ("/update", "/scan")  # /scan kept as a legacy alias
ALL_CMDS = ("/all",)

GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REF = os.environ.get("GITHUB_REF_NAME", "master")

_scan_dispatched = False


def log(msg: str) -> None:
    print(f"[poller] {msg}", file=sys.stderr)


def command_of(text: str) -> str | None:
    """'/update@MyBot now please' -> 'update'; None if not a known command."""
    first = (text or "").strip().split()[:1]
    if not first:
        return None
    word = first[0].lower().split("@")[0]
    if word in UPDATE_CMDS:
        return "update"
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


def all_text(wrap_urls: bool) -> str:
    today = dt.date.today()
    recs = [r for r in load_records() if event_status(r, today) in ("Upcoming", "Unknown")]
    if not recs:
        return ("The catalog is empty so far — send /update to run a scan first "
                "(the list fills up as scans run).")
    recs.sort(key=lambda r: (r.get("start") is None, r.get("start") or "9999", r.get("title", "")))
    shown = recs[:ALL_LIMIT]
    lines = [f"📋 {len(recs)} known upcoming hackathon(s), soonest first:", ""]
    for r in shown:
        span = fmt_span(r.get("start"), r.get("end")) or r.get("dates") or "dates tbd"
        place = r.get("location") or r.get("country") or ""
        meta = " · ".join(v for v in (span, place) if v)
        lines.append(f"• {r['title']}")
        lines.append(f"  {meta}")
        if r.get("reg_deadline"):
            lines.append(f"  📝 register by {fmt_span(r['reg_deadline'], None)}")
        lines.append(f"  <{r['url']}>" if wrap_urls else f"  {r['url']}")
        lines.append("")
    if len(recs) > ALL_LIMIT:
        lines.append(f"…and {len(recs) - ALL_LIMIT} more — see the Notion table for the full list.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Trigger the scan workflow
# --------------------------------------------------------------------------- #

def trigger_scan() -> bool:
    global _scan_dispatched
    if _scan_dispatched:
        return True
    r = requests.post(
        f"https://api.github.com/repos/{REPO}/actions/workflows/scan.yml/dispatches",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
        json={"ref": REF},
        timeout=TIMEOUT,
    )
    if r.status_code == 204:
        _scan_dispatched = True
        log("scan dispatched")
        return True
    log(f"dispatch failed: {r.status_code} {r.text[:200]}")
    return False


UPDATE_ACK = ("🔍 Update triggered — new finds arrive here in ~2 min. "
              "(Only *new* events are posted; use /all for everything known.)")
UPDATE_FAIL = "⚠️ Couldn't trigger the scan workflow — check the Actions logs."


# --------------------------------------------------------------------------- #
#  Chunking (Telegram 4096 / Discord 2000 char caps)
# --------------------------------------------------------------------------- #

def chunks(text: str, n: int):
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
#  Telegram
# --------------------------------------------------------------------------- #

def tg_send(token: str, chat: str, text: str) -> None:
    for chunk in chunks(text, 3500):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
                      timeout=TIMEOUT)


def poll_telegram() -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"limit": 100, "allowed_updates": '["message"]'},
                         timeout=TIMEOUT)
        updates = r.json().get("result", [])
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
        if cmd == "all":
            tg_send(token, chat, all_text(wrap_urls=False))
        else:
            tg_send(token, chat, UPDATE_ACK if trigger_scan() else UPDATE_FAIL)

    if max_id is not None:  # confirm everything so it isn't re-delivered next poll
        try:
            requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": max_id + 1, "limit": 1}, timeout=TIMEOUT)
        except Exception as e:
            log(f"telegram offset confirm failed: {e}")


# --------------------------------------------------------------------------- #
#  Discord
# --------------------------------------------------------------------------- #

_CHECK = "\u2705"  # ✅


def dc_headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}"}


def dc_send(token: str, channel: str, text: str) -> None:
    for chunk in chunks(text, 1900):
        requests.post(f"https://discord.com/api/v10/channels/{channel}/messages",
                      headers=dc_headers(token), json={"content": chunk}, timeout=TIMEOUT)


def dc_mark_handled(token: str, channel: str, message_id: str) -> None:
    requests.put(
        f"https://discord.com/api/v10/channels/{channel}/messages/{message_id}/reactions/{_CHECK}/@me",
        headers=dc_headers(token), timeout=TIMEOUT)


def poll_discord() -> None:
    token, channel = os.environ.get("DISCORD_BOT_TOKEN"), os.environ.get("DISCORD_CHANNEL_ID")
    if not (token and channel):
        return
    try:
        r = requests.get(f"https://discord.com/api/v10/channels/{channel}/messages",
                         headers=dc_headers(token), params={"limit": 30}, timeout=TIMEOUT)
        r.raise_for_status()
        messages = r.json()
    except Exception as e:
        log(f"discord poll failed: {e}")
        return

    now = dt.datetime.now(dt.timezone.utc)
    for msg in reversed(messages):  # oldest first
        if (msg.get("author") or {}).get("bot"):
            continue
        cmd = command_of(msg.get("content", ""))
        if not cmd:
            continue
        if any(rx.get("me") and (rx.get("emoji") or {}).get("name") == _CHECK
               for rx in msg.get("reactions", [])):
            continue  # already handled on a previous poll
        dc_mark_handled(token, channel, msg["id"])
        sent = dt.datetime.fromisoformat(msg["timestamp"])
        if now - sent > MAX_AGE:
            log(f"discord: skipping stale /{cmd}")
            continue
        log(f"discord: handling /{cmd}")
        if cmd == "all":
            dc_send(token, channel, all_text(wrap_urls=True))
        else:
            dc_send(token, channel, UPDATE_ACK if trigger_scan() else UPDATE_FAIL)


if __name__ == "__main__":
    poll_telegram()
    poll_discord()

"""Notion sync — mirrors the event catalog into a Notion database.

Setup (one-time, ~3 minutes):
  1. https://www.notion.so/my-integrations -> New integration -> copy the
     "Internal Integration Secret"            -> secret NOTION_TOKEN
  2. In Notion, create an empty *database* (full page, table view).
  3. On that database: ••• menu -> Connections -> add your integration.
  4. Copy the database id from its URL
     notion.so/<workspace>/<DATABASE_ID>?v=...  (32 hex chars)
                                                -> secret NOTION_DATABASE_ID

This module creates any missing columns by itself on the first run:
  Name (title) | Registration (date) | Dates (date) | Country (select) |
  Link (url)   | Status (select)     | Source (select) | Location |
  Dates (text) | Notes (yours — never written by the scout)

Only the columns above are ever written; add as many of your own columns
(and hand-made rows) as you like — the sync will leave them alone.
Events are upserted by Link, so re-runs update rows instead of duplicating.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time

import requests

from parser import event_status

API = "https://api.notion.com/v1"
TIMEOUT = 30
_SLEEP = 0.35  # Notion allows ~3 requests/second

REQUIRED_PROPS = {
    "Registration": {"date": {}},
    "Dates": {"date": {}},
    "Country": {"select": {}},
    "Link": {"url": {}},
    "Status": {"select": {}},
    "Source": {"select": {}},
    "Location": {"rich_text": {}},
    "Dates (text)": {"rich_text": {}},
    "Notes": {"rich_text": {}},  # created for YOU — the scout never writes here
}


def _log(msg: str) -> None:
    print(f"[notion] {msg}", file=sys.stderr)


def enabled() -> bool:
    return bool(os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID"))


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _db_id() -> str:
    return os.environ["NOTION_DATABASE_ID"].replace("-", "").strip()


def _select_name(value: str) -> str:
    """Notion select options may not contain commas."""
    return value.replace(",", " /")[:100]


def ensure_schema() -> str:
    """Add any missing columns; return the name of the title property."""
    r = requests.get(f"{API}/databases/{_db_id()}", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    props = r.json().get("properties", {})
    title_prop = next((n for n, p in props.items() if p.get("type") == "title"), "Name")
    missing = {n: spec for n, spec in REQUIRED_PROPS.items() if n not in props}
    if missing:
        _log(f"adding columns: {', '.join(missing)}")
        r = requests.patch(f"{API}/databases/{_db_id()}", headers=_headers(),
                           json={"properties": missing}, timeout=TIMEOUT)
        r.raise_for_status()
    return title_prop


def build_props(ev: dict, title_prop: str, today: dt.date) -> dict:
    props: dict = {
        title_prop: {"title": [{"text": {"content": (ev.get("title") or "Untitled")[:200]}}]},
        "Link": {"url": ev.get("url") or None},
        "Status": {"select": {"name": _select_name(event_status(ev, today))}},
    }
    if ev.get("start"):
        end = ev.get("end")
        props["Dates"] = {"date": {"start": ev["start"],
                                   "end": end if end and end != ev["start"] else None}}
    if ev.get("reg_deadline"):
        props["Registration"] = {"date": {"start": ev["reg_deadline"]}}
    if ev.get("country"):
        props["Country"] = {"select": {"name": _select_name(ev["country"])}}
    if ev.get("source"):
        props["Source"] = {"select": {"name": _select_name(ev["source"])}}
    if ev.get("location"):
        props["Location"] = {"rich_text": [{"text": {"content": ev["location"][:500]}}]}
    if ev.get("dates") and not ev.get("start"):  # keep the raw string when unparsed
        props["Dates (text)"] = {"rich_text": [{"text": {"content": ev["dates"][:500]}}]}
    return props


def _find_page(url: str) -> str | None:
    r = requests.post(f"{API}/databases/{_db_id()}/query", headers=_headers(),
                      json={"filter": {"property": "Link", "url": {"equals": url}},
                            "page_size": 1},
                      timeout=TIMEOUT)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0]["id"] if results else None


def sync(events: list[dict], today: dt.date | None = None) -> int:
    """Upsert events into the database. Returns number of pages touched.

    Events carrying an unchanged `notion_hash` are skipped (no API calls),
    and the hash is written back onto the event dict for the caller to save.
    """
    if not enabled() or not events:
        return 0
    today = today or dt.date.today()
    try:
        title_prop = ensure_schema()
    except Exception as e:
        _log(f"schema check failed, skipping sync: {e}")
        return 0

    touched = 0
    for ev in events:
        try:
            if not ev.get("url"):  # hand-edited / partial record — can't upsert
                continue
            props = build_props(ev, title_prop, today)
            payload_hash = hashlib.sha1(
                json.dumps(props, sort_keys=True).encode()).hexdigest()
            if ev.get("notion_hash") == payload_hash:
                continue
            time.sleep(_SLEEP)
            page_id = _find_page(ev["url"])
            time.sleep(_SLEEP)
            if page_id:
                r = requests.patch(f"{API}/pages/{page_id}", headers=_headers(),
                                   json={"properties": props}, timeout=TIMEOUT)
            else:
                r = requests.post(f"{API}/pages", headers=_headers(),
                                  json={"parent": {"database_id": _db_id()},
                                        "properties": props},
                                  timeout=TIMEOUT)
            r.raise_for_status()
            ev["notion_hash"] = payload_hash
            touched += 1
        except Exception as e:
            _log(f"upsert failed for {ev.get('url', '?')}: {e}")
    _log(f"synced {touched}/{len(events)} events")
    return touched
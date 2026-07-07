"""Event parsing: date parsing, registration deadlines, country detection.

All best-effort — scraped date strings are messy ("Jul 11 - 13, 2026",
"JUL 11TH - 13TH", "11.–13. Juli 2026", "2026-03-05"...). Anything we can't
parse simply stays None; unknown never means "drop the event".
"""

from __future__ import annotations

import datetime as dt
import re

# --------------------------------------------------------------------------- #
#  Months (EN + DE, matched by first 3 letters)
# --------------------------------------------------------------------------- #

_MONTHS3 = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11,
    "dec": 12, "dez": 12,
}
_MON_RE = r"(?:jan|feb|mar|mär|maer|apr|may|mai|jun|jul|aug|sep|oct|okt|nov|dec|dez)[a-zäöü]*\.?"


def _month_num(token: str) -> int | None:
    key = token.lower().replace("ä", "a").replace("maer", "mar")[:3]
    return _MONTHS3.get(key)


def _mkdate(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
#  Single-date patterns (on normalized text)
# --------------------------------------------------------------------------- #

_RE_ISO = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_RE_DMY_NUM = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")           # 11.07.2026
_RE_DM_NUM = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)")                 # 11.07.
_RE_MD = re.compile(rf"\b({_MON_RE})\s+(\d{{1,2}})(?!\d)(?:\s*,?\s*(20\d{{2}}))?", re.I)   # Jul 11(, 2026)
_RE_DM = re.compile(rf"\b(\d{{1,2}})\.?\s*(?:of\s+)?({_MON_RE})\s*,?\s*(20\d{{2}})?", re.I)  # 11(.) Juli (2026)
_RE_BARE_DAY = re.compile(r"^\s*(\d{1,2})\.?\s*(?:,\s*(20\d{2}))?\s*$")    # "13" / "13, 2026"


def _normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", t)   # 11th -> 11
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", t)          # fancy dashes -> "-"
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _parse_single(text: str, default_year: int | None = None):
    """-> (year|None, month|None, day|None, had_explicit_year). Partial results allowed."""
    t = text.strip()
    if m := _RE_ISO.search(t):
        return int(m[1]), int(m[2]), int(m[3]), True
    if m := _RE_DMY_NUM.search(t):
        return int(m[3]), int(m[2]), int(m[1]), True
    if m := _RE_DM_NUM.search(t):
        return default_year, int(m[2]), int(m[1]), False
    if m := _RE_MD.search(t):
        mon = _month_num(m[1])
        if mon:
            return (int(m[3]) if m[3] else default_year), mon, int(m[2]), bool(m[3])
    if m := _RE_DM.search(t):
        mon = _month_num(m[2])
        if mon:
            return (int(m[3]) if m[3] else default_year), mon, int(m[1]), bool(m[3])
    if m := _RE_BARE_DAY.match(t):
        day = int(m[1])
        if 1 <= day <= 31:
            return (int(m[2]) if m[2] else default_year), None, day, bool(m[2])
    return None, None, None, False


# --------------------------------------------------------------------------- #
#  Public: parse a dedicated date string like "Nov 30 - Dec 4, 2026"
# --------------------------------------------------------------------------- #

def parse_date_range(text: str, default_year: int | None = None):
    """Parse a *date field* (not free prose) into (start, end) dt.dates or Nones."""
    if not text or not text.strip():
        return None, None
    t = _normalize(text)

    # ISO dates contain dashes — handle them before the range split
    if isos := _RE_ISO.findall(t):
        ds = [d for a, b, c in isos if (d := _mkdate(int(a), int(b), int(c)))]
        if ds:
            return min(ds), max(ds)

    t = re.sub(r"\s*(?:\bto\b|\bbis\b|-)\s*", " | ", t, count=1) if re.search(r"(?:\bto\b|\bbis\b|-)", t) else t

    if " | " in t:
        left_s, right_s = t.split(" | ", 1)
    else:
        left_s, right_s = t, ""

    ly, lm, ld, l_year = _parse_single(left_s, default_year)
    ry, rm, rd, r_year = _parse_single(right_s, default_year) if right_s else (None, None, None, False)

    # inherit missing pieces across the dash: "Jul 11 - 13" / "11. - 13. Juli 2026"
    if ld is not None and rd is not None:
        if lm is None and rm is not None:
            lm = rm
        if rm is None and lm is not None:
            rm = lm
        if ly is None and ry is not None:      # "March 5 - 7, 2026"
            ly = ry - 1 if (lm and rm and lm > rm) else ry
        if ry is None and ly is not None:      # "Nov 30, 2025 - Jan 15" (rare)
            ry = ly + 1 if (lm and rm and rm < lm) else ly

    year_guess = default_year or dt.date.today().year
    start = _mkdate(ly or year_guess, lm, ld) if (lm and ld) else None
    end = _mkdate(ry or year_guess, rm, rd) if (rm and rd) else None

    # cross-year range without explicit years: "Dec 28 - Jan 3"
    if start and end and end < start and not (l_year or r_year):
        end = _mkdate(end.year + 1, end.month, end.day)

    # no year given anywhere and the range looks long past -> probably next year
    if start and not (l_year or r_year) and default_year is None:
        ref = end or start
        if ref < dt.date.today() - dt.timedelta(days=14):
            start = _mkdate(start.year + 1, start.month, start.day)
            end = _mkdate(end.year + 1, end.month, end.day) if end else None

    if start and not end:
        end = start
    if start is None and end:
        start = end
    if start and end and end < start:
        end = start
    return start, end


# --------------------------------------------------------------------------- #
#  Public: fish full dates / a registration deadline out of free-form text
# --------------------------------------------------------------------------- #

_FULL_DATE = re.compile(
    rf"(20\d{{2}}-\d{{1,2}}-\d{{1,2}})|(\d{{1,2}}\.\d{{1,2}}\.20\d{{2}})"
    rf"|({_MON_RE}\s+\d{{1,2}}(?!\d)\s*,?\s*(?:20\d{{2}})?)"
    rf"|(\d{{1,2}}\.?\s*{_MON_RE}\s*,?\s*(?:20\d{{2}})?)",
    re.I,
)

_REG_WORDS = re.compile(
    r"(register(?:ed)?|registration|apply|application|sign.?up|deadline|"
    r"submissions?\s+(?:close|due)|closes?|anmeld\w*|bewerbungs?\w*|frist)",
    re.I,
)


def dates_from_text(text: str, default_year: int | None = None):
    """First/last full date found in prose -> (start, end). None if nothing solid."""
    if not text:
        return None, None
    t = _normalize(text)
    found = []
    for m in _FULL_DATE.finditer(t):
        y, mo, d, _ = _parse_single(m.group(0), default_year)
        if mo and d:
            date = _mkdate(y or default_year or dt.date.today().year, mo, d)
            if date:
                found.append(date)
    if not found:
        return None, None
    return min(found), max(found)


def find_reg_deadline(text: str, default_year: int | None = None) -> dt.date | None:
    """A date that appears shortly *after* a registration-ish keyword."""
    if not text:
        return None
    t = _normalize(text)
    for m in _FULL_DATE.finditer(t):
        window = t[max(0, m.start() - 60):m.start()]
        if _REG_WORDS.search(window):
            y, mo, d, _ = _parse_single(m.group(0), default_year)
            if mo and d:
                return _mkdate(y or default_year or dt.date.today().year, mo, d)
    return None


# --------------------------------------------------------------------------- #
#  Country from a free-form location string
# --------------------------------------------------------------------------- #

_ONLINE = re.compile(r"\b(online|virtual|remote|worldwide|global|everywhere|digital)\b", re.I)

_US_STATES = set("""al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo
mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc""".split())
_US_STATE_NAMES = set("""alabama alaska arizona arkansas california colorado connecticut delaware
florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland
massachusetts michigan minnesota mississippi missouri montana nebraska nevada ohio oklahoma
oregon pennsylvania tennessee texas utah vermont virginia washington wisconsin wyoming""".split()) \
    | {"new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
       "rhode island", "south carolina", "south dakota", "west virginia"}
_CA_PROVINCES = {"on", "bc", "qc", "ab", "mb", "sk", "ns", "nb", "nl", "pe", "ontario", "quebec",
                 "british columbia", "alberta", "manitoba", "saskatchewan", "nova scotia"}
_ISO2 = {"de": "Germany", "at": "Austria", "ch": "Switzerland", "fr": "France", "nl": "Netherlands",
         "be": "Belgium", "es": "Spain", "it": "Italy", "pt": "Portugal", "pl": "Poland",
         "cz": "Czechia", "se": "Sweden", "dk": "Denmark", "no": "Norway", "fi": "Finland",
         "ie": "Ireland", "gb": "United Kingdom", "uk": "United Kingdom", "us": "United States",
         "sg": "Singapore", "in": "India", "jp": "Japan", "au": "Australia", "br": "Brazil"}
_ALIASES = {"usa": "United States", "u.s.": "United States", "u.s.a.": "United States",
            "united states of america": "United States", "america": "United States",
            "deutschland": "Germany", "the netherlands": "Netherlands", "holland": "Netherlands",
            "uae": "United Arab Emirates", "england": "United Kingdom", "great britain": "United Kingdom",
            "schweiz": "Switzerland", "österreich": "Austria", "czech republic": "Czechia",
            "südkorea": "South Korea", "korea": "South Korea", "frankreich": "France",
            "italien": "Italy", "spanien": "Spain", "polen": "Poland", "belgien": "Belgium"}
_KNOWN_COUNTRIES = {"germany", "austria", "switzerland", "france", "netherlands", "belgium",
                    "spain", "italy", "portugal", "poland", "czechia", "sweden", "denmark",
                    "norway", "finland", "ireland", "united kingdom", "united states", "canada",
                    "singapore", "india", "japan", "china", "australia", "brazil", "mexico",
                    "israel", "turkey", "greece", "hungary", "romania", "estonia", "latvia",
                    "lithuania", "luxembourg", "slovenia", "slovakia", "croatia", "serbia",
                    "ukraine", "south korea", "taiwan", "hong kong", "indonesia", "malaysia",
                    "thailand", "vietnam", "philippines", "south africa", "egypt", "nigeria",
                    "kenya", "argentina", "chile", "colombia", "new zealand",
                    "united arab emirates", "saudi arabia", "qatar", "iceland"}


def country_from_location(location: str) -> str:
    """'Boston, MA' -> 'United States'; 'Online' -> 'Online'; unknown -> ''."""
    if not location or not location.strip():
        return ""
    if _ONLINE.search(location):
        return "Online"
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return ""
    last = parts[-1].strip(" .").lower()
    if last in _ALIASES:
        return _ALIASES[last]
    if last in _US_STATES or last in _US_STATE_NAMES:
        return "United States"
    if last in _CA_PROVINCES:
        return "Canada"
    if last in _ISO2:
        return _ISO2[last]
    if last in _KNOWN_COUNTRIES:
        return last.title()
    # multi-part location -> trust the last chunk as a country-ish label
    if len(parts) >= 2 and len(last) > 3 and not any(ch.isdigit() for ch in last):
        return parts[-1].strip(" .")
    return ""


# --------------------------------------------------------------------------- #
#  Event status from parsed ISO strings (shared by agent, notion sync, poller)
# --------------------------------------------------------------------------- #

def event_status(ev: dict, today: dt.date) -> str:
    """'Past' / 'Ongoing' / 'Reg. closed' / 'Upcoming' / 'Unknown'.

    Works on records with ISO-string fields start/end/reg_deadline (any may be
    None). ISO strings compare correctly as plain strings.
    """
    t = today.isoformat()
    start = ev.get("start")
    end = ev.get("end") or start
    reg = ev.get("reg_deadline")
    if end and end < t:
        return "Past"
    if start and start <= t and (not end or end >= t):
        return "Ongoing"
    if reg and reg < t:
        return "Reg. closed"
    if start:
        return "Upcoming"
    return "Unknown"


# --------------------------------------------------------------------------- #
#  Pretty-printing
# --------------------------------------------------------------------------- #

def fmt_span(start_iso: str | None, end_iso: str | None) -> str:
    """'2026-07-11','2026-07-13' -> 'Jul 11–13, 2026' (empty string if unknown)."""
    if not start_iso:
        return ""
    s = dt.date.fromisoformat(start_iso)
    e = dt.date.fromisoformat(end_iso) if end_iso else s
    if s == e:
        return s.strftime("%b %d, %Y")
    if (s.year, s.month) == (e.year, e.month):
        return f"{s.strftime('%b %d')}–{e.day:02d}, {s.year}"
    if s.year == e.year:
        return f"{s.strftime('%b %d')} – {e.strftime('%b %d')}, {s.year}"
    return f"{s.strftime('%b %d, %Y')} – {e.strftime('%b %d, %Y')}"


# --------------------------------------------------------------------------- #
#  Shared text util (used by agent.py and poller.py)
# --------------------------------------------------------------------------- #

def chunks(text: str, n: int):
    """Split text into <=n char pieces on line boundaries (chat APIs cap
    message length; Telegram 4096, Discord 2000). Single lines longer than n
    are hard-split so one oversized line can never yield an oversized chunk,
    and empty / whitespace-only chunks are never emitted (chat APIs reject
    empty message text with HTTP 400). Such chunks can arise when a blank
    line lands right after a size-triggered buffer flush (buf == [""])."""
    def _raw():
        buf: list[str] = []
        size = 0
        for line in text.splitlines():
            while len(line) > n:  # pathological single line — hard split
                if buf:
                    yield "\n".join(buf)
                    buf, size = [], 0
                yield line[:n]
                line = line[n:]
            if size + len(line) > n and buf:
                yield "\n".join(buf)
                buf, size = [], 0
            buf.append(line)
            size += len(line) + 1
        if buf:
            yield "\n".join(buf)

    for chunk in _raw():
        if chunk.strip():
            yield chunk
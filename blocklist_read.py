"""
blocklist_read.py
Reads the master "All Client Block List" Google Sheet (all tabs) and returns a
map of {block_value -> earliest date_added}. Used to audit whether placements
that are supposedly blocked are still serving in the report.

Set BLOCKLIST_READ_URL to the sheet's normal share link (or just its ID). The
sheet must be shared "anyone with the link can view". Reading uses the gviz CSV
endpoint so we can pull each tab by name (no gid needed).
"""
import os
import io
import re
import csv
import datetime
import urllib.request
import urllib.parse

DEFAULT_TABS = ["All Products", "CTV", "Audio", "Social Mirror"]
_HEADER_WORDS = {"bundle id", "bundle id (app only)", "url/identifier", "domain or app",
                 "type", "formatting", "formatted", "date added"}
_GVIZ = "https://docs.google.com/spreadsheets/d/{id}/gviz/tq?tqx=out:csv&sheet={tab}"


def _sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    # maybe they set just the bare ID
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", url.strip()):
        return url.strip()
    return None


def _parse_date(v):
    """Handle gviz 'Date(2026,3,29)' (month 0-indexed) and plain 'M/D/YYYY'."""
    if not v:
        return None
    v = v.strip()
    m = re.match(r"Date\((\d+),(\d+),(\d+)", v)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(y, mo + 1, d)
        except ValueError:
            return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _fetch_tab(sheet_id, tab, timeout=8):
    url = _GVIZ.format(id=sheet_id, tab=urllib.parse.quote(tab))
    req = urllib.request.Request(url, headers={"User-Agent": "adtini-insights"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(raw)))


def _looks_like_header(row):
    """True if any cell in the row is one of the known header labels. Used to skip
    a header row ONLY when one is actually present — some tabs (e.g. 'Excluded')
    have no header, and blindly dropping row 0 would lose the first real entry."""
    return any(str(c).strip().lower() in _HEADER_WORDS for c in row if c is not None)


def load_blocklist(url=None, tabs=None, value_col=1, date_col=4):
    """Return {value_lower: {'value': original, 'date_added': date|None, 'tabs': set}}.
    Column defaults: B (index 1) = block value, E (index 4) = date added.
    Never raises; returns {} if unavailable."""
    url = url or os.environ.get("BLOCKLIST_READ_URL", "").strip()
    if not url:
        return {}
    sheet_id = _sheet_id(url)
    if not sheet_id:
        return {}
    tabs = tabs or DEFAULT_TABS
    out = {}
    for tab in tabs:
        try:
            rows = _fetch_tab(sheet_id, tab)
        except Exception:
            continue
        if not rows:
            continue
        # Drop the first row only if it's genuinely a header. The main tabs have a
        # header ("Domain or App / Bundle ID / Type / ... / Date added"); the
        # 'Excluded' tab does NOT, so its first entry must be kept.
        body = rows[1:] if _looks_like_header(rows[0]) else rows
        for r in body:
            if len(r) <= value_col:
                continue
            val = (r[value_col] or "").strip()
            if not val or val.lower() in _HEADER_WORDS:
                continue
            d = _parse_date(r[date_col]) if len(r) > date_col else None
            key = val.lower()
            if key not in out:
                out[key] = {"value": val, "date_added": d, "tabs": set(), "tab_dates": {}}
            entry = out[key]
            entry["tabs"].add(tab)
            # earliest date per tab
            if d and (tab not in entry["tab_dates"] or entry["tab_dates"][tab] is None
                      or d < entry["tab_dates"][tab]):
                entry["tab_dates"][tab] = d
            elif tab not in entry["tab_dates"]:
                entry["tab_dates"][tab] = d
            # overall earliest date_added
            if d and (entry["date_added"] is None or d < entry["date_added"]):
                entry["date_added"] = d
    return out

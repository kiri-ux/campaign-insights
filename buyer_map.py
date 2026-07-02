"""
buyer_map.py
Optional partner -> buyer lookup.

Set the environment variable BUYER_MAP_URL to a *published* Google Sheet CSV
(In Google Sheets: File -> Share -> Publish to web -> pick the sheet -> CSV).
Any public CSV URL works. The sheet needs a column identifying the partner
(named "Partner", "Business Unit", "BU", or "Advertiser") and a "Buyer" column.

If the variable isn't set or the fetch fails, buyer lookups are simply skipped —
the app works exactly as before.
"""
import os
import io
import re
import csv
import urllib.request

_PARTNER_KEYS = ("partner", "business unit", "advertiser", "agency")
_BUYER_KEYS = ("buyer", "trader", "account manager", "owner")


def _csv_url(url):
    """Turn a normal Google Sheets link into a CSV-export link. Leaves other
    URLs (already-published CSVs, plain CSV files) unchanged."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        return url
    sheet_id = m.group(1)
    gid = None
    g = re.search(r"[#&?]gid=(\d+)", url)
    if g:
        gid = g.group(1)
    out = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid:
        out += f"&gid={gid}"
    return out


def _pick(header, keys):
    low = {h.lower().strip(): h for h in header}
    for k in keys:
        for lc, orig in low.items():
            if lc == k:
                return orig
    for k in keys:
        for lc, orig in low.items():
            if k in lc:
                return orig
    return None


def load_buyer_map(url=None, timeout=8):
    """Return {partner_name: buyer} or {} if unavailable. Never raises.

    Column resolution: first try to match header names (Partner/Buyer etc.);
    if that fails, fall back to positional column B (partner) and C (buyer),
    treating the first row as a header.
    """
    url = url or os.environ.get("BUYER_MAP_URL", "").strip()
    if not url:
        return {}
    try:
        req = urllib.request.Request(_csv_url(url), headers={"User-Agent": "adtini-insights"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(raw)))
        if len(rows) < 2:
            return {}
        header = rows[0]
        pcol = _pick(header, _PARTNER_KEYS)
        bcol = _pick(header, _BUYER_KEYS)
        if pcol is not None and bcol is not None and header.index(pcol) != header.index(bcol):
            pi, bi = header.index(pcol), header.index(bcol)
        else:  # positional fallback: column B = partner, column C = buyer
            pi, bi = 1, 2
        out = {}
        for r in rows[1:]:
            if len(r) > max(pi, bi):
                partner = (r[pi] or "").strip()
                buyer = (r[bi] or "").strip()
                if partner and buyer:
                    out[partner] = buyer
        return out
    except Exception:
        return {}


def buyer_for(partner, bmap):
    """Case-insensitive lookup with light fuzzing on surrounding whitespace."""
    if not bmap or not isinstance(partner, str):
        return ""
    if partner in bmap:
        return bmap[partner]
    low = {k.lower().strip(): v for k, v in bmap.items()}
    return low.get(partner.lower().strip(), "")

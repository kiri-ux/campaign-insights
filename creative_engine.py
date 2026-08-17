"""
creative_engine.py
Everything the creative-grain export can tell us: is the data trustworthy, is
anything trafficked wrong, and which creatives are actually working.

Input is the TapClicks *creative-insights* export (one row per date x campaign x
creative). Three families of output, all rendered in the Creative tab of the
main dashboard:

1. DATA QUALITY — the vendor errors we want to catch the day they happen
   blank_creative     Creative Name empty or a placeholder ('n/a', 'untitled'…).
                      Rolled up to the campaign, with every ID the export
                      carries so the vendor can find the row.
   unmapped_product   Product 2 isn't a Vici product (campaign name leaked in)
   missing_bu         Client Business Unit blank
   impossible_metrics clicks > impressions, or a negative count
   delivery_no_spend  impressions > 0 but billable spend == 0

2. TRAFFICKING — the creative doesn't match the product it's running under
   sm_display_size    Social Mirror creative whose name carries an IAB DISPLAY
                      banner size (300x250, 728x90…). Social Mirror should be
                      running social-native assets; a banner size in the name is
                      the signature of a display creative on a social line.
   sm_social_size     Social Mirror with a social-native size (1080x1080…) —
                      naming noise, not an error. Kept separate so the real
                      signal doesn't drown.
   format_mismatch    Video product carrying a still-image asset, or a display
                      product carrying a video file.

3. PERFORMANCE — creative-grain, which the placement dashboard can't see
   winners / laggards vs each product's own CTR norm, zero-click waste,
   zero-conversion spend, creative fatigue (CTR decay across the window),
   no-rotation campaigns, dominant creatives, and names reused across clients.

Click-based judgements skip CTV / Social Mirror CTV / Online Audio, where a low
CTR is expected and not a finding — same policy the placement dashboard uses.
"""
import os
import re

import numpy as np
import pandas as pd

# --- column aliases -------------------------------------------------------
# The engine works on the Title Case names tap_adapter normalizes to, but stays
# tolerant of a raw export being handed straight in.
_ALIASES = {
    "creative": ["Creative Name", "creative_name", "Creative", "creative"],
    "creative_id": ["Creative ID", "creative_id", "Creative Id"],
    "creative_size": ["Creative Size", "creative_size"],
    "creative_type": ["Creative Type", "creative_type"],
    "click_url": ["Clickthrough URL", "creative_clickthrough_url",
                  "Creative Clickthrough URL", "clickthrough_url"],
    "preview_url": ["Preview Image URL", "preview_image_url", "preview_url"],
    "q25": ["25% Completed", "25_completed", "completed_25"],
    "q50": ["50% Completed", "50_completed", "completed_50"],
    "q75": ["75% Completed", "75_completed", "completed_75"],
    "q100": ["100% Completed", "100_completed", "completed_100"],
    "date": ["Date", "date"],
    "bu": ["Client Business Unit", "Business Unit", "client_business_unit"],
    "client": ["Client", "client"],
    "product": ["Product 2", "Product", "product_2"],
    "strategy_type": ["Strategy Type", "strategy_type"],
    "strategy_name": ["Strategy Name", "strategy_name"],
    "campaign": ["Campaign Pool Name", "campaign_pool_name", "Campaign Name"],
    "campaign_id": ["Campaign ID", "campaign_id"],
    "pool_id": ["Campaign Pool ID", "campaign_pool_id"],
    "impressions": ["Impressions", "impressions"],
    "clicks": ["Clicks", "clicks"],
    "conv": ["Post Click Conversions", "post_click_conversions"],
    "vconv": ["Post View Conversions", "post_view_conversions"],
    "spend": ["Billable Spend", "billable_spend", "Internal Cost", "Total Spend"],
}

# Values that are technically populated but mean "nobody named this creative".
_PLACEHOLDERS = {"n/a", "na", "#n/a", "-", "--", "null", "none", "nan", "unknown",
                 "untitled", "unnamed", "no name", "tbd", "test", "0", "."}
_COPY_OF = re.compile(r"^(copy of|untitled|new creative|creative\s*\d*)\b", re.I)

# Known Vici products — anything else in Product 2 is the vendor leaking a
# campaign/strategy string into the product column.
_KNOWN_PRODUCTS = {
    "display", "social mirror", "social mirror ctv", "ctv", "native display",
    "native video", "online audio", "audio", "video", "ctv + video",
    "geo-framing", "geo-fencing", "connected tv", "dynamic",
    "website visitor id", "performance max", "pay-per-click", "youtube",
    "amazon premium display", "search engine optimization",
    "reputation management", "streaming audio", "spotify", "addressable geo-fencing",
}

# Products where clicks aren't the point — excluded from CTR/zero-click flags.
_NO_CLICK_PRODUCTS = {"ctv", "social mirror ctv", "online audio", "audio",
                      "connected tv", "streaming audio"}

# WxH anywhere in a creative name. Guarded so '1.5x2' and version strings like
# 'v2x' don't match.
_SIZE_RE = re.compile(r"(?<![\d.])(\d{2,4})\s?[xX×]\s?(\d{2,4})(?![\d.])")

# IAB display banner sizes — a Social Mirror creative named with one of these is
# almost certainly a display asset on a social line.
_DISPLAY_SIZES = {
    "300x250", "728x90", "320x50", "300x600", "160x600", "336x280", "970x250",
    "970x90", "300x50", "320x100", "234x60", "468x60", "250x250", "200x200",
    "240x400", "580x400", "300x1050", "320x480", "480x320", "120x600", "180x150",
}
# Social-native aspect ratios — fine on Social Mirror, just naming noise.
_SOCIAL_SIZES = {
    "1080x1080", "1080x1920", "1200x628", "1080x1350", "1200x1200", "1920x1080",
    "1080x608", "600x600", "1080x566", "640x640", "1200x675",
}

_VIDEO_EXT = re.compile(r"\.(?:mp4|mov|m4v|webm|avi)\b", re.I)
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp)\b", re.I)
_VIDEO_PRODUCTS = {"video", "ctv", "social mirror ctv", "native video", "connected tv"}
_DISPLAY_PRODUCTS = {"display", "native display"}


def _env_num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _col(df, key):
    """First matching column name for an alias key, or None."""
    for c in _ALIASES.get(key, []):
        if c in df.columns:
            return c
    return None


def _num(s):
    return pd.to_numeric(s, errors="coerce").fillna(0)


def blank_reason(v):
    """'empty' | 'placeholder' | None for one Creative Name value."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "empty"
    s = str(v).strip()
    if not s:
        return "empty"
    low = s.lower()
    if low in _PLACEHOLDERS:
        return "placeholder"
    if _COPY_OF.match(s) and len(s) <= 24:
        return "placeholder"
    if len(s) <= 2:
        return "placeholder"
    return None


def sizes_in(name):
    """Every WxH token in a creative name, normalized to '300x250'."""
    return [f"{a}x{b}" for a, b in _SIZE_RE.findall(str(name))]


def _window(df, dcol):
    if not dcol:
        return None, None
    d = pd.to_datetime(df[dcol], errors="coerce").dropna()
    if not len(d):
        return None, None
    return d.min().date().isoformat(), d.max().date().isoformat()


def _rank(df, by, n):
    """Top-n by a column, tolerant of the column being absent/empty."""
    if df is None or not len(df) or by not in df.columns:
        return df if df is not None else pd.DataFrame()
    return df.sort_values(by, ascending=False).head(n).reset_index(drop=True)


def _is_blank(series):
    """True where a text column is empty in any of the ways an export manages."""
    s = series.astype(str).str.strip().str.lower()
    return series.isna() | s.isin(["", "nan", "none", "null", "n/a", "na", "-", "#n/a", "0"])


# ------------------------------------------------------------------------ UTMs
_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
             "utm_id", "utm_source_platform")
# Click IDs and other tracking that isn't UTM — worth knowing about, but it does
# not give you campaign attribution in GA.
_OTHER_TRACKING = ("gclid", "fbclid", "msclkid", "ttclid", "dclid", "wbraid", "gbraid",
                   "cid", "mc_cid", "li_fat_id", "epik", "twclid", "sccid", "vmcid")


def parse_utms(url):
    """(present_keys, other_tracking_keys) for one clickthrough URL. Reads the
    whole string rather than only the query, because vendor URLs frequently
    double-encode or append the tag block after a fragment."""
    if url is None or (isinstance(url, float) and pd.isna(url)):
        return (), ()
    u = str(url).strip().lower()
    if not u:
        return (), ()
    present = tuple(k for k in _UTM_KEYS if f"{k}=" in u)
    other = tuple(k for k in _OTHER_TRACKING if f"{k}=" in u)
    return present, other


def analyze_utms(d, cols, ccol, bad_mask):
    """How much of the creative library — and of actual delivery — carries UTM
    tagging on its clickthrough URL. Returns None when the export has no
    clickthrough column at all."""
    ucol = cols.get("click_url")
    if not ucol or ucol not in d.columns:
        return None
    named = d[~bad_mask].copy()
    if not len(named):
        return None
    named["_url"] = named[ucol].astype(str).where(~_is_blank(named[ucol]), "")

    # Group to the creative FIRST, then parse once per creative. Parsing per row
    # and aggregating with max() would let a creative land in two buckets at once
    # when the export carries different URLs for it on different dates.
    gk = [c for c in (cols["client"], cols["product"], cols["campaign_id"],
                      cols["creative_id"], ccol) if c]
    per = named.groupby(gk, dropna=False, observed=True).agg(
        impressions=("_impr", "sum"), clicks=("_clicks", "sum"), spend=("_spend", "sum"),
        # the URL that carries the most delivery wins when a creative has several
        url=("_url", lambda s: next((v for v in s if v), "")),
        urls_seen=("_url", lambda s: int(len({v for v in s if v}))),
    ).reset_index()
    ren = {cols["client"]: "client", cols["product"]: "product",
           cols["campaign_id"]: "campaign_id", cols["creative_id"]: "creative_id",
           ccol: "creative"}
    per = per.rename(columns={k: v for k, v in ren.items() if k})
    for c in ("client", "product", "campaign_id", "creative_id", "creative"):
        if c in per.columns and str(per[c].dtype) == "category":
            per[c] = per[c].astype(str)

    parsed = per["url"].map(parse_utms)
    per["utm_params"] = parsed.map(lambda t: ", ".join(t[0]))
    per["utm_count"] = parsed.map(lambda t: len(t[0]))
    per["other_tracking"] = parsed.map(lambda t: ", ".join(t[1]))
    per["no_url"] = per["url"].eq("")
    # The core three are what GA needs to attribute a session to the campaign.
    per["has_core"] = parsed.map(lambda t: all(k in t[0] for k in
                                              ("utm_source", "utm_medium", "utm_campaign")))
    per["status"] = np.where(per["no_url"], "no clickthrough URL",
                     np.where(per["utm_count"] == 0, "no UTM codes",
                       np.where(per["has_core"], "fully tagged", "partially tagged")))
    per["ctr"] = np.where(per["impressions"] > 0, per["clicks"] / per["impressions"], 0.0)

    # Headline counts are DISTINCT creatives (by ID when the export carries one,
    # else by name). Collapse to that grain BEFORE classifying: a creative that
    # runs in five campaigns is one creative, and if those campaigns carry
    # different URLs it gets one verdict — from the URL behind the most delivery —
    # instead of landing in two buckets at once.
    idkey = ("creative_id" if "creative_id" in per.columns
             and per["creative_id"].astype(str).str.strip().ne("").any() else "creative")
    ranked = per.sort_values("impressions", ascending=False)
    cre = ranked.groupby(idkey, dropna=False, observed=True).agg(
        creative=("creative", "first"),
        client=("client", "first") if "client" in per.columns else ("creative", "first"),
        product=("product", "first") if "product" in per.columns else ("creative", "first"),
        url=("url", "first"),                        # highest-delivery URL wins
        distinct_urls=("url", lambda s: int(len({v for v in s if v}))),
        campaigns=("campaign_id", "nunique") if "campaign_id" in per.columns else ("url", "size"),
        impressions=("impressions", "sum"), clicks=("clicks", "sum"),
        spend=("spend", "sum")).reset_index()
    cparsed = cre["url"].map(parse_utms)
    cre["utm_params"] = cparsed.map(lambda t: ", ".join(t[0]))
    cre["utm_count"] = cparsed.map(lambda t: len(t[0]))
    cre["other_tracking"] = cparsed.map(lambda t: ", ".join(t[1]))
    cre["no_url"] = cre["url"].eq("")
    cre["has_core"] = cparsed.map(lambda t: all(k in t[0] for k in
                                               ("utm_source", "utm_medium", "utm_campaign")))
    cre["status"] = np.where(cre["no_url"], "no clickthrough URL",
                     np.where(cre["utm_count"] == 0, "no UTM codes",
                       np.where(cre["has_core"], "fully tagged", "partially tagged")))
    cre["ctr"] = np.where(cre["impressions"] > 0, cre["clicks"] / cre["impressions"], 0.0)

    tot_i = float(cre["impressions"].sum()) or 1.0

    def _slice(mask):
        sub = cre[mask]
        return {"creatives": int(len(sub)),
                "impressions": int(sub["impressions"].sum()),
                "spend": float(sub["spend"].sum()),
                "impr_pct": float(sub["impressions"].sum() / tot_i)}
    summary = {
        "creatives": int(len(cre)),
        "placements": int(len(per)),
        "id_basis": "creative ID" if idkey == "creative_id" else "creative name",
        # tagged / untagged / no_url partition the library exactly
        "tagged": _slice(cre["utm_count"] > 0),
        "fully_tagged": _slice(cre["has_core"]),
        "partially_tagged": _slice((cre["utm_count"] > 0) & ~cre["has_core"]),
        "untagged": _slice((cre["utm_count"] == 0) & ~cre["no_url"]),
        "no_url": _slice(cre["no_url"]),
        "other_tracking_only": _slice((cre["utm_count"] == 0) & cre["other_tracking"].ne("")),
        "multi_url": _slice(cre["distinct_urls"] > 1),
    }
    # Which individual parameters are in use, by creative count
    param_rows = []
    for k in _UTM_KEYS:
        m = cre["utm_params"].str.contains(k, na=False)
        if m.any():
            param_rows.append({"parameter": k, "creatives": int(m.sum()),
                               "impressions": int(cre.loc[m, "impressions"].sum()),
                               "pct_of_creatives": float(m.sum() / max(len(cre), 1))})
    params = pd.DataFrame(param_rows)

    cre["_tagged"] = cre["utm_count"] > 0
    by_client = pd.DataFrame()
    if "client" in cre.columns:
        by_client = cre.groupby("client", dropna=False, observed=True).agg(
            creatives=("creative", "size"), tagged=("_tagged", "sum"),
            impressions=("impressions", "sum"), spend=("spend", "sum")).reset_index()
        ti = cre[cre["_tagged"]].groupby("client", dropna=False, observed=True)["impressions"].sum()
        by_client["tagged_impressions"] = by_client["client"].map(ti).fillna(0).astype("int64")
        by_client["untagged"] = by_client["creatives"] - by_client["tagged"]
        by_client["tagged_pct"] = by_client["tagged"] / by_client["creatives"].replace(0, np.nan)
        by_client["tagged_impr_pct"] = (by_client["tagged_impressions"] /
                                        by_client["impressions"].replace(0, np.nan)).fillna(0)
        by_client = by_client.sort_values(["untagged", "impressions"],
                                         ascending=False).reset_index(drop=True)

    keep = [c for c in ("client", "product", "creative", "creative_id", "campaigns",
                        "status", "utm_params", "other_tracking", "distinct_urls", "url",
                        "impressions", "clicks", "ctr", "spend") if c in cre.columns]
    untagged = cre[cre["utm_count"] == 0][keep].sort_values(
        "impressions", ascending=False).reset_index(drop=True)
    partial = cre[(cre["utm_count"] > 0) & ~cre["has_core"]][keep].sort_values(
        "impressions", ascending=False).reset_index(drop=True)
    multi_url = cre[cre["distinct_urls"] > 1][keep].sort_values(
        "impressions", ascending=False).reset_index(drop=True)
    return {"summary": summary, "per_creative": cre[keep], "by_placement": per,
            "params": params, "by_client": by_client, "untagged": untagged,
            "partial": partial, "multi_url": multi_url}


# ------------------------------------------------- size / type / completion
def _size_from_name(name):
    """First WxH token in a creative name — the fallback when Creative Size is
    blank, which it often is for social and video assets."""
    got = sizes_in(name)
    return got[0] if got else None


def _size_bucket(size):
    """Label a WxH size so a size table reads as media planning, not arithmetic."""
    if not size or "x" not in str(size):
        return "unknown"
    try:
        w, h = (int(x) for x in str(size).lower().split("x")[:2])
    except ValueError:
        return "unknown"
    if size in _DISPLAY_SIZES:
        return "IAB display"
    if size in _SOCIAL_SIZES:
        return "social native"
    ratio = w / h if h else 0
    if ratio >= 1.6:
        return "landscape/video"
    if ratio <= 0.7:
        return "vertical/story"
    return "square-ish"


def analyze_size_type(d, cols, ccol, bad_mask, norms):
    """Delivery and performance rolled up by creative size and by creative type,
    with video completion rate wherever the quartile fields are populated."""
    scol, tcol = cols.get("creative_size"), cols.get("creative_type")
    has_q = bool(cols.get("q100") or cols.get("q25"))
    if not (scol or tcol or has_q):
        return None
    named = d[~bad_mask].copy()
    if not len(named):
        return None

    # Size: the column when populated, else parsed out of the creative name.
    if scol and scol in named.columns:
        size = named[scol].astype(str).str.strip().str.lower().str.replace(" ", "", regex=False)
        blank = _is_blank(named[scol])
        parsed = named[ccol].map(_size_from_name)
        size = size.where(~blank, parsed)
        named["_size_source"] = np.where(blank, "parsed from name", "export column")
    else:
        size = named[ccol].map(_size_from_name)
        named["_size_source"] = "parsed from name"
    named["_size"] = size.fillna("(not specified)").replace({"": "(not specified)"})
    named["_size_bucket"] = named["_size"].map(_size_bucket)
    if tcol and tcol in named.columns:
        _t = named[tcol].astype("object").astype(str).str.strip()
        named["_type"] = _t.where(~_is_blank(named[tcol]), "(not specified)")
    else:
        named["_type"] = "(not specified)"

    q = {}
    for key in ("q25", "q50", "q75", "q100"):
        c = cols.get(key)
        q[key] = _num(named[c]) if c and c in named.columns else pd.Series(0.0, index=named.index)
        named[f"_{key}"] = q[key]

    def _roll(keys, label_map=None):
        g = named.groupby(keys, dropna=False, observed=True).agg(
            creatives=(ccol, "nunique"), campaigns=(cols["campaign_id"], "nunique")
            if cols["campaign_id"] else (ccol, "nunique"),
            impressions=("_impr", "sum"), clicks=("_clicks", "sum"),
            conversions=("_conv", "sum"), spend=("_spend", "sum"),
            q25=("_q25", "sum"), q50=("_q50", "sum"), q75=("_q75", "sum"),
            q100=("_q100", "sum")).reset_index()
        g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0.0)
        g["cpm"] = np.where(g["impressions"] > 0, g["spend"] / g["impressions"] * 1000, 0.0)
        g["conv_rate"] = np.where(g["impressions"] > 0, g["conversions"] / g["impressions"], 0.0)
        g["vcr"] = np.where(g["impressions"] > 0, g["q100"] / g["impressions"], 0.0)
        g["q25_rate"] = np.where(g["impressions"] > 0, g["q25"] / g["impressions"], 0.0)
        g["pct_of_impr"] = g["impressions"] / max(float(named["_impr"].sum()), 1.0)
        if label_map:
            g = g.rename(columns=label_map)
        return g.sort_values("impressions", ascending=False).reset_index(drop=True)

    by_size = _roll(["_size", "_size_bucket"], {"_size": "size", "_size_bucket": "family"})
    by_type = _roll(["_type"], {"_type": "type"})
    keys = ["_type", "_size"]
    if cols["product"]:
        keys = [cols["product"]] + keys
    by_type_size = _roll(keys, {cols["product"]: "product", "_type": "type", "_size": "size"}
                         if cols["product"] else {"_type": "type", "_size": "size"})

    # Size performance within each product, so a 300x250 on Display isn't judged
    # against a 1080x1920 on Social Mirror.
    by_product_size = pd.DataFrame()
    if cols["product"]:
        by_product_size = _roll([cols["product"], "_size"],
                                {cols["product"]: "product", "_size": "size"})
        by_product_size = by_product_size[by_product_size["impressions"] >= 5000]

    # Video completion, creative grain — the metric CTV/Video/Audio deserve,
    # since they're deliberately excluded from CTR judgements.
    completion = pd.DataFrame()
    if has_q and named["_q100"].sum() > 0:
        gk = [c for c in (cols["client"], cols["product"], cols["campaign_id"],
                          cols["creative_id"], ccol) if c]
        cg = named.groupby(gk, dropna=False, observed=True).agg(
            impressions=("_impr", "sum"), spend=("_spend", "sum"),
            q25=("_q25", "sum"), q50=("_q50", "sum"), q75=("_q75", "sum"),
            q100=("_q100", "sum")).reset_index()
        ren = {cols["client"]: "client", cols["product"]: "product",
               cols["campaign_id"]: "campaign_id", cols["creative_id"]: "creative_id",
               ccol: "creative"}
        cg = cg.rename(columns={k: v for k, v in ren.items() if k})
        for c in ("client", "product", "campaign_id", "creative_id", "creative"):
            if c in cg.columns and str(cg[c].dtype) == "category":
                cg[c] = cg[c].astype(str)
        for name, num in (("vcr", "q100"), ("q25_rate", "q25"),
                          ("q50_rate", "q50"), ("q75_rate", "q75")):
            cg[name] = np.where(cg["impressions"] > 0, cg[num] / cg["impressions"], 0.0)
        # Drop-off between first quartile and completion: high drop = wrong
        # audience or a weak first three seconds.
        cg["dropoff"] = np.where(cg["q25"] > 0, 1 - cg["q100"] / cg["q25"], 0.0)
        completion = cg[cg["q25"] > 0].sort_values("impressions",
                                                   ascending=False).reset_index(drop=True)

    low_vcr = pd.DataFrame()
    if len(completion):
        floor = _env_num("CREATIVE_VCR_MIN_IMPR", 5000, int)
        thresh = _env_num("CREATIVE_VCR_FLOOR", 0.50)
        low_vcr = completion[(completion["impressions"] >= floor) &
                             (completion["vcr"] < thresh)].sort_values(
            "spend", ascending=False).reset_index(drop=True)

    counts = {"sizes": int(by_size["size"].nunique()) if len(by_size) else 0,
              "types": int(by_type["type"].nunique()) if len(by_type) else 0,
              "size_from_name": int((named["_size_source"] == "parsed from name").sum()),
              "completion_creatives": int(len(completion)),
              "low_vcr": int(len(low_vcr))}
    return {"by_size": by_size, "by_type": by_type, "by_type_size": by_type_size,
            "by_product_size": by_product_size, "completion": completion,
            "low_vcr": low_vcr, "counts": counts,
            "vcr_floor": _env_num("CREATIVE_VCR_FLOOR", 0.50),
            "vcr_min_impr": _env_num("CREATIVE_VCR_MIN_IMPR", 5000, int)}


# ---------------------------------------------------------------- performance
def _creative_roster(d, cols, ccol):
    """Roll the export up to one row per creative x campaign — the grain every
    performance insight is computed on."""
    keys = [c for c in (cols["client"], cols["bu"], cols["product"], cols["campaign"],
                        cols["campaign_id"], cols["creative_id"], ccol) if c]
    agg = {"impressions": ("_impr", "sum"), "clicks": ("_clicks", "sum"),
           "spend": ("_spend", "sum"), "conversions": ("_conv", "sum"),
           "rows": ("_impr", "size")}
    g = d.groupby(keys, dropna=False, observed=True).agg(**agg).reset_index()
    if cols["date"]:
        dts = d.groupby(keys, dropna=False, observed=True)["_date"].agg(["min", "max", "nunique"]).reset_index()
        dts.columns = list(dts.columns[:-3]) + ["first_seen", "last_seen", "days"]
        g = g.merge(dts, on=keys, how="left")
        for c in ("first_seen", "last_seen"):
            g[c] = pd.to_datetime(g[c], errors="coerce").dt.date.astype(str)
    ren = {cols["client"]: "client", cols["bu"]: "business_unit",
           cols["product"]: "product", cols["campaign"]: "campaign",
           cols["campaign_id"]: "campaign_id", cols["creative_id"]: "creative_id",
           ccol: "creative"}
    g = g.rename(columns={k: v for k, v in ren.items() if k})
    # Categorical dimension columns (memory downcast on read) can't be compared
    # or mapped against floats downstream — plain strings from here on.
    for c in ("client", "business_unit", "product", "campaign", "campaign_id",
              "creative_id", "creative"):
        if c in g.columns and str(g[c].dtype) == "category":
            g[c] = g[c].astype(str)
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0.0)
    g["cpm"] = np.where(g["impressions"] > 0, g["spend"] / g["impressions"] * 1000, 0.0)
    g["cost_per_conv"] = np.where(g["conversions"] > 0, g["spend"] / g["conversions"], np.nan)
    return g


def _product_norms(roster):
    """Pooled CTR per product — the yardstick every CTR judgement uses, because
    a 1.5% CTR is alarming for CTV and unremarkable for display retargeting.
    Returns (all_norms, display_norms): the second drops the junk values that
    leak into Product 2 so the caption doesn't read like a campaign list."""
    if "product" not in roster.columns:
        return {}, []
    g = roster.groupby("product", observed=True).agg(i=("impressions", "sum"), c=("clicks", "sum"))
    norms = {p: (r.c / r.i if r.i else 0.0) for p, r in g.iterrows()}
    shown = [(p, norms[p], int(r.i)) for p, r in g.iterrows()
             if str(p).strip().lower() in _KNOWN_PRODUCTS]
    shown.sort(key=lambda t: t[2], reverse=True)
    return norms, [{"product": p, "ctr": c, "impressions": i} for p, c, i in shown[:12]]


def _clickable(roster):
    """Mask of rows where CTR is a meaningful signal."""
    if "product" not in roster.columns:
        return pd.Series(True, index=roster.index)
    return ~roster["product"].astype(str).str.strip().str.lower().isin(_NO_CLICK_PRODUCTS)


def analyze_performance(d, cols, ccol, bad_mask):
    """Creative-grain performance. `bad_mask` marks blank-name rows, which are
    excluded — an unnamed creative can't be judged or acted on."""
    min_impr = _env_num("CREATIVE_MIN_IMPR", 10000, int)
    noclick_impr = _env_num("CREATIVE_NOCLICK_MIN_IMPR", 5000, int)
    noconv_spend = _env_num("CREATIVE_NOCONV_MIN_SPEND", 50.0)
    fat_impr = _env_num("CREATIVE_FATIGUE_MIN_IMPR", 5000, int)
    fat_drop = _env_num("CREATIVE_FATIGUE_DROP", 0.40)
    single_impr = _env_num("CREATIVE_SINGLE_MIN_IMPR", 10000, int)

    named = d[~bad_mask]
    if not len(named):
        return None
    roster = _creative_roster(named, cols, ccol)
    norms, norms_display = _product_norms(roster)
    roster["product_ctr"] = (pd.to_numeric(roster["product"].map(norms), errors="coerce").fillna(0.0)
                             if "product" in roster else 0.0)
    roster["x_over_norm"] = np.where(roster["product_ctr"] > 0,
                                     roster["ctr"] / roster["product_ctr"], 0.0)
    clickable = _clickable(roster)
    big = roster[clickable & (roster["impressions"] >= min_impr)]

    winners = big[(big["x_over_norm"] >= 3) & (big["clicks"] > 0)].copy()
    if len(winners):
        winners["read"] = np.where(winners["x_over_norm"] >= 10,
                                   "verify — implausibly high, check for invalid traffic",
                                   "outperformer — worth scaling / reusing")
        winners = winners.sort_values("x_over_norm", ascending=False).reset_index(drop=True)

    laggards = big[(big["x_over_norm"] <= 1 / 3.0)].copy()
    laggards = laggards.sort_values("spend", ascending=False).reset_index(drop=True)

    no_clicks = roster[clickable & (roster["clicks"] == 0) &
                       (roster["impressions"] >= noclick_impr)].copy()
    no_clicks = no_clicks.sort_values("spend", ascending=False).reset_index(drop=True)

    no_conv = roster[(roster["conversions"] == 0) & (roster["spend"] >= noconv_spend)].copy()
    no_conv = no_conv.sort_values("spend", ascending=False).reset_index(drop=True)

    # ---- fatigue: first half vs second half of the delivery window ----------
    fatigue = pd.DataFrame()
    if cols["date"] and named["_date"].notna().any():
        dts = named["_date"]
        lo, hi = dts.min(), dts.max()
        if pd.notna(lo) and pd.notna(hi) and (hi - lo).days >= 3:
            mid = lo + (hi - lo) / 2
            keys = [c for c in ("client", "product", "campaign_id", "creative") if c in roster.columns]
            src = named.assign(_half=np.where(dts <= mid, "a", "b"))
            ren = {cols["client"]: "client", cols["product"]: "product",
                   cols["campaign_id"]: "campaign_id", ccol: "creative"}
            src = src.rename(columns={k: v for k, v in ren.items() if k})
            h = src.groupby(keys + ["_half"], dropna=False, observed=True).agg(
                i=("_impr", "sum"), c=("_clicks", "sum"), s=("_spend", "sum")).unstack("_half")
            h.columns = [f"{a}_{b}" for a, b in h.columns]
            for c in h.columns:
                h[c] = h[c].fillna(0)
            need = {"i_a", "i_b", "c_a", "c_b"}
            if need <= set(h.columns):
                h = h[(h["i_a"] >= fat_impr) & (h["i_b"] >= fat_impr)].reset_index()
                if len(h):
                    h["ctr_early"] = np.where(h["i_a"] > 0, h["c_a"] / h["i_a"], 0.0)
                    h["ctr_late"] = np.where(h["i_b"] > 0, h["c_b"] / h["i_b"], 0.0)
                    h["drop"] = np.where(h["ctr_early"] > 0,
                                         1 - h["ctr_late"] / h["ctr_early"], 0.0)
                    h["impressions"] = h["i_a"] + h["i_b"]
                    h["spend"] = h.get("s_a", 0) + h.get("s_b", 0)
                    fatigue = h[(h["ctr_early"] > 0) & (h["drop"] >= fat_drop)].copy()
                    if len(fatigue) and "product" in fatigue.columns:
                        keep = ~fatigue["product"].astype(str).str.strip().str.lower().isin(_NO_CLICK_PRODUCTS)
                        fatigue = fatigue[keep]
                    fatigue = fatigue.sort_values("impressions", ascending=False).reset_index(drop=True)
                    fatigue = fatigue[[c for c in ["client", "product", "campaign_id", "creative",
                                                   "impressions", "spend", "ctr_early", "ctr_late", "drop"]
                                       if c in fatigue.columns]]

    # ---- rotation: campaigns running a single creative, or one that dominates
    single_creative = pd.DataFrame()
    dominant = pd.DataFrame()
    if "campaign_id" in roster.columns:
        ckeys = [c for c in ("client", "product", "campaign", "campaign_id") if c in roster.columns]
        camp = roster.groupby(ckeys, dropna=False, observed=True).agg(
            creatives=("creative", "nunique"), impressions=("impressions", "sum"),
            clicks=("clicks", "sum"), spend=("spend", "sum")).reset_index()
        camp["ctr"] = np.where(camp["impressions"] > 0, camp["clicks"] / camp["impressions"], 0.0)
        single_creative = camp[(camp["creatives"] == 1) &
                               (camp["impressions"] >= single_impr)].sort_values(
            "impressions", ascending=False).reset_index(drop=True)
        multi = camp[camp["creatives"] >= 3][ckeys + ["impressions"]].rename(
            columns={"impressions": "campaign_impressions"})
        if len(multi):
            j = roster.merge(multi, on=ckeys, how="inner")
            j["share"] = np.where(j["campaign_impressions"] > 0,
                                  j["impressions"] / j["campaign_impressions"], 0.0)
            dominant = j[(j["share"] >= 0.8) &
                         (j["campaign_impressions"] >= single_impr)].sort_values(
                "campaign_impressions", ascending=False).reset_index(drop=True)

    # ---- one creative name showing up under several clients -----------------
    dupe_names = pd.DataFrame()
    if "client" in roster.columns:
        dn = roster.groupby("creative", observed=True).agg(
            clients=("client", "nunique"), campaigns=("campaign_id", "nunique"),
            impressions=("impressions", "sum"), spend=("spend", "sum")).reset_index()
        dupe_names = dn[dn["clients"] > 1].sort_values(
            ["clients", "impressions"], ascending=False).reset_index(drop=True)

    top = _rank(roster, "impressions", 100)
    counts = {"creatives": int(roster["creative"].nunique()),
              "creative_campaign_pairs": int(len(roster)),
              "winners": int(len(winners)), "laggards": int(len(laggards)),
              "no_clicks": int(len(no_clicks)), "no_clicks_spend": float(no_clicks["spend"].sum()) if len(no_clicks) else 0.0,
              "no_conv": int(len(no_conv)), "no_conv_spend": float(no_conv["spend"].sum()) if len(no_conv) else 0.0,
              "fatigue": int(len(fatigue)), "single_creative": int(len(single_creative)),
              "dominant": int(len(dominant)), "dupe_names": int(len(dupe_names))}
    return {"roster": roster, "top": top, "winners": winners, "laggards": laggards,
            "no_clicks": no_clicks, "no_conversions": no_conv, "fatigue": fatigue,
            "single_creative": single_creative, "dominant": dominant,
            "dupe_names": dupe_names, "norms": norms, "norms_display": norms_display,
            "counts": counts,
            "thresholds": {"min_impr": min_impr, "noclick_impr": noclick_impr,
                           "noconv_spend": noconv_spend, "fatigue_drop": fat_drop,
                           "fatigue_impr": fat_impr, "single_impr": single_impr}}


# ------------------------------------------------------------------ main audit
def audit_creatives(df, min_impressions=None):
    """Audit one creative-grain export. Returns a dict of summary + DataFrames,
    or None if the frame has no Creative Name column at all (wrong file type).

    min_impressions (env CREATIVE_ALERT_MIN_IMPR, default 1) is the delivery
    floor for calling a blank creative *alerting* — rows with zero delivery are
    still listed, just not counted as urgent."""
    if df is None or not len(df):
        return None
    ccol = _col(df, "creative")
    if ccol is None:
        return None
    if min_impressions is None:
        min_impressions = _env_num("CREATIVE_ALERT_MIN_IMPR", 1, int)

    cols = {k: _col(df, k) for k in _ALIASES}
    d = df.copy()
    impr = _num(d[cols["impressions"]]) if cols["impressions"] else pd.Series(0, index=d.index)
    clicks = _num(d[cols["clicks"]]) if cols["clicks"] else pd.Series(0, index=d.index)
    spend = _num(d[cols["spend"]]) if cols["spend"] else pd.Series(0.0, index=d.index)
    conv = _num(d[cols["conv"]]) if cols["conv"] else pd.Series(0, index=d.index)
    conv = conv + (_num(d[cols["vconv"]]) if cols["vconv"] else 0)
    d["_impr"], d["_clicks"], d["_spend"], d["_conv"] = impr, clicks, spend, conv
    for _k in ("q25", "q50", "q75", "q100"):
        _c = cols.get(_k)
        d[f"_{_k}"] = _num(d[_c]) if _c and _c in d.columns else 0.0
    d["_date"] = pd.to_datetime(d[cols["date"]], errors="coerce") if cols["date"] else pd.NaT

    reason = d[ccol].map(blank_reason)
    d["_reason"] = reason
    bad = reason.notna()

    wstart, wend = _window(d, cols["date"])
    summary = {
        "rows": int(len(d)),
        "impressions": int(impr.sum()),
        "spend": float(spend.sum()),
        "creatives": int(d.loc[~bad, ccol].nunique()),
        "window_start": wstart,
        "window_end": wend,
        "blank_rows": int(bad.sum()),
        "blank_empty": int((reason == "empty").sum()),
        "blank_placeholder": int((reason == "placeholder").sum()),
        "blank_impressions": int(impr[bad].sum()),
        "blank_spend": float(spend[bad].sum()),
        "blank_impr_pct": float(impr[bad].sum() / impr.sum()) if impr.sum() else 0.0,
        "blank_spend_pct": float(spend[bad].sum() / spend.sum()) if spend.sum() else 0.0,
        "has_creative_id": bool(cols["creative_id"]),
        "has_click_url": bool(cols.get("click_url")),
        "has_preview_url": bool(cols.get("preview_url")),
        "has_quartiles": bool(cols.get("q100") or cols.get("q25")),
        "has_creative_size": bool(cols.get("creative_size")),
        "has_creative_type": bool(cols.get("creative_type")),
    }

    # ---- roll the blanks up to the campaign the vendor has to fix -----------
    # Every identifier the export carries goes in: the creative ID when the file
    # has one, otherwise the campaign + pool IDs, which are what a blank creative
    # can actually be located by.
    gkeys = [cols[k] for k in ("bu", "client", "product", "campaign", "campaign_id",
                               "pool_id", "creative_id") if cols[k]]
    blank_campaigns = pd.DataFrame()
    if gkeys and bad.any():
        b = d[bad]
        agg = b.groupby(gkeys, dropna=False, observed=True).agg(
            blank_rows=("_impr", "size"),
            impressions=("_impr", "sum"),
            clicks=("_clicks", "sum"),
            spend=("_spend", "sum"),
        ).reset_index()
        tot = d.groupby(gkeys, dropna=False, observed=True).agg(
            total_rows=("_impr", "size")).reset_index()
        agg = agg.merge(tot, on=gkeys, how="left")
        agg["coverage"] = agg.apply(
            lambda r: "every row" if r["blank_rows"] >= r["total_rows"] else
                      f"{int(r['blank_rows'])} of {int(r['total_rows'])} rows", axis=1)
        rmix = b.groupby(gkeys, dropna=False, observed=True)["_reason"].agg(
            lambda s: ", ".join(sorted(set(s)))).reset_index().rename(columns={"_reason": "issue"})
        agg = agg.merge(rmix, on=gkeys, how="left")
        if cols["date"]:
            dr = b.groupby(gkeys, dropna=False, observed=True)["_date"].agg(["min", "max"]).reset_index()
            dr["first_seen"] = pd.to_datetime(dr["min"], errors="coerce").dt.date.astype(str)
            dr["last_seen"] = pd.to_datetime(dr["max"], errors="coerce").dt.date.astype(str)
            agg = agg.merge(dr.drop(columns=["min", "max"]), on=gkeys, how="left")
        ren = {cols["bu"]: "business_unit", cols["client"]: "client",
               cols["product"]: "product", cols["campaign"]: "campaign",
               cols["campaign_id"]: "campaign_id", cols["pool_id"]: "campaign_pool_id",
               cols["creative_id"]: "creative_id"}
        agg = agg.rename(columns={k: v for k, v in ren.items() if k})
        agg = agg.sort_values(["impressions", "blank_rows"], ascending=False).reset_index(drop=True)
        order = [c for c in ["client", "business_unit", "product", "campaign", "campaign_id",
                             "campaign_pool_id", "creative_id", "issue", "coverage",
                             "blank_rows", "impressions", "clicks", "spend",
                             "first_seen", "last_seen"] if c in agg.columns]
        blank_campaigns = agg[order]

    delivering = blank_campaigns[blank_campaigns["impressions"] >= max(min_impressions, 1)] \
        if len(blank_campaigns) else blank_campaigns
    summary["blank_campaigns"] = int(len(blank_campaigns))
    summary["blank_campaigns_delivering"] = int(len(delivering))
    summary["blank_clients"] = int(blank_campaigns["client"].nunique()) if len(blank_campaigns) and "client" in blank_campaigns else 0
    summary["blank_campaign_ids"] = ([str(x) for x in blank_campaigns["campaign_id"].tolist()]
                                     if len(blank_campaigns) and "campaign_id" in blank_campaigns else [])

    keep_raw = [c for c in [cols["date"], cols["bu"], cols["client"], cols["product"],
                            cols["strategy_type"], cols["strategy_name"], cols["campaign"],
                            cols["campaign_id"], cols["pool_id"], cols["creative_id"], ccol,
                            cols["impressions"], cols["clicks"], cols["spend"]] if c]
    blank_rows = d.loc[bad, keep_raw].copy() if bad.any() else pd.DataFrame(columns=keep_raw)
    if len(blank_rows):
        blank_rows.insert(len(blank_rows.columns), "issue", d.loc[bad, "_reason"].values)
        blank_rows = blank_rows.sort_values(cols["impressions"] or keep_raw[0], ascending=False)

    by_client = pd.DataFrame()
    if len(blank_campaigns) and "client" in blank_campaigns.columns:
        by_client = blank_campaigns.groupby("client", dropna=False, observed=True).agg(
            campaigns=("campaign_id", "nunique") if "campaign_id" in blank_campaigns else ("client", "size"),
            blank_rows=("blank_rows", "sum"),
            impressions=("impressions", "sum"),
            spend=("spend", "sum"),
        ).reset_index().sort_values("impressions", ascending=False).reset_index(drop=True)

    # ---- checks --------------------------------------------------------------
    checks, tables = [], {}

    def _add(key, label, severity, mask, note="", extra_cols=()):
        n = int(mask.sum())
        checks.append({"key": key, "label": label, "severity": severity, "count": n,
                       "impressions": int(impr[mask].sum()) if n else 0,
                       "spend": float(spend[mask].sum()) if n else 0.0,
                       "note": note})
        if n:
            want = [c for c in list(keep_raw) + list(extra_cols) if c in d.columns]
            out = d.loc[mask, want].copy()
            tables[key] = out.sort_values(cols["impressions"], ascending=False) \
                if cols["impressions"] in out.columns else out

    _add("blank_creative", "Blank creative name", "critical", bad,
         note="creative name missing or a placeholder — vendor data error")

    # -- trafficking: Social Mirror carrying a display-banner size ------------
    name_s = d[ccol].astype(str)
    prod_l = d[cols["product"]].astype(str).str.strip().str.lower() if cols["product"] else pd.Series("", index=d.index)
    is_sm = prod_l.eq("social mirror")
    all_sizes = name_s.map(sizes_in)
    d["_sizes"] = all_sizes.map(lambda l: ", ".join(dict.fromkeys(l)))
    has_display_size = all_sizes.map(lambda l: any(s in _DISPLAY_SIZES for s in l))
    has_any_size = all_sizes.map(bool)
    has_social_size = has_any_size & ~has_display_size

    _add("sm_display_size", "Social Mirror creative named with a display banner size",
         "critical", is_sm & has_display_size & ~bad,
         note="a display asset (300x250, 728x90…) appears to be running on a Social Mirror line — "
              "verify what actually served", extra_cols=("_sizes",))
    _add("sm_social_size", "Social Mirror creative with a size in its name",
         "info", is_sm & has_social_size & ~bad,
         note="social-native size (1080x1080, 1200x628…) — naming noise, not a trafficking error",
         extra_cols=("_sizes",))

    # -- trafficking: asset format doesn't match the product ------------------
    is_video_prod = prod_l.isin(_VIDEO_PRODUCTS)
    is_disp_prod = prod_l.isin(_DISPLAY_PRODUCTS)
    looks_video = name_s.str.contains(_VIDEO_EXT, na=False)
    looks_image = name_s.str.contains(_IMAGE_EXT, na=False)
    _add("format_mismatch", "Creative format doesn't match the product", "warn",
         (~bad) & ((is_video_prod & looks_image) | (is_disp_prod & looks_video)),
         note="a still image on a video/CTV product, or a video file on a display product")

    # -- creative asset completeness ------------------------------------------
    # A creative with no preview image can't be eyeballed in a QA pass or shown
    # to a client, so it's an asset-completeness gap rather than a metrics one.
    if cols.get("preview_url"):
        _add("missing_preview", "Missing preview image URL", "warn",
             (~bad) & _is_blank(d[cols["preview_url"]]),
             note="no preview image on the creative — can't be visually QA'd or shown to the client",
             extra_cols=(cols["preview_url"],))
    if cols.get("click_url"):
        _add("missing_click_url", "Missing clickthrough URL", "warn",
             (~bad) & _is_blank(d[cols["click_url"]]),
             note="no landing page on the creative — clicks have nowhere to go",
             extra_cols=(cols["click_url"],))
        _add("no_utm", "Clickthrough URL with no UTM codes", "info",
             (~bad) & ~_is_blank(d[cols["click_url"]]) &
             ~d[cols["click_url"]].astype(str).str.lower().str.contains("utm_", na=False),
             note="traffic from this creative lands unattributed in Google Analytics",
             extra_cols=(cols["click_url"],))

    # -- creative identity ----------------------------------------------------
    # Now that the export carries creative_id, the ID and the name should agree.
    if cols.get("creative_id"):
        cid = d[cols["creative_id"]].astype(str)
        _add("missing_creative_id", "Missing creative ID", "warn",
             (~bad) & _is_blank(d[cols["creative_id"]]),
             note="named creative with no ID — can't be matched back to the DSP")
        pair = d.loc[~bad, [cols["creative_id"], ccol]].astype(str)
        multi_name = pair.groupby(cols["creative_id"], observed=True)[ccol].transform("nunique") > 1
        mask_mn = pd.Series(False, index=d.index)
        mask_mn.loc[pair.index] = multi_name.values
        _add("id_name_conflict", "One creative ID, several names", "warn",
             mask_mn & ~bad,
             note="the same creative ID appears under different names — renamed mid-flight, or an ID collision")

    # -- data integrity --------------------------------------------------------
    if cols["product"]:
        unmapped = ~prod_l.isin(_KNOWN_PRODUCTS) & prod_l.ne("") & prod_l.ne("nan")
        _add("unmapped_product", "Unrecognized product value", "warn", unmapped,
             note="Product 2 holds something that isn't a Vici product (campaign name leaked in)")
    if cols["bu"]:
        bu = d[cols["bu"]]
        _add("missing_bu", "Missing business unit", "info",
             bu.isna() | bu.astype(str).str.strip().isin(["", "nan", "None"]),
             note="row can't be attributed to a partner")
    _add("impossible_metrics", "Impossible metrics", "warn",
         (clicks > impr) | (impr < 0) | (clicks < 0) | (spend < 0),
         note="clicks exceed impressions, or a negative count")
    _add("delivery_no_spend", "Delivery with zero spend", "info",
         (impr > 0) & (spend <= 0),
         note="impressions served but billable spend is 0 — check billing feed")

    # Trafficking flags are worth seeing per CREATIVE, not per row — 1,900 daily
    # rows collapse to the handful of assets somebody actually has to re-traffic.
    grouped = {}
    for key in ("sm_display_size", "sm_social_size", "format_mismatch"):
        tbl = tables.get(key)
        if tbl is None or not len(tbl):
            continue
        gk = [c for c in (cols["client"], cols["product"], cols["campaign"],
                          cols["campaign_id"], cols["creative_id"], ccol, "_sizes")
              if c and c in tbl.columns]
        gg = tbl.groupby(gk, dropna=False, observed=True).agg(
            days=(cols["impressions"], "size"),
            impressions=(cols["impressions"], "sum"),
            clicks=(cols["clicks"], "sum") if cols["clicks"] else (cols["impressions"], "size"),
            spend=(cols["spend"], "sum") if cols["spend"] else (cols["impressions"], "size"),
        ).reset_index()
        ren = {cols["client"]: "client", cols["product"]: "product",
               cols["campaign"]: "campaign", cols["campaign_id"]: "campaign_id",
               cols["creative_id"]: "creative_id", ccol: "creative", "_sizes": "sizes"}
        gg = gg.rename(columns={k: v for k, v in ren.items() if k})
        grouped[key] = gg.sort_values("impressions", ascending=False).reset_index(drop=True)
        for ck in checks:
            if ck["key"] == key:
                ck["creatives"] = int(gg["creative"].nunique()) if "creative" in gg else 0

    summary["sm_display_size_creatives"] = int(
        grouped.get("sm_display_size", pd.DataFrame()).get("creative", pd.Series(dtype=str)).nunique())
    summary["issues"] = int(sum(c["count"] for c in checks))
    summary["critical_issues"] = int(sum(c["count"] for c in checks if c["severity"] == "critical"))
    summary["status"] = ("fail" if summary["blank_campaigns_delivering"] or
                         any(c["count"] and c["severity"] == "critical" for c in checks)
                         else "warn" if summary["blank_rows"] or
                         any(c["count"] and c["severity"] == "warn" for c in checks)
                         else "clean")

    performance = analyze_performance(d, cols, ccol, bad)
    if performance:
        summary.update({f"perf_{k}": v for k, v in performance["counts"].items()})

    utms = analyze_utms(d, cols, ccol, bad)
    if utms:
        u = utms["summary"]
        summary.update({
            "utm_creatives": u["creatives"],
            "utm_tagged": u["tagged"]["creatives"],
            "utm_tagged_pct": (u["tagged"]["creatives"] / u["creatives"]) if u["creatives"] else 0.0,
            "utm_fully_tagged": u["fully_tagged"]["creatives"],
            "utm_partial": u["partially_tagged"]["creatives"],
            "utm_untagged": u["untagged"]["creatives"],
            "utm_no_url": u["no_url"]["creatives"],
            "utm_tagged_impr_pct": u["tagged"]["impr_pct"],
        })

    sizetype = analyze_size_type(d, cols, ccol, bad,
                                (performance or {}).get("norms", {}))
    if sizetype:
        summary.update({f"st_{k}": v for k, v in sizetype["counts"].items()})

    # Preview-image gap, rolled up to the creative — this is the export the
    # design team actually works from.
    missing_preview = pd.DataFrame()
    if cols.get("preview_url") and "missing_preview" in tables:
        mp = tables["missing_preview"]
        gk = [c for c in (cols["client"], cols["product"], cols["campaign"],
                          cols["campaign_id"], cols["creative_id"], ccol)
              if c and c in mp.columns]
        if gk:
            missing_preview = mp.groupby(gk, dropna=False, observed=True).agg(
                days=(cols["impressions"], "size"),
                impressions=(cols["impressions"], "sum"),
                clicks=(cols["clicks"], "sum") if cols["clicks"] else (cols["impressions"], "size"),
                spend=(cols["spend"], "sum") if cols["spend"] else (cols["impressions"], "size"),
            ).reset_index()
            ren = {cols["client"]: "client", cols["product"]: "product",
                   cols["campaign"]: "campaign", cols["campaign_id"]: "campaign_id",
                   cols["creative_id"]: "creative_id", ccol: "creative"}
            missing_preview = missing_preview.rename(
                columns={k: v for k, v in ren.items() if k}).sort_values(
                "impressions", ascending=False).reset_index(drop=True)
    summary["missing_preview_creatives"] = int(
        missing_preview["creative"].nunique()) if len(missing_preview) and "creative" in missing_preview else 0

    return {"summary": summary, "checks": checks, "tables": tables,
            "grouped": grouped,
            "blank_campaigns": blank_campaigns, "blank_rows": blank_rows,
            "by_client": by_client, "performance": performance,
            "utms": utms, "sizetype": sizetype, "missing_preview": missing_preview}


def alert_subject(summary, label=""):
    """Subject line for the vendor-error email."""
    s = summary or {}
    tail = f" — {label}" if label else ""
    if s.get("blank_campaigns_delivering"):
        return (f"⚠ Creative QA: {s['blank_campaigns_delivering']} campaign(s) delivering with "
                f"NO creative name{tail}")
    if s.get("blank_rows"):
        return f"Creative QA: {s['blank_rows']} blank creative row(s){tail}"
    if s.get("critical_issues"):
        return f"⚠ Creative QA: {s['critical_issues']} trafficking issue(s){tail}"
    return f"Creative QA: clean{tail}"

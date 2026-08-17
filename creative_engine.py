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

    return {"summary": summary, "checks": checks, "tables": tables,
            "grouped": grouped,
            "blank_campaigns": blank_campaigns, "blank_rows": blank_rows,
            "by_client": by_client, "performance": performance}


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

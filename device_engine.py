"""
device_engine.py
Device-grain delivery (S3 prefix 'device-insights'): one row per date x campaign
x device. Two jobs.

1. INSIGHT — where delivery actually lands
   Device mix by impressions and spend, CTR / conversion rate / CPM per device,
   device mix within each product (a CTV line delivering on desktop is a
   trafficking problem, not a preference), device mix per client, and the
   clients whose mix has drifted furthest from the book-wide norm.

2. RECONCILIATION — the tripwire for the Jul 2026 dispute
   In July, device-grain impressions in the warehouse read ~33% higher than the
   campaign totals for several advertisers (Babb and Masters, Fresh Start
   Cleaning, Pit River, Gym Guys), while the vendor's own S3 files reconciled
   exactly. Nobody noticed for weeks because nothing compared the two grains.

   Every grain of the same delivery must total the same. This engine therefore:
     - de-dupes pooled rolling-window files and REPORTS what it removed
       (see tap_adapter.combine_devices — the rolling LAST-7-DAYS drop means one
       delivery date appears in up to seven files, and summing them blindly
       multiplies it),
     - cross-checks device totals against a reference grain (creative or
       site/app) per client and per campaign, and
     - flags any client whose two grains disagree by more than a tolerance,
       with the direction of the error, so an inflation shows up the same day.

   A double-count inflates impressions AND clicks by the same factor, which is
   the fingerprint to look for; a genuinely missing file shows up as a shortfall
   on specific dates. Both are reported separately.
"""
import os
import re

import numpy as np
import pandas as pd

_ALIASES = {
    "device": ["Device Type", "device_type", "Device", "device"],
    "os": ["Operating System", "operating_system", "os"],
    "browser": ["Browser", "browser"],
    "make": ["Device Make", "device_make"],
    "environment": ["Environment", "environment"],
    "date": ["Date", "date"],
    "bu": ["Client Business Unit", "Business Unit", "client_business_unit"],
    "client": ["Client", "client"],
    "product": ["Product 2", "Product", "product_2"],
    "strategy_type": ["Strategy Type", "strategy_type"],
    "campaign": ["Campaign Pool Name", "campaign_pool_name", "Campaign Name"],
    "campaign_id": ["Campaign ID", "campaign_id"],
    "pool_id": ["Campaign Pool ID", "campaign_pool_id"],
    "impressions": ["Impressions", "impressions"],
    "clicks": ["Clicks", "clicks"],
    "conv": ["Post Click Conversions", "post_click_conversions"],
    "vconv": ["Post View Conversions", "post_view_conversions"],
    "spend": ["Billable Spend", "billable_spend", "Internal Cost", "Total Spend"],
}

# Products that should be delivering to a TV screen, not a browser. A meaningful
# share of desktop/mobile delivery on these is a trafficking question.
_CTV_PRODUCTS = {"ctv", "social mirror ctv", "connected tv", "ctv + video"}
_TV_DEVICES = {"ctv", "connected tv", "tv", "smart tv", "set top box", "settopbox",
               "connectedtv", "streaming device"}


def _env_num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _col(df, key):
    for c in _ALIASES.get(key, []):
        if c in df.columns:
            return c
    return None


def _num(s):
    return pd.to_numeric(s, errors="coerce").fillna(0)


def _destr(df, cols):
    """Category columns break comparisons and merges downstream — plain strings."""
    for c in cols:
        if c in df.columns and str(df[c].dtype) == "category":
            df[c] = df[c].astype(str)
    return df


def _norm_device(v):
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none", "") else "(not specified)"


# ----------------------------------------------------------- reconciliation
def reconcile(device_df, reference_df, ref_label="creative export",
              tolerance=None, min_impressions=None):
    """Compare device-grain totals against another grain of the SAME delivery.

    Both frames must carry Client / Campaign ID / Date / Impressions. Returns a
    per-client and per-campaign comparison plus a summary. Any difference beyond
    `tolerance` (default 0.5%, env DEVICE_RECON_TOLERANCE) is a finding: the two
    grains describe identical delivery, so they must agree.
    """
    if device_df is None or not len(device_df) or reference_df is None or not len(reference_df):
        return None
    if tolerance is None:
        tolerance = _env_num("DEVICE_RECON_TOLERANCE", 0.005)
    if min_impressions is None:
        min_impressions = _env_num("DEVICE_RECON_MIN_IMPR", 1000, int)

    dc, rc = {k: _col(device_df, k) for k in _ALIASES}, {k: _col(reference_df, k) for k in _ALIASES}
    if not (dc["client"] and rc["client"]):
        return None

    def _norm_name(s):
        """Join key for advertiser/client names. AdLib's device report strips
        commas that its campaign report keeps ('Artman Equipment Inc.' vs
        'Artman Equipment, Inc.'), which affected 22 advertisers in the Jul 2026
        check — enough to overstate the gross error 8x. Compare on letters and
        digits only so a punctuation difference is never read as a data fault."""
        return re.sub(r"[^a-z0-9]", "", str(s).lower())

    def _roll(df, cols, keys):
        d = df.copy()
        d["_impr"] = _num(d[cols["impressions"]]) if cols["impressions"] else 0
        d["_clicks"] = _num(d[cols["clicks"]]) if cols["clicks"] else 0
        d["_spend"] = _num(d[cols["spend"]]) if cols["spend"] else 0.0
        gk = [cols[k] for k in keys if cols.get(k)]
        if not gk:
            return pd.DataFrame()
        g = d.groupby(gk, dropna=False, observed=True).agg(
            impressions=("_impr", "sum"), clicks=("_clicks", "sum"),
            spend=("_spend", "sum")).reset_index()
        g.columns = [{cols[k]: k for k in keys if cols.get(k)}.get(c, c) for c in g.columns]
        g = _destr(g, ["client", "campaign_id", "date"])
        if "client" in g.columns:
            g["client_key"] = g["client"].map(_norm_name)
        return g

    out = {"tolerance": tolerance, "reference": ref_label}
    for scope, keys in (("by_client", ["client"]), ("by_campaign", ["client", "campaign_id"])):
        dev, ref = _roll(device_df, dc, keys), _roll(reference_df, rc, keys)
        if not len(dev) or not len(ref):
            continue
        on = [("client_key" if k == "client" else k) for k in keys
              if (("client_key" if k == "client" else k) in dev.columns
                  and ("client_key" if k == "client" else k) in ref.columns)]
        if "client_key" in on:
            dev = dev.drop(columns=["client"], errors="ignore")
            ref = ref.rename(columns={"client": "client"})
        m = dev.merge(ref, on=on, how="outer", suffixes=("_device", "_reference")).fillna(
            {"impressions_device": 0, "impressions_reference": 0,
             "clicks_device": 0, "clicks_reference": 0,
             "spend_device": 0.0, "spend_reference": 0.0})
        m["impr_diff"] = m["impressions_device"] - m["impressions_reference"]
        m["impr_pct"] = np.where(m["impressions_reference"] > 0,
                                 m["impr_diff"] / m["impressions_reference"], np.nan)
        m["click_diff"] = m["clicks_device"] - m["clicks_reference"]
        m["click_pct"] = np.where(m["clicks_reference"] > 0,
                                  m["click_diff"] / m["clicks_reference"], np.nan)
        # A duplicated row inflates impressions and clicks by the SAME factor —
        # that similarity is what separates a double-count from a real gap.
        m["same_factor"] = (m["impr_pct"].notna() & m["click_pct"].notna() &
                            (m["impr_pct"] > tolerance) &
                            ((m["impr_pct"] - m["click_pct"]).abs() <= 0.10))
        m["verdict"] = np.where(m["impr_pct"].abs() <= tolerance, "matches",
                        np.where(m["impr_pct"] > 0,
                                 np.where(m["same_factor"],
                                          "device HIGHER — looks like duplicated rows",
                                          "device HIGHER"),
                                 "device LOWER — missing from the device export"))
        m = m[(m["impressions_reference"] >= min_impressions) |
              (m["impressions_device"] >= min_impressions)]
        out[scope] = m.sort_values("impr_diff", key=lambda s: s.abs(),
                                   ascending=False).reset_index(drop=True)

    bc = out.get("by_client")
    if bc is None or not len(bc):
        return None
    mism = bc[bc["verdict"] != "matches"]
    dev_t, ref_t = float(bc["impressions_device"].sum()), float(bc["impressions_reference"].sum())
    out["summary"] = {
        "clients": int(len(bc)),
        "clients_matching": int((bc["verdict"] == "matches").sum()),
        "clients_mismatched": int(len(mism)),
        "clients_device_higher": int((mism["impr_diff"] > 0).sum()),
        "clients_device_lower": int((mism["impr_diff"] < 0).sum()),
        "clients_duplication_shape": int(mism["same_factor"].sum()),
        "device_impressions": int(dev_t),
        "reference_impressions": int(ref_t),
        # The net is what a vendor-style month-total comparison would show; the
        # gross is the real error. In July the two differed by 200x, which is
        # exactly how a genuine per-advertiser problem hides inside a clean total.
        "net_diff": int(dev_t - ref_t),
        "net_pct": float((dev_t - ref_t) / ref_t) if ref_t else 0.0,
        "gross_diff": int(bc["impr_diff"].abs().sum()),
        "gross_pct": float(bc["impr_diff"].abs().sum() / ref_t) if ref_t else 0.0,
        "worst_client": (mism.iloc[0]["client"] if len(mism) else None),
        "worst_pct": (float(mism.iloc[0]["impr_pct"]) if len(mism) and pd.notna(mism.iloc[0]["impr_pct"]) else None),
    }
    return out


# ------------------------------------------------------------------ analysis
def analyze_devices(df, reference_df=None, ref_label="creative export", dedupe_stats=None):
    """Full device analysis. `reference_df` (the creative or site/app frame for the
    same window) enables the cross-grain reconciliation."""
    if df is None or not len(df):
        return None
    dcol = _col(df, "device")
    if dcol is None:
        return None

    cols = {k: _col(df, k) for k in _ALIASES}
    d = df.copy()
    d["_impr"] = _num(d[cols["impressions"]]) if cols["impressions"] else 0
    d["_clicks"] = _num(d[cols["clicks"]]) if cols["clicks"] else 0
    d["_spend"] = _num(d[cols["spend"]]) if cols["spend"] else 0.0
    conv = _num(d[cols["conv"]]) if cols["conv"] else 0
    d["_conv"] = conv + (_num(d[cols["vconv"]]) if cols["vconv"] else 0)
    d["_device"] = d[dcol].map(_norm_device)
    d = _destr(d, [c for c in (cols["client"], cols["product"], cols["campaign_id"],
                               cols["bu"]) if c])

    tot_i = float(d["_impr"].sum()) or 1.0
    dates = pd.to_datetime(d[cols["date"]], errors="coerce") if cols["date"] else pd.Series(pd.NaT, index=d.index)

    def _mix(keys, rename=None):
        g = d.groupby(keys, dropna=False, observed=True).agg(
            impressions=("_impr", "sum"), clicks=("_clicks", "sum"),
            conversions=("_conv", "sum"), spend=("_spend", "sum"),
            campaigns=(cols["campaign_id"], "nunique") if cols["campaign_id"] else ("_impr", "size"),
        ).reset_index()
        g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0.0)
        g["cpm"] = np.where(g["impressions"] > 0, g["spend"] / g["impressions"] * 1000, 0.0)
        g["conv_rate"] = np.where(g["impressions"] > 0, g["conversions"] / g["impressions"], 0.0)
        g["share"] = g["impressions"] / tot_i
        if rename:
            g = g.rename(columns=rename)
        return g.sort_values("impressions", ascending=False).reset_index(drop=True)

    by_device = _mix(["_device"], {"_device": "device"})
    by_product_device = pd.DataFrame()
    if cols["product"]:
        by_product_device = _mix([cols["product"], "_device"],
                                 {cols["product"]: "product", "_device": "device"})
        # share within the product, not of the whole book
        tot_by_p = by_product_device.groupby("product")["impressions"].transform("sum")
        by_product_device["share"] = by_product_device["impressions"] / tot_by_p.replace(0, np.nan)
    by_client_device = pd.DataFrame()
    if cols["client"]:
        by_client_device = _mix([cols["client"], "_device"],
                                {cols["client"]: "client", "_device": "device"})
        tot_by_c = by_client_device.groupby("client")["impressions"].transform("sum")
        by_client_device["share"] = by_client_device["impressions"] / tot_by_c.replace(0, np.nan)

    # CTV/Video products delivering somewhere that isn't a TV
    ctv_off_target = pd.DataFrame()
    if cols["product"] and len(by_product_device):
        p = by_product_device["product"].astype(str).str.strip().str.lower()
        dv = by_product_device["device"].astype(str).str.strip().str.lower()
        off = by_product_device[p.isin(_CTV_PRODUCTS) & ~dv.isin(_TV_DEVICES) &
                                (by_product_device["impressions"] > 0)]
        ctv_off_target = off.sort_values("impressions", ascending=False).reset_index(drop=True)

    # daily totals — a missing vendor file shows up as a hole, a double-count as a spike
    by_date = pd.DataFrame()
    if cols["date"] and dates.notna().any():
        bd = d.assign(_d=dates.dt.date).groupby("_d", dropna=False).agg(
            impressions=("_impr", "sum"), clicks=("_clicks", "sum"),
            spend=("_spend", "sum"), rows=("_impr", "size")).reset_index()
        bd = bd.rename(columns={"_d": "date"}).sort_values("date").reset_index(drop=True)
        med = bd["impressions"].median() or 1
        bd["vs_median"] = bd["impressions"] / med
        bd["flag"] = np.where(bd["vs_median"] >= 1.5, "spike — check for duplicate files",
                       np.where(bd["vs_median"] <= 0.5, "dip — check for a missing file", ""))
        by_date = bd

    summary = {
        "rows": int(len(d)),
        "impressions": int(d["_impr"].sum()),
        "clicks": int(d["_clicks"].sum()),
        "spend": float(d["_spend"].sum()),
        "devices": int(by_device["device"].nunique()) if len(by_device) else 0,
        "clients": int(d[cols["client"]].nunique()) if cols["client"] else 0,
        "window_start": dates.min().date().isoformat() if dates.notna().any() else None,
        "window_end": dates.max().date().isoformat() if dates.notna().any() else None,
        "top_device": (by_device.iloc[0]["device"] if len(by_device) else None),
        "top_device_share": (float(by_device.iloc[0]["share"]) if len(by_device) else 0.0),
        "ctv_off_target_impressions": int(ctv_off_target["impressions"].sum()) if len(ctv_off_target) else 0,
        "date_flags": int((by_date["flag"] != "").sum()) if len(by_date) else 0,
    }
    if dedupe_stats:
        summary["dedupe"] = dedupe_stats

    recon = reconcile(df, reference_df, ref_label) if reference_df is not None else None
    if recon:
        summary["recon"] = recon["summary"]

    return {"summary": summary, "by_device": by_device,
            "by_product_device": by_product_device, "by_client_device": by_client_device,
            "ctv_off_target": ctv_off_target, "by_date": by_date,
            "reconciliation": recon}

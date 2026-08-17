"""
creative_engine.py
Answers: "did the vendor's creative export come through clean, or is it missing
the fields we report on?"

The TapClicks *creative-insights* export is creative-grain delivery (one row per
date x campaign x creative). Recently it has been arriving with the Creative
Name column completely blank on some campaigns — those rows still carry
impressions and spend, so the delivery is real but unattributable to a creative.
This engine finds those rows fast and rolls them up to the campaign level so the
vendor can be handed an exact list.

Primary check
  blank_creative   Creative Name empty, or a placeholder standing in for a real
                   name ('n/a', 'null', 'untitled', '0', ...). Split by whether
                   the row actually delivered.

Secondary integrity checks (same export, cheap to run, same "is this data
trustworthy" question)
  unmapped_product     Product 2 isn't a known Vici product — usually a campaign
                       name leaking into the product column
  missing_bu           Client Business Unit blank
  impossible_metrics   clicks > impressions, or negative counts
  delivery_no_spend    impressions > 0 but billable spend == 0

Everything returns plain DataFrames so app.py can format/download them the same
way it does the placement tables.
"""
import os
import re

import pandas as pd

# --- column aliases -------------------------------------------------------
# The engine works on the Title Case names tap_adapter normalizes to, but stays
# tolerant of a raw export being handed straight in.
_ALIASES = {
    "creative": ["Creative Name", "creative_name", "Creative", "creative"],
    "creative_id": ["Creative ID", "creative_id"],
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


def _window(df, dcol):
    if not dcol:
        return None, None
    d = pd.to_datetime(df[dcol], errors="coerce").dropna()
    if not len(d):
        return None, None
    return d.min().date().isoformat(), d.max().date().isoformat()


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
        min_impressions = int(os.environ.get("CREATIVE_ALERT_MIN_IMPR", "1"))

    cols = {k: _col(df, k) for k in _ALIASES}
    d = df.copy()
    impr = _num(d[cols["impressions"]]) if cols["impressions"] else pd.Series(0, index=d.index)
    clicks = _num(d[cols["clicks"]]) if cols["clicks"] else pd.Series(0, index=d.index)
    spend = _num(d[cols["spend"]]) if cols["spend"] else pd.Series(0.0, index=d.index)
    d["_impr"], d["_clicks"], d["_spend"] = impr, clicks, spend

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
    }

    # ---- roll the blanks up to the campaign the vendor has to fix -----------
    gkeys = [cols[k] for k in ("bu", "client", "product", "campaign", "campaign_id")
             if cols[k]]
    blank_campaigns = pd.DataFrame()
    if gkeys and bad.any():
        b = d[bad]
        agg = b.groupby(gkeys, dropna=False, observed=True).agg(
            blank_rows=("_impr", "size"),
            impressions=("_impr", "sum"),
            clicks=("_clicks", "sum"),
            spend=("_spend", "sum"),
        ).reset_index()
        # how much of the campaign is affected — all rows, or only some?
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
            dt = pd.to_datetime(b[cols["date"]], errors="coerce")
            dr = b.assign(_d=dt).groupby(gkeys, dropna=False, observed=True)["_d"].agg(["min", "max"]).reset_index()
            dr["first_seen"] = dr["min"].dt.date.astype(str)
            dr["last_seen"] = dr["max"].dt.date.astype(str)
            agg = agg.merge(dr.drop(columns=["min", "max"]), on=gkeys, how="left")
        ren = {cols["bu"]: "business_unit", cols["client"]: "client",
               cols["product"]: "product", cols["campaign"]: "campaign",
               cols["campaign_id"]: "campaign_id"}
        agg = agg.rename(columns={k: v for k, v in ren.items() if k})
        agg = agg.sort_values(["impressions", "blank_rows"], ascending=False).reset_index(drop=True)
        order = [c for c in ["client", "business_unit", "product", "campaign", "campaign_id",
                             "issue", "coverage", "blank_rows", "impressions", "clicks",
                             "spend", "first_seen", "last_seen"] if c in agg.columns]
        blank_campaigns = agg[order]

    delivering = blank_campaigns[blank_campaigns["impressions"] >= max(min_impressions, 1)] \
        if len(blank_campaigns) else blank_campaigns
    summary["blank_campaigns"] = int(len(blank_campaigns))
    summary["blank_campaigns_delivering"] = int(len(delivering))
    summary["blank_clients"] = int(blank_campaigns["client"].nunique()) if len(blank_campaigns) and "client" in blank_campaigns else 0

    # raw offending rows, for the CSV the vendor gets
    keep_raw = [c for c in [cols["date"], cols["bu"], cols["client"], cols["product"],
                            cols["strategy_type"], cols["strategy_name"], cols["campaign"],
                            cols["campaign_id"], cols["pool_id"], ccol,
                            cols["impressions"], cols["clicks"], cols["spend"]] if c]
    blank_rows = d.loc[bad, keep_raw].copy() if bad.any() else pd.DataFrame(columns=keep_raw)
    if len(blank_rows):
        blank_rows.insert(len(blank_rows.columns), "issue", d.loc[bad, "_reason"].values)
        blank_rows = blank_rows.sort_values(cols["impressions"] or keep_raw[0], ascending=False)

    # ---- by client, for the "who is affected" glance ------------------------
    by_client = pd.DataFrame()
    if len(blank_campaigns) and "client" in blank_campaigns.columns:
        by_client = blank_campaigns.groupby("client", dropna=False, observed=True).agg(
            campaigns=("campaign_id", "nunique") if "campaign_id" in blank_campaigns else ("client", "size"),
            blank_rows=("blank_rows", "sum"),
            impressions=("impressions", "sum"),
            spend=("spend", "sum"),
        ).reset_index().sort_values("impressions", ascending=False).reset_index(drop=True)

    # ---- secondary integrity checks ----------------------------------------
    checks, tables = [], {}

    def _add(key, label, severity, mask, cols_out=None, note=""):
        n = int(mask.sum())
        checks.append({"key": key, "label": label, "severity": severity, "count": n,
                       "impressions": int(impr[mask].sum()) if n else 0,
                       "spend": float(spend[mask].sum()) if n else 0.0,
                       "note": note})
        if n:
            out = d.loc[mask, [c for c in (cols_out or keep_raw) if c in d.columns]].copy()
            tables[key] = out.sort_values(cols["impressions"], ascending=False) \
                if cols["impressions"] in out.columns else out

    _add("blank_creative", "Blank creative name", "critical", bad,
         note="creative name missing or a placeholder — vendor data error")

    if cols["product"]:
        p = d[cols["product"]].astype(str).str.strip().str.lower()
        unmapped = ~p.isin(_KNOWN_PRODUCTS) & p.ne("") & p.ne("nan")
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

    summary["issues"] = int(sum(c["count"] for c in checks))
    summary["status"] = ("fail" if summary["blank_campaigns_delivering"]
                         else "warn" if summary["blank_rows"] or
                         any(c["count"] and c["severity"] == "warn" for c in checks)
                         else "clean")

    return {"summary": summary, "checks": checks, "tables": tables,
            "blank_campaigns": blank_campaigns, "blank_rows": blank_rows,
            "by_client": by_client}


def alert_subject(summary, label=""):
    """Subject line for the vendor-error email."""
    s = summary or {}
    if s.get("blank_campaigns_delivering"):
        return (f"⚠ Creative QA: {s['blank_campaigns_delivering']} campaign(s) delivering with "
                f"NO creative name{(' — ' + label) if label else ''}")
    if s.get("blank_rows"):
        return f"Creative QA: {s['blank_rows']} blank creative row(s){(' — ' + label) if label else ''}"
    return f"Creative QA: clean{(' — ' + label) if label else ''}"

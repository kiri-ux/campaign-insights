"""
preview_engine.py
Cross-check delivery against AdLib's **Ad Previews** export: which creatives are
running with no preview image on file.

This is a different question from the one the Creative tab already answers. That
one asks whether the *delivery export* carries a preview URL on the row. This one
asks whether a **client-shareable** image exists for the creative at all.

The distinction that matters is the HOST, not whether the column is populated.
AdLib's Campaign Creative Report fills Preview Link for essentially every row,
but on the 14 Aug 2026 file 59,608 of those rows point at
app2.adlibdsp.com/dashboard/… — AdLib's own console, which opens a login screen
for anyone outside their team. Only 11,752 point at app.adreform.com, the public
asset. The Ad Previews export is 100% adreform, which is why it is the reference.

Scored that way on the same day: 3,222 creatives delivered and **1,262 (39%) had
a preview anyone could send to a client**. The other 1,960 — 9.6M impressions —
had nothing but a dashboard link.

Join is on Creative ID, which both exports carry and which is stable — advertiser
and creative NAMES are not (AdLib's own reports punctuate them differently).
"""
import os
from urllib.parse import urlparse

import numpy as np
import pandas as pd

_ID = "Creative ID"

# Not all preview URLs are equal, and the difference is the whole point of this
# check. A preview is only useful if it can be SENT TO A CLIENT.
#
#   app.adreform.com/storage/attachments/perm/…   public asset — shareable
#   app2.adlibdsp.com/dashboard/management/…      AdLib's own dashboard, behind
#                                                 their login — useless to a client
#
# In the 14 Aug 2026 creative report, 59,608 rows carried an adlibdsp dashboard
# link and only 11,752 an adreform asset. Counting a row as "has a preview"
# because the column was non-empty scored the unusable ones as fine.
PUBLIC_HOSTS = {h.strip().lower() for h in os.environ.get(
    "PREVIEW_PUBLIC_HOSTS", "app.adreform.com,adreform.com").split(",") if h.strip()}
INTERNAL_HOSTS = {h.strip().lower() for h in os.environ.get(
    "PREVIEW_INTERNAL_HOSTS", "app2.adlibdsp.com,app.adlibdsp.com,adlibdsp.com").split(",")
    if h.strip()}


def link_kind(url):
    """'public' | 'internal' | 'none' for one preview URL."""
    s = str(url or "").strip()
    if not s or s.lower() in ("nan", "none", "null", "-"):
        return "none"
    host = (urlparse(s).netloc or "").lower()
    if not host or "." not in host:
        return "none"
    host = host.split("@")[-1].split(":")[0]
    if host in PUBLIC_HOSTS or any(host.endswith("." + h) for h in PUBLIC_HOSTS):
        return "public"
    if host in INTERNAL_HOSTS or any(host.endswith("." + h) for h in INTERNAL_HOSTS):
        return "internal"
    # An unfamiliar host is treated as public rather than dismissed — it is at
    # least a real URL, and the host list is env-tunable when a new one shows up.
    return "public"


def _sid(s):
    """Creative IDs compared as clean strings: one export reads them as ints,
    the other as floats, and '200634' != '200634.0'."""
    return (s.astype(str).str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"^nan$", "", regex=True))


def _col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    return None


# Creative types that have no visual preview to publish, so counting them as a
# gap is noise: audio has no image, and a VAST tag is a pointer to a video
# served by the exchange rather than an asset AdLib hosts. Excluded from the
# coverage denominator, but always REPORTED, so the number stays auditable.
EXCLUDE_TYPES = {c.strip().lower().replace(" ", "") for c in os.environ.get(
    "PREVIEW_EXCLUDE_TYPES", "audio,vast tag,vasttag").split(",") if c.strip()}


def audit_previews(creative_df, preview_df, min_impressions=1, exclude_types=None):
    """Coverage of delivering creatives by the Ad Previews export.

    creative_df: creative-grain delivery (AdLib's Campaign Creative Report, or
                 the TapClicks creative export — either carries Creative ID).
    preview_df:  the Ad Previews export.

    Returns {summary, missing, by_client, orphans, no_url, multi_url}.
    """
    out = {"summary": {"available": False}, "missing": pd.DataFrame(),
           "by_client": pd.DataFrame(), "orphans": pd.DataFrame(),
           "no_url": pd.DataFrame(), "multi_url": pd.DataFrame(),
           "nowhere": pd.DataFrame(), "internal_only": pd.DataFrame()}
    if creative_df is None or not len(creative_df) or _ID not in creative_df.columns:
        out["summary"]["reason"] = "the delivery export has no Creative ID column"
        return out
    if preview_df is None or not len(preview_df) or _ID not in preview_df.columns:
        out["summary"]["reason"] = "no Ad Previews export was available"
        return out

    d = creative_df.copy()
    icol = _col(d, "Impressions") or ""
    scol = _col(d, "Billable Spend", "Total Spend", "Internal Cost")
    ccol = _col(d, "Client", "Advertiser Name")
    ncol = _col(d, "Creative Name")
    tcol = _col(d, "Creative Type")
    zcol = _col(d, "Creative Size")
    pcol = _col(d, "Product 2")
    lcol = _col(d, "Preview Image URL", "Preview Link")

    d["_id"] = _sid(d[_ID])
    d["_impr"] = pd.to_numeric(d[icol], errors="coerce").fillna(0) if icol else 0
    d["_spend"] = pd.to_numeric(d[scol], errors="coerce").fillna(0.0) if scol else 0.0
    d = d[d["_id"] != ""]

    p = preview_df.copy()
    p["_id"] = _sid(p[_ID])
    p = p[p["_id"] != ""]
    purl = _col(p, "Preview Image URL", "Preview Link")
    if purl:
        u = p[purl].astype(str).str.strip()
        p["_url"] = u.where(u.map(link_kind) == "public", "")
    else:
        p["_url"] = ""

    have = p.groupby("_id").agg(urls=("_url", lambda s: int(len({v for v in s if v}))),
                                any_url=("_url", "max")).reset_index()
    have_ids = set(have["_id"])
    with_url = set(have.loc[have["any_url"] != "", "_id"])

    agg = {"impressions": ("_impr", "sum"), "spend": ("_spend", "sum"),
           "rows": ("_impr", "size")}
    for label, col in (("creative", ncol), ("client", ccol), ("type", tcol),
                       ("size", zcol), ("product", pcol)):
        if col:
            agg[label] = (col, "first")
    if lcol:
        d["_kind"] = d[lcol].map(link_kind)
        # A public delivery link counts as coverage; an internal one is recorded
        # so the report can say "there IS a link, it just isn't shareable".
        d["_pub_link"] = d[lcol].where(d["_kind"] == "public", "")
        d["_int_link"] = d[lcol].where(d["_kind"] == "internal", "")
        agg["delivery_link"] = ("_pub_link", "max")
        agg["internal_link"] = ("_int_link", "max")
    per = d.groupby("_id", dropna=False, observed=True).agg(**agg).reset_index()
    for c in per.columns:
        if str(per[c].dtype) == "category":
            per[c] = per[c].astype(str)

    delivering = per[per["impressions"] >= max(min_impressions, 1)].copy()

    skip = EXCLUDE_TYPES if exclude_types is None else {
        str(t).strip().lower().replace(" ", "") for t in exclude_types}
    excluded = pd.DataFrame()
    if skip and "type" in delivering.columns:
        is_skip = delivering["type"].astype(str).str.lower().str.replace(
            " ", "", regex=False).isin(skip)
        excluded = delivering[is_skip].copy()
        delivering = delivering[~is_skip].copy()

    delivering["in_previews"] = delivering["_id"].isin(have_ids)
    # "Covered" means a client-shareable image exists SOMEWHERE — the previews
    # export, or a public link on the delivery rows. An AdLib dashboard link is
    # not coverage; it needs their login to open.
    has_pub_delivery = (delivering["delivery_link"].astype(str).str.strip().ne("")
                        if "delivery_link" in delivering.columns
                        else pd.Series(False, index=delivering.index))
    delivering["preview_url"] = delivering["_id"].isin(with_url) | has_pub_delivery

    missing = delivering[~delivering["preview_url"]].copy()
    if "internal_link" in missing.columns:
        has_int = missing["internal_link"].astype(str).str.strip().ne("")
    else:
        has_int = pd.Series(False, index=missing.index)
    missing["reason"] = np.where(
        has_int, "only an AdLib dashboard link — needs their login, can't go to a client",
        np.where(missing["_id"].isin(have_ids),
                 "in the previews export, but with no usable image URL",
                 "no preview link anywhere"))
    missing = missing.sort_values("impressions", ascending=False).reset_index(drop=True)
    missing = missing.rename(columns={"_id": "creative_id"})

    # Creatives in the previews export that aren't delivering. Not a fault —
    # useful for telling "the export is incomplete" apart from "these creatives
    # simply stopped running".
    live_ids = set(per["_id"])
    orph = have[~have["_id"].isin(live_ids)].rename(columns={"_id": "creative_id"})
    if len(orph) and "Creative Name" in p.columns:
        nm = p.groupby("_id")["Creative Name"].first()
        orph["creative"] = orph["creative_id"].map(nm)

    by_client = pd.DataFrame()
    if "client" in delivering.columns:
        g = delivering.groupby("client", dropna=False, observed=True).agg(
            creatives=("creative_id" if "creative_id" in delivering else "_id", "size"),
            with_preview=("preview_url", "sum"),
            impressions=("impressions", "sum"),
            missing_impressions=("impressions", lambda s: 0)).reset_index()
        miss_i = (missing.groupby("client")["impressions"].sum()
                  if "client" in missing.columns else pd.Series(dtype=float))
        g["missing"] = g["creatives"] - g["with_preview"]
        g["missing_impressions"] = g["client"].map(miss_i).fillna(0).astype("int64")
        g["coverage"] = g["with_preview"] / g["creatives"].replace(0, np.nan)
        by_client = g.sort_values("missing_impressions", ascending=False).reset_index(drop=True)

    n_deliv = int(len(delivering))
    n_cov = int(delivering["preview_url"].sum())
    summary = {
        "available": True,
        "preview_rows": int(len(p)),
        "preview_creatives": int(p["_id"].nunique()),
        "delivering_creatives": n_deliv,
        "covered": n_cov,
        "missing": int(len(missing)),
        "coverage_pct": float(n_cov / n_deliv) if n_deliv else 0.0,
        "missing_impressions": int(missing["impressions"].sum()) if len(missing) else 0,
        "missing_spend": float(missing["spend"].sum()) if len(missing) else 0.0,
        "missing_impr_pct": (float(missing["impressions"].sum() / delivering["impressions"].sum())
                             if len(missing) and delivering["impressions"].sum() else 0.0),
        "in_export_no_url": int((missing["reason"].str.startswith("in the previews")).sum())
                            if len(missing) else 0,
        "orphan_previews": int(len(orph)),
        "clients_affected": int(missing["client"].nunique()) if "client" in missing else 0,
        "excluded_creatives": int(len(excluded)),
        "excluded_impressions": (int(excluded["impressions"].sum())
                                 if len(excluded) else 0),
        "excluded_types": sorted(set(excluded["type"].astype(str))) if len(excluded) else [],
    }
    # Everything in `missing` is un-sendable to a client. Split by WHY, because
    # the two need different asks: an AdLib dashboard link means the creative
    # exists and just wasn't published as a shareable asset (a request to AdLib);
    # nothing at all may mean the creative itself is unavailable.
    if len(missing):
        internal_only = missing[missing["reason"].str.startswith("only an AdLib")].copy()
        nothing = missing[missing["reason"].eq("no preview link anywhere")].copy()
    else:
        internal_only = nothing = pd.DataFrame()
    summary["internal_link_only"] = int(len(internal_only))
    summary["internal_link_only_impressions"] = (int(internal_only["impressions"].sum())
                                                 if len(internal_only) else 0)
    summary["no_preview_anywhere"] = int(len(nothing))
    summary["no_preview_anywhere_impressions"] = (int(nothing["impressions"].sum())
                                                  if len(nothing) else 0)
    out["nowhere"] = nothing
    out["internal_only"] = internal_only
    # One creative pointing at several different preview images — usually a
    # recycled ID, and it makes the preview you show the client a coin flip.
    multi = have[have["urls"] > 1].rename(columns={"_id": "creative_id"})

    keep = [c for c in ("creative_id", "client", "creative", "product", "type", "size",
                        "impressions", "clicks", "spend", "reason", "internal_link")
            if c in missing.columns]
    out.update({"summary": summary, "missing": missing[keep] if len(missing) else missing,
                "by_client": by_client, "orphans": orph, "multi_url": multi})
    return out

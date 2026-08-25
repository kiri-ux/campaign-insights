"""
preview_engine.py
Cross-check delivery against AdLib's **Ad Previews** export: which creatives are
running with no preview image on file.

This is a different question from the one the Creative tab already answers. That
one asks whether the *delivery export* carries a preview URL on the row. This one
asks whether the creative exists in the **preview data view at all** — the list
the previews are actually served from. A creative can have a Preview Link in the
delivery file and still be absent from the preview export, and when it is absent
nobody can visually QA it or show it to the client.

On the 14 Aug 2026 sample that gap was large: 3,235 creatives delivered, 1,043
appear in the previews file, so **2,207 live creatives were running with no
preview record** — 11.5M impressions. Worth having as a standing check rather
than something discovered by hand.

Join is on Creative ID, which both exports carry and which is stable — advertiser
and creative NAMES are not (AdLib's own reports punctuate them differently).
"""
import numpy as np
import pandas as pd

_ID = "Creative ID"


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


def audit_previews(creative_df, preview_df, min_impressions=1):
    """Coverage of delivering creatives by the Ad Previews export.

    creative_df: creative-grain delivery (AdLib's Campaign Creative Report, or
                 the TapClicks creative export — either carries Creative ID).
    preview_df:  the Ad Previews export.

    Returns {summary, missing, by_client, orphans, no_url, multi_url}.
    """
    out = {"summary": {"available": False}, "missing": pd.DataFrame(),
           "by_client": pd.DataFrame(), "orphans": pd.DataFrame(),
           "no_url": pd.DataFrame(), "multi_url": pd.DataFrame()}
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
        p["_url"] = u.where(~u.str.lower().isin(["", "nan", "none", "null"]), "")
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
        d["_link"] = d[lcol].astype(str).str.strip()
        agg["delivery_link"] = ("_link", "max")
    per = d.groupby("_id", dropna=False, observed=True).agg(**agg).reset_index()
    for c in per.columns:
        if str(per[c].dtype) == "category":
            per[c] = per[c].astype(str)

    delivering = per[per["impressions"] >= max(min_impressions, 1)].copy()
    delivering["in_previews"] = delivering["_id"].isin(have_ids)
    delivering["preview_url"] = delivering["_id"].isin(with_url)

    missing = delivering[~delivering["preview_url"]].copy()
    missing["reason"] = np.where(missing["_id"].isin(have_ids),
                                 "in the previews export, but with no image URL",
                                 "not in the previews export at all")
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
    }
    # The distinction that decides whether anyone has to chase this. A creative
    # absent from the previews export usually still carries a Preview Link on
    # its delivery rows — annoying, but the image exists and the dashboard can
    # fall back to it. A creative with NO preview in EITHER place cannot be
    # visually QA'd at all, and that is the urgent list.
    nowhere = pd.DataFrame()
    if len(missing) and "delivery_link" in missing.columns:
        link = missing["delivery_link"].astype(str).str.strip()
        bad = link.eq("") | link.str.lower().isin(["nan", "none", "null", "-"])
        nowhere = missing[bad].copy()
    elif len(missing):
        nowhere = missing.copy()          # no link column at all to fall back on
    summary["no_preview_anywhere"] = int(len(nowhere))
    summary["no_preview_anywhere_impressions"] = (int(nowhere["impressions"].sum())
                                                  if len(nowhere) else 0)
    summary["recoverable_from_delivery"] = summary["missing"] - summary["no_preview_anywhere"]
    out["nowhere"] = nowhere
    # One creative pointing at several different preview images — usually a
    # recycled ID, and it makes the preview you show the client a coin flip.
    multi = have[have["urls"] > 1].rename(columns={"_id": "creative_id"})

    keep = [c for c in ("creative_id", "client", "creative", "product", "type", "size",
                        "impressions", "clicks", "spend", "reason", "delivery_link")
            if c in missing.columns]
    out.update({"summary": summary, "missing": missing[keep] if len(missing) else missing,
                "by_client": by_client, "orphans": orph, "multi_url": multi})
    return out

"""
dim_lookup.py
The dimension table: Client, Business Unit, Product, Strategy — the columns
AdLib's delivery files do not carry.

AdLib's reports are delivery truth (their creative and device grains agree to
331 impressions in 17.4M) but they know nothing about how Vici organises the
business: no Client Business Unit, no Product 2, no Strategy. Those live in a
Vici-side export dropped to our own bucket. This module reads that export,
builds a lookup keyed on the IDs both sides share, and fills the gap.

Two jobs, deliberately kept together because they use the same file:

1. **Enrich** — join BU / Product / Strategy onto any delivery frame, on
   Campaign ID first and Campaign Pool ID as the fallback. Reports coverage,
   because a lookup that silently misses half the rows is worse than none.
2. **Audit the naming** — one campaign mapping to two different Business Units,
   a Product that isn't a Vici product, a client name that disagrees with
   AdLib's. These are the naming problems the dimension file itself introduces,
   and they are invisible unless something checks.

Header matching is canonical (lowercase, alphanumerics only) and each field has
a list of aliases, so the export can be renamed or re-ordered without breaking
this. Run `python dim_lookup.py --inspect <file.csv>` to see exactly which of
its columns were recognised and which were ignored.
"""
import os
import re

import numpy as np
import pandas as pd

BUCKET = os.environ.get("DIM_S3_BUCKET", "") or os.environ.get("S3_BUCKET", "")
PREFIX = os.environ.get("DIM_S3_PREFIX", "")
MATCH = os.environ.get("DIM_MATCH", "insight,dimension,lookup,client")


def canon(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# Every spelling we're willing to accept for each field. First match wins, so
# the most specific alias is listed first — 'client_business_unit' must beat
# 'client' for the business-unit slot.
FIELDS = {
    "Campaign ID": ["campaignid", "campaign_id", "campid"],
    "Campaign Pool ID": ["campaignpoolid", "poolid", "campaign_pool_id"],
    "Campaign Pool Name": ["campaignpoolname", "poolname"],
    "Campaign Name": ["campaignname"],
    "Client": ["client", "clientname", "advertisername", "advertiser"],
    "Advertiser ID": ["advertiserid", "clientid"],
    "Client Business Unit": ["clientbusinessunit", "businessunit", "bu",
                             "business_unit", "partner"],
    "Product 2": ["product2", "product", "producttype", "product_2"],
    "Strategy Type": ["strategytype", "strategy_type"],
    "Strategy Name": ["strategyname", "strategy_name", "strategy"],
    "Creative ID": ["creativeid", "creative_id"],
    "Creative Name": ["creativename", "creative_name"],
}
# What a lookup can supply. Everything else in the file is context for the
# naming checks, not something we join on.
FILLABLE = ["Client", "Client Business Unit", "Product 2", "Strategy Type",
            "Strategy Name"]
_MISSING = {"", "nan", "none", "null", "(not in export)", "n/a", "na", "-"}

# Known Vici products, mirrored from creative_engine so both flag the same set.
KNOWN_PRODUCTS = {
    "display", "social mirror", "social mirror ctv", "ctv", "native display",
    "native video", "online audio", "audio", "video", "ctv + video",
    "geo-framing", "geo-fencing", "connected tv", "dynamic",
    "website visitor id", "performance max", "pay-per-click", "youtube",
    "amazon premium display", "search engine optimization",
    "reputation management", "streaming audio", "spotify", "addressable geo-fencing",
}


def map_columns(cols):
    """{engine name: source column} for the headers we recognise."""
    taken, out = set(), {}
    for target, aliases in FIELDS.items():
        for c in cols:
            if c in taken:
                continue
            if canon(c) in aliases:
                out[target] = c
                taken.add(c)
                break
    return out


def _blank(s):
    t = s.astype(str).str.strip().str.lower()
    return s.isna() | t.isin(_MISSING)


def _sid(s):
    """IDs as clean strings — one export reads them int, another float."""
    return (s.astype(str).str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"^nan$", "", regex=True))


def build_lookup(df):
    """Campaign-keyed and pool-keyed dimension tables, plus what's wrong with them.

    One row per ID, because a lookup with duplicate keys silently multiplies the
    frame it is joined to — the same failure mode as the rolling-window
    duplication. Where an ID carries conflicting values the most frequent wins
    and the conflict is reported rather than hidden.
    """
    cmap = map_columns(list(df.columns))
    out = {"columns": cmap, "ignored": [c for c in df.columns if c not in cmap.values()],
           "by_campaign": pd.DataFrame(), "by_pool": pd.DataFrame(),
           "conflicts": pd.DataFrame(), "rows": int(len(df)),
           "supplies": [f for f in FILLABLE if f in cmap]}
    if not out["supplies"]:
        out["error"] = ("that file has none of Client / Business Unit / Product / "
                        "Strategy — nothing to look up")
        return out

    work = pd.DataFrame({t: df[c] for t, c in cmap.items()})
    for idc in ("Campaign ID", "Campaign Pool ID", "Advertiser ID", "Creative ID"):
        if idc in work.columns:
            work[idc] = _sid(work[idc])

    conflicts = []
    for key in ("Campaign ID", "Campaign Pool ID"):
        if key not in work.columns:
            continue
        w = work[work[key] != ""]
        if not len(w):
            continue
        agg = {}
        for f in out["supplies"]:
            agg[f] = (f, lambda s: (s[~_blank(s)].mode().iat[0]
                                    if len(s[~_blank(s)]) else ""))
        tbl = w.groupby(key, dropna=False).agg(**agg).reset_index()
        # Where one ID carries more than one value, say so: that is a naming
        # problem in the source, and it makes the joined value a coin flip.
        for f in out["supplies"]:
            n = w.groupby(key)[f].nunique(dropna=True)
            bad = n[n > 1]
            for k, cnt in bad.items():
                vals = sorted(set(w.loc[w[key] == k, f].dropna().astype(str)))[:4]
                conflicts.append({"key": key, "id": k, "field": f,
                                  "distinct_values": int(cnt), "values": ", ".join(vals)})
        out["by_campaign" if key == "Campaign ID" else "by_pool"] = tbl
    out["conflicts"] = pd.DataFrame(conflicts)
    return out


def enrich(df, lookup, report=False):
    """Fill missing dimension columns on a delivery frame from the lookup.

    Campaign ID first, Campaign Pool ID second. Only ever fills what is blank —
    a value the delivery file already carries is never overwritten, so this can
    run against a TapClicks frame as safely as an AdLib one.
    """
    stats = {"rows": int(len(df)), "filled": {}, "matched_campaign": 0,
             "matched_pool": 0, "unmatched": 0}
    if df is None or not len(df) or not lookup:
        return (df, stats) if report else df
    supplies = lookup.get("supplies") or []
    if not supplies:
        return (df, stats) if report else df

    out = df.copy()
    matched = pd.Series(False, index=out.index)
    for key, tbl, label in (("Campaign ID", lookup.get("by_campaign"), "matched_campaign"),
                            ("Campaign Pool ID", lookup.get("by_pool"), "matched_pool")):
        if tbl is None or not len(tbl) or key not in out.columns:
            continue
        idx = tbl.set_index(key)
        ids = _sid(out[key])
        hit = ids.isin(idx.index) & ~matched
        if not hit.any():
            continue
        stats[label] = int(hit.sum())
        for f in supplies:
            if f not in idx.columns:
                continue
            if f not in out.columns:
                out[f] = np.nan
            need = hit & _blank(out[f])
            if need.any():
                vals = ids[need].map(idx[f])
                # category columns reject unseen values — go to plain object first
                if str(out[f].dtype) == "category":
                    out[f] = out[f].astype("object")
                out.loc[need, f] = vals
                stats["filled"][f] = stats["filled"].get(f, 0) + int(need.sum())
        matched = matched | hit
    stats["unmatched"] = int((~matched).sum())
    stats["match_pct"] = float(matched.mean()) if len(out) else 0.0
    return (out, stats) if report else out


def audit_naming(df, lookup=None):
    """Naming problems in the dimension file itself.

    Separate from creative_engine's checks, which look at delivery. These look
    at the reference data: if the lookup says a campaign is two different
    Business Units, every number attributed to that partner is suspect.
    """
    res = {"unknown_products": pd.DataFrame(), "missing_bu": pd.DataFrame(),
           "conflicts": (lookup or {}).get("conflicts", pd.DataFrame()),
           "summary": {}}
    cmap = map_columns(list(df.columns))
    work = pd.DataFrame({t: df[c] for t, c in cmap.items()})
    keep = [c for c in ("Campaign ID", "Campaign Name", "Client",
                        "Client Business Unit", "Product 2") if c in work.columns]

    if "Product 2" in work.columns:
        p = work["Product 2"].astype(str).str.strip().str.lower()
        bad = ~p.isin(KNOWN_PRODUCTS) & ~_blank(work["Product 2"])
        if bad.any():
            res["unknown_products"] = (work.loc[bad, keep]
                                       .drop_duplicates().reset_index(drop=True))
    if "Client Business Unit" in work.columns:
        miss = _blank(work["Client Business Unit"])
        if miss.any():
            res["missing_bu"] = (work.loc[miss, keep]
                                 .drop_duplicates().reset_index(drop=True))
    res["summary"] = {
        "rows": int(len(df)),
        "campaigns": int(work["Campaign ID"].nunique()) if "Campaign ID" in work else 0,
        "clients": int(work["Client"].nunique()) if "Client" in work else 0,
        "business_units": (int(work["Client Business Unit"].nunique())
                           if "Client Business Unit" in work else 0),
        "unknown_products": int(len(res["unknown_products"])),
        "missing_bu": int(len(res["missing_bu"])),
        "conflicts": int(len(res["conflicts"])),
    }
    return res


def load_from_s3(bucket=None, prefix=None, match=None):
    """Newest dimension export from our own bucket → (df, meta).

    Uses the same credentials as the rest of the app's S3 access (this file
    lands in OUR bucket, not AdLib's). Only the newest matching object is read:
    it is a reference table, so older drops are strictly stale.
    """
    from s3_pull import _client_and_cfg
    s3, default_bucket = _client_and_cfg()
    bucket = bucket or BUCKET or default_bucket
    pre = str(prefix if prefix is not None else PREFIX or "").strip().lstrip("/")
    if pre and not pre.endswith("/"):
        pre += "/"
    pats = [canon(p) for p in (match or MATCH).split(",") if p.strip()]
    newest = None
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=pre):
        for o in page.get("Contents", []):
            key = o["Key"]
            if key.endswith("/") or not key.lower().endswith((".csv", ".xlsx", ".xls")):
                continue
            base = key.rsplit("/", 1)[-1]
            # Inside an explicit folder every file qualifies; at the bucket root
            # the filename has to look like a dimension export or we would read
            # whatever else happens to be lying there.
            if not pre and pats and not any(p in canon(base) for p in pats):
                continue
            if newest is None or o["LastModified"] > newest["LastModified"]:
                newest = o
    if newest is None:
        return pd.DataFrame(), {"error": "no dimension file under s3://%s/%s" % (bucket, pre)}
    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    import io as _io
    if newest["Key"].lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(_io.BytesIO(body))
    else:
        df = pd.read_csv(_io.BytesIO(body), low_memory=False)
    return df, {"bucket": bucket, "key": newest["Key"],
                "file": newest["Key"].rsplit("/", 1)[-1],
                "modified": str(newest["LastModified"].replace(tzinfo=None)),
                "rows": int(len(df)), "mb": round(newest["Size"] / 1e6, 2)}


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Inspect a dimension export.")
    ap.add_argument("path", help="the CSV/XLSX to inspect")
    a = ap.parse_args()
    df = (pd.read_excel(a.path) if a.path.lower().endswith((".xlsx", ".xls"))
          else pd.read_csv(a.path, low_memory=False))
    lk = build_lookup(df)
    print("%s rows, %d columns\n" % (format(len(df), ","), len(df.columns)))
    print("RECOGNISED:")
    for t, c in lk["columns"].items():
        print("   %-22s <- %s" % (t, c))
    if lk["ignored"]:
        print("\nIGNORED (%d): %s" % (len(lk["ignored"]), ", ".join(map(str, lk["ignored"][:20]))))
    if lk.get("error"):
        print("\n!! %s" % lk["error"])
        return
    print("\nSUPPLIES: %s" % ", ".join(lk["supplies"]))
    print("   by campaign : %s row(s)" % format(len(lk["by_campaign"]), ","))
    print("   by pool     : %s row(s)" % format(len(lk["by_pool"]), ","))
    aud = audit_naming(df, lk)
    print("\nNAMING:")
    for k, v in aud["summary"].items():
        print("   %-18s %s" % (k, format(v, ",") if isinstance(v, int) else v))
    if len(aud["conflicts"]):
        print("\n   conflicting IDs (same ID, different value):")
        print(aud["conflicts"].head(10).to_string(index=False))
    if len(aud["unknown_products"]):
        print("\n   unrecognised products:")
        print(aud["unknown_products"].head(10).to_string(index=False))


if __name__ == "__main__":
    _cli()

#!/usr/bin/env python3
"""
s3_diagnose.py — settle "the device data shows more impressions than what ran",
directly against the S3 files, without moving credentials anywhere.

Run it wherever the AWS credentials already live (Render, a laptop with an AWS
profile, CloudShell). It reads only; it never writes to the bucket.

    pip3 install boto3 pandas openpyxl
    export S3_BUCKET=…                       # S3_PREFIX optional

    python3 s3_diagnose.py --list            # inventory an unfamiliar bucket first
    python3 s3_diagnose.py --start 2026-07-15 --end 2026-07-15 \
        --reference-match Campaign_Report \
        --advertiser "Fresh Start Cleaning" --date 2026-07-15
    python3 s3_diagnose.py --local-dir ./files     # rehearse with no AWS at all

What it answers
  1. SCHEMA      The real column list of each report, and which columns were
                 treated as dimensions vs metrics. Everything below depends on
                 that split, so it is printed rather than assumed.
  2. OVERLAP     How many files carry each delivery date. These are rolling
                 LAST-N-DAYS drops, so a date appears in several files — that is
                 the duplication exposure.
  3. DUPLICATION Naive concatenation vs correct de-duplication, split into exact
                 repeated rows and restatements (same dimensions, newer numbers).
  4. RECONCILE   Device vs a reference grain per advertiser, net AND gross.
  5. DRILL       One advertiser, one date, every file that carries it — the number
                 to hold against what the warehouse shows.

Everything printed is also written to an .xlsx containing no credentials.
"""
__version__ = "4.7"

import argparse
import io
import os
import re
import sys
from collections import defaultdict

import pandas as pd


def canon(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# A column is a MEASURE if its name carries one of these; everything else is a
# dimension and therefore part of the de-duplication key. A wrong call in either
# direction is visible in the printed schema.
METRIC_TOKENS = ("impression", "click", "ctr", "cpm", "cpc", "cpa", "cpv", "cpcv",
                 "spend", "cost", "conversion", "revenue", "quartile", "complete",
                 "view", "start", "margin", "vcr", "reach", "frequency", "budget",
                 "win", "bid", "midpoint", "currency")
ROLES = {"date": "date", "client": "client", "advertisername": "client",
         "advertiser": "client", "impressions": "impressions", "clicks": "clicks",
         "billablespend": "spend", "devicetype": "device", "campaignid": "campaign_id"}


def classify(cols):
    """(dimensions, metrics) for a header row."""
    metrics = [c for c in cols if any(t in canon(c) for t in METRIC_TOKENS)]
    return [c for c in cols if c not in metrics], metrics


def role_map(cols):
    out = {}
    for c in cols:
        r = ROLES.get(canon(c))
        if r and r not in out:
            out[r] = c
    return out


def kind_of(name, dev_pats, ref_pats):
    n = canon(name)
    if any(canon(p) in n for p in dev_pats):
        return "device"
    if any(canon(p) in n for p in ref_pats):
        return "reference"
    return None


FNAME_DATE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


def date_in_name(name):
    import datetime as _dt
    m = FNAME_DATE.search(name.rsplit("/", 1)[-1])
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def window_filter(metas, lo, hi, grace=8):
    """Keep files whose FILENAME date could carry delivery in [lo, hi].
    A 'Last N Days' file stamped D holds roughly D-N..D, so only stamps from lo
    to hi+grace matter; anything stamped before lo cannot help."""
    if not (lo and hi):
        return metas, 0
    import datetime as _dt
    a, b = lo, hi + _dt.timedelta(days=grace)
    keep, skipped = [], 0
    for m in metas:
        d = date_in_name(m["name"])
        if d is None or (a <= d <= b):
            keep.append(m)
        else:
            skipped += 1
    return keep, skipped


def friendly_aws_error(e, bucket):
    import botocore.exceptions as be
    if isinstance(e, (be.NoCredentialsError, be.PartialCredentialsError)):
        return ("No AWS credentials found. Set AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY in this Terminal window, or pass --profile.")
    if isinstance(e, be.ParamValidationError):
        clean = "".join(c for c in str(bucket) if c.isalnum() or c in ".-_")
        return (f"Invalid bucket name {bucket!r}. If that shows curly quotes, "
                f"retype it: --bucket {clean}")
    if isinstance(e, be.ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        msg = e.response.get("Error", {}).get("Message", "")
        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
            return (f"AWS rejected the credentials ({code}) — usually a stray quote "
                    f"or space; the key should be 20 chars and the secret 40.")
        if code in ("AccessDenied", "AllAccessDisabled"):
            return (f"Credentials work but can't list '{bucket}' (AccessDenied). "
                    f"Need s3:ListBucket and s3:GetObject.")
        if code == "NoSuchBucket":
            return f"No bucket called '{bucket}'."
        if code in ("PermanentRedirect", "IllegalLocationConstraintException",
                    "AuthorizationHeaderMalformed"):
            region = e.response.get("Error", {}).get("Region") or ""
            return (f"Wrong region for '{bucket}'." +
                    (f" Try AWS_DEFAULT_REGION={region}." if region else
                     " Set AWS_DEFAULT_REGION."))
        return f"AWS error {code}: {msg}"
    if isinstance(e, be.EndpointConnectionError):
        return "Couldn't reach AWS — check the network / VPN."
    return f"{type(e).__name__}: {e}"


def list_s3(bucket, prefix, profile=None):
    import boto3
    sess = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = sess.client("s3", region_name=os.environ.get("AWS_REGION") or None)
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                             Prefix=prefix or ""):
        for o in page.get("Contents", []):
            out.append({"name": o["Key"].rsplit("/", 1)[-1], "key": o["Key"],
                        # tz-aware datetimes cannot be written to Excel later
                        "modified": o["LastModified"].replace(tzinfo=None),
                        "size": o["Size"]})
    return sorted(out, key=lambda m: m["modified"]), s3


def list_local(d):
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.lower().endswith((".csv", ".xlsx")):
            p = os.path.join(d, fn)
            out.append({"name": fn, "key": p,
                        "modified": pd.Timestamp(os.path.getmtime(p), unit="s").to_pydatetime(),
                        "size": os.path.getsize(p)})
    return out


def open_stream(meta, s3, bucket):
    if s3 is None:
        with open(meta["key"], "rb") as f:
            return f.read()
    return s3.get_object(Bucket=bucket, Key=meta["key"])["Body"].read()


def scan_files(metas, s3, bucket, lo, hi, label, chunksize=200_000):
    """Stream files NEWEST FIRST, de-duplicating on every dimension column.

    Newest-first with keep-first is chronologically identical to keep-last: the
    most recent restatement of a row wins, which is what a correct ingestion of
    rolling windows must do.
    """
    seen_dims, seen_full = set(), set()
    naive_i = dedup_i = 0
    exact_dupes = restatements = 0
    per_adv = defaultdict(lambda: {"impressions": 0, "clicks": 0})
    per_file, date_files, schema = [], defaultdict(set), None

    for m in sorted(metas, key=lambda x: x["modified"], reverse=True):
        data = open_stream(m, s3, bucket)
        is_csv = m["name"].lower().endswith(".csv")
        head = (pd.read_csv(io.BytesIO(data), nrows=0) if is_csv
                else pd.read_excel(io.BytesIO(data), nrows=0))
        dims, metrics = classify(list(head.columns))
        roles = role_map(list(head.columns))
        if schema is None:
            schema = {"columns": list(head.columns), "dimensions": dims,
                      "metrics": metrics}
            print(f"\n  {label} schema, from {m['name']}:")
            print(f"    dimensions ({len(dims)}): {', '.join(map(str, dims))}")
            print(f"    metrics    ({len(metrics)}): {', '.join(map(str, metrics))}")
            missing = [r for r in ("date", "client", "impressions") if r not in roles]
            if missing:
                print(f"    ! not found: {', '.join(missing)} — analysis may be limited")
        if "date" not in roles or "impressions" not in roles:
            print(f"  ! {m['name']}: no date/impressions column, skipped")
            del data
            continue

        f_rows = f_impr = 0
        f_min = f_max = None
        reader = (pd.read_csv(io.BytesIO(data), chunksize=chunksize, low_memory=False)
                  if is_csv else iter([pd.read_excel(io.BytesIO(data))]))
        for chunk in reader:
            chunk = chunk.copy()
            chunk["_d"] = pd.to_datetime(chunk[roles["date"]], errors="coerce").dt.date
            if lo:
                chunk = chunk[chunk["_d"] >= lo]
            if hi:
                chunk = chunk[chunk["_d"] <= hi]
            if not len(chunk):
                continue
            impr = pd.to_numeric(chunk[roles["impressions"]], errors="coerce").fillna(0)
            clk = (pd.to_numeric(chunk[roles["clicks"]], errors="coerce").fillna(0)
                   if "clicks" in roles else pd.Series(0, index=chunk.index))

            dcols = [c for c in dims if c in chunk.columns]
            mcols = [c for c in metrics if c in chunk.columns]
            dh = pd.util.hash_pandas_object(chunk[dcols].astype(str),
                                            index=False).to_numpy()
            fh = pd.util.hash_pandas_object(chunk[dcols + mcols].astype(str),
                                            index=False).to_numpy()

            naive_i += int(impr.sum())
            f_rows += len(chunk)
            f_impr += int(impr.sum())
            for d in chunk["_d"].dropna().unique():
                date_files[d].add(m["name"])
            cmin, cmax = chunk["_d"].min(), chunk["_d"].max()
            f_min = cmin if f_min is None or cmin < f_min else f_min
            f_max = cmax if f_max is None or cmax > f_max else f_max

            # Vectorised: a per-row Python loop over millions of rows is the
            # difference between seconds and many minutes.
            sdh = pd.Series(dh)
            is_new = (~sdh.isin(seen_dims)) & (~sdh.duplicated(keep="first"))
            new_mask = is_new.to_numpy()
            dup_mask = ~new_mask
            if dup_mask.any():
                exact = int(pd.Series(fh[dup_mask]).isin(seen_full).sum())
                exact_dupes += exact
                restatements += int(dup_mask.sum()) - exact
            if new_mask.any():
                iv, cv = impr.to_numpy(), clk.to_numpy()
                dedup_i += int(iv[new_mask].sum())
                seen_dims.update(dh[new_mask].tolist())
                seen_full.update(fh[new_mask].tolist())
                if "client" in roles:
                    adv = chunk[roles["client"]].astype(str).to_numpy()[new_mask]
                    g = pd.DataFrame({"a": adv, "i": iv[new_mask],
                                      "c": cv[new_mask]}).groupby("a", sort=False)[
                        ["i", "c"]].sum()
                    for name, row in g.iterrows():
                        per_adv[name]["impressions"] += int(row.i)
                        per_adv[name]["clicks"] += int(row.c)
                else:
                    per_adv["(unknown)"]["impressions"] += int(iv[new_mask].sum())
                    per_adv["(unknown)"]["clicks"] += int(cv[new_mask].sum())

        per_file.append({"file": m["name"], "modified": m["modified"],
                         "rows_in_window": f_rows, "impressions_in_window": f_impr,
                         "first_date": f_min, "last_date": f_max})
        print(f"  read {m['name'][:52]:54s} {f_rows:>8,} rows  {f_impr:>12,} impr")
        del data

    return {"naive_impressions": naive_i, "dedup_impressions": dedup_i,
            "exact_duplicate_rows": exact_dupes, "restated_rows": restatements,
            "per_advertiser": per_adv, "per_file": per_file,
            "date_files": date_files, "schema": schema}


# Columns stable enough to anchor a row across snapshots. Everything else is
# tested for drift against these.
ANCHOR_CANON = ("date", "advertiserid", "campaignid", "campaignpoolid")


def compare_snapshots(metas, s3, bucket, lo, hi):
    """Which dimension columns change between two snapshots of the same delivery?

    Anchors each row on the stable IDs, then compares the set of values every
    other dimension takes under that anchor in the oldest vs the newest file.
    A column that differs is a column that silently creates a NEW row in any
    warehouse whose unique key includes it.
    """
    metas = [m for m in metas if m["name"].lower().endswith(".csv")]
    if len(metas) < 2:
        print("   need at least 2 CSV files in the window to compare")
        return pd.DataFrame()
    ordered = sorted(metas, key=lambda x: x["modified"])

    def load_window(m):
        """Rows for the requested dates, dimensions + impressions only.

        Returns None when the file carries no rows for the window. A
        LAST-7-DAYS file does NOT contain its own drop date, so the oldest
        file in the window is routinely empty — that is not an error, we
        just move to the next one.
        """
        data = open_stream(m, s3, bucket)
        try:
            head = pd.read_csv(io.BytesIO(data), nrows=0)
            roles = role_map(list(head.columns))
            if "date" not in roles:
                print(f"   {m['name'][:52]:54s} no date column — skipped")
                return None
            dims, _ = classify(list(head.columns))
            keep = list(dims)
            for extra in (roles["date"], roles.get("impressions")):
                if extra and extra not in keep:
                    keep.append(extra)
            # Read every dimension as raw TEXT. Left to infer, pandas reads an ID
            # column as float when one file has a blank in it and int when the
            # next doesn't — which shows up as a fake "123996.0 -> 123996" drift.
            dtypes = {c: str for c in keep if c != roles.get("impressions")}
            frames = []
            for chunk in pd.read_csv(io.BytesIO(data), usecols=keep, dtype=dtypes,
                                     chunksize=200_000, low_memory=False):
                d = pd.to_datetime(chunk[roles["date"]], errors="coerce").dt.date
                if lo is not None:
                    chunk = chunk[d >= lo]; d = d[d >= lo]
                if hi is not None:
                    chunk = chunk[d <= hi]
                if len(chunk):
                    frames.append(chunk)
        finally:
            del data
        if not frames:
            print(f"   {m['name'][:52]:54s} no rows for this date — skipped")
            return None
        df = pd.concat(frames, ignore_index=True)
        print(f"   {m['name'][:52]:54s} {len(df):>8,} rows  USING")
        return df

    # Cheap pre-filter so we don't download files that cannot hold the date.
    # A LAST-7-DAYS file stamped D covers roughly D-7..D-1 (it excludes its own
    # drop day), so only stamps in (hi, lo+7] are worth opening. Falls back to
    # everything if the filenames don't carry usable dates.
    if lo is not None and hi is not None:
        import datetime as _dt
        cand = [m for m in ordered
                if (date_in_name(m["name"]) or _dt.date.min) > hi
                and (date_in_name(m["name"]) or _dt.date.max) <= lo + _dt.timedelta(days=7)]
        if len(cand) >= 2:
            if len(cand) < len(ordered):
                print(f"   {len(ordered) - len(cand)} file(s) can't cover this date "
                      f"by their filename — not downloaded")
            ordered = cand

    a = b = None
    na = nb = ""
    used = set()
    for m in ordered:                       # oldest snapshot that has the date
        a = load_window(m)
        if a is not None:
            na = m["name"]; used.add(m["key"])
            break
    for m in reversed(ordered):             # newest snapshot that has the date
        if m["key"] in used:
            break
        b = load_window(m)
        if b is not None:
            nb = m["name"]
            break
    if a is None:
        print("\n   No file in this window contains that date.")
        print("   Remember a LAST-7-DAYS file excludes its own drop day, so a date")
        print("   first appears in the NEXT day's file. Widen --start/--end by a few")
        print("   days, or run --list to see what dates the filenames cover.")
        return pd.DataFrame()
    if b is None:
        print(f"\n   Only one snapshot ({na}) contains that date, so there is nothing")
        print("   to compare it against. Widen the window by a day or two — the whole")
        print("   point is to see the same delivery date in two different drops.")
        return pd.DataFrame()
    dims_a, _ = classify(list(a.columns))
    anchors = [c for c in dims_a if canon(c) in ANCHOR_CANON and c in b.columns]
    if not anchors:
        print("   could not find anchor columns (Date / Advertiser ID / Campaign ID)")
        return pd.DataFrame()
    others = [c for c in dims_a if c not in anchors and c in b.columns]
    print(f"   anchored on: {', '.join(anchors)}")

    ka = a[anchors].astype(str).agg("\x1f".join, axis=1)
    kb = b[anchors].astype(str).agg("\x1f".join, axis=1)
    roles_b = role_map(list(b.columns))
    impr_b = (pd.to_numeric(b[roles_b["impressions"]], errors="coerce").fillna(0)
              if "impressions" in roles_b else None)
    impr_by_key = (impr_b.groupby(kb).sum() if impr_b is not None else None)
    total_impr = float(impr_b.sum()) if impr_b is not None else 0.0
    rows = []
    for col in others:
        ga = a.assign(_k=ka).groupby("_k")[col].apply(lambda s: frozenset(s.astype(str)))
        gb = b.assign(_k=kb).groupby("_k")[col].apply(lambda s: frozenset(s.astype(str)))
        common = ga.index.intersection(gb.index)
        if not len(common):
            continue
        differs = (ga.loc[common] != gb.loc[common])
        n = int(differs.sum())
        ex = ""
        at_risk = 0
        if n:
            k = differs[differs].index[0]
            va = sorted(ga.loc[k])[:2]
            vb = sorted(gb.loc[k])[:2]
            ex = f"{va} -> {vb}"
            if impr_by_key is not None:
                keys = differs[differs].index
                at_risk = float(impr_by_key.reindex(keys).fillna(0).sum())
        rows.append({"column": col, "anchor_groups_compared": int(len(common)),
                     "groups_where_it_changed": n,
                     "pct_changed": n / len(common) if len(common) else 0,
                     "impressions_at_risk": int(at_risk),
                     "pct_of_impressions": (at_risk / total_impr) if total_impr else 0,
                     "example": ex})
    out = pd.DataFrame(rows).sort_values("groups_where_it_changed", ascending=False)
    print(f"\n   comparing {na}  ->  {nb}")
    print(f"   {'COLUMN':28s} {'CHANGED':>9s} {'OF':>9s} {'PCT':>7s} {'IMPR AT RISK':>14s}")
    for _, r in out.iterrows():
        flag = "  <<<" if r.groups_where_it_changed else ""
        print(f"   {str(r.column)[:28]:28s} {r.groups_where_it_changed:>9,} "
              f"{r.anchor_groups_compared:>9,} {r.pct_changed:>6.1%} "
              f"{r.impressions_at_risk:>14,}{flag}")
    worst = out[out.groups_where_it_changed > 0]
    if len(worst):
        print(f"\n   Unstable column(s): {', '.join(worst.column.astype(str))}")
        print(f"   Example: {worst.iloc[0].example}")
        print(f"   Impressions under an unstable key: "
              f"{int(worst.impressions_at_risk.max()):,} "
              f"({worst.pct_of_impressions.max():.1%} of the newest snapshot)")
        print("   Any warehouse whose unique key includes these will INSERT a new row")
        print("   instead of updating, i.e. count the same delivery twice.")
    else:
        print("\n   No dimension drift between these two snapshots.")
    return out


def drill(metas, s3, bucket, advertiser, day):
    """Every file's figure for one advertiser on one date."""
    rows = []
    for m in sorted(metas, key=lambda x: x["modified"]):
        data = open_stream(m, s3, bucket)
        if not m["name"].lower().endswith(".csv"):
            del data
            continue
        head = pd.read_csv(io.BytesIO(data), nrows=0)
        roles = role_map(list(head.columns))
        if not {"date", "client", "impressions"} <= set(roles):
            del data
            continue
        tot_i = tot_c = n = 0
        for chunk in pd.read_csv(io.BytesIO(data), chunksize=200_000, low_memory=False):
            d = pd.to_datetime(chunk[roles["date"]], errors="coerce").dt.date
            sel = chunk[(d == day) &
                        (chunk[roles["client"]].astype(str).str.strip().str.lower()
                         == advertiser.strip().lower())]
            if len(sel):
                n += len(sel)
                tot_i += int(pd.to_numeric(sel[roles["impressions"]],
                                           errors="coerce").fillna(0).sum())
                if "clicks" in roles:
                    tot_c += int(pd.to_numeric(sel[roles["clicks"]],
                                               errors="coerce").fillna(0).sum())
        rows.append({"file": m["name"], "rows": n, "impressions": tot_i, "clicks": tot_c})
        del data
    return pd.DataFrame(rows)


def upload_lag(metas, window=7):
    """Files whose upload time is long after their filename date — i.e. re-uploads.

    Metadata only, nothing is downloaded. A LAST-N-DAYS file stamped D normally
    lands on D and covers D-N..D-1. When it lands weeks later the warehouse sees
    a changed object and re-reads it, so every delivery day inside that file can
    be ingested a second time. Line the covered spans up against the days the
    warehouse over-counts and the culprit names itself.
    """
    import datetime as _dt
    rows = []
    for m in metas:
        d = date_in_name(m["name"])
        if not d:
            continue
        lag = (m["modified"].date() - d).days
        rows.append({"file": m["name"], "filename_date": d,
                     "uploaded": m["modified"], "lag_days": lag,
                     "covers_from": d - _dt.timedelta(days=window),
                     "covers_to": d - _dt.timedelta(days=1),
                     "mb": round(m["size"] / 1e6, 1)})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("lag_days", ascending=False)
    late = df[df.lag_days > 1]
    print(f"   {len(df)} dated file(s); {len(late)} uploaded more than a day "
          f"after their filename date")
    if not len(late):
        print("   No re-uploads — every file landed on schedule.")
        return df
    print(f"\n   {'FILE':50s} {'UPLOADED':12s} {'LAG':>5s} {'COVERS':23s} {'MB':>7s}")
    for _, r in late.iterrows():
        print(f"   {str(r.file)[:50]:50s} {str(r.uploaded)[:10]:12s} "
              f"{r.lag_days:>4}d  {str(r.covers_from)} → {str(r.covers_to)} "
              f"{r.mb:>7,.1f}")
    days = set()
    for _, r in late.iterrows():
        d = r.covers_from
        while d <= r.covers_to:
            days.add(d); d += _dt.timedelta(days=1)
    print(f"\n   Delivery days sitting inside a re-uploaded file: "
          f"{min(days)} → {max(days)} ({len(days)} days)")
    print("   Any of those days that read exactly 2x in the warehouse were "
          "ingested twice.")
    return df


def daily_truth(metas, s3, bucket, lo, hi, advertiser):
    """Per-DAY delivery for one advertiser, to hold against TapClicks day by day.

    A month total can be 1.33x while a single day matches exactly — that means
    SOME days are counted twice, not all of them, and only a daily table shows
    which. Reads a minimal NON-OVERLAPPING set of snapshots: a LAST-7-DAYS file
    stamped D holds D-7..D-1 complete, so roughly five files cover a month
    instead of thirty-one.
    """
    import datetime as _dt
    csvs = [m for m in metas if m["name"].lower().endswith(".csv")]
    stamped = [(date_in_name(m["name"]), m) for m in csvs]
    stamped = [(d, m) for d, m in stamped if d]
    if not stamped:
        print("   filenames carry no dates — can't pick a covering set")
        return pd.DataFrame()
    sizes = sorted(m["size"] for _, m in stamped)
    med = sizes[len(sizes) // 2] if sizes else 0

    plan, cur = [], lo
    while cur <= hi:
        cands = [(d, m) for d, m in stamped if cur + _dt.timedelta(days=1) <= d
                 <= cur + _dt.timedelta(days=7)]
        full = [(d, m) for d, m in cands if not (med and m["size"] < 0.5 * med)]
        pool = full or cands
        if not pool:
            cur += _dt.timedelta(days=1)
            continue
        d, m = max(pool, key=lambda t: t[0])
        span = (max(cur, d - _dt.timedelta(days=7)), min(hi, d - _dt.timedelta(days=1)))
        plan.append((m, span, bool(full)))
        cur = d
    if not plan:
        print("   no files cover that range")
        return pd.DataFrame()
    print(f"   {len(plan)} file(s) cover {lo} → {hi} with no overlap:")
    for m, (s, e), ok in plan:
        print(f"     {m['name'][:52]:54s} {s} → {e}"
              + ("" if ok else "   ! undersized file, may be partial"))

    want = canon(advertiser)
    out = []
    for m, (s, e), _ in plan:
        data = open_stream(m, s3, bucket)
        try:
            head = pd.read_csv(io.BytesIO(data), nrows=0)
            roles = role_map(list(head.columns))
            if not {"date", "client", "impressions"} <= set(roles):
                print(f"   {m['name']}: missing date/advertiser/impressions — skipped")
                continue
            dims, _ = classify(list(head.columns))
            keep = list(dict.fromkeys(dims + [roles["date"], roles["impressions"]]
                                      + ([roles["clicks"]] if "clicks" in roles else [])))
            dtypes = {c: str for c in dims}
            got = []
            for chunk in pd.read_csv(io.BytesIO(data), usecols=keep, dtype=dtypes,
                                     chunksize=200_000, low_memory=False):
                d = pd.to_datetime(chunk[roles["date"]], errors="coerce").dt.date
                sel = chunk[(d >= s) & (d <= e) &
                            (chunk[roles["client"]].map(canon) == want)]
                if len(sel):
                    got.append(sel)
        finally:
            del data
        if not got:
            continue
        df = pd.concat(got, ignore_index=True)
        df["_d"] = pd.to_datetime(df[roles["date"]], errors="coerce").dt.date
        i = pd.to_numeric(df[roles["impressions"]], errors="coerce").fillna(0)
        c = (pd.to_numeric(df[roles["clicks"]], errors="coerce").fillna(0)
             if "clicks" in roles else pd.Series(0, index=df.index))
        dedup = df.duplicated(subset=[x for x in dims if x in df.columns], keep="first")
        for day, g in df.groupby("_d"):
            k = g.index
            out.append({"date": day, "file": m["name"], "rows": len(g),
                        "impressions": int(i.loc[k].sum()),
                        "impressions_dedup": int(i.loc[k][~dedup.loc[k]].sum()),
                        "clicks": int(c.loc[k].sum()),
                        "duplicate_rows": int(dedup.loc[k].sum())})
    if not out:
        print(f"   no rows for '{advertiser}' in that range — check the spelling "
              f"against the --list output")
        return pd.DataFrame()
    res = pd.DataFrame(out).sort_values("date").reset_index(drop=True)
    print(f"\n   {advertiser} — one row per delivery day, AdLib's own files")
    print(f"   {'DATE':12s} {'IMPRESSIONS':>13s} {'DEDUPED':>11s} {'CLICKS':>8s} {'DUP ROWS':>9s}")
    for _, r in res.iterrows():
        flag = "  <<<" if r.duplicate_rows else ""
        print(f"   {str(r.date):12s} {r.impressions:>13,} {r.impressions_dedup:>11,} "
              f"{r.clicks:>8,} {r.duplicate_rows:>9,}{flag}")
    print(f"   {'TOTAL':12s} {res.impressions.sum():>13,} "
          f"{res.impressions_dedup.sum():>11,} {res.clicks.sum():>8,} "
          f"{res.duplicate_rows.sum():>9,}")
    print("\n   Put TapClicks' DAILY figures for the same advertiser next to these.")
    print("   Days that match are ingesting fine; days that are a clean multiple")
    print("   name the drop that was counted twice.")
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--advertiser"); ap.add_argument("--date")
    ap.add_argument("--local-dir")
    ap.add_argument("--bucket", default=os.environ.get("S3_BUCKET", ""))
    ap.add_argument("--prefix", default=os.environ.get("S3_PREFIX", ""))
    ap.add_argument("--max-files", type=int, default=60)
    ap.add_argument("--max-mb", type=float, default=2000)
    ap.add_argument("--profile")
    ap.add_argument("--version", action="version", version="s3_diagnose " + __version__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="compare the oldest and newest snapshot in the window and "
                         "report which dimension columns change between them")
    ap.add_argument("--daily", action="store_true",
                    help="per-day delivery for one --advertiser across the window, "
                         "read from a minimal non-overlapping set of snapshots")
    ap.add_argument("--uploads", action="store_true",
                    help="metadata only: which files were uploaded late (re-uploads) "
                         "and which delivery days they cover. Downloads nothing")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--device-match", default="device-report,device-insights,device-data")
    ap.add_argument("--reference-match",
                    default="creative-insights,campaign-creative-report")
    ap.add_argument("--out", default="s3_device_diagnosis.xlsx")
    a = ap.parse_args()

    smart = dict.fromkeys(map(ord, "“”‘’«»\"'"), None)
    for f in ("bucket", "prefix", "advertiser", "date", "device_match",
              "reference_match", "local_dir", "start", "end", "profile", "out"):
        v = getattr(a, f, None)
        if isinstance(v, str):
            setattr(a, f, v.translate(smart).strip())
    if a.profile:
        os.environ["AWS_PROFILE"] = a.profile
    dev_pats = [p for p in a.device_match.split(",") if p.strip()]
    ref_pats = [p for p in a.reference_match.split(",") if p.strip()]

    if a.local_dir:
        metas, s3 = list_local(a.local_dir), None
        print(f"Reading local files from {a.local_dir}")
    else:
        if not a.bucket:
            sys.exit("Set S3_BUCKET or pass --bucket.")
        try:
            metas, s3 = list_s3(a.bucket, a.prefix, a.profile)
        except Exception as e:
            sys.exit("\n  " + friendly_aws_error(e, a.bucket) + "\n")
        print(f"Bucket s3://{a.bucket}/{a.prefix}")

    if a.list:
        print(f"{len(metas)} object(s)\n")
        groups = defaultdict(list)
        for m in metas:
            groups[re.sub(r"\d+", "#", m["name"])].append(m)
        print(f"{'FILENAME PATTERN':56s} {'COUNT':>6s} {'MATCHED':>10s}  NEWEST")
        for pat, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f"{pat[:56]:56s} {len(items):>6d} "
                  f"{str(kind_of(pat, dev_pats, ref_pats)):>10s}  "
                  f"{max(i['modified'] for i in items)}")
        print("\nUnmatched pattern you need? Pass --device-match / --reference-match.")
        return

    lo = pd.to_datetime(a.start).date() if a.start else None
    hi = pd.to_datetime(a.end).date() if a.end else None
    dev_all = [m for m in metas if kind_of(m["name"], dev_pats, ref_pats) == "device"]
    ref_all = [m for m in metas if kind_of(m["name"], dev_pats, ref_pats) == "reference"]
    dev, ds = window_filter(dev_all, lo, hi)
    ref, rs = window_filter(ref_all, lo, hi)
    if ds or rs:
        print(f"Date filter: skipped {ds + rs} file(s) outside {lo} → {hi} (+8 days)")
    dev, ref = dev[:a.max_files], ref[:a.max_files]
    mb = sum(m["size"] for m in dev + ref) / 1e6
    print(f"Found {len(dev)} device file(s), {len(ref)} reference file(s) — {mb:,.0f} MB\n")

    suspect = []
    for kind, group in (("device", dev), ("reference", ref)):
        if len(group) >= 4:
            sizes = sorted(m["size"] for m in group)
            med = sizes[len(sizes) // 2]
            for m in group:
                if med and m["size"] < 0.5 * med:
                    suspect.append((m["name"], kind, m["size"] / med, m["size"] / 1e6))
    if suspect:
        print("  *** UNDERSIZED FILES — likely truncated or partial drops ***")
        for n, k, ratio, sz in sorted(suspect, key=lambda t: t[2]):
            print(f"   !  {n}  {sz:,.1f} MB = {ratio:.0%} of a typical {k} file")
        print()

    if a.dry_run:
        for m in dev + ref:
            print(f"  {m['size']/1e6:8,.1f} MB  {m['name']}")
        print(f"\nDry run — nothing downloaded. Total {mb:,.0f} MB.")
        return
    if not dev:
        sys.exit("No device files matched — check --device-match against --list.")

    if a.uploads:
        print("\n=== UPLOAD TIMING (metadata only, nothing downloaded) ===")
        up_df = upload_lag(dev)
        with pd.ExcelWriter(a.out, engine="openpyxl") as xl:
            (up_df if len(up_df) else
             pd.DataFrame([["no dated files", ""]], columns=["Measure", "Value"])
             ).to_excel(xl, sheet_name="Upload timing", index=False)
        print(f"\nWrote {a.out}")
        return

    # --daily and --compare open only a handful of the matched files, so the
    # window-size guard (which assumes every file is read) doesn't apply.
    if a.daily:
        if not (a.advertiser and lo and hi):
            sys.exit("--daily needs --advertiser, --start and --end.")
        print("\n=== DAILY DELIVERY, ONE ADVERTISER ===")
        day_df = daily_truth(dev, s3, a.bucket, lo, hi, a.advertiser)
        with pd.ExcelWriter(a.out, engine="openpyxl") as xl:
            if len(day_df):
                day_df.to_excel(xl, sheet_name="Daily", index=False)
            else:
                pd.DataFrame([["no daily table produced",
                               "See the terminal output."]],
                             columns=["Measure", "Value"]).to_excel(
                    xl, sheet_name="Summary", index=False)
        print(f"\nWrote {a.out}")
        return

    if a.compare:
        print("\n=== DIMENSION STABILITY BETWEEN SNAPSHOTS ===")
        cmp_df = compare_snapshots(dev, s3, a.bucket, lo, hi)
        with pd.ExcelWriter(a.out, engine="openpyxl") as xl:
            if len(cmp_df):
                cmp_df.to_excel(xl, sheet_name="Dimension drift", index=False)
            else:
                pd.DataFrame(
                    [["no comparison produced",
                      "See the terminal output for the reason. Most often: only "
                      "one file in the window carries that delivery date. A "
                      "LAST-7-DAYS file excludes its own drop day, so widen "
                      "--start/--end by a few days."]],
                    columns=["Measure", "Value"]).to_excel(
                    xl, sheet_name="Summary", index=False)
        print(f"\nWrote {a.out}")
        return

    if mb > a.max_mb:
        sys.exit(f"\n  {mb:,.0f} MB exceeds the {a.max_mb:,.0f} MB limit. Narrow "
                 f"--start/--end, lower --max-files, or raise --max-mb.\n")

    dv = scan_files(dev, s3, a.bucket, lo, hi, "DEVICE")
    rf = scan_files(ref, s3, a.bucket, lo, hi, "REFERENCE") if ref else None

    n_i, c_i = dv["naive_impressions"], dv["dedup_impressions"]
    overlap = pd.DataFrame([{"delivery_date": d, "files_containing_it": len(f),
                             "files": ", ".join(sorted(f))}
                            for d, f in sorted(dv["date_files"].items())])
    print("\n" + "=" * 78)
    print("1. ROLLING-WINDOW OVERLAP")
    if len(overlap):
        print(f"   delivery dates covered : {len(overlap)}")
        print(f"   files per date         : min {overlap.files_containing_it.min()} "
              f"median {int(overlap.files_containing_it.median())} "
              f"max {overlap.files_containing_it.max()}")
    print("\n2. DUPLICATION WITHIN THE FILES AS DELIVERED")
    print(f"   naive concatenation    : {n_i:>15,} impressions")
    print(f"   correctly de-duplicated: {c_i:>15,} impressions")
    print(f"   >>> inflation factor   : {(n_i / c_i) if c_i else 0:.3f}x")
    print(f"   exact repeated rows    : {dv['exact_duplicate_rows']:>15,}")
    print(f"   restated rows (same key, newer numbers): {dv['restated_rows']:,}")
    print("   De-duplication keys on EVERY dimension column in the schema above.")

    recon = pd.DataFrame()
    if rf:
        d_adv, r_adv = dv["per_advertiser"], rf["per_advertiser"]
        rows = []
        for name in sorted(set(d_adv) | set(r_adv)):
            di, ri = d_adv[name]["impressions"], r_adv[name]["impressions"]
            dc, rc = d_adv[name]["clicks"], r_adv[name]["clicks"]
            rows.append({"advertiser": name, "impressions_device": di,
                         "impressions_reference": ri, "impr_diff": di - ri,
                         "impr_pct": ((di - ri) / ri) if ri else None,
                         "clicks_device": dc, "clicks_reference": rc,
                         "click_pct": ((dc - rc) / rc) if rc else None})
        recon = pd.DataFrame(rows)
        recon["same_factor_as_clicks"] = (
            recon.impr_pct.notna() & recon.click_pct.notna() &
            (recon.impr_pct > 0.005) & ((recon.impr_pct - recon.click_pct).abs() <= 0.10))
        recon = recon.sort_values("impr_diff", key=lambda s: s.abs(), ascending=False)
        off = recon[recon.impr_pct.abs() > 0.005]
        tot_r = recon.impressions_reference.sum()
        print("\n3. DEVICE vs REFERENCE PER ADVERTISER (both de-duplicated)")
        print(f"   advertisers compared    : {len(recon)}")
        print(f"   out of tolerance (>0.5%): {len(off)}")
        print(f"   net difference          : {int(recon.impr_diff.sum()):+,} "
              f"({(recon.impr_diff.sum() / tot_r) if tot_r else 0:+.3%})")
        print(f"   GROSS difference        : {int(recon.impr_diff.abs().sum()):,} "
              f"({(recon.impr_diff.abs().sum() / tot_r) if tot_r else 0:.3%})")
        if len(recon) and len(off) / len(recon) > 0.5:
            print("\n   *** NOT LIKE-FOR-LIKE — most advertisers disagree, which points to")
            print("       a source or window mismatch, not that many separate faults.")
        for _, r in off.head(12).iterrows():
            tag = "  <- duplicated-rows shape" if r.same_factor_as_clicks else ""
            pct = r.impr_pct if pd.notna(r.impr_pct) else 0
            print(f"     {str(r.advertiser)[:42]:44s} device {int(r.impressions_device):>10,}"
                  f"  ref {int(r.impressions_reference):>10,}  {pct:+7.2%}{tag}")

    dr = pd.DataFrame()
    if a.advertiser and a.date:
        day = pd.to_datetime(a.date).date()
        print(f"\n4. DRILL-DOWN  {a.advertiser} · {day}")
        dr = drill(dev, s3, a.bucket, a.advertiser, day)
        if len(dr):
            dr = dr[dr["rows"] > 0]
        if not len(dr):
            print("   no rows — check the advertiser spelling and the date range")
        else:
            for _, r in dr.iterrows():
                print(f"     {r.file[:54]:56s} {int(r.impressions):>9,} impr  "
                      f"{int(r.clicks):>5,} clicks  {int(r['rows']):>6,} rows")
            vals = sorted(set(dr.impressions))
            print("   ---")
            print(f"   naive sum across files : {int(dr.impressions.sum()):>9,}")
            print(f"   each file individually : {', '.join('{:,}'.format(v) for v in vals)}")
            if len(vals) == 1:
                print(f"   >>> every file agrees: {vals[0]:,} impressions is AdLib's "
                      f"figure for this advertiser/date.")
            print("   >>> COMPARE THAT to what the warehouse reports for the same day.")

    with pd.ExcelWriter(a.out, engine="openpyxl") as xl:
        def put(df, name):
            try:
                if df is not None and len(df):
                    d = df.copy()
                    for c in d.columns:      # Excel rejects tz-aware datetimes
                        try:
                            if getattr(d[c].dtype, "tz", None) is not None:
                                d[c] = d[c].dt.tz_localize(None)
                        except (AttributeError, TypeError):
                            pass
                    d.to_excel(xl, sheet_name=name[:31], index=False)
            except Exception as e:
                print(f"  ! could not write sheet '{name}': {e}")

        summary = [["Script version", __version__],
                   ["Device files read", len(dev)], ["Reference files read", len(ref)],
                   ["Window start", str(lo)], ["Window end", str(hi)],
                   ["Naive impressions", n_i], ["De-duplicated impressions", c_i],
                   ["Inflation factor", round(n_i / c_i, 4) if c_i else 0],
                   ["Exact duplicate rows", dv["exact_duplicate_rows"]],
                   ["Restated rows", dv["restated_rows"]]]
        for n, k, ratio, sz in suspect:
            summary.append(["UNDERSIZED: " + n, f"{sz:,.1f} MB = {ratio:.0%} of typical"])
        put(pd.DataFrame(summary, columns=["Measure", "Value"]), "Summary")
        put(pd.DataFrame(dv["per_file"]), "Device files")
        if rf:
            put(pd.DataFrame(rf["per_file"]), "Reference files")
        put(overlap, "Date overlap")
        put(recon, "Device vs reference")
        put(dr, "Drill-down")
        if dv["schema"]:
            put(pd.DataFrame({"column": dv["schema"]["columns"],
                              "treated_as": ["metric" if c in dv["schema"]["metrics"]
                                             else "dimension"
                                             for c in dv["schema"]["columns"]]}),
                "Device schema")
    print(f"\nWrote {a.out} — no credentials in it.")


if __name__ == "__main__":
    main()

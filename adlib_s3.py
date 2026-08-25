"""
adlib_s3.py
Read AdLib's own S3 bucket directly, instead of waiting on a manual TapClicks
export.

Why this exists: everything the dashboard sees today has been through TapClicks
ingestion, and the July 2026 dispute was caused *by* that ingestion — 22-31 July
counted twice, invisible until someone exported a month and compared it by hand.
Reading AdLib's files gives an independent copy of the same delivery, so the two
can be reconciled automatically and a repeat gets caught the day it happens.

Four file kinds live in that bucket:

  Campaign_Creative_Report_Last_7_Days_2026-08-14_16.csv   creative grain
  Vici_Device_Report_Last_7_Days_2026_08_14.csv            device grain
  Campaign_Report_Last_7_Days_2026-08-14_16.csv            campaign grain
  Ad Previews 2026-08-14T08:02:26.csv                      creative asset list

Three things about these files drive the whole module:

1. **They are rolling LAST-7-DAYS windows dropped daily**, so one delivery date
   sits in up to seven files. Reading them all and concatenating multiplies
   delivery by ~7. `cover_set()` picks a MINIMAL NON-OVERLAPPING set instead —
   a file stamped D holds D-7..D-1 complete, so five files cover a month rather
   than thirty-one, and no date is read twice.
2. **A file does not contain its own drop day.** The 15 July file has zero
   15 July rows; the complete day first appears in the 16 July file. Anything
   asking for "today" from today's file gets nothing.
3. **They are ~150 MB each.** Every read is streamed in chunks with an explicit
   column list; nothing loads a whole file into memory.

Credentials are separate from the app's own S3 (this is AdLib's bucket, a
different key pair): ADLIB_AWS_ACCESS_KEY_ID / ADLIB_AWS_SECRET_ACCESS_KEY, or
ADLIB_AWS_PROFILE, falling back to the ambient AWS credentials if neither is
set. Read-only — nothing here writes to the bucket.

Run it directly to check the wiring from a machine that has the credentials:

    python adlib_s3.py --check                  # list + classify, downloads nothing
    python adlib_s3.py --previews               # preview coverage, today
    python adlib_s3.py --creative --days 7      # creative pull, report the window
"""
import datetime as dt
import io
import os
import re

import pandas as pd

BUCKET = os.environ.get("ADLIB_S3_BUCKET", "adlib-vici")
PREFIX = os.environ.get("ADLIB_S3_PREFIX", "")
WINDOW_DAYS = int(os.environ.get("ADLIB_WINDOW_DAYS", "7"))

# Filename substrings that identify each report, matched case- and
# separator-insensitively so a rename to 'ad-previews' or 'AdPreviews' keeps
# working. Override any of them with the matching env var.
MATCH = {
    "creative": os.environ.get("ADLIB_CREATIVE_MATCH", "campaign_creative_report"),
    "previews": os.environ.get("ADLIB_PREVIEW_MATCH", "adpreviews,ad previews"),
    "device": os.environ.get("ADLIB_DEVICE_MATCH", "device_report"),
    "campaign": os.environ.get("ADLIB_CAMPAIGN_MATCH", "campaign_report"),
}


def _canon(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


_PATS = {k: [_canon(p) for p in v.split(",") if p.strip()] for k, v in MATCH.items()}


def kind_of(name):
    """Which report a filename is, or None. Order matters: every
    Campaign_Creative_Report is also a 'campaign_report' by substring, so the
    more specific kinds are tested first."""
    n = _canon(name)
    for kind in ("creative", "previews", "device", "campaign"):
        if any(p in n for p in _PATS[kind]):
            return kind
    return None


_FNAME_DATE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


def date_in_name(name):
    m = _FNAME_DATE.search(str(name).rsplit("/", 1)[-1])
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# --------------------------------------------------------------- S3 access
def client():
    """boto3 S3 client for AdLib's bucket, using its own credentials if given."""
    import boto3
    key = os.environ.get("ADLIB_AWS_ACCESS_KEY_ID")
    sec = os.environ.get("ADLIB_AWS_SECRET_ACCESS_KEY")
    prof = os.environ.get("ADLIB_AWS_PROFILE")
    region = os.environ.get("ADLIB_AWS_REGION") or os.environ.get("AWS_REGION") or None
    if key and sec:
        return boto3.client("s3", aws_access_key_id=key, aws_secret_access_key=sec,
                            region_name=region)
    if prof:
        return boto3.Session(profile_name=prof).client("s3", region_name=region)
    return boto3.client("s3", region_name=region)


def configured():
    """True when this deployment has been pointed at AdLib's bucket. The UI uses
    it to decide whether to offer the direct pull at all."""
    return bool(BUCKET) and bool(
        os.environ.get("ADLIB_AWS_ACCESS_KEY_ID") or os.environ.get("ADLIB_AWS_PROFILE")
        or os.environ.get("AWS_ACCESS_KEY_ID"))


def list_objects(s3=None, bucket=None, prefix=None):
    """[{name, key, modified, size, kind, stamp}] for everything in the bucket."""
    s3 = s3 or client()
    bucket = bucket or BUCKET
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix if prefix is not None else PREFIX):
        for o in page.get("Contents", []):
            name = o["Key"].rsplit("/", 1)[-1]
            out.append({"name": name, "key": o["Key"], "size": o["Size"],
                        "modified": o["LastModified"].replace(tzinfo=None),
                        "kind": kind_of(name), "stamp": date_in_name(name)})
    return sorted(out, key=lambda m: m["modified"])


def _get(s3, bucket, key):
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


# ------------------------------------------------------- choosing the files
def cover_set(metas, start, end, window=WINDOW_DAYS):
    """Minimal non-overlapping files covering [start, end], newest data winning.

    A file stamped D holds D-window..D-1 complete. Walking forward and taking
    the LATEST file that still reaches back to the first uncovered day gives
    full coverage with no date read twice — five files for a month, not
    thirty-one. Undersized files (under half the median) are skipped when a
    full-size alternative exists: the truncated 7/29 and 8/02 drops in this
    bucket are partial, and silently reading one produces a dip that looks like
    a delivery drop.

    Returns [(meta, (from_date, to_date), full_size_bool)].
    """
    dated = [m for m in metas if m.get("stamp")]
    if not dated:
        return []
    sizes = sorted(m["size"] for m in dated)
    med = sizes[len(sizes) // 2] if sizes else 0
    plan, cur = [], start
    while cur <= end:
        cands = [m for m in dated
                 if cur + dt.timedelta(days=1) <= m["stamp"] <= cur + dt.timedelta(days=window)]
        full = [m for m in cands if not (med and m["size"] < 0.5 * med)]
        pool = full or cands
        if not pool:
            cur += dt.timedelta(days=1)
            continue
        m = max(pool, key=lambda x: x["stamp"])
        span = (max(cur, m["stamp"] - dt.timedelta(days=window)),
                min(end, m["stamp"] - dt.timedelta(days=1)))
        plan.append((m, span, m in full))
        cur = m["stamp"]
    return plan


def complete_through(metas, kind=None, window=WINDOW_DAYS):
    """Latest delivery date the bucket can answer for completely.

    The newest file excludes its own drop day, so the answer is its stamp minus
    one — NOT today. Reporting 'data through today' off a file that contains no
    rows for today is how a dashboard shows a fake cliff every morning.
    """
    dated = [m for m in metas if m.get("stamp") and (kind is None or m["kind"] == kind)]
    if not dated:
        return None
    return max(m["stamp"] for m in dated) - dt.timedelta(days=1)


# ------------------------------------------------------------- the readers
# AdLib header -> the Title Case names the engines already use. Canonical
# matching, so 'Creative Clickthrough Url' and 'creative_clickthrough_url' both
# land here.
_CREATIVE_MAP = {
    "date": "Date",
    "advertisername": "Client", "advertiserid": "Advertiser ID",
    "campaignname": "Campaign Name", "campaignid": "Campaign ID",
    "campaignpoolid": "Campaign Pool ID", "campaignpoolname": "Campaign Pool Name",
    "creativeid": "Creative ID", "creativename": "Creative Name",
    "creativeexternalname": "Creative External Name",
    "creativesize": "Creative Size", "creativetype": "Creative Type",
    "creativeclickthroughurl": "Clickthrough URL", "previewlink": "Preview Image URL",
    "impressions": "Impressions", "clicks": "Clicks", "ctr": "CTR", "cpm": "CPM",
    "billablespend": "Billable Spend",
    "postclickconversions": "Post Click Conversions",
    "postviewconversions": "Post View Conversions",
    "videofirstquartile": "25% Completed", "videomidpoint": "50% Completed",
    "videothirdquartile": "75% Completed", "videocomplete": "100% Completed",
    "creativestartdate": "Creative Start Date", "creativeenddate": "Creative End Date",
}
_PREVIEW_MAP = {
    "creativeid": "Creative ID", "creativename": "Creative Name",
    "creativetype": "Creative Type", "advertiserid": "Advertiser ID",
    "advertisername": "Client", "campaignid": "Campaign ID",
    "campaignname": "Campaign Name", "campaignpoolid": "Campaign Pool ID",
    "campaignpoolname": "Campaign Pool Name",
    "previewimageurl": "Preview Image URL", "domain": "Domain",
    "creativecreatedat": "Created At", "creativeupdatedat": "Updated At",
    "currentflightstartdate": "Flight Start", "currentflightend": "Flight End",
    "currentflightenddate": "Flight End",
}
_DEVICE_MAP = {
    "date": "Date", "advertisername": "Client", "advertiserid": "Advertiser ID",
    "campaignname": "Campaign Name", "campaignid": "Campaign ID",
    "campaignpoolid": "Campaign Pool ID",
    "devicetype": "Device Type", "devicemake": "Device Make",
    "devicemodel": "Device Model", "operatingsystem": "Operating System",
    "browser": "Browser", "impressions": "Impressions", "clicks": "Clicks",
    "billablespend": "Billable Spend",
}
_MEASURES = ("Impressions", "Clicks", "Post Click Conversions", "Post View Conversions",
             "Billable Spend", "CTR", "CPM", "25% Completed", "50% Completed",
             "75% Completed", "100% Completed")

# Products are not columns in AdLib's export — they live inside the campaign
# pool name ('… - Social Mirror - 99971'). Longest name first so 'Social Mirror
# CTV' is never truncated to 'Social Mirror'.
_PRODUCTS = ["Social Mirror CTV", "Native Display", "Native Video", "Online Audio",
             "Connected TV", "Streaming Audio", "Social Mirror", "Display", "CTV",
             "Video", "Audio", "Geo-Framing", "Geo-Fencing", "Website Visitor ID",
             "Performance Max", "Pay-Per-Click", "YouTube"]
_PROD_RE = re.compile("|".join(re.escape(p) for p in _PRODUCTS), re.I)
_PROD_CANON = {_canon(p): p for p in _PRODUCTS}


def infer_product(text):
    """Product 2 read out of the campaign / pool name. AdLib doesn't send the
    column; the dashboard's CTR norms are per product, so a wrong guess is worse
    than none — anything unrecognized stays '(not in export)'."""
    m = _PROD_RE.search(str(text or ""))
    return _PROD_CANON.get(_canon(m.group(0)), "(not in export)") if m else "(not in export)"


def _read_csv(data, colmap, want=None, chunksize=200_000, lo=None, hi=None):
    """Stream one AdLib CSV, keeping only mapped columns, filtered to a window."""
    head = pd.read_csv(io.BytesIO(data), nrows=0)
    # First header wins a target name. Two source columns mapping to the same
    # engine column would otherwise produce two identically-labelled columns,
    # and df['X'] then returns a DataFrame — the failure is loud but far away.
    ren, seen = {}, set()
    for c in head.columns:
        tgt = colmap.get(_canon(c))
        if tgt and tgt not in seen:
            ren[c] = tgt
            seen.add(tgt)
    if not ren:
        return pd.DataFrame()
    keep = list(ren)
    if want:
        keep = [c for c in keep if ren[c] in want]
        if not keep:
            return pd.DataFrame()
    dtypes = {c: str for c in keep if ren[c] not in _MEASURES}
    dcol = next((c for c, v in ren.items() if v == "Date"), None)
    frames = []
    for chunk in pd.read_csv(io.BytesIO(data), usecols=keep, dtype=dtypes,
                             chunksize=chunksize, low_memory=False):
        chunk = chunk.rename(columns=ren)
        if dcol and (lo or hi):
            d = pd.to_datetime(chunk["Date"], errors="coerce").dt.date
            if lo:
                chunk = chunk[d >= lo]
                d = d[d >= lo]
            if hi:
                chunk = chunk[d <= hi]
        if len(chunk):
            frames.append(chunk)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _finish(df):
    """Numeric coercion, product inference, and the engine columns AdLib omits."""
    if not len(df):
        return df
    for c in _MEASURES:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    if "Product 2" not in df.columns:
        src = ("Campaign Pool Name" if "Campaign Pool Name" in df.columns
               else "Creative Name" if "Creative Name" in df.columns else None)
        df["Product 2"] = df[src].map(infer_product) if src else "(not in export)"
    for c in ("Client Business Unit", "Strategy Type", "Strategy Name"):
        if c not in df.columns:
            df[c] = "(not in export)"
    if "DSP" not in df.columns:
        df["DSP"] = "AdLib"
    return df


def _pull(kind, days=None, start=None, end=None, s3=None, bucket=None, metas=None,
          colmap=None, on_file=None):
    """Shared body: choose a covering set, read each file over its own span."""
    s3 = s3 or client()
    bucket = bucket or BUCKET
    metas = metas if metas is not None else list_objects(s3, bucket)
    files = [m for m in metas if m["kind"] == kind]
    if not files:
        return pd.DataFrame(), {"files": [], "error": f"no {kind} files in the bucket"}
    end = end or complete_through(files) or dt.date.today() - dt.timedelta(days=1)
    start = start or (end - dt.timedelta(days=(days or WINDOW_DAYS) - 1))
    plan = cover_set(files, start, end)
    used, frames = [], []
    for m, (s, e), full in plan:
        df = _read_csv(_get(s3, bucket, m["key"]), colmap, lo=s, hi=e)
        used.append({"file": m["name"], "from": str(s), "to": str(e),
                     "rows": int(len(df)), "full_size": bool(full),
                     "mb": round(m["size"] / 1e6, 1)})
        if on_file:
            on_file(used[-1])
        if len(df):
            frames.append(df)
    out = _finish(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    meta = {"files": used, "start": str(start), "end": str(end),
            "rows": int(len(out)),
            "impressions": int(out["Impressions"].sum()) if "Impressions" in out else 0,
            "partial_files": [u["file"] for u in used if not u["full_size"]]}
    return out, meta


def fetch_creative(days=None, start=None, end=None, s3=None, bucket=None, metas=None,
                   on_file=None):
    """Creative-grain delivery straight from AdLib, shaped for creative_engine."""
    return _pull("creative", days, start, end, s3, bucket, metas, _CREATIVE_MAP, on_file)


def fetch_device(days=None, start=None, end=None, s3=None, bucket=None, metas=None,
                 on_file=None):
    """Device-grain delivery straight from AdLib, shaped for device_engine."""
    return _pull("device", days, start, end, s3, bucket, metas, _DEVICE_MAP, on_file)


def fetch_previews(s3=None, bucket=None, metas=None):
    """The newest Ad Previews export — the creative asset list, not delivery.

    It carries no Date column and is a full snapshot each time, so only the
    newest file is read; older ones are strictly stale.
    """
    s3 = s3 or client()
    bucket = bucket or BUCKET
    metas = metas if metas is not None else list_objects(s3, bucket)
    files = [m for m in metas if m["kind"] == "previews"]
    if not files:
        return pd.DataFrame(), {"error": "no Ad Previews file in the bucket"}
    m = max(files, key=lambda x: x["modified"])
    df = _read_csv(_get(s3, bucket, m["key"]), _PREVIEW_MAP)
    if len(df) and "Creative ID" in df.columns:
        df["Creative ID"] = df["Creative ID"].astype(str).str.strip().str.replace(
            r"\.0$", "", regex=True)
    return df, {"file": m["name"], "modified": str(m["modified"]),
                "rows": int(len(df)),
                "creatives": int(df["Creative ID"].nunique()) if "Creative ID" in df else 0}


# ------------------------------------------------------------------- CLI
def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Check the AdLib S3 wiring.")
    ap.add_argument("--check", action="store_true", help="list and classify, no downloads")
    ap.add_argument("--previews", action="store_true")
    ap.add_argument("--creative", action="store_true")
    ap.add_argument("--device", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--bucket", default=BUCKET)
    a = ap.parse_args()

    s3 = client()
    metas = list_objects(s3, a.bucket)
    print(f"s3://{a.bucket}/{PREFIX}  {len(metas):,} object(s)")
    counts = {}
    for m in metas:
        counts[m["kind"]] = counts.get(m["kind"], 0) + 1
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        newest = max((m for m in metas if m["kind"] == k), key=lambda x: x["modified"])
        through = complete_through(metas, k)
        print(f"  {str(k):10s} {n:>5} file(s)   newest {newest['name'][:46]:48s}"
              f" complete through {through}")
    if a.check:
        end = complete_through(metas, "creative") or dt.date.today()
        plan = cover_set([m for m in metas if m["kind"] == "creative"],
                         end - dt.timedelta(days=a.days - 1), end)
        print(f"\nA {a.days}-day creative pull would read {len(plan)} file(s), "
              f"{sum(m['size'] for m, _, _ in plan)/1e6:,.0f} MB:")
        for m, (s, e), full in plan:
            print(f"  {m['name'][:52]:54s} {s} → {e}"
                  + ("" if full else "   ! undersized, may be partial"))
        return
    if a.previews:
        df, meta = fetch_previews(s3, a.bucket, metas)
        print(f"\npreviews: {meta}")
    if a.creative:
        df, meta = fetch_creative(days=a.days, s3=s3, bucket=a.bucket, metas=metas,
                                  on_file=lambda u: print(f"  read {u['file'][:52]:54s} "
                                                          f"{u['rows']:>8,} rows"))
        print(f"\ncreative: {meta['rows']:,} rows, {meta['impressions']:,} impressions "
              f"({meta['start']} → {meta['end']})")
    if a.device:
        df, meta = fetch_device(days=a.days, s3=s3, bucket=a.bucket, metas=metas)
        print(f"\ndevice: {meta['rows']:,} rows, {meta['impressions']:,} impressions")


if __name__ == "__main__":
    _cli()

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
    "previews": os.environ.get("ADLIB_PREVIEW_MATCH", "adpreviews,ad previews,preview"),
    "device": os.environ.get("ADLIB_DEVICE_MATCH", "device_report"),
    "campaign": os.environ.get("ADLIB_CAMPAIGN_MATCH", "campaign_report"),
    "screenshot": os.environ.get("ADLIB_SCREENSHOT_MATCH", "screenshot"),
}

# The bucket is organised one folder per data view — the same directories the
# TapClicks S3 connector is pointed at. Listing the whole bucket means paging
# 6,000+ objects to find the handful that matter, and it makes filename matching
# do work the folder already did. Each report is listed under its own prefix;
# set any of these to "" to fall back to scanning from PREFIX.
PREFIXES = {
    "creative": os.environ.get("ADLIB_CREATIVE_PREFIX", "performance/creatives"),
    "previews": os.environ.get("ADLIB_PREVIEW_PREFIX", "preview_link_files"),
    "device": os.environ.get("ADLIB_DEVICE_PREFIX", "device"),
    "campaign": os.environ.get("ADLIB_CAMPAIGN_PREFIX", "performance/campaigns"),
    "screenshot": os.environ.get("ADLIB_SCREENSHOT_PREFIX", "screenshot_files"),
    "sites": os.environ.get("ADLIB_SITE_PREFIX", "site-domain"),
    "apps": os.environ.get("ADLIB_APP_PREFIX", "app"),
    "ttddv": os.environ.get("ADLIB_TTDDV_PREFIX", "app_TTD-DV"),
}
# The bucket also holds a testing/ tree (testing/site-domain, testing/app,
# testing/geo…) whose newest files are from Nov 2025 - Jan 2026. Reading it
# would quietly mix months-old delivery into a current report, so every prefix
# above is exact and nothing globs.
# Other directories in the same bucket, not read yet but worth naming so the
# next person doesn't have to rediscover them: site-domain, app, app_TTD-DV,
# publisher, audience, pixel, dooh, geo/city, geo/region, geo/zip,
# performance/reach_frequency. The screenshot_files folder is shared by three
# data views, two of them marked inactive — ignore those.


def _norm_prefix(p):
    """'/device' and 'device' and 'device/' all mean the same folder."""
    p = str(p or "").strip().lstrip("/")
    return (p.rstrip("/") + "/") if p else ""


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


def _list_prefix(s3, bucket, prefix, kind=None):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            if o["Key"].endswith("/"):
                continue                      # folder placeholder object
            name = o["Key"].rsplit("/", 1)[-1]
            out.append({"name": name, "key": o["Key"], "size": o["Size"],
                        "modified": o["LastModified"].replace(tzinfo=None),
                        # The folder is the authority on what a file is; the
                        # filename is only consulted when it isn't in a known one.
                        "kind": kind or kind_of(name), "stamp": date_in_name(name),
                        "prefix": prefix})
    return out


def list_objects(s3=None, bucket=None, prefix=None, kinds=None):
    """[{name, key, modified, size, kind, stamp, prefix}] for the reports we read.

    Lists each report's own folder rather than the whole bucket — one LIST per
    report instead of paging thousands of objects, and a file's folder decides
    what it is. Falls back to a full scan when no prefixes are configured, or
    when an explicit `prefix` is passed.
    """
    s3 = s3 or client()
    bucket = bucket or BUCKET
    if prefix is not None:
        return sorted(_list_prefix(s3, bucket, prefix), key=lambda m: m["modified"])
    wanted = list(kinds) if kinds else [k for k in PREFIXES if k != "screenshot"]
    seen, out = set(), []
    for kind in wanted:
        pre = _norm_prefix(PREFIXES.get(kind, ""))
        if not pre:
            continue
        for m in _list_prefix(s3, bucket, _norm_prefix(PREFIX) + pre, kind):
            if m["key"] not in seen:
                seen.add(m["key"])
                out.append(m)
    if not out:
        # No prefixes matched anything — either they aren't set, or the bucket
        # is flat. Scan and classify by filename, as before.
        out = [m for m in _list_prefix(s3, bucket, _norm_prefix(PREFIX)) if m["kind"]]
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
_SITE_MAP = {
    "date": "Date", "advertisername": "Client", "advertiserid": "Advertiser ID",
    "campaignname": "Campaign Name", "campaignid": "Campaign ID",
    "campaignpoolid": "Campaign Pool ID", "campaignpoolname": "Campaign Pool Name",
    "sitedomain": "Site Domain",
    "impressions": "Impressions", "clicks": "Clicks", "ctr": "CTR", "cpm": "CPM",
    "billablespend": "Billable Spend",
    "postclickconversions": "Post Click Conversions",
    "postviewconversions": "Post View Conversions",
}
_APP_MAP = dict(_SITE_MAP, **{
    "appid": "App ID", "appname": "App Name", "devicetype": "Device Type",
})
_APP_MAP.pop("sitedomain", None)
# TTD/DV360 ships one combined column ('App/URL') holding either a domain or an
# app name; tap_adapter.split_ttddv separates them downstream.
_TTDDV_MAP = dict(_SITE_MAP, **{"appurl": "App/URL"})
_TTDDV_MAP.pop("sitedomain", None)

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


# The previews folder holds 334 files as of Aug 2026 (160 MB total) — small,
# incremental drops, several per day, going back to April. The cap has to clear
# that comfortably or coverage is understated: pooling 60 of 334 is the same bug
# as reading only the newest one, just less obvious.
PREVIEW_MAX_FILES = int(os.environ.get("ADLIB_PREVIEW_MAX_FILES", "600"))

# Pooling 160 MB on every page load is wasteful when the folder changes a few
# times a day. Cache on what the folder actually looks like — newest file plus
# file count — so a new drop invalidates it and nothing else does.
_PREVIEW_CACHE = {}


def fetch_sites(days=None, start=None, end=None, s3=None, bucket=None, metas=None,
                on_file=None):
    """Site-domain delivery from AdLib, shaped for the placement engines."""
    return _pull("sites", days, start, end, s3, bucket, metas, _SITE_MAP, on_file)


def fetch_apps(days=None, start=None, end=None, s3=None, bucket=None, metas=None,
               on_file=None):
    """App delivery from AdLib. `Final App Name` is what the block list reads, and
    AdLib has no such column — the raw App Name is copied into it rather than
    left blank, so a placement never scores as 'Blank / Missing Name' purely
    because the source changed."""
    df, meta = _pull("apps", days, start, end, s3, bucket, metas, _APP_MAP, on_file)
    if len(df) and "App Name" in df.columns and "Final App Name" not in df.columns:
        df["Final App Name"] = df["App Name"]
    return df, meta


def fetch_ttddv(days=None, start=None, end=None, s3=None, bucket=None, metas=None,
                on_file=None):
    """The TTD/DV360 combined site+app file, split downstream by split_ttddv."""
    return _pull("ttddv", days, start, end, s3, bucket, metas, _TTDDV_MAP, on_file)


def fetch_previews(s3=None, bucket=None, metas=None, max_files=None):
    """The Ad Previews exports POOLED — the creative asset list, not delivery.

    Reading only the newest file is wrong unless each drop is a complete
    snapshot, and there is no way to tell from one file that it is. The 14 Aug
    2026 drop held 1,043 creatives against 3,235 delivering, and every creative
    in it had a flight ending on or after the drop date — consistent with a
    partial or filtered extract. Under-reading here shows up as creatives
    "missing a preview" that in fact have one in another drop, so this pools
    every previews file (newest first, capped) and keeps ONE row per
    Creative ID + URL, newest winning.

    Returns (df, meta); meta['files_read'] vs meta['files_available'] says
    whether the cap truncated the pool.
    """
    s3 = s3 or client()
    bucket = bucket or BUCKET
    metas = metas if metas is not None else list_objects(s3, bucket)
    files = sorted([m for m in metas if m["kind"] == "previews"],
                   key=lambda x: x["modified"], reverse=True)
    if not files:
        return pd.DataFrame(), {"error": "no Ad Previews file in the bucket",
                                "files_available": 0, "files_read": 0}
    cap = max_files or PREVIEW_MAX_FILES
    ckey = (bucket, files[0]["key"], str(files[0]["modified"]), len(files), cap)
    if ckey in _PREVIEW_CACHE:
        df, meta = _PREVIEW_CACHE[ckey]
        return df.copy(), dict(meta, cached=True)
    frames, read = [], []
    for m in files[:cap]:
        df = _read_csv(_get(s3, bucket, m["key"]), _PREVIEW_MAP)
        if len(df):
            df["_src"] = m["name"]
            frames.append(df)
        read.append({"file": m["name"], "modified": str(m["modified"]),
                     "rows": int(len(df))})
    if not frames:
        return pd.DataFrame(), {"error": "previews files were unreadable",
                                "files_available": len(files), "files_read": len(read)}
    out = pd.concat(frames, ignore_index=True)
    if "Creative ID" in out.columns:
        out["Creative ID"] = out["Creative ID"].astype(str).str.strip().str.replace(
            r"\.0$", "", regex=True)
        sub = ["Creative ID"] + [c for c in ("Preview Image URL",) if c in out.columns]
        out = out.drop_duplicates(subset=sub, keep="first")   # newest file read first
    meta = {"file": files[0]["name"], "modified": str(files[0]["modified"]),
            "files_available": len(files), "files_read": len(read),
            "truncated": len(files) > cap, "cached": False,
            "oldest_read": read[-1]["modified"] if read else None,
            "rows": int(len(out)),
            "creatives": int(out["Creative ID"].nunique()) if "Creative ID" in out else 0}
    _PREVIEW_CACHE.clear()          # only the current folder state is worth keeping
    _PREVIEW_CACHE[ckey] = (out.copy(), meta)
    return out, meta


# ------------------------------------------------------------------- CLI
def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Check the AdLib S3 wiring.")
    ap.add_argument("--check", action="store_true", help="list and classify, no downloads")
    ap.add_argument("--folders", action="store_true",
                    help="list the bucket's top-level folders and what we map them to")
    ap.add_argument("--previews", action="store_true")
    ap.add_argument("--creative", action="store_true")
    ap.add_argument("--device", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--bucket", default=BUCKET)
    a = ap.parse_args()

    s3 = client()
    if a.folders:
        # Cheap directory listing: one delimited LIST, no recursion into the
        # thousands of files underneath. Confirms the folder names before any
        # prefix is trusted.
        mapped = {_norm_prefix(v): k for k, v in PREFIXES.items() if v}
        print(f"s3://{a.bucket}/{PREFIX}  top-level folders")
        stack = [_norm_prefix(PREFIX)]
        while stack:
            base = stack.pop(0)
            resp = s3.list_objects_v2(Bucket=a.bucket, Prefix=base, Delimiter="/")
            for cp in resp.get("CommonPrefixes", []):
                p = cp["Prefix"]
                role = mapped.get(p)
                print(f"  {p:34s} {'-> ' + role if role else ''}")
                if p.count("/") < 2:          # one level down (performance/…, geo/…)
                    stack.append(p)
        return
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

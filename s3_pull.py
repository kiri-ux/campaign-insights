"""
s3_pull.py
Read the newest enriched Insights export that TapClicks drops into an S3 prefix.
No email, no attachment handoff — just list the prefix and grab the latest object.

Env vars:
  S3_BUCKET             bucket name
  S3_PREFIX             key prefix/folder, e.g. 'tapclicks/insights/'  (optional)
  S3_SUFFIX             filter by extension, default '.xlsx' (use '.csv' if CSV)
  AWS_ACCESS_KEY_ID     read-only key scoped to that prefix
  AWS_SECRET_ACCESS_KEY
  AWS_REGION            e.g. 'us-east-1'  (optional; boto3 picks up default)
"""
import os
import datetime


def _date_from_key(key, last_modified):
    """Prefer an 8-digit date in the filename (YYYYMMDD or YYYY-MM-DD); else the
    object's LastModified date."""
    import re
    base = key.rsplit("/", 1)[-1]
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", base)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    if last_modified:
        return last_modified.date().strftime("%Y-%m-%d")
    return datetime.date.today().strftime("%Y-%m-%d")


def _client_and_cfg():
    import boto3
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET not set")
    region = os.environ.get("AWS_REGION", "").strip() or None
    return boto3.client("s3", region_name=region), bucket


def list_range(start_iso, end_iso, grace_days=None):
    """List sites/apps files whose per-file date (filename date, else LastModified)
    falls in [start, end + grace_days]. Exports are rolling multi-day windows
    dropped AFTER the delivery they contain, so the grace (default 8 days,
    override with S3_RANGE_GRACE_DAYS) catches files dropped up to a week+ after
    the requested window that still carry in-range delivery — rows get filtered
    to the exact range downstream. Returns (site_metas, app_metas, capped):
    metas are [{'name','key','date'}] oldest-first (so later files win de-dupe);
    capped=True if the S3_RANGE_MAX_FILES cap (default 16 per side) trimmed the
    oldest files out."""
    import datetime as _dt
    if grace_days is None:
        grace_days = int(os.environ.get("S3_RANGE_GRACE_DAYS", "8"))
    start = _dt.date.fromisoformat(start_iso)
    end = _dt.date.fromisoformat(end_iso) + _dt.timedelta(days=grace_days)
    s3, bucket = _client_and_cfg()
    prefix = os.environ.get("S3_PREFIX", "").strip()
    suffix = os.environ.get("S3_SUFFIX", ".xlsx").strip().lower()
    site_match = os.environ.get("S3_SITES_MATCH", "site").strip().lower()
    app_match = os.environ.get("S3_APPS_MATCH", "app").strip().lower()
    cap = int(os.environ.get("S3_RANGE_MAX_FILES", "16"))

    sites, apps = [], []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not key.lower().endswith(suffix):
                continue
            fdate = _dt.date.fromisoformat(_date_from_key(key, obj.get("LastModified")))
            if not (start <= fdate <= end):
                continue
            base = key.rsplit("/", 1)[-1]
            meta = {"name": base, "key": key, "date": fdate.isoformat(),
                    "_lm": obj["LastModified"]}
            if site_match in base.lower():
                sites.append(meta)
            elif app_match in base.lower():
                apps.append(meta)

    capped = False
    out = []
    for lst in (sites, apps):
        lst.sort(key=lambda m: (m["date"], m["_lm"]))  # oldest first
        if len(lst) > cap:
            lst = lst[-cap:]  # keep the newest `cap` files
            capped = True
        for m in lst:
            m.pop("_lm", None)
        out.append(lst)
    return out[0], out[1], capped


def list_available_dates():
    """Inventory of the prefix by file-date: {date: {'sites': n, 'apps': n}}.
    Lets the UI show what's pullable before anyone hits the button."""
    s3, bucket = _client_and_cfg()
    prefix = os.environ.get("S3_PREFIX", "").strip()
    suffix = os.environ.get("S3_SUFFIX", ".xlsx").strip().lower()
    site_match = os.environ.get("S3_SITES_MATCH", "site").strip().lower()
    app_match = os.environ.get("S3_APPS_MATCH", "app").strip().lower()
    dates = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not key.lower().endswith(suffix):
                continue
            base = key.rsplit("/", 1)[-1].lower()
            is_site = site_match in base
            is_app = (not is_site) and app_match in base
            if not (is_site or is_app):
                continue
            fdate = _date_from_key(key, obj.get("LastModified"))
            d = dates.setdefault(fdate, {"sites": 0, "apps": 0})
            d["sites" if is_site else "apps"] += 1
    return dates


def get_bytes(key):
    """Download one object's bytes (used file-by-file to keep peak memory low)."""
    s3, bucket = _client_and_cfg()
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def fetch_two():
    """For the two-flat-file export: return (sites_name, sites_bytes, apps_name,
    apps_bytes, date_str). Classifies objects by a filename substring
    (S3_SITES_MATCH default 'site', S3_APPS_MATCH default 'app'); newest of each.
    Missing side comes back as (name=None, bytes=None)."""
    import os
    import boto3
    bucket = os.environ.get("S3_BUCKET", "").strip()
    prefix = os.environ.get("S3_PREFIX", "").strip()
    suffix = os.environ.get("S3_SUFFIX", ".xlsx").strip().lower()
    site_match = os.environ.get("S3_SITES_MATCH", "site").strip().lower()
    app_match = os.environ.get("S3_APPS_MATCH", "app").strip().lower()
    if not bucket:
        raise RuntimeError("S3_BUCKET not set")
    region = os.environ.get("AWS_REGION", "").strip() or None
    s3 = boto3.client("s3", region_name=region)

    newest_site = newest_app = None
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not key.lower().endswith(suffix):
                continue
            base = key.rsplit("/", 1)[-1].lower()
            if site_match in base:
                if newest_site is None or obj["LastModified"] > newest_site["LastModified"]:
                    newest_site = obj
            elif app_match in base:
                if newest_app is None or obj["LastModified"] > newest_app["LastModified"]:
                    newest_app = obj

    def _get(o):
        if not o:
            return None, None, None
        body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()
        name = o["Key"].rsplit("/", 1)[-1]
        return name, body, _date_from_key(o["Key"], o.get("LastModified"))

    sname, sbytes, sdate = _get(newest_site)
    aname, abytes, adate = _get(newest_app)
    date_str = sdate or adate
    return sname, sbytes, aname, abytes, date_str


def fetch_latest_xlsx():
    """Return (filename, bytes, date_str) for the newest matching object, or
    (None, None, None). Raises on client/credential error."""
    import boto3
    bucket = os.environ.get("S3_BUCKET", "").strip()
    prefix = os.environ.get("S3_PREFIX", "").strip()
    suffix = os.environ.get("S3_SUFFIX", ".xlsx").strip().lower()
    if not bucket:
        raise RuntimeError("S3_BUCKET not set")
    region = os.environ.get("AWS_REGION", "").strip() or None
    s3 = boto3.client("s3", region_name=region)

    newest = None
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not key.lower().endswith(suffix):
                continue
            if newest is None or obj["LastModified"] > newest["LastModified"]:
                newest = obj
    if not newest:
        return None, None, None

    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    fn = newest["Key"].rsplit("/", 1)[-1]
    date_str = _date_from_key(newest["Key"], newest.get("LastModified"))
    return fn, body, date_str

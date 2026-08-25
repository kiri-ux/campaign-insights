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
    to the exact range downstream. Returns (site_metas, app_metas, ttddv_metas,
    creative_metas, device_metas, capped): metas are [{'name','key','date'}] oldest-first (so
    later files win de-dupe); capped=True if the S3_RANGE_MAX_FILES cap (default
    16 per side) trimmed the oldest files out."""
    import datetime as _dt
    if grace_days is None:
        grace_days = int(os.environ.get("S3_RANGE_GRACE_DAYS", "8"))
    start = _dt.date.fromisoformat(start_iso)
    end = _dt.date.fromisoformat(end_iso) + _dt.timedelta(days=grace_days)
    s3, bucket = _client_and_cfg()
    cap = int(os.environ.get("S3_RANGE_MAX_FILES", "16"))

    sites, apps, ttddv, creatives, devices = [], [], [], [], []
    buckets = {"site": sites, "app": apps, "ttddv": ttddv, "creative": creatives,
               "device": devices}
    for obj in _iter_objects(s3, bucket):
        key = obj["Key"]
        kind = _kind_for_key(key)
        if not kind:
            continue
        fdate = _dt.date.fromisoformat(_date_from_key(key, obj.get("LastModified")))
        if not (start <= fdate <= end):
            continue
        buckets[kind].append({"name": key.rsplit("/", 1)[-1], "key": key,
                              "date": fdate.isoformat(), "_lm": obj["LastModified"]})

    capped = False
    out = []
    for lst in (sites, apps, ttddv, creatives, devices):
        lst.sort(key=lambda m: (m["date"], m["_lm"]))  # oldest first
        if len(lst) > cap:
            lst = lst[-cap:]  # keep the newest `cap` files
            capped = True
        for m in lst:
            m.pop("_lm", None)
        out.append(lst)
    return out[0], out[1], out[2], out[3], out[4], capped


def list_available_dates():
    """Inventory of the prefix by file-date:
    {date: {'sites': n, 'apps': n, 'ttddv': n, 'creatives': n}}.
    Lets the UI show what's pullable before anyone hits the button."""
    s3, bucket = _client_and_cfg()
    dates = {}
    label = {"site": "sites", "app": "apps", "ttddv": "ttddv",
             "creative": "creatives", "device": "devices"}
    for obj in _iter_objects(s3, bucket):
        kind = _kind_for_key(obj["Key"])
        if not kind:
            continue
        fdate = _date_from_key(obj["Key"], obj.get("LastModified"))
        d = dates.setdefault(fdate, {"sites": 0, "apps": 0, "ttddv": 0,
                                     "creatives": 0, "devices": 0})
        d[label[kind]] += 1
    return dates


def get_bytes(key):
    """Download one object's bytes (used file-by-file to keep peak memory low)."""
    s3, bucket = _client_and_cfg()
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def _matchers():
    return (os.environ.get("S3_TTDDV_MATCH", "ttddv").strip().lower(),
            os.environ.get("S3_SITES_MATCH", "site").strip().lower(),
            os.environ.get("S3_APPS_MATCH", "app").strip().lower())


def _sep_strip(s):
    """Filename with separators removed, so 'creative-insights',
    'creative_insights' and 'creativeinsights' all compare equal."""
    return s.replace("-", "").replace("_", "").replace(" ", "")


def _creative_match():
    """Substring marking a creative-insights export. Matched separator-insensitively,
    so the default covers both 'creative-insights-…' and the vendor's actual drop
    name 'creativeinsights_20260816_1728_0.csv'. Override: S3_CREATIVE_MATCH."""
    return os.environ.get("S3_CREATIVE_MATCH", "creative-insights").strip().lower()


def _device_match():
    """Substring marking a device-insights export, e.g.
    'device-insights_20260822_0844_0.csv'. Override: S3_DEVICE_MATCH."""
    return os.environ.get("S3_DEVICE_MATCH", "device-insights").strip().lower()


def _classify(base):
    """'creative' | 'device' | 'ttddv' | 'site' | 'app' | None for a lowercased
    filename.
    Creative exports are checked FIRST — they're a different grain entirely and
    their name can contain other markers. TTD/DV360 combined exports come next
    because their name ('ttddv-siteapp-…') also contains the site/app substrings."""
    t, si, ap = _matchers()
    cr, dv = _creative_match(), _device_match()
    if cr and _sep_strip(cr) in _sep_strip(base):
        return "creative"
    if dv and _sep_strip(dv) in _sep_strip(base):
        return "device"
    if t and t in base:
        return "ttddv"
    if si in base:
        return "site"
    if ap in base:
        return "app"
    return None


def _suffixes(kind):
    """Accepted extensions per kind. Site/app/ttddv follow S3_SUFFIX (default
    .xlsx); the creative export arrives as CSV, so it accepts .csv and .xlsx
    unless S3_CREATIVE_SUFFIX narrows it."""
    if kind == "creative":
        raw = os.environ.get("S3_CREATIVE_SUFFIX", ".csv,.xlsx")
    elif kind == "device":
        raw = os.environ.get("S3_DEVICE_SUFFIX", ".csv,.xlsx")
    else:
        raw = os.environ.get("S3_SUFFIX", ".xlsx")
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _kind_for_key(key):
    """File kind for an S3 key, or None if it isn't one of ours (folder marker,
    wrong extension for its kind, or an unrecognized name)."""
    if key.endswith("/"):
        return None
    base = key.rsplit("/", 1)[-1].lower()
    kind = _classify(base)
    if not kind:
        return None
    return kind if base.endswith(_suffixes(kind)) else None


def _prefixes():
    """Prefixes to scan. S3_CREATIVE_PREFIX is optional — set it only if the
    creative exports land in a different folder than the site/app ones."""
    out = [os.environ.get("S3_PREFIX", "").strip()]
    for var in ("S3_CREATIVE_PREFIX", "S3_DEVICE_PREFIX"):
        p = os.environ.get(var, "").strip()
        if p and p not in out:
            out.append(p)
    return out


def _iter_objects(s3, bucket):
    """Yield every object under every configured prefix, de-duped by key."""
    paginator = s3.get_paginator("list_objects_v2")
    seen = set()
    for prefix in _prefixes():
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"] in seen:
                    continue
                seen.add(obj["Key"])
                yield obj


def fetch_two():
    """Return (sites_name, sites_bytes, apps_name, apps_bytes, ttddv_name,
    ttddv_bytes, date_str) — the newest of each export type. TTD/DV360 combined
    exports (S3_TTDDV_MATCH default 'ttddv') are optional and classified before
    site/app since their filename contains both substrings.
    Missing side comes back as (name=None, bytes=None)."""
    import os
    import boto3
    bucket = os.environ.get("S3_BUCKET", "").strip()
    prefix = os.environ.get("S3_PREFIX", "").strip()
    suffix = os.environ.get("S3_SUFFIX", ".xlsx").strip().lower()
    if not bucket:
        raise RuntimeError("S3_BUCKET not set")
    region = os.environ.get("AWS_REGION", "").strip() or None
    s3 = boto3.client("s3", region_name=region)

    newest = {"site": None, "app": None, "ttddv": None, "creative": None,
              "device": None}
    for obj in _iter_objects(s3, bucket):
        kind = _kind_for_key(obj["Key"])
        if kind and (newest[kind] is None or obj["LastModified"] > newest[kind]["LastModified"]):
            newest[kind] = obj
    newest_site, newest_app, newest_ttddv = newest["site"], newest["app"], newest["ttddv"]

    def _get(o):
        if not o:
            return None, None, None
        body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()
        name = o["Key"].rsplit("/", 1)[-1]
        return name, body, _date_from_key(o["Key"], o.get("LastModified"))

    sname, sbytes, sdate = _get(newest_site)
    aname, abytes, adate = _get(newest_app)
    tname, tbytes, _tdate = _get(newest_ttddv)
    date_str = sdate or adate
    return sname, sbytes, aname, abytes, tname, tbytes, date_str


def fetch_latest_device():
    """Return (filename, bytes, date_str) for the newest device-insights export."""
    s3, bucket = _client_and_cfg()
    newest = None
    for obj in _iter_objects(s3, bucket):
        if _kind_for_key(obj["Key"]) != "device":
            continue
        if newest is None or obj["LastModified"] > newest["LastModified"]:
            newest = obj
    if not newest:
        return None, None, None
    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    return (newest["Key"].rsplit("/", 1)[-1], body,
            _date_from_key(newest["Key"], newest.get("LastModified")))


def fetch_latest_creative():
    """Return (filename, bytes, date_str) for the newest creative-insights export,
    or (None, None, None). Standalone from fetch_two so the creative QA check can
    run on its own without pulling the (much larger) site/app files."""
    s3, bucket = _client_and_cfg()
    newest = None
    for obj in _iter_objects(s3, bucket):
        if _kind_for_key(obj["Key"]) != "creative":
            continue
        if newest is None or obj["LastModified"] > newest["LastModified"]:
            newest = obj
    if not newest:
        return None, None, None
    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    return (newest["Key"].rsplit("/", 1)[-1], body,
            _date_from_key(newest["Key"], newest.get("LastModified")))


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
            if _classify(key.rsplit("/", 1)[-1].lower()) in ("creative", "device"):
                continue  # different grain — never the "insights workbook"
            if newest is None or obj["LastModified"] > newest["LastModified"]:
                newest = obj
    if not newest:
        return None, None, None

    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    fn = newest["Key"].rsplit("/", 1)[-1]
    date_str = _date_from_key(newest["Key"], newest.get("LastModified"))
    return fn, body, date_str

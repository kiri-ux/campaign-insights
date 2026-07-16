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

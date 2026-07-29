"""
tap_adapter.py
TapClicks now exports two flat, single-sheet files (one Site Domains, one Apps)
with all fields as columns. The analysis engines expect the old six-sheet
Insights workbook, so this adapter reads the two flat files and synthesizes that
workbook in memory:
  - Site Overview  = the sites flat file (row-level, as-is)
  - App Overview   = the apps flat file (row-level, as-is)
  - Product Overview  = aggregated up from both, by BU/Client/Product
  - Strategy Overview = aggregated up from both, by BU/Client/Product/Strategy

No enrichment is invented — Product 2, Strategy Type/Name and Client Business
Unit come straight from the export. 'Internal Cost' is mapped from 'Billable
Spend' (the cost field present in the data views).
"""
import io
import re
import tempfile
import pandas as pd

_MEASURES = ["Impressions", "Clicks", "Post Click Conversions",
             "Post View Conversions", "Billable Spend"]

# TapClicks data-view exports use snake_case DB column names; the engines expect
# the Title Case labels from the old report. Map them (only ones present are used).
_COLMAP = {
    "date": "Date",
    "client_business_unit": "Client Business Unit",
    "client": "Client",
    "product_2": "Product 2",
    "strategy_type": "Strategy Type",
    "strategy_name": "Strategy Name",
    "campaign_id": "Campaign ID", "app_url": "App/URL", "dsp": "DSP",
    "site_domain": "Site Domain",
    "final_site_domain_name": "Final Site Domain Name",
    "app_name": "App Name",
    "final_app_name_use_me": "Final App Name",
    "final_app_name": "Final App Name",
    "app_id": "App ID",
    "device_type": "Device Type",
    "impressions": "Impressions",
    "clicks": "Clicks",
    "ctr": "CTR",
    "post_click_conversions": "Post Click Conversions",
    "post_view_conversions": "Post View Conversions",
    "cpm": "CPM",
    "billable_spend": "Billable Spend",
    "total_spend": "Total Spend",
}


def _canon(s):
    """Canonical form for header matching: lowercase, alphanumerics only.
    'Campaign ID', 'campaign_id', 'CampaignId ' all -> 'campaignid' — while
    'Campaign Pool ID' -> 'campaignpoolid' stays distinct."""
    import re
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# canon(header) -> engine name, built from the snake_case map AND the Title Case
# targets themselves, so any casing/spacing variant of either form matches.
_CANON_MAP = {_canon(k): v for k, v in _COLMAP.items()}
_CANON_MAP.update({_canon(v): v for v in _COLMAP.values()})


def _normalize_headers(df):
    """Rename export headers to the Title Case names the engines use, matching
    on canonical form so 'Campaign Id', 'campaign_id', 'CAMPAIGN ID' all work."""
    ren = {c: _CANON_MAP[_canon(c)] for c in df.columns if _canon(c) in _CANON_MAP}
    return df.rename(columns=ren) if ren else df


# The only columns the engines actually use. The export has ~30 more (CPV/CPCV,
# budgets, margins, external IDs...) that we drop on read to save a lot of memory
# on the full ~385k-row dataset.
_KEEP = ["Date", "Client Business Unit", "Client", "Product 2", "Strategy Type",
         "Strategy Name", "Campaign ID", "Site Domain", "Final Site Domain Name",
         "App Name", "Final App Name", "App ID", "Impressions", "Clicks", "CTR",
         "Post Click Conversions", "Post View Conversions", "CPM", "Billable Spend", "App/URL", "DSP"]
_FLOAT32 = ["CTR", "CPM"]              # display-only metrics; recomputed downstream
_CATEGORY = ["Client Business Unit", "Client", "Product 2", "Strategy Type", "Campaign ID"]


def _prune_and_downcast(df):
    keep = [c for c in _KEEP if c in df.columns]
    df = df[keep].copy()
    # money + count columns stay full precision (no overflow / cent errors on big data)
    for c in ("Impressions", "Clicks", "Post Click Conversions", "Post View Conversions"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    if "Billable Spend" in df.columns:
        df["Billable Spend"] = pd.to_numeric(df["Billable Spend"], errors="coerce").fillna(0.0)
    for c in _FLOAT32:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    # repeated text -> category is the big memory win (BU/Client/Product/Strategy)
    for c in _CATEGORY:
        if c in df.columns:
            df[c] = df[c].astype("category")
    return df


def _wanted_engine_name(header):
    """Engine column name for a raw export header, or None if we don't use it."""
    cn = _canon(header)
    name = _CANON_MAP.get(cn)
    return name if name in _KEEP else None


def _read_xlsx_slim(bio):
    """Stream the first worksheet in read_only mode, materializing ONLY the
    columns the engines use. pandas.read_excel builds every one of the export's
    ~30+ columns before we prune — on a 385k-row file that's the single biggest
    memory spike in the app, and it's what OOM-killed workers mid-pull."""
    from openpyxl import load_workbook
    wb = load_workbook(bio, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        if header is None:
            return pd.DataFrame()
        wanted = {}
        for i, h in enumerate(header):
            if h is None:
                continue
            name = _wanted_engine_name(h)
            if name and name not in wanted.values():
                wanted[i] = name
        if not wanted:
            # Unrecognized layout — fall back to the full read so the existing
            # error path can report it sensibly.
            bio.seek(0)
            return pd.read_excel(bio)
        data = {name: [] for name in wanted.values()}
        for row in it:
            for i, name in wanted.items():
                data[name].append(row[i] if i < len(row) else None)
        return pd.DataFrame(data)
    finally:
        wb.close()


_COMPOSITE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")


def _split_name_id(v):
    """'QR & Barcode Scanner: Home - Android (qrcode.barcodescanner.reader)'
    -> ('QR & Barcode Scanner: Home - Android', 'qrcode.barcodescanner.reader').
    Values without an id-looking trailing parenthetical return (v, v)."""
    v = str(v).strip()
    m = _COMPOSITE.match(v)
    if m:
        nid = m.group(2).strip()
        if "." in nid or re.fullmatch(r"\d{6,}", nid):
            return (m.group(1).strip() or nid), nid
    return v, v


def clean_app_identity(df):
    """AdLib sometimes ships composite App IDs ('Name - Android (bundle.id)').
    Split them: pure id into App ID, name part into App Name / Final App Name
    where those held the same composite (or were blank). No-op otherwise."""
    if df is None or not len(df) or "App ID" not in df.columns:
        return df
    raw = df["App ID"].astype(str)
    pairs = raw.map(_split_name_id)
    ids = pairs.map(lambda t: t[1])
    changed = ids != raw
    if not changed.any():
        return df
    df = df.copy()
    names = pairs.map(lambda t: t[0])
    for col in ("App Name", "Final App Name"):
        if col in df.columns:
            cur = df[col].astype(str)
            replace = changed & (cur.eq(raw) | cur.isin(["", "nan", "None", "NA"]))
            df[col] = cur.where(~replace, names)
    df["App ID"] = ids
    return df


def read_flat(data, filename=""):
    """Read one flat export (xlsx or csv bytes) into a DataFrame with normalized
    headers, pruned to the columns the engines use, and memory-downcast. Both
    readers load only the needed columns to keep peak memory low."""
    bio = io.BytesIO(data)
    if (filename or "").lower().endswith(".csv"):
        try:
            df = pd.read_csv(bio, low_memory=False,
                             usecols=lambda c: _wanted_engine_name(c) is not None)
        except ValueError:  # no recognizable columns — full read for the error path
            bio.seek(0)
            df = pd.read_csv(bio, low_memory=False)
    else:
        df = _read_xlsx_slim(bio)
    return clean_app_identity(_prune_and_downcast(_normalize_headers(df)))


_MEASURE_COLS = set(_MEASURES) | {"CTR", "CPM", "Total Spend"}


def combine_flats(dfs):
    """Concat several date-ranged flat exports into one frame. Overlapping exports
    (rolling windows / restated data) produce duplicate dimension rows — keep the
    LAST occurrence (files are processed oldest-first, so the newest export's
    version of any restated row wins) and drop the rest so pooled metrics never
    double-count."""
    dfs = [d for d in dfs if d is not None and len(d)]
    if not dfs:
        return pd.DataFrame()
    combined = pd.concat(dfs, ignore_index=True, sort=False)
    dims = [c for c in combined.columns if c not in _MEASURE_COLS]
    if dims:
        combined = combined.drop_duplicates(subset=dims, keep="last")
    return combined.reset_index(drop=True)


# Real TLDs a placement domain plausibly ends with; used to split the TTD/DV360
# combined export's single App/URL column into site rows vs app rows.
_TLDS = {"com","net","org","co","io","tv","us","uk","ca","de","fr","es","it","nl",
         "se","jp","au","in","br","mx","fm","gg","me","app","edu","gov","info","biz","news"}
_REV_DNS = {"com","net","org","co","io","tv","jp","de","uk","us","air","me","app","tw","kr"}


def _looks_like_app(v):
    v = str(v).strip().lower()
    if not v or v in ("nan", "none"):
        return True
    if re.fullmatch(r"(id)?\d{6,}", v):
        return True                                   # iOS store ids
    if "." not in v:
        return True                                   # bare app names
    labels = v.split(".")
    if len(labels) >= 3 and labels[0] in _REV_DNS:
        return True                                   # reverse-DNS bundle ids
    return labels[-1] not in _TLDS                    # real TLD -> site




def split_ttddv(df, dsp_label="TTD/DV360"):
    """Split the TTD/DV360 combined export (single App/URL column) into
    site-shaped and app-shaped frames tagged with a DSP column, so they merge
    straight into the regular site/app analysis. Composite 'Name (bundle.id)'
    values are parsed so App Name and App ID land in their own columns."""
    empty = pd.DataFrame()
    if df is None or not len(df) or "App/URL" not in df.columns:
        return empty, empty
    df = df.copy()
    df["DSP"] = dsp_label
    pairs = df["App/URL"].map(_split_name_id)
    names = pairs.map(lambda t: t[0])
    ids = pairs.map(lambda t: t[1])
    isapp = ids.map(_looks_like_app)
    sites = df[~isapp].copy()
    apps = df[isapp].copy()
    if len(sites):
        sites["Site Domain"] = ids[~isapp]
        sites["Final Site Domain Name"] = ids[~isapp]
        sites = sites.drop(columns=["App/URL"])
    if len(apps):
        apps["App Name"] = names[isapp]
        apps["Final App Name"] = names[isapp]
        apps["App ID"] = ids[isapp]
        apps = apps.drop(columns=["App/URL"])
    return sites, apps


def filter_date_range(df, start_iso, end_iso):
    """Keep only rows whose Date falls within [start, end] inclusive. Frames
    without a Date column pass through untouched (nothing to filter on)."""
    if df is None or not len(df) or "Date" not in df.columns:
        return df
    d = pd.to_datetime(df["Date"], errors="coerce")
    import datetime as _dt
    start = pd.Timestamp(_dt.date.fromisoformat(start_iso))
    end = pd.Timestamp(_dt.date.fromisoformat(end_iso))
    return df[(d >= start) & (d <= end)].reset_index(drop=True)


def _bu_col(df):
    for c in ("Business Unit", "Client Business Unit"):
        if c in df.columns:
            return c
    return df.columns[0]


def _overview(df, keys):
    """Aggregate row-level delivery up to an overview grain, renaming the flat
    measure columns to the Overview names the insights engine expects."""
    d = df.copy()
    for m in _MEASURES:
        if m in d.columns:
            d[m] = pd.to_numeric(d[m], errors="coerce").fillna(0)
    agg = {"Impressions": ("Impressions", "sum"), "Clicks": ("Clicks", "sum")}
    if "Post Click Conversions" in d.columns:
        agg["Click Conversions"] = ("Post Click Conversions", "sum")
    if "Post View Conversions" in d.columns:
        agg["View-throughs"] = ("Post View Conversions", "sum")
    if "Billable Spend" in d.columns:
        agg["Internal Cost"] = ("Billable Spend", "sum")
    g = d.groupby(keys, dropna=False, observed=True).agg(**agg).reset_index()
    # groupby on category keys yields category columns; the small overview frames
    # are tiny, so cast keys back to str to avoid category edge cases downstream.
    for k in keys:
        if k in g.columns and str(g[k].dtype) == "category":
            g[k] = g[k].astype(str)
    # guarantee the columns downstream aggregations reference
    for col in ("Click Conversions", "View-throughs", "Internal Cost"):
        if col not in g.columns:
            g[col] = 0
    return g


def build_frames(sites_df, apps_df):
    """Build the four analysis frames (site/app/product/strategy) directly, with
    NO xlsx round-trip — keeps the memory downcasting and avoids a duplicate copy."""
    combined = pd.concat([sites_df, apps_df], ignore_index=True, sort=False)
    bu = _bu_col(combined)
    prod_keys = [k for k in (bu, "Client", "Product 2") if k in combined.columns]
    prod = _overview(combined, prod_keys).rename(columns={"Product 2": "Product"})
    strat_keys = [k for k in (bu, "Client", "Product 2", "Strategy Type", "Strategy Name")
                  if k in combined.columns]
    strat = _overview(combined, strat_keys).rename(columns={"Product 2": "Product"})
    del combined
    import gc
    gc.collect()
    return {"site": sites_df, "app": apps_df, "product": prod, "strategy": strat}


def synthesize_workbook(sites_df, apps_df):
    """Write the four frames to a temp .xlsx and return its path (manual-upload
    path; the automated pull uses build_frames to skip the xlsx round-trip)."""
    f = build_frames(sites_df, apps_df)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.close()
    with pd.ExcelWriter(tmp.name, engine="openpyxl") as xl:
        f["site"].to_excel(xl, sheet_name="Site Overview", index=False)
        f["app"].to_excel(xl, sheet_name="App Overview", index=False)
        f["product"].to_excel(xl, sheet_name="Product Overview", index=False)
        f["strategy"].to_excel(xl, sheet_name="Strategy Overview", index=False)
    return tmp.name

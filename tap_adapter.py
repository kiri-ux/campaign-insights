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


def _normalize_headers(df):
    """Rename snake_case export headers to the Title Case names the engines use.
    Leaves already-correct headers untouched, so both formats work."""
    ren = {c: _COLMAP[str(c).strip().lower()] for c in df.columns
           if str(c).strip().lower() in _COLMAP}
    return df.rename(columns=ren) if ren else df


def read_flat(data, filename=""):
    """Read one flat export (xlsx or csv bytes) into a DataFrame with normalized headers."""
    bio = io.BytesIO(data)
    if (filename or "").lower().endswith(".csv"):
        df = pd.read_csv(bio)
    else:
        df = pd.read_excel(bio)
    return _normalize_headers(df)


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
    g = d.groupby(keys, dropna=False).agg(**agg).reset_index()
    # guarantee the columns downstream aggregations reference
    for col in ("Click Conversions", "View-throughs", "Internal Cost"):
        if col not in g.columns:
            g[col] = 0
    return g


def synthesize_workbook(sites_df, apps_df):
    """Write a temp .xlsx with Site/App/Product/Strategy Overview sheets and
    return its path (the engines read it exactly like the old export)."""
    combined = pd.concat([sites_df, apps_df], ignore_index=True, sort=False)
    bu = _bu_col(combined)

    prod_keys = [k for k in (bu, "Client", "Product 2") if k in combined.columns]
    prod = _overview(combined, prod_keys).rename(columns={"Product 2": "Product"})

    strat_keys = [k for k in (bu, "Client", "Product 2", "Strategy Type", "Strategy Name")
                  if k in combined.columns]
    strat = _overview(combined, strat_keys).rename(columns={"Product 2": "Product"})

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.close()
    with pd.ExcelWriter(tmp.name, engine="openpyxl") as xl:
        sites_df.to_excel(xl, sheet_name="Site Overview", index=False)
        apps_df.to_excel(xl, sheet_name="App Overview", index=False)
        prod.to_excel(xl, sheet_name="Product Overview", index=False)
        strat.to_excel(xl, sheet_name="Strategy Overview", index=False)
    return tmp.name

"""
insights_engine.py
Parses the AdLib "Insights" workbook (Client / Product / Strategy / Site+App
Overview sheets) into structured insights: performance by business unit
(partner), product, and strategy, plus waste/plausibility flags.

Pure functions -> import straight into the Flask app or a scheduled Render job.
Defensive against the real-world dirtiness seen in the export:
  - a leading TOTAL row per sheet
  - full line-item strings leaking into the Product column
  - mixed/blank numerics
"""
import pandas as pd
import numpy as np

VALID_PRODUCTS = {
    "Display", "Social Mirror", "Video", "CTV", "Native Display",
    "Native Video", "Social Mirror CTV", "Online Audio",
}
NUM_COLS = ["Impressions", "Clicks", "Click Conversions", "View-throughs", "CPM", "Internal Cost"]


def _load_sheet(xls, name):
    df = pd.read_excel(xls, sheet_name=name)
    # drop the TOTAL summary row (first column == TOTAL)
    df = df[df.iloc[:, 0].astype(str).str.strip().str.upper() != "TOTAL"].copy()
    for c in NUM_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _bu_col(df):
    for c in ("Business Unit", "Client Business Unit"):
        if c in df.columns:
            return c
    return df.columns[0]


def load_workbook_frames(path_or_buffer):
    xls = pd.ExcelFile(path_or_buffer)
    frames = {}
    for want, aliases in {
        "client": ["Client Overview"],
        "product": ["Product Overview"],
        "strategy": ["Strategy Overview"],
        "siteapp": ["Site + App Overview"],
    }.items():
        for a in aliases:
            if a in xls.sheet_names:
                frames[want] = _load_sheet(xls, a)
                break
    return frames


def by_business_unit(product_df, zero_conv_min_spend=300.0):
    bu = _bu_col(product_df)
    g = (product_df.groupby(bu)
         .agg(impressions=("Impressions", "sum"), clicks=("Clicks", "sum"),
              conversions=("Click Conversions", "sum"),
              view_throughs=("View-throughs", "sum"),
              internal_cost=("Internal Cost", "sum"))
         .reset_index().rename(columns={bu: "business_unit"}))
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)
    g["cost_per_conv"] = np.where(g["conversions"] > 0, g["internal_cost"] / g["conversions"], np.nan)
    g["zero_conversion_waste"] = (g["conversions"] == 0) & (g["internal_cost"] >= zero_conv_min_spend)
    # Tiered CTR flag: the more impressions, the lower the CTR bar to flag.
    g["flagged"] = (((g["impressions"] >= 10000) & (g["ctr"] > 0.05)) |
                    ((g["impressions"] >= 30000) & (g["ctr"] > 0.03)) |
                    ((g["impressions"] >= 50000) & (g["ctr"] > 0.01)))
    return g.sort_values("ctr", ascending=False)


def by_product(product_df):
    df = product_df.copy()
    df["product_clean"] = df["Product"].where(df["Product"].isin(VALID_PRODUCTS), "Other/Uncategorized")
    g = (df.groupby("product_clean")
         .agg(impressions=("Impressions", "sum"), clicks=("Clicks", "sum"),
              conversions=("Click Conversions", "sum"),
              internal_cost=("Internal Cost", "sum"))
         .reset_index().rename(columns={"product_clean": "product"}))
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)
    g["click_conv_rate"] = np.where(g["clicks"] > 0, g["conversions"] / g["clicks"], 0)
    total = g["internal_cost"].sum() or 1
    g["pct_of_spend"] = g["internal_cost"] / total
    book_ctr = (g["clicks"].sum() / g["impressions"].sum()) if g["impressions"].sum() else 0
    g["above_norm"] = g["ctr"] > book_ctr
    return g.sort_values("ctr", ascending=False)


def by_strategy(strategy_df):
    g = (strategy_df.groupby("Strategy Type")
         .agg(impressions=("Impressions", "sum"), clicks=("Clicks", "sum"),
              conversions=("Click Conversions", "sum"),
              view_throughs=("View-throughs", "sum"),
              internal_cost=("Internal Cost", "sum"))
         .reset_index().rename(columns={"Strategy Type": "strategy_type"}))
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)
    g["cost_per_conv"] = np.where(g["conversions"] > 0, g["internal_cost"] / g["conversions"], np.nan)
    book_ctr = (g["clicks"].sum() / g["impressions"].sum()) if g["impressions"].sum() else 0
    g["above_norm"] = g["ctr"] > book_ctr
    return g.sort_values("ctr", ascending=False)


def strategy_flags(strategy_df, min_impr=5000, ctr_multiple=3.0, ctr_floor=0.005):
    """Flag individual STRATEGY NAMES (line items) whose CTR is abnormally high for
    their strategy type — the 'looks great on paper' anomalies, per strategy."""
    if "Strategy Name" not in strategy_df.columns or "Strategy Type" not in strategy_df.columns:
        return pd.DataFrame()
    bu = _bu_col(strategy_df)
    df = strategy_df.copy()
    g = (df.groupby([bu, "Client", "Product", "Strategy Type", "Strategy Name"], dropna=False)
         .agg(impressions=("Impressions", "sum"), clicks=("Clicks", "sum"),
              conversions=("Click Conversions", "sum"), internal_cost=("Internal Cost", "sum"))
         .reset_index().rename(columns={bu: "business_unit"}))
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)
    norms = (g.groupby("Strategy Type").apply(
        lambda x: (x["clicks"].sum() / x["impressions"].sum()) if x["impressions"].sum() else 0)
        .rename("type_ctr").reset_index())
    g = g.merge(norms, on="Strategy Type", how="left")
    g["x_over_norm"] = np.where(g["type_ctr"] > 0, g["ctr"] / g["type_ctr"], 0)
    flagged = g[(g["impressions"] >= min_impr) & (g["ctr"] >= ctr_floor) &
                (g["x_over_norm"] >= ctr_multiple)].copy()
    flagged["reason"] = flagged.apply(
        lambda r: f"CTR {r['ctr']*100:.2f}% is {r['x_over_norm']:.1f}× the {r['Strategy Type']} norm "
                  f"({r['type_ctr']*100:.2f}%)", axis=1)
    return flagged.sort_values("x_over_norm", ascending=False)


def client_flags(product_df, strategy_df, min_impr=20000, ctr_multiple=3.0, ctr_floor=0.008):
    """Per-client, per-product watchlist keyed on ABNORMALLY HIGH CTR relative to
    each product's own norm (a 1.5% CTR is alarming for CTV, normal for display
    retargeting). Conversions are ignored — not all clients track them, so 0 isn't
    a reliable signal. Returns one row per flagged client+product."""
    bu = _bu_col(product_df)
    if "Client" not in product_df.columns or "Product" not in product_df.columns:
        return pd.DataFrame()
    valid = {"Display", "Social Mirror", "Video", "CTV", "Native Display",
             "Native Video", "Social Mirror CTV", "Online Audio"}
    df = product_df.copy()
    df["product"] = df["Product"].where(df["Product"].isin(valid), "Other")
    g = (df.groupby([bu, "Client", "product"])
         .agg(impressions=("Impressions", "sum"), clicks=("Clicks", "sum"),
              internal_cost=("Internal Cost", "sum"))
         .reset_index().rename(columns={bu: "business_unit"}))
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)

    # Per-product norm = pooled CTR across all clients on that product
    norms = (g.groupby("product").apply(
        lambda x: (x["clicks"].sum() / x["impressions"].sum()) if x["impressions"].sum() else 0)
        .rename("product_ctr").reset_index())
    g = g.merge(norms, on="product", how="left")
    g["x_over_norm"] = np.where(g["product_ctr"] > 0, g["ctr"] / g["product_ctr"], 0)

    flagged = g[(g["impressions"] >= min_impr) &
                (g["ctr"] >= ctr_floor) &
                (g["x_over_norm"] >= ctr_multiple)].copy()
    flagged["reason"] = flagged.apply(
        lambda r: f"CTR {r['ctr']*100:.2f}% is {r['x_over_norm']:.1f}× the {r['product']} norm "
                  f"({r['product_ctr']*100:.2f}%)", axis=1)
    return flagged.sort_values("x_over_norm", ascending=False)


def build_insights(path_or_buffer=None, zero_conv_min_spend=300.0, frames=None):
    if frames is not None:
        prod = frames.get("product")
        strat = frames.get("strategy")
    else:
        wframes = load_workbook_frames(path_or_buffer)
        prod = wframes.get("product")
        strat = wframes.get("strategy")
    if prod is None:
        raise ValueError("Product Overview data not found.")

    bu = by_business_unit(prod, zero_conv_min_spend)
    pr = by_product(prod)
    st = by_strategy(strat) if strat is not None else pd.DataFrame()
    fl = strategy_flags(strat) if strat is not None else pd.DataFrame()

    zero = bu[bu["zero_conversion_waste"]]
    sm = pr[pr["product"] == "Social Mirror"]

    summary = {
        "total_impressions": float(prod["Impressions"].sum()),
        "total_clicks": float(pd.to_numeric(prod["Clicks"], errors="coerce").fillna(0).sum()) if "Clicks" in prod else 0.0,
        "total_cost": float(prod["Internal Cost"].sum()),
        "business_units": int(bu["business_unit"].nunique()),
        "zero_conv_bu_count": int(len(zero)),
        "zero_conv_spend": float(zero["internal_cost"].sum()),
        "social_mirror_pct_spend": float(sm["pct_of_spend"].iloc[0]) if len(sm) else None,
        "social_mirror_conv_rate": float(sm["click_conv_rate"].iloc[0]) if len(sm) else None,
        "strategy_flag_count": int(len(fl)),
    }
    summary["book_ctr"] = (summary["total_clicks"] / summary["total_impressions"]) if summary["total_impressions"] else 0.0
    return {
        "summary": summary,
        "by_business_unit": bu,
        "by_product": pr,
        "by_strategy": st,
        "strategy_flags": fl,
        "client_flags": client_flags(prod, strat),
    }

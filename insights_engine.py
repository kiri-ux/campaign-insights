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
    return g.sort_values("internal_cost", ascending=False)


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
    return g.sort_values("internal_cost", ascending=False)


def by_strategy(strategy_df):
    g = (strategy_df.groupby("Strategy Type")
         .agg(impressions=("Impressions", "sum"), clicks=("Clicks", "sum"),
              conversions=("Click Conversions", "sum"),
              view_throughs=("View-throughs", "sum"),
              internal_cost=("Internal Cost", "sum"))
         .reset_index().rename(columns={"Strategy Type": "strategy_type"}))
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)
    g["cost_per_conv"] = np.where(g["conversions"] > 0, g["internal_cost"] / g["conversions"], np.nan)
    return g.sort_values("internal_cost", ascending=False)


def plausibility_flags(strategy_df, ctr_ceiling=0.03, min_impr=20000):
    """High CTR + zero conversions = the 'looks great on paper, no outcome' pattern."""
    df = strategy_df.copy()
    df["ctr"] = np.where(df["Impressions"] > 0, df["Clicks"] / df["Impressions"], 0)
    flag = df[(df["ctr"] > ctr_ceiling) & (df["Click Conversions"] == 0) & (df["Impressions"] > min_impr)]
    bu = _bu_col(df)
    keep = [bu, "Strategy Name", "Impressions", "Clicks", "ctr", "Internal Cost"]
    keep = [c for c in keep if c in flag.columns]
    return flag[keep].sort_values("ctr", ascending=False)


def client_flags(product_df, strategy_df, zero_conv_min_spend=250.0, ctr_ceiling=0.03):
    """Per-client watchlist: surfaces clients that need attention — spend with no
    conversions, and high-CTR/zero-conversion (plausibility) exposure."""
    bu = _bu_col(product_df)
    keys = [c for c in [bu, "Client"] if c in product_df.columns]
    if "Client" not in product_df.columns:
        return pd.DataFrame()
    g = (product_df.groupby(keys)
         .agg(impressions=("Impressions", "sum"), clicks=("Clicks", "sum"),
              conversions=("Click Conversions", "sum"),
              internal_cost=("Internal Cost", "sum"))
         .reset_index())
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)
    g["zero_conv_spend"] = (g["conversions"] == 0) & (g["internal_cost"] >= zero_conv_min_spend)

    # plausibility exposure per client from strategy grain
    plaus = pd.DataFrame()
    if strategy_df is not None and "Client" in strategy_df.columns:
        s = strategy_df.copy()
        s["ctr"] = np.where(s["Impressions"] > 0, s["Clicks"] / s["Impressions"], 0)
        s = s[(s["ctr"] > ctr_ceiling) & (s["Click Conversions"] == 0) & (s["Impressions"] > 20000)]
        if len(s):
            plaus = (s.groupby("Client").agg(plausibility_lineitems=("Strategy Name", "nunique"),
                                             plausibility_cost=("Internal Cost", "sum")).reset_index())
    if len(plaus):
        g = g.merge(plaus, on="Client", how="left")
    g["plausibility_lineitems"] = g.get("plausibility_lineitems", 0)
    g["plausibility_cost"] = g.get("plausibility_cost", 0.0)
    g[["plausibility_lineitems", "plausibility_cost"]] = g[["plausibility_lineitems", "plausibility_cost"]].fillna(0)

    reasons = []
    for _, r in g.iterrows():
        rs = []
        if r["zero_conv_spend"]:
            rs.append("spend, 0 conversions")
        if r["plausibility_lineitems"]:
            rs.append(f"{int(r['plausibility_lineitems'])} high-CTR/0-conv line item(s)")
        reasons.append("; ".join(rs))
    g["reason"] = reasons
    flagged = g[g["reason"] != ""].sort_values("internal_cost", ascending=False)
    return flagged


def build_insights(path_or_buffer, zero_conv_min_spend=300.0):
    frames = load_workbook_frames(path_or_buffer)
    prod = frames.get("product")
    strat = frames.get("strategy")
    if prod is None:
        raise ValueError("Product Overview sheet not found.")

    bu = by_business_unit(prod, zero_conv_min_spend)
    pr = by_product(prod)
    st = by_strategy(strat) if strat is not None else pd.DataFrame()
    fl = plausibility_flags(strat) if strat is not None else pd.DataFrame()

    zero = bu[bu["zero_conversion_waste"]]
    sm = pr[pr["product"] == "Social Mirror"]

    summary = {
        "total_impressions": float(prod["Impressions"].sum()),
        "total_cost": float(prod["Internal Cost"].sum()),
        "business_units": int(bu["business_unit"].nunique()),
        "zero_conv_bu_count": int(len(zero)),
        "zero_conv_spend": float(zero["internal_cost"].sum()),
        "social_mirror_pct_spend": float(sm["pct_of_spend"].iloc[0]) if len(sm) else None,
        "social_mirror_conv_rate": float(sm["click_conv_rate"].iloc[0]) if len(sm) else None,
        "plausibility_flag_count": int(len(fl)),
    }
    return {
        "summary": summary,
        "by_business_unit": bu,
        "by_product": pr,
        "by_strategy": st,
        "plausibility_flags": fl,
        "client_flags": client_flags(prod, strat),
    }

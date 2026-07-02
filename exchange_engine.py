"""
exchange_engine.py
Reads the "Exchanges Overview" sheet and flags exchanges behaving abnormally.

NOTE: the exchange grain has no conversions column, so "low conversions" can't be
measured here. What we CAN flag from this sheet:
  - CTR far above the book average at meaningful spend  -> possible invalid / bot
    traffic or a junk supply path (abnormal clicks)
  - CTR far below the book average at meaningful spend  -> likely dead/wasted or
    audio/CTV inventory not driving engagement
  - single exchanges concentrating a large share of spend
Uses openpyxl read_only streaming to stay memory-light.
"""
import pandas as pd
import numpy as np
from openpyxl import load_workbook

SHEET = "Exchanges Overview"
NEED = [("exchange",), ("client business unit",), ("business unit",), ("client",),
        ("product",), ("strategy", "type"), ("deal",),
        ("impression",), ("click",), ("billable", "spend"), ("spend",), ("cost",)]


def _find(cols, *groups):
    low = {c.lower(): c for c in cols}
    for g in groups:
        for lc, orig in low.items():
            if all(t in lc for t in g):
                return orig
    return None


def _stream(ws):
    it = ws.iter_rows(values_only=True)
    try:
        header = next(it)
    except StopIteration:
        return None
    wanted = {}
    for i, h in enumerate(header):
        if h is None:
            continue
        hl = str(h).lower()
        if any(all(t in hl for t in g) for g in NEED):
            wanted[str(h)] = i
    if not wanted:
        return None
    data = {n: [] for n in wanted}
    for r in it:
        for n, i in wanted.items():
            data[n].append(r[i] if i < len(r) else None)
    return pd.DataFrame(data)


def analyze_exchanges(path_or_buffer, min_spend=150.0, min_impr=50000):
    wb = load_workbook(path_or_buffer, read_only=True, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            return None
        df = _stream(wb[SHEET])
    finally:
        wb.close()
    if df is None or df.empty:
        return None

    ex = _find(df.columns, ("exchange",))
    imp = _find(df.columns, ("impression",))
    clk = _find(df.columns, ("click",))
    spd = _find(df.columns, ("billable", "spend"), ("spend",), ("cost",))
    if not ex:
        return None
    for c in (imp, clk, spd):
        if c:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    g = (df.groupby(ex)
         .agg(impressions=(imp, "sum"), clicks=(clk, "sum"), spend=(spd, "sum"))
         .reset_index().rename(columns={ex: "exchange"}))
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"], 0)

    total_spend = g["spend"].sum() or 1
    g["pct_of_spend"] = g["spend"] / total_spend
    book_ctr = (g["clicks"].sum() / g["impressions"].sum()) if g["impressions"].sum() else 0

    material = g[(g["spend"] >= min_spend) & (g["impressions"] >= min_impr)].copy()
    hi = book_ctr * 2.5   # abnormally high
    lo = book_ctr * 0.25  # abnormally low
    material["flag"] = np.where(
        material["ctr"] >= hi, "CTR abnormally high (possible invalid traffic)",
        np.where(material["ctr"] <= lo, "CTR abnormally low (likely wasted / non-engaging)", ""))
    flags = material[material["flag"] != ""].sort_values("spend", ascending=False)

    concentration = g.sort_values("spend", ascending=False).head(1)
    top_share = float(concentration["pct_of_spend"].iloc[0]) if len(concentration) else 0

    summary = {
        "exchanges": int(len(g)),
        "book_ctr": float(book_ctr),
        "total_spend": float(g["spend"].sum()),
        "flag_count": int(len(flags)),
        "flag_spend": float(flags["spend"].sum()),
        "top_exchange": concentration["exchange"].iloc[0] if len(concentration) else None,
        "top_share": top_share,
    }
    return {
        "summary": summary,
        "table": g.sort_values("spend", ascending=False),
        "flags": flags,
    }

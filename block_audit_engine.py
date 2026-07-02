"""
block_audit_engine.py
Answers: "placements we've flagged to block — are they actually blocked?"

In the AdLib Insights workbook, the Site Overview and App Overview sheets carry a
'Final ... Name' column whose value is the literal sentinel "Block" when that
placement is supposed to be blocked. Any such row that still shows impressions /
billable spend is a BLOCK THAT ISN'T BEING ENFORCED — a leak.

This engine finds those leaks and attributes the wasted spend/impressions to each
Business Unit (partner), Client, Product, and Strategy.
"""
import pandas as pd
import numpy as np
from openpyxl import load_workbook

SENTINELS = {"block", "blocked"}

# Column-name token groups we need to pull when streaming the big sheets.
NEED_TOKENS = [("final",), ("site", "domain"), ("app", "name"), ("impression",),
               ("click",), ("billable", "spend"), ("spend",), ("cost",), ("date",),
               ("business", "unit"), ("client",), ("product",), ("strategy",)]

SHEET_CONFIG = {
    "Site Overview": {"kind": "site"},
    "App Overview": {"kind": "app"},
}


def _find(cols, *tokens_any):
    """Return first column whose lowercased name contains ALL tokens in any group."""
    low = {c.lower(): c for c in cols}
    for group in tokens_any:
        for lc, orig in low.items():
            if all(t in lc for t in group):
                return orig
    return None


def _detect(df, kind):
    cols = df.columns
    final = _find(cols, ("final", "site"), ("final", "app"), ("final", "domain"))
    raw = _find(cols, ("site", "domain"), ("app", "name")) or final
    return {
        "final": final,
        "raw": raw,
        "impr": _find(cols, ("impression",)),
        "clicks": _find(cols, ("click",)),
        "spend": _find(cols, ("billable", "spend"), ("spend",), ("cost",)),
        "date": _find(cols, ("date",)),
        "bu": _find(cols, ("business", "unit")),
        "client": _find(cols, ("client",)),
        "product": _find(cols, ("product",)),
        "strategy": _find(cols, ("strategy", "type"), ("strategy",)),
    }


def _normalize(df, c):
    out = pd.DataFrame()
    out["placement"] = df[c["raw"]].astype(str)
    out["final"] = df[c["final"]].astype(str).str.strip()
    out["impressions"] = pd.to_numeric(df[c["impr"]], errors="coerce").fillna(0) if c["impr"] else 0
    out["clicks"] = pd.to_numeric(df[c["clicks"]], errors="coerce").fillna(0) if c["clicks"] else 0
    out["spend"] = pd.to_numeric(df[c["spend"]], errors="coerce").fillna(0) if c["spend"] else 0
    out["served_date"] = pd.to_datetime(df[c["date"]], errors="coerce") if c["date"] else pd.NaT
    for dim in ("bu", "client", "product", "strategy"):
        out[dim] = df[c[dim]].astype(str) if c[dim] else "(not in export)"
    out["is_block"] = out["final"].str.lower().isin(SENTINELS)
    out["is_unresolved"] = df[c["final"]].isna() | (out["final"].str.lower().isin({"nan", ""}))
    return out


def _rollup(leak, dim):
    if leak.empty:
        return pd.DataFrame(columns=[dim, "leaked_impressions", "leaked_spend", "placements"])
    g = (leak.groupby(dim)
         .agg(leaked_impressions=("impressions", "sum"), leaked_spend=("spend", "sum"),
              placements=("placement", "nunique"))
         .reset_index().sort_values("leaked_spend", ascending=False))
    return g


def _stream_sheet(ws):
    """Stream a worksheet in read_only mode, pulling only the columns we need.
    Keeps peak memory low (never materializes the full sheet) and avoids loading
    the whole 40MB workbook. Returns (slim_df, row_count)."""
    it = ws.iter_rows(values_only=True)
    try:
        header = next(it)
    except StopIteration:
        return None, 0
    wanted = {}
    for i, h in enumerate(header):
        if h is None:
            continue
        hl = str(h).lower()
        if any(all(t in hl for t in grp) for grp in NEED_TOKENS):
            wanted[str(h)] = i
    if not wanted:
        return None, 0
    data = {name: [] for name in wanted}
    n = 0
    for r in it:
        for name, i in wanted.items():
            data[name].append(r[i] if i < len(r) else None)
        n += 1
    return pd.DataFrame(data), n


def audit_block_leak(path_or_buffer):
    wb = load_workbook(path_or_buffer, read_only=True, data_only=True)
    frames = []
    truncation = {}
    try:
        for sheet, cfg in SHEET_CONFIG.items():
            if sheet not in wb.sheetnames:
                continue
            df, nrows = _stream_sheet(wb[sheet])
            if df is None:
                continue
            c = _detect(df, cfg["kind"])
            if not c["final"]:
                continue
            norm = _normalize(df, c)
            norm["placement_type"] = cfg["kind"]
            frames.append(norm)
            truncation[sheet] = nrows >= 100000  # Tap export row cap heuristic
    finally:
        wb.close()

    if not frames:
        raise ValueError("No Site/App Overview sheets with a 'Final ... Name' column found.")

    allp = pd.concat(frames, ignore_index=True)
    leak = allp[allp["is_block"] & (allp["impressions"] > 0)]
    unresolved = allp[allp["is_unresolved"] & (allp["impressions"] > 0)]

    # Recency: a trailing report shows impressions that may predate the block.
    # Where a date exists (apps), use last-served to tell "stopped mid-window"
    # (block likely took hold) from "still serving at the window edge" (verify).
    window_end = allp["served_date"].max()
    window_start = allp["served_date"].min()
    ACTIVE_DAYS = 1  # served on/after window_end - 1 day = still active

    by_type = (leak.groupby("placement_type")
               .agg(rows=("placement", "size"), placements=("placement", "nunique"),
                    impressions=("impressions", "sum"), spend=("spend", "sum")).reset_index())

    offenders = (leak.groupby(["placement", "placement_type"])
                 .agg(impressions=("impressions", "sum"), clicks=("clicks", "sum"),
                      spend=("spend", "sum"), last_served=("served_date", "max"))
                 .reset_index().sort_values("spend", ascending=False))
    if pd.notna(window_end):
        no_date = offenders["last_served"].isna()
        offenders["days_since_last"] = (window_end - offenders["last_served"]).dt.days
        offenders["still_active"] = (offenders["days_since_last"] <= ACTIVE_DAYS).astype("object")
        offenders.loc[no_date, "still_active"] = pd.NA
    else:
        offenders["days_since_last"] = pd.NA
        offenders["still_active"] = pd.NA
    offenders["last_served"] = offenders["last_served"].dt.strftime("%Y-%m-%d").fillna("(no date)")

    active = offenders[offenders["still_active"] == True]  # noqa: E712

    # Distinct names already flagged "Block" (for copyable AdLib filter syntax)
    blocked = allp[allp["is_block"]]
    block_names = {
        "site": sorted(blocked.loc[blocked["placement_type"] == "site", "placement"].dropna().unique().tolist()),
        "app": sorted(blocked.loc[blocked["placement_type"] == "app", "placement"].dropna().unique().tolist()),
    }

    # Candidates for the AI pass: placements NOT already flagged Block, with real
    # delivery, aggregated distinct by spend (so we send Claude a ranked shortlist).
    already = {k: set(v) for k, v in block_names.items()}
    cand = allp[(~allp["is_block"]) & (allp["impressions"] > 0)]

    def _candidates(kind):
        d = (cand[cand["placement_type"] == kind]
             .groupby("placement")
             .agg(impressions=("impressions", "sum"), clicks=("clicks", "sum"),
                  spend=("spend", "sum"))
             .reset_index().rename(columns={"placement": "name"}))
        d = d[~d["name"].isin(already.get(kind, set()))]
        return d.sort_values("spend", ascending=False).reset_index(drop=True)

    candidates = {"site": _candidates("site"), "app": _candidates("app")}

    summary = {
        "leaked_spend": float(leak["spend"].sum()),
        "leaked_impressions": float(leak["impressions"].sum()),
        "leaked_placements": int(leak["placement"].nunique()),
        "leaked_rows": int(len(leak)),
        "by_type": by_type.to_dict("records"),
        "truncated_sheets": [s for s, t in truncation.items() if t],
        "unresolved_placements": int(unresolved["placement"].nunique()),
        "unresolved_spend": float(unresolved["spend"].sum()),
        "window_start": window_start.strftime("%b %d") if pd.notna(window_start) else None,
        "window_end": window_end.strftime("%b %d") if pd.notna(window_end) else None,
        "active_placements": int(len(active)),
        "active_spend": float(active["spend"].sum()) if len(active) else 0.0,
        "has_dates": bool(pd.notna(window_end)),
    }

    return {
        "summary": summary,
        "offenders": offenders,
        "leak_by_bu": _rollup(leak, "bu"),
        "leak_by_client": _rollup(leak, "client"),
        "leak_by_product": _rollup(leak, "product"),
        "leak_by_strategy": _rollup(leak, "strategy"),
        "block_names": block_names,
        "candidates": candidates,
    }

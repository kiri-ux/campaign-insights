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

SENTINELS = {"block", "blocked"}

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


def audit_block_leak(path_or_buffer):
    xls = pd.ExcelFile(path_or_buffer)
    frames = []
    truncation = {}
    for sheet, cfg in SHEET_CONFIG.items():
        if sheet not in xls.sheet_names:
            continue
        df = pd.read_excel(xls, sheet_name=sheet)
        c = _detect(df, cfg["kind"])
        if not c["final"]:
            continue
        norm = _normalize(df, c)
        norm["placement_type"] = cfg["kind"]
        frames.append(norm)
        truncation[sheet] = len(df) >= 100000  # Tap export row cap heuristic

    if not frames:
        raise ValueError("No Site/App Overview sheets with a 'Final ... Name' column found.")

    allp = pd.concat(frames, ignore_index=True)
    leak = allp[allp["is_block"] & (allp["impressions"] > 0)]
    unresolved = allp[allp["is_unresolved"] & (allp["impressions"] > 0)]

    by_type = (leak.groupby("placement_type")
               .agg(rows=("placement", "size"), placements=("placement", "nunique"),
                    impressions=("impressions", "sum"), spend=("spend", "sum")).reset_index())

    summary = {
        "leaked_spend": float(leak["spend"].sum()),
        "leaked_impressions": float(leak["impressions"].sum()),
        "leaked_placements": int(leak["placement"].nunique()),
        "leaked_rows": int(len(leak)),
        "by_type": by_type.to_dict("records"),
        "truncated_sheets": [s for s, t in truncation.items() if t],
        "unresolved_placements": int(unresolved["placement"].nunique()),
        "unresolved_spend": float(unresolved["spend"].sum()),
    }

    offenders = (leak.groupby(["placement", "placement_type"])
                 .agg(impressions=("impressions", "sum"), clicks=("clicks", "sum"),
                      spend=("spend", "sum"))
                 .reset_index().sort_values("spend", ascending=False))

    return {
        "summary": summary,
        "offenders": offenders,
        "leak_by_bu": _rollup(leak, "bu"),
        "leak_by_client": _rollup(leak, "client"),
        "leak_by_product": _rollup(leak, "product"),
        "leak_by_strategy": _rollup(leak, "strategy"),
    }

"""
placement_engine.py
Scores a site/app-grain export (the deeper Tap pull) and produces:
  - a block list (high-confidence junk placements)
  - attribution of the blocked waste UP to Business Unit / Product / Strategy
    (answers "how does blocking affect our partners, products, strategies")

If the export carries BU/Product/Strategy columns per placement row, the roll-up
is exact. If it doesn't, the block list still stands on its own.

The audience-fit layer (recognizable publishers whose fit depends on the campaign
audience) is left to classify_with_llm() — wired to Claude at deploy time.
"""
import os
import re
import pandas as pd
import numpy as np

PLACEMENT_CANDS = ["site/app", "site / app", "app/site", "site", "app", "placement",
                   "domain", "publisher", "inventory", "site name", "app name", "bundle"]
IMPR_CANDS = ["impression", "impr", "imps"]
CLICK_CANDS = ["click"]
SPEND_CANDS = ["internal cost", "spend", "cost", "media cost", "amount"]
CONV_CANDS = ["click conversion", "conversion", "conv", "actions"]
BU_CANDS = ["client business unit", "business unit"]
PROD_CANDS = ["product"]
STRAT_CANDS = ["strategy type", "strategy"]

JUNK_RULES = [
    ("Photo/Beauty Editor App", "BLOCK", re.compile(
        r"\b(b612|beauty\s?plus|beautyplus|youcam|sweet\s?selfie|candy\s?camera|"
        r"natural\s?beauty|beauty\s?camera|selfie|photo\s?(&|and)?\s?video\s?edit|"
        r"photo\s?edit|retrica|meitu|facetune|cymera|z\s?camera|makeup\s?cam)", re.I)),
    ("Rewarded / Casual Game", "BLOCK", re.compile(
        r"\b(block!?\s?(puzzle|app)?|block\s?puzzle|bubble\s?shoot|woodoku|candy\s?crush|"
        r"match\s?3|solitaire|2048|mahjong|coin\s?master|slots?|casino|spin\s?to\s?win|"
        r"lucky\s?(spin|wheel|day)|scratch\s?(off|win)|idle\s|merge\s|\.io\b|jewel\s?blast)", re.I)),
    ("Utility-Junk App", "BLOCK", re.compile(
        r"\b(flashlight|battery\s?(saver|doctor)|du\s?battery|phone\s?clean|clean\s?master|"
        r"junk\s?clean|speed\s?boost|booster|antivirus|qr\s?(scanner|code)|wallpaper|"
        r"ringtone|keyboard|file\s?manager|compass|magnifier)", re.I)),
    ("Opaque / Unknown Inventory", "BLOCK", re.compile(
        r"^(unknown|n/?a|not\s?available|other|mobile|in-?app|app|display|video|untracked|\(?not\s?set\)?)$", re.I)),
    ("Unresolved App Bundle", "BLOCK", re.compile(r"^(com|net|org|io)\.[a-z0-9_.]+$", re.I)),
    ("Kids / Coloring App", "REVIEW", re.compile(
        r"\b(coloring|nursery\s?rhyme|toddler|preschool|abc\s?kids|baby\s?game)\b", re.I)),
    # Mobile game titles the keyword list above misses. Games are named from a
    # small vocabulary — an action word plus hero/master/saga/quest — and it is
    # the CATEGORY that makes them junk inventory, not any single franchise.
    # 'Slicing Hero: Sword Master' matched nothing before this rule existed.
    ("Casual Game (title pattern)", "BLOCK", re.compile(
        r"\b(hero|master|saga|quest|clash|tycoon|simulator|clicker|craft|ninja|"
        r"warrior|sword|slic(e|ing)|smash|crush|blast|dash|rush|jump|shoot(er|ing)|"
        r"battle|legend|empire|kingdom|farm|garden|bubble|puzzle|tiles?|sudoku|"
        r"word\s?(search|connect|cross)|brain\s?(game|test)|escape|survival)\b", re.I)),
]

# A placement's CTR is the one junk signal that needs no name at all. Real
# display inventory does not click at 5%, let alone 46% — that is invalid
# traffic or a broken click macro, and it is worth blocking whatever the site
# is called. Both floors matter: without the impression floor, 1 click on 2
# impressions reads as 50%.
CTR_BLOCK = float(os.environ.get("PLACEMENT_CTR_BLOCK", "0.10"))
CTR_REVIEW = float(os.environ.get("PLACEMENT_CTR_REVIEW", "0.05"))
CTR_MIN_IMPR = float(os.environ.get("PLACEMENT_CTR_MIN_IMPR", "100"))
KNOWN_PUBLISHER = re.compile(
    r"\b(fox\s?news|cnn|msnbc|al\s?jazeera|nytimes|washington\s?post|usa\s?today|"
    r"weather\.com|accuweather|espn|yahoo|forbes|buzzfeed|daily\s?mail|hulu|roku|"
    r"pluto\s?tv|tubi|peacock|paramount|pandora|spotify|iheart)", re.I)


def _detect(cols, cands):
    lc = {c.lower().strip(): c for c in cols}
    for cand in cands:
        for low, orig in lc.items():
            if low == cand:
                return orig
    for cand in cands:
        for low, orig in lc.items():
            if cand in low:
                return orig
    return None


def categorize(name):
    if not isinstance(name, str) or not name.strip():
        return ("Blank / Missing Name", "BLOCK")
    for cat, concern, rx in JUNK_RULES:
        if rx.search(name.strip()):
            return (cat, concern)
    if KNOWN_PUBLISHER.search(name):
        return ("Recognizable Publisher (audience-fit check)", "REVIEW")
    return ("Unclassified", "REVIEW")


def score_placements(df):
    cols = {
        "placement": _detect(df.columns, PLACEMENT_CANDS),
        "impr": _detect(df.columns, IMPR_CANDS),
        "clicks": _detect(df.columns, CLICK_CANDS),
        "spend": _detect(df.columns, SPEND_CANDS),
        "conv": _detect(df.columns, CONV_CANDS),
        "bu": _detect(df.columns, BU_CANDS),
        "product": _detect(df.columns, PROD_CANDS),
        "strategy": _detect(df.columns, STRAT_CANDS),
    }
    if not cols["placement"]:
        raise ValueError(f"No placement/site column found. Columns: {list(df.columns)}")
    w = df.copy()
    w["placement"] = w[cols["placement"]].astype(str)
    for role in ("impr", "clicks", "spend", "conv"):
        w[role] = pd.to_numeric(w[cols[role]], errors="coerce").fillna(0) if cols[role] else 0
    cat = w["placement"].apply(categorize)
    w["category"] = cat.apply(lambda x: x[0])
    w["concern"] = cat.apply(lambda x: x[1])

    # CTR override, applied AFTER the name rules so it can promote a placement
    # no keyword recognized. An implausible click rate outranks any name-based
    # verdict, including 'Recognizable Publisher'.
    w["ctr"] = np.where(w["impr"] > 0, w["clicks"] / w["impr"], 0.0)
    eligible = w["impr"] >= CTR_MIN_IMPR
    hot = eligible & (w["ctr"] >= CTR_BLOCK)
    warm = eligible & (w["ctr"] >= CTR_REVIEW) & ~hot
    if hot.any():
        w.loc[hot, "category"] = ("Implausible CTR — likely invalid traffic ("
                                  + (w.loc[hot, "ctr"] * 100).round(1).astype(str) + "%)")
        w.loc[hot, "concern"] = "BLOCK"
    if warm.any():
        w.loc[warm, "category"] = ("Elevated CTR — verify ("
                                   + (w.loc[warm, "ctr"] * 100).round(1).astype(str) + "%)")
        w.loc[warm, "concern"] = "REVIEW"
    for role in ("bu", "product", "strategy"):
        w[role] = w[cols[role]] if cols[role] else "(not in export)"
    return w, cols


def attribute_blocked_waste(scored, dim):
    """Roll blocked spend/impressions up to a dimension (bu/product/strategy)."""
    blk = scored[scored["concern"] == "BLOCK"]
    if blk.empty:
        return pd.DataFrame(columns=[dim, "blocked_impressions", "blocked_spend", "blocked_placements"])
    g = (blk.groupby(dim)
         .agg(blocked_impressions=("impr", "sum"), blocked_spend=("spend", "sum"),
              blocked_placements=("placement", "nunique"))
         .reset_index().sort_values("blocked_spend", ascending=False))
    return g


def build_block_report(df):
    scored, cols = score_placements(df)
    block = scored[scored["concern"] == "BLOCK"]
    summary = {
        "placements": int(len(scored)),
        "block_count": int(len(block)),
        "blocked_impressions": float(block["impr"].sum()),
        "blocked_spend": float(block["spend"].sum()),
        "has_dimensions": bool(cols["bu"] or cols["product"] or cols["strategy"]),
    }
    return {
        "summary": summary,
        "scored": scored,
        "block_list": block[["placement", "category", "impr", "clicks", "spend", "bu", "product", "strategy"]],
        "waste_by_bu": attribute_blocked_waste(scored, "bu"),
        "waste_by_product": attribute_blocked_waste(scored, "product"),
        "waste_by_strategy": attribute_blocked_waste(scored, "strategy"),
    }

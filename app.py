"""
AdLib Placement & Impact Insights — Flask app (Render-ready)
Upload the AdLib Insights workbook -> Business Unit / Product / Strategy insights.
Upload a site/app-grain export -> block list + waste attributed to BU/Product/Strategy.
"""
import io
import os
import gc
import re
import json
import tempfile
import threading
import urllib.request
from flask import Flask, request, render_template, send_file, abort, jsonify
import pandas as pd

from insights_engine import build_insights
from block_audit_engine import audit_block_leak
from exchange_engine import analyze_exchanges
from ai_blocks import recommend_blocks, to_adlib_filter, merge_app_blocks, merge_site_blocks
from product_map import build_pmap
from buyer_map import load_buyer_map, buyer_for
from blocklist_read import load_blocklist

PMAP = build_pmap()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40 MB
_CACHE = {}  # token -> {"name": df} for CSV downloads within the session
_ANALYSIS_LOCK = threading.Lock()  # serialize heavy runs so they can't stack in memory


def _build_version():
    """Build stamp baked into VERSION at package time; 'dev' if absent."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


BUILD_VERSION = _build_version()


@app.context_processor
def _inject_build_version():
    """Make the build stamp available to every template (home, dashboards,
    saved-report snapshots) without threading it through each ctx dict."""
    return {"build_version": BUILD_VERSION}


def _norm_site(s):
    """Normalize a site domain for matching: lowercase, drop scheme, path, query,
    a leading 'www.', and any trailing dot. So 'https://www.TMZ.com/foo' and
    'tmz.com' compare equal — otherwise an excluded site slips back into the
    recommendations on a www/case/path variant."""
    s = str(s).strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)   # strip http:// / https://
    s = s.split("/")[0].split("?")[0]  # drop path + query
    if s.startswith("www."):
        s = s[4:]
    return s.rstrip(".")


def _fmt(df, pct_cols=(), money_cols=(), int_cols=()):
    d = df.copy()
    for c in int_cols:
        if c in d: d[c] = d[c].map(lambda x: f"{x:,.0f}")
    for c in money_cols:
        if c in d:
            _f = (lambda x: f"${x:,.2f}") if "cpm" in str(c).lower() else (lambda x: f"${x:,.0f}")
            d[c] = d[c].map(_f)
    for c in pct_cols:
        if c in d: d[c] = d[c].map(lambda x: "" if pd.isna(x) else f"{x:.2%}")
    return d


def _add_cpm(df, impr_col="impressions", spend_col="spend"):
    """Attach a CPM column (spend/impr*1000) wherever both inputs exist."""
    if df is not None and len(df) and impr_col in df.columns and spend_col in df.columns:
        i = pd.to_numeric(df[impr_col], errors="coerce")
        sp = pd.to_numeric(df[spend_col], errors="coerce").fillna(0)
        df["cpm"] = (sp / i * 1000).where(i > 0, 0).fillna(0)
    return df


def _fmt_report_name(d):
    """Human display for a report id: '2026-07-16_to_2026-07-22' -> '07/16/26 – 07/22/26',
    '2026-07-23' -> '07/23/26'. Unparseable parts pass through untouched."""
    import datetime as _dt
    def _one(iso):
        try:
            return _dt.date.fromisoformat(iso).strftime("%m/%d/%y")
        except ValueError:
            return iso
    if "_to_" in d:
        a, b = d.split("_to_", 1)
        return f"{_one(a)} – {_one(b)}"
    return _one(d)


app.jinja_env.filters["report_name"] = _fmt_report_name


@app.route("/")
def index():
    return render_template("index.html",
                           build_version=BUILD_VERSION,
                           default_pull_days=int(os.environ.get("DEFAULT_PULL_DAYS", "7")),
                           reports=_list_reports()[:12],
                           has_source=bool(os.environ.get("S3_BUCKET", "").strip()
                                           or os.environ.get("GRAPH_CLIENT_ID", "").strip()
                                           or os.environ.get("IMAP_USER", "").strip()),
                           has_email=bool(os.environ.get("EMAIL_FROM", "").strip()
                                          and os.environ.get("EMAIL_TO", "").strip()))


@app.route("/version")
def version():
    return jsonify({"build": BUILD_VERSION})


@app.route("/analyze", methods=["POST"])
def analyze():
    wb = request.files.get("insights_workbook")
    if not (wb and wb.filename):
        return render_template("dashboard.html", pmap=PMAP, errors=["Upload the Insights workbook (.xlsx)."])
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)
    tmp.close()
    try:
        ctx = _analyze_path(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        gc.collect()
    return render_template("dashboard.html", **ctx)


def _analyze_path(path=None, frames=None):
    """Run the full analysis and return the template ctx. Pass either an .xlsx
    `path` (manual upload) or pre-built `frames` (automated pull — no xlsx read)."""
    # Free the previous run's cached DataFrames before loading a new dataset —
    # every MB matters on a small instance, and disk-persisted CSVs still serve
    # downloads for saved reports while the new run repopulates the cache.
    _CACHE.clear()
    gc.collect()
    ctx = {"insights": None, "audit": None, "blocks": None, "ai": None,
           "clients": None, "clients_total": 0, "has_buyer": False,
           "exchanges": None, "top": None, "block_impact": None,
           "partner": None, "pmap": PMAP, "blocklist_check": None, "topcards": None,
           "block_impact_strategy": None,
           "low_ctr_sites": None, "low_ctr_sites_total": 0, "low_ctr_sites_acct_blocked": 0,
           "blocked_site_clients": None, "rec_high": None, "rec_low": None,
           "has_blocklist": bool(os.environ.get("BLOCKLIST_WEBHOOK_URL", "").strip()),
           "errors": []}
    perf_bu = pd.DataFrame()
    cflag = pd.DataFrame()
    sf = pd.DataFrame()
    bmap = load_buyer_map()  # {} unless BUYER_MAP_URL env var is set
    blocklist = load_blocklist()  # {} unless BLOCKLIST_READ_URL env var is set
    excluded = set(load_blocklist(tabs=["Excluded"]).keys()) if os.environ.get("BLOCKLIST_READ_URL", "").strip() else set()
    _CACHE.clear()
    try:
        try:
            r = build_insights(path, frames=frames)
            _CACHE["by_business_unit.csv"] = r["by_business_unit"]
            _CACHE["by_product.csv"] = r["by_product"]
            _CACHE["by_strategy.csv"] = r["by_strategy"]
            _CACHE["strategy_flags.csv"] = r["strategy_flags"]
            sf = r["strategy_flags"]
            ctx["insights"] = {
                "summary": r["summary"],
                "product": _fmt(r["by_product"], pct_cols=["ctr", "click_conv_rate", "pct_of_spend"],
                                money_cols=["internal_cost"], int_cols=["impressions", "clicks", "conversions"]).to_dict("records"),
                "strategy": _fmt(r["by_strategy"], pct_cols=["ctr"], money_cols=["internal_cost", "cost_per_conv"],
                                 int_cols=["impressions", "clicks", "conversions"]).to_dict("records"),
                "strategy_flags": _fmt(_add_cpm(sf, spend_col="internal_cost").head(30),
                                        pct_cols=["ctr", "type_ctr"], money_cols=["internal_cost", "cpm"],
                                       int_cols=["impressions", "clicks", "conversions"]).to_dict("records") if len(sf) else [],
            }
            perf_bu = r["by_business_unit"]  # kept for the combined Partner grid
            cflag = r.get("client_flags", pd.DataFrame())
            if len(cflag):
                _CACHE["client_flags.csv"] = cflag
                _add_cpm(cflag, spend_col="internal_cost")
                crows = _fmt(cflag.head(50), pct_cols=["ctr", "product_ctr"],
                             money_cols=["internal_cost", "cpm"],
                             int_cols=["impressions", "clicks"]).to_dict("records")
                for row in crows:
                    row["buyer"] = buyer_for(row.get("business_unit", ""), bmap)
                ctx["clients"] = crows
                ctx["clients_total"] = int(len(cflag))
                ctx["has_buyer"] = bool(bmap)
            del r
        except Exception as e:
            ctx["errors"].append(f"Insights workbook: {e}")

        gc.collect()  # release the performance frames before the big Site/App parse

        try:
            a = audit_block_leak(path, blocklist=blocklist, frames=frames)
            _CACHE["block_leak_offenders.csv"] = a["offenders"]
            _CACHE["block_leak_by_bu.csv"] = a["leak_by_bu"]
            _CACHE["block_leak_by_client.csv"] = a["leak_by_client"]
            _CACHE["block_leak_by_product.csv"] = a["leak_by_product"]
            _CACHE["block_leak_by_strategy.csv"] = a["leak_by_strategy"]
            ctx["audit"] = {
                "summary": a["summary"],
                "has_conv": a.get("has_conv", False),
            }

            # Low-CTR site watchlist (by client): sites both far below their
            # product's CTR norm AND under an absolute floor. CTV/SM CTV/Online
            # Audio are excluded (low CTR is expected there).
            lcs = a.get("low_ctr_sites", pd.DataFrame())
            _CACHE["low_ctr_sites_by_client.csv"] = lcs
            if len(lcs):
                # Which of these per-client sites are ALSO on the account-level
                # recommended block list (low across ALL clients). We only block at
                # the account level, not per individual client — this lets buyers see
                # which watchlist sites are actually getting blocked for everyone.
                acct_block_sites = set()
                _asb = a.get("auto_site_blocks", pd.DataFrame())
                if _asb is not None and len(_asb) and "name" in _asb:
                    acct_block_sites = {_norm_site(n) for n in _asb["name"].tolist()}
                _add_cpm(lcs)
                lrows = _fmt(lcs.head(100), pct_cols=["ctr", "product_ctr", "conv_rate"],
                             money_cols=["spend", "cpm"],
                             int_cols=["impressions", "clicks", "conversions"]).to_dict("records")
                for row in lrows:
                    row["buyer"] = buyer_for(row.get("business_unit", ""), bmap)
                    row["acct_blocked"] = _norm_site(row.get("site", "")) in acct_block_sites
                ctx["low_ctr_sites"] = lrows
                ctx["low_ctr_sites_total"] = int(len(lcs))
                ctx["low_ctr_sites_acct_blocked"] = sum(1 for r in lrows if r["acct_blocked"])
                ctx["has_buyer"] = ctx["has_buyer"] or bool(bmap)

            # Combined Partner grid: performance (all delivery) + block-leak exposure,
            # one row per partner, sortable.
            bbu = a.get("blocklist_by_bu")
            if bbu is not None and len(bbu):
                leaked = bbu.rename(columns={"bu": "business_unit"})
            else:
                leaked = a["leak_by_bu"].rename(columns={"bu": "business_unit",
                         "leaked_impressions": "blocked_impr", "placements": "blocked_placements"})
            leaked = leaked[["business_unit", "blocked_impr", "blocked_placements"]]
            if len(perf_bu):
                pm = perf_bu.merge(leaked, on="business_unit", how="left")
                pm[["blocked_impr", "blocked_placements"]] = pm[["blocked_impr", "blocked_placements"]].fillna(0)
            else:  # insights failed — fall back to leaked-only
                pm = leaked.assign(impressions=0, clicks=0, ctr=0, conversions=0,
                                   internal_cost=0, cost_per_conv=float("nan"), flagged=False)
            # Watchlist = only partners meeting the tiered CTR-vs-volume flag
            # (>=10K impr & CTR >2.5%, or >=30K & >1%). Unflagged
            # partners drop off the grid, the CSV, and the watchlist Excel tab.
            if "flagged" in pm:
                pm = pm[pm["flagged"].astype(bool)].reset_index(drop=True)
            _CACHE["partner_summary.csv"] = pm
            if "impressions" in pm:
                pm["cpm"] = (pm["internal_cost"] / pm["impressions"] * 1000).where(pm["impressions"] > 0, 0).fillna(0)
            if bmap:
                pm["buyer"] = pm["business_unit"].map(lambda b: buyer_for(b, bmap))
            flagged = pm.get("flagged", pd.Series([False] * len(pm))).tolist()
            prow = _fmt(pm.head(60), pct_cols=["ctr"], money_cols=["internal_cost", "cost_per_conv", "cpm"],
                        int_cols=["impressions", "clicks", "conversions", "blocked_impr", "blocked_placements"]).to_dict("records")
            for row, fl in zip(prow, flagged):
                row["flagged"] = bool(fl)
                row["buyer"] = buyer_for(row.get("business_unit", ""), bmap)
            ctx["partner"] = prow
            ctx["has_buyer"] = bool(bmap)

            # Master-blocklist leak check
            bc = a.get("blocklist_check")
            if bc:
                _add_cpm(bc["rows"])
                _CACHE["blocklist_check.csv"] = bc["rows"]
                brows = _fmt(bc["rows"].head(100), money_cols=["spend", "post_spend", "cpm"],
                             int_cols=["impressions", "post_impr"]).to_dict("records")
                ctx["blocklist_check"] = {
                    "matched": bc["matched"], "leaking_count": bc["leaking_count"],
                    "leaking_spend": bc["leaking_spend"], "rows": brows,
                }

            # Separate grid: clients serving on blocklisted placements (verify their
            # block settings). Kept in `bsc_df` for the watchlist-xlsx cache below.
            bsc_df = a.get("blocked_site_clients")
            if bsc_df is not None and len(bsc_df):
                # sites_list is a Python list (for the full-width drawer); drop it from
                # the flat CSV/Excel outputs, which keep the joined `sites` string.
                _add_cpm(bsc_df)
                bsc_flat = bsc_df.drop(columns=["sites_list"], errors="ignore")
                _CACHE["clients_on_blocked_sites.csv"] = bsc_flat
                leak_flags = (bsc_df["post_impr"] > 0).tolist()
                brows2 = _fmt(bsc_df.head(200), pct_cols=["ctr"],
                              money_cols=["spend", "post_spend", "cpm"],
                              int_cols=["impressions", "clicks", "post_impr",
                                        "n_sites", "n_site", "n_app"]).to_dict("records")
                for row, lf in zip(brows2, leak_flags):
                    row["buyer"] = buyer_for(row.get("business_unit", ""), bmap)
                    row["leaking"] = bool(lf)
                    if not isinstance(row.get("sites_list"), list):
                        row["sites_list"] = [s for s in str(row.get("sites", "")).split(", ") if s]
                ctx["blocked_site_clients"] = brows2
                # True when no row carries a Strategy ID — i.e. the Campaign ID
                # column never made it through from the export. Surfaced in the
                # grid note so a schema/mapping problem is visible, not silent.
                _sid = bsc_df.get("Strategy ID")
                ctx["bsc_no_sid"] = bool(_sid is None or _sid.astype(str).str.strip()
                                         .isin(["", "nan", "none"]).all())
                ctx["has_buyer"] = ctx["has_buyer"] or bool(bmap)

            # AI runs on every upload now. Merge Claude's picks with the
            # deterministic gaming/junk/unresolved auto-block. Apps key on App ID.
            rec = recommend_blocks(a["candidates"])
            # Merge AI site picks with the deterministic auto-blocks: abnormally HIGH
            # CTR (invalid traffic) and across-the-board LOW CTR / no-conversion sites.
            rec_site = merge_site_blocks(rec.get("site", pd.DataFrame()),
                                         a.get("auto_site_blocks", pd.DataFrame()),
                                         a.get("auto_high_ctr_site_blocks", pd.DataFrame()))
            if len(rec_site) and "impressions" in rec_site:
                rec_site = rec_site.sort_values("impressions", ascending=False)  # sites by impr high-low
            rec_app = merge_app_blocks(rec.get("app", pd.DataFrame()), a["auto_app_blocks"])
            # Drop anything you've previously unchecked (logged to the Excluded tab),
            # so it stops being recommended on every upload.
            if excluded:
                # Sites: match on a normalized domain so www/case/path variants of an
                # excluded site are still dropped. Apps: exact match on the App ID.
                excluded_sites = {_norm_site(k) for k in excluded}
                if len(rec_site) and "name" in rec_site:
                    rec_site = rec_site[~rec_site["name"].map(
                        lambda x: _norm_site(x) in excluded_sites
                        or str(x).strip().lower() in excluded)]
                if len(rec_app) and "app_id" in rec_app:
                    rec_app = rec_app[~rec_app["app_id"].astype(str).str.strip().str.lower().isin(excluded)]

            # Flag every recommended block: High CTR (abnormal CTR — possible invalid
            # traffic), Low CTR (across-the-board low-CTR/no-conversion sites), or
            # Quality (AI-flagged MFA/junk/brand-safety sites + gaming/junk/unresolved
            # apps — blocked on quality, not CTR). is_low_ctr_block marks the account-
            # level low-CTR/no-conv site blocks (highlighted in the UI).
            def _flag(c):
                # Category may now be composite ("MFA + High CTR"); map each
                # component and de-dupe, e.g. -> "Quality + High CTR".
                parts = []
                for x in [p.strip() for p in str(c).split(" + ")] or [str(c)]:
                    if x == "High CTR":
                        parts.append("High CTR")
                    elif x == "Low CTR / no conv":
                        parts.append("Low CTR")
                    elif x:
                        parts.append("Quality")
                return " + ".join(dict.fromkeys(parts)) or "Quality"
            for _df in (rec_site, rec_app):
                if len(_df):
                    cat = _df["category"] if "category" in _df else pd.Series([""] * len(_df))
                    _df["flag"] = cat.apply(_flag)
                    _df["is_low_ctr_block"] = cat.apply(lambda c: "Low CTR / no conv" in str(c))

            _add_cpm(rec_site)
            _add_cpm(rec_app)
            _CACHE["ai_recommended_sites.csv"] = rec_site
            _CACHE["ai_recommended_apps.csv"] = rec_app
            app_vals = rec_app["app_id"].tolist() if "app_id" in rec_app else rec_app.get("name", pd.Series([])).tolist()
            _int = ["impressions", "clicks"]
            ctx["ai"] = {
                "error": rec.get("error"),
                "has_app_id": a.get("has_app_id", False),
                "site_count": len(rec_site), "app_count": len(rec_app),
                "total_impr": f"{int(pd.to_numeric(rec_site.get('impressions'), errors='coerce').fillna(0).sum() + pd.to_numeric(rec_app.get('impressions'), errors='coerce').fillna(0).sum()):,}",
                "total_clicks": f"{int(pd.to_numeric(rec_site.get('clicks'), errors='coerce').fillna(0).sum() + pd.to_numeric(rec_app.get('clicks'), errors='coerce').fillna(0).sum()):,}",
                "sites": _fmt(rec_site.head(50), pct_cols=["ctr"], money_cols=["spend", "cpm"],
                              int_cols=_int).to_dict("records"),
                "apps": _fmt(rec_app.head(50), pct_cols=["ctr"], money_cols=["spend", "cpm"],
                             int_cols=_int).to_dict("records"),
                "site_filter": to_adlib_filter(rec_site["name"].tolist(), "site") if len(rec_site) else "",
                "app_filter": to_adlib_filter(app_vals, "app") if len(rec_app) else "",
                "site_csv": ", ".join(rec_site["name"].tolist()) if len(rec_site) else "",
                "app_csv": ", ".join(app_vals) if len(rec_app) else "",
            }

            # Combined (site + app) recommended-block rows split by CTR flag, for the
            # per-CTR-type tables on the High CTR and Low CTR tabs (read-only).
            def _combined(kind_df_pairs):
                frames = []
                for kind, df in kind_df_pairs:
                    if df is None or not len(df):
                        continue
                    d = df.copy()
                    d["kind"] = kind
                    frames.append(d)
                if not frames:
                    return pd.DataFrame()
                return pd.concat(frames, ignore_index=True, sort=False).sort_values("spend", ascending=False)
            comb = _combined([("site", rec_site), ("app", rec_app)])
            def _comb_rows(mask_flag):
                if not len(comb):
                    return []
                # substring match so "Quality + High CTR" rows still appear on the
                # High CTR tab (equality would drop every multi-flagged row).
                sub = comb[comb["flag"].astype(str).str.contains(mask_flag, regex=False)]
                return _fmt(sub.head(100), pct_cols=["ctr"], money_cols=["spend", "cpm"],
                            int_cols=_int).to_dict("records")
            ctx["rec_high"] = _comb_rows("High CTR")
            ctx["rec_low"] = _comb_rows("Low CTR")

            # Combined Placements grid (all delivery), with a coral flag for any
            # placement on the recommended-block list.
            rec_names = set(rec_site.get("name", pd.Series([], dtype=str)).tolist()) \
                | set(rec_app.get("name", pd.Series([], dtype=str)).tolist())

            # Block impact by product — impact of applying the RECOMMENDED block set.
            rec_keys = set(str(x).strip().lower() for x in rec_site.get("name", pd.Series([], dtype=str)).tolist())
            if "app_id" in rec_app:
                rec_keys |= set(str(x).strip().lower() for x in rec_app["app_id"].tolist())
            dpp = a["delivery_pp"]
            tot = dpp.groupby("product").agg(total_impr=("impressions", "sum"),
                                             total_spend=("spend", "sum"),
                                             total_placements=("match_key", "nunique")).reset_index()
            blk = (dpp[dpp["match_key"].isin(rec_keys)].groupby("product")
                   .agg(blocked_impr=("impressions", "sum"), blocked_spend=("spend", "sum"),
                        blocked_placements=("match_key", "nunique")).reset_index())
            bimp = tot.merge(blk, on="product", how="left").fillna(
                {"blocked_impr": 0, "blocked_spend": 0, "blocked_placements": 0})
            bimp["pct_impr_blocked"] = (bimp["blocked_impr"] / bimp["total_impr"]).where(bimp["total_impr"] > 0, 0).fillna(0)
            bimp["pct_spend_blocked"] = (bimp["blocked_spend"] / bimp["total_spend"]).where(bimp["total_spend"] > 0, 0).fillna(0)
            bimp = bimp.sort_values("pct_impr_blocked", ascending=False)
            bimp["cpm"] = (bimp["total_spend"] / bimp["total_impr"] * 1000).where(bimp["total_impr"] > 0, 0).fillna(0)
            _CACHE["block_impact_by_product.csv"] = bimp
            hot_flags = (bimp["pct_impr_blocked"] >= 0.5).tolist()
            bi_rows = _fmt(bimp, pct_cols=["pct_impr_blocked", "pct_spend_blocked"],
                           money_cols=["total_spend", "blocked_spend", "cpm"],
                           int_cols=["total_impr", "total_placements", "blocked_impr", "blocked_placements"]).to_dict("records")
            for row, hot in zip(bi_rows, hot_flags):
                row["hot"] = hot
            ctx["block_impact"] = bi_rows

            # Block impact by strategy type — same idea, grouped by strategy.
            dps = a["delivery_strat"]
            tots = dps.groupby("strategy").agg(total_impr=("impressions", "sum"),
                                               total_spend=("spend", "sum"),
                                               total_placements=("match_key", "nunique")).reset_index()
            blks = (dps[dps["match_key"].isin(rec_keys)].groupby("strategy")
                    .agg(blocked_impr=("impressions", "sum"), blocked_spend=("spend", "sum"),
                         blocked_placements=("match_key", "nunique")).reset_index())
            bis = tots.merge(blks, on="strategy", how="left").fillna(
                {"blocked_impr": 0, "blocked_spend": 0, "blocked_placements": 0})
            bis["pct_impr_blocked"] = (bis["blocked_impr"] / bis["total_impr"]).where(bis["total_impr"] > 0, 0).fillna(0)
            bis["pct_spend_blocked"] = (bis["blocked_spend"] / bis["total_spend"]).where(bis["total_spend"] > 0, 0).fillna(0)
            bis = bis.sort_values("pct_impr_blocked", ascending=False)
            bis["cpm"] = (bis["total_spend"] / bis["total_impr"] * 1000).where(bis["total_impr"] > 0, 0).fillna(0)
            _CACHE["block_impact_by_strategy.csv"] = bis
            hs = (bis["pct_impr_blocked"] >= 0.5).tolist()
            bis_rows = _fmt(bis, pct_cols=["pct_impr_blocked", "pct_spend_blocked"],
                            money_cols=["total_spend", "blocked_spend", "cpm"],
                            int_cols=["total_impr", "total_placements", "blocked_impr", "blocked_placements"]).to_dict("records")
            for row, hot in zip(bis_rows, hs):
                row["hot"] = hot
            ctx["block_impact_strategy"] = bis_rows

            # Impact of recommendations on the watchlists: recompute CTR after removing
            # delivery on recommended-block placements, per grain.
            wl = a["wl_src"].copy()
            wl["is_rec"] = wl["match_key"].isin(rec_keys)

            def _adj(keys):
                tot = wl.groupby(keys).agg(ti=("impressions", "sum"), tc=("clicks", "sum"))
                rec = wl[wl["is_rec"]].groupby(keys).agg(ri=("impressions", "sum"), rc=("clicks", "sum"))
                m = tot.join(rec).fillna(0.0)
                ai = m["ti"] - m["ri"]
                m["adj_ctr"] = ((m["tc"] - m["rc"]) / ai).where(ai > 0, 0.0).fillna(0.0)
                m["rec_pct"] = (m["ri"] / m["ti"]).where(m["ti"] > 0, 0.0).fillna(0.0)
                return m

            partner_adj, client_adj, strat_adj = _adj(["bu"]), _adj(["client", "product"]), _adj(["strategy_name"])

            def _pct(v):
                return f"{v * 100:.2f}%"

            def _attach(rows, adj, keyfn):
                for row in rows:
                    k = keyfn(row)
                    if k in adj.index:
                        row["adj_ctr"] = _pct(adj.loc[k, "adj_ctr"])
                        row["rec_pct"] = _pct(adj.loc[k, "rec_pct"])
                    else:
                        row["adj_ctr"] = row["rec_pct"] = "—"

            _attach(ctx.get("partner") or [], partner_adj, lambda r: r.get("business_unit"))
            _attach(ctx.get("clients") or [], client_adj, lambda r: (r.get("Client"), r.get("product")))
            _attach((ctx.get("insights") or {}).get("strategy_flags", []), strat_adj,
                    lambda r: r.get("Strategy Name"))

            # Cache the three watchlists (with adj CTR) for the single Excel export.
            def _merge_adj(df, adj, on):
                if not len(df):
                    return df
                a2 = adj[["adj_ctr", "rec_pct"]].rename(
                    columns={"adj_ctr": "ctr_after_recs", "rec_pct": "pct_impr_on_recs"})
                return df.merge(a2, left_on=on, right_index=True, how="left")

            # CPM on the client & strategy watchlists (partner already has it above)
            for _wl in (cflag, sf):
                if len(_wl) and "impressions" in _wl.columns and "internal_cost" in _wl.columns:
                    _wl["cpm"] = (_wl["internal_cost"] / _wl["impressions"] * 1000).where(_wl["impressions"] > 0, 0).fillna(0)

            # Buyer as the FIRST column on every tab (mapped from business unit),
            # with a buyer_review flag second for manual sign-off.
            def _buyer_first(df):
                if not len(df):
                    return df
                df = df.copy()
                if "buyer" in df.columns:
                    df = df.drop(columns=["buyer"])
                buyers = df["business_unit"].map(lambda b: buyer_for(b, bmap)) if ("business_unit" in df.columns and bmap) else ""
                df.insert(0, "buyer", buyers)
                df.insert(1, "buyer_review", False)
                return df

            _CACHE["wl_partner"] = _buyer_first(_merge_adj(pm, partner_adj, "business_unit")) if len(pm) else pm
            _CACHE["wl_client"] = _buyer_first(_merge_adj(cflag, client_adj, ["Client", "product"])) if len(cflag) else cflag
            _CACHE["wl_strategy"] = _buyer_first(_merge_adj(sf, strat_adj, "Strategy Name")) if len(sf) else sf
            _CACHE["wl_low_ctr_sites"] = _buyer_first(lcs) if len(lcs) else lcs
            _CACHE["wl_blocked_site_clients"] = (
                _buyer_first(bsc_df.drop(columns=["sites_list"], errors="ignore"))
                if (bsc_df is not None and len(bsc_df)) else pd.DataFrame())

            # Top summary cards: date range, impr, CTR, internal cost, # placements,
            # # recommended-block placements, spend on recommended-block placements.
            ins = (ctx.get("insights") or {}).get("summary", {})
            asum = a["summary"]
            if asum.get("window_start") and asum.get("window_end"):
                date_range = f"{asum['window_start']} – {asum['window_end']}"
            elif asum.get("window_end"):
                date_range = asum["window_end"]
            else:
                date_range = "—"
            rec_spend = float(dpp[dpp["match_key"].isin(rec_keys)]["spend"].sum())
            ctx["topcards"] = {
                "date_range": date_range,
                "impressions": ins.get("total_impressions", 0),
                "ctr": ins.get("book_ctr", 0),
                "internal_cost": ins.get("total_cost", 0),
                "placements": int(dpp["match_key"].nunique()),
                "rec_placements": int(len(rec_site) + len(rec_app)),
                "rec_spend": rec_spend,
            }

            _CACHE["top_placements.csv"] = a["top_placements"]
            top_rows = _fmt(a["top_placements"].head(100), pct_cols=["ctr", "pct_impr"],
                            money_cols=["spend", "cpm"],
                            int_cols=["impressions", "clicks", "conversions"]).to_dict("records")
            for row in top_rows:
                row["rec"] = row.get("name") in rec_names
            ctx["top"] = top_rows
            del a
        except Exception as e:
            ctx["errors"].append(f"Block audit: {e}")

        # Exchange anomaly analysis (only for the manual-upload xlsx path; the
        # section isn't displayed, and the flat pull has no Exchanges data)
        try:
            ex = analyze_exchanges(path) if path else None
            if ex:
                _CACHE["exchange_flags.csv"] = ex["flags"]
                _CACHE["exchange_table.csv"] = ex["table"]
                ctx["exchanges"] = {
                    "summary": ex["summary"],
                    "flags": _fmt(ex["flags"].head(20), pct_cols=["ctr", "product_ctr"],
                                  money_cols=["spend"], int_cols=["impressions", "clicks", "conversions"]).to_dict("records"),
                    "top": _fmt(ex["table"].head(12), pct_cols=["ctr", "pct_of_spend"],
                                money_cols=["spend"], int_cols=["impressions", "clicks"]).to_dict("records"),
                }
        except Exception as e:
            ctx["errors"].append(f"Exchange analysis: {e}")
        # Persist every cached CSV to disk so /download links keep working after the
        # in-memory cache is cleared (next run) or the process restarts (e.g. saved
        # reports served later). The latest analysis's CSVs are always available.
        _persist_download_csvs()
    finally:
        gc.collect()

    return ctx


REPORTS_DIR = os.environ.get("REPORTS_DIR", os.path.join(tempfile.gettempdir(), "insights_reports"))
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", os.path.join(tempfile.gettempdir(), "insights_downloads"))


def _persist_download_csvs():
    """Write each cached DataFrame CSV to DOWNLOADS_DIR (latest run wins)."""
    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        for k, v in list(_CACHE.items()):
            if k.endswith(".csv") and isinstance(v, pd.DataFrame):
                try:
                    v.to_csv(os.path.join(DOWNLOADS_DIR, os.path.basename(k)), index=False)
                except Exception:
                    pass
    except OSError:
        pass


def _save_report(html, date_str=None):
    """Persist a rendered dashboard as reports/insights-YYYY-MM-DD.html. Returns date."""
    import datetime
    os.makedirs(REPORTS_DIR, exist_ok=True)
    date_str = date_str or datetime.date.today().strftime("%Y-%m-%d")
    with open(os.path.join(REPORTS_DIR, f"insights-{date_str}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return date_str


def _migrate_report_names():
    """One-time cleanup: reports saved before range-naming were named by export
    DROP date even though each covers a multi-day delivery window. Their real
    range is baked into their own topcards ('Insights date range: Jul 16 – Jul 22'),
    so read it back out and rename the report (and its paired watchlists /
    blocklist-check files) to the full range. Runs at startup; skips anything
    unparseable or already range-named. os.rename keeps mtime, so 'latest'
    ordering is unaffected."""
    import glob
    import datetime as _dt
    if not os.path.isdir(REPORTS_DIR):
        return
    pat = re.compile(r"^insights-(\d{4}-\d{2}-\d{2})\.html$")
    rng = re.compile(r'<div class="n">([A-Z][a-z]{2} \d{1,2})\s*–\s*([A-Z][a-z]{2} \d{1,2})</div>\s*'
                     r'<div class="l">Insights date range</div>')
    for f in glob.glob(os.path.join(REPORTS_DIR, "insights-*.html")):
        m = pat.match(os.path.basename(f))
        if not m:
            continue  # already range-named
        year = int(m.group(1)[:4])
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                html = fh.read()
        except OSError:
            continue
        r = rng.search(html)
        if not r:
            continue
        def _p(txt, yr):
            return _dt.datetime.strptime(f"{txt} {yr}", "%b %d %Y").date()
        try:
            gen_date = _dt.date.fromisoformat(m.group(1))
            start, end = _p(r.group(1), year), _p(r.group(2), year)
            if start > end:  # Dec–Jan wrap: the start belongs to the prior year
                start = _p(r.group(1), year - 1)
            if start > gen_date:  # whole window after the generation date is
                # impossible — a January-generated report covering December data
                # inherited the wrong year; shift both back one.
                start = _p(r.group(1), start.year - 1)
                end = _p(r.group(2), end.year - 1)
        except ValueError:
            continue
        new_id = f"{start.isoformat()}_to_{end.isoformat()}" if start != end else start.isoformat()
        if new_id == m.group(1):
            continue
        target = os.path.join(REPORTS_DIR, f"insights-{new_id}.html")
        if os.path.exists(target):
            continue  # a range-named twin already exists — touch nothing
        try:
            os.rename(f, target)
            for prefix in ("watchlists", "blocklist-check"):
                oldp = os.path.join(REPORTS_DIR, f"{prefix}-{m.group(1)}.xlsx")
                newp = os.path.join(REPORTS_DIR, f"{prefix}-{new_id}.xlsx")
                if os.path.isfile(oldp) and not os.path.exists(newp):
                    os.rename(oldp, newp)
        except OSError:
            pass


try:
    _migrate_report_names()
except Exception as _e:  # never let a migration hiccup block startup
    app.logger.warning("Report-name migration skipped: %s", _e)


def _list_reports():
    import glob
    if not os.path.isdir(REPORTS_DIR):
        return []
    files = glob.glob(os.path.join(REPORTS_DIR, "insights-*.html"))
    # Sort by generation time (mtime), newest first — with range-named reports
    # ("2026-07-16_to_2026-07-22"), name order no longer matches recency.
    files.sort(key=os.path.getmtime, reverse=True)
    return [os.path.basename(f)[len("insights-"):-len(".html")] for f in files]


def _watchlists_path(date_str):
    return os.path.join(REPORTS_DIR, f"watchlists-{date_str}.xlsx")


def _blocklist_check_path(date_str):
    return os.path.join(REPORTS_DIR, f"blocklist-check-{date_str}.xlsx")


def _blocklist_check_xlsx_bytes():
    """One-tab workbook mirroring the dashboard's 'Master blocklist check' grid
    (same columns & full row set as the blocklist_check.csv download)."""
    df = _CACHE.get("blocklist_check.csv")
    if df is None or not len(df):
        return None
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name="Blocklist check", index=False)
    buf.seek(0)
    return buf.getvalue()


def _save_blocklist_check(date_str, xlsx_bytes):
    if not xlsx_bytes:
        return
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(_blocklist_check_path(date_str), "wb") as f:
            f.write(xlsx_bytes)
    except OSError:
        pass


def _load_blocklist_check(date_str):
    try:
        with open(_blocklist_check_path(date_str), "rb") as f:
            return f.read()
    except OSError:
        return None


def _save_watchlists(date_str, xlsx_bytes):
    if not xlsx_bytes:
        return
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(_watchlists_path(date_str), "wb") as f:
            f.write(xlsx_bytes)
    except OSError:
        pass


def _load_watchlists(date_str):
    try:
        with open(_watchlists_path(date_str), "rb") as f:
            return f.read()
    except OSError:
        return None


@app.route("/ingest", methods=["POST"])
def ingest():
    """Headless entry point for automation. Auth via ?key= matching INGEST_KEY.
    Accepts the .xlsx as multipart ('insights_workbook') or base64 JSON
    {'filename','content_b64','date'}. Runs the analysis, saves a dated report,
    and returns {ok, date, view_url, html} (html lets the caller archive to Drive)."""
    key = os.environ.get("INGEST_KEY", "").strip()
    if key and request.args.get("key", "") != key:
        return jsonify({"ok": False, "error": "Unauthorized (bad or missing key)."}), 401
    date_str = None
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        wb = request.files.get("insights_workbook")
        if wb and wb.filename:
            wb.save(tmp.name)
        else:
            body = request.get_json(silent=True) or {}
            b64 = body.get("content_b64")
            file_url = body.get("file_url")
            date_str = body.get("date")
            if b64:
                import base64
                with open(tmp.name, "wb") as f:
                    f.write(base64.b64decode(b64))
            elif file_url:
                req = urllib.request.Request(file_url, headers={"User-Agent": "adtini-insights"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(tmp.name, "wb") as f:
                    f.write(resp.read())
            else:
                return jsonify({"ok": False, "error": "No file (send multipart 'insights_workbook', or JSON content_b64, or JSON file_url)."}), 400
        tmp.close()
        ctx = _analyze_path(tmp.name)
        html = render_template("dashboard.html", **ctx)
        saved = _save_report(html, date_str)
        base = request.host_url.rstrip("/")
        return jsonify({"ok": True, "date": saved,
                        "view_url": f"{base}/reports/{saved}",
                        "latest_url": f"{base}/reports/latest", "html": html})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ingest failed: {e}"}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        gc.collect()


@app.route("/pull", methods=["GET", "POST"])
def pull():
    """Scheduled entry point. Auth via ?key= matching INGEST_KEY.
    Source priority: S3 two flat files (adapter) -> Graph -> IMAP. Sends email."""
    key = os.environ.get("INGEST_KEY", "").strip()
    if key and request.args.get("key", "") != key:
        return jsonify({"ok": False, "error": "Unauthorized (bad or missing key)."}), 401
    result, status = _run_pull(send_email=True)
    return jsonify(result), status


@app.route("/ui/s3dates")
def ui_s3dates():
    """Inventory of pullable data in S3, for the home page: which dates have
    exports, and whether each date has the complete site+app pair."""
    if not os.environ.get("S3_BUCKET", "").strip():
        return jsonify({"ok": False, "error": "S3 source not configured."}), 200
    try:
        from s3_pull import list_available_dates
        inv = list_available_dates()
        days = [{"date": d, "complete": bool(v["sites"] and v["apps"])}
                for d, v in sorted(inv.items())]
        return jsonify({"ok": True, "days": days}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e}"}), 200


@app.route("/ui/pull", methods=["POST"])
def ui_pull():
    """UI 'Pull latest data' button — same-origin, no email. Optional JSON body
    {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} pools every S3 export in that
    range into one combined dashboard."""
    body = request.get_json(silent=True) or {}
    start = (body.get("start") or "").strip() or None
    end = (body.get("end") or "").strip() or None
    if bool(start) != bool(end):
        return jsonify({"ok": False, "error": "Provide both a start and an end date."}), 400
    if start and end:
        import datetime as _dt
        try:
            s, e = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
        except ValueError:
            return jsonify({"ok": False, "error": "Dates must be YYYY-MM-DD."}), 400
        if s > e:
            start, end = end, start
        if not os.environ.get("S3_BUCKET", "").strip():
            return jsonify({"ok": False, "error": "Date-range pulls need the S3 source configured."}), 400
    result, status = _run_pull(send_email=False, start=start, end=end)
    return jsonify(result), status


@app.route("/ui/email", methods=["POST"])
def ui_email():
    """UI 'Email latest' button — emails the most recent saved report using its
    saved watchlists. No re-pull, no re-analysis (so no memory spike)."""
    dates = _list_reports()
    if not dates:
        return jsonify({"ok": False, "error": "No report yet — pull first."}), 400
    date = dates[0]
    base = request.host_url.rstrip("/")
    view_url = f"{base}/reports/{date}"
    xlsx = _load_watchlists(date)  # from disk; None if not saved (older report)
    bl_xlsx = _load_blocklist_check(date)
    status = _send_weekly_email(date, view_url, xlsx if xlsx is not None else b"",
                                bl_xlsx if bl_xlsx is not None else b"")
    return jsonify({"ok": True, "date": date, "view_url": view_url, "email": status}), 200


def _run_pull(send_email=False, start=None, end=None):
    """Pull latest data (S3 two files -> Graph -> IMAP), analyze, save a dated
    report, optionally email. When start/end (ISO dates) are given and S3 is the
    source, pools EVERY sites/apps export in the window into one combined,
    de-duped, date-filtered analysis. Returns (result_dict, http_status)."""
    if not _ANALYSIS_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True,
                "error": "A run is already in progress — give it a minute, then try again."}, 429
    try:
        frames = None
        workbook_path = None
        cleanup = []
        try:
            if os.environ.get("S3_BUCKET", "").strip():
                from s3_pull import fetch_two, list_range, get_bytes
                from tap_adapter import read_flat, build_frames, combine_flats, filter_date_range
                if start and end:
                    smetas, ametas, capped = list_range(start, end)
                    if not (smetas and ametas):
                        have = ", ".join(x for x in [smetas and "sites", ametas and "apps"] if x) or "neither"
                        return {"ok": True, "skipped": True,
                                "message": f"No complete site+app file pairs dated {start} → {end} (found: {have})."}, 200
                    sdfs, adfs = [], []
                    for metas, acc in ((smetas, sdfs), (ametas, adfs)):
                        for m in metas:
                            b = get_bytes(m["key"])
                            acc.append(read_flat(b, m["name"]))
                            b = None
                            gc.collect()
                    sites = filter_date_range(combine_flats(sdfs), start, end)
                    apps = filter_date_range(combine_flats(adfs), start, end)
                    sdfs = adfs = None
                    gc.collect()
                    if not len(sites) and not len(apps):
                        return {"ok": True, "skipped": True,
                                "message": f"Files found, but no delivery rows dated {start} → {end}."}, 200
                    frames = build_frames(sites, apps)
                    sites = apps = None
                    gc.collect()
                    date_str = f"{start}_to_{end}"
                    source_file = (f"{len(smetas)} site + {len(ametas)} app files pooled ({start} → {end})"
                                   + (" — oldest files trimmed by S3_RANGE_MAX_FILES cap" if capped else ""))
                else:
                    sname, sbytes, aname, abytes, date_str = fetch_two()
                    if not (sbytes and abytes):
                        have = ", ".join(x for x in [sname and "sites", aname and "apps"] if x) or "neither"
                        return {"ok": True, "skipped": True,
                                "message": f"Need both a sites and an apps file under the prefix (found: {have})."}, 200
                    sdf = read_flat(sbytes, sname)
                    adf = read_flat(abytes, aname)
                    sbytes = abytes = None
                    gc.collect()
                    # Default pull = the last DEFAULT_PULL_DAYS days of DELIVERY in
                    # the newest export (7 unless overridden; 0 = whole file). The
                    # exports are rolling windows that can span a month — without
                    # this trim, "pull latest" quietly meant "whatever window the
                    # file happens to contain".
                    trim_days = int(os.environ.get("DEFAULT_PULL_DAYS", "7"))
                    if trim_days > 0:
                        maxd = None
                        for _df in (sdf, adf):
                            if _df is not None and len(_df) and "Date" in _df.columns:
                                m = pd.to_datetime(_df["Date"], errors="coerce").max()
                                if pd.notna(m) and (maxd is None or m > maxd):
                                    maxd = m
                        if maxd is not None:
                            cut_s = (maxd - pd.Timedelta(days=trim_days - 1)).date().isoformat()
                            cut_e = maxd.date().isoformat()
                            sdf = filter_date_range(sdf, cut_s, cut_e)
                            adf = filter_date_range(adf, cut_s, cut_e)
                    frames = build_frames(sdf, adf)
                    sdf = adf = None
                    gc.collect()
                    source_file = f"{sname} + {aname}" + (f" (last {trim_days} delivery days)" if trim_days > 0 else "")
            else:
                if os.environ.get("GRAPH_CLIENT_ID", "").strip():
                    from graph_pull import fetch_latest_xlsx
                else:
                    from mailbox_pull import fetch_latest_xlsx
                fn, payload, date_str = fetch_latest_xlsx()
                if not payload:
                    return {"ok": True, "skipped": True, "message": "No matching export found."}, 200
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
                tmp.write(payload)
                tmp.close()
                workbook_path = tmp.name
                cleanup.append(workbook_path)
                source_file = fn
        except Exception as e:
            for p in cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            return {"ok": False, "error": f"Source error: {e}"}, 502

        try:
            ctx = _analyze_path(workbook_path, frames=frames)
            frames = None
            # Name the report by the DELIVERY date range actually in the data,
            # not the export drop date — multiple same-day pulls of different
            # windows get distinct names, while re-pulling the same window
            # overwrites its report (same data, freshest build wins).
            _asum = (ctx.get("audit") or {}).get("summary", {})
            _ws, _we = _asum.get("window_start_iso"), _asum.get("window_end_iso")
            if _ws and _we:
                date_str = _ws if _ws == _we else f"{_ws}_to_{_we}"
            html = render_template("dashboard.html", **ctx)
            saved = _save_report(html, date_str)
            xlsx = _watchlists_xlsx_bytes()
            _save_watchlists(saved, xlsx)  # persist so the email button never re-crunches
            bl_xlsx = _blocklist_check_xlsx_bytes()
            _save_blocklist_check(saved, bl_xlsx)
            base = request.host_url.rstrip("/")
            view_url = f"{base}/reports/{saved}"
            result = {"ok": True, "date": saved, "file": source_file,
                      "view_url": view_url, "latest_url": f"{base}/reports/latest"}
            if send_email:
                result["email"] = _send_weekly_email(saved, view_url, xlsx, bl_xlsx)
            return result, 200
        except Exception as e:
            return {"ok": False, "error": f"Analysis failed: {e}"}, 500
        finally:
            for p in cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            gc.collect()
    finally:
        _ANALYSIS_LOCK.release()


def _send_weekly_email(date_str, view_url, xlsx=None, bl_xlsx=None):
    """Email the dashboard link + the watchlists + the blocklist-check grid
    (native Google Sheets if configured, else .xlsx attachments). Pass bytes to
    avoid re-computing. No-op unless EMAIL_FROM/EMAIL_TO are set."""
    try:
        import emailer
        if not emailer.configured():
            return "skipped (EMAIL_FROM/EMAIL_TO not set)"
        if xlsx is None:
            xlsx = _watchlists_xlsx_bytes()
        if bl_xlsx is None:
            bl_xlsx = _blocklist_check_xlsx_bytes()
        sheet_url = None
        bl_sheet_url = None
        try:
            import google_sheet
            if google_sheet.configured():
                if xlsx:
                    sheet_url = google_sheet.upload_as_sheet(xlsx, f"Insights watchlists — {date_str}")
                if bl_xlsx:
                    bl_sheet_url = google_sheet.upload_as_sheet(
                        bl_xlsx, f"Blocklist check — still serving — {date_str}")
        except Exception as e:
            app.logger.warning("Google Sheet upload failed: %s", e)

        extra = []
        if sheet_url:
            wl_line = f'<p><strong>Watchlists (Google Sheet):</strong> <a href="{sheet_url}">{sheet_url}</a></p>'
            attach, attach_name = None, None
        elif xlsx:
            wl_line = "<p><strong>Watchlists:</strong> attached (Partner / Client / Strategy tabs).</p>"
            attach, attach_name = xlsx, f"watchlists-{date_str}.xlsx"
        else:
            wl_line = ""
            attach, attach_name = None, None

        if bl_sheet_url:
            bl_line = (f'<p><strong>Blocklist check — still serving (Google Sheet):</strong> '
                       f'<a href="{bl_sheet_url}">{bl_sheet_url}</a></p>')
        elif bl_xlsx:
            bl_line = "<p><strong>Blocklist check — still serving:</strong> attached.</p>"
            extra.append((bl_xlsx, f"blocklist-check-{date_str}.xlsx"))
        else:
            bl_line = ""

        body = (f"<p>The weekly Insights dashboard for <strong>{date_str}</strong> is ready.</p>"
                f'<p><strong>Dashboard:</strong> <a href="{view_url}">{view_url}</a></p>'
                f"{wl_line}{bl_line}")
        prefix = os.environ.get("EMAIL_SUBJECT_PREFIX", "Weekly Insights")
        mid = emailer.send_email(f"{prefix} — {date_str}", body,
                                 attachment=attach, attachment_name=attach_name or "watchlists.xlsx",
                                 extra_attachments=extra)
        return f"sent ({'sheet links' if (sheet_url or bl_sheet_url) else 'xlsx attached'}), id={mid}"
    except Exception as e:
        app.logger.warning("Weekly email failed: %s", e)
        return f"email failed: {e}"


@app.route("/reports")
def reports_index():
    dates = _list_reports()
    links = "".join(f'<li><a href="/reports/{d}">{_fmt_report_name(d)}</a></li>' for d in dates)
    body = f"<h2>Saved insights dashboards</h2><p>{len(dates)} report(s).</p><ul>{links}</ul>" \
        if dates else "<h2>Saved insights dashboards</h2><p>None yet.</p>"
    return f"<!doctype html><meta charset='utf-8'><title>Reports</title><body style='font-family:sans-serif;max-width:700px;margin:40px auto'>{body}</body>"


@app.route("/reports/latest")
def reports_latest():
    dates = _list_reports()
    if not dates:
        abort(404)
    return _serve_report(dates[0])


@app.route("/reports/<date>")
def reports_date(date):
    return _serve_report(date)


def _serve_report(date):
    path = os.path.join(REPORTS_DIR, f"insights-{os.path.basename(date)}.html")
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="text/html")


@app.route("/reports/<date>/delete", methods=["POST"])
def reports_delete(date):
    """Delete a saved dashboard (and its saved watchlists) by date."""
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        return jsonify({"ok": False, "error": "Bad date."}), 400
    removed = []
    for p in (os.path.join(REPORTS_DIR, f"insights-{date}.html"), _watchlists_path(date)):
        try:
            os.unlink(p)
            removed.append(os.path.basename(p))
        except OSError:
            pass
    if not removed:
        return jsonify({"ok": False, "error": "Not found."}), 404
    return jsonify({"ok": True, "date": date, "removed": removed})


@app.route("/favicon.ico")
def favicon():
    return send_file(os.path.join(app.static_folder, "favicon.svg"),
                     mimetype="image/svg+xml")


@app.route("/download/<name>")
def download(name):
    df = _CACHE.get(name)
    if df is not None:
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return send_file(io.BytesIO(buf.getvalue().encode()), mimetype="text/csv",
                         as_attachment=True, download_name=name)
    # Fallback: the in-memory cache was cleared/restarted — serve the last analysis's
    # CSV persisted to disk (so downloads work on saved reports too).
    disk = os.path.join(DOWNLOADS_DIR, os.path.basename(name))
    if os.path.isfile(disk):
        return send_file(disk, mimetype="text/csv", as_attachment=True, download_name=name)
    abort(404)


def _watchlists_xlsx_bytes():
    """Build the 3-tab watchlists workbook from cache; return bytes or None."""
    sheets = [("Partner watchlist", _CACHE.get("wl_partner")),
              ("Client watchlist", _CACHE.get("wl_client")),
              ("Strategy watchlist", _CACHE.get("wl_strategy")),
              ("Low-CTR sites", _CACHE.get("wl_low_ctr_sites")),
              ("Clients on blocked sites", _CACHE.get("wl_blocked_site_clients"))]
    if all(df is None or not len(df) for _, df in sheets):
        return None
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, df in sheets:
            (df if df is not None and len(df) else pd.DataFrame({"(none)": []})).to_excel(
                xl, sheet_name=name[:31], index=False)
    buf.seek(0)
    return buf.getvalue()


@app.route("/download_watchlists.xlsx")
def download_watchlists():
    data = _watchlists_xlsx_bytes()
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="watchlists.xlsx")


@app.route("/push_blocklist", methods=["POST"])
def push_blocklist():
    """Forward the user's checked placements to the blocklist Google Sheet via an
    Apps Script web-app webhook (URL in the BLOCKLIST_WEBHOOK_URL env var). Keeping
    the webhook server-side means it isn't exposed in the page."""
    webhook = os.environ.get("BLOCKLIST_WEBHOOK_URL", "").strip()
    if not webhook:
        return jsonify({"ok": False, "error": "Blocklist sheet isn't configured (set BLOCKLIST_WEBHOOK_URL)."}), 400
    try:
        body_in = request.get_json(force=True) or {}
        placements = body_in.get("placements", [])
        excluded = body_in.get("excluded", [])
    except Exception:
        placements, excluded = [], []
    if not placements and not excluded:
        return jsonify({"ok": False, "error": "Nothing to push."}), 400
    try:
        payload = json.dumps({"placements": placements, "excluded": excluded}).encode()
        req = urllib.request.Request(webhook, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(body)
        except Exception:
            result = {"ok": True, "raw": body[:200]}
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Push failed: {e}"}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

"""
AdLib Placement & Impact Insights — Flask app (Render-ready)
Upload the AdLib Insights workbook -> Business Unit / Product / Strategy insights.
Upload a site/app-grain export -> block list + waste attributed to BU/Product/Strategy.
"""
import io
import os
import gc
import re
import datetime
import json
import tempfile
import threading
import urllib.request
from flask import Flask, request, render_template, send_file, abort, jsonify
import pandas as pd

from insights_engine import build_insights
from block_audit_engine import audit_block_leak
from creative_engine import audit_creatives, alert_subject
from device_engine import analyze_devices
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


def _range_label(date_str):
    """Filename-safe range label: '2026-07-16_to_2026-07-22' -> '07.16.26-07.22.26',
    '2026-07-23' -> '07.23.26'. Unparseable ids pass through."""
    import datetime as _dt
    def _one(iso):
        try:
            return _dt.date.fromisoformat(iso).strftime("%m.%d.%y")
        except ValueError:
            return iso
    if "_to_" in str(date_str):
        a, b = str(date_str).split("_to_", 1)
        return f"{_one(a)}-{_one(b)}"
    return _one(str(date_str))


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
    cf = request.files.get("creative_file")
    if not (wb and wb.filename):
        if cf and cf.filename:  # creative-only upload → the standalone QA page
            return _analyze_creative_upload(cf)
        return render_template("dashboard.html", pmap=PMAP,
                               errors=["Upload the Insights workbook (.xlsx)."])
    creative_df = None
    if cf and cf.filename:
        try:
            from tap_adapter import read_creative_flat
            creative_df = read_creative_flat(cf.read(), cf.filename)
        except Exception:
            creative_df = None
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)
    tmp.close()
    try:
        ctx = _analyze_path(tmp.name, creative_df=creative_df)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        gc.collect()
    return render_template("dashboard.html", **ctx)


def _analyze_path(path=None, frames=None, creative_df=None, device_df=None,
                  device_dedupe=None):
    """Run the full analysis and return the template ctx. Pass either an .xlsx
    `path` (manual upload) or pre-built `frames` (automated pull — no xlsx read).
    `creative_df` is the optional creative-insights export and `device_df` the
    optional device-insights export. Both are their own grain: they feed their own
    tabs and never touch the placement analysis. `device_dedupe` carries what
    combine_devices removed when pooling rolling-window files."""
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
           "creative": None, "device": None,
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
            _sa = a.get("sheet_audit")
            if _sa is not None and len(_sa):
                _CACHE["sheet_audit.csv"] = _sa
                ctx["sheet_audit"] = _sa.to_dict("records")

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
            # Volume floor on QUALITY recommendations (env QUALITY_MIN_IMPR,
            # default 1000): tiny placements aren't worth review time — EXCEPT
            # gaming inventory, which stays block-by-default at any volume, and
            # CTR flags, which carry their own >=10K pooled requirement.
            _qmin = float(os.environ.get("QUALITY_MIN_IMPR", "1000"))
            def _qfloor(df):
                if df is None or not len(df) or "impressions" not in df.columns:
                    return df
                _cat = df.get("category", pd.Series("", index=df.index)).astype(str)
                keep = (pd.to_numeric(df["impressions"], errors="coerce").fillna(0) >= _qmin) \
                    | _cat.str.contains("Gaming", case=False, na=False) \
                    | _cat.str.contains("High CTR|Low CTR", case=False, na=False)
                return df[keep]
            rec_site = _qfloor(rec_site)
            rec_app = _qfloor(rec_app)
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
                "sites": _fmt(rec_site.head(500), pct_cols=["ctr"], money_cols=["spend", "cpm"],
                              int_cols=_int).to_dict("records"),
                "apps": _fmt(rec_app.head(500), pct_cols=["ctr"], money_cols=["spend", "cpm"],
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
            rec_rows = dpp[dpp["match_key"].isin(rec_keys)]
            rec_spend = float(rec_rows["spend"].sum())
            rec_impr = float(rec_rows["impressions"].sum())
            _tot = float(ins.get("total_impressions", 0) or 0)
            ctx["topcards"] = {
                "date_range": date_range,
                "impressions": ins.get("total_impressions", 0),
                "ctr": ins.get("book_ctr", 0),
                "internal_cost": ins.get("total_cost", 0),
                "placements": int(dpp["match_key"].nunique()),
                "rec_placements": int(len(rec_site) + len(rec_app)),
                "rec_spend": rec_spend,
                "rec_impr": rec_impr,
                "rec_impr_pct": (rec_impr / _tot) if _tot > 0 else 0,
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

        # Creative QA — vendor data-integrity check on the creative-insights
        # export. Independent grain: a failure here must never take the
        # placement dashboard down with it.
        try:
            if creative_df is not None and len(creative_df):
                ctx["creative"] = _creative_ctx(creative_df)
                if ctx["creative"] is None:
                    ctx["errors"].append(
                        "Creative QA: the creative file has no Creative Name column — "
                        "check that the creative-insights export layout hasn't changed.")
        except Exception as e:
            ctx["errors"].append(f"Creative QA: {e}")

        # Device analysis — own grain, own tab. The reconciliation compares it
        # against the creative frame (same delivery, different grain), which is
        # the check that would have caught July's inflated device impressions.
        try:
            if device_df is not None and len(device_df):
                ctx["device"] = _device_ctx(device_df, creative_df, device_dedupe)
                if ctx["device"] is None:
                    ctx["errors"].append(
                        "Device: the file has no Device Type column — check that the "
                        "device-insights export layout hasn't changed.")
        except Exception as e:
            ctx["errors"].append(f"Device analysis: {e}")

        # Persist every cached CSV to disk so /download links keep working after the
        # in-memory cache is cleared (next run) or the process restarts (e.g. saved
        # reports served later). The latest analysis's CSVs are always available.
        _persist_download_csvs()
    finally:
        gc.collect()

    return ctx


def _creative_ctx(creative_df):
    """Run the creative audit on a frame and shape it for the template.
    Returns None if the file isn't a creative export (no Creative Name column)."""
    cr = audit_creatives(creative_df)
    if not cr:
        return None
    _CACHE_CREATIVE["latest"] = {"cr": cr, "source": "pull"}
    return _creative_ctx_from(cr)


_DV_MONEY = ["spend", "cpm", "spend_device", "spend_reference"]
_DV_INTS = ["impressions", "clicks", "conversions", "campaigns", "rows",
            "impressions_device", "impressions_reference", "impr_diff",
            "clicks_device", "clicks_reference", "click_diff"]
_DV_PCT = ["ctr", "conv_rate", "share", "impr_pct", "click_pct", "vs_median"]
_CACHE_DEVICE = {}


def _dv_rows(df, n=200):
    if df is None or not len(df):
        return []
    return _fmt(df.head(n), pct_cols=_DV_PCT, money_cols=_DV_MONEY,
                int_cols=_DV_INTS).to_dict("records")


def _device_ctx(device_df, reference_df=None, dedupe_stats=None):
    """Run the device analysis and shape it for the template. The reference frame
    is the creative export for the same window — same delivery, different grain,
    so the two must total the same."""
    dv = analyze_devices(device_df, reference_df, "creative export", dedupe_stats)
    if not dv:
        return None
    for key in ("by_device", "by_product_device", "by_client_device",
                "ctv_off_target", "by_date"):
        tbl = dv.get(key)
        if tbl is not None and len(tbl):
            _CACHE[f"device_{key}.csv"] = tbl
    recon = dv.get("reconciliation") or {}
    for key in ("by_client", "by_campaign"):
        tbl = recon.get(key)
        if tbl is not None and len(tbl):
            _CACHE[f"device_recon_{key}.csv"] = tbl
    _CACHE_DEVICE["latest"] = dv
    ctx = {
        "summary": dv["summary"],
        "by_device": _dv_rows(dv["by_device"], 30),
        "by_product_device": _dv_rows(dv["by_product_device"], 120),
        "by_client_device": _dv_rows(dv["by_client_device"], 200),
        "ctv_off_target": _dv_rows(dv["ctv_off_target"], 40),
        "by_date": _dv_rows(dv["by_date"], 60),
        "recon": None,
    }
    if recon.get("summary"):
        bc = recon.get("by_client")
        bk = recon.get("by_campaign")
        mism_c = bc[bc["verdict"] != "matches"] if bc is not None and len(bc) else pd.DataFrame()
        mism_k = bk[bk["verdict"] != "matches"] if bk is not None and len(bk) else pd.DataFrame()
        ctx["recon"] = {
            "summary": recon["summary"], "reference": recon["reference"],
            "tolerance": recon["tolerance"],
            "clients": _dv_rows(mism_c, 100), "clients_total": int(len(mism_c)),
            "campaigns": _dv_rows(mism_k, 100), "campaigns_total": int(len(mism_k)),
        }
    return ctx


def _device_xlsx_bytes(dv):
    """Every device table in one workbook — what the reconciliation alert attaches."""
    if not dv:
        return None
    recon = dv.get("reconciliation") or {}
    sheets = [("Reconciliation by client", recon.get("by_client")),
              ("Reconciliation by campaign", recon.get("by_campaign")),
              ("By device", dv.get("by_device")),
              ("By product and device", dv.get("by_product_device")),
              ("By client and device", dv.get("by_client_device")),
              ("CTV off target", dv.get("ctv_off_target")),
              ("Daily totals", dv.get("by_date"))]
    sheets = [(n, t) for n, t in sheets if t is not None and len(t)]
    if not sheets:
        return None
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, t in sheets:
            t.head(20000).to_excel(xl, sheet_name=name[:31], index=False)
        _autosize_columns(xl)
    return buf.getvalue()


@app.route("/download_device.xlsx")
def download_device():
    data = _device_xlsx_bytes(_CACHE_DEVICE.get("latest"))
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"device insights_{_CACHE.get('report_range', 'latest')}.xlsx")


def _creative_xlsx_bytes(cr):
    """One-sheet-per-table workbook of the creative findings — what gets attached
    to the vendor alert email."""
    if not cr:
        return None
    perf = cr.get("performance") or {}
    utm = cr.get("utms") or {}
    st = cr.get("sizetype") or {}
    sheets = [("Blank creative campaigns", cr["blank_campaigns"]),
              ("Blank creative rows", cr["blank_rows"].head(50000)),
              ("Blank by client", cr["by_client"]),
              ("Missing preview image", cr.get("missing_preview")),
              ("SM display size", (cr.get("grouped") or {}).get("sm_display_size")),
              ("Format mismatch", (cr.get("grouped") or {}).get("format_mismatch")),
              ("UTM by creative", utm.get("per_creative")),
              ("UTM missing", utm.get("untagged")),
              ("UTM partial", utm.get("partial")),
              ("UTM multiple URLs", utm.get("multi_url")),
              ("UTM by client", utm.get("by_client")),
              ("By creative size", st.get("by_size")),
              ("By creative type", st.get("by_type")),
              ("By product and size", st.get("by_product_size")),
              ("Video completion", st.get("completion")),
              ("Low completion rate", st.get("low_vcr")),
              ("Top creatives", perf.get("top")),
              ("Outperformers", perf.get("winners")),
              ("Underperformers", perf.get("laggards")),
              ("Zero-click creatives", perf.get("no_clicks")),
              ("Zero-conversion spend", perf.get("no_conversions")),
              ("Creative fatigue", perf.get("fatigue")),
              ("No rotation", perf.get("single_creative")),
              ("Dominant creative", perf.get("dominant")),
              ("Name reused by clients", perf.get("dupe_names"))]
    sheets += [(k[:31], v.head(20000)) for k, v in cr["tables"].items()
               if k not in ("blank_creative", "sm_display_size", "sm_social_size",
                            "format_mismatch")]
    sheets = [(n, d) for n, d in sheets if d is not None and len(d)]
    if not sheets:
        return None
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, d in sheets:
            d.to_excel(xl, sheet_name=name[:31], index=False)
        _autosize_columns(xl)
    return buf.getvalue()


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


def _selections_path(date_str):
    return os.path.join(REPORTS_DIR, f"selections-{os.path.basename(date_str)}.json")


@app.route("/reports/<date>/selections", methods=["GET", "POST"])
def report_selections(date):
    """Persist the block-review checkbox state per report, so one team member's
    review (unchecking placements to exclude) is what the next person sees when
    they open the dashboard to do the RZ copy-blocks or the sheet push."""
    path = _selections_path(date)
    if request.method == "GET":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return jsonify({"ok": True, "excluded": data.get("excluded", []),
                            "updated_at": data.get("updated_at")}), 200
        except (OSError, ValueError):
            return jsonify({"ok": True, "excluded": [], "updated_at": None}), 200
    body = request.get_json(silent=True) or {}
    excluded = body.get("excluded")
    if not isinstance(excluded, list) or len(excluded) > 5000:
        return jsonify({"ok": False, "error": "Bad payload."}), 400
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"excluded": [str(x)[:300] for x in excluded],
                       "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, f)
        return jsonify({"ok": True, "count": len(excluded)}), 200
    except OSError as e:
        return jsonify({"ok": False, "error": f"{e}"}), 500


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


_REPORT_ID_RE = r"\d{4}-\d{2}-\d{2}(_to_\d{4}-\d{2}-\d{2})?"


def _ctx_path(date_str):
    return os.path.join(REPORTS_DIR, f"ctx-{os.path.basename(date_str)}.pkl")


def _save_ctx(date_str, ctx):
    """Freeze the fully-computed render context so the report can later be
    re-rendered through a NEWER template without re-pulling data ('restyle')."""
    try:
        import pickle
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(_ctx_path(date_str), "wb") as f:
            pickle.dump(ctx, f, protocol=4)
    except Exception as e:
        app.logger.warning("ctx snapshot failed for %s: %s", date_str, e)


def _load_ctx(date_str):
    try:
        import pickle
        with open(_ctx_path(date_str), "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _sheet_links_path(date_str):
    return os.path.join(REPORTS_DIR, f"sheet-links-{date_str}.json")


def _save_sheet_links(date_str, links):
    if not links:
        return
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(_sheet_links_path(date_str), "w", encoding="utf-8") as f:
            json.dump(links, f)
    except OSError:
        pass


def _load_sheet_links(date_str):
    try:
        with open(_sheet_links_path(date_str), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _blocklist_check_path(date_str):
    return os.path.join(REPORTS_DIR, f"blocklist-check-{date_str}.xlsx")


def _blocklist_check_xlsx_bytes():
    """One-tab workbook mirroring the dashboard's 'Clients serving blocked sites'
    grid — per client & strategy (Strategy Name / Strategy ID), with dates added
    / last served, buyer, and post-block delivery. Same data as the watchlist
    Excel's 'Clients on blocked sites' tab."""
    df = _CACHE.get("wl_blocked_site_clients")
    if df is None or not len(df):
        return None
    df = df.drop(columns=["buyer", "buyer_review"], errors="ignore")
    # Second-layer scrub: epoch sentinels from any cached/older frame read "—".
    for c in ("Date added", "Last served"):
        if c in df.columns:
            df[c] = df[c].astype(str).replace({"1970-01-01": "—", "nan": "—", "NaT": "—"})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name="Clients serving blocked sites"[:31], index=False)
        _autosize_columns(xl)
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
        days = [{"date": d, "complete": bool(v["sites"] and v["apps"]),
                 "creatives": v.get("creatives", 0)}
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
                                bl_xlsx if bl_xlsx is not None else b"",
                                _load_sheet_links(date))
    return jsonify({"ok": True, "date": date, "view_url": view_url, "email": status}), 200


def _load_creative_frame(start=None, end=None):
    """Fetch the creative-insights export(s) from S3 and return (df, source_label).
    With start/end, pools every creative file in the window; otherwise takes the
    newest one and trims to DEFAULT_PULL_DAYS of delivery."""
    from s3_pull import list_range, get_bytes, fetch_latest_creative
    from tap_adapter import read_creative_flat, combine_creatives, filter_date_range
    if start and end:
        *_ignored, cmetas, _capped = list_range(start, end)
        if not cmetas:
            return None, None
        dfs = []
        for m in cmetas:
            dfs.append(read_creative_flat(get_bytes(m["key"]), m["name"]))
            gc.collect()
        df = filter_date_range(combine_creatives(dfs), start, end)
        return df, f"{len(cmetas)} creative file(s) pooled ({start} → {end})"
    name, data, _d = fetch_latest_creative()
    if not data:
        return None, None
    df = read_creative_flat(data, name)
    data = None
    gc.collect()
    trim_days = int(os.environ.get("CREATIVE_PULL_DAYS", os.environ.get("DEFAULT_PULL_DAYS", "7")))
    if trim_days > 0 and df is not None and len(df) and "Date" in df.columns:
        maxd = pd.to_datetime(df["Date"], errors="coerce").max()
        if pd.notna(maxd):
            df = filter_date_range(df,
                                   (maxd - pd.Timedelta(days=trim_days - 1)).date().isoformat(),
                                   maxd.date().isoformat())
    return df, name


def _alert_extra_html(cr):
    """Trafficking + performance headlines appended under the blank-creative table
    in the alert email, so one message covers everything the Creative tab found."""
    s = cr["summary"]
    perf = (cr.get("performance") or {}).get("counts", {})
    bits = []
    if s.get("sm_display_size_creatives"):
        g = (cr.get("grouped") or {}).get("sm_display_size")
        impr = int(g["impressions"].sum()) if g is not None and len(g) else 0
        bits.append(f"<li><strong>{s['sm_display_size_creatives']}</strong> Social Mirror creative(s) named "
                    f"with a display banner size ({impr:,} impressions) — likely a display asset on a "
                    f"social line</li>")
    for key, label in (("no_clicks", "creative(s) delivering with zero clicks"),
                       ("fatigue", "creative(s) with CTR down sharply across the window"),
                       ("single_creative", "campaign(s) delivering on a single creative — no rotation"),
                       ("laggards", "creative(s) at or below ⅓ of their product's CTR norm")):
        if perf.get(key):
            bits.append(f"<li><strong>{perf[key]}</strong> {label}</li>")
    if s.get("missing_preview_creatives"):
        bits.append(f"<li><strong>{s['missing_preview_creatives']}</strong> creative(s) with no preview "
                    f"image URL — can't be visually QA'd (export attached)</li>")
    u = (cr.get("utms") or {}).get("summary")
    if u and u.get("creatives"):
        pct = u["tagged"]["creatives"] / u["creatives"]
        bits.append(f"<li><strong>{u['tagged']['creatives']} of {u['creatives']}</strong> creatives "
                    f"({pct:.0%}) are running UTM codes — {u['untagged']['creatives']} carry none, "
                    f"{u['partially_tagged']['creatives']} are partially tagged</li>")
    st = (cr.get("sizetype") or {})
    if st.get("counts", {}).get("low_vcr"):
        bits.append(f"<li><strong>{st['counts']['low_vcr']}</strong> video creative(s) below the "
                    f"completion-rate floor</li>")
    if not bits:
        return ""
    return ('<p style="margin-top:18px"><strong>Also in this report:</strong></p>'
            '<ul style="font-size:13px;line-height:1.6">' + "".join(bits) + "</ul>")


def _send_creative_alert(cr, source_label, view_url=None):
    """Email the creative findings with the workbook attached.
    No-op (returns a reason string) unless EMAIL_FROM/EMAIL_TO are configured."""
    try:
        import emailer
        if not emailer.configured():
            return "skipped (EMAIL_FROM/EMAIL_TO not set)"
        s = cr["summary"]
        label = (f"{s.get('window_start')} → {s.get('window_end')}"
                 if s.get("window_start") else (source_label or ""))
        rows = cr["blank_campaigns"].head(25)
        def _tr(r):
            tds = "".join(
                f"<td style='padding:6px 10px;border-bottom:1px solid #e6e6e6'>{r.get(c, '')}</td>"
                for c in ("client", "product", "campaign", "campaign_id", "coverage"))
            return ("<tr>" + tds +
                    f"<td style='padding:6px 10px;border-bottom:1px solid #e6e6e6;text-align:right'>"
                    f"{int(r.get('impressions', 0)):,}</td>"
                    f"<td style='padding:6px 10px;border-bottom:1px solid #e6e6e6;text-align:right'>"
                    f"${float(r.get('spend', 0)):,.2f}</td></tr>")
        body = f"""
        <div style="font-family:Arial,Helvetica,sans-serif;color:#12213a">
          <h2 style="margin:0 0 4px">Creative QA — blank creative names</h2>
          <p style="margin:0 0 14px;color:#5a6a7e">Source: {source_label or 'creative-insights export'} &middot; {label}</p>
          <p><strong>{s['blank_rows']:,}</strong> row(s) across
             <strong>{s['blank_campaigns']:,}</strong> campaign(s) came through with no creative name
             ({s['blank_campaigns_delivering']:,} of them still delivering) —
             <strong>{s['blank_impressions']:,}</strong> impressions and
             <strong>${s['blank_spend']:,.2f}</strong> of spend can't be attributed to a creative.</p>
          <table style="border-collapse:collapse;font-size:13px">
            <tr style="background:#f4f6f9;text-align:left">
              <th style="padding:6px 10px">Client</th><th style="padding:6px 10px">Product</th>
              <th style="padding:6px 10px">Campaign</th><th style="padding:6px 10px">Campaign ID</th>
              <th style="padding:6px 10px">Affected</th>
              <th style="padding:6px 10px;text-align:right">Impressions</th>
              <th style="padding:6px 10px;text-align:right">Spend</th></tr>
            {''.join(_tr(r) for r in rows.to_dict('records'))}
          </table>
          {f'<p style="margin-top:14px">Showing 25 of {len(cr["blank_campaigns"]):,} — full list attached.</p>' if len(cr['blank_campaigns']) > 25 else ''}
          {_alert_extra_html(cr)}
          {f'<p style="margin-top:14px"><a href="{view_url}">Open the dashboard</a></p>' if view_url else ''}
        </div>"""
        xlsx = _creative_xlsx_bytes(cr)
        return emailer.send_email(alert_subject(s, label), body, attachment=xlsx,
                                  attachment_name=f"creative QA_{_range_label(s.get('window_end') or 'latest')}.xlsx")
    except Exception as e:
        app.logger.warning("Creative alert email failed: %s", e)
        return f"error: {e}"


def _creative_check(start=None, end=None, send_email=None):
    """Run the standalone creative QA check. Returns (result_dict, status).
    send_email=None means 'email only if something is wrong' (the default for
    the scheduled route)."""
    if not os.environ.get("S3_BUCKET", "").strip():
        return {"ok": False, "error": "S3 source not configured."}, 400
    try:
        df, source = _load_creative_frame(start, end)
    except Exception as e:
        return {"ok": False, "error": f"Source error: {e}"}, 502
    if df is None or not len(df):
        return {"ok": True, "skipped": True,
                "message": "No creative-insights export found for that window."}, 200
    cr = audit_creatives(df)
    if not cr:
        return {"ok": False, "error": "That file has no Creative Name column — "
                                      "the creative-insights layout may have changed."}, 422
    s = cr["summary"]
    _CACHE_CREATIVE["latest"] = {"cr": cr, "source": source}
    emailed = None
    should = (bool(s["blank_rows"] or s.get("critical_issues"))
              if send_email is None else bool(send_email))
    if should:
        emailed = _send_creative_alert(cr, source)
    return {"ok": True, "source": source, "status": s["status"],
            "window": [s.get("window_start"), s.get("window_end")],
            "rows": s["rows"], "blank_rows": s["blank_rows"],
            "blank_campaigns": s["blank_campaigns"],
            "blank_campaigns_delivering": s["blank_campaigns_delivering"],
            "blank_impressions": s["blank_impressions"],
            "blank_spend": round(s["blank_spend"], 2),
            "sm_display_size_creatives": s.get("sm_display_size_creatives", 0),
            "issues": s["issues"], "critical_issues": s.get("critical_issues", 0),
            "performance": {k[5:]: v for k, v in s.items() if k.startswith("perf_")},
            "email": emailed}, 200


_CACHE_CREATIVE = {}  # last standalone creative run, for the /creative page


def _analyze_creative_upload(cf):
    """Creative-insights file uploaded on its own — render the standalone QA page."""
    from tap_adapter import read_creative_flat
    try:
        df = read_creative_flat(cf.read(), cf.filename)
        cr = audit_creatives(df)
    except Exception as e:
        return render_template("creative.html", pmap=PMAP, creative=None,
                               source=cf.filename, errors=[f"Could not read that file: {e}"])
    if not cr:
        return render_template(
            "creative.html", pmap=PMAP, creative=None, source=cf.filename,
            errors=["No Creative Name column in that file — is it the creative-insights export?"])
    _CACHE_CREATIVE["latest"] = {"cr": cr, "source": cf.filename}
    return render_template("creative.html", pmap=PMAP, creative=_creative_ctx_from(cr),
                           source=cf.filename, errors=[])


@app.route("/creative")
def creative_page():
    """Standalone Creative QA page — pulls the newest creative-insights export and
    renders just the creative checks. Optional ?start=&end= to pool a window,
    ?email=1 to force the alert even when clean."""
    start = (request.args.get("start") or "").strip() or None
    end = (request.args.get("end") or "").strip() or None
    force_email = request.args.get("email") in ("1", "true", "yes")
    result, status = _creative_check(start, end, send_email=True if force_email else False)
    cached = _CACHE_CREATIVE.get("latest") or {}
    cr = cached.get("cr")
    ctx = {"pmap": PMAP, "creative": _creative_ctx_from(cr) if cr else None,
           "source": cached.get("source"), "errors": [] if result.get("ok") else
           [result.get("error") or result.get("message")],
           "message": result.get("message")}
    return render_template("creative.html", **ctx), (200 if status == 200 else status)


@app.route("/creative/check", methods=["GET", "POST"])
def creative_check_route():
    """Scheduled/headless creative QA. Auth via ?key= matching INGEST_KEY.
    Emails only when something is wrong, so it's safe to run daily.
    ?email=always to email every run, ?email=never to never email."""
    key = os.environ.get("INGEST_KEY", "").strip()
    if key and request.args.get("key", "") != key:
        return jsonify({"ok": False, "error": "Unauthorized (bad or missing key)."}), 401
    mode = (request.args.get("email") or "").strip().lower()
    send = True if mode == "always" else False if mode == "never" else None
    start = (request.args.get("start") or "").strip() or None
    end = (request.args.get("end") or "").strip() or None
    result, status = _creative_check(start, end, send_email=send)
    return jsonify(result), status


_CR_MONEY = ["spend", "cpm", "cost_per_conv"]
_CR_INTS = ["blank_rows", "impressions", "clicks", "campaigns", "conversions",
            "days", "rows", "creatives", "clients", "campaign_impressions",
            "tagged", "untagged", "placements", "distinct_urls", "urls_seen",
            "tagged_impressions", "q25", "q50", "q75", "q100"]
_CR_PCT = ["ctr", "product_ctr", "share", "drop", "ctr_early", "ctr_late",
           "tagged_pct", "tagged_impr_pct", "pct_of_creatives", "pct_of_impr",
           "conv_rate", "vcr", "q25_rate", "q50_rate", "q75_rate", "dropoff"]


def _cr_rows(df, n=200):
    """Format a creative table for the template (money/int/percent columns)."""
    if df is None or not len(df):
        return []
    return _fmt(df.head(n), pct_cols=_CR_PCT, money_cols=_CR_MONEY,
                int_cols=_CR_INTS).to_dict("records")


def _creative_ctx_from(cr, persist=True):
    """Template ctx from an already-run audit; caches the CSVs the tab links to."""
    _CACHE["creative_blank_campaigns.csv"] = cr["blank_campaigns"]
    _CACHE["creative_blank_rows.csv"] = cr["blank_rows"]
    if len(cr["by_client"]):
        _CACHE["creative_blank_by_client.csv"] = cr["by_client"]
    for key, tbl in cr["tables"].items():
        if key != "blank_creative":
            _CACHE[f"creative_{key}.csv"] = tbl
    for key, tbl in (cr.get("grouped") or {}).items():
        _CACHE[f"creative_{key}_by_creative.csv"] = tbl

    perf = cr.get("performance") or {}
    for key in ("roster", "top", "winners", "laggards", "no_clicks", "no_conversions",
                "fatigue", "single_creative", "dominant", "dupe_names"):
        tbl = perf.get(key)
        if tbl is not None and len(tbl):
            _CACHE[f"creative_{key}.csv"] = tbl

    utm = cr.get("utms") or {}
    for key in ("per_creative", "untagged", "partial", "multi_url", "by_client", "params"):
        tbl = utm.get(key)
        if tbl is not None and len(tbl):
            _CACHE[f"creative_utm_{key}.csv"] = tbl
    st = cr.get("sizetype") or {}
    for key in ("by_size", "by_type", "by_type_size", "by_product_size",
                "completion", "low_vcr"):
        tbl = st.get(key)
        if tbl is not None and len(tbl):
            _CACHE[f"creative_{key}.csv"] = tbl
    mp = cr.get("missing_preview")
    if mp is not None and len(mp):
        _CACHE["creative_missing_preview_by_creative.csv"] = mp
    if persist:
        _persist_download_csvs()

    ctx = {
        "summary": cr["summary"], "checks": cr["checks"],
        "campaigns": _cr_rows(cr["blank_campaigns"], 300),
        "campaigns_total": int(len(cr["blank_campaigns"])),
        "by_client": _cr_rows(cr["by_client"], 50),
        "grouped": {k: _cr_rows(v, 200) for k, v in (cr.get("grouped") or {}).items()},
        "grouped_total": {k: int(len(v)) for k, v in (cr.get("grouped") or {}).items()},
        "perf": None, "utm": None, "sizetype": None,
        "missing_preview": None, "missing_preview_total": 0,
    }
    if perf:
        ctx["perf"] = {
            "counts": perf["counts"], "thresholds": perf["thresholds"],
            "norms": perf.get("norms_display") or [],
            "top": _cr_rows(perf["top"], 100),
            "winners": _cr_rows(perf["winners"], 50),
            "laggards": _cr_rows(perf["laggards"], 50),
            "no_clicks": _cr_rows(perf["no_clicks"], 50),
            "no_conversions": _cr_rows(perf["no_conversions"], 50),
            "fatigue": _cr_rows(perf["fatigue"], 50),
            "single_creative": _cr_rows(perf["single_creative"], 60),
            "dominant": _cr_rows(perf["dominant"], 40),
            "dupe_names": _cr_rows(perf["dupe_names"], 40),
            "totals": {k: int(len(perf[k])) for k in
                       ("winners", "laggards", "no_clicks", "no_conversions", "fatigue",
                        "single_creative", "dominant", "dupe_names", "roster")},
        }
    if utm:
        ctx["utm"] = {
            "summary": utm["summary"],
            "params": _cr_rows(utm["params"], 12),
            "untagged": _cr_rows(utm["untagged"], 100),
            "partial": _cr_rows(utm["partial"], 60),
            "multi_url": _cr_rows(utm["multi_url"], 60),
            "by_client": _cr_rows(utm["by_client"], 60),
            "totals": {k: int(len(utm[k])) for k in
                       ("untagged", "partial", "multi_url", "by_client", "per_creative")},
        }
    if st:
        ctx["sizetype"] = {
            "counts": st["counts"], "vcr_floor": st["vcr_floor"],
            "vcr_min_impr": st["vcr_min_impr"],
            "by_size": _cr_rows(st["by_size"], 40),
            "by_type": _cr_rows(st["by_type"], 20),
            "by_product_size": _cr_rows(st["by_product_size"], 60),
            "completion": _cr_rows(st["completion"], 50),
            "low_vcr": _cr_rows(st["low_vcr"], 40),
            "totals": {k: int(len(st[k])) for k in
                       ("by_size", "by_type", "by_product_size", "completion", "low_vcr")},
        }
    if mp is not None and len(mp):
        ctx["missing_preview"] = _cr_rows(mp, 200)
        ctx["missing_preview_total"] = int(len(mp))
    return ctx


@app.route("/download_creative_qa.xlsx")
def download_creative_qa():
    cached = (_CACHE_CREATIVE.get("latest") or {}).get("cr")
    data = _creative_xlsx_bytes(cached) if cached else None
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"creative QA_{_CACHE.get('report_range', 'latest')}.xlsx")


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
            creative_df = device_df = None
            device_dedupe = None
            if os.environ.get("S3_BUCKET", "").strip():
                from s3_pull import (fetch_two, list_range, get_bytes,
                                     fetch_latest_creative, fetch_latest_device)
                from tap_adapter import (read_flat, build_frames, combine_flats,
                                         filter_date_range, split_ttddv,
                                         read_creative_flat, combine_creatives,
                                         read_device_flat, combine_devices)
                if start and end:
                    smetas, ametas, tmetas, cmetas, dmetas, capped = list_range(start, end)
                    if not (smetas and ametas):
                        have = ", ".join(x for x in [smetas and "sites", ametas and "apps"] if x) or "neither"
                        return {"ok": True, "skipped": True,
                                "message": f"No complete site+app file pairs dated {start} → {end} (found: {have})."}, 200
                    # Creative QA rides along on the same window (own grain, own tab)
                    cdfs = []
                    for m in cmetas:
                        b = get_bytes(m["key"])
                        cdfs.append(read_creative_flat(b, m["name"]))
                        b = None
                        gc.collect()
                    if cdfs:
                        creative_df = filter_date_range(combine_creatives(cdfs), start, end)
                        cdfs = None
                        gc.collect()
                    # Device files are rolling LAST-7-DAYS windows: pooling a range
                    # means the same delivery date arrives in several files, so the
                    # de-dupe is mandatory and what it removed gets reported.
                    ddfs = []
                    for m in dmetas:
                        b = get_bytes(m["key"])
                        ddfs.append(read_device_flat(b, m["name"]))
                        b = None
                        gc.collect()
                    if ddfs:
                        device_df, device_dedupe = combine_devices(ddfs, report=True)
                        device_df = filter_date_range(device_df, start, end)
                        ddfs = None
                        gc.collect()
                    sdfs, adfs = [], []
                    for metas, acc in ((smetas, sdfs), (ametas, adfs)):
                        for m in metas:
                            b = get_bytes(m["key"])
                            acc.append(read_flat(b, m["name"]))
                            b = None
                            gc.collect()
                    for m in tmetas:  # TTD/DV360 combined files split into both sides
                        b = get_bytes(m["key"])
                        _ts, _ta = split_ttddv(read_flat(b, m["name"]))
                        if len(_ts):
                            sdfs.append(_ts)
                        if len(_ta):
                            adfs.append(_ta)
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
                    source_file = (f"{len(smetas)} site + {len(ametas)} app"
                                   + (f" + {len(tmetas)} TTD/DV360" if tmetas else "")
                                   + (f" + {len(cmetas)} creative" if cmetas else "")
                                   + (f" + {len(dmetas)} device" if dmetas else "")
                                   + f" files pooled ({start} → {end})"
                                   + (" — oldest files trimmed by S3_RANGE_MAX_FILES cap" if capped else ""))
                else:
                    sname, sbytes, aname, abytes, tname, tbytes, date_str = fetch_two()
                    if not (sbytes and abytes):
                        have = ", ".join(x for x in [sname and "sites", aname and "apps"] if x) or "neither"
                        return {"ok": True, "skipped": True,
                                "message": f"Need both a sites and an apps file under the prefix (found: {have})."}, 200
                    sdf = read_flat(sbytes, sname)
                    adf = read_flat(abytes, aname)
                    sbytes = abytes = None
                    if tbytes:  # optional TTD/DV360 combined export merges in, DSP-tagged
                        _ts, _ta = split_ttddv(read_flat(tbytes, tname))
                        if len(_ts):
                            sdf = pd.concat([sdf, _ts], ignore_index=True, sort=False)
                        if len(_ta):
                            adf = pd.concat([adf, _ta], ignore_index=True, sort=False)
                        tbytes = None
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
                    # Newest creative export, trimmed to the same delivery window
                    cname, cbytes, _cdate = fetch_latest_creative()
                    if cbytes:
                        creative_df = read_creative_flat(cbytes, cname)
                        cbytes = None
                        if trim_days > 0 and maxd is not None:
                            creative_df = filter_date_range(creative_df, cut_s, cut_e)
                        gc.collect()
                    dname, dbytes, _ddate = fetch_latest_device()
                    if dbytes:
                        # Run the single file through the de-duper too: at device
                        # grain (date x campaign x device) a repeated row is always
                        # a fault, and we would rather remove it and say so than
                        # quietly report inflated delivery.
                        device_df, device_dedupe = combine_devices(
                            [read_device_flat(dbytes, dname)], report=True)
                        dbytes = None
                        if trim_days > 0 and maxd is not None:
                            device_df = filter_date_range(device_df, cut_s, cut_e)
                        gc.collect()
                    source_file = f"{sname} + {aname}" + (f" + {tname}" if tname else "") \
                        + (f" + {cname}" if cname else "") \
                        + (f" + {dname}" if dname else "") \
                        + (f" (last {trim_days} delivery days)" if trim_days > 0 else "")
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
            ctx = _analyze_path(workbook_path, frames=frames, creative_df=creative_df,
                                device_df=device_df, device_dedupe=device_dedupe)
            frames = creative_df = device_df = None
            # Name the report by the DELIVERY date range actually in the data,
            # not the export drop date — multiple same-day pulls of different
            # windows get distinct names, while re-pulling the same window
            # overwrites its report (same data, freshest build wins).
            _asum = (ctx.get("audit") or {}).get("summary", {})
            _ws, _we = _asum.get("window_start_iso"), _asum.get("window_end_iso")
            if not (_ws and _we) and _asum.get("window_start") and _asum.get("window_end"):
                # Belt-and-braces: derive ISO dates from the display fields
                # ('Jul 16' / 'Jul 22') so naming survives an engine that
                # predates the ISO keys. Year = today's, with Dec–Jan wrap.
                import datetime as _dt
                _yr = _dt.date.today().year
                try:
                    _s = _dt.datetime.strptime(f"{_asum['window_start']} {_yr}", "%b %d %Y").date()
                    _e = _dt.datetime.strptime(f"{_asum['window_end']} {_yr}", "%b %d %Y").date()
                    if _s > _e:
                        _s = _s.replace(year=_yr - 1)
                    if _s > _dt.date.today():
                        _s, _e = _s.replace(year=_s.year - 1), _e.replace(year=_e.year - 1)
                    _ws, _we = _s.isoformat(), _e.isoformat()
                except ValueError:
                    pass
            if _ws and _we:
                date_str = _ws if _ws == _we else f"{_ws}_to_{_we}"
            # Build the export workbooks once, create their Google Sheets now (if
            # configured), and bake the links into the dashboard itself. The
            # email reuses these same sheets instead of minting duplicates.
            xlsx = _watchlists_xlsx_bytes()
            bl_xlsx = _blocklist_check_xlsx_bytes()
            sheet_links = {}
            _rl = _range_label(date_str)
            _CACHE["report_range"] = _rl
            try:
                import google_sheet
                if google_sheet.configured():
                    if xlsx:
                        sheet_links["watchlists"] = google_sheet.upload_as_sheet(
                            xlsx, f"buyer watchlists_{_rl}")
                    if bl_xlsx:
                        sheet_links["blocked_clients"] = google_sheet.upload_as_sheet(
                            bl_xlsx, f"serving block list_{_rl}")
            except Exception as e:
                app.logger.warning("Sheet creation at report time failed: %s", e)
            ctx["sheet_links"] = sheet_links or None
            ctx["report_id"] = date_str  # lets the dashboard load/save its block review
            html = render_template("dashboard.html", **ctx)
            saved = _save_report(html, date_str)
            _save_watchlists(saved, xlsx)  # persist so the email button never re-crunches
            _save_blocklist_check(saved, bl_xlsx)
            _save_sheet_links(saved, sheet_links)
            _save_ctx(saved, ctx)
            base = request.host_url.rstrip("/")
            view_url = f"{base}/reports/{saved}"
            result = {"ok": True, "date": saved, "file": source_file,
                      "view_url": view_url, "latest_url": f"{base}/reports/latest"}
            if ctx.get("creative"):
                _cs = ctx["creative"]["summary"]
                result["creative"] = {"status": _cs["status"],
                                      "blank_rows": _cs["blank_rows"],
                                      "blank_campaigns": _cs["blank_campaigns"],
                                      "blank_impressions": _cs["blank_impressions"],
                                      "sm_display_size_creatives": _cs.get("sm_display_size_creatives", 0),
                                      "critical_issues": _cs.get("critical_issues", 0)}
                # Blank creative names are a vendor error we want to catch the day
                # it happens — alert on the scheduled run without waiting for
                # anyone to open the dashboard.
                if send_email and (_cs["blank_rows"] or _cs.get("critical_issues")) and \
                        os.environ.get("CREATIVE_ALERT", "1").strip() not in ("0", "false", "no"):
                    _cached = (_CACHE_CREATIVE.get("latest") or {}).get("cr")
                    if _cached:
                        result["creative"]["email"] = _send_creative_alert(
                            _cached, source_file, view_url)
            if ctx.get("device"):
                _ds = ctx["device"]["summary"]
                result["device"] = {"impressions": _ds["impressions"],
                                    "devices": _ds["devices"],
                                    "date_flags": _ds.get("date_flags", 0)}
                if _ds.get("recon"):
                    result["device"]["reconciliation"] = {
                        k: _ds["recon"][k] for k in
                        ("clients_mismatched", "clients_device_higher",
                         "clients_device_lower", "net_pct", "gross_pct")}
                if _ds.get("dedupe", {}).get("rows_removed"):
                    result["device"]["dedupe_rows_removed"] = _ds["dedupe"]["rows_removed"]
            if send_email:
                result["email"] = _send_weekly_email(saved, view_url, xlsx, bl_xlsx, sheet_links)
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


def _send_weekly_email(date_str, view_url, xlsx=None, bl_xlsx=None, sheet_links=None):
    """Email the dashboard link + the watchlists + the clients-serving-blocked-
    sites grid. Reuses the Google Sheets created at report time (sheet_links)
    when available; otherwise uploads fresh ones if configured, else attaches
    the .xlsx files. No-op unless EMAIL_FROM/EMAIL_TO are set."""
    try:
        import emailer
        if not emailer.configured():
            return "skipped (EMAIL_FROM/EMAIL_TO not set)"
        if xlsx is None:
            xlsx = _watchlists_xlsx_bytes()
        if bl_xlsx is None:
            bl_xlsx = _blocklist_check_xlsx_bytes()
        sheet_links = sheet_links or {}
        sheet_url = sheet_links.get("watchlists")
        bl_sheet_url = sheet_links.get("blocked_clients")
        try:
            import google_sheet
            if google_sheet.configured():
                _rl = _range_label(date_str)
                if xlsx and not sheet_url:
                    sheet_url = google_sheet.upload_as_sheet(xlsx, f"buyer watchlists_{_rl}")
                if bl_xlsx and not bl_sheet_url:
                    bl_sheet_url = google_sheet.upload_as_sheet(
                        bl_xlsx, f"serving block list_{_rl}")
        except Exception as e:
            app.logger.warning("Google Sheet upload failed: %s", e)

        extra = []
        if sheet_url:
            wl_line = f'<p><strong>Watchlists (Google Sheet):</strong> <a href="{sheet_url}">{sheet_url}</a></p>'
            attach, attach_name = None, None
        elif xlsx:
            wl_line = "<p><strong>Watchlists:</strong> attached (Partner / Client / Strategy tabs).</p>"
            attach, attach_name = xlsx, f"buyer watchlists_{_range_label(date_str)}.xlsx"
        else:
            wl_line = ""
            attach, attach_name = None, None

        if bl_sheet_url:
            bl_line = (f'<p><strong>Clients serving blocked sites (Google Sheet):</strong> '
                       f'<a href="{bl_sheet_url}">{bl_sheet_url}</a></p>')
        elif bl_xlsx:
            bl_line = "<p><strong>Clients serving blocked sites:</strong> attached.</p>"
            extra.append((bl_xlsx, f"serving block list_{_range_label(date_str)}.xlsx"))
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


@app.route("/reports/<date>/rerender", methods=["POST"])
def reports_rerender(date):
    """Re-render a saved report's FROZEN data through the current template:
    formatting/feature updates apply, the numbers stay exactly as pulled."""
    import re
    if not re.fullmatch(_REPORT_ID_RE, date or ""):
        return jsonify({"ok": False, "error": "Bad report id."}), 400
    ctx = _load_ctx(date)
    if ctx is None:
        return jsonify({"ok": False, "error": "No data snapshot for this report "
                        "(saved before snapshots existed) — re-pull to refresh it."}), 404
    ctx["report_id"] = date
    try:
        html = render_template("dashboard.html", **ctx)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Render failed: {e}"}), 500
    path = os.path.join(REPORTS_DIR, f"insights-{date}.html")
    try:
        st = os.stat(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        os.utime(path, (st.st_atime, st.st_mtime))  # keep 'latest' ordering intact
    except OSError as e:
        return jsonify({"ok": False, "error": f"{e}"}), 500
    return jsonify({"ok": True}), 200


@app.route("/reports/<date>/delete", methods=["POST"])
def reports_delete(date):
    """Delete a saved dashboard and every file that rides with it."""
    import re
    if not re.fullmatch(_REPORT_ID_RE, date or ""):
        return jsonify({"ok": False, "error": "Bad date."}), 400
    removed = []
    for p in (os.path.join(REPORTS_DIR, f"insights-{date}.html"), _watchlists_path(date),
              _blocklist_check_path(date), _sheet_links_path(date),
              _selections_path(date), _ctx_path(date)):
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


# Header-driven number formats, applied to every tab of every export workbook.
# Sheets inherits these from the converted xlsx, so both the Excel download and
# the Google Sheets render integers / currency / percentages consistently.
_PCT_TOKENS = ("ctr", "rate", "pct")            # substring match, checked first
_CUR_TOKENS = ("spend", "cost", "cpm")          # substring match
_INT_HEADERS = {"impressions", "clicks", "conversions", "view_throughs",
                "blocked_impr", "blocked_placements", "post_impr",
                "n_sites", "n_site", "n_app"}   # exact match


def _number_format_for(header):
    h = str(header).strip().lower()
    if any(t in h for t in _PCT_TOKENS):
        return "0.00%"
    if any(t in h for t in _CUR_TOKENS):
        return '"$"#,##0.00'
    if h in _INT_HEADERS:
        return "#,##0"
    return None


def _autosize_columns(xl):
    """Style every sheet: fit each column to its longest value (capped) and
    apply integer / currency / percentage number formats by header name."""
    for ws in xl.book.worksheets:
        for col in ws.columns:
            width = 0
            letter = None
            header = None
            for cell in col:
                if letter is None:
                    letter = cell.column_letter
                    header = cell.value
                v = cell.value
                if v is not None:
                    width = max(width, len(str(v)))
            if letter:
                ws.column_dimensions[letter].width = min(max(width + 2, 8), 50)
            fmt = _number_format_for(header) if header is not None else None
            if fmt:
                first = True
                for cell in col:
                    if first:
                        first = False
                        continue
                    cell.number_format = fmt


def _watchlists_xlsx_bytes():
    """Build the watchlists workbook from cache; return bytes or None. The
    clients-on-blocked-sites data ships as its own 'serving block list' sheet,
    so it is not duplicated here."""
    sheets = [("Partner watchlist", _CACHE.get("wl_partner")),
              ("Client watchlist", _CACHE.get("wl_client")),
              ("Strategy watchlist", _CACHE.get("wl_strategy")),
              ("Low-CTR sites", _CACHE.get("wl_low_ctr_sites"))]
    if all(df is None or not len(df) for _, df in sheets):
        return None
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, df in sheets:
            if df is not None and len(df):
                # 'flagged' is always TRUE on the filtered partner tab — noise.
                df = df.drop(columns=["flagged"], errors="ignore")
            (df if df is not None and len(df) else pd.DataFrame({"(none)": []})).to_excel(
                xl, sheet_name=name[:31], index=False)
        _autosize_columns(xl)
    buf.seek(0)
    return buf.getvalue()


@app.route("/download_watchlists.xlsx")
def download_watchlists():
    data = _watchlists_xlsx_bytes()
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"buyer watchlists_{_CACHE.get('report_range', 'latest')}.xlsx")


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


# ---------------------------------------------------------------- AdLib direct
# Read AdLib's own S3 bucket rather than a hand-made TapClicks export, so the
# dashboard has current data without anyone exporting anything — and so the two
# copies of the same delivery can be compared instead of trusted.
def _adlib_window(default_days=7):
    start = (request.args.get("start") or "").strip() or None
    end = (request.args.get("end") or "").strip() or None
    days = request.args.get("days")
    to_date = (lambda s: pd.to_datetime(s).date() if s else None)
    return (to_date(start), to_date(end),
            int(days) if (days or "").isdigit() else default_days)


@app.route("/adlib")
def adlib_status():
    """What's in AdLib's bucket, how current it is, and what a pull would read.
    Metadata only — downloads nothing, so it's safe to hit any time."""
    import adlib_s3
    try:
        metas = adlib_s3.list_objects()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e),
                        "hint": "Set ADLIB_S3_BUCKET plus ADLIB_AWS_ACCESS_KEY_ID / "
                                "ADLIB_AWS_SECRET_ACCESS_KEY (or ADLIB_AWS_PROFILE)."}), 502
    _, _, days = _adlib_window()
    reports = {}
    for kind in ("creative", "previews", "device", "campaign"):
        group = [m for m in metas if m["kind"] == kind]
        if not group:
            continue
        newest = max(group, key=lambda m: m["modified"])
        through = adlib_s3.complete_through(group)
        entry = {"files": len(group), "newest": newest["name"],
                 "newest_uploaded": str(newest["modified"]),
                 "complete_through": str(through) if through else None}
        if kind in ("creative", "device") and through:
            plan = adlib_s3.cover_set(
                group, through - datetime.timedelta(days=days - 1), through)
            entry["a_pull_would_read"] = [
                {"file": m["name"], "from": str(s), "to": str(e),
                 "mb": round(m["size"] / 1e6, 1), "full_size": bool(full)}
                for m, (s, e), full in plan]
            entry["mb"] = round(sum(m["size"] for m, _, _ in plan) / 1e6, 1)
        reports[kind] = entry
    return jsonify({"ok": True, "bucket": adlib_s3.BUCKET, "objects": len(metas),
                    "window_days": days, "reports": reports})


@app.route("/previews")
def previews_page():
    """Preview coverage: which delivering creatives have no image on file.

    Distinct from the Creative tab's 'missing preview URL' check, which only
    reads the delivery export's own column. This joins delivery against the Ad
    Previews export — the list the previews are actually served from.
    """
    import adlib_s3
    from preview_engine import audit_previews
    start, end, days = _adlib_window()
    try:
        metas = adlib_s3.list_objects()
        creative, cmeta = adlib_s3.fetch_creative(days=days, start=start, end=end,
                                                  metas=metas)
        previews, pmeta = adlib_s3.fetch_previews(metas=metas)
    except Exception as e:
        return render_template("previews.html", pmap=PMAP, res=None, cmeta=None,
                               pmeta=None, rows=[], by_client=[], error=str(e),
                               version=_build_version()), 502
    res = audit_previews(creative, previews)
    for name, tbl in (("preview_missing.csv", res.get("missing")),
                      ("preview_missing_by_client.csv", res.get("by_client")),
                      ("preview_no_image_anywhere.csv", res.get("nowhere")),
                      ("preview_orphans.csv", res.get("orphans"))):
        if tbl is not None and len(tbl):
            _CACHE[name] = tbl
    _persist_download_csvs()
    return render_template("previews.html", pmap=PMAP, res=res, cmeta=cmeta,
                           pmeta=pmeta, error=None,
                           rows=_cr_rows(res.get("missing"), 300),
                           by_client=_cr_rows(res.get("by_client"), 60),
                           version=_build_version())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

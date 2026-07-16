"""
AdLib Placement & Impact Insights — Flask app (Render-ready)
Upload the AdLib Insights workbook -> Business Unit / Product / Strategy insights.
Upload a site/app-grain export -> block list + waste attributed to BU/Product/Strategy.
"""
import io
import os
import gc
import json
import tempfile
import urllib.request
from flask import Flask, request, render_template, send_file, abort, jsonify
import pandas as pd

from insights_engine import build_insights
from block_audit_engine import audit_block_leak
from exchange_engine import analyze_exchanges
from ai_blocks import recommend_blocks, to_adlib_filter, merge_app_blocks
from product_map import build_pmap
from buyer_map import load_buyer_map, buyer_for
from blocklist_read import load_blocklist

PMAP = build_pmap()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40 MB
_CACHE = {}  # token -> {"name": df} for CSV downloads within the session


def _fmt(df, pct_cols=(), money_cols=(), int_cols=()):
    d = df.copy()
    for c in int_cols:
        if c in d: d[c] = d[c].map(lambda x: f"{x:,.0f}")
    for c in money_cols:
        if c in d: d[c] = d[c].map(lambda x: f"${x:,.0f}")
    for c in pct_cols:
        if c in d: d[c] = d[c].map(lambda x: "" if pd.isna(x) else f"{x:.2%}")
    return d


@app.route("/")
def index():
    return render_template("index.html")


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


def _analyze_path(path):
    """Run the full analysis on an .xlsx at `path` and return the template ctx."""
    ctx = {"insights": None, "audit": None, "blocks": None, "ai": None,
           "clients": None, "clients_total": 0, "has_buyer": False,
           "exchanges": None, "top": None, "block_impact": None,
           "partner": None, "pmap": PMAP, "blocklist_check": None, "topcards": None,
           "block_impact_strategy": None,
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
            r = build_insights(path)
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
                "strategy_flags": _fmt(sf.head(30), pct_cols=["ctr", "type_ctr"], money_cols=["internal_cost"],
                                       int_cols=["impressions", "clicks", "conversions"]).to_dict("records") if len(sf) else [],
            }
            perf_bu = r["by_business_unit"]  # kept for the combined Partner grid
            cflag = r.get("client_flags", pd.DataFrame())
            if len(cflag):
                _CACHE["client_flags.csv"] = cflag
                crows = _fmt(cflag.head(50), pct_cols=["ctr", "product_ctr"],
                             money_cols=["internal_cost"],
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
            a = audit_block_leak(path, blocklist=blocklist)
            _CACHE["block_leak_offenders.csv"] = a["offenders"]
            _CACHE["block_leak_by_bu.csv"] = a["leak_by_bu"]
            _CACHE["block_leak_by_client.csv"] = a["leak_by_client"]
            _CACHE["block_leak_by_product.csv"] = a["leak_by_product"]
            _CACHE["block_leak_by_strategy.csv"] = a["leak_by_strategy"]
            ctx["audit"] = {
                "summary": a["summary"],
                "has_conv": a.get("has_conv", False),
            }

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
            _CACHE["partner_summary.csv"] = pm
            flagged = pm.get("flagged", pd.Series([False] * len(pm))).tolist()
            prow = _fmt(pm.head(60), pct_cols=["ctr"], money_cols=["internal_cost", "cost_per_conv"],
                        int_cols=["impressions", "clicks", "conversions", "blocked_impr", "blocked_placements"]).to_dict("records")
            for row, fl in zip(prow, flagged):
                row["flagged"] = bool(fl)
                row["buyer"] = buyer_for(row.get("business_unit", ""), bmap)
            ctx["partner"] = prow
            ctx["has_buyer"] = bool(bmap)

            # Master-blocklist leak check
            bc = a.get("blocklist_check")
            if bc:
                _CACHE["blocklist_check.csv"] = bc["rows"]
                brows = _fmt(bc["rows"].head(100), money_cols=["spend", "post_spend"],
                             int_cols=["impressions", "post_impr"]).to_dict("records")
                ctx["blocklist_check"] = {
                    "matched": bc["matched"], "leaking_count": bc["leaking_count"],
                    "leaking_spend": bc["leaking_spend"], "rows": brows,
                }

            # AI runs on every upload now. Merge Claude's picks with the
            # deterministic gaming/junk/unresolved auto-block. Apps key on App ID.
            rec = recommend_blocks(a["candidates"])
            rec_site = rec.get("site", pd.DataFrame())
            if len(rec_site) and "impressions" in rec_site:
                rec_site = rec_site.sort_values("impressions", ascending=False)  # sites by impr high-low
            rec_app = merge_app_blocks(rec.get("app", pd.DataFrame()), a["auto_app_blocks"])
            # Drop anything you've previously unchecked (logged to the Excluded tab),
            # so it stops being recommended on every upload.
            if excluded:
                if len(rec_site) and "name" in rec_site:
                    rec_site = rec_site[~rec_site["name"].astype(str).str.strip().str.lower().isin(excluded)]
                if len(rec_app) and "app_id" in rec_app:
                    rec_app = rec_app[~rec_app["app_id"].astype(str).str.strip().str.lower().isin(excluded)]
            _CACHE["ai_recommended_sites.csv"] = rec_site
            _CACHE["ai_recommended_apps.csv"] = rec_app
            app_vals = rec_app["app_id"].tolist() if "app_id" in rec_app else rec_app.get("name", pd.Series([])).tolist()
            ctx["ai"] = {
                "error": rec.get("error"),
                "has_app_id": a.get("has_app_id", False),
                "site_count": len(rec_site), "app_count": len(rec_app),
                "sites": _fmt(rec_site.head(50), pct_cols=["ctr"], money_cols=["spend"],
                              int_cols=["impressions", "clicks"]).to_dict("records"),
                "apps": _fmt(rec_app.head(50), pct_cols=["ctr"], money_cols=["spend"],
                             int_cols=["impressions", "clicks"]).to_dict("records"),
                "site_filter": to_adlib_filter(rec_site["name"].tolist(), "site") if len(rec_site) else "",
                "app_filter": to_adlib_filter(app_vals, "app") if len(rec_app) else "",
                "site_csv": ", ".join(rec_site["name"].tolist()) if len(rec_site) else "",
                "app_csv": ", ".join(app_vals) if len(rec_app) else "",
            }

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
            _CACHE["block_impact_by_product.csv"] = bimp
            hot_flags = (bimp["pct_impr_blocked"] >= 0.5).tolist()
            bi_rows = _fmt(bimp, pct_cols=["pct_impr_blocked", "pct_spend_blocked"],
                           money_cols=["total_spend", "blocked_spend"],
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
            _CACHE["block_impact_by_strategy.csv"] = bis
            hs = (bis["pct_impr_blocked"] >= 0.5).tolist()
            bis_rows = _fmt(bis, pct_cols=["pct_impr_blocked", "pct_spend_blocked"],
                            money_cols=["total_spend", "blocked_spend"],
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

            _CACHE["wl_partner"] = _merge_adj(pm, partner_adj, "business_unit") if len(pm) else pm
            _CACHE["wl_client"] = _merge_adj(cflag, client_adj, ["Client", "product"]) if len(cflag) else cflag
            _CACHE["wl_strategy"] = _merge_adj(sf, strat_adj, "Strategy Name") if len(sf) else sf

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
            top_rows = _fmt(a["top_placements"].head(100), pct_cols=["ctr"], money_cols=["spend"],
                            int_cols=["impressions", "clicks", "conversions"]).to_dict("records")
            for row in top_rows:
                row["rec"] = row.get("name") in rec_names
            ctx["top"] = top_rows
            del a
        except Exception as e:
            ctx["errors"].append(f"Block audit: {e}")

        # Exchange anomaly analysis
        try:
            ex = analyze_exchanges(path)
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
    finally:
        gc.collect()

    return ctx


REPORTS_DIR = os.environ.get("REPORTS_DIR", os.path.join(tempfile.gettempdir(), "insights_reports"))


def _save_report(html, date_str=None):
    """Persist a rendered dashboard as reports/insights-YYYY-MM-DD.html. Returns date."""
    import datetime
    os.makedirs(REPORTS_DIR, exist_ok=True)
    date_str = date_str or datetime.date.today().strftime("%Y-%m-%d")
    with open(os.path.join(REPORTS_DIR, f"insights-{date_str}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return date_str


def _list_reports():
    import glob
    if not os.path.isdir(REPORTS_DIR):
        return []
    files = glob.glob(os.path.join(REPORTS_DIR, "insights-*.html"))
    dates = sorted((os.path.basename(f)[len("insights-"):-len(".html")] for f in files), reverse=True)
    return dates


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
    Source priority: S3 two flat files (adapter) -> Graph -> IMAP."""
    key = os.environ.get("INGEST_KEY", "").strip()
    if key and request.args.get("key", "") != key:
        return jsonify({"ok": False, "error": "Unauthorized (bad or missing key)."}), 401

    workbook_path = None
    cleanup = []
    try:
        if os.environ.get("S3_BUCKET", "").strip():
            from s3_pull import fetch_two
            from tap_adapter import read_flat, synthesize_workbook
            sname, sbytes, aname, abytes, date_str = fetch_two()
            if not (sbytes and abytes):
                have = ", ".join(x for x in [sname and "sites", aname and "apps"] if x) or "neither"
                return jsonify({"ok": True, "skipped": True,
                                "message": f"Need both a sites and an apps file under the prefix (found: {have})."})
            workbook_path = synthesize_workbook(read_flat(sbytes, sname), read_flat(abytes, aname))
            sbytes = abytes = None
            gc.collect()
            cleanup.append(workbook_path)
            source_file = f"{sname} + {aname}"
        else:
            if os.environ.get("GRAPH_CLIENT_ID", "").strip():
                from graph_pull import fetch_latest_xlsx
            else:
                from mailbox_pull import fetch_latest_xlsx
            fn, payload, date_str = fetch_latest_xlsx()
            if not payload:
                return jsonify({"ok": True, "skipped": True, "message": "No matching export found."})
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
        return jsonify({"ok": False, "error": f"Source error: {e}"}), 502

    try:
        ctx = _analyze_path(workbook_path)
        html = render_template("dashboard.html", **ctx)
        saved = _save_report(html, date_str)
        base = request.host_url.rstrip("/")
        view_url = f"{base}/reports/{saved}"
        result = {"ok": True, "date": saved, "file": source_file,
                  "view_url": view_url, "latest_url": f"{base}/reports/latest"}
        result["email"] = _send_weekly_email(saved, view_url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Analysis failed: {e}"}), 500
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass
        gc.collect()


def _send_weekly_email(date_str, view_url):
    """After a successful pull: email the dashboard link + the 3 watchlists (as a
    native Google Sheet if configured, else as an .xlsx attachment). No-op unless
    EMAIL_FROM/EMAIL_TO are set. Never breaks the pull — returns a status string."""
    try:
        import emailer
        if not emailer.configured():
            return "skipped (EMAIL_FROM/EMAIL_TO not set)"
        xlsx = _watchlists_xlsx_bytes()
        sheet_url = None
        try:
            import google_sheet
            if xlsx and google_sheet.configured():
                sheet_url = google_sheet.upload_as_sheet(xlsx, f"Insights watchlists — {date_str}")
        except Exception as e:
            sheet_url = None
            app.logger.warning("Google Sheet upload failed: %s", e)

        if sheet_url:
            wl_line = f'<p><strong>Watchlists (Google Sheet):</strong> <a href="{sheet_url}">{sheet_url}</a></p>'
            attach, attach_name = None, None
        elif xlsx:
            wl_line = "<p><strong>Watchlists:</strong> attached (Partner / Client / Strategy tabs).</p>"
            attach, attach_name = xlsx, f"watchlists-{date_str}.xlsx"
        else:
            wl_line = ""
            attach, attach_name = None, None

        body = (f"<p>The weekly Insights dashboard for <strong>{date_str}</strong> is ready.</p>"
                f'<p><strong>Dashboard:</strong> <a href="{view_url}">{view_url}</a></p>'
                f"{wl_line}")
        prefix = os.environ.get("EMAIL_SUBJECT_PREFIX", "Weekly Insights")
        mid = emailer.send_email(f"{prefix} — {date_str}", body,
                                 attachment=attach, attachment_name=attach_name or "watchlists.xlsx")
        return f"sent ({'sheet link' if sheet_url else 'xlsx attached'}), id={mid}"
    except Exception as e:
        app.logger.warning("Weekly email failed: %s", e)
        return f"email failed: {e}"


@app.route("/reports")
def reports_index():
    dates = _list_reports()
    links = "".join(f'<li><a href="/reports/{d}">{d}</a></li>' for d in dates)
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


@app.route("/download/<name>")
def download(name):
    df = _CACHE.get(name)
    if df is None:
        abort(404)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return send_file(io.BytesIO(buf.getvalue().encode()), mimetype="text/csv",
                     as_attachment=True, download_name=name)


def _watchlists_xlsx_bytes():
    """Build the 3-tab watchlists workbook from cache; return bytes or None."""
    sheets = [("Partner watchlist", _CACHE.get("wl_partner")),
              ("Client watchlist", _CACHE.get("wl_client")),
              ("Strategy watchlist", _CACHE.get("wl_strategy"))]
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

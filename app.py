"""
AdLib Placement & Impact Insights — Flask app (Render-ready)
Upload the AdLib Insights workbook -> Business Unit / Product / Strategy insights.
Upload a site/app-grain export -> block list + waste attributed to BU/Product/Strategy.
"""
import io
import os
import gc
import tempfile
from flask import Flask, request, render_template, send_file, abort
import pandas as pd

from insights_engine import build_insights
from block_audit_engine import audit_block_leak
from exchange_engine import analyze_exchanges
from ai_blocks import recommend_blocks, to_adlib_filter

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
    ctx = {"insights": None, "audit": None, "blocks": None, "ai": None,
           "clients": None, "exchanges": None, "errors": []}
    _CACHE.clear()

    wb = request.files.get("insights_workbook")
    if not (wb and wb.filename):
        ctx["errors"].append("Upload the Insights workbook (.xlsx).")
        return render_template("dashboard.html", **ctx)

    # Stream the upload straight to disk so we never hold the whole file (and
    # duplicate BytesIO copies) in RAM. Both engines read from the same path.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)
    tmp.close()
    try:
        try:
            r = build_insights(tmp.name)
            _CACHE["by_business_unit.csv"] = r["by_business_unit"]
            _CACHE["by_product.csv"] = r["by_product"]
            _CACHE["by_strategy.csv"] = r["by_strategy"]
            _CACHE["plausibility_flags.csv"] = r["plausibility_flags"]
            ctx["insights"] = {
                "summary": r["summary"],
                "bu": _fmt(r["by_business_unit"].head(15),
                          pct_cols=["ctr"], money_cols=["internal_cost", "cost_per_conv"],
                          int_cols=["impressions", "clicks", "conversions", "view_throughs"]).to_dict("records"),
                "product": _fmt(r["by_product"], pct_cols=["ctr", "click_conv_rate", "pct_of_spend"],
                                money_cols=["internal_cost"], int_cols=["impressions", "clicks", "conversions"]).to_dict("records"),
                "strategy": _fmt(r["by_strategy"], pct_cols=["ctr"], money_cols=["internal_cost", "cost_per_conv"],
                                 int_cols=["impressions", "clicks", "conversions"]).to_dict("records"),
                "flags": _fmt(r["plausibility_flags"], pct_cols=["ctr"], money_cols=["Internal Cost"],
                              int_cols=["Impressions", "Clicks"]).to_dict("records"),
            }
            cflag = r.get("client_flags", pd.DataFrame())
            if len(cflag):
                _CACHE["client_flags.csv"] = cflag
                ctx["clients"] = _fmt(cflag.head(25), pct_cols=["ctr"],
                                      money_cols=["internal_cost", "plausibility_cost"],
                                      int_cols=["impressions", "clicks", "conversions"]).to_dict("records")
            del r
        except Exception as e:
            ctx["errors"].append(f"Insights workbook: {e}")

        gc.collect()  # release the performance frames before the big Site/App parse

        try:
            a = audit_block_leak(tmp.name)
            _CACHE["block_leak_offenders.csv"] = a["offenders"]
            _CACHE["block_leak_by_bu.csv"] = a["leak_by_bu"]
            _CACHE["block_leak_by_client.csv"] = a["leak_by_client"]
            _CACHE["block_leak_by_product.csv"] = a["leak_by_product"]
            _CACHE["block_leak_by_strategy.csv"] = a["leak_by_strategy"]
            ctx["audit"] = {
                "summary": a["summary"],
                "offenders": _fmt(a["offenders"].head(25), money_cols=["spend"],
                                  int_cols=["impressions", "clicks"]).to_dict("records"),
                "by_bu": _fmt(a["leak_by_bu"].head(15), money_cols=["leaked_spend"],
                              int_cols=["leaked_impressions", "placements"]).to_dict("records"),
            }

            # Copyable AdLib filter syntax for placements ALREADY flagged "Block"
            bs, bap = a["block_names"]["site"], a["block_names"]["app"]
            ctx["blocks"] = {
                "site_count": len(bs), "app_count": len(bap),
                "site_filter": to_adlib_filter(bs, "site"),
                "app_filter": to_adlib_filter(bap, "app"),
            }

            # Optional AI pass: recommend NEW blocks from non-flagged candidates
            if request.form.get("ai_blocks"):
                rec = recommend_blocks(a["candidates"])
                rec_site = rec.get("site", pd.DataFrame())
                rec_app = rec.get("app", pd.DataFrame())
                _CACHE["ai_recommended_sites.csv"] = rec_site
                _CACHE["ai_recommended_apps.csv"] = rec_app
                ctx["ai"] = {
                    "error": rec.get("error"),
                    "site_count": len(rec_site), "app_count": len(rec_app),
                    "sites": _fmt(rec_site.head(40), pct_cols=["ctr"], money_cols=["spend"],
                                  int_cols=["impressions", "clicks"]).to_dict("records"),
                    "apps": _fmt(rec_app.head(40), pct_cols=["ctr"], money_cols=["spend"],
                                 int_cols=["impressions", "clicks"]).to_dict("records"),
                    "site_filter": to_adlib_filter(rec_site["name"].tolist(), "site") if len(rec_site) else "",
                    "app_filter": to_adlib_filter(rec_app["name"].tolist(), "app") if len(rec_app) else "",
                }
            del a
        except Exception as e:
            ctx["errors"].append(f"Block audit: {e}")

        # Exchange anomaly analysis
        try:
            ex = analyze_exchanges(tmp.name)
            if ex:
                _CACHE["exchange_flags.csv"] = ex["flags"]
                _CACHE["exchange_table.csv"] = ex["table"]
                ctx["exchanges"] = {
                    "summary": ex["summary"],
                    "flags": _fmt(ex["flags"].head(20), pct_cols=["ctr", "pct_of_spend"],
                                  money_cols=["spend"], int_cols=["impressions", "clicks"]).to_dict("records"),
                    "top": _fmt(ex["table"].head(12), pct_cols=["ctr", "pct_of_spend"],
                                money_cols=["spend"], int_cols=["impressions", "clicks"]).to_dict("records"),
                }
        except Exception as e:
            ctx["errors"].append(f"Exchange analysis: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        gc.collect()

    return render_template("dashboard.html", **ctx)


@app.route("/download/<name>")
def download(name):
    df = _CACHE.get(name)
    if df is None:
        abort(404)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return send_file(io.BytesIO(buf.getvalue().encode()), mimetype="text/csv",
                     as_attachment=True, download_name=name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

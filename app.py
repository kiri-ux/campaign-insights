"""
AdLib Placement & Impact Insights — Flask app (Render-ready)
Upload the AdLib Insights workbook -> Business Unit / Product / Strategy insights.
Upload a site/app-grain export -> block list + waste attributed to BU/Product/Strategy.
"""
import io
import os
from flask import Flask, request, render_template, send_file, abort
import pandas as pd

from insights_engine import build_insights
from placement_engine import build_block_report
from block_audit_engine import audit_block_leak

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
    ctx = {"insights": None, "audit": None, "placement": None, "errors": []}
    _CACHE.clear()

    wb = request.files.get("insights_workbook")
    if wb and wb.filename:
        wb_bytes = wb.read()
        try:
            r = build_insights(io.BytesIO(wb_bytes))
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
        except Exception as e:
            ctx["errors"].append(f"Insights workbook: {e}")

        # Block-enforcement audit from the Site/App Overview sheets
        try:
            a = audit_block_leak(io.BytesIO(wb_bytes))
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
        except Exception as e:
            ctx["errors"].append(f"Block audit: {e}")

    sa = request.files.get("siteapp_export")
    if sa and sa.filename:
        try:
            raw = sa.read()
            df = pd.read_csv(io.BytesIO(raw)) if sa.filename.lower().endswith(".csv") \
                else pd.read_excel(io.BytesIO(raw))
            r = build_block_report(df)
            _CACHE["block_list.csv"] = r["block_list"]
            _CACHE["waste_by_bu.csv"] = r["waste_by_bu"]
            _CACHE["waste_by_product.csv"] = r["waste_by_product"]
            _CACHE["waste_by_strategy.csv"] = r["waste_by_strategy"]
            ctx["placement"] = {
                "summary": r["summary"],
                "block_list": _fmt(r["block_list"].head(25), money_cols=["spend"],
                                   int_cols=["impr", "clicks"]).to_dict("records"),
                "waste_by_bu": _fmt(r["waste_by_bu"].head(15), money_cols=["blocked_spend"],
                                    int_cols=["blocked_impressions", "blocked_placements"]).to_dict("records"),
            }
        except Exception as e:
            ctx["errors"].append(f"Site/App export: {e}")

    if not wb and not sa:
        ctx["errors"].append("Upload at least one file.")
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

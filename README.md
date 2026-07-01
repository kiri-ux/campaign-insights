# AdLib Placement & Impact Insights

Proactive oversight layer for AdLib delivery. Two inputs:

1. **Insights workbook** (.xlsx) — the AdLib "Insights" export with Client /
   Product / Strategy / Site+App Overview sheets. Produces performance by
   Business Unit (partner), Product, and Strategy, plus:
   - zero-conversion waste flags (BUs spending over threshold with 0 conversions)
   - plausibility flags (high CTR + zero conversions)
   - Social Mirror spotlight

2. **Site/App export** (.csv/.xlsx) — site/app-grain delivery (site x
   impressions/clicks/conversions). Produces a junk block list and, if the export
   carries BU/Product/Strategy columns, attributes the blocked waste to each
   partner/product/strategy.

## Run locally
    pip install -r requirements.txt
    python app.py            # http://localhost:5000

## Deploy to Render
Push to GitHub, create a Render Web Service from the repo (render.yaml is included),
or set Build `pip install -r requirements.txt` / Start `gunicorn app:app`.
Set ANTHROPIC_API_KEY to enable the audience-fit LLM layer for recognizable
publishers (optional; heuristic block list works without it).

## Notes / limits
- The Insights workbook is line-item grain and contains NO site/app rows, so it
  cannot produce a block list on its own — that requires the site/app export.
- The in-memory download cache is per-process and fine for single-user internal
  use; move to a store (e.g. Redis/S3) before multi-user or scheduled runs.

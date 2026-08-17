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

3. **Creative-insights export** (.csv/.xlsx, filename prefix `creative-insights`)
   — creative-grain delivery. Feeds the **Creative QA** check: catches rows the
   vendor sends with a **blank creative name** (delivery that can't be attributed
   to a creative), rolled up to the campaign the vendor has to fix, plus
   secondary integrity checks on the same file (unrecognized Product 2 values,
   missing business unit, clicks > impressions, delivery with zero spend).

## Creative QA

| Where | What it does |
|---|---|
| `Creative QA` tab on any dashboard | Rides along with every pull — the tab header shows a count when blanks are present |
| `GET /creative` | Standalone page: pulls the newest creative export and shows just the QA. `?start=&end=` pools a window, `?email=1` forces the alert |
| `GET/POST /creative/check?key=INGEST_KEY` | Headless JSON check for a scheduler. **Emails only when something is wrong**, so it's safe to run daily. `?email=always` / `?email=never` override |
| Home page | "Check newest creative export" button, or upload a creative file directly |

Downloads: `creative_blank_campaigns.csv`, `creative_blank_rows.csv`,
`creative_blank_by_client.csv`, one CSV per secondary check, and
`/download_creative_qa.xlsx` (every finding, one sheet per table — this is what
the alert email attaches).

Env vars (all optional):

| Var | Default | Meaning |
|---|---|---|
| `S3_CREATIVE_MATCH` | `creative-insights` | Filename substring marking a creative export. Matched separator-insensitively, so it also catches `creativeinsights_20260816_1728_0.csv` |
| `S3_CREATIVE_SUFFIX` | `.csv,.xlsx` | Extensions accepted for creative files (the main `S3_SUFFIX` still governs site/app) |
| `S3_CREATIVE_PREFIX` | — | Set only if creative exports land in a different S3 folder than the site/app ones |
| `CREATIVE_PULL_DAYS` | `DEFAULT_PULL_DAYS` (7) | Delivery days to keep from the newest creative export |
| `CREATIVE_ALERT_MIN_IMPR` | `1` | Impressions before a blank creative counts as *delivering* (i.e. urgent) |
| `CREATIVE_ALERT` | `1` | Set `0` to stop the scheduled pull from emailing creative alerts |

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

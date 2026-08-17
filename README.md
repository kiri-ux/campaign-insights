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
   — creative-grain delivery. Feeds the **Creative tab**: vendor data errors
   (blank creative names), trafficking flags (Social Mirror running display
   banner sizes), and per-creative performance. See below.

## Creative insights

The creative export feeds a **Creative tab on every dashboard** — same report as the
placement analysis, no separate destination. Three families of output:

**Data quality** — blank / placeholder creative names, rolled up to the campaign with
every ID the export carries (campaign ID + pool ID; `creative_id` too, the moment the
vendor adds that column to the data view). Plus unrecognized Product 2 values, missing
business unit, clicks > impressions, and delivery with zero spend.

**Trafficking** — Social Mirror creatives whose name carries an **IAB display banner
size** (300x250, 728x90, 300x600…), which is the signature of a display asset trafficked
onto a social line. Social-native sizes (1080x1080, 1200x628…) are listed separately as
naming noise. Also flags a still image on a video/CTV product, or a video file on a
display product.

**Performance** (creative grain — invisible to the placement dashboard):

| Insight | Rule |
|---|---|
| Outperformers | CTR ≥ 3× the product's pooled norm; ≥ 10× is labelled "verify — possible invalid traffic" |
| Underperformers | CTR ≤ ⅓ of the product norm, sorted by spend |
| Zero-click creatives | ≥ 5,000 impressions, no clicks |
| Spend with no conversions | ≥ $50 spent, zero post-click + post-view |
| Creative fatigue | CTR down ≥ 40% in the second half of the window vs the first |
| No rotation | Campaign delivering ≥ 10,000 impressions on a single creative |
| Dominant creative | One creative takes ≥ 80% of a 3+ creative campaign |
| Name reused across clients | Same creative name under more than one client |

CTV, Social Mirror CTV and Online Audio are excluded from click-based flags — a low CTR
is expected there, the same policy the Low-CTR tab uses.

### Where to find it

| Where | What it does |
|---|---|
| **Creative tab** on any dashboard | The whole thing, in the same report. Tab header shows a "N to review" badge |
| `GET /creative` | Same block, standalone, against the newest creative export. `?start=&end=` pools a window |
| `GET/POST /creative/check?key=INGEST_KEY` | Headless JSON for a scheduler. **Emails only when something is wrong**, so it's safe daily. `?email=always` / `?email=never` override |
| Home page | "Check newest creative export", or upload a creative file directly |

Downloads: a CSV per table (`creative_blank_campaigns.csv`, `creative_winners.csv`,
`creative_fatigue.csv`, `creative_roster.csv`, …) and `/download_creative_qa.xlsx` —
every finding, one sheet per table, which is what the alert email attaches.

Env vars (all optional):

| Var | Default | Meaning |
|---|---|---|
| `S3_CREATIVE_MATCH` | `creative-insights` | Filename substring marking a creative export. Matched separator-insensitively, so it also catches `creativeinsights_20260816_1728_0.csv` |
| `S3_CREATIVE_SUFFIX` | `.csv,.xlsx` | Extensions accepted for creative files (`S3_SUFFIX` still governs site/app) |
| `S3_CREATIVE_PREFIX` | — | Set only if creative exports land in a different S3 folder |
| `CREATIVE_PULL_DAYS` | `DEFAULT_PULL_DAYS` (7) | Delivery days kept from the newest creative export |
| `CREATIVE_ALERT_MIN_IMPR` | `1` | Impressions before a blank creative counts as *delivering* (urgent) |
| `CREATIVE_ALERT` | `1` | Set `0` to stop the scheduled pull from emailing creative alerts |
| `CREATIVE_MIN_IMPR` | `10000` | Impression floor for CTR-vs-norm judgements |
| `CREATIVE_NOCLICK_MIN_IMPR` | `5000` | Impression floor for the zero-click flag |
| `CREATIVE_NOCONV_MIN_SPEND` | `50` | Spend floor for the zero-conversion flag |
| `CREATIVE_FATIGUE_MIN_IMPR` / `CREATIVE_FATIGUE_DROP` | `5000` / `0.40` | Fatigue thresholds (per half, and the decline) |
| `CREATIVE_SINGLE_MIN_IMPR` | `10000` | Impression floor for the no-rotation flag |

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

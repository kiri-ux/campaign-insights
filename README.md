# AdLib Placement & Impact Insights

Proactive oversight layer for AdLib delivery. Four inputs:

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
   banner sizes), asset gaps (missing preview image / clickthrough URL), UTM
   tagging coverage, and per-creative performance by size, type and completion
   rate. See below.

4. **Device-insights export** (.csv/.xlsx, filename prefix `device-insights`) —
   device-grain delivery. Feeds the **Device tab**: device mix and performance,
   CTV/Video delivering off a TV screen, and the cross-grain reconciliation
   described below.

## Device insights & reconciliation

Device-grain delivery, plus the check that exists because of the July 2026 dispute:
device impressions in the warehouse read ~33% higher than campaign totals for several
advertisers while the vendor's S3 files reconciled exactly, and nothing compared the two
grains, so it ran for three weeks.

Every grain of the same delivery must total the same. On each pull the Device tab:

| Check | What it does |
|---|---|
| Reconciliation vs the creative export | Per client **and** per campaign. Reports **net and gross** difference side by side — netting hides offsetting errors, gross doesn't |
| Duplicated-rows fingerprint | Flags a client whose impressions **and** clicks are both up by the same factor — the signature of a rolling export pooled without de-duplication |
| Rolling-window de-duplication | The vendor drops a LAST-7-DAYS file daily, so one delivery date lands in up to seven files. `combine_devices` de-dupes on every dimension (newest wins) and **reports what it removed** |
| Daily totals | Spike = duplicate files, dip = a missing one |
| CTV off target | Connected-TV / Video products delivering to desktop, mobile or tablet |

Env vars: `S3_DEVICE_MATCH` (default `device-insights`), `S3_DEVICE_SUFFIX`
(`.csv,.xlsx`), `S3_DEVICE_PREFIX`, `DEVICE_RECON_TOLERANCE` (default `0.005`),
`DEVICE_RECON_MIN_IMPR` (default `1000`).

## Creative insights

The creative export feeds a **Creative tab on every dashboard** — same report as the
placement analysis, no separate destination. Three families of output:

**Name recovery (Aug 2026).** The creative data view ships several name fields — `Creative Name`,
`Creative External Name`, `Creative Name**`, `Creative Name - use me` — and populates them
inconsistently, which is what produced the "blank creative name" reports. On read, a blank name is
filled from its sibling columns, and failing that from the **same Creative ID on another row** (one ID
is one creative, so a name found anywhere applies everywhere; the most frequent spelling wins, and
pooled files are searched too). Every row is stamped with a `Name Source` — `vendor`, the column it
came from, or `matched on Creative ID` — and the Creative tab reports how many names were recovered
and how many impressions they cover. **Recovered names are reconstructed here, not sent by the
vendor**, so the export still needs fixing; what remains blank after recovery is a genuinely missing
name. Headers that canonicalize identically (`Creative Name` vs `Creative Name**`) no longer collide
— the second and later take a ` (alt)` suffix rather than silently producing duplicate columns.

**Data quality** — blank / placeholder creative names, rolled up to the campaign with
every ID the export carries (campaign ID + pool ID; `creative_id` too, the moment the
vendor adds that column to the data view). Plus unrecognized Product 2 values, missing
business unit, clicks > impressions, and delivery with zero spend.

**Trafficking** — Social Mirror creatives whose name carries an **IAB display banner
size** (300x250, 728x90, 300x600…), which is the signature of a display asset trafficked
onto a social line. Social-native sizes (1080x1080, 1200x628…) are listed separately as
naming noise. Also flags a still image on a video/CTV product, or a video file on a
display product.

**Assets & tracking** — creatives with no preview image URL (can't be visually QA'd or shown
to a client; CSV + Excel export), creatives with no clickthrough URL, missing creative IDs,
and one creative ID appearing under several names.

**UTM tagging** — how many creatives carry `utm_*` codes on their clickthrough URL, counted by
distinct creative ID. Split into *fully tagged* (all three of `utm_source` / `utm_medium` /
`utm_campaign` — the minimum GA needs to attribute a session), *partially tagged*, *no UTMs*, and
*no URL at all*; those three buckets partition the library exactly. Also: which parameters are in
use, creatives carrying only a click ID (gclid / fbclid) instead of UTMs, one creative pointing at
several landing pages, and tagging coverage per client. Verdicts are per creative — where a creative
runs several URLs, the URL behind the most delivery wins, so nothing lands in two buckets.

**Performance by size & type** — delivery, CTR, conversion rate, CPM and video completion rate
rolled up by `Creative Size` and `Creative Type`, plus a product×size table (the like-for-like
comparison) and a size "family" label (IAB display / social native / vertical / landscape). Where
`Creative Size` is blank the size is parsed out of the creative name.

**Video completion** — from the 25/50/75/100% quartile fields: VCR per creative, the quartile
funnel, drop-off between first quartile and completion, and a low-completion flag. This is what
gives CTV / Video / Audio a real metric, since they're excluded from CTR judgements.

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
| `CREATIVE_VCR_FLOOR` / `CREATIVE_VCR_MIN_IMPR` | `0.50` / `5000` | Completion-rate floor, and the impressions needed to apply it |

## Diagnosing a device/creative discrepancy — `s3_diagnose.py`

A standalone, read-only script for settling "the warehouse shows more impressions than
what ran" questions directly against the S3 files. Run it **where the credentials already
are** (the Render service, a laptop with an AWS profile, CloudShell) — nothing is written
to the bucket and the output workbook contains no credentials.

    pip install boto3 pandas openpyxl
    export S3_BUCKET=…                       # S3_PREFIX optional

    # 1. understand an unfamiliar bucket first (e.g. the AdLib direct one)
    python s3_diagnose.py --list

    # 2. full comparison for a month
    python s3_diagnose.py --start 2026-07-01 --end 2026-07-31

    # 3. the decisive drill-down: one advertiser, one delivery date
    python s3_diagnose.py --start 2026-07-01 --end 2026-07-31 \
        --advertiser "Fresh Start Cleaning" --date 2026-07-15

    # a second credential set (the AdLib direct bucket)
    python s3_diagnose.py --profile adlib-direct --bucket adlib-bucket --list

    # rehearse the logic on files already downloaded — no AWS at all
    python s3_diagnose.py --local-dir ./downloaded_files

It reports: how many files carry each delivery date (the rolling-window overlap), what
naive concatenation totals **versus** correct de-duplication (the inflation factor an
ingestion without de-duping would produce), device-vs-creative impressions per advertiser
with **net and gross** difference, and for one advertiser/date every file carrying it and
the de-duplicated truth — the number to compare against TapClicks. It warns loudly when
the two sides aren't a like-for-like comparison (different windows, or the TapClicks
bucket compared against the AdLib direct bucket, which name advertisers differently).
`--device-match` / `--creative-match` override the filename patterns.

## Placement scoring — implausible CTR

Name-based rules only catch inventory whose name gives it away. A placement clicking at 46% is
invalid traffic (or a broken click macro) whatever it is called, and until Aug 2026 nothing flagged
it — `Slicing Hero: Sword Master`, 783 impressions and 365 clicks, scored as "Unclassified".

Two rules now run **after** the name rules and override them, including "Recognizable Publisher":

| Rule | Default | Env var |
|---|---|---|
| CTR ≥ 10% → **BLOCK**, "Implausible CTR — likely invalid traffic" | `0.10` | `PLACEMENT_CTR_BLOCK` |
| CTR ≥ 5% → **REVIEW**, "Elevated CTR — verify" | `0.05` | `PLACEMENT_CTR_REVIEW` |
| Impressions floor before either applies | `100` | `PLACEMENT_CTR_MIN_IMPR` |

The floor matters: without it, 1 click on 2 impressions reads as 50%. There is also a
`Casual Game (title pattern)` rule for game titles the keyword list misses — games are named from a
small vocabulary (hero / master / saga / quest / sword / blast…), and it is the category that makes
them junk, not the individual franchise.

## Reading AdLib's S3 directly — `adlib_s3.py`

Everything else in this app reads a TapClicks export. That export is the thing that was wrong in
July 2026 (22-31 July ingested twice), and nobody could see it because there was no second copy to
compare against. This module reads **AdLib's own bucket**, which gives an independent copy of the
same delivery and removes the manual export step.

Set these and the Home page offers it:

| Var | Default | Meaning |
|---|---|---|
| `ADLIB_S3_BUCKET` | `adlib-vici` | AdLib's bucket |
| `ADLIB_AWS_ACCESS_KEY_ID` / `ADLIB_AWS_SECRET_ACCESS_KEY` | — | Its own key pair (separate from the app's S3) |
| `ADLIB_AWS_PROFILE` | — | Use instead of a key pair |
| `ADLIB_S3_PREFIX` / `ADLIB_AWS_REGION` | — | Only if needed |
| `ADLIB_WINDOW_DAYS` | `7` | The vendor's rolling window length |
| `ADLIB_CREATIVE_MATCH` / `ADLIB_PREVIEW_MATCH` / `ADLIB_DEVICE_MATCH` / `ADLIB_CAMPAIGN_MATCH` | see module | Filename patterns, matched separator-insensitively |

Three properties of those files shape the reader, and getting any of them wrong produces
confidently wrong numbers:

- **Rolling LAST-7-DAYS drops.** One delivery date sits in up to seven files, so concatenating the
  window multiplies delivery ~7x. `cover_set()` picks a minimal non-overlapping set — a file stamped
  D holds D-7..D-1 complete, so **five files cover a month, not thirty-one**, and no date is read twice.
- **A file excludes its own drop day.** The 15 July file has zero 15 July rows. `complete_through()`
  therefore reports the newest stamp *minus one day*, so the dashboard never shows a fake cliff.
- **Undersized files are partial.** The truncated 7/29 and 8/02 drops are skipped when a full-size
  file covers the same days, and named in the response when they aren't.

AdLib's export has no Product 2 / Business Unit / Strategy columns — those are Vici's TapClicks
enrichment. Product is inferred from the campaign pool name (`… - Social Mirror - 99971`); anything
unrecognized stays `(not in export)` rather than being guessed, since the CTR norms are per product.

| Route | What it does |
|---|---|
| `GET /adlib` | JSON: what's in the bucket, how current each report is, and what a pull *would* read. **Downloads nothing** |
| `GET /previews` | Preview coverage (below). `?days=` / `?start=&end=` |

    python adlib_s3.py --check          # list + classify + the pull plan, no downloads
    python adlib_s3.py --creative --days 7

## Preview coverage — is anything running without an image?

The Creative tab's "missing preview URL" check reads the delivery export's own column. This is the
other half: does the creative exist in the **Ad Previews export** at all — the list previews are
actually served from? A creative can have a Preview Link on its delivery rows and be entirely absent
from the preview data view.

On the 14 Aug 2026 files, 3,222 creatives delivered and **999 (31%) appear in the previews export** —
2,223 missing, behind 11.1M impressions. The page splits that into the two things it can mean:

- **No image in either source** — genuinely cannot be QA'd or shown to a client. This is the urgent list.
- **Missing from the previews export but with a Preview Link on delivery** — the image exists and the
  dashboard falls back to it; the *previews export* is incomplete, which is AdLib's to fix.

It also reports previews for creatives that aren't delivering (stale, not a fault) and creatives
pointing at several different preview images. Joined on **Creative ID**, never on name — AdLib's own
reports punctuate advertiser and creative names differently.

Downloads: `preview_missing.csv`, `preview_no_image_anywhere.csv`, `preview_missing_by_client.csv`,
`preview_orphans.csv`.

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

"""
ai_blocks.py
1. to_adlib_filter(): turn placement names into AdLib filter syntax:
     OR {Site Domain}="a.com" OR {Site Domain}="b.com" ...
     OR {App Name}="App One" OR {App Name}="App Two" ...
2. recommend_blocks(): send candidate placements (NOT already flagged Block) to
   Claude and get back the low-quality ones that should be added to the block list.

Claude calls go out in PARALLEL (thread pool) and the candidate count is capped,
so the whole pass finishes well under the gunicorn request timeout even stacked
on top of the workbook parse. Runs only when ANTHROPIC_API_KEY is set.

Tunable via env vars (no redeploy needed): ANTHROPIC_MODEL, AI_MAX_CANDIDATES,
AI_BATCH_SIZE, AI_MAX_WORKERS.
"""
import os
import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MAX_CANDIDATES = int(os.environ.get("AI_MAX_CANDIDATES", "200"))
BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "50"))
MAX_WORKERS = int(os.environ.get("AI_MAX_WORKERS", "6"))
FIELD = {"site": "Site Domain", "app": "App ID"}
_COLS = ["name", "app_id", "products", "impressions", "clicks", "ctr", "spend", "category", "reason"]


def to_adlib_filter(names, kind):
    """Build the copyable AdLib filter string for a list of placement names."""
    field = FIELD.get(kind, "Site Domain")
    parts = []
    for n in names:
        if n is None:
            continue
        val = str(n).replace('"', "").strip()
        if val:
            parts.append(f'OR {{{field}}}="{val}"')
    return " ".join(parts)


def _call_claude(prompt, api_key, model, max_tokens=4000):
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


# Major legitimate FAST/AVOD platforms: never AI-quality-blocked, no matter what
# the model says (deterministic CTR-anomaly flags still apply — a click anomaly
# on a legit platform is still an anomaly). Matched as substrings of the
# lowercased placement name / app id.
_LEGIT_FAST_TOKENS = (
    "xumo", "fawesome", "pluto", "tubi", "plex", "roku channel", "samsung tv plus",
    "lg channels", "watchfree", "watch free", "vizio", "freevee", "sling", "telly", "philo",
    "directv", "fubo", "crackle", "local now", "scripps", "stirr",
)


def _is_legit_fast(name):
    n = str(name).lower()
    return any(t in n for t in _LEGIT_FAST_TOKENS)


def _classify_batch(rows, kind, api_key, model):
    listing = "\n".join(
        f'{i + 1}. {r["name"]}' + (f'  [products: {r["products"]}]' if r.get("products") else "")
        for i, r in enumerate(rows))
    prompt = (
        f"You audit programmatic ad {kind} placements for a digital advertising agency. "
        f"These ran across many local/regional advertisers (auto dealers, home services, "
        f"healthcare, retail, events, nonprofits).\n\n"
        f"Flag ONLY placements with a genuine QUALITY or FRAUD problem worth blocking "
        f"account-wide, for every client: made-for-advertising (MFA) / ad-arbitrage sites, "
        f"content farms, scraped or auto-generated content, clickbait networks, piracy/illegal, "
        f"adult, gambling, and low-effort junk apps (fake utilities, ad-stuffed "
        f"flashlight/cleaner/wallpaper clones with no real product).\n\n"
        f"CRITICAL — relevance is NOT quality. Content that is niche, lowbrow, or irrelevant "
        f"to some advertisers is a client-level targeting decision, never an account-wide "
        f"block. Do NOT flag:\n"
        f"- FAST/AVOD streaming services (Xumo, Tubi, Pluto, fawesome, Plex, The Roku Channel, "
        f"Samsung TV Plus, LG Channels, WatchFree, Telly, etc.) — being ad-supported is the "
        f"normal model, especially for CTV placements\n"
        f"- established social/UGC platforms (e.g. Tumblr) or real publishers, including "
        f"tabloid/celebrity/gossip outlets\n"
        f"- legitimate niche content or apps with real user bases: horoscopes/astrology, "
        f"reading/novel apps, recipes, games from real studios, utilities that do what they say\n"
        f"- mainstream news, weather, sports, or local business/organization sites\n"
        f"When unsure, do not flag.\n\n"
        f"Return ONLY a JSON array (no prose). For each placement to BLOCK include "
        f'{{"n": <line number>, "reason": "<under 12 words>", "category": "<2-4 words>"}}. '
        f"Omit placements that are acceptable.\n\n"
        f"{kind.title()} placements:\n{listing}"
    )
    text = _call_claude(prompt, api_key, model)
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        verdicts = json.loads(text)
    except Exception:
        return []
    out = []
    for v in verdicts:
        try:
            r = rows[int(v["n"]) - 1]
            if _is_legit_fast(r["name"]) or _is_legit_fast(r.get("app_id", "")):
                continue  # deterministic guardrail against over-blocking FAST/AVOD
            impr = r.get("impressions", 0) or 0
            clk = r.get("clicks", 0) or 0
            out.append({"name": r["name"], "app_id": r.get("app_id", r["name"]),
                        "products": r.get("products", ""), "impressions": impr, "clicks": clk,
                        "ctr": (clk / impr) if impr else 0, "spend": r["spend"],
                        "category": v.get("category", ""), "reason": v.get("reason", "")})
        except Exception:
            continue
    return out


def merge_app_blocks(ai_apps, auto_apps):
    """Combine AI-flagged apps with the deterministic auto-block apps. An app
    caught by both shows a combined category instead of silently keeping one."""
    frames = [df for df in (ai_apps, auto_apps) if df is not None and len(df)]
    if not frames:
        return pd.DataFrame(columns=_COLS)
    return _merge_flagged(frames)


def _merge_flagged(frames):
    """Concat block frames and de-dupe by name — but instead of silently dropping
    duplicates, a placement caught by MULTIPLE rules keeps the primary row's
    metrics and gets a combined category ('MFA + High CTR') with the secondary
    reasons appended, so overlaps are visible instead of hidden."""
    merged = pd.concat(frames, ignore_index=True)
    dup = merged.duplicated(subset=["name"], keep="first")
    if dup.any():
        primary = merged[~dup].copy()
        extras = merged[dup]
        add_cat, add_rsn = {}, {}
        for name, g in extras.groupby("name"):
            add_cat[name] = list(dict.fromkeys(str(c) for c in g["category"]))
            add_rsn[name] = [f"{c}: {r}" for c, r in
                             zip(g["category"].astype(str), g["reason"].astype(str))]
        def _cat(row):
            base = str(row["category"])
            extra = [c for c in add_cat.get(row["name"], []) if c and c != base]
            return " + ".join([base] + extra) if extra else base
        def _rsn(row):
            ex = add_rsn.get(row["name"])
            return f"{row['reason']} | Also: " + " · ".join(ex) if ex else row["reason"]
        primary["category"] = primary.apply(_cat, axis=1)
        primary["reason"] = primary.apply(_rsn, axis=1)
        merged = primary
    return merged.sort_values("impressions", ascending=False).reset_index(drop=True)


def merge_site_blocks(ai_sites, auto_low_sites, auto_high_sites=None):
    """Combine AI-flagged sites with the deterministic HIGH-CTR (invalid-traffic) and
    LOW-CTR/no-conversion site blocks. A site flagged by more than one rule shows a
    combined category (priority for metrics: AI first, then high-CTR, then low-CTR)."""
    frames = [df for df in (ai_sites, auto_high_sites, auto_low_sites)
              if df is not None and len(df)]
    if not frames:
        return pd.DataFrame(columns=_COLS)
    return _merge_flagged(frames)


def recommend_blocks(candidates, api_key=None, model=None,
                     batch_size=None, max_candidates=None, max_workers=None):
    """candidates: {'site': df[name,impressions,spend], 'app': df[...]}
    Returns {'site': df, 'app': df, 'error': str|None} of recommended blocks.

    All batches (both kinds) are dispatched to a thread pool so total wall time is
    roughly one batch's latency, not the sum of every call."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    model = model or DEFAULT_MODEL
    batch_size = batch_size or BATCH_SIZE
    max_candidates = max_candidates or MAX_CANDIDATES
    max_workers = max_workers or MAX_WORKERS
    if not api_key:
        return {"site": pd.DataFrame(columns=_COLS), "app": pd.DataFrame(columns=_COLS),
                "error": "ANTHROPIC_API_KEY is not set on the service — add it in Render -> Environment."}

    tasks = []  # (kind, rows)
    for kind, df in candidates.items():
        rows = df.head(max_candidates).to_dict("records")
        for i in range(0, len(rows), batch_size):
            tasks.append((kind, rows[i:i + batch_size]))

    flagged = {"site": [], "app": []}
    error = None
    if tasks:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_classify_batch, batch, kind, api_key, model): kind
                    for kind, batch in tasks}
            for fu in as_completed(futs):
                kind = futs[fu]
                try:
                    flagged[kind] += fu.result()
                except urllib.error.HTTPError as e:
                    error = f"Claude API error ({e.code}) — check the API key and model name."
                except Exception as e:
                    error = f"Claude API call failed: {e}"

    result = {"error": error}
    for kind in ("site", "app"):
        rows = flagged.get(kind, [])
        result[kind] = (pd.DataFrame(rows, columns=_COLS)
                        .sort_values("spend", ascending=False).reset_index(drop=True)
                        if rows else pd.DataFrame(columns=_COLS))
    return result

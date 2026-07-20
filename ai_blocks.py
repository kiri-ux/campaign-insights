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


def _classify_batch(rows, kind, api_key, model):
    listing = "\n".join(f'{i + 1}. {r["name"]}' for i, r in enumerate(rows))
    prompt = (
        f"You audit programmatic ad {kind} placements for a digital advertising agency. "
        f"These ran across many local/regional advertisers (auto dealers, home services, "
        f"healthcare, retail, events, nonprofits).\n\n"
        f"Flag ONLY placements that are low quality and should be added to a block list: "
        f"made-for-advertising (MFA) / ad-arbitrage sites, content farms, clickbait, "
        f"scraped or auto-generated content, piracy/illegal, adult, gambling, and junk "
        f"utility / photo-editor / casual-game apps, or anything not brand-safe or "
        f"unlikely to drive real business outcomes.\n"
        f"Do NOT flag mainstream reputable news, weather, sports, streaming services, or "
        f"clearly legitimate local business/organization sites. When unsure, do not flag.\n\n"
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
    """Combine AI-flagged apps with the deterministic auto-block apps, de-duped by
    name. Auto-block reasons are kept unless the AI already flagged the same app."""
    frames = [df for df in (ai_apps, auto_apps) if df is not None and len(df)]
    if not frames:
        return pd.DataFrame(columns=_COLS)
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=["name"], keep="first")
    return merged.sort_values("spend", ascending=False).reset_index(drop=True)


def merge_site_blocks(ai_sites, auto_sites):
    """Combine AI-flagged sites with the deterministic low-CTR/no-conversion site
    blocks, de-duped by name. The AI's reason wins when both flag the same site."""
    frames = [df for df in (ai_sites, auto_sites) if df is not None and len(df)]
    if not frames:
        return pd.DataFrame(columns=_COLS)
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=["name"], keep="first")
    return merged.sort_values("spend", ascending=False).reset_index(drop=True)


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

"""
ai_blocks.py
Two things:
1. to_adlib_filter(): turn a list of placement names into AdLib filter syntax:
     OR {Site Domain}="a.com" OR {Site Domain}="b.com" ...
     OR {App Name}="App One" OR {App Name}="App Two" ...
2. recommend_blocks(): send candidate placements (NOT already flagged Block) to
   Claude and get back the low-quality ones that should be added to the block list.

The Claude call uses the standard /v1/messages endpoint via urllib (no extra
dependency). It runs only when ANTHROPIC_API_KEY is set, so the rest of the app
works without it. Model is overridable via ANTHROPIC_MODEL.
"""
import os
import json
import urllib.request
import urllib.error
import pandas as pd

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
FIELD = {"site": "Site Domain", "app": "App Name"}


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
    with urllib.request.urlopen(req, timeout=90) as r:
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
            out.append({"name": r["name"], "impressions": r["impressions"], "spend": r["spend"],
                        "category": v.get("category", ""), "reason": v.get("reason", "")})
        except Exception:
            continue
    return out


def recommend_blocks(candidates, api_key=None, model=None, batch_size=60, max_candidates=300):
    """candidates: {'site': df[name,impressions,spend], 'app': df[...]}
    Returns {'site': df, 'app': df, 'error': str|None} of recommended blocks."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    model = model or DEFAULT_MODEL
    if not api_key:
        return {"site": pd.DataFrame(), "app": pd.DataFrame(),
                "error": "ANTHROPIC_API_KEY is not set on the service — add it in Render → Environment."}
    result = {"error": None}
    for kind, df in candidates.items():
        rows = df.head(max_candidates).to_dict("records")
        flagged = []
        try:
            for i in range(0, len(rows), batch_size):
                flagged += _classify_batch(rows[i:i + batch_size], kind, api_key, model)
        except urllib.error.HTTPError as e:
            result["error"] = f"Claude API error ({e.code}) — check the API key and model name."
        except Exception as e:
            result["error"] = f"Claude API call failed: {e}"
        result[kind] = (pd.DataFrame(flagged, columns=["name", "impressions", "spend", "category", "reason"])
                        .sort_values("spend", ascending=False).reset_index(drop=True)
                        if flagged else pd.DataFrame(columns=["name", "impressions", "spend", "category", "reason"]))
    return result

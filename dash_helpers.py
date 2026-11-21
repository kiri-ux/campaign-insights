"""Shapes the dashboard ctx for the template: short block reasons, the combined
Publishers table (AdLib + StackAdapt), and the Actions list. Works on a fresh
ctx and on a saved snapshot, so re-rendered reports get the same view."""
import re


def num(v):
    """'$1,234' / '12.5%' / 1234 -> float. Blank or dash -> 0."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[$,%\s×x]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return 0.0


def n0(v):
    return f"{num(v):,.0f}"


def money(v):
    return f"${num(v):,.0f}"


def money2(v):
    return f"${num(v):,.2f}"


def pct(v, digits=2):
    return f"{num(v) * 100:.{digits}f}%"


_HIGH = re.compile(r"CTR\s+([\d.]+)%\s+is\s+([\d.]+)×.*HIGH", re.I)
_LOW = re.compile(r"CTR\s+([\d.]+)%\s+is\s+([\d.]+)×.*below", re.I)
_NOCONV = re.compile(r"CTR\s+([\d.]+)%\s+with\s+(\d+)\s+conversions", re.I)


def short_reason(text):
    """Block reasons as one short fact."""
    t = str(text or "").strip()
    if not t or t.lower() == "nan":
        return ""
    parts = [p.strip() for p in t.split(" | Also: ")[0].split(" · ")]
    out = []
    for p in parts:
        m = _HIGH.search(p)
        if m:
            out.append(f"CTR {float(m.group(2)):.1f}x normal")
            continue
        m = _LOW.search(p)
        if m:
            out.append(f"CTR {m.group(1)}%, {float(m.group(2)):.2f}x normal")
            continue
        m = _NOCONV.search(p)
        if m:
            out.append(f"CTR {m.group(1)}%, {m.group(2)} conv")
            continue
        out.append(p.rstrip("."))
    return "; ".join(out)


def _key(name):
    s = str(name or "").strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)
    if "/" in s and "." in s.split("/")[0]:
        s = s.split("/")[0]
    return (s[4:] if s.startswith("www.") else s).rstrip(".")


def build_publishers(ctx, limit=500):
    """One row per publisher across AdLib and StackAdapt."""
    rows = {}
    leaking = {_key(r.get("name")) for r in ((ctx.get("blocklist_check") or {}).get("rows") or [])
               if r.get("leaking")}
    for r in ctx.get("top") or []:
        k = _key(r.get("name"))
        rows[k] = {
            "name": r.get("name"), "type": r.get("placement_type") or "site",
            "products": r.get("products") or "", "adlib": True, "sa": False,
            "impressions": num(r.get("impressions")), "clicks": num(r.get("clicks")),
            "spend": num(r.get("spend")), "last_served": r.get("last_served") or "",
            "serving": r.get("serving") or "",
            "action": "block" if r.get("rec") else ("leak" if k in leaking else ""),
        }
    sa = ctx.get("sa") or {}
    for p in sa.get("publishers") or []:
        k = _key(p.get("key") or p.get("name"))
        row = rows.get(k)
        if row is None:
            row = rows[k] = {
                "name": p.get("title") if p.get("type") == "app" and p.get("title") else p.get("name"),
                "type": p.get("type") or "site", "products": "", "adlib": False, "sa": True,
                "impressions": 0.0, "clicks": 0.0, "spend": 0.0, "last_served": "", "serving": "",
                "action": "",
            }
        row["sa"] = True
        row["sa_impressions"] = num(p.get("impressions"))
        row["impressions"] += num(p.get("impressions"))
        row["clicks"] += num(p.get("clicks"))
        row["spend"] += num(p.get("spend"))
        flags = p.get("flags") or []
        if not row["action"]:
            if "Blocked" in flags:
                row["action"] = "sa_list"
            elif "To block" in flags or "High CTR" in flags:
                row["action"] = "block"
    out = sorted(rows.values(), key=lambda r: -r["impressions"])[:limit]
    tot = (num((ctx.get("topcards") or {}).get("impressions"))
           + num((sa.get("summary") or {}).get("impressions"))) or sum(r["impressions"] for r in rows.values()) or 1.0
    for r in out:
        r["ctr"] = r["clicks"] / r["impressions"] if r["impressions"] else 0.0
        r["cpm"] = r["spend"] / r["impressions"] * 1000 if r["impressions"] else 0.0
        r["share"] = r["impressions"] / tot
    return out


def _pl(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def build_actions(ctx):
    """Plain next steps, biggest problems first. Each links to its list."""
    acts = []

    def add(n, text, tab, anchor, tone="bad"):
        n = int(num(n))
        if n > 0:
            acts.append({"n": n, "text": text(n), "tab": tab, "anchor": anchor, "tone": tone})

    ai = ctx.get("ai") or {}
    add((ai.get("site_count") or 0) + (ai.get("app_count") or 0),
        lambda n: f"Block {n:,} {_pl(n, 'publisher')}", "publishers", "pub-block")
    bc = ctx.get("blocklist_check") or {}
    add(bc.get("leaking_count"),
        lambda n: f"Check DSP block settings: {n:,} blocked {_pl(n, 'publisher')} still serving",
        "publishers", "pub-leak")
    bsc = ctx.get("blocked_site_clients") or []
    add(sum(1 for r in bsc if r.get("leaking")),
        lambda n: f"Fix block settings for {n:,} {_pl(n, 'client')}", "publishers", "pub-clients")
    ss = (ctx.get("sa") or {}).get("summary") or {}
    add(ss.get("blocked_serving"),
        lambda n: f"Add {n:,} {_pl(n, 'publisher')} to the StackAdapt exclusion list", "stackadapt", "sa-blocked")
    add(ss.get("high_ctr"),
        lambda n: f"Review {n:,} high CTR {_pl(n, 'publisher')} in StackAdapt", "stackadapt", "sa-high", "warn")
    add(len(ctx.get("sheet_audit") or []),
        lambda n: f"Fix {n:,} block sheet {_pl(n, 'entry', 'entries')}", "publishers", "pub-sheet", "warn")
    lcs = ctx.get("low_ctr_sites") or []
    add(sum(1 for r in lcs if not r.get("acct_blocked")),
        lambda n: f"Add client blocks for {n:,} low CTR {_pl(n, 'publisher')}", "publishers", "pub-lowctr", "warn")
    add(len(ctx.get("partner") or []),
        lambda n: f"Review {n:,} {_pl(n, 'partner')} with high CTR", "accounts", "acc-partners", "warn")
    add(ctx.get("clients_total") or len(ctx.get("clients") or []),
        lambda n: f"Review {n:,} {_pl(n, 'client')} with high CTR", "accounts", "acc-clients", "warn")
    add(len(((ctx.get("insights") or {}).get("strategy_flags")) or []),
        lambda n: f"Review {n:,} {_pl(n, 'strategy', 'strategies')} with high CTR", "accounts", "acc-strategies", "warn")
    cr = (ctx.get("creative") or {}).get("summary") or {}
    add(cr.get("blank_campaigns"),
        lambda n: f"Tell AdLib: blank creative names in {n:,} {_pl(n, 'campaign')}", "creative", "cq-blank")
    add(cr.get("sm_display_size_creatives"),
        lambda n: f"Swap {n:,} display {_pl(n, 'creative')} running on Social Mirror", "creative", "cq-traffic")
    rc = ((ctx.get("device") or {}).get("recon") or {}).get("summary") or {}
    add(rc.get("clients_mismatched"),
        lambda n: f"Check device totals for {n:,} {_pl(n, 'client')}", "device", "dv-recon", "warn")
    return acts


def enrich(ctx):
    ctx.setdefault("sa", None)
    ctx["publishers"] = build_publishers(ctx)
    ctx["actions"] = build_actions(ctx)
    return ctx


def register(app):
    f = app.jinja_env.filters
    f["n0"], f["money"], f["money2"], f["pct"], f["short_reason"] = n0, money, money2, pct, short_reason

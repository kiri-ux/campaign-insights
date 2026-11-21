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


_SA_PROD = {"social mirror ads": "Social Mirror", "audio": "Online Audio", "b2b display": "Display",
            "restricted ctv": "CTV", "restricted social mirror ctv": "Social Mirror CTV",
            "ctv + video": "CTV, Video", "native": "Native Display"}


def sa_products(prodstr):
    """SA product names (from campaign group names) in AdLib's product names."""
    out = []
    for p in str(prodstr or "").split(", "):
        p = p.strip()
        if not p:
            continue
        for q in _SA_PROD.get(p.lower(), p).split(", "):
            if q not in out:
                out.append(q)
    return ", ".join(out)


def _merge_products(a, b):
    out = [x for x in str(a or "").split(", ") if x]
    for x in str(b or "").split(", "):
        if x and x not in out:
            out.append(x)
    return ", ".join(out)


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
            "al": {"impressions": num(r.get("impressions")), "clicks": num(r.get("clicks")),
                   "spend": num(r.get("spend")), "products": r.get("products") or ""},
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
        row["products"] = _merge_products(row["products"], sa_products(p.get("products")))
        row["sa_impressions"] = num(p.get("impressions"))
        row["sad"] = {"impressions": num(p.get("impressions")), "clicks": num(p.get("clicks")),
                      "spend": num(p.get("spend")), "products": sa_products(p.get("products"))}
        row["impressions"] += num(p.get("impressions"))
        row["clicks"] += num(p.get("clicks"))
        row["spend"] += num(p.get("spend"))
        flags = p.get("flags") or []
        if not row["action"]:
            if "Blocked" in flags:
                row["action"] = "sa_list"
            elif "To block" in flags or "High CTR" in flags:
                row["action"] = "block"
    # Top `limit` from each DSP, so the AdLib and StackAdapt filters each get a full list.
    keep = set()
    for d in ("al", "sad"):
        ranked = sorted((r for r in rows.values() if r.get(d)), key=lambda r: -r[d]["impressions"])
        keep.update(id(r) for r in ranked[:limit])
    out = sorted((r for r in rows.values() if id(r) in keep), key=lambda r: -r["impressions"])
    tot = (num((ctx.get("topcards") or {}).get("impressions"))
           + num((sa.get("summary") or {}).get("impressions"))) or sum(r["impressions"] for r in rows.values()) or 1.0
    al_tot = num((ctx.get("topcards") or {}).get("impressions")) or 1.0
    sa_tot = num((sa.get("summary") or {}).get("impressions")) or 1.0
    for r in out:
        r["ctr"] = r["clicks"] / r["impressions"] if r["impressions"] else 0.0
        r["cpm"] = r["spend"] / r["impressions"] * 1000 if r["impressions"] else 0.0
        r["share"] = r["impressions"] / tot
        # Per-DSP numbers, so the AdLib / StackAdapt filters show that DSP only.
        r["by_dsp"] = {}
        for dsp, d, t in (("adlib", r.get("al"), al_tot), ("sa", r.get("sad"), sa_tot)):
            if not d:
                continue
            i = d["impressions"]
            r["by_dsp"][dsp] = {
                "products": d["products"], "impressions": n0(i), "share": pct(i / t),
                "clicks": n0(d["clicks"]), "ctr": pct(d["clicks"] / i if i else 0),
                "spend": money(d["spend"]), "cpm": money2(d["spend"] / i * 1000 if i else 0),
                "last_served": (r["last_served"] or "—") if dsp == "adlib" else "—",
            }
    return out


def _sa_value(p):
    """What goes on the block sheet for an SA publisher: domain, bundle ID or iOS ID."""
    return p.get("key") or p.get("name") or ""


def build_block_rows(ctx):
    """The To block list: AdLib recommendations plus SA high CTR publishers,
    each tagged with the DSPs it served on."""
    ai = ctx.get("ai") or {}
    sa = ctx.get("sa") or {}
    sa_by_key = {_key(p.get("key") or p.get("name")): p for p in sa.get("publishers") or []}
    rows, seen = [], set()
    for kind, src in (("site", ai.get("sites") or []), ("app", ai.get("apps") or [])):
        for r in src:
            val = r.get("name") if kind == "site" else (r.get("app_id") or r.get("name"))
            k = _key(val)
            seen.add(k)
            p = sa_by_key.get(k)
            rows.append(dict(r, kind=kind, val=val, adlib=True, sa=bool(p),
                             why=short_reason(r.get("reason")) or r.get("category") or "",
                             sa_impressions=num(p.get("impressions")) if p else 0))
    for p in sa.get("high_ctr") or []:
        k = _key(p.get("key") or p.get("name"))
        if k in seen:
            continue
        seen.add(k)
        rows.append({
            "kind": p.get("type") or "site", "val": _sa_value(p),
            "name": p.get("title") or p.get("name"), "app_id": p.get("name") if p.get("type") == "app" else "",
            "products": sa_products(p.get("products")), "flag": "High CTR", "raw_value": _sa_value(p),
            "ttddv": False, "adlib": False, "sa": True,
            "why": f"CTR {num(p.get('x_norm')):.1f}x normal",
            "impressions": n0(p.get("impressions")), "clicks": n0(p.get("clicks")),
            "ctr": pct(p.get("ctr")), "spend": money(p.get("spend")), "cpm": money2(p.get("cpm")),
            "sa_impressions": num(p.get("impressions")),
        })
    return rows


def build_leak_rows(ctx):
    """Blocked publishers that still served: AdLib after the block date, SA in a
    week that started after the block date."""
    rows = []
    for r in ((ctx.get("blocklist_check") or {}).get("rows") or []):
        if r.get("leaking"):
            rows.append(dict(r, dsp="adlib"))
    for p in ((ctx.get("sa") or {}).get("blocked") or []):
        rows.append({
            "dsp": "sa", "name": p.get("title") or p.get("name"),
            "sub": p.get("name") if p.get("title") else "", "key": p.get("key"),
            "placement_type": p.get("type") or "site", "products": sa_products(p.get("products")),
            "lists": p.get("block_lists"), "date_added_str": p.get("date_added") or "—",
            "last_served": "", "post_impr": n0(p.get("impressions")), "post_spend": money(p.get("spend")),
            "cpm": money2(p.get("cpm")),
        })
    return rows


def _pl(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def build_actions(ctx):
    """Plain next steps, biggest problems first. Each links to its list."""
    acts = []

    def add(n, text, tab, anchor, tone="bad"):
        n = int(num(n))
        if n > 0:
            acts.append({"n": n, "text": text(n), "tab": tab, "anchor": anchor, "tone": tone})

    blk = ctx.get("block_rows") or []
    add(len(blk), lambda n: f"Block {n:,} {_pl(n, 'publisher')}", "publishers", "pub-block")
    add(sum(1 for r in ctx.get("leak_rows") or [] if r.get("dsp") == "adlib"),
        lambda n: f"Check AdLib block settings: {n:,} blocked {_pl(n, 'publisher')} still serving",
        "publishers", "pub-leak")
    add(sum(1 for r in ctx.get("leak_rows") or [] if r.get("dsp") == "sa"),
        lambda n: f"Add {n:,} {_pl(n, 'publisher')} to the StackAdapt exclusion list", "publishers", "pub-leak")
    add(len(ctx.get("sheet_audit") or []),
        lambda n: f"Fix {n:,} block sheet {_pl(n, 'entry', 'entries')}", "publishers", "pub-sheet", "warn")
    lcs = ctx.get("low_ctr_sites") or []
    add(sum(1 for r in lcs if not r.get("acct_blocked")),
        lambda n: f"Add client blocks for {n:,} low CTR {_pl(n, 'publisher')}", "publishers", "pub-lowctr", "warn")
    rcs = ((ctx.get("sa") or {}).get("recs") or {}).get("summary") or {}
    add(rcs.get("below"),
        lambda n: f"{n:,} StackAdapt {_pl(n, 'campaign')} below CTR goal", "recs", "rc-all", "warn")
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
    ctx["block_rows"] = build_block_rows(ctx)
    ctx["leak_rows"] = build_leak_rows(ctx)
    ctx["actions"] = build_actions(ctx)
    br, lr = ctx["block_rows"], ctx["leak_rows"]
    ctx["splits"] = {
        "block_adlib": sum(1 for r in br if r.get("adlib")), "block_sa": sum(1 for r in br if r.get("sa")),
        "block_sa_impr": sum(num(r.get("sa_impressions")) for r in br),
        "leak_adlib": sum(1 for r in lr if r.get("dsp") == "adlib"), "leak_sa": sum(1 for r in lr if r.get("dsp") == "sa"),
    }
    return ctx


def register(app):
    f = app.jinja_env.filters
    f["n0"], f["money"], f["money2"], f["pct"], f["short_reason"] = n0, money, money2, pct, short_reason

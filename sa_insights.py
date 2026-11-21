"""StackAdapt delivery for the insights dashboard (read-only GraphQL).

Needs STACKADAPT_TOKEN. Pulls campaign totals and site/app (APP attribute)
delivery for the report window, then flags:
  - publishers on the master block sheet that still served on SA
  - publishers the AdLib analysis recommends blocking that also serve on SA
  - high-CTR publishers (CTR >= 3x the product's SA norm)
"""
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

URL = "https://api.stackadapt.com/graphql"
PAGE = 2400  # the API caps a page at 2,400 records

# Low CTR is expected on these, so they never get a CTR flag.
NO_CTR = re.compile(r"CTV|Audio|DOOH", re.I)
HIGH_CTR_X = float(os.environ.get("SA_HIGH_CTR_X", "3"))
HIGH_CTR_MIN_IMPR = int(os.environ.get("SA_HIGH_CTR_MIN_IMPR", "1000"))
HIGH_CTR_MIN_CLICKS = int(os.environ.get("SA_HIGH_CTR_MIN_CLICKS", "10"))


def configured():
    return bool(os.environ.get("STACKADAPT_TOKEN", "").strip())


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _gql(query, variables=None, tries=4):
    req = urllib.request.Request(
        URL, json.dumps({"query": query, "variables": variables or {}}).encode(),
        {"Authorization": f"Bearer {os.environ['STACKADAPT_TOKEN'].strip()}",
         "Content-Type": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, context=_ctx(), timeout=120) as r:
                body = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(3 * 2 ** attempt)
    if body.get("errors"):
        raise RuntimeError(body["errors"][0]["message"])
    return body["data"]


CAMPAIGNS = """query($d: DateRangeInput, $after: String) {
  campaignDelivery(date: $d, granularity: TOTAL, dataType: TABLE) {
    ... on CampaignDeliveryOutcome { records(first: 500, after: $after) {
      nodes { campaign { id name channelType advertiser { id name } campaignGroup { id name } }
        metrics { impressionsBigint clicksBigint cost conversionsBigint } }
      pageInfo { hasNextPage endCursor } } } } }"""

PUBLISHERS = """query($d: DateRangeInput, $after: String) {
  campaignInsight(attributes: [APP], date: $d) {
    __typename
    ... on CampaignInsightOutcome { records(first: %d, after: $after) {
      nodes { campaign { id } attributes { app { domain title } }
        metrics { impressions clicks cost conversions } }
      pageInfo { hasNextPage endCursor } } } } }""" % PAGE


def _pages(query, field, d):
    after = None
    while True:
        for _ in range(40):  # insight queries answer "Progress" until the job is ready
            out = _gql(query, {"d": d, "after": after})[field]
            if out.get("__typename") != "Progress":
                break
            time.sleep(3)
        else:
            raise RuntimeError("StackAdapt report timed out")
        recs = (out or {}).get("records") or {}
        yield from recs.get("nodes") or []
        if not (recs.get("pageInfo") or {}).get("hasNextPage"):
            return
        after = recs["pageInfo"]["endCursor"]


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _product(group_name, channel):
    """Campaign group names are '<Advertiser> - <Product> - <line ID>'."""
    parts = [p.strip() for p in str(group_name or "").split(" - ")]
    if len(parts) >= 3 and parts[-1].isdigit():
        return parts[-2], parts[-1]
    return channel or "Other", ""


def app_key(dom):
    """SA reports apps as 'bundle/<id>', 'storeurl/<store link>' or 'name/<name>'.
    Return (key, is_app) with the key in the block sheet's form: bundle ID,
    numeric iOS ID, or the bare name."""
    d = str(dom or "").strip()
    low = d.lower()
    if low.startswith("bundle/"):
        return low[7:], True
    if low.startswith("storeurl/"):
        m = re.search(r"/id(\d{6,})", low) or re.search(r"[?&]id=([\w.]+)", low)
        return (m.group(1) if m else low[9:]), True
    if low.startswith("name/"):
        return low[5:], True
    return norm_site(low), False


def norm_site(s):
    s = str(s or "").strip().lower()
    s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split("?")[0]
    return (s[4:] if s.startswith("www.") else s).rstrip(".")


def _rate(n, d):
    return n / d if d else 0.0


def fetch(start, end, blocklist=None, rec_keys=None):
    """Return the StackAdapt block for the dashboard ctx. `blocklist` is the
    master block sheet map ({value_lower: {...}}); `rec_keys` the set of
    normalized publisher keys the AdLib analysis recommends blocking."""
    d = {"from": start, "to": end}
    blocklist = blocklist or {}
    block_keys = {}
    for k, e in blocklist.items():
        tabs = set(e.get("tabs") or [])
        if tabs and tabs <= {"Social Mirror"}:
            continue  # SA has one global exclusion list; SM-only blocks are not on it
        added = e.get("date_added")
        if added and str(added) >= str(start):
            continue  # blocked during or after this window; no daily grain to tell

        kk = str(k).strip().lower()
        block_keys.setdefault(kk, e)
        block_keys.setdefault(norm_site(kk), e)
    rec_keys = {str(k).strip().lower() for k in (rec_keys or ())} | {norm_site(k) for k in (rec_keys or ())}

    camps = {}
    for n in _pages(CAMPAIGNS, "campaignDelivery", d):
        c, m = n["campaign"], n["metrics"]
        product, line = _product((c.get("campaignGroup") or {}).get("name"), c.get("channelType"))
        camps[c["id"]] = {
            "id": c["id"], "name": c.get("name") or "", "channel": c.get("channelType") or "",
            "advertiser": (c.get("advertiser") or {}).get("name") or "",
            "group": (c.get("campaignGroup") or {}).get("name") or "",
            "product": product, "line": line,
            "impressions": _num(m.get("impressionsBigint")), "clicks": _num(m.get("clicksBigint")),
            "spend": _num(m.get("cost")), "conversions": _num(m.get("conversionsBigint")),
        }
    camps = {k: v for k, v in camps.items()
             if v["impressions"] > 0 and not any(str(v[f]).lower().startswith("test_")
                                                  for f in ("advertiser", "name", "group"))}

    # Product CTR norms (display-type products only)
    norms = {}
    for c in camps.values():
        p = norms.setdefault(c["product"], [0.0, 0.0])
        p[0] += c["impressions"]
        p[1] += c["clicks"]
    norm_ctr = {k: _rate(v[1], v[0]) for k, v in norms.items()}

    pubs = {}
    for n in _pages(PUBLISHERS, "campaignInsight", d):
        cid = (n.get("campaign") or {}).get("id")
        if cid not in camps:
            continue
        a = ((n.get("attributes") or {}).get("app") or {})
        dom = (a.get("domain") or "").strip()
        if not dom:
            continue
        key, is_app = app_key(dom)
        m = n["metrics"]
        c = camps.get(cid, {})
        p = pubs.setdefault(key, {"name": key if is_app else dom, "title": a.get("title") or "",
                                  "type": "app" if is_app else "site",
                                  "impressions": 0.0, "clicks": 0.0, "spend": 0.0, "conversions": 0.0,
                                  "products": set(), "advertisers": set(), "exp_clicks": 0.0,
                                  "ctr_impr": 0.0, "ctr_clicks": 0.0})
        i, cl = _num(m.get("impressions")), _num(m.get("clicks"))
        p["impressions"] += i
        p["clicks"] += cl
        p["spend"] += _num(m.get("cost"))
        p["conversions"] += _num(m.get("conversions"))
        if c:
            p["products"].add(c["product"])
            p["advertisers"].add(c["advertiser"])
            if not NO_CTR.search(c["product"]):
                p["ctr_impr"] += i
                p["ctr_clicks"] += cl
                p["exp_clicks"] += i * norm_ctr.get(c["product"], 0)

    pub_rows = []
    for key, p in pubs.items():
        if p["impressions"] <= 0:
            continue
        flags = []
        on_block = block_keys.get(key)
        if on_block:
            flags.append("Blocked")
        elif key in rec_keys:
            flags.append("To block")
        x = _rate(p["ctr_clicks"], p["exp_clicks"])
        if (p["ctr_impr"] >= HIGH_CTR_MIN_IMPR and p["ctr_clicks"] >= HIGH_CTR_MIN_CLICKS
                and x >= HIGH_CTR_X):
            flags.append("High CTR")
        pub_rows.append({
            "key": key, "name": p["name"], "title": p["title"], "type": p["type"],
            "impressions": p["impressions"], "clicks": p["clicks"], "spend": p["spend"],
            "conversions": p["conversions"], "ctr": _rate(p["clicks"], p["impressions"]),
            "cpm": _rate(p["spend"], p["impressions"]) * 1000, "x_norm": x,
            "products": ", ".join(sorted(p["products"])), "advertisers": len(p["advertisers"]),
            "flags": flags,
            "block_lists": ", ".join(sorted(on_block.get("tabs", []))) if on_block else "",
            "date_added": str(on_block.get("date_added") or "") if on_block else "",
        })
    pub_rows.sort(key=lambda r: -r["impressions"])

    adv = {}
    for c in camps.values():
        a = adv.setdefault(c["advertiser"], {"advertiser": c["advertiser"], "impressions": 0.0,
                                             "clicks": 0.0, "spend": 0.0, "conversions": 0.0,
                                             "campaigns": 0, "products": set()})
        for k in ("impressions", "clicks", "spend", "conversions"):
            a[k] += c[k]
        a["campaigns"] += 1
        a["products"].add(c["product"])
    adv_rows = []
    for a in adv.values():
        a["products"] = ", ".join(sorted(a["products"]))
        a["ctr"] = _rate(a["clicks"], a["impressions"])
        a["cpm"] = _rate(a["spend"], a["impressions"]) * 1000
        adv_rows.append(a)
    adv_rows.sort(key=lambda r: -r["impressions"])

    prod_rows = []
    for name, (imp, clk) in norms.items():
        sp = sum(c["spend"] for c in camps.values() if c["product"] == name)
        prod_rows.append({"product": name, "impressions": imp, "clicks": clk,
                          "ctr": _rate(clk, imp), "spend": sp, "cpm": _rate(sp, imp) * 1000,
                          "campaigns": sum(1 for c in camps.values() if c["product"] == name)})
    prod_rows.sort(key=lambda r: -r["impressions"])

    camp_rows = sorted(camps.values(), key=lambda r: -r["impressions"])
    for c in camp_rows:
        c["ctr"] = _rate(c["clicks"], c["impressions"])
        c["cpm"] = _rate(c["spend"], c["impressions"]) * 1000

    ti = sum(c["impressions"] for c in camp_rows)
    tc = sum(c["clicks"] for c in camp_rows)
    ts = sum(c["spend"] for c in camp_rows)
    blocked = [r for r in pub_rows if "Blocked" in r["flags"]]
    to_block = [r for r in pub_rows if "To block" in r["flags"]]
    high = [r for r in pub_rows if "High CTR" in r["flags"]]
    return {
        "start": start, "end": end,
        "summary": {
            "impressions": ti, "clicks": tc, "ctr": _rate(tc, ti), "spend": ts,
            "cpm": _rate(ts, ti) * 1000, "campaigns": len(camp_rows), "advertisers": len(adv_rows),
            "publishers": len(pub_rows),
            "blocked_serving": len(blocked),
            "blocked_impr": sum(r["impressions"] for r in blocked),
            "blocked_spend": sum(r["spend"] for r in blocked),
            "to_block": len(to_block), "high_ctr": len(high),
        },
        "products": prod_rows,
        "advertisers": adv_rows,
        "campaigns": camp_rows,
        "publishers": pub_rows[:2000],
        "blocked": blocked,
        "to_block": to_block,
        "high_ctr": sorted(high, key=lambda r: -r["x_norm"]),
    }

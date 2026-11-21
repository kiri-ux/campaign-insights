"""StackAdapt campaign recommendations: compare each campaign's CTR with its
product's CTR goal (Product Details sheet), then point at the sites, devices,
operating systems, ads and audiences pulling it down or lifting it up.

Rules (all on the report window):
  - Sites: exclude a site with >= MIN_SITE_IMPR impressions and CTR under both
    a quarter of the goal and half the campaign's CTR (zero clicks counts).
  - Devices / OS: a slice with 10-50% of the campaign's impressions and CTR
    under half the campaign's gets "Exclude" (no clicks) or "Bid down"; a slice
    with >= 5% of impressions and CTR 1.5x the campaign's gets "Bid up".
  - Ads: pause an ad with >= MIN_AD_IMPR impressions and CTR under half the best
    ad's; flag campaigns running one ad, and display campaigns missing core sizes.
  - Audiences: same split as devices: "Remove" or "Shift budget to".
"""
import os
import re
import time

MIN_SITE_IMPR = int(os.environ.get("SA_REC_MIN_SITE_IMPR", "1000"))
MIN_AD_IMPR = int(os.environ.get("SA_REC_MIN_AD_IMPR", "1000"))
MIN_CAMP_IMPR = int(os.environ.get("SA_REC_MIN_CAMP_IMPR", "5000"))
CORE_SIZES = ["300x250", "728x90", "320x50", "160x600", "300x600"]

# CTR goals from the Product Details sheet (11IuAikFBQL1bDGx8sOuOQ173SZ69bcbShcQsdtQ_V1k).
# First match wins, so the more specific names come first.
GOALS = [
    (r"social mirror.*ctv|ctv.*social mirror", None),
    (r"social mirror", 0.006),
    (r"native video", 0.003),
    (r"native", 0.003),
    (r"ctv \+ video|ctv, video", 0.005),
    (r"ctv|connected tv|audio|dooh|out.of.home", None),
    (r"conquest", 0.005),
    (r"video", 0.005),
    (r"dynamic", 0.004),
    (r"geo.?fram", 0.003),
    (r"display", 0.004),
]
NO_CTR_CHANNEL = {"CTV", "AUDIO", "DOOH"}

DEVICE_NAMES = {"APP": "App", "CONNECTED_DEVICE": "Connected device", "CONNECTED_TV": "CTV",
                "DESKTOP": "Desktop", "MOBILE_APP": "Mobile app", "MOBILE_WEB": "Mobile web",
                "SET_TOP": "Set-top box", "TABLET_APP": "Tablet app", "TABLET_WEB": "Tablet web",
                "UNKNOWN": "Unknown"}
OS_NAMES = {"IOS": "iOS", "MACINTOSH": "macOS", "CHROME_OS": "Chrome OS", "WEBOS": "webOS",
            "APPLE_TV": "Apple TV"}


def goal_for(product, channel=""):
    if str(channel or "").upper() in NO_CTR_CHANNEL:
        return None
    p = str(product or "").lower()
    for pat, g in GOALS:
        if re.search(pat, p):
            return g
    return None


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _rate(n, d):
    return n / d if d else 0.0


def _q(attr, sel, page):
    return """query($d: DateRangeInput, $after: String) {
  campaignInsight(attributes: [%s], date: $d) {
    __typename
    ... on CampaignInsightOutcome { records(first: %d, after: $after) {
      nodes { campaign { id } attributes { %s } metrics { impressions clicks cost } }
      pageInfo { hasNextPage endCursor } } } } }""" % (attr, page, sel)


def _audience_sel(gql):
    try:
        types = gql('{ __type(name: "InsightAudience") { possibleTypes { name } } }')
        names = [t["name"] for t in types["__type"]["possibleTypes"]]
    except Exception:
        names = ["RetargetingPixel"]
    return "audience { __typename " + " ".join("... on %s { id name }" % t for t in names) + " }"


def _label(attr, a):
    if attr == "DEVICE_TYPE":
        v = a.get("deviceType") or "UNKNOWN"
        return v, DEVICE_NAMES.get(v, v.title())
    if attr == "DEVICE_OS":
        v = a.get("deviceOs") or "OTHER"
        return v, OS_NAMES.get(v, v.replace("_", " ").title())
    if attr == "AD":
        ad = a.get("ad") or {}
        return ad.get("id") or "", ad.get("name") or ad.get("id") or ""
    if attr == "AUDIENCE":
        au = a.get("audience") or {}
        return au.get("id") or "", au.get("name") or ""
    return "", ""


def _pull(pages, d, gql, page, wanted):
    sels = {"DEVICE_TYPE": "deviceType", "DEVICE_OS": "deviceOs",
            "AD": "ad { id name creativeSize }", "AUDIENCE": _audience_sel(gql)}
    out = {k: {} for k in sels}

    def one(attr):
        rows = {}
        for n in pages(_q(attr, sels[attr], page), "campaignInsight", d):
            cid = (n.get("campaign") or {}).get("id")
            if cid not in wanted:
                continue
            a = n.get("attributes") or {}
            key, name = _label(attr, a)
            if not key:
                continue
            m = n.get("metrics") or {}
            r = rows.setdefault(cid, {}).setdefault(key, {"name": name, "impressions": 0.0, "clicks": 0.0,
                                                          "spend": 0.0, "size": ""})
            r["impressions"] += _num(m.get("impressions"))
            r["clicks"] += _num(m.get("clicks"))
            r["spend"] += _num(m.get("cost"))
            if attr == "AD":
                r["size"] = (a.get("ad") or {}).get("creativeSize") or ""
        return attr, rows

    # One at a time: parallel insight jobs trip SA's rate limit.
    for attr in sels:
        for attempt in range(5):
            try:
                out[attr] = one(attr)[1]
                break
            except RuntimeError as e:
                if "rate limit" not in str(e).lower() or attempt == 4:
                    raise
                time.sleep(15 * (attempt + 1))
    return out


def _slices(kind, rows, c, goal):
    """Device / OS / audience recs for one campaign."""
    recs = []
    ci, cctr = c["impressions"], c["ctr"]
    for r in sorted(rows.values(), key=lambda r: -r["impressions"]):
        i = r["impressions"]
        if i <= 0:
            continue
        share, ctr = i / ci, _rate(r["clicks"], i)
        base = {"kind": kind, "target": r["name"], "impressions": i, "clicks": r["clicks"],
                "ctr": ctr, "share": share}
        if share > 0.5:
            continue  # most of the campaign; cutting it isn't a tweak
        if share >= 0.10 and i >= MIN_SITE_IMPR and ctr < cctr * 0.5:
            if kind == "Audience":
                recs.append(dict(base, action="Remove", tone="bad"))
            elif r["clicks"] == 0:
                recs.append(dict(base, action="Exclude", tone="bad"))
            else:
                recs.append(dict(base, action="Bid down 30%", tone="warn"))
        elif share >= 0.05 and cctr > 0 and ctr >= cctr * 1.5 and r["clicks"] >= 5:
            recs.append(dict(base, action="Shift budget to" if kind == "Audience" else "Bid up 20%",
                             tone="ok"))
    return recs


def build(d, camps, camp_sites, pages, gql, page=2400):
    """Returns {"campaigns": [...], "summary": {...}}. `camp_sites` is
    {campaign_id: {site_key: {"name", "type", "impressions", "clicks"}}}."""
    wanted = {}
    for cid, c in camps.items():
        g = goal_for(c["product"], c.get("channel"))
        if g and c["impressions"] >= MIN_CAMP_IMPR:
            wanted[cid] = g
    if not wanted:
        return {"campaigns": [], "summary": {"campaigns": 0, "below": 0, "recs": 0}}
    br = _pull(pages, d, gql, page, wanted)

    out = []
    for cid, goal in wanted.items():
        c = dict(camps[cid])
        c["ctr"] = _rate(c["clicks"], c["impressions"])
        c["goal"] = goal
        c["pct_goal"] = _rate(c["ctr"], goal)
        recs = []

        # Sites
        sites = (camp_sites or {}).get(cid) or {}
        bad = [s for s in sites.values()
               if s["impressions"] >= MIN_SITE_IMPR
               and _rate(s["clicks"], s["impressions"]) < min(goal * 0.25, c["ctr"] * 0.5)]
        bad.sort(key=lambda s: -s["impressions"])
        for s in bad[:10]:
            recs.append({"kind": "Site", "target": s["name"], "action": "Exclude", "tone": "bad",
                         "impressions": s["impressions"], "clicks": s["clicks"],
                         "ctr": _rate(s["clicks"], s["impressions"]),
                         "share": _rate(s["impressions"], c["impressions"])})

        recs += _slices("Device", br["DEVICE_TYPE"].get(cid) or {}, c, goal)
        recs += _slices("OS", br["DEVICE_OS"].get(cid) or {}, c, goal)

        # Creatives
        ads = [a for a in (br["AD"].get(cid) or {}).values() if a["impressions"] > 0]
        big = [a for a in ads if a["impressions"] >= MIN_AD_IMPR]
        if big:
            best = max(_rate(a["clicks"], a["impressions"]) for a in big)
            for a in sorted(big, key=lambda a: -a["impressions"]):
                ctr = _rate(a["clicks"], a["impressions"])
                share = _rate(a["impressions"], c["impressions"])
                if len(big) > 1 and best > 0 and ctr < best * 0.5 and share <= 0.5:
                    recs.append({"kind": "Creative", "target": a["name"], "action": "Pause", "tone": "warn",
                                 "impressions": a["impressions"], "clicks": a["clicks"], "ctr": ctr,
                                 "share": _rate(a["impressions"], c["impressions"])})
        if len(ads) == 1:
            recs.append({"kind": "Creative", "target": "1 ad running", "action": "Add creatives",
                         "tone": "warn", "impressions": ads[0]["impressions"], "clicks": ads[0]["clicks"],
                         "ctr": _rate(ads[0]["clicks"], ads[0]["impressions"]), "share": 1.0})
        if str(c.get("channel") or "").upper() == "DISPLAY" and ads:
            sizes = {a["size"] for a in ads}
            missing = [s for s in CORE_SIZES if s not in sizes]
            if len(missing) >= 2:
                recs.append({"kind": "Creative", "target": ", ".join(missing), "action": "Add sizes",
                             "tone": "warn", "impressions": 0, "clicks": 0, "ctr": 0, "share": 0})

        recs += _slices("Audience", br["AUDIENCE"].get(cid) or {}, c, goal)

        # Below goal: show everything. On goal: only the upside (bid up / shift budget).
        if c["ctr"] >= goal:
            recs = [r for r in recs if r["tone"] == "ok"]
        c["recs"] = recs
        c["status"] = "On goal" if c["ctr"] >= goal else "Below goal"
        out.append(c)

    out.sort(key=lambda c: (c["status"] != "Below goal", c["pct_goal"], -c["impressions"]))
    below = [c for c in out if c["status"] == "Below goal"]
    return {"campaigns": out,
            "summary": {"campaigns": len(out), "below": len(below),
                        "recs": sum(len(c["recs"]) for c in out),
                        "sites": sum(1 for c in out for r in c["recs"] if r["kind"] == "Site")}}

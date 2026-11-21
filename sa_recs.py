"""StackAdapt campaign recommendations (on sa_insights.REC_DAYS of delivery, default 30): compare each campaign's CTR with its
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
import hashlib
import os
import re
import time

MIN_SITE_IMPR = int(os.environ.get("SA_REC_MIN_SITE_IMPR", "2500"))
MIN_AD_IMPR = int(os.environ.get("SA_REC_MIN_AD_IMPR", "2500"))
MIN_CAMP_IMPR = int(os.environ.get("SA_REC_MIN_CAMP_IMPR", "10000"))
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
                                                          "spend": 0.0, "size": "",
                                                          "type": (a.get("audience") or {}).get("__typename") or ""})
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
    """Device / OS / audience recs for one campaign or group."""
    recs = []
    ci, cctr = c["impressions"], c["ctr"]
    for key, r in sorted(rows.items(), key=lambda kv: -kv[1]["impressions"]):
        i = r["impressions"]
        if i <= 0:
            continue
        share, ctr = i / ci, _rate(r["clicks"], i)
        base = {"kind": kind, "target": r["name"], "impressions": i, "clicks": r["clicks"],
                "ctr": ctr, "share": share, "key": key, "aud_type": r.get("type", "")}
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


def _site_recs(sites, ci, cctr, goal):
    bad = [dict(x, key=k) for k, x in sites.items()
           if x["impressions"] >= MIN_SITE_IMPR
           and _rate(x["clicks"], x["impressions"]) < min(goal * 0.25, cctr * 0.5)]
    bad.sort(key=lambda x: -x["impressions"])
    return [{"kind": "Site", "target": x["name"], "action": "Exclude", "tone": "bad", "key": x["key"],
             "impressions": x["impressions"], "clicks": x["clicks"],
             "ctr": _rate(x["clicks"], x["impressions"]), "share": _rate(x["impressions"], ci)}
            for x in bad[:10]]


def _merge(dicts):
    """Sum per-key metric dicts across campaigns."""
    out = {}
    for d in dicts:
        for k, r in (d or {}).items():
            o = out.setdefault(k, {"name": r["name"], "impressions": 0.0, "clicks": 0.0, "spend": 0.0,
                                   "size": r.get("size", ""), "type": r.get("type", "")})
            o["impressions"] += r["impressions"]
            o["clicks"] += r["clicks"]
            o["spend"] += r.get("spend", 0.0)
    return out


def _creative_recs(ads_d, c):
    recs = []
    ads = [dict(a, key=k) for k, a in (ads_d or {}).items() if a["impressions"] > 0]
    big = [a for a in ads if a["impressions"] >= MIN_AD_IMPR]
    if len(big) > 1:
        best = max(_rate(a["clicks"], a["impressions"]) for a in big)
        for a in sorted(big, key=lambda a: -a["impressions"]):
            ctr = _rate(a["clicks"], a["impressions"])
            share = _rate(a["impressions"], c["impressions"])
            if best > 0 and ctr < best * 0.5 and share <= 0.5:
                recs.append({"kind": "Creative", "target": a["name"], "action": "Pause", "tone": "warn", "key": a["key"],
                             "impressions": a["impressions"], "clicks": a["clicks"], "ctr": ctr, "share": share})
    if len(ads) == 1:
        recs.append({"kind": "Creative", "target": "1 ad running", "action": "Add creatives", "tone": "warn",
                     "impressions": ads[0]["impressions"], "clicks": ads[0]["clicks"],
                     "ctr": _rate(ads[0]["clicks"], ads[0]["impressions"]), "share": 1.0})
    if str(c.get("channel") or "").upper() == "DISPLAY" and ads:
        sizes = {a["size"] for a in ads}
        missing = [x for x in CORE_SIZES if x not in sizes]
        if len(missing) >= 2:
            recs.append({"kind": "Creative", "target": ", ".join(missing), "action": "Add sizes",
                         "tone": "warn", "impressions": 0, "clicks": 0, "ctr": 0, "share": 0})
    return recs


def _short(name, adv):
    n = str(name or "")
    for pre in (adv + " - ", adv + "- ", adv + " "):
        if adv and n.startswith(pre):
            return n[len(pre):]
    return n


PACING_Q = """query($ids: [ID!]) { campaignGroups(first: 50, filterBy: {ids: $ids}) { nodes { id
  pacing { flightPacing { calculatedPacePercent lifetimeBudget }
           projectedDailySpend { currentPacePercentVar } } } } }"""


def _pacing(gql, ids):
    """SA's live pacing per group: projected share of the flight budget and
    current daily pace vs what's needed (1.0 = on pace)."""
    out, ids = {}, [i for i in ids if i]
    for k in range(0, len(ids), 50):
        try:
            nodes = gql(PACING_Q, {"ids": ids[k:k + 50]})["campaignGroups"]["nodes"]
        except Exception:
            continue
        for g in nodes:
            p = g.get("pacing") or {}
            fp, pd = p.get("flightPacing") or {}, p.get("projectedDailySpend") or {}
            if fp.get("calculatedPacePercent") is None:
                continue
            out[g["id"]] = {"projected": _num(fp.get("calculatedPacePercent")),
                            "daily": 1 + _num(pd.get("currentPacePercentVar"))}
    return out


def _trend(load_camps, end, gids):
    """Last 7 days vs the 7 before, per group, ending `end`."""
    import datetime as dt
    e = dt.date.fromisoformat(str(end)[:10])
    wins = {"l7": (e - dt.timedelta(days=6), e), "p7": (e - dt.timedelta(days=13), e - dt.timedelta(days=7))}
    out = {}
    for w, (a, b) in wins.items():
        try:
            cs = load_camps({"from": a.isoformat(), "to": b.isoformat()})
        except Exception:
            continue
        for c in cs.values():
            g = c.get("group_id")
            if g not in gids:
                continue
            t = out.setdefault(g, {}).setdefault(w, {"impressions": 0.0, "clicks": 0.0, "spend": 0.0})
            for k in t:
                t[k] += c[k]
    for g, ws in out.items():
        for w in ("l7", "p7"):
            t = ws.setdefault(w, {"impressions": 0.0, "clicks": 0.0, "spend": 0.0})
            t["ctr"] = _rate(t["clicks"], t["impressions"])
            t["cpm"] = _rate(t["spend"], t["impressions"]) * 1000
        l, p = ws["l7"], ws["p7"]
        ws["new"] = p["impressions"] < max(1000, l["impressions"] * 0.1)
        ws["d_impr"] = _rate(l["impressions"] - p["impressions"], p["impressions"]) if p["impressions"] else None
        ws["d_ctr"] = _rate(l["ctr"] - p["ctr"], p["ctr"]) if p["ctr"] else None
        ws["d_spend"] = _rate(l["spend"] - p["spend"], p["spend"]) if p["spend"] else None
    return out


# Insight device -> bid factor rule (device type + supply type)
DEVICE_RULES = {
    "MOBILE_APP": (["PHONE"], ["IN_APP"]), "MOBILE_WEB": (["PHONE"], ["MOBILE_WEB"]),
    "TABLET_APP": (["TABLET"], ["IN_APP"]), "TABLET_WEB": (["TABLET"], ["MOBILE_WEB"]),
    "DESKTOP": (["DESKTOP"], None), "CONNECTED_TV": (["CONNECTED_TV"], None),
    "SET_TOP": (["SET_TOP"], None), "CONNECTED_DEVICE": (["CONNECTED_DEVICE"], None),
    "APP": (None, ["IN_APP"]),
}
BID_OS = {"ANDROID", "IOS", "LINUX", "MACINTOSH", "PLAYSTATION", "ROKU", "TIZEN", "WEBOS",
          "WINDOWS", "WINDOWS_PHONE"}


def _factor(action):
    if action in ("Exclude", "Remove"):
        return 0
    m = re.search(r"([+-]?)(\d+)%", action)
    if action.startswith("Bid down") and m:
        return 100 - int(m.group(2))
    if action.startswith("Bid up") and m:
        return 100 + int(m.group(2))
    if action.startswith("Shift budget"):
        return 120
    return None


def push_spec(r, group_id, campaign_ids, group_wide):
    """What the Push button does for a rec, or None when it can't be pushed.
      group_exclude: add domains to the campaign group's domain exclusions
      bid_factor:    add a bid factor (100 = no change, 0 = no bids) to each campaign
      pause_ads:     pause ads"""
    kind, action, key = r.get("kind"), r.get("action"), r.get("key")
    if not key:
        return None
    if kind == "Site":
        if group_wide:
            return {"type": "group_exclude", "group_id": group_id, "domains": [key]}
        return {"type": "bid_factor", "campaign_ids": campaign_ids, "factor": 0,
                "rule": {"domainRule": {"domains": [key]}}}
    if kind == "Creative" and action == "Pause":
        return {"type": "pause_ads", "ad_ids": [key], "campaign_ids": campaign_ids}
    f = _factor(action or "")
    if f is None:
        return None
    if kind == "Device":
        dev = DEVICE_RULES.get(key)
        if not dev:
            return None
        rule = {}
        if dev[0]:
            rule["deviceTypeRule"] = {"deviceTypes": dev[0]}
        if dev[1]:
            rule["supplyTypeRule"] = {"supplyTypes": dev[1]}
    elif kind == "OS":
        if key not in BID_OS:
            return None
        rule = {"deviceOsRule": {"deviceOses": [key]}}
    elif kind == "Audience":
        t = r.get("aud_type") or ""
        if t == "RetargetingPixel":
            rule = {"retargetingPixelRule": {"retargetingPixelIds": [key]}}
        elif t.endswith("CustomSegment"):
            rule = {"customSegmentRule": {"customSegmentIds": [key]}}
        else:
            return None
    else:
        return None
    return {"type": "bid_factor", "campaign_ids": campaign_ids, "factor": f, "rule": rule}


def build(d, camps, camp_sites, pages, gql, page=2400, load_camps=None):
    """One entry per campaign group. Group recs ("All campaigns") come from the
    group's combined breakdowns; a campaign gets its own rec only when the group
    rec doesn't already cover it. Creatives are always per campaign."""
    wanted = {cid: goal_for(c["product"], c.get("channel")) for cid, c in camps.items()}
    wanted = {cid: g for cid, g in wanted.items() if g}
    groups = {}
    for cid in wanted:
        c = camps[cid]
        groups.setdefault(c.get("group_id") or c["group"], []).append(cid)
    groups = {g: ids for g, ids in groups.items()
              if sum(camps[i]["impressions"] for i in ids) >= MIN_CAMP_IMPR}
    if not groups:
        return {"groups": [], "summary": {"groups": 0, "below": 0, "recs": 0}}
    keep = {i for ids in groups.values() for i in ids}
    br = _pull(pages, d, gql, page, keep)
    pace = _pacing(gql, list(groups))
    trend = _trend(load_camps, d["to"], set(groups)) if load_camps else {}

    out = []
    for gid, ids in groups.items():
        cs = [camps[i] for i in ids]
        c0 = cs[0]
        goal = wanted[ids[0]]
        g = {"id": gid, "name": c0["group"], "advertiser": c0["advertiser"], "product": c0["product"],
             "line": c0["line"], "goal": goal,
             "impressions": sum(c["impressions"] for c in cs), "clicks": sum(c["clicks"] for c in cs),
             "spend": sum(c["spend"] for c in cs)}
        g["ctr"] = _rate(g["clicks"], g["impressions"])
        g["pct_goal"] = _rate(g["ctr"], goal)
        g["status"] = "On goal" if g["ctr"] >= goal else "Below goal"
        g["pacing"] = pace.get(gid)
        g["trend"] = trend.get(gid)
        gi, gctr = g["impressions"], g["ctr"]

        # Group-wide recs on the combined breakdowns
        recs = _site_recs(_merge((camp_sites or {}).get(i) for i in ids), gi, gctr, goal)
        recs += _slices("Device", _merge(br["DEVICE_TYPE"].get(i) for i in ids), g, goal)
        recs += _slices("OS", _merge(br["DEVICE_OS"].get(i) for i in ids), g, goal)
        recs += _slices("Audience", _merge(br["AUDIENCE"].get(i) for i in ids), g, goal)
        multi = len(ids) > 1
        for r in recs:
            r["scope"] = "All campaigns" if multi else ""
        covered = {(r["kind"], r["target"]) for r in recs}

        # Per-campaign recs the group recs don't cover
        camp_rows = []
        for i in sorted(ids, key=lambda i: -camps[i]["impressions"]):
            c = dict(camps[i])
            c["ctr"] = _rate(c["clicks"], c["impressions"])
            c["short"] = _short(c["name"], c["advertiser"])
            own = []
            if multi and c["impressions"] >= MIN_CAMP_IMPR:
                own += _site_recs((camp_sites or {}).get(i) or {}, c["impressions"], c["ctr"], goal)
                own += _slices("Device", br["DEVICE_TYPE"].get(i) or {}, c, goal)
                own += _slices("OS", br["DEVICE_OS"].get(i) or {}, c, goal)
                own += _slices("Audience", br["AUDIENCE"].get(i) or {}, c, goal)
                own = [r for r in own if (r["kind"], r["target"]) not in covered]
            own += _creative_recs(br["AD"].get(i), c)
            for r in own:
                r["scope"] = c["short"] if multi else ""
            recs += own
            camp_rows.append({"id": i, "name": c["short"], "impressions": c["impressions"],
                              "clicks": c["clicks"], "ctr": c["ctr"], "spend": c["spend"],
                              "pct_goal": _rate(c["ctr"], goal)})

        # Same "Add sizes" on every campaign -> one group line
        sz = [r for r in recs if r["action"] == "Add sizes"]
        if multi and len(sz) > 1 and len({r["target"] for r in sz}) == 1 and len(sz) == len(ids):
            recs = [r for r in recs if r["action"] != "Add sizes"] + [dict(sz[0], scope="All campaigns")]

        # On goal: only the upside
        if g["status"] == "On goal":
            recs = [r for r in recs if r["tone"] == "ok"]
        order = {"Site": 0, "Device": 1, "OS": 2, "Audience": 3, "Creative": 4}
        recs.sort(key=lambda r: (r["scope"] not in ("", "All campaigns"), order.get(r["kind"], 9)))
        by_short = {_short(camps[i]["name"], camps[i]["advertiser"]): i for i in ids}
        for r in recs:
            wide = r["scope"] in ("", "All campaigns")
            cids = list(ids) if wide else [by_short.get(r["scope"])]
            r["push"] = push_spec(r, gid, [c for c in cids if c], wide) if all(cids) else None
            r["rid"] = hashlib.sha1("|".join(str(x) for x in (gid, r["scope"], r["kind"], r.get("key"),
                                                               r["action"])).encode()).hexdigest()[:12]
        g["recs"], g["campaigns"] = recs, camp_rows
        out.append(g)

    out.sort(key=lambda g: (g["status"] != "Below goal", g["pct_goal"], -g["impressions"]))
    return {"groups": out,
            "summary": {"groups": len(out), "below": sum(1 for g in out if g["status"] == "Below goal"),
                        "recs": sum(len(g["recs"]) for g in out)}}

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
  - Audiences: same split as devices: "Remove" or "Bid up 20%".
"""
import hashlib
import os
import re
import time

# Every rec row needs this many impressions (kiri 2026-11-23: 5K)
MIN_SITE_IMPR = int(os.environ.get("SA_REC_MIN_SITE_IMPR", "5000"))
MIN_AD_IMPR = int(os.environ.get("SA_REC_MIN_AD_IMPR", "5000"))
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
        elif share >= 0.05 and i >= MIN_SITE_IMPR and cctr > 0 and ctr >= cctr * 1.5 and r["clicks"] >= 5:
            recs.append(dict(base, action="Bid up 20%",
                             tone="ok"))
    return recs


SUPPLY = {"DESKTOP": "desktop", "MOBILE_WEB": "web", "TABLET_WEB": "web",
          "MOBILE_APP": "app", "TABLET_APP": "app", "APP": "app"}
SUPPLY_NAMES = {"desktop": "Desktop", "web": "Mobile web", "app": "In-app"}


def _down5(x):
    return max(0.05, int(x * 20) / 20)


def _device_recs(rows, c, goal):
    """Device recs on SA's supply caps (Desktop / Mobile web / In-app, each a max
    share of the campaign's impressions). Capping the weak supply moves delivery to
    the others without paying more. A bid factor is only used when caps can't
    help: one supply type only, or nothing else is below the campaign's CTR."""
    ci, cctr = c["impressions"], c["ctr"]
    sup = {}
    for key, r in rows.items():
        k = SUPPLY.get(key)
        if not k or r["impressions"] <= 0:
            continue
        a = sup.setdefault(k, {"name": SUPPLY_NAMES[k], "impressions": 0, "clicks": 0})
        a["impressions"] += r["impressions"]
        a["clicks"] += r["clicks"]
    if len(sup) < 2:
        return _slices("Device", rows, c, goal)
    st = {k: dict(a, share=a["impressions"] / ci, ctr=_rate(a["clicks"], a["impressions"]))
          for k, a in sup.items()}
    recs = []

    def base(k):
        a = st[k]
        return {"kind": "Device", "target": a["name"], "impressions": a["impressions"],
                "clicks": a["clicks"], "ctr": a["ctr"], "share": a["share"], "key": k}

    weak = [k for k, a in st.items() if a["share"] >= 0.10 and a["impressions"] >= MIN_SITE_IMPR
            and a["ctr"] < cctr * 0.5]
    for k in weak:
        a = st[k]
        # Never cut a device to 0: zero clicks gets the 5% floor, not an exclude
        cap = 0.05 if a["clicks"] == 0 else _down5(a["share"] * 0.5)
        recs.append(dict(base(k), action=f"Cap at {round(cap * 100)}%", tone="warn", caps={k: cap}))
    if weak:
        return recs  # the caps already push delivery to the stronger supply
    for k, a in sorted(st.items(), key=lambda kv: -kv[1]["ctr"]):
        if not (a["share"] >= 0.05 and a["impressions"] >= MIN_SITE_IMPR and cctr > 0
                and a["ctr"] >= cctr * 1.5 and a["clicks"] >= 5):
            continue
        others = {o: _down5(st[o]["share"] * 0.75) for o in st
                  if o != k and st[o]["ctr"] < cctr and st[o]["share"] >= 0.10}
        if others:
            recs.append(dict(base(k), action="Shift impr here", tone="ok", caps=others))
        else:
            recs.append(dict(base(k), action="Bid up 20%", tone="ok"))
        break
    return recs


def _site_recs(sites, ci, cctr, goal):
    bad = [dict(x, key=k) for k, x in sites.items()
           if x["impressions"] >= MIN_SITE_IMPR
           and _rate(x["clicks"], x["impressions"]) < min(goal * 0.25, cctr * 0.5)]
    bad.sort(key=lambda x: -x["impressions"])
    return [{"kind": "Site", "target": x["name"], "action": "Exclude", "tone": "bad", "key": x["key"],
             "site_type": x.get("type", ""), "raw": x.get("raw", ""),
             "impressions": x["impressions"], "clicks": x["clicks"],
             "ctr": _rate(x["clicks"], x["impressions"]), "share": _rate(x["impressions"], ci)}
            for x in bad[:10]]


def _merge(dicts):
    """Sum per-key metric dicts across campaigns."""
    out = {}
    for d in dicts:
        for k, r in (d or {}).items():
            o = out.setdefault(k, {"name": r["name"], "impressions": 0.0, "clicks": 0.0, "spend": 0.0,
                                   "size": r.get("size", ""), "type": r.get("type", ""),
                                   "raw": r.get("raw", "")})
            o["impressions"] += r["impressions"]
            o["clicks"] += r["clicks"]
            o["spend"] += r.get("spend", 0.0)
    return out


def _slice_impr(r, cid, camp_sites, br):
    """One campaign's impressions on the slice a group rec targets."""
    k = r.get("key")
    if r["kind"] == "Site":
        return ((camp_sites or {}).get(cid) or {}).get(k, {}).get("impressions", 0)
    if r["kind"] == "Device":
        rows = br["DEVICE_TYPE"].get(cid) or {}
        if k in SUPPLY_NAMES:
            return sum(x["impressions"] for d, x in rows.items() if SUPPLY.get(d) == k)
        return (rows.get(k) or {}).get("impressions", 0)
    attr = {"OS": "DEVICE_OS", "Audience": "AUDIENCE"}.get(r["kind"])
    return ((br[attr].get(cid) or {}).get(k) or {}).get("impressions", 0) if attr else 0


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
    "desktop": (["DESKTOP"], None), "web": (None, ["MOBILE_WEB"]), "app": (None, ["IN_APP"]),
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
    if action == "Raise CPM":
        if not campaign_ids:
            return None
        return {"type": "raise_bid", "campaign_ids": campaign_ids, "factor": 1.2,
                "group_id": group_id, "share": round(r.get("camp_share") or 0, 4)}
    if action in ("Add High CTR inventory", "Copy with High CTR inventory"):
        return {"type": "high_ctr", "campaign_ids": campaign_ids,
                "mode": "copy" if action.startswith("Copy") else "edit"} if campaign_ids else None
    if action == "Add viewability segment":
        return {"type": "viewability", "campaign_ids": campaign_ids} if campaign_ids else None
    if action == "Add frequency cap":
        return {"type": "freq_cap", "campaign_ids": campaign_ids, "limit": r.get("cap", FREQ_CAP), "hours": 24}
    if not key:
        return None
    if kind == "Site":
        if group_wide:
            val = r.get("raw") or (f"app:{key}" if r.get("site_type") == "app" else key)
            if val.lower().startswith("name/") or (r.get("site_type") == "app" and not re.fullmatch(r"[\w.]+", key)):
                return None  # SA only names this app; no store URL to exclude
            return {"type": "group_exclude", "group_id": group_id, "domains": [val]}
        return {"type": "bid_factor", "campaign_ids": campaign_ids, "factor": 0,
                "rule": {"domainRule": {"domains": [key]}}}
    if kind == "Device" and r.get("caps") is not None:
        return {"type": "device_caps", "campaign_ids": campaign_ids, "caps": r["caps"]}
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


MIN_MEASURED = 2000      # measured impressions before a viewability rec
VIEW_FLOOR = 0.60        # group viewability under this -> DV viewability segment
FREQ_CAP = 3             # recommended cap: impressions per person per 24 hours
FREQ_CAP_RT = 5          # retargeting pools are small and warm
FREQ_MAX = 5.0           # impressions per person over the window, no cap set
CTR_GOAL_CHANNELS = {"DISPLAY", "NATIVE"}


def _settings(gql, ids):
    """Goals and frequency cap per campaign. {} on any failure: these recs are
    extras and must never take the page down."""
    out = {}
    q = ("query($ids:[ID!]){ campaigns(first:50, filterBy:{ids:$ids}){ nodes{ id freqCapLimit "
         "campaignGoal{ goalsConnection{ nodes{ goalType goalValue } } } } } }")
    ids = list(ids)
    try:
        for k in range(0, len(ids), 50):
            for n in gql(q, {"ids": ids[k:k + 50]})["campaigns"]["nodes"]:
                goals = [(x.get("goalType"), x.get("goalValue"))
                         for x in ((n.get("campaignGoal") or {}).get("goalsConnection") or {}).get("nodes") or []]
                out[n["id"]] = {"freq_cap": n.get("freqCapLimit"), "goals": goals}
    except Exception:
        return {}
    return out


def _inventory(gql, ids):
    """Package setup and current CPM bid per campaign. {} on any failure."""
    import datetime as dt
    out, ids = {}, list(ids)
    q = ("query($ids:[ID!]){ campaigns(first:50, filterBy:{ids:$ids}){ nodes{ id "
         "inventory{ isInventoryPackagesStrict inventoryPackages(first:100){ nodes{ id } } } "
         "flights{ nodes{ endTime bidAmount } } } } }")
    now = dt.datetime.now(dt.timezone.utc)
    try:
        for k in range(0, len(ids), 50):
            for n in gql(q, {"ids": ids[k:k + 50]})["campaigns"]["nodes"]:
                inv = n.get("inventory") or {}
                live = [f for f in (n.get("flights") or {}).get("nodes") or []
                        if f.get("endTime") and dt.datetime.fromisoformat(f["endTime"]) >= now]
                out[n["id"]] = {"strict": bool(inv.get("isInventoryPackagesStrict")),
                                "pkgs": {str(p["id"]) for p in (inv.get("inventoryPackages") or {}).get("nodes") or []},
                                "bid": live[0].get("bidAmount") if live else None}
    except Exception:
        return {}
    return out


def _goal_fix(goals, goal):
    types = {t for t, _ in goals}
    if "MAX_CTR" in types or any(t == "CTR" and v for t, v in goals):
        return None
    if "CTR" in types:
        if types & {"CPA", "MIN_CPA"}:
            return ("Set CTR target", f"{goal * 100:.2f}%")
        return ("Switch to Maximize CTR", "CTR target is blank")
    return ("Add CTR goal", "No CTR goal")


def _setting_recs(ids, camps, sets, g, goal, multi):
    """Goal, frequency and viewability recs. Same fix on every campaign -> one
    group line; otherwise one line per campaign."""
    recs = []
    per = {}
    for i in ids:
        c, st = camps[i], sets.get(i)
        if not st:
            continue
        if str(c.get("channel") or "").upper() in CTR_GOAL_CHANNELS:
            fx = _goal_fix(st["goals"], goal)
            if fx:
                per.setdefault(("Goal",) + fx, []).append(i)
        u = c.get("uniques") or 0
        freq = c["impressions"] / u if u else 0
        # No cap on a campaign that's beating goal: it's the one we want to serve
        # more (kiri 2026-11-23, Keystone retargeting).
        if not st["freq_cap"] and freq >= FREQ_MAX and _rate(c["clicks"], c["impressions"]) < goal:
            per.setdefault(("Frequency", "Add frequency cap", "No cap set"), []).append(i)
    for (kind, action, target), cids in per.items():
        cap = None
        if kind == "Frequency":
            fr = max(camps[i]["impressions"] / camps[i]["uniques"] for i in cids)
            rt = all("retarget" in camps[i]["name"].lower() for i in cids)
            cap = FREQ_CAP_RT if rt else FREQ_CAP
            target = f"{fr:.1f} impr per person. Cap {cap} per day"
        base = {"kind": kind, "action": action, "target": target, "tone": "warn", "key": None,
                "impressions": 0, "clicks": 0, "ctr": 0, "share": 0}
        if cap:
            base["cap"] = cap
        if not multi or len(cids) == len(ids):
            recs.append(dict(base, scope="All campaigns" if multi else ""))
        else:
            recs += [dict(base, scope=_short(camps[i]["name"], camps[i]["advertiser"])) for i in cids]
    meas = sum(camps[i].get("measured") or 0 for i in ids)
    if meas >= MIN_MEASURED:
        v = sum(camps[i].get("viewed") or 0 for i in ids) / meas
        if v < VIEW_FLOOR:
            recs.append({"kind": "Viewability", "action": "Add viewability segment",
                         "target": f"{v * 100:.0f}% viewable. DV All Sizes 70%+ (82031170), +$0.13 CPM",
                         "tone": "warn", "key": None, "impressions": 0, "clicks": 0, "ctr": 0,
                         "share": 0, "scope": "All campaigns" if multi else ""})
    return recs


RAISE_AFTER_DAYS = 7


def _now():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def _first_push():
    """group id -> time of the first push logged from the app."""
    import datetime as _dt
    import json as _json
    import tempfile
    path = os.path.join(os.environ.get("REPORTS_DIR", os.path.join(tempfile.gettempdir(), "insights_reports")),
                        "sa_push_log.jsonl")
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = _json.loads(line)
                    t = _dt.datetime.fromisoformat(e["at"])
                except Exception:
                    continue
                gid = str(e.get("group_id") or "")
                if gid and (gid not in out or t < out[gid]):
                    out[gid] = t
    except OSError:
        pass
    return out


RANK_PRODUCTS = {"Video", "Native Video"}


def _pace_guard(recs, g, ids, camps):
    """Bids change media cost and delivery, and groups pace to fixed impression
    goals (kiri 2026-11-23). Bid ups only when the group is behind pace; bid downs
    only when it isn't. A strong retargeting audience points at the retargeting
    campaign instead: Raise CPM when behind; when on pace, Rank first for Video /
    Native Video groups and nothing for click-based groups. Caps and
    exclusions don't move CPM and always stay."""
    p = g.get("pacing") or {}
    behind = bool(p) and (p.get("projected") or 0) < 0.95
    rt = [i for i in ids if "retarget" in camps[i]["name"].lower()]
    rt_short = _short(camps[rt[0]]["name"], camps[rt[0]]["advertiser"]) if len(rt) == 1 else None
    out = []
    for r in recs:
        a = r.get("action") or ""
        if a.startswith("Bid up"):
            if r["kind"] == "Audience" and r.get("aud_type") == "RetargetingPixel" and rt_short:
                # Rank only steers Video / Native Video groups; click-based groups
                # (CBBA) already move impressions to the best-CTR campaign.
                if len(ids) > 1 and (behind or g.get("product") in RANK_PRODUCTS):
                    tot = sum(camps[i]["impressions"] for i in ids) or 1
                    out.append(dict(r, action="Raise CPM" if behind else "Rank first",
                                    scope=rt_short, key=None,
                                    camp_share=camps[rt[0]]["impressions"] / tot))
                continue
            if behind:
                out.append(r)
            continue
        if a.startswith("Bid down") and behind:
            continue
        out.append(r)
    return out


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
    sets = _settings(gql, keep)
    inv = _inventory(gql, keep)
    try:
        import sa_push
        hc = sa_push.high_ctr_packages(gql)
    except Exception:
        hc = {}

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
        recs += _device_recs(_merge(br["DEVICE_TYPE"].get(i) for i in ids), g, goal)
        recs += _slices("OS", _merge(br["DEVICE_OS"].get(i) for i in ids), g, goal)
        recs += _slices("Audience", _merge(br["AUDIENCE"].get(i) for i in ids), g, goal)
        multi = len(ids) > 1
        for r in recs:
            r["scope"] = "All campaigns" if multi else ""

        # Per-campaign recs. A group rec only stays "All in group" when every
        # campaign with enough of that slice to judge points the same way
        # (kiri 2026-11-23); otherwise it's dropped for the per-campaign recs.
        own_by = {}
        for i in ids:
            c = dict(camps[i])
            c["ctr"] = _rate(c["clicks"], c["impressions"])
            own = []
            if multi and c["impressions"] >= MIN_CAMP_IMPR:
                own += _site_recs((camp_sites or {}).get(i) or {}, c["impressions"], c["ctr"], goal)
                own += _device_recs(br["DEVICE_TYPE"].get(i) or {}, c, goal)
                own += _slices("OS", br["DEVICE_OS"].get(i) or {}, c, goal)
                own += _slices("Audience", br["AUDIENCE"].get(i) or {}, c, goal)
            own_by[i] = own
        if multi:
            keep = []
            for r in recs:
                judged = [i for i in ids if camps[i]["impressions"] >= MIN_CAMP_IMPR
                          and _slice_impr(r, i, camp_sites, br) >= MIN_SITE_IMPR]
                agree = [i for i in judged if any(o["kind"] == r["kind"] and o.get("key") == r.get("key")
                                                  and o["tone"] == r["tone"] for o in own_by[i])]
                if len(agree) == len(judged):
                    keep.append(r)
            recs = keep
        covered = {(r["kind"], r.get("key")) for r in recs}

        camp_rows = []
        for i in sorted(ids, key=lambda i: -camps[i]["impressions"]):
            c = dict(camps[i])
            c["ctr"] = _rate(c["clicks"], c["impressions"])
            c["short"] = _short(c["name"], c["advertiser"])
            own = [r for r in own_by[i] if (r["kind"], r.get("key")) not in covered]
            own += _creative_recs(br["AD"].get(i), c)
            for r in own:
                r["scope"] = c["short"] if multi else ""
            recs += own
            camp_rows.append({"id": i, "name": c["short"], "impressions": c["impressions"],
                              "clicks": c["clicks"], "ctr": c["ctr"], "spend": c["spend"],
                              "pct_goal": _rate(c["ctr"], goal)})

        recs += _setting_recs(ids, camps, sets, g, goal, multi)

        # Same "Add sizes" on every campaign -> one group line
        sz = [r for r in recs if r["action"] == "Add sizes"]
        if multi and len(sz) > 1 and len({r["target"] for r in sz}) == 1 and len(sz) == len(ids):
            recs = [r for r in recs if r["action"] != "Add sizes"] + [dict(sz[0], scope="All campaigns")]

        recs = _pace_guard(recs, g, ids, camps)

        # Last resort (kiri 2026-11-23): higher CPMs win better inventory but cut
        # margin, so only once our pushes have had a week and the group is still
        # below goal.
        first = _first_push().get(str(gid))
        if g["status"] == "Below goal" and first and (_now() - first).days >= RAISE_AFTER_DAYS:
            recs.append({"kind": "Bid", "action": "Raise CPM", "tone": "warn", "key": None,
                         "target": f"Still below goal {(_now() - first).days} days after first push. "
                                   "Bid for better inventory",
                         "impressions": 0, "clicks": 0, "ctr": 0, "share": 0,
                         "scope": "All campaigns" if multi else "", "camp_share": 1.0})

        # High CTR inventory (kiri 2026-11-23): on pace, switch the campaign to SA's
        # High CTR packages; behind, copy it and switch the copy, so delivery holds.
        if g["status"] == "Below goal" and hc:
            behind = bool(g.get("pacing")) and (g["pacing"].get("projected") or 0) < 0.95
            for i in ids:
                ch = str(camps[i].get("channel") or "").upper()
                pk = hc.get(ch) or []
                st = inv.get(i)
                if not pk or st is None or (st["strict"] and st["pkgs"] & {p["id"] for p in pk}):
                    continue
                lo = min(p["bid"][0] for p in pk if p["bid"][0] is not None)
                hi = max(p["bid"][1] for p in pk if p["bid"][1] is not None)
                bid = f" vs ${st['bid']:.2f} CPM" if st.get("bid") else ""
                # Retargeting pools are small: always a copy, never an edit (kiri 2026-11-23).
                copy = behind or "retarget" in camps[i]["name"].lower()
                recs.append({"kind": "Inventory",
                             "action": "Copy with High CTR inventory" if copy else "Add High CTR inventory",
                             "target": f"{len(pk)} US High CTR packages. Bid guidance ${lo:g}-${hi:g}{bid}",
                             "tone": "warn", "key": None, "impressions": 0, "clicks": 0, "ctr": 0, "share": 0,
                             "scope": _short(camps[i]["name"], camps[i]["advertiser"]) if multi else ""})

        # On goal: only the upside
        if g["status"] == "On goal":
            recs = [r for r in recs if r["tone"] == "ok"]
        order = {"Goal": -1, "Site": 0, "Device": 1, "OS": 2, "Audience": 3, "Creative": 4,
                 "Frequency": 5, "Viewability": 6, "Inventory": 6.5, "Bid": 7}
        recs.sort(key=lambda r: (r["scope"] not in ("", "All campaigns"), order.get(r["kind"], 9)))
        by_short = {_short(camps[i]["name"], camps[i]["advertiser"]): i for i in ids}
        for r in recs:
            wide = r["scope"] in ("", "All campaigns")
            cids = list(ids) if wide else [by_short.get(r["scope"])]
            if wide and r["kind"] == "Audience":
                # Only campaigns that serve this audience; SA rejects a bid factor
                # for an audience the campaign doesn't target.
                cids = [i for i in ids if r.get("key") in (br["AUDIENCE"].get(i) or {})] or cids
            r["push"] = push_spec(r, gid, [c for c in cids if c], wide) if all(cids) else None
            r["rid"] = hashlib.sha1("|".join(str(x) for x in (gid, r["scope"], r["kind"], r.get("key"),
                                                               r["action"])).encode()).hexdigest()[:12]
        g["recs"], g["campaigns"] = recs, camp_rows
        out.append(g)

    out.sort(key=lambda g: (g["status"] != "Below goal", g["pct_goal"], -g["impressions"]))
    return {"groups": out,
            "summary": {"groups": len(out), "below": sum(1 for g in out if g["status"] == "Below goal"),
                        "recs": sum(len(g["recs"]) for g in out)}}

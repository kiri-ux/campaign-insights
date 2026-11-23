"""Push a Campaign recs row to StackAdapt (write calls). Uses STACKADAPT_TOKEN
(the write key on adlib-insights); STACKADAPT_WRITE_TOKEN overrides it if set.

Every write reads the current value first and adds to it, so nothing already set
in StackAdapt is dropped:
  group_exclude: campaign group domainExclusions += domains (updateCampaignGroup)
  bid_factor:    campaign bidFactors += one factor named "Insights: ..." (upsertCampaign);
                 a factor with the same name is updated instead of duplicated
  device_caps:   campaign inventory.deviceCaps lowered (upsertCampaign)
  pause_ads:     pauseAds
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

URL = "https://api.stackadapt.com/graphql"
CHANNEL_KEY = {"DISPLAY": "display", "NATIVE": "native", "VIDEO": "video", "CTV": "ctv",
               "AUDIO": "audio", "DOOH": "dooh"}
# Rule fields we can round-trip when re-sending a campaign's existing bid factors.
RULES = {
    "deviceTypeRule": "deviceTypeRule { deviceTypes }",
    "deviceOsRule": "deviceOsRule { deviceOses }",
    "supplyTypeRule": "supplyTypeRule { supplyTypes }",
    "domainRule": "domainRule { domains }",
    "customSegmentRule": "customSegmentRule { customSegments { nodes { id } } }",
    "retargetingPixelRule": "retargetingPixelRule { retargetingPixels { nodes { id } } }",
}


def token():
    return (os.environ.get("STACKADAPT_WRITE_TOKEN") or os.environ.get("STACKADAPT_TOKEN") or "").strip()


def configured():
    return bool(token())


def _gql(query, variables=None):
    req = urllib.request.Request(URL, json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    for attempt in range(5):
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
        errs = body.get("errors") or []
        if errs and "rate limit" in str(errs[0].get("message", "")).lower() and attempt < 4:
            time.sleep(5 * (attempt + 1))
            continue
        if errs:
            raise RuntimeError(errs[0].get("message") or "StackAdapt error")
        return body["data"]


def _user_errors(payload):
    errs = (payload or {}).get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(str(e.get("message") or e) for e in errs))


def _group_exclude(spec):
    """Add exclusions to the campaign group's Domain Exclusion List. SA copies the
    group list onto each campaign and can replace their own lists, so campaign
    entries that drop off are put back after."""
    gid = spec["group_id"]
    cq = ("query($g:[ID!]){ campaigns(first:100, filterBy:{campaignGroupIds:$g}){ nodes{ id channelType "
          "inventory{ domainExclusions } } } }")
    g = _gql("query($ids:[ID!]){ campaignGroups(first:1, filterBy:{ids:$ids}){ nodes{ id domainExclusions } } }",
             {"ids": [gid]})["campaignGroups"]["nodes"]
    if not g:
        raise RuntimeError(f"Campaign group {gid} not found")
    cur = [x for x in (g[0].get("domainExclusions") or []) if x]
    have = {_norm_excl(x) for x in cur}
    add = [sa_exclusion(d) for d in spec["domains"] if _norm_excl(d) not in have]
    if not add:
        return "Already on the campaign group's exclusion list"
    before = {c["id"]: [x for x in ((c.get("inventory") or {}).get("domainExclusions") or []) if x]
              for c in _gql(cq, {"g": [gid]})["campaigns"]["nodes"]}
    out = _gql("""mutation($i: CampaignGroupInput!){ updateCampaignGroup(input:$i){
        campaignGroup{ id domainExclusions } userErrors{ message } } }""",
               {"i": {"id": gid, "domainExclusions": cur + add}})["updateCampaignGroup"]
    _user_errors(out)
    saved = {_norm_excl(x) for x in ((out.get("campaignGroup") or {}).get("domainExclusions") or [])}
    if not all(_norm_excl(d) in saved for d in add):
        raise RuntimeError("StackAdapt didn't keep the exclusion on the campaign group")
    for c in _gql(cq, {"g": [gid]})["campaigns"]["nodes"]:
        now = [x for x in ((c.get("inventory") or {}).get("domainExclusions") or []) if x]
        seen = {_norm_excl(x) for x in now}
        lost = [x for x in before.get(c["id"], []) if _norm_excl(x) not in seen]
        if not lost:
            continue
        ch = CHANNEL_KEY.get(str(c.get("channelType") or "").upper())
        if not ch:
            raise RuntimeError(f"Campaign {c['id']}: unsupported channel {c.get('channelType')}")
        r = _gql("""mutation($i: CampaignInput!){ upsertCampaign(input:$i){
            campaign{ id } userErrors{ message } } }""",
                 {"i": {ch: {"id": c["id"], "inventory": {"domainExclusions": now + lost}}}})
        _user_errors(r["upsertCampaign"])
    return f"Excluded on the campaign group ({len(cur) + len(add)} exclusions)"


def sa_exclusion(v):
    """Entry in the form SA's UI uses: bare domain, or the app's store URL.
    Takes SA report values (storeurl/..., bundle/..., name/...), app:<id>, or a domain."""
    raw = str(v or "").strip()
    low = raw.lower()
    if low.startswith("name/"):
        raise RuntimeError("SA only reports this app by name, so it can't be excluded by URL")
    if low.startswith(("bundle/", "app:")):
        bid = raw.split("/", 1)[1] if low.startswith("bundle/") else raw[4:]
        if bid.isdigit():
            return "https://itunes.apple.com/app/id" + bid
        return "https://play.google.com/store/apps/details?id=" + bid
    k = _norm_excl(raw)
    if k.startswith("ios:"):
        return "https://itunes.apple.com/app/id" + k[4:]
    if k.startswith("android:"):
        return "https://play.google.com/store/apps/details?id=" + k[8:]
    if low.startswith("storeurl/"):
        u = raw[9:]
        return u if re.match(r"https?://", u) else "https://" + u
    return k


def _norm_excl(v):
    """Compare exclusions loosely: SA rewrites app entries to itunes/play URLs."""
    v = str(v).strip().lower().removeprefix("storeurl/")
    for p in ("bundle/", "app:"):
        if v.startswith(p):
            b = v[len(p):]
            return ("ios:" if b.isdigit() else "android:") + b
    for p in ("https://", "http://", "www."):
        v = v.removeprefix(p)
    m = re.search(r"apple\.com/.*?id(\d+)", v)
    if m:
        return "ios:" + m.group(1)
    m = re.search(r"play\.google\.com/.*?id=([\w.]+)", v)
    if m:
        return "android:" + m.group(1)
    return v


def _rule_in(rule_name, val):
    """Turn a read rule back into its input shape."""
    if not val:
        return None
    if rule_name == "customSegmentRule":
        return {"customSegmentIds": [x["id"] for x in (val.get("customSegments") or {}).get("nodes") or []]}
    if rule_name == "retargetingPixelRule":
        return {"retargetingPixelIds": [x["id"] for x in (val.get("retargetingPixels") or {}).get("nodes") or []]}
    return val


def _bid_factor(spec, name):
    done = []
    for cid in spec["campaign_ids"]:
        q = "query($ids:[ID!]){ campaigns(first:1, filterBy:{ids:$ids}){ nodes{ id channelType bidFactors{ nodes{ id name factorPercent paused %s } } } } }" \
            % " ".join(RULES.values())
        nodes = _gql(q, {"ids": [cid]})["campaigns"]["nodes"]
        if not nodes:
            raise RuntimeError(f"Campaign {cid} not found")
        c = nodes[0]
        ch = CHANNEL_KEY.get(str(c.get("channelType") or "").upper())
        if not ch:
            raise RuntimeError(f"Campaign {cid}: unsupported channel {c.get('channelType')}")
        factors = []
        replaced = False
        for f in (c.get("bidFactors") or {}).get("nodes") or []:
            item = {"id": f["id"], "name": f.get("name"), "factorPercent": f.get("factorPercent"),
                    "paused": bool(f.get("paused"))}
            for rn in RULES:
                v = _rule_in(rn, f.get(rn))
                if v:
                    item[rn] = v
            if f.get("name") == name:
                item.update(spec["rule"])
                item["factorPercent"] = spec["factor"]
                item["paused"] = False
                replaced = True
            factors.append(item)
        if not replaced:
            factors.append(dict(spec["rule"], name=name, factorPercent=spec["factor"], paused=False))
        out = _gql("""mutation($i: CampaignInput!){ upsertCampaign(input:$i){
            campaign{ id } userErrors{ message } } }""", {"i": {ch: {"id": cid, "bidFactors": factors}}})
        _user_errors(out["upsertCampaign"])
        done.append(cid)
    return f"Bid factor {spec['factor']}% set on {len(done)} {'campaign' if len(done) == 1 else 'campaigns'}"


def _device_caps(spec):
    """Lower SA's supply caps (max share of impressions; 0 = none). Each cap only
    ever goes down from what's set."""
    done, lower = [], []
    for cid in spec["campaign_ids"]:
        nodes = _gql("query($ids:[ID!]){ campaigns(first:1, filterBy:{ids:$ids}){ nodes{ id channelType "
                     "inventory{ deviceCaps{ app desktop web } } } } }", {"ids": [cid]})["campaigns"]["nodes"]
        if not nodes:
            raise RuntimeError(f"Campaign {cid} not found")
        c = nodes[0]
        ch = CHANNEL_KEY.get(str(c.get("channelType") or "").upper())
        if not ch:
            raise RuntimeError(f"Campaign {cid}: unsupported channel {c.get('channelType')}")
        cur = (c.get("inventory") or {}).get("deviceCaps") or {"app": 1, "desktop": 1, "web": 1}
        caps = {k: float(cur.get(k, 1)) for k in ("app", "desktop", "web")}
        if all(caps[k] <= float(v) for k, v in spec["caps"].items()):
            lower.append(cid)
            continue
        for k, v in spec["caps"].items():
            caps[k] = min(caps[k], float(v))
        out = _gql("""mutation($i: CampaignInput!){ upsertCampaign(input:$i){
            campaign{ id } userErrors{ message } } }""", {"i": {ch: {"id": cid, "inventory": {"deviceCaps": caps}}}})
        _user_errors(out["upsertCampaign"])
        done.append(cid)
    names = {"desktop": "Desktop", "web": "Mobile web", "app": "In-app"}
    what = ", ".join(f"{names[k]} {round(v * 100)}%" for k, v in spec["caps"].items())
    msg = f"Caps set ({what}) on {len(done)} {'campaign' if len(done) == 1 else 'campaigns'}"
    if lower:
        msg += f", {len(lower)} already lower"
    return msg


def _pause_ads(spec):
    out = _gql("""mutation($i: PauseAdsInput!){ pauseAds(input:$i){ pausedCount userErrors{ message } } }""",
               {"i": {"adIds": spec["ad_ids"]}})["pauseAds"]
    _user_errors(out)
    return f"Paused {out.get('pausedCount') or len(spec['ad_ids'])} ad"


def push(rec):
    """Run one rec's push spec. Returns a short result string; raises on failure."""
    spec = rec.get("push")
    if not spec:
        raise RuntimeError("This rec can't be pushed")
    if not configured():
        raise RuntimeError("Add STACKADAPT_TOKEN to the adlib-insights service on Render")
    t = spec["type"]
    if t == "group_exclude":
        return _group_exclude(spec)
    if t == "bid_factor":
        name = f"Insights: {rec.get('kind')} {rec.get('target')}"[:120]
        return _bid_factor(spec, name)
    if t == "device_caps":
        return _device_caps(spec)
    if t == "pause_ads":
        return _pause_ads(spec)
    raise RuntimeError(f"Unknown push type {t}")


def exclusion_entry(kind, val):
    """Block-list value -> StackAdapt domainExclusions entry, or None.
    Sites go in as bare domains. Apps use SA's store-URL form (seen on existing
    group exclusions): iOS numeric ids and Android bundle ids."""
    v = str(val or "").strip()
    if not v or " " in v:
        return None
    if kind == "site":
        return v.lower().removeprefix("www.")
    if v.isdigit():
        return f"storeurl/apps.apple.com/us/app/id{v}"
    if "." in v:
        return f"storeurl/play.google.com/store/apps/details?id={v}"
    return None


def _split(vals):
    out = []
    for x in vals or []:
        out.extend(s.strip() for s in str(x).split("\n") if s.strip())
    return out


def block_advertisers(entries):
    """Add entries to every StackAdapt advertiser's domain exclusions (read,
    merge, write; nothing already there is dropped). SA's account-wide Global
    Exclusion List isn't in the API, so the advertiser level is the widest one
    we can write. Returns {"advertisers": n_updated, "total": n, "added": k}."""
    entries = [e for e in dict.fromkeys(entries) if e]
    if not entries:
        raise RuntimeError("Nothing StackAdapt can take")
    if not configured():
        raise RuntimeError("Add STACKADAPT_TOKEN to the adlib-insights service on Render")
    advs, after = [], None
    while True:
        page = _gql("query($a:String){ advertisers(first:100, after:$a){ nodes{ id domainExclusions } "
                    "pageInfo{ hasNextPage endCursor } } }", {"a": after})["advertisers"]
        advs += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    updated = 0
    for a in advs:
        cur = _split(a.get("domainExclusions"))
        have = {c.lower() for c in cur}
        add = [e for e in entries if e.lower() not in have]
        if not add:
            continue
        out = _gql("""mutation($i: AdvertiserInput!){ updateAdvertiser(input:$i){
            advertiser{ id } userErrors{ message } } }""",
                   {"i": {"id": a["id"], "domainExclusions": cur + add}})["updateAdvertiser"]
        _user_errors(out)
        updated += 1
    return {"advertisers": updated, "total": len(advs), "added": len(entries)}

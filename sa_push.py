"""Push a Campaign recs row to StackAdapt (write calls). Uses STACKADAPT_TOKEN
(the write key on adlib-insights); STACKADAPT_WRITE_TOKEN overrides it if set.

Every write reads the current value first and adds to it, so nothing already set
in StackAdapt is dropped:
  group_exclude: campaign group domainExclusions += domains (updateCampaignGroup)
  bid_factor:    campaign bidFactors += one factor named "Insights: ..." (upsertCampaign);
                 a factor with the same name is updated instead of duplicated
  pause_ads:     pauseAds
"""
import json
import os
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
    gid = spec["group_id"]
    g = _gql("query($ids:[ID!]){ campaignGroups(first:1, filterBy:{ids:$ids}){ nodes{ id domainExclusions } } }",
             {"ids": [gid]})["campaignGroups"]["nodes"]
    if not g:
        raise RuntimeError(f"Campaign group {gid} not found")
    cur = [x for x in (g[0].get("domainExclusions") or []) if x]
    add = [d for d in spec["domains"] if d.lower() not in {c.lower() for c in cur}]
    if not add:
        return "Already excluded"
    out = _gql("""mutation($i: CampaignGroupInput!){ updateCampaignGroup(input:$i){
        campaignGroup{ id domainExclusions } userErrors{ message } } }""",
               {"i": {"id": gid, "domainExclusions": cur + add}})["updateCampaignGroup"]
    _user_errors(out)
    return f"Excluded on the campaign group ({len(cur) + len(add)} exclusions)"


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
    if t == "pause_ads":
        return _pause_ads(spec)
    raise RuntimeError(f"Unknown push type {t}")

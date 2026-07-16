"""
graph_pull.py
Read the newest Insights .xlsx from one Outlook/M365 mailbox via Microsoft Graph
using app-only auth (client credentials). No IMAP, no app password, no user
sign-in — the app registration is granted read access to a single mailbox.

Env vars:
  GRAPH_TENANT_ID      Directory (tenant) ID
  GRAPH_CLIENT_ID      Application (client) ID
  GRAPH_CLIENT_SECRET  client secret value
  GRAPH_MAILBOX        the mailbox address that receives the export
  GRAPH_SUBJECT        optional subject substring filter
  GRAPH_FROM           optional sender substring filter
"""
import os
import json
import base64
import datetime
import urllib.parse
import urllib.request

_AUTH = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH = "https://graph.microsoft.com/v1.0"


def _get_token(tenant, cid, secret, timeout=30):
    data = urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(_AUTH.format(tenant=tenant), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())["access_token"]


def _graph_get(url, token, timeout=30):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _pick_message(messages, subj, frm):
    """First message (already newest-first) matching optional subject/sender."""
    subj, frm = (subj or "").lower(), (frm or "").lower()
    for m in messages:
        s = (m.get("subject") or "").lower()
        sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        if subj and subj not in s:
            continue
        if frm and frm not in sender:
            continue
        return m
    return None


def _pick_xlsx(attachments):
    """Return (name, bytes) of the first .xlsx attachment, or None."""
    for a in attachments:
        name = a.get("name") or ""
        if name.lower().endswith(".xlsx") and a.get("contentBytes"):
            return name, base64.b64decode(a["contentBytes"])
    return None


def fetch_latest_xlsx():
    """Return (filename, bytes, date_str) or (None, None, None). Raises on auth/API error."""
    tenant = os.environ.get("GRAPH_TENANT_ID", "").strip()
    cid = os.environ.get("GRAPH_CLIENT_ID", "").strip()
    secret = os.environ.get("GRAPH_CLIENT_SECRET", "").strip()
    mailbox = os.environ.get("GRAPH_MAILBOX", "").strip()
    if not all([tenant, cid, secret, mailbox]):
        raise RuntimeError("GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET / GRAPH_MAILBOX not all set")
    token = _get_token(tenant, cid, secret)

    params = {"$filter": "hasAttachments eq true",
              "$orderby": "receivedDateTime desc",
              "$top": "25",
              "$select": "id,subject,from,receivedDateTime"}
    url = f"{_GRAPH}/users/{urllib.parse.quote(mailbox)}/messages?" + \
        urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    msgs = _graph_get(url, token).get("value", [])

    subj = os.environ.get("GRAPH_SUBJECT", "")
    frm = os.environ.get("GRAPH_FROM", "")
    # try newest matching first, then fall back to scanning others for an .xlsx
    ordered = []
    top = _pick_message(msgs, subj, frm)
    if top:
        ordered.append(top)
    ordered += [m for m in msgs if m is not top]

    for m in ordered:
        if subj and subj.lower() not in (m.get("subject") or "").lower():
            continue
        if frm and frm.lower() not in (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower():
            continue
        aurl = f"{_GRAPH}/users/{urllib.parse.quote(mailbox)}/messages/{m['id']}/attachments?$select=name,contentBytes,contentType"
        atts = _graph_get(aurl, token).get("value", [])
        picked = _pick_xlsx(atts)
        if picked:
            fn, data = picked
            try:
                dt = datetime.datetime.fromisoformat((m.get("receivedDateTime") or "").replace("Z", "+00:00"))
                date_str = dt.date().strftime("%Y-%m-%d")
            except Exception:
                date_str = datetime.date.today().strftime("%Y-%m-%d")
            return fn, data, date_str
    return None, None, None

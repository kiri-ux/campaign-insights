"""
mailbox_pull.py
Pull the newest Insights .xlsx straight from an Outlook/M365 mailbox over IMAP,
so no third-party middleman (Zapier/Power Automate) has to hand over the file.

Env vars:
  IMAP_HOST      default 'outlook.office365.com'
  IMAP_USER      the mailbox address
  IMAP_PASSWORD  an APP PASSWORD for that mailbox (not the normal password)
  IMAP_FOLDER    default 'INBOX'
  IMAP_FROM      optional sender filter (e.g. 'reporting.zone')
  IMAP_SUBJECT   optional subject filter (e.g. 'Insights')
"""
import os
import email
import imaplib
import datetime


def _search_criteria():
    crit = []
    frm = os.environ.get("IMAP_FROM", "").strip()
    subj = os.environ.get("IMAP_SUBJECT", "").strip()
    if frm:
        crit += ["FROM", frm]
    if subj:
        crit += ["SUBJECT", subj]
    if not crit:
        since = (datetime.date.today() - datetime.timedelta(days=4)).strftime("%d-%b-%Y")
        crit = ["SINCE", since]
    return crit


def _extract_xlsx(msg):
    """Return (filename, bytes, date_str) for the first .xlsx attachment, or None."""
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        date_str = dt.date().strftime("%Y-%m-%d")
    except Exception:
        date_str = datetime.date.today().strftime("%Y-%m-%d")
    for part in msg.walk():
        fn = part.get_filename()
        if fn and fn.lower().endswith(".xlsx"):
            payload = part.get_payload(decode=True)
            if payload:
                return fn, payload, date_str
    return None


def fetch_latest_xlsx():
    """Log in, find the newest matching email with an .xlsx, return
    (filename, bytes, date_str) or (None, None, None). Raises on conn/login error."""
    host = os.environ.get("IMAP_HOST", "outlook.office365.com")
    user = os.environ.get("IMAP_USER", "").strip()
    pw = os.environ.get("IMAP_PASSWORD", "").strip()
    folder = os.environ.get("IMAP_FOLDER", "INBOX")
    if not (user and pw):
        raise RuntimeError("IMAP_USER / IMAP_PASSWORD not set")
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, pw)
        M.select(folder)
        typ, data = M.search(None, *_search_criteria())
        ids = data[0].split() if data and data[0] else []
        if not ids:
            return None, None, None
        # newest first; scan the most recent 25 for one carrying an .xlsx
        for eid in reversed(ids[-25:]):
            typ, msgdata = M.fetch(eid, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            got = _extract_xlsx(msg)
            if got:
                return got
        return None, None, None
    finally:
        try:
            M.logout()
        except Exception:
            pass

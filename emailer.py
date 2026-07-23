"""
emailer.py
Send the weekly insights email via AWS SES (you're already in AWS; boto3 is
already a dependency, and SES uses IAM auth — no SMTP basic-auth wall).

Env vars:
  EMAIL_FROM        verified SES sender, e.g. 'insights@vicimediainc.com'
  EMAIL_TO          comma-separated recipients
  AWS_SES_REGION    SES region (default: AWS_REGION, else 'us-east-1')
  (AWS creds come from the same env vars used for S3)
"""
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication


def configured():
    return bool(os.environ.get("EMAIL_FROM", "").strip()
                and os.environ.get("EMAIL_TO", "").strip())


def send_email(subject, html_body, attachment=None, attachment_name="watchlists.xlsx",
               extra_attachments=None):
    """Send an HTML email, optionally with attachments. `attachment` is the
    original single-file path (bytes); `extra_attachments` is an optional list
    of (bytes, filename) tuples appended after it. Returns SES msg id."""
    import boto3
    sender = os.environ["EMAIL_FROM"].strip()
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",") if r.strip()]
    region = (os.environ.get("AWS_SES_REGION") or os.environ.get("AWS_REGION") or "us-east-1").strip()

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    # Stable custom header so IT can add an Exchange/Gmail mail-flow rule
    # ("header X-Adtini-Insights exists -> never junk") that survives filter
    # heuristics — reporting 'not junk' alone doesn't stick for SES mail
    # sent as the recipient's own address.
    msg["X-Adtini-Insights"] = "weekly"
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)
    parts = []
    if attachment is not None:
        parts.append((attachment, attachment_name))
    for data, name in (extra_attachments or []):
        if data:
            parts.append((data, name))
    for data, name in parts:
        part = MIMEApplication(data)
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)

    ses = boto3.client("ses", region_name=region)
    resp = ses.send_raw_email(Source=sender, Destinations=recipients,
                              RawMessage={"Data": msg.as_string()})
    return resp.get("MessageId")

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


def send_email(subject, html_body, attachment=None, attachment_name="watchlists.xlsx"):
    """Send an HTML email, optionally with one attachment (bytes). Returns SES msg id."""
    import boto3
    sender = os.environ["EMAIL_FROM"].strip()
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",") if r.strip()]
    region = (os.environ.get("AWS_SES_REGION") or os.environ.get("AWS_REGION") or "us-east-1").strip()

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)
    if attachment is not None:
        part = MIMEApplication(attachment)
        part.add_header("Content-Disposition", "attachment", filename=attachment_name)
        msg.attach(part)

    ses = boto3.client("ses", region_name=region)
    resp = ses.send_raw_email(Source=sender, Destinations=recipients,
                              RawMessage={"Data": msg.as_string()})
    return resp.get("MessageId")

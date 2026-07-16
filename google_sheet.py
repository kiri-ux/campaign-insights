"""
google_sheet.py  (optional)
Upload the 3-tab watchlists workbook to Google Drive AS a native Google Sheet
(Drive converts xlsx -> Sheet, keeping the three tabs), and return its link.

Only used if a service account is configured. If not, the app just attaches the
xlsx to the email instead.

Env vars:
  GOOGLE_SA_JSON      the service-account key JSON (paste the whole JSON as the value)
  GDRIVE_FOLDER_ID    optional Drive folder to place the sheet in (share it with the
                      service account's client_email)
  GSHEET_SHARE_EMAIL  optional: also share the sheet with this person (writer)
  GSHEET_ANYONE_LINK  '1' to make it viewable by anyone with the link
"""
import os
import io
import json


def configured():
    return bool(os.environ.get("GOOGLE_SA_JSON", "").strip())


def upload_as_sheet(xlsx_bytes, title):
    """Create a native Google Sheet from xlsx bytes; return its webViewLink."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload

    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    meta = {"name": title, "mimeType": "application/vnd.google-apps.spreadsheet"}
    folder = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    if folder:
        meta["parents"] = [folder]
    media = MediaIoBaseUpload(
        io.BytesIO(xlsx_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=False)
    f = drive.files().create(body=meta, media_body=media,
                             fields="id,webViewLink", supportsAllDrives=True).execute()

    share_email = os.environ.get("GSHEET_SHARE_EMAIL", "").strip()
    if share_email:
        try:
            drive.permissions().create(
                fileId=f["id"], sendNotificationEmail=False,
                body={"type": "user", "role": "writer", "emailAddress": share_email},
                supportsAllDrives=True).execute()
        except Exception:
            pass
    if os.environ.get("GSHEET_ANYONE_LINK", "").strip() == "1":
        try:
            drive.permissions().create(
                fileId=f["id"], body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True).execute()
        except Exception:
            pass
    return f.get("webViewLink")

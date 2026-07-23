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


def _apply_checkbox_columns(creds, spreadsheet_id, header_names):
    """Apply BOOLEAN data validation (checkbox rendering) to every column whose
    header matches one of `header_names` (case-insensitive), on every tab."""
    from googleapiclient.discovery import build
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount)))").execute()
    tabs = [sh["properties"] for sh in meta.get("sheets", [])]
    if not tabs:
        return
    headers = sheets.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=[f"'{t['title']}'!1:1" for t in tabs]).execute()
    wanted = {h.lower() for h in header_names}
    requests = []
    for t, vr in zip(tabs, headers.get("valueRanges", [])):
        row = (vr.get("values") or [[]])[0]
        for j, h in enumerate(row):
            if str(h).strip().lower() in wanted:
                requests.append({"repeatCell": {
                    "range": {"sheetId": t["sheetId"], "startRowIndex": 1,
                              "endRowIndex": t.get("gridProperties", {}).get("rowCount", 1000),
                              "startColumnIndex": j, "endColumnIndex": j + 1},
                    "cell": {"dataValidation": {"condition": {"type": "BOOLEAN"}}},
                    "fields": "dataValidation"}})
    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()


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

    # Render buyer_review columns as real checkboxes: Sheets shows a checkbox
    # wherever a cell holds a boolean AND carries BOOLEAN data validation, so
    # find the column on each tab and apply the rule. Best-effort — the link is
    # returned even if this decoration fails.
    try:
        _apply_checkbox_columns(creds, f["id"], ("buyer_review",))
    except Exception:
        pass

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

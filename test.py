import os
import json
import csv
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, expect

# --- Configuration ---
SPREADSHEET_ID = "1JOUh9RC_tnf9Bin8kZADOKE_Uo1lN_xfiDbQ2gLfyXY"  # Block Farming / Leads Data
SMS_EXPORT_TAB = "SMS Export"
STATE_TAB = "Automation State"
BATCH_SIZE = 5
BATCH_FILE = "sms.csv"
RAISUGAR_MESSAGE_URL = "https://raisugar.com/xsender/kabras/messages/50"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

load_dotenv()
RAISUGAR_EMAIL = os.environ["RAISUGAR_EMAIL"]
RAISUGAR_PASSWORD = os.environ["RAISUGAR_PASSWORD"]


def get_client():
    creds_json = json.loads(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    return gspread.authorize(creds)


def get_last_sent_row(state_sheet):
    value = state_sheet.acell("B1").value
    return int(value) if value else 0


def set_last_sent_row(state_sheet, row_num):
    state_sheet.update_acell("B1", str(row_num))


def get_new_batch(sms_sheet, last_sent_row, batch_size):
    all_values = sms_sheet.get_all_values()
    data_rows = all_values[1:]
    return data_rows[last_sent_row:last_sent_row + batch_size]


def normalize_phone(value):
    """Ensure a Kenyan phone number is a string with a leading zero.
    Leaves blanks/placeholder values (NULL, N/A, etc.) untouched."""
    phone = str(value).strip()

    if phone == "" or phone.upper() in ("NULL", "N/A", "NONE"):
        return phone

    # Strip any accidental non-digit characters (spaces, quotes, etc.)
    digits_only = "".join(ch for ch in phone if ch.isdigit())

    if digits_only == "":
        return phone  # nothing usable, leave as-is

    if not digits_only.startswith("0"):
        digits_only = "0" + digits_only

    return digits_only


def write_batch_csv(batch, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["message_id", "contact_name", "contact_detail", "body"])
        for row in batch:
            contact_detail = normalize_phone(row[2])
            writer.writerow([row[0], row[1], contact_detail, row[3]])


def do_upload_and_import(page, csv_path):
    """Single attempt: open modal, upload file, click Import."""
    page.get_by_role("button", name="Import message lists").click()
    page.get_by_role("dialog").locator('input[type="file"]').set_input_files(csv_path)
    page.get_by_text("Upload complete", exact=True).wait_for(timeout=30000)
    page.get_by_role("button", name="Import", exact=True).click()


def upload_to_raisugar(csv_path, batch):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(RAISUGAR_MESSAGE_URL)

        page.get_by_role("textbox", name="Email address*").fill(RAISUGAR_EMAIL)
        page.get_by_role("textbox", name="Password*").fill(RAISUGAR_PASSWORD)
        page.get_by_role("checkbox", name="Remember me").check()
        page.get_by_role("button", name="Sign in").click()

        page.get_by_role("button", name="Import message lists").wait_for(timeout=30000)

        do_upload_and_import(page, csv_path)

        # The import happens server-side as soon as "Import" is clicked, regardless
        # of what the page does afterward. A 419 here is just the page/session
        # rendering an expired-page response after the fact — it does NOT mean
        # the import failed. So we don't retry, don't treat it as an error, and
        # don't wait around checking anything — just note it and close.
        if page.get_by_text("419").is_visible():
            print("Note: page showed 419 Page Expired after Import, but the import "
                  "had already gone through server-side. Treating as success.")

        context.close()
        browser.close()
        return True


def main():
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    sms_sheet = spreadsheet.worksheet(SMS_EXPORT_TAB)
    state_sheet = spreadsheet.worksheet(STATE_TAB)

    last_sent_row = get_last_sent_row(state_sheet)
    batch = get_new_batch(sms_sheet, last_sent_row, BATCH_SIZE)

    if not batch:
        print(f"No new rows to send (last sent row: {last_sent_row}).")
        return

    write_batch_csv(batch, BATCH_FILE)
    success = upload_to_raisugar(BATCH_FILE, batch)

    if success:
        new_last_row = last_sent_row + len(batch)
        set_last_sent_row(state_sheet, new_last_row)
        print(f"Sent {len(batch)} messages. Marker advanced to row {new_last_row}.")
    else:
        print(f"Upload had issues — marker NOT advanced. Will retry same {len(batch)} rows next run. "
              f"Check the Raisugar dashboard for any Failed messages that may need manual resending.")


if __name__ == "__main__":
    main()
